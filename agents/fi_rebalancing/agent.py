"""agent.py — run_rebalance: rules-only daily FI rebalancing run (spec G4-D1..D10, task 4.1e).

No model calls, no direct AWS calls beyond the injected clients.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable

from agents.fi_rebalancing import (
    candidates as cands_mod,
    eligibility,
    evidence as ev_mod,
    sources,
    write as write_mod,
)
from agents.fi_rebalancing.candidates import money_for
from agents.fi_rebalancing import policy as rebal_policy
from agents.fi_rebalancing.fallback import rules_fallback
from agents.fi_rebalancing.llm import ModelStats, plan_rebalance
from agents.fi_rebalancing.selection import select_deficit_locations
from agents.fi_rebalancing.sources import SourceReadError
from agents.fi_rebalancing.validate import ValidationError

log = logging.getLogger(__name__)

# D8 step 6 bound: at most this many Converse calls across a run (one per
# metro, with at most one retry inside the per-metro call via
# ``run_with_validator_fallback``).  The counter is incremented on every
# attempt (including retries), so the practical ceiling on *metros* served by
# the model is at most ``max_model_calls_per_run`` and at least
# ``max_model_calls_per_run // 2``.  U9's default is 12.
_DEFAULT_MAX_MODEL_CALLS: int = 12

# The MOVE evidence kinds the model may reference in ``evidence_refs`` for a
# move.  Must match the set that :func:`move_evidence` emits (and that D10
# § "Evidence keys" lists for move items).  A model-named kind outside this
# set fails validation.
_MOVE_EVIDENCE_KINDS: tuple[str, ...] = (
    "rebalance.forecast",
    "rebalance.supply",
    "rebalance.reservations",
    "rebalance.events",
    "rebalance.transfer",
    "rebalance.revenue",
    "rebalance.vehicle",
    "rebalance.recent_outcomes",
    "rebalance.diagnosis_source",
    "rebalance.hold",
)


def run_rebalance(
    *,
    stage: str,
    athena_client: Any,
    workgroup: str,
    results_s3_path: str,
    ddb_client: Any,
    findings_table: str,
    vehicles_table: str,
    groundings_table: str,
    fleet_locations_table: str,
    write_fn: Callable,
    refresh_fn: Callable,
    now: datetime,
    config: dict | None = None,
    fleet_id: str = "demo-rental-carshare",
    agent_version: str = "1.0.0",
    bedrock_client: Any = None,
    model_id: str = "",
    guardrail_id: str = "",
    guardrail_version: str = "",
) -> dict:
    """Execute one rebalancing run; return stats dict.

    ``now`` must be timezone-aware UTC.  All source read errors propagate
    immediately (SourceReadError) except per-vehicle grounding/open-items reads
    which are counted and make that vehicle ineligible.

    Model-call parameters are optional (default: no model path, rules only).
    When all four are supplied — ``bedrock_client``, ``model_id``,
    ``guardrail_id``, ``guardrail_version`` — the agent uses D8 step 6's
    per-metro model call (one Converse per metro with a candidate, up to the
    configured ``max_model_calls_per_run`` ceiling); the rules fallback runs
    once per metro that could not be served by the model (either because the
    model exhausted its two attempts or because the budget was reached).
    """
    # U9 thresholds from the run's policy (agents.fi_rebalancing.policy, loaded from SSM by
    # the runner); *config* overrides individual keys (tests). No threshold is written here.
    cfg = {**rebal_policy.current(), **(config or {})}
    now_utc = now.astimezone(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")
    now_str = now_utc.strftime("%Y-%m-%d %H:%M:%S")
    now_iso_z = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    computed_at = now_iso_z

    stats: dict[str, int] = {
        "deficits_selected": 0,
        "surplus_locations": 0,
        "locations_incomplete": 0,
        "vehicles_checked": 0,
        "vehicles_eligible": 0,
        "grounding_read_errors": 0,
        "open_items_read_errors": 0,
        "candidates_built": 0,
        "moves_planned": 0,
        "move_items_written": 0,
        "gap_items_written": 0,
        "items_unchanged": 0,
        "items_refused": 0,
        "gap_items_without_vehicle": 0,
        "forecast_days_missing": 0,
        # W-c3-1: deficit locations and move candidates build_candidates dropped for a
        # missing forecast day (a stale forecast drops every move here); the gap loops
        # count their own misses in forecast_days_missing.
        "candidates_forecast_days_missing": 0,
        "model_calls": 0,
        # D8 step 5: metros whose live items carry the same metro_inputs_hash so the
        # model call (and any writes) are skipped for that metro.  Counts metros, not
        # candidates — one skip per metro.  Issue
        # ``issues/2026-10-18-fi-rebalance-rerun-calls-model/``.
        "metros_skipped_unchanged": 0,
        # F1.3 W2: whole move plans dropped because a VIN appeared in an earlier
        # plan of the same run (across any planning path: model, rules, reused).
        "plans_deduped": 0,
    }

    # -----------------------------------------------------------------------
    # 1. Locations (abort on failure)
    # -----------------------------------------------------------------------
    locations, incomplete_ids, metros_with_incomplete = sources.read_locations(
        fleet_id, ddb_client=ddb_client, table=fleet_locations_table
    )
    stats["locations_incomplete"] = len(incomplete_ids)
    loc_by_id: dict[str, dict] = {loc["locationId"]: loc for loc in locations}

    # -----------------------------------------------------------------------
    # 2. Shared Athena reads (all abort on SourceReadError)
    # -----------------------------------------------------------------------
    _aq = dict(athena_client=athena_client, workgroup=workgroup,
               results_s3_path=results_s3_path)

    gaps = sources.read_gaps(stage=stage, fleet_id=fleet_id, **_aq)
    forecast = sources.read_forecast_days(stage=stage, fleet_id=fleet_id, **_aq)
    if not forecast:
        raise SourceReadError("forecast read returned no rows — cannot determine run_date (W5)")
    outcomes = sources.read_outcomes(stage=stage, fleet_id=fleet_id, **_aq)
    vehicle_days = sources.read_vehicle_days(stage=stage, fleet_id=fleet_id, **_aq)
    utilization = sources.read_vehicle_utilization(stage=stage, fleet_id=fleet_id, **_aq)
    busy_windows = sources.read_busy_windows(
        stage=stage, fleet_id=fleet_id, now=now_str, **_aq
    )
    request_lengths = sources.read_request_lengths(
        stage=stage, fleet_id=fleet_id, now=now_str, **_aq
    )
    events = sources.read_events(stage=stage, today=today_str, **_aq)
    open_ros = sources.read_open_ros(stage=stage, **_aq)
    service_soon = sources.read_service_soon(stage=stage, today=today_str, **_aq)

    # -----------------------------------------------------------------------
    # 3. Identify surplus locations and vehicles at each
    # -----------------------------------------------------------------------
    gap_by_loc: dict[str, dict] = {g["location_id"]: g for g in gaps}
    # D8 step 1: deficits come from the gaps view's rows only (selection.select_deficit_locations).
    deficits = {g["location_id"] for g in select_deficit_locations(gaps)}
    surplus_locs = {g["location_id"] for g in gaps
                    if g["status"] == "surplus" and g.get("vehicles_spare", 0) > 0}

    # metros that have a deficit
    deficit_metros: set[str] = set()
    for lid in deficits:
        loc = loc_by_id.get(lid)
        if loc:
            deficit_metros.add(loc["metro"])

    stats["deficits_selected"] = len(deficits)
    stats["surplus_locations"] = len(surplus_locs)

    # Build VIN→location index from vehicle_days
    vin_location: dict[str, str] = {r["vin"]: r["location_id"] for r in vehicle_days}

    # Vehicles to check: those at a surplus location in a metro that has a deficit
    vins_to_check: list[str] = []
    for r in vehicle_days:
        vin, lid = r["vin"], r["location_id"]
        if lid not in surplus_locs:
            continue
        loc = loc_by_id.get(lid)
        if loc is None:
            continue
        if loc["metro"] in deficit_metros:
            vins_to_check.append(vin)

    stats["vehicles_checked"] = len(vins_to_check)

    # -----------------------------------------------------------------------
    # 4. Per-vehicle: grounding + open-items + facts → eligibility
    # -----------------------------------------------------------------------
    eligible_by_location: dict[str, list[dict]] = defaultdict(list)
    checked_by_location: dict[str, list[dict]] = defaultdict(list)

    for vin in vins_to_check:
        lid = vin_location[vin]

        # grounding
        is_grounded, grounding_err = sources.read_grounding(
            vin,
            ddb_client=ddb_client,
            vehicles_table=vehicles_table,
            groundings_table=groundings_table,
        )
        if grounding_err:
            stats["grounding_read_errors"] += 1

        # open FI items
        fi_items, items_err = sources.read_open_items(
            vin, ddb_client=ddb_client,
            findings_table=findings_table, now_iso=now_iso_z,
        )
        if items_err:
            stats["open_items_read_errors"] += 1

        # assemble vehicle facts
        base = sources.build_vehicle_facts(
            vin,
            location_id=lid,
            vehicle_days_rows=vehicle_days,
            utilization_rows=utilization,
            busy_rows=busy_windows,
            open_ros_rows=open_ros,
            service_rows=service_soon,
            now_dt=now_utc,
            today_str=today_str,
        )

        # merge grounding + open-items into the record
        vehicle = {
            **base,
            "is_grounded": is_grounded,
            "grounding_read_error": grounding_err if grounding_err else False,
            "open_fi_items": fi_items,
            "open_items_read_error": items_err if items_err else False,
        }

        checked_by_location[lid].append(vehicle)

    # Filter and rank each source's vehicles together: D8 step 3 ranks by days idle, and
    # filtering one vehicle at a time kept them in read order (architect check 2026-10-10).
    for lid, records in checked_by_location.items():
        eligible_by_location[lid] = eligibility.filter_eligible(records)
        stats["vehicles_eligible"] += len(eligible_by_location[lid])

    # -----------------------------------------------------------------------
    # 5. Build candidates and run rules fallback
    # -----------------------------------------------------------------------
    build_result = cands_mod.build_candidates(
        gaps=gaps,
        locations=locations,
        forecast=forecast,
        outcomes=outcomes,
        assigned=vehicle_days,
        eligible_by_location={
            lid: vs for lid, vs in eligible_by_location.items()
        },
        request_hours=request_lengths,
        config=cfg,
        today=today_str,
    )
    candidates = build_result["candidates"]
    uncovered_deficits: list[str] = build_result["uncovered_deficits"]
    stats["candidates_built"] = build_result["counts"].get("candidates_built", 0)
    stats["candidates_forecast_days_missing"] = build_result["counts"].get(
        "forecast_days_missing", 0)

    # -----------------------------------------------------------------------
    # 5. Plan moves: per metro, model → validator → rules fallback (D8 steps 6-7)
    # -----------------------------------------------------------------------
    # W6: run_date is needed for inputs_hash (first_deficit_day expressed as an offset).
    # Compute it before any write so both move and gap findings use the same value.
    run_date = max((r["run_date"] for r in forecast), default=today_str)
    horizon = cands_mod.horizon_days(run_date)
    outcomes_by_dest: dict[str, list[dict]] = defaultdict(list)
    for o in outcomes:
        outcomes_by_dest[o["to_location_id"]].append(o)
    fc_by_key = {(r["location_id"], r["forecast_date"]): r for r in forecast}

    model_stats = ModelStats()
    plans, holds_by_gap_id, metro_hashes = _plan_moves(
        candidates=candidates,
        gaps=gaps,
        gap_by_loc=gap_by_loc,
        loc_by_id=loc_by_id,
        forecast=forecast,
        outcomes_by_dest=outcomes_by_dest,
        fc_by_key=fc_by_key,
        events=events,
        bedrock_client=bedrock_client,
        model_id=model_id,
        guardrail_id=guardrail_id,
        guardrail_version=guardrail_version,
        cfg=cfg,
        stats=stats,
        model_stats=model_stats,
        min_net_benefit_usd=float(cfg["min_net_benefit_usd"]),
        # D8 step 7: a source stays at or under the target utilisation after its moves.
        source_util_cap=float(cfg.get("source_util_cap", cfg["target_utilization"])),
        max_model_calls_per_run=int(
            cfg.get("max_model_calls_per_run", _DEFAULT_MAX_MODEL_CALLS)
        ),
        today_str=today_str,
        horizon=horizon,
        # D8 step 5: read-back path for the metro-level inputs hash.  The agent writes
        # ``metro_inputs_hash`` on each move and deficit gap Finding of a planned metro as
        # a DDB attribute (not a schema property); the next run reads the metro's live
        # rows back (``write.read_live_item``) to decide whether the metro's inputs have
        # changed before planning it again.
        ddb_client=ddb_client,
        findings_table=findings_table,
        fleet_id=fleet_id,
        now_iso_z=now_iso_z,
        agent_version=agent_version,
    )
    stats["moves_planned"] = len(plans)
    stats["model_calls"] = int(model_stats.model_calls)
    # The run record's token and cost fields (prompt 40: tokens_used was 0 in every run
    # record and payload while the model ran). Every Converse attempt counts, retries too.
    stats["tokens_used"] = int(model_stats.input_tokens + model_stats.output_tokens)
    stats["cost_usd"] = float(model_stats.cost_usd())

    def _write(finding: dict, written_counter: str,
               metro_inputs_hash: str | None = None) -> None:
        """G4-D9: equal hashes only refresh the expiry; changed content is written.

        When ``metro_inputs_hash`` is provided (every move and deficit gap item of a metro
        that went through ``_plan_moves``, whichever way it ended), the hash is injected as a
        top-level attribute on the dict passed to the DDB writer — a DDB attribute, NOT a
        schema property (same pattern as the T10.2 scope denormalisation).  The next run
        reads it back (``write.read_live_item``) and, when the metro's inputs are
        unchanged, reuses the metro's latest outcome instead of planning it again (D8 step 5).
        ``None`` is a no-op and the finding is written byte-identical to before.

        Fix Group 1 (review cycle 1 W1): the shared tier2 ``_update_item``
        does NOT persist ``metro_inputs_hash`` on UPDATE, so after a per-
        item input change the stored hash would be stale and the skip
        would stop firing until the row expires.  ``set_metro_inputs_hash``
        forces the attribute onto the stored row after the main write,
        regardless of PUT or UPDATE path, so the skip stays live across
        day-to-day re-emits (two extra DDB calls per move Finding).
        """
        live = write_mod.read_live_hash(finding["finding_key"], ddb_client=ddb_client,
                                        table_name=findings_table, now_str=now_iso_z)
        if metro_inputs_hash is not None:
            payload = {**finding, "metro_inputs_hash": metro_inputs_hash}
        else:
            payload = finding
        write_mod.write_finding_if_changed(
            live_item_hash=live, new_hash=finding["inputs_hash"], finding=payload,
            writer=write_fn, refresher=refresh_fn)
        stats["items_unchanged" if live == finding["inputs_hash"] else written_counter] += 1
        # Converge the stored ``metro_inputs_hash`` on both PUT and UPDATE
        # paths.  The main writer sets it on PUT (via ``_to_ddb_item`` of
        # ``payload``) but not on UPDATE; the direct UpdateItem here closes
        # that gap so a subsequent unchanged-input run reads the current
        # hash, not whichever one landed on the first PUT.
        if metro_inputs_hash is not None:
            write_mod.set_metro_inputs_hash(
                finding_key=finding["finding_key"],
                metro_inputs_hash=metro_inputs_hash,
                ddb_client=ddb_client,
                table_name=findings_table,
                now_str=now_iso_z,
            )

    # -----------------------------------------------------------------------
    # 6. Write move findings
    # -----------------------------------------------------------------------
    planned_dests: set[str] = set()
    for plan in plans:
        planned_dests.add(plan["to_location_id"])
        # find the candidate to get metadata
        cand = next(
            (c for c in candidates if c["candidate_id"] == plan["candidate_id"]), None
        )
        if cand is None:
            stats["items_refused"] += 1
            continue

        from_loc = loc_by_id.get(plan["from_location_id"])
        to_loc = loc_by_id.get(plan["to_location_id"])
        if from_loc is None or to_loc is None:
            stats["items_refused"] += 1
            continue

        evs = ev_mod.move_evidence(
            plan, cand,
            computed_at=computed_at,
            from_location=from_loc,
            to_location=to_loc,
            outcomes=outcomes,
            events=events,
            vehicle_days=vehicle_days,
            today=today_str,
        )
        try:
            finding = write_mod.build_move_finding(
                plan, cand,
                fleet_id=fleet_id,
                from_location=from_loc,
                to_location=to_loc,
                evidence=evs,
                computed_at=computed_at,
                agent_version=agent_version,
                run_date=run_date,
            )
        except ValueError as exc:
            log.warning("agent: move finding refused: %s", exc)
            stats["items_refused"] += 1
            continue

        _write(finding, "move_items_written",
               metro_inputs_hash=plan.get("metro_inputs_hash"))

    # -----------------------------------------------------------------------
    # 7. Gap items
    # -----------------------------------------------------------------------
    # Deficit gap items: uncovered_deficits (no plan built or no move planned)
    # Also include deficits that had candidates but all were dropped by fallback
    deficits_with_plan: set[str] = {plan["to_location_id"] for plan in plans}
    deficit_gap_locs = list(deficits - deficits_with_plan)

    # Surplus gap items: surplus locations in metros with no deficit.
    # S3: if a metro has an incomplete location, its shortfall is unknown; skip any
    # surplus gap items for that metro so we don't write "no location is short" when
    # that may be untrue.  metros_with_incomplete comes from read_locations.

    surplus_gap_locs = [
        lid for lid in surplus_locs
        if loc_by_id.get(lid, {}).get("metro") not in deficit_metros
        and loc_by_id.get(lid, {}).get("metro") not in metros_with_incomplete
    ]

    def _gap_days(lid: str) -> list[dict]:
        """The location's horizon forecast, with method and data dates, for its evidence."""
        days = []
        for d in horizon:
            row = fc_by_key.get((lid, d))
            if row is not None:
                days.append({"date": d, "forecast_hours": row["forecast_hours"],
                             "supply_vehicles": gap_by_loc[lid]["supply_vehicles"],
                             "op_hours": loc_by_id[lid]["op_hours"],
                             "method": row.get("method"), "mape_28d": row.get("mape_28d"),
                             "run_date": row.get("run_date"), "data_as_of": row.get("data_as_of")})
        return days

    def _outlook(lid: str) -> dict:
        """The same outlook a move candidate gets (candidates.location_outlook)."""
        return cands_mod.location_outlook(
            lid, forecast=forecast, outcomes=outcomes_by_dest[lid],
            supply=float(gap_by_loc[lid]["supply_vehicles"]),
            op_hours=float(loc_by_id[lid]["op_hours"]),
            deficit_above=float(cfg["deficit_above"]),
            max_utilization=float(cfg["max_utilization"]),
            horizon=horizon)

    # assigned VINs per location (from vehicle_days)
    assigned_vins_by_loc: dict[str, list[str]] = defaultdict(list)
    for r in vehicle_days:
        assigned_vins_by_loc[r["location_id"]].append(r["vin"])

    # Write deficit gap items.
    # D10: a deficit gap item is a model-chosen hold OR a rules-side uncovered
    # deficit.  Both produce the same shape; the only difference is the
    # ``diagnosis_source`` and the (optional) ``rebalance.hold`` evidence
    # entry which carries the model's reason/detail.
    for lid in deficit_gap_locs:
        gap = gap_by_loc.get(lid)
        loc = loc_by_id.get(lid)
        if gap is None or loc is None:
            continue
        try:
            outlook = _outlook(lid)
        except ValueError:
            stats["forecast_days_missing"] += 1
            continue
        gap_id = f"{lid}/{outlook['forecast_week']}"
        hold = holds_by_gap_id.get(gap_id)
        diagnosis_source = "model" if hold is not None else "rules"
        gap_item = {
            "location_id": lid,
            "gap_kind": "deficit",
            "gaps_data_as_of": gap.get("data_as_of"),
            "per_day": _gap_days(lid),
            "forecast_week": outlook["forecast_week"],
            "first_deficit_day": outlook["first_deficit_day"],
            "severity": outlook["severity"],
            "confidence": outlook["confidence"],
            "vehicles_short": int(gap["vehicles_short"]),
            "vehicles_spare": 0,
            "diagnosis_source": diagnosis_source,
        }
        avins = assigned_vins_by_loc.get(lid, [])
        try:
            evs = ev_mod.gap_evidence(
                gap_item, computed_at=computed_at, location=loc, assigned_vins=avins
            )
        except ValueError:
            log.warning("agent: no assigned vehicle at deficit location %s; gap item skipped", lid)
            stats["gap_items_without_vehicle"] += 1
            continue
        if hold is not None:
            # Attach a rebalance.hold entry with the model's reason/detail. VIN
            # for the entry mirrors the gap evidence helper's first-VIN choice
            # (sorted assigned list).
            first_vin = sorted(avins)[0]
            evs.append(ev_mod.hold_evidence_entry(
                hold, computed_at=computed_at, vin=first_vin,
            ))
        try:
            finding = write_mod.build_gap_finding(
                gap_item,
                fleet_id=fleet_id,
                location=loc,
                evidence=evs,
                computed_at=computed_at,
                agent_version=agent_version,
                run_date=run_date,
            )
        except ValueError as exc:
            log.warning("agent: gap finding refused: %s", exc)
            stats["items_refused"] += 1
            continue
        # D8 step 5: a deficit in a planned metro carries the metro's fingerprint, so a
        # metro whose outcome had no move (holds, or nothing worth moving) can still be
        # recognised as unchanged on the next run.
        _write(finding, "gap_items_written",
               metro_inputs_hash=metro_hashes.get(loc.get("metro")))

    # Write surplus gap items
    for lid in surplus_gap_locs:
        gap = gap_by_loc.get(lid)
        loc = loc_by_id.get(lid)
        if gap is None or loc is None:
            continue
        try:
            outlook = _outlook(lid)
        except ValueError:
            stats["forecast_days_missing"] += 1
            continue
        gap_item = {
            "location_id": lid,
            "gap_kind": "surplus",
            "gaps_data_as_of": gap.get("data_as_of"),
            "per_day": _gap_days(lid),
            "forecast_week": outlook["forecast_week"],
            "first_deficit_day": outlook["first_deficit_day"],
            "severity": "routine",  # D10: a surplus is never urgent
            "confidence": outlook["confidence"],
            "vehicles_short": 0,
            "vehicles_spare": int(gap["vehicles_spare"]),
        }
        avins = assigned_vins_by_loc.get(lid, [])
        try:
            evs = ev_mod.gap_evidence(
                gap_item, computed_at=computed_at, location=loc, assigned_vins=avins
            )
        except ValueError:
            log.warning("agent: no assigned vehicle at surplus location %s; gap item skipped", lid)
            stats["gap_items_without_vehicle"] += 1
            continue
        try:
            finding = write_mod.build_gap_finding(
                gap_item,
                fleet_id=fleet_id,
                location=loc,
                evidence=evs,
                computed_at=computed_at,
                agent_version=agent_version,
                run_date=run_date,
            )
        except ValueError as exc:
            log.warning("agent: surplus gap finding refused: %s", exc)
            stats["items_refused"] += 1
            continue
        _write(finding, "gap_items_written")

    return stats


