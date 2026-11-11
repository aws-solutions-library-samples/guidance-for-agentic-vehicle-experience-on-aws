"""write.py — Finding builders and write helpers for fi-rebalancing (G4-D7, G4-D8, G4-D9).

Pure functions where possible; no AWS calls except read_live_hash.
"""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

_AGENT_ID = "agent-fi-rebalancing"
_SCOPE_FIELD = "fleet_id"


# ---------------------------------------------------------------------------
# Headline helpers
# ---------------------------------------------------------------------------

_MAX_HEADLINE_CHARS = 120  # Finding schema ``headline.maxLength``

#: Part of every ``inputs_hash`` summary. Bump it whenever the headline wording changes:
#: the headline itself stays out of the hash (W6: it carries the absolute deficit day), so
#: without this a row whose inputs are unchanged only has its expiry refreshed and keeps
#: the old wording. 2 = names kept past 120 characters, singular counts (issue
#: 2026-11-04-rebalancing-headline-location-ids).
_HEADLINE_FORMAT = 2


def _count(n: int, noun: str) -> str:
    """``"1 vehicle"``, ``"2 vehicles"``."""
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _move_headline(
    count: int,
    from_name: str,
    to_name: str,
    vehicles_short: int,
    first_deficit_day: str,
    from_id: str,
    to_id: str,
) -> str:
    """The first of these that fits in 120 characters (contract §2.1, amended 2026-11-04):

    1. ``Move <n> vehicles from <from> to <to>: <to> short <k> vehicles from <day>``
    2. ``Move <n> vehicles from <from> to <to>: short <k> vehicles from <day>``
    3. ``Move <n> vehicles from <from> to <to>: short <k> vehicles``
    4. form 1 with location ids, only when the names can't fit at all.

    Names are kept before the shortfall's wording is: a "Car-Share Hub" destination made
    form 1 121-122 characters and sent it straight to ids (issue
    2026-11-04-rebalancing-headline-location-ids). Every form keeps the
    ``from <from> to <to>: `` shape, because CMS's Action Queue reads the From and To lines
    out of it (``parseMoveLocations`` in ``ActionQueuePanel.tsx``, falling back to the ids).
    """
    move = f"Move {_count(count, 'vehicle')}"
    short = f"short {_count(vehicles_short, 'vehicle')}"
    dated = f"{short} from {first_deficit_day}"
    forms = (
        f"{move} from {from_name} to {to_name}: {to_name} {dated}",
        f"{move} from {from_name} to {to_name}: {dated}",
        f"{move} from {from_name} to {to_name}: {short}",
    )
    for hl in forms:
        if len(hl) <= _MAX_HEADLINE_CHARS:
            return hl
    return f"{move} from {from_id} to {to_id}: {to_id} {dated}"


def _gap_deficit_headline(name: str, vehicles_short: int, first_deficit_day: str) -> str:
    return (f"{name}: short {_count(vehicles_short, 'vehicle')} from {first_deficit_day}, "
            "no move recommended")


def _gap_surplus_headline(name: str, vehicles_spare: int) -> str:
    return f"{name}: {_count(vehicles_spare, 'spare vehicle')}, no location in the metro is short"


# ---------------------------------------------------------------------------
# inputs_hash summary (G4-D8)
# ---------------------------------------------------------------------------

def _net_bucket(net_usd: float) -> int:
    """Floor net benefit to nearest $25 bucket."""
    return int(math.floor(net_usd / 25.0)) * 25


def _deficit_day_offset(first_deficit_day: str, run_date: str) -> int | None:
    """Days from run_date to first_deficit_day; None when either is absent.

    W6: hashing the absolute date makes every daily run produce a new hash for
    an unchanged deficit.  An offset from run_date is stable for a standing
    deficit whose first projected shortfall day does not move relative to the
    run window.
    """
    if not first_deficit_day or not run_date:
        return None
    from datetime import date
    try:
        return (date.fromisoformat(first_deficit_day) - date.fromisoformat(run_date)).days
    except (ValueError, TypeError):
        return None


