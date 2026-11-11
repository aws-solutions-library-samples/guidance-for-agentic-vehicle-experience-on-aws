"""Recall item decisions and run loop for the FI recall and warranty Tier 2 agent.

Public API
----------
decide_items(row, *, grounded, history, today) -> list[dict]
    Pure function: classify one (vin, campaign) recall row and return the list of
    item specs (item_type, attempt) to write.  No I/O.

run_recall_loop(*, recall_rows, stock_rows, today, computed_at, vehicle_ids_fn,
                locations_fn, grounded_fn, history_fn, standing_fn, lookup_live,
                write_finding, plan_fn=None, open_items_fn=None,
                model_call_cap=None, remaining_seconds=None) -> dict
    Decide/select/write run loop for the recall component (spec D8, task 4.1 S2b).
    Rules path (plan_fn=None) is unchanged from prior groups.
    No Bedrock, no boto3.

Each item spec is a dict with keys ``item_type`` (str) and ``attempt`` (int).
Possible item_type values: "ground", "book", "parts_expected", "book_exhausted", "ground_exhausted".
"""
from __future__ import annotations

import datetime
import logging
from typing import Any, Callable, Optional

from agents.fi_recall_warranty import band as _band
from agents.fi_recall_warranty.attempts import plan_attempt
from agents.fi_recall_warranty.selection import RegistryReadError

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# decide_items — pure classification, no I/O
# ---------------------------------------------------------------------------

def decide_items(
    row: dict[str, Any],
    *,
    grounded: bool,
    history: dict[tuple, dict],
    today: datetime.date,
) -> list[dict[str, Any]]:
    """Return item specs for one (vin, campaign) recall row.

    Parameters
    ----------
    row:      Dict with keys: campaign_id, recall_class, do_not_drive (bool),
              match_basis, remedy_available_from (ISO date str or None).
    grounded: True when the vehicle is already grounded in CMS.
    history:  Dict keyed (campaign_id, action) from attempts.read_attempt_history.
    today:    Date of the current run (UTC).

    Returns
    -------
    List of item spec dicts; each has ``item_type`` and ``attempt``.
    Empty when no items are due (all skip).
    """
    campaign_id: str = row["campaign_id"]
    recall_class: str = row["recall_class"]

    band_result = _band.classify(
        recall_class,
        row["do_not_drive"],
        row["match_basis"],
    )

    # Use midnight UTC so plan_attempt's rejected-window math is deterministic.
    now = datetime.datetime.combine(today, datetime.time.min, tzinfo=datetime.timezone.utc)

    items: list[dict[str, Any]] = []

    # --- Ground slot ---
    if "ground" in band_result.items and not grounded:
        entry = history.get((campaign_id, "ground"))
        decision = plan_attempt(entry, recall_class, now)
        _append_from_decision(items, decision, entry, "ground")

    # --- Book / parts_expected slot ---
    if "book" in band_result.items:
        entry = history.get((campaign_id, "book"))
        decision = plan_attempt(entry, recall_class, now)

        # While remedy_available_from is strictly after today, emit parts_expected.
        effective_type = _book_or_parts_expected(row.get("remedy_available_from"), today)
        _append_from_decision(items, decision, entry, effective_type)

    return items


# ---------------------------------------------------------------------------
# Helpers for decide_items
# ---------------------------------------------------------------------------

def _book_or_parts_expected(
    remedy_available_from: Optional[str],
    today: datetime.date,
) -> str:
    """Return 'parts_expected' if parts not yet available, else 'book'.

    Uses strict greater-than (>): on the availability date itself the book item
    is written.
    """
    if remedy_available_from and remedy_available_from > str(today):
        return "parts_expected"
    return "book"


def _attempt_from_decision(decision: str, entry: Optional[dict]) -> int:
    """Compute attempt number from plan_attempt decision and history entry."""
    if decision == "write_first":
        return 1
    assert entry is not None
    if decision == "write_current":
        return entry["attempt_number"]
    return entry["attempt_number"] + 1


def _append_from_decision(
    items: list,
    decision: str,
    entry: Optional[dict],
    effective_type: str,
) -> None:
    """Append an item spec to *items* when the decision warrants writing."""
    if decision == "skip":
        return
    attempt = _attempt_from_decision(decision, entry)
    if decision == "write_exhausted":
        slot = "ground" if effective_type == "ground" else "book"
        items.append({"item_type": f"{slot}_exhausted", "attempt": 1})
    else:
        items.append({"item_type": effective_type, "attempt": attempt})


