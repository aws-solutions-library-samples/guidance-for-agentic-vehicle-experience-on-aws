"""AVX Broker Lambda — channel-policy chokepoint for all customer-facing pushes.

This Lambda is the **sole** permitted caller of ``sns:Publish`` anywhere under
``agents/`` or ``lambdas/``.  ``tests/architecture/test_no_direct_sns_publish.py``
enforces this structurally (AST-walk, Group 5 task).  Every outbound push to a
customer — new AVX Finding card or silent badge-update — must flow through here.

Subscribed to the ``vsa-{stage}-avx-findings`` DynamoDB stream (NEW_IMAGE).
For each new or updated Finding, after the owner and namespace checks and the
notify-once check (``already_carded``), the broker applies four gates in order.
The three reads come first and the only write comes last, so that no skip ever
consumes rate budget:

  1. **Cardinality cap** — total in-app cards per owner capped at a stage-scoped
     max (``cardinality-cap`` SSM param).
  2. **Consent gate** — per-category notification consent stored in
     ``notification-devices``, one device per owner (D-SCOPE-1).  Consent is
     keyed on ``finding_kind`` prefix (``health.*`` → ``safety``,
     ``coverage.*`` / ``energy.*`` → ``coverage``, everything else → ``service``).
  3. **Feedback demotion** — trailing 30-day dismissed-finding signal from
     ``avx-feedback``.  Demotion is **owner-scoped only** — keyed on
     ``(owner_id, finding_kind)`` where ``owner_id`` is a Cognito ``sub``.  It
     does not block the card; it shifts the APNs threshold one step more
     severe, clamped at ``p0`` (D-G9-1).
  4. **Rate limit** — per-owner daily and hourly counters in ``avx-rate-limits``.

A DynamoDB failure in any gate raises ``BrokerTransientError`` and becomes a
batch item failure (retry, then DLQ), never a policy outcome.  The demotion
read is the one exception: its failure is ``demotion_check_failed`` and the
push decision proceeds (settled in G7 review Cycle 3, W1).

Namespace mismatch detection (decisions.md § 2026-09-23)
----------------------------------------------------------
The rate-limit, cardinality-cap, consent, and in-app-card write paths all key
on a Cognito ``sub`` (UUID).  A Finding's party-id fields (``scope.owner_id``
and top-level ``owner_id``) carry a CMS ``CUST-XXXXXXXX`` registry ID — a
different namespace that cannot match any Cognito-keyed row.

Because DynamoDB returns ``Count: 0`` (not an exception) when a key is absent,
routing a ``CUST-*`` value through these gates would **fail open** on the
cardinality cap (``0 < cap = True``), **write orphaned rows** to the rate-limits
table, and produce Action rows whose ``created_by`` field no owner can ever
dismiss (the dismiss route matches against the Cognito sub).

To prevent all of this, the broker detects the namespace mismatch **once, up
front**, before any gate runs or any counter is incremented, and returns
immediately with ``skip_reason="owner_namespace_unmatched"``.  This reason is
kept distinct from ``owner_absent`` so both are independently observable via
CloudWatch metrics.

Three skip outcomes are counted and logged independently:

* ``owner_absent``              — no party-id on the Finding (VIN-scoped,
                                  unresolvable at this seam).  Returned at
                                  the top of _process_finding, before any
                                  gate runs.
* ``owner_namespace_unmatched`` — party-id present but is NOT a Cognito
                                  sub (UUID shape).  Includes ``CUST-*``
                                  registry IDs, ``DRV-*`` driver IDs, and any
                                  other unrecognised format.  All per-owner
                                  gates are skipped entirely — no reads, no
                                  writes.  Applicable until the driver
                                  construct lands (issue
                                  ``avx-owner-id-not-comparable-to-cognito-sub``).

Both are observable via CloudWatch metrics and logs.  A future owner-namespace
reconciliation **must** change the broker; the two tests below are the
tripwires that will catch it:

  * ``test_broker_demotion_skips_cust_namespace_owner_for_owner_namespace_unmatched``
    — must FAIL when the CUST-* namespace is reconciled with Cognito subs.
  * ``test_broker_demotion_skips_owner_absent_finding``
    — must FAIL when VIN → owner resolution feeds a Cognito sub into
      top-level ``owner_id``.

VIN-scope limitation (accepted product decision)
-------------------------------------------------
A VIN-scoped Finding with no resolvable party-id is skipped with
``owner_absent`` at the top of _process_finding.  It does NOT reach the
consent gate and is NOT attributed to a consent decision the owner never made.

Cold-start SSM reads
--------------------
Three policy inputs are read from SSM at cold start, never hardcoded:

  /vsa/{stage}/avx/broker/apns-severity-threshold  (string, default "p0")
  /vsa/{stage}/avx/broker/rate-limit               (JSON: {"day": int, "hour": int})
  /vsa/{stage}/avx/broker/cardinality-cap          (string → int, default 15)

In-app card creation
--------------------
An in-app card (DDB ``avx-actions`` PutItem) is created for **every** Finding
that passes the cap + consent + rate gates, regardless of severity. Demotion
never blocks the card; it only shifts the push threshold.
The ``action_id`` is deterministic (``AVX-ACT-{sha256(finding_id|action_kind|created_by)[:32]}``)
and the write carries ``ConditionExpression="attribute_not_exists(action_id)"`` so
repeated stream deliveries of the same Finding are idempotent.
An APNs ``Publish`` fires ONLY when the card was newly written AND the Finding's
severity meets the effective threshold: ``apns-severity-threshold`` (Addendum
D3), shifted one step by demotion and clamped at ``p0``.

Notify-once idempotency (F32.4)
---------------------------------
After the owner/namespace checks and before any gate, the broker does a
GetItem on ``avx-actions`` using the deterministic ``action_id``.  If the row
already exists in ANY status (including ``dismissed``), the broker returns
``skip_reason="already_carded"``: no gate consumes budget, no push retriggers,
no dismissed card is resurrected.  If a concurrent delivery wins the write
race anyway, the conditional write fails and the outcome is
``duplicate_delivery``, also with no push.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Stage + table names from environment (injected by CDK)
# ---------------------------------------------------------------------------
_STAGE = os.environ.get("AVX_STAGE", "dev")
_FINDINGS_TABLE = os.environ.get("AVX_FINDINGS_TABLE", f"vsa-{_STAGE}-avx-findings")
_ACTIONS_TABLE = os.environ.get("AVX_ACTIONS_TABLE", f"vsa-{_STAGE}-avx-actions")
_FEEDBACK_TABLE = os.environ.get("AVX_FEEDBACK_TABLE", f"vsa-{_STAGE}-avx-feedback")
_DEVICES_TABLE = os.environ.get(
    "AVX_NOTIFICATION_DEVICES_TABLE", f"vsa-{_STAGE}-notification-devices"
)
_RATE_LIMITS_TABLE = os.environ.get(
    "AVX_RATE_LIMITS_TABLE", f"vsa-{_STAGE}-avx-rate-limits"
)

# SSM parameter paths (never hardcoded values — Addendum D3)
_SSM_PREFIX = f"/vsa/{_STAGE}/avx/broker"
_SSM_APNS_THRESHOLD = f"{_SSM_PREFIX}/apns-severity-threshold"
_SSM_RATE_LIMIT = f"{_SSM_PREFIX}/rate-limit"
_SSM_CARDINALITY_CAP = f"{_SSM_PREFIX}/cardinality-cap"

# ---------------------------------------------------------------------------
# Severity ordering (most-severe-first, per finding.schema.json)
# The gate compares rank; a test pins this ordering against the schema so a
# future reorder of the enum cannot silently move the threshold.
# ---------------------------------------------------------------------------
_SEVERITY_ORDER: list[str] = ["p0", "attention", "soon", "routine", "informational"]

# ---------------------------------------------------------------------------
# finding_kind → consent category mapping
# ---------------------------------------------------------------------------
_KIND_TO_CONSENT_CATEGORY: dict[str, str] = {
    "health.tires":           "safety",
    "health.brakes":          "safety",
    "health.battery":         "safety",
    "energy.charging":        "coverage",
    "energy.cost":            "coverage",
    "coverage.warranty":      "coverage",
    "coverage.recall":        "coverage",
    "loyalty.points":         "service",
    "loyalty.expiry":         "service",
    "concierge.appointment":  "service",
    "concierge.service":      "service",
}

# Prefix fallback for unknown kinds (future schema extensions)
_KIND_PREFIX_TO_CONSENT: list[tuple[str, str]] = [
    ("health.",    "safety"),
    ("coverage.",  "coverage"),
    ("energy.",    "coverage"),
    ("loyalty.",   "service"),
    ("concierge.", "service"),
]

# Regex for Cognito sub (UUID format) vs CMS registry ID (CUST-*)
_COGNITO_SUB_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_CUST_ID_RE = re.compile(r"^CUST-[0-9A-F]{8}$")

# ---------------------------------------------------------------------------
# finding_kind → action_kind mapping (urn:cvx:avx:action:v1 enum)
# ---------------------------------------------------------------------------
_KIND_TO_ACTION_KIND: dict[str, str] = {
    "health.tires":           "schedule_inspection",
    "health.brakes":          "schedule_inspection",
    "health.battery":         "schedule_inspection",
    "energy.charging":        "update_charging_schedule",
    "energy.cost":            "acknowledge_only",
    "coverage.warranty":      "file_warranty_pre",
    "coverage.recall":        "surface_recall",
    "loyalty.points":         "acknowledge_only",
    "loyalty.expiry":         "acknowledge_only",
    "concierge.appointment":  "acknowledge_only",
    "concierge.service":      "acknowledge_only",
}

_KIND_PREFIX_TO_ACTION: list[tuple[str, str]] = [
    ("health.",    "schedule_inspection"),
    ("energy.",    "update_charging_schedule"),
    ("coverage.",  "acknowledge_only"),
    ("loyalty.",   "acknowledge_only"),
    ("concierge.", "acknowledge_only"),
]


def _action_kind_for(finding_kind: str) -> str:
    """Map a ``finding_kind`` to the ``action_kind`` enum value in urn:cvx:avx:action:v1."""
    if finding_kind in _KIND_TO_ACTION_KIND:
        return _KIND_TO_ACTION_KIND[finding_kind]
    for prefix, kind in _KIND_PREFIX_TO_ACTION:
        if finding_kind.startswith(prefix):
            return kind
    return "acknowledge_only"


def _derive_action_id(finding_id: str, action_kind: str, created_by: str) -> str:
    """Derive a deterministic ``action_id`` for idempotency.

    sha256(f"{finding_id}|{action_kind}|{created_by}")[:32] used as the UUID
    hex, formatted as ``AVX-ACT-{uuid4-like}`` for schema compatibility.
    Matches the pattern in lambdas/api-avx-findings/handler.py:_derive_action_id.
    """
    h = hashlib.sha256(f"{finding_id}|{action_kind}|{created_by}".encode()).hexdigest()
    s = h[:32]
    uid = f"{s[0:8]}-{s[8:12]}-{s[12:16]}-{s[16:20]}-{s[20:32]}"
    return f"AVX-ACT-{uid}"

# ---------------------------------------------------------------------------
# Lazy AWS client singletons (cold-start initialised)
# ---------------------------------------------------------------------------
_ddb: Any = None
_ssm: Any = None
_sns: Any = None

# ---------------------------------------------------------------------------
# Module-level policy cache (populated once at cold start)
# ---------------------------------------------------------------------------
_policy: dict[str, Any] | None = None


def _get_ddb():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb")
    return _ddb


def _get_ssm():
    global _ssm
    if _ssm is None:
        _ssm = boto3.client("ssm")
    return _ssm


def _get_sns():
    global _sns
    if _sns is None:
        _sns = boto3.client("sns")
    return _sns


# ---------------------------------------------------------------------------
# Cold-start policy loader
# ---------------------------------------------------------------------------

def _load_policy() -> dict[str, Any]:
    """Read broker policy from SSM at cold start.

    Three parameters:
      apns-severity-threshold  → string, default "p0"
      rate-limit               → JSON {"day": int, "hour": int}
      cardinality-cap          → string → int, default 15

    Raises on any SSM failure so the broker fails loudly rather than silently
    applying wrong policy (fail-closed per security review).
    """
    ssm = _get_ssm()

    def _fetch(name: str) -> str:
        resp = ssm.get_parameter(Name=name, WithDecryption=True)
        return resp["Parameter"]["Value"]

    apns_threshold = _fetch(_SSM_APNS_THRESHOLD)
    if apns_threshold not in _SEVERITY_ORDER:
        raise ValueError(
            f"SSM {_SSM_APNS_THRESHOLD!r} value {apns_threshold!r} is not a valid "
            f"severity. Valid: {_SEVERITY_ORDER}"
        )

    rate_limit_raw = _fetch(_SSM_RATE_LIMIT)
    try:
        rate_limit = json.loads(rate_limit_raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"SSM {_SSM_RATE_LIMIT!r} is not valid JSON: {rate_limit_raw!r}"
        ) from exc
    if not isinstance(rate_limit, dict):
        raise ValueError(
            f"SSM {_SSM_RATE_LIMIT!r} must be a JSON object, got: {type(rate_limit)}"
        )

    cardinality_cap = int(_fetch(_SSM_CARDINALITY_CAP))

    log.info(
        "broker policy loaded: apns_threshold=%s rate_limit=%s cardinality_cap=%d",
        apns_threshold,
        rate_limit,
        cardinality_cap,
    )
    return {
        "apns_threshold": apns_threshold,
        "rate_limit": rate_limit,
        "cardinality_cap": cardinality_cap,
    }


def _get_policy() -> dict[str, Any]:
    global _policy
    if _policy is None:
        _policy = _load_policy()
    return _policy


# ---------------------------------------------------------------------------
# Severity helpers
# ---------------------------------------------------------------------------

def _severity_rank(severity: str) -> int:
    """Lower rank = more severe.  Unknown severity treated as least severe."""
    try:
        return _SEVERITY_ORDER.index(severity)
    except ValueError:
        return len(_SEVERITY_ORDER)


def _meets_apns_threshold(severity: str, threshold: str) -> bool:
    """Return True when ``severity`` is at least as severe as ``threshold``."""
    return _severity_rank(severity) <= _severity_rank(threshold)


def _effective_apns_threshold(threshold: str, demoted: bool) -> str:
    """Return the APNs threshold to apply after feedback demotion (F9.3).

    Demotion shifts the threshold one step MORE severe, clamped at ``p0``
    (D-G9-1). A ``p0`` Finding therefore always meets its effective threshold:
    a prior dismissal of a routine card of the same kind can never silence a
    stop-driving push. At the staging default ``p0`` demotion changes no push
    decision; it starts to matter when an operator lowers the threshold.
    """
    if not demoted:
        return threshold
    return _SEVERITY_ORDER[max(_severity_rank(threshold) - 1, 0)]


# ---------------------------------------------------------------------------
# Consent category helper
# ---------------------------------------------------------------------------

def _consent_category(finding_kind: str) -> str:
    """Map a ``finding_kind`` string to one of ``safety``, ``coverage``, ``service``."""
    if finding_kind in _KIND_TO_CONSENT_CATEGORY:
        return _KIND_TO_CONSENT_CATEGORY[finding_kind]
    for prefix, category in _KIND_PREFIX_TO_CONSENT:
        if finding_kind.startswith(prefix):
            return category
    # Unknown kinds default to service (least-privileged push category)
    return "service"


# ---------------------------------------------------------------------------
# Transient failure (F32.5)
# ---------------------------------------------------------------------------

class BrokerTransientError(Exception):
    """A DynamoDB call failed for a reason that is not a policy outcome.

    Raised instead of returning a policy result (``consent_off``, cap or rate
    exceeded) so that an outage is never reported as a decision. ``handler``
    turns it into a batch item failure: Lambda retries the record and, once
    ``retryAttempts`` is exhausted, sends it to the broker DLQ, whose alarm
    reaches the escalation topic. An error is not an absence.
    """


# Every DynamoDB error site catches BOTH families. botocore raises
# ClientError for service errors and BotoCoreError for transport errors
# (ReadTimeoutError, ConnectTimeoutError, EndpointConnectionError). Catching
# only ClientError let a timeout skip the rate refund and the day rollback,
# which re-opened review Cycle 5 W1 by another route (review Cycle 6 W1).
_AWS_ERRORS = (ClientError, BotoCoreError)


def _error_code(exc: Exception) -> str:
    """The service error code of a ClientError, or '' for a transport error."""
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code", "")
    return ""


# ---------------------------------------------------------------------------
# Consent check — ONE device per owner (D-SCOPE-1)
# ---------------------------------------------------------------------------

def _check_consent(owner_party_id: str, category: str) -> bool:
    """Return True when the owner's registered device consents to ``category``.

    **1:1 device model (D-SCOPE-1, decisions-scope-1to1-2026-09-24.md).** An
    owner has at most one row in ``notification-devices``: the register-device
    handler deletes the owner's other rows before it upserts. ``Limit=1`` is
    therefore correct by construction here, and is one of exactly two
    ``Limit`` sites the structural guard in ``test_avx_broker_fix_group_32.py`` allows.
    If multi-device is ever reintroduced, this function must change with it.

    No row means no consent (fail-closed). A DynamoDB error raises
    ``BrokerTransientError`` rather than reporting ``consent_off``.

    ``owner_party_id`` must be a Cognito sub (UUID) — the PK of
    notification-devices.  Callers must verify the namespace before calling
    this function.
    """
    table = _get_ddb().Table(_DEVICES_TABLE)
    try:
        resp = table.query(
            KeyConditionExpression=Key("owner_id").eq(owner_party_id),
            Limit=1,
        )
    except _AWS_ERRORS as exc:
        log.warning("consent_check: DDB query failed owner=%s: %s", owner_party_id, exc)
        raise BrokerTransientError("consent read failed") from exc

    items = resp.get("Items", [])
    if not items:
        log.info(
            "consent_check: no device registered for owner=%s — consent=False",
            owner_party_id,
        )
        return False

    consent = items[0].get("per_category_consent", {})
    allowed = bool(consent.get(category, False))
    log.info(
        "consent_check: owner=%s category=%s allowed=%s",
        owner_party_id,
        category,
        allowed,
    )
    return allowed


# ---------------------------------------------------------------------------
# Rate-limit check + increment
# ---------------------------------------------------------------------------

def _rate_window_keys(now: datetime) -> tuple[str, str]:
    """Return ``(day_key, hour_key)`` for ``now``: one derivation for increment and refund."""
    return f"day:{now.strftime('%Y-%m-%d')}", f"hour:{now.strftime('%Y-%m-%dT%H')}"


def _refund_rate(owner_party_id: str, now: datetime) -> None:
    """Best-effort undo of one ``_check_and_increment_rate`` call made at ``now``.

    Called when budget was spent but no card was written: the card write
    raised, or a concurrent delivery won the write race. Without it, every
    Lambda retry of a failing card write spends budget again, until a retry
    is reported as the policy outcome ``rate_limit_exceeded`` and the record
    is checkpointed past without reaching the DLQ (review Cycle 5 W1).
    ``now`` must be the instant passed to the increment, so a refund that
    straddles a window boundary still decrements the windows it incremented.
    """
    table = _get_ddb().Table(_RATE_LIMITS_TABLE)
    for window_key in _rate_window_keys(now):
        try:
            table.update_item(
                Key={"owner_id": owner_party_id, "window_key": window_key},
                UpdateExpression="ADD #c :neg",
                ConditionExpression="attribute_exists(#c) AND #c > :zero",
                ExpressionAttributeNames={"#c": "count"},
                ExpressionAttributeValues={":neg": -1, ":zero": 0},
            )
        except _AWS_ERRORS as exc:
            log.warning(
                "rate_limit: refund failed owner=%s window=%s: %s",
                owner_party_id, window_key, type(exc).__name__,
            )


def _check_and_increment_rate(owner_party_id: str, rate_limit: dict[str, int],
                              now: datetime | None = None) -> bool:
    """Return True when the owner is within both the day and hour rate limits.

    Uses atomic UpdateItem with a ConditionExpression to guarantee the counter
    stays within the limit even under concurrent invocations.  If the counter
    would exceed the limit, the update fails and we return False.

    ``owner_party_id`` must be a Cognito sub (UUID).  Callers must verify the
    namespace before calling this function — a CUST-* value would write orphaned
    rows and fail open (Count starts at 0 which is always under the limit).

    Window keys:
      day:YYYY-MM-DD
      hour:YYYY-MM-DDTHH

    TTL is set to the end of the respective window.
    """
    now = now or datetime.now(timezone.utc)
    day_key, hour_key = _rate_window_keys(now)

    # End of the current day (UTC)
    day_end = datetime.combine(now.date(), datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=1)
    # End of the current hour (UTC)
    hour_end = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    table = _get_ddb().Table(_RATE_LIMITS_TABLE)

    def _try_increment(window_key: str, limit: int, ttl_epoch: int) -> bool:
        """Attempt atomic increment; return False if limit exceeded."""
        try:
            table.update_item(
                Key={"owner_id": owner_party_id, "window_key": window_key},
                UpdateExpression="ADD #c :one SET #t = if_not_exists(#t, :ttl)",
                ConditionExpression="attribute_not_exists(#c) OR #c < :limit",
                ExpressionAttributeNames={"#c": "count", "#t": "ttl"},
                ExpressionAttributeValues={
                    ":one": 1,
                    ":ttl": ttl_epoch,
                    ":limit": limit,
                },
            )
            return True
        except _AWS_ERRORS as exc:
            if _error_code(exc) == "ConditionalCheckFailedException":
                log.info(
                    "rate_limit: owner=%s window=%s exceeded (limit=%d)",
                    owner_party_id, window_key, limit,
                )
                return False
            log.warning("rate_limit: DDB update failed owner=%s window=%s: %s",
                        owner_party_id, window_key, exc)
            # Not a policy outcome: report a batch failure rather than
            # "rate_limit_exceeded" (F32.5).
            raise BrokerTransientError("rate-limit write failed") from exc

    day_limit = int(rate_limit.get("day", 10))
    hour_limit = int(rate_limit.get("hour", 3))

    if not _try_increment(day_key, day_limit, int(day_end.timestamp())):
        return False

    def _rollback_day() -> None:
        """Best-effort undo of the day increment (race condition tolerated)."""
        try:
            table.update_item(
                Key={"owner_id": owner_party_id, "window_key": day_key},
                UpdateExpression="ADD #c :neg",
                ExpressionAttributeNames={"#c": "count"},
                ExpressionAttributeValues={":neg": -1},
            )
        except _AWS_ERRORS:
            pass

    try:
        hour_ok = _try_increment(hour_key, hour_limit, int(hour_end.timestamp()))
    except BrokerTransientError:
        # The record will be retried; do not leave the day counter one high.
        _rollback_day()
        raise
    if not hour_ok:
        _rollback_day()
        return False
    return True


# ---------------------------------------------------------------------------
# Cardinality cap check
# ---------------------------------------------------------------------------

def _check_cardinality(owner_party_id: str, cap: int) -> bool:
    """Return True when the in-app card count for this owner is below ``cap``.

    Counts open (non-terminal) Actions in the actions table keyed by ``created_by``.
    Uses the ``by_created_by`` GSI.

    ``owner_party_id`` must be a Cognito sub (UUID).  Callers must verify the
    namespace before calling this function — a CUST-* value would always return
    Count=0 (matching nothing) and thus always evaluate as under_cap=True,
    making the cap permanently inert.
    """
    table = _get_ddb().Table(_ACTIONS_TABLE)
    try:
        resp = table.query(
            IndexName="by_created_by",
            KeyConditionExpression=Key("created_by").eq(f"owner-{owner_party_id}"),
            Select="COUNT",
        )
        count = resp.get("Count", 0)
    except _AWS_ERRORS as exc:
        log.warning("cardinality_check: DDB query failed owner=%s: %s", owner_party_id, exc)
        # Not a policy outcome: report a batch failure rather than
        # "cardinality_cap_exceeded" (F32.5).
        raise BrokerTransientError("cardinality read failed") from exc

    under_cap = count < cap
    log.info(
        "cardinality_check: owner=%s count=%d cap=%d under_cap=%s",
        owner_party_id, count, cap, under_cap,
    )
    return under_cap


# ---------------------------------------------------------------------------
# Feedback demotion check
# ---------------------------------------------------------------------------

def _check_demotion(finding: dict[str, Any]) -> tuple[bool, str]:
    """Check whether this Finding should be demoted based on prior dismissals.

    Returns ``(demoted, reason)`` where ``reason`` is one of:
      ``"demoted"``                — match found in feedback table (promote to in-app-only)
      ``"no_prior_dismissal"``     — owner present + comparable, no matching row
      ``"owner_absent"``           — no top-level owner_id on the Finding
      ``"owner_namespace_unmatched"``  — owner_id is CUST-* (not a Cognito sub)

    F9.1: Uses ``_resolve_owner_party_id`` (the same function ``_process_finding``
    gates on) for a single derivation of the owner.  It previously read only
    top-level ``owner_id``, so an owner-scoped Finding carrying only
    ``scope.owner_id`` was never demoted. That is latent today, because the
    writer projects top-level ``owner_id`` for owner-scoped Findings.

    F9.2: Filters feedback rows on ``ttl > :now`` (epoch seconds). DynamoDB TTL
    deletion is asynchronous so a row past its TTL can still be returned; the
    filter provides a single source of truth aligned with the 30-day window the
    dismiss handler writes.

    Namespace note (decisions.md § 2026-09-23):
    The feedback table PK is a Cognito sub (UUID).  The Finding's party-id is
    resolved via ``_resolve_owner_party_id``; if the result is a CUST-XXXXXXXX
    registry ID the broker returns ``owner_namespace_unmatched``.

    Tests pin BOTH skip reasons so a future namespace reconciliation forces
    an intentional code change rather than silent widening.
    """
    owner_id = _resolve_owner_party_id(finding)
    finding_kind = finding.get("finding_kind", "")

    if not owner_id:
        log.info(
            "demotion_check: skipping — owner_absent finding_kind=%s finding_id=%s",
            finding_kind, finding.get("finding_id"),
        )
        return False, "owner_absent"

    if _CUST_ID_RE.match(owner_id):
        # CUST-XXXXXXXX namespace — incompatible with feedback table PK (Cognito sub)
        log.info(
            "demotion_check: skipping — owner_namespace_unmatched owner_id=%s finding_kind=%s",
            owner_id, finding_kind,
        )
        return False, "owner_namespace_unmatched"

    if not _COGNITO_SUB_RE.match(owner_id):
        # Unknown namespace — treat as unresolvable, skip demotion
        log.info(
            "demotion_check: skipping — owner_namespace_unmatched (unknown format) "
            "owner_id=%s finding_kind=%s",
            owner_id, finding_kind,
        )
        return False, "owner_namespace_unmatched"

    # Owner is a Cognito sub — query the feedback table.
    # F9.2: Filter on ttl > :now so asynchronously-deleted rows do not demote.
    now_epoch = int(datetime.now(timezone.utc).timestamp())
    table = _get_ddb().Table(_FEEDBACK_TABLE)
    try:
        paginator_kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("owner_id").eq(owner_id),
            "FilterExpression": "finding_kind = :fk AND #ttl > :now",
            "ExpressionAttributeNames": {"#ttl": "ttl"},
            "ExpressionAttributeValues": {
                ":fk": finding_kind,
                ":now": now_epoch,
            },
        }
        last_key: Any = None
        found = False
        while True:
            if last_key:
                paginator_kwargs["ExclusiveStartKey"] = last_key
            resp = table.query(**paginator_kwargs)
            if resp.get("Count", 0) > 0:
                found = True
                break
            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                break
    except _AWS_ERRORS as exc:
        log.warning(
            "demotion_check: DDB query failed owner=%s finding_kind=%s: %s",
            owner_id, finding_kind, exc,
        )
        # Fail-open on unexpected errors (do not suppress valid notifications),
        # but use a distinct reason so a DDB outage is observable separately
        # from a genuinely empty feedback table.
        return False, "demotion_check_failed"

    if found:
        log.info(
            "demotion_check: demoted owner=%s finding_kind=%s",
            owner_id, finding_kind,
        )
        return True, "demoted"

    return False, "no_prior_dismissal"


# ---------------------------------------------------------------------------
# In-app card existence (F32.4 — notify once per card)
# ---------------------------------------------------------------------------

def _card_identity(finding: dict[str, Any], owner_party_id: str) -> tuple[str, str, str]:
    """Return ``(action_id, action_kind, created_by)`` for this Finding's card.

    One derivation, used by both the existence check and the write, so the two
    can never disagree about which row is "this Finding's card".
    """
    action_kind = _action_kind_for(finding.get("finding_kind", "unknown"))
    created_by = f"owner-{owner_party_id}"
    action_id = _derive_action_id(finding.get("finding_id", ""), action_kind, created_by)
    return action_id, action_kind, created_by


def _card_exists(action_id: str) -> bool:
    """Return True when this Finding's card row already exists, in ANY status.

    Strongly consistent read, because an earlier stream record for the same
    Finding may have written the row moments ago. A ``dismissed`` row counts as
    existing: a dismissed card is never resurrected by a later MODIFY.
    ``"Item" in resp`` is the boto3 contract (the key is absent on a miss).
    A DynamoDB error raises ``BrokerTransientError``.
    """
    table = _get_ddb().Table(_ACTIONS_TABLE)
    try:
        resp = table.get_item(
            Key={"action_id": action_id},
            ProjectionExpression="action_id",
            ConsistentRead=True,
        )
    except _AWS_ERRORS as exc:
        log.warning("card_exists: DDB get_item failed action_id=%s: %s", action_id, exc)
        raise BrokerTransientError("card existence read failed") from exc
    return "Item" in resp


# ---------------------------------------------------------------------------
# In-app card creation
# ---------------------------------------------------------------------------

def _create_in_app_card(finding: dict[str, Any], owner_party_id: str) -> tuple[str, bool]:
    """Write an Action row to ``avx-actions`` as an in-app card.

    Returns ``(action_id, created)``. ``created`` is False when the conditional
    write found the row already present, which the caller treats as a duplicate
    delivery and does NOT push for (review-group7-deeper C-new).

    The row conforms to ``urn:cvx:avx:action:v1`` (action.schema.json).
    The ``action_id`` is deterministic — derived from
    ``sha256(finding_id|action_kind|created_by)`` — so repeated stream
    deliveries of the same Finding are idempotent.  The write carries
    ``ConditionExpression="attribute_not_exists(action_id)"`` to suppress
    duplicate writes on retry/MODIFY events (at-least-once delivery).

    ``owner_party_id`` must be a Cognito sub (UUID).  The ``created_by``
    field is keyed on this value; if it is not a Cognito sub the owner can
    never dismiss the card (the dismiss route matches ``caller_sub``).
    Callers must verify the namespace before calling this function.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    finding_id = finding.get("finding_id", "")
    action_id, action_kind, created_by = _card_identity(finding, owner_party_id)

    # Copy evidence from the Finding (immutable snapshot per D4).
    # When absent, produce an empty array — schema requires the field.
    evidence = list(finding.get("evidence") or [])

    item: dict[str, Any] = {
        "action_id":     action_id,
        "finding_id":    finding_id,
        "created_by":    created_by,
        "target_system": "ios-owner",
        "status":        "pending_execution",
        "created_at":    now,
        "updated_at":    now,
        "action_kind":   action_kind,
        "parameters":    {},
        "evidence":      evidence,
        "audit_trail":   [
            {
                "at":          now,
                "by":          "broker",
                "from_status": "pending_execution",
                "to_status":   "pending_execution",
                "note":        "broker created in-app card from DDB stream event",
            }
        ],
    }
    table = _get_ddb().Table(_ACTIONS_TABLE)
    try:
        table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(action_id)",
        )
        log.info("in_app_card: created action_id=%s for finding_id=%s",
                 action_id, finding.get("finding_id"))
    except _AWS_ERRORS as exc:
        if _error_code(exc) == "ConditionalCheckFailedException":
            log.info(
                "in_app_card: duplicate suppressed — action_id=%s already exists",
                action_id,
            )
            return action_id, False
        log.error(
            "in_app_card: DDB put_item failed action_id=%s finding_id=%s: %s",
            action_id, finding_id, exc,
        )
        raise BrokerTransientError("card write failed") from exc
    return action_id, True