def _move_summary(
    *,
    plan: dict,
    candidate: dict,
    subject_key: str,
    agent_version: str,
    run_date: str,
    config_version: str = "1",
) -> dict:
    """Build the G4-D8 summary dict for compute_inputs_hash.

    W6 changes vs original:
    - ``eligible_vins`` dropped (turns over daily; a VIN change is captured by
      ``vehicle_vins`` which stays).
    - ``headline`` dropped (contained the absolute first deficit day).
    - ``first_deficit_day`` replaced by ``first_deficit_day_offset`` (days from
      run_date) so a standing deficit whose shortfall day doesn't move produces
      the same hash on consecutive daily runs.
    """
    vins: list[str] = plan.get("vehicle_vins", [])
    transfer_cents = int(round(float(plan.get("transfer_cost_usd", 0)) * 100))
    return {
        "agent_version": agent_version,
        "config_version": config_version,
        "headline_format": _HEADLINE_FORMAT,
        "kind": "fleet.rebalance.move",
        "subject_key": subject_key,
        "from_location_id": plan.get("from_location_id"),
        "to_location_id": plan.get("to_location_id"),
        "forecast_shortfall": candidate.get("vehicles_short", 0),
        "count": len(vins),
        "vehicle_vins": vins,
        "severity": candidate.get("severity"),
        "confidence": candidate.get("confidence"),
        "first_deficit_day_offset": _deficit_day_offset(
            candidate.get("first_deficit_day", ""), run_date),
        "transfer_cost_cents": transfer_cents,
        "net_benefit_bucket": _net_bucket(float(plan.get("net_benefit_usd", 0))),
    }


def _gap_summary(
    *,
    gap_item: dict,
    subject_key: str,
    agent_version: str,
    run_date: str,
    config_version: str = "1",
) -> dict:
    """Build the G4-D8 summary dict for a gap finding.

    W6: surplus gaps have no first deficit day in the summary (none exists;
    putting run_date+1 would change every day).  Deficit gaps use the offset
    from run_date for the same reason as move summaries.
    """
    is_surplus = gap_item.get("gap_kind") == "surplus"
    base = {
        "agent_version": agent_version,
        "config_version": config_version,
        "headline_format": _HEADLINE_FORMAT,
        "kind": "fleet.rebalance.gap",
        "subject_key": subject_key,
        "location_id": gap_item.get("location_id"),
        "forecast_shortfall": gap_item.get("vehicles_short", 0),
        "vehicles_spare": gap_item.get("vehicles_spare", 0),
        "severity": gap_item.get("severity"),
        "confidence": gap_item.get("confidence"),
    }
    if not is_surplus:
        base["first_deficit_day_offset"] = _deficit_day_offset(
            gap_item.get("first_deficit_day", ""), run_date)
    return base


# ---------------------------------------------------------------------------
# expires_at helper
# ---------------------------------------------------------------------------

def _expires_at(computed_at: str) -> str:
    """Return computed_at + 2 days as an ISO-8601 UTC 'Z' string."""
    dt = datetime.fromisoformat(computed_at.replace("Z", "+00:00"))
    exp = dt + timedelta(days=2)
    return exp.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Public finding builders
# ---------------------------------------------------------------------------