# ---------------------------------------------------------------------------
# Per-metro model/rules orchestrator (spec D8 steps 6-7)
# ---------------------------------------------------------------------------

def _plan_moves(
    *,
    candidates: list[dict],
    gaps: list[dict],
    gap_by_loc: dict[str, dict],
    loc_by_id: dict[str, dict],
    forecast: list[dict],
    outcomes_by_dest: dict[str, list[dict]],
    fc_by_key: dict[tuple[str, str], dict],
    events: list[dict],
    bedrock_client: Any,
    model_id: str,
    guardrail_id: str,
    guardrail_version: str,
    cfg: dict,
    stats: dict,
    model_stats: ModelStats,
    min_net_benefit_usd: float,
    source_util_cap: float,
    max_model_calls_per_run: int,
    today_str: str,
    horizon: list[str],
    ddb_client: Any,
    findings_table: str,
    fleet_id: str,
    now_iso_z: str,
    agent_version: str,
) -> tuple[list[dict], dict[str, dict], dict[str, str]]:
    """One pass per metro: reuse an unchanged outcome, else model → validator (one retry) → rules.

    Returns ``(plans, holds_by_gap_id, metro_hashes)``:

    * ``plans`` — rules-shape plan dicts (the same shape ``rules_fallback``
      returns), ready for :func:`write.build_move_finding`.  Money is **always**
      recomputed via :func:`candidates.money_for` for the chosen VIN count;
      the model's ``detail`` string is kept and ``diagnosis_source`` is
      ``"model"`` or ``"rules"`` accordingly (D9: money and confidence are
      formulas, never model output — mutation M3).  **Every plan carries
      ``metro_inputs_hash``** (D8 step 5), whichever way its metro ended — the
      hash is written on the move Finding as a DDB attribute so the next run can
      read it back (``write.read_live_item``).
    * ``holds_by_gap_id`` — ``{<location_id>/<forecast_week>: {reason, detail,
      metro}}`` for every hold the model issued and the validator accepted, or
      that a reused outcome's live gap item carries.
      Keyed by the gap_id so the gap-item loop can look it up by
      ``f"{location_id}/{forecast_week}"``.
    * ``metro_hashes`` — ``{metro: metro_inputs_hash}`` for every metro with a
      candidate; the gap-item loop stores it on that metro's deficit gap items, so
      an outcome with no move (holds only, or nothing worth moving) is recognised
      on the next run too.

    D8 step 5 (prompt 40): a metro whose latest outcome (its live items with the
    newest ``computed_at``: candidates' move Findings and deficits' gap Findings)
    carries this run's hash throughout is not planned again:
    :func:`_reuse_live_outcome` returns that outcome's moves as this run's plans
    and its holds, so the moves are refreshed, counted unchanged and still cover
    their targets, and no model call or rules run is made.  The check runs
    **before** the model budget check, so a reused metro does not consume model
    budget.  A ``None`` live hash never matches (the ``should_skip_metro``
    contract), so a metro seen for the first time is always planned.

    Model path preconditions (ALL must hold, otherwise the metro uses rules):
    * ``bedrock_client`` is not ``None``.
    * ``model_id``, ``guardrail_id`` and ``guardrail_version`` are non-empty.
    * ``model_stats.model_calls < max_model_calls_per_run`` at the start of the
      metro (the budget caps total Converse requests across the run).
    * at least one candidate has a count and eligible VINs.
    """
    # Local import keeps the dependency explicit at the call site; the
    # module-level import graph is unchanged.
    from agents.fi_rebalancing import write as write_mod
    # Group candidates by metro using each candidate's from_location.
    cands_by_metro: dict[str, list[dict]] = defaultdict(list)
    for c in candidates:
        src = loc_by_id.get(c.get("from_location_id"))
        if src is None:
            # No source location found — drop the candidate silently; the
            # outer loop's deficit gap-item path still covers the destination.
            continue
        cands_by_metro[src["metro"]].append(c)

    # Deterministic metro order so a model run is reproducible and the
    # budget caps apply to the same metros each day.
    metros = sorted(cands_by_metro.keys())

    # Build gap_ids for every deficit location (one per location per week).
    # The validator uses this allowlist; the agent's gap-item loop uses the
    # same key format ``f"{location_id}/{forecast_week}"``.
    gap_ids_by_metro: dict[str, list[str]] = defaultdict(list)
    gap_lookup: dict[str, tuple[str, str]] = {}
    for g in gaps:
        if g.get("status") != "deficit":
            continue
        lid = g.get("location_id")
        loc = loc_by_id.get(lid)
        if loc is None:
            continue
        try:
            outlook = cands_mod.location_outlook(
                lid,
                forecast=forecast,
                outcomes=outcomes_by_dest.get(lid, []),
                supply=float(gap_by_loc[lid]["supply_vehicles"]),
                op_hours=float(loc_by_id[lid]["op_hours"]),
                deficit_above=float(cfg["deficit_above"]),
                max_utilization=float(cfg["max_utilization"]),
                horizon=horizon,
            )
        except (ValueError, KeyError):
            continue
        gap_id = f"{lid}/{outlook['forecast_week']}"
        gap_ids_by_metro[loc["metro"]].append(gap_id)
        gap_lookup[gap_id] = (lid, outlook["forecast_week"])

    plans: list[dict] = []
    holds_by_gap_id: dict[str, dict] = {}
    metro_hashes: dict[str, str] = {}
    # Shared across all metro iterations so a VIN assigned in metro A is
    # unavailable for metro B (T1.3 item 1 — cross-metro uniqueness).
    used_vins_global: set[str] = set()
    use_model_pathway = bool(
        bedrock_client is not None
        and model_id
        and guardrail_id
        and guardrail_version
    )

    for metro in metros:
        metro_cands = cands_by_metro[metro]
        if not metro_cands:
            continue

        def _metro_rules_fn(_exc: Exception | None = None) -> dict:
            return {
                "moves": rules_fallback(
                    candidates=metro_cands,
                    model_call=None,
                    min_net_benefit_usd=min_net_benefit_usd,
                ),
                "holds": [],
            }

        # The per-metro bundle: what the model reasons over, and what the metro's
        # fingerprint (``metro_inputs_hash``) is computed from.  Built for every metro with
        # a candidate, whichever way the metro then ends, so every outcome can be stored
        # and recognised on the next run (prompt 40, defect B).
        sources_by_id: dict[str, dict] = {}
        for c in metro_cands:
            sid = c["from_location_id"]
            if sid in sources_by_id:
                continue
            # The source's forecast hours over the horizon (used for the 85%
            # utilisation check).  Sum per-day forecast rows for the source.
            src_hours = sum(
                float(fc_by_key[(sid, d)]["forecast_hours"])
                for d in horizon
                if (sid, d) in fc_by_key
            )
            sources_by_id[sid] = {
                "supply_vehicles": int(gap_by_loc.get(sid, {}).get("supply_vehicles", 0)),
                "op_hours": float(loc_by_id[sid]["op_hours"]) * max(1, len(horizon)),
                "forecast_hours": src_hours,
            }

        bundle_candidates = [
            {
                "candidate_id": c["candidate_id"],
                "from_location_id": c["from_location_id"],
                "to_location_id": c["to_location_id"],
                "count": int(c["count"]),
                "eligible_vins": list(c.get("eligible_vins", [])),
            }
            for c in metro_cands
            if int(c.get("count", 0)) > 0 and c.get("eligible_vins")
        ]

        metro_bundle = {
            "candidates": bundle_candidates,
            "gap_ids": list(gap_ids_by_metro.get(metro, [])),
            "gap_lookup": {gid: gap_lookup[gid] for gid in gap_ids_by_metro.get(metro, [])},
            "sources_by_id": sources_by_id,
            "events": [e for e in events
                       if any(loc_by_id.get(c["from_location_id"], {}).get("metro") == metro
                              for c in metro_cands)],
            "evidence_kinds": list(_MOVE_EVIDENCE_KINDS),
            "item_context": {
                "metro": metro,
                "candidates": bundle_candidates,
                "gap_ids": list(gap_ids_by_metro.get(metro, [])),
                "events": events,
                "evidence_kinds": list(_MOVE_EVIDENCE_KINDS),
                "today": today_str,
            },
        }

        # D8 step 5.  The hash is over the deterministic model inputs (candidates,
        # gap_ids, sources, evidence_kinds), NOT the per-call date, so an identical
        # daily snapshot produces the same hash.  It is stored on every move and deficit
        # gap item this metro writes (``_write``), and a metro whose latest outcome
        # carries the same hash throughout is not planned again: that outcome is this
        # run's plan (``_reuse_live_outcome``): no model call, no rules run, and its moves
        # are refreshed and still cover their targets (prompt 40, defect A).
        new_metro_hash = write_mod.compute_metro_inputs_hash(metro_bundle)
        metro_hashes[metro] = new_metro_hash
        reused = _reuse_live_outcome(
            metro=metro,
            metro_cands=metro_cands,
            gap_ids=list(gap_ids_by_metro.get(metro, [])),
            new_metro_hash=new_metro_hash,
            fleet_id=fleet_id,
            ddb_client=ddb_client,
            findings_table=findings_table,
            now_iso_z=now_iso_z,
            min_net_benefit_usd=min_net_benefit_usd,
        )
        if reused is not None:
            reused_plans, reused_holds = reused
            plans.extend({**p, "_metro": metro} for p in reused_plans)
            holds_by_gap_id.update(reused_holds)
            stats["metros_skipped_unchanged"] += 1
            # G2.1 S1: feed reused-path VINs into used_vins_global so a later
            # model metro's validator avoids them.
            for p in reused_plans:
                used_vins_global.update(p.get("vehicle_vins", []))
            continue

        if (not use_model_pathway
                or model_stats.model_calls >= max_model_calls_per_run
                or not bundle_candidates):
            # Rules only for this metro (no model configured, the run's model budget is
            # spent, or no candidate can be served) — the rules_fallback output is
            # already a list of plan dicts.
            for p in _metro_rules_fn()["moves"]:
                plans.append({**p, "diagnosis_source": "rules",
                              "metro_inputs_hash": new_metro_hash,
                              "_metro": metro})
                # G2.1 S1: feed rules-path VINs into used_vins_global so a later
                # model metro's validator avoids them.
                used_vins_global.update(p.get("vehicle_vins", []))
            continue

        result = plan_rebalance(
            bedrock_client=bedrock_client,
            metro_bundle=metro_bundle,
            used_vins_global=used_vins_global,
            rules_fn=_metro_rules_fn,
            stats=model_stats,
            model_id=model_id,
            guardrail_id=guardrail_id,
            guardrail_version=guardrail_version,
            source_util_cap=source_util_cap,
        )
        source = result.get("diagnosis_source", "rules")

        if source == "rules":
            # The rules branch returns already-shaped plan dicts in
            # ``moves`` (see ``_metro_rules_fn`` above).  The fingerprint is stored
            # here too, so a metro whose model output keeps failing validation is not
            # sent to the model again on unchanged inputs (prompt 40, defect B).
            for p in result["moves"]:
                plans.append({**p, "diagnosis_source": "rules",
                              "metro_inputs_hash": new_metro_hash,
                              "_metro": metro})
                # G2.1 S1: feed model→rules fallback VINs into used_vins_global so
                # a later model metro's validator avoids them.
                used_vins_global.update(p.get("vehicle_vins", []))
            continue

        # Model path: convert each tool-input move into a rules-shape plan.
        # Money is recomputed via ``money_for`` for the chosen VIN count — the
        # model is NEVER consulted for a numeric field (D9, mutation M3).
        for mv in result["moves"]:
            cand = next(
                (c for c in metro_cands if c["candidate_id"] == mv["candidate_id"]),
                None,
            )
            if cand is None:
                stats["items_refused"] += 1
                continue
            chosen_vins: list[str] = list(mv["vehicle_vins"])
            try:
                money = money_for(cand, len(chosen_vins))
            except ValueError:
                stats["items_refused"] += 1
                continue
            if money["net_benefit_usd"] < min_net_benefit_usd:
                stats["items_refused"] += 1
                continue
            plans.append({
                "candidate_id": cand["candidate_id"],
                "from_location_id": cand["from_location_id"],
                "to_location_id": cand["to_location_id"],
                "vehicle_vins": chosen_vins,
                "timing": mv["timing"],
                "detail": mv["detail"],
                "diagnosis_source": "model",
                # D8 step 5: carry the metro's inputs_hash so the write path
                # can inject it as a DDB attribute on the move Finding.
                "metro_inputs_hash": new_metro_hash,
                # ``**money`` is spread last on purpose: money_for recomputes the
                # financial fields from the real candidate/VIN-count; spreading last
                # means any key overlap resolves to the recomputed value, not a
                # model-supplied one (T1.3 item 3).
                **money,
                # F1.3 W2: tag for post-loop dedup; stripped before returning.
                "_metro": metro,
            })
            # Mark these VINs as committed so later metro iterations cannot
            # select them again (T1.3 item 1 — cross-metro VIN uniqueness).
            used_vins_global.update(chosen_vins)

        for h in result["holds"]:
            holds_by_gap_id[h["gap_id"]] = {
                "reason": h["reason"],
                "detail": h["detail"],
                "metro": metro,
            }

    # F1.3 W2: Post-loop dedup — drop any later plan that names a VIN already
    # committed to an earlier plan of this run, on every path (model, rules-only,
    # model→rules fallback, reused).  ``_metro`` is stripped from returned plans.
    vin_to_metro: dict[str, str] = {}
    deduped: list[dict] = []
    for plan in plans:
        plan_metro = plan["_metro"]
        overlap = [v for v in plan["vehicle_vins"] if v in vin_to_metro]
        if overlap:
            log.warning(
                "agent: dropping move plan for metro %s: VIN(s) %s already claimed "
                "by metro %s",
                plan_metro, sorted(overlap), vin_to_metro[overlap[0]],
            )
            stats["plans_deduped"] += 1
            # G2.1 S2: remove the deduped metro from metro_hashes so the next run
            # re-plans it rather than treating it as unchanged.
            metro_hashes.pop(plan_metro, None)
            # Limit — REUSED metro: if the deduped metro took the REUSED path, its
            # deficit gap rows in DDB already carry the stored hash (written on a
            # prior run).  Popping metro_hashes means no non-None hash is written to
            # those gap rows by _write this run, but the stored value stays in place.
            # On the next run, _reuse_live_outcome will again hash the same inputs and
            # find a match, returning the same "no move" outcome, so the metro is
            # planned again only when its own inputs change (different gap data →
            # different new_metro_hash → _reuse returns None → full planning).
            # This requires a VIN eligible in two metros at once; with one location
            # per VIN that cannot happen today, so this path is currently unreachable.
        else:
            for vin in plan["vehicle_vins"]:
                vin_to_metro[vin] = plan_metro
            deduped.append({k: v for k, v in plan.items() if k != "_metro"})
    plans = deduped

    return plans, holds_by_gap_id, metro_hashes


