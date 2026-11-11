"""Unit tests for scripts/live-verify-fi-rebalancing.py (--part adp).

Tests use stubbed data only — zero live AWS calls.

Structure:
  - autouse fixture strips AWS_* and LIVE_TESTS env vars
  - subprocess test: --help exits 0
  - athena_rows helper: paging and FAILED-state tests
  - A1–A7: placeholder tests (pass + fail per item, updated as each item is implemented)

Run:
    .venv/bin/python -m pytest scripts/tests/test_live_verify_fi_rebalancing.py -q
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Load the script as a module (hyphenated name requires importlib)
# ---------------------------------------------------------------------------

_SCRIPT = Path(__file__).resolve().parents[1] / "live-verify-fi-rebalancing.py"
_spec = importlib.util.spec_from_file_location("live_verify_fi_rebalancing", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

record = _mod.record
athena_rows = _mod.athena_rows
check_a1_tables = _mod.check_a1_tables
check_a2_dates = _mod.check_a2_dates
check_a3_answer_key = _mod.check_a3_answer_key
check_a4_iam = _mod.check_a4_iam
check_a5_sns = _mod.check_a5_sns
check_a6_vins = _mod.check_a6_vins
check_a7_policy = _mod.check_a7_policy

_SCRIPT_PATH = _SCRIPT  # for subprocess tests

# ---------------------------------------------------------------------------
# Autouse: strip AWS credentials and LIVE_TESTS so no real call can fire
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("AWS_") or key == "LIVE_TESTS":
            monkeypatch.delenv(key, raising=False)
    # Also set dummy values so botocore doesn't complain about missing creds
    # Not access-key-shaped: the publish scanner flags AKIA/ASIA ids, even the docs example.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


# ---------------------------------------------------------------------------
# Fake Athena helpers
# ---------------------------------------------------------------------------


def _athena_multi(*query_results: list[list[str | None]]) -> Any:
    """Fake Athena that serves successive start_query_execution calls.

    Each positional argument is a list of row-lists (strings or None).
    The first row of the first argument is the header row.
    Single page per query (no NextToken).
    """
    responses = [list(rows) for rows in query_results]
    call_no = [0]

    class _Ath:
        def start_query_execution(self, **kw: Any) -> dict:
            n = call_no[0]
            call_no[0] += 1
            return {"QueryExecutionId": str(n)}

        def get_query_execution(self, **kw: Any) -> dict:
            return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

        def get_query_results(self, QueryExecutionId: str, **kw: Any) -> dict:
            idx = int(QueryExecutionId)
            rows = responses[idx]
            return {
                "ResultSet": {
                    "Rows": [
                        {
                            "Data": [
                                {"VarCharValue": v} if v is not None else {}
                                for v in row
                            ]
                        }
                        for row in rows
                    ]
                }
            }

    return _Ath()


def _athena_failed() -> Any:
    """Fake Athena whose queries always fail."""

    class _Ath:
        def start_query_execution(self, **kw: Any) -> dict:
            return {"QueryExecutionId": "fail-qid"}

        def get_query_execution(self, **kw: Any) -> dict:
            return {
                "QueryExecution": {
                    "Status": {"State": "FAILED", "StateChangeReason": "table not found"}
                }
            }

        def get_query_results(self, **kw: Any) -> dict:
            raise AssertionError("should not be called for a FAILED query")

    return _Ath()


# ---------------------------------------------------------------------------
# Subprocess test
# ---------------------------------------------------------------------------


def test_subprocess_help() -> None:
    """The script parses --help with exit 0 and no live AWS calls (PYTHONPATH stripped)."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("AWS_") and k not in ("PYTHONPATH", "LIVE_TESTS")
    }
    with tempfile.TemporaryDirectory() as tmp:
        result = subprocess.run(
            [sys.executable, str(_SCRIPT_PATH), "--help"],
            capture_output=True, text=True, cwd=tmp, env=env,
        )
    assert result.returncode == 0, result.stderr
    assert "--part" in result.stdout
    assert "--workgroup" in result.stdout


# ---------------------------------------------------------------------------
# athena_rows helper
# ---------------------------------------------------------------------------


def test_athena_rows_single_page() -> None:
    """athena_rows strips the header and returns data rows from a single page."""
    ath = _athena_multi(
        [["count"], ["42"]],
    )
    rows = athena_rows(ath, "SELECT count(*) FROM t", "wg")
    assert rows == [["42"]]


def test_athena_rows_paging() -> None:
    """athena_rows assembles rows across multiple get_query_results calls via NextToken."""
    call_no = [0]

    class _PagedAth:
        def start_query_execution(self, **kw: Any) -> dict:
            return {"QueryExecutionId": "0"}

        def get_query_execution(self, **kw: Any) -> dict:
            return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

        def get_query_results(self, **kw: Any) -> dict:
            n = call_no[0]
            call_no[0] += 1
            if n == 0:
                return {
                    "ResultSet": {
                        "Rows": [
                            {"Data": [{"VarCharValue": "col"}]},
                            {"Data": [{"VarCharValue": "row1"}]},
                        ]
                    },
                    "NextToken": "tok1",
                }
            return {
                "ResultSet": {
                    "Rows": [{"Data": [{"VarCharValue": "row2"}]}]
                }
            }

    rows = athena_rows(_PagedAth(), "SELECT col FROM t", "wg")
    assert rows == [["row1"], ["row2"]]


def test_athena_rows_failed() -> None:
    """athena_rows raises RuntimeError when the query FAILED."""
    with pytest.raises(RuntimeError, match="FAILED"):
        athena_rows(_athena_failed(), "SELECT 1", "wg")


# ---------------------------------------------------------------------------
# record helper
# ---------------------------------------------------------------------------


def test_record_appends_tuple(capsys: pytest.CaptureFixture) -> None:
    results: list = []
    record(results, "item-x", "PASS", "all good")
    assert results == [("item-x", "PASS", "all good")]
    out = capsys.readouterr().out
    assert "PASS" in out and "item-x" in out


# ---------------------------------------------------------------------------
# A1 — tables/views/snapshots exist
# ---------------------------------------------------------------------------


def _a1_all_ok_ath() -> Any:
    """Fake Athena where every COUNT(*) returns '100'."""
    obj_count = len(_mod._RAW_TABLES) + len(_mod._VIEWS) + len(_mod._SNAPSHOT_TABLES)
    pages = [[["count"], ["100"]]] * obj_count
    return _athena_multi(*pages)


def test_a1_pass_all_objects_present() -> None:
    """A1 PASS: all tables, views, and snapshots return a count row."""
    results: list = []
    check_a1_tables(_a1_all_ok_ath(), "adp_staging_fleet_rebalance", "wg", results)
    assert results[0][1] == "PASS", results[0]
    assert "booking_requests" in results[0][2]