def build_move_finding(
    plan: dict,
    candidate: dict,
    *,
    fleet_id: str,
    from_location: dict,
    to_location: dict,
    evidence: list,
    computed_at: str,
    agent_version: str,
    run_date: str = "",
) -> dict:
    """Build and validate a fleet.rebalance.move Finding (G4-D7).

    Raises ValueError on missing required fields or schema validation failure.
    """
    from agents.fi_rebalancing.evidence import compute_inputs_hash
    from agents.tier2.finding_transform import _validate_finding
    from agents.tier2.writer import compute_finding_key

    # Required field checks
    for field in ("vehicle_vins", "from_location_id", "to_location_id",
                  "net_benefit_usd", "transfer_cost_usd",
                  "predicted_on_rent_hours_gain", "predicted_turnaways_avoided"):
        if field not in plan:
            raise ValueError(f"plan missing required field: {field!r}")
    for field in ("vehicles_short", "eligible_vins", "first_deficit_day",
                  "forecast_week", "severity", "confidence", "effective_date"):
        if field not in candidate:
            raise ValueError(f"candidate missing required field: {field!r}")
    for loc, name in ((from_location, "from_location"), (to_location, "to_location")):
        for f in ("locationId", "name"):
            if f not in loc:
                raise ValueError(f"{name} missing field: {f!r}")

    vins: list[str] = plan["vehicle_vins"]
    if not vins:
        raise ValueError("plan.vehicle_vins is empty")

    from_id: str = plan["from_location_id"]
    to_id: str = plan["to_location_id"]
    from_name: str = from_location["name"]
    to_name: str = to_location["name"]
    forecast_week: str = candidate["forecast_week"]
    first_deficit_day: str = candidate["first_deficit_day"]
    vehicles_short: int = int(candidate["vehicles_short"])
    effective_date: str = candidate["effective_date"]

    subject_key = f"{from_id}/{to_id}/{forecast_week}"
    headline = _move_headline(
        len(vins), from_name, to_name, vehicles_short, first_deficit_day, from_id, to_id
    )

    summary = _move_summary(
        plan=plan, candidate=candidate,
        subject_key=subject_key, agent_version=agent_version, run_date=run_date,
    )
    inputs_hash = compute_inputs_hash(summary)

    finding_key = compute_finding_key(
        agent_id=_AGENT_ID,
        scope_field=_SCOPE_FIELD,
        scope_value=fleet_id,
        finding_kind="fleet.rebalance.move",
        subject_key=subject_key,
    )

    rec_action = {
        "action_kind": "move_vehicles",
        "target_system": "cms",
        "parameters": {
            "fleet_id": fleet_id,
            "from_location_id": from_id,
            "to_location_id": to_id,
            "vehicle_vins": vins,
            "forecast_week": forecast_week,
            "transfer_cost_usd": round(float(plan["transfer_cost_usd"]), 2),
            "predicted_on_rent_hours_gain": round(
                float(plan["predicted_on_rent_hours_gain"]), 2
            ),
            "predicted_turnaways_avoided": int(plan["predicted_turnaways_avoided"]),
        },
        "estimated_value_usd": round(float(plan["net_benefit_usd"]), 2),
        "estimated_value_basis": (
            f"forecast on-rent hours recovered at {to_name} less lost at {from_name}, "
            f"at location-type rates, less transfer cost, 7 days from {effective_date}"
        ),
    }

    finding: dict[str, Any] = {
        "finding_id": f"FIND-{uuid.uuid4()}",
        "finding_key": finding_key,
        "agent_id": _AGENT_ID,
        "agent_version": agent_version,
        "scope": {_SCOPE_FIELD: fleet_id},
        "severity": candidate["severity"],
        "computed_at": computed_at,
        "expires_at": _expires_at(computed_at),
        "confidence": candidate["confidence"],
        "finding_kind": "fleet.rebalance.move",
        "headline": headline,
        "detail": plan.get("detail", ""),
        "evidence": evidence,
        "inputs_hash": inputs_hash,
        "tokens_used": 0,
        "cost_usd": 0.0,
        "audit_trail": [],
        "subject_key": subject_key,
        "recommended_action": rec_action,
        "location": {"from_location_id": from_id, "to_location_id": to_id},
    }

    _validate_finding(finding)
    return finding