def _move_finding_key(cand: dict, fleet_id: str) -> str | None:
    """The finding_key of the move Finding a candidate would produce (``build_move_finding``)."""
    from agents.tier2.writer import compute_finding_key

    if not cand.get("forecast_week"):
        return None
    return compute_finding_key(
        agent_id="agent-fi-rebalancing",
        scope_field="fleet_id",
        scope_value=fleet_id,
        finding_kind="fleet.rebalance.move",
        subject_key=(f"{cand['from_location_id']}/{cand['to_location_id']}/"
                     f"{cand['forecast_week']}"),
    )


def _gap_finding_key(gap_id: str, fleet_id: str) -> str:
    """The finding_key of the gap Finding for ``<location_id>/<forecast_week>``."""
    from agents.tier2.writer import compute_finding_key

    return compute_finding_key(
        agent_id="agent-fi-rebalancing",
        scope_field="fleet_id",
        scope_value=fleet_id,
        finding_kind="fleet.rebalance.gap",
        subject_key=gap_id,
    )


def _reuse_live_outcome(
    *,
    metro: str,
    metro_cands: list[dict],
    gap_ids: list[str],
    new_metro_hash: str,
    fleet_id: str,
    ddb_client: Any,
    findings_table: str,
    now_iso_z: str,
    min_net_benefit_usd: float,
) -> tuple[list[dict], dict[str, dict]] | None:
    """D8 step 5: this run's plan for a metro whose inputs are unchanged, from its live items.

    The metro's live items are the live move Finding each current candidate would produce
    and the live gap Finding of each current deficit.  Every write and refresh of a run sets
    that run's ``computed_at``, so the items with the newest ``computed_at`` are the metro's
    latest outcome; older live items are what earlier outcomes left behind (review g6r c1
    W1).  The latest outcome is reused only when every one of its items carries
    ``new_metro_hash``; then it is returned as this run's outcome:

    * ``plans`` — one plan per move of that outcome: its VINs (in the row's order),
      ``detail`` and ``diagnosis_source``, with money recomputed by :func:`money_for` as
      the model path does (D9), so the rebuilt Finding has the live row's ``inputs_hash``
      and is refreshed, counted unchanged, and covers its target (prompt 40, defect A).
    * ``holds`` — ``{gap_id: {reason, detail, metro}}`` from each of its gap items'
      ``rebalance.hold`` evidence, so a held deficit keeps its hold.

    Returns ``None`` (plan the metro as usual) when the metro has no live item, when any
    item of the latest outcome carries another hash or none, or when the outcome no longer
    fits this run: a move whose VIN is outside its candidate's eligible pool or over its
    count, a VIN used twice, money that can't be computed or is under the floor (money and
    the floor are not in the hash), a missing detail, diagnosis source or hold reason, or a
    move and a hold on one location.  Planning again costs a model call; reusing an outcome
    that no longer fits would recommend it.
    """
    from agents.fi_rebalancing import write as write_mod
    from agents.fi_rebalancing.selection import should_skip_metro

    def _live_row(fk: str) -> dict | None:
        return write_mod.read_live_item(
            fk, ddb_client=ddb_client, table_name=findings_table, now_str=now_iso_z)

    items: list[tuple[str, Any, dict]] = []
    for cand in metro_cands:
        fk = _move_finding_key(cand, fleet_id)
        row = _live_row(fk) if fk is not None else None
        if row is not None:
            items.append(("move", cand, row))
    for gid in gap_ids:
        row = _live_row(_gap_finding_key(gid, fleet_id))
        if row is not None:
            items.append(("gap", gid, row))
    if not items:
        return None
    latest = max(str(row.get("computed_at") or "") for _, _, row in items)
    outcome = [(kind, subject, row) for kind, subject, row in items
               if str(row.get("computed_at") or "") == latest]
    if not latest or not all(
            should_skip_metro(live_item_hash=row.get("metro_inputs_hash"),
                              candidate_hash=new_metro_hash)
            for _, _, row in outcome):
        return None

    plans: list[dict] = []
    holds: dict[str, dict] = {}
    used_vins: set[str] = set()
    for kind, subject, row in outcome:
        if kind == "move":
            plan = _plan_from_live_move(
                row, subject, new_metro_hash=new_metro_hash,
                min_net_benefit_usd=min_net_benefit_usd, used_vins=used_vins)
            if plan is None:
                log.warning("agent: metro %s: a live move no longer fits its candidate %s; "
                            "planning the metro again", metro, subject.get("candidate_id"))
                return None
            used_vins.update(plan["vehicle_vins"])
            plans.append(plan)
            continue
        for e in row.get("evidence") or []:
            if isinstance(e, dict) and e.get("kind") == "rebalance.hold":
                value = e.get("value") or {}
                reason, detail = value.get("reason"), value.get("detail")
                if not (isinstance(reason, str) and reason and isinstance(detail, str)
                        and detail):
                    return None
                holds[subject] = {"reason": reason, "detail": detail, "metro": metro}
                break
    held = {gid.rsplit("/", 1)[0] for gid in holds}
    if held & {p["to_location_id"] for p in plans}:
        return None
    return plans, holds