# ---------------------------------------------------------------------------
# APNs push
# ---------------------------------------------------------------------------

def _send_apns_push(owner_party_id: str, finding: dict[str, Any]) -> str:
    """Publish to SNS for APNs delivery and return the outcome.

    Outcome is one of ``published``, ``no_device``, ``endpoint_missing``,
    ``device_lookup_failed`` or ``publish_failed``. The card already exists
    when this runs, so a failure here is recorded rather than raised: a
    retried record would stop at ``already_carded`` and never reach the push,
    so raising would lose the push AND hide it.

    **1:1 device model (D-SCOPE-1)**: ``Limit=1`` is correct by construction
    because register-device keeps at most one row per owner. This is the second
    of the two ``Limit`` sites the structural guard allows.

    This is the ONLY permitted ``sns.publish(...)`` call in the codebase.

    ``owner_party_id`` must be a Cognito sub (UUID) — the PK of
    notification-devices.
    """
    table = _get_ddb().Table(_DEVICES_TABLE)
    try:
        resp = table.query(
            KeyConditionExpression=Key("owner_id").eq(owner_party_id),
            Limit=1,
        )
    except _AWS_ERRORS as exc:
        log.error("apns_push: device lookup failed owner=%s: %s", owner_party_id, exc)
        return "device_lookup_failed"

    items = resp.get("Items", [])
    if not items:
        log.info("apns_push: no device registered for owner=%s — skip", owner_party_id)
        return "no_device"

    endpoint_arn = items[0].get("endpoint_arn")
    if not endpoint_arn:
        log.info("apns_push: endpoint_arn not set for owner=%s — skip", owner_party_id)
        return "endpoint_missing"

    payload = json.dumps({
        "default": finding.get("headline", "New vehicle health finding"),
        "APNS": json.dumps({
            "aps": {
                "alert": finding.get("headline", "New vehicle health finding"),
                "sound": "default",
                "badge": 1,
            },
            "finding_id": finding.get("finding_id"),
            "finding_kind": finding.get("finding_kind"),
            "severity": finding.get("severity"),
        }),
        "APNS_SANDBOX": json.dumps({
            "aps": {
                "alert": finding.get("headline", "New vehicle health finding"),
                "sound": "default",
                "badge": 1,
            },
            "finding_id": finding.get("finding_id"),
            "finding_kind": finding.get("finding_kind"),
            "severity": finding.get("severity"),
        }),
    })

    try:
        _get_sns().publish(
            TargetArn=endpoint_arn,
            Message=payload,
            MessageStructure="json",
        )
        log.info(
            "apns_push: published finding_id=%s owner=%s severity=%s",
            finding.get("finding_id"), owner_party_id, finding.get("severity"),
        )
        return "published"
    except _AWS_ERRORS as exc:
        # BotoCoreError too (e.g. a connection failure): the card exists, so an
        # uncaught error here would retry into already_carded and lose the push.
        log.error("apns_push: sns.publish failed owner=%s: %s", owner_party_id, exc)
        return "publish_failed"