def build_gap_finding(
    gap_item: dict,
    *,
    fleet_id: str,
    location: dict,
    evidence: list,
    computed_at: str,
    agent_version: str,
    run_date: str = "",
) -> dict:
    """Build and validate a fleet.rebalance.gap Finding (G4-D7).

    Handles both deficit-no-move and surplus-no-deficit cases.
    Raises ValueError on missing required fields or schema validation failure.
    """
    from agents.fi_rebalancing.evidence import compute_inputs_hash
    from agents.tier2.finding_transform import _validate_finding
    from agents.tier2.writer import compute_finding_key

    for field in ("location_id", "gap_kind", "forecast_week", "severity", "confidence"):
        if field not in gap_item:
            raise ValueError(f"gap_item missing required field: {field!r}")
    for f in ("locationId", "name"):
        if f not in location:
            raise ValueError(f"location missing field: {f!r}")

    loc_id: str = gap_item["location_id"]
    loc_name: str = location["name"]
    forecast_week: str = gap_item["forecast_week"]
    first_deficit_day: str = gap_item.get("first_deficit_day", "")
    severity: str = gap_item["severity"]

    # Which gap this is is stated by the caller, never inferred from which counts are set.
    if gap_item["gap_kind"] not in ("deficit", "surplus"):
        raise ValueError(f"gap_kind must be 'deficit' or 'surplus', got {gap_item['gap_kind']!r}")
    is_surplus = gap_item["gap_kind"] == "surplus"

    if is_surplus:
        vehicles_spare = int(gap_item["vehicles_spare"])
        headline = _gap_surplus_headline(loc_name, vehicles_spare)
        severity = "routine"
    else:
        vehicles_short = int(gap_item["vehicles_short"])
        if not first_deficit_day:
            raise ValueError("gap_item missing first_deficit_day for deficit gap")
        headline = _gap_deficit_headline(loc_name, vehicles_short, first_deficit_day)

    subject_key = f"{loc_id}/{forecast_week}"

    summary = _gap_summary(
        gap_item={**gap_item, "severity": severity},
        subject_key=subject_key,
        agent_version=agent_version,
        run_date=run_date,
    )
    inputs_hash = compute_inputs_hash(summary)

    finding_key = compute_finding_key(
        agent_id=_AGENT_ID,
        scope_field=_SCOPE_FIELD,
        scope_value=fleet_id,
        finding_kind="fleet.rebalance.gap",
        subject_key=subject_key,
    )

    finding: dict[str, Any] = {
        "finding_id": f"FIND-{uuid.uuid4()}",
        "finding_key": finding_key,
        "agent_id": _AGENT_ID,
        "agent_version": agent_version,
        "scope": {_SCOPE_FIELD: fleet_id},
        "severity": severity,
        "computed_at": computed_at,
        "expires_at": _expires_at(computed_at),
        "confidence": gap_item["confidence"],
        "finding_kind": "fleet.rebalance.gap",
        "headline": headline,
        "detail": gap_item.get("detail", "Rebalancing gap item."),
        "evidence": evidence,
        "inputs_hash": inputs_hash,
        "tokens_used": 0,
        "cost_usd": 0.0,
        "audit_trail": [],
        "subject_key": subject_key,
        "location": {"location_id": loc_id},
    }

    _validate_finding(finding)
    return finding


# ---------------------------------------------------------------------------
# Finding item list builder (deduplication)
# ---------------------------------------------------------------------------

def build_finding_items(signals: list[dict], *, fleet_id: str) -> list[dict]:
    """Deduplicate signals by subject_key ``<from>/<to>/<week>``.

    Returns one dict per unique subject_key with the fields needed to identify
    the item downstream.  For move signals: ``subject_key``, ``from_location_id``,
    ``to_location_id``, ``forecast_week``, ``vehicle_vins``.
    """
    seen: dict[str, dict] = {}
    for sig in signals:
        fid = sig.get("from_location_id", "")
        tid = sig.get("to_location_id", "")
        week = sig.get("forecast_week", "")
        key = f"{fid}/{tid}/{week}"
        if key not in seen:
            seen[key] = {
                "subject_key": key,
                "from_location_id": fid,
                "to_location_id": tid,
                "forecast_week": week,
                "vehicle_vins": sig.get("vehicle_vins", []),
                "fleet_id": fleet_id,
            }
    return list(seen.values())


# ---------------------------------------------------------------------------
# Write helpers (G4-D9)
# ---------------------------------------------------------------------------

def write_finding_if_changed(
    *,
    live_item_hash: str | None,
    new_hash: str,
    finding: dict,
    writer: Any,
    refresher: Any = None,
) -> None:
    """Call writer or refresher based on hash comparison (G4-D9).

    - Equal hashes: call only refresher (keeps expiry alive).
    - Different (or no live) hash: call writer once.
    """
    if live_item_hash == new_hash:
        if refresher is not None:
            refresher(finding)
    else:
        writer(finding)


def read_live_hash(
    finding_key: str,
    *,
    ddb_client: Any,
    table_name: str,
    now_str: str,
) -> str | None:
    """Read the live row's inputs_hash by finding_key via the GSI (G4-D9).

    Returns the inputs_hash string or None if no live row exists.
    """
    from agents.tier2.writer import _query_live_row

    row = _query_live_row(
        finding_key=finding_key,
        table_name=table_name,
        dynamodb_client=ddb_client,
        now_str=now_str,
    )
    if row is None:
        return None
    return row.get("inputs_hash")