def test_a1_fail_one_table_missing() -> None:
    """A1 FAIL: one table raises (FAILED query state); item records FAIL."""
    # First query succeeds, then one fails (returns FAILED state)
    call_no = [0]

    class _PartialAth:
        def start_query_execution(self, **kw: Any) -> dict:
            n_ = call_no[0]
            call_no[0] += 1
            return {"QueryExecutionId": str(n_)}

        def get_query_execution(self, QueryExecutionId: str, **kw: Any) -> dict:
            # Make the second query (index 1 = vehicle_days) FAIL
            state = "FAILED" if int(QueryExecutionId) == 1 else "SUCCEEDED"
            return {"QueryExecution": {"Status": {"State": state, "StateChangeReason": "table not found"}}}

        def get_query_results(self, QueryExecutionId: str, **kw: Any) -> dict:
            return {"ResultSet": {"Rows": [{"Data": [{"VarCharValue": "count"}]}, {"Data": [{"VarCharValue": "50"}]}]}}

    results: list = []
    check_a1_tables(_PartialAth(), "adp_staging_fleet_rebalance", "wg", results)
    assert results[0][1] == "FAIL", results[0]
    assert "vehicle_days" in results[0][2]


def test_a1_fail_bad_db_name() -> None:
    """A1 FAIL: fr_db with a slash is rejected before any SQL is run."""
    results: list = []
    check_a1_tables(MagicMock(), "adp_staging/bad", "wg", results)
    assert results[0][1] == "FAIL"
    assert "validation" in results[0][2]


# ---------------------------------------------------------------------------
# A2 — max dates from bookings_runs and fleet_bookings_forecast
# ---------------------------------------------------------------------------


def test_a2_pass_no_expect_after() -> None:
    """A2 PASS: returns current dates without --expect-after."""
    ath = _athena_multi(
        [["max(data_as_of)"], ["2026-10-10"]],
        [["max(run_date)"],   ["2026-10-10"]],
    )
    results: list = []
    check_a2_dates(ath, "adp_staging_fleet_rebalance", "wg", None, results)
    assert results[0][1] == "PASS", results[0]
    assert "2026-10-10" in results[0][2]


def test_a2_pass_with_expect_after_both_later() -> None:
    """A2 PASS: both dates are strictly after --expect-after."""
    ath = _athena_multi(
        [["max(data_as_of)"], ["2026-10-11"]],
        [["max(run_date)"],   ["2026-10-11"]],
    )
    results: list = []
    check_a2_dates(ath, "adp_staging_fleet_rebalance", "wg", "2026-10-10", results)
    assert results[0][1] == "PASS", results[0]


def test_a2_fail_date_not_later_than_expect_after() -> None:
    """A2 FAIL: data_as_of equals expect_after (must be strictly later)."""
    ath = _athena_multi(
        [["max(data_as_of)"], ["2026-10-10"]],   # equal, not strictly later
        [["max(run_date)"],   ["2026-10-11"]],
    )
    results: list = []
    check_a2_dates(ath, "adp_staging_fleet_rebalance", "wg", "2026-10-10", results)
    assert results[0][1] == "FAIL", results[0]
    assert "data_as_of" in results[0][2]


def test_a2_fail_null_date() -> None:
    """A2 FAIL: NULL (None) data_as_of fails the strictly-later check."""
    ath = _athena_multi(
        [["max(data_as_of)"], [None]],
        [["max(run_date)"],   ["2026-10-11"]],
    )
    results: list = []
    check_a2_dates(ath, "adp_staging_fleet_rebalance", "wg", "2026-10-10", results)
    assert results[0][1] == "FAIL", results[0]


def test_a2_fail_bad_date_arg() -> None:
    """A2 FAIL: malformed --expect-after rejected before any Athena call."""
    results: list = []
    check_a2_dates(MagicMock(), "adp_staging_fleet_rebalance", "wg", "not-a-date", results)
    assert results[0][1] == "FAIL"
    assert "validation" in results[0][2]


# ---------------------------------------------------------------------------
# A3 — answer key exists in S3
# ---------------------------------------------------------------------------


def _s3_fake(has_plants: bool = True, parquet_count: int = 1) -> Any:
    """Fake S3 client for A3 tests."""
    _prefix = _mod._ANSWER_KEY_PREFIX

    class _S3:
        def head_object(self, Bucket: str, Key: str) -> dict:
            if not has_plants:
                raise RuntimeError("NoSuchKey")
            return {"ContentLength": 1234}

        def list_objects_v2(self, **kw: Any) -> dict:
            contents = [
                {"Key": f"{_prefix}/2026-10-10.parquet"}
            ] * parquet_count
            return {"Contents": contents} if parquet_count > 0 else {}

    return _S3()


def test_a3_pass_plants_and_parquet() -> None:
    """A3 PASS: plants JSON + one parquet present."""
    results: list = []
    check_a3_answer_key(
        _s3_fake(has_plants=True, parquet_count=1),
        "adp-staging-foundation-lake-123456789012-us-east-1",
        results,
    )
    assert results[0][1] == "PASS", results[0]
    assert "parquet" in results[0][2]


def test_a3_fail_no_plants_json() -> None:
    """A3 FAIL: plants JSON head_object raises (file missing)."""
    results: list = []
    check_a3_answer_key(
        _s3_fake(has_plants=False, parquet_count=1),
        "adp-staging-foundation-lake-123456789012-us-east-1",
        results,
    )
    assert results[0][1] == "FAIL", results[0]
    assert "plants" in results[0][2]


def test_a3_fail_no_parquet() -> None:
    """A3 FAIL: no parquet files under the prefix."""
    results: list = []
    check_a3_answer_key(
        _s3_fake(has_plants=True, parquet_count=0),
        "adp-staging-foundation-lake-123456789012-us-east-1",
        results,
    )
    assert results[0][1] == "FAIL", results[0]
    assert "parquet" in results[0][2]


# ---------------------------------------------------------------------------
# A4 — IAM simulate: consumer roles denied, bookings role allowed
# ---------------------------------------------------------------------------


def _iam_fake(
    avx_decision: str = "implicitDeny",
    cms_decision: str = "implicitDeny",
    bookings_decision: str = "allowed",
) -> Any:
    """Fake IAM client for A4 simulate_principal_policy calls.

    Decisions are returned in call order: AVX GetObject, CMS GetObject, bookings PutObject.
    """
    decisions = [avx_decision, cms_decision, bookings_decision]
    call_no = [0]

    class _IAM:
        def simulate_principal_policy(self, **kw: Any) -> dict:
            d = decisions[call_no[0]]
            call_no[0] += 1
            return {"EvaluationResults": [{"EvalDecision": d}]}

    return _IAM()


def _lam_fake(
    role_arn: str = "arn:aws:iam::123456789012:role/test-role",
    fn_prefix: str = "cms-staging-ui-FleetIntelligenceFunction",
) -> Any:
    """Fake Lambda client that returns *role_arn* for any get_function_configuration call."""

    class _Pager:
        def paginate(self) -> list:
            return [{"Functions": [{"FunctionName": fn_prefix + "-ABCD1234"}]}]

    class _Lam:
        def get_function_configuration(self, **kw: Any) -> dict:
            return {"Role": role_arn}

        def get_paginator(self, op: str) -> Any:
            return _Pager()

    return _Lam()


def test_a4_pass_consumers_denied_bookings_allowed() -> None:
    """A4 PASS: AVX+CMS implicitDeny on GetObject; bookings allowed PutObject."""
    results: list = []
    check_a4_iam(
        _iam_fake("implicitDeny", "implicitDeny", "allowed"),
        _lam_fake("arn:aws:iam::123456789012:role/vsa-staging-tier2-scheduler-us-west-2"),
        _lam_fake("arn:aws:iam::123456789012:role/cms-staging-ui-FleetIntelligenceRole"),
        "adp-staging-foundation-lake-123456789012-us-east-1",
        "staging", "123456789012", results,
    )
    assert results[0][1] == "PASS", results[0]
    assert "allowed" in results[0][2]


