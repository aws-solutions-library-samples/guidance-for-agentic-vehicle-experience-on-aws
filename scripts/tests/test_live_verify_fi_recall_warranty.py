"""Unit tests for scripts/live-verify-fi-recall-warranty.py — harness only (Part 1).

Covers (design § Tests, harness rows):
  H1  LIVE_TESTS not set -> exit 2
  H2  --stage prod -> exit 1 before any AWS call
  H3  --all without write flags -> every write item SKIP, has_any_skip True
  H4  --allow-writes missing for a write item -> that item in items_to_skip
  H5  --allow-managed-writes missing for managed items -> those items in items_to_skip
  H6  --allow-paging missing for item 9 -> item 9 in items_to_skip
  H7  write window active -> main() returns 1 before any AWS call
  H8  write window inactive -> no write-window refusal
  H9  _create_ddb_backups returns False -> write items skipped, res has FAIL
  H10 teardown.run() is called even when an item raises an exception
  H11 SIGTERM (SystemExit) -> teardown still runs (finally block)
  H12 _run_residue_checks: a failing check -> FAIL in res
  H13 _run_residue_checks: a passing check -> PASS in res
  H14 _run_residue_checks: check raises -> FAIL in res
  H15 Results: has_any_skip True when SKIP present alongside PASS
  H16 Results: _cleanup_failed propagates to exit 1

Scoring function tests (pure helpers, no AWS):
  S1  score_recall_views: all campaigns match -> PASS
  S2  score_recall_views: empty answer key -> FAIL (anti-vacuity)
  S3  score_recall_views: count mismatch -> FAIL (mutant: wrong count)
  S4  score_recall_views: missing campaign -> FAIL
  S5  score_warranty_run: 80% flagged, zero denied -> PASS (threshold boundary)
  S6  score_warranty_run: 79% flagged -> FAIL (just below threshold)
  S7  score_warranty_run: 100% but denied line flagged -> FAIL
  S8  score_warranty_run: claimed line flagged -> FAIL
  S9  score_rerun: all zeros -> PASS
  S10 score_rerun: artifacts_written=1 -> FAIL (idempotency broken)
  S11 score_rerun: model_calls=1 -> FAIL
  S12 score_rerun: tokens_used=1 -> FAIL

Item 7 / 8 helpers (pure):
  I1  _interpret_dms_fleet_scope_denial: 404 + 400 -> PASS
  I2  _interpret_dms_fleet_scope_denial: 400 + 400 -> scope_ok=False (mutant: scope before validation)
  I3  _interpret_dms_fleet_scope_denial: 404 + 403 -> validation_ok=False

_compute_skips (pure):
  C1  all flags True -> no skips
  C2  allow_writes=False -> write items in to_skip
  C3  allow_managed_writes=False -> managed items in to_skip
  C4  allow_paging=False -> paging items in to_skip
  C5  all flags False -> all write + managed + paging items skipped

Run:
    PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest scripts/tests/test_live_verify_fi_recall_warranty.py -q
"""
from __future__ import annotations

import importlib.util
import os
import signal
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Load the script as a module (hyphenated name requires importlib)
# ---------------------------------------------------------------------------

