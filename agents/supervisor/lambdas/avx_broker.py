"""AVX Broker Lambda — channel-policy chokepoint for all customer-facing pushes.

This Lambda is the **sole** permitted caller of ``sns:Publish`` anywhere under
``agents/`` or ``lambdas/``.  ``tests/architecture/test_no_direct_sns_publish.py``
enforces this structurally (AST-walk, Group 5 task).  Every outbound push to a
customer — new AVX Finding card or silent badge-update — must flow through here.

Subscribed to the ``vsa-{stage}-avx-findings`` DynamoDB stream (NEW_IMAGE).
For each new or updated Finding, the broker first skips FI items and checks
the owner/namespace, then the notify-once check (``already_carded``), then
applies four gates in order.
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

* ``fi_item``                   — FI Finding (``agent_id`` starts with
                                  ``"agent-fi-"`` or carries
                                  ``recommended_action``).  Returned first,
                                  before any owner resolution, gate read,
                                  counter, or write.  FI items are
                                  operator-only and must never reach a
                                  driver's phone (spec D13, contract § 12).
* ``owner_absent``              — no party-id on the Finding (VIN-scoped,
                                  unresolvable at this seam).  Returned after
                                  the FI check but before any gate runs.
* ``owner_namespace_unmatched`` — party-id present but is NOT a Cognito
                                  sub (UUID shape).  Includes ``CUST-*``
                                  registry IDs, ``DRV-*`` driver IDs, and any
                                  other unrecognised format.  All per-owner
                                  gates are skipped entirely — no reads, no
                                  writes.  Applicable until the driver
                                  construct lands (issue
                                  ``avx-owner-id-not-comparable-to-cognito-sub``).

All three are observable via logs.  A future owner-namespace
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
``owner_absent`` after the FI item check but before any gate runs.  It does
NOT reach the consent gate and is NOT attributed to a consent decision the
owner never made.

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
no dismissed card is resurrected.  If the conditional card write then finds
the row present (a concurrent writer, or this delivery's own write retried by
botocore after it committed), a Finding below ``p0`` ends in
``duplicate_delivery`` with no push, counted as ``card_write_conflict`` when the
push was due; a ``p0`` goes on to the push through its ledger (review cycle 2 W1).

p0 escalation (issue 2026-10-23-tier2-writer-frozen-severity)
--------------------------------------------------------------
The writer now records severity changes on an open Finding, so a MODIFY can
carry a Finding that worsened into ``p0`` under a card written at a lower
severity. Every p0 push goes through a per-owner, per-Finding ledger row in the
rate-limits table (sk ``p0:<finding_id>``): a carded Finding that is ``p0`` and
has no ``published`` ledger row is pushed once (consent, rate, claim, push), and
every other carded Finding stays ``already_carded``. A failed p0 push leaves the
row ``failed`` so the Finding's next write that reaches the stream retries it. That
is a write that changes the Finding (inputs, severity or standing), or a new
Finding after this one expires: a nightly run with unchanged inputs is a writer
no-op and reaches no one (review cycle 1 W1). The lost push is counted at once, so
the operator alarm, not the retry, is what makes it visible.

Lost pushes (issue 2026-10-23-lost-p0-push-not-alarmed)
--------------------------------------------------------
A push that was due and did not happen (any outcome but ``published``, a due
push blocked by the rate limit or cardinality cap, or a due push below ``p0``
dropped on a card-write conflict) is one EMF count of
``AVX/Broker`` ``PushNotDelivered`` ``{Stage, Severity}``. The ``Severity=p0``
series alarms on the operator topic.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

# The broker bundle copies agents/ alongside this module (see vsa-tier2-stack.ts bundling),
# so agents.supervisor.dtc_response_catalog is available at Lambda runtime.
from agents.supervisor.dtc_response_catalog import (  # noqa: E402
    is_p0_directive as _is_p0_directive,
)

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
# p0 escalation ledger + lost-push metric
# (issues 2026-10-23-tier2-writer-frozen-severity, 2026-10-23-lost-p0-push-not-alarmed)
# ---------------------------------------------------------------------------
#: The one severity whose push is tracked per Finding and owner, so that an open
#: Finding that escalates into it is pushed although its card exists, and not
#: pushed again once a push is ``published``. While a push is unconfirmed
#: (``claimed`` or ``failed``) the next delivery pushes: at-least-once, not
#: exactly-once.
_P0 = "p0"

#: Ledger rows live in the rate-limits table (the broker's own per-owner state),
#: pk ``owner_id`` = the Cognito sub, sk ``"p0:<finding_id>"``. The rate counters
#: use ``day:`` / ``hour:`` keys only, so neither ``_refund_rate`` nor the
#: cardinality cap (which counts Actions) can see these rows.
_P0_LEDGER_PREFIX = "p0:"

#: Ledger states. ``published`` stops any further push for this Finding and owner;
#: every other state lets the next delivery push, so ``claimed`` behaves exactly like
#: no row or ``failed``. ``claimed`` is written before the publish and
#: ``published``/``failed`` after it; ``claimed``, ``claimed_at`` and ``outcome`` are
#: there for diagnosis, and nothing reads ``claimed_at``.
#:
#: Why any non-``published`` row may be reclaimed at once (review cycle 2 W1/S1,
#: cycle 3 S1): no two deliveries of one Finding run at the same time. Lambda keeps
#: one partition key's stream records in order, one batch at a time, for any
#: ParallelizationFactor; the Findings table is keyed on ``finding_id``; exactly one
#: event source mapping invokes the broker; and the broker is the only writer of
#: ``claimed`` and ``failed`` (the live check also marks rows ``published``, which
#: can only make the claim refuse). The mapping count and the partition key are
#: pinned by test_p0_push_lost_alarm_pinned.py; the single writer is a convention
#: (only this module writes ``claimed``/``failed``). So a ``claimed`` row seen by a delivery is
#: either an earlier delivery that died between claim and outcome, or this
#: delivery's own write retried by botocore after its first attempt committed.
#: Neither is running. Reclaiming pushes again: the at-least-once side, chosen for
#: a stop-driving push. One known double-push window: if CloudFormation ever
#: replaces the broker's event source mapping, it creates the new one before
#: deleting the old, and both read the stream for that time.
_P0_STATE_CLAIMED = "claimed"
_P0_STATE_PUBLISHED = "published"
_P0_STATE_FAILED = "failed"

#: The ledger row outlives the Finding it guards by this margin.
_P0_LEDGER_TTL_MARGIN = timedelta(days=30)
#: Used when a Finding's ``expires_at`` cannot be parsed (the runner's own TTL).
_P0_LEDGER_FALLBACK_LIFETIME = timedelta(days=8)

#: EMF metric for a push that was due and did not happen. Dimensions
#: ``{Stage, Severity}``; the ``Severity=p0`` series has an alarm on the operator
#: topic (``vsa-{stage}-avx-broker-p0-push-lost-{region}``, vsa-tier2-stack.ts).
_PUSH_METRIC_NAMESPACE = "AVX/Broker"
_PUSH_METRIC_NAME = "PushNotDelivered"
_PUSH_OUTCOME_PUBLISHED = "published"

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


#: Client timeouts. botocore's defaults (60 s connect, 60 s read, retries on top)
#: let one hung call outlive the broker's 30 s Lambda timeout (vsa-tier2-stack.ts).
#: The bound is per CALL: at most 2 x (3 + 5) s for a publish and 3 x (3 + 5) s for a
#: DynamoDB call. An invocation carries up to 10 records, so under a degraded SNS a
#: batch can still run out of time after a p0 claim (review cycle 2 S2). That is not
#: a silent loss: the stream retries the batch and the retry reclaims the ``claimed``
#: row and pushes (see ``_P0_STATE_CLAIMED``); after the event source's retries the
#: record goes to the broker DLQ, whose alarm pages the escalation topic (it does
#: not emit ``PushNotDelivered``). Bounded, a hung publish usually comes back as
#: ``publish_failed`` and is counted.
#:
#: Retries of a conditional write whose first attempt committed are handled where
#: they bite (review cycle 2 W1): the p0 claim admits its own row, and a p0 whose
#: card write conflicts goes on to the push. A retried rate increment can over-count
#: by one; a due push that this blocks is counted as ``rate_limit_exceeded``.
_SNS_CONFIG = BotoConfig(connect_timeout=3, read_timeout=5,
                         retries={"total_max_attempts": 2, "mode": "standard"})
_DDB_CONFIG = BotoConfig(connect_timeout=3, read_timeout=5,
                         retries={"total_max_attempts": 3, "mode": "standard"})


def _get_ddb():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb", config=_DDB_CONFIG)
    return _ddb


def _get_ssm():
    global _ssm
    if _ssm is None:
        _ssm = boto3.client("ssm")
    return _ssm


def _get_sns():
    global _sns
    if _sns is None:
        _sns = boto3.client("sns", config=_SNS_CONFIG)
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
    write found the row already present. Below ``p0`` the caller treats that as a
    duplicate delivery and does NOT push (review-group7-deeper C-new); a ``p0``
    goes on to the push through its ledger (review cycle 2 W1).

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
    when this runs, so a failure here is recorded rather than raised: below
    ``p0`` a retried record would stop at ``already_carded`` and never reach the
    push, so raising would lose the push AND hide it. (A ``p0`` retry takes the
    escalation path while its ledger is not ``published``; the push is still
    counted here, not raised.)

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

    # P0 directive payload (spec 2026-11-05-avx-proactive-p0-alert § P, T1.2a):
    # When the Finding is severity "p0" AND its stored text is a catalog directive
    # (verified by is_p0_directive), the alert body is the directive word for word.
    # Any other Finding keeps the legacy headline-based payload. The stored AVX
    # Finding's body text is ``detail`` (finding.schema.json has no ``finding``
    # property; the runner maps the agent's ``finding`` text to ``detail``).
    _directive_text = finding.get("detail", "")
    if not isinstance(_directive_text, str):
        _directive_text = ""
    if finding.get("severity") == _P0 and _is_p0_directive(_directive_text):
        _apns_channel = json.dumps({
            "aps": {
                "alert": {"title": "Critical vehicle alert", "body": _directive_text},
                "sound": "default",
                "category": "AVX_SAFETY",
            },
            "finding_id": finding.get("finding_id"),
            "finding_kind": finding.get("finding_kind"),
            "severity": finding.get("severity"),
            "open": "assistant",
        })
        payload = json.dumps({
            "default": _directive_text,
            "APNS": _apns_channel,
            "APNS_SANDBOX": _apns_channel,
        })
    else:
        _headline = finding.get("headline", "New vehicle health finding")
        _apns_channel = json.dumps({
            "aps": {
                "alert": _headline,
                "sound": "default",
                "badge": 1,
            },
            "finding_id": finding.get("finding_id"),
            "finding_kind": finding.get("finding_kind"),
            "severity": finding.get("severity"),
        })
        payload = json.dumps({
            "default": _headline,
            "APNS": _apns_channel,
            "APNS_SANDBOX": _apns_channel,
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
# Lost-push metric (issue 2026-10-23-lost-p0-push-not-alarmed)
# ---------------------------------------------------------------------------

def _emit_push_not_delivered(severity: str, reason: str, finding_id: str) -> None:
    """Count one push that was due and did not happen, by severity (one EMF line).

    ``reason`` is the push outcome (``publish_failed``, ``device_lookup_failed``,
    ``no_device``, ``endpoint_missing``), the gate that blocked a due push
    (``rate_limit_exceeded``, ``cardinality_cap_exceeded``), or
    ``card_write_conflict`` (a due push below ``p0`` dropped because the card
    write found the row present). It and ``finding_id``
    are EMF properties, not dimensions, so the alarm's series stays one per
    severity. Never raises: losing the metric must not change the push decision.
    """
    dim_severity = severity if severity in _SEVERITY_ORDER else "unknown"
    log.error(
        "broker: push_not_delivered severity=%s reason=%s finding_id=%s",
        dim_severity, reason, finding_id,
    )
    try:
        sys.stdout.write(json.dumps({
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [{
                    "Namespace": _PUSH_METRIC_NAMESPACE,
                    "Dimensions": [["Stage", "Severity"]],
                    "Metrics": [{"Name": _PUSH_METRIC_NAME, "Unit": "Count"}],
                }],
            },
            "Stage": _STAGE,
            "Severity": dim_severity,
            _PUSH_METRIC_NAME: 1,
            "reason": reason,
            "finding_id": finding_id,
        }) + "\n")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001 — a metric failure must not fail the record
        log.warning("broker: push_not_delivered metric write failed finding_id=%s", finding_id)


def _push_and_count(owner_party_id: str, finding: dict[str, Any]) -> str:
    """Push a non-p0 Finding and count the outcome if it was not delivered."""
    outcome = _send_apns_push(owner_party_id, finding)
    if outcome != _PUSH_OUTCOME_PUBLISHED:
        _emit_push_not_delivered(
            finding.get("severity", "informational"), outcome, finding.get("finding_id", ""),
        )
    return outcome


# ---------------------------------------------------------------------------
# p0 notification ledger (issue 2026-10-23-tier2-writer-frozen-severity)
# ---------------------------------------------------------------------------

def _p0_ledger_key(owner_party_id: str, finding_id: str) -> dict[str, str]:
    return {"owner_id": owner_party_id, "window_key": f"{_P0_LEDGER_PREFIX}{finding_id}"}


def _p0_ledger_ttl(finding: dict[str, Any], now: datetime) -> int:
    """Epoch seconds: the Finding's ``expires_at`` plus the margin, so the row outlives it."""
    raw = finding.get("expires_at")
    expires: datetime | None = None
    if isinstance(raw, str) and raw:
        try:
            expires = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            expires = None
    if expires is None or expires.tzinfo is None:
        expires = now + _P0_LEDGER_FALLBACK_LIFETIME
    return int((expires + _P0_LEDGER_TTL_MARGIN).timestamp())