def test_a4_fail_consumer_allowed_getobject() -> None:
    """A4 FAIL: AVX role is 'allowed' for s3:GetObject (must be denied)."""
    results: list = []
    check_a4_iam(
        _iam_fake("allowed", "implicitDeny", "allowed"),  # AVX=allowed → FAIL
        _lam_fake("arn:aws:iam::123456789012:role/vsa-staging-tier2-scheduler-us-west-2"),
        _lam_fake("arn:aws:iam::123456789012:role/cms-staging-ui-FleetIntelligenceRole"),
        "adp-staging-foundation-lake-123456789012-us-east-1",
        "staging", "123456789012", results,
    )
    assert results[0][1] == "FAIL", results[0]
    assert "GetObject is ALLOWED" in results[0][2]


def test_a4_fail_positive_control_denied() -> None:
    """A4 FAIL: bookings role denied PutObject (positive control must pass)."""
    results: list = []
    check_a4_iam(
        _iam_fake("implicitDeny", "implicitDeny", "implicitDeny"),  # bookings=implicitDeny → FAIL
        _lam_fake("arn:aws:iam::123456789012:role/vsa-staging-tier2-scheduler-us-west-2"),
        _lam_fake("arn:aws:iam::123456789012:role/cms-staging-ui-FleetIntelligenceRole"),
        "adp-staging-foundation-lake-123456789012-us-east-1",
        "staging", "123456789012", results,
    )
    assert results[0][1] == "FAIL", results[0]
    assert "positive control" in results[0][2]


# ---------------------------------------------------------------------------
# A5 — SNS topics have confirmed subscriptions
# ---------------------------------------------------------------------------


def _cfn_fake(topic_arns: list[str]) -> Any:
    """Fake CloudFormation client listing the given SNS topic ARNs."""

    class _Pager:
        def paginate(self, **kw: Any) -> list:
            return [{
                "StackResourceSummaries": [
                    {"ResourceType": "AWS::SNS::Topic", "PhysicalResourceId": arn}
                    for arn in topic_arns
                ]
            }]

    class _CFN:
        def get_paginator(self, op: str) -> Any:
            return _Pager()

    return _CFN()


def _sns_fake(has_confirmed: bool = True) -> Any:
    """Fake SNS client that returns one subscription per topic."""

    class _SNS:
        def list_subscriptions_by_topic(self, TopicArn: str, **kw: Any) -> dict:
            arn = "arn:aws:sns:us-east-1:123456789012:email-sub" if has_confirmed else "PendingConfirmation"
            return {"Subscriptions": [{"SubscriptionArn": arn}]}

    return _SNS()


def test_a5_pass_all_topics_confirmed() -> None:
    """A5 PASS: two topics, both have confirmed subscriptions."""
    results: list = []
    check_a5_sns(
        _sns_fake(has_confirmed=True),
        _cfn_fake(["arn:aws:sns:us-east-1:123:fleet-rebalance-alarms"]),
        "staging", results,
    )
    assert results[0][1] == "PASS", results[0]
    assert "confirmed" in results[0][2]


def test_a5_fail_topic_has_no_confirmed_subscription() -> None:
    """A5 FAIL: topic's only subscription is PendingConfirmation."""
    results: list = []
    check_a5_sns(
        _sns_fake(has_confirmed=False),
        _cfn_fake(["arn:aws:sns:us-east-1:123:fleet-rebalance-alarms"]),
        "staging", results,
    )
    assert results[0][1] == "FAIL", results[0]
    assert "PendingConfirmation" in results[0][2]


def test_a5_fail_no_topics_in_stack() -> None:
    """A5 FAIL: stack has no SNS::Topic resources."""
    results: list = []
    check_a5_sns(
        _sns_fake(has_confirmed=True),
        _cfn_fake([]),
        "staging", results,
    )
    assert results[0][1] == "FAIL", results[0]
    assert "no SNS topics" in results[0][2]


# ---------------------------------------------------------------------------
# A6 — the bookings job's input refusals, run on DuckDB (review c1 W5)
# ---------------------------------------------------------------------------


def _a6(vehicles, vins, moves) -> tuple[str, str]:
    """Run check_a6_vins's real SQL on DuckDB tables shaped like the lake's."""
    import duckdb
    con = duckdb.connect()
    con.execute("CREATE SCHEMA adp_staging_cms_ingest; CREATE SCHEMA adp_staging_dimensions")
    con.execute("CREATE TABLE adp_staging_cms_ingest.vehicles(vin VARCHAR, fleet_id VARCHAR)")
    con.execute("CREATE TABLE adp_staging_dimensions.vins(vin VARCHAR, in_service_date DATE)")
    con.execute("CREATE TABLE adp_staging_cms_ingest.vehicle_moves(vins VARCHAR[], "
                "to_location_id VARCHAR, fleet_id VARCHAR, decided_by_kind VARCHAR)")
    for v in vehicles:
        con.execute("INSERT INTO adp_staging_cms_ingest.vehicles VALUES (?, ?)", v)
    for v in vins:
        con.execute("INSERT INTO adp_staging_dimensions.vins VALUES (?, DATE '2025-01-01')", [v])
    for mv in moves:
        con.execute("INSERT INTO adp_staging_cms_ingest.vehicle_moves VALUES (?, ?, ?, ?)", mv)

    class _Ath:
        rows: list = []

        def start_query_execution(self, QueryString: str, WorkGroup: str) -> dict:
            _Ath.rows = con.execute(QueryString).fetchall()
            return {"QueryExecutionId": "q"}

        def get_query_execution(self, **kw: Any) -> dict:
            return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

        def get_query_results(self, **kw: Any) -> dict:
            rows = [["h"] * 5] + [[str(x) for x in r] for r in _Ath.rows]
            return {"ResultSet": {"Rows": [{"Data": [{"VarCharValue": x} for x in r]}
                                           for r in rows]}}

    res: list = []
    check_a6_vins(_Ath(), "staging", "wg", res)
    return res[0][1], res[0][2]


_R = "demo-rental-carshare"


def test_a6_passes_when_every_rental_vin_has_its_inputs() -> None:
    status, detail = _a6([("V1", _R), ("V2", _R), ("X9", "other")], ["V1", "V2"],
                         [(["V1", "V2"], "loc-a", _R, "seed"), (["V1"], "loc-b", _R, "move")])
    assert status == "PASS", detail
    assert "rental_vins=2" in detail


def test_a6_a_duplicate_dimensions_row_does_not_offset_a_missing_one() -> None:
    """W5: V1 twice, V2 never; a join count(*) gives 2 == 2 and passes."""
    status, detail = _a6([("V1", _R), ("V2", _R)], ["V1", "V1"],
                         [(["V1", "V2"], "loc-a", _R, "seed")])
    assert status == "FAIL" and "without dimensions.vins row=1" in detail
    assert "repeated dimensions rows=1" in detail


def test_a6_a_seed_of_another_fleet_does_not_count() -> None:
    """W5: the job's SQL filters m.fleet_id."""
    status, detail = _a6([("V1", _R)], ["V1"], [(["V1"], "loc-a", "other", "seed")])
    assert status == "FAIL" and "without seed record=1" in detail