# ---------------------------------------------------------------------------
# Agent-side DDB-only attribute projection registry.
#
# The rebalance agent writes top-level DDB attributes on move Findings that
# are NOT in the frozen Finding schema (``additionalProperties: false``).
# The response-boundary strip in ``lambdas/api-avx-findings/handler.py``
# (``_PROJECTED_SCOPE_ATTRS``) must include every one of these so that
# strict-validating consumers do not reject the response (Fix Group 30
# defect class; see the ``metro_inputs_hash`` entry in that frozen set).
#
# This registry exists so a cross-repo guard test
# (``test_strip_list_covers_every_writer_projected_attr`` in
# ``lambdas/api-avx-findings/tests/test_handler.py``) can parse the agent-
# side projections at source and fail CI if a new entry is added here
# without the matching strip-list extension.  Add a one-line entry below
# whenever a new top-level DDB attribute is projected by this agent;
# remove when retired.
_AGENT_PROJECTED_ATTRS: tuple[str, ...] = (
    "metro_inputs_hash",  # G6 G3 / spec D8 step 5; written in _put_item payload
                          # injection and converge via set_metro_inputs_hash on
                          # the UPDATE path.  On move Findings and, since prompt
                          # 40, on the deficit gap Findings of planned metros.
    # NOTE: "ttl" was removed from this registry (FA1 Fix Group 1, 2026-10-27).
    # It belongs in writer.py's _STORAGE_ONLY_ATTRS, not in the agent-side
    # registry.  The guard test now parses _STORAGE_ONLY_ATTRS from writer.py.
)

def compute_metro_inputs_hash(metro_bundle: dict) -> str:
    """SHA-256 of canonical JSON of a metro's **deterministic model inputs**.

    Hashed fields are the ones the model actually reasons over, chosen so
    the hash is stable across daily re-runs when the underlying data has
    not changed:

    * ``candidates`` — the metro's candidate dicts (``candidate_id``,
      ``from_location_id``, ``to_location_id``, ``count``,
      ``eligible_vins``).  Sorted by ``candidate_id`` so upstream
      page-order drift (``build_candidates`` reads gaps rows and
      iterates in that order) does not silently change the hash.
      ``eligible_vins`` is also sorted so a VIN pool permutation
      doesn't change the hash.
    * ``gap_ids`` — the metro's deficit gap ids (``<location>/<week>``).
    * ``sources_by_id`` — per-source ``supply_vehicles``, ``op_hours`` and
      ``forecast_hours``.  All three come from the gaps and forecast
      reads; the forecast and gaps run date differs only when the data
      actually advances.
    * ``evidence_kinds`` — the fixed list of move evidence kinds passed to
      the model; included so a code change to that list produces a fresh
      hash (``_MOVE_EVIDENCE_KINDS`` in ``agent.py``).  Sorted for the
      same defensive reason as ``candidates``.

    **Deliberately excluded**: ``item_context.today`` and ``events``.
    * ``today`` is a per-call date, and D8 step 5's intent is "unchanged
      inputs across days", so a daily-rolling value would never match and
      the skip would never fire — including it would effectively disable
      the skip in production.
    * ``events`` contains metro events for the forecast window but can
      pick up small ordering drift from Athena pages; excluding them
      keeps the hash stable in the one case the skip is meant to catch
      (re-run with ``--use-run`` or an identical daily snapshot).

    The hash is written to the move Finding as a top-level DDB attribute
    (``metro_inputs_hash``), NOT a schema property — the schema's
    ``additionalProperties: false`` applies to the artifact, not the
    stored row, matching the T10.2 scope-denormalisation pattern.
    """
    from agents.fi_rebalancing.evidence import compute_inputs_hash

    canonical: dict[str, Any] = {
        "candidates": sorted(
            (
                {
                    "candidate_id": c.get("candidate_id"),
                    "from_location_id": c.get("from_location_id"),
                    "to_location_id": c.get("to_location_id"),
                    "count": int(c.get("count", 0)),
                    "eligible_vins": sorted(c.get("eligible_vins", [])),
                }
                for c in metro_bundle.get("candidates", [])
            ),
            key=lambda c: c.get("candidate_id") or "",
        ),
        "gap_ids": sorted(metro_bundle.get("gap_ids", [])),
        "sources_by_id": {
            sid: {
                "supply_vehicles": int(sv.get("supply_vehicles", 0)),
                "op_hours": float(sv.get("op_hours", 0.0)),
                "forecast_hours": float(sv.get("forecast_hours", 0.0)),
            }
            for sid, sv in sorted(metro_bundle.get("sources_by_id", {}).items())
        },
        "evidence_kinds": sorted(metro_bundle.get("evidence_kinds", [])),
    }
    return compute_inputs_hash(canonical)