def _p0_ledger_state(owner_party_id: str, finding_id: str) -> str | None:
    """The ledger ``state`` for this Finding and owner, or ``None`` when no row exists.

    Strongly consistent. A row whose ``state`` is not a string reads as ``failed``
    (pushable). A DynamoDB error raises ``BrokerTransientError``: an unreadable
    ledger is not an absent one, and treating it as absent could push a p0 twice.
    """
    table = _get_ddb().Table(_RATE_LIMITS_TABLE)
    try:
        resp = table.get_item(
            Key=_p0_ledger_key(owner_party_id, finding_id),
            ConsistentRead=True,
        )
    except _AWS_ERRORS as exc:
        log.warning("p0_ledger: read failed owner=%s finding_id=%s: %s",
                    owner_party_id, finding_id, type(exc).__name__)
        raise BrokerTransientError("p0 ledger read failed") from exc
    item = resp.get("Item")
    if not item:
        return None
    state = item.get("state")
    return state if isinstance(state, str) else _P0_STATE_FAILED


#: The claim's condition: refuse a ``published`` row only (see the ledger-state
#: comment above). Named once so the test can pin the whole expression.
_P0_CLAIM_CONDITION = (
    "attribute_not_exists(window_key) OR attribute_not_exists(#st) OR #st <> :published"
)


