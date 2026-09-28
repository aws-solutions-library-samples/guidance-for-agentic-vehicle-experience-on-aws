"""DMS warranty-claim verdict source — the read half of Agent 2's post-service-visit path.

Spec: `.kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/tasks.md` § Fix Group 21,
tasks 1–2. Design record: `decisions.md` § "2026-09-23 — Fix Group 21 tasks 1–2".
Issue: `issues/2026-09-23-warranty-verdict-enum-fabricated-in-cvx-narration-guard/`.

## What this module is for

Agent 2 can narrate a DMS warranty verdict, and has been able to since `df5fb43`. Nothing
ever called it: 18 non-test call sites of ``assess_vehicle_health`` and not one passed
``warranty_verdict_phrase``. This module is the evidence source that makes that path
reachable — it fetches the verdict so :func:`agents.health.agent.run_health_agent` can
forward it.

The verdict is narrated **verbatim**. It is a money decision made by DMS's rules engine, and
softening ``denied`` crosses the deterministic-seam boundary described in
`~/.kiro/steering/agentic-tiers.md` § "What stays deterministic, and why". This module does
not adjudicate, re-derive, default, or soften a verdict. It reads one, validates that DMS
could actually have produced it, and hands it over unchanged.

## The access path is two hops, and why

**The obvious implementation does not exist.** ``dms-{stage}-warranty-claims`` has no VIN
access path: its GSIs are ``dealer-status-index`` and ``ro-index``, and the VIN is stored as
``vehicle_vin`` — not ``vin``, which appears on **zero** rows (0 of 300 scanned live
2026-09-23). Both facts contradict the task text that specified a per-VIN fetch, and both
were established by `describe-table` / `scan` against staging rather than from prose. See
`decisions.md` F1 and F2.

So VIN → verdict goes through the repair order, using an index on each table:

    hop 1   dms-{stage}-repair-orders   / vin-index   vehicle_vin = <vin>  → ro_id(s)
    hop 2   dms-{stage}-warranty-claims / ro-index    ro_id       = <ro>   → claim + verdict

Live-verified end to end 2026-09-23:
``MRDN0298222340245`` → ``RO-PORTLAND-202510-0172`` → ``WC-PORTLAND-0007``.

This is also what the design intended without saying so. The trigger is
``dms.ro.status_changed`` (already consumed by ``lambdas/api-avx-dms-event-consumer``, DMS
T5.4, deployed), whose payload is RO-keyed, and ``ro-index`` exists for exactly this lookup.

**Why not a Scan.** A ``Scan`` + ``FilterExpression`` on ``vehicle_vin`` would let hop 1
collapse into the claims table, and is wrong twice over: the IAM grant this path is designed
around (Fix Group 21 task 3) deliberately carries ``GetItem`` + ``Query`` only — citing
``T10.6``'s trim of four over-provisioned ``dynamodb:*`` grants — so a Scan would fail closed
on ``AccessDenied`` permanently rather than temporarily; and a per-VIN Scan across a Tier 2
batch is O(table × batch), which is survivable against 300 staging rows and not against a
real dealer group.

**The IAM grant for hop 1 does not exist yet.** Task 3 as written names only the claims
table. Until it is widened to include ``table/dms-{stage}-repair-orders/index/vin-index``,
this module fails closed on hop 1. That is the correct intermediate state — nothing calls
this path in production today — but it means a green test suite here says nothing about
whether the path works against AWS. See `decisions.md` F4.

## Attribute names are mirrored by hand, and nothing detects drift

CVX cannot import from the DMS repo. These names are transcribed from
``~/guidance-for-dealer-management-system-on-aws/source/handlers/warranty_claims.py:436-452``,
which is the code that writes them:

  - ``verdict_status``  → the value narrated verbatim; vocabulary is
    :data:`agents.health.dms_verdicts.DMS_VERDICT_ENUM`
  - ``verdict_reason``  → human-readable reason string
  - ``vehicle_vin``     → the VIN, on both DMS tables

Note the asymmetry that produced the original error: DMS's **event** payload spells it
``vin`` (``source/_lib/events.py:54``) while its **tables** spell it ``vehicle_vin``. Both
spellings are real; they just belong to different surfaces.

A claim row's top-level ``status`` is the claim's *lifecycle* state
(``Draft`` / ``Validated`` / ``Submitted``) and is **not** the verdict. Reading it as one is
the mistake this module is shaped to prevent.

## Absence is not a verdict

``fetch_verdict`` returns ``None`` when there is no repair order, no claim, or a claim with
no verdict written. Callers must treat that as "no verdict" and narrate nothing — **never**
substitute a default. A fabricated ``approved`` is a false statement about a money decision.

This is not hypothetical: as of 2026-09-23, **0 of 300** staging claims carry
``verdict_status``, including all 87 whose lifecycle status is already ``Validated``, because
the seeded data never went through DMS's ``validate`` endpoint. Every live read today returns
``None``. See `decisions.md` F5 — it is why live verification (task 4) needs a seeded verdict
before it can prove anything.

## Pagination

Both hops page to exhaustion on ``LastEvaluatedKey``. This repo's Tier 2 agent has already
shipped a defect of exactly the opposite shape — a single-page ``GetQueryResults`` that
silently truncated at the 4th VIN and made an idempotency claim untrue — so unpaged reads are
a known-recurring failure here, not a theoretical one.

No boto3 import-time side effects: the DynamoDB client is injected so tests supply a stub.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from agents.health._vin import sanitise_vin
from agents.health.dms_verdicts import DMS_VERDICT_ENUM

log = logging.getLogger(__name__)

__all__ = [
    "CLAIMS_TABLE_ENV_VAR",
    "REPAIR_ORDERS_TABLE_ENV_VAR",
    "CLAIMS_RO_INDEX",
    "REPAIR_ORDERS_VIN_INDEX",
    "MAX_AGE_DAYS_ENV_VAR",
    "DEFAULT_MAX_AGE_DAYS",
    "resolve_max_age_days",
    "DmsWarrantyConfigError",
    "DmsWarrantyVerdictError",
    "DmsWarrantySource",
    "fetch_verdict",
    "resolve_table_name",
]

# ---------------------------------------------------------------------------
# Configuration — fail closed, no fallback defaults
# ---------------------------------------------------------------------------

#: Env var carrying the DMS warranty-claims table name, injected by the CDK.
CLAIMS_TABLE_ENV_VAR = "DMS_WARRANTY_CLAIMS_TABLE"

#: Env var carrying the DMS repair-orders table name, injected by the CDK.
#: Required because hop 1 (VIN → ``ro_id``) has no alternative — see the module
#: docstring's "The access path is two hops".
REPAIR_ORDERS_TABLE_ENV_VAR = "DMS_REPAIR_ORDERS_TABLE"

#: GSI on the claims table keyed by ``ro_id`` (HASH). Verified live 2026-09-23.
CLAIMS_RO_INDEX = "ro-index"

#: GSI on the repair-orders table keyed by ``vehicle_vin`` (HASH), projection ``ALL``.
#: Verified live 2026-09-23.
REPAIR_ORDERS_VIN_INDEX = "vin-index"

#: Attribute holding the VIN on both DMS tables. **Not** ``vin`` — see the module
#: docstring. Named as a constant so the spelling appears once.
ATTR_VEHICLE_VIN = "vehicle_vin"

#: Attribute holding the narrated verdict value.
ATTR_VERDICT_STATUS = "verdict_status"

#: Attribute holding the verdict's reason string.
ATTR_VERDICT_REASON = "verdict_reason"

#: Attribute holding the claim's last-update timestamp. DMS writes this in the *same*
#: ``UpdateExpression`` as the verdict (``warranty_claims.py:427-452``), so it dates the
#: ruling rather than some unrelated touch.
ATTR_UPDATED_AT = "updated_at"

#: Attribute holding the repair-order identifier, on both tables.
ATTR_RO_ID = "ro_id"

#: Env var bounding how old a verdict may be and still be narrated, in days.
MAX_AGE_DAYS_ENV_VAR = "DMS_WARRANTY_VERDICT_MAX_AGE_DAYS"

#: Default recency window when :data:`MAX_AGE_DAYS_ENV_VAR` is unset.
#:
#: Unlike the table names, this one **does** have a default: an absent tuning value is not
#: the same class of error as an absent table name, and failing closed on it would disable
#: warranty narration entirely rather than bound it. 30 days is a starting stage default and
#: is expected to move to an operator-facing configurator rather than staying a code
#: constant.
DEFAULT_MAX_AGE_DAYS = 30

#: How far into the future a verdict timestamp may sit and still be accepted.
#:
#: DMS writes ``updated_at`` on a different host, so a few seconds of clock drift is ordinary
#: and must not discard a legitimate fresh verdict. Beyond this it is not skew, it is a bad
#: row — and a future-dated row would win the most-recent-wins sort, so accepting one risks
#: inverting the verdict rather than merely mis-dating it. See :func:`_is_within_window`.
_FUTURE_SKEW_ALLOWANCE = timedelta(minutes=5)


class DmsWarrantyConfigError(RuntimeError):
    """Raised when a required table-name env var is absent or empty.

    Deliberately fatal rather than defaulted. Mirrors the precedent set by
    ``lambdas/api-avx-dms-event-consumer/handler.py:103`` — "refusing to project against an
    unknown table rather than guess a stage". Guessing ``staging`` while deployed to prod
    would read one environment's warranty decisions and narrate them into another's.
    """


class DmsWarrantyVerdictError(ValueError):
    """Raised when a stored ``verdict_status`` is outside :data:`DMS_VERDICT_ENUM`.

    A **hard error, not a pass-through.** Agent 2 narrates this value verbatim, so an
    unrecognised verdict is either DMS-side drift or a corrupted row, and narrating it
    would defeat the whole point of Fix Group 19 — which existed because three verdict
    values had been invented CVX-side and narrated as if DMS had produced them.

    Failing loudly here converts a silent false statement about a money decision into a
    visible operational error. The caller in :func:`agents.health.agent.run_health_agent`
    degrades to ``None`` (no narration) rather than propagating, so a single bad row cannot
    take down a health assessment — but it is logged and counted, not ignored.
    """


def resolve_table_name(env_var: str) -> str:
    """Return the table name held in *env_var*, or raise :exc:`DmsWarrantyConfigError`.

    Read at call time rather than at import time. The precedent module
    (``api-avx-dms-event-consumer/handler.py``) reads at import because it is a Lambda
    handler whose environment is fixed before the first invocation; this is a library module
    imported by the Tier 2 runner, where call-time resolution keeps the fail-closed property
    while remaining testable without module reloading.

    Parameters
    ----------
    env_var:
        Name of the environment variable to read.

    Returns
    -------
    str
        The table name, stripped of surrounding whitespace.

    Raises
    ------
    DmsWarrantyConfigError
        If the variable is unset, empty, or whitespace-only. Whitespace-only counts as
        unset: a table name of ``" "`` is not a table, and treating it as one would push
        the failure to a confusing ``ResourceNotFoundException`` at the API boundary.
    """
    raw = os.environ.get(env_var)
    if raw is None or not raw.strip():
        raise DmsWarrantyConfigError(
            f"{env_var} is not set; refusing to read DMS warranty data against an "
            "unknown table rather than guess a stage."
        )
    return raw.strip()


def resolve_max_age_days() -> int:
    """Return the verdict recency window in days.

    Falls back to :data:`DEFAULT_MAX_AGE_DAYS` when unset or unparseable, rather than raising
    — see that constant for why this differs from the table-name handling. A non-numeric or
    non-positive value is logged and ignored, because silently treating ``"abc"`` as ``0``
    would disable warranty narration with no signal.
    """
    raw = os.environ.get(MAX_AGE_DAYS_ENV_VAR)
    if raw is None or not raw.strip():
        return DEFAULT_MAX_AGE_DAYS
    try:
        value = int(raw.strip())
    except ValueError:
        log.warning(
            "dms_warranty_source: %s=%r is not an integer; using default %d days.",
            MAX_AGE_DAYS_ENV_VAR,
            raw,
            DEFAULT_MAX_AGE_DAYS,
        )
        return DEFAULT_MAX_AGE_DAYS
    if value <= 0:
        log.warning(
            "dms_warranty_source: %s=%d is not positive; using default %d days.",
            MAX_AGE_DAYS_ENV_VAR,
            value,
            DEFAULT_MAX_AGE_DAYS,
        )
        return DEFAULT_MAX_AGE_DAYS
    return value


def _parse_updated_at(raw: Any) -> Optional[datetime]:
    """Parse DMS's ``updated_at`` into an aware datetime, or ``None`` if unusable.

    DMS writes ``datetime.now(timezone.utc).isoformat()``, which yields a ``+00:00`` offset;
    live staging rows were observed carrying a ``Z`` suffix. Both are accepted. A naive
    timestamp is assumed UTC, since that is what the producer writes.
    """
    if not isinstance(raw, str) or not raw:
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _is_within_window(updated_at: Optional[datetime], max_age_days: int) -> bool:
    """True when *updated_at* is recent enough, and not in the future, to narrate.

    The window is bounded at **both** ends, which is not symmetry for its own sake:

    - **Upper bound** (too old) — the reason the window exists. See the module docstring.
    - **Lower bound** (future-dated) — ``age <= timedelta(days=N)`` is also true for a
      *negative* age, and the same timestamp is the ``reverse=True`` sort key in
      :func:`fetch_verdict`. So a future-dated row does not merely pass the check, it wins
      the ordering: a corrupt ``approved`` stamped next year beats a real ``denied`` from an
      hour ago, and the owner is told their claim was approved when DMS denied it.

      That is a **verdict inversion**, and it was the one corrupt-data path in this module
      that neither degraded nor raised — an out-of-enum verdict hard-errors, an undated one
      fails closed, a missing ``ro_id`` returns ``None``, but a future date silently won.
      Found in review Cycle 2.

    An unparseable or absent timestamp returns ``False`` — fail closed. An undated verdict
    cannot be shown to be current, and narrating one inside an artifact stamped
    ``computed_at=now`` is the staleness bug this bound exists to prevent.

    A small clock-skew allowance is granted on the future side: DMS writes its timestamp on a
    different host, so a few seconds of drift is ordinary and must not discard a legitimate
    fresh verdict. Anything beyond that is not skew, it is a bad row.
    """
    if updated_at is None:
        return False
    age = datetime.now(timezone.utc) - updated_at
    if age < -_FUTURE_SKEW_ALLOWANCE:
        log.warning(
            "dms_warranty_source: verdict is dated %s in the future beyond the skew "
            "allowance; refusing to narrate it. A future-dated row would also win the "
            "most-recent sort and could invert the verdict.",
            -age,
        )
        return False
    return age <= timedelta(days=max_age_days)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _unwrap(attr: Any) -> Any:
    """Return the Python value from a low-level DynamoDB attribute-value dict.

    Handles the three shapes this module reads: ``{"S": str}``, ``{"N": str}``, and
    ``{"NULL": True}``. Values already unwrapped (as a ``boto3.resource`` Table would
    return, or as a test double may supply) pass through unchanged, so callers may inject
    either client flavour.
    """
    if isinstance(attr, dict):
        if "S" in attr:
            return attr["S"]
        if "N" in attr:
            return attr["N"]
        if "NULL" in attr:
            return None
        return None
    return attr


def _query_all(
    ddb_client: Any,
    *,
    table_name: str,
    index_name: str,
    key_attr: str,
    key_value: str,
) -> list[dict[str, Any]]:
    """Query *index_name* for ``key_attr = key_value``, paging to exhaustion.

    Pagination is followed rather than assumed away. A single-page read here would be the
    same defect this repo already shipped once in the Tier 2 Athena path, where an
    unfollowed continuation token truncated results at the 4th VIN and silently falsified an
    idempotency guarantee.

    Returns
    -------
    list[dict]
        Raw items in DynamoDB attribute-value form, across all pages.
    """
    items: list[dict[str, Any]] = []
    start_key: Optional[dict[str, Any]] = None

    while True:
        kwargs: dict[str, Any] = {
            "TableName": table_name,
            "IndexName": index_name,
            "KeyConditionExpression": f"{key_attr} = :k",
            "ExpressionAttributeValues": {":k": {"S": key_value}},
        }
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key

        resp = ddb_client.query(**kwargs)
        items.extend(resp.get("Items", []))

        start_key = resp.get("LastEvaluatedKey")
        if not start_key:
            return items


def _validated_verdict(item: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Return the verdict dict for *item*, ``None`` if it has none, or raise if invalid.

    Raises
    ------
    DmsWarrantyVerdictError
        If ``verdict_status`` is present but outside :data:`DMS_VERDICT_ENUM`.
    """
    status = _unwrap(item.get(ATTR_VERDICT_STATUS))
    if status is None or status == "":
        # No verdict written yet. Not an error — the claim exists, DMS has not ruled.
        return None

    if status not in DMS_VERDICT_ENUM:
        raise DmsWarrantyVerdictError(
            f"verdict_status {status!r} is not in DMS_VERDICT_ENUM "
            f"({sorted(DMS_VERDICT_ENUM)}); refusing to narrate an unrecognised verdict. "
            "Either DMS has added a verdict value and agents/health/dms_verdicts.py is "
            "stale, or this row is corrupt."
        )

    reason = _unwrap(item.get(ATTR_VERDICT_REASON))
    return {
        ATTR_VERDICT_STATUS: status,
        ATTR_VERDICT_REASON: reason if reason is not None else "",
        # Surfaced rather than discarded. The Tier 2 artifact contract in
        # `~/.kiro/steering/agentic-tiers.md` requires staleness to be visible to the
        # reader — a verdict carried into a Finding stamped `computed_at=now` with no way to
        # date the ruling is a stale answer presented as live.
        "verdict_updated_at": _unwrap(item.get(ATTR_UPDATED_AT)),
        "provenance": "live",
    }


