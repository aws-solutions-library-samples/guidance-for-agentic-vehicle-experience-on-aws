"""Simulated fleet operator Lambda (spec D10, task 11.3).

Scheduled by EventBridge Scheduler ``cron(10 7 * * ? *)`` UTC.  For each enabled
demo fleet, reads all Findings, classifies them, builds a plan, executes it, and
writes a run record to ``vsa-{stage}-avx-agent-runs``.

Environment variables:
  AVX_SIM_OPERATOR_TABLE         — ledger table (``vsa-{stage}-avx-sim-operator``)
  AVX_ACTIONS_TABLE              — Actions table (``vsa-{stage}-avx-actions``)
  AVX_FINDINGS_TABLE             — Findings table (``vsa-{stage}-avx-findings``)
  AVX_AGENT_RUNS_TABLE           — agent runs table (``vsa-{stage}-avx-agent-runs``)
  FI_SIM_OPERATOR_PARAM          — SSM path ``/cvx/{stage}/fi/sim-operator``
  FI_SIM_OPERATOR_ENABLED        — must equal ``"true"`` (set only on this Lambda)
  CMS_GROUNDINGS_TABLE           — CMS groundings table (for grounded-VIN check)
  CMS_VEHICLES_TABLE             — CMS registry table (vehicle-id → VIN resolution)
  VSA_DMS_INTERNAL_API_ENDPOINT  — DMS internal API (forwarded to DmsExecutor)
  VSA_CMS_FI_INTERNAL_API_ENDPOINT — CMS FI internal API (forwarded to CmsExecutor)
  VSA_AGENT_VERSION              — agent version string (default "0.1.0")
  AWS_REGION                     — Lambda runtime-provided

Handler event shape:
  Invoked by EventBridge Scheduler: ``{}`` or ``{"mode": "dry_run"|"observe"|"approve"}``
  Default mode: ``"approve"``.

Response shape:
  ``{"status": "ok"|"config_error"|"error",
     "mode": <mode>,
     "fleets": {<fleet_id>: {...per-fleet counts...}},
     "error": <optional error string>}``
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_LAMBDA_ROOT = os.path.abspath(os.path.dirname(__file__))
_LAMBDAS_ROOT = os.path.dirname(_LAMBDA_ROOT)
_REPO_ROOT = os.path.abspath(os.path.join(_LAMBDA_ROOT, "..", ".."))
for _p in (_LAMBDA_ROOT, _LAMBDAS_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

_AGENT_VERSION = os.environ.get("VSA_AGENT_VERSION", "0.1.0")

# ---------------------------------------------------------------------------
# Required env vars — unset means misconfigured Lambda, fail closed
# ---------------------------------------------------------------------------
_REQUIRED_ENV = (
    "AVX_SIM_OPERATOR_TABLE",
    "AVX_ACTIONS_TABLE",
    "AVX_FINDINGS_TABLE",
    "AVX_AGENT_RUNS_TABLE",
    "FI_SIM_OPERATOR_PARAM",
)

_ddb = None
_ssm = None


def _get_ddb() -> Any:
    global _ddb
    if _ddb is None:
        import boto3  # noqa: PLC0415
        _ddb = boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION", "us-west-2"))
    return _ddb


def _get_ssm() -> Any:
    global _ssm
    if _ssm is None:
        import boto3  # noqa: PLC0415
        _ssm = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "us-west-2"))
    return _ssm


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# SSM config loader
# ---------------------------------------------------------------------------

def _load_ssm_config(param_name: str) -> Any:
    """Return the parsed JSON value of the SSM parameter, or raise."""
    resp = _get_ssm().get_parameter(Name=param_name)
    raw = resp["Parameter"]["Value"]
    return json.loads(raw)


# ---------------------------------------------------------------------------
# CMS registry lookup (fix e: hold_vehicle_ids → VINs)
# ---------------------------------------------------------------------------

#: A VIN: 17 characters, no I, O or Q (ISO 3779).
_VIN_SHAPED = re.compile(r"[A-HJ-NPR-Z0-9]{17}")


def _resolve_vehicle_ids_to_vins(
    vehicle_ids: frozenset[str],
    vehicles_table: str,
) -> dict[str, str]:
    """Return ``{vehicle_id: vin}`` for every id in ``vehicle_ids``.

    Each id is looked up by ``vehicleId`` (primary key of the CMS registry).
    A missing item is considered unresolvable and the caller must stop the run.

    Fix (e) D10 11.3: ``hold_vehicle_ids`` from the config are resolved here.
    An id that can't be resolved stops EVERY fleet for the run so the vehicle
    is never touched while its VIN is unknown.

    Mutation guard (mutant = skip resolution): a held vehicle_id would not be
    resolved; its VIN would not be added to hold_vins; the vehicle could be
    approved. ``test_hold_vehicle_id_unresolvable_stops_all_fleets`` catches this
    with a fake that returns no item for the given id.
    """
    result: dict[str, str] = {}
    for vehicle_id in vehicle_ids:
        resp = _get_ddb().get_item(
            TableName=vehicles_table,
            Key={"vehicleId": {"S": vehicle_id}},
            ProjectionExpression="vehicleId, vin",
            ConsistentRead=True,
        )
        item = resp.get("Item", {})
        vin = item.get("vin", {}).get("S")
        if not vin and not item and _VIN_SHAPED.fullmatch(vehicle_id):
            # vehicleId = VIN (CMS spec 2026-10-28-vehicleid-equals-vin): a VIN-shaped id
            # names the car whose VIN it is, under either id convention. Before that car's
            # cohort is re-keyed no row has this id yet; holding the VIN holds the car.
            # A non-VIN id that resolves to nothing still stops every fleet (below).
            log.info("sim-operator: hold_vehicle_id %r has no registry row yet; held as its VIN", vehicle_id)
            vin = vehicle_id
        if not vin:
            raise RuntimeError(
                f"sim-operator: hold_vehicle_id {vehicle_id!r} could not be resolved to a VIN "
                f"in {vehicles_table}; stopping the run to protect this vehicle"
            )
        result[vehicle_id] = vin
    return result


# ---------------------------------------------------------------------------
# Findings scan (by_fleet GSI)
# ---------------------------------------------------------------------------

def _scan_findings_for_fleet(fleet_id: str, findings_table: str) -> list[dict]:
    """Fetch all Findings for ``fleet_id`` via the ``by_fleet`` GSI (all pages)."""
    from boto3.dynamodb.types import TypeDeserializer  # noqa: PLC0415

    deser = TypeDeserializer()
    results: list[dict] = []
    last_key = None
    while True:
        kwargs: dict[str, Any] = {
            "TableName": findings_table,
            "IndexName": "by_fleet",
            "KeyConditionExpression": "fleet_id = :fid",
            "ExpressionAttributeValues": {":fid": {"S": fleet_id}},
            "ConsistentRead": False,
        }
        if last_key:
            kwargs["ExclusiveStartKey"] = last_key
        resp = _get_ddb().query(**kwargs)
        for raw in resp.get("Items", []):
            results.append({k: deser.deserialize(v) for k, v in raw.items()})
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
    return results


# ---------------------------------------------------------------------------
# Decision existence check
# ---------------------------------------------------------------------------

def _get_decided_ids(finding_ids: list[str], actions_table: str) -> set[str]:
    """Return the set of finding_ids for which a decision Action already exists.

    Uses the derived ``action_id`` formula (same as the approve route and
    auto-approver) with ConsistentRead to avoid approving a Finding that was
    decided between the scan and this check.
    """
    from agents.fi_sim_operator.run import _derive_action_id  # noqa: PLC0415

    decided: set[str] = set()
    for fid in finding_ids:
        action_id = _derive_action_id(fid)
        resp = _get_ddb().get_item(
            TableName=actions_table,
            Key={"action_id": {"S": action_id}},
            ProjectionExpression="action_id",
            ConsistentRead=True,
        )
        if resp.get("Item"):
            decided.add(fid)
    return decided


# ---------------------------------------------------------------------------
# EMF metric emission
# ---------------------------------------------------------------------------

def _emit_emf(
    fleet_id: str,
    mode: str,
    decisions: int,
    skips: int,
    errors: int,
    run_completed: bool,
) -> None:
    """Emit CloudWatch Embedded Metric Format counts (spec D10g liveness metric)."""
    try:
        import json as _json  # noqa: PLC0415
        metric = {
            "_aws": {
                "Timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
                "CloudWatchMetrics": [{
                    "Namespace": "AVX/SimOperator",
                    "Dimensions": [["FleetId", "Mode"]],
                    "Metrics": [
                        {"Name": "DecisionsWritten", "Unit": "Count"},
                        {"Name": "UnitsSkipped", "Unit": "Count"},
                        {"Name": "RunErrors", "Unit": "Count"},
                    ],
                }],
            },
            "FleetId": fleet_id,
            "Mode": mode,
            "DecisionsWritten": decisions,
            "UnitsSkipped": skips,
            "RunErrors": errors,
        }
        print(_json.dumps(metric))  # CloudWatch agent picks this up from stdout
    except Exception as exc:  # noqa: BLE001
        log.warning("sim-operator: EMF emission failed (%s)", type(exc).__name__)


def _emit_run_completed() -> None:
    """One dimensionless ``AVX/SimOperator`` ``SimOperatorRunCompleted = 1`` per run.

    The staleness alarm (``vsa-{stage}-avx-sim-operator-stale-{region}``) reads this
    metric with no dimensions; a run that never got this far (a timeout, a crash
    before the run record) emits nothing, and 27 empty hours alarm.
    """
    print(json.dumps({
        "_aws": {
            "Timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": "AVX/SimOperator",
                "Dimensions": [[]],
                "Metrics": [{"Name": "SimOperatorRunCompleted", "Unit": "Count"}],
            }],
        },
        "SimOperatorRunCompleted": 1,
    }))


# ---------------------------------------------------------------------------
# CMS open FI fault groundings (11.3b item 7)
# ---------------------------------------------------------------------------

def _read_grounded_codes(fleet_id: str, groundings_table: str) -> tuple[dict[str, frozenset[str]], int]:
    """``({vin: fault codes}, error_count)`` from the fleet's open CMS groundings.

    Reads ``open-groundings-index`` (``openFleetId`` = the fleet; CMS removes the
    attribute when a car returns, so every row is open), all pages. Only FI fault
    rows count: ``reason == "fault"`` with ``findingId`` and ``actionId`` (FI made
    them) and a ``faultCodes`` list; the codes of a VIN's rows are unioned. A read
    error, or the table name unset, gives ``({}, 1)``: every VIN is ungrounded, so a
    p0 booking waits (the safe direction) and the run counts an error.
    """
    if not groundings_table:
        log.error("sim-operator: CMS_GROUNDINGS_TABLE is not set; no VIN counts as grounded")
        return {}, 1
    codes: dict[str, set[str]] = {}
    kwargs: dict[str, Any] = {
        "TableName": groundings_table,
        "IndexName": "open-groundings-index",
        "KeyConditionExpression": "openFleetId = :f",
        "ExpressionAttributeValues": {":f": {"S": fleet_id}},
    }
    try:
        while True:
            resp = _get_ddb().query(**kwargs)
            for item in resp.get("Items", []):
                if item.get("reason", {}).get("S") != "fault":
                    continue
                if not item.get("findingId", {}).get("S") or not item.get("actionId", {}).get("S"):
                    continue
                vin = item.get("vin", {}).get("S")
                if not vin:
                    continue
                row_codes = {
                    c.get("S") for c in item.get("faultCodes", {}).get("L", [])
                    if isinstance(c, dict) and c.get("S")
                } | set(item.get("faultCodes", {}).get("SS", []))
                if row_codes:
                    codes.setdefault(vin, set()).update(row_codes)
            last = resp.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
    except Exception as exc:  # noqa: BLE001
        log.error("sim-operator: open groundings read failed for %s (%s)", fleet_id, type(exc).__name__)
        return {}, 1
    return {vin: frozenset(c) for vin, c in codes.items()}, 0


def _recall_pending_groundings(
    findings: list[dict], decided_ids: set[str], now_iso: str,
) -> set[tuple[str, str]]:
    """``(vin, campaign)`` of every undecided, unexpired recall grounding Finding.

    11.3b item 8: a standalone ``recall_book`` waits while any recall grounding
    Finding for the same VIN and campaign is pending, whether or not the stand-in
    can take that grounding this run (held, too new, cap): a booking must not run
    ahead of the grounding a person may still approve.
    """
    from agents.fi_sim_operator.select import _is_expired  # noqa: PLC0415

    pending: set[tuple[str, str]] = set()
    for f in findings:
        rec = f.get("recommended_action") or {}
        if rec.get("action_kind") != "ground_vehicle":
            continue
        recall_id = (rec.get("parameters") or {}).get("recall_id")
        vin = (f.get("scope") or {}).get("vin")
        if not (isinstance(recall_id, str) and recall_id and isinstance(vin, str) and vin):
            continue
        if f.get("finding_id") in decided_ids or _is_expired(f, now_iso):
            continue
        pending.add((vin, recall_id))
    return pending


# ---------------------------------------------------------------------------
# Run record
# ---------------------------------------------------------------------------

def _write_run_record(
    *,
    started_at: str,
    finished_at: str,
    mode: str,
    fleet_results: dict,
    status: str,
    errors: int,
    reason: Optional[str] = None,
    component: str,
) -> bool:
    """Write ``agent-fi-sim-operator#{component}`` to ``AVX_AGENT_RUNS_TABLE``; True if stored.

    11.3b item 4: the first pass passed an ``extra=`` keyword ``write_run_record``
    doesn't take, and swallowed the TypeError, so no run record was ever written.
    The per-run detail goes in the ``sim_operator`` map.
    S3: the component is the run's mode (``dry_run``, ``observe``, ``approve``), so a
    by-hand dry run or observe run never shares the scheduled approve run's key.
    """
    from agents.tier2.run_records import write_run_record  # noqa: PLC0415
    import boto3  # noqa: PLC0415

    table = os.environ.get("AVX_AGENT_RUNS_TABLE", "")
    if not table:
        return False
    ddb_resource = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-west-2"))
    detail: dict[str, Any] = {"mode": mode, "errors": int(errors), "fleets": fleet_results}
    if reason:
        detail["reason"] = reason
    return write_run_record(
        agent_id="agent-fi-sim-operator",
        component=component,
        started_at=started_at,
        finished_at=finished_at,
        status=status,
        artifacts_written=sum(int(fr.get("decisions_written", 0)) for fr in fleet_results.values()),
        duplicates_skipped=sum(int(fr.get("units_person_first", 0)) for fr in fleet_results.values()),
        agent_version=_AGENT_VERSION,
        ddb_client=ddb_resource,
        table_name=table,
        tokens_used=0,
        sim_operator=detail,
    )


def _finish(
    *, started_at: str, mode: str, fleet_results: dict, status: str, errors: int,
    reason: Optional[str] = None, response_status: str,
) -> dict:
    """Write the run record, emit the liveness metric, and raise if anything erred.

    11.3b item 5: the schedule and the function don't retry, so an error must reach
    the Lambda ``Errors`` alarm: raise AFTER the run record, which says what failed.
    A run record that didn't write is an error too. The liveness metric is emitted
    only for a run that got here and recorded itself.

    The record's component is the mode (``#dry_run``, ``#observe``, ``#approve``), and
    only an approve run, the one the schedule makes (input ``{}``), emits the liveness
    metric: a by-hand dry run or ``--observe-now`` must not reset the 27 h staleness
    alarm (issue 2026-11-08-sim-operator-observe-run-label; S3 for dry_run).
    """
    from agents.fi_sim_operator.run import MODE_APPROVE  # noqa: PLC0415

    stored = _write_run_record(
        started_at=started_at, finished_at=_now_iso(), mode=mode,
        fleet_results=fleet_results, status=status, errors=errors, reason=reason,
        component=mode,
    )
    if not stored:
        log.error("sim-operator: the run record was not written")
        errors += 1
    elif mode == MODE_APPROVE:
        _emit_run_completed()
    response = {"status": response_status, "mode": mode, "fleets": fleet_results, "errors": errors}
    if reason:
        response["error"] = reason
    if errors:
        raise RuntimeError(f"sim-operator: run finished with {errors} error(s): {json.dumps(response)[:1500]}")
    return response


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------

def handler(event: dict, context: Any) -> dict:  # noqa: ARG001
    """Scheduled Lambda handler for the simulated fleet operator (spec D10).

    Event: ``{}`` (the schedule) or ``{"mode": "dry_run"|"observe"|"approve"}`` exactly.
    Any other mode → ``config_error``, nothing read or written (11.3b item 3).

    Outcomes:
    - the parameter doesn't exist → ``off``: a run record, the liveness metric if the run is
      approve, no decisions (the switch is off; D10a);
    - unreadable or malformed parameter, an unresolvable held vehicle id → a run record
      with the reason, then raise (the Errors alarm);
    - a run with any unit, read or run-record error → raise after the run record.
    """
    from agents.fi_sim_operator.config import parse_config, ConfigError  # noqa: PLC0415
    from agents.fi_sim_operator.select import classify_finding  # noqa: PLC0415
    from agents.fi_sim_operator.pairing import pair_booking  # noqa: PLC0415
    from agents.fi_sim_operator.plan import build_plan  # noqa: PLC0415
    from agents.fi_sim_operator.ledger import (  # noqa: PLC0415
        get_first_seen_times,
        get_today_cap_used,
        record_first_seen,
    )
    from agents.fi_sim_operator.run import run_fleet, MODE_DRY_RUN, MODE_OBSERVE, MODE_APPROVE  # noqa: PLC0415
    from agents.executors.simulated_operator import process_enabled, SIMULATED_KINDS  # noqa: PLC0415

    # ── Safety: must only run in the sim-operator process ────────────────────
    if not process_enabled():
        raise RuntimeError(
            "sim-operator: FI_SIM_OPERATOR_ENABLED is not 'true'; "
            "this Lambda must not run without the flag set"
        )

    # ── Mode: exact values only; anything else writes nothing (item 3) ───────
    raw_mode = (event or {}).get("mode", MODE_APPROVE)
    if raw_mode not in (MODE_DRY_RUN, MODE_OBSERVE, MODE_APPROVE):
        log.error("sim-operator: unknown mode %r; nothing read or written", raw_mode)
        return {"status": "config_error", "mode": None, "fleets": {},
                "error": f"unknown mode {str(raw_mode)[:40]!r}"}
    mode: str = raw_mode

    # ── Required env vars ────────────────────────────────────────────────────
    missing = [name for name in _REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"sim-operator: required env var(s) unset: {missing}")

    ledger_table = os.environ["AVX_SIM_OPERATOR_TABLE"]
    actions_table = os.environ["AVX_ACTIONS_TABLE"]
    findings_table = os.environ["AVX_FINDINGS_TABLE"]
    vehicles_table = os.environ.get("CMS_VEHICLES_TABLE", "")
    groundings_table = os.environ.get("CMS_GROUNDINGS_TABLE", "")
    param_name = os.environ["FI_SIM_OPERATOR_PARAM"]

    started_at = _now_iso()
    today = _today_utc()

    # ── Load SSM config ──────────────────────────────────────────────────────
    try:
        raw_config = _load_ssm_config(param_name)
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "") if hasattr(exc, "response") else ""
        if code == "ParameterNotFound":
            log.info("sim-operator: %s does not exist; the stand-in is off", param_name)
            return _finish(started_at=started_at, mode=mode, fleet_results={}, status="completed",
                           errors=0, reason="parameter_absent", response_status="off")
        log.error("sim-operator: could not load SSM config (%s)", type(exc).__name__)
        return _finish(started_at=started_at, mode=mode, fleet_results={}, status="aborted",
                       errors=1, reason=f"ssm_unreadable:{type(exc).__name__}",
                       response_status="config_error")

    cfg = parse_config(raw_config)
    if isinstance(cfg, ConfigError):
        log.error("sim-operator: invalid config: %s", cfg.reason)
        return _finish(started_at=started_at, mode=mode, fleet_results={}, status="aborted",
                       errors=1, reason=f"config_invalid:{cfg.reason}"[:300],
                       response_status="config_error")

    if cfg.non_demo_ids:
        log.warning("sim-operator: config contains non-demo fleet ids: %s", cfg.non_demo_ids)

    # ── fix (e): resolve hold_vehicle_ids → VINs; failure stops every fleet ──
    extra_hold_vins: frozenset[str] = frozenset()
    if cfg.hold_vehicle_ids:
        if not vehicles_table:
            return _finish(started_at=started_at, mode=mode, fleet_results={}, status="aborted",
                           errors=1, reason="hold_vehicle_ids present but CMS_VEHICLES_TABLE not set",
                           response_status="error")
        try:
            vid_to_vin = _resolve_vehicle_ids_to_vins(cfg.hold_vehicle_ids, vehicles_table)
        except Exception as exc:  # noqa: BLE001
            log.error("sim-operator: %s", exc)
            return _finish(started_at=started_at, mode=mode, fleet_results={}, status="aborted",
                           errors=1, reason=f"hold_unresolved:{type(exc).__name__}",
                           response_status="error")
        extra_hold_vins = frozenset(vid_to_vin.values())
        log.info("sim-operator: resolved %d hold_vehicle_ids to VINs", len(extra_hold_vins))

    hold_vins = cfg.hold_vins | extra_hold_vins

    # ── Per-fleet run ────────────────────────────────────────────────────────
    fleet_results: dict[str, Any] = {}
    run_errors = 0

    for fleet_id in cfg.enabled_fleets:
        log.info("sim-operator: processing fleet %s (mode=%s)", fleet_id, mode)
        try:
            findings = _scan_findings_for_fleet(fleet_id, findings_table)

            # item 6: decision reads only for findings the stand-in could decide.
            in_scope_ids = [
                f["finding_id"] for f in findings
                if f.get("finding_id")
                and ((f.get("recommended_action") or {}).get("action_kind") in SIMULATED_KINDS)
            ]
            decided_ids = _get_decided_ids(in_scope_ids, actions_table)

            # item 7: open FI fault groundings, by VIN and code.
            grounded_codes, grounding_errors = _read_grounded_codes(fleet_id, groundings_table)

            # item 8: pending recall groundings block a standalone recall booking.
            recall_pending = _recall_pending_groundings(findings, decided_ids, started_at)

            classified = [
                classify_finding(
                    finding=f,
                    decided_ids=decided_ids,
                    now_iso=started_at,
                    hold_vins=hold_vins,
                    grounded_codes=grounded_codes,
                    recall_pending_groundings=recall_pending,
                )
                for f in findings
            ]

            candidate_ids = [
                cf.finding.get("finding_id", "") for cf in classified if cf.class_name is not None
            ]

            # W4: count candidates BEFORE the 20 h first-seen and daily-cap filters
            # so dry_run leg 1 reports non-zero class counts even with an empty ledger.
            _cands_by_cls: dict[str, int] = {}
            _cand_ids_by_cls: dict[str, list[str]] = {}
            for _cf in classified:
                if _cf.class_name is not None:
                    _cls = _cf.class_name
                    _cands_by_cls[_cls] = _cands_by_cls.get(_cls, 0) + 1
                    _ids_list = _cand_ids_by_cls.setdefault(_cls, [])
                    _fid = _cf.finding.get("finding_id", "")
                    if _fid and len(_ids_list) < 50:
                        _ids_list.append(_fid)

            # C1: compute pairs BEFORE first-seen so booking ids are included in
            # the SEEN fetch and record — pair bookings are unclassified (class_name
            # is None) so they never appear in candidate_ids, but plan.py requires
            # a ≥20 h first-seen for the booking leg of every pair.
            paired_bookings: dict[str, tuple] = {}
            for cf in classified:
                if cf.class_name in ("fh_ground_book", "recall_ground_book"):
                    paired_bookings[cf.finding.get("finding_id", "")] = pair_booking(
                        grounding=cf.finding,
                        candidates=findings,
                        decided_ids=decided_ids,
                        now_iso=started_at,
                    )

            # C1: ids to fetch and record first-seen for: classified candidates
            # PLUS the booking leg of every successfully paired grounding.
            _booking_ids = [
                booking.get("finding_id", "")
                for booking, reason in paired_bookings.values()
                if booking is not None and reason is None
            ]
            _cand_id_set = set(candidate_ids)
            _all_first_seen_ids = candidate_ids + [
                bid for bid in _booking_ids if bid and bid not in _cand_id_set
            ]

            first_seen = get_first_seen_times(_get_ddb(), ledger_table, fleet_id, _all_first_seen_ids)

            # observe/approve: record first sighting of every undecided in-scope
            # finding (candidates and pair bookings) — never in dry_run (D10c).
            if mode in (MODE_OBSERVE, MODE_APPROVE):
                for fid in _all_first_seen_ids:
                    if fid and fid not in first_seen:
                        record_first_seen(_get_ddb(), ledger_table, fleet_id, fid, started_at)

            cap_used = get_today_cap_used(_get_ddb(), ledger_table, fleet_id, today)
            plan = build_plan(
                classified=classified,
                paired_bookings=paired_bookings,
                first_seen=first_seen,
                run_at=started_at,
                remaining_cap=max(0, cfg.daily_cap - cap_used),
                decided_ids=decided_ids,  # S1
            )

            fleet_result = run_fleet(
                fleet_id=fleet_id,
                plan_units=plan.units,
                plan_skip_counts=plan.skip_counts,
                mode=mode,
                ddb_client=_get_ddb(),
                actions_table=actions_table,
                ledger_table=ledger_table,
                today=today,
                run_at=started_at,
                daily_cap=cfg.daily_cap,
            )

            class_counts: dict[str, int] = {}
            for u in plan.units:
                class_counts[u.class_name] = class_counts.get(u.class_name, 0) + 1
            fleet_errors = fleet_result.units_error + grounding_errors
            run_errors += fleet_errors
            fleet_results[fleet_id] = {
                "units_approved": fleet_result.units_approved,
                "units_would_approve": fleet_result.units_would_approve,
                "units_skipped": fleet_result.units_skipped,
                "units_person_first": fleet_result.units_person_first,
                "units_error": fleet_result.units_error,
                "grounding_read_errors": grounding_errors,
                "decisions_written": fleet_result.decisions_written,
                # W1: finer-grained approved-but-incomplete counters
                "grounding_not_made": fleet_result.grounding_not_made,
                "booking_person_first": fleet_result.booking_person_first,
                # W1: executor dispatch outcomes (both grounding and booking legs)
                "executor_executed": fleet_result.executor_executed,
                "executor_failed": fleet_result.executor_failed,
                "executor_declined": fleet_result.executor_declined,
                "candidates_by_class": _cands_by_cls,
                "candidate_ids_by_class": _cand_ids_by_cls,
                "planned_by_class": class_counts,
                "skip_counts": fleet_result.skip_counts,
                "cap_used_before": cap_used,
                "daily_cap": cfg.daily_cap,
            }
            _emit_emf(
                fleet_id=fleet_id, mode=mode,
                decisions=fleet_result.decisions_written,
                skips=fleet_result.units_skipped,
                errors=fleet_errors,
                run_completed=True,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("sim-operator: fleet %s failed (%s)", fleet_id, type(exc).__name__)
            run_errors += 1
            fleet_results[fleet_id] = {"error": type(exc).__name__}
            _emit_emf(fleet_id=fleet_id, mode=mode, decisions=0, skips=0, errors=1, run_completed=False)

    return _finish(started_at=started_at, mode=mode, fleet_results=fleet_results,
                   status="completed", errors=run_errors, response_status="ok")