def _claim_p0_ledger(owner_party_id: str, finding: dict[str, Any], now: datetime) -> bool:
    """Claim this Finding's p0 push for this owner. ``False`` only when it was published.

    Admits every row that is not ``published``, including a ``claimed`` one of any
    age: no other delivery of this item can be running (ordering argument at
    ``_P0_STATE_CLAIMED``), and a botocore retry of this very write after its first
    attempt committed must succeed rather than read as someone else's claim (review
    cycle 2 W1). Any other DynamoDB error raises ``BrokerTransientError``.
    """
    table = _get_ddb().Table(_RATE_LIMITS_TABLE)
    try:
        table.update_item(
            Key=_p0_ledger_key(owner_party_id, finding.get("finding_id", "")),
            UpdateExpression="SET #st = :claimed, claimed_at = :now, #t = :ttl",
            ConditionExpression=_P0_CLAIM_CONDITION,
            ExpressionAttributeNames={"#st": "state", "#t": "ttl"},
            ExpressionAttributeValues={
                ":claimed": _P0_STATE_CLAIMED,
                ":published": _P0_STATE_PUBLISHED,
                ":now": int(now.timestamp()),
                ":ttl": _p0_ledger_ttl(finding, now),
            },
        )
        return True
    except _AWS_ERRORS as exc:
        if _error_code(exc) == "ConditionalCheckFailedException":
            log.info("p0_ledger: already published owner=%s finding_id=%s",
                     owner_party_id, finding.get("finding_id"))
            return False
        log.warning("p0_ledger: claim failed owner=%s finding_id=%s: %s",
                    owner_party_id, finding.get("finding_id"), type(exc).__name__)
        raise BrokerTransientError("p0 ledger claim failed") from exc