# ---------------------------------------------------------------------------
# Module-level per-VIN fetch
# ---------------------------------------------------------------------------


def fetch_verdict(vin: str, *, ddb_client: Any) -> Optional[dict[str, Any]]:
    """Return the DMS warranty verdict for *vin*, or ``None`` when there is none.

    Resolves VIN → ``ro_id`` → claim across two index queries; see the module docstring for
    why a single-table lookup is not available.

    When a VIN has several repair orders — the common case for any vehicle that has been
    serviced more than once — every one is checked and the verdict from the
    **most-recently-updated** claim carrying one is returned. Ordering is by the claim's
    ``updated_at``, which DMS writes in the same ``update_item`` call that writes the
    verdict (``warranty_claims.py:436-452``), so the two cannot disagree. Claims with no
    verdict are skipped rather than treated as absent verdicts, which is what makes "latest
    ruling" correct instead of "latest claim".

    Parameters
    ----------
    vin:
        Vehicle Identification Number. Sanitised via
        :func:`agents.health._vin.sanitise_vin` for consistency with the three ADP sources.
        DynamoDB key conditions are parameterised, so this is defence in depth rather than
        injection prevention.
    ddb_client:
        boto3 DynamoDB **client** (low-level) or a stub. Injected so unit tests need no
        network. Must be created in DMS's region — ``us-west-2``, per DMS `decisions.md`
        2026-08-28, which records ``us-east-1`` as the wrong default.

    Returns
    -------
    dict | None
        ``{"verdict_status": str, "verdict_reason": str, "provenance": "live"}`` when a
        verdict exists, else ``None``. ``None`` means "no verdict" and must never be
        substituted with a default — see the module docstring's "Absence is not a verdict".

    Raises
    ------
    DmsWarrantyConfigError
        If either table-name env var is unset.
    DmsWarrantyVerdictError
        If a claim carries a ``verdict_status`` outside :data:`DMS_VERDICT_ENUM`.
    ValueError
        If *vin* contains no usable characters (from ``sanitise_vin``).
    """
    safe_vin = sanitise_vin(vin)
    claims_table = resolve_table_name(CLAIMS_TABLE_ENV_VAR)
    repair_orders_table = resolve_table_name(REPAIR_ORDERS_TABLE_ENV_VAR)

    # Hop 1: VIN → repair orders.
    ro_items = _query_all(
        ddb_client,
        table_name=repair_orders_table,
        index_name=REPAIR_ORDERS_VIN_INDEX,
        key_attr=ATTR_VEHICLE_VIN,
        key_value=safe_vin,
    )
    if not ro_items:
        log.debug("dms_warranty_source: no repair orders for VIN")
        return None

    ro_ids = [rid for rid in (_unwrap(item.get(ATTR_RO_ID)) for item in ro_items) if rid]
    if not ro_ids:
        log.warning(
            "dms_warranty_source: %d repair-order row(s) carried no ro_id; "
            "cannot reach the claims table.",
            len(ro_items),
        )
        return None

    # Hop 2: each repair order → its warranty claim(s).
    max_age_days = resolve_max_age_days()
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    stale_seen = 0

    for ro_id in ro_ids:
        for claim in _query_all(
            ddb_client,
            table_name=claims_table,
            index_name=CLAIMS_RO_INDEX,
            key_attr=ATTR_RO_ID,
            key_value=ro_id,
        ):
            verdict = _validated_verdict(claim)
            if verdict is None:
                continue
            updated_at = _parse_updated_at(verdict.get("verdict_updated_at"))
            if not _is_within_window(updated_at, max_age_days):
                stale_seen += 1
                continue
            # `updated_at` is non-None here: _is_within_window returns False for None.
            candidates.append((updated_at, verdict))

    if stale_seen:
        log.info(
            "dms_warranty_source: %d verdict(s) outside the %d-day window were not "
            "narrated (undated verdicts count as outside).",
            stale_seen,
            max_age_days,
        )

    if not candidates:
        log.debug(
            "dms_warranty_source: %d repair order(s), no in-window claim with a verdict.",
            len(ro_ids),
        )
        return None

    # Most-recently-updated verdict wins. Sorting on the parsed datetime rather than the raw
    # string: the string sort was correct only because DMS writes a fixed `+00:00` offset,
    # which made the ordering depend on a producer formatting detail rather than on the
    # instant. Parsing is already done for the recency bound, so this costs nothing.
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return candidates[0][1]