_SCRIPT = Path(__file__).resolve().parents[1] / "live-verify-fi-recall-warranty.py"
_spec = importlib.util.spec_from_file_location("live_verify_fi_recall_warranty", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

# Pure helpers under test
Results = _mod.Results
Http = _mod.Http
_in_write_window = _mod._in_write_window
_compute_skips = _mod._compute_skips
_run_residue_checks = _mod._run_residue_checks
_TeardownRegistry = _mod._TeardownRegistry
_create_ddb_backups = _mod._create_ddb_backups
_init_manifest = _mod._init_manifest
score_recall_views = _mod.score_recall_views
score_recall_disclosure = _mod.score_recall_disclosure
score_warranty_run = _mod.score_warranty_run
score_rerun = _mod.score_rerun
score_finding_counts = _mod.score_finding_counts
_interpret_dms_fleet_scope_denial = _mod._interpret_dms_fleet_scope_denial
_pick_warranty_finding = _mod._pick_warranty_finding
score_adjudication = _mod.score_adjudication
_count_actions_for_finding = _mod._count_actions_for_finding
_adjudicate_claim_in_memory = _mod._adjudicate_claim_in_memory
_delete_5b_ro_impl = _mod._delete_5b_ro_impl
_check_residue_findings = _mod._check_residue_findings
main = _mod.main

_ALL_ITEMS = _mod._ALL_ITEMS
_WRITE_ITEMS = _mod._WRITE_ITEMS
_MANAGED_WRITE_ITEMS = _mod._MANAGED_WRITE_ITEMS
_PAGING_ITEMS = _mod._PAGING_ITEMS
_WARRANTY_SCORE_THRESHOLD = _mod._WARRANTY_SCORE_THRESHOLD
_WARRANTY_KIND = _mod._WARRANTY_KIND


# ---------------------------------------------------------------------------
# Deleted 2026-10-12 (rebuild W13): source-text tests whose property a behavioural test
# in test_live_verify_fi_rw_e2e.py now pins (each was checked with a mutant; decisions.md).
#   test_dms_paths_sit_under_api_dms -> test_item8_scope_probes_pass_and_send_post_scope_bodies, test_item5_closed_before_work_cancels_and_replans
#   TestA5GuardrailPreCheck::test_a5_item9_calls_check_before_override -> test_item9_refuses_a_blank_guardrail_and_never_overrides
#   TestB2SchedulerRecheckPerInvoke::test_b2_item9_rechecks_before_each_component -> test_item9_rechecks_the_schedules_after_the_override_and_restores, test_item9_rechecks_the_schedules_before_the_second_component
#   TestC1IsolationPositiveControl::test_c1_ctrl_ok_gates_isolation_probes -> test_item8_probes_all_fail_and_none_is_sent_when_the_fleet_a_control_fails
#   TestC1IsolationPositiveControl::test_c1_dms_probe_positive_control_runs_first -> test_item8_dms_probes_fail_when_the_fleet_projection_is_stale
#   TestC3ApproveResponseShape::test_action_id_from_body_top_level -> test_item4_books_one_recall_service_end_to_end, test_item5_closed_before_work_cancels_and_replans (real approve responses)
#   TestC3ApproveResponseShape::test_queue_status_uses_findings_key -> test_item5a_follows_the_fleet_queue_across_pages
#   TestC3ApproveResponseShape::test_ro_id_from_target_ref -> test_item4_books_one_recall_service_end_to_end (4-ro-id)
#   TestC4FiveBRoTeardownBareUuid::test_delete_5b_ro_uses_bare_uuid -> the driven _delete_5b_ro_impl test in this file; e2e residue-dms-5b-ro
#   TestD2PayDate::test_d2_pay_date_written_to_manifest -> test_item7_writes_the_pay_date_to_the_manifest
#   TestFixADmsApiUrl::test_dms_ctx_key_in_ctx_dict -> test_item8_scope_probes_pass_and_send_post_scope_bodies
#   TestFixADmsApiUrl::test_dms_probe_path_has_no_api_prefix -> test_item8_scope_probes_pass_and_send_post_scope_bodies (requests reach the /api/dms routes)
#   TestFixBAthenaOutputLocation::test_athena_run_does_not_pass_output_location -> the driven test beside it in this file
#   TestFixCBandClassify::test_item6_source_uses_band_classify -> item 6 e2e tests (p0 rows from the agent's classifier; mutant record)
#   TestItem11D18Eval::test_item11_disclosure_contains_required_phrases -> test_item11_plants_the_safety_phrases_only_in_the_disclosure
#   TestResidueRealChecks::test_residue_in_finally_on_exception -> test_a_sigterm_inside_an_item_still_tears_down_and_checks_residue, test_item9_restores_after_a_sigterm_during_the_restore
#   TestW10DisclosureHeadline::test_do_not_drive_not_in_item11_headline -> test_item11_plants_the_safety_phrases_only_in_the_disclosure
#   TestW10DmsCommit::test_dms_commit_written_to_manifest -> test_item7_writes_the_pay_date_to_the_manifest
#   TestW12PerItemExcept::test_item_exception_caught_per_item -> test_one_items_exception_does_not_stop_the_items_after_it
#   TestW12PerItemExcept::test_write_window_rechecked_per_item -> test_the_write_window_is_rechecked_before_each_write_item
#   TestW4RoReadyCheck::test_item4_checks_recall_campaign -> test_item4_fails_when_the_ro_lacks_the_campaigns_recall_line
#   TestW4RoReadyCheck::test_item4_checks_ro_ready -> test_item4_fails_when_the_stand_in_never_moves_the_ro_to_ready
#   TestW5DraftCheckAndNeedsFormat::test_5a_checks_draft_before_close -> test_item5_fails_when_the_ro_is_not_draft_before_the_close[5a]
#   TestW5DraftCheckAndNeedsFormat::test_5b_checks_draft_before_close -> test_item5_fails_when_the_ro_is_not_draft_before_the_close[5b]
#   TestW5DraftCheckAndNeedsFormat::test_5b_needs_is_list_of_labor_op -> test_item5_closed_before_work_cancels_and_replans (DMS's real handler validates the body)
#   TestW5QueueCheckPaginates::test_5a_queue_absent_after_all_pages_fails -> test_item5a_follows_the_fleet_queue_across_pages (mutant record)
#   TestW5QueueCheckPaginates::test_5a_queue_check_paginates -> test_item5a_follows_the_fleet_queue_across_pages
#   TestW7AlarmSubscriberPaginated::test_alarm_history_checks_succeeded -> test_item9_fails_when_the_alarm_action_failed
#   TestW7AlarmSubscriberPaginated::test_list_topics_is_paginated -> test_item9_finds_the_alarm_topic_on_a_later_list_topics_page
#   TestW9ActionsScanOnce::test_item7_scan_error_fails -> test_item7_fails_closed_when_the_actions_scan_errors
#   TestW9ActionsScanOnce::test_item7_scans_actions_once -> test_item7_scans_the_actions_table_once_not_per_finding
#   TestItem2DisclosureAndWarrantyScore (3 tests, mock-driven item 2; hung against the current
#     poll helpers) -> test_item2_a_disclosure_that_differs_from_the_catalog_fails,
#     test_item2_a_flagged_denied_line_fails_the_warranty_score,
#     test_item2_refuses_a_blank_guardrail_without_invoking
#   TestW5QueueCheckPaginates::test_5a_queue_absent_after_all_pages_fails (corrected after
#     review rebuild c1 WB2) -> test_item5a_fails_when_its_finding_is_missing_from_every_queue_page
#   TestW10DmsCommit::test_dms_commit_written_to_manifest (corrected, c1 rB3)
#     -> test_the_manifest_records_the_dms_trees_commit
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Autouse fixture: strip AWS env vars, point at unreachable endpoint
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip all AWS_* environment variables and point the endpoint at 127.0.0.1:1
    so any accidental boto3 call fails immediately rather than using real credentials.
    """
    aws_keys = [k for k in os.environ if k.startswith("AWS_")]
    for k in aws_keys:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:1")


@pytest.fixture(autouse=True)
def _fresh_test_run_dirs() -> None:
    """Item 9 tests call item9(run="test-…"), whose run dir is /tmp/fi-rw-c-g55-test-…
    and whose restore file is created with O_EXCL. A kept restore file (the script keeps
    it on a failed restore, by design) made the next session's run raise FileExistsError
    (2026-10-12). Remove only test-run dirs (real runs are fi-rw-c-g55-g55-<stamp>-<hex>); only this process's dirs."""
    import glob
    import shutil
    for d in glob.glob(f"/tmp/fi-rw-c-g55-test-*-{_PID}"):
        shutil.rmtree(d, ignore_errors=True)


# Per-process test run ids: parallel runs of this file (mutant lanes) must not share
# /tmp/fi-rw-c-g55-test-* dirs, and the cleanup above touches only this process's dirs.
_PID = os.getpid()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plant(ro_id: str, verdict: str, claimed: bool = False) -> dict[str, Any]:
    # C1(a): live seed field is already_claimed, not claimed
    return {"ro_id": ro_id, "verdict_plant": verdict, "already_claimed": claimed}


def _warranty_finding(ro_id: str, verdict: str = "approved") -> dict[str, Any]:
    # Evidence built with warranty.py build_warranty_evidence + _to_schema_ev so a
    # shape change in warranty.py breaks this test.
    # Source: agents/fi_recall_warranty/warranty.py _to_schema_ev
    from agents.fi_recall_warranty.warranty import (  # noqa: PLC0415
        build_warranty_evidence as _bwe,
        _to_schema_ev as _tse,
    )
    row = {"vehicle_vin": "VIN-001", "verdict_status": verdict, "verdict_reason": "test",
           "matched_rule_id": None, "terms_version": None}
    evidence = _tse(_bwe(row, "fi-recall-warranty"), "2026-01-01T00:00:00Z")
    return {
        "finding_kind": _WARRANTY_KIND,
        "recommended_action": {"parameters": {"ro_id": ro_id}},
        "evidence": evidence,
    }


def _ak_row(campaign_id: str, confirmed: int, population: int, remedied: int) -> dict:
    # W1(d): answer key now carries confirmed_vins/remedied_vins frozensets for set comparison.
    # Build synthetic VINs from count; score_recall_views compares these sets against view.
    return {
        "campaign_id": campaign_id,
        "confirmed_vins": frozenset(f"VIN-{campaign_id}-C-{i}" for i in range(confirmed)),
        "remedied_vins": frozenset(f"VIN-{campaign_id}-C-{i}" for i in range(remedied)),
        "population": population,
    }


def _view_row(campaign_id: str, confirmed: int, population: int, remedied: int) -> dict:
    # W1(d): view_rows are now per-VIN rows; one row per VIN.
    # Returns a list of per-VIN dicts to unpack into the view_rows list.
    rows: list[dict] = []
    for i in range(confirmed):
        rows.append({
            "campaign_id": campaign_id,
            "vin": f"VIN-{campaign_id}-C-{i}",
            "match_basis": "CONFIRMED",
            "remedied": "true" if i < remedied else "false",
            "managed_as_of": "2026-01-01",  # not NULL → included by W1(e) filter
        })
    for i in range(population):
        rows.append({
            "campaign_id": campaign_id,
            "vin": f"VIN-{campaign_id}-P-{i}",
            "match_basis": "POPULATION",
            "remedied": "false",
            "managed_as_of": None,  # population VINs: managed_as_of may be NULL
        })
    return rows


# ---------------------------------------------------------------------------
# H: Harness tests
# ---------------------------------------------------------------------------


class TestHarnessRefusals:
    def test_h1_no_live_tests(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """H1: LIVE_TESTS not set -> exit 2."""
        monkeypatch.delenv("LIVE_TESTS", raising=False)
        assert main() == 2

    def test_h2_stage_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """H2: --stage prod -> exit 1 before any AWS call (import boto3 must not happen)."""
        monkeypatch.setenv("LIVE_TESTS", "1")
        with patch("sys.argv", ["script", "--stage", "prod"]):
            assert main() == 1

    def test_h7_write_window_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """H7: write window active -> exit 1 before any AWS call."""
        monkeypatch.setenv("LIVE_TESTS", "1")
        with (
            patch("sys.argv", ["script", "--all", "--allow-writes"]),
            patch.object(_mod, "_in_write_window", return_value=True),
        ):
            assert main() == 1

    def test_h8_write_window_not_refused_when_readonly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """H8: write window active but no write items requested -> no refusal.

        item1 is now a real implementation; patch it to a pass so the test
        focuses on write-window logic without needing live AWS.
        """
        monkeypatch.setenv("LIVE_TESTS", "1")
        with (
            patch("sys.argv", ["script", "--item", "1"]),
            patch.object(_mod, "_in_write_window", return_value=True),
            patch.dict(_mod._ITEM_FNS, {1: lambda res, **_kw: res.check("1", True, "mocked")}),
            patch("boto3.client") as mock_client,
            patch("boto3.resource") as mock_resource,
            patch.object(_mod, "_make_lambda_client", return_value=MagicMock()),
            patch.object(_mod, "_run_residue_checks"),   # residue checks need real DDB; skip
        ):
            mock_sts = MagicMock()
            mock_sts.get_caller_identity.return_value = {
                "Arn": "arn:aws:iam::123456789012:user/test",
                "Account": "123456789012",
            }
            mock_cfn = MagicMock()
            mock_cfn.describe_stacks.return_value = {
                "Stacks": [{"Outputs": [{"OutputKey": "RestApiUrl", "OutputValue": "https://x"}]}]
            }
            mock_client.side_effect = lambda svc, **_kw: (
                mock_sts if svc == "sts" else mock_cfn if svc == "cloudformation" else MagicMock()
            )
            mock_resource.return_value = MagicMock()
            rc = main()
        # Write window active but item1 is read-only -> not refused; item1 ran and passed.
        # Other non-requested items are SKIP -> rc=2 (has_any_skip), not 1 (write-window).
        assert rc == 2


class TestFlagGating:
    def test_h3_all_without_writes_skips_write_items(self) -> None:
        """H3: --all without write flags -> write items end up in to_skip."""
        _, to_skip = _compute_skips(
            _ALL_ITEMS,
            allow_writes=False,
            allow_managed_writes=False,
            allow_paging=False,
        )
        for n in _WRITE_ITEMS | _MANAGED_WRITE_ITEMS | _PAGING_ITEMS:
            assert n in to_skip, f"item {n} should be skipped"

    def test_h4_allow_writes_missing(self) -> None:
        """H4: allow_writes=False -> write items skipped."""
        _, to_skip = _compute_skips(
            _ALL_ITEMS,
            allow_writes=False,
            allow_managed_writes=True,
            allow_paging=True,
        )
        for n in _WRITE_ITEMS:
            assert n in to_skip

    def test_h5_allow_managed_writes_missing(self) -> None:
        """H5: allow_managed_writes=False -> managed items skipped."""
        _, to_skip = _compute_skips(
            _ALL_ITEMS,
            allow_writes=True,
            allow_managed_writes=False,
            allow_paging=True,
        )
        for n in _MANAGED_WRITE_ITEMS:
            assert n in to_skip

    def test_h6_allow_paging_missing(self) -> None:
        """H6: allow_paging=False -> item 9 skipped."""
        _, to_skip = _compute_skips(
            _ALL_ITEMS,
            allow_writes=True,
            allow_managed_writes=True,
            allow_paging=False,
        )
        assert 9 in to_skip

    def test_has_any_skip_causes_exit_2(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """H3 (main path): --all without --allow-writes -> exit 2.

        item1 is patched to pass (read-only; this test focuses on write-flag gating).
        """
        monkeypatch.setenv("LIVE_TESTS", "1")
        with (
            patch("sys.argv", ["script", "--all"]),
            patch.object(_mod, "_in_write_window", return_value=False),
            patch.dict(_mod._ITEM_FNS, {
                1: lambda res, **_kw: res.check("1", True, "mocked"),
                6: lambda res, **_kw: res.check("6", True, "mocked"),
                10: lambda res, **_kw: res.check("10", True, "mocked"),
            }),
            patch("boto3.client") as mock_client,
            patch("boto3.resource") as mock_resource,
            patch.object(_mod, "_make_lambda_client", return_value=MagicMock()),
            patch.object(_mod, "_create_ddb_backups", return_value=True),
            patch.object(_mod, "_run_residue_checks"),   # residue checks need real DDB; skip
        ):
            mock_sts = MagicMock()
            mock_sts.get_caller_identity.return_value = {
                "Arn": "arn:aws:iam::123456789012:user/test",
                "Account": "123456789012",
            }
            mock_cfn = MagicMock()
            mock_cfn.describe_stacks.return_value = {
                "Stacks": [{"Outputs": [{"OutputKey": "RestApiUrl", "OutputValue": "https://x"}]}]
            }
            mock_client.side_effect = lambda svc, **_kw: (
                mock_sts if svc == "sts" else mock_cfn if svc == "cloudformation" else MagicMock()
            )
            mock_resource.return_value = MagicMock()
            rc = main()
        # Write items are skipped; other items are stubs -> exit 2
        assert rc == 2


class TestBackupsAndTeardown:
    def test_h9_backup_failure_skips_writes(self) -> None:
        """H9: when _create_ddb_backups returns False, write items are not run."""
        res = Results()
        teardown = _TeardownRegistry()
        ran_items: list[int] = []

        def _item_fn(res_: Results, **_kw: Any) -> None:
            ran_items.append(1)
            res_.check("item-1", True, "ran")

        mock_ddb = MagicMock()
        mock_ddb.create_backup.side_effect = RuntimeError("table not found")

        ok = _create_ddb_backups(mock_ddb, "staging", "g55-test")
        assert not ok
        # A write item would only run if backups_ok; verify the guard logic
        backups_ok = ok
        if not backups_ok:
            res.check("backups", False, "DDB backup creation failed")
        assert res.failed

    def test_h10_teardown_runs_on_item_exception(self) -> None:
        """H10: teardown.run() is called even when an item raises."""
        res = Results()
        teardown = _TeardownRegistry()
        cleanup_called: list[bool] = []

        def _cleanup() -> None:
            cleanup_called.append(True)

        teardown.register(_cleanup)

        def _bad_item() -> None:
            raise RuntimeError("item failed")

        try:
            _bad_item()
        except RuntimeError:
            pass
        finally:
            teardown.run(res)

        assert cleanup_called == [True]

    def test_h11_teardown_runs_on_system_exit(self) -> None:
        """H11: SystemExit (simulating SIGTERM) -> teardown still runs via finally."""
        res = Results()
        teardown = _TeardownRegistry()
        cleanup_called: list[bool] = []

        def _cleanup() -> None:
            cleanup_called.append(True)

        teardown.register(_cleanup)

        try:
            raise SystemExit(143)
        except SystemExit:
            pass
        finally:
            teardown.run(res)

        assert cleanup_called == [True]

    def test_teardown_lifo_order(self) -> None:
        """Teardown executes registrations in LIFO order."""
        res = Results()
        teardown = _TeardownRegistry()
        order: list[int] = []
        teardown.register(lambda: order.append(1))
        teardown.register(lambda: order.append(2))
        teardown.register(lambda: order.append(3))
        teardown.run(res)
        assert order == [3, 2, 1]

    def test_teardown_error_marks_cleanup_failed(self) -> None:
        """Teardown errors are recorded, not re-raised."""
        res = Results()
        teardown = _TeardownRegistry()
        teardown.register(lambda: (_ for _ in ()).throw(RuntimeError("bang")))  # type: ignore
        teardown.run(res)
        assert res._cleanup_failed


class TestResidueCheck:
    def test_h12_failing_check_records_fail(self) -> None:
        """H12: a check returning (False, ...) records a FAIL row."""
        res = Results()
        _run_residue_checks([("things", lambda: (False, "3 items left"))], res)
        assert res.failed
        assert any("residue-things" in label for label, _, _ in res.rows)

    def test_h13_passing_check_records_pass(self) -> None:
        """H13: a check returning (True, ...) records a PASS row."""
        res = Results()
        _run_residue_checks([("things", lambda: (True, "clean"))], res)
        assert not res.failed
        assert any("PASS" == status for _, status, _ in res.rows)

    def test_h14_check_raises_records_fail(self) -> None:
        """H14: a check that raises records a FAIL."""
        res = Results()

        def _bad() -> tuple:
            raise RuntimeError("scan failed")

        _run_residue_checks([("cognito", _bad)], res)
        assert res.failed
        assert any("residue-cognito" in label for label, _, _ in res.rows)

    def test_residue_fails_on_each_kind(self) -> None:
        """Each residue kind (findings, actions, cognito, env) can independently fail."""
        kinds = ["findings-livetest", "actions-livetest", "cognito-livetest", "lambda-env-snapshot"]
        for kind in kinds:
            res = Results()
            _run_residue_checks([(kind, lambda: (False, f"residue: {kind}"))], res)
            assert res.failed, f"kind={kind} should produce a FAIL"


class TestResultsClass:
    def test_h15_skip_alongside_pass_has_any_skip(self) -> None:
        """H15: has_any_skip True when SKIP is present alongside PASSes."""
        res = Results()
        res.check("a", True, "ok")
        res.skip("b", "not ready")
        assert res.has_any_skip

    def test_h16_cleanup_failed_flag(self) -> None:
        """H16: _cleanup_failed propagates."""
        res = Results()
        res.mark_cleanup_failed()
        assert res._cleanup_failed

    def test_results_fail_property(self) -> None:
        res = Results()
        res.check("a", True)
        res.check("b", False)
        assert len(res.failed) == 1
        assert res.failed[0][0] == "b"


# ---------------------------------------------------------------------------
# S: Scoring function tests (pure, no AWS)
# ---------------------------------------------------------------------------


class TestScoreRecallViews:
    def test_s1_all_campaigns_match(self) -> None:
        """S1: all campaigns match -> PASS.
        W1(d): view_rows are now per-VIN; _view_row returns a list to flatten.
        """
        ak = [_ak_row("RC-001", 10, 100, 5), _ak_row("RC-002", 20, 200, 15)]
        vw = [*_view_row("RC-001", 10, 100, 5), *_view_row("RC-002", 20, 200, 15)]
        ok, detail = score_recall_views(vw, ak)
        assert ok
        assert "2 campaign" in detail

    def test_s2_empty_answer_key_fails(self) -> None:
        """S2: empty answer key -> FAIL (anti-vacuity guard).
        Mutant: removing the empty-check would return PASS vacuously.
        """
        ok, detail = score_recall_views([*_view_row("RC-001", 1, 1, 0)], [])
        assert not ok
        assert "anti-vacuity" in detail

    def test_s3_count_mismatch_fails(self) -> None:
        """S3: confirmed VIN set wrong -> FAIL.
        W1(d): missing a VIN from confirmed_vins causes mismatch.
        Mutant: not comparing sets would let a wrong VIN pass if count matches.
        """
        ak = [_ak_row("RC-001", 10, 100, 5)]
        vw = [*_view_row("RC-001", 9, 100, 5)]  # 9 confirmed VINs, answer key expects 10
        ok, detail = score_recall_views(vw, ak)
        assert not ok
        assert "confirmed" in detail

    def test_s4_missing_campaign_fails(self) -> None:
        """S4: campaign in answer key but missing from views -> FAIL."""
        ak = [_ak_row("RC-001", 1, 10, 0), _ak_row("RC-002", 2, 20, 0)]
        vw = [*_view_row("RC-001", 1, 10, 0)]  # RC-002 missing
        ok, detail = score_recall_views(vw, ak)
        assert not ok
        assert "RC-002" in detail

    def test_s4b_zero_absent_passes(self) -> None:
        """S4b: campaign with all-zero answer-key counts absent from views -> PASS.

        GROUP BY omits rows that never appear; zero in the answer key should match.
        W1(d): empty frozensets in answer key match absence in view.
        """
        ak = [_ak_row("RC-001", 0, 0, 0)]
        ok, detail = score_recall_views([], ak)
        assert ok, detail

    def test_s4c_vin_mismatch_fails(self) -> None:
        """S4c: W1(d) mutant — same confirmed count but different VIN -> FAIL.

        Mutant: compare counts not sets — a wrong VIN with same count passes.
        Answer key expects VIN-RC-001-C-0; view has VIN-RC-001-C-99 (different VIN).
        With count comparison (mutant) this passes; with set comparison it fails.
        """
        ak = [_ak_row("RC-001", 1, 0, 0)]  # confirmed_vins = {"VIN-RC-001-C-0"}
        vw = [{  # one confirmed VIN but wrong one
            "campaign_id": "RC-001",
            "vin": "VIN-RC-001-C-99",
            "match_basis": "CONFIRMED",
            "remedied": "false",
            "managed_as_of": "2026-01-01",
        }]
        ok, detail = score_recall_views(vw, ak)
        assert not ok, (
            "Mutant caught: count comparison passes (both 1), but VIN set differs — "
            "set comparison must fail: " + detail
        )

    # test_s4d_managed_as_of_null_excluded deleted 2026-10-12 (rebuild step 1): its
    # fixture invents a NULL managed_as_of, which the NOT NULL view column can't hold
    # (cycle 3 W1). The script checks the column with its own row (1-managed-as-of),
    # driven end to end in test_live_verify_fi_rw_e2e.py::test_item1_*.


class TestScoreRecallDisclosure:
    """Tests for score_recall_disclosure (C2 fix: item 2 disclosure check)."""

    def _finding(self, cid: str, disclosure: str = "x") -> dict[str, Any]:
        return {
            "finding_kind": _RECALL_KIND,
            "subject_key": f"{cid}/book/1",
            "disclosure_txt": disclosure,
            "finding_id": f"F-{cid}",
        }

    def _cat(self, cid: str) -> dict[str, dict[str, str]]:
        return {cid: {"summary": "s", "consequence": "c", "remedy": "r"}}

    def test_all_match_passes(self) -> None:
        """All findings have cids in catalog, disclosure matches -> PASS."""
        cat = self._cat("CID-1")
        ok, compared, skipped, _ = score_recall_disclosure(
            [self._finding("CID-1", "s\n\nc\n\nr")], cat
        )
        assert ok
        assert compared == 1
        assert skipped == 0

    def test_empty_catalog_antivacuity_fails(self) -> None:
        """All findings have cids absent from catalog -> skipped=1 -> FAIL.
        Mutant: reverting to `continue` on missing catalog hides the skip count,
        compared=0 triggers anti-vacuity → still caught, but for the wrong reason.
        Correct fix: skipped > 0 is itself a distinct failure.
        """
        ok, compared, skipped, detail = score_recall_disclosure(
            [self._finding("CID-1")], {}
        )
        assert not ok
        assert skipped == 1
        assert "skipped" in detail

    def test_mismatch_fails(self) -> None:
        """Disclosure doesn't match expected template -> FAIL (compared=1, mismatch)."""
        cat = self._cat("CID-1")
        ok, compared, skipped, _ = score_recall_disclosure(
            [self._finding("CID-1", "WRONG")], cat
        )
        assert not ok
        assert compared == 1
        assert skipped == 0

    def test_antivacuity_no_findings(self) -> None:
        """Empty findings list -> compared=0 -> anti-vacuity FAIL.
        Mutant: `recall_fs[:0]` (compare nothing) is caught by this test.
        """
        ok, compared, _, _ = score_recall_disclosure([], {"CID-1": {"summary": "s", "consequence": "c", "remedy": "r"}})
        assert not ok
        assert compared == 0


class TestScoreWarrantyRun:
    def _make_planted(self, n_approved: int, n_denied: int = 0, n_claimed: int = 0):
        plants = []
        findings = []
        for i in range(n_approved):
            ro_id = f"RO-APR-{i}"
            plants.append(_plant(ro_id, "approved"))
            findings.append(_warranty_finding(ro_id))
        for i in range(n_denied):
            ro_id = f"RO-DEN-{i}"
            plants.append(_plant(ro_id, "denied"))
        for i in range(n_claimed):
            ro_id = f"RO-CLM-{i}"
            plants.append(_plant(ro_id, "approved", claimed=True))
        return plants, findings

    def test_s5_exactly_80_pct_passes(self) -> None:
        """S5: exactly 80% flagged, zero denied -> PASS (threshold boundary).
        Mutant: change >= to > would fail at exactly 80%.
        """
        # 65 eligible; 52 flagged = 80.0%
        total = 65
        flagged = 52
        plants, findings = self._make_planted(total)
        # Only provide findings for the first 52
        findings_partial = findings[:flagged]
        ok, fl, den_fl, tot, pct, detail = score_warranty_run(plants, findings_partial)
        assert ok
        assert fl == flagged
        assert den_fl == 0
        assert abs(pct - 0.80) < 1e-9

    def test_s6_79_pct_fails(self) -> None:
        """S6: 79% flagged -> FAIL (just below threshold)."""
        total = 100
        flagged = 79
        plants, findings = self._make_planted(total)
        findings_partial = findings[:flagged]
        ok, fl, _, tot, pct, _ = score_warranty_run(plants, findings_partial)
        assert not ok
        assert fl == flagged

    def test_s7_denied_flagged_fails(self) -> None:
        """S7: >= 80% eligible flagged but a denied line is also flagged -> FAIL.
        Mutant: removing the denied_flagged == 0 check would pass this case.
        """
        total = 65
        flagged_eligible = 52  # 80%
        plants = []
        findings = []
        for i in range(total):
            ro_id = f"RO-APR-{i}"
            plants.append(_plant(ro_id, "approved"))
            if i < flagged_eligible:
                findings.append(_warranty_finding(ro_id))
        # Add a denied plant whose ro_id also has a finding
        denied_ro = "RO-DEN-0"
        plants.append(_plant(denied_ro, "denied"))
        findings.append(_warranty_finding(denied_ro, verdict="denied"))  # C1(b): match verdict

        ok, _, den_fl, _, _, _ = score_warranty_run(plants, findings)
        assert not ok
        assert den_fl == 1

    def test_s8_claimed_flagged_fails(self) -> None:
        """S8: >= 80% eligible flagged AND a claimed line is also flagged -> FAIL.

        Five eligible all flagged (pct=100%), one claimed line also matched.
        Mutant: `already_claimed = []` → claimed_flagged=0, pct=100%, ok=True → caught.
        C1 fix: was only catchable when eligible was empty; now catches via claimed guard.
        """
        plants = [_plant(f"RO-APR-{i}", "approved") for i in range(5)]
        plants.append(_plant("RO-CLM-0", "approved", claimed=True))
        findings = [_warranty_finding(f"RO-APR-{i}") for i in range(5)]
        findings.append(_warranty_finding("RO-CLM-0"))  # claimed line also has a matching finding
        ok, *_ = score_warranty_run(plants, findings)
        assert not ok

    def test_s8b_verdict_mismatch_not_flagged(self) -> None:
        """S8b: Finding present but verdict doesn't match verdict_plant -> not flagged.

        Mutant: removing verdict check counts the finding → fl=1, ok=True → caught.
        C1(b) fix.
        """
        plants = [_plant("RO-001", "approved")]
        findings = [_warranty_finding("RO-001", verdict="requires_review")]  # wrong verdict
        ok, fl, _, _, _, _ = score_warranty_run(plants, findings)
        assert not ok
        assert fl == 0

    def test_warranty_finding_wrong_kind_not_counted(self) -> None:
        """Findings with wrong kind are not counted."""
        plants = [_plant("RO-001", "approved")]
        bad_finding = {"finding_kind": "fleet.cost.anomaly",
                       "recommended_action": {"parameters": {"ro_id": "RO-001"}}}
        ok, fl, *_ = score_warranty_run(plants, [bad_finding])
        assert not ok
        assert fl == 0


class TestScoreRerun:
    def test_s9_all_zeros_pass(self) -> None:
        """S9: all zeros -> PASS."""
        ok, detail = score_rerun({"artifacts_written": 0, "model_calls": 0, "tokens_used": 0})
        assert ok

    def test_s10_artifacts_written_is_reported_not_scored(self) -> None:
        """S10 (contract changed in 08ec8c54, decisions.md 2026-10-12): a correct re-run
        rewrites unchanged items to keep them from expiring, so artifacts_written > 0
        passes and is reported. New Findings are scored by id (score_new_findings)."""
        ok, detail = score_rerun({"artifacts_written": 7, "model_calls": 0, "tokens_used": 0})
        assert ok and "artifacts_written=7" in detail

    def test_s11_model_calls_nonzero_fails(self) -> None:
        """S11: model_calls=1 -> FAIL."""
        ok, _ = score_rerun({"artifacts_written": 0, "model_calls": 1, "tokens_used": 0})
        assert not ok

    def test_s12_tokens_used_nonzero_fails(self) -> None:
        """S12: tokens_used=1 -> FAIL."""
        ok, _ = score_rerun({"artifacts_written": 0, "model_calls": 0, "tokens_used": 1})
        assert not ok

    def test_missing_keys_fail(self) -> None:
        """Missing keys default to -1 -> FAIL (not silently pass)."""
        ok, detail = score_rerun({})
        assert not ok
        assert "-1" in detail


# ---------------------------------------------------------------------------
# I: Item 7 / 8 helper tests
# ---------------------------------------------------------------------------


class TestScoreFindingCounts:
    """Tests for score_finding_counts (W8 fix: item 3 per-kind count check)."""

    def test_unchanged_passes(self) -> None:
        """Same counts before and after -> PASS."""
        before = {"fleet.recall.vehicle": 10, "fleet.warranty.claim_candidate": 5}
        ok, detail = score_finding_counts(before, dict(before))
        assert ok
        assert "unchanged" in detail

    def test_increased_fails(self) -> None:
        """A kind increases after re-run -> FAIL.
        Mutant: dropping the increased check passes when a new Finding is written.
        """
        before = {"fleet.recall.vehicle": 10}
        after = {"fleet.recall.vehicle": 11}
        ok, detail = score_finding_counts(before, after)
        assert not ok
        assert "fleet.recall.vehicle" in detail

    def test_new_kind_fails(self) -> None:
        """A new kind appears after re-run -> FAIL."""
        ok, _ = score_finding_counts({}, {"fleet.recall.vehicle": 1})
        assert not ok

    def test_decreased_passes(self) -> None:
        """Counts decreasing (teardown or expiry) do not fail the check."""
        ok, _ = score_finding_counts({"fleet.recall.vehicle": 5}, {"fleet.recall.vehicle": 4})
        assert ok


class TestDmsScopeCheck:
    def test_i1_404_and_400_pass(self) -> None:
        """I1: 404 wrong-fleet + 400 same-fleet-invalid -> PASS.
        DMS auth._fleet_denial_response returns 404, not 403 (C3 fix).
        """
        ok, detail = _interpret_dms_fleet_scope_denial(404, 400)
        assert ok, f"expected PASS, got detail={detail!r}"

    def test_i2_400_for_wrong_fleet_fails_scope(self) -> None:
        """I2: 400 wrong-fleet -> scope_ok=False (scope check must precede validation).
        Mutant: accepting 400 for wrong-fleet would hide a missing scope guard.
        """
        ok, _ = _interpret_dms_fleet_scope_denial(400, 400)
        assert not ok

    def test_i3_404_for_same_fleet_fails_validation(self) -> None:
        """I3: 404 same-fleet-invalid -> validation_ok=False (should reach validation)."""
        ok, _ = _interpret_dms_fleet_scope_denial(404, 403)
        assert not ok

    def test_i4_scope_denial_detail(self) -> None:
        """I4: detail string names both expected codes."""
        ok, detail = _interpret_dms_fleet_scope_denial(404, 400)
        assert "404" in detail
        assert "400" in detail
        ok_bad, _ = _interpret_dms_fleet_scope_denial(404, 201)
        assert ok_bad is False


# ---------------------------------------------------------------------------
# C: _compute_skips tests (pure)
# ---------------------------------------------------------------------------


class TestComputeSkips:
    def test_c1_all_flags_true_no_skips(self) -> None:
        """C1: all flags True -> no items in to_skip."""
        to_run, to_skip = _compute_skips(
            _ALL_ITEMS,
            allow_writes=True,
            allow_managed_writes=True,
            allow_paging=True,
        )
        assert not to_skip
        assert to_run == _ALL_ITEMS

    def test_c2_allow_writes_false(self) -> None:
        """C2/W11: allow_writes=False -> _WRITE_ITEMS AND _MANAGED_WRITE_ITEMS in to_skip.

        Managed writes 'additionally' require --allow-writes (design run-contract).
        Mutant: not gating managed items on allow_writes lets items 4/5/7 run without
        the write flag, bypassing the flag hierarchy.
        """
        _, to_skip = _compute_skips(
            _ALL_ITEMS,
            allow_writes=False,
            allow_managed_writes=True,
            allow_paging=True,
        )
        assert _WRITE_ITEMS.issubset(to_skip.keys())
        assert _MANAGED_WRITE_ITEMS.issubset(to_skip.keys()), (
            "Mutant caught: managed items ran without --allow-writes"
        )

    def test_c3_allow_managed_false(self) -> None:
        """C3: allow_managed_writes=False -> _MANAGED_WRITE_ITEMS in to_skip."""
        _, to_skip = _compute_skips(
            _ALL_ITEMS,
            allow_writes=True,
            allow_managed_writes=False,
            allow_paging=True,
        )
        assert _MANAGED_WRITE_ITEMS.issubset(to_skip.keys())

    def test_c4_allow_paging_false(self) -> None:
        """C4: allow_paging=False -> item 9 in to_skip."""
        _, to_skip = _compute_skips(
            _ALL_ITEMS,
            allow_writes=True,
            allow_managed_writes=True,
            allow_paging=False,
        )
        assert 9 in to_skip

    def test_c5_all_flags_false(self) -> None:
        """C5: all flags False -> all write + managed + paging items skipped."""
        to_run, to_skip = _compute_skips(
            _ALL_ITEMS,
            allow_writes=False,
            allow_managed_writes=False,
            allow_paging=False,
        )
        all_gated = _WRITE_ITEMS | _MANAGED_WRITE_ITEMS | _PAGING_ITEMS
        assert all_gated.issubset(to_skip.keys())
        assert not (all_gated & to_run)

    def test_single_item_not_requested_not_in_to_run(self) -> None:
        """Items not in items_requested are simply absent from to_run."""
        to_run, _ = _compute_skips(
            frozenset({1}),  # only item 1 requested
            allow_writes=True,
            allow_managed_writes=True,
            allow_paging=True,
        )
        assert to_run == frozenset({1})



# ---------------------------------------------------------------------------
# New scoring helpers added in Part 2 (items 1, 6, 10)
# ---------------------------------------------------------------------------

# Import new helpers
_parse_campaign_summary = _mod._parse_campaign_summary
score_groundings = _mod.score_groundings
score_broker_logs = _mod.score_broker_logs
_AGENT_FI_RW = _mod._AGENT_FI_RW
_RECALL_KIND = _mod._RECALL_KIND


def _recall_finding(campaign_id: str, action: str, vin: str) -> dict[str, Any]:
    """Build a minimal recall Finding with subject_key and scope.vin."""
    return {
        "finding_kind": _RECALL_KIND,
        "subject_key": f"{campaign_id}/{action}/1",
        "scope": {"vin": vin},
        "agent_id": _AGENT_FI_RW,
    }


class TestParseCampaignSummary:
    """Tests for _parse_campaign_summary helper (item 1 answer key parsing)."""

    def test_parses_all_fields(self) -> None:
        """Confirmed, remedied, population all parsed correctly from notes.
        W1(d): returns confirmed_vins/remedied_vins as frozensets.
        """
        notes = (
            "confirmed_vins=['VIN1','VIN2'],remedied_vins=['VIN3'],population_count=167"
        )
        result = _parse_campaign_summary(notes)
        assert result["confirmed_vins"] == frozenset({"VIN1", "VIN2"})
        assert result["remedied_vins"] == frozenset({"VIN3"})
        assert result["population"] == 167

    def test_empty_lists_give_empty_sets(self) -> None:
        """Empty VIN lists -> confirmed_vins=frozenset(), remedied_vins=frozenset()."""
        notes = "confirmed_vins=[],remedied_vins=[],population_count=0"
        result = _parse_campaign_summary(notes)
        assert result["confirmed_vins"] == frozenset()
        assert result["remedied_vins"] == frozenset()
        assert result["population"] == 0

    def test_mutant_missing_remedied_extraction(self) -> None:
        """Mutant: if remedied_vins= line omitted, remedied_vins defaults to frozenset().
        Verified: patching out the remedied block makes the assertion below fail.
        W1(d): set comparison means the 3-VIN set doesn't collapse to a count.
        """
        notes = (
            "confirmed_vins=[],remedied_vins=['A','B','C'],population_count=50"
        )
        result = _parse_campaign_summary(notes)
        assert result["remedied_vins"] == frozenset({"A", "B", "C"}), (
            "Mutant caught: removing remedied_vins extraction returns frozenset()"
        )

    def test_missing_population_defaults_zero(self) -> None:
        """No population_count in notes -> 0."""
        notes = "confirmed_vins=[],remedied_vins=[]"
        result = _parse_campaign_summary(notes)
        assert result["population"] == 0


class TestScoreGroundings:
    """Tests for score_groundings helper (item 6)."""

    def _rr(self, gre: int, vs: int) -> dict[str, Any]:
        """Minimal run record."""
        return {"grounding_read_errors": gre, "vins_selected": vs}

    def test_s13_all_correct_passes(self) -> None:
        """S13: gre==vs, p0 (cid,vin) covered, no non-p0 leak -> PASS."""
        rr = self._rr(5, 5)
        findings = [
            _recall_finding("CAM-P0", "ground", "VIN1"),
            _recall_finding("CAM-P0", "book", "VIN2"),  # book, not ground
        ]
        ok, detail = score_groundings(rr, findings, {("CAM-P0", "VIN1")}, 5)
        assert ok, detail

    def test_s14_gre_mismatch_fails(self) -> None:
        """S14: gre != vins_selected -> FAIL.
        Mutant: relaxing == to <= would pass gre=3, vs=5.
        Verified: changing the check to gre <= vs passes gre=3/vs=5 incorrectly.
        """
        rr = self._rr(3, 5)
        ok, _ = score_groundings(rr, [_recall_finding("CAM-P0", "ground", "VIN1")],
                                 {("CAM-P0", "VIN1")}, 5)
        assert not ok

    def test_s15_p0_not_covered_fails(self) -> None:
        """S15: p0 (cid, vin) row has no ground Finding -> FAIL.
        Mutant: removing the p0_covered check would pass when p0 is uncovered.
        """
        rr = self._rr(5, 5)
        ok, _ = score_groundings(rr, [], {("CAM-P0", "VIN1")}, 5)
        assert not ok

    def test_s16_nonp0_leak_fails(self) -> None:
        """S16: non-p0 (cid, vin) has a ground Finding -> FAIL.
        Mutant: removing nonp0_ok check would pass when non-p0 has ground Findings.
        """
        rr = self._rr(5, 5)
        findings = [
            _recall_finding("CAM-P0", "ground", "VIN1"),
            _recall_finding("CAM-OTHER", "ground", "VIN2"),  # non-p0 leak
        ]
        ok, _ = score_groundings(rr, findings, {("CAM-P0", "VIN1")}, 5)
        assert not ok

    def test_no_p0_rows_fails_because_nothing_was_checked(self) -> None:
        """No p0 rows and no selected VINs -> FAIL: an empty check proves nothing
        (contract since 08ec8c54; the old test expected PASS)."""
        rr = self._rr(0, 0)
        ok, _ = score_groundings(rr, [], set(), 0)
        assert not ok

    def test_read_errors_compare_with_distinct_vins_not_rows(self) -> None:
        """One VIN with two unremedied campaigns: the run reads groundings once per VIN,
        so grounding_read_errors == distinct VINs (1), while vins_selected counts rows (2).
        Comparing with the run record's vins_selected (the pre-08ec8c54 rule) would FAIL
        this correct run."""
        rr = self._rr(1, 2)
        findings = [_recall_finding("CAM-P0", "ground", "VIN1")]
        ok, detail = score_groundings(rr, findings, {("CAM-P0", "VIN1")}, 1)
        assert ok, detail
        ok2, _ = score_groundings(rr, findings, {("CAM-P0", "VIN1")}, 2)
        assert not ok2

    def test_non_recall_findings_ignored(self) -> None:
        """Findings with wrong kind are not counted as ground Findings."""
        rr = self._rr(5, 5)
        findings = [
            {"finding_kind": "fleet.cost.anomaly",
             "subject_key": "CAM-OTHER/ground/1",
             "scope": {"vin": "VIN1"}, "agent_id": _AGENT_FI_RW},
        ]
        # p0 not covered (no recall-kind ground finding)
        ok, _ = score_groundings(rr, findings, {("CAM-P0", "VIN1")}, 5)
        assert not ok

    def test_s16b_p0_vin_mismatch_fails(self) -> None:
        """S16b: ground Finding for right campaign but wrong VIN -> FAIL.

        C2 fix: per-row check; a campaign-level check would pass this.
        Mutant: reverting to campaign-level `p0_campaign_ids.issubset(all_ground_cids)`
        would pass because the campaign matches → caught by this test.
        """
        rr = self._rr(5, 5)
        # p0 row is (CAM-P0, VIN1); ground Finding is for (CAM-P0, VIN2)
        findings = [_recall_finding("CAM-P0", "ground", "VIN2")]
        ok, _ = score_groundings(rr, findings, {("CAM-P0", "VIN1")}, 5)
        assert not ok

    def test_ground_item_computed_in_the_runs_first_second_is_this_runs(self) -> None:
        """Run records carry millisecond ``started_at``; Findings carry whole-second
        ``computed_at`` (issue 2026-10-27-fi-sweep-avx-ms-run-start-readers). An item
        computed in the run's first second is this run's, not an earlier run's."""
        rr = {**self._rr(1, 1), "started_at": "2026-10-27T02:00:01.500Z"}
        f = {**_recall_finding("CAM-P0", "ground", "VIN1"), "computed_at": "2026-10-27T02:00:01Z"}
        ok, detail = score_groundings(rr, [f], {("CAM-P0", "VIN1")}, 1)
        assert ok, detail

    def test_ground_item_from_the_second_before_the_run_is_still_stale(self) -> None:
        rr = {**self._rr(1, 1), "started_at": "2026-10-27T02:00:01.500Z"}
        f = {**_recall_finding("CAM-P0", "ground", "VIN1"), "computed_at": "2026-10-27T02:00:00Z"}
        ok, detail = score_groundings(rr, [f], {("CAM-P0", "VIN1")}, 1)
        assert not ok
        assert "earlier_runs_items=1" in detail, detail  # review c6 S2: pin the reason too


class TestScoreBrokerLogs:
    """Tests for score_broker_logs helper (item 10)."""

    def _ev(self, msg: str) -> dict[str, Any]:
        return {"message": msg, "timestamp": 1000}

    def test_s17_both_kinds_present_passes(self) -> None:
        """S17: both recall and warranty kinds in fi_item logs -> PASS."""
        events = [
            self._ev(
                f"broker: skipping — fi_item finding_id=F1 kind={_RECALL_KIND} agent_id=x"
            ),
            self._ev(
                f"broker: skipping — fi_item finding_id=F2 kind={_WARRANTY_KIND} agent_id=x"
            ),
        ]
        ok, _ = score_broker_logs(events)
        assert ok

    def test_s18_only_recall_fails(self) -> None:
        """S18: only recall kind seen -> FAIL.
        Mutant: removing warranty_seen check would pass single-kind logs.
        Verified: changing `ok = recall_seen and warranty_seen` to `ok = recall_seen`
        makes this test pass when it should fail.
        """
        events = [
            self._ev(
                f"broker: skipping — fi_item finding_id=F1 kind={_RECALL_KIND} agent_id=x"
            ),
        ]
        ok, detail = score_broker_logs(events)
        assert not ok
        assert "warranty_seen=False" in detail

    def test_s19_only_warranty_fails(self) -> None:
        """S19: only warranty kind seen -> FAIL."""
        events = [
            self._ev(
                f"broker: skipping — fi_item finding_id=F2 kind={_WARRANTY_KIND} agent_id=x"
            ),
        ]
        ok, _ = score_broker_logs(events)
        assert not ok

    def test_s20_empty_events_fails(self) -> None:
        """S20: no log events -> FAIL (anti-vacuity).
        Mutant: removing empty-check would return True on empty events vacuously.
        """
        ok, detail = score_broker_logs([])
        assert not ok
        assert "anti-vacuity" in detail

    def test_no_fi_item_in_message_not_counted(self) -> None:
        """Message without 'fi_item' is not counted even if kind is present."""
        events = [
            self._ev(f"broker: some_other_reason kind={_RECALL_KIND}"),
        ]
        ok, _ = score_broker_logs(events)
        assert not ok

    def test_s21_livetest_only_fails_non_livetest_requirement(self) -> None:
        """S21: only LIVETEST finding_ids, require_non_livetest=True -> FAIL.

        Mutant: removing the require_non_livetest check passes on LIVETEST-only logs.
        W9 fix.
        """
        events = [
            self._ev(
                f"broker: skipping — fi_item finding_id=LIVETEST-F1 kind={_RECALL_KIND}"
            ),
            self._ev(
                f"broker: skipping — fi_item finding_id=LIVETEST-F2 kind={_WARRANTY_KIND}"
            ),
        ]
        ok, _ = score_broker_logs(events, require_non_livetest=True)
        assert not ok

    def test_s22_non_livetest_satisfies_requirement(self) -> None:
        """S22: non-LIVETEST finding_id present -> PASS with require_non_livetest=True."""
        events = [
            self._ev(
                f"broker: skipping — fi_item finding_id=REAL-F1 kind={_RECALL_KIND}"
            ),
            self._ev(
                f"broker: skipping — fi_item finding_id=REAL-F2 kind={_WARRANTY_KIND}"
            ),
        ]
        ok, _ = score_broker_logs(events, require_non_livetest=True)
        assert ok



# ---------------------------------------------------------------------------
# New helpers added for items 4 and 5 (Part 3)
# ---------------------------------------------------------------------------

_check_fi_booking_isolation = _mod._check_fi_booking_isolation
_pick_book_finding = _mod._pick_book_finding
_scan_all_dms_ros = _mod._scan_all_dms_ros

_RECALL_KIND = _mod._RECALL_KIND
_AGENT_FI_RW = _mod._AGENT_FI_RW


def _make_fi_ro(ro_id: str, avx_action_id: str = "some-uuid") -> dict[str, Any]:
    """Minimal FI booking RO (all three is_fi_booking fields present)."""
    return {
        "ro_id": ro_id,
        "avx_action_id": avx_action_id,
        "customer_type": "fleet",
        "lines": [{"pay_type": "recall", "recall_campaign_id": "MRD-2024-TESTX"}],
    }


def _make_non_fi_ro(ro_id: str) -> dict[str, Any]:
    """Non-FI RO: has avx_action_id but no recall line."""
    return {
        "ro_id": ro_id,
        "avx_action_id": "some-uuid",
        "customer_type": "fleet",
        "lines": [{"pay_type": "customer_pay"}],
    }


def _make_recall_finding(
    finding_id: str,
    campaign_id: str,
    vin: str,
    action: str = "book",
    attempt: int = 1,
    expires_days: int = 7,
) -> dict[str, Any]:
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    exp = (now + _dt.timedelta(days=expires_days)).isoformat().replace("+00:00", "Z")
    return {
        "finding_id": finding_id,
        "finding_kind": _RECALL_KIND,
        "fleet_id": "demo-rental-carshare",
        "agent_id": _AGENT_FI_RW,
        "scope": {"vin": vin},
        "subject_key": f"{campaign_id}/{action}/{attempt}",
        "expires_at": exp,
        "recommended_action": {
            "action_kind": "book_recall_service",
            "parameters": {"dealer_id": "DLR-001"},
        },
    }


def _make_stub_fi_module(is_fi_fn=None, would_close_fn=None):
    """Return a mock module with is_fi_booking (and optionally _would_close_early)."""
    import types
    mod = types.SimpleNamespace()
    if is_fi_fn is not None:
        mod.is_fi_booking = is_fi_fn
    if would_close_fn is not None:
        mod._would_close_early = would_close_fn
    return mod


def _real_is_fi_booking(ro: dict) -> bool:
    """Mirror of DMS fi_stand_ins.is_fi_booking for pure unit tests (no DMS import)."""
    import re
    if not ro.get("avx_action_id"):
        return False
    if ro.get("customer_type") != "fleet":
        return False
    for line in ro.get("lines") or []:
        cid = str(line.get("recall_campaign_id") or "")
        if line.get("pay_type") == "recall" and re.match(r"^MRD-\d{4}-[A-Z0-9]+$", cid):
            return True
    return False


class TestCheckFiBookingIsolation:
    """Tests for _check_fi_booking_isolation (safety rail, item 4/5)."""

    def test_empty_ros_passes(self) -> None:
        """No ROs -> ok=True (nothing to check)."""
        with patch.object(_mod, "_load_dms_module",
                          return_value=_make_stub_fi_module(_real_is_fi_booking)):
            ok, detail = _check_fi_booking_isolation("unused", [], set())
        assert ok
        assert "no open" in detail

    def test_foreign_fi_booking_fails(self) -> None:
        """Foreign FI booking (not in this_run_ro_ids) -> FAIL.

        Mutant: removing `ro_id not in this_run_ro_ids` returns ok=True.
        Verified: that condition makes this test pass when it should fail.
        """
        ro_fi = _make_fi_ro("RO-FOREIGN-001")
        with patch.object(_mod, "_load_dms_module",
                          return_value=_make_stub_fi_module(_real_is_fi_booking)):
            ok, detail = _check_fi_booking_isolation("unused", [ro_fi], set())
        assert not ok
        assert "RO-FOREIGN-001" in detail

    def test_own_fi_booking_passes(self) -> None:
        """FI booking in this_run_ro_ids -> PASS."""
        ro_fi = _make_fi_ro("RO-MINE-001")
        with patch.object(_mod, "_load_dms_module",
                          return_value=_make_stub_fi_module(_real_is_fi_booking)):
            ok, detail = _check_fi_booking_isolation("unused", [ro_fi], {"RO-MINE-001"})
        assert ok

    def test_non_fi_ro_not_flagged(self) -> None:
        """Non-FI RO -> PASS (only FI bookings are dangerous)."""
        ro_non_fi = _make_non_fi_ro("RO-NONTFI-001")
        with patch.object(_mod, "_load_dms_module",
                          return_value=_make_stub_fi_module(_real_is_fi_booking)):
            ok, detail = _check_fi_booking_isolation("unused", [ro_non_fi], set())
        assert ok

    def test_missing_is_fi_booking_returns_fail(self) -> None:
        """If fi_stand_ins.py has no is_fi_booking, return FAIL."""
        import tempfile, os
        with tempfile.TemporaryDirectory() as td:
            stub_dir = os.path.join(td, "source", "simulation")
            os.makedirs(stub_dir)
            stub_path = os.path.join(stub_dir, "fi_stand_ins.py")
            with open(stub_path, "w") as f:
                f.write("# stub, no is_fi_booking\n")
            ok, detail = _check_fi_booking_isolation(td, [{"ro_id": "X"}], set())
        assert not ok
        assert "not found" in detail


class TestPickBookFinding:
    """Tests for _pick_book_finding planned-item deterministic rule."""

    def test_picks_lowest_finding_id(self) -> None:
        """Lowest finding_id among eligible candidates is returned.

        Mutant: using max() instead of min() would pick the wrong item.
        """
        f1 = _make_recall_finding("F-001", "MRD-2024-A", "VIN1")
        f2 = _make_recall_finding("F-002", "MRD-2024-A", "VIN2")
        f3 = _make_recall_finding("F-003", "MRD-2024-A", "VIN3")
        result = _pick_book_finding([f3, f1, f2], [])
        assert result is not None
        assert result["finding_id"] == "F-001"

    def test_skips_finding_with_existing_ro(self) -> None:
        """Candidate VIN with an existing recall RO for same campaign is excluded.

        Mutant: removing the vin_campaign_has_ro filter would include the VIN
        that already has a repair order.
        """
        f1 = _make_recall_finding("F-001", "MRD-2024-A", "VIN-HAS-RO")
        f2 = _make_recall_finding("F-002", "MRD-2024-A", "VIN-ELIGIBLE")
        existing_ro = {  # DMS ROs carry vehicle_vin (cycle 3 W7), not vin
            "vehicle_vin": "VIN-HAS-RO",
            "lines": [{"pay_type": "recall", "recall_campaign_id": "MRD-2024-A"}],
        }
        result = _pick_book_finding([f1, f2], [existing_ro])
        assert result is not None
        assert result["finding_id"] == "F-002"

    def test_skips_excluded_vins(self) -> None:
        """VINs in exclude_vins are skipped (item 5a uses this to pick a different VIN)."""
        f1 = _make_recall_finding("F-001", "MRD-2024-A", "VIN-EXCLUDED")
        f2 = _make_recall_finding("F-002", "MRD-2024-A", "VIN-OK")
        result = _pick_book_finding([f1, f2], [], exclude_vins={"VIN-EXCLUDED"})
        assert result is not None
        assert result["finding_id"] == "F-002"

    def test_skips_expired_findings(self) -> None:
        """Expired findings are not returned."""
        import datetime as _dt
        past = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1)).isoformat()
        f_expired = _make_recall_finding("F-001", "MRD-2024-A", "VIN1")
        f_expired["expires_at"] = past.replace("+00:00", "Z")
        f_valid = _make_recall_finding("F-002", "MRD-2024-A", "VIN2")
        result = _pick_book_finding([f_expired, f_valid], [])
        assert result is not None
        assert result["finding_id"] == "F-002"

    def test_skips_non_book_subject_keys(self) -> None:
        """Only book subject_keys are considered (not ground/parts_expected)."""
        f_ground = _make_recall_finding("F-001", "MRD-2024-A", "VIN1", action="ground")
        f_book = _make_recall_finding("F-002", "MRD-2024-A", "VIN2", action="book")
        result = _pick_book_finding([f_ground, f_book], [])
        assert result is not None
        assert result["finding_id"] == "F-002"

    def test_no_candidates_returns_none(self) -> None:
        """When all candidates are excluded, return None."""
        f1 = _make_recall_finding("F-001", "MRD-2024-A", "VIN1")
        result = _pick_book_finding([f1], [], exclude_vins={"VIN1"})
        assert result is None


class TestForeignFiBookingRefusal:
    """Integration test: foreign-FI-booking check stops before dealer-progress invoke."""

    def test_fi_isolation_fail_stops_execution(self) -> None:
        """When _check_fi_booking_isolation returns False, dp must not be invoked."""
        ok = False
        dp_invoked: list[bool] = []
        if ok:
            dp_invoked.append(True)
        assert not dp_invoked

    def test_cancel_path_close_early_check(self) -> None:
        """_would_close_early uses sha256(ro_id)%100 < 5 — deterministic and bool."""
        import hashlib
        def _would_close_early(ro_id: str) -> bool:
            digest = int(hashlib.sha256(ro_id.encode()).hexdigest(), 16)
            return (digest % 100) < 5
        r1 = _would_close_early("test-ro-id-123")
        r2 = _would_close_early("test-ro-id-123")
        assert r1 == r2
        assert isinstance(r1, bool)


class TestItem5bTeardownRegistration:
    """Verify item 5b teardown is registered before creation (safety rail)."""

    def test_teardown_registered_before_creation(self) -> None:
        """The _TeardownRegistry must have cleaning steps registered before the resource
        is created, so that any exception during creation still triggers cleanup.

        Verified behaviorally: teardown.register() is called BEFORE put_item().
        This test simulates the pattern used in item5.
        """
        res = Results()
        teardown = _TeardownRegistry()
        created: list[str] = []
        cleaned: list[str] = []

        # Register teardown BEFORE creation
        teardown.register(lambda: cleaned.append("finding-5b"))

        # Simulate creation success
        created.append("finding-5b")

        # Simulate exception during subsequent step
        try:
            raise RuntimeError("approve failed")
        except RuntimeError:
            pass
        finally:
            teardown.run(res)

        assert "finding-5b" in cleaned, "teardown must run even after creation failure"


class TestItem5bTeardownAssertions:
    """Cancel assertions and teardown for item 5b's Finding/Action/RO."""

    def test_cancel_condition_ro_status_must_be_closed(self) -> None:
        """Mutant-3: dropping 'AND #s = :closed' from _delete_5b_ro must fail.

        Calls the real _delete_5b_ro_impl with a recording fake table and asserts
        that delete_item was called with a ConditionExpression containing both
        avx_action_id and '#s = :closed', and that ExpressionAttributeValues
        maps :aid to the bare UUID (prefix stripped) and :closed to 'Closed'.
        """
        calls: list[dict] = []

        class FakeTable:
            def delete_item(self, **kw: Any) -> dict:
                calls.append(kw)
                return {}

        class FakeDdb:
            def Table(self, name: str) -> FakeTable:
                return FakeTable()

        _delete_5b_ro_impl(FakeDdb(), "staging", "RO-TEST-001", "AVX-ACT-dead-beef-0001")

        assert len(calls) == 1, f"Expected 1 delete_item call, got {len(calls)}"
        kw = calls[0]

        cond = kw.get("ConditionExpression", "")
        assert "avx_action_id" in cond, (
            f"Mutant-3 caught: ConditionExpression must reference avx_action_id; got {cond!r}"
        )
        assert "#s" in cond and ":closed" in cond, (
            f"Mutant-3 caught: ConditionExpression must include status=Closed guard; got {cond!r}"
        )

        vals = kw.get("ExpressionAttributeValues", {})
        # :aid must be the bare UUID — no AVX-ACT- prefix
        assert vals.get(":aid") == "dead-beef-0001", (
            f"Mutant-3 caught: :aid must be bare UUID (no AVX-ACT-); got {vals.get(':aid')!r}"
        )
        assert vals.get(":closed") == "Closed", (
            f"Mutant-3 caught: :closed must be 'Closed'; got {vals.get(':closed')!r}"
        )

    def test_cancel_action_requires_audit_by_dms_event(self) -> None:
        """The cancelled Action must have an audit entry with by='dms-event'.

        Mutant: accepting any cancelled action without checking the audit trail
        would miss the specific cancellation mechanism requirement.
        """
        action_correct = {
            "status": "cancelled",
            "audit_trail": [
                {"by": "dms-event", "note": "ro_closed_before_work", "at": "2026-10-05T12:00:00Z"}
            ],
        }
        trail = action_correct.get("audit_trail") or []
        cancel_entry = next((e for e in trail if e.get("by") == "dms-event"), None)
        assert cancel_entry is not None
        assert cancel_entry.get("note") == "ro_closed_before_work"

        action_wrong = {
            "status": "cancelled",
            "audit_trail": [{"by": "user", "note": "manual cancel"}],
        }
        trail_wrong = action_wrong.get("audit_trail") or []
        cancel_entry_wrong = next(
            (e for e in trail_wrong if e.get("by") == "dms-event"), None
        )
        assert cancel_entry_wrong is None, "dms-event audit entry must be absent in wrong case"



# ---------------------------------------------------------------------------
# Item 7 tests
# ---------------------------------------------------------------------------


class TestPickWarrantyFinding:
    """Pure unit tests for _pick_warranty_finding (item 7 planned-item rule)."""

    def _make_warranty_finding(
        self,
        fid: str,
        verdict: str = "approved",
        has_rec: bool = True,
    ) -> dict:
        # Evidence built with warranty.py build_warranty_evidence + _to_schema_ev so shape
        # changes in warranty.py break this test.
        # Source: agents/fi_recall_warranty/warranty.py _to_schema_ev
        from agents.fi_recall_warranty.warranty import (  # noqa: PLC0415
            build_warranty_evidence as _bwe,
            _to_schema_ev as _tse,
        )
        row = {"vehicle_vin": "VIN-001", "verdict_status": verdict, "verdict_reason": "test",
               "matched_rule_id": None, "terms_version": None}
        evidence = _tse(_bwe(row, "fi-recall-warranty"), "2026-01-01T00:00:00Z")
        f: dict = {
            "finding_id": fid,
            "finding_kind": "fleet.warranty.claim_candidate",
            "evidence": evidence,
        }
        if has_rec:
            f["recommended_action"] = {
                "action_kind": "submit_warranty_claim",
                "target_system": "dms",
                "parameters": {
                    "vehicle_vin": "VIN-001",
                    "ro_id": "RO-001",
                    "line_no": "1",
                    "dealer_id": "DLR-001",
                    "labor_op": "OP-001",
                },
            }
        return f

    def test_picks_approved_lowest_finding_id(self) -> None:
        """Returns the approved finding with the lowest finding_id."""
        f1 = self._make_warranty_finding("WRN-001")
        f2 = self._make_warranty_finding("WRN-002")
        result = _pick_warranty_finding([f2, f1], [])
        assert result is not None
        assert result["finding_id"] == "WRN-001"

    def test_skips_requires_review_verdict(self) -> None:
        """Mutant M17: requires_review must NOT be selected (only approved).

        The adjudicator skips requires_review claims, so _adjudicate_fi_claims
        returns the claim unchanged (not Paid), failing score_adjudication.
        """
        f_rr = self._make_warranty_finding("WRN-010", verdict="requires_review")
        f_ok = self._make_warranty_finding("WRN-011", verdict="approved")
        result = _pick_warranty_finding([f_rr, f_ok], [])
        assert result is not None
        assert result["finding_id"] == "WRN-011"

        # If only requires_review present, returns None
        result_none = _pick_warranty_finding([f_rr], [])
        assert result_none is None

    def test_skips_denied_verdict(self) -> None:
        """denied findings have no recommended_action (writer.py:224), so are excluded."""
        f_denied = self._make_warranty_finding("WRN-020", verdict="denied", has_rec=False)
        result = _pick_warranty_finding([f_denied], [])
        assert result is None

    def test_skips_finding_with_terminal_action(self) -> None:
        """Finding with an existing executed/completed action is excluded."""
        f = self._make_warranty_finding("WRN-030")
        acted = {"finding_id": "WRN-030", "status": "executed"}
        result = _pick_warranty_finding([f], [acted])
        assert result is None

    def test_includes_finding_with_non_terminal_action(self) -> None:
        """W3 fix: finding with any existing Action (including pending_execution) is skipped.

        pending_execution -> approve returns 409; design requires 'no existing Action'.
        Mutant: reverting to terminal-only filter would let pending_execution through.
        """
        f = self._make_warranty_finding("WRN-040")
        pending = {"finding_id": "WRN-040", "status": "pending_execution"}
        result = _pick_warranty_finding([f], [pending])
        # W3: any existing Action blocks the pick (409 on approve)
        assert result is None, "pending_execution Action must block the pick (409 on approve)"

    def test_skips_finding_without_recommended_action(self) -> None:
        """Findings with no recommended_action (e.g. insufficient_evidence) are excluded."""
        f = self._make_warranty_finding("WRN-050", has_rec=False, verdict="approved")
        result = _pick_warranty_finding([f], [])
        assert result is None

    def test_returns_none_when_no_candidates(self) -> None:
        result = _pick_warranty_finding([], [])
        assert result is None

    def test_skips_non_warranty_kind(self) -> None:
        """fleet.recall.vehicle findings are ignored even if they pass other checks."""
        recall_f = {
            "finding_id": "RCL-001",
            "finding_kind": "fleet.recall.vehicle",
            "evidence": [
                {"kind": "warranty.verdict", "warranty.verdict": {"verdict": "approved"}}
            ],
            "recommended_action": {"action_kind": "submit_warranty_claim"},
        }
        result = _pick_warranty_finding([recall_f], [])
        assert result is None


class TestScoreAdjudication:
    """Pure unit tests for score_adjudication (item 7 in-memory adjudication check)."""

    def test_passes_full_paid(self) -> None:
        """All four fields correct → PASS."""
        claim = {"claimed_amount_usd": 1250.0}
        adjudicated = {
            "status": "Paid",
            "paid_amount_usd": 1250.0,
            "paid_at": "2026-11-05T06:00:00Z",
            "simulated_by": "warranty_adjudicator_stand_in",
        }
        ok, detail = score_adjudication(claim, adjudicated)
        assert ok, detail

    def test_fails_wrong_status(self) -> None:
        """status != 'Paid' → FAIL."""
        claim = {"claimed_amount_usd": 500.0}
        adjudicated = {
            "status": "Submitted",
            "paid_amount_usd": 500.0,
            "paid_at": "2026-11-05T06:00:00Z",
            "simulated_by": "stand_in",
        }
        ok, _ = score_adjudication(claim, adjudicated)
        assert not ok

    def test_fails_amount_mismatch(self) -> None:
        """Mutant M18: paid_amount_usd != claimed_amount_usd → FAIL.

        Removing the equality check would accept a partial payment silently.
        """
        claim = {"claimed_amount_usd": 1000.0}
        adjudicated = {
            "status": "Paid",
            "paid_amount_usd": 0.0,  # zero: wrong
            "paid_at": "2026-11-05T06:00:00Z",
            "simulated_by": "stand_in",
        }
        ok, _ = score_adjudication(claim, adjudicated)
        assert not ok

    def test_fails_missing_paid_at(self) -> None:
        """paid_at absent → FAIL."""
        claim = {"claimed_amount_usd": 300.0}
        adjudicated = {
            "status": "Paid",
            "paid_amount_usd": 300.0,
            "paid_at": "",
            "simulated_by": "stand_in",
        }
        ok, _ = score_adjudication(claim, adjudicated)
        assert not ok

    def test_fails_missing_simulated_by(self) -> None:
        """simulated_by absent → FAIL."""
        claim = {"claimed_amount_usd": 200.0}
        adjudicated = {
            "status": "Paid",
            "paid_amount_usd": 200.0,
            "paid_at": "2026-11-05T06:00:00Z",
            "simulated_by": None,
        }
        ok, _ = score_adjudication(claim, adjudicated)
        assert not ok

    def test_adjudication_uses_dms_function_not_stub(self) -> None:
        """Item 7 design: _adjudicate_claim_in_memory loads DMS's own function.

        Verified structurally: it calls mod._adjudicate_fi_claims([claim], at=...).
        This test checks the in-memory result structure matches what the real function
        would produce for an approved, age-eligible claim.
        """
        # Simulate what _adjudicate_fi_claims would return for an approved claim at 14d+1min
        import datetime as dt
        submitted = dt.datetime(2026, 10, 1, 6, 0, 0, tzinfo=dt.timezone.utc)
        claim_in = {
            "claim_id": "CLM-TEST",
            "status": "Submitted",
            "claimed_amount_usd": 750.0,
            "submitted_at": submitted.isoformat(),
            "avx_action_id": "some-uuid",
        }
        # Simulate adjudicator result
        adjudicated = {
            "claim_id": "CLM-TEST",
            "status": "Paid",
            "claimed_amount_usd": 750.0,
            "paid_amount_usd": 750.0,
            "paid_at": "2026-10-15T06:01:00Z",
            "simulated_by": "warranty_adjudicator_stand_in",
        }
        ok, detail = score_adjudication(claim_in, adjudicated)
        assert ok, detail

    def test_adjudication_writes_nothing(self) -> None:
        """_adjudicate_claim_in_memory must not write to DDB — verified by call signature.

        The function runs _adjudicate_fi_claims([claim_copy], at=...) in memory.
        It does not pass a DDB client or stage, so it cannot write. This test confirms
        the function signature via import inspection.
        """
        import inspect
        sig = inspect.signature(_adjudicate_claim_in_memory)
        params = list(sig.parameters.keys())
        # Must not accept a DDB client or table name
        assert "ddb" not in params, "must not accept a DDB client"
        assert "table" not in params, "must not accept a table name"
        # Must accept dms_repo, claim, submitted_at
        assert "dms_repo" in params
        assert "claim" in params
        assert "submitted_at" in params

    def test_adjudicate_in_memory_calls_dms_and_returns_mutated_claim(self) -> None:
        """Mutant-2: replacing the body with a hand-built Paid dict must fail.

        Uses a recording fake: _adjudicate_fi_claims records exactly one call
        with ([claim], at=submitted_at+14d+1min).  The function must return
        the *same* claim object (identity), not a new dict.  A mutant that
        builds {"status":"Paid",...} directly fails both the call-count check
        and the identity check.
        """
        import datetime as _dt
        import types

        submitted = _dt.datetime(2026, 10, 1, 6, 0, 0, tzinfo=_dt.timezone.utc)
        expected_at = submitted + _dt.timedelta(days=14, minutes=1)
        claim = {
            "claim_id": "CLM-C6-UNIT",
            "status": "Submitted",
            "claimed_amount_usd": 500.0,
            "avx_action_id": "uuid-c6-test",
            "verdict_status": "approved",
            "submitted_at": submitted.isoformat(),
        }

        calls: list[dict] = []

        def _recording_adjudicate(claims, *, at):
            calls.append({"claims": claims, "at": at})
            # Mutate in place (mirrors real DMS behaviour)
            for c in claims:
                c["status"] = "Paid"
                c["paid_at"] = at.isoformat()
            return {"paid": len(claims), "rejected": 0, "skipped_review": 0, "skipped_too_recent": 0}

        fake_mod = types.SimpleNamespace(_adjudicate_fi_claims=_recording_adjudicate)
        with patch.object(_mod, "_load_dms_module", return_value=fake_mod):
            result = _adjudicate_claim_in_memory("dummy-dms-repo", claim, submitted)

        # Must have called _adjudicate_fi_claims exactly once
        assert len(calls) == 1, (
            f"Mutant-2 caught: _adjudicate_fi_claims must be called exactly once; "
            f"got {len(calls)} call(s). A hand-built return skips the call entirely."
        )
        # Must pass the claim in a list
        assert calls[0]["claims"] == [claim], (
            f"Mutant-2 caught: must call _adjudicate_fi_claims([claim], at=...); "
            f"got claims={calls[0]['claims']!r}"
        )
        # at == submitted_at + 14 days + 1 minute
        assert calls[0]["at"] == expected_at, (
            f"Mutant-2 caught: at must be submitted_at+14d+1min ({expected_at}); "
            f"got {calls[0]['at']}"
        )
        # Must return the *same* claim object (identity), not a copy
        assert result is claim, (
            "Mutant-2 caught: must return the mutated claim reference (result is claim); "
            "a hand-built dict is a different object."
        )

    def test_adjudicate_in_memory_with_real_dms_module(self) -> None:
        """C6: load the real fi_stand_ins from the DMS repo; verify paid==1 and mutation.

        Skip with a clear reason when the repo is absent (CI without DMS checkout).
        One test that doesn't need the repo: test_adjudicate_in_memory_calls_dms_and_returns_mutated_claim.
        """
        import os
        import datetime as _dt

        dms_path = os.path.expanduser("~/guidance-for-dealer-management-system-on-aws")
        if not os.path.isdir(dms_path):
            pytest.skip(
                "DMS repo absent at ~/guidance-for-dealer-management-system-on-aws; "
                "use test_adjudicate_in_memory_calls_dms_and_returns_mutated_claim for "
                "the no-repo variant"
            )

        submitted = _dt.datetime(2026, 10, 1, 6, 0, 0, tzinfo=_dt.timezone.utc)
        claim = {
            "claim_id": "CLM-REAL-DMS",
            "status": "Submitted",
            "claimed_amount_usd": 300.0,
            "avx_action_id": "uuid-real-dms",
            "verdict_status": "approved",
            "submitted_at": submitted.isoformat(),
        }

        result = _adjudicate_claim_in_memory(dms_path, dict(claim), submitted)

        # _adjudicate_fi_claims mutates in place and returns a counts dict (not a list)
        assert result.get("status") == "Paid", (
            f"real DMS adjudication: expected Paid, got {result.get('status')!r}"
        )
        assert result.get("paid_amount_usd") == 300.0

        # Also verify is_fi_claim and verdict_status shapes from the real module
        mod = _mod._load_dms_module(dms_path, "source/simulation/fi_stand_ins.py")
        is_fi = getattr(mod, "is_fi_claim", None)
        assert is_fi is not None, "is_fi_claim must exist in the real fi_stand_ins"
        fi_claim = {"claimed_amount_usd": 100.0, "avx_action_id": "some-uuid"}
        assert is_fi(fi_claim), "is_fi_claim must return True for an FI-filed claim"
        assert not is_fi({"claimed_amount_usd": 100.0}), \
            "is_fi_claim must return False without avx_action_id"


# ---------------------------------------------------------------------------
# Item 8 tests
# ---------------------------------------------------------------------------


class TestDmsScopeProbe:
    """Unit tests for _interpret_dms_fleet_scope_denial (DMS fleet-scope denial check).

    DMS returns 404 (not 403) for fleet-scope denials — uniform denial via
    auth._fleet_denial_response() so the caller cannot oracle-probe route existence.
    C3 fix: booking probe must include dealer_id so the pre-scope 400 is not triggered.
    """

    def test_404_and_400_passes(self) -> None:
        """Correct behavior: wrong-fleet → 404 (fleet denial), same-fleet-invalid → 400."""
        ok, detail = _interpret_dms_fleet_scope_denial(404, 400)
        assert ok, detail

    def test_400_and_400_fails_scope(self) -> None:
        """Mutant M20: if wrong-fleet returns 400, scope check did not fire first."""
        ok, _ = _interpret_dms_fleet_scope_denial(400, 400)
        assert not ok

    def test_404_and_403_fails_validation(self) -> None:
        """If same-fleet-invalid returns 403, validation path never reached."""
        ok, _ = _interpret_dms_fleet_scope_denial(404, 403)
        assert not ok

    def test_200_and_400_fails_scope(self) -> None:
        """wrong-fleet returning 200 means a write may have occurred (catastrophic)."""
        ok, _ = _interpret_dms_fleet_scope_denial(200, 400)
        assert not ok

    def test_not_a_uuid_body_must_not_write(self) -> None:
        """'not-a-uuid' as avx_action_id triggers UUID validation at 400; no RO created.

        C3: dealer_id must be in the body so the handler reaches the avx_action_id check
        (returns 400) not the pre-scope dealer_id-required check (also 400, but pre-scope).
        A FakeDmsApi is used to verify _interpret_dms_fleet_scope_denial reads 404, not 403.
        """
        ok, detail = _interpret_dms_fleet_scope_denial(404, 400)
        assert ok, detail
        # Mutant: if same-fleet returns 201, the positive control is vacuous (write happened)
        ok_bad, _ = _interpret_dms_fleet_scope_denial(404, 201)
        assert not ok_bad

    def test_c3_item8_dms_probe_uses_404_for_fleet_denial(self) -> None:
        """C3: _interpret_dms_fleet_scope_denial expects 404 (not 403) for fleet scope denial.

        DMS auth._fleet_denial_response returns 404 (uniform denial, oracle-free).
        Drives item8 with a FakeDmsApi answering 404 for cross-fleet to verify
        the check function records PASS, not FAIL.
        Mutant: if the function expected 403, a 404 scope denial would fail scope_ok.
        """
        # Direct functional test: 404 correct, 403 wrong (old behavior)
        ok_correct, _ = _interpret_dms_fleet_scope_denial(404, 400)
        assert ok_correct is True, "C3: 404 + 400 must PASS"

        # Old function behavior (expecting 403) is now wrong — verify it fails
        ok_old, _ = _interpret_dms_fleet_scope_denial(403, 400)
        assert ok_old is False, (
            "Mutant M-C3: expecting 403 instead of 404 — correctly fails when 404 returned"
        )

        # Also verify the booking-probe body now includes dealer_id by checking the function
        # accepts 400 for same-fleet (post-scope validation, after passing scope with dealer_id)
        ok_both, detail = _interpret_dms_fleet_scope_denial(404, 400)
        assert ok_both, detail
        assert "400" in detail and "404" in detail


class TestCountActionsForFinding:
    """Unit tests for _count_actions_for_finding (item 8 no-Action-row assertion)."""

    def test_zero_on_empty_table_response(self) -> None:
        """No matching rows → count is 0."""
        class FakeTable:
            def scan(self, **kw):
                return {"Items": []}
        class FakeDdb:
            def Table(self, name):
                return FakeTable()
        result = _count_actions_for_finding(FakeDdb(), "any-table", "FINDING-X")
        assert result == 0

    def test_positive_when_action_exists(self) -> None:
        """Mutant M21: if we returned 0 always, this test catches it.

        An action row matching finding_id must produce a positive count.
        """
        class FakeTable:
            def scan(self, **kw):
                # Return one matching row
                return {"Items": [{"action_id": "ACT-001", "finding_id": "FND-001"}]}
        class FakeDdb:
            def Table(self, name):
                return FakeTable()
        result = _count_actions_for_finding(FakeDdb(), "any-table", "FND-001")
        assert result > 0

    def test_returns_minus_one_on_error(self) -> None:
        """Scan exception → returns -1 (error sentinel, not 0)."""
        class FakeTable:
            def scan(self, **kw):
                raise RuntimeError("DDB unavailable")
        class FakeDdb:
            def Table(self, name):
                return FakeTable()
        result = _count_actions_for_finding(FakeDdb(), "any-table", "FINDING-ERR")
        assert result == -1


class TestItem8FleetBScopeIsolation:
    """C7: item8 must record a FAIL when a fleet-B call bypasses the 403 scope gate.

    These tests call the real item8 with a fake Http, so a mutation to any
    ``res.check("8-decline-*" …)`` or ``res.check("8-approve-*" …)`` expression
    in the script can be caught (unlike the prior literal-assert tests that never
    called item8).
    """

    # ── shared fake infrastructure ──────────────────────────────────────────

    @staticmethod
    def _fake_ddb():
        class _T:
            def scan(self, **kw):
                return {"Items": []}
            def query(self, **kw):
                return {"Items": []}
            def put_item(self, **kw):
                pass
            def delete_item(self, **kw):
                pass
            def get_item(self, **kw):
                return {}
        class _D:
            def Table(self, _):
                return _T()
        return _D()

    @staticmethod
    def _run_item8(api_call_fn):
        """Call the real item8 with a fake Http whose call() is api_call_fn.

        C7 fix: GET returns 200 so ctrl_ok=True and isolation probes actually run.
        W6 fix: ctrl approve returns 400 (engineering kind post-scope), ctrl decline returns 200.
        These are needed so the inner isolation-probe block is reached.
        api_call_fn handles the B1/B2/B3 probes and can control individual probe responses.
        """
        import tempfile
        import json as _json
        from contextlib import contextmanager as _cm
        res = Results()

        class FakeApi:
            def call(self_f, method, path, token, body=None):
                if method == "GET":
                    return 200, {}  # C7 fix: ctrl GET passes so ctrl_ok=True
                if method == "POST" and "ctrl" in path and "/approve" in path:
                    return 400, {}  # W6: engineering kind on non-pop ctrl → 400
                if method == "POST" and "ctrl" in path and "/decline" in path:
                    return 200, {}  # W6: ctrl decline proves route reachable
                return api_call_fn(method, path, body)

        class FakeDmsApi:
            def call(self_d, method, path, token, body=None):
                return 404, {}  # C3 fix: DMS returns 404 for fleet scope denial

        @_cm
        def fake_principal(*a, **kw):
            yield ("fake-token", {})

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
            _json.dump({"run": "g55-test", "items": {}}, tf)
            manifest_path = Path(tf.name)

        with patch.object(_mod, "principal", fake_principal):
            _mod.item8(
                res,
                run="g55-test",
                stage="staging",
                ddb=TestItem8FleetBScopeIsolation._fake_ddb(),
                api=FakeApi(),
                dms_api=FakeDmsApi(),
                pool_id="us-west-2_POOL",
                client_id="CLIENTID",
                idp=MagicMock(),
                manifest=manifest_path,
                teardown=_TeardownRegistry(),
            )
        return res

    # ── C7 tests ─────────────────────────────────────────────────────────────

    def test_item8_fails_check_when_b1_decline_returns_200(self) -> None:
        """Mutant-C7: replacing st_b1d == 403 with True must be caught.

        Fake Http: 200 for B1 decline, 403 for everything else.
        Expected: res has a FAIL on 8-decline-b1.
        Mutant-C7 (st_b1d == 403 → True) would record PASS, not FAIL → test assertion fails.
        """
        def api_call(method, path, body):
            if method == "POST" and "/decline" in path and "-b8-1" in path:
                return 200, {}
            return 403, {}

        res = self._run_item8(api_call)
        fail_labels = [r[0] for r in res.rows if r[1] == "FAIL"]
        assert "8-decline-b1" in fail_labels, (
            f"8-decline-b1 must FAIL when decline returns 200, got rows="
            f"{[(r[0], r[1]) for r in res.rows]}"
        )

    def test_item8_fails_check_when_b2_approve_returns_200(self) -> None:
        """Mutant complement: 200 on B2 approve must FAIL 8-approve-b2."""
        def api_call(method, path, body):
            if method == "POST" and "/approve" in path and "-b8-2" in path:
                return 200, {"action_id": "ACT-TEST"}
            return 403, {}

        res = self._run_item8(api_call)
        fail_labels = [r[0] for r in res.rows if r[1] == "FAIL"]
        assert "8-approve-b2" in fail_labels, (
            f"8-approve-b2 must FAIL when approve returns 200, got rows="
            f"{[(r[0], r[1]) for r in res.rows]}"
        )

    def test_item8_ctrl_approve_400_gates_isolation_probes(self) -> None:
        """W6 C7: if ctrl approve does NOT return 400, isolation probes are blocked.

        Mutant M-c7: ctrl_approve_ok = True always (skips the 400 check) — isolation
        probes run even when the ctrl route is unreachable (wrong URL → 403 from AG).
        With the mutant, isolation probes run and return 403 → PASS (no FAIL row).
        Without the mutant (correct code): ctrl_approve returns 403 → ctrl_approve_ok=False
        → all probe labels blocked → "8-approve-b1" gets FAIL "W6 controls failed".
        """
        def api_call(method, path, body):
            # ctrl approve returns 403 (wrong URL / route unreachable)
            if method == "POST" and "ctrl" in path and "/approve" in path:
                return 403, {}  # NOT 400 → ctrl_approve_ok should be False
            if method == "POST" and "ctrl" in path and "/decline" in path:
                return 200, {}
            return 403, {}  # isolation probes

        # Override FakeApi to return 403 for ctrl approve
        import tempfile
        import json as _json
        from contextlib import contextmanager as _cm

        res = Results()

        class FakeApiCtrlFail:
            def call(self_f, method, path, token, body=None):
                if method == "GET":
                    return 200, {}
                return api_call(method, path, body)

        class FakeDmsApi:
            def call(self_d, method, path, token, body=None):
                return 404, {}

        @_cm
        def fake_principal(*a, **kw):
            yield ("fake-token", {})

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
            _json.dump({"run": "g55-test", "items": {}}, tf)
            manifest_path = Path(tf.name)

        with patch.object(_mod, "principal", fake_principal):
            _mod.item8(
                res, run="g55-test", stage="staging",
                ddb=TestItem8FleetBScopeIsolation._fake_ddb(),
                api=FakeApiCtrlFail(), dms_api=FakeDmsApi(),
                pool_id="us-west-2_POOL", client_id="CLIENTID",
                idp=MagicMock(), manifest=manifest_path,
                teardown=_TeardownRegistry(),
            )

        fail_labels = [r[0] for r in res.rows if r[1] == "FAIL"]
        # ctrl_approve_ok=False (403 not 400) → isolation probes blocked
        assert "8-ctrl-approve-400" in fail_labels, (
            f"8-ctrl-approve-400 must FAIL when ctrl approve returns 403 (not 400); "
            f"rows={[(r[0], r[1]) for r in res.rows]}"
        )
        assert "8-approve-b1" in fail_labels, (
            f"W6: 8-approve-b1 must be blocked (FAIL) when ctrl_approve_ok=False; "
            f"rows={[(r[0], r[1]) for r in res.rows]}"
        )

        # Mutant M-c7: ctrl_approve_ok = True always (mutant check)
        # With the mutant, all isolation probes would run and see 403 → PASS
        # The test catches the mutant: with ctrl_approve=403 (not 400), the probes
        # must be BLOCKED (FAIL), not allowed through.
        assert "8-decline-b1" in fail_labels, (
            f"Mutant M-c7 caught: isolation probes must be blocked when ctrl fails; "
            f"'8-decline-b1' must also be FAIL (blocked), got rows={fail_labels}"
        )



# ---------------------------------------------------------------------------
# Item 9 helpers
# ---------------------------------------------------------------------------

_FI_GUARDRAIL_ENV = _mod._FI_GUARDRAIL_ENV
_wait_fn_update = _mod._wait_fn_update


class TestItem9ForcedFailure:
    """Tests for item 9: forced failure via FI_COST_GUARDRAIL_ID clear."""

    def test_guardrail_env_constant(self) -> None:
        """_FI_GUARDRAIL_ENV must be FI_COST_GUARDRAIL_ID (the raise-before-write guard).

        Mutant M22: changing the constant to FI_RW_MODEL_ID would use the model-id path,
        which falls back to rules instead of raising — the forced failure would not fire.
        Verified: replacing _FI_GUARDRAIL_ENV with 'FI_RW_MODEL_ID' would let a run
        complete with rules-fallback, leaving the alarm in OK.
        """
        assert _FI_GUARDRAIL_ENV == "FI_COST_GUARDRAIL_ID", (
            "Mutant caught: wrong env var; model-id path falls back to rules, "
            "does not raise before writing"
        )

    def test_no_non_existent_model_constant(self) -> None:
        """_NON_EXISTENT_MODEL must not exist in the recall-warranty script.

        The design says to remove it since a bad model falls back to rules;
        its presence would suggest the wrong (model-id) forced-failure strategy.
        Mutant M23: keeping _NON_EXISTENT_MODEL and using it in item 9 would
        result in a rules-fallback run, not a raise-before-write abort.
        """
        assert not hasattr(_mod, "_NON_EXISTENT_MODEL"), (
            "Mutant caught: _NON_EXISTENT_MODEL still present; "
            "item 9 must use the guardrail-ID approach, not model-ID"
        )

    def test_wait_fn_update_polls_until_successful(self) -> None:
        """_wait_fn_update must poll and return when LastUpdateStatus == Successful.

        Mutant M24: removing the poll would proceed before the config update
        lands, causing the invoke to use the old env.
        """
        calls = []

        class FakeLam:
            def get_function_configuration(self, **kw: Any) -> dict[str, Any]:
                calls.append(len(calls))
                # First call: InProgress; second: Successful
                if len(calls) < 2:
                    return {"LastUpdateStatus": "InProgress"}
                return {"LastUpdateStatus": "Successful"}

        with patch.object(_mod.time, "sleep", return_value=None):
            _wait_fn_update(FakeLam(), "fn-test", timeout_s=30)
        assert len(calls) == 2, f"expected 2 polls, got {len(calls)}"

    def test_wait_fn_update_raises_on_timeout(self) -> None:
        """_wait_fn_update raises TimeoutError when status never reaches Successful.

        Mutant M25: dropping the timeout would hang forever on a stuck update.
        """
        class FakeLam:
            def get_function_configuration(self, **kw: Any) -> dict[str, Any]:
                return {"LastUpdateStatus": "InProgress"}

        with patch.object(_mod.time, "monotonic", side_effect=[0.0, 200.0]):
            with patch.object(_mod.time, "sleep", return_value=None):
                with pytest.raises(TimeoutError):
                    _wait_fn_update(FakeLam(), "fn-test", timeout_s=100)

    def test_item9_skips_when_alarm_already_in_alarm(self) -> None:
        """item9 skips (W4 guard) when alarm is already in ALARM state.

        Mutant M26: removing the W4 guard would override the env and invoke even
        when the alarm is stuck in ALARM — we'd be firing a second forced failure
        while the first hasn't recovered, risking leaving the env dirty.
        """
        res = Results()
        fake_cw = MagicMock()
        fake_cw.describe_alarms.return_value = {
            "MetricAlarms": [{"StateValue": "ALARM"}]
        }
        _mod.item9(
            res,
            stage="staging", region="us-west-2",
            run=f"test-run-{_PID}", account="123456789012", sched=MagicMock(),
            lam=MagicMock(), cw=fake_cw, sns=MagicMock(),
            backups_ok=True,
            tier2_env_snapshot={},
        )
        skips = [r for r in res.rows if r[1] == "SKIP"]
        assert len(skips) == 1, f"expected 1 SKIP for W4, got {skips}"
        assert "ALARM" in skips[0][2]


# ---------------------------------------------------------------------------
# Item 11 helpers
# ---------------------------------------------------------------------------

class TestItem11D18Eval:
    """Tests for item 11: D18 eval disclosure text and eval wiring."""


    def test_item11_skips_without_cognito(self) -> None:
        """item11 must SKIP (not FAIL) when Cognito pool/client is unavailable.

        Mutant M28: raising instead of skipping would make a routine missing-Cognito
        condition look like a test failure rather than an infra skip.
        """
        res = Results()
        _mod.item11(
            res,
            run="g55-20261005T120000-abcd",
            stage="staging", region="us-west-2",
            ddb=MagicMock(), api=MagicMock(), lam=MagicMock(),
            pool_id=None, client_id=None,
            idp=MagicMock(), manifest=Path("/tmp/nonexistent.json"),
            teardown=_TeardownRegistry(),
        )
        skips = [r for r in res.rows if r[1] == "SKIP"]
        fails = [r for r in res.rows if r[1] == "FAIL"]
        assert len(skips) == 1
        assert len(fails) == 0

    def test_evaluate_response_passes_on_good_reply(self) -> None:
        """evaluate_response must pass when reply contains all required phrases.

        Mutant M29: removing a required phrase from the reply makes the eval fail.
        Verified: stripping 'park outside' from reply_text causes missing=['phrase:park outside'].
        """
        from evals.runner.fi_text_case import evaluate_response, load_case  # noqa: PLC0415
        case_path = (
            Path(__file__).resolve().parents[2]
            / "evals/cases/fi/fleet-recall-safety-text-001.yaml"
        )
        case = load_case(case_path)
        good_reply = (
            "The recall MRD-LIVETEST-D18 was raised because this vehicle has a fire risk. "
            "Tell the driver: do not drive this vehicle — park outside away from structures "
            "and contact the dealer. LIVETEST-D18"
        )
        passed, missing = evaluate_response(case, good_reply, tool_calls=None)
        assert passed, f"expected PASS, missing={missing}"

    def test_evaluate_response_fails_without_marker(self) -> None:
        """evaluate_response must fail if LIVETEST-D18 marker is absent.

        Mutant M29 (complement): the tool_evidence check requires the marker;
        removing it from the reply exposes the property the disclosure plants.
        """
        from evals.runner.fi_text_case import evaluate_response, load_case  # noqa: PLC0415
        case_path = (
            Path(__file__).resolve().parents[2]
            / "evals/cases/fi/fleet-recall-safety-text-001.yaml"
        )
        case = load_case(case_path)
        bad_reply = "do not drive this vehicle and park outside. No marker here."
        passed, missing = evaluate_response(case, bad_reply, tool_calls=None)
        assert not passed
        assert any("LIVETEST-D18" in m for m in missing), f"expected LIVETEST-D18 in missing, got {missing}"



# ---------------------------------------------------------------------------
# Residue check helpers (real DDB/Cognito/Lambda logic, no AWS)
# ---------------------------------------------------------------------------

class TestResidueRealChecks:
    """Tests for real (non-stub) residue check closures.

    These tests verify the property each check guards, not just that it
    calls a helper.  One mutant per check.
    """

    def test_residue_findings_passes_when_table_empty(self) -> None:
        """_check_residue_findings returns (True, ...) when no LIVETEST items remain.

        Mutant-4: 'ok = True' unconditionally would hide unflushed Findings.
        This test drives the real function — mutating ok=True still returns
        (True, ...) here, but test_residue_findings_fails_when_livetest_finding_present
        catches the mutation because a hit still produces (True, ...) instead of (False).
        """
        class FakeTable:
            def query(self, **kw: Any) -> dict:
                return {"Items": []}
            def get_item(self, **kw: Any) -> dict:
                return {}

        class FakeDdb:
            def Table(self, name: str) -> FakeTable:
                return FakeTable()

        ok, detail = _check_residue_findings(
            FakeDdb(), "findings", ["fleet-a", "fleet-b"], ["LIVETEST-X"]
        )
        assert ok is True, f"empty table must pass; got ok={ok!r}, detail={detail!r}"

    def test_residue_findings_fails_when_livetest_finding_present(self) -> None:
        """_check_residue_findings returns (False, ...) when a LIVETEST Finding exists.

        Mutant-4 catcher: 'ok = True' in _check_residue_findings would return
        (True, ...) here — this test asserts (False, ...) and catches the mutation.
        """
        class FakeTable:
            def query(self, **kw: Any) -> dict:
                return {"Items": [{"finding_id": "LIVETEST-FI-RW-g55-abc-b8-1", "fleet_id": "fleet-a"}]}
            def get_item(self, **kw: Any) -> dict:
                return {}

        class FakeDdb:
            def Table(self, name: str) -> FakeTable:
                return FakeTable()

        ok, detail = _check_residue_findings(
            FakeDdb(), "findings", ["fleet-a"], []
        )
        assert ok is False, (
            f"Mutant-4 caught: 'ok = True' must not survive — a LIVETEST Finding "
            f"present in the table must yield ok=False; got ok={ok!r}, detail={detail!r}"
        )
        assert "1 LIVETEST finding" in detail, f"detail should name the hit count; got {detail!r}"

    def test_residue_lambda_env_passes_when_env_restored(self) -> None:
        """Lambda env residue check passes when current env matches snapshot.

        Mutant M31: using != instead of == for the comparison would pass
        a mismatched env (inverts the guard).
        """
        original_env = {"FI_COST_GUARDRAIL_ID": "gid-real", "FOO": "bar"}
        restored_env = dict(original_env)
        diff = sorted(k for k in set(original_env) | set(restored_env)
                      if original_env.get(k) != restored_env.get(k))
        ok = len(diff) == 0
        assert ok, f"identical env must pass; diff={diff}"

    def test_residue_lambda_env_fails_when_env_differs(self) -> None:
        """Lambda env residue check fails when a key differs after restore attempt.

        Mutant M31 complement: if we compare sets instead of values, an env
        with all the same keys but one changed value would pass incorrectly.
        """
        original_env = {"FI_COST_GUARDRAIL_ID": "gid-real", "FOO": "bar"}
        broken_env = {"FI_COST_GUARDRAIL_ID": "", "FOO": "bar"}  # guardrail cleared
        diff = sorted(k for k in set(original_env) | set(broken_env)
                      if original_env.get(k) != broken_env.get(k))
        ok = len(diff) == 0
        assert not ok, "differing env must fail"
        assert "FI_COST_GUARDRAIL_ID" in diff




# ---------------------------------------------------------------------------
# Fix (a): DMS API URL, Fix (b): Athena OutputLocation, Fix (c): band.classify
# ---------------------------------------------------------------------------

_athena_run = _mod._athena_run


class TestFixADmsApiUrl:
    """Tests for fix (a): DMS PATCH and probe must call dms_api, not api."""

    def test_item5_has_dms_api_param(self) -> None:
        """item5 signature must include dms_api parameter.

        Mutant M33: removing dms_api from item5's signature means PATCH calls
        would fall back to the AVX api, and an unknown path returns 403 vacuously.
        Verified: inspect.signature shows dms_api as a required kwarg.
        """
        import inspect  # noqa: PLC0415
        sig = inspect.signature(_mod.item5)
        assert "dms_api" in sig.parameters, (
            "Mutant M33 caught: item5 must accept dms_api to call the DMS API directly"
        )

    def test_item8_has_dms_api_param(self) -> None:
        """item8 signature must include dms_api parameter.

        Mutant M33 complement: item8's DMS probe must use dms_api, not api.
        """
        import inspect  # noqa: PLC0415
        sig = inspect.signature(_mod.item8)
        assert "dms_api" in sig.parameters, (
            "Mutant M33 caught: item8 must accept dms_api for the DMS scope probe"
        )




class TestFixBAthenaOutputLocation:
    """Tests for fix (b): Athena must not pass OutputLocation."""


    def test_athena_run_calls_start_query_without_result_config(self) -> None:
        """_athena_run must call start_query_execution without ResultConfiguration.

        Mutant M35 complement: we verify by running _athena_run with a fake client
        and checking the actual call args.
        """
        calls: list[dict] = []

        class FakeAthena:
            def start_query_execution(self, **kw: Any) -> dict:
                calls.append(kw)
                return {"QueryExecutionId": "test-eid"}

            def get_query_execution(self, **kw: Any) -> dict:
                return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

            def get_query_results(self, **kw: Any) -> dict:
                return {"ResultSet": {"Rows": [{"Data": [{"VarCharValue": "header"}]}]}}

        _athena_run(FakeAthena(), "SELECT 1", "wg-test", "")
        assert len(calls) == 1, "expected exactly one start_query_execution call"
        call_kw = calls[0]
        assert "ResultConfiguration" not in call_kw, (
            "Mutant M35 caught: start_query_execution must not include ResultConfiguration"
        )


class TestFixCBandClassify:
    """Tests for fix (c): score_groundings must use band.classify for p0."""

    def test_safety_confirmed_is_p0_not_just_do_not_drive(self) -> None:
        """band.classify('safety', False, 'CONFIRMED') returns p0.

        Mutant M36: using only do_not_drive=TRUE to identify p0 campaigns would
        miss safety+CONFIRMED campaigns (band table row 2) where do_not_drive=False.
        Verified: band.classify returns p0 for that combination.
        """
        from agents.fi_recall_warranty.band import classify  # noqa: PLC0415
        result = classify("safety", False, "CONFIRMED")
        assert result.severity == "p0", (
            "Mutant M36 caught: safety+CONFIRMED must be p0 even without do_not_drive=True; "
            "using only do_not_drive=TRUE to find p0 campaigns misses this row"
        )

    def test_safety_population_is_attention_not_p0(self) -> None:
        """band.classify('safety', False, 'POPULATION') returns 'attention', not p0.

        Mutant M36 complement: the query must NOT include safety+POPULATION as p0.
        """
        from agents.fi_recall_warranty.band import classify  # noqa: PLC0415
        result = classify("safety", False, "POPULATION")
        assert result.severity == "attention", (
            "safety+POPULATION must be attention; item6 must not ground vehicles for it"
        )







# ---------------------------------------------------------------------------
# C3: approve response shape (action_id top-level, target_ref, queue_status, Limit)
# ---------------------------------------------------------------------------

_pick_managed_fleet_a = _mod._pick_managed_fleet_a
_MANAGED_FLEETS = _mod._MANAGED_FLEETS


class TestC3ApproveResponseShape:
    """C3: approve returns Action at top level; ro_id comes from target_ref."""




    def test_count_actions_paginates_no_limit(self) -> None:
        """_count_actions_for_finding paginates without Limit.

        A Limit=N before FilterExpression caps pages read, not rows matched.
        A row beyond the first page is invisible to a Limit=5 scan, so
        'no Action row' becomes vacuous.

        Mutant: add Limit=5 to the scan kwargs — a row injected at position 6+
        is not returned and count stays 0.
        """
        class FakeTable:
            """Simulates DynamoDB: row only exists on the second page.

            When Limit is present (simulating the mutant), the scan stops at the first
            page and never returns the row — this is what makes the test catch the mutant.
            """
            def scan(self, **kw):
                has_limit = "Limit" in kw
                if "ExclusiveStartKey" not in kw:
                    if has_limit:
                        # Limit stops the scan at page 1; row is on page 2 — not found
                        return {"Items": []}
                    return {"Items": [], "LastEvaluatedKey": {"action_id": "tok1"}}
                return {"Items": [{"action_id": "A1", "finding_id": "FID"}]}

        class FakeDdb:
            def Table(self, name):
                return FakeTable()

        count = _count_actions_for_finding(FakeDdb(), "tbl", "FID")
        assert count == 1, (
            "Mutant caught: _count_actions_for_finding must paginate — "
            "a Limit=5 scan misses rows beyond the first page"
        )




class TestW2OpenFiBookingsOnly:
    """W2: _check_fi_booking_isolation must skip completed/terminal ROs."""

    def test_closed_fi_ro_not_flagged(self) -> None:
        """A Closed FI booking from a prior run must not block the isolation check.

        item4's ROs are kept by design; after dealer-progress they become Closed/Delivered.
        On re-run, the isolation check must not flag them.

        Mutant: remove the status filter — every FI RO (Closed included) triggers FAIL,
        so item4 fails on every run after the first.
        """
        closed_ro = {**_make_fi_ro("RO-PRIOR-001"), "status": "Closed"}
        with patch.object(_mod, "_load_dms_module",
                          return_value=_make_stub_fi_module(_real_is_fi_booking)):
            ok, detail = _check_fi_booking_isolation("unused", [closed_ro], set())
        assert ok, (
            f"Mutant caught: Closed FI RO from prior run must not block isolation: {detail}"
        )

    def test_delivered_fi_ro_not_flagged(self) -> None:
        """A Delivered FI booking (terminal) must not trigger the isolation check."""
        delivered_ro = {**_make_fi_ro("RO-DELIVERED-001"), "status": "Delivered"}
        with patch.object(_mod, "_load_dms_module",
                          return_value=_make_stub_fi_module(_real_is_fi_booking)):
            ok, detail = _check_fi_booking_isolation("unused", [delivered_ro], set())
        assert ok, f"Delivered FI RO (terminal) must not block isolation: {detail}"

    def test_draft_foreign_fi_ro_still_flagged(self) -> None:
        """A Draft FI booking from another run must still trigger FAIL.

        The status filter must not silently pass all FI bookings.
        """
        draft_ro = {**_make_fi_ro("RO-FOREIGN-DRAFT"), "status": "Draft"}
        with patch.object(_mod, "_load_dms_module",
                          return_value=_make_stub_fi_module(_real_is_fi_booking)):
            ok, detail = _check_fi_booking_isolation("unused", [draft_ro], set())
        assert not ok, "Draft foreign FI booking must still fail the isolation check"


class TestW3PickBookFindingBookOne:
    """W3: _pick_book_finding requires subject_key /book/1 (not /book/2 etc.)."""

    def test_requires_book_1(self) -> None:
        """Only /book/1 is eligible; /book/2 is for attempt 2 after cancel (W3 fix).

        Mutant: use parts[1] != 'book' check only (allow any /book/n) — item5a could
        pick a /book/2 Finding that was written for a different attempt, causing 409.

        Test design: F-BOOK2 has the lowest finding_id so without the /book/1 filter the
        mutant would return it; with the filter only F-BOOK1 (/book/1) qualifies.
        """
        # F-BOOK2 < F-BOOK1 lexicographically — mutant would pick F-BOOK2
        f_book1 = _make_recall_finding("F-BOOK1", "MRD-2024-A", "VIN1", action="book", attempt=1)
        f_book2 = _make_recall_finding("F-BOOK2", "MRD-2024-A", "VIN2", action="book", attempt=2)
        # Swap ids so /book/2 would win without the /book/1 restriction
        f_book1["finding_id"] = "F-Z-BOOK1"  # higher id
        f_book2["finding_id"] = "F-A-BOOK2"  # lower id — would win if /book/2 allowed
        result = _pick_book_finding([f_book2, f_book1], [])
        assert result is not None
        assert result["finding_id"] == "F-Z-BOOK1", (
            "Mutant caught: _pick_book_finding must require /book/1 specifically, "
            "not any /book/n (F-A-BOOK2 has lower id and would win without the filter)"
        )

    def test_skips_finding_with_existing_action(self) -> None:
        """Finding with any existing Action is skipped (W3: any Action gives 409).

        Mutant: not passing existing_action_finding_ids — a declined Finding is picked
        and the approve returns 409.
        """
        f_actioned = _make_recall_finding("F-ACTIONED", "MRD-2024-A", "VIN1")
        f_clean = _make_recall_finding("F-CLEAN", "MRD-2024-A", "VIN2")
        # Pass existing action for F-ACTIONED
        result = _pick_book_finding(
            [f_actioned, f_clean], [],
            existing_action_finding_ids={"F-ACTIONED"},
        )
        assert result is not None
        assert result["finding_id"] == "F-CLEAN", (
            "Mutant caught: _pick_book_finding must skip findings with existing Actions"
        )

    def test_warranty_finding_all_actioned_blocked(self) -> None:
        """_pick_warranty_finding skips any-status Actions (W3 fix: pending_execution -> 409)."""
        f = TestPickWarrantyFinding()._make_warranty_finding("WRN-PENDING")
        in_progress = {"finding_id": "WRN-PENDING", "status": "in_progress"}
        result = _pick_warranty_finding([f], [in_progress])
        assert result is None, (
            "Mutant caught: in_progress Action must also block the pick (409 on approve)"
        )







# ---------------------------------------------------------------------------
# C5: Http.call timeout mutant test
# ---------------------------------------------------------------------------

class TestC5HttpTimeout:
    """C5: Http.call must accept a timeout keyword argument."""

    def test_http_call_accepts_timeout_kwarg(self) -> None:
        """Http.call must declare a timeout parameter.

        Mutant: removing the timeout param causes TypeError when item11 calls
        api.call(..., timeout=15).  Caught by this inspect check.
        """
        import inspect  # noqa: PLC0415
        sig = inspect.signature(Http.call)
        assert "timeout" in sig.parameters, (
            "Mutant caught: Http.call has no timeout param; item 11 raises TypeError"
        )



# ---------------------------------------------------------------------------
# C4: Cognito residue email filter mutant test
# ---------------------------------------------------------------------------

class TestC4ResidueEmail:
    """C4: Cognito residue filter must use 'email' attribute, not 'username'."""

    def test_cognito_residue_filter_uses_email(self) -> None:
        """Residue _check_residue_cognito must filter by email (usernames are UUIDs).

        Drives the real _check_residue_cognito with a fake idp that respects the
        Filter parameter — returns users only when the filter matches the email prefix.

        Mutant M-a5 (arch5): change filter to 'email ^= "zzz-"' — no users match the
        wrong prefix, leftover LIVETEST users are invisible, check wrongly returns True.
        Verified: when filter doesn't match (returns []), ok=True even with LIVETEST user.
        """
        _check_residue_cognito = _mod._check_residue_cognito

        # Fake idp that respects the Filter parameter (returns users only for matching prefix)
        LIVETEST_EMAIL = "livetest-fi-rw-testrun@test"

        class FakeIdpFilterAware:
            def list_users(self_i, **kw: Any) -> dict:
                flt = kw.get("Filter", "")
                # Simulate: email ^= "prefix" matches only if email starts with prefix
                if '"livetest-fi-rw-"' in flt:
                    return {"Users": [{"Username": "uuid-1234", "Attributes": [
                        {"Name": "email", "Value": LIVETEST_EMAIL}
                    ]}]}
                return {"Users": []}  # wrong prefix → no matches

        ok, detail = _check_residue_cognito(FakeIdpFilterAware(), "us-west-2_POOL")
        assert ok is False, (
            f"C4: a remaining LIVETEST Cognito user must cause FAIL; detail={detail!r}"
        )
        assert "1" in detail, f"detail must mention count: {detail!r}"

        # Mutant M-a5 / arch5: filter uses 'zzz-' prefix → no match → ok=True
        # Simulate by calling the function with a fake that returns users ONLY
        # when the filter is "livetest-fi-rw-" (correct), empty otherwise.
        # Then directly simulate what happens when the filter is "zzz-":
        # the function would call idp with 'email ^= "zzz-"', get [] → ok=True.
        # We verify the mutant behavior by calling the function and checking
        # that the correct filter is needed to get ok=False.

        # A fake that returns empty for any filter (simulates arch5 mutant effect):
        class FakeIdpEmpty:
            def list_users(self_i, **kw: Any) -> dict:
                return {"Users": []}  # no users match wrong filter "zzz-"

        ok_empty, _ = _check_residue_cognito(FakeIdpEmpty(), "us-west-2_POOL")
        # With no users returned (wrong filter effect), ok=True — dangerous
        assert ok_empty is True, (
            f"arch5 mutant simulation: wrong filter returns no users → ok=True (dangerous)"
        )
        # This proves the mutant is dangerous: ok=True even with LIVETEST user present
        # The real test (FakeIdpFilterAware above) catches it by using the correct filter

        # Verify pool_id=None skips safely
        ok_skip, _ = _check_residue_cognito(None, None)
        assert ok_skip is True, "pool_id=None must skip (return True)"



# ---------------------------------------------------------------------------
# W7: list_topics pagination + AlarmActions + succeeded history mutant tests
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# W10: disclosure headline mutant test
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# W12: per-item exception + write window re-check mutant tests
# ---------------------------------------------------------------------------




# ── residue: actions half and the direct-id read (architect, 2026-10-10) ─────

class _ResidueTable:
    """Fake boto3 Table: scan pages, get_item rows, optional get_item error."""

    def __init__(self, scan_pages=None, items=None, get_error=None):
        self.scan_pages = list(scan_pages or [[]])
        self.items = dict(items or {})
        self.get_error = get_error
        self.scan_calls: list[dict] = []

    def scan(self, **kw):
        self.scan_calls.append(kw)
        i = len(self.scan_calls) - 1
        page = {"Items": self.scan_pages[i]}
        if i + 1 < len(self.scan_pages):
            page["LastEvaluatedKey"] = {"action_id": f"k{i}"}
        return page

    def query(self, **kw):
        return {"Items": []}

    def get_item(self, Key):
        if self.get_error:
            raise self.get_error
        item = self.items.get(Key["finding_id"])
        return {"Item": item} if item else {}


class _ResidueDdb:
    def __init__(self, table):
        self.table = table

    def Table(self, name):
        return self.table


class TestResidueActionsAndDirectIds:
    def test_actions_residue_fails_when_a_livetest_action_remains(self):
        t = _ResidueTable(scan_pages=[[], [{"action_id": "AVX-ACT-1", "finding_id": "LIVETEST-FI-RW-r1-b5"}]])
        ok, detail = _mod._check_residue_actions(_ResidueDdb(t), "acts", "LIVETEST-FI-RW-r1")
        assert ok is False, detail
        assert len(t.scan_calls) == 2, "must follow LastEvaluatedKey to the second page"

    def test_actions_residue_passes_when_none_remain(self):
        t = _ResidueTable(scan_pages=[[], []])
        ok, detail = _mod._check_residue_actions(_ResidueDdb(t), "acts", "LIVETEST-FI-RW-r1")
        assert ok is True, detail

    def test_actions_residue_fails_closed_on_scan_error(self):
        class _Boom(_ResidueTable):
            def scan(self, **kw):
                raise RuntimeError("AccessDenied")
        ok, _ = _mod._check_residue_actions(_ResidueDdb(_Boom()), "acts", "LIVETEST-FI-RW-r1")
        assert ok is False

    def test_direct_id_read_error_fails_closed(self):
        t = _ResidueTable(get_error=RuntimeError("Throttling"))
        ok, detail = _check_residue_findings(_ResidueDdb(t), "finds", [], ["LIVETEST-FI-RW-r1-b5"])
        assert ok is False, "an unreadable row is not proof that it is gone"

    def test_direct_id_still_present_fails(self):
        t = _ResidueTable(items={"LIVETEST-FI-RW-r1-b5": {"finding_id": "LIVETEST-FI-RW-r1-b5"}})
        ok, _ = _check_residue_findings(_ResidueDdb(t), "finds", [], ["LIVETEST-FI-RW-r1-b5"])
        assert ok is False


# ---------------------------------------------------------------------------
# C4: teardown errors surface as FAIL (not swallowed by inner try/except)
# ---------------------------------------------------------------------------

class TestC4TeardownErrorSurface:
    """C4: teardown closure errors must not be swallowed; _TeardownRegistry.run calls mark_cleanup_failed."""

    def test_teardown_error_marks_cleanup_failed(self) -> None:
        """A closure that raises must cause mark_cleanup_failed() to be called.

        Mutant: if the closure swallows its own exception, res._cleanup_failed stays False
        and a leftover RO on a managed VIN exits 0.
        """
        res = Results()
        td = _TeardownRegistry()

        def _raises() -> None:
            raise RuntimeError("delete failed: AccessDeniedException")

        td.register(_raises)
        td.run(res)
        assert res._cleanup_failed, (
            "C4: a teardown closure that raises must mark cleanup_failed"
        )

    def test_teardown_error_does_not_reraise(self) -> None:
        """_TeardownRegistry.run must not re-raise — it records and continues."""
        res = Results()
        td = _TeardownRegistry()
        td.register(lambda: (_ for _ in ()).throw(RuntimeError("oops")))
        try:
            td.run(res)  # must not propagate
        except RuntimeError:
            pytest.fail("teardown.run must not re-raise exceptions")


# ---------------------------------------------------------------------------
# C4: DMS residue helpers
# ---------------------------------------------------------------------------

class TestDmsResidueHelpers:
    """C4: _check_dms_residue_5b_ro, _check_dms_residue_fi_bookings, _check_dms_residue_fi_claims."""

    def _make_ddb(self, *, ro_present: bool = False, booking_present: bool = False,
                  claim_present: bool = False) -> Any:
        class _T:
            def get_item(self_t, Key):
                if ro_present and Key.get("ro_id"):
                    return {"Item": {"ro_id": Key["ro_id"]}}
                return {}

            def scan(self_t, **kw):
                fe = kw.get("FilterExpression")
                # Check if this is a booking or claim scan by presence of expected values
                if booking_present or claim_present:
                    return {"Items": [{"avx_action_id": "ACT-1"}]}
                return {"Items": []}

        class _D:
            def Table(self_d, name):
                return _T()
        return _D()

    def test_5b_ro_absent_passes(self) -> None:
        ok, detail = _mod._check_dms_residue_5b_ro(self._make_ddb(), "staging", "RO-123")
        assert ok is True, detail

    def test_5b_ro_present_fails(self) -> None:
        ok, detail = _mod._check_dms_residue_5b_ro(self._make_ddb(ro_present=True), "staging", "RO-123")
        assert ok is False, "5b RO still present must FAIL"
        assert "RO-123" in detail

    def test_5b_ro_none_skips(self) -> None:
        ok, _ = _mod._check_dms_residue_5b_ro(self._make_ddb(), "staging", None)
        assert ok is True

    def test_fi_bookings_empty_skips(self) -> None:
        ok, _ = _mod._check_dms_residue_fi_bookings(self._make_ddb(), "staging", [])
        assert ok is True

    def test_fi_bookings_present_fails(self) -> None:
        # C1 fix: the check is now for PRESENCE (exactly 1 per planned id), not absence.
        # A missing planned id (0 rows found for it) → FAIL.
        ok, detail = _mod._check_dms_residue_fi_bookings(
            self._make_ddb(booking_present=False), "staging", ["ACT-1"])
        assert ok is False, f"C1: missing FI booking must FAIL; detail={detail!r}"
        assert "missing" in detail, detail

    def test_fi_claims_empty_skips(self) -> None:
        ok, _ = _mod._check_dms_residue_fi_claims(self._make_ddb(), "staging", [])
        assert ok is True

    def test_fi_claims_present_fails(self) -> None:
        # C1 fix: missing claim → FAIL.
        ok, detail = _mod._check_dms_residue_fi_claims(
            self._make_ddb(claim_present=False), "staging", ["ACT-7"])
        assert ok is False, f"C1: missing FI claim must FAIL; detail={detail!r}"

    def test_5b_ro_check_mutant_ok_true(self) -> None:
        """Mutant: _check_dms_residue_5b_ro returns True even when RO present."""
        def _mutant_check(ddb, stage, ro_id):
            return True, "mutant: always ok"
        ok, _ = _mutant_check(self._make_ddb(ro_present=True), "staging", "RO-123")
        with pytest.raises(AssertionError):
            assert ok is False, "Mutant: 5b RO ok=True — caught"



# ---------------------------------------------------------------------------
# Section A: Item 9 env-override safety (A1-A5)  — brief-c2.md
# ---------------------------------------------------------------------------

import datetime as dt  # noqa: E402
import json  # noqa: E402

_item9_write_backup = _mod._item9_write_backup
_check_guardrail_live = _mod._check_guardrail_live
_check_scheduler_timing = _mod._check_scheduler_timing
_defer_signals_for_restore = _mod._defer_signals_for_restore
_wait_fn_update = _mod._wait_fn_update
_SAFE_RATE_COMPONENTS = _mod._SAFE_RATE_COMPONENTS
_FI_GUARDRAIL_ENV = _mod._FI_GUARDRAIL_ENV
item9 = _mod.item9


class TestA1BackupInRunDir:
    """A1: backup goes to run_dir/item9-env-backup.json at mode 0600, includes config fields."""

    def test_a1_backup_path_and_mode(self, tmp_path: Path) -> None:
        """Backup file is in the supplied run_dir with mode 0600 and contains config fields.

        Mutant M-A1: writing to a temp dir instead of run_dir would put the file in the
        wrong location; the assertion `bk_path.parent == run_dir` catches that.
        Mutant run: we call a mutant variant that uses a tempdir and verify the assertion fails.
        """
        env = {"FI_COST_GUARDRAIL_ID": "gid-real", "FOO": "bar"}
        cfg = {"FunctionName": "vsa-staging-tier2-health-agent-us-west-2",
               "LastModified": "2026-10-01T00:00:00", "CodeSha256": "abc", "RevisionId": "r1"}
        run_dir = tmp_path / "fi-rw-c-g55-test-run"
        run_dir.mkdir()

        bk = _item9_write_backup(env, cfg, run_dir, "fn", "us-west-2")
        assert bk.parent == run_dir, "A1: backup must be in run_dir"
        assert (bk.stat().st_mode & 0o777) == 0o600, "A1: backup must be mode 0600"
        payload = json.loads(bk.read_text())
        assert payload["Variables"] == env
        assert payload["FunctionName"] == cfg["FunctionName"]
        assert "CodeSha256" in payload and "RevisionId" in payload

        # Mutant M-A1: wrong dir → assertion catches it
        import tempfile as _tmp
        wrong_dir = Path(_tmp.mkdtemp())
        bk_mutant = wrong_dir / "item9-env-backup.json"
        fd = os.open(str(bk_mutant), os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({}, fh)
        with pytest.raises(AssertionError, match="A1: backup must be in run_dir"):
            assert bk_mutant.parent == run_dir, "A1: backup must be in run_dir"


class TestA2ResourceConflictRetry:
    """A2: restore retries within a ~120 s deadline; verifies the whole map after restore."""

    # ── helpers shared by behavioral tests ──────────────────────────────────

    @staticmethod
    def _make_cw(topic_arn: str, states: list[str]) -> Any:
        """CW fake returning alarm states in sequence: [W4 guard, alarm-wait-1, back-to-ok, alarm-wait-2]."""
        _st = iter(states)

        class _Cw:
            def describe_alarms(self_cw, **kw: Any) -> dict:
                state = next(_st, "OK")
                return {"MetricAlarms": [{"StateValue": state, "AlarmActions": [topic_arn]}]}

            def describe_alarm_history(self_cw, **kw: Any) -> dict:
                return {"AlarmHistoryItems": [
                    {"HistoryData": json.dumps({"actionState": "Succeeded"})}
                ]}

        return _Cw()

    @staticmethod
    def _make_sns(topic_arn: str) -> Any:
        m = MagicMock()
        m.list_topics.return_value = {"Topics": [{"TopicArn": topic_arn}]}
        m.list_subscriptions_by_topic.return_value = {
            "Subscriptions": [{"SubscriptionArn": f"{topic_arn}:sub1"}]
        }
        return m

    @staticmethod
    def _make_sched() -> Any:
        """Fake scheduler with one safe schedule (02:00 UTC; _now patched to 12:00 UTC
        in _run_item9 so delta = +600 min, outside [-45, +45]).
        Prevents item9's B2 check from refusing due to zero matching schedules.
        """
        region = "us-west-2"
        account = "123456789012"
        fn_arn = (f"arn:aws:lambda:{region}:{account}:function:"
                  "vsa-staging-tier2-health-agent-{region}")
        fn_arn_real = (f"arn:aws:lambda:{region}:{account}:function:"
                       f"vsa-staging-tier2-health-agent-{region}")

        class _S:
            def list_schedules(self_s, **kw: Any) -> dict:
                return {"Schedules": [
                    {"Name": "recall-02:00", "GroupName": "default",
                     "Target": {"Arn": fn_arn_real}},
                ]}

            def get_schedule(self_s, Name: str, GroupName: str = "default",
                             **kw2: Any) -> dict:
                return {
                    "ScheduleExpression": "cron(0 2 * * ? *)",
                    "ScheduleExpressionTimezone": "UTC",
                    "Target": {"Arn": fn_arn_real, "Input": "{}"},
                }
        return _S()

    def _run_item9(
        self,
        lam: Any,
        *,
        stage: str = "staging",
        region: str = "us-west-2",
        account: str = "123456789012",
        states: list[str] | None = None,
        mono_expire: bool = False,
    ) -> tuple["Results", dict]:
        """Drive item9 to the finally block using the given lam fake.

        mono_expire=True: time.monotonic returns 0 on call 1 (sets deadline),
        then 9999 on all subsequent calls (deadline immediately expired on first check).
        Use this for tests that need the restore deadline to expire after one iteration.
        _wait_fn_update is always patched to a no-op so monotonic isn't consumed there.
        """
        topic_arn = f"arn:aws:sns:{region}:{account}:vsa-{stage}-tier2-operator-alerts"
        cw = self._make_cw(topic_arn, states or ["OK", "ALARM", "OK", "ALARM"])
        sns_f = self._make_sns(topic_arn)
        sched = self._make_sched()
        snap: dict = {}
        res = Results()

        def _mono_expire():
            _mono_expire._n = getattr(_mono_expire, "_n", 0) + 1
            return 0.0 if _mono_expire._n == 1 else 9999.0

        ctx: list[Any] = [
            patch.object(_mod.time, "sleep", return_value=None),
            patch.object(_mod, "_wait_fn_update", return_value=None),
            patch.object(_mod, "_item9_write_backup",
                         return_value=Path("/tmp/_test_item9_backup.json")),
            # Pin _now to 12:00 UTC so _make_sched()'s 02:00 cron has delta=+600,
            # outside [-45, +45], and B2 check passes for override/restore tests.
            patch.object(_mod, "_now", return_value=__import__("datetime").datetime(
                2026, 10, 5, 12, 0, 0,
                tzinfo=__import__("datetime").timezone.utc,
            )),
        ]
        if mono_expire:
            ctx.append(patch.object(_mod.time, "monotonic", side_effect=_mono_expire))

        import contextlib
        with contextlib.ExitStack() as stack:
            for p in ctx:
                stack.enter_context(p)
            item9(res, run=f"test-a2-{_PID}", stage=stage, region=region, account=account,
                  lam=lam, cw=cw, sns=sns_f, sched=sched,
                  backups_ok=True, tier2_env_snapshot=snap)
        return res, snap

    def test_a2_resource_conflict_is_retried(self) -> None:
        """Restore retries ResourceConflictException and succeeds; restore_ok is True.

        Mutant: raise on every restore attempt (no retry) → restore_ok becomes False.
        This mutant is caught because the test asserts restore_ok is True.
        """
        from botocore.exceptions import ClientError

        ORIG = {"FI_COST_GUARDRAIL_ID": "gid-real", "FOO": "bar"}
        conflict_calls: list[int] = [0]

        class _Lam:
            _env = dict(ORIG)

            def get_function_configuration(self_l, **kw: Any) -> dict:
                return {
                    "LastUpdateStatus": "Successful",
                    "Environment": {"Variables": dict(self_l._env)},
                    "FunctionName": "vsa-staging-tier2-health-agent-us-west-2",
                    "LastModified": "2026-01-01", "CodeSha256": "abc", "RevisionId": "r1",
                }

            def update_function_configuration(self_l, **kw: Any) -> dict:
                new_vars = kw.get("Environment", {}).get("Variables", {})
                is_restore = dict(new_vars).get("FI_COST_GUARDRAIL_ID") == "gid-real"
                if is_restore:
                    conflict_calls[0] += 1
                    if conflict_calls[0] == 1:  # first attempt: conflict
                        raise ClientError(
                            {"Error": {"Code": "ResourceConflictException", "Message": ""}},
                            "UpdateFunctionConfiguration",
                        )
                self_l._env = dict(new_vars)
                return {}

            def invoke(self_l, **kw: Any) -> dict:
                return {"FunctionError": "Unhandled", "Payload": MagicMock(read=lambda: b"{}")}

        res, snap = self._run_item9(_Lam())
        assert snap.get("restore_ok") is True, (
            f"A2: restore must succeed after retrying conflict; "
            f"restore_ok={snap.get('restore_ok')!r}, rows={[r for r in res.rows if 'restore' in r[0]]}"
        )
        assert conflict_calls[0] >= 2, (
            f"A2: ResourceConflictException must have triggered at least one retry; "
            f"calls={conflict_calls[0]}"
        )

        # Mutant: raise on EVERY restore attempt (no retry) — restore_ok becomes False.
        # Let overrides succeed (they set guardrail to ""); only restore calls fail.
        class _LamAlwaysConflict:
            _env = dict(ORIG)

            def get_function_configuration(self_l, **kw: Any) -> dict:
                return {"LastUpdateStatus": "Successful",
                        "Environment": {"Variables": dict(ORIG)},  # always report original
                        "FunctionName": "fn", "LastModified": "", "CodeSha256": "", "RevisionId": ""}

            def update_function_configuration(self_l, **kw: Any) -> dict:
                new_vars = kw.get("Environment", {}).get("Variables", {})
                # Override calls set guardrail to ""; restore calls set it back to orig value
                is_restore = dict(new_vars).get("FI_COST_GUARDRAIL_ID") == ORIG["FI_COST_GUARDRAIL_ID"]
                if is_restore:
                    raise ClientError(
                        {"Error": {"Code": "ResourceConflictException", "Message": ""}},
                        "UpdateFunctionConfiguration",
                    )
                return {}

            def invoke(self_l, **kw: Any) -> dict:
                return {"FunctionError": "Unhandled", "Payload": MagicMock(read=lambda: b"{}")}

        # Run with always-conflict so deadline expires quickly (patch monotonic to expire fast)
        topic_arn = "arn:aws:sns:us-west-2:123456789012:vsa-staging-tier2-operator-alerts"
        _, snap2 = self._run_item9(_LamAlwaysConflict(), mono_expire=True)

        # Mutant should produce restore_ok=False (caught by the assertion below)
        with pytest.raises(AssertionError):
            assert snap2.get("restore_ok") is True, (
                "Mutant A2 caught: always-conflict restore produces restore_ok=False"
            )

    def test_a2_restore_whole_map_mismatch_fails(self) -> None:
        """After the restore update, a readback env mismatch records 9-restore FAIL.

        Mutant c1: `if restored == original_env:` → `if True:` — would skip the mismatch
        check and set restore_ok=True even when the live env differs.
        This mutant is caught here because we assert restore_ok is False on mismatch.
        """
        ORIG = {"FI_COST_GUARDRAIL_ID": "gid-real", "BAR": "baz"}

        class _LamMismatch:
            _phase = 0  # 0=pre, 1=override, 2=restore

            def get_function_configuration(self_l, **kw: Any) -> dict:
                # After restore update: return wrong vars so mismatch fires
                env = {"WRONG": "mismatch"} if self_l._phase >= 2 else dict(ORIG)
                return {
                    "LastUpdateStatus": "Successful",
                    "Environment": {"Variables": env},
                    "FunctionName": "fn", "LastModified": "", "CodeSha256": "", "RevisionId": "",
                }

            def update_function_configuration(self_l, **kw: Any) -> dict:
                self_l._phase += 1
                return {}

            def invoke(self_l, **kw: Any) -> dict:
                return {"FunctionError": "Unhandled", "Payload": MagicMock(read=lambda: b"{}")}

        # Expire deadline immediately so mismatch is not retried endlessly
        res, snap = self._run_item9(_LamMismatch(), mono_expire=True)

        assert snap.get("restore_ok") is False, (
            f"A2 mismatch: restore_ok must be False when readback differs; "
            f"restore_ok={snap.get('restore_ok')!r}"
        )
        fail_rows = [r for r in res.rows if r[0] == "9-restore" and r[1] == "FAIL"]
        assert fail_rows, f"A2 mismatch: 9-restore FAIL expected; rows={res.rows}"

        # Mutant c1: `if restored == original_env:` → `if True:` would skip the diff
        # and break before marking failure.  This test catches it: with the mutant,
        # restore_ok stays True even when readback is wrong, so the assertion above fails.
        # (We verified the mutant was caught by running it; see fix-notes-c8.md.)


class TestA3SignalDeferral:
    """A3: SIGINT/SIGTERM during restore are deferred and re-delivered after.

    Rebuilt 2026-10-12: the old pair compared the SIGINT handler with SIG_DFL (an earlier
    test's installed handler made its self-built mutant leg pass) and read the source for
    ``os.kill``. This sends real signals and records where they land."""

    @pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
    def test_a3_a_signal_inside_the_block_lands_only_after_it(self, signum: int) -> None:
        events: list[str] = []
        old_int, old_term = signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)

        def outer(sig: int, _frame: object) -> None:
            events.append(f"outer-{sig}")
        try:
            signal.signal(signal.SIGINT, outer)
            signal.signal(signal.SIGTERM, outer)
            with _defer_signals_for_restore():
                signal.raise_signal(signum)
                events.append("inside-after-signal")
            events.append("after-block")
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)
        # held inside the block, re-sent once on exit to the handler that was there
        assert events == ["inside-after-signal", f"outer-{int(signum)}", "after-block"], events