#: ``failed`` is written only over a row that is not ``published``.
_P0_OUTCOME_FAILED_CONDITION = "attribute_not_exists(#st) OR #st <> :published"


def _record_p0_outcome(owner_party_id: str, finding_id: str, outcome: str) -> None:
    """Set the ledger to ``published`` or ``failed`` after the push. Best effort.

    Never raises: the push already happened (or failed). If this write fails the
    row stays ``claimed``, which the next delivery of this Finding reclaims and
    pushes again: the at-least-once side, chosen for a safety push.

    A ``published`` row never goes back (security review cycle 1 S1): ``failed``
    is written only over a row that is not ``published``, so a failed delivery
    running beside a successful one (the mapping-replacement window) cannot
    reopen the Finding for a third push. The refused write is a no-op.
    """
    state = _P0_STATE_PUBLISHED if outcome == _PUSH_OUTCOME_PUBLISHED else _P0_STATE_FAILED
    table = _get_ddb().Table(_RATE_LIMITS_TABLE)
    kwargs: dict[str, Any] = {
        "Key": _p0_ledger_key(owner_party_id, finding_id),
        "UpdateExpression": "SET #st = :state, outcome = :outcome",
        "ExpressionAttributeNames": {"#st": "state"},
        "ExpressionAttributeValues": {":state": state, ":outcome": outcome},
    }
    if state == _P0_STATE_FAILED:
        kwargs["ConditionExpression"] = _P0_OUTCOME_FAILED_CONDITION
        kwargs["ExpressionAttributeValues"][":published"] = _P0_STATE_PUBLISHED
    try:
        table.update_item(**kwargs)
    except _AWS_ERRORS as exc:
        if _error_code(exc) == "ConditionalCheckFailedException":
            log.info("p0_ledger: outcome failed not recorded over published owner=%s "
                     "finding_id=%s", owner_party_id, finding_id)
            return
        log.error("p0_ledger: outcome write failed owner=%s finding_id=%s outcome=%s: %s",
                  owner_party_id, finding_id, outcome, type(exc).__name__)