def test_a6_conflicting_seed_records_fail() -> None:
    """W5: the job refuses two seeds naming one VIN with different locations."""
    status, detail = _a6([("V1", _R)], ["V1"],
                         [(["V1"], "loc-a", _R, "seed"), (["V1"], "loc-b", _R, "seed")])
    assert status == "FAIL" and "conflicting seed records=1" in detail


def test_a6_no_rental_vins_fails() -> None:
    status, detail = _a6([("X9", "other")], [], [])
    assert status == "FAIL" and "no rental VINs" in detail


# ---------------------------------------------------------------------------
# A7 — fleet_bookings_parameters matches AVX SSM policy
# ---------------------------------------------------------------------------

_VALID_POLICY = {
    "target_utilization": 0.85,
    "deficit_above": 0.90,
    "max_utilization": 0.95,
    "min_net_benefit_usd": 50.0,
    "max_vehicles_per_move": 5,
}


def _ssm_fake(policy_json: str | None = None, error: bool = False) -> Any:
    """Fake SSM client that returns *policy_json* as the parameter value."""
    import json as _json

    if policy_json is None:
        policy_json = _json.dumps(_VALID_POLICY)

    class _SSM:
        def get_parameter(self, Name: str, **kw: Any) -> dict:
            if error:
                raise RuntimeError("ParameterNotFound")
            return {"Parameter": {"Value": policy_json}}

    return _SSM()


def _a7_ath(deficit_above: str = "0.9", target: str = "0.85", max_util: str = "0.95") -> Any:
    """Fake Athena that returns one row of fleet_bookings_parameters."""
    return _athena_multi(
        [["deficit_above", "target_utilization", "max_utilization"],
         [deficit_above, target, max_util]]
    )


def test_a7_pass_values_match() -> None:
    """A7 PASS: ADP parameters match the SSM policy for all three shared keys."""
    results: list = []
    check_a7_policy(
        _a7_ath("0.9", "0.85", "0.95"),
        _ssm_fake(),
        "adp_staging_fleet_rebalance", "wg", "staging", results,
    )
    assert results[0][1] == "PASS", results[0]
    assert "deficit_above" in results[0][2]


def test_a7_fail_deficit_above_mismatch() -> None:
    """A7 FAIL: ADP deficit_above differs from the SSM policy value."""
    results: list = []
    check_a7_policy(
        _a7_ath(deficit_above="0.80"),  # ADP says 0.80, SSM says 0.90
        _ssm_fake(),
        "adp_staging_fleet_rebalance", "wg", "staging", results,
    )
    assert results[0][1] == "FAIL", results[0]
    assert "deficit_above" in results[0][2]


def test_a7_fail_ssm_parameter_missing() -> None:
    """A7 FAIL: SSM get_parameter raises (parameter does not exist)."""
    results: list = []
    check_a7_policy(
        _a7_ath(),
        _ssm_fake(error=True),
        "adp_staging_fleet_rebalance", "wg", "staging", results,
    )
    assert results[0][1] == "FAIL", results[0]
    assert "SSM" in results[0][2] or "parameter" in results[0][2].lower()


def test_a7_fail_empty_parameters_table() -> None:
    """A7 FAIL: fleet_bookings_parameters returns no rows."""
    ath = _athena_multi(
        [["deficit_above", "target_utilization", "max_utilization"]]  # header only, no data
    )
    results: list = []
    check_a7_policy(ath, _ssm_fake(), "adp_staging_fleet_rebalance", "wg", "staging", results)
    assert results[0][1] == "FAIL", results[0]
    assert "empty" in results[0][2]


# ---------------------------------------------------------------------------
# Re-import new helpers (module was already loaded above)
# ---------------------------------------------------------------------------

_in_write_window      = _mod._in_write_window
_check_scheduler_timing = _mod._check_scheduler_timing


# ---------------------------------------------------------------------------
# _in_write_window
# ---------------------------------------------------------------------------