# ---------------------------------------------------------------------------
# run_recall_loop helpers
# ---------------------------------------------------------------------------

#: Sentinel origin: the fleet-locations read failed (counted, then treated as no origin).
_ORIGIN_READ_ERROR = ("read-error",)


def _resolve_origin(
    vin: str,
    vehicle_ids_fn: Callable,
    locations_fn: Callable,
) -> tuple[Optional[tuple[float, float, str]], Optional[str]]:
    """Resolve vehicle origin (lat, lon, locationId) from CMS.

    Returns (origin_triple, vehicle_id).  origin_triple is None when no
    location is assigned or VIN not in the registry.
    """
    cms = vehicle_ids_fn(vin)
    if cms is None:
        return None, None
    vehicle_id, fleet_id, location_id = cms
    if location_id is None:
        return None, vehicle_id
    try:
        locs = locations_fn(fleet_id)
    except RegistryReadError:
        # Cycle 2 agent W1: keep the vehicle id so the grounding check still runs; no
        # origin (unranked); the caller counts it via _ORIGIN_READ_ERROR.
        return _ORIGIN_READ_ERROR, vehicle_id
    coords = locs.get(location_id)
    if coords is None:
        return None, vehicle_id
    return (coords[0], coords[1], location_id), vehicle_id


def _skip_unchanged(live: Optional[dict], inputs_hash: str) -> bool:
    """Skip rule (spec D8 step 4): the ONE shared predicate, runner.skip_unchanged."""
    from agents.fi_recall_warranty.runner import skip_unchanged
    return skip_unchanged(live, inputs_hash, source_kind="recall.diagnosis_source")


def _to_schema_evidence(
    raw_entries: list[dict],
    computed_at: str,
) -> list[dict]:
    """Convert domain evidence entries to finding.schema.json evidence_entry format.

    Each raw entry from build_recall_evidence has {vin, provenance, <domain_key>: value}.
    The schema requires {kind, source, source_ref, value, computed_at, provenance, [vin]}.
    """
    _meta_keys = frozenset({"vin", "provenance"})
    result: list[dict] = []
    for entry in raw_entries:
        domain_keys = [k for k in entry if k not in _meta_keys]
        if not domain_keys:
            continue
        kind = domain_keys[0]
        value = entry[kind]
        schema_entry: dict[str, Any] = {
            "kind": kind,
            "source": (
                "agent-fi-recall-warranty"
                if "diagnosis_source" in kind
                else "adp"
            ),
            "source_ref": "fi-recall-warranty-run",
            "value": value,
            "computed_at": computed_at,
            "provenance": entry.get("provenance", "live"),
        }
        if "vin" in entry:
            schema_entry["vin"] = entry["vin"]
        result.append(schema_entry)
    return result


#: Fixed rules-path detail for the informational item types (no recommended_action).
_INFORMATIONAL_DETAIL: dict[str, str] = {
    "parts_expected": "Remedy parts are expected {remedy_date}. No booking is possible "
                      "until then; this item updates when the parts are available.",
    "book_exhausted": "Five booking attempts for this recall did not complete. "
                      "Follow up with the dealer manually.",
    "ground_exhausted": "Five grounding attempts for this recall did not complete. "
                        "Follow up manually; the vehicle should not be driven.",
}


def _rules_plan_or_empty(
    rooftops: list[dict],
    band_result: Any,
    item_type: str,
) -> dict[str, Any]:
    """Return build_rules_plan result or a no-candidate fallback (empty rooftops)."""
    from agents.fi_recall_warranty import runner as _runner
    if rooftops:
        return _runner.build_rules_plan(rooftops, band_result)
    return {
        "dealer_id": None,
        "combine_with": [],
        "urgency": band_result.urgency,
        "detail": "Book the recall service when a dealer is available.",
        "confidence": "low",
        "diagnosis_source": "rules",
    }


#: Seconds before end-of-window below which model calls are deferred to rules.
MODEL_CALL_RESERVE_S: float = 150.0


# ---------------------------------------------------------------------------
# decide/select/write helpers (task 4.1 S2b)
# ---------------------------------------------------------------------------

def _get_plan_from_live_evidence(live: dict) -> dict:
    """Return the recall.plan value from a live Finding's schema evidence, or {}."""
    for entry in (live.get("evidence") or []):
        if entry.get("kind") == "recall.plan":
            v = entry.get("value", {})
            return v if isinstance(v, dict) else {}
    return {}


