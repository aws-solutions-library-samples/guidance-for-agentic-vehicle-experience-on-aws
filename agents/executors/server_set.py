"""Server-set parameter rule for the approve route (spec D11, contract § 4).

Extracted from ``lambdas/api-avx-findings/handler.py`` so the simulated fleet
operator (spec D10e, task 11.3) can build an Action's parameters using the same
rule without calling the approve Lambda.

The findings handler re-imports :func:`check_server_set_params` as its private
``_check_server_set_params``; its tests therefore pass unedited.

Public API
----------
check_server_set_params(finding, action_kind, body_parameters)
    -> (completed_params, error_or_None, decision)
"""
from __future__ import annotations

import math
from typing import Optional

from agents.executors.policy import ACTION_KINDS as _ACTION_KINDS_TABLE


def check_server_set_params(
    finding: dict,
    action_kind: str,
    body_parameters: dict,
) -> "tuple[dict, Optional[str], str]":
    """Validate and fill server-set / body-overridable params (contract § 3, spec D11).

    Returns ``(completed_params, error_or_None, decision)`` where ``decision`` is
    ``'approved'`` or ``'overridden'``.  On error the caller must return 400 (HTTP) or
    skip the unit (sim-operator); the other two values are undefined in that case.

    Rules (decisions.md 2026-10-05 call 3 / call 10 / group 3 task 3.2):
    - server_set: body must omit or match the server value (400 otherwise); filled from server.
      ``vehicle_vin`` is always taken from ``finding.scope.vin`` (what the fleet check ran on);
      scope vin missing → 400.  Other server-set keys absent or blank in the recommendation
      → 400 ("recommendation is missing parameters.<key>"), except ``fault_code`` (optional
      per DMS § 3.2) — if fault_code is absent in the recommendation the body must also omit it.
      Server-set params never cause ``'overridden'``.
    - body_overridable: body may differ from rec → ``'overridden'``; omission fills from
      rec and is NOT an override.  ``dealer_id`` must be a non-empty string (stripped);
      ``notes`` must be a string of at most 1000 characters.
    - Unknown body keys (not in server_set or body_overridable) are dropped silently.
    """
    # --- move_vehicles: dedicated branch (G4-D11) -----------------------------------------
    # fleet_id from scope.fleet_id; from/to_location_id from finding.location;
    # five rec fields required (missing → 400); body values must be absent or equal the
    # pre-scale recommendation value; vehicle_vins is body-overridable with subset rule.
    if action_kind == "move_vehicles":
        fleet_id = (finding.get("scope") or {}).get("fleet_id")
        _loc = finding.get("location") or {}
        from_loc = _loc.get("from_location_id")
        to_loc = _loc.get("to_location_id")
        _rec: dict = (finding.get("recommended_action") or {}).get("parameters") or {}
        forecast_week = _rec.get("forecast_week")
        transfer_cost = _rec.get("transfer_cost_usd")
        hours_gain = _rec.get("predicted_on_rent_hours_gain")
        turnaways = _rec.get("predicted_turnaways_avoided")
        # The Finding's own fleet and locations are the server values; the recommendation
        # must repeat them, so a drifted recommendation can't move vehicles somewhere else.
        for _k, _v in (("fleet_id", fleet_id), ("from_location_id", from_loc),
                       ("to_location_id", to_loc)):
            if not isinstance(_v, str) or not _v.strip():
                return {}, f"parameters.{_k}: the Finding carries no {_k}", ""
            if _rec.get(_k) != _v:
                return {}, f"parameters.{_k}: recommendation does not match the Finding", ""
        # Five rec fields must be present.
        for _k, _v in (
            ("forecast_week", forecast_week),
            ("transfer_cost_usd", transfer_cost),
            ("predicted_on_rent_hours_gain", hours_gain),
            ("predicted_turnaways_avoided", turnaways),
        ):
            if _v is None:
                return {}, f"recommendation is missing parameters.{_k}", ""
        # Server-set: body must be absent or equal the pre-scale rec value.
        # Numbers compared as numbers (contract § 4).
        _server_vals: dict = {
            "fleet_id": fleet_id,
            "from_location_id": from_loc,
            "to_location_id": to_loc,
            "forecast_week": forecast_week,
            "transfer_cost_usd": float(transfer_cost),  # pre-scale basis
            "predicted_on_rent_hours_gain": float(hours_gain),  # pre-scale basis
            "predicted_turnaways_avoided": int(turnaways),  # pre-scale basis
        }
        _float_keys = frozenset({"transfer_cost_usd", "predicted_on_rent_hours_gain",
                                 "predicted_turnaways_avoided"})
        _int_keys: frozenset = frozenset()
        for _k, _sv in _server_vals.items():
            _bv = body_parameters.get(_k)
            if _bv is None:
                continue  # absent: filled from server below
            if _k in _float_keys:
                try:
                    _match = (not isinstance(_bv, bool)) and float(_bv) == float(_sv)
                except (TypeError, ValueError):
                    _match = False
            elif _k in _int_keys:
                try:
                    _match = int(_bv) == int(_sv)
                except (TypeError, ValueError):
                    _match = False
            else:
                _match = _bv == _sv
            if not _match:
                return {}, f"parameters.{_k} must match the Finding's recommended value", ""
        # vehicle_vins: omitted → full rec list; else non-empty, distinct strings, each in rec.
        _rec_vins_raw = _rec.get("vehicle_vins")
        if (not isinstance(_rec_vins_raw, list) or not _rec_vins_raw
                or not all(isinstance(v, str) and v for v in _rec_vins_raw)
                or len(set(_rec_vins_raw)) != len(_rec_vins_raw)):
            return {}, "recommendation parameters.vehicle_vins must be a non-empty list of distinct VINs", ""
        _rec_vins: list = list(_rec_vins_raw)
        _body_vins_raw = body_parameters.get("vehicle_vins")
        if _body_vins_raw is None:
            _final_vins = _rec_vins
            _decision = "approved"
        else:
            if not isinstance(_body_vins_raw, list) or len(_body_vins_raw) == 0:
                return {}, "parameters.vehicle_vins must be a non-empty list", ""
            for _vv in _body_vins_raw:
                if not isinstance(_vv, str):
                    return {}, "parameters.vehicle_vins must contain strings", ""
            if len(_body_vins_raw) != len(set(_body_vins_raw)):
                return {}, "parameters.vehicle_vins must not contain duplicates", ""
            _rec_vins_set = set(_rec_vins)
            for _vv in _body_vins_raw:
                if _vv not in _rec_vins_set:
                    return (
                        {},
                        f"parameters.vehicle_vins contains a VIN not in the recommendation: {_vv}",
                        "",
                    )
            # Output in recommendation's order; decide subset vs full.
            _body_vins_set = set(_body_vins_raw)
            _final_vins = [v for v in _rec_vins if v in _body_vins_set]
            _decision = "approved" if _body_vins_set == _rec_vins_set else "overridden"
        _scale = len(_final_vins) / len(_rec_vins)
        # Floats here; _approve_finding converts them to Decimal before the Action is put
        # (the boto3 resource API refuses Python floats).
        return (
            {
                "fleet_id": fleet_id,
                "from_location_id": from_loc,
                "to_location_id": to_loc,
                "forecast_week": forecast_week,
                "transfer_cost_usd": round(float(transfer_cost) * _scale, 2),
                "predicted_on_rent_hours_gain": round(float(hours_gain) * _scale, 2),
                "predicted_turnaways_avoided": math.floor(float(turnaways) * _scale),
                "vehicle_vins": _final_vins,
            },
            None,
            _decision,
        )

    row = _ACTION_KINDS_TABLE.get(action_kind)
    server_set = getattr(row, "server_set", None) or ()
    body_overridable = getattr(row, "body_overridable", None) or ()
    rec_params: dict = (finding.get("recommended_action") or {}).get("parameters") or {}
    # Rule 3: unknown body keys are dropped — start empty, populate only declared keys.
    params: dict = {}

    for key in server_set:
        if key == "vehicle_vin":
            # Rule 1: VIN is always sourced from scope (what the fleet check ran on).
            scope_vin = (finding.get("scope") or {}).get("vin")
            if not scope_vin:
                return params, "parameters.vehicle_vin: scope vin is missing from the Finding", ""
            rec_vin = rec_params.get("vehicle_vin")
            if rec_vin and rec_vin != scope_vin:
                return params, "parameters.vehicle_vin: recommendation vin does not match scope", ""
            body_val = body_parameters.get("vehicle_vin")
            if body_val is not None and body_val != scope_vin:
                return params, "parameters.vehicle_vin must match the Finding's recommended value", ""
            params[key] = scope_vin
        elif key == "recall_id" and action_kind == "ground_vehicle":
            # --- ground_vehicle dual-form: detect form from rec_params ---
            # The fault form carries fault_codes (Stage D); the recall form carries
            # recall_id (Stage C).  Exactly one must be present.
            # W1 fix: treat a blank recall_id the same as absent, matching cms.py:159.
            _raw_recall_id = rec_params.get("recall_id")
            rec_has_recall = isinstance(_raw_recall_id, str) and bool(_raw_recall_id.strip())
            rec_has_faults = rec_params.get("fault_codes") is not None

            if rec_has_recall and rec_has_faults:
                return params, "ground_vehicle: recommendation has both recall_id and fault_codes", ""
            if not rec_has_recall and not rec_has_faults:
                return params, "ground_vehicle: recommendation must carry recall_id or fault_codes", ""

            if rec_has_recall:
                # Stage C recall form — recall_id is a non-blank string (checked above).
                recall_id = _raw_recall_id.strip()  # type: ignore[union-attr]
                body_val = body_parameters.get("recall_id")
                if body_val is not None and body_val != recall_id:
                    return params, "parameters.recall_id must match the Finding's recommended value", ""
                params["recall_id"] = recall_id
            else:
                # Stage D fault form — substitute fault_codes for recall_id in this iteration.
                fault_codes = rec_params["fault_codes"]
                # Fix #4: fault_codes must be a non-empty list of non-blank strings (spec D11).
                if (
                    not isinstance(fault_codes, list)
                    or not fault_codes
                    or not all(isinstance(c, str) and c.strip() for c in fault_codes)
                ):
                    return (
                        params,
                        "parameters.fault_codes must be a non-empty list of non-blank strings",
                        "",
                    )
                body_val = body_parameters.get("fault_codes")
                if body_val is not None and body_val != fault_codes:
                    return params, "parameters.fault_codes must match the Finding's recommended value", ""
                params["fault_codes"] = fault_codes
        else:
            rec_val = rec_params.get(key)
            # W2 (G3F1.5): validate each need in the server-set 'needs' list before
            # recording the Action.  A malformed need is dropped silently by
            # DmsExecutor (which skips non-Mapping items or items without labor_op),
            # booking fewer services than approved.  Refuse here instead (400 before
            # any write) so the operator is told exactly which need is malformed.
            if key == "needs" and rec_val is not None:
                if not isinstance(rec_val, list) or not rec_val:
                    return params, "parameters.needs must be a non-empty list", ""
                for _i, _need in enumerate(rec_val):
                    if not isinstance(_need, dict):
                        return (
                            params,
                            f"parameters.needs[{_i}] must be an object",
                            "",
                        )
                    _labor_op = _need.get("labor_op")
                    if not isinstance(_labor_op, str) or not _labor_op.strip():
                        return (
                            params,
                            f"parameters.needs[{_i}].labor_op must be a non-blank string",
                            "",
                        )
            # Rule 2: rec must supply a non-blank value for every server-set key, except
            # fault_code (optional per DMS § 3.2; body must also omit it if rec omits it).
            if (rec_val is None or rec_val == "") and key != "fault_code":
                return params, f"recommendation is missing parameters.{key}", ""
            body_val = body_parameters.get(key)
            if body_val is not None and body_val != rec_val:
                return params, f"parameters.{key} must match the Finding's recommended value", ""
            if rec_val is None:
                continue  # fault_code absent from rec and body: not stored (S2, no None padding)
            params[key] = rec_val

    decision = "approved"
    for key in body_overridable:
        rec_val = rec_params.get(key)
        body_val = body_parameters.get(key)
        if body_val is not None:
            # Rule 4: validate overridable values supplied by the body.
            if key == "dealer_id":
                if not isinstance(body_val, str) or not body_val.strip():
                    return params, "parameters.dealer_id must be a non-empty string", ""
                body_val = body_val.strip()
            elif key == "notes":
                if not isinstance(body_val, str) or len(body_val) > 1000:
                    return params, "parameters.notes must be a string of at most 1000 characters", ""
            elif key == "urgency":
                # Contract § 4: urgency must be one of the three valid values.
                _VALID_URGENCIES = frozenset({"this_week", "this_month", "next_visit"})
                if body_val not in _VALID_URGENCIES:
                    return params, f"parameters.urgency must be one of: {sorted(_VALID_URGENCIES)}", ""
                # Contract § 4: p0 items require 'this_week' only.
                if finding.get("severity") == "p0" and body_val != "this_week":
                    return params, "parameters.urgency: p0 items require urgency 'this_week'", ""
            if body_val != rec_val:
                # Fix #1 (spec D11 / contract § 2.7 / § 4): for
                # ``record_engineering_decision`` only, changing ``notes`` alone does
                # NOT make the decision overridden.  Stage C's ``submit_warranty_claim``
                # keeps notes as an override trigger.
                # G3F3.1 (c W3), narrowed in cycle 4 (a W4, user 2026-10-09): only
                # stage D's p0 booking with no candidate rooftop leaves dealer_id out
                # of the recommendation, and the operator filling it is 'approved'
                # (design answer 9). Every other change, including filling any other
                # missing value or any other kind's missing dealer_id, stays
                # 'overridden' (stage C's contract, test_approve_fix_f5).
                fills_p0_rooftop = (
                    rec_val is None
                    and action_kind == "book_fleet_service"
                    and key == "dealer_id"
                )
                if not fills_p0_rooftop and not (
                    action_kind == "record_engineering_decision" and key == "notes"
                ):
                    decision = "overridden"
            params[key] = body_val
        elif rec_val is not None:
            params[key] = rec_val  # S2: fill only keys the recommendation has

    # W2 (review cycle 1 routes): a booking needs a rooftop after the fill, as Stage A's
    # _dms_parameters required; never record an Action DMS can't be sent.
    if action_kind in ("book_recall_service", "book_fleet_service"):
        dealer = params.get("dealer_id")
        if not isinstance(dealer, str) or not dealer.strip():
            return params, "parameters.dealer_id is required", ""

    # Security review cycle 1 W1: p0 => 'this_week' must hold after the fill too, not only
    # when the body supplies urgency, so a drifted recommendation (the rules path today,
    # the model in 4.1) can never send a weaker p0 booking to DMS. Fails closed on a
    # missing urgency. Mutation guard: drop this check -> a p0 rec with 'this_month' and
    # a body without urgency is approved -> TestP0UrgencyAfterFill FAILS.
    if ("urgency" in body_overridable and finding.get("severity") == "p0"
            and params.get("urgency") != "this_week"):
        return params, "parameters.urgency: p0 items require urgency 'this_week'", ""

    return params, None, decision