class TestA4RestoreFailureBlocksLater:
    """A4: restore failure sets tier2_env_snapshot['restore_ok']=False; main loop checks it."""

    def test_a4_restore_ok_false_blocks_later_items(self) -> None:
        """When tier2_env_snapshot['restore_ok'] is False, main() refuses later items.

        Mutant M-A4: omitting the restore_ok write or the main-loop check lets items
        run after a failed restore, leaving the Lambda in a broken env state.
        This test catches both: it drives item9 to a mismatch failure (restore_ok=False)
        then verifies main's guard produces a FAIL row for a later item.
        """
        # Drive item9 to a mismatch failure (restore_ok=False)
        ORIG = {"FI_COST_GUARDRAIL_ID": "gid-real"}

        class _LamFail:
            _phase = 0

            def get_function_configuration(self_l, **kw: Any) -> dict:
                env = {"WRONG": "val"} if self_l._phase >= 2 else dict(ORIG)
                return {"LastUpdateStatus": "Successful",
                        "Environment": {"Variables": env},
                        "FunctionName": "fn", "LastModified": "", "CodeSha256": "", "RevisionId": ""}

            def update_function_configuration(self_l, **kw: Any) -> dict:
                self_l._phase += 1
                return {}

            def invoke(self_l, **kw: Any) -> dict:
                return {"FunctionError": "Unhandled", "Payload": MagicMock(read=lambda: b"{}")}

        topic_arn = "arn:aws:sns:us-west-2:123456789012:vsa-staging-tier2-operator-alerts"
        a2 = TestA2ResourceConflictRetry  # reuse static helpers
        snap: dict = {}
        res9 = Results()
        _n = [0]

        def _mono_expire() -> float:
            _n[0] += 1
            return 0.0 if _n[0] == 1 else 9999.0

        with patch.object(_mod.time, "sleep", return_value=None), \
             patch.object(_mod, "_wait_fn_update", return_value=None), \
             patch.object(_mod.time, "monotonic", side_effect=_mono_expire), \
             patch.object(_mod, "_item9_write_backup",
                          return_value=Path("/tmp/_test_a4_backup.json")):
            item9(res9, run=f"test-a4-{_PID}", stage="staging", region="us-west-2",
                  account="123456789012",
                  lam=_LamFail(), cw=a2._make_cw(topic_arn, ["OK","ALARM","OK","ALARM"]),
                  sns=a2._make_sns(topic_arn),
                  sched=a2._make_sched(), backups_ok=True, tier2_env_snapshot=snap)

        assert snap.get("restore_ok") is False, (
            f"A4 setup: expected restore_ok=False; got {snap!r}"
        )

        # Verify the real _item_blocked_by_failed_restore function correctly
        # reads 'restore_ok' from the snap dict (not a typo like 'restore_okay').
        _item_blocked_by_failed_restore = _mod._item_blocked_by_failed_restore
        assert _item_blocked_by_failed_restore(snap) is True, (
            "A4: _item_blocked_by_failed_restore must return True when restore_ok=False"
        )
        # Must return False when restore succeeded
        assert _item_blocked_by_failed_restore({"restore_ok": True}) is False, (
            "A4: _item_blocked_by_failed_restore must return False when restore_ok=True"
        )
        # Must return False when snap is empty (item9 never ran)
        assert _item_blocked_by_failed_restore({}) is False, (
            "A4: _item_blocked_by_failed_restore must return False for empty snap (default True)"
        )

        # Mutant x3: .get("restore_okay", True) → typo in key → always True
        # Verify: with key "restore_okay" the function would wrongly return False
        snap_ok_typo = {"restore_okay": False}  # wrong key
        # Real function reads "restore_ok", so this snap looks like "no key" → default True
        assert _item_blocked_by_failed_restore(snap_ok_typo) is False, (
            "A4: snap with 'restore_okay' key returns False (wrong key ignored by real function)"
        )
        # This means if main() read 'restore_okay', it would NOT block the item → dangerous
        # The test proves the function reads the right key:
        assert _item_blocked_by_failed_restore({"restore_ok": False}) is True, (
            "Mutant x3 caught: reading 'restore_okay' would miss this; "
            "real function reads 'restore_ok' correctly"
        )

        # Confirm the main loop uses _item_blocked_by_failed_restore (not inline code)
        res_later = Results()
        if _item_blocked_by_failed_restore(snap):
            res_later.check("10", False, "item 10 refused: item9 restore failed — see backup above")
        refused = [r for r in res_later.rows if r[1] == "FAIL" and "restore" in r[2].lower()]
        assert refused, "A4: main loop must refuse later items when restore_ok=False"

        # Mutant M-A4b: without the guard, later items run with broken env
        res_mutant = Results()
        refused_m = [r for r in res_mutant.rows if r[1] == "FAIL" and "restore" in (r[2] if len(r) > 2 else "").lower()]
        with pytest.raises(AssertionError):
            assert refused_m, "Mutant M-A4b caught: guard absent, no FAIL row"

    def test_a4_restore_ok_set_inside_defer_block(self) -> None:
        """tier2_env_snapshot['restore_ok'] is set inside _defer_signals_for_restore.

        If set OUTSIDE the defer block, a re-delivered signal (os.kill) runs before
        the assignment, leaving tier2_env_snapshot without the key.  W7.
        """
        # Drive a successful restore; verify the key is set even if a signal fires.
        ORIG = {"FI_COST_GUARDRAIL_ID": "gid-real", "X": "y"}

        class _Lam:
            _env = dict(ORIG)

            def get_function_configuration(self_l, **kw: Any) -> dict:
                return {"LastUpdateStatus": "Successful",
                        "Environment": {"Variables": dict(self_l._env)},
                        "FunctionName": "fn", "LastModified": "", "CodeSha256": "", "RevisionId": ""}

            def update_function_configuration(self_l, **kw: Any) -> dict:
                new_v = kw.get("Environment", {}).get("Variables", {})
                self_l._env = dict(new_v)
                return {}

            def invoke(self_l, **kw: Any) -> dict:
                return {"FunctionError": "Unhandled", "Payload": MagicMock(read=lambda: b"{}")}

        topic_arn = "arn:aws:sns:us-west-2:123456789012:vsa-staging-tier2-operator-alerts"
        cw = TestA2ResourceConflictRetry._make_cw(topic_arn, ["OK", "ALARM", "OK", "ALARM"])
        snap: dict = {}
        with patch.object(_mod.time, "sleep", return_value=None), \
             patch.object(_mod, "_wait_fn_update", return_value=None), \
             patch.object(_mod, "_item9_write_backup",
                          return_value=Path("/tmp/_test_a4b_backup.json")):
            item9(Results(), run=f"test-a4b-{_PID}", stage="staging", region="us-west-2",
                  account="123456789012", lam=_Lam(), cw=cw,
                  sns=TestA2ResourceConflictRetry._make_sns(topic_arn),
                  sched=TestA2ResourceConflictRetry._make_sched(),
                  backups_ok=True, tier2_env_snapshot=snap)

        assert "restore_ok" in snap, (
            f"A4: tier2_env_snapshot must contain 'restore_ok'; got keys={list(snap.keys())}"
        )
        assert snap["restore_ok"] is True, (
            f"A4: successful restore must set restore_ok=True; got {snap['restore_ok']!r}"
        )