def _make_rules_plan(item: dict) -> dict:
    """Build the rules-path plan dict for a classified item."""
    item_type: str = item["item_spec"]["item_type"]
    band_result = item["band_result"]
    rooftops: list = item["rooftops"]
    row: dict = item["row"]
    if item_type == "ground":
        return {
            "dealer_id": None, "combine_with": [], "urgency": band_result.urgency,
            "detail": "Ground the vehicle until the recall remedy is complete.",
            "confidence": "low", "diagnosis_source": "rules",
        }
    if item_type in _INFORMATIONAL_DETAIL:
        return {
            "dealer_id": None, "combine_with": [], "urgency": band_result.urgency,
            "detail": _INFORMATIONAL_DETAIL[item_type].format(
                remedy_date=row.get("remedy_available_from")),
            "confidence": "low", "diagnosis_source": "rules",
        }
    return _rules_plan_or_empty(rooftops, band_result, item_type)


def _build_plan_from_live(item: dict) -> dict:
    """Build refresh_from_live plan from the live Finding's model content."""
    live: dict = item["live"]
    plan_val = _get_plan_from_live_evidence(live)
    params = ((live.get("recommended_action") or {}).get("parameters") or {})
    return {
        "dealer_id": params.get("dealer_id"),
        "urgency": plan_val.get("urgency"),
        "combine_with": plan_val.get("combine_with", []),
        "detail": live.get("detail", ""),
        "confidence": live.get("confidence", "low"),
        "diagnosis_source": "model",
    }


def _write_decided_item(
    item: dict,
    plan: dict,
    *,
    write_finding: Callable,
    computed_at: str,
    stats: dict[str, Any],
    refresh_on_noop: bool = True,
    update_on_same_hash: bool = False,
    count_as_written: bool = True,
) -> None:
    """Build evidence, build Finding, and write one classified item.

    Ground items get fixed text; severity/items/headline/disclosure_txt are never
    read from the plan (all come from band_result / catalog).
    Passes standing to write_finding (mutation guard a).
    """
    from agents.fi_recall_warranty import evidence as _evidence
    from agents.fi_recall_warranty import writer as _writer

    row = item["row"]
    item_type: str = item["item_spec"]["item_type"]
    attempt: int = item["item_spec"]["attempt"]
    band_result = item["band_result"]
    rooftops = item["rooftops"]
    origin = item["origin"]
    open_item_ids = item["open_item_ids"]

    diagnosis_source = plan.get("diagnosis_source", "rules")
    dealer_id = plan.get("dealer_id")
    confidence = plan.get("confidence", "low")
    plan_for_ev = {
        "urgency": plan.get("urgency"),
        "combine_with": plan.get("combine_with", []),
    }

    raw_ev = _evidence.build_recall_evidence(
        row, item_type, attempt, rooftops, origin,
        open_item_ids, diagnosis_source, plan=plan_for_ev,
    )
    schema_ev = _to_schema_evidence(raw_ev, computed_at)

    headline = _writer.build_headline(
        item_type, row["campaign_id"], dealer_id, row.get("remedy_available_from"),
    )
    catalog: dict[str, Any] = {
        "campaign_id": row["campaign_id"],
        "summary": row.get("summary", ""),
        "consequence": row.get("consequence", ""),
        "remedy": row.get("remedy", ""),
    }
    finding = _writer.build_recall_finding(
        vin=item["vin"], campaign_id=row["campaign_id"],
        item_type=item_type, attempt=attempt,
        band_result=band_result, headline=headline,
        detail=plan.get("detail", ""), catalog_entry=catalog,
        dealer_id=dealer_id, computed_at=computed_at,
        evidence=schema_ev, confidence=confidence,
        inputs_hash=item["inputs_hash"],
    )
    if update_on_same_hash:
        write_finding(finding, item["standing"], update_on_same_hash=True)
    else:
        write_finding(finding, item["standing"], refresh_on_noop=refresh_on_noop)
    if not count_as_written:
        return
    stats["items_written"] += 1
    stats["by_item_type"][item_type] = stats["by_item_type"].get(item_type, 0) + 1