# ---------------------------------------------------------------------------
# Batch class — follows the FindingSource shape of adp_brake_source.AdpBrakeSource
# ---------------------------------------------------------------------------


class DmsWarrantySource:
    """Batch reader for DMS warranty verdicts across a VIN set.

    Follows the shape of :class:`agents.health.adp_brake_source.AdpBrakeSource` — a batch
    class beside a module-level per-VIN function.

    **Not registered as a Tier 2 component source, deliberately.** The three ADP sources
    each produce a *component finding* that Agent 2 assesses (battery, tires, brakes). A
    warranty verdict is not a component and is not assessed — it is narrated verbatim
    alongside whichever component finding the visit concerned. Registering it as a
    ``FindingSource`` in ``agents/tier2/contract.py``'s ``VALID_COMPONENTS`` would create a
    fourth pseudo-component with no assessment logic behind it. The shape is matched so the
    runner *can* consume it if a later spec decides otherwise.

    Lifetime: one instance per Tier 2 run; the DynamoDB client is injected.

    Parameters
    ----------
    ddb_client:
        boto3 DynamoDB client (low-level) or stub, created in DMS's region (``us-west-2``).
    """

    def __init__(self, *, ddb_client: Any) -> None:
        self._ddb = ddb_client

    def fetch_verdicts(self, vins: list[str]) -> dict[str, dict[str, Any]]:
        """Return ``{vin: verdict}`` for every VIN in *vins* that has one.

        VINs with no verdict are **absent from the mapping** rather than mapped to ``None``,
        so ``mapping.get(vin)`` and "no verdict" coincide and a caller cannot accidentally
        treat a present-but-empty entry as a ruling.

        A failure on one VIN does not fail the batch: the error is logged and that VIN is
        omitted. One corrupt claim row must not suppress every other vehicle's verdict.
        """
        out: dict[str, dict[str, Any]] = {}
        for vin in vins:
            try:
                verdict = fetch_verdict(vin, ddb_client=self._ddb)
            except DmsWarrantyConfigError:
                # Configuration is not per-VIN — if the table name is missing it is missing
                # for every VIN, so continuing would log identical errors N times.
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "dms_warranty_source: verdict fetch failed for one VIN (%s: %s); "
                    "omitting it from the batch.",
                    type(exc).__name__,
                    exc,
                )
                continue
            if verdict is not None:
                out[vin] = verdict
        return out

    def fetch_findings(self, vins: Optional[list[str]]) -> list[dict[str, Any]]:
        """Return verdict rows for *vins*, in the list-of-dicts shape the runner expects.

        Present for ``FindingSource`` shape-compatibility. ``vins=None`` returns an empty
        list rather than scanning the fleet: there is no VIN-keyed index on the claims table
        (see the module docstring), so "all VINs" would require a full ``Scan`` the IAM
        grant does not authorise. Returning ``[]`` fails closed and visibly, instead of
        issuing a call that will be denied.
        """
        if not vins:
            return []
        return [
            {ATTR_VEHICLE_VIN: vin, **verdict}
            for vin, verdict in self.fetch_verdicts(vins).items()
        ]