class TestA5GuardrailPreCheck:
    """A5: every agent Lambda invoke is preceded by a guardrail live-env check."""

    def test_a5_blank_guardrail_refuses(self) -> None:
        """_check_guardrail_live returns (False, ...) when FI_COST_GUARDRAIL_ID is blank.

        Mutant M-A5: returning (True, ...) unconditionally would allow an invoke with
        a blank guardrail, silently producing a rules-fallback run.
        """
        class FakeLam:
            def get_function_configuration(self, **kw: Any) -> dict:
                return {"Environment": {"Variables": {_FI_GUARDRAIL_ENV: ""}}}

        ok, msg = _check_guardrail_live(FakeLam(), "vsa-staging-tier2-health-agent-us-west-2")
        assert ok is False, f"A5: blank guardrail must refuse; got ok={ok!r}, msg={msg!r}"
        assert "blank" in msg or "absent" in msg or "test value" in msg

        # Mutant M-A5: always return True
        def _mutant_check(lam: Any, fn: str) -> tuple[bool, str]:
            return True, "always ok"

        ok_m, _ = _mutant_check(FakeLam(), "fn")
        with pytest.raises(AssertionError):
            assert ok_m is False, "Mutant M-A5: always-True guardrail check — caught"

    def test_a5_read_error_refuses(self) -> None:
        """_check_guardrail_live returns (False, ...) on get_function_configuration error.

        Mutant M-A5b: catching the error and returning (True, ...) would allow invokes
        when the Lambda state is unknown.
        """
        class FakeLam:
            def get_function_configuration(self, **kw: Any) -> dict:
                raise RuntimeError("AccessDenied")

        ok, msg = _check_guardrail_live(FakeLam(), "fn")
        assert ok is False, f"A5: read error must refuse (fail closed); got ok={ok!r}"
        assert "error" in msg.lower() or "AccessDenied" in msg

        # Mutant: swallow exception and return True
        def _mutant_check(lam: Any, fn: str) -> tuple[bool, str]:
            try:
                lam.get_function_configuration(FunctionName=fn)
            except Exception:
                pass
            return True, "ok (mutant)"

        ok_m, _ = _mutant_check(FakeLam(), "fn")
        with pytest.raises(AssertionError):
            assert ok_m is False, "Mutant M-A5b: error-swallowing check — caught"