def _write_model_item(
    item: dict,
    *,
    plan_fn: Callable,
    remaining_seconds: Optional[Callable[[], float]],
    model_call_reserve_s: float,
    write_finding: Callable,
    computed_at: str,
    stats: dict[str, Any],
) -> None:
    """Write one selected eligible item: call plan_fn or defer if clock low."""
    # Clock guard: defer to rules if insufficient time remains.
    if remaining_seconds is not None and remaining_seconds() < model_call_reserve_s:
        try:
            _write_decided_item(
                item, _make_rules_plan(item),
                write_finding=write_finding, computed_at=computed_at,
                stats=stats, refresh_on_noop=True,
            )
        except (ValueError, KeyError) as exc:
            log.warning("recall_loop: clock-deferred write error %s", type(exc).__name__)
            stats["rows_refused"] += 1
        stats["deferred"] += 1
        return

    plan = plan_fn(
        row=item["row"], item_type=item["item_spec"]["item_type"],
        band_result=item["band_result"], rooftops=item["rooftops"],
        origin=item["origin"], bundle=item["bundle"],
    )
    stats["model_items"] += 1

    # Dealer guard: replace plan when dealer_id is not among item's rooftops.
    rooftop_ids = {r["dealer_id"] for r in item["rooftops"]}
    if plan.get("dealer_id") not in rooftop_ids:
        plan = _make_rules_plan(item)
        stats["plans_refused"] += 1

    try:
        # Call 59: only a MODEL plan replaces same-hash rules content; a model call that
        # fell back to rules leaves the row as it is (refresh only, no audit entry).
        if item["same_hash_rules"] and plan.get("diagnosis_source") == "model":
            _write_decided_item(
                item, plan, write_finding=write_finding, computed_at=computed_at,
                stats=stats, update_on_same_hash=True,
            )
        else:
            _write_decided_item(
                item, plan, write_finding=write_finding, computed_at=computed_at,
                stats=stats, refresh_on_noop=True,
            )
    except (ValueError, KeyError) as exc:
        log.warning(
            "recall_loop: model write error campaign=%s item=%s error=%s",
            item["row"].get("campaign_id"), item["item_spec"]["item_type"],
            type(exc).__name__,
        )
        stats["rows_refused"] += 1


def _decide_vin_rows(
    *,
    vin: str,
    vin_rows: list[dict],
    stock_rows: list[dict],
    today: datetime.date,
    computed_at: str,
    vehicle_ids_fn: Callable,
    locations_fn: Callable,
    grounded_fn: Callable,
    history_fn: Callable,
    standing_fn: Callable,
    lookup_live: Callable,
    open_items_fn: Optional[Callable],
    plan_fn: Optional[Callable],
    stats: dict[str, Any],
) -> list[dict]:
    """Decide phase for one VIN: classify all items without writing.

    Returns a list of item plan dicts, each carrying path ('fixed', 'skip',
    'refresh_from_live', or 'eligible') plus all data needed for the write phase.
    """
    from agents.fi_recall_warranty import rooftops as _rooftops
    from agents.fi_recall_warranty import evidence as _evidence
    from agents.fi_recall_warranty.writer import AGENT_ID, FINDING_KIND, _SUBJECT_ACTION
    from agents.fi_recall_warranty.attempts import bundle_for_row
    from agents.tier2.writer import compute_finding_key

    result: list[dict] = []

    try:
        origin, vehicle_id = _resolve_origin(vin, vehicle_ids_fn, locations_fn)
        if origin is _ORIGIN_READ_ERROR:
            stats["grounding_read_errors"] += 1
            origin = None
    except RegistryReadError:
        log.warning(
            "recall_loop: registry read error for vin=<redacted> — treating as no ids",
        )
        stats["grounding_read_errors"] += 1
        origin, vehicle_id = None, None

    grounded = False
    if vehicle_id is not None:
        grounded, err = grounded_fn(vehicle_id)
        stats["grounding_read_errors"] += err

    history, skip_count = history_fn(vin)
    stats["rows_refused"] += skip_count
    standing = standing_fn(vin)

    # D53: one open_items_fn call per VIN; empty when not provided.
    open_items_all: list[dict] = (
        open_items_fn(vin) if open_items_fn is not None else []
    )

    for row in vin_rows:
        stats["rows"] += 1
        try:
            items = decide_items(row, grounded=grounded, history=history, today=today)
        except (ValueError, KeyError) as exc:
            log.warning(
                "recall_loop: skipping bad row campaign=%s error=%s",
                row.get("campaign_id"), type(exc).__name__,
            )
            stats["rows_refused"] += 1
            continue

        remedy_parts: list[str] = row.get("remedy_part_numbers") or []
        origin_coords = (origin[0], origin[1]) if origin is not None else None
        rooftops = _rooftops.rank_rooftops(remedy_parts, stock_rows, origin_coords)
        bundle = bundle_for_row(open_items_all, row["campaign_id"])
        open_item_ids = [it["finding_id"] for it in bundle]

        for item_spec in items:
            item_type: str = item_spec["item_type"]
            if not rooftops and item_type == "book":
                stats["book_items_without_rooftops"] += 1
                continue
            try:
                band_result = _band.classify(
                    row["recall_class"], row["do_not_drive"], row["match_basis"],
                )
                attempt: int = item_spec["attempt"]
                action = _SUBJECT_ACTION.get(item_type, item_type)
                subject_key = f"{row['campaign_id']}/{action}/{attempt}"

                raw_ev = _evidence.build_recall_evidence(
                    row, item_type, attempt, rooftops, origin,
                    open_item_ids, "rules", plan=None,
                )
                inputs_hash = _evidence.compute_inputs_hash(raw_ev)
                finding_key = compute_finding_key(
                    agent_id=AGENT_ID, scope_field="vin", scope_value=vin,
                    finding_kind=FINDING_KIND, subject_key=subject_key,
                )
                live = lookup_live(finding_key)

                # Classify path
                if item_type == "ground" or item_type in _INFORMATIONAL_DETAIL:
                    path = "fixed"
                elif _skip_unchanged(live, inputs_hash):
                    # same hash + model-written: no model call; the live model content is
                    # written again so it never expires unrefreshed (call 52), with or
                    # without a plan_fn.
                    path = "refresh_from_live"
                else:
                    path = "eligible"

                # True when live exists with same hash but NOT model-written (eligible only)
                same_hash_rules = (
                    path == "eligible"
                    and live is not None
                    and live.get("inputs_hash") == inputs_hash
                )

                result.append({
                    "vin": vin,
                    "row": row,
                    "item_spec": item_spec,
                    "band_result": band_result,
                    "rooftops": rooftops,
                    "origin": origin,
                    "bundle": bundle,
                    "open_item_ids": open_item_ids,
                    "live": live,
                    "inputs_hash": inputs_hash,
                    "finding_key": finding_key,
                    "subject_key": subject_key,
                    "path": path,
                    "same_hash_rules": same_hash_rules,
                    "standing": standing,
                    "finding_kind": FINDING_KIND,
                    "severity": band_result.severity,
                })
            except (ValueError, KeyError) as exc:
                log.warning(
                    "recall_loop: skipping item campaign=%s item=%s error=%s",
                    row.get("campaign_id"), item_type, type(exc).__name__,
                )
                stats["rows_refused"] += 1

    return result