def _push_p0_once(owner_party_id: str, finding: dict[str, Any], now: datetime) -> str:
    """Claim, push, record. Returns the push outcome, or ``already_notified``.

    Every p0 push goes through here, whether its card is new or the Finding
    escalated under an existing card, so a p0 Finding is pushed once per owner
    once a push has been published. A claim error raises ``BrokerTransientError``
    before anything is published. A refused claim means the ledger already says
    ``published``.
    """
    finding_id = finding.get("finding_id", "")
    if not _claim_p0_ledger(owner_party_id, finding, now):
        return "already_notified"
    outcome = _send_apns_push(owner_party_id, finding)
    _record_p0_outcome(owner_party_id, finding_id, outcome)
    if outcome != _PUSH_OUTCOME_PUBLISHED:
        _emit_push_not_delivered(_P0, outcome, finding_id)
    return outcome


def _due_push_would_reach_owner(owner_party_id: str, finding_kind: str) -> bool:
    """For a gate that blocked a due push before consent was read: would it have reached?

    ``False`` only when the consent read succeeds and says no (the owner's choice,
    not a lost push). A read error counts as reachable, so a cap-blocked p0 is
    counted rather than silently dropped.
    """
    try:
        return _check_consent(owner_party_id, _consent_category(finding_kind))
    except BrokerTransientError:
        return True


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
# FI item predicate (spec D13, contract § 12)
# ---------------------------------------------------------------------------

