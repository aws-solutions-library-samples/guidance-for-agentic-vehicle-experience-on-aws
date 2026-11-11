"""fallback.py — rules-only move plan for fi-rebalancing (spec G4-D5).

``rules_fallback`` never calls its ``model_call`` argument.
"""
from __future__ import annotations

from agents.fi_rebalancing import policy as rebal_policy
from agents.fi_rebalancing.candidates import money_for

_DETAIL = "Rules-based move: transfer vehicles to address fleet deficit."


def rules_fallback(*, candidates: list[dict], model_call: object,
                   min_net_benefit_usd: float | None = None) -> list[dict]:
    """Produce move plans from candidates without invoking the model.

    Candidates are processed in descending net-benefit order (then candidate_id).
    For each candidate:
    - actual_count = min(candidate count, remaining spare at source, available eligible VINs)
    - actual_count == original count → use existing money from candidate
    - actual_count < original count → recompute via money_for; drop on ValueError or
      if recomputed net is below the minimum
    VINs are consumed globally; a VIN used in one move is never reused.
    Source spare is tracked across candidates sharing the same from_location_id.
    """
    min_net = (min_net_benefit_usd if min_net_benefit_usd is not None
               else rebal_policy.current()["min_net_benefit_usd"])

    sorted_cands = sorted(
        candidates,
        key=lambda c: (-float(c.get("net_benefit_usd", 0)), c.get("candidate_id", "")),
    )

    # Initialise remaining spare per source (first-seen value wins)
    remaining_spare: dict[str, int] = {}
    for c in sorted_cands:
        fid = c["from_location_id"]
        if fid not in remaining_spare:
            remaining_spare[fid] = int(c.get("vehicles_spare", 0))

    # Per destination: up to 3 sources may each offer the whole shortfall (D8 step 2 lists
    # them as alternatives), so a rules plan caps the destination's total by its shortfall
    # and by its free parking, or it would send up to three times the vehicles it needs.
    # Missing fields make the cap 0: the candidate is dropped, never sized from a default.
    remaining_short: dict[str, int] = {}
    remaining_parking: dict[str, int] = {}
    for c in sorted_cands:
        tid = c["to_location_id"]
        remaining_short.setdefault(tid, int(c.get("vehicles_short", 0)))
        remaining_parking.setdefault(tid, int(c.get("parking_room", 0)))

    used_vins: set[str] = set()
    moves: list[dict] = []

    for cand in sorted_cands:
        fid = cand["from_location_id"]
        tid = cand["to_location_id"]
        spare = remaining_spare.get(fid, 0)
        if spare <= 0:
            continue

        eligible_vins: list[str] = cand.get("eligible_vins", [])
        available = [v for v in eligible_vins if v not in used_vins]
        if "count" not in cand:
            continue
        original_count = int(cand["count"])
        actual_count = min(original_count, spare, len(available),
                           remaining_short[tid], remaining_parking[tid])

        if actual_count <= 0:
            continue

        taken = available[:actual_count]

        if actual_count == original_count:
            # Count unchanged — existing money is valid, no recompute needed.
            # All money keys must be present; a candidate built without money is dropped
            # rather than silently priced at zero (W4).
            _MONEY_KEYS = (
                "transfer_cost_usd", "revenue_gain_usd", "source_loss_usd",
                "revenue_impact_usd", "net_benefit_usd",
                "predicted_on_rent_hours_gain", "predicted_turnaways_avoided",
            )
            if any(k not in cand for k in _MONEY_KEYS):
                continue
            money: dict = {k: cand[k] for k in _MONEY_KEYS}
            money["net_benefit_usd"] = float(money["net_benefit_usd"])
        else:
            # Count scaled down — per_day inputs required; drop if absent or net too low
            try:
                money = money_for(cand, actual_count)
            except ValueError:
                continue
            if money["net_benefit_usd"] < min_net:
                continue

        if money["net_benefit_usd"] < min_net:
            continue

        used_vins.update(taken)
        remaining_spare[fid] -= actual_count
        remaining_short[tid] -= actual_count
        remaining_parking[tid] -= actual_count

        moves.append({
            "candidate_id": cand["candidate_id"],
            "from_location_id": fid,
            "to_location_id": cand["to_location_id"],
            "vehicle_vins": taken,
            "timing": "today",
            "detail": _DETAIL,
            "diagnosis_source": "rules",
            **money,
        })

    return moves