def test_in_write_window_inside(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_in_write_window` returns True when UTC time is 05:00 (inside window)."""
    monkeypatch.setattr(
        _mod, "_now",
        lambda: datetime.datetime(2026, 10, 6, 5, 0, 0, tzinfo=datetime.timezone.utc),
    )
    assert _in_write_window() is True


@pytest.mark.parametrize("hh,mm,inside", [(4, 49, False), (4, 50, True), (6, 10, True),
                                          (6, 11, False), (0, 0, False), (23, 59, False)])
def test_in_write_window_edges(monkeypatch: pytest.MonkeyPatch, hh: int, mm: int,
                               inside: bool) -> None:
    """The refused window is exactly 04:50-06:10 UTC, both ends inclusive (security c1 S-G8)."""
    monkeypatch.setattr(
        _mod, "_now",
        lambda: datetime.datetime(2026, 10, 6, hh, mm, 0, tzinfo=datetime.timezone.utc),
    )
    assert _in_write_window() is inside


def test_in_write_window_outside(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_in_write_window` returns False when UTC time is 09:00 (outside window)."""
    monkeypatch.setattr(
        _mod, "_now",
        lambda: datetime.datetime(2026, 10, 6, 9, 0, 0, tzinfo=datetime.timezone.utc),
    )
    assert _in_write_window() is False


# ---------------------------------------------------------------------------
# _check_scheduler_timing fakes
# ---------------------------------------------------------------------------


def _make_sched_fake(
    schedules: list[dict],
    cron_hour: int = 14,
    cron_minute: int = 0,
    state: str = "ENABLED",
    tz: str = "UTC",
) -> Any:
    """Fake EventBridge Scheduler client for timing tests."""

    class _Sched:
        def list_schedules(self, **kw: Any) -> dict:
            return {"Schedules": schedules}

        def get_schedule(self, Name: str, GroupName: str = "default", **kw: Any) -> dict:
            return {
                "State": state,
                "ScheduleExpression": f"cron({cron_minute} {cron_hour} * * ? *)",
                "ScheduleExpressionTimezone": tz,
                "Target": {"Input": json.dumps({"component": "rebalance"})},
            }

    return _Sched()


_FAKE_FN_ARN = "arn:aws:lambda:us-west-2:123456789012:function:vsa-staging-tier2-health-agent-us-west-2"


def test_scheduler_timing_ok_outside_45min(monkeypatch: pytest.MonkeyPatch) -> None:
    """PASS when cron is at 14:00Z and now is 09:00Z (300 min away)."""
    monkeypatch.setattr(
        _mod, "_now",
        lambda: datetime.datetime(2026, 10, 6, 9, 0, 0, tzinfo=datetime.timezone.utc),
    )
    sched = _make_sched_fake(
        [{"Name": "rebal", "GroupName": "default", "Target": {"Arn": _FAKE_FN_ARN}}],
        cron_hour=14, cron_minute=0,
    )
    ok, msg = _check_scheduler_timing(sched, _FAKE_FN_ARN)
    assert ok is True, msg


def test_scheduler_timing_refuse_within_45min(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse when cron is 06:00Z and now is 06:30Z (30 min after, within ±45 min)."""
    monkeypatch.setattr(
        _mod, "_now",
        lambda: datetime.datetime(2026, 10, 6, 6, 30, 0, tzinfo=datetime.timezone.utc),
    )
    sched = _make_sched_fake(
        [{"Name": "rebal", "GroupName": "default", "Target": {"Arn": _FAKE_FN_ARN}}],
        cron_hour=6, cron_minute=0,
    )
    ok, msg = _check_scheduler_timing(sched, _FAKE_FN_ARN)
    assert ok is False
    assert "within ±45 min" in msg


def test_scheduler_timing_no_schedules_refuses() -> None:
    """Refuse when zero schedules match the Lambda ARN."""
    sched = _make_sched_fake([])  # empty list: no matching schedules
    ok, msg = _check_scheduler_timing(sched, _FAKE_FN_ARN)
    assert ok is False
    assert "0 schedule" in msg


def _at(monkeypatch: pytest.MonkeyPatch, h: int, m: int) -> None:
    monkeypatch.setattr(
        _mod, "_now",
        lambda: datetime.datetime(2026, 10, 6, h, m, 0, tzinfo=datetime.timezone.utc))


def _one(**kw: Any) -> Any:
    return _make_sched_fake(
        [{"Name": "s", "GroupName": "default", "Target": {"Arn": _FAKE_FN_ARN}}], **kw)


@pytest.mark.parametrize("h, m, ok", [
    (6, 44, False),   # 44 min after a 06:00 cron: refuse (catches a band narrowed below 45)
    (6, 45, False),   # exactly 45: refuse
    (6, 46, True),    # 46: allowed
    (5, 15, False),   # 45 before: refuse
    (5, 14, True),    # 46 before: allowed
])
def test_scheduler_timing_band_edges(monkeypatch, h, m, ok) -> None:
    """W2: +-45 minutes inclusive, both sides of the cron."""
    _at(monkeypatch, h, m)
    assert _check_scheduler_timing(_one(cron_hour=6, cron_minute=0), _FAKE_FN_ARN)[0] is ok


def test_scheduler_timing_wraps_midnight(monkeypatch) -> None:
    """W2: a 23:50 cron refuses at 00:20 the next day."""
    _at(monkeypatch, 0, 20)
    ok, msg = _check_scheduler_timing(_one(cron_hour=23, cron_minute=50), _FAKE_FN_ARN)
    assert ok is False and "within" in msg


def test_scheduler_timing_refuses_a_non_utc_schedule(monkeypatch) -> None:
    """W2 / M6: a schedule in another zone refuses, even far from its UTC-read time."""
    _at(monkeypatch, 9, 0)
    ok, msg = _check_scheduler_timing(_one(cron_hour=14, tz="America/New_York"), _FAKE_FN_ARN)
    assert ok is False and "not UTC" in msg


def test_scheduler_timing_refuses_an_unparseable_cron(monkeypatch) -> None:
    _at(monkeypatch, 9, 0)

    class _S:
        def list_schedules(self, **kw):
            return {"Schedules": [{"Name": "s", "Target": {"Arn": _FAKE_FN_ARN}}]}

        def get_schedule(self, **kw):
            return {"State": "ENABLED", "ScheduleExpression": "cron(0/5 * * * ? *)",
                    "ScheduleExpressionTimezone": "UTC", "Target": {"Input": "{}"}}
    ok, msg = _check_scheduler_timing(_S(), _FAKE_FN_ARN)
    assert ok is False and "unparseable cron" in msg


@pytest.mark.parametrize("component, ok", [("diagnostics", True), ("rebalance", False)])
def test_scheduler_timing_rate_schedules(monkeypatch, component, ok) -> None:
    """A rate() schedule is safe only for a component the script never collides with."""
    _at(monkeypatch, 9, 0)

    class _S:
        def list_schedules(self, **kw):
            return {"Schedules": [{"Name": "r", "Target": {"Arn": _FAKE_FN_ARN}}]}

        def get_schedule(self, **kw):
            return {"State": "ENABLED", "ScheduleExpression": "rate(5 minutes)",
                    "ScheduleExpressionTimezone": "UTC",
                    "Target": {"Input": json.dumps({"component": component})}}
    assert _check_scheduler_timing(_S(), _FAKE_FN_ARN)[0] is ok


def test_scheduler_timing_pages_list_schedules(monkeypatch) -> None:
    """The refusing schedule is on the second page of list_schedules."""
    _at(monkeypatch, 6, 10)

    class _S:
        def list_schedules(self, **kw):
            if not kw.get("NextToken"):
                return {"Schedules": [{"Name": "far", "Target": {"Arn": _FAKE_FN_ARN}}],
                        "NextToken": "p2"}
            return {"Schedules": [{"Name": "near", "Target": {"Arn": _FAKE_FN_ARN}}]}

        def get_schedule(self, Name, **kw):
            hour = 14 if Name == "far" else 6
            return {"State": "ENABLED", "ScheduleExpression": f"cron(0 {hour} * * ? *)",
                    "ScheduleExpressionTimezone": "Etc/UTC", "Target": {"Input": "{}"}}
    ok, msg = _check_scheduler_timing(_S(), _FAKE_FN_ARN)
    assert ok is False and "'near'" in msg


def test_scheduler_timing_disabled_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the only schedule is DISABLED, it is skipped; result is PASS with note."""
    monkeypatch.setattr(
        _mod, "_now",
        lambda: datetime.datetime(2026, 10, 6, 6, 30, 0, tzinfo=datetime.timezone.utc),
    )
    sched = _make_sched_fake(
        [{"Name": "rebal", "GroupName": "default", "Target": {"Arn": _FAKE_FN_ARN}}],
        cron_hour=6, cron_minute=0,
        state="DISABLED",
    )
    ok, msg = _check_scheduler_timing(sched, _FAKE_FN_ARN)
    assert ok is True
    assert "disabled skipped" in msg


# ---------------------------------------------------------------------------
# Botocore guard tests (C1 + W6)
#
# Strategy: real boto3 clients built through the script's _client, pointed at a dead
# loopback endpoint (the repo conftest passes loopback through and refuses every real AWS
# host). The guard fires in before-parameter-build, before any request, so:
#   - blocked ops raise GuardViolation
#   - allowed ops reach the transport and fail with EndpointConnectionError
# ---------------------------------------------------------------------------

import botocore.config as _bcc
import botocore.exceptions as _bce

_GUARD_STAGE = "staging"
_GUARD_WG = "cvx-staging-analytics"
_GUARD_FN = "vsa-staging-tier2-health-agent-us-west-2"

GuardViolation = _mod.GuardViolation


@pytest.fixture()
def _guard_ro(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read-only guarded session + instant HTTP failure fixture."""
    monkeypatch.setattr(_mod, "_SESSION", None)
    _mod._init_session(
        allow_writes=False,
        stage=_GUARD_STAGE,
        workgroup=_GUARD_WG,
        fn_name=_GUARD_FN,
    )
    yield
    monkeypatch.setattr(_mod, "_SESSION", None)


@pytest.fixture()
def _guard_rw(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read-write guarded session + instant HTTP failure fixture."""
    monkeypatch.setattr(_mod, "_SESSION", None)
    _mod._init_session(
        allow_writes=True,
        stage=_GUARD_STAGE,
        workgroup=_GUARD_WG,
        fn_name=_GUARD_FN,
    )
    yield
    monkeypatch.setattr(_mod, "_SESSION", None)


def _c(service: str, region: str = "us-east-1") -> Any:
    """Return a real boto3 client through the guarded session, at a dead loopback endpoint."""
    return _mod._client(service, region, endpoint_url="http://127.0.0.1:1",
                        config=_bcc.Config(retries={"total_max_attempts": 1},
                                           connect_timeout=1, read_timeout=1))


# --- _client raises RuntimeError before _init_session ---

def test_guard_client_before_init(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_mod, "_SESSION", None)
    with pytest.raises(RuntimeError, match="_init_session"):
        _mod._client("sts", "us-east-1")


# --- Always-allowed: read-prefix ops (Describe-prefix) ---

def test_guard_dynamodb_describe_allowed(_guard_ro: None) -> None:
    ddb = _c("dynamodb")
    with pytest.raises(_bce.EndpointConnectionError):
        ddb.describe_table(TableName="test")


# --- Always-allowed: sts GetCallerIdentity ---

def test_guard_sts_get_caller_identity_allowed(_guard_ro: None) -> None:
    sts = _c("sts")
    with pytest.raises(_bce.EndpointConnectionError):
        sts.get_caller_identity()


# --- dynamodb Scan and Query allowed; PutItem blocked ---

def test_guard_dynamodb_scan_allowed(_guard_ro: None) -> None:
    ddb = _c("dynamodb")
    with pytest.raises(_bce.EndpointConnectionError):
        ddb.scan(TableName="test")


def test_guard_dynamodb_query_allowed(_guard_ro: None) -> None:
    ddb = _c("dynamodb")
    with pytest.raises(_bce.EndpointConnectionError):
        ddb.query(
            TableName="test",
            KeyConditionExpression="pk = :v",
            ExpressionAttributeValues={":v": {"S": "x"}},
        )


def test_guard_dynamodb_put_item_blocked(_guard_ro: None) -> None:
    ddb = _c("dynamodb")
    with pytest.raises(GuardViolation, match="blocked operation"):
        ddb.put_item(TableName="test", Item={"pk": {"S": "x"}})


# --- s3 HeadObject allowed; PutObject + DeleteObject blocked ---

def test_guard_s3_head_object_allowed(_guard_ro: None) -> None:
    s3 = _c("s3")
    with pytest.raises(_bce.EndpointConnectionError):
        s3.head_object(Bucket="b", Key="k")


def test_guard_s3_put_object_blocked(_guard_ro: None) -> None:
    s3 = _c("s3")
    with pytest.raises(GuardViolation):
        s3.put_object(Bucket="b", Key="k", Body=b"x")


def test_guard_s3_delete_object_blocked(_guard_ro: None) -> None:
    s3 = _c("s3")
    with pytest.raises(GuardViolation):
        s3.delete_object(Bucket="b", Key="k")


# --- cloudwatch-logs FilterLogEvents allowed (botocore service id: cloudwatch-logs) ---

def test_guard_logs_filter_log_events_allowed(_guard_ro: None) -> None:
    logs = _c("logs")
    with pytest.raises(_bce.EndpointConnectionError):
        logs.filter_log_events(logGroupName="/aws/lambda/fn", filterPattern="ERROR")


# --- Athena: SELECT/SHOW + correct workgroup allowed; DELETE/wrong-wg/ResultConfig blocked ---

def test_guard_athena_start_select_allowed(_guard_ro: None) -> None:
    ath = _c("athena", "us-east-1")
    with pytest.raises(_bce.EndpointConnectionError):
        ath.start_query_execution(QueryString="SELECT 1", WorkGroup=_GUARD_WG)


def test_guard_athena_start_show_allowed(_guard_ro: None) -> None:
    ath = _c("athena", "us-east-1")
    with pytest.raises(_bce.EndpointConnectionError):
        ath.start_query_execution(
            QueryString="SHOW TABLES IN db", WorkGroup=_GUARD_WG
        )


def test_guard_athena_start_delete_blocked(_guard_ro: None) -> None:
    ath = _c("athena", "us-east-1")
    with pytest.raises(GuardViolation, match="StartQueryExecution blocked"):
        ath.start_query_execution(QueryString="DELETE FROM db.tbl", WorkGroup=_GUARD_WG)


def test_guard_athena_start_wrong_workgroup_blocked(_guard_ro: None) -> None:
    ath = _c("athena", "us-east-1")
    with pytest.raises(GuardViolation, match="StartQueryExecution blocked"):
        ath.start_query_execution(QueryString="SELECT 1", WorkGroup="other-wg")


def test_guard_athena_start_result_config_blocked(_guard_ro: None) -> None:
    ath = _c("athena", "us-east-1")
    with pytest.raises(GuardViolation, match="StartQueryExecution blocked"):
        ath.start_query_execution(
            QueryString="SELECT 1",
            WorkGroup=_GUARD_WG,
            ResultConfiguration={"OutputLocation": "s3://bucket/prefix/"},
        )


# --- Lambda Invoke: correct params allowed in rw mode; wrong fn/payload blocked; ro blocked ---

def test_guard_lambda_invoke_allowed_write_mode(_guard_rw: None) -> None:
    lam = _c("lambda", "us-west-2")
    with pytest.raises(_bce.EndpointConnectionError):
        lam.invoke(
            FunctionName=_GUARD_FN,
            InvocationType="RequestResponse",
            Payload=json.dumps({"component": "rebalance"}).encode(),
        )


def test_guard_lambda_invoke_wrong_fn_blocked(_guard_rw: None) -> None:
    lam = _c("lambda", "us-west-2")
    with pytest.raises(GuardViolation, match="Invoke blocked"):
        lam.invoke(
            FunctionName="vsa-staging-tier2-health-agent-us-east-1",
            InvocationType="RequestResponse",
            Payload=json.dumps({"component": "rebalance"}).encode(),
        )


def test_guard_lambda_invoke_wrong_payload_blocked(_guard_rw: None) -> None:
    lam = _c("lambda", "us-west-2")
    with pytest.raises(GuardViolation, match="Invoke blocked"):
        lam.invoke(
            FunctionName=_GUARD_FN,
            InvocationType="RequestResponse",
            Payload=json.dumps({"component": "health"}).encode(),
        )


def test_guard_lambda_invoke_blocked_in_read_only_mode(_guard_ro: None) -> None:
    lam = _c("lambda", "us-west-2")
    with pytest.raises(GuardViolation):
        lam.invoke(
            FunctionName=_GUARD_FN,
            InvocationType="RequestResponse",
            Payload=json.dumps({"component": "rebalance"}).encode(),
        )


# --- SSM PutParameter: correct params allowed in rw; wrong name blocked; ro blocked ---

def test_guard_ssm_put_parameter_allowed_write_mode(_guard_rw: None) -> None:
    ssm = _c("ssm", "us-west-2")
    with pytest.raises(_bce.EndpointConnectionError):
        ssm.put_parameter(
            Name=f"/vsa/{_GUARD_STAGE}/avx/fi-rebalancing/policy",
            Value='{"rebalance": true}',
            Type="String",
            Overwrite=True,
        )


def test_guard_ssm_put_parameter_wrong_name_blocked(_guard_rw: None) -> None:
    ssm = _c("ssm", "us-west-2")
    with pytest.raises(GuardViolation, match="PutParameter blocked"):
        ssm.put_parameter(
            Name="/vsa/staging/avx/fi-rebalancing/other-param",
            Value="x",
            Type="String",
            Overwrite=True,
        )


def test_guard_ssm_put_parameter_blocked_read_only_mode(_guard_ro: None) -> None:
    ssm = _c("ssm", "us-west-2")
    with pytest.raises(GuardViolation):
        ssm.put_parameter(
            Name=f"/vsa/{_GUARD_STAGE}/avx/fi-rebalancing/policy",
            Value='{"rebalance": true}',
            Type="String",
            Overwrite=True,
        )


# --- Cognito AdminCreateUser: correct username allowed; wrong username blocked ---

def test_guard_cognito_admin_create_user_allowed_write_mode(_guard_rw: None) -> None:
    idp = _c("cognito-idp", "us-west-2")
    with pytest.raises(_bce.EndpointConnectionError):
        idp.admin_create_user(
            UserPoolId="us-west-2_EXAMPLE",
            Username="livetest-fi-rebal-g4-alice",
        )


def test_guard_cognito_admin_create_user_wrong_username_blocked(
    _guard_rw: None,
) -> None:
    idp = _c("cognito-idp", "us-west-2")
    with pytest.raises(GuardViolation, match="AdminCreateUser blocked"):
        idp.admin_create_user(
            UserPoolId="us-west-2_EXAMPLE",
            Username="prod-user-alice",
        )



# --- W-C: every Cognito call G4b makes, through the real guard ---

# Shorter than a real pool id (8+ characters after the region) on purpose: the publish
# scanner flags pool-id-shaped values in shipped files, made-up ones included.
_POOL = "us-west-2_EXAMPLE"
_TEMP_USER = "livetest-fi-rebal-g4-alice"
#: G4b's four username-gated writes: (client method, API operation, other required params).
_COGNITO_USER_WRITES = [
    ("admin_create_user", "AdminCreateUser", {}),
    ("admin_set_user_password", "AdminSetUserPassword",
     {"Password": "unused-at-dead-endpoint", "Permanent": True}),
    ("admin_add_user_to_group", "AdminAddUserToGroup", {"GroupName": "fleet-operator"}),
    ("admin_delete_user", "AdminDeleteUser", {}),
]


def _raised(call: Any) -> BaseException:
    """What *call* raised; the dead endpoint makes every allowed call raise."""
    try:
        call()
    except Exception as exc:  # noqa: BLE001
        return exc
    raise AssertionError("the call returned; the guard or the dead endpoint should stop it")


@pytest.mark.parametrize("method,op,extra", _COGNITO_USER_WRITES)
def test_guard_cognito_write_reaches_the_network_for_the_temp_user(
        _guard_rw: None, method: str, op: str, extra: dict) -> None:
    idp = _c("cognito-idp", "us-west-2")
    exc = _raised(lambda: getattr(idp, method)(UserPoolId=_POOL, Username=_TEMP_USER, **extra))
    assert type(exc) is _bce.EndpointConnectionError, f"{op}: {type(exc).__name__}: {exc}"


@pytest.mark.parametrize("user", ["alice", "prod-user-alice", "x-livetest-fi-rebal-g4-alice",
                                  "livetest-fi-rebal-g4malicious"])
@pytest.mark.parametrize("method,op,extra", _COGNITO_USER_WRITES)
def test_guard_cognito_write_blocked_for_any_other_user(
        _guard_rw: None, method: str, op: str, extra: dict, user: str) -> None:
    """The username prefix is all that stands between G4b and a real CMS user
    (AdminDeleteUser above all)."""
    idp = _c("cognito-idp", "us-west-2")
    exc = _raised(lambda: getattr(idp, method)(UserPoolId=_POOL, Username=user, **extra))
    assert type(exc) is GuardViolation and f"cognito {op} blocked" in str(exc), (
        f"{op} {user!r}: {type(exc).__name__}: {exc}")


@pytest.mark.parametrize("method,op,extra", _COGNITO_USER_WRITES)
def test_guard_cognito_write_blocked_read_only(
        _guard_ro: None, method: str, op: str, extra: dict) -> None:
    idp = _c("cognito-idp", "us-west-2")
    exc = _raised(lambda: getattr(idp, method)(UserPoolId=_POOL, Username=_TEMP_USER, **extra))
    assert type(exc) is GuardViolation and "blocked operation" in str(exc), (
        f"{op}: {type(exc).__name__}: {exc}")


def _initiate_auth(idp: Any) -> None:
    idp.initiate_auth(AuthFlow="USER_PASSWORD_AUTH", ClientId="testclient",
                      AuthParameters={"USERNAME": _TEMP_USER, "PASSWORD": "unused-at-dead-endpoint"})


def test_guard_cognito_initiate_auth_allowed_write_mode(_guard_rw: None) -> None:
    exc = _raised(lambda: _initiate_auth(_c("cognito-idp", "us-west-2")))
    assert type(exc) is _bce.EndpointConnectionError, f"{type(exc).__name__}: {exc}"


def test_guard_cognito_initiate_auth_blocked_read_only(_guard_ro: None) -> None:
    exc = _raised(lambda: _initiate_auth(_c("cognito-idp", "us-west-2")))
    assert type(exc) is GuardViolation and "blocked operation" in str(exc), (
        f"{type(exc).__name__}: {exc}")


@pytest.mark.parametrize("mode", ["_guard_ro", "_guard_rw"])
def test_guard_cognito_admin_get_user_is_a_read(mode: str, request: pytest.FixtureRequest) -> None:
    request.getfixturevalue(mode)
    idp = _c("cognito-idp", "us-west-2")
    exc = _raised(lambda: idp.admin_get_user(UserPoolId=_POOL, Username=_TEMP_USER))
    assert type(exc) is _bce.EndpointConnectionError, f"{type(exc).__name__}: {exc}"


# --- security c1 survivors: the guard stays as narrow as reviewed ---

def test_guard_athena_with_clause_blocked(_guard_ro: None) -> None:
    """Only SELECT/SHOW statements start (SM1): the script sends no WITH statement."""
    exc = _raised(lambda: _c("athena").start_query_execution(
        QueryString="WITH x AS (SELECT 1) SELECT * FROM x", WorkGroup=_GUARD_WG))
    assert type(exc) is GuardViolation and "StartQueryExecution blocked" in str(exc), (
        f"{type(exc).__name__}: {exc}")


def test_guard_generate_operations_blocked(_guard_rw: None) -> None:
    """No read prefix covers Generate* (SM2): a data key is a secret the script never needs."""
    exc = _raised(lambda: _c("kms", "us-west-2").generate_data_key(KeyId="alias/x",
                                                                   KeySpec="AES_256"))
    assert type(exc) is GuardViolation and "blocked operation" in str(exc), (
        f"{type(exc).__name__}: {exc}")


def test_guard_ssm_put_parameter_securestring_blocked(_guard_rw: None) -> None:
    """The policy override is a String parameter (SM3)."""
    exc = _raised(lambda: _c("ssm", "us-west-2").put_parameter(
        Name=f"/vsa/{_GUARD_STAGE}/avx/fi-rebalancing/policy", Value='{"bad": true}',
        Type="SecureString", Overwrite=True))
    assert type(exc) is GuardViolation and "PutParameter blocked" in str(exc), (
        f"{type(exc).__name__}: {exc}")


# --- W-F (security c1): every Athena statement the script sends passes its own guard ---
# A real Athena client through the guarded session at a dead endpoint: an admitted statement
# fails to connect, a blocked one raises GuardViolation before any request.

def _dead_athena() -> Any:
    return _c("athena", "us-east-1")


def _recorded(results: list, name: str) -> str:
    rows = [d for n, _s, d in results if n == name]
    assert len(rows) == 1, results
    return rows[0]


def _admitted(detail: str) -> None:
    assert "blocked" not in detail and "GuardViolation" not in detail, detail
    assert "Could not connect to the endpoint URL" in detail, detail


def test_guard_admits_a1_statements(_guard_ro: None) -> None:
    results: list = []
    _mod.check_a1_tables(_dead_athena(), "adp_staging_fleet_rebalance", _GUARD_WG, results)
    _admitted(_recorded(results, "A1 tables"))


def test_guard_admits_a2_statements(_guard_ro: None) -> None:
    results: list = []
    _mod.check_a2_dates(_dead_athena(), "adp_staging_fleet_rebalance", _GUARD_WG, None, results)
    _admitted(_recorded(results, "A2 dates"))


def test_guard_admits_the_a6_statement(_guard_ro: None) -> None:
    results: list = []
    _mod.check_a6_vins(_dead_athena(), "staging", _GUARD_WG, results)
    _admitted(_recorded(results, "A6 rental VINs"))


class _RecordingAthena:
    """Answers every statement with one row of ones and keeps the SQL, so a check runs past
    its first query; each kept statement is then sent through the real guard."""

    def __init__(self) -> None:
        self.sql: list[str] = []

    def start_query_execution(self, *, QueryString: str, WorkGroup: str) -> dict:
        self.sql.append(QueryString)
        return {"QueryExecutionId": str(len(self.sql))}

    def get_query_execution(self, *, QueryExecutionId: str) -> dict:
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

    def get_query_results(self, *, QueryExecutionId: str, NextToken: str | None = None) -> dict:
        row = {"Data": [{"VarCharValue": "1"}, {"VarCharValue": "1"}, {"VarCharValue": "1"}]}
        return {"ResultSet": {"Rows": [{"Data": [{"VarCharValue": "h"}] * 3}, row]}}


def _every_statement_admitted(statements: list[str], want: int) -> None:
    assert len(statements) == want, statements
    for sql in statements:
        exc = _raised(lambda: _dead_athena().start_query_execution(QueryString=sql,
                                                                   WorkGroup=_GUARD_WG))
        assert type(exc) is _bce.EndpointConnectionError, f"{sql[:80]!r}: {type(exc).__name__}: {exc}"


def test_guard_admits_both_a2_statements(_guard_ro: None) -> None:
    """Security c2 S-C2A2b (SC2B): A2's second statement, which the dead-endpoint test above
    never reaches because the first fails."""
    ath = _RecordingAthena()
    _mod.check_a2_dates(ath, "adp_staging_fleet_rebalance", _GUARD_WG, None, [])
    _every_statement_admitted(ath.sql, 2)


def test_guard_admits_a7s_ssm_read(_guard_ro: None) -> None:
    """Security c2 S-C2A7ssm: A7's GetParameter, past its Athena read, through the guard."""
    results: list = []
    _mod.check_a7_policy(_RecordingAthena(), _c("ssm", "us-west-2"),
                         "adp_staging_fleet_rebalance", _GUARD_WG, "staging", results)
    detail = _recorded(results, "A7 policy")
    assert "cannot read SSM parameter" in detail, detail
    _admitted(detail)


def test_guard_admits_the_a7_statement(_guard_ro: None) -> None:
    results: list = []
    _mod.check_a7_policy(_dead_athena(), None, "adp_staging_fleet_rebalance", _GUARD_WG,
                         "staging", results)
    _admitted(_recorded(results, "A7 policy"))


@pytest.mark.parametrize("call", [
    lambda ath: _mod._read_gaps(ath, "staging", _GUARD_WG),
    lambda ath: _mod._forecast_rows(ath, "staging", "2026-10-15", _GUARD_WG),
    lambda ath: _mod._snapshot_supply(ath, "staging", "2026-10-15", _GUARD_WG),
], ids=["g1_gaps", "g2_forecast", "g2_snapshot_supply"])
def test_guard_admits_the_agent_part_statements(_guard_ro: None, call: Any) -> None:
    exc = _raised(lambda: call(_dead_athena()))
    assert type(exc) is _bce.EndpointConnectionError, f"{type(exc).__name__}: {exc}"


def test_guard_lambda_invoke_event_type_blocked(_guard_rw: None) -> None:
    lam = _c("lambda", "us-west-2")
    with pytest.raises(GuardViolation, match="Invoke blocked"):
        lam.invoke(
            FunctionName=_GUARD_FN,
            InvocationType="Event",
            Payload=json.dumps({"component": "rebalance"}).encode(),
        )


def test_guard_cognito_admin_create_user_blocked_read_only_mode(_guard_ro: None) -> None:
    idp = _c("cognito-idp", "us-west-2")
    with pytest.raises(GuardViolation):
        idp.admin_create_user(
            UserPoolId="us-west-2_EXAMPLE",
            Username="livetest-fi-rebal-g4-alice",
        )


def test_guard_ssm_put_parameter_without_overwrite_blocked(_guard_rw: None) -> None:
    ssm = _c("ssm", "us-west-2")
    with pytest.raises(GuardViolation, match="PutParameter blocked"):
        ssm.put_parameter(
            Name=f"/vsa/{_GUARD_STAGE}/avx/fi-rebalancing/policy",
            Value='{"rebalance": true}',
            Type="String",
        )


# ---------------------------------------------------------------------------
# _items_of_run: run records carry millisecond started_at, Findings whole-second
# computed_at (issue 2026-10-27-fi-sweep-avx-ms-run-start-readers)
# ---------------------------------------------------------------------------


def _run_item(fid: str, computed_at: str) -> dict:
    return {"finding_id": fid, "finding_kind": "fleet.rebalance.move",
            "scope": {"fleet_id": _mod._RENTAL_FLEET}, "computed_at": computed_at,
            "expires_at": "2026-11-03T00:00:00Z"}


def test_items_of_run_keeps_an_item_computed_in_the_runs_first_second() -> None:
    run = {"started_at": "2026-10-27T11:36:01.881Z", "finished_at": "2026-10-27T11:36:02.010Z"}
    now = datetime.datetime(2026, 10, 27, 12, 0, tzinfo=datetime.timezone.utc)
    items, bad = _mod._items_of_run(
        [_run_item("first-second", "2026-10-27T11:36:01Z"),
         _run_item("last-second", "2026-10-27T11:36:02Z")], run, now)
    assert bad == []
    assert sorted(f["finding_id"] for f in items) == ["first-second", "last-second"]


def test_items_of_run_still_drops_an_item_from_the_second_before_the_run() -> None:
    run = {"started_at": "2026-10-27T11:36:01.881Z", "finished_at": "2026-10-27T11:36:02.010Z"}
    now = datetime.datetime(2026, 10, 27, 12, 0, tzinfo=datetime.timezone.utc)
    items, _ = _mod._items_of_run(
        [_run_item("earlier", "2026-10-27T11:36:00Z"),
         _run_item("later", "2026-10-27T11:36:03Z"),
         _run_item("own", "2026-10-27T11:36:01Z")], run, now)
    assert [f["finding_id"] for f in items] == ["own"]