def _is_fi_finding(finding: dict[str, Any]) -> bool:
    """True if this Finding is an FI item that must never be pushed to a driver's phone.

    Matches Findings whose ``agent_id`` starts with ``"agent-fi-"`` OR that
    carry a ``recommended_action`` field (spec D13, contract § 12).  Both
    conditions ensure that an FI decision on a pre-existing health kind is
    also excluded when the Finding itself carries the FI mark.

    Mirrors ``_is_fi_finding`` in ``lambdas/api-avx-findings/handler.py``
    (iOS route exclusion, same predicate).  The two bundles are independent:
    neither imports the other.
    """
    agent_id = finding.get("agent_id", "")
    return (
        (isinstance(agent_id, str) and agent_id.startswith("agent-fi-"))
        or "recommended_action" in finding
    )


# ---------------------------------------------------------------------------
# Single-finding broker logic
# ---------------------------------------------------------------------------

def _escalate_to_p0(finding: dict[str, Any], owner_party_id: str, policy: dict[str, Any],
                    action_id: str, ledger_state: str | None) -> dict[str, Any]:
    """Push a carded Finding that is now ``p0`` and has no published p0 push yet.

    Issue 2026-10-23-tier2-writer-frozen-severity. The card already exists (any
    status), so no card is written and the cardinality cap does not apply; a p0
    push is never demoted (D-G9-1), so demotion is not read. Consent is read
    (the owner's choice), then the rate limit (the only write), then the ledger
    claim, then the push. A rate-limited p0 is counted as not delivered and makes
    no claim, so the Finding's next write that reaches the stream tries again. A
    refused claim (the ledger already says ``published``) refunds the budget.
    """
    finding_id = finding.get("finding_id", "<unknown>")
    log.info(
        "broker: p0 escalation on carded finding action_id=%s finding_id=%s ledger=%s",
        action_id, finding_id, ledger_state,
    )

    category = _consent_category(finding.get("finding_kind", "unknown"))
    if not _check_consent(owner_party_id, category):
        log.info("broker: consent_off (p0 escalation) finding_id=%s owner=%s category=%s",
                 finding_id, owner_party_id, category)
        return {
            "action": "skipped",
            "skip_reason": "consent_off",
            "finding_id": finding_id,
            "demotion_reason": None,
            "action_id": action_id,
            "escalation": True,
        }

    rate_now = datetime.now(timezone.utc)
    if not _check_and_increment_rate(owner_party_id, policy["rate_limit"], now=rate_now):
        log.info("broker: rate_limit exceeded (p0 escalation) finding_id=%s owner=%s",
                 finding_id, owner_party_id)
        _emit_push_not_delivered(_P0, "rate_limit_exceeded", finding_id)
        return {
            "action": "skipped",
            "skip_reason": "rate_limit_exceeded",
            "finding_id": finding_id,
            "demotion_reason": None,
            "action_id": action_id,
            "escalation": True,
        }

    try:
        push_outcome = _push_p0_once(owner_party_id, finding, rate_now)
    except BrokerTransientError:
        _refund_rate(owner_party_id, rate_now)
        raise
    if push_outcome == "already_notified":
        _refund_rate(owner_party_id, rate_now)
        return {
            "action": "skipped",
            "skip_reason": "duplicate_delivery",
            "finding_id": finding_id,
            "demotion_reason": None,
            "action_id": action_id,
            "escalation": True,
        }

    return {
        "action": "p0_escalation_apns",
        "skip_reason": None,
        "finding_id": finding_id,
        "demotion_reason": None,
        "push_outcome": push_outcome,
        "action_id": action_id,
        "escalation": True,
    }