# ---------------------------------------------------------------------------
# Owner party-id resolution from a Finding
# ---------------------------------------------------------------------------

def _resolve_owner_party_id(finding: dict[str, Any]) -> str | None:
    """Extract the owner party-id from the Finding, or None if unavailable.

    The Finding's party-id fields (``scope.owner_id`` and top-level
    ``owner_id``) carry a CMS ``CUST-XXXXXXXX`` registry ID — NOT a Cognito
    sub.  This function returns that raw party-id; the caller must check its
    namespace before routing it to any Cognito-keyed table.

    Returned value is used for the namespace check only.  Per-owner gates
    (rate-limit, cardinality-cap, consent, in-app-card write) are skipped
    entirely when the namespace is ``CUST-*`` — see ``_process_finding``.
    """
    # Prefer scope.owner_id (owner-scoped Finding)
    scope = finding.get("scope") or {}
    if isinstance(scope, dict) and scope.get("owner_id"):
        return scope["owner_id"]

    # Fallback: top-level owner_id (VIN-scoped, projected by F11.2)
    top_owner = finding.get("owner_id")
    if top_owner:
        return top_owner

    return None


# ---------------------------------------------------------------------------
# Single-finding broker logic
# ---------------------------------------------------------------------------

def _process_finding(finding: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    """Apply all four gates for one Finding and take the appropriate action.

    Returns a decision dict with keys:
      ``action``         — ``"in_app_card_and_apns"`` | ``"in_app_card_only"`` | ``"skipped"``
      ``skip_reason``    — populated when action == "skipped"
      ``finding_id``     — the Finding's ID
      ``demotion_reason``— from the demotion check

    Namespace guard
    ~~~~~~~~~~~~~~~
    The per-owner gates (rate-limit, cardinality-cap, consent, in-app-card
    write) are keyed on a Cognito sub (UUID).  If the Finding's party-id is
    a ``CUST-XXXXXXXX`` registry ID, the broker returns immediately with
    ``skip_reason="owner_namespace_unmatched"`` — no gate runs, no counter is
    incremented, no row is written.  This is distinct from ``owner_absent``
    (no party-id at all) so both skip rates are independently observable.
    """
    finding_id = finding.get("finding_id", "<unknown>")
    severity = finding.get("severity", "informational")
    finding_kind = finding.get("finding_kind", "unknown")

    apns_threshold = policy["apns_threshold"]
    rate_limit = policy["rate_limit"]
    cardinality_cap = policy["cardinality_cap"]

    # Resolve owner party-id from the Finding
    owner_raw = _resolve_owner_party_id(finding)

    log.info(
        "broker: processing finding_id=%s severity=%s kind=%s owner_raw=%s",
        finding_id, severity, finding_kind, owner_raw,
    )

    # --- owner_absent: no party-id at all — skip before any gate runs -------
    # Distinct from owner_namespace_unmatched (party-id present but wrong
    # namespace).  Both are independently observable via CloudWatch metrics.
    # A VIN-scoped Finding with no owner is NOT attributed to a consent
    # decision the owner never made.
    if owner_raw is None:
        log.info(
            "broker: skipping — owner_absent finding_id=%s kind=%s",
            finding_id, finding_kind,
        )
        return {
            "action": "skipped",
            "skip_reason": "owner_absent",
            "finding_id": finding_id,
            "demotion_reason": None,
        }

    # --- Namespace guard (must run BEFORE any gate, counter, or write) -----
    # Allowlist: proceed ONLY when the party-id is positively recognised as a
    # Cognito subject (UUID shape). Any other value — CUST-*, DRV-*, or any
    # unrecognised format — fails closed with owner_namespace_unmatched.
    #
    # A non-UUID party-id cannot match any Cognito-keyed table. Routing it
    # through the gates would fail the cardinality cap open (Count=0 < cap),
    # write orphaned rows to the rate-limits table, and produce Action rows
    # with an undismissable created_by value. Detect once, skip entirely.
    #
    # The _CUST_ID_RE pattern is retained solely to enrich the log line with
    # which foreign namespace was seen — it is never the decision input.
    if owner_raw is not None and not _COGNITO_SUB_RE.match(owner_raw):
        _ns_hint = "cust-registry" if _CUST_ID_RE.match(owner_raw) else "unknown"
        log.info(
            "broker: skipping — owner_namespace_unmatched "
            "owner_id=%s namespace_hint=%s finding_id=%s",
            owner_raw, _ns_hint, finding_id,
        )
        return {
            "action": "skipped",
            "skip_reason": "owner_namespace_unmatched",
            "finding_id": finding_id,
            "demotion_reason": None,
        }

    # --- Notify once per card (F32.4) --------------------------------------
    # Before any gate, counter or push. The D2 writer rewrites every open
    # Finding in place each night (a MODIFY), and the stream retries; without
    # this, every delivery re-pushed and drained the owner's rate budget.
    # Severity cannot change on that MODIFY (writer.py's in-place SET omits
    # severity — tripwire test_writer_in_place_update_does_not_set_severity).
    action_id, _, _ = _card_identity(finding, owner_raw)
    if _card_exists(action_id):
        log.info(
            "broker: skipping — already_carded action_id=%s finding_id=%s",
            action_id, finding_id,
        )
        return {
            "action": "skipped",
            "skip_reason": "already_carded",
            "finding_id": finding_id,
            "demotion_reason": None,
            "action_id": action_id,
        }

    # --- Gate 1: Cardinality cap (read) -------------------------------------
    cardinality_ok = _check_cardinality(owner_raw, cardinality_cap)
    if not cardinality_ok:
        log.info("broker: cardinality_cap exceeded finding_id=%s owner=%s", finding_id, owner_raw)
        return {
            "action": "skipped",
            "skip_reason": "cardinality_cap_exceeded",
            "finding_id": finding_id,
            "demotion_reason": None,
        }

    # --- Gate 2: Consent (read) ---------------------------------------------
    category = _consent_category(finding_kind)
    consent_ok = _check_consent(owner_raw, category)
    if not consent_ok:
        log.info(
            "broker: consent_off finding_id=%s owner=%s category=%s",
            finding_id, owner_raw, category,
        )
        return {
            "action": "skipped",
            "skip_reason": "consent_off",
            "finding_id": finding_id,
            "demotion_reason": None,
        }

    # --- Gate 3: Feedback demotion (read; shifts the push threshold only) ---
    demoted, demotion_reason = _check_demotion(finding)

    # --- Gate 4: Rate limit (the only gate that WRITES) ---------------------
    # Last among the gates so that no skip above ever consumes budget
    # (review-group7-deeper W4-new).
    rate_now = datetime.now(timezone.utc)
    rate_ok = _check_and_increment_rate(owner_raw, rate_limit, now=rate_now)
    if not rate_ok:
        log.info("broker: rate_limit exceeded finding_id=%s owner=%s", finding_id, owner_raw)
        return {
            "action": "skipped",
            "skip_reason": "rate_limit_exceeded",
            "finding_id": finding_id,
            "demotion_reason": demotion_reason,
        }

    # owner_raw is guaranteed non-None here (owner_absent returned early above).
    # Budget is spent only for a card that was written (review Cycle 5 W1).
    try:
        action_id, created = _create_in_app_card(finding, owner_raw)
    except BrokerTransientError:
        _refund_rate(owner_raw, rate_now)
        raise
    if not created:
        # Lost a race with a concurrent delivery that wrote the card first.
        # That delivery owns the push and the budget; pushing here would be
        # the double push.
        _refund_rate(owner_raw, rate_now)
        return {
            "action": "skipped",
            "skip_reason": "duplicate_delivery",
            "finding_id": finding_id,
            "demotion_reason": demotion_reason,
            "action_id": action_id,
        }

    # APNs push: severity must meet the EFFECTIVE threshold (F9.3 / D-G9-1).
    effective_threshold = _effective_apns_threshold(apns_threshold, demoted)
    push_outcome: str | None = None
    if _meets_apns_threshold(severity, effective_threshold):
        push_outcome = _send_apns_push(owner_raw, finding)
        final_action = "in_app_card_and_apns"
    else:
        log.info(
            "broker: apns skipped — below effective threshold (severity=%s "
            "threshold=%s effective=%s demotion=%s) finding_id=%s",
            severity, apns_threshold, effective_threshold, demotion_reason, finding_id,
        )
        final_action = "in_app_card_only"

    return {
        "action": final_action,
        "skip_reason": None,
        "finding_id": finding_id,
        "demotion_reason": demotion_reason,
        "effective_apns_threshold": effective_threshold,
        "push_outcome": push_outcome,
        "action_id": action_id,
    }


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------

def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """DynamoDB stream handler — processes NEW_IMAGE records from avx-findings.

    Returns the partial-batch-response shape the event source is configured
    for (``reportBatchItemFailures: true`` in vsa-tier2-stack.ts). On the first
    record that raises, it returns that record's ``SequenceNumber`` at once;
    Lambda checkpoints before it and retries from it, and after
    ``retryAttempts`` the record goes to the broker DLQ, whose alarm reaches
    the escalation topic. Records after the failure are not processed in this
    invocation, because they would be redelivered from the checkpoint anyway.
    https://docs.aws.amazon.com/lambda/latest/dg/services-ddb-batchfailurereporting.html

    Before F32.5 every exception was caught and reported as success, so the
    retry policy and the DLQ could never engage.

    Cold-start loads policy from SSM once; subsequent invocations reuse the
    module-level cache.
    """
    policy = _get_policy()
    records = event.get("Records", [])
    results: list[dict[str, Any]] = []

    for record in records:
        if record.get("eventName") not in ("INSERT", "MODIFY"):
            continue
        new_image = record.get("dynamodb", {}).get("NewImage")
        if not new_image:
            continue

        # Deserialisation runs inside the try: a number that Decimal cannot
        # take modulo (over 28 integer digits) used to raise out of the
        # handler with no itemIdentifier (review Cycle 6 S4).
        finding: dict[str, Any] = {}
        try:
            finding = _deserialise_ddb_item(new_image)
            result = _process_finding(finding, policy)
        except Exception as exc:  # noqa: BLE001 — every failure becomes a batch item failure
            sequence_number = record.get("dynamodb", {}).get("SequenceNumber")
            log.error(
                "broker: record failed finding_id=%s sequence_number=%s error=%s "
                "— reporting batch item failure after %d processed",
                finding.get("finding_id"), sequence_number, type(exc).__name__,
                len(results), exc_info=True,
            )
            # A missing SequenceNumber yields a null itemIdentifier, which
            # Lambda treats as a whole-batch failure: fail-closed, not skipped.
            return {"batchItemFailures": [{"itemIdentifier": sequence_number}]}
        results.append(result)

    log.info("broker: processed %d findings: %s", len(results), results)
    return {"batchItemFailures": []}


# ---------------------------------------------------------------------------
# DynamoDB item deserialiser
# ---------------------------------------------------------------------------

def _deserialise_ddb_item(item: dict[str, Any]) -> dict[str, Any]:
    """Convert a DynamoDB-typed item (``{"S": "...", "N": "...", ...}``) to plain Python."""
    out: dict[str, Any] = {}
    for key, typed_val in item.items():
        out[key] = _deserialise_value(typed_val)
    return out


def _deserialise_value(typed_val: Any) -> Any:
    if not isinstance(typed_val, dict) or len(typed_val) != 1:
        return typed_val
    type_key, value = next(iter(typed_val.items()))
    if type_key == "S":
        return value
    if type_key == "N":
        d = Decimal(value)
        # Non-integers stay Decimal: the boto3 resource serializer rejects
        # float, so a float here made every card write for a Finding with a
        # fractional evidence value (e.g. psi 28.5) fail permanently
        # (review Cycle 5 S5).
        return int(d) if d % 1 == 0 else d
    if type_key == "BOOL":
        return value
    if type_key == "NULL":
        return None
    if type_key == "L":
        return [_deserialise_value(v) for v in value]
    if type_key == "M":
        return {k: _deserialise_value(v) for k, v in value.items()}
    if type_key == "SS":
        return set(value)
    if type_key == "NS":
        return {Decimal(v) for v in value}
    return value