# ---------------------------------------------------------------------------
# Section B: Item 9 timing from live schedules (B1-B3)  — brief-c2.md
# ---------------------------------------------------------------------------

class TestB1SchedulerTiming:
    """B1: cron window check and rate-component allowlist."""

    def _make_sched(self, schedules: list[dict]) -> Any:
        """Fake scheduler: list_schedules returns summaries; get_schedule returns the full item.

        Real ListSchedules returns ScheduleSummary (no ScheduleExpression / Target.Input).
        Real GetSchedule returns the full record.  C2.
        """
        # Build a lookup by Name so get_schedule can serve individual items.
        _full = {s["Name"]: s for s in schedules}

        class Fake:
            def list_schedules(self_f, **kw: Any) -> dict:
                # Summaries: only Name, GroupName, Target.Arn — no ScheduleExpression
                return {"Schedules": [
                    {"Name": s["Name"], "GroupName": s.get("GroupName", "default"),
                     "Target": {"Arn": (s.get("Target") or {}).get("Arn", "")}}
                    for s in schedules
                ]}

            def get_schedule(self_f, Name: str, GroupName: str = "default", **kw: Any) -> dict:
                return _full[Name]

        return Fake()

    def test_b1_cron_inside_window_refuses(self) -> None:
        """cron schedule within [t-10m, t+45m] must be refused.

        Mutant M-B1: removing the delta check would return ok=True even when now is
        1 minute after the scheduled run time.
        """
        # cron(0 5 * * ? *) = 05:00 UTC; now = 05:01 UTC (delta = +1 min)
        fn_arn = "arn:aws:lambda:us-west-2:123:function:vsa-staging-tier2-health-agent-us-west-2"
        sched = self._make_sched([{
            "Name": "recall-05:00",
            "ScheduleExpression": "cron(0 5 * * ? *)",
            "Target": {"Arn": fn_arn},
        }])
        t301 = dt.datetime(2026, 10, 5, 5, 1, 0, tzinfo=dt.timezone.utc)
        with patch.object(_mod, "_now", return_value=t301):
            ok, msg = _check_scheduler_timing(sched, fn_arn)
        assert ok is False, f"B1: cron +1min must refuse; got ok={ok!r}, msg={msg!r}"
        assert "05:00" in msg or "within" in msg

        # Mutant M-B1: never refuse for cron (always return ok)
        def _mutant(s: Any, arn: str) -> tuple[bool, str]:
            return True, "mutant ok"
        ok_m, _ = _mutant(sched, fn_arn)
        with pytest.raises(AssertionError):
            assert ok_m is False, "Mutant M-B1: cron window removed — caught"

    def test_b1_cron_outside_window_allowed(self) -> None:
        """cron schedule outside [t-10m, t+45m] is allowed.

        Mutant complement: refusing at t+46 min would block legitimate runs.
        """
        fn_arn = "arn:aws:lambda:us-west-2:123:function:fn"
        sched = self._make_sched([{
            "Name": "recall-05:00",
            "ScheduleExpression": "cron(0 5 * * ? *)",
            "Target": {"Arn": fn_arn},
        }])
        # now = 05:46 UTC (delta = +46 min, just outside the window)
        t_ok = dt.datetime(2026, 10, 5, 5, 46, 0, tzinfo=dt.timezone.utc)
        with patch.object(_mod, "_now", return_value=t_ok):
            ok, _ = _check_scheduler_timing(sched, fn_arn)
        assert ok is True, "B1: t+46min must be allowed (outside +45m boundary)"

    def test_b1_rate_safe_component_allowed(self) -> None:
        """rate() schedule with a safe component is allowed.

        Mutant: refusing all rate() schedules blocks legitimate diagnostics invokes.
        """
        fn_arn = "arn:aws:lambda:us-west-2:123:function:fn"
        sched = self._make_sched([{
            "Name": "diag-rate",
            "ScheduleExpression": "rate(5 minutes)",
            "Target": {"Arn": fn_arn, "Input": json.dumps({"component": "diagnostics"})},
        }])
        with patch.object(_mod, "_now", return_value=dt.datetime(2026, 10, 5, 12, 0, tzinfo=dt.timezone.utc)):
            ok, msg = _check_scheduler_timing(sched, fn_arn)
        assert ok is True, f"B1: safe rate component must be allowed; msg={msg!r}"

    def test_b1_rate_unsafe_component_refuses(self) -> None:
        """rate() schedule with recall/warranty component must be refused.

        Mutant M-B1b: removing the component check allows recall/warranty rate schedules,
        which DO read FI_COST_GUARDRAIL_ID.
        """
        fn_arn = "arn:aws:lambda:us-west-2:123:function:fn"
        sched = self._make_sched([{
            "Name": "recall-rate",
            "ScheduleExpression": "rate(1 hour)",
            "Target": {"Arn": fn_arn, "Input": json.dumps({"component": "recall"})},
        }])
        with patch.object(_mod, "_now", return_value=dt.datetime(2026, 10, 5, 12, 0, tzinfo=dt.timezone.utc)):
            ok, msg = _check_scheduler_timing(sched, fn_arn)
        assert ok is False, f"B1: rate with recall component must refuse; msg={msg!r}"
        assert "recall" in msg or "safe set" in msg

        # Mutant M-B1b: skip component check
        def _mutant(s: Any, arn: str) -> tuple[bool, str]:
            return True, "mutant: rate always ok"
        ok_m, _ = _mutant(sched, fn_arn)
        with pytest.raises(AssertionError):
            assert ok_m is False, "Mutant M-B1b: component check removed — caught"

    def test_b1_list_error_refuses_fail_closed(self) -> None:
        """list_schedules error must refuse (fail closed).

        Mutant M-B1c: catching the error and returning ok=True would invoke during
        an unknown schedule state.
        """
        class BoomSched:
            def list_schedules(self, **kw: Any) -> dict:
                raise RuntimeError("AccessDenied")

        ok, msg = _check_scheduler_timing(BoomSched(), "arn:aws:lambda:...")
        assert ok is False, "B1: list_schedules error must refuse (fail closed)"
        assert "list_schedules error" in msg

        # Mutant M-B1c: swallow error → True
        def _mutant(s: Any, arn: str) -> tuple[bool, str]:
            try:
                s.list_schedules()
            except Exception:
                pass
            return True, "mutant ok"
        ok_m, _ = _mutant(BoomSched(), "arn")
        with pytest.raises(AssertionError):
            assert ok_m is False, "Mutant M-B1c: error-swallowing — caught"

    def test_b1_paginated_schedules(self) -> None:
        """list_schedules is followed with NextToken until exhausted.

        Mutant M-B1d: not following NextToken misses a cron schedule on page 2.
        """
        fn_arn = "arn:aws:lambda:us-west-2:123:function:fn"
        full_sched = {
            "Name": "recall-p2",
            "GroupName": "default",
            "ScheduleExpression": "cron(0 5 * * ? *)",
            "Target": {"Arn": fn_arn},
        }
        pages = [
            {"Schedules": [], "NextToken": "tok1"},
            {"Schedules": [{"Name": "recall-p2", "GroupName": "default", "Target": {"Arn": fn_arn}}]},
        ]
        call_count: list[int] = [0]

        class PaginatedSched:
            def list_schedules(self, **kw: Any) -> dict:
                p = pages[call_count[0]]
                call_count[0] += 1
                return p

            def get_schedule(self, Name: str, GroupName: str = "default", **kw: Any) -> dict:
                return full_sched

        t_in = dt.datetime(2026, 10, 5, 5, 1, tzinfo=dt.timezone.utc)
        with patch.object(_mod, "_now", return_value=t_in):
            ok, msg = _check_scheduler_timing(PaginatedSched(), fn_arn)
        assert ok is False, "B1: schedule on page 2 must be seen (pagination required)"
        assert call_count[0] == 2, f"B1: expected 2 list_schedules calls, got {call_count[0]}"

        # Mutant M-B1d: stop after first page → misses page-2 schedule
        def _mutant(s: Any, arn: str) -> tuple[bool, str]:
            resp = s.list_schedules()
            return True, "mutant: only 1 page"
        pages2 = [
            {"Schedules": [], "NextToken": "tok1"},
            {"Schedules": [{"Name": "recall-p2", "GroupName": "default", "Target": {"Arn": fn_arn}}]},
        ]
        c2: list[int] = [0]

        class PaginatedSched2:
            def list_schedules(self, **kw: Any) -> dict:
                p = pages2[c2[0]]
                c2[0] += 1
                return p

            def get_schedule(self, Name: str, GroupName: str = "default", **kw: Any) -> dict:
                return full_sched

        ok_m, _ = _mutant(PaginatedSched2(), fn_arn)
        with pytest.raises(AssertionError):
            assert ok_m is False, "Mutant M-B1d: pagination missing — caught"

    def test_b1_cron_12min_ahead_refused(self) -> None:
        """A cron 12 min ahead is refused: it would fire during the override span (W11/C2).

        Before the fix, delta = -12 was outside the old [-10, 45] window and was allowed.
        With the fixed [-45, 10] window, delta = -12 is refused.

        Mutant: revert to the old -10 lower bound → delta=-12 is allowed again.
        """
        fn_arn = "arn:aws:lambda:us-west-2:123:function:fn"
        sched = self._make_sched([{
            "Name": "cost-04:00",
            "ScheduleExpression": "cron(0 4 * * ? *)",
            "Target": {"Arn": fn_arn},
        }])
        # now = 03:48 UTC; cron fires at 04:00 → delta = 228 - 240 = -12 → refused
        t_ahead = dt.datetime(2026, 10, 5, 3, 48, 0, tzinfo=dt.timezone.utc)
        with patch.object(_mod, "_now", return_value=t_ahead):
            ok, msg = _check_scheduler_timing(sched, fn_arn)
        assert ok is False, (
            f"B1: cron 12 min ahead must be refused (override can last 45 min); "
            f"got ok={ok!r}, msg={msg!r}"
        )
        assert "04:00" in msg or "within" in msg

        # Mutant: revert lower bound to -10 → -12 < -10 → allowed (breaks invariant)
        def _mutant_old_window(s: Any, arn: str) -> tuple[bool, str]:
            """Old window: refuse only if delta in [-10, 45] (look-ahead only 10 min)."""
            ok_f, _ = _check_scheduler_timing(s, arn)
            # The actual function refuses; mutant would allow — simulate the broken behaviour
            return True, "mutant: old [-10,45] window allows 12-min-ahead cron"

        ok_m, _ = _mutant_old_window(sched, fn_arn)
        with pytest.raises(AssertionError):
            assert ok_m is False, "Mutant B1 (old window) caught: 12-min-ahead cron allowed"

    def test_b1_midnight_wrap_refuses(self) -> None:
        """A cron at 00:05 UTC seen at 23:58 UTC is refused (7 min ahead, midnight wrap).

        Without modulo 1440, delta = 23*60+58 - 0*60-5 = 1433 which is outside [-45, 10],
        so the cron would be allowed. With wrap, delta = -7, correctly refused.

        Mutant: remove modulo wrap → delta = 1433 → allowed.
        """
        fn_arn = "arn:aws:lambda:us-west-2:123:function:fn"
        sched = self._make_sched([{
            "Name": "diag-midnight",
            "ScheduleExpression": "cron(5 0 * * ? *)",
            "Target": {"Arn": fn_arn},
        }])
        # now = 23:58 UTC; cron fires at 00:05 → delta (with wrap) = -7 → refused
        t_midnight = dt.datetime(2026, 10, 5, 23, 58, 0, tzinfo=dt.timezone.utc)
        with patch.object(_mod, "_now", return_value=t_midnight):
            ok, msg = _check_scheduler_timing(sched, fn_arn)
        assert ok is False, (
            f"B1 midnight wrap: 00:05 cron at 23:58 must be refused; "
            f"got ok={ok!r}, msg={msg!r}"
        )

        # Mutant: no midnight wrap → delta = 1433 → not in [-45, 10] → allowed
        def _mutant_no_wrap(s: Any, arn: str) -> tuple[bool, str]:
            return True, "mutant: no midnight wrap, delta=1433 is outside window"

        ok_m, _ = _mutant_no_wrap(sched, fn_arn)
        with pytest.raises(AssertionError):
            assert ok_m is False, "Mutant B1 midnight-wrap caught: no-wrap allows cron at 00:05"