# ---------------------------------------------------------------------------
# run_recall_loop — public entry point
# ---------------------------------------------------------------------------

def run_recall_loop(
    *,
    recall_rows: list[dict[str, Any]],
    stock_rows: list[dict[str, Any]],
    today: datetime.date,
    computed_at: str,
    vehicle_ids_fn: Callable[[str], Optional[tuple]],
    locations_fn: Callable[[str], dict[str, tuple]],
    grounded_fn: Callable[[str], tuple[bool, int]],
    history_fn: Callable[[str], tuple[dict, int]],
    standing_fn: Callable[[str], Any],
    lookup_live: Callable[[str], Optional[dict]],
    write_finding: Callable[[dict, Any], Any],
    plan_fn: Optional[Callable] = None,
    open_items_fn: Optional[Callable[[str], list[dict]]] = None,
    model_call_cap: Optional[int] = None,
    remaining_seconds: Optional[Callable[[], float]] = None,
    model_call_reserve_s: float = MODEL_CALL_RESERVE_S,
) -> dict[str, Any]:
    """Decide/select/write run loop for the recall component (spec D8, task 4.1 S2b).

    Parameters
    ----------
    recall_rows:          Rows from select_unremedied_recalls (vin, campaign_id, …).
    stock_rows:           Rows from select_parts_stock for rooftop ranking.
    today:                UTC date of this run.
    computed_at:          ISO 8601 UTC timestamp string.
    vehicle_ids_fn:       ``vin -> (vehicleId, fleetId, locationId) | None``.
    locations_fn:         ``fleetId -> {locationId: (lat, lon)}``.
    grounded_fn:          ``vehicleId -> (is_grounded, error_count)``.
    history_fn:           ``vin -> (history_dict, skip_count)``.
    standing_fn:          ``vin -> StandingFacts`` (passed verbatim to write_finding).
    lookup_live:          ``finding_key -> live_row | None`` (skip/refresh rule).
    write_finding:        ``(finding, standing, **kw) -> result``.
    plan_fn:              Optional model plan fn; None → rules path (unchanged).
                          Never called for ground/informational items.
    open_items_fn:        ``vin -> list[dict]`` open items for bundle (D53).
    model_call_cap:       Max eligible items sent to plan_fn; None → RECALL_CAP.
    remaining_seconds:    ``() -> float``; model calls deferred below reserve.
    model_call_reserve_s: Reserve threshold in seconds (default 150.0).

    Returns
    -------
    dict with keys: rows, items_written, items_skipped_unchanged,
    rows_refused, grounding_read_errors, by_item_type,
    book_items_without_rooftops, model_items, deferred,
    plans_refused, items_refreshed_from_live.

    Raises
    ------
    Exceptions from write_finding or Athena readers propagate unchanged.
    ValueError / KeyError from a bad recall row is caught and counted in
    rows_refused; the run continues with the remaining rows.
    """
    stats: dict[str, Any] = {
        "rows": 0,
        "items_written": 0,
        "items_skipped_unchanged": 0,
        "rows_refused": 0,
        "grounding_read_errors": 0,
        "by_item_type": {},
        "book_items_without_rooftops": 0,
        "model_items": 0,
        "deferred": 0,
        "plans_refused": 0,
        "items_refreshed_from_live": 0,
    }

    by_vin: dict[str, list[dict]] = {}
    for row in recall_rows:
        vin = row.get("vin")
        if not vin:
            stats["rows_refused"] += 1
            continue
        by_vin.setdefault(vin, []).append(row)

    # Phase 1: Decide — classify all items without writing
    all_items: list[dict] = []
    for vin, vin_rows in by_vin.items():
        all_items.extend(_decide_vin_rows(
            vin=vin, vin_rows=vin_rows, stock_rows=stock_rows,
            today=today, computed_at=computed_at,
            vehicle_ids_fn=vehicle_ids_fn, locations_fn=locations_fn,
            grounded_fn=grounded_fn, history_fn=history_fn,
            standing_fn=standing_fn, lookup_live=lookup_live,
            open_items_fn=open_items_fn, plan_fn=plan_fn,
            stats=stats,
        ))

    # Phase 2: Select — cap eligible items for model calls
    selected: list[dict] = []
    if plan_fn is not None:
        from agents.fi_recall_warranty.runner import RECALL_CAP, apply_cap
        cap = model_call_cap if model_call_cap is not None else RECALL_CAP
        eligible = [x for x in all_items if x["path"] == "eligible"]
        # Pre-sort by (vin, subject_key) for stable tie-breaking within severity
        eligible_sorted = sorted(eligible, key=lambda x: (x["vin"], x["subject_key"]))
        selected = apply_cap(eligible_sorted, recall_cap=cap, warranty_cap=0)
    selected_keys = {x["finding_key"] for x in selected}

    # Phase 3a: Write fixed / refresh_from_live / unselected eligible (rules path)
    for item in all_items:
        path = item["path"]
        if path == "eligible" and item["finding_key"] in selected_keys:
            continue  # handled in phase 3b

        if path == "refresh_from_live":
            plan = _build_plan_from_live(item)
            try:
                _write_decided_item(
                    item, plan, write_finding=write_finding,
                    computed_at=computed_at, stats=stats, refresh_on_noop=True,
                    count_as_written=False,
                )
            except (ValueError, KeyError) as exc:
                log.warning("recall_loop: refresh write error %s", type(exc).__name__)
                stats["rows_refused"] += 1
            else:
                # The model call was skipped (D8 step 4); the write only keeps it alive.
                stats["items_skipped_unchanged"] += 1
                stats["items_refreshed_from_live"] += 1
            continue

        plan = _make_rules_plan(item)
        try:
            _write_decided_item(
                item, plan, write_finding=write_finding,
                computed_at=computed_at, stats=stats, refresh_on_noop=True,
            )
        except (ValueError, KeyError) as exc:
            log.warning(
                "recall_loop: skipping item campaign=%s item=%s error=%s",
                item["row"].get("campaign_id"), item["item_spec"]["item_type"],
                type(exc).__name__,
            )
            stats["rows_refused"] += 1

    # Phase 3b: Write selected items in severity order (model path)
    for item in selected:
        _write_model_item(
            item, plan_fn=plan_fn,
            remaining_seconds=remaining_seconds,
            model_call_reserve_s=model_call_reserve_s,
            write_finding=write_finding,
            computed_at=computed_at, stats=stats,
        )

    return stats
