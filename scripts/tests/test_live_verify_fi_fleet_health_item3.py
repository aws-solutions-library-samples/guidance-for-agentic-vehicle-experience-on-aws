"""Tests for item 3 helper functions in live-verify-fi-fleet-health.py.

Fakes built from the real handler/executor code shapes (cited per source).
Each bad world fails on the item's own assertion; a positive control passes.

Source citations:
  score_fleet_service_lines: agents/executors/dms.py _fleet_service_body:183-196
  _check_labor_ops_in_map:   agents/fi_fleet_health/loop.py:1455-1467 (loop.py
                              need_lines builds description=task_code|code|fk_component)
                             pm_vehicle_status.sql:197-205 (dms_completions CTE)
  _load_labor_op_map_s3:     (column shape: code VARCHAR, labor_op VARCHAR)
  _pick_fleet_service_finding: agents/supervisor/tools/fleet_recommendation.py:191-205
                               (_derive_decision_action_id)

Run (AWS env stripped):
    env -u AWS_PROFILE AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing \\
      PYTHONDONTWRITEBYTECODE=1 \\
      .venv/bin/python -m pytest scripts/tests/test_live_verify_fi_fleet_health_item3.py -q
"""
from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest.mock as mock
from pathlib import Path
from typing import Any

import duckdb
import pytest

# ---------------------------------------------------------------------------
# Load module under test
# ---------------------------------------------------------------------------