def _plan_from_live_move(
    row: dict | None,
    cand: dict,
    *,
    new_metro_hash: str,
    min_net_benefit_usd: float,
    used_vins: set[str],
) -> dict | None:
    """Rebuild the plan behind a live move Finding, or ``None`` if it no longer fits *cand*."""
    if row is None or row.get("metro_inputs_hash") != new_metro_hash:
        return None
    params = (row.get("recommended_action") or {}).get("parameters") or {}
    if (params.get("from_location_id") != cand["from_location_id"]
            or params.get("to_location_id") != cand["to_location_id"]):
        return None
    vins = params.get("vehicle_vins")
    if not isinstance(vins, list) or not vins or not all(isinstance(v, str) for v in vins):
        return None
    eligible = set(cand.get("eligible_vins") or [])
    if (len(set(vins)) != len(vins) or not set(vins) <= eligible
            or len(vins) > int(cand.get("count", 0)) or used_vins & set(vins)):
        return None
    try:
        money = money_for(cand, len(vins))
    except ValueError:
        return None
    if money["net_benefit_usd"] < min_net_benefit_usd:
        return None
    detail = row.get("detail")
    diagnosis_source = next(
        ((e.get("value") or {}).get("diagnosis_source")
         for e in row.get("evidence") or []
         if isinstance(e, dict) and e.get("kind") == "rebalance.diagnosis_source"),
        None)
    if not isinstance(detail, str) or not detail or diagnosis_source not in ("model", "rules"):
        return None
    return {
        "candidate_id": cand["candidate_id"],
        "from_location_id": cand["from_location_id"],
        "to_location_id": cand["to_location_id"],
        "vehicle_vins": list(vins),
        "detail": detail,
        "diagnosis_source": diagnosis_source,
        "metro_inputs_hash": new_metro_hash,
        # ``**money`` is spread last on purpose: money_for recomputes the
        # financial fields; spreading last ensures those recomputed values win
        # over any earlier key (T1.3 item 3).
        **money,
    }