def read_live_item(
    finding_key: str,
    *,
    ddb_client: Any,
    table_name: str,
    now_str: str,
) -> dict | None:
    """The live (non-expired) row for ``finding_key`` as a plain dict, or ``None``.

    Same Query, filter and GSI as :func:`read_live_hash`.  D8 step 5 (prompt 40): when a
    metro's inputs are unchanged, the agent rebuilds that metro's plans and holds from
    these rows instead of planning it again.
    """
    from agents.tier2.writer import _query_live_row

    return _query_live_row(
        finding_key=finding_key,
        table_name=table_name,
        dynamodb_client=ddb_client,
        now_str=now_str,
    )



def set_metro_inputs_hash(
    *,
    finding_key: str,
    metro_inputs_hash: str,
    ddb_client: Any,
    table_name: str,
    now_str: str,
) -> bool:
    """Force ``metro_inputs_hash`` onto the live row for ``finding_key``.

    Fixes Fix Group 1 Warning W1: the shared ``agents.tier2.writer._update_item``
    does NOT include ``metro_inputs_hash`` in its UpdateExpression, so a
    row that was written by ``_put_item`` (fresh row) and then re-written
    by ``_update_item`` (next run with changed per-item inputs) keeps the
    OLD metro_hash on the stored row.  Without this helper the skip-metro
    read would see a stale hash and the skip would stop firing after the
    first per-item change inside the 8-day TTL.

    Why not add to ``_FI_REEMIT_ATTRS``: tier2 code is shared across every
    FI agent and the user's constraint on this fix is "rebalance agent's
    Lambda code only".  This agent-side helper runs AFTER the main
    writer and ensures the attribute converges to the current value
    regardless of which path (PUT or UPDATE) the main writer took.

    Two DDB calls (Query the by_finding_key GSI, then UpdateItem by
    finding_id); runs once per move and deficit gap Finding of a planned
    metro.  Noop
    when no live row exists (the main writer wrote one, so this is
    expected to find it, but if the row was expired between the two
    calls — which cannot happen inside a single run because the TTL is
    2 days and a run finishes in seconds — the helper leaves the state
    alone).

    Fix Group 2 (security review S1 + S2):

    * ``ConditionExpression="attribute_exists(finding_id)"`` on the
      ``UpdateItem`` so a stub row with only ``finding_id`` +
      ``metro_inputs_hash`` can never be created on a Query→UpdateItem
      race (vanishingly unlikely with the 2-day TTL and no auto-TTL,
      but defence-in-depth; matches ``_refresh_expiry``'s pattern in
      ``agents/tier2/writer.py``).  A failing condition is treated as
      "no live row" and the function returns ``False``.
    * Shape check at entry: ``metro_inputs_hash`` must be a 64-char
      lowercase hex string (SHA-256 output).  A caller that passed a
      different-length or non-hex value would silently disable the
      skip on the next run (the stored value would never equal a fresh
      compute).  Fail loud with
      ``ValueError`` instead.

    Returns ``True`` iff the UpdateItem ran.  A ``False`` return means
    no live row was found (benign; the next write re-tries).
    """
    import re
    from agents.tier2.writer import _query_live_row

    if not isinstance(metro_inputs_hash, str) or not re.fullmatch(
        r"[0-9a-f]{64}", metro_inputs_hash
    ):
        raise ValueError(
            f"metro_inputs_hash must be a 64-char lowercase hex SHA-256 digest; "
            f"got {metro_inputs_hash!r}"
        )

    row = _query_live_row(
        finding_key=finding_key,
        table_name=table_name,
        dynamodb_client=ddb_client,
        now_str=now_str,
    )
    if row is None:
        return False
    finding_id = row.get("finding_id")
    if not finding_id:
        return False
    try:
        ddb_client.update_item(
            TableName=table_name,
            Key={"finding_id": {"S": finding_id}},
            UpdateExpression="SET metro_inputs_hash = :mh",
            ConditionExpression="attribute_exists(finding_id)",
            ExpressionAttributeValues={":mh": {"S": metro_inputs_hash}},
        )
    except Exception as exc:  # noqa: BLE001 — only the condition failure is expected
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            # Race: the row was deleted between Query and UpdateItem.  Benign;
            # the next write will re-try.
            return False
        raise
    return True