_SCRIPT = Path(__file__).resolve().parents[1] / "live-verify-fi-fleet-health.py"
_spec = importlib.util.spec_from_file_location("live_verify_fi_fleet_health", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

Results = _mod.Results
score_fleet_service_lines = _mod.score_fleet_service_lines
_check_labor_ops_in_map = _mod._check_labor_ops_in_map
_load_labor_op_map_s3 = _mod._load_labor_op_map_s3
_pick_fleet_service_finding = _mod._pick_fleet_service_finding


# ---------------------------------------------------------------------------
# Parquet helper
# ---------------------------------------------------------------------------

def _make_labor_op_parquet(rows: list[dict]) -> bytes:
    """Serialise list of {code, labor_op} dicts to parquet bytes (duckdb)."""
    conn = duckdb.connect()
    conn.execute(
        "CREATE OR REPLACE TABLE _lom(code VARCHAR, labor_op VARCHAR)"
    )
    for r in rows:
        conn.execute("INSERT INTO _lom VALUES (?, ?)",
                     [r.get("code") or "", r.get("labor_op") or ""])
    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        conn.execute(f"COPY _lom TO '{tmp_path}' (FORMAT PARQUET)")
        with open(tmp_path, "rb") as fh:
            return fh.read()
    finally:
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Fixtures — need shapes from agents/fi_fleet_health/loop.py:1455-1467
# (need_lines dict: need_id, labor_op, description=task_code|code|fk_component, pay_type)
# ---------------------------------------------------------------------------

def _fault_need(code: str = "P0AA6", labor_op: str = "HV_ISOLATION_REPAIR") -> dict:
    """Fault need as stored in recommended_action.parameters.needs (loop.py:1455)."""
    return {
        "need_id": f"fault:{code}",
        "labor_op": labor_op,
        "description": code,          # loop.py:1464: need.get("code")
        "pay_type": "customer_pay",   # always (spec D9)
    }


def _pm_need(task_code: str = "TIRE_ROTATION", labor_op: str = "TIRE_ROTATION") -> dict:
    """PM-task need as stored in recommended_action.parameters.needs (loop.py:1456)."""
    return {
        "need_id": f"pm:{task_code}",
        "labor_op": labor_op,
        "description": task_code,     # loop.py:1463: need.get("task_code")
        "pay_type": "customer_pay",
    }


def _agent2_need(fk: str = "health.battery",
                 labor_op: str = "HV_BATTERY_DIAGNOSTIC") -> dict:
    """Agent2 need; description=component kind (loop.py:1465 fk.split('.')[-1])."""
    component = fk.split(".")[-1]
    return {
        "need_id": f"a2:{component}",
        "labor_op": labor_op,
        "description": component,     # not a fault/PM code; excluded from G6-D2
        "pay_type": "customer_pay",
    }


def _dms_ro_line(line_no: int = 1,
                 labor_op: str = "HV_ISOLATION_REPAIR",
                 pay_type: str = "customer_pay") -> dict:
    """DMS repair-order line as stored in DDB (dms.py _fleet_service_body:183-196)."""
    return {"line_no": line_no, "labor_op": labor_op, "pay_type": pay_type}


# ---------------------------------------------------------------------------
# score_fleet_service_lines
# ---------------------------------------------------------------------------

class TestScoreFleetServiceLines:
    """Verify the DMS RO lines == Finding needs in order."""

    def test_positive_control_single_need(self):
        """A single-line RO matching one need passes (dms.py:_fleet_service_body)."""
        needs = [_fault_need()]
        lines = [_dms_ro_line(1, "HV_ISOLATION_REPAIR")]
        ok, detail = score_fleet_service_lines(lines, needs)
        assert ok, detail

    def test_positive_control_two_needs_in_order(self):
        """Two lines matching two needs in order passes."""
        needs = [_fault_need("P0AA6", "HV_ISOLATION_REPAIR"),
                 _pm_need("TIRE_ROTATION", "TIRE_ROTATION")]
        lines = [_dms_ro_line(1, "HV_ISOLATION_REPAIR"),
                 _dms_ro_line(2, "TIRE_ROTATION")]
        ok, detail = score_fleet_service_lines(lines, needs)
        assert ok, detail

    def test_fail_line_out_of_order(self):
        """Lines swapped: line_no mismatch -> FAIL (mutation: swap lines passes count,
        but the line_no check catches it).
        Source: dms.py:_fleet_service_body:185 'line_no': idx+1.
        """
        needs = [_fault_need("P0AA6", "OP_A"), _pm_need("TIRE_ROTATION", "OP_B")]
        # Swap: first line has line_no=2 instead of 1
        lines = [_dms_ro_line(2, "OP_A"), _dms_ro_line(1, "OP_B")]
        ok, detail = score_fleet_service_lines(lines, needs)
        assert not ok, f"should fail on swapped line_no, got: {detail}"

    def test_fail_wrong_labor_op(self):
        """Wrong labor_op on line 1 -> FAIL.
        Source: dms.py:_fleet_service_body:186 'labor_op': labor_op.strip().
        """
        needs = [_fault_need("P0AA6", "HV_ISOLATION_REPAIR")]
        lines = [_dms_ro_line(1, "WRONG_OP")]
        ok, detail = score_fleet_service_lines(lines, needs)
        assert not ok, f"should fail on wrong labor_op, got: {detail}"

    def test_fail_wrong_pay_type(self):
        """Non-customer_pay pay_type -> FAIL (spec D9: always customer_pay).
        Source: dms.py:_fleet_service_body:187 'pay_type': 'customer_pay'.
        """
        needs = [_fault_need()]
        lines = [_dms_ro_line(1, "HV_ISOLATION_REPAIR", "recall")]
        ok, detail = score_fleet_service_lines(lines, needs)
        assert not ok, f"should fail on pay_type='recall', got: {detail}"

    def test_fail_count_mismatch(self):
        """Two needs but one line -> FAIL."""
        needs = [_fault_need(), _pm_need()]
        lines = [_dms_ro_line(1, "HV_ISOLATION_REPAIR")]
        ok, detail = score_fleet_service_lines(lines, needs)
        assert not ok, f"should fail on count mismatch, got: {detail}"

    def test_line_no_accepts_decimal_type(self):
        """DDB returns Decimal; int conversion must work (dms.py:189 W3 note)."""
        from decimal import Decimal
        needs = [_fault_need()]
        lines = [{"line_no": Decimal("1"), "labor_op": "HV_ISOLATION_REPAIR",
                  "pay_type": "customer_pay"}]
        ok, detail = score_fleet_service_lines(lines, needs)
        assert ok, detail


# ---------------------------------------------------------------------------
# _check_labor_ops_in_map (G6-D2)
# ---------------------------------------------------------------------------

_MAP_SET: set[tuple[str, str]] = {
    ("P0AA6", "HV_ISOLATION_REPAIR"),
    ("TIRE_ROTATION", "TIRE_ROTATION"),
    ("C0561", "ABS_REPAIR"),
}


class TestCheckLaborOpsInMap:
    """G6-D2: (description, labor_op) pair must be in code_labor_op_map."""

    def test_positive_fault_in_map(self):
        """Fault need with (code, labor_op) in map -> PASS."""
        needs = [_fault_need("P0AA6", "HV_ISOLATION_REPAIR")]
        ok, detail = _check_labor_ops_in_map(needs, _MAP_SET)
        assert ok, detail

    def test_positive_pm_in_map(self):
        """PM need with (task_code, labor_op) in map -> PASS."""
        needs = [_pm_need("TIRE_ROTATION", "TIRE_ROTATION")]
        ok, detail = _check_labor_ops_in_map(needs, _MAP_SET)
        assert ok, detail

    def test_agent2_finding_skipped(self):
        """Agent2_finding component kinds are skipped; with no fault/pm need checked==0 -> FAIL.
        Source: loop.py:1465 component=fk.split('.')[-1]; not a fault/PM code.
        Checked==0 means no real labor_op coverage was confirmed -> FAIL (C2S fix).
        """
        needs = [_agent2_need("health.battery", "HV_BATTERY_DIAGNOSTIC")]
        ok, detail = _check_labor_ops_in_map(needs, _MAP_SET)
        assert not ok, f"all-a2 needs should FAIL (checked==0); got: {detail}"

    def test_fail_labor_op_not_in_map_for_code(self):
        """(code, wrong_labor_op) not in map -> FAIL naming op and code (G6-D2).
        Mutant: removing membership check lets missing pairs pass.
        """
        needs = [_fault_need("P0AA6", "WRONG_OP")]
        ok, detail = _check_labor_ops_in_map(needs, _MAP_SET)
        assert not ok, f"should fail on missing pair, got: {detail}"
        assert "WRONG_OP" in detail
        assert "P0AA6" in detail

    def test_fail_pm_wrong_op(self):
        """PM need with wrong labor_op -> FAIL."""
        needs = [_pm_need("TIRE_ROTATION", "HV_ISOLATION_REPAIR")]
        ok, detail = _check_labor_ops_in_map(needs, _MAP_SET)
        assert not ok, f"should fail for TIRE_ROTATION/HV_ISOLATION_REPAIR: {detail}"

    def test_mixed_fault_and_agent2_only_fault_checked(self):
        """Mixed fault + agent2: only the fault's pair is checked."""
        needs = [
            _fault_need("P0AA6", "HV_ISOLATION_REPAIR"),
            _agent2_need("health.tires", "TIRE_INSPECTION"),
        ]
        ok, detail = _check_labor_ops_in_map(needs, _MAP_SET)
        assert ok, detail  # P0AA6/HV_ISOLATION_REPAIR is in map; agent2 skipped

    def test_empty_needs_fails(self):
        """Empty needs -> FAIL (C2: vacuous PASS replaced with FAIL)."""
        ok, _ = _check_labor_ops_in_map([], _MAP_SET)
        assert not ok


# ---------------------------------------------------------------------------
# _load_labor_op_map_s3
# ---------------------------------------------------------------------------

class TestLoadLaborOpMapS3:
    """Read code_labor_op_map parquet from fake S3 GetObject."""

    def _make_s3_mock(self, rows: list[dict]) -> Any:
        parquet_bytes = _make_labor_op_parquet(rows)
        body_mock = mock.MagicMock()
        body_mock.read.return_value = parquet_bytes
        s3 = mock.MagicMock()
        s3.get_object.return_value = {"Body": body_mock}
        return s3

    def test_positive_parses_rows(self):
        """Parquet with 2 rows yields a set of 2 (code, labor_op) tuples."""
        rows = [
            {"code": "P0AA6",        "labor_op": "HV_ISOLATION_REPAIR"},
            {"code": "TIRE_ROTATION", "labor_op": "TIRE_ROTATION"},
        ]
        s3 = self._make_s3_mock(rows)
        result = _load_labor_op_map_s3(s3, "test-bucket")
        assert ("P0AA6", "HV_ISOLATION_REPAIR") in result
        assert ("TIRE_ROTATION", "TIRE_ROTATION") in result
        assert len(result) == 2

    def test_s3_op_key(self):
        """GetObject called with the correct key path."""
        s3 = self._make_s3_mock([{"code": "X", "labor_op": "Y"}])
        _load_labor_op_map_s3(s3, "my-bucket")
        s3.get_object.assert_called_once()
        call_kw = s3.get_object.call_args
        assert call_kw.kwargs["Bucket"] == "my-bucket"
        assert "code_labor_op_map" in call_kw.kwargs["Key"]

    def test_s3_error_raises(self):
        """S3 error propagates (caller records FAIL)."""
        s3 = mock.MagicMock()
        s3.get_object.side_effect = RuntimeError("NoSuchKey")
        with pytest.raises(RuntimeError, match="NoSuchKey"):
            _load_labor_op_map_s3(s3, "bucket")


# ---------------------------------------------------------------------------
# _pick_fleet_service_finding
# ---------------------------------------------------------------------------

def _make_ddb_low_mock(
    findings: list[dict],
    action_exists_for: set[str] | None = None,
) -> Any:
    """Fake low-level DDB client for _pick_fleet_service_finding.

    Findings are returned from Query (by_fleet GSI).
    GetItem for actions table returns Item if fid is in action_exists_for.

    Sources:
      - by_fleet Query: agents/fi_fleet_health/selection.py:select_agent2_findings:347
      - _derive_decision_action_id: fleet_recommendation.py:191-205
    """
    from boto3.dynamodb.types import TypeSerializer  # noqa: PLC0415

    ser = TypeSerializer()
    action_exists_for = action_exists_for or set()

    def _ser_item(d: dict) -> dict:
        return {k: ser.serialize(v) for k, v in d.items()}

    def query(**kw: Any) -> dict:
        return {"Items": [_ser_item(f) for f in findings], "Count": len(findings)}

    def get_item(**kw: Any) -> dict:
        # Determine if this is an actions-table GetItem
        action_id = (kw.get("Key") or {}).get("action_id", {}).get("S", "")
        # Extract the bare UUID to match against finding_id prefixes
        import hashlib
        for fid in action_exists_for:
            # _derive_decision_action_id: sha256("{fid}|fi-decision")[:32] as UUID
            h = hashlib.sha256(f"{fid}|fi-decision".encode()).hexdigest()[:32]
            derived = f"AVX-ACT-{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
            if action_id == derived:
                return {"Item": {"action_id": {"S": action_id}}}
        return {}

    ddb = mock.MagicMock()
    ddb.query.side_effect = query
    ddb.get_item.side_effect = get_item
    return ddb


def _book_finding(fid: str = "FIND-001",
                  vin: str = "VIN0000000000TEST1",
                  fleet_id: str = "demo-rental-carshare") -> dict:
    """Minimal fleet.health.service Finding with book_fleet_service action.

    Shape from agents/fi_fleet_health/loop.py:1478-1492 (_write_slot booking item).
    """
    return {
        "finding_id": fid,
        "agent_id": "agent-fi-fleet-health",
        "finding_kind": "fleet.health.service",
        "scope": {"vin": vin},
        "fleet_id": fleet_id,
        "severity": "attention",
        "attempt": 1,
        "computed_at": "2026-10-17T02:05:00Z",
        "expires_at": "2099-01-01T00:00:00Z",
        "recommended_action": {
            "action_kind": "book_fleet_service",
            "target_system": "dms",
            "parameters": {
                "vehicle_vin": vin,
                "needs": [_fault_need("C0561", "ABS_REPAIR")],
                "dealer_id": "DLR-001",
            },
        },
    }


class TestPickFleetServiceFinding:
    """_pick_fleet_service_finding returns lowest finding_id eligible Finding."""

    _TABLE = "vsa-staging-avx-findings"
    _ACTIONS = "vsa-staging-avx-actions"
    _FLEET = "demo-rental-carshare"
    _NOW = "2026-10-17T02:10:00Z"

    def test_positive_returns_lowest_finding_id(self):
        """Two eligible findings -> returns the one with lexicographically lower id."""
        f1 = _book_finding("FIND-AAA")
        f2 = _book_finding("FIND-BBB")
        ddb = _make_ddb_low_mock([f2, f1])
        result = _pick_fleet_service_finding(
            ddb, self._TABLE, self._ACTIONS, self._FLEET, self._NOW,
        )
        assert result is not None
        assert result["finding_id"] == "FIND-AAA"

    def test_actioned_finding_excluded(self):
        """A Finding whose decision Action exists is excluded (no duplicate approve).
        Mutant: removing action GetItem check returns an already-actioned Finding.
        Source: fleet_recommendation.py:191-205 _derive_decision_action_id.
        """
        f = _book_finding("FIND-001")
        ddb = _make_ddb_low_mock([f], action_exists_for={"FIND-001"})
        result = _pick_fleet_service_finding(
            ddb, self._TABLE, self._ACTIONS, self._FLEET, self._NOW,
        )
        assert result is None, "actioned finding should be excluded"

    def test_expired_finding_excluded(self):
        """Expired finding (expires_at <= now) is excluded."""
        f = _book_finding("FIND-EXP")
        f["expires_at"] = "2020-01-01T00:00:00Z"  # in the past
        ddb = _make_ddb_low_mock([f])
        result = _pick_fleet_service_finding(
            ddb, self._TABLE, self._ACTIONS, self._FLEET, self._NOW,
        )
        assert result is None, "expired finding should be excluded"

    def test_wrong_action_kind_excluded(self):
        """Finding with action_kind != book_fleet_service is excluded."""
        f = _book_finding("FIND-WRONG")
        f["recommended_action"]["action_kind"] = "submit_warranty_claim"
        ddb = _make_ddb_low_mock([f])
        result = _pick_fleet_service_finding(
            ddb, self._TABLE, self._ACTIONS, self._FLEET, self._NOW,
        )
        assert result is None

    def test_no_findings_returns_none(self):
        """Empty DDB result -> None."""
        ddb = _make_ddb_low_mock([])
        result = _pick_fleet_service_finding(
            ddb, self._TABLE, self._ACTIONS, self._FLEET, self._NOW,
        )
        assert result is None



# ===========================================================================
# Guard: item 3 Lambda InvokeFunction passes the guard
# ===========================================================================

def test_guard_item3_lambda_invoke_passes() -> None:
    """Guard passes item 3's Lambda InvokeFunction; before-send halts network."""
    from typing import Any  # noqa: PLC0415

    class _NoNet(Exception):
        pass

    def _halt(**_kw: Any) -> None:
        raise _NoNet

    lam = _mod._client(
        "lambda", "us-west-2",
        allowed_ops=frozenset({"lambda.Invoke"}),
    )
    lam.meta.events.register("before-send", _halt)

    raised: Exception | None = None
    try:
        lam.invoke(FunctionName="x", InvocationType="RequestResponse", Payload=b"{}")
    except Exception as exc:
        raised = exc
    assert type(raised) is _NoNet, (
        f"expected _NoNet from before-send but got {type(raised).__name__}: {raised}"
    )



# ===========================================================================
# Lambda client config: read_timeout / connect_timeout / total_max_attempts
# ===========================================================================

def test_lambda_invoke_client_has_invoke_config() -> None:
    """lam3/lam6/lam7 Lambda clients must have read_timeout=910, connect_timeout=10, total_max_attempts=1."""
    from botocore.config import Config  # noqa: PLC0415

    _INVOKE_CFG = _mod._lambda_invoke_config()
    try:
        lam = _mod._client(
            "lambda", "us-west-2",
            allowed_ops=frozenset({"lambda.Invoke"}),
            config=_INVOKE_CFG,
        )
    except TypeError:
        # _client doesn't accept config yet — fall back so assertion fires below
        lam = _mod._client("lambda", "us-west-2", allowed_ops=frozenset({"lambda.Invoke"}))

    cfg = lam.meta.config
    assert cfg is not None, "meta.config is None"
    assert cfg.read_timeout == 910, f"read_timeout={cfg.read_timeout}"
    assert cfg.connect_timeout == 10, f"connect_timeout={cfg.connect_timeout}"
    assert cfg.retries.get("total_max_attempts") == 1, f"retries={cfg.retries}"


# ===========================================================================
# _attempt_from_subject_key (C1 fix)
# ===========================================================================

class TestAttemptFromSubjectKey:
    """_attempt_from_subject_key parses attempt from subject_key (loop.py:1474).
    build_finding copies subject_key but not attempt (write.py:93-114).
    """

    def test_service_1_returns_1(self):
        assert _mod._attempt_from_subject_key("service/1") == 1

    def test_service_3_returns_3(self):
        assert _mod._attempt_from_subject_key("service/3") == 3

    def test_missing_field_returns_1(self):
        """Finding with no subject_key -> attempt 1 (first-time booking)."""
        assert _mod._attempt_from_subject_key(None) == 1

    def test_non_service_key_returns_1(self):
        """ground/2 or other patterns -> 1 (not a service re-raise)."""
        assert _mod._attempt_from_subject_key("ground/2") == 1

    def test_re_raise_check_uses_subject_key(self):
        """C1: a Finding with subject_key='service/2' is attempt 2, not 1.
        build_finding output shape from write.py:93-114.
        """
        finding = {
            "finding_id": "fid-002",
            "subject_key": "service/2",      # write.py:114 copies item['subject_key']
            "scope": {"vin": "VIN001"},
            "recommended_action": {
                "action_kind": "book_fleet_service",
                "parameters": {"dealer_id": "D1"},
            },
        }
        assert _mod._attempt_from_subject_key(
            finding.get("subject_key")
        ) == 2


# ===========================================================================
# _check_labor_ops_in_map — C2 fix: key on need_id, not map presence
# ===========================================================================

class TestCheckLaborOpsInMapC2:
    """G6-D2: needs with fault: or pm: need_id must have (code, labor_op) in map.
    A fault code absent from the map is a FAIL, not a skip.
    need_id prefix is the discriminator (loop.py:1455-1465).
    """

    def test_fault_need_no_labor_op_fails(self):
        """fault:P0AA6 with no labor_op field → FAIL (not silently skipped).

        Catches c5-g6d2-skip-unmapped mutant: the mutant's `code in {map_codes}`
        check returns False for an empty map, so no missing entry is appended and
        the function wrongly returns PASS.
        With the fix, code comes from need_id suffix (P0AA6) and the empty
        labor_op causes a missing entry immediately → FAIL.
        """
        needs = [{"need_id": "fault:P0AA6"}]  # no labor_op field
        ok, detail = _check_labor_ops_in_map(needs, set())
        assert not ok, f"should FAIL for fault need with no labor_op, got: {detail}"

    def test_pm_need_no_labor_op_fails(self):
        """pm:TIRE_ROTATION with no labor_op → FAIL."""
        needs = [{"need_id": "pm:TIRE_ROTATION"}]
        ok, detail = _check_labor_ops_in_map(needs, set())
        assert not ok, f"should FAIL for pm need with no labor_op, got: {detail}"

    def test_fault_need_code_absent_from_map_fails(self):
        """fault:EV_DRIVETRAIN_DIAGNOSTIC not in map -> FAIL (C2: used to pass via skip)."""
        needs = [{"need_id": "fault:EV_DRIVETRAIN_DIAGNOSTIC",
                  "labor_op": "EV_DIAG", "description": "EV_DRIVETRAIN_DIAGNOSTIC"}]
        ok, detail = _check_labor_ops_in_map(needs, set())
        assert not ok, f"should FAIL when fault code absent from map, got: {detail}"

    def test_pm_need_code_absent_from_map_fails(self):
        """pm:EV_SYSTEM_CHECK not in map -> FAIL."""
        needs = [{"need_id": "pm:EV_SYSTEM_CHECK",
                  "labor_op": "EV_CHK", "description": "EV_SYSTEM_CHECK"}]
        ok, detail = _check_labor_ops_in_map(needs, set())
        assert not ok, f"should FAIL when pm code absent from map, got: {detail}"

    def test_a2_need_excluded_even_when_absent(self):
        """a2:battery has no fault/PM prefix -> excluded; checked==0 -> FAIL (C2S fix)."""
        needs = [{"need_id": "a2:battery",
                  "labor_op": "HV_BATTERY_DIAGNOSTIC", "description": "battery"}]
        ok, detail = _check_labor_ops_in_map(needs, set())
        assert not ok, f"all-a2 needs should FAIL (checked==0); got: {detail}"

    def test_empty_needs_fails(self):
        """Empty needs list -> FAIL (not vacuously PASS as before)."""
        ok, detail = _check_labor_ops_in_map([], set())
        assert not ok, f"empty needs should FAIL, got: {detail}"

    def test_positive_fault_in_map_passes(self):
        """fault:P0AA6 with matching (P0AA6, HV_ISOLATION_REPAIR) in map -> PASS."""
        needs = [_fault_need("P0AA6", "HV_ISOLATION_REPAIR")]
        ok, detail = _check_labor_ops_in_map(
            needs, {("P0AA6", "HV_ISOLATION_REPAIR")}
        )
        assert ok, detail


# ===========================================================================
# _check_invoke_result (W2 fix)
# ===========================================================================

class TestCheckInvokeResult:
    """_check_invoke_result fails on FunctionError or non-200 StatusCode (W2).
    Items 6 and 7 both check FunctionError; 3d-invoke used to pass unconditionally.
    """

    def test_function_error_fails(self):
        """FunctionError present -> FAIL (like items 6 and 7 do)."""
        resp = {"StatusCode": 200, "FunctionError": "Unhandled",
                "Payload": b'{"errorMessage":"oom"}'}
        ok, detail = _mod._check_invoke_result(resp)
        assert not ok, f"FunctionError should FAIL, got: {detail}"

    def test_non_200_fails(self):
        """StatusCode 400 -> FAIL."""
        resp = {"StatusCode": 400, "Payload": b""}
        ok, detail = _mod._check_invoke_result(resp)
        assert not ok, f"non-200 should FAIL, got: {detail}"

    def test_200_no_error_passes(self):
        """StatusCode 200, no FunctionError -> PASS."""
        resp = {"StatusCode": 200, "Payload": b"{}"}
        ok, detail = _mod._check_invoke_result(resp)
        assert ok, detail


# ===========================================================================
# _check_item3_ro_residue and _item_3 end-to-end (W1)
# ===========================================================================

import re
import unittest.mock as mock
from contextlib import contextmanager
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers to build fake DDB low-level responses (TypeSerializer shapes)
# ---------------------------------------------------------------------------

def _ddb_item_ros(ro_id: str, status: str) -> dict:
    """DMS RO row as returned by DynamoDB GetItem (low-level TypeDeserializer shape)."""
    # Matches dms.py _store_ro — keys: ro_id, status, lines, etc.
    return {
        "ro_id": {"S": ro_id},
        "status": {"S": status},
        "lines": {"L": [
            {"M": {"line_no": {"N": "1"}, "labor_op": {"S": "HV_ISOLATION_REPAIR"},
                   "pay_type": {"S": "customer_pay"}}}
        ]},
    }


def _ddb_item_action(action_id: str, status: str, ro_id: str) -> dict:
    """AVX Actions row as returned by DynamoDB GetItem (low-level)."""
    # Matches executor dms.py _update_action_status
    return {
        "action_id": {"S": action_id},
        "status": {"S": status},
        "target_ref": {"S": ro_id},
    }


class TestCheckItem3RoResidue:
    """_check_item3_ro_residue: exactly planned ROs in DMS. (W1)"""

    def _fake_ddb(self, item_map: dict, scan_items: list | None = None) -> Any:
        """Fake DDB low-level client that returns item_map[key] for GetItem."""
        class _FakeDDB:
            def get_item(self_inner, *, TableName, Key, **_kw):
                key_val = list(Key.values())[0]
                key_str = list(key_val.values())[0]
                row = item_map.get(key_str)
                if row is None:
                    return {}
                return {"Item": row}

            def scan(self_inner, **_kw):
                _items = scan_items if scan_items is not None else []
                return {"Items": _items, "Count": len(_items)}
        return _FakeDDB()

    def test_both_ros_present_passes(self):
        """action_a and action_b ROs found in DMS table -> PASS (W1 positive control)."""
        item_map = {
            "RO-A": _ddb_item_ros("RO-A", "Ready"),
            "RO-B": _ddb_item_ros("RO-B", "Draft"),
        }
        scan_items = [
            {"ro_id": {"S": "RO-A"}, "avx_action_id": {"S": "ACT-A"}},
            {"ro_id": {"S": "RO-B"}, "avx_action_id": {"S": "ACT-B"}},
        ]
        res = Results()
        _mod._check_item3_ro_residue(
            self._fake_ddb(item_map, scan_items=scan_items),
            "dms-staging-repair-orders",
            [{"action_id": "ACT-A", "ro_id": "RO-A"},
             {"action_id": "ACT-B", "ro_id": "RO-B"}],
            res,
        )
        assert not res.failed, f"should pass with both ROs present: {res.failed}"

    def test_missing_ro_fails(self):
        """ro_id not found in DMS -> FAIL (W1 bad world: residue missing)."""
        res = Results()
        _mod._check_item3_ro_residue(
            self._fake_ddb({}, scan_items=[]),
            "dms-staging-repair-orders",
            [{"action_id": "ACT-A", "ro_id": "RO-MISSING"}],
            res,
        )
        assert res.failed, f"should fail when RO missing: {res.rows}"


# ---------------------------------------------------------------------------
# _item_3 end-to-end with fakes shaped like build_finding output (W1 + C1)
# ---------------------------------------------------------------------------

_STAGE3 = "staging"
_REGION3 = "us-west-2"
_ACCOUNT3 = "123456789012"
_FLEET3 = "demo-rental-carshare"   # _MANAGED_FH_FLEETS[0] (live-verify:1142)
_FLEET3B = "demo-lastmile-delivery"  # _MANAGED_FH_FLEETS[1]
_VIN3A = "VIN3A00000000001"
_VIN3B = "VIN3B00000000002"
_RUN3 = "run-test-item3-e2e"

# build_finding output shape (write.py:93-114)
_FINDING_A = {
    "finding_id": "FIND-item3-A",
    "finding_kind": "fleet.health.service",
    "agent_id": "agent-fi-fleet-health",
    "scope": {"vin": _VIN3A},
    "fleet_id": _FLEET3,
    "subject_key": "service/1",  # write.py:114 — attempt embedded here, NOT a top-level field
    "severity": "attention",
    "computed_at": "2026-10-17T05:44:00Z",
    "expires_at": "2026-10-30T05:44:00Z",
    "confidence": "low",
    "evidence": [],
    "inputs_hash": "a" * 64,
    "tokens_used": 0,
    "recommended_action": {
        "action_kind": "book_fleet_service",
        "target_system": "dms",
        "parameters": {
            "dealer_id": "DEALER-1",
            "needs": [_fault_need("P0AA6", "HV_ISOLATION_REPAIR")],
        },
    },
}

# Finding B with subject_key service/2 (attempt 2 — distinguishes from attempt=1 default).
# c3-attempt-attr mutant: `int(finding_b.get("attempt") or 1)` returns 1 (no "attempt"
# field), but _attempt_from_subject_key("service/2") returns 2 → different expected
# subject_key on the re-raised Finding → re-raise check catches the mutant.
_FINDING_B = {
    "finding_id": "FIND-item3-B",
    "finding_kind": "fleet.health.service",
    "agent_id": "agent-fi-fleet-health",
    "scope": {"vin": _VIN3B},
    "fleet_id": _FLEET3B,
    "subject_key": "service/2",  # write.py:114 — attempt 2; no top-level "attempt" field
    "severity": "attention",
    "computed_at": "2026-10-17T05:44:00Z",
    "expires_at": "2026-10-30T05:44:00Z",
    "confidence": "low",
    "evidence": [],
    "inputs_hash": "b" * 64,
    "tokens_used": 0,
    "recommended_action": {
        "action_kind": "book_fleet_service",
        "target_system": "dms",
        "parameters": {
            "dealer_id": "DEALER-2",
            "needs": [_fault_need("P0AA6", "HV_ISOLATION_REPAIR")],
        },
    },
}

# Re-raised Finding B: subject_key service/2 (attempt 2, from next service run)
_FINDING_B_RERAISED = {
    "finding_id": "FIND-item3-B2",
    "finding_kind": "fleet.health.service",
    "agent_id": "agent-fi-fleet-health",
    "scope": {"vin": _VIN3B},
    "fleet_id": _FLEET3B,
    "subject_key": "service/3",   # write.py:114 — attempt 3 (Finding B was attempt 2)
    "severity": "attention",
    "computed_at": "2026-10-17T06:10:00Z",
    "expires_at": "2026-10-30T06:10:00Z",
    "confidence": "low",
    "evidence": [],
    "inputs_hash": "c" * 64,
    "tokens_used": 0,
    "recommended_action": {
        "action_kind": "book_fleet_service",
        "target_system": "dms",
        "parameters": {
            "dealer_id": "DEALER-2",
            "needs": [_fault_need("P0AA6", "HV_ISOLATION_REPAIR")],
        },
    },
}


def _build_item3_world():
    """Build a complete fake world for _item_3 happy path.

    DDB, Cognito, SSM, CFN, Lambda, S3, HTTP all mocked.
    Findings shaped like build_finding output (write.py:93-114).
    Actions and ROs shaped like executor dms.py.
    """
    labor_op_parquet = _make_labor_op_parquet(
        [{"code": "P0AA6", "labor_op": "HV_ISOLATION_REPAIR"}]
    )
    action_id_a = "ACT-A-test"
    ro_id_a = "RO-A-test"
    action_id_b = "ACT-B-test"
    ro_id_b = "RO-B-test"

    # Track call counts for invoke and poll state
    state = {
        "action_a_polls": 0,
        "action_b_polls": 0,
        "invoke_called": False,
        "reraise_polls": 0,
    }

    from boto3.dynamodb.types import TypeSerializer as _TS  # noqa: PLC0415
    ser = _TS()

    def _serialize(obj: dict) -> dict:
        return {k: ser.serialize(v) for k, v in obj.items()}

    class FakeDDB:
        def query(self, **kw):
            table = kw.get("TableName", "")
            if "findings" in table:
                # Return findings based on fleet queried (by_fleet GSI uses :flt)
                expr = kw.get("ExpressionAttributeValues", {})
                fleet_val = (expr.get(":flt") or expr.get(":fid") or {}).get("S", "")
                if fleet_val == _FLEET3:
                    # FLEET3 has finding A; after invoke it's gone (action exists)
                    return {"Items": [_serialize(_FINDING_A)], "Count": 1}
                if fleet_val == _FLEET3B:
                    if state["invoke_called"] and state["reraise_polls"] < 2:
                        state["reraise_polls"] += 1
                        return {"Items": [_serialize(_FINDING_B_RERAISED)], "Count": 1}
                    elif not state["invoke_called"]:
                        # Before invoke, FLEET3B has finding B (for pick)
                        return {"Items": [_serialize(_FINDING_B)], "Count": 1}
                    return {"Items": [_serialize(_FINDING_B_RERAISED)], "Count": 1}
                return {"Items": [], "Count": 0}
            if "actions" in table:
                return {"Items": [], "Count": 0}
            return {"Items": [], "Count": 0}

        def get_item(self, **kw):
            table = kw.get("TableName", "")
            key = kw.get("Key", {})
            if "actions" in table:
                aid = (key.get("action_id") or {}).get("S", "")
                if aid == action_id_a:
                    state["action_a_polls"] += 1
                    # First poll: in_progress with ro_id;  second+: completed
                    status = "completed" if state["action_a_polls"] > 1 else "in_progress"
                    return {"Item": _serialize({
                        "action_id": action_id_a, "status": status,
                        "target_ref": ro_id_a,
                    })}
                if aid == action_id_b:
                    state["action_b_polls"] += 1
                    # Immediately cancelled
                    return {"Item": _serialize({
                        "action_id": action_id_b, "status": "cancelled",
                        "target_ref": ro_id_b,
                    })}
                return {}
            if "repair-orders" in table:
                rid = (key.get("ro_id") or {}).get("S", "")
                if rid == ro_id_a:
                    return {"Item": _serialize({
                        "ro_id": ro_id_a, "status": "Ready", "dealer_id": "DEALER-1",
                        "lines": [{"line_no": 1, "labor_op": "HV_ISOLATION_REPAIR",
                                   "pay_type": "customer_pay"}],
                    })}
                if rid == ro_id_b:
                    return {"Item": _serialize({
                        "ro_id": ro_id_b, "status": "Draft", "dealer_id": "DEALER-2",
                        "lines": [],
                    })}
                return {}
            return {}

        def scan(self, **kw):
            """Paged DDB Scan for avx_action_id filter in repair-orders (residue check)."""
            table = kw.get("TableName", "")
            if "repair-orders" in table:
                # Return both planned ROs with avx_action_id matching the stripped action IDs
                # action_id_a and action_id_b are bare UUIDs (no AVX-ACT- prefix in tests)
                items = [
                    _serialize({"ro_id": ro_id_a, "avx_action_id": action_id_a}),
                    _serialize({"ro_id": ro_id_b, "avx_action_id": action_id_b}),
                ]
                return {"Items": items, "Count": len(items)}
            return {"Items": [], "Count": 0}

    class FakeCognito:
        def create_user(self, **_kw): return {}
        def set_password(self, **_kw): return {}
        def add_to_group(self, **_kw): return {}
        def delete_user(self, **_kw): return {}
        def initiate_auth(self, **_kw):
            return {"AuthenticationResult": {"IdToken": "tok-test"}}
        def list_user_pools(self, **_kw):
            return {"UserPools": [{"Id": "pool-test", "Name": f"cvx-{_STAGE3}-voice"}]}
        def list_user_pool_clients(self, **_kw):
            return {"UserPoolClients": [{"ClientId": "client-test",
                                         "ClientName": "password-auth"}]}
        def describe_user_pool_client(self, **_kw):
            return {"UserPoolClient": {"ExplicitAuthFlows":
                                       ["ALLOW_USER_PASSWORD_AUTH"]}}
        # Support all methods via __getattr__
        def __getattr__(self, name):
            def _noop(**_kw): return {}
            return _noop

    class FakeSSM:
        def get_parameter(self, **kw):
            return {"Parameter": {"Value": "pool-test"}}

    class FakeCFN:
        def describe_stacks(self, **kw):
            name = kw.get("StackName", "")
            if "api" in name.lower() and "dms" not in name.lower():
                return {"Stacks": [{"Outputs": [
                    {"OutputKey": "RestApiUrl", "OutputValue": "https://avx.test/"}
                ]}]}
            return {"Stacks": [{"Outputs": [
                {"OutputKey": "ApiUrl", "OutputValue": "https://dms.test/"}
            ]}]}

    class FakeLambda:
        def invoke(self, **kw):
            state["invoke_called"] = True
            return {"StatusCode": 200, "Payload": b"{}"}

    class FakeS3:
        def get_object(self, **kw):
            import io  # noqa: PLC0415
            return {"Body": io.BytesIO(labor_op_parquet)}

    return {
        "ddb": FakeDDB(),
        "cognito": FakeCognito(),
        "ssm": FakeSSM(),
        "cfn": FakeCFN(),
        "lam": FakeLambda(),
        "s3": FakeS3(),
        "action_id_a": action_id_a,
        "ro_id_a": ro_id_a,
        "action_id_b": action_id_b,
        "ro_id_b": ro_id_b,
        "state": state,
    }


class _FakeHttp:
    """Minimal HTTP fake for _item_3: approve returns action_id, others return 200."""
    def __init__(self, action_a: str, action_b: str):
        self._a = action_a
        self._b = action_b
        self._call_no = 0

    def call(self, method: str, path: str, auth: str, body=None):
        self._call_no += 1
        if method == "POST" and "/approve" in path:
            if self._call_no <= 1:
                return 200, {"action_id": self._a}
            return 200, {"action_id": self._b}
        if method == "PATCH":
            return 200, {}
        return 200, {}


def _run_item3_with_world(world: dict) -> Results:
    """Call _item_3 with mocked AWS clients and HTTP from world."""
    import tempfile  # noqa: PLC0415
    import json as _json  # noqa: PLC0415

    ddb = world["ddb"]
    cognito = world["cognito"]
    ssm = world["ssm"]
    cfn = world["cfn"]
    lam = world["lam"]
    s3 = world["s3"]

    http_avx = _FakeHttp(world["action_id_a"], world["action_id_b"])
    http_dms = _FakeHttp("", "")

    def _fake_client(service, region, *, allowed_ops, **_kw):
        if service == "dynamodb":   return ddb
        if service == "cognito-idp": return cognito
        if service == "ssm":        return ssm
        if service == "cloudformation": return cfn
        if service == "lambda":     return lam
        if service == "s3":         return s3
        raise ValueError(f"unexpected service {service!r}")

    # Mock principal context manager to return a fake token + user
    @contextmanager
    def _fake_principal(*_a, **_kw):
        yield "tok-test", "user-test"

    res = Results()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as tf:
        _json.dump({"run_id": _RUN3, "items": {}}, tf)
        manifest_path = Path(tf.name)

    with (mock.patch.object(_mod, "_client", side_effect=_fake_client),
          mock.patch.object(_mod, "principal", side_effect=_fake_principal),
          mock.patch("time.sleep"),  # suppress all polling sleeps
          mock.patch.object(_mod, "_in_write_window", return_value=False),
          mock.patch.object(_mod, "Http", side_effect=lambda base: (
              http_avx if "avx" in base or "9000" in base else http_dms
          ))):
        _mod._item_3(
            res,
            run=_RUN3,
            stage=_STAGE3,
            region=_REGION3,
            account=_ACCOUNT3,
            manifest=manifest_path,
        )
    return res


def test_item3_e2e_happy_path_passes():
    """_item_3 e2e: approve A (complete path) + approve/cancel B (cancel path) +
    invoke service -> reraised Finding with subject_key=service/3 passes.

    Finding B has subject_key=service/2 (attempt 2); re-raised must be attempt>=3.
    Fakes shaped like build_finding (write.py:93-114):
      - subject_key encodes attempt ('service/N'), no top-level 'attempt' field.
    DMS RO/Action shapes from executor dms.py.
    """
    world = _build_item3_world()
    res = _run_item3_with_world(world)
    # Key checks: re-raise detection and invoke check pass
    statuses = {r[0]: r[1] for r in res.rows}
    assert statuses.get("3d-invoke") == "PASS", f"3d-invoke: {statuses.get('3d-invoke')} — {res.rows}"
    assert statuses.get("3d-reraised") == "PASS", \
        f"3d-reraised failed: {statuses.get('3d-reraised')} — {res.rows}"



# ---------------------------------------------------------------------------
# Negative: re-raised at same attempt as Finding B → 3d-reraised FAIL
# (catches mutant c3-attempt-attr)
# ---------------------------------------------------------------------------

# A re-raised Finding at attempt 2 (same as Finding B). Real code sets
# next_attempt = _attempt_from_subject_key("service/2")+1 = 3; a finding at
# attempt 2 does NOT satisfy 2>=3 → FAIL.
# Mutant: fb_attempt=int(None or 1)=1, next_attempt=2; 2>=2 → wrongly PASS.
_FINDING_B_SAME_ATTEMPT = {**_FINDING_B_RERAISED, "subject_key": "service/2",
                            "finding_id": "FIND-item3-B2x"}


def test_item3_reraised_same_attempt_fails() -> None:
    """Re-raised Finding at same attempt as Finding B → 3d-reraised FAIL.

    Finding B subject_key=service/2 (attempt 2); next_attempt=3.
    Re-raised at service/2 (attempt 2) does not satisfy 2>=3 → 3d-reraised FAIL.
    Catches c3-attempt-attr mutant: int(finding_b.get("attempt") or 1)=1 gives
    next_attempt=2, and 2>=2 would wrongly pass.
    """
    import time as _time_mod  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    import json as _json  # noqa: PLC0415

    from boto3.dynamodb.types import TypeSerializer as _TS2  # noqa: PLC0415
    _ser2 = _TS2()

    def _ser(obj):
        return {k: _ser2.serialize(v) for k, v in obj.items()}

    world = _build_item3_world()
    orig_ddb = world["ddb"]

    class _PatchedDDB:
        """Wraps FakeDDB but swaps service/3 re-raised items to service/2."""
        def query(self, **kw):
            resp = orig_ddb.query(**kw)
            new_items = []
            for item in resp.get("Items", []):
                sk = (item.get("subject_key") or {}).get("S", "")
                if sk == "service/3":
                    new_items.append(_ser(_FINDING_B_SAME_ATTEMPT))
                else:
                    new_items.append(item)
            return {**resp, "Items": new_items, "Count": len(new_items)}

        def get_item(self, **kw):
            return orig_ddb.get_item(**kw)

    world["ddb"] = _PatchedDDB()

    # Make the 300-second re-raise poll expire immediately.
    # The poll loop does `deadline = _t.time() + 300; while _t.time() < deadline`.
    # Return a real starting time for the first 100 calls (to let earlier loops
    # succeed in 1-2 iterations each), then a value past any 300-second deadline.
    _t0 = _time_mod.time()
    _time_calls = [0]
    def _quick_time():
        _time_calls[0] += 1
        return _t0 if _time_calls[0] <= 100 else _t0 + 400  # expire after 100 calls

    ddb = world["ddb"]
    cognito = world["cognito"]
    ssm = world["ssm"]
    cfn = world["cfn"]
    lam = world["lam"]
    s3 = world["s3"]

    @contextmanager
    def _fake_principal(*_a, **_kw):
        yield "tok-test", "user-test"

    res = Results()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as tf:
        _json.dump({"run_id": _RUN3, "items": {}}, tf)
        manifest_path = Path(tf.name)

    with (mock.patch.object(_mod, "_client",
                            side_effect=lambda svc, *a, **kw: {
                                "dynamodb": ddb, "cognito-idp": cognito,
                                "ssm": ssm, "cloudformation": cfn,
                                "lambda": lam, "s3": s3,
                            }.get(svc, mock.MagicMock())),
          mock.patch.object(_mod, "principal", side_effect=_fake_principal),
          mock.patch("time.sleep"),
          mock.patch("time.time", side_effect=_quick_time),
          mock.patch.object(_mod, "_in_write_window", return_value=False),
          mock.patch.object(_mod, "Http", side_effect=lambda base: (
              _FakeHttp(world["action_id_a"], world["action_id_b"])
              if "avx" in base or "9000" in base else _FakeHttp("", "")
          ))):
        _mod._item_3(
            res,
            run=_RUN3,
            stage=_STAGE3,
            region=_REGION3,
            account=_ACCOUNT3,
            manifest=manifest_path,
        )

    statuses = {r[0]: r[1] for r in res.rows}
    assert statuses.get("3d-reraised") == "FAIL", (
        f"expected 3d-reraised FAIL when re-raised attempt==Finding-B attempt; rows={res.rows}"
    )



# ---------------------------------------------------------------------------
# _check_item3_ro_residue: paged scan catches extra ROs (item 3 RO residue)
# ---------------------------------------------------------------------------

def test_item3_ro_residue_scan_extra_ro_fails() -> None:
    """Paged scan for avx_action_ids finds an extra RO → 3-residue-scan FAIL.

    GetItem pass only checks planned ro_ids. An extra RO (unexpected duplicate
    or wrong ID) is invisible to GetItem but caught by the paged scan.
    Source: _check_item3_ro_residue pass 2 (paged Scan with FilterExpression
    on avx_action_id); DMS stores bare UUIDs, AVX-ACT- prefix stripped.
    """
    from boto3.dynamodb.types import TypeSerializer as _TS3  # noqa: PLC0415
    _ser3 = _TS3()

    def _s(obj):
        return {k: _ser3.serialize(v) for k, v in obj.items()}

    planned = [
        {"action_id": "ACT-X-test", "ro_id": "RO-X-test"},
    ]
    # GetItem returns the planned RO
    ddb = mock.MagicMock()
    ddb.get_item.return_value = {
        "Item": _s({"ro_id": "RO-X-test"})
    }
    # Scan returns the planned RO PLUS an extra RO for the same avx_action_id
    ddb.scan.return_value = {
        "Items": [
            _s({"ro_id": "RO-X-test", "avx_action_id": "ACT-X-test"}),
            _s({"ro_id": "RO-X-extra", "avx_action_id": "ACT-X-test"}),  # extra!
        ],
        "Count": 2,
    }

    res = _mod.Results()
    _mod._check_item3_ro_residue(ddb, "dms-staging-repair-orders", planned, res)
    statuses = {r[0]: r[1] for r in res.rows}
    assert statuses.get("3-residue-scan") == "FAIL", (
        f"expected 3-residue-scan FAIL when extra RO found; rows={res.rows}"
    )


def test_item3_ro_residue_scan_happy_path_passes() -> None:
    """Scan finds exactly the planned ROs → 3-residue-scan PASS."""
    from boto3.dynamodb.types import TypeSerializer as _TS4  # noqa: PLC0415
    _ser4 = _TS4()

    def _s(obj):
        return {k: _ser4.serialize(v) for k, v in obj.items()}

    planned = [
        {"action_id": "ACT-Y-test", "ro_id": "RO-Y-test"},
    ]
    ddb = mock.MagicMock()
    ddb.get_item.return_value = {"Item": _s({"ro_id": "RO-Y-test"})}
    ddb.scan.return_value = {
        "Items": [_s({"ro_id": "RO-Y-test", "avx_action_id": "ACT-Y-test"})],
        "Count": 1,
    }
    res = _mod.Results()
    _mod._check_item3_ro_residue(ddb, "dms-staging-repair-orders", planned, res)
    statuses = {r[0]: r[1] for r in res.rows}
    assert statuses.get("3-residue-scan") == "PASS", (
        f"expected 3-residue-scan PASS; rows={res.rows}"
    )


# ---------------------------------------------------------------------------
# W3 — helpers: try/finally residue tests (early-return + exception paths)
# ---------------------------------------------------------------------------


def _make_ddb_w3(action_id: str, ro_poll_raises: bool = False) -> Any:
    """DDB fake for W3 residue tests.

    Findings query: FLEET3 -> FINDING_A (future expiry).
    Actions get_item: {} for pick eligibility; "pending" for action poll.
      ro_poll_raises=True: raises RuntimeError on the action poll get_item.
    DMS repair-orders scan: stray RO keyed to bare action_id.
    """
    from boto3.dynamodb.types import TypeSerializer as _TS_W3  # noqa: PLC0415

    _ts = _TS_W3()

    def _s(obj: dict) -> dict:
        return {k: _ts.serialize(v) for k, v in obj.items()}

    class _DW3:
        def query(self, **kw: Any) -> dict:
            ev = kw.get("ExpressionAttributeValues") or {}
            fleet = (ev.get(":flt") or {}).get("S", "")
            if "findings" in kw.get("TableName", "") and fleet == _FLEET3:
                return {"Items": [_s({**_FINDING_A, "expires_at": "2999-12-31T00:00:00Z"})]}
            return {"Items": []}

        def get_item(self, **kw: Any) -> dict:
            k = kw.get("Key") or {}
            ks = list(list(k.values())[0].values())[0] if k else ""
            if "actions" in kw.get("TableName", "") and ks == action_id:
                if ro_poll_raises:
                    raise RuntimeError("W3-test DDB error")
                return {"Item": _s({"action_id": action_id, "status": "pending"})}
            return {}

        def scan(self, **kw: Any) -> dict:
            if "repair-orders" in kw.get("TableName", ""):
                bare = action_id.removeprefix("AVX-ACT-")
                return {"Items": [_s({"ro_id": "STRAY-W3", "avx_action_id": bare})]}
            return {"Items": []}

    return _DW3()


def _run_item3_w3(action_id: str, ddb: Any) -> tuple[Any, Any]:
    """Run _item_3 with W3-specific mocks; return (res, exc_or_None)."""
    import json as _jw3  # noqa: PLC0415
    import time as _tw3  # noqa: PLC0415
    from contextlib import contextmanager as _cm  # noqa: PLC0415

    @_cm
    def _fp(*_a: Any, **_kw: Any) -> Any:
        yield "tok-w3", "user-w3"

    class _FH:
        def call(self, method: str, path: str, auth: str, body: Any = None) -> tuple:
            if "approve" in path and method == "POST":
                return 200, {"action_id": action_id}
            return 200, {}

    ssm = mock.MagicMock()
    ssm.get_parameter.return_value = {"Parameter": {"Value": "pool-w3"}}
    cfn = mock.MagicMock()
    cfn.describe_stacks.return_value = {"Stacks": [{"Outputs": [
        {"OutputKey": "RestApiUrl", "OutputValue": "https://avx-w3.test/"},
        {"OutputKey": "ApiUrl",     "OutputValue": "https://dms-w3.test/"},
    ]}]}
    idp = mock.MagicMock()
    idp.list_user_pool_clients.return_value = {"UserPoolClients": [{"ClientId": "c-w3"}]}
    idp.describe_user_pool_client.return_value = {
        "UserPoolClient": {"ExplicitAuthFlows": ["ALLOW_USER_PASSWORD_AUTH"]}}
    idp.initiate_auth.return_value = {"AuthenticationResult": {"IdToken": "tok-w3"}}

    res = Results()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as tf:
        _jw3.dump({"run_id": _RUN3, "items": {}}, tf)
        mp = Path(tf.name)

    _t0 = _tw3.time()
    n = [0]

    def _qt() -> float:
        n[0] += 1
        return _t0 if n[0] <= 30 else _t0 + 500

    exc_out: Any = None
    with (mock.patch.object(_mod, "_client", side_effect=lambda s, *a, **kw: {
              "dynamodb": ddb, "ssm": ssm, "cloudformation": cfn,
              "cognito-idp": idp, "s3": mock.MagicMock(), "lambda": mock.MagicMock(),
          }.get(s, mock.MagicMock())),
          mock.patch.object(_mod, "principal", side_effect=_fp),
          mock.patch.object(_mod, "_load_labor_op_map_s3", return_value={}),
          mock.patch.object(_mod, "Http", return_value=_FH()),
          mock.patch.object(_mod, "_in_write_window", return_value=False),
          mock.patch("time.sleep"),
          mock.patch("time.time", side_effect=_qt)):
        try:
            _mod._item_3(res, run=_RUN3, stage=_STAGE3, region=_REGION3,
                         account=_ACCOUNT3, manifest=mp)
        except RuntimeError as e:
            exc_out = e
    return res, exc_out


def test_item3_w3_early_return_residue_scan_fails() -> None:
    """W3: early return (3a-ro-id times out), stray RO in DMS → 3-residue-scan FAIL.

    Action poll never returns in_progress; time.time expires deadline quickly.
    Before W3 fix: _residue_planned never populated (early return before line 2203)
    → _check_item3_ro_residue never called → 3-residue-scan absent.
    After fix: try/finally records action_id; scan finds stray RO → FAIL.
    """
    action_id = "ACT-W3-EARLY-001"
    ddb = _make_ddb_w3(action_id, ro_poll_raises=False)
    res, exc = _run_item3_w3(action_id, ddb)
    assert exc is None, f"unexpected exception: {exc}"
    statuses = {r[0]: r[1] for r in res.rows}
    assert statuses.get("3-residue-scan") == "FAIL", (
        f"expected 3-residue-scan FAIL after early return with stray RO; rows={res.rows}"
    )


def test_item3_w3_exception_residue_fires() -> None:
    """W3: RuntimeError in action poll still triggers residue check in finally.

    DDB raises RuntimeError when _item_3 polls the action status (no except handler).
    Before fix: exception propagates without calling _check_item3_ro_residue.
    After fix: try/finally fires residue check; stray RO found → 3-residue-scan FAIL.
    """
    action_id = "ACT-W3-EXC-002"
    ddb = _make_ddb_w3(action_id, ro_poll_raises=True)
    res, exc = _run_item3_w3(action_id, ddb)
    assert exc is not None, "expected RuntimeError to propagate through _item_3"
    statuses = {r[0]: r[1] for r in res.rows}
    assert statuses.get("3-residue-scan") == "FAIL", (
        f"expected 3-residue-scan FAIL even after exception; rows={res.rows}"
    )


# ===========================================================================
# C2S: checked==0 is FAIL; _pick_fleet_service_finding prefers fault/pm Finding
# ===========================================================================

class TestCheckLaborOpsCheckedZero:
    """_check_labor_ops_in_map: all-a2 needs (checked==0) must FAIL."""

    def test_all_a2_needs_fails(self):
        """All needs are a2: -> checked==0 -> FAIL 'no fault/PM need checked'."""
        needs = [_agent2_need("health.battery"), _agent2_need("health.tires")]
        ok, detail = _check_labor_ops_in_map(needs, _MAP_SET)
        assert not ok, f"expected FAIL for all-a2 needs; got ok=True detail={detail!r}"
        assert "no fault" in detail.lower() or "checked" in detail.lower(), detail

    def test_one_fault_and_a2_is_not_zero(self):
        """One fault: need plus a2 needs -> checked>=1 -> PASS when in map."""
        needs = [_fault_need("P0AA6", "HV_ISOLATION_REPAIR"), _agent2_need()]
        ok, detail = _check_labor_ops_in_map(needs, _MAP_SET)
        assert ok, detail


class TestPickFleetServiceFindingPrefersFaultNeeds:
    """_pick_fleet_service_finding must prefer Findings with fault/pm needs."""

    _TABLE = "vsa-staging-avx-findings"
    _ACTIONS = "vsa-staging-avx-actions"
    _FLEET = "demo-rental-carshare"
    _NOW = "2026-10-17T02:10:00Z"

    def _a2_only_finding(self, fid: str = "FIND-A2") -> dict:
        f = _book_finding(fid)
        f["recommended_action"]["parameters"]["needs"] = [_agent2_need()]
        return f

    def test_fault_finding_preferred_over_a2_only(self):
        """Two eligible findings: one fault, one a2-only -> fault finding returned."""
        f_fault = _book_finding("FIND-002")  # has _fault_need by default
        f_a2 = self._a2_only_finding("FIND-001")  # lower id but a2-only
        ddb = _make_ddb_low_mock([f_a2, f_fault])
        result = _pick_fleet_service_finding(
            ddb, self._TABLE, self._ACTIONS, self._FLEET, self._NOW,
        )
        assert result is not None
        assert result["finding_id"] == "FIND-002", (
            f"expected fault finding FIND-002 to be preferred; got {result.get('finding_id')!r}"
        )

    def test_all_a2_only_returns_none(self):
        """All eligible findings have only a2: needs -> None (fail visibly)."""
        ddb = _make_ddb_low_mock([self._a2_only_finding("FIND-001")])
        result = _pick_fleet_service_finding(
            ddb, self._TABLE, self._ACTIONS, self._FLEET, self._NOW,
        )
        assert result is None, (
            f"expected None when no fault/pm needs available; got {result!r}"
        )


# ===========================================================================
# Review cycle 3 W1: one residue check on EVERY exit after the first approve
# (a return, an exception, or SystemExit from a signal), exactly once.
# ===========================================================================

def _w1_world(*, raise_in_b_poll: BaseException | None = None,
              no_finding_b: bool = False) -> dict:
    """The happy world, with B's ro_id poll raising, or Finding B absent.

    The repair-orders scan in the happy world returns both A's and B's ROs, so a
    run that never learned B's ro_id (B's poll raised) reports B's RO as extra;
    with Finding B absent the scan returns A's RO only.
    """
    world = _build_item3_world()
    inner = world["ddb"]
    aid_b = world["action_id_b"]
    ro_a, aid_a = world["ro_id_a"], world["action_id_a"]
    from boto3.dynamodb.types import TypeSerializer as _TSw1  # noqa: PLC0415
    _s = _TSw1()

    class _DDB:
        def query(self, **kw: Any) -> dict:
            ev = kw.get("ExpressionAttributeValues") or {}
            fleet = (ev.get(":flt") or ev.get(":fid") or {}).get("S", "")
            if no_finding_b and "findings" in kw.get("TableName", "") and fleet == _FLEET3B:
                return {"Items": [], "Count": 0}
            return inner.query(**kw)

        def get_item(self, **kw: Any) -> dict:
            key = kw.get("Key") or {}
            if (raise_in_b_poll is not None and "actions" in kw.get("TableName", "")
                    and (key.get("action_id") or {}).get("S") == aid_b):
                raise raise_in_b_poll
            return inner.get_item(**kw)

        def scan(self, **kw: Any) -> dict:
            if no_finding_b and "repair-orders" in kw.get("TableName", ""):
                item = {"ro_id": _s.serialize(ro_a), "avx_action_id": _s.serialize(aid_a)}
                return {"Items": [item], "Count": 1}
            return inner.scan(**kw)

    world["ddb"] = _DDB()
    return world


def _residue_rows(res: Results) -> list[tuple[str, str, str]]:
    return [r for r in res.rows if r[0] == "3-residue-scan"]


def test_item3_w1_full_path_checks_residue_once_and_finds_both_ros() -> None:
    """The all-pass path (every normal run) checks the RO residue exactly once, and finds
    the two ROs the run made (mutant p3-final-residue-dropped)."""
    res = _run_item3_with_world(_build_item3_world())
    rows = _residue_rows(res)
    assert len(rows) == 1, f"expected exactly one 3-residue-scan row; rows={res.rows}"
    assert rows[0][1] == "PASS" and rows[0][2] == "scan found 2 RO(s) for 2 action_id(s)", rows


def test_item3_w1_raise_after_poll_a_still_checks_residue() -> None:
    """An exception after poll A (here B's ro_id poll) propagates AND the residue check
    still runs once; B's RO, whose id the run never learned, is reported as extra."""
    world = _w1_world(raise_in_b_poll=RuntimeError("W1 B-poll error"))
    try:
        _run_item3_with_world(world)
    except RuntimeError as exc:
        assert "W1 B-poll error" in str(exc)
    else:
        raise AssertionError("expected the B-poll RuntimeError to propagate")


def test_item3_w1_raise_after_poll_a_residue_row() -> None:
    """Same failure, reading what the check saw and wrote: it ran exactly once, with A's
    action and RO and B's action (no RO id), and its scan row FAILs naming B's RO as extra."""
    world = _w1_world(raise_in_b_poll=RuntimeError("W1 B-poll error"))
    seen: list[list[dict]] = []
    rows: list[tuple[str, str, str]] = []
    orig = _mod._check_item3_ro_residue

    def _spy(ddb, table, planned, r):  # noqa: ANN001
        seen.append([dict(p) for p in planned])
        out = orig(ddb, table, planned, r)
        rows.extend(x for x in r.rows if x[0] == "3-residue-scan")
        return out

    with mock.patch.object(_mod, "_check_item3_ro_residue", side_effect=_spy):
        try:
            _run_item3_with_world(world)
        except RuntimeError:
            pass
    assert len(seen) == 1, f"residue check must run exactly once; ran {len(seen)} times"
    assert [p.get("action_id") for p in seen[0]] == [world["action_id_a"], world["action_id_b"]], seen
    assert seen[0][0].get("ro_id") == world["ro_id_a"] and not seen[0][1].get("ro_id"), seen
    assert len(rows) == 1 and rows[0][1] == "FAIL" and world["ro_id_b"] in rows[0][2], rows


def test_item3_w1_signal_after_first_approve_still_checks_residue() -> None:
    """SystemExit (what a SIGTERM/SIGHUP/SIGINT raises) after the first approve still goes
    through the residue check, once, and then propagates."""
    seen: list[int] = []
    orig = _mod._check_item3_ro_residue

    def _spy(ddb, table, planned, r):  # noqa: ANN001
        seen.append(len(planned))
        return orig(ddb, table, planned, r)

    with mock.patch.object(_mod, "_check_item3_ro_residue", side_effect=_spy):
        try:
            _run_item3_with_world(_w1_world(raise_in_b_poll=SystemExit(143)))
        except SystemExit as exc:
            assert exc.code == 143
        else:
            raise AssertionError("expected SystemExit(143) to propagate")
    assert seen == [2], f"residue check must run exactly once with both planned actions; got {seen}"


def test_item3_w1_3d_skip_still_checks_residue() -> None:
    """No second Finding (3d SKIP) still checks A's RO, once (mutant p3-3d-skip-residue-dropped)."""
    res = _run_item3_with_world(_w1_world(no_finding_b=True))
    statuses = {r[0]: r[1] for r in res.rows}
    assert statuses.get("3d") == "SKIP", res.rows
    rows = _residue_rows(res)
    assert len(rows) == 1, f"expected exactly one 3-residue-scan row; rows={res.rows}"
    assert rows[0][1] == "PASS" and rows[0][2] == "scan found 1 RO(s) for 1 action_id(s)", rows


def test_item3_c3_s2_finding_b_with_only_agent2_needs_still_runs_3d(monkeypatch) -> None:
    """Review c3 S2: the fault:/pm: filter is for Finding A only. With B carrying only an
    Agent 2 need, the cancel path still runs (3d-approve/3d-invoke PASS, no 3d SKIP)."""
    import copy  # noqa: PLC0415
    import sys  # noqa: PLC0415
    b = copy.deepcopy(_FINDING_B)
    b["recommended_action"]["parameters"]["needs"] = [_agent2_need()]
    monkeypatch.setattr(sys.modules[__name__], "_FINDING_B", b)
    res = _run_item3_with_world(_build_item3_world())
    statuses = {r[0]: r[1] for r in res.rows}
    assert "3d" not in statuses, f"3d must not SKIP when B has only Agent 2 needs; rows={res.rows}"
    assert statuses.get("3d-approve") == "PASS" and statuses.get("3d-invoke") == "PASS", res.rows