def _process_finding(finding: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    """Apply all four gates for one Finding and take the appropriate action.

    Returns a decision dict with keys:
      ``action``         — ``"in_app_card_and_apns"`` | ``"in_app_card_only"`` | ``"skipped"``
      ``skip_reason``    — populated when action == "skipped"
      ``finding_id``     — the Finding's ID
      ``demotion_reason``— from the demotion check

    FI item check (first)
    ~~~~~~~~~~~~~~~~~~~~~
    Before any owner look-up, gate read or counter, FI Findings (``agent_id``
    starts with ``"agent-fi-"`` or carries ``recommended_action``) are returned
    with ``skip_reason="fi_item"``.  FI items are operator-only and must never
    reach a driver's phone (spec D13).

    Namespace guard (after FI check)
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    The per-owner gates (rate-limit, cardinality-cap, consent, in-app-card
    write) are keyed on a Cognito sub (UUID).  If the Finding's party-id is
    a ``CUST-XXXXXXXX`` registry ID, the broker returns immediately with
    ``skip_reason="owner_namespace_unmatched"`` — no gate runs, no counter is
    incremented, no row is written.  This is distinct from ``owner_absent``
    (no party-id at all) so both skip rates are independently observable.
    """
    # --- FI item: skip before any gate, owner resolution, counter or write ----
    # FI Findings (agent_id starts with "agent-fi-" or carry recommended_action)
    # are operator-only and must never reach a driver's phone (spec D13).
    # This check runs first so no namespace check, gate read, or rate counter
    # ever fires for an FI item.
    if _is_fi_finding(finding):
        fi_fid = finding.get("finding_id", "<unknown>")
        log.info(
            "broker: skipping — fi_item finding_id=%s kind=%s agent_id=%s",
            fi_fid,
            finding.get("finding_kind", "unknown"),
            finding.get("agent_id", ""),
        )
        return {
            "action": "skipped",
            "skip_reason": "fi_item",
            "finding_id": fi_fid,
            "demotion_reason": None,
        }

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
    #
    # Severity CAN change on a MODIFY (issue 2026-10-23-tier2-writer-frozen-
    # severity: the writer records it both ways). Every severity below p0 stays
    # notify-once on the card. A p0 is tracked separately in the p0 ledger: a
    # carded Finding that is now p0 and has not had a published p0 push takes the
    # escalation path, regardless of the card's status (a dismissed card stays
    # dismissed; the push does not read it).
    action_id, _, _ = _card_identity(finding, owner_raw)
    if _card_exists(action_id):
        if severity == _P0:
            ledger = _p0_ledger_state(owner_raw, finding_id)
            if ledger != _P0_STATE_PUBLISHED:
                return _escalate_to_p0(finding, owner_raw, policy, action_id, ledger)
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

    # A push is "due" when the Finding meets the configured threshold. A gate that
    # blocks a due push is counted (issue 2026-10-23-lost-p0-push-not-alarmed).
    push_due = _meets_apns_threshold(severity, apns_threshold)

    # --- Gate 1: Cardinality cap (read) -------------------------------------
    cardinality_ok = _check_cardinality(owner_raw, cardinality_cap)
    if not cardinality_ok:
        log.info("broker: cardinality_cap exceeded finding_id=%s owner=%s", finding_id, owner_raw)
        if push_due and _due_push_would_reach_owner(owner_raw, finding_kind):
            _emit_push_not_delivered(severity, "cardinality_cap_exceeded", finding_id)
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
        if _meets_apns_threshold(severity, _effective_apns_threshold(apns_threshold, demoted)):
            # Consent already passed above, so this due push would have reached. The
            # demoted threshold is known here, so a push demotion would have
            # suppressed is not counted (review cycle 1 S8).
            _emit_push_not_delivered(severity, "rate_limit_exceeded", finding_id)
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
    if not created and severity != _P0:
        # The card write found the row present. A concurrent writer may own it and
        # its push, but right after a consistent _card_exists miss it is usually this
        # delivery's own PutItem retried by botocore (review cycle 2 W1), whose push
        # is then dropped. Below p0 that is knowingly kept (notify-once; no ledger),
        # and a push that was due is counted rather than silent (review cycle 3 S4).
        # The unit is refunded even when the card is this delivery's own (it cannot
        # tell), and in a mapping-replacement window the count can fire for a push the
        # other delivery made; both are below p0 and so off the operator alarm.
        if _meets_apns_threshold(severity, _effective_apns_threshold(apns_threshold, demoted)):
            _emit_push_not_delivered(severity, "card_write_conflict", finding_id)
        _refund_rate(owner_raw, rate_now)
        return {
            "action": "skipped",
            "skip_reason": "duplicate_delivery",
            "finding_id": finding_id,
            "demotion_reason": demotion_reason,
            "action_id": action_id,
        }
    # A p0 whose card write found the row present goes on to the push (review cycle 2
    # W1). Right after a consistent _card_exists miss, the row is almost always this
    # delivery's own PutItem, retried by botocore after its first attempt committed
    # (one item's records are processed in order). Skipping here dropped the push with
    # no metric. The p0 ledger claim is the dedup: it refuses only a published push.

    # APNs push: severity must meet the EFFECTIVE threshold (F9.3 / D-G9-1).
    effective_threshold = _effective_apns_threshold(apns_threshold, demoted)
    push_outcome: str | None = None
    if _meets_apns_threshold(severity, effective_threshold):
        if severity == _P0:
            # Through the p0 ledger, so a later p0 MODIFY of this carded Finding
            # does not take the escalation path and push again. A claim error is
            # raised before anything is published; the card exists, so the retry
            # reaches the escalation path, which spends the budget for the push.
            try:
                push_outcome = _push_p0_once(owner_raw, finding, rate_now)
            except BrokerTransientError:
                _refund_rate(owner_raw, rate_now)
                raise
        else:
            push_outcome = _push_and_count(owner_raw, finding)
        # A refused p0 claim published nothing (review cycle 1 S7).
        final_action = ("in_app_card_only" if push_outcome == "already_notified"
                        else "in_app_card_and_apns")
    else:
        log.info(
            "broker: apns skipped — below effective threshold (severity=%s "
            "threshold=%s effective=%s demotion=%s) finding_id=%s",
            severity, apns_threshold, effective_threshold, demotion_reason, finding_id,
        )
        final_action = "in_app_card_only"

    if not created and push_outcome == "already_notified":
        # A p0 whose card write found the row present and that published nothing
        # here (the ledger already said published): this delivery wrote neither the
        # card nor a push, so it returns the unit and reports the duplicate, as
        # below p0. (``not created`` here implies p0, which always meets its
        # effective threshold, so the push was attempted.)
        _refund_rate(owner_raw, rate_now)
        return {
            "action": "skipped",
            "skip_reason": "duplicate_delivery",
            "finding_id": finding_id,
            "demotion_reason": demotion_reason,
            "action_id": action_id,
        }

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
