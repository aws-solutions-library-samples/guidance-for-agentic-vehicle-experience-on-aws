"""Tests for items 5+6 of scripts/live-verify-fi-fleet-health.py.

Fakes built from the real code/data shapes (source citations below).
A positive control per property; at least one bad world per property.

Source citations:
  score_rerun:           agents/tier2/runner.py:1420,1422
                         (model_calls, tokens_used in vsa-{stage}-avx-agent-runs)
  score_new_findings:    agents/fi_fleet_health/runner.py:31 re-export of same_hash_model_written;
                         agents/fi_recall_warranty/runner.py:113 (skip predicate definition)
  _scan_all_fh_finding_ids: lambdas/api-avx-findings/write.py:18,24
                         (AGENT_ID='agent-fi-fleet-health', FINDING_KIND='fleet.health.service')
  _run_started_ats:      agents/tier2/run_records.py:120 pk=agent_component sk=started_at
  _poll_run_to_completion: run_records.py:120; agents/tier2/runner.py:1635,1636
  _check_scheduler_timing: infrastructure/lib/vsa-tier2-stack.ts:2030
                         (service cron(40 5 * * ? *)); diagnostics rate(5 minutes)
  _item_5 GET /findings/fleet/{id}: lambdas/api-avx-findings/handler.py:2435
                         (check_fleet_scope -> 403 {"error": "insufficient scope"})
  _item_5 approve 403:   handler.py:1275-1276
                         ({"error": "insufficient scope to approve this finding"})
  _item_5 decline 403:   handler.py:1613,1618
                         ({"error": "insufficient scope to decline this finding"})
  _query_by_fleet_fh:    scripts/live-verify-fi-fleet-health.py:1006 (by_fleet GSI)

Run (AWS env stripped):
    env -u AWS_PROFILE AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing \\
      PYTHONDONTWRITEBYTECODE=1 \\
      .venv/bin/python -m pytest scripts/tests/test_live_verify_fi_fleet_health_item56.py -v
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Load module under test
# ---------------------------------------------------------------------------

_SCRIPT = Path(__file__).resolve().parents[1] / "live-verify-fi-fleet-health.py"
_spec = importlib.util.spec_from_file_location("live_verify_fi_fleet_health", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

Results = _mod.Results
score_rerun = _mod.score_rerun
score_new_findings = _mod.score_new_findings
_scan_all_fh_finding_ids = _mod._scan_all_fh_finding_ids
_run_started_ats = _mod._run_started_ats
_poll_run_to_completion = _mod._poll_run_to_completion
_check_scheduler_timing = _mod._check_scheduler_timing
_item_5 = _mod._item_5
_item_6 = _mod._item_6
_AGENT_FI_FH = _mod._AGENT_FI_FH
_FH_FINDING_KIND = _mod._FH_FINDING_KIND
_FH_RUN_PK = _mod._FH_RUN_PK


# ---------------------------------------------------------------------------
# Fixtures: fake finding shapes from write.py:39 (finding schema)
# ---------------------------------------------------------------------------

_FLT_A = "demo-rental-carshare"        # managed fleet A
_FLT_B = "demo-lastmile-delivery"       # managed fleet B
_FLT_B_LT = "LIVETEST-B"               # a bad fleet that must never appear in A's list


def _finding(fleet_id: str, idx: int = 1) -> dict:
    """Fake fleet.health.service Finding in DDB high-level shape.

    Source: lambdas/api-avx-findings/write.py:39 build_finding field list.
    fleet_id is denormalized as top-level attribute (vsa-tier2-stack.ts:243).
    """
    return {
        "finding_id": f"FIND-{fleet_id}-{idx:04d}",
        "finding_kind": _FH_FINDING_KIND,
        "agent_id": _AGENT_FI_FH,
        "fleet_id": fleet_id,
        "scope": {"vin": f"VIN{fleet_id.replace('-','').upper()[:6]}{idx:04d}XXXXXXXXXXX"},
        "severity": "soon",
        "computed_at": "2026-10-15T03:00:00Z",
        "expires_at": "2999-12-31T00:00:00Z",
        "recommended_action": {
            "action_kind": "book_fleet_service",
            "target_system": "dms",
            "parameters": {"dealer_id": f"DEALER-{fleet_id[:4].upper()}", "needs": []},
        },
    }


def _run_record(
    started_at: str = "2026-10-15T05:45:00Z",
    status: str = "completed",
    model_calls: int = 0,
    tokens_used: int = 0,
    artifacts_written: int = 0,
) -> dict:
    """Fake run record from vsa-{stage}-avx-agent-runs.
    Source: agents/tier2/runner.py:1420,1422 (model_calls, tokens_used).
    """
    return {
        "agent_component": _FH_RUN_PK,
        "started_at": started_at,
        "status": status,
        "model_calls": model_calls,
        "tokens_used": tokens_used,
        "artifacts_written": artifacts_written,
    }


def _ddb_item(d: dict) -> dict:
    """Wrap a Python dict into DynamoDB low-level item shape."""
    from boto3.dynamodb.types import TypeSerializer
    ser = TypeSerializer()
    return {k: ser.serialize(v) for k, v in d.items()}


# ===========================================================================
# score_rerun
# ===========================================================================

class TestScoreRerun:
    def test_positive_control_zeros_pass(self):
        """S1: model_calls=0, tokens_used=0 → PASS."""
        ok, detail = score_rerun({"model_calls": 0, "tokens_used": 0, "artifacts_written": 0})
        assert ok
        assert "failed" not in detail.lower() and "error" not in detail.lower()

    def test_nonzero_artifacts_written_passes(self):
        """S1b: artifacts_written can be nonzero (refreshes allowed); still PASS.
        Source: agents/tier2/runner.py comment on refresh_from_live.
        """
        ok, _ = score_rerun({"model_calls": 0, "tokens_used": 0, "artifacts_written": 7})
        assert ok

    def test_model_calls_one_fails(self):
        """S2: model_calls=1 → FAIL (mutation: one model call slipped through)."""
        ok, detail = score_rerun({"model_calls": 1, "tokens_used": 0, "artifacts_written": 0})
        assert not ok
        assert "model_calls=1" in detail

    def test_tokens_used_one_fails(self):
        """S3: tokens_used=1 → FAIL."""
        ok, _ = score_rerun({"model_calls": 0, "tokens_used": 1, "artifacts_written": 0})
        assert not ok

    def test_missing_fields_default_negative(self):
        """S4: missing fields default to -1 (sentinel) → FAIL; not silently PASS."""
        ok, detail = score_rerun({})
        assert not ok
        assert "model_calls=-1" in detail


# ===========================================================================
# score_new_findings
# ===========================================================================

class TestScoreNewFindings:
    def test_positive_no_new_ids(self):
        """N1: same before/after set → PASS."""
        ids = {"FIND-001", "FIND-002"}
        ok, detail = score_new_findings(ids, ids)
        assert ok
        assert "0 new" in detail

    def test_subset_after_no_new(self):
        """N2: after is a subset of before → PASS (deletions are fine)."""
        ok, _ = score_new_findings({"FIND-001", "FIND-002"}, {"FIND-001"})
        assert ok

    def test_new_finding_id_fails(self):
        """N3 bad world: a fresh id in after-set → FAIL.
        Mutation: same_hash_model_written returned False and a new item was written.
        """
        before = {"FIND-001"}
        after = {"FIND-001", "FIND-NEW-999"}
        ok, detail = score_new_findings(before, after)
        assert not ok
        assert "FIND-NEW-999" in detail

    def test_empty_before_new_id_fails(self):
        """N4: empty before, one after → FAIL."""
        ok, _ = score_new_findings(set(), {"FIND-001"})
        assert not ok


# ===========================================================================
# _scan_all_fh_finding_ids (unit — fake low-level DDB Scan responses)
# ===========================================================================

class TestScanAllFhFindingIds:
    def _make_ddb(self, pages: list[list[dict]]) -> Any:
        """Fake low-level DDB client returning pages of items for .scan()."""
        ddb = MagicMock()
        responses = []
        for i, items in enumerate(pages):
            resp: dict[str, Any] = {
                "Items": [_ddb_item(it) for it in items],
            }
            if i < len(pages) - 1:
                resp["LastEvaluatedKey"] = {"finding_id": {"S": f"FIND-PAGE-{i}"}}
            responses.append(resp)
        ddb.scan.side_effect = responses
        return ddb

    def test_positive_single_page(self):
        """SC1: one page with two fleet-health findings → both IDs returned."""
        f1 = _finding(_FLT_A, 1)
        f2 = _finding(_FLT_A, 2)
        ddb = self._make_ddb([[f1, f2]])
        ids = _scan_all_fh_finding_ids(ddb, "vsa-staging-avx-findings")
        assert ids == {f1["finding_id"], f2["finding_id"]}

    def test_paged_two_pages(self):
        """SC2: two pages → all IDs collected."""
        f1 = _finding(_FLT_A, 1)
        f2 = _finding(_FLT_B, 1)
        ddb = self._make_ddb([[f1], [f2]])
        ids = _scan_all_fh_finding_ids(ddb, "vsa-staging-avx-findings")
        assert ids == {f1["finding_id"], f2["finding_id"]}

    def test_empty_table(self):
        """SC3: empty scan → empty set."""
        ddb = self._make_ddb([[]])
        ids = _scan_all_fh_finding_ids(ddb, "vsa-staging-avx-findings")
        assert ids == set()


# ===========================================================================
# _run_started_ats (unit — fake low-level DDB Query responses)
# ===========================================================================

class TestRunStartedAts:
    def _make_ddb(self, items: list[dict]) -> Any:
        ddb = MagicMock()
        ddb.query.return_value = {
            "Items": [_ddb_item({"started_at": it["started_at"]}) for it in items],
        }
        return ddb

    def test_positive_returns_started_ats(self):
        """RA1: two run records → both started_ats returned."""
        ddb = self._make_ddb([
            {"started_at": "2026-10-15T02:00:00Z"},
            {"started_at": "2026-10-15T05:45:00Z"},
        ])
        out = _run_started_ats(ddb, "vsa-staging-avx-agent-runs", _FH_RUN_PK, "2026-10-15T00:00:00Z")
        assert out == {"2026-10-15T02:00:00Z", "2026-10-15T05:45:00Z"}

    def test_empty_returns_empty_set(self):
        """RA2: no records → empty set."""
        ddb = MagicMock()
        ddb.query.return_value = {"Items": []}
        out = _run_started_ats(ddb, "vsa-staging-avx-agent-runs", _FH_RUN_PK, "2026-10-15T00:00:00Z")
        assert out == set()


# ===========================================================================
# _poll_run_to_completion (unit — fake low-level DDB Query responses)
# ===========================================================================

class TestPollRunToCompletion:
    def _make_ddb(self, items_per_call: list[list[dict]]) -> Any:
        ddb = MagicMock()
        ddb.query.side_effect = [
            {"Items": [_ddb_item(it) for it in items]}
            for items in items_per_call
        ]
        return ddb

    def test_positive_completed_record_returned(self):
        """PR1: completed record not in exclude → returned immediately."""
        rec = _run_record("2026-10-15T05:45:10Z", "completed", 0, 0, 0)
        ddb = self._make_ddb([[rec]])
        result = _poll_run_to_completion(
            ddb, "vsa-staging-avx-agent-runs", _FH_RUN_PK,
            "2026-10-15T05:45:00Z", timeout_s=0.5, poll_s=0,
        )
        assert result is not None
        assert result.get("status") == "completed"

    def test_older_run_record_excluded(self):
        """PR2 bad world: older run record whose started_at is in exclude → skipped; returns None.
        Mutation: removing the exclude check returns the old record as this run's record.
        """
        old_rec = _run_record("2026-10-15T05:45:10Z", "completed", 0, 0, 0)
        ddb = MagicMock()
        ddb.query.return_value = {"Items": [_ddb_item(old_rec)]}
        result = _poll_run_to_completion(
            ddb, "vsa-staging-avx-agent-runs", _FH_RUN_PK,
            "2026-10-15T05:45:00Z", timeout_s=0.1, poll_s=0,
            exclude={"2026-10-15T05:45:10Z"},  # this run's started_at was pre-existing
        )
        # The exclude check causes the record to be skipped; poll times out.
        assert result is None

    def test_aborted_record_returned(self):
        """PR3: aborted record (also terminal) is returned."""
        rec = _run_record("2026-10-15T05:45:10Z", "aborted", 0, 0, 0)
        ddb = MagicMock()
        ddb.query.return_value = {"Items": [_ddb_item(rec)]}
        result = _poll_run_to_completion(
            ddb, "vsa-staging-avx-agent-runs", _FH_RUN_PK,
            "2026-10-15T05:45:00Z", timeout_s=0.5, poll_s=0,
        )
        assert result is not None
        assert result.get("status") == "aborted"

    def test_timeout_returns_none(self):
        """PR4: no completed record within timeout → None."""
        rec = _run_record("2026-10-15T05:45:10Z", "in_progress", 0, 0, 0)
        ddb = MagicMock()
        ddb.query.return_value = {"Items": [_ddb_item(rec)]}
        result = _poll_run_to_completion(
            ddb, "vsa-staging-avx-agent-runs", _FH_RUN_PK,
            "2026-10-15T05:45:00Z", timeout_s=0.05, poll_s=0,
        )
        assert result is None


# ===========================================================================
# _check_scheduler_timing (unit — fake EB Scheduler responses)
# ===========================================================================

class TestCheckSchedulerTiming:
    """Source: infrastructure/lib/vsa-tier2-stack.ts:2030 — service at cron(40 5 * * ? *).
    diagnostics at rate(5 minutes). _UTC_ZONES = {"UTC", "Etc/UTC"}.
    """

    def _make_sched(
        self,
        expr: str,
        tz: str = "Etc/UTC",
        state: str = "ENABLED",
        comp: str = "service",
        fn_arn: str = "arn:aws:lambda:us-west-2:123456789012:function:vsa-staging-tier2-health-agent-us-west-2",
    ) -> tuple[Any, str]:
        sched = MagicMock()
        sched.list_schedules.return_value = {
            "Schedules": [{"Name": "sched-1", "GroupName": "default", "Target": {"Arn": fn_arn}}]
        }
        sched.get_schedule.return_value = {
            "State": state,
            "ScheduleExpression": expr,
            "ScheduleExpressionTimezone": tz,
            "Target": {"Arn": fn_arn, "Input": json.dumps({"component": comp})},
        }
        return sched, fn_arn

    def test_positive_cron_far_from_now(self):
        """CS1: cron(40 5 * * ? *), now 12:00Z (>45 min away) → PASS."""
        sched, fn_arn = self._make_sched("cron(40 5 * * ? *)")
        with patch.object(_mod, "_now",
                          return_value=dt.datetime(2026, 10, 15, 12, 0, 0,
                                                   tzinfo=dt.timezone.utc)):
            ok, msg = _check_scheduler_timing(sched, fn_arn)
        assert ok, msg

    def test_cron_within_45_min_refuses(self):
        """CS2 bad world: now 05:30Z is within [-45m,+45m] of 05:40Z cron → FAIL.
        Mutation: removing the delta check allows invokes near the schedule window.
        """
        sched, fn_arn = self._make_sched("cron(40 5 * * ? *)")
        with patch.object(_mod, "_now",
                          return_value=dt.datetime(2026, 10, 15, 5, 30, 0,
                                                   tzinfo=dt.timezone.utc)):
            ok, msg = _check_scheduler_timing(sched, fn_arn)
        assert not ok
        assert "within" in msg.lower() or "05:40" in msg

    def test_rate_diagnostics_passes(self):
        """CS3: rate(5 minutes) with component=diagnostics → PASS (safe rate)."""
        sched, fn_arn = self._make_sched("rate(5 minutes)", comp="diagnostics")
        ok, msg = _check_scheduler_timing(sched, fn_arn)
        assert ok, msg

    def test_rate_unknown_component_refuses(self):
        """CS4 bad world: rate with component=service (not in safe set) → FAIL.
        Mutation: allowing all rate schedules bypasses the safe-component guard.
        """
        sched, fn_arn = self._make_sched("rate(5 minutes)", comp="service")
        ok, _ = _check_scheduler_timing(sched, fn_arn)
        assert not ok

    def test_disabled_schedule_skipped(self):
        """CS5: DISABLED schedule is skipped; remaining enabled schedules checked."""
        sched = MagicMock()
        fn_arn = "arn:aws:lambda:us-west-2:123456789012:function:vsa-staging-tier2-health-agent-us-west-2"
        sched.list_schedules.return_value = {
            "Schedules": [{"Name": "s1", "GroupName": "default", "Target": {"Arn": fn_arn}}]
        }
        sched.get_schedule.return_value = {
            "State": "DISABLED",
            "ScheduleExpression": "cron(40 5 * * ? *)",
            "ScheduleExpressionTimezone": "Etc/UTC",
            "Target": {"Arn": fn_arn, "Input": "{}"},
        }
        ok, msg = _check_scheduler_timing(sched, fn_arn)
        # All matching schedules are DISABLED → summaries is non-empty but all skip
        # The function returns PASS with "0 enabled schedule(s) checked; 1 disabled skipped"
        assert ok
        assert "disabled" in msg.lower()

    def test_no_matching_schedules_refuses(self):
        """CS6: no schedules target this ARN → FAIL (fail closed)."""
        sched = MagicMock()
        sched.list_schedules.return_value = {"Schedules": []}
        ok, msg = _check_scheduler_timing(sched, "arn:aws:lambda:us-west-2:123:function:other")
        assert not ok
        assert "0 schedule" in msg.lower()

    def test_non_utc_timezone_refuses(self):
        """CS7: schedule in America/Chicago → FAIL (not UTC)."""
        sched, fn_arn = self._make_sched("cron(40 5 * * ? *)", tz="America/Chicago")
        ok, msg = _check_scheduler_timing(sched, fn_arn)
        assert not ok
        assert "refusing" in msg.lower() or "America/Chicago" in msg


# ===========================================================================
# _item_5 — integration test with fully mocked AWS clients
# ===========================================================================

class TestItem5:
    """Tests call _item_5 with fakes; each bad world fails exactly the named check."""

    def _base_ctx(self, run: str = "test-run-56") -> dict:
        """Build a minimal ctx dict that item 5 unpacks via **_kw."""
        import tempfile
        manifest = Path(tempfile.mktemp(suffix=".json"))
        manifest.write_text(json.dumps({"run_id": run, "items": {}}))
        return {"run": run, "stage": "staging", "region": "us-west-2", "manifest": manifest}

    def _make_mocks(
        self,
        fleet_a_items: list[dict],  # items returned for fleet A by_fleet GSI
        fleet_b_items: list[dict],  # items returned for fleet B by_fleet GSI
        run: str = "test-run-56",   # used to compute ctrl_id (LIVETEST finding)
        get_fleet_a_resp: tuple[int, dict] = (200, {}),
        get_fleet_b_resp: tuple[int, dict] = (403, {"error": "insufficient scope"}),
        # Default: record_engineering_decision+adp on a VIN-scoped finding → 400
        # 'requires a population-scoped Finding' — a post-scope refusal that proves
        # fleet-A scope without creating any Action.
        # Source: lambdas/api-avx-findings/handler.py:1300-1308 (_ENGINEERING_KINDS check).
        approve_ctrl_resp: tuple[int, dict] = (
            400,
            {"error": "action_kind 'record_engineering_decision' requires a "
                      "population-scoped Finding (scope must have population_id and no vin)"},
        ),
        approve_b_resp: tuple[int, dict] = (403, {"error": "insufficient scope to approve this finding"}),
        decline_ctrl_resp: tuple[int, dict] = (400, {"error": "decline is only for FI queue items (Findings with recommended_action)"}),
        decline_b_resp: tuple[int, dict] = (403, {"error": "insufficient scope to decline this finding"}),
    ) -> tuple:
        """Return (ddb5_mock, api_call_side_effects) for patching _client and Http."""
        fa = fleet_a_items
        fb = fleet_b_items

        # ctrl_id is the LIVETEST finding planted for the 5c-ctrl and 5d-ctrl probes.
        # It uses the run id to match what _item_5 computes (LIVETEST-{run}-FI-FH-5-ctrl).
        ctrl_id = f"LIVETEST-{run}-FI-FH-5-ctrl"
        fb_id = fb[0]["finding_id"] if fb else "FB-001"

        # Build fleet A GET /fleet/A response body. Include ctrl item so ctrl_present
        # passes after the W1 fix (ctrl is planted in DDB but API is mocked here).
        ctrl_item_in_resp = {
            "finding_id": ctrl_id,
            "scope": {"vin": f"LIVETEST-VIN-5C-{run[:8].upper()}"},
        }
        if get_fleet_a_resp[0] == 200 and "findings" not in get_fleet_a_resp[1]:
            get_fleet_a_resp = (200, {"findings": fa + [ctrl_item_in_resp]})

        # Map call signature → response
        def _api_call(method: str, path: str, auth: str, body: dict | None = None):
            if method == "GET" and f"/fleet/{_FLT_A}" in path:
                return get_fleet_a_resp
            if method == "GET" and f"/fleet/{_FLT_B}" in path:
                return get_fleet_b_resp
            if method == "POST" and f"/{ctrl_id}/approve" in path:
                return approve_ctrl_resp
            if method == "POST" and f"/{fb_id}/approve" in path:
                return approve_b_resp
            if method == "POST" and f"/{ctrl_id}/decline" in path:
                return decline_ctrl_resp
            if method == "POST" and f"/{fb_id}/decline" in path:
                return decline_b_resp
            return (404, {"error": "not found"})

        return fa, fb, _api_call

    def _run_item5(self, res: Results, fa: list, fb: list, api_call, ctx: dict) -> None:
        """Patch all external dependencies and run _item_5."""
        from boto3.dynamodb.types import TypeSerializer
        ser = TypeSerializer()

        # Build VIN → fleet_id map from all fixture items (for _vin_fleet_cms mock).
        _vin_to_fleet: dict[str, str] = {}
        for _it in fa + fb:
            _vin = str((_it.get("scope") or {}).get("vin") or "")
            _flt = str(_it.get("fleet_id") or "")
            if _vin and _flt:
                _vin_to_fleet[_vin] = _flt

        def _fake_query(*, TableName, IndexName, KeyConditionExpression,
                        FilterExpression=None, ExpressionAttributeValues,
                        ExclusiveStartKey=None, **kw) -> dict:
            flt = ExpressionAttributeValues.get(":flt", {}).get("S", "")
            vin = ExpressionAttributeValues.get(":vin", {}).get("S", "")
            if flt == _FLT_A:
                items = [{k: ser.serialize(v) for k, v in it.items()} for it in fa]
            elif flt == _FLT_B:
                items = [{k: ser.serialize(v) for k, v in it.items()} for it in fb]
            elif vin and IndexName == "vin-index":
                # _vin_fleet_cms vin-index query: return vehicleId if VIN is known
                if vin in _vin_to_fleet:
                    items = [{"vehicleId": {"S": f"VEH-{vin}"}, "vin": {"S": vin}}]
                else:
                    items = []
            else:
                items = []
            return {"Items": items}

        def _fake_get_item(*, TableName, Key, ProjectionExpression=None, **kw) -> dict:
            # _vin_fleet_cms GetItem: resolve vehicleId → fleetId
            vid = Key.get("vehicleId", {}).get("S", "")
            if vid.startswith("VEH-"):
                vin = vid[4:]
                flt = _vin_to_fleet.get(vin)
                if flt:
                    return {"Item": {"fleetId": {"S": flt}}}
            return {"Item": {}}

        fake_ddb = MagicMock()
        fake_ddb.query.side_effect = _fake_query
        fake_ddb.get_item.side_effect = _fake_get_item

        fake_idp = MagicMock()
        fake_idp.admin_create_user.return_value = {}
        fake_idp.admin_set_user_password.return_value = {}
        fake_idp.admin_add_user_to_group.return_value = {}
        fake_idp.admin_delete_user.return_value = {}
        fake_idp.list_user_pools.return_value = {
            "UserPools": [{"Name": "cms-staging-ui-users", "Id": "us-west-2_POOL"}]
        }
        fake_idp.list_user_pool_clients.return_value = {
            "UserPoolClients": [{"ClientId": "CLIENT123", "ClientName": "cms-staging-app"}]
        }
        fake_idp.describe_user_pool_client.return_value = {
            "UserPoolClient": {"ExplicitAuthFlows": ["ALLOW_USER_PASSWORD_AUTH"]}
        }
        import base64
        import json as _j
        fake_claims = {"sub": "livetest-sub", "cognito:groups": ["fleet-operator"],
                       "custom:fleetIds": _FLT_A}
        payload_b64 = base64.urlsafe_b64encode(_j.dumps(fake_claims).encode()).rstrip(b"=").decode()
        fake_token = f"header.{payload_b64}.sig"
        fake_idp.initiate_auth.return_value = {
            "AuthenticationResult": {"IdToken": fake_token}
        }

        fake_ssm = MagicMock()
        fake_ssm.get_parameter.return_value = {"Parameter": {"Value": "us-west-2_POOL"}}

        fake_cfn = MagicMock()
        fake_cfn.describe_stacks.return_value = {
            "Stacks": [{"Outputs": [{"OutputKey": "RestApiUrl", "OutputValue": "https://fake.api"}]}]
        }

        fake_http_instance = MagicMock()
        fake_http_instance.call.side_effect = api_call

        def _fake_client(service, region, *, allowed_ops, endpoint_url=None, config=None):
            if service == "dynamodb":
                return fake_ddb
            if service == "cognito-idp":
                return fake_idp
            if service == "ssm":
                return fake_ssm
            if service == "cloudformation":
                return fake_cfn
            return MagicMock()

        with patch.object(_mod, "_client", side_effect=_fake_client), \
             patch.object(_mod, "Http", return_value=fake_http_instance):
            _item_5(res, **ctx)

    def _failed_labels(self, res: Results) -> list[str]:
        return [r[0] for r in res.rows if r[1] == "FAIL"]

    def _label_failed(self, res: Results, label: str) -> bool:
        return any(r[0] == label and r[1] == "FAIL" for r in res.rows)

    def test_positive_control_all_pass(self):
        """I5-P: correct world — A-operator sees A items, B is 403 everywhere → PASS.
        ctrl checks: approve ctrl returns 400 'population-scoped' (post-scope refusal);
        decline ctrl returns 400 'FI queue items' (no recommended_action on LIVETEST ctrl finding).
        """
        fa = [_finding(_FLT_A, i) for i in range(1, 3)]
        fb = [_finding(_FLT_B, i) for i in range(1, 2)]
        _, _, api_call = self._make_mocks(
            fa, fb,
            # approve_ctrl defaults to (400, "...population-scoped...") — post-scope 400
            # decline_ctrl defaults to (400, "...FI queue items...") — post-scope 400
        )
        res = Results()
        self._run_item5(res, fa, fb, api_call, self._base_ctx())
        fails = self._failed_labels(res)
        assert not fails, f"unexpected failures: {fails}"

    def test_fleet_b_item_in_fleet_a_list_fails(self):
        """I5-1 bad world: fleet B item appears in GET /fleet/A response → FAIL 5a-items.
        Mutation: wrong_fleet check removed → fleet B item passes undetected.
        Source: handler.py:2435 by_fleet GSI query (fleet_id top-level attribute).

        Must use the same idx (1) as fb so the finding_id is in _fb_ids; idx=99 would
        produce a different finding_id not in _fb_ids and the intersection would be empty.
        """
        fa = [_finding(_FLT_A, 1)]
        fb = [_finding(_FLT_B, 1)]
        poisoned_body = {"findings": [_finding(_FLT_B, 1)]}  # same finding_id as fb[0]

        def api_call(method, path, auth, body=None):
            if method == "GET" and f"/fleet/{_FLT_A}" in path:
                return (200, poisoned_body)
            if method == "GET" and f"/fleet/{_FLT_B}" in path:
                return (403, {"error": "insufficient scope"})
            if "approve" in path:
                return (409, {"error": "already_decided"})
            if "decline" in path:
                return (400, {"error": "decline is only for FI queue items (Findings with recommended_action)"})
            return (404, {})

        res = Results()
        self._run_item5(res, fa, fb, api_call, self._base_ctx())
        assert self._label_failed(res, "5a-items"), "should fail 5a-items"

    def test_fleet_b_get_returns_200_fails(self):
        """I5-2 bad world: GET /fleet/B returns 200 → FAIL 5b-get (no isolation).
        Mutation: check_fleet_scope check removed from GET /fleet handler.
        Source: handler.py:2435 (check_fleet_scope → 403 before GSI query).
        """
        fa = [_finding(_FLT_A, 1)]
        fb = [_finding(_FLT_B, 1)]
        _, _, api_call = self._make_mocks(
            fa, fb,
            get_fleet_b_resp=(200, {"findings": fb}),  # leaks fleet B!
        )
        res = Results()
        self._run_item5(res, fa, fb, api_call, self._base_ctx())
        assert self._label_failed(res, "5b-get"), "should fail 5b-get"

    def test_approve_ctrl_returns_403_fails(self):
        """I5-3 bad world: approve control (fleet A) returns 403 → FAIL 5c-ctrl.
        Mutation: scope check fires for fleet A as well (scope broken for A).
        """
        fa = [_finding(_FLT_A, 1)]
        fb = [_finding(_FLT_B, 1)]
        _, _, api_call = self._make_mocks(
            fa, fb,
            approve_ctrl_resp=(403, {"error": "insufficient scope to approve this finding"}),
        )
        res = Results()
        self._run_item5(res, fa, fb, api_call, self._base_ctx())
        assert self._label_failed(res, "5c-ctrl"), "should fail 5c-ctrl"

    def test_approve_fleet_b_returns_200_fails(self):
        """I5-4 bad world: approve fleet-B finding returns 200 → FAIL 5c-approve (no isolation).
        Mutation: scope check on the approve route removed.
        Source: handler.py:1275 (check_approve_scope → 403 for non-member fleet).
        """
        fa = [_finding(_FLT_A, 1)]
        fb = [_finding(_FLT_B, 1)]
        _, _, api_call = self._make_mocks(
            fa, fb,
            approve_b_resp=(200, {"action_id": "AVX-ACT-leaky"}),
        )
        res = Results()
        self._run_item5(res, fa, fb, api_call, self._base_ctx())
        assert self._label_failed(res, "5c-approve"), "should fail 5c-approve"

    def test_approve_ctrl_200_fails(self):
        """I5-C1 bad world: approve ctrl returning 200 → FAIL 5c-ctrl.

        Current code checks `st_ca != 403`; a 200 passes that test but creates a real
        DMS repair order.  The fix requires a post-scope 400 with 'population-scoped'
        in the error, so a 200 must fail.
        Mutation: check loosened to `!= 403` (current code).
        """
        fa = [_finding(_FLT_A, 1)]
        fb = [_finding(_FLT_B, 1)]
        _, _, api_call = self._make_mocks(
            fa, fb,
            approve_ctrl_resp=(200, {"action_id": "AVX-ACT-ctrl-real"}),  # would create real RO
        )
        res = Results()
        self._run_item5(res, fa, fb, api_call, self._base_ctx())
        assert self._label_failed(res, "5c-ctrl"), "should fail 5c-ctrl when ctrl approve returns 200"

    def test_decline_ctrl_200_fails(self):
        """I5-D1 bad world: decline ctrl returning 200 → FAIL 5d-ctrl.

        Current code checks `st_da != 403`; a 200 passes that test but creates a
        decision Action on a real managed-fleet Finding.  The fix requires 400 with
        'FI queue items' in the error, so a 200 must fail.
        Mutation: check loosened to `!= 403` (current code).
        """
        fa = [_finding(_FLT_A, 1)]
        fb = [_finding(_FLT_B, 1)]
        _, _, api_call = self._make_mocks(
            fa, fb,
            decline_ctrl_resp=(200, {"action_id": "AVX-ACT-ctrl-decline-real"}),  # creates Action
        )
        res = Results()
        self._run_item5(res, fa, fb, api_call, self._base_ctx())
        assert self._label_failed(res, "5d-ctrl"), "should fail 5d-ctrl when ctrl decline returns 200"

    # --- isolation exact-refusal bad worlds (Mutant M6 guard) ---

    def test_5b_get_returns_500_fails(self):
        """I5-5b-500 bad world: GET /fleet/B returns 500 → FAIL 5b-get.
        Mutant M6 would loosen check to `!= 200`, passing a 500 as isolation.
        Source: handler.py:851 check_fleet_scope → 403 {"error": "insufficient scope"}.
        """
        fa, fb, api_call = self._make_mocks(
            [_finding(_FLT_A, 1)], [_finding(_FLT_B, 1)],
            get_fleet_b_resp=(500, {}),
        )
        res = Results()
        self._run_item5(res, fa, fb, api_call, self._base_ctx())
        assert self._label_failed(res, "5b-get"), "500 is not a scope refusal — should fail"

    def test_5b_get_returns_403_wrong_body_fails(self):
        """I5-5b-403w bad world: GET /fleet/B returns 403 with different body → FAIL 5b-get.
        Mutant M6 would accept any 403 as isolation.
        E.g. API Gateway auth 403 body: {"message": "Forbidden"}.
        """
        fa, fb, api_call = self._make_mocks(
            [_finding(_FLT_A, 1)], [_finding(_FLT_B, 1)],
            get_fleet_b_resp=(403, {"message": "Forbidden"}),  # API-GW auth layer
        )
        res = Results()
        self._run_item5(res, fa, fb, api_call, self._base_ctx())
        assert self._label_failed(res, "5b-get"), "wrong 403 body should fail"

    def test_5c_approve_returns_500_fails(self):
        """I5-5c-500 bad world: approve fleet-B returns 500 → FAIL 5c-approve.
        Mutant M6 would accept any non-200 as isolation.
        Source: handler.py:1275-1276 (check_approve_scope → 403).
        """
        fa, fb, api_call = self._make_mocks(
            [_finding(_FLT_A, 1)], [_finding(_FLT_B, 1)],
            approve_b_resp=(500, {}),
        )
        res = Results()
        self._run_item5(res, fa, fb, api_call, self._base_ctx())
        assert self._label_failed(res, "5c-approve"), "500 is not a scope refusal — should fail"

    def test_5c_approve_returns_403_wrong_body_fails(self):
        """I5-5c-403w bad world: approve fleet-B returns 403 with different body → FAIL.
        Mutant M6 would accept any 403.
        """
        fa, fb, api_call = self._make_mocks(
            [_finding(_FLT_A, 1)], [_finding(_FLT_B, 1)],
            approve_b_resp=(403, {"error": "Unauthorized"}),
        )
        res = Results()
        self._run_item5(res, fa, fb, api_call, self._base_ctx())
        assert self._label_failed(res, "5c-approve"), "wrong 403 body should fail"

    def test_5d_decline_returns_500_fails(self):
        """I5-5d-500 bad world: decline fleet-B returns 500 → FAIL 5d-decline.
        Mutant M6 would accept any non-200 as isolation.
        Source: handler.py:1613,1618 (check_approve_scope → 403 on decline).
        """
        fa, fb, api_call = self._make_mocks(
            [_finding(_FLT_A, 1)], [_finding(_FLT_B, 1)],
            decline_b_resp=(500, {}),
        )
        res = Results()
        self._run_item5(res, fa, fb, api_call, self._base_ctx())
        assert self._label_failed(res, "5d-decline"), "500 is not a scope refusal — should fail"

    def test_5d_decline_returns_403_wrong_body_fails(self):
        """I5-5d-403w bad world: decline fleet-B returns 403 with different body → FAIL.
        Mutant M6 would accept any 403.
        """
        fa, fb, api_call = self._make_mocks(
            [_finding(_FLT_A, 1)], [_finding(_FLT_B, 1)],
            decline_b_resp=(403, {"error": "Unauthorized"}),
        )
        res = Results()
        self._run_item5(res, fa, fb, api_call, self._base_ctx())
        assert self._label_failed(res, "5d-decline"), "wrong 403 body should fail"


# ===========================================================================
# _item_6 — integration test with fully mocked AWS clients
# ===========================================================================

class TestItem6:
    """Tests call _item_6 with fakes; each bad world fails exactly the named check."""

    def _base_ctx(self) -> dict:
        import tempfile
        run = "test-run-56"
        manifest = Path(tempfile.mktemp(suffix=".json"))
        manifest.write_text(json.dumps({"run_id": run, "items": {}}))
        return {"run": run, "stage": "staging",
                "region": "us-west-2", "manifest": manifest}

    def _run_item6(
        self, res: Results, ctx: dict, *,
        before_findings: list[dict],
        after_findings: list[dict],
        invoke_resp: dict,
        run_record: dict | None,
        model_enabled: bool = True,
        first_run_record: dict | None = None,
    ) -> None:
        """Patch all external deps and run _item_6.

        model_enabled mirrors FI_SERVICE_MODEL_ENABLED on the Lambda env.
        When False, get_function_configuration returns no Environment key so
        FI_SERVICE_MODEL_ENABLED is absent (= off).

        first_run_record is the baseline run (first invoke); run_record is the
        scored run (second invoke).  If first_run_record is None it defaults to
        run_record (backward compat for tests that don't care about the double invoke).
        """
        if first_run_record is None:
            first_run_record = run_record
        # DDB scan: first call = before (after run 1), second call = after (after run 2)
        before_items = [{"Items": [_ddb_item({"finding_id": f["finding_id"]}) for f in before_findings]}]
        after_items  = [{"Items": [_ddb_item({"finding_id": f["finding_id"]}) for f in after_findings]}]
        scan_responses = before_items + after_items

        poll_response = {"Items": [_ddb_item(run_record)]} if run_record else {"Items": []}

        call_counts = {"scan": 0, "query": 0}

        def _fake_scan(**kw):
            idx = call_counts["scan"]
            call_counts["scan"] += 1
            if idx < len(scan_responses):
                return scan_responses[idx]
            return {"Items": []}

        def _fake_query(**kw):
            call_counts["query"] += 1
            # Queries are only _run_started_ats calls (2 after fix: one per invoke).
            return {"Items": []}

        fake_ddb = MagicMock()
        fake_ddb.scan.side_effect = _fake_scan
        fake_ddb.query.side_effect = _fake_query

        fake_lam = MagicMock()
        _fn_arn = "arn:aws:lambda:us-west-2:123456789012:function:vsa-staging-tier2-health-agent-us-west-2"
        _env_vars = {"FI_SERVICE_MODEL_ENABLED": "true"} if model_enabled else {}
        fake_lam.get_function_configuration.return_value = {
            "FunctionArn": _fn_arn,
            "LastUpdateStatus": "Successful",
            "Environment": {"Variables": _env_vars},
        }
        fake_lam.invoke.return_value = invoke_resp

        # Scheduler: far-from-now cron so timing check passes
        fake_sched = MagicMock()
        fn_arn = _fn_arn
        fake_sched.list_schedules.return_value = {
            "Schedules": [{"Name": "svc-sched", "GroupName": "default",
                           "Target": {"Arn": fn_arn}}]
        }
        fake_sched.get_schedule.return_value = {
            "State": "ENABLED",
            "ScheduleExpression": "cron(40 5 * * ? *)",
            "ScheduleExpressionTimezone": "Etc/UTC",
            "Target": {"Arn": fn_arn, "Input": json.dumps({"component": "service"})},
        }

        def _fake_client(service, region, *, allowed_ops, endpoint_url=None, config=None):
            if service == "dynamodb":
                return fake_ddb
            if service == "lambda":
                return fake_lam
            if service == "scheduler":
                return fake_sched
            return MagicMock()

        # Patch _now to return a time far from the cron (12:00Z)
        now_dt = dt.datetime(2026, 10, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
        # _poll_run_to_completion called twice: first = baseline, second = scored
        poll_side = [
            first_run_record,
            run_record if run_record else None,
        ]
        with patch.object(_mod, "_client", side_effect=_fake_client), \
             patch.object(_mod, "_now", return_value=now_dt), \
             patch.object(_mod, "_poll_run_to_completion",
                          side_effect=poll_side):
            _item_6(res, **ctx)

    def _failed_labels(self, res: Results) -> list[str]:
        return [r[0] for r in res.rows if r[1] == "FAIL"]

    def _label_failed(self, res: Results, label: str) -> bool:
        return any(r[0] == label and r[1] == "FAIL" for r in res.rows)

    def test_positive_control_all_pass(self):
        """I6-P: correct world — run completed, model_calls=0, tokens_used=0, no new ids → all PASS."""
        fa_before = [_finding(_FLT_A, i) for i in range(1, 4)]
        rec = _run_record("2026-10-15T12:00:10Z", "completed", 0, 0, 3)
        resp = {"StatusCode": 200, "FunctionError": None,
                "Payload": MagicMock(read=lambda: b"{}")}
        res = Results()
        self._run_item6(res, self._base_ctx(),
                        before_findings=fa_before, after_findings=fa_before,
                        invoke_resp=resp, run_record=rec)
        fails = self._failed_labels(res)
        assert not fails, f"unexpected failures: {fails}"

    def test_new_finding_id_fails(self):
        """I6-1 bad world: new finding id after invoke → FAIL 6-new-findings.
        Mutation: same_hash_model_written returned False and a new item was written.
        Source: agents/fi_fleet_health/runner.py:143 (same_hash_model_written skip predicate).
        """
        fa = [_finding(_FLT_A, 1)]
        fa_after = fa + [_finding(_FLT_A, 99)]  # new item added
        rec = _run_record("2026-10-15T12:00:10Z", "completed", 0, 0, 1)
        resp = {"StatusCode": 200, "FunctionError": None,
                "Payload": MagicMock(read=lambda: b"{}")}
        res = Results()
        self._run_item6(res, self._base_ctx(),
                        before_findings=fa, after_findings=fa_after,
                        invoke_resp=resp, run_record=rec)
        assert self._label_failed(res, "6-new-findings"), "should fail 6-new-findings"

    def test_model_calls_one_fails(self):
        """I6-2 bad world: model_calls=1 in run record → FAIL 6-rerun.
        Mutation: same_hash_model_written returned False and a model call was made.
        """
        fa = [_finding(_FLT_A, 1)]
        rec = _run_record("2026-10-15T12:00:10Z", "completed", model_calls=1, tokens_used=0)
        resp = {"StatusCode": 200, "FunctionError": None,
                "Payload": MagicMock(read=lambda: b"{}")}
        res = Results()
        self._run_item6(res, self._base_ctx(),
                        before_findings=fa, after_findings=fa,
                        invoke_resp=resp, run_record=rec)
        assert self._label_failed(res, "6-rerun"), "should fail 6-rerun"

    def test_no_run_record_returned_fails(self):
        """I6-3 bad world: second _poll_run_to_completion returns None → FAIL 6-run-rec.
        (Second invoke's poll times out; the scored run is the second one.)
        Mutation: exclude check removed → older record picked instead of this run's.
        Source: _poll_run_to_completion exclude logic in run_started_ats contract.
        """
        fa = [_finding(_FLT_A, 1)]
        rec1 = _run_record("2026-10-15T12:00:10Z", "completed", 0, 0, 0)  # first run OK
        resp = {"StatusCode": 200, "FunctionError": None,
                "Payload": MagicMock(read=lambda: b"{}")}
        res = Results()
        self._run_item6(res, self._base_ctx(),
                        before_findings=fa, after_findings=fa,
                        invoke_resp=resp,
                        run_record=None,           # second poll → times out
                        first_run_record=rec1)     # first poll → succeeds
        assert self._label_failed(res, "6-run-rec"), "should fail 6-run-rec on second poll timeout"

    def test_double_invoke_both_started_ats_in_manifest(self):
        """I6-W2: item invokes twice; both started_ats in manifest.

        With current code (single invoke) only item6_invoke_iso is recorded →
        neither item6_first_invoke_iso nor item6_second_invoke_iso exist → test fails.
        Fix: two invokes; record both invoke_iso values under distinct keys.
        """
        fa = [_finding(_FLT_A, 1)]
        rec1 = _run_record("2026-10-15T12:00:10Z", "completed", 0, 0, 1)
        rec2 = _run_record("2026-10-15T12:20:10Z", "completed", 0, 0, 0)
        resp = {"StatusCode": 200, "FunctionError": None,
                "Payload": MagicMock(read=lambda: b"{}")}
        ctx = self._base_ctx()
        res = Results()
        self._run_item6(res, ctx,
                        before_findings=fa, after_findings=fa,
                        invoke_resp=resp,
                        run_record=rec2,
                        first_run_record=rec1)
        manifest_items = json.loads(ctx["manifest"].read_text()).get("items", {})
        assert "item6_first_invoke_iso" in manifest_items, \
            "first invoke iso missing from manifest"
        assert "item6_second_invoke_iso" in manifest_items, \
            "second invoke iso missing from manifest"

    def test_double_invoke_scores_second_run(self):
        """I6-W2b: second run is scored; first run's model_calls are NOT checked.

        Even if first run had model_calls=1 (model ran on changing inputs),
        the second run on stable inputs should have model_calls=0 → PASS.
        With current code (single invoke): the one scored run has model_calls=1 → FAIL.
        """
        fa = [_finding(_FLT_A, 1)]
        rec1 = _run_record("2026-10-15T12:00:10Z", "completed", model_calls=1, tokens_used=10)
        rec2 = _run_record("2026-10-15T12:20:10Z", "completed", model_calls=0, tokens_used=0)
        resp = {"StatusCode": 200, "FunctionError": None,
                "Payload": MagicMock(read=lambda: b"{}")}
        res = Results()
        self._run_item6(res, self._base_ctx(),
                        before_findings=fa, after_findings=fa,
                        invoke_resp=resp,
                        run_record=rec2,
                        first_run_record=rec1)
        fails = self._failed_labels(res)
        assert not fails, f"second run (mc=0) should PASS even if first run had mc=1: {fails}"

    def test_model_off_absent_model_fields_passes(self):
        """I6-W1: FI_SERVICE_MODEL_ENABLED off → model fields absent in run record.

        runner.py:2550-2559 omits model_calls/tokens_used when model_budget is None.
        score_rerun returns -1 for absent fields → FAIL with current code.
        Fix: read FI_SERVICE_MODEL_ENABLED; if off, emit NOTE and PASS without scoring.
        Source: agents/tier2/runner.py:2550-2559 (model_budget guard).
        """
        fa = [_finding(_FLT_A, 1)]
        # Run record with NO model_calls / tokens_used (model_budget was None)
        rec = {
            "agent_component": _FH_RUN_PK,
            "started_at": "2026-10-15T05:45:00Z",
            "status": "completed",
            "artifacts_written": 2,
        }
        resp = {"StatusCode": 200, "FunctionError": None,
                "Payload": MagicMock(read=lambda: b"{}")}
        res = Results()
        self._run_item6(res, self._base_ctx(),
                        before_findings=fa, after_findings=fa,
                        invoke_resp=resp, run_record=rec,
                        model_enabled=False)  # FI_SERVICE_MODEL_ENABLED not set
        fails = self._failed_labels(res)
        assert not fails, f"model-off run should not FAIL: {fails}"

    def test_model_on_absent_model_fields_fails(self):
        """I6-W1b: FI_SERVICE_MODEL_ENABLED on but model fields absent → FAIL 6-rerun.

        If the model is enabled but the run record has no model_calls/tokens_used
        (i.e. they default to -1), that is an error — field must be present when enabled.
        Source: agents/tier2/run_records.py:214-218 (writes both when model_budget set).
        """
        fa = [_finding(_FLT_A, 1)]
        rec = {
            "agent_component": _FH_RUN_PK,
            "started_at": "2026-10-15T05:45:00Z",
            "status": "completed",
            "artifacts_written": 2,
            # model_calls / tokens_used absent
        }
        resp = {"StatusCode": 200, "FunctionError": None,
                "Payload": MagicMock(read=lambda: b"{}")}
        res = Results()
        self._run_item6(res, self._base_ctx(),
                        before_findings=fa, after_findings=fa,
                        invoke_resp=resp, run_record=rec,
                        model_enabled=True)  # model is ON — fields must be 0
        assert self._label_failed(res, "6-rerun"), \
            "model enabled but fields absent should FAIL 6-rerun"



# ===========================================================================
# Guard: item 6 Lambda ops pass the guard
# ===========================================================================

def test_guard_item6_lambda_ops_pass() -> None:
    """Guard passes item 6's GetFunctionConfiguration and InvokeFunction."""
    from typing import Any  # noqa: PLC0415

    class _NoNet(Exception):
        pass

    def _halt(**_kw: Any) -> None:
        raise _NoNet

    lam = _mod._client(
        "lambda", "us-west-2",
        allowed_ops=frozenset({"lambda.GetFunctionConfiguration", "lambda.Invoke"}),
    )
    lam.meta.events.register("before-send", _halt)

    for call_fn in [
        lambda: lam.get_function_configuration(FunctionName="x"),
        lambda: lam.invoke(FunctionName="x", InvocationType="RequestResponse", Payload=b"{}"),
    ]:
        raised: Exception | None = None
        try:
            call_fn()
        except Exception as exc:
            raised = exc
        assert type(raised) is _NoNet, (
            f"expected _NoNet from before-send but got {type(raised).__name__}: {raised}"
        )



# ---------------------------------------------------------------------------
# _compute_skips: item 5 is a write item — skipped without --allow-writes
# Catches mutant c7-item5-not-write: `_WRITE_ITEMS = frozenset({6, 7})` drops 5.
# ---------------------------------------------------------------------------

_compute_skips = _mod._compute_skips


def test_compute_skips_item5_without_allow_writes() -> None:
    """_compute_skips puts item 5 in to_skip when allow_writes=False.

    Catches c7-item5-not-write mutant: `_WRITE_ITEMS = frozenset({6, 7})`.
    With item 5 removed from _WRITE_ITEMS, _compute_skips wouldn't skip it,
    and the test assertion '5 in to_skip' would fail.
    """
    to_run, to_skip = _compute_skips(
        frozenset({5}),
        allow_writes=False,
        allow_managed_writes=False,
        allow_paging=False,
        item7_verify=False,
    )
    assert 5 in to_skip, f"item 5 should be skipped without --allow-writes; to_skip={to_skip}"
    assert 5 not in to_run, f"item 5 should not run without --allow-writes; to_run={to_run}"


def test_compute_skips_item5_with_allow_writes_runs() -> None:
    """item 5 runs (not skipped) when allow_writes=True.

    Positive control: verifies the skip only applies without the flag.
    """
    to_run, to_skip = _compute_skips(
        frozenset({5}),
        allow_writes=True,
        allow_managed_writes=False,
        allow_paging=False,
        item7_verify=False,
    )
    assert 5 in to_run, f"item 5 should run with --allow-writes; to_run={to_run}"
    assert 5 not in to_skip, f"item 5 should not be skipped; to_skip={to_skip}"


# ===========================================================================
# W1 — 5a-items: VIN-based leak detection, next_token pagination, ctrl present
# ===========================================================================

class TestItem5W1:
    """W1: 5a-items resolves scope.vin via _vin_fleet_cms, follows next_token,
    asserts ctrl_present.  Three bad worlds (FAIL) + none needed for GREEN
    (existing test_positive_control_all_pass covers clean world after fix).
    """

    def _run(self, pages: list[dict], vin_fleet: dict[str, str],
             run: str = "rw1") -> Results:
        """Run _item_5 with minimal mocks focused on 5a-items.

        pages: iterable of {"findings": [...], "next_token": ...} response bodies
               returned in order for each GET /fleet/A call.
        vin_fleet: {vin -> fleet_id} for _vin_fleet_cms mock.
        """
        import base64 as _b64, json as _jj, tempfile as _tf  # noqa: PLC0415
        from unittest.mock import MagicMock, patch  # noqa: PLC0415
        from boto3.dynamodb.types import TypeSerializer as _TS5  # noqa: PLC0415

        ser = _TS5()
        def _s(o: dict) -> dict: return {k: ser.serialize(v) for k, v in o.items()}

        ev_flt = lambda kw: (kw.get("ExpressionAttributeValues") or {}).get(":flt", {}).get("S", "")  # noqa: E731
        ddb = MagicMock()
        ddb.query.side_effect = lambda **kw: {"Items": (
            [_s(_finding(_FLT_A, 1))] if ev_flt(kw) == _FLT_A
            else ([_s(_finding(_FLT_B, 1))] if ev_flt(kw) == _FLT_B else [])
        )}
        ddb.put_item.return_value = {}
        ddb.delete_item.return_value = {}

        _cl = {"sub": "s", "cognito:groups": ["fleet-operator"], "custom:fleetIds": _FLT_A}
        _tok = f"h.{_b64.urlsafe_b64encode(_jj.dumps(_cl).encode()).rstrip(b'=').decode()}.s"
        idp = MagicMock()
        idp.list_user_pools.return_value = {"UserPools": [{"Id": "p", "Name": "cvx-staging-v"}]}
        idp.list_user_pool_clients.return_value = {"UserPoolClients": [{"ClientId": "c"}]}
        idp.describe_user_pool_client.return_value = {
            "UserPoolClient": {"ExplicitAuthFlows": ["ALLOW_USER_PASSWORD_AUTH"]}}
        idp.initiate_auth.return_value = {"AuthenticationResult": {"IdToken": _tok}}
        ssm = MagicMock()
        ssm.get_parameter.return_value = {"Parameter": {"Value": "p"}}
        cfn = MagicMock()
        cfn.describe_stacks.return_value = {"Stacks": [{"Outputs": [
            {"OutputKey": "RestApiUrl", "OutputValue": "https://avx.t/"}]}]}

        pgs = iter(pages)
        _scp = {"error": "insufficient scope"}
        _pop = {"error": "action_kind 'record_engineering_decision' requires a population-scoped Finding (scope must have population_id and no vin)"}
        _dfi = {"error": "decline is only for FI queue items (Findings with recommended_action)"}

        def _h(m: str, p: str, a: str, b: dict | None = None) -> tuple:
            if m == "GET" and f"/fleet/{_FLT_A}" in p: return 200, next(pgs, {"findings": []})
            if m == "GET" and f"/fleet/{_FLT_B}" in p: return 403, _scp
            if "approve" in p: return 400, _pop
            if "decline" in p: return 400, _dfi
            return 404, {}

        fh = MagicMock()
        fh.call.side_effect = _h
        res = Results()
        with _tf.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as tf:
            _jj.dump({"run_id": run, "items": {}}, tf)
            mp = Path(tf.name)

        with (patch.object(_mod, "_client", side_effect=lambda s, *a, **kw: {
                  "dynamodb": ddb, "ssm": ssm, "cloudformation": cfn,
                  "cognito-idp": idp}.get(s, MagicMock())),
              patch.object(_mod, "Http", return_value=fh),
              patch.object(_mod, "_vin_fleet_cms",
                           side_effect=lambda _d, _t, vin: vin_fleet.get(vin))):
            _item_5(res, run=run, stage="staging", region="us-west-2", manifest=mp)
        return res

    def test_b_vin_under_a_partition_fails(self) -> None:
        """W1: finding in fleet-A partition with scope.vin belonging to fleet B → FAIL.

        _vin_fleet_cms resolves the VIN to fleet_b; 5a-items fails naming the finding_id.
        RED: current 5a-items uses finding_id intersection which is empty by construction
        (different partitions never share a finding_id in real DDB).
        GREEN after fix: per-item VIN check detects fleet mismatch → FAIL with finding_id.
        """
        run = "rw1a"
        ctrl_id = f"LIVETEST-{run}-FI-FH-5-ctrl"
        b_vin = "VIN-FLEET-B-LEAK"
        page = {"findings": [
            {"finding_id": ctrl_id, "scope": {"vin": f"LIVETEST-VIN-5C-{run[:8].upper()}"}},
            {"finding_id": "FIND-W1-LEAK", "scope": {"vin": b_vin}},
        ]}
        res = self._run([page], {b_vin: _FLT_B}, run=run)
        fail_rows = [r for r in res.rows if r[0] == "5a-items" and r[1] == "FAIL"]
        assert fail_rows, f"expected 5a-items FAIL; rows={res.rows}"
        assert "FIND-W1-LEAK" in str(fail_rows[0]), (
            f"expected finding_id in FAIL detail; row={fail_rows[0]}")

    def test_page2_leak_fails(self) -> None:
        """W1: fleet-B VIN only on page 2 (next_token) → 5a-items FAIL.

        Current 5a-items fetches one page (next_token ignored) → VIN on page 2
        never inspected → PASS (RED).
        After fix: next_token followed → fleet-B VIN detected → FAIL (GREEN).
        """
        run = "rw1b"
        ctrl_id = f"LIVETEST-{run}-FI-FH-5-ctrl"
        b_vin = "VIN-FLEET-B-P2"
        page1 = {
            "findings": [{"finding_id": ctrl_id,
                          "scope": {"vin": f"LIVETEST-VIN-5C-{run[:8].upper()}"}}],
            "next_token": "tok-page2",
        }
        page2 = {"findings": [{"finding_id": "FIND-W1-P2", "scope": {"vin": b_vin}}]}
        res = self._run([page1, page2], {b_vin: _FLT_B}, run=run)
        assert any(r[0] == "5a-items" and r[1] == "FAIL" for r in res.rows), (
            f"expected 5a-items FAIL for page-2 leak; rows={res.rows}")

    def test_ctrl_missing_fails(self) -> None:
        """W1: ctrl_id absent from fleet-A response → 5a-items FAIL.

        Current check: len>=1 and not wrong → PASS even without ctrl (RED).
        After fix: assert ctrl_present → FAIL (GREEN).
        """
        a_vin = "VIN-FLEET-A-ONLY"
        page = {"findings": [{"finding_id": "FIND-A-ONLY", "scope": {"vin": a_vin}}]}
        res = self._run([page], {a_vin: _FLT_A})
        assert any(r[0] == "5a-items" and r[1] == "FAIL" for r in res.rows), (
            f"expected 5a-items FAIL when ctrl absent; rows={res.rows}")


# ===========================================================================
# Review cycle 3: item 5's plant delete (S1, S5) and 5a's paging (S3)
# ===========================================================================

class TestItem5Cycle3:
    """The plant delete runs on a failed put too, waits between tries, holds signals;
    5a quotes the next_token and stops at a page cap."""

    _RUN = "rc3"

    def _run(self, *, ddb: Any = None, get_a: Any = None, run: str = _RUN) -> tuple[Results, Any, Any]:
        import base64 as _b64, json as _jj, tempfile as _tf  # noqa: PLC0415
        from boto3.dynamodb.types import TypeSerializer as _TSc3  # noqa: PLC0415

        ser = _TSc3()

        def _s(o: dict) -> dict:
            return {k: ser.serialize(v) for k, v in o.items()}

        ctrl_id = f"LIVETEST-{run}-FI-FH-5-ctrl"
        ev_flt = lambda kw: (kw.get("ExpressionAttributeValues") or {}).get(":flt", {}).get("S", "")  # noqa: E731
        if ddb is None:
            ddb = MagicMock()
            ddb.put_item.return_value = {}
            ddb.delete_item.return_value = {}
        ddb.query.side_effect = lambda **kw: {"Items": (
            [_s(_finding(_FLT_A, 1))] if ev_flt(kw) == _FLT_A
            else ([_s(_finding(_FLT_B, 1))] if ev_flt(kw) == _FLT_B else [])
        )}
        _cl = {"sub": "s", "cognito:groups": ["fleet-operator"], "custom:fleetIds": _FLT_A}
        _tok = f"h.{_b64.urlsafe_b64encode(_jj.dumps(_cl).encode()).rstrip(b'=').decode()}.s"
        idp = MagicMock()
        idp.list_user_pools.return_value = {"UserPools": [{"Id": "p", "Name": "cvx-staging-v"}]}
        idp.list_user_pool_clients.return_value = {"UserPoolClients": [{"ClientId": "c"}]}
        idp.describe_user_pool_client.return_value = {
            "UserPoolClient": {"ExplicitAuthFlows": ["ALLOW_USER_PASSWORD_AUTH"]}}
        idp.initiate_auth.return_value = {"AuthenticationResult": {"IdToken": _tok}}
        ssm = MagicMock()
        ssm.get_parameter.return_value = {"Parameter": {"Value": "p"}}
        cfn = MagicMock()
        cfn.describe_stacks.return_value = {"Stacks": [{"Outputs": [
            {"OutputKey": "RestApiUrl", "OutputValue": "https://avx.t/"}]}]}
        page = {"findings": [{"finding_id": ctrl_id,
                              "scope": {"vin": f"LIVETEST-VIN-5C-{run[:8].upper()}"}}]}
        paths: list[str] = []

        def _h(m: str, p: str, a: str, b: dict | None = None) -> tuple:
            paths.append(p)
            if m == "GET" and f"/fleet/{_FLT_A}" in p:
                return get_a(p) if get_a is not None else (200, page)
            if m == "GET" and f"/fleet/{_FLT_B}" in p:
                return 403, {"error": "insufficient scope"}
            if "approve" in p:
                return 400, {"error": "action_kind 'record_engineering_decision' requires a "
                             "population-scoped Finding"}
            if "decline" in p:
                return 400, {"error": "decline is only for FI queue items"}
            return 404, {}

        fh = MagicMock()
        fh.call.side_effect = _h
        res = Results()
        with _tf.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as tf:
            _jj.dump({"run_id": run, "items": {}}, tf)
            mp = Path(tf.name)
        sleeps: list[float] = []
        with (patch.object(_mod, "_client", side_effect=lambda s, *a, **kw: {
                  "dynamodb": ddb, "ssm": ssm, "cloudformation": cfn,
                  "cognito-idp": idp}.get(s, MagicMock())),
              patch.object(_mod, "Http", return_value=fh),
              patch.object(_mod.time, "sleep", side_effect=sleeps.append),
              patch.object(_mod, "_vin_fleet_cms", side_effect=lambda _d, _t, vin: None)):
            _item_5(res, run=run, stage="staging", region="us-west-2", manifest=mp)
        return res, ddb, (paths, sleeps)

    @staticmethod
    def _throttle() -> Exception:
        from botocore.exceptions import ClientError  # noqa: PLC0415
        return ClientError({"Error": {"Code": "ProvisionedThroughputExceededException",
                                      "Message": "x"}}, "DeleteItem")

    def test_put_raising_still_deletes_the_plant(self) -> None:
        """S1: PutItem raises (it may have landed): the delete runs, keyed on the plant,
        conditional on the LIVETEST- prefix, and item 5 SKIPs."""
        ddb = MagicMock()
        ddb.put_item.side_effect = RuntimeError("read timeout after the write")
        ddb.delete_item.return_value = {}
        res, ddb, _ = self._run(ddb=ddb)
        ctrl_id = f"LIVETEST-{self._RUN}-FI-FH-5-ctrl"
        assert ddb.delete_item.call_count == 1, ddb.delete_item.call_args_list
        kw = ddb.delete_item.call_args.kwargs
        assert kw.get("Key") == {"finding_id": {"S": ctrl_id}}, kw
        assert kw.get("ConditionExpression") == "begins_with(finding_id, :pfx)", kw
        assert kw.get("ExpressionAttributeValues") == {":pfx": {"S": "LIVETEST-"}}, kw
        assert [r[1] for r in res.rows if r[0] == "item-5"] == ["SKIP"], res.rows
        assert res._cleanup_failed is False

    def test_three_failed_deletes_set_the_flag_and_wait_between_tries(self) -> None:
        """S5/P2: every DeleteItem throttled: 3 tries, waits of 1 s then 2 s, flag set."""
        ddb = MagicMock()
        ddb.put_item.return_value = {}
        ddb.delete_item.side_effect = self._throttle()
        res, ddb, (_paths, sleeps) = self._run(ddb=ddb)
        assert ddb.delete_item.call_count == 3, ddb.delete_item.call_args_list
        assert sleeps == [1, 2], sleeps
        assert res._cleanup_failed is True

    def test_conditional_check_failed_counts_as_gone(self) -> None:
        from botocore.exceptions import ClientError  # noqa: PLC0415
        ddb = MagicMock()
        ddb.put_item.return_value = {}
        ddb.delete_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, "DeleteItem")
        res, ddb, (_p, sleeps) = self._run(ddb=ddb)
        assert ddb.delete_item.call_count == 1 and sleeps == []
        assert res._cleanup_failed is False

    def test_a_signal_during_the_delete_is_held_until_the_delete_is_done(self) -> None:
        """S5: SIGTERM arrives during the first (throttled) DeleteItem: the retry still runs
        and deletes the plant, THEN the held signal is acted on (SystemExit 143)."""
        import os  # noqa: PLC0415
        import signal  # noqa: PLC0415

        calls = {"n": 0}

        def _delete(**_kw: Any) -> dict:
            calls["n"] += 1
            if calls["n"] == 1:
                os.kill(os.getpid(), signal.SIGTERM)
                raise self._throttle()
            return {}

        ddb = MagicMock()
        ddb.put_item.return_value = {}
        ddb.delete_item.side_effect = _delete
        saved = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        try:
            _mod._install_signal_handlers()
            try:
                self._run(ddb=ddb)
            except SystemExit as exc:
                assert exc.code == 143
            else:
                raise AssertionError("expected the held SIGTERM to raise SystemExit(143)")
        finally:
            for s, h in saved.items():
                signal.signal(s, h)
        assert calls["n"] == 2, f"the delete must be retried before the signal acts; calls={calls}"

    def test_next_token_is_url_quoted(self) -> None:
        """S3: a base64 token with '+', '/', '=' reaches the handler intact."""
        run = self._RUN
        ctrl = {"finding_id": f"LIVETEST-{run}-FI-FH-5-ctrl",
                "scope": {"vin": f"LIVETEST-VIN-5C-{run[:8].upper()}"}}

        def _get_a(path: str) -> tuple:
            if "next_token=" in path:
                return 200, {"findings": [ctrl]}
            return 200, {"findings": [], "next_token": "ab+c/d=="}

        res, _ddb, (paths, _s) = self._run(get_a=_get_a)
        gets = [p for p in paths if f"/fleet/{_FLT_A}" in p]
        assert gets == [f"/findings/fleet/{_FLT_A}",
                        f"/findings/fleet/{_FLT_A}?next_token=ab%2Bc%2Fd%3D%3D"], gets
        assert [r[1] for r in res.rows if r[0] == "5a-items"] == ["PASS"], res.rows

    def test_a_cursor_that_never_ends_stops_at_the_page_cap(self) -> None:
        """S3: a queue that always answers a next_token FAILs 5a-items after the cap."""
        res, _ddb, (paths, _s) = self._run(
            get_a=lambda p: (200, {"findings": [], "next_token": "again"}))
        gets = [p for p in paths if f"/fleet/{_FLT_A}" in p]
        assert len(gets) == _mod._5A_MAX_PAGES == 200, len(gets)
        fails = [r for r in res.rows if r[0] == "5a-items" and r[1] == "FAIL"]
        assert fails and "after 200 pages" in fails[0][2], res.rows


class TestPickFleetServiceFindingCycle3:
    """S2: only Finding A needs a fault:/pm: need; B takes any eligible Finding."""

    def test_b_pick_takes_an_a2_only_finding(self) -> None:
        from scripts.tests.test_live_verify_fi_fleet_health_item3 import (  # noqa: PLC0415
            _agent2_need, _book_finding, _make_ddb_low_mock)
        f = _book_finding("FIND-A2-ONLY")
        f["recommended_action"]["parameters"]["needs"] = [_agent2_need()]
        ddb = _make_ddb_low_mock([f])
        args = (ddb, "vsa-staging-avx-findings", "vsa-staging-avx-actions",
                "demo-rental-carshare", "2026-10-17T02:10:00Z")
        assert _mod._pick_fleet_service_finding(*args) is None
        got = _mod._pick_fleet_service_finding(*args, require_fault_or_pm=False)
        assert got is not None and got["finding_id"] == "FIND-A2-ONLY", got

    def test_parameters_none_does_not_raise(self) -> None:
        from scripts.tests.test_live_verify_fi_fleet_health_item3 import (  # noqa: PLC0415
            _book_finding, _make_ddb_low_mock)
        f = _book_finding("FIND-NULL-PARAMS")
        f["recommended_action"]["parameters"] = None
        ddb = _make_ddb_low_mock([f])
        try:
            got = _mod._pick_fleet_service_finding(
                ddb, "vsa-staging-avx-findings", "vsa-staging-avx-actions",
                "demo-rental-carshare", "2026-10-17T02:10:00Z")
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(
                f"parameters=None must not raise: {type(exc).__name__}: {exc}") from exc
        assert got is None


class TestItem5ProbeSideEffect:
    """Security review c1 W2: item 5's approve/decline probes hit a REAL fleet-B Finding
    with a fleet-A token. Its decision Action is read (consistently) before and after;
    no baseline -> no probe; any change -> FAIL and exit 1."""

    _RUN = TestItem5Cycle3._RUN
    _run = TestItem5Cycle3._run

    def _ddb(self, actions_reads: list) -> Any:
        """A DDB fake whose actions-table GetItems return/raise ``actions_reads`` in order."""
        ddb = MagicMock()
        ddb.put_item.return_value = {}
        ddb.delete_item.return_value = {}
        seq = list(actions_reads)
        calls: list[dict] = []

        def _get_item(**kw: Any) -> dict:
            if kw.get("TableName") == "vsa-staging-avx-actions":
                calls.append(kw)
                nxt = seq.pop(0)
                if isinstance(nxt, Exception):
                    raise nxt
                return nxt
            return {}

        ddb.get_item.side_effect = _get_item
        ddb.actions_calls = calls
        return ddb

    @staticmethod
    def _fb() -> tuple[str, str]:
        from agents.supervisor.tools.fleet_recommendation import (  # noqa: PLC0415
            _derive_decision_action_id)
        fb_id = _finding(_FLT_B, 1)["finding_id"]
        return fb_id, _derive_decision_action_id(fb_id)

    @staticmethod
    def _rows(res: Results, label: str) -> list[tuple[str, str, str]]:
        return [r for r in res.rows if r[0] == label]

    def test_unchanged_decision_action_passes(self) -> None:
        fb_id, aid = self._fb()
        ddb = self._ddb([{}, {}])
        res, ddb, _ = self._run(ddb=ddb)
        assert [r[1] for r in self._rows(res, "5-probe-baseline")] == ["PASS"], res.rows
        assert [r[1] for r in self._rows(res, "5-probe-no-side-effect")] == ["PASS"], res.rows
        assert res._cleanup_failed is False
        assert [c.get("Key") for c in ddb.actions_calls] == [{"action_id": {"S": aid}}] * 2
        assert all(c.get("ConsistentRead") is True for c in ddb.actions_calls), ddb.actions_calls

    def test_a_decision_action_created_by_the_probes_fails_and_exits_1(self) -> None:
        """The route fails open: the Action is absent before and present after."""
        fb_id, aid = self._fb()
        created = {"Item": {"action_id": {"S": aid}, "status": {"S": "declined"}}}
        res, _ddb, _ = self._run(ddb=self._ddb([{}, created]))
        rows = self._rows(res, "5-probe-no-side-effect")
        assert [r[1] for r in rows] == ["FAIL"], res.rows
        assert fb_id in rows[0][2] and aid in rows[0][2], rows
        assert res._cleanup_failed is True

    def test_a_changed_decision_action_fails(self) -> None:
        fb_id, aid = self._fb()
        before = {"Item": {"action_id": {"S": aid}, "status": {"S": "approved"}}}
        after = {"Item": {"action_id": {"S": aid}, "status": {"S": "declined"}}}
        res, _ddb, _ = self._run(ddb=self._ddb([before, after]))
        assert [r[1] for r in self._rows(res, "5-probe-no-side-effect")] == ["FAIL"], res.rows
        assert res._cleanup_failed is True

    def test_an_unreadable_after_state_fails(self) -> None:
        res, _ddb, _ = self._run(ddb=self._ddb([{}, RuntimeError("throttled")]))
        assert [r[1] for r in self._rows(res, "5-probe-no-side-effect")] == ["FAIL"], res.rows
        assert res._cleanup_failed is True

    def test_no_baseline_means_no_probe_of_the_real_finding(self) -> None:
        """Fail closed: without a before-state the real fleet-B Finding is never probed,
        and the plant is still deleted."""
        fb_id, _aid = self._fb()
        res, ddb, (paths, _s) = self._run(ddb=self._ddb([RuntimeError("throttled")]))
        assert [r[1] for r in self._rows(res, "5-probe-baseline")] == ["FAIL"], res.rows
        assert not any(fb_id in p for p in paths), f"probed the real Finding without a baseline: {paths}"
        assert ddb.delete_item.call_count == 1, ddb.delete_item.call_args_list
        assert not self._rows(res, "5-probe-no-side-effect"), res.rows


# ===========================================================================
# Run-record polls against real key-condition semantics (moto): review cycle 6 W1, Fix Group 6
# M6.1, issue 2026-10-27-fi-sweep-avx-ms-run-start-readers. Run records carry millisecond
# started_at ("...:01.500Z"); items 6 and 7 take the bound as _iso(_now()), a whole second with
# a "Z". DynamoDB compares strings and "." sorts below "Z", so a run that starts in the bound's
# own second must still be found. The MagicMock tests above can't show this: a fake client
# ignores the key condition.
# ===========================================================================

_FH_BOUND = "2026-10-27T05:45:01Z"
_FH_RUNS = "vsa-staging-avx-agent-runs"


class TestRunRecordPollBoundMoto:
    def _client(self, monkeypatch: Any) -> Any:
        import boto3  # noqa: PLC0415

        for k in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_ENDPOINT_URL"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
        ddb = boto3.client("dynamodb", region_name="us-west-2")
        ddb.create_table(
            TableName=_FH_RUNS,
            KeySchema=[{"AttributeName": "agent_component", "KeyType": "HASH"},
                       {"AttributeName": "started_at", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "agent_component", "AttributeType": "S"},
                                  {"AttributeName": "started_at", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        return ddb

    @staticmethod
    def _put(ddb: Any, started_at: str, status: str = "completed") -> None:
        ddb.put_item(TableName=_FH_RUNS, Item=_ddb_item(_run_record(started_at, status, 0, 0, 0)))

    def test_started_ats_include_a_run_that_starts_in_the_bounds_second(self, monkeypatch) -> None:
        """Item 7's negative check: a run record written in the bound's second must show up
        in new_ats - existing_ats."""
        from moto import mock_aws  # noqa: PLC0415
        with mock_aws():
            ddb = self._client(monkeypatch)
            self._put(ddb, "2026-10-27T05:45:01.500Z")
            assert _run_started_ats(ddb, _FH_RUNS, _FH_RUN_PK, _FH_BOUND) == {
                "2026-10-27T05:45:01.500Z"}

    def test_started_ats_leave_out_a_run_from_the_second_before(self, monkeypatch) -> None:
        from moto import mock_aws  # noqa: PLC0415
        with mock_aws():
            ddb = self._client(monkeypatch)
            self._put(ddb, "2026-10-27T05:45:00.999Z")
            self._put(ddb, "2026-10-27T05:45:00Z")
            assert _run_started_ats(ddb, _FH_RUNS, _FH_RUN_PK, _FH_BOUND) == set()

    def test_poll_finds_a_run_that_starts_in_the_bounds_second(self, monkeypatch) -> None:
        """Item 6: the second invoke follows the first back to back, so its run usually
        starts in the bound's second."""
        from moto import mock_aws  # noqa: PLC0415
        with mock_aws():
            ddb = self._client(monkeypatch)
            self._put(ddb, "2026-10-27T05:45:01.500Z")
            rec = _poll_run_to_completion(ddb, _FH_RUNS, _FH_RUN_PK, _FH_BOUND,
                                          timeout_s=0.3, poll_s=0)
            assert rec is not None and rec["started_at"] == "2026-10-27T05:45:01.500Z"

    def test_poll_skips_a_same_second_run_that_existed_before_the_invoke(self, monkeypatch) -> None:
        from moto import mock_aws  # noqa: PLC0415
        with mock_aws():
            ddb = self._client(monkeypatch)
            self._put(ddb, "2026-10-27T05:45:01.200Z")
            existing = _run_started_ats(ddb, _FH_RUNS, _FH_RUN_PK, _FH_BOUND)
            assert existing == {"2026-10-27T05:45:01.200Z"}
            self._put(ddb, "2026-10-27T05:45:01.500Z")
            rec = _poll_run_to_completion(ddb, _FH_RUNS, _FH_RUN_PK, _FH_BOUND,
                                          timeout_s=0.3, poll_s=0, exclude=existing)
            assert rec is not None and rec["started_at"] == "2026-10-27T05:45:01.500Z"

    def test_poll_does_not_take_a_run_from_the_second_before(self, monkeypatch) -> None:
        from moto import mock_aws  # noqa: PLC0415
        with mock_aws():
            ddb = self._client(monkeypatch)
            self._put(ddb, "2026-10-27T05:45:00.999Z")
            assert _poll_run_to_completion(ddb, _FH_RUNS, _FH_RUN_PK, _FH_BOUND,
                                           timeout_s=0.2, poll_s=0) is None