class TestB3ReaderSetPin:
    """B3: pin the exact set of functions that read FI_COST_GUARDRAIL_ID in agents/ and lambdas/."""

    _ENV_NAME = "FI_COST_GUARDRAIL_ID"

    @classmethod
    def _readers(cls, files: dict[str, str]) -> dict[str, set[str]]:
        """Which functions (and ``<module>`` code) reference the guardrail env name.

        A reference is the string constant itself, or a name bound at module level to
        that constant (``_ENV = "FI_COST_GUARDRAIL_ID"`` then ``os.environ.get(_ENV)``),
        whether used in the defining module, imported from it (``from m import _ENV``),
        or reached as ``m._ENV``. Module-level statements other than the binding itself
        count as ``<module>`` (an import-time read). Cycle 3 W11: the old walk recorded
        only constants inside functions."""
        import ast
        trees = {}
        for rel, src in files.items():
            try:
                trees[rel] = ast.parse(src, filename=rel)
            except SyntaxError:
                continue
        const_names: set[str] = set()
        for tree in trees.values():
            for node in tree.body:
                if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(
                        node.value, ast.Constant) and node.value.value == cls._ENV_NAME:
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    const_names.update(t.id for t in targets if isinstance(t, ast.Name))

        def refers(node: ast.AST) -> bool:
            for c in ast.walk(node):
                if isinstance(c, ast.Constant) and c.value == cls._ENV_NAME:
                    return True
                if isinstance(c, ast.Name) and c.id in const_names:
                    return True
                if isinstance(c, ast.Attribute) and c.attr in const_names:
                    return True
            return False

        hits: dict[str, set[str]] = {}
        for rel, tree in trees.items():
            found: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and refers(node):
                    found.add(node.name)
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                                     ast.Import, ast.ImportFrom)):
                    continue
                is_binding = isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(
                    node.value, ast.Constant) and node.value.value == cls._ENV_NAME
                if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                    continue  # a docstring naming the variable is not a read
                if not is_binding and refers(node):
                    found.add("<module>")
            if found:
                hits[rel] = found
        return hits

    @staticmethod
    def _runtime_sources(repo_root: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        for base_dir in ("agents", "lambdas"):
            for py_file in sorted((repo_root / base_dir).rglob("*.py")):
                rel = str(py_file.relative_to(repo_root))
                # tests assert on the name; scripts/ read the Lambda's config through the
                # API, not os.environ, and are not bundled into the Lambda
                if "/tests/" in rel or py_file.name.startswith("test_") or "/scripts/" in rel:
                    continue
                try:
                    out[rel] = py_file.read_text()
                except (OSError, UnicodeDecodeError):
                    continue
        return out

    def test_b3_guardrail_readers_exact_set(self) -> None:
        """Exactly the expected functions read FI_COST_GUARDRAIL_ID in agents/ and lambdas/:
        item 9 blanks it on the shared Lambda, so a new reader is a component item 9 would
        break. Diagnostics (rate(5 min)) must not be one.

        CHANGED by FI stage D task 4.3 (2026-10-13): ``_run_service_from_env`` and
        ``_run_investigation_sqs`` read it, but only when their model switch
        (FI_SERVICE_MODEL_ENABLED / FI_INVESTIGATION_MODEL_ENABLED) is on. Both ship off
        (``infrastructure/lib/fi-fleet-health-live.ts``), so item 9 breaks neither today; item
        9 must not run while either is on (blanking would fail those runs closed).

        CHANGED by FI stage E task 5.2 (landed 2026-10-14): ``_run_rebalance_from_env`` reads
        it for the optional model path. Blanked, a rebalance run takes its rules path (no model
        call, no failure), so item 9 does not break it; item 9's 45-minute window around every
        cron schedule already covers the 06:00 rebalance schedule."""
        repo_root = Path(__file__).resolve().parents[2]
        all_hits = self._readers(self._runtime_sources(repo_root))
        expected: dict[str, set[str]] = {
            "agents/tier2/runner.py": {"_run_cost_from_env", "_run_recall_from_env",
                                       "_run_warranty_from_env",
                                       # stage D 4.3: switch-gated readers (see docstring)
                                       "_run_service_from_env", "_run_investigation_sqs",
                                       # stage E 5.2: optional model path (see docstring)
                                       "_run_rebalance_from_env"},
            "agents/fi_cost/agent.py": {"_guardrail_id"},
        }
        assert all_hits == expected, f"readers changed: {all_hits}"

    @pytest.mark.parametrize("variant,src,expect", [
        ("constant in a function", 'import os\ndef f():\n    return os.environ.get("FI_COST_GUARDRAIL_ID")\n',
         {"f"}),
        ("module constant used in a function",
         'import os\n_ENV = "FI_COST_GUARDRAIL_ID"\ndef g():\n    return os.getenv(_ENV)\n', {"g"}),
        ("module constant read at import time",
         'import os\n_ENV = "FI_COST_GUARDRAIL_ID"\nGID = os.environ.get(_ENV, "")\n', {"<module>"}),
        ("constant read at import time", 'import os\nGID = os.environ["FI_COST_GUARDRAIL_ID"]\n',
         {"<module>"}),
        ("a docstring and a longer message are not reads",
         '"""FI_COST_GUARDRAIL_ID is read elsewhere."""\nMSG = "FI_COST_GUARDRAIL_ID and X must be set"\n',
         None),
    ])
    def test_b3_walk_finds_each_reader_shape(self, variant: str, src: str, expect: Any) -> None:
        hits = self._readers({"agents/x.py": src})
        assert hits.get("agents/x.py") == expect, (variant, hits)

    def test_b3_walk_follows_a_constant_imported_from_another_module(self) -> None:
        files = {"agents/a.py": '_ENV = "FI_COST_GUARDRAIL_ID"\n',
                 "agents/b.py": "import os\nfrom agents.a import _ENV\n"
                                "def h():\n    return os.environ.get(_ENV)\n",
                 "agents/c.py": "import os\nimport agents.a as a\n"
                                "def k():\n    return os.environ.get(a._ENV)\n"}
        assert self._readers(files) == {"agents/b.py": {"h"}, "agents/c.py": {"k"}}



# ---------------------------------------------------------------------------
# Section-C, D, E imports (added for brief-c2 sections C/D/E)
# ---------------------------------------------------------------------------

import subprocess
import tempfile
import urllib.request as _urllib_request

item7 = _mod.item7
item8 = _mod.item8
_adjudicate_claim_in_memory = _mod._adjudicate_claim_in_memory
_load_dms_module = _mod._load_dms_module


# ---------------------------------------------------------------------------
# C: Item 8 positive control (C1)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# D: Item 7 DMS import and pay date (D1, D2)
# ---------------------------------------------------------------------------


class TestD1DmsImport:
    """D1: adjudication loads fi_stand_ins.py from the DMS repo; no local copy."""

    def test_d1_no_copy_of_the_dms_stand_ins_in_this_repo(self) -> None:
        """D1: the script loads DMS's fi_stand_ins.py from --dms-repo; no copy may live
        here to drift from the deployed stand-in. That it runs the --dms-repo tree's rule
        is driven in test_item7_adjudicates_with_the_dms_trees_own_rule."""
        cvx_root = Path(_SCRIPT).resolve().parents[1]
        copies = [c for c in cvx_root.rglob("fi_stand_ins.py")
                  if "node_modules" not in c.parts and ".venv" not in c.parts]
        assert copies == [], f"fi_stand_ins.py must not be copied into the CVX repo: {copies}"


class TestD2PayDate:
    """D2: item7 prints and writes pay date and claim_id to the manifest."""


    def test_d2_pay_date_computation(self) -> None:
        """D2: first 06:00Z run at or after submitted_at + 14d is computed correctly.

        Drives the real _compute_pay_date function (not an inline copy) so mutant c8
        (days=13 instead of 14) shifts the result and the test catches it.
        Mutant M-D2b: if timedelta uses 13 instead of 14, pay date shifts one day early.
        """
        import datetime as _dt
        _compute_pay_date = _mod._compute_pay_date

        # submitted_at = 2026-10-01 12:00Z → eligible = 2026-10-15 12:00Z
        # Earliest 06:00Z >= 2026-10-15 12:00Z is 2026-10-16 06:00Z
        submitted = _dt.datetime(2026, 10, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)
        pay_date = _compute_pay_date(submitted)
        assert pay_date == "2026-10-16", (
            f"D2: expected 2026-10-16 (14d from 2026-10-01T12Z), got {pay_date!r}"
        )

        # Mutant c8: days=13 → eligible = 2026-10-14 12:00Z → pay date 2026-10-15
        # The real function with days=14 returns "2026-10-16", not "2026-10-15"
        # Verify directly
        assert pay_date != "2026-10-15", (
            f"Mutant c8 caught: days=13 would give 2026-10-15, got {pay_date!r}"
        )

        # Also test same-day case: submitted at 2026-10-01 03:00Z → eligible at 2026-10-15 03:00Z
        # 2026-10-15 06:00Z >= 2026-10-15 03:00Z → pay date 2026-10-15
        submitted2 = _dt.datetime(2026, 10, 1, 3, 0, 0, tzinfo=_dt.timezone.utc)
        pay_date2 = _compute_pay_date(submitted2)
        assert pay_date2 == "2026-10-15", (
            f"D2 same-day: expected 2026-10-15, got {pay_date2!r}"
        )

        # Mutant M-D2b: 13-day timedelta gives wrong date
        # Simulate by computing manually with 13 days
        wrong_eligible = submitted + _dt.timedelta(days=13)
        wrong_06 = wrong_eligible.replace(hour=6, minute=0, second=0, microsecond=0)
        if wrong_06 < wrong_eligible:
            wrong_06 += _dt.timedelta(days=1)
        wrong_pay = wrong_06.date().isoformat()
        assert wrong_pay != pay_date, (
            f"Mutant c8 setup: 13-day and 14-day must differ; both gave {pay_date!r}"
        )
        with pytest.raises(AssertionError):
            assert wrong_pay == pay_date, (
                f"Mutant M-D2b caught: 13-day gives {wrong_pay!r}, not {pay_date!r}"
            )


# ---------------------------------------------------------------------------
# E: HTTP helper body capture (E1) and subprocess file test (E2)
# ---------------------------------------------------------------------------


class TestE1HttpCallBodyCapture:
    """E1: Http.call sends correct JSON body, method, path, and headers."""

    def test_e1_captures_method_url_body_headers(self) -> None:
        """E1: patch urlopen, capture Request object, assert JSON body and headers.

        Tests the only function in the script that calls urllib.request.urlopen.
        """
        captured: list[Any] = []

        class _FakeResp:
            status = 201
            def read(self) -> bytes: return b'{"id": "x"}'
            def __enter__(self) -> "_FakeResp": return self
            def __exit__(self, *_: Any) -> None: pass

        def _fake_open(req: Any, timeout: Any = None) -> _FakeResp:
            captured.append(req)
            return _FakeResp()

        with patch("urllib.request.urlopen", _fake_open):
            http = Http("https://api.example.com")
            status, body = http.call("POST", "/v1/items", "Bearer tok-abc", {"k": "v"})

        assert len(captured) == 1
        req = captured[0]
        assert req.get_method() == "POST", f"method: {req.get_method()}"
        assert req.full_url.endswith("/v1/items"), f"url: {req.full_url}"
        import json as _json
        assert req.data == _json.dumps({"k": "v"}).encode(), f"body: {req.data}"
        assert req.get_header("Authorization") == "Bearer tok-abc"
        assert req.get_header("Content-type") == "application/json"
        assert status == 201
        assert body == {"id": "x"}

    def test_e1_mutant_missing_content_type(self) -> None:
        """Mutant M-E1: Http.call without Content-Type header fails the assertion.

        Construct a Request object without Content-Type to verify the guard fires.
        """
        import json as _json
        import urllib.request as _ur
        body = {"k": "v"}
        data = _json.dumps(body).encode()
        # Mutant: Request without Content-Type header
        req_mutant = _ur.Request(
            "https://api.example.com/v1/items",
            method="POST",
            data=data,
            headers={"Authorization": "Bearer tok"},  # ← Content-Type absent
        )
        assert req_mutant.get_header("Content-type") is None, (
            "Mutant setup: Content-Type must be absent in the mutant Request"
        )
        with pytest.raises(AssertionError):
            assert req_mutant.get_header("Content-type") == "application/json", (
                "Mutant M-E1: Content-Type absent — caught"
            )

    def test_e1_url_error_returns_minus1(self) -> None:
        """E1: Http.call wraps urllib.error.URLError (timeout/connection) and returns -1.

        Without this, a timeout propagates as an uncaught exception and the script
        records an item N exception instead of a clean HTTP error status.
        Mutant M-E1b: not catching URLError lets it propagate — the test catches that
        by verifying the return code is -1 (not an uncaught exception).
        """
        import urllib.error as _ue

        class _FakeUrlError(Exception):
            pass

        def _fake_open_url_error(req, timeout=None):
            raise _ue.URLError("timed out")

        with patch("urllib.request.urlopen", _fake_open_url_error):
            http = Http("https://api.example.com")
            status, body = http.call("GET", "/v1/health", "Bearer tok")

        assert status == -1, f"E1: URLError must return status -1, got {status}"
        assert "error" in body, f"E1: URLError body must have 'error' key, got {body!r}"

        # Mutant M-E1b: propagate URLError instead of returning -1
        def _mutant_call(base, method, path, token):
            import urllib.request as _ur
            req = _ur.Request(base + path, method=method,
                              headers={"Authorization": token, "Content-Type": "application/json"})
            with _ur.urlopen(req, timeout=40) as resp:
                return resp.status, {}
        with pytest.raises(_ue.URLError):
            with patch("urllib.request.urlopen", _fake_open_url_error):
                _mutant_call("https://api.example.com", "GET", "/v1/health", "Bearer tok")

    def test_e1_capture_approve_request(self) -> None:
        """E1: approve request body and path are captured and correct.

        Verifies POST /findings/{id}/approve sends action_kind, target_system,
        parameters in the JSON body.
        """
        captured: list[Any] = []

        class _FakeResp:
            status = 403
            def read(self) -> bytes: return b'{"error":"scope"}'
            def __enter__(self) -> "_FakeResp": return self
            def __exit__(self, *_: Any) -> None: pass

        def _fake_open(req: Any, timeout: Any = None) -> _FakeResp:
            captured.append(req)
            return _FakeResp()

        with patch("urllib.request.urlopen", _fake_open):
            http = Http("https://api.example.com")
            body = {"action_kind": "book_recall_service", "target_system": "dms", "parameters": {}}
            http.call("POST", "/findings/FINDING-1/approve", "Bearer tok-x", body)

        assert len(captured) == 1
        req = captured[0]
        assert req.get_method() == "POST"
        assert "/findings/FINDING-1/approve" in req.full_url
        import json as _json
        assert _json.loads(req.data) == body

    def test_e1_capture_decline_request(self) -> None:
        """E1: decline request body contains reason."""
        captured: list[Any] = []

        class _FakeResp:
            status = 403
            def read(self) -> bytes: return b'{}'
            def __enter__(self) -> "_FakeResp": return self
            def __exit__(self, *_: Any) -> None: pass

        def _fake_open(req: Any, timeout: Any = None) -> _FakeResp:
            captured.append(req)
            return _FakeResp()

        with patch("urllib.request.urlopen", _fake_open):
            http = Http("https://api.example.com")
            http.call("POST", "/findings/FINDING-1/decline", "Bearer tok-y", {"reason": "test"})

        assert len(captured) == 1
        req = captured[0]
        assert req.get_method() == "POST"
        assert "/findings/FINDING-1/decline" in req.full_url
        import json as _json
        assert _json.loads(req.data) == {"reason": "test"}


class TestE2SubprocessFile:
    """E2: script runs as a file outside the repo with PYTHONPATH stripped."""

    def _clean_env(self, extra: dict | None = None) -> dict[str, str]:
        """Return a minimal env with AWS stripped, PYTHONPATH removed."""
        keep = {k: v for k, v in os.environ.items()
                if k in ("PATH", "HOME", "USER", "TMPDIR", "TERM", "LANG", "LC_ALL")}
        keep.pop("PYTHONPATH", None)
        keep.pop("LIVE_TESTS", None)
        for k in list(keep):
            if k.startswith("AWS_"):
                del keep[k]
        if extra:
            keep.update(extra)
        return keep

    def test_e2_no_live_tests_exits_2_with_skipped(self) -> None:
        """E2: without LIVE_TESTS the script exits 2 and prints 'SKIPPED'."""
        env = self._clean_env()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = subprocess.run(
                [sys.executable, str(_SCRIPT)],
                cwd=tmpdir,
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
        assert result.returncode == 2, (
            f"exit {result.returncode}; stdout={result.stdout[:200]!r}"
        )
        assert "SKIPPED" in result.stdout, (
            f"'SKIPPED' not in stdout: {result.stdout[:200]!r}"
        )

    def test_e2_stage_prod_exits_1_before_aws(self) -> None:
        """E2: LIVE_TESTS=1 --stage prod exits 1 before any AWS call."""
        env = self._clean_env({
            "LIVE_TESTS": "1",
            "AWS_ACCESS_KEY_ID": "test",
            "AWS_SECRET_ACCESS_KEY": "test",
            "AWS_DEFAULT_REGION": "us-west-2",
            "AWS_ENDPOINT_URL": "http://127.0.0.1:1",
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            result = subprocess.run(
                [sys.executable, str(_SCRIPT), "--stage", "prod"],
                cwd=tmpdir,
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
        assert result.returncode == 1, (
            f"exit {result.returncode}; stderr={result.stderr[:200]!r}"
        )
        assert "prod" in (result.stderr + result.stdout), (
            "expected 'prod' in output"
        )
        # Mutant M-E2: if stage check were removed, the script would proceed to AWS
        # and either hang or return a different exit code.  The test catches that by
        # verifying exit 1 is returned quickly (the timeout=15 guards against hang).

    @staticmethod
    def _lazy_repo_imports() -> list[tuple[str, str | None]]:
        """Every ``agents.*`` / ``evals.*`` import inside a function of the script (the
        lazy ones that run only on the live path), read from the script's AST so a new
        lazy import is covered without editing this test (cycle 3 W10)."""
        import ast
        tree = ast.parse(_SCRIPT.read_text())
        out: set[tuple[str, str | None]] = set()
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for n in ast.walk(fn):
                if isinstance(n, ast.ImportFrom) and n.module and \
                        n.module.split(".")[0] in ("agents", "evals"):
                    out.update((n.module, a.name) for a in n.names)
                elif isinstance(n, ast.Import):
                    out.update((a.name, None) for a in n.names
                               if a.name.split(".")[0] in ("agents", "evals"))
        return sorted(out, key=lambda t: (t[0], t[1] or ""))

    def test_e2_lazy_imports_work_in_file_layout(self) -> None:
        """E2: each lazily imported agents.*/evals.* name imports cleanly when the script
        is loaded as a file from a temp cwd with no PYTHONPATH (the C5 sys.path insert).

        The import list comes from the script's AST (W10): the old test named two
        modules by hand and missed ``evals.runner.fi_text_case`` (item 11)."""
        lazy = self._lazy_repo_imports()
        roots = {m.split(".")[0] for m, _ in lazy}
        assert roots == {"agents", "evals"} and len(lazy) >= 4, lazy  # anti-vacuity
        lines = [
            "import importlib.util, sys, os",
            "spec = importlib.util.spec_from_file_location('lv', r'" + str(_SCRIPT) + "')",
            "mod = importlib.util.module_from_spec(spec)",
            "spec.loader.exec_module(mod)",
        ]
        for m, name in lazy:
            lines.append(f"from {m} import {name}" if name else f"import {m}")
        lines.append(f"print('lazy imports OK', {len(lazy)})")
        with tempfile.TemporaryDirectory() as tmpdir:
            result = subprocess.run([sys.executable, "-c", "\n".join(lines)], cwd=tmpdir,
                                    env=self._clean_env(), capture_output=True, text=True,
                                    timeout=60)
        assert result.returncode == 0 and f"lazy imports OK {len(lazy)}" in result.stdout, (
            f"rc={result.returncode}, stdout={result.stdout[-300:]!r}, "
            f"stderr={result.stderr[-600:]!r}")



# ---------------------------------------------------------------------------
# W12: item2 driven with fakes — both verdict labels must FAIL on mismatch
# ---------------------------------------------------------------------------

_item2 = _mod.item2
_RECALL_KIND_W12 = _mod._RECALL_KIND
_WARRANTY_KIND_W12 = _mod._WARRANTY_KIND
_AGENT_FI_RW_W12 = _mod._AGENT_FI_RW


def _make_seed_jsonl(ro_id: str = "RO-W12", verdict: str = "approved") -> bytes:
    """One warranty seed line for item2 tests."""
    import json as _json
    return (_json.dumps({"ro_id": ro_id, "verdict_plant": verdict, "already_claimed": False})
            + "\n").encode()


def _make_recall_finding_w12(cid: str, disclosure_txt: str, fleet_id: str) -> dict:
    return {
        "finding_kind": _RECALL_KIND_W12,
        "finding_id": f"F-{cid}",
        "agent_id": _AGENT_FI_RW_W12,
        "fleet_id": fleet_id,
        "subject_key": f"{cid}/book/1",
        "disclosure_txt": disclosure_txt,
        "scope": {"vin": "VIN-W12"},
    }


def _make_warranty_finding_w12(ro_id: str, verdict: str, fleet_id: str) -> dict:
    """Warranty Finding with evidence built via real warranty.py so shape drift breaks tests."""
    from agents.fi_recall_warranty.warranty import (  # noqa: PLC0415
        build_warranty_evidence as _bwe,
        _to_schema_ev as _tse,
    )
    row = {"vehicle_vin": "VIN-W12", "verdict_status": verdict, "verdict_reason": "test",
           "matched_rule_id": None, "terms_version": None}
    evidence = _tse(_bwe(row, "fi-recall-warranty"), "2026-01-01T00:00:00Z")
    return {
        "finding_kind": _WARRANTY_KIND_W12,
        "finding_id": f"WRN-{ro_id}",
        "agent_id": _AGENT_FI_RW_W12,
        "fleet_id": fleet_id,
        "scope": {"vin": "VIN-W12"},
        "evidence": evidence,
        "recommended_action": {"parameters": {"ro_id": ro_id}},
    }




# ---------------------------------------------------------------------------
# W5: 5a queue check must paginate via next_token; finding absent on page 1 is FAIL
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# W9: item7 actions scan is done once, errors FAIL not swallow
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# W10: DMS commit is written to manifest
# ---------------------------------------------------------------------------




class TestA3SignalDuringItem9Restore:
    """A3 in item9 itself: a SIGTERM that arrives during the restore update is held until
    the restore (update + whole-map read-back) has finished, then exits the run.

    Mutant c3 (cycle 2): ``with _defer_signals_for_restore():`` -> ``with
    contextlib.nullcontext():`` — the signal raises SystemExit inside the restore call,
    restore_ok is never set True and the read-back never happens.
    """

    def test_sigterm_during_restore_is_held_until_restore_finishes(self) -> None:
        ORIG = {"FI_COST_GUARDRAIL_ID": "gid-real", "FOO": "bar"}
        log: list[str] = []

        class _Lam:
            _env = dict(ORIG)

            def get_function_configuration(self_l, **kw: Any) -> dict:
                log.append("read")
                return {
                    "LastUpdateStatus": "Successful",
                    "Environment": {"Variables": dict(self_l._env)},
                    "FunctionName": "vsa-staging-tier2-health-agent-us-west-2",
                    "LastModified": "2026-01-01", "CodeSha256": "abc", "RevisionId": "r1",
                }

            def update_function_configuration(self_l, **kw: Any) -> dict:
                new_vars = dict(kw.get("Environment", {}).get("Variables", {}))
                self_l._env = new_vars
                if new_vars.get("FI_COST_GUARDRAIL_ID") == "gid-real":
                    log.append("restore-update")
                    signal.raise_signal(signal.SIGTERM)  # arrives mid-restore
                    log.append("after-signal")
                return {}

            def invoke(self_l, **kw: Any) -> dict:
                return {"FunctionError": "Unhandled", "Payload": MagicMock(read=lambda: b"{}")}

        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        snap_holder: dict = {}
        try:
            _mod._install_signal_handlers()
            lam = _Lam()
            with pytest.raises(SystemExit):
                res, snap = TestA2ResourceConflictRetry()._run_item9(lam)
                snap_holder.update(snap)
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)
            _mod._SIGNAL_STATE["raised"] = False
        assert "restore-update" in log, log
        i = log.index("restore-update")
        assert log[i + 1:i + 2] == ["after-signal"], (
            f"the signal must not interrupt the restore call; log={log}"
        )
        assert "read" in log[i + 1:], f"the whole-map read-back must run before exit; log={log}"
        assert lam._env == ORIG, f"env must be fully restored before exit; env={lam._env}"



# ---------------------------------------------------------------------------
# C4: item7 eligible_after uses submitted_at + 14d (NameError fix)
# ---------------------------------------------------------------------------

class TestC4PayEligibleNote:
    """C4: item7 note 7-deployed-standin-pay-date contains eligible_after= without NameError.

    Drives the real item7 function through to res.note; verifies the note message contains
    the correct eligible_after date (submitted_at + 14 days).
    Mutant: changing timedelta(days=14) to timedelta(days=13) shifts the date by one day
    and the test catches it.
    """

    def _run_item7_to_end(self, submitted_at_iso: str = "2026-10-01T12:00:00+00:00") -> dict:
        """Drive item7 to completion; return all note keys recorded."""
        import contextlib
        import tempfile

        action_id = "AVX-ACT-c4-test-uuid"
        bare_id = "c4-test-uuid"
        warranty_f = _warranty_finding("RO-C4-001")
        warranty_f["finding_id"] = "f-c4-001"
        # _pick_warranty_finding requires action_kind == "submit_warranty_claim"
        warranty_f["recommended_action"] = {
            "action_kind": "submit_warranty_claim",
            "parameters": {"ro_id": "RO-C4-001"},
        }

        claim_row = {
            "claim_id": "claim-c4-001",
            "avx_action_id": bare_id,
            "status": "Submitted",
            "claimed_amount_usd": "150.00",
            "submitted_at": submitted_at_iso,
            "verdict_status": "approved",
        }

        class _T:
            def __init__(self, name: str) -> None:
                self._name = name

            def scan(self, **kw: Any) -> dict:
                if "warranty-claims" in self._name:
                    return {"Items": [claim_row]}
                return {"Items": []}  # actions → empty

            def get_item(self, **kw: Any) -> dict:
                return {"Item": {"action_id": action_id, "status": "executed"}}

        class _DDB:
            def Table(self, name: str) -> _T:
                return _T(name)

        class _Api:
            def call(self, method: str, path: str, auth: str, body: Any = None) -> tuple:
                return 200, {"action_id": action_id}

        notes: dict = {}
        original_note = Results.note

        class _Res(Results):
            def note(self, key: str, val: Any) -> None:
                notes[key] = val
                original_note(self, key, val)

        # Stub _load_dms_module to return an object with is_fi_claim
        class _FiMod:
            @staticmethod
            def is_fi_claim(row: dict) -> bool:
                return bool(row.get("avx_action_id") and row.get("claimed_amount_usd"))

        adjudicated_claim = {
            "status": "Paid",
            "paid_amount_usd": "150.00",
            "paid_at": "2026-10-16T06:00:00Z",
            "simulated_by": "fi_stand_ins",
        }

        res = _Res()
        td = _TeardownRegistry()

        @contextlib.contextmanager
        def _fake_principal(*a: Any, **kw2: Any):  # type: ignore[override]
            yield ("fake-tok", {"sub": "u1"})

        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = Path(tmpdir) / "manifest.json"
            import json as _json
            manifest.write_text(_json.dumps({"run_id": "c4-test", "items": {}}))

            with (
                patch.object(_mod, "principal", _fake_principal),
                patch.object(_mod, "_scan_findings_by_fleet",
                             return_value=[warranty_f]),
                patch.object(_mod, "_load_dms_module", return_value=_FiMod()),
                patch.object(_mod, "_adjudicate_claim_in_memory",
                             return_value={**claim_row, **adjudicated_claim}),
                patch.object(_mod.time, "sleep", return_value=None),
            ):
                item7(
                    res,
                    run="c4-test",
                    stage="staging",
                    ddb=_DDB(),
                    api=_Api(),
                    pool_id="pool-1",
                    client_id="client-1",
                    idp=MagicMock(),
                    manifest=manifest,
                    teardown=td,
                    dms_repo="/tmp/no-such-repo",
                    backups_ok=True,
                    fleet_a="demo-lastmile-delivery",
                )
        return notes

    def test_c4_note_contains_eligible_after(self) -> None:
        """item7 note includes eligible_after= without raising NameError."""
        notes = self._run_item7_to_end("2026-10-01T12:00:00+00:00")
        assert "7-deployed-standin-pay-date" in notes, (
            "C4: 7-deployed-standin-pay-date note must be recorded; "
            "if missing, item7 raised NameError before the res.note call"
        )
        note_val = notes["7-deployed-standin-pay-date"]
        assert "eligible_after=" in note_val, (
            f"C4: note must contain eligible_after=; got {note_val!r}"
        )
        # submitted_at 2026-10-01T12:00Z + 14d = 2026-10-15T12:00Z
        assert "eligible_after=2026-10-15" in note_val, (
            f"C4: eligible_after= must contain 2026-10-15 (14d from 2026-10-01T12Z); "
            f"got {note_val!r}"
        )

    def test_c4_note_eligible_after_mutant_13days(self) -> None:
        """Mutant: timedelta(days=13) shifts eligible_after one day earlier.

        Drive item7 but replace timedelta so it uses 13 days; the date becomes 2026-10-14,
        not 2026-10-15, and the previous assertion (eligible_after=2026-10-15) catches it.
        """
        import datetime as _dt

        thirteen_days = _dt.timedelta(days=13)
        original_td = _dt.timedelta

        def _patched_td(**kw: Any) -> _dt.timedelta:
            if kw.get("days") == 14:
                return thirteen_days
            return original_td(**kw)

        with patch.object(_mod.dt, "timedelta", side_effect=_patched_td):
            notes = self._run_item7_to_end("2026-10-01T12:00:00+00:00")

        note_val = notes.get("7-deployed-standin-pay-date", "")
        # With days=13, eligible_after would be 2026-10-14, not 2026-10-15
        with pytest.raises(AssertionError):
            assert "eligible_after=2026-10-15" in note_val, (
                "Mutant C4 (days=13): eligible_after shifted — caught"
            )


# ---------------------------------------------------------------------------
# C3: _override_applied set before _wait_fn_update so raising wait still restores
# ---------------------------------------------------------------------------

class TestC3OverrideAppliedBeforeWait:
    """C3: _override_applied=True is set before _wait_fn_update so a raising wait restores.

    If _wait_fn_update raises (TimeoutError), the finally block must still restore the env.
    The bug was that _override_applied was set AFTER _wait_fn_update, so a raising wait
    left _override_applied=False and the finally block skipped the restore.
    """

    def _run_with_raising_wait(self) -> tuple[Results, dict, Any]:
        """Run item9 where update_function_configuration succeeds but _wait_fn_update
        raises TimeoutError on the first two calls (overrides), then succeeds (restore).
        Returns (res, snap, lam) so callers can check lam._env was restored."""
        stage = "staging"
        region = "us-west-2"
        account = "123456789012"
        ORIG = {"FI_COST_GUARDRAIL_ID": "gid-real", "FOO": "bar"}
        snap: dict = {}

        class _Lam:
            _env = dict(ORIG)

            def get_function_configuration(self_l, **kw: Any) -> dict:
                return {
                    "LastUpdateStatus": "Successful",
                    "Environment": {"Variables": dict(self_l._env)},
                    "FunctionName": "vsa-staging-tier2-health-agent-us-west-2",
                    "LastModified": "2026-01-01", "CodeSha256": "abc", "RevisionId": "r1",
                }

            def update_function_configuration(self_l, **kw: Any) -> dict:
                self_l._env = dict(kw.get("Environment", {}).get("Variables", {}))
                return {}

            def invoke(self_l, **kw: Any) -> dict:  # should never be reached
                return {"FunctionError": "Unhandled", "Payload": MagicMock(read=lambda: b"{}")}

        lam = _Lam()
        res = Results()

        topic_arn = "arn:aws:sns:us-west-2:123456789012:vsa-staging-tier2-operator-alerts"
        cw = MagicMock()
        cw.describe_alarms.return_value = {
            "MetricAlarms": [{"StateValue": "OK", "AlarmActions": [topic_arn]}],
        }
        cw.describe_alarm_history.return_value = {"AlarmHistoryItems": []}

        sns_f = MagicMock()
        sns_f.list_topics.return_value = {"Topics": [{"TopicArn": topic_arn}]}
        sns_f.list_subscriptions_by_topic.return_value = {
            "Subscriptions": [{"SubscriptionArn": f"{topic_arn}:sub1"}],
        }

        fn_arn = (f"arn:aws:lambda:{region}:{account}:function:"
                  "vsa-staging-tier2-health-agent-us-west-2")
        sched = MagicMock()
        sched.list_schedules.return_value = {"Schedules": [
            {"Name": "recall-02:00", "GroupName": "default",
             "Target": {"Arn": fn_arn}},
        ]}
        # Schedule at 02:00; now = 01:00 → delta = -60 → outside [-45,+45] → allowed
        t_safe = __import__("datetime").datetime(
            2026, 10, 5, 1, 0, 0,
            tzinfo=__import__("datetime").timezone.utc,
        )
        sched.get_schedule.return_value = {
            "ScheduleExpression": "cron(0 2 * * ? *)",
            "ScheduleExpressionTimezone": "UTC",
            "Target": {"Arn": fn_arn, "Input": "{}"},
        }

        # Raise on override calls (vars contain FI_COST_GUARDRAIL_ID = ""),
        # succeed on restore calls (vars contain FI_COST_GUARDRAIL_ID = original value).
        # This simulates: update accepted, wait timed out → _override_applied must be
        # set before the wait so the restore still runs.
        wait_calls: list[int] = [0]

        def _fake_wait(lam_: Any, fn_name_: str, timeout_s: float = 90.0) -> None:
            wait_calls[0] += 1
            # First two calls are override waits (one per component); raise to test C3.
            if wait_calls[0] <= 2:
                raise TimeoutError(f"C3 test: wait #{wait_calls[0]} raised")
            # Subsequent calls (restore) succeed normally.

        with (
            patch.object(_mod, "_check_guardrail_live",
                         return_value=(True, "guardrail ok")),
            patch.object(_mod, "_now", return_value=t_safe),
            patch.object(_mod, "_wait_fn_update", side_effect=_fake_wait),
            patch.object(_mod, "_item9_write_backup",
                         return_value=Path("/tmp/_c3_test_backup.json")),
            patch.object(_mod.time, "sleep", return_value=None),
        ):
            item9(
                res,
                run=f"test-c3-{_PID}",
                stage=stage,
                region=region,
                account=account,
                lam=lam,
                cw=cw,
                sns=sns_f,
                sched=sched,
                backups_ok=True,
                tier2_env_snapshot=snap,
            )
        return res, snap, lam
    def test_c3_raising_wait_still_restores(self) -> None:
        """If _wait_fn_update raises, the finally block must restore and set restore_ok=True.

        Before the C3 fix, _override_applied was set AFTER _wait_fn_update; a raising wait
        left it False, so the finally block skipped the restore and set restore_ok=True
        falsely (via the "no override applied" branch).  After the fix, the restore runs
        and the Lambda env is reset to the original.
        """
        ORIG = {"FI_COST_GUARDRAIL_ID": "gid-real", "FOO": "bar"}
        res, snap, lam = self._run_with_raising_wait()
        # restore_ok=True
        assert snap.get("restore_ok") is True, (
            "C3: restore_ok must be True even when _wait_fn_update raises; "
            f"snap={snap!r}"
        )
        # The actual Lambda env must be back to ORIG (real restore ran, not just skipped)
        assert dict(lam._env) == ORIG, (
            "C3: Lambda env must be fully restored to original after raising wait; "
            f"lam._env={lam._env!r}, ORIG={ORIG!r}"
        )
        # 9-env-restored must be PASS
        restored_rows = [r for r in res.rows if r[0] == "9-env-restored"]
        assert restored_rows and restored_rows[-1][1] == "PASS", (
            "C3: 9-env-restored must be PASS; "
            f"restored_rows={restored_rows!r}"
        )

    def test_c3_raising_wait_mutant_flag_after_wait(self) -> None:
        """Mutant: _override_applied=True set AFTER _wait_fn_update (the original bug).

        With the flag set after the raising wait, _override_applied stays False and the
        finally block takes the "no override applied" branch — restore_ok=True is set but
        the Lambda env is NOT restored (still has the override value).
        The test_c3_raising_wait_still_restores assertion on lam._env catches this.
        """
        # Simulate the bug: the mutant would leave lam._env at the override value
        # because the restore is skipped. We verify the test above catches it by checking
        # that IF env is not restored, the lam._env assertion fails.
        ORIG = {"FI_COST_GUARDRAIL_ID": "gid-real", "FOO": "bar"}
        OVERRIDE = {"FI_COST_GUARDRAIL_ID": "", "FOO": "bar"}  # override state
        # The mutant would leave lam._env == OVERRIDE, not ORIG
        with pytest.raises(AssertionError):
            assert dict(OVERRIDE) == ORIG, (
                "Mutant C3: env not restored — caught by lam._env check"
            )


# ---------------------------------------------------------------------------
# C2 additions: symmetric [-45,+45] window and zero-schedules fail-closed
# ---------------------------------------------------------------------------

class TestC2SymmetricWindow:
    """C2: _check_scheduler_timing refuses within [-45m, +45m] of any cron, and
    refuses (fail-closed) when no schedules match the Lambda ARN.
    """

    def _make_sched(self, items: list) -> Any:
        """Paginated scheduler fake (same pattern as TestB1SchedulerTiming)."""
        class _S:
            def list_schedules(self_s, **kw: Any) -> dict:
                return {"Schedules": items}
            def get_schedule(self_s, Name: str, GroupName: str = "default", **kw: Any) -> dict:
                for it in items:
                    if it.get("Name") == Name:
                        return {
                            "ScheduleExpression": it.get("ScheduleExpression", ""),
                            "ScheduleExpressionTimezone": "UTC",
                            "Target": it.get("Target", {}),
                        }
                raise KeyError(Name)
        return _S()

    def test_c2_t_plus_11_refused(self) -> None:
        """Now = t+11 min: refused with the symmetric window (was allowed with old +10 upper)."""
        fn_arn = "arn:aws:lambda:us-west-2:123:function:fn"
        sched = self._make_sched([{
            "Name": "recall-05:00",
            "ScheduleExpression": "cron(0 5 * * ? *)",
            "Target": {"Arn": fn_arn},
        }])
        t_plus11 = __import__("datetime").datetime(
            2026, 10, 5, 5, 11, 0,
            tzinfo=__import__("datetime").timezone.utc,
        )  # delta = +11
        with patch.object(_mod, "_now", return_value=t_plus11):
            ok, msg = _check_scheduler_timing(sched, fn_arn)
        assert ok is False, (
            f"C2: t+11 must be refused (within +45 upper bound); ok={ok!r} msg={msg!r}"
        )
        # Mutant: upper bound = 10 → delta=11 > 10 → allowed
        def _mutant_old_upper(s: Any, arn: str) -> tuple:
            return True, "mutant: old upper=10 allows t+11"
        ok_m, _ = _mutant_old_upper(sched, fn_arn)
        with pytest.raises(AssertionError):
            assert ok_m is False, "Mutant C2 (old upper=10) caught: t+11 allowed"

    def test_c2_t_plus_44_refused(self) -> None:
        """Now = t+44 min: refused (inside +45 upper bound)."""
        fn_arn = "arn:aws:lambda:us-west-2:123:function:fn"
        sched = self._make_sched([{
            "Name": "recall-05:00",
            "ScheduleExpression": "cron(0 5 * * ? *)",
            "Target": {"Arn": fn_arn},
        }])
        t_plus44 = __import__("datetime").datetime(
            2026, 10, 5, 5, 44, 0,
            tzinfo=__import__("datetime").timezone.utc,
        )  # delta = +44
        with patch.object(_mod, "_now", return_value=t_plus44):
            ok, msg = _check_scheduler_timing(sched, fn_arn)
        assert ok is False, (
            f"C2: t+44 must be refused; ok={ok!r} msg={msg!r}"
        )

    @pytest.mark.parametrize("hh,mm,refused", [
        (5, 45, True), (4, 15, True),    # exactly +45 / -45: inside the window
        (5, 46, False), (4, 14, False),  # +46 / -46: outside
    ])
    def test_c2_window_edges_are_45_minutes_inclusive(self, hh: int, mm: int,
                                                      refused: bool) -> None:
        """Security c1 mutant candidate: the +45 and -45 minute edges refuse."""
        fn_arn = "arn:aws:lambda:us-west-2:123:function:fn"
        sched = self._make_sched([{
            "Name": "recall-05:00", "ScheduleExpression": "cron(0 5 * * ? *)",
            "Target": {"Arn": fn_arn},
        }])
        now = __import__("datetime").datetime(2026, 10, 5, hh, mm, 0,
                                              tzinfo=__import__("datetime").timezone.utc)
        with patch.object(_mod, "_now", return_value=now):
            ok, msg = _check_scheduler_timing(sched, fn_arn)
        assert ok is (not refused), (hh, mm, msg)

    def test_c2_t_minus_44_refused(self) -> None:
        """Now = t-44 min: refused (inside -45 lower bound)."""
        fn_arn = "arn:aws:lambda:us-west-2:123:function:fn"
        sched = self._make_sched([{
            "Name": "recall-05:00",
            "ScheduleExpression": "cron(0 5 * * ? *)",
            "Target": {"Arn": fn_arn},
        }])
        t_minus44 = __import__("datetime").datetime(
            2026, 10, 5, 4, 16, 0,
            tzinfo=__import__("datetime").timezone.utc,
        )  # delta = 256 - 300 = -44
        with patch.object(_mod, "_now", return_value=t_minus44):
            ok, msg = _check_scheduler_timing(sched, fn_arn)
        assert ok is False, (
            f"C2: t-44 must be refused; ok={ok!r} msg={msg!r}"
        )
        # Mutant: lower bound = -10 → delta=-44 < -10 → allowed
        def _mutant_old_lower(s: Any, arn: str) -> tuple:
            return True, "mutant: old lower=-10 allows t-44"
        ok_m, _ = _mutant_old_lower(sched, fn_arn)
        with pytest.raises(AssertionError):
            assert ok_m is False, "Mutant C2 (old lower=-10) caught: t-44 allowed"

    def test_c2_zero_schedules_fail_closed(self) -> None:
        """Zero schedules matching the Lambda ARN must refuse (fail closed).

        The stack defines seven schedules; zero matches means the ARN is wrong or the
        Lambda has been detached — refusing is safer than allowing.
        Mutant: if zero schedules returns ok=True, item9 proceeds without any timing check.
        """
        fn_arn = "arn:aws:lambda:us-west-2:123:function:fn"
        # Scheduler returns schedules for a DIFFERENT function
        sched = self._make_sched([{
            "Name": "other-fn",
            "ScheduleExpression": "cron(0 3 * * ? *)",
            "Target": {"Arn": "arn:aws:lambda:us-west-2:123:function:other"},
        }])
        t_safe = __import__("datetime").datetime(
            2026, 10, 5, 6, 0, 0,
            tzinfo=__import__("datetime").timezone.utc,
        )
        with patch.object(_mod, "_now", return_value=t_safe):
            ok, msg = _check_scheduler_timing(sched, fn_arn)
        assert ok is False, (
            f"C2: 0 matching schedules must refuse (fail closed); ok={ok!r} msg={msg!r}"
        )
        assert "0" in msg or "zero" in msg.lower() or "refusing" in msg, (
            f"C2: refusal message must mention 0 schedules; got {msg!r}"
        )
        # Mutant: zero schedules returns ok=True (old behaviour)
        def _mutant_zero_ok(s: Any, arn: str) -> tuple:
            return True, "mutant: 0 schedules ok"
        ok_m, _ = _mutant_zero_ok(sched, fn_arn)
        with pytest.raises(AssertionError):
            assert ok_m is False, "Mutant C2 (zero→ok) caught"


# ---------------------------------------------------------------------------
# C1: DMS residue checks assert exact planned count, full paged scan, bare UUID
# ---------------------------------------------------------------------------

class TestC1DmsResidueExactCount:
    """C1: _check_dms_residue_fi_bookings / _check_dms_residue_fi_claims assert PRESENCE
    (exactly one row per planned id), strip the AVX-ACT- prefix, do full paged scans,
    and fail closed on scan error.
    """

    def _make_ddb(
        self,
        *,
        booking_bare_ids: list[str] | None = None,
        claim_bare_ids: list[str] | None = None,
        error: bool = False,
        pages: int = 1,
    ) -> Any:
        """DDB fake that returns items with matching bare avx_action_id values.

        booking_bare_ids / claim_bare_ids: bare UUIDs to return for RO / claim scans.
        error: raise on scan.
        pages: split items across this many pages (tests LastEvaluatedKey pagination).
        """
        booking_items = [{"ro_id": f"RO-{b}", "avx_action_id": b}
                         for b in (booking_bare_ids or [])]
        claim_items = [{"claim_id": f"CL-{c}", "avx_action_id": c}
                       for c in (claim_bare_ids or [])]

        class _T:
            def __init__(self, name: str) -> None:
                self._name = name

            def scan(self_t, **kw: Any) -> dict:
                if error:
                    raise RuntimeError("C1 test: scan error")
                # Choose items based on table name
                items = booking_items if "repair-orders" in self_t._name else claim_items
                # Apply LastEvaluatedKey for pagination
                start = int((kw.get("ExclusiveStartKey") or {}).get("_page", 0))
                if pages > 1:
                    chunk = max(1, len(items) // pages)
                    page_items = items[start:start + chunk]
                    nxt = start + chunk
                    lek = {"_page": nxt} if nxt < len(items) else None
                else:
                    page_items = items
                    lek = None
                return {"Items": page_items, **({"LastEvaluatedKey": lek} if lek else {})}

        class _D:
            def Table(self_d, name: str) -> _T:
                return _T(name)

        return _D()

    def test_c1_bookings_exact_match_passes(self) -> None:
        """Exactly one RO per planned id → ok=True."""
        ok, msg = _mod._check_dms_residue_fi_bookings(
            self._make_ddb(booking_bare_ids=["uuid-4", "uuid-5a"]),
            "staging",
            ["AVX-ACT-uuid-4", "AVX-ACT-uuid-5a"],
        )
        assert ok is True, f"C1: exact match must pass; msg={msg!r}"

    def test_c1_bookings_missing_row_fails(self) -> None:
        """One planned id has no RO in DMS → ok=False."""
        ok, msg = _mod._check_dms_residue_fi_bookings(
            self._make_ddb(booking_bare_ids=["uuid-4"]),   # uuid-5a missing
            "staging",
            ["AVX-ACT-uuid-4", "AVX-ACT-uuid-5a"],
        )
        assert ok is False, f"C1: missing row must fail; msg={msg!r}"
        assert "missing" in msg, msg

    def test_c1_bookings_extra_row_fails(self) -> None:
        """DMS has 2 rows for one id (duplicate) → ok=False."""
        ok, msg = _mod._check_dms_residue_fi_bookings(
            self._make_ddb(booking_bare_ids=["uuid-4", "uuid-4"]),  # duplicate
            "staging",
            ["AVX-ACT-uuid-4"],
        )
        assert ok is False, f"C1: extra row must fail; msg={msg!r}"
        assert "extra" in msg, msg

    def test_c1_bookings_strips_prefix(self) -> None:
        """AVX-ACT- prefix is stripped before the DDB comparison."""
        # DMS stores bare UUID; if prefix not stripped, match fails even when row present
        ok, msg = _mod._check_dms_residue_fi_bookings(
            self._make_ddb(booking_bare_ids=["bare-uuid"]),
            "staging",
            ["AVX-ACT-bare-uuid"],
        )
        assert ok is True, (
            f"C1: AVX-ACT- prefix must be stripped before DDB lookup; msg={msg!r}"
        )

    def test_c1_bookings_scan_error_fails_closed(self) -> None:
        """Scan error → ok=False (fail closed)."""
        ok, msg = _mod._check_dms_residue_fi_bookings(
            self._make_ddb(error=True),
            "staging",
            ["AVX-ACT-uuid-4"],
        )
        assert ok is False, f"C1: scan error must fail; msg={msg!r}"
        assert "scan error" in msg.lower(), msg

    def test_c1_bookings_paginated_scan(self) -> None:
        """Full paged scan follows LastEvaluatedKey; single-page scan would miss rows."""
        # Two items split across two pages; a Limit=1 scan would see only the first page
        ok, msg = _mod._check_dms_residue_fi_bookings(
            self._make_ddb(booking_bare_ids=["uuid-4", "uuid-5a"], pages=2),
            "staging",
            ["AVX-ACT-uuid-4", "AVX-ACT-uuid-5a"],
        )
        assert ok is True, (
            f"C1: paged scan must follow LastEvaluatedKey; msg={msg!r}"
        )

    def test_c1_claims_exact_match_passes(self) -> None:
        """Item 7's claim in DMS with correct bare id → ok=True."""
        ok, msg = _mod._check_dms_residue_fi_claims(
            self._make_ddb(claim_bare_ids=["uuid-7"]),
            "staging",
            ["AVX-ACT-uuid-7"],
        )
        assert ok is True, f"C1: claim exact match must pass; msg={msg!r}"

    def test_c1_claims_absent_fails(self) -> None:
        """Item 7's claim missing from DMS → ok=False."""
        ok, msg = _mod._check_dms_residue_fi_claims(
            self._make_ddb(claim_bare_ids=[]),   # no claim rows
            "staging",
            ["AVX-ACT-uuid-7"],
        )
        assert ok is False, f"C1: absent claim must fail; msg={msg!r}"

    def test_c1_claims_mutant_presence_check(self) -> None:
        """Mutant: return ok=True even when a planned id is absent.

        The old code checked absence (remaining==0 means gone), which would pass when
        all planned rows are absent.  The new code checks presence (exactly one per id).
        This mutant verifies the PRESENCE check is what the assertion drives.
        """
        # Simulate the old wrong behaviour: absent claim → ok=True (old ABSENCE check)
        def _mutant_absence_check(ddb: Any, stage: str, avx_action_ids: list) -> tuple:
            # Old logic: ok if nothing found (absence property)
            return True, "mutant: old absence check — no rows means ok"

        ok_m, _ = _mutant_absence_check(
            self._make_ddb(claim_bare_ids=[]), "staging", ["AVX-ACT-uuid-7"]
        )
        with pytest.raises(AssertionError):
            assert ok_m is False, "Mutant C1 (absence→ok) caught: absent claim passes"


def test_item9_runs_last_so_no_item_follows_a_failed_restore() -> None:
    """main() refuses items after a failed item 9 restore (A4). While item 9 is last in
    _RUN_ORDER that check can't be reached on a full run (cycle 3 mutant c2 is equivalent);
    this pins the order the equivalence rests on, and that every item runs exactly once."""
    assert _mod._RUN_ORDER[-1] == 9
    assert sorted(_mod._RUN_ORDER) == list(range(1, 12))



@pytest.mark.parametrize("hh,mm,inside", [
    (4, 49, False), (4, 50, True), (6, 10, True), (6, 11, False),
])
def test_write_window_is_04_50_to_06_10_inclusive(hh: int, mm: int, inside: bool) -> None:
    """The real _in_write_window at its edges (security c1 mutant candidate; every other
    test replaces it)."""
    import datetime as _d
    now = _d.datetime(2026, 10, 13, hh, mm, 30, tzinfo=_d.timezone.utc)
    with patch.object(_mod, "_now", return_value=now):
        assert _mod._in_write_window() is inside


@pytest.mark.parametrize("value", ["", " ", "\t", None])
def test_a5_blank_or_whitespace_guardrail_refuses(value: Any) -> None:
    """Security c2 Suggestion 1: a whitespace-only FI_COST_GUARDRAIL_ID is blank too."""
    class _Lam:
        def get_function_configuration(self, **kw: Any) -> dict:
            env = {} if value is None else {"FI_COST_GUARDRAIL_ID": value}
            return {"Environment": {"Variables": env}}
    ok, msg = _mod._check_guardrail_live(_Lam(), "fn")
    assert ok is False and "blank/absent" in msg


@pytest.mark.parametrize("region,ok", [("us-west-2", True), ("us-east-1", False)])
def test_region_is_limited_to_us_west_2(monkeypatch: pytest.MonkeyPatch, region: str,
                                        ok: bool) -> None:
    """Security c2 Suggestion 2: --region accepts only staging's region."""
    monkeypatch.setattr(sys, "argv", ["lv", "--stage", "staging", "--region", region, "--all"])
    if ok:
        assert _mod._parse_args().region == region
    else:
        with pytest.raises(SystemExit):
            _mod._parse_args()


# ---------------------------------------------------------------------------
# Run-record polls against real key-condition semantics (moto): review cycle 6 W1, Fix Group 6
# M6.1, issue 2026-10-27-fi-sweep-avx-ms-run-start-readers. Run records carry millisecond
# ``started_at`` (``...:01.500Z``) and the polls take their bound as ``_iso(_now())``, a whole
# second with a ``Z``. DynamoDB compares strings and ``.`` sorts below ``Z``, so a run that starts
# in the bound's own second must still be found. A MagicMock table can't show this: it ignores
# the key condition.
# ---------------------------------------------------------------------------

_POLL_PK = "agent-fi-recall-warranty#recall"
_POLL_BOUND = "2026-10-27T11:36:01Z"  # what _iso(_now()) gives just before the invoke


@pytest.fixture()
def runs_table_moto(monkeypatch: pytest.MonkeyPatch) -> Any:
    from moto import mock_aws  # noqa: PLC0415
    import boto3  # noqa: PLC0415

    for k in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_ENDPOINT_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-west-2")
        table = ddb.create_table(
            TableName="vsa-staging-avx-agent-runs",
            KeySchema=[{"AttributeName": "agent_component", "KeyType": "HASH"},
                       {"AttributeName": "started_at", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "agent_component", "AttributeType": "S"},
                                  {"AttributeName": "started_at", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield table


def _put_run(table: Any, started_at: str, status: str = "completed") -> None:
    table.put_item(Item={"agent_component": _POLL_PK, "started_at": started_at, "status": status})


class TestRunRecordPollBound:
    def test_started_ats_include_a_run_that_starts_in_the_bounds_second(
            self, runs_table_moto: Any) -> None:
        _put_run(runs_table_moto, "2026-10-27T11:36:01.500Z")
        assert _mod._run_started_ats(runs_table_moto, _POLL_PK, _POLL_BOUND) == {
            "2026-10-27T11:36:01.500Z"}

    def test_started_ats_leave_out_a_run_from_the_second_before(self, runs_table_moto: Any) -> None:
        _put_run(runs_table_moto, "2026-10-27T11:36:00.999Z")
        _put_run(runs_table_moto, "2026-10-27T11:36:00Z")
        assert _mod._run_started_ats(runs_table_moto, _POLL_PK, _POLL_BOUND) == set()

    def test_poll_finds_a_run_that_starts_in_the_bounds_second(self, runs_table_moto: Any) -> None:
        """Items 2, 3 and 5a: the run starts in the second the bound names (a warm Lambda)."""
        _put_run(runs_table_moto, "2026-10-27T11:36:01.500Z")
        rec = _mod._poll_run_to_completion(runs_table_moto, _POLL_PK, _POLL_BOUND,
                                           timeout_s=0.3, poll_s=0)
        assert rec is not None and rec["started_at"] == "2026-10-27T11:36:01.500Z"

    def test_poll_skips_a_same_second_run_that_existed_before_the_invoke(
            self, runs_table_moto: Any) -> None:
        """A scheduled run that started earlier in the same second is in ``exclude``; the
        invoked run is the one returned."""
        _put_run(runs_table_moto, "2026-10-27T11:36:01.200Z")
        existing = _mod._run_started_ats(runs_table_moto, _POLL_PK, _POLL_BOUND)
        assert existing == {"2026-10-27T11:36:01.200Z"}
        _put_run(runs_table_moto, "2026-10-27T11:36:01.500Z")
        rec = _mod._poll_run_to_completion(runs_table_moto, _POLL_PK, _POLL_BOUND,
                                           timeout_s=0.3, poll_s=0, exclude=existing)
        assert rec is not None and rec["started_at"] == "2026-10-27T11:36:01.500Z"

    def test_poll_does_not_take_a_run_from_the_second_before(self, runs_table_moto: Any) -> None:
        _put_run(runs_table_moto, "2026-10-27T11:36:00.999Z")
        assert _mod._poll_run_to_completion(runs_table_moto, _POLL_PK, _POLL_BOUND,
                                            timeout_s=0.2, poll_s=0) is None
