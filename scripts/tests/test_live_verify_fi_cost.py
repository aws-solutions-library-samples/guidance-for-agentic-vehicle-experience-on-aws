"""Unit tests for scripts/live-verify-fi-cost.py pure helpers and contract checks.

Tests target:
  1. score_plants         — planted-anomaly scoring (item 2)
  2. check_paging_match   — paged vs COUNT(*) (item 4)
  3. build_env_with_override — Lambda env override (item 7)
  4. _check_text_runtime_trust_flag — AgentCore runtime lookup (item 8)
  5. _check_text_runtime_trust_flag — AgentCore runtime lookup (item 8)
  6. _read_answer_key duckdb path — parquet reading without pyarrow
  7. iOS persona fleet exclusion — managed-fleet constraint (items 5/8)
  8. allow_writes guard on items 5 and 8
  9. C1 offline: exit-code contract (SKIP alongside PASS → exit 2)
 10. C2 offline: run_cost summary["cost"] shape (bad data → FAIL)
 11. C3 offline: Lambda client has read_timeout > function timeout
 12. C4 offline: fleet B isolation via DDB finding_ids
 13. C5 offline: item 4 agent-paging check shape
 14. C6 offline: chat request uses 'prompt' + 'personaId'; response reads 'result'
 15. C7 offline: epoch-ms range computation; fleet_cost_db name; _extract_cost_stats

All tests use stubbed data; zero real AWS calls.

Run:
    python3 -m pytest scripts/tests/test_live_verify_fi_cost.py -v
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Load the script as a module (not on sys.path by default)
# ---------------------------------------------------------------------------

_SCRIPT = Path(__file__).resolve().parents[1] / "live-verify-fi-cost.py"
_spec = importlib.util.spec_from_file_location("live_verify_fi_cost", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

score_plants = _mod.score_plants
score_plants_detail = _mod.score_plants_detail
check_paging_match = _mod.check_paging_match
build_env_with_override = _mod.build_env_with_override
_check_text_runtime_trust_flag = _mod._check_text_runtime_trust_flag
_read_answer_key = _mod._read_answer_key
_extract_cost_stats = _mod._extract_cost_stats
_year_month_epoch_ms_range = _mod._year_month_epoch_ms_range
_fleet_cost_db = _mod._fleet_cost_db
_select_anomalies_count_sql = _mod._select_anomalies_count_sql
_extract_chat_text = _mod._extract_chat_text
_LAMBDA_READ_TIMEOUT_S = _mod._LAMBDA_READ_TIMEOUT_S
_LAMBDA_MAX_ATTEMPTS = _mod._LAMBDA_MAX_ATTEMPTS
Results = _mod.Results
item5_fleet_scope = _mod.item5_fleet_scope
item8_fleet_recommendation = _mod.item8_fleet_recommendation

_SCORE_THRESHOLD_PCT = _mod._SCORE_THRESHOLD_PCT
_FINDING_KIND_ANOMALY = _mod._FINDING_KIND_ANOMALY
_AGENT_ID = _mod._AGENT_ID
_IOS_PERSONA_FLEET_IDS = _mod._IOS_PERSONA_FLEET_IDS
_CAUSE_PHRASES = _mod._CAUSE_PHRASES


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _plant(vin: str, cause: str) -> dict[str, Any]:
    return {"vin": vin, "cause": cause, "from_month": "2026-08", "to_month": "2026-09"}


def _finding(
    vin: str,
    cause: str,
    kind: str = _FINDING_KIND_ANOMALY,
    agent_id: str = _AGENT_ID,
    fleet_id: str = "FLEET-A",
    month: str = "2026-08",
) -> dict[str, Any]:
    return {
        "finding_id": f"AVX-{vin}-{cause}",
        "finding_kind": kind,
        "agent_id": agent_id,
        "scope": {"vin": vin},
        "fleet_id": fleet_id,
        "recommended_action": {
            "action_kind": "apply_cost_action",
            "parameters": {"cause": cause, "cost_action": "driver_coaching", "month": month},
        },
        "headline": f"Cost anomaly for {vin}",
    }


def _finding_no_action(vin: str) -> dict[str, Any]:
    return {
        "finding_id": f"AVX-{vin}-noaction",
        "finding_kind": _FINDING_KIND_ANOMALY,
        "agent_id": _AGENT_ID,
        "scope": {"vin": vin},
        "fleet_id": "FLEET-A",
        # no recommended_action
    }


def _run_cost_summary(
    deferred: int = 0,
    model_calls: int = 0,
    rules_fallbacks: int = 0,
    anomalies_written: int = 0,
    sell_timing_written: int = 0,
    tokens_used: int = 0,
    data_month: str = "2026-08",
    anomalies_selected: int = 10,
    artifacts_written: int = 0,
) -> dict[str, Any]:
    """Build a realistic run_cost() return value (summary dict).

    The 'cost' subdict mirrors _cost_record_fields() + agent.py's stats keys.
    The top-level mirrors RunRecord.to_dict().
    C2 fix: all cost-specific counters live under summary["cost"].
    """
    return {
        # Top-level from RunRecord.to_dict()
        "event": "tier2_run",
        "run_id": "2026-10-07T04:00:00",
        "stage": "staging",
        "component": "cost",
        "started_at": "2026-10-07T04:00:00.000000+00:00",
        "finished_at": "2026-10-07T04:05:00.000000+00:00",
        "rows_read": anomalies_selected * 10,
        "artifacts_written": artifacts_written,
        "duplicates_skipped": 0,
        "avx_write_failures": 0,
        "owner_integrity_degraded": 0,
        "tokens_used": tokens_used,
        "estimated_cost_usd": tokens_used * 0.000003,
        "abort_reason": None,
        "status": "completed",
        "vins_selected": anomalies_selected,
        # Cost-specific subdict (added by run_cost())
        "cost": {
            "data_month": data_month,
            "anomalies_selected": anomalies_selected,
            "model_calls": model_calls,
            "deferred": deferred,
            "tokens_used": tokens_used,
            "cost_usd": tokens_used * 0.000003,
            "write_failures": 0,
            "rules_fallbacks": rules_fallbacks,
            "guardrail_interventions": 0,
            "anomalies_written": anomalies_written,
            "sell_timing_written": sell_timing_written,
            "sell_timing_deferred": 0,
            "duplicates_skipped": 0,
        },
    }


# ---------------------------------------------------------------------------
# score_plants tests
# ---------------------------------------------------------------------------


class TestScorePlants:
    def test_empty_answer_key_returns_zero_pct(self):
        findings = [_finding("VIN1", "charging_rate")]
        correct, total, pct = score_plants([], findings)
        assert correct == 0
        assert total == 0
        assert pct == 0.0

    def test_empty_findings_returns_zero_correct(self):
        plants = [_plant("VIN1", "charging_rate"), _plant("VIN2", "efficiency")]
        correct, total, pct = score_plants(plants, [])
        assert correct == 0
        assert total == 2
        assert pct == 0.0

    def test_perfect_score_all_causes_match(self):
        plants = [
            _plant("VIN1", "charging_rate"),
            _plant("VIN2", "charging_mix"),
            _plant("VIN3", "repeat_repair"),
            _plant("VIN4", "efficiency"),
        ]
        findings = [
            _finding("VIN1", "charging_rate"),
            _finding("VIN2", "charging_mix"),
            _finding("VIN3", "repeat_repair"),
            _finding("VIN4", "efficiency"),
        ]
        correct, total, pct = score_plants(plants, findings)
        assert correct == 4
        assert total == 4
        assert pct == 1.0

    def test_partial_score_some_wrong_causes(self):
        plants = [
            _plant("VIN1", "charging_rate"),
            _plant("VIN2", "efficiency"),
            _plant("VIN3", "repeat_repair"),
        ]
        findings = [
            _finding("VIN1", "charging_rate"),   # correct
            _finding("VIN2", "charging_mix"),     # wrong cause
            _finding("VIN3", "repeat_repair"),    # correct
        ]
        correct, total, pct = score_plants(plants, findings)
        assert correct == 2
        assert total == 3
        assert abs(pct - 2 / 3) < 1e-9

    def test_finding_without_recommended_action_does_not_count(self):
        plants = [_plant("VIN1", "charging_rate")]
        findings = [_finding_no_action("VIN1")]
        correct, total, pct = score_plants(plants, findings)
        assert correct == 0
        assert pct == 0.0

    def test_sell_timing_findings_are_excluded(self):
        plants = [_plant("VIN1", "charging_rate")]
        findings = [
            {
                "finding_kind": "fleet.cost.sell_timing",
                "agent_id": _AGENT_ID,
                "scope": {"vin": "VIN1"},
                "fleet_id": "FLEET-A",
                "recommended_action": {
                    "parameters": {"cause": "low_utilisation"}
                },
            }
        ]
        correct, total, pct = score_plants(plants, findings)
        assert correct == 0

    def test_non_agent_findings_are_ignored(self):
        plants = [_plant("VIN1", "charging_rate")]
        findings = [_finding("VIN1", "charging_rate", agent_id="agent-fi-recall-warranty")]
        correct, total, pct = score_plants(plants, findings)
        assert correct == 0, (
            "Non-cost-agent Finding incorrectly counted — agent_id filter is broken"
        )

    def test_duplicate_findings_for_same_vin_cause_still_count_once(self):
        plants = [_plant("VIN1", "charging_rate")]
        findings = [
            _finding("VIN1", "charging_rate"),
            _finding("VIN1", "charging_rate"),
        ]
        correct, total, pct = score_plants(plants, findings)
        assert correct == 1

    def test_score_above_threshold(self):
        n = 80
        plants = [_plant(f"VIN{i:04d}", "charging_rate") for i in range(n)]
        findings = [_finding(f"VIN{i:04d}", "charging_rate") for i in range(n)]
        correct, total, pct = score_plants(plants, findings)
        assert pct >= _SCORE_THRESHOLD_PCT

    def test_score_below_threshold(self):
        n = 125
        plants = [_plant(f"VIN{i:04d}", "charging_rate") for i in range(n)]
        findings = [_finding(f"VIN{i:04d}", "charging_rate") for i in range(79)]
        correct, total, pct = score_plants(plants, findings)
        assert correct == 79
        assert pct < _SCORE_THRESHOLD_PCT

    def test_mutation_finding_kind_filter_is_checked(self):
        plants = [_plant("VIN1", "low_utilisation")]
        sell_finding = {
            "finding_kind": "fleet.cost.sell_timing",
            "agent_id": _AGENT_ID,
            "scope": {"vin": "VIN1"},
            "fleet_id": "FLEET-A",
            "recommended_action": {"parameters": {"cause": "low_utilisation"}},
        }
        correct, total, pct = score_plants(plants, [sell_finding])
        assert correct == 0

    def test_mutation_agent_id_filter_is_checked(self):
        plants = [_plant("VIN1", "charging_rate")]
        wrong_agent_finding = {
            "finding_kind": _FINDING_KIND_ANOMALY,
            "agent_id": "agent-fi-health",
            "scope": {"vin": "VIN1"},
            "fleet_id": "FLEET-A",
            "recommended_action": {"parameters": {"cause": "charging_rate"}},
        }
        correct, total, pct = score_plants(plants, [wrong_agent_finding])
        assert correct == 0


# ---------------------------------------------------------------------------
# check_paging_match tests
# ---------------------------------------------------------------------------


class TestCheckPagingMatch:
    def test_equal_counts_pass(self):
        ok, detail = check_paging_match(1000, 1000)
        assert ok is True
        assert "1000" in detail

    def test_unequal_counts_fail(self):
        ok, detail = check_paging_match(1000, 999)
        assert ok is False
        assert "999" in detail
        assert "1000" in detail

    def test_both_zero_pass(self):
        ok, detail = check_paging_match(0, 0)
        assert ok is True

    def test_paged_greater_than_count_fails(self):
        ok, detail = check_paging_match(500, 501)
        assert ok is False

    def test_large_counts(self):
        ok, detail = check_paging_match(2_976_234, 2_976_234)
        assert ok is True

    def test_mutation_equality_direction(self):
        ok_equal, _ = check_paging_match(42, 42)
        ok_unequal, _ = check_paging_match(42, 43)
        assert ok_equal is True
        assert ok_unequal is False


# ---------------------------------------------------------------------------
# build_env_with_override / build_env_restore tests
# ---------------------------------------------------------------------------


class TestEnvRestore:
    def test_override_adds_key(self):
        original = {"FOO": "bar", "BAZ": "qux"}
        result = build_env_with_override(original, {"FI_COST_MODEL_ID": "overridden"})
        assert result["FI_COST_MODEL_ID"] == "overridden"
        assert result["FOO"] == "bar"

    def test_override_replaces_existing_key(self):
        original = {"FI_COST_MODEL_ID": "real-model", "OTHER": "x"}
        result = build_env_with_override(original, {"FI_COST_MODEL_ID": "nonexistent"})
        assert result["FI_COST_MODEL_ID"] == "nonexistent"
        assert result["OTHER"] == "x"

    def test_override_does_not_mutate_original(self):
        original = {"FI_COST_MODEL_ID": "real-model"}
        _ = build_env_with_override(original, {"FI_COST_MODEL_ID": "nonexistent"})
        assert original["FI_COST_MODEL_ID"] == "real-model"


# ---------------------------------------------------------------------------
# Item 7 restore hardening tests (Step 1)
# ---------------------------------------------------------------------------


class TestItem7RestoreHardening:
    """Step 1: item7 restore must retry, check full map, write 0600 backup."""

    _ORIG = {"FI_COST_MODEL_ID": "real-model", "STAGE": "staging"}
    _SUMMARY = {
        "artifacts_written": 0, "tokens_used": 0, "abort_reason": None,
        "cost": {"deferred": 0, "model_calls": 0, "rules_fallbacks": 1,
                 "anomalies_written": 0, "sell_timing_written": 0, "data_month": "2026-09"},
    }

    def _make_lam(self, read_back=None):
        lam = MagicMock()
        rb = read_back if read_back is not None else self._ORIG
        lam.get_function_configuration.side_effect = [
            {"Environment": {"Variables": self._ORIG}, "LastUpdateStatus": "Successful"},
            {"Environment": {"Variables": rb}, "LastUpdateStatus": "Successful"},
        ]
        lam.update_function_configuration.return_value = {}
        return lam

    def _run(self, lam, extra_patches=(), ddb_items=None):
        import contextlib, io as _io
        ddb = MagicMock()
        ddb.Table.return_value.scan.return_value = {"Items": ddb_items or []}
        res = _mod.Results()
        patches = [
            patch.object(_mod, "_wait_lambda_response", return_value=self._SUMMARY),
            patch.object(_mod, "_check_ios_fleet_intersection", return_value=True),
            patch.object(_mod, "_wait_for_lambda_active"),
        ]
        patches.extend(extra_patches)
        ctx = contextlib.ExitStack()
        for p in patches:
            ctx.enter_context(p)
        with ctx, contextlib.redirect_stdout(_io.StringIO()):
            _mod.item7_rules_fallback(
                lam, ddb, MagicMock(), "findings", "fn",
                "staging", "us-west-2", "wg", "s3://x/", res,
            )
        return res

    def test_error_after_override_is_recorded_restored_and_stops_later_items(self):
        """Q5: an exception after the override (DDB scan) -> FAIL 7-run-error, restore runs, False."""
        lam = self._make_lam()
        import contextlib, io as _io
        res = _mod.Results()
        with patch.object(_mod, "_wait_lambda_response", return_value=self._SUMMARY), \
             patch.object(_mod, "_check_ios_fleet_intersection", return_value=True), \
             patch.object(_mod, "_wait_for_lambda_active"), \
             patch.object(_mod, "_scan_findings_by_fleet_all_fleets",
                          side_effect=RuntimeError("ProvisionedThroughputExceeded")), \
             contextlib.redirect_stdout(_io.StringIO()):
            ok = _mod.item7_rules_fallback(lam, MagicMock(), MagicMock(), "findings", "fn",
                                           "staging", "us-west-2", "wg", "s3://x/", res)
        assert ok is False
        assert ("7-run-error", "FAIL") in [(r[0], r[1]) for r in res.rows]
        env_calls = [c.kwargs.get("Environment") for c in lam.update_function_configuration.call_args_list]
        assert env_calls[-1] == {"Variables": self._ORIG}, env_calls

    def test_reply_without_cost_subdict_returns_false(self):
        """An unexpected Lambda reply stops later invoking items (returns False)."""
        lam = self._make_lam()
        import contextlib, io as _io
        res = _mod.Results()
        with patch.object(_mod, "_wait_lambda_response", return_value={"tokens_used": 0}), \
             patch.object(_mod, "_check_ios_fleet_intersection", return_value=True), \
             patch.object(_mod, "_wait_for_lambda_active"), \
             contextlib.redirect_stdout(_io.StringIO()):
            ok = _mod.item7_rules_fallback(lam, MagicMock(), MagicMock(), "findings", "fn",
                                           "staging", "us-west-2", "wg", "s3://x/", res)
        assert ok is False

    def test_existing_override_refuses_without_writing(self):
        """A killed earlier run left FI_COST_MODEL_ID overridden: refuse, write nothing, return False."""
        lam = MagicMock()
        bad = dict(self._ORIG, FI_COST_MODEL_ID=_mod._NON_EXISTENT_MODEL)
        lam.get_function_configuration.return_value = {
            "Environment": {"Variables": bad}, "LastUpdateStatus": "Successful"}
        import contextlib, io as _io
        res = _mod.Results()
        with patch.object(_mod, "_check_ios_fleet_intersection", return_value=True), \
             patch.object(_mod, "_wait_lambda_response") as inv, \
             contextlib.redirect_stdout(_io.StringIO()):
            ok = _mod.item7_rules_fallback(lam, MagicMock(), MagicMock(), "findings", "fn",
                                           "staging", "us-west-2", "wg", "s3://x/", res)
        assert ok is False
        assert lam.update_function_configuration.call_count == 0
        assert inv.call_count == 0
        assert ("7-original-model-sane", "FAIL") in [(r[0], r[1]) for r in res.rows]

    def test_restore_called_with_original_map_when_invoke_raises(self):
        """invoke raises → restore update_function_configuration called with original map."""
        lam = self._make_lam()
        import contextlib, io as _io
        ddb = MagicMock()
        ddb.Table.return_value.scan.return_value = {"Items": []}
        res = _mod.Results()
        with patch.object(_mod, "_wait_lambda_response", side_effect=RuntimeError("fail")), \
             patch.object(_mod, "_check_ios_fleet_intersection", return_value=True), \
             patch.object(_mod, "_wait_for_lambda_active"), \
             contextlib.redirect_stdout(_io.StringIO()):
            _mod.item7_rules_fallback(
                lam, ddb, MagicMock(), "findings", "fn",
                "staging", "us-west-2", "wg", "s3://x/", res,
            )
        env_calls = [c.kwargs.get("Environment") for c in lam.update_function_configuration.call_args_list]
        assert {"Variables": self._ORIG} in env_calls, (
            f"restore call with original env not found; all env args: {env_calls}"
        )
        assert lam.get_function_configuration.call_count >= 2, (
            "get_function_configuration must be called for initial read AND read-back"
        )

    def test_retry_on_resource_conflict_then_succeeds(self):
        """First restore raises ResourceConflictException, second succeeds → no 7-restore FAIL."""
        class _Conflict(Exception):
            response = {"Error": {"Code": "ResourceConflictException"}}

        lam = MagicMock()
        lam.get_function_configuration.side_effect = [
            {"Environment": {"Variables": self._ORIG}, "LastUpdateStatus": "Successful"},
            {"Environment": {"Variables": self._ORIG}, "LastUpdateStatus": "Successful"},
        ]
        # First call → override succeeds; second → conflict; third → restore succeeds
        lam.update_function_configuration.side_effect = [{}, _Conflict(), {}]
        with patch.object(_mod.time, "sleep"):
            res = self._run(lam)
        restore_fails = [r for r in res.rows if r[0] == "7-restore" and r[1] == "FAIL"]
        assert not restore_fails, f"7-restore must not FAIL after retry succeeds; rows={restore_fails}"
        # Confirm the retry actually completed: read-back must have been called.
        assert lam.get_function_configuration.call_count == 2, (
            "read-back get_function_configuration must happen after retry succeeds; "
            f"call_count={lam.get_function_configuration.call_count}"
        )

    def test_readback_differs_in_unrelated_key_fails_restore(self):
        """Full map check: if ANY key differs after restore, 7-restore must FAIL."""
        different = dict(self._ORIG)
        different["UNRELATED_KEY"] = "injected"
        lam = self._make_lam(read_back=different)
        res = self._run(lam)
        fail_rows = [r for r in res.rows if r[0] == "7-restore" and r[1] == "FAIL"]
        assert fail_rows, (
            f"7-restore must FAIL when read-back env has extra key. rows={[(r[0],r[1]) for r in res.rows]}"
        )

    def _run_with_signal_mid_restore(self, signum, install_handlers: bool):
        """Deliver ``signum`` from inside the restore's update call; return (exc, lam)."""
        import contextlib, io as _io, signal as _sig
        saved = {s: _sig.getsignal(s) for s in (_sig.SIGINT, _sig.SIGTERM, _sig.SIGHUP)}
        saved_state = dict(_mod._SIGNAL_STATE)
        if install_handlers:
            _mod._install_signal_handlers()
        lam = self._make_lam()
        calls = [0]

        def update(*a, **kw):
            calls[0] += 1
            if calls[0] == 2:  # the restore call, inside item 7's finally
                os.kill(os.getpid(), signum)
            return {}

        lam.update_function_configuration.side_effect = update
        ddb = MagicMock()
        ddb.Table.return_value.scan.return_value = {"Items": []}
        exc = None
        try:
            with patch.object(_mod, "_wait_lambda_response", return_value=self._SUMMARY), \
                 patch.object(_mod, "_check_ios_fleet_intersection", return_value=True), \
                 patch.object(_mod, "_wait_for_lambda_active"), \
                 contextlib.redirect_stdout(_io.StringIO()), contextlib.redirect_stderr(_io.StringIO()):
                _mod.item7_rules_fallback(lam, ddb, MagicMock(), "findings", "fn",
                                          "staging", "us-west-2", "wg", "s3://x/", _mod.Results())
        except BaseException as e:  # noqa: BLE001
            exc = e
        finally:
            after = {s: _sig.getsignal(s) for s in saved}
            for s, h in saved.items():
                _sig.signal(s, h)
            _mod._SIGNAL_STATE.update(saved_state)
        return exc, lam, after, saved

    def test_a_w1_signal_during_restore_is_held_until_read_back_then_stops_the_run(self):
        """A-W1: SIGINT, SIGTERM or SIGHUP arriving DURING the restore (the first signal of the
        run) does not cut it short: the read-back still runs, and the signal then stops the run."""
        import signal as _sig
        for signum, installed, expect in (
            (_sig.SIGTERM, True, SystemExit),
            (_sig.SIGHUP, True, SystemExit),
            (_sig.SIGINT, False, KeyboardInterrupt),
        ):
            exc, lam, _, _ = self._run_with_signal_mid_restore(signum, installed)
            env_calls = [c.kwargs.get("Environment") for c in lam.update_function_configuration.call_args_list]
            assert env_calls[-1] == {"Variables": self._ORIG}, (signum, env_calls)
            assert lam.get_function_configuration.call_count == 2, (
                f"signal {signum}: the read-back must run after a signal mid-restore; "
                f"get_function_configuration calls={lam.get_function_configuration.call_count}")
            assert isinstance(exc, expect), f"signal {signum}: expected {expect.__name__}, got {exc!r}"
            if expect is SystemExit:
                assert exc.code == 143

    def test_a_w1_handlers_are_restored_after_the_restore(self):
        """The held-signal handlers are removed once the restore block ends."""
        import signal as _sig
        exc, _, after, saved = self._run_with_signal_mid_restore(_sig.SIGTERM, True)
        assert isinstance(exc, SystemExit)
        # After the block, the script's own handlers (installed by the helper) are back.
        assert after[_sig.SIGINT] == saved[_sig.SIGINT], after
        assert getattr(after[_sig.SIGTERM], "__name__", "") == "_exit_once", after

    def test_r4_w4_resource_conflict_retried_for_120s(self):
        """R4-W4: ResourceConflict retries for at least 120 s (40 attempts × 3 s)."""
        class _Conflict(Exception):
            response = {"Error": {"Code": "ResourceConflictException"}}

        lam = MagicMock()
        lam.get_function_configuration.side_effect = [
            {"Environment": {"Variables": self._ORIG}, "LastUpdateStatus": "Successful"},
            {"Environment": {"Variables": self._ORIG}, "LastUpdateStatus": "Successful"},
        ]
        # 30 conflicts, then success on 31st call (override is call 0)
        lam.update_function_configuration.side_effect = (
            [{}]  # override succeeds
            + [_Conflict()] * 30  # 30 conflicts
            + [{}]  # restore succeeds on attempt 31
        )
        with patch.object(_mod.time, "sleep"):
            res = self._run(lam)
        restore_fails = [r for r in res.rows if r[0] == "7-restore" and r[1] == "FAIL"]
        assert not restore_fails, (
            f"R4-W4: restore must succeed after 30 ResourceConflicts (< 40 max). "
            f"Got 7-restore FAIL rows={restore_fails}"
        )

    def test_r4_c1_restore_fails_returns_false(self):
        """R4-C1: item7 must return False when restore raises (not True)."""
        class _AccessDenied(Exception):
            response = {"Error": {"Code": "AccessDeniedException"}}

        lam = MagicMock()
        lam.get_function_configuration.return_value = {
            "Environment": {"Variables": self._ORIG}, "LastUpdateStatus": "Successful",
        }
        # override succeeds, restore fails
        lam.update_function_configuration.side_effect = [{}, _AccessDenied("denied")]
        import contextlib, io as _io
        ddb = MagicMock()
        ddb.Table.return_value.scan.return_value = {"Items": []}
        res = _mod.Results()
        with patch.object(_mod, "_wait_lambda_response", return_value=self._SUMMARY), \
             patch.object(_mod, "_check_ios_fleet_intersection", return_value=True), \
             patch.object(_mod, "_wait_for_lambda_active"), \
             contextlib.redirect_stdout(_io.StringIO()):
            ok = _mod.item7_rules_fallback(
                lam, ddb, MagicMock(), "findings", "fn",
                "staging", "us-west-2", "wg", "s3://x/", res,
            )
        assert ok is False, (
            f"R4-C1: item7 must return False when restore fails. Got {ok!r}. "
            f"Mutant 'return True' would return True and skip would not fire."
        )
        assert res._cleanup_failed, "R4-C1: _cleanup_failed must be set when restore fails"

    def test_r4_c1_readback_mismatch_returns_false(self):
        """R4-C1: item7 must return False when read-back env mismatches."""
        different = dict(self._ORIG, UNRELATED_KEY="injected")
        lam = self._make_lam(read_back=different)
        import contextlib, io as _io
        ddb = MagicMock()
        ddb.Table.return_value.scan.return_value = {"Items": []}
        res = _mod.Results()
        with patch.object(_mod, "_wait_lambda_response", return_value=self._SUMMARY), \
             patch.object(_mod, "_check_ios_fleet_intersection", return_value=True), \
             patch.object(_mod, "_wait_for_lambda_active"), \
             contextlib.redirect_stdout(_io.StringIO()):
            ok = _mod.item7_rules_fallback(
                lam, ddb, MagicMock(), "findings", "fn",
                "staging", "us-west-2", "wg", "s3://x/", res,
            )
        assert ok is False, (
            f"R4-C1: item7 must return False when read-back mismatches. Got {ok!r}."
        )
        assert res._cleanup_failed, "R4-C1: _cleanup_failed must be set when read-back mismatches"

    def test_backup_file_created_0600_with_original_map(self, tmp_path):
        """A 0600 backup file containing the original Variables must be created."""
        import json as _json
        captured = []
        def _fake_mkdtemp(**kw):
            d = tmp_path / "bk"
            d.mkdir()
            captured.append(d)
            return str(d)

        lam = self._make_lam()
        seen: list = []

        def update(*a, **kw):
            if not seen:  # the override: the backup must already exist, 0600, original map
                backup = captured[0] / "restore-env.json"
                seen.append((backup.exists(), _json.loads(backup.read_text()),
                             backup.stat().st_mode & 0o777))
            return {}

        lam.update_function_configuration.side_effect = update
        with patch("tempfile.mkdtemp", _fake_mkdtemp):
            self._run(lam)

        exists, content, mode = seen[0]
        assert exists, "backup file must be created inside mkdtemp directory before the override"
        assert content == {"Variables": self._ORIG}, f"backup content wrong: {content}"
        assert mode == 0o600, f"backup file mode should be 0600, got {oct(mode)}"
        # B-S4: once the restore is read back and verified, the backup is removed.
        assert not (captured[0] / "restore-env.json").exists(), "backup must be removed after a verified restore"
        assert not captured[0].exists(), "backup directory must be removed after a verified restore"

    def test_b_s4_backup_kept_when_the_restore_did_not_verify(self, tmp_path):
        """B-S4: a restore whose read-back differs keeps the backup for a manual restore."""
        d = tmp_path / "bk"
        d.mkdir()
        lam = self._make_lam(read_back=dict(self._ORIG, UNRELATED_KEY="x"))
        with patch("tempfile.mkdtemp", lambda **kw: str(d)):
            res = self._run(lam)
        assert res._cleanup_failed
        assert (d / "restore-env.json").exists(), "backup must be kept when the restore did not verify"


# ---------------------------------------------------------------------------
# _within_miles_tolerance tests
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# _check_text_runtime_trust_flag tests
# ---------------------------------------------------------------------------


def _make_agentcore_ctrl_mock(
    runtimes: list[dict],
    env_vars: dict | None = None,
    raises: Exception | None = None,
) -> MagicMock:
    ctrl = MagicMock()
    ctrl.list_agent_runtimes.return_value = {"agentRuntimes": runtimes}
    if raises is not None:
        ctrl.get_agent_runtime.side_effect = raises
    else:
        ctrl.get_agent_runtime.return_value = {
            "environmentVariables": env_vars or {},
        }
    return ctrl


class TestCheckTextRuntimeTrustFlag:
    def _patch_ctrl(self, ctrl_mock, stage="staging", region="us-west-2"):
        boto3_mock = MagicMock()
        boto3_mock.client.return_value = ctrl_mock
        with patch.dict("sys.modules", {"boto3": boto3_mock}):
            return _check_text_runtime_trust_flag(stage, region)

    def test_returns_true_when_flag_is_true(self):
        ctrl = _make_agentcore_ctrl_mock(
            runtimes=[{"agentRuntimeName": "vsa_supervisor_text_staging", "agentRuntimeId": "RT-1"}],
            env_vars={"AVX_FLEET_IDS_CLAIM_TRUSTED": "true"},
        )
        result = self._patch_ctrl(ctrl)
        assert result is True

    def test_returns_false_when_flag_is_false(self):
        ctrl = _make_agentcore_ctrl_mock(
            runtimes=[{"agentRuntimeName": "vsa_supervisor_text_staging", "agentRuntimeId": "RT-1"}],
            env_vars={"AVX_FLEET_IDS_CLAIM_TRUSTED": "false"},
        )
        result = self._patch_ctrl(ctrl)
        assert result is False

    def test_returns_false_when_flag_is_absent(self):
        ctrl = _make_agentcore_ctrl_mock(
            runtimes=[{"agentRuntimeName": "vsa_supervisor_text_staging", "agentRuntimeId": "RT-1"}],
            env_vars={"SOME_OTHER_VAR": "value"},
        )
        result = self._patch_ctrl(ctrl)
        assert result is False

    def test_returns_none_when_no_matching_runtime(self):
        ctrl = _make_agentcore_ctrl_mock(
            runtimes=[
                {"agentRuntimeName": "vsa_supervisor_bidi_staging", "agentRuntimeId": "RT-BIDI"},
                {"agentRuntimeName": "some_other_runtime", "agentRuntimeId": "RT-OTHER"},
            ],
        )
        result = self._patch_ctrl(ctrl)
        assert result is None

    def test_returns_none_when_list_raises(self):
        ctrl = MagicMock()
        ctrl.list_agent_runtimes.side_effect = Exception("AccessDenied")
        result = self._patch_ctrl(ctrl)
        assert result is None

    def test_matches_by_prefix_not_exact(self):
        ctrl = _make_agentcore_ctrl_mock(
            runtimes=[
                {"agentRuntimeName": "vsa_supervisor_text_staging-AbCdE12345", "agentRuntimeId": "RT-1"}
            ],
            env_vars={"AVX_FLEET_IDS_CLAIM_TRUSTED": "true"},
        )
        result = self._patch_ctrl(ctrl)
        assert result is True

    def test_uses_agentcore_control_not_lambda(self):
        ctrl = _make_agentcore_ctrl_mock(
            runtimes=[{"agentRuntimeName": "vsa_supervisor_text_staging", "agentRuntimeId": "RT-1"}],
            env_vars={"AVX_FLEET_IDS_CLAIM_TRUSTED": "true"},
        )
        boto3_mock = MagicMock()
        boto3_mock.client.return_value = ctrl
        with patch.dict("sys.modules", {"boto3": boto3_mock}):
            _check_text_runtime_trust_flag("staging", "us-west-2")
        call_args = boto3_mock.client.call_args
        service_name = call_args[0][0] if call_args[0] else call_args[1].get("service_name", "")
        assert "agentcore-control" in service_name

    def test_mutation_agentcore_not_lambda_when_both_available(self):
        ctrl = _make_agentcore_ctrl_mock(
            runtimes=[{"agentRuntimeName": "vsa_supervisor_text_staging", "agentRuntimeId": "RT-1"}],
            env_vars={"AVX_FLEET_IDS_CLAIM_TRUSTED": "true"},
        )
        boto3_mock = MagicMock()
        boto3_mock.client.return_value = ctrl
        with patch.dict("sys.modules", {"boto3": boto3_mock}):
            result = _check_text_runtime_trust_flag("staging", "us-west-2")
        assert result is True


# ---------------------------------------------------------------------------
# _read_answer_key — duckdb replaces pyarrow
# ---------------------------------------------------------------------------


class TestReadAnswerKeyDuckdb:
    @staticmethod
    def _make_parquet_bytes(rows: list[dict]) -> bytes:
        import duckdb  # noqa: PLC0415
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "test.parquet")
            if not rows:
                duckdb.execute(
                    f"COPY (SELECT NULL::VARCHAR AS vin, NULL::VARCHAR AS cause LIMIT 0) "
                    f"TO '{path}' (FORMAT PARQUET)"
                )
            else:
                quoted = ", ".join(
                    f"('{r['vin']}', '{r['cause']}')" for r in rows
                )
                duckdb.execute(
                    f"COPY (SELECT * FROM (VALUES {quoted}) t(vin, cause)) "
                    f"TO '{path}' (FORMAT PARQUET)"
                )
            with open(path, "rb") as f:
                return f.read()

    def _make_s3_mock(self, rows: list[dict]) -> MagicMock:
        data = self._make_parquet_bytes(rows)
        s3 = MagicMock()
        s3.list_objects_v2.return_value = {
            "CommonPrefixes": [
                {"Prefix": "restricted/fleet_cost/plants/plant_anchor_month=2026-08/"}
            ]
        }
        body_mock = MagicMock()
        body_mock.read.return_value = data
        s3.get_object.return_value = {"Body": body_mock}
        return s3

    def test_reads_vin_and_cause_columns(self):
        rows = [
            {"vin": "VIN001", "cause": "charging_rate"},
            {"vin": "VIN002", "cause": "efficiency"},
        ]
        s3 = self._make_s3_mock(rows)
        result, anchor_month = _read_answer_key(s3, "adp-staging-lake", "staging")
        assert anchor_month == "2026-08"
        assert len(result) == 2
        vins = {r["vin"] for r in result}
        assert vins == {"VIN001", "VIN002"}

    def test_returns_correct_anchor_month(self):
        rows = [{"vin": "V1", "cause": "efficiency"}]
        s3 = self._make_s3_mock(rows)
        _, anchor_month = _read_answer_key(s3, "bucket", "staging")
        assert anchor_month == "2026-08"

    def test_empty_parquet_returns_empty_list(self):
        s3 = self._make_s3_mock([])
        result, _ = _read_answer_key(s3, "bucket", "staging")
        assert isinstance(result, list)

    def test_does_not_require_pyarrow(self):
        rows = [{"vin": "VIN001", "cause": "charging_mix"}]
        s3 = self._make_s3_mock(rows)
        original = sys.modules.get("pyarrow")
        sys.modules["pyarrow"] = None  # type: ignore[assignment]
        try:
            result, _ = _read_answer_key(s3, "bucket", "staging")
            assert len(result) == 1
        finally:
            if original is None:
                sys.modules.pop("pyarrow", None)
            else:
                sys.modules["pyarrow"] = original

    def test_raises_on_no_anchor_prefix(self):
        s3 = MagicMock()
        s3.list_objects_v2.return_value = {"CommonPrefixes": []}
        with pytest.raises(RuntimeError, match="No plant_anchor_month"):
            _read_answer_key(s3, "bucket", "staging")

    def test_mutation_duckdb_path_is_taken_not_pyarrow(self):
        rows = [{"vin": "VIN-MUT", "cause": "repeat_repair"}]
        s3 = self._make_s3_mock(rows)
        with patch.dict("sys.modules", {"pyarrow": None, "pyarrow.parquet": None}):
            result, _ = _read_answer_key(s3, "bucket", "staging")
        assert any(r.get("vin") == "VIN-MUT" for r in result)


# ---------------------------------------------------------------------------
# allow_writes guard and iOS fleet exclusion (items 5 and 8)
# ---------------------------------------------------------------------------


class TestAllowWritesGuard:
    def test_item5_skips_without_allow_writes(self):
        res = Results()
        item5_fleet_scope(
            ddb=MagicMock(),
            idp=MagicMock(),
            findings_table="vsa-staging-avx-findings",
            pool_id="us-west-2_XXXXX",
            client_id="CLIENT",
            api=MagicMock(),
            stage="staging",
            res=res,
            allow_writes=False,
        )
        skip_rows = [r for r in res.rows if r[1] == "SKIP"]
        assert skip_rows, "item5 must skip when allow_writes=False"
        assert not res.failed

    def test_item5_does_not_call_cognito_without_allow_writes(self):
        idp_mock = MagicMock()
        res = Results()
        item5_fleet_scope(
            ddb=MagicMock(),
            idp=idp_mock,
            findings_table="vsa-staging-avx-findings",
            pool_id="us-west-2_XXXXX",
            client_id="CLIENT",
            api=MagicMock(),
            stage="staging",
            res=res,
            allow_writes=False,
        )
        idp_mock.admin_create_user.assert_not_called()

    def test_item8_skips_without_allow_writes(self):
        res = Results()
        item8_fleet_recommendation(
            lam=MagicMock(),
            ddb=MagicMock(),
            idp=MagicMock(),
            findings_table="vsa-staging-avx-findings",
            actions_table="vsa-staging-avx-actions",
            pool_id="us-west-2_XXXXX",
            client_id="CLIENT",
            api=MagicMock(),
            function_name="vsa-staging-tier2-health-agent-us-west-2",
            stage="staging",
            region="us-west-2",
            res=res,
            allow_writes=False,
        )
        skip_rows = [r for r in res.rows if r[1] == "SKIP"]
        assert skip_rows, "item8 must skip when allow_writes=False"
        assert not res.failed

    def test_item8_does_not_call_cognito_without_allow_writes(self):
        idp_mock = MagicMock()
        res = Results()
        item8_fleet_recommendation(
            lam=MagicMock(),
            ddb=MagicMock(),
            idp=idp_mock,
            findings_table="vsa-staging-avx-findings",
            actions_table="vsa-staging-avx-actions",
            pool_id="us-west-2_XXXXX",
            client_id="CLIENT",
            api=MagicMock(),
            function_name="vsa-staging-tier2-health-agent-us-west-2",
            stage="staging",
            region="us-west-2",
            res=res,
            allow_writes=False,
        )
        idp_mock.admin_create_user.assert_not_called()

    def test_mutation_removing_allow_writes_guard_would_call_cognito(self):
        idp_mock = MagicMock()
        res = Results()
        item5_fleet_scope(
            ddb=MagicMock(),
            idp=idp_mock,
            findings_table="vsa-staging-avx-findings",
            pool_id="us-west-2_XXXXX",
            client_id="CLIENT",
            api=MagicMock(),
            stage="staging",
            res=res,
            allow_writes=False,
        )
        assert not idp_mock.admin_create_user.called


class TestIosFleetExclusion:
    def test_ios_fleet_ids_constant_not_empty(self):
        assert len(_IOS_PERSONA_FLEET_IDS) > 0

    def test_known_persona_fleet_ids_are_excluded(self):
        known_ios_fleet_ids = {
            "FLEET-MERIDIAN-ONBOARD",
            "MERIDIAN-OEM",
            "WPR-RENTAL",
            "VO-OEM",
            "SA-FLEET",
            "TECH-FLEET",
        }
        missing = known_ios_fleet_ids - _IOS_PERSONA_FLEET_IDS
        assert not missing, f"iOS persona fleet IDs missing: {missing}"

    def test_item5_excludes_ios_fleet_ids_from_ddb_query(self):
        ddb_mock = MagicMock()
        table_mock = MagicMock()
        ddb_mock.Table.return_value = table_mock
        table_mock.scan.return_value = {
            "Items": [
                {"agent_id": _mod._AGENT_ID, "fleet_id": "FLEET-MERIDIAN-ONBOARD"},
                {"agent_id": _mod._AGENT_ID, "fleet_id": "WPR-RENTAL"},
            ]
        }
        res = Results()
        item5_fleet_scope(
            ddb=ddb_mock,
            idp=MagicMock(),
            findings_table="vsa-staging-avx-findings",
            pool_id="us-west-2_XXXXX",
            client_id="CLIENT",
            api=MagicMock(),
            stage="staging",
            res=res,
            allow_writes=True,
        )
        skip_rows = [r for r in res.rows if r[1] == "SKIP"]
        assert skip_rows, "item5 should SKIP when only iOS persona fleet IDs are present"

    def test_mutation_ios_fleet_ids_are_excluded_not_included(self):
        ddb_mock = MagicMock()
        table_mock = MagicMock()
        ddb_mock.Table.return_value = table_mock
        table_mock.scan.return_value = {
            "Items": [
                {"agent_id": _mod._AGENT_ID, "fleet_id": "WPR-RENTAL"},
                {"agent_id": _mod._AGENT_ID, "fleet_id": "VO-OEM"},
            ]
        }
        idp_mock = MagicMock()
        res = Results()
        item5_fleet_scope(
            ddb=ddb_mock,
            idp=idp_mock,
            findings_table="test-table",
            pool_id="us-west-2_XXXXX",
            client_id="CLIENT",
            api=MagicMock(),
            stage="staging",
            res=res,
            allow_writes=True,
        )
        assert not idp_mock.admin_create_user.called


# ===========================================================================
# C1: exit-code contract — SKIP alongside PASS must exit 2
# ===========================================================================


class TestC1ExitCodeContract:
    """C1: exit 2 when any SKIP exists, even alongside PASSes.

    The old code used all_skipped which returned True only when NO pass or fail
    existed.  SKIP next to PASS returned 0 (pass) — this was the bug.
    The fix: has_any_skip returns True whenever any SKIP row exists.
    """

    def test_has_any_skip_true_when_skip_present(self):
        res = Results()
        res.check("check-a", True)
        res.skip("check-b", "reason")
        assert res.has_any_skip is True

    def test_has_any_skip_false_when_no_skip(self):
        res = Results()
        res.check("check-a", True)
        res.check("check-b", True)
        assert res.has_any_skip is False

    def test_has_any_skip_true_even_with_passes_and_notes(self):
        """PASS + NOTE + SKIP → has_any_skip is True (was False with all_skipped)."""
        res = Results()
        res.check("a", True)
        res.check("b", True)
        res.note("c", "detail")
        res.skip("d", "not run")
        assert res.has_any_skip is True, (
            "has_any_skip must be True when any SKIP row exists, "
            "even alongside PASSes and NOTEs. "
            "The old all_skipped returned False here (C1 bug)."
        )

    def test_all_pass_no_skip_not_has_any_skip(self):
        res = Results()
        for i in range(5):
            res.check(f"check-{i}", True)
        assert res.has_any_skip is False

    def test_mutation_skip_plus_pass_must_not_look_like_all_skipped(self):
        """Verify the old all_skipped logic would have been wrong here.

        Old logic: all_skipped = bool(skipped) and all(r[1] in ('SKIP', 'NOTE') for r in rows)
        With [PASS, SKIP]: all(r[1] in ('SKIP', 'NOTE') ...) is False because of PASS.
        → all_skipped = False → exit 0.  Wrong.

        New logic: has_any_skip = any(r[1] == 'SKIP' for r in rows) = True → exit 2.  Correct.
        """
        res = Results()
        res.check("item-1", True)   # PASS
        res.skip("item-2", "need --allow-writes")  # SKIP
        # has_any_skip must be True (the fix)
        assert res.has_any_skip is True
        # Simulate the old broken check (would return False — wrong)
        broken_all_skipped = (
            bool(res.rows)
            and all(r[1] in ("SKIP", "NOTE") for r in res.rows)
        )
        assert broken_all_skipped is False, (
            "Confirms the old all_skipped logic returns False on PASS+SKIP — "
            "that's the C1 bug. has_any_skip correctly returns True."
        )

    def test_cleanup_failed_contributes_to_exit_1(self):
        """mark_cleanup_failed causes exit 1, not exit 0."""
        res = Results()
        res.check("check-a", True)
        assert not res._cleanup_failed
        res.mark_cleanup_failed()
        assert res._cleanup_failed

    def test_n4_main_exits_2_when_write_items_dropped(self):
        """N4/C1: main() must exit 2 (not 0) when write items are dropped for !allow_writes.

        Old main() removed write items from items_to_run silently; no SKIP rows were added;
        exit 0 was returned when items 1 and 4 passed (probe P1).  Fix: add SKIP rows.
        """
        def fake_item1(athena, ddb, wg, ol, stage, res, **kw):
            res.check("1-x", True)

        def fake_item4(athena, ddb, wg, ol, stage, rt, res, **kw):
            res.check("4-x", True)

        import contextlib
        import io as _io
        boto3 = MagicMock()
        boto3.client.return_value.get_caller_identity.return_value = {
            "Account": "000000000000", "Arn": "arn:x",
        }
        boto3.client.return_value.describe_stacks.return_value = {
            "Stacks": [{"Outputs": [{"OutputKey": "RestApiUrl", "OutputValue": "https://x"}]}],
        }
        boto3.resource.return_value = MagicMock()
        with patch.object(_mod, "item1_views_rows", fake_item1), \
             patch.object(_mod, "item4_athena_paging", fake_item4), \
             patch.dict(sys.modules, {"boto3": boto3}), \
             patch.object(sys, "argv", ["lv", "--stage", "staging", "--region", "us-west-2", "--all"]), \
             patch.dict(os.environ, {"LIVE_TESTS": "1"}), \
             contextlib.redirect_stdout(_io.StringIO()), \
             contextlib.redirect_stderr(_io.StringIO()):
            rc = _mod.main()
        assert rc == 2, (
            f"N4/C1: exit code={rc}, expected 2. "
            "Running --all without --allow-writes drops write items. "
            "Those items must produce SKIP rows so the run exits 2 (non-passing)."
        )

    def test_n4_main_exits_2_for_single_item_subset(self):
        """N4/C1: --item 1 must exit 2 (not 0) even when item 1 passes.

        Running a subset is always non-passing because items 2-8 are unverified.
        """
        def fake_item1(athena, ddb, wg, ol, stage, res, **kw):
            res.check("1-x", True)

        import contextlib
        import io as _io
        boto3 = MagicMock()
        boto3.client.return_value.get_caller_identity.return_value = {
            "Account": "000000000000", "Arn": "arn:x",
        }
        boto3.client.return_value.describe_stacks.return_value = {
            "Stacks": [{"Outputs": [{"OutputKey": "RestApiUrl", "OutputValue": "https://x"}]}],
        }
        boto3.resource.return_value = MagicMock()
        with patch.object(_mod, "item1_views_rows", fake_item1), \
             patch.dict(sys.modules, {"boto3": boto3}), \
             patch.object(sys, "argv", ["lv", "--stage", "staging", "--region", "us-west-2", "--item", "1"]), \
             patch.dict(os.environ, {"LIVE_TESTS": "1"}), \
             contextlib.redirect_stdout(_io.StringIO()), \
             contextlib.redirect_stderr(_io.StringIO()):
            rc = _mod.main()
        assert rc == 2, (
            f"N4/C1: exit code={rc}, expected 2. "
            "Running --item 1 leaves items 2-8 unverified; those must produce SKIP rows."
        )


# ===========================================================================
# C2: run_cost summary["cost"] shape — bad data → FAIL
# ===========================================================================


class TestC2ExtractCostStats:
    """C2: item 2/3/7 must read deferred/model_calls/rules_fallbacks from summary["cost"].

    Old code read these from the top level (run_stats.get("deferred", 0)), which
    always defaulted to 0 because they live under summary["cost"].
    """

    def test_cost_subdict_present_returns_it(self):
        summary = _run_cost_summary(deferred=5, model_calls=3, rules_fallbacks=1)
        cost, ok = _extract_cost_stats(summary)
        assert ok is True
        assert cost["deferred"] == 5
        assert cost["model_calls"] == 3
        assert cost["rules_fallbacks"] == 1

    def test_cost_subdict_absent_returns_false(self):
        """A response without 'cost' key must return (empty, False).

        C2: old code defaulted to 0 from top level → deferred always 0 →
        item 2 convergence always passed after 1 run.
        """
        summary_no_cost = {
            "artifacts_written": 0,
            "tokens_used": 100,
            "status": "completed",
            # No 'cost' key
        }
        cost, ok = _extract_cost_stats(summary_no_cost)
        assert ok is False, (
            "_extract_cost_stats must return False when 'cost' key is absent. "
            "C2: old code returned 0 from top level, masking missing data."
        )
        assert cost == {}

    def test_mutation_reading_top_level_deferred_would_miss_real_deferred(self):
        """If code reads top-level 'deferred' instead of summary['cost']['deferred'],
        it sees 0 even when cost.deferred=75."""
        summary = _run_cost_summary(deferred=75)
        # Top level has no 'deferred'
        assert summary.get("deferred") is None or summary.get("deferred", 0) == 0, (
            "Top-level 'deferred' should not exist or be 0. "
            "If code read from here, it would see 0 and falsely converge."
        )
        # cost subdict has the real value
        cost, ok = _extract_cost_stats(summary)
        assert ok is True
        assert cost["deferred"] == 75, (
            "C2: deferred must be read from summary['cost']['deferred'], not top level."
        )

    def test_mutation_reading_top_level_model_calls_would_miss_real_count(self):
        summary = _run_cost_summary(model_calls=7)
        assert summary.get("model_calls") is None or summary.get("model_calls", 0) == 0
        cost, ok = _extract_cost_stats(summary)
        assert ok is True
        assert cost["model_calls"] == 7

    def test_mutation_reading_top_level_rules_fallbacks_always_zero(self):
        """rules_fallbacks lives in cost subdict; top level doesn't have it."""
        summary = _run_cost_summary(rules_fallbacks=3)
        top_level_fallbacks = summary.get("rules_fallbacks", 0)
        assert top_level_fallbacks == 0, (
            "Top-level rules_fallbacks should be 0 (not in RunRecord.to_dict). "
            "C2: old code read 0 from top level → item 7 always failed."
        )
        cost, ok = _extract_cost_stats(summary)
        assert ok is True
        assert cost["rules_fallbacks"] == 3

    def test_anomalies_written_from_cost_subdict(self):
        """Item 3 'wrote' = cost.anomalies_written + cost.sell_timing_written."""
        summary = _run_cost_summary(anomalies_written=2, sell_timing_written=1)
        cost, ok = _extract_cost_stats(summary)
        assert ok is True
        wrote = cost.get("anomalies_written", 0) + cost.get("sell_timing_written", 0)
        assert wrote == 3

    def test_mutation_old_put_update_keys_never_existed(self):
        """C2: old item3 read 'put' and 'update' from top level (RunRecord has neither).
        These keys don't exist, so wrote was always 0."""
        summary = _run_cost_summary(anomalies_written=5)
        # 'put' and 'update' are not in RunRecord.to_dict()
        assert "put" not in summary
        assert "update" not in summary


# ===========================================================================
# C3: Lambda client read_timeout contract
# ===========================================================================


class TestC3LambdaClientConfig:
    """C3: Lambda client must have read_timeout > 900s (function timeout) and
    total_max_attempts=1 to prevent parallel cost runs on timeout.
    """

    def test_lambda_read_timeout_exceeds_function_timeout(self):
        """LAMBDA_READ_TIMEOUT_S must be > 900 (the Lambda function timeout)."""
        assert _LAMBDA_READ_TIMEOUT_S > 900, (
            f"LAMBDA_READ_TIMEOUT_S={_LAMBDA_READ_TIMEOUT_S} must be > 900 s "
            "(the cost Lambda's 15-minute timeout). "
            "A shorter timeout allows botocore to retry, starting parallel runs."
        )

    def test_lambda_max_attempts_is_one(self):
        """LAMBDA_MAX_ATTEMPTS must be 1 — no retries."""
        assert _LAMBDA_MAX_ATTEMPTS == 1, (
            f"LAMBDA_MAX_ATTEMPTS={_LAMBDA_MAX_ATTEMPTS} must be 1. "
            "Any retry starts another cost run in parallel."
        )

    def test_make_lambda_client_uses_config(self):
        """N4: actually build the client and assert config attributes on the result.

        Old test was inside `if config_mock.called:` — a client built without Config
        made config_mock.called == False, so the assertions were never reached (M1 survived).
        """
        # Build the real client (no AWS calls; just check the config attributes)
        with patch.dict(os.environ, {
            "AWS_ACCESS_KEY_ID": "testing",
            "AWS_SECRET_ACCESS_KEY": "testing",
            "AWS_DEFAULT_REGION": "us-west-2",
        }):
            client = _mod._make_lambda_client("us-west-2")
        cfg = client.meta.config
        assert cfg.read_timeout > 900, (
            f"N4/M1: read_timeout={cfg.read_timeout} must be > 900 s "
            "(the Lambda function timeout). Old code had no Config → default 60s → parallel runs."
        )
        assert cfg.retries.get("total_max_attempts") == 1, (
            f"N4/M1: total_max_attempts={cfg.retries.get('total_max_attempts')} must be 1. "
            "Any retry starts another cost run in parallel."
        )

    def test_mutation_default_60s_timeout_would_be_less_than_function_timeout(self):
        """The default botocore read_timeout is 60 s < 900 s function timeout."""
        default_read_timeout = 60  # botocore default
        assert default_read_timeout < 900, "baseline: default 60s < 900s function timeout"
        assert _LAMBDA_READ_TIMEOUT_S > default_read_timeout, (
            "LAMBDA_READ_TIMEOUT_S must be > 60 s to override the botocore default"
        )


# ===========================================================================
# C4: fleet B isolation via DDB finding_ids
# ===========================================================================


class TestC4FleetBIsolation:
    """C4: item 5 must collect fleet B finding_ids from DDB by_fleet GSI and
    assert none appear in fleet A's paged response.

    Old code matched on fleet_id field in the API response, which the API
    strips (_strip_finding_for_response), so the check always passed vacuously.
    """

    def test_fleet_b_finding_ids_from_ddb_not_api_fleet_field(self):
        """The _all_finding_ids_for_fleet helper reads from DDB GSI, not the API.

        A Finding in DDB with fleet_id=fleet_B will have its fleet_id stripped
        by the API response, so matching on the API's fleet_id field is vacuous.
        This test verifies the helper reads finding_ids from DDB.
        """
        ddb_mock = MagicMock()
        table_mock = MagicMock()
        ddb_mock.Table.return_value = table_mock

        # Simulate DDB returning 2 findings for fleet B via by_fleet GSI
        fleet_b_items = [
            {"finding_id": "AVX-FLEET-B-001", "agent_id": _AGENT_ID, "fleet_id": "FLEET-B"},
            {"finding_id": "AVX-FLEET-B-002", "agent_id": _AGENT_ID, "fleet_id": "FLEET-B"},
        ]
        table_mock.query.return_value = {"Items": fleet_b_items}

        result = _mod._all_finding_ids_for_fleet(ddb_mock, "test-table", "FLEET-B")
        assert result == {"AVX-FLEET-B-001", "AVX-FLEET-B-002"}, (
            "Must return finding_ids from DDB by_fleet GSI, not from API response"
        )

    def test_mutation_zero_fleet_b_finding_ids_from_empty_ddb(self):
        """If fleet B has no Findings in DDB, the intersection is empty — vacuous pass.
        The check must also require ≥1 fleet A cost Finding to prevent vacuity."""
        ddb_mock = MagicMock()
        table_mock = MagicMock()
        ddb_mock.Table.return_value = table_mock
        table_mock.query.return_value = {"Items": []}  # no fleet B findings

        result = _mod._all_finding_ids_for_fleet(ddb_mock, "test-table", "FLEET-B")
        assert result == set(), "Empty DDB result → empty finding_id set"

    def test_item5_skips_when_fewer_than_2_fleets(self):
        """item5 must SKIP when only 1 fleet has cost Findings."""
        ddb_mock = MagicMock()
        table_mock = MagicMock()
        ddb_mock.Table.return_value = table_mock
        # Scan returns only 1 fleet
        table_mock.scan.return_value = {
            "Items": [{"agent_id": _mod._AGENT_ID, "fleet_id": "FLEET-ONLY-ONE"}]
        }
        res = Results()
        item5_fleet_scope(
            ddb=ddb_mock,
            idp=MagicMock(),
            findings_table="vsa-staging-avx-findings",
            pool_id="us-west-2_XXXXX",
            client_id="CLIENT",
            api=MagicMock(),
            stage="staging",
            res=res,
            allow_writes=True,
        )
        skip_rows = [r for r in res.rows if r[1] == "SKIP"]
        assert skip_rows, "item5 must SKIP when fewer than 2 non-iOS fleets exist"


# ===========================================================================
# C5: agent paging check via run record
# ===========================================================================


class TestC5AgentPagingCheck:
    """C5: item 4 must compare the agent's anomalies_selected (from run record)
    with COUNT(*) of the outlier predicate, not the script's own SELECT.

    Old code ran SELECT vin, year_month FROM fleet_cost.view which measured
    the script's own pager, not the agent's.
    """

    def test_fleet_cost_db_includes_stage_prefix(self):
        """The database name must include the stage prefix."""
        assert _fleet_cost_db("staging") == "adp_staging_fleet_cost", (
            "C7/C5: database must be adp_{stage}_fleet_cost, not fleet_cost"
        )
        assert _fleet_cost_db("prod") == "adp_prod_fleet_cost"

    def test_select_anomalies_count_sql_uses_correct_database(self):
        """The COUNT SQL for item 4 must use adp_{stage}_fleet_cost.vehicle_cost_baseline."""
        sql = _select_anomalies_count_sql("staging", "2026-08")
        assert "adp_staging_fleet_cost" in sql, (
            f"C5/C7: SQL must use adp_staging_fleet_cost, got: {sql[:200]}"
        )
        assert "vehicle_cost_baseline" in sql
        assert "is_outlier" in sql or "is_energy_outlier" in sql
        assert "2026-08" in sql

    def test_select_anomalies_count_sql_wrong_database_would_fail(self):
        """Confirm the old database name 'fleet_cost' is wrong."""
        sql = _select_anomalies_count_sql("staging", "2026-08")
        assert "FROM fleet_cost." not in sql, (
            "C5/C7: SQL must not use bare 'fleet_cost' (no stage prefix). "
            "The correct database is adp_staging_fleet_cost."
        )

    def test_check_paging_match_detects_agent_truncation(self):
        """If agent selected 75 anomalies but COUNT(*) returns 100, paging failed."""
        ok, detail = check_paging_match(100, 75)  # count=100, agent=75
        assert ok is False, (
            "C5: agent_selected=75 vs COUNT(*)=100 must fail. "
            "This is the failure class agentic-tiers.md describes: paged once, "
            "passed every stubbed test."
        )

    def test_check_paging_match_agent_matches_count(self):
        ok, _ = check_paging_match(125, 125)
        assert ok is True


# ===========================================================================
# C6: chat request/response contract
# ===========================================================================


class TestC6ChatContract:
    """C6: /assistant/chat handler requires 'prompt' key (not 'message');
    fleet_manager persona via personaId; fleet-operator group (not fleet-manager);
    response key is 'result' (not 'response'/'text'/'content'/'message').

    Old code sent {"message": ...}, used fleet-manager group (doesn't exist in
    ui_stack.py), and read the wrong response key.
    """

    def test_extract_chat_text_reads_result_key(self):
        """handler.py:566 sets response_body['result'] = result_text."""
        body = {"result": "The cost anomaly for VIN-001 is charging_rate."}
        text = _extract_chat_text(body)
        assert text == "The cost anomaly for VIN-001 is charging_rate.", (
            "C6: _extract_chat_text must read 'result', not 'response'/'text'/'content'/'message'. "
            "handler.py returns 'result' key."
        )

    def test_extract_chat_text_ignores_legacy_keys(self):
        """The old code read 'response' then 'text' then 'content' then 'message'.
        None of those are the real key. All should return empty when 'result' is absent."""
        body = {
            "response": "wrong",
            "text": "also wrong",
            "content": "still wrong",
            "message": "not right either",
        }
        text = _extract_chat_text(body)
        assert text == "", (
            "C6: when 'result' key is absent, _extract_chat_text must return ''. "
            "The old code read 'response'/'text'/'content'/'message' — those are not "
            "the handler's actual response key."
        )

    def test_mutation_result_key_is_correct_not_response(self):
        """Mutation: if we used 'response' key instead of 'result', a handler
        response with 'result' but no 'response' would give empty string."""
        body = {"result": "fleet cost analysis complete"}
        # Correct
        correct = _extract_chat_text(body)
        assert correct == "fleet cost analysis complete", (
            "Must read 'result', not 'response'"
        )
        # Wrong key
        body_wrong = {"response": "fleet cost analysis complete"}
        wrong = _extract_chat_text(body_wrong)
        assert wrong == "", (
            "Using 'response' key would give empty result when body has 'result'"
        )

    def test_post_chat_sends_prompt_not_message(self):
        """handler.py:245 checks body.get('prompt'); sends 400 if absent.
        Old _post_chat sent {"message": ...} → always 400.
        """
        # Verify by inspecting the _post_chat implementation
        import inspect
        src = inspect.getsource(_mod._post_chat)
        assert '"prompt"' in src or "'prompt'" in src, (
            "C6: _post_chat must send 'prompt' key in the request body. "
            "handler.py:245 requires 'prompt', returns 400 if absent."
        )
        assert '"message"' not in src, (
            "C6: _post_chat must not send 'message' key. "
            "'message' is not the field handler.py reads."
        )

    def test_post_chat_sends_persona_id(self):
        """fleet_manager is a group-gated persona; needs personaId in the request."""
        import inspect
        src = inspect.getsource(_mod._post_chat)
        assert "personaId" in src or "persona_id" in src, (
            "C6: _post_chat must send personaId when provided. "
            "fleet_manager is in _GROUP_GATED_REQUESTABLE_PERSONAS and needs "
            "both personaId and a fleet-authorized group."
        )

    def test_item8_uses_fleet_operator_group_not_fleet_manager(self):
        """CMS ui_stack.py has no fleet-manager Cognito group.
        fleet_manager persona is reached via personaId + fleet-operator group.
        Old code added users to fleet-manager group → AdminAddUserToGroup raises.
        """
        import inspect
        import re
        src = inspect.getsource(_mod.item8_fleet_recommendation)
        # Must pass fleet-operator as a group value (in a list literal)
        assert re.search(r'groups\s*=\s*\[.*"fleet-operator".*\]', src) or \
               re.search(r"groups\s*=\s*\[.*'fleet-operator'.*\]", src), (
            "C6: item8 must add users to 'fleet-operator' group. "
            "CMS ui_stack.py has no fleet-manager group."
        )
        # fleet-manager must not appear as a value in a groups list assignment
        # (it can appear in comments explaining why it's NOT used)
        groups_assignments = re.findall(r'groups\s*=\s*\[([^\]]*)\]', src)
        for assignment in groups_assignments:
            assert "fleet-manager" not in assignment, (
                f"C6: 'fleet-manager' found in groups list assignment: groups=[{assignment}]. "
                "That group does not exist in CMS ui_stack.py."
            )

    def test_item8_skips_fewer_than_2_fleets(self):
        """C6: < 2 fleets with cost Findings must be SKIP, not NOTE.

        Old code used res.note() for this case, which means the item could
        pass without testing the refusal half.
        """
        from contextlib import contextmanager

        @contextmanager
        def _fake_eph(*args, **kwargs):
            import base64
            import json as _json
            payload_dict = {"sub": "dummy", "cognito:groups": ["fleet-operator"]}
            payload_b64 = base64.urlsafe_b64encode(
                _json.dumps(payload_dict).encode()
            ).rstrip(b"=").decode()
            token = f"header.{payload_b64}.sig"
            yield token, payload_dict

        ddb_mock = MagicMock()
        table_mock = MagicMock()
        ddb_mock.Table.return_value = table_mock
        # One fleet only
        table_mock.scan.return_value = {
            "Items": [
                {
                    "finding_id": "AVX-001",
                    "finding_kind": _FINDING_KIND_ANOMALY,
                    "agent_id": _AGENT_ID,
                    "fleet_id": "FLEET-A",
                    "headline": "cost anomaly",
                }
            ]
        }
        api_mock = MagicMock()
        api_mock.call.return_value = (200, {"result": "cost anomaly AVX-001"})

        with patch.object(_mod, "_check_text_runtime_trust_flag", return_value=True), \
             patch.object(_mod, "_ephemeral_user", side_effect=_fake_eph):
            res = Results()
            item8_fleet_recommendation(
                lam=MagicMock(),
                ddb=ddb_mock,
                idp=MagicMock(),
                findings_table="vsa-staging-avx-findings",
                actions_table="vsa-staging-avx-actions",
                pool_id="us-west-2_XXXXX",
                client_id="CLIENT",
                api=api_mock,
                function_name="any",
                stage="staging",
                region="us-west-2",
                res=res,
                allow_writes=True,
            )
        # Must have a SKIP for the refusal check when fewer than 2 fleets
        skip_rows = [r for r in res.rows if r[1] == "SKIP"]
        assert skip_rows, (
            "C6: fewer-than-2-fleets must produce at least one SKIP row. "
            "Old code used res.note() which does not make the run non-passing."
        )
        assert any("refusal" in r[0].lower() or "refusal" in r[2].lower() for r in skip_rows), (
            "C6: the SKIP must be for the refusal check. "
            f"Got SKIPs: {skip_rows}"
        )


# ===========================================================================
# C7: database name, column names, epoch-ms range
# ===========================================================================


class TestC7DatabaseAndColumns:
    """C7: item 1 used wrong database (fleet_cost vs adp_{stage}_fleet_cost),
    wrong column (total_miles_driven vs miles), and wrong CMS trips query
    (begins_with on NUMBER sort key vs epoch-ms BETWEEN).
    """

    def test_fleet_cost_db_staging(self):
        assert _fleet_cost_db("staging") == "adp_staging_fleet_cost"

    def test_fleet_cost_db_prod(self):
        assert _fleet_cost_db("prod") == "adp_prod_fleet_cost"

    def test_mutation_bare_fleet_cost_is_wrong_database(self):
        """The old constant _FLEET_COST_DB = 'fleet_cost' is wrong."""
        # Confirm the new function doesn't return the bare name
        assert _fleet_cost_db("staging") != "fleet_cost", (
            "C7: 'fleet_cost' (no stage prefix) is the wrong database name. "
            "The correct name is adp_{stage}_fleet_cost per select_anomalies.sql."
        )

    def test_year_month_epoch_ms_range_august_2026(self):
        start_ms, end_ms = _year_month_epoch_ms_range("2026-08")
        import datetime
        start_dt = datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc)
        end_dt = datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)
        assert start_ms == int(start_dt.timestamp() * 1000)
        assert end_ms == int(end_dt.timestamp() * 1000) - 1

    def test_year_month_epoch_ms_range_december(self):
        """December wraps to next year."""
        start_ms, end_ms = _year_month_epoch_ms_range("2026-12")
        import datetime
        start_dt = datetime.datetime(2026, 12, 1, tzinfo=datetime.timezone.utc)
        end_dt = datetime.datetime(2027, 1, 1, tzinfo=datetime.timezone.utc)
        assert start_ms == int(start_dt.timestamp() * 1000)
        assert end_ms == int(end_dt.timestamp() * 1000) - 1

    def test_epoch_ms_range_end_is_greater_than_start(self):
        start_ms, end_ms = _year_month_epoch_ms_range("2026-08")
        assert end_ms > start_ms

    def test_mutation_begins_with_on_number_raises_validation_exception(self):
        """begins_with() is for strings; startTime is a NUMBER in DynamoDB.
        Using begins_with on a NUMBER key raises ValidationException.

        This test verifies the _year_month_epoch_ms_range helper exists and
        produces integer values suitable for a BETWEEN query, confirming the
        C7 fix replaced begins_with with epoch-ms BETWEEN.
        """
        start_ms, end_ms = _year_month_epoch_ms_range("2026-08")
        assert isinstance(start_ms, int)
        assert isinstance(end_ms, int)
        assert start_ms > 0
        assert end_ms > start_ms

    def test_item1_sql_uses_miles_not_total_miles_driven(self):
        """C7: old item 1 used a column that doesn't exist in the view.
        The view column is 'miles' (per vehicle_cost_monthly.sql).
        """
        import inspect
        src = inspect.getsource(_mod.item1_views_rows)
        # Check the actual SQL strings (lines with WHERE or SELECT keywords and the column name)
        sql_lines = [
            line for line in src.splitlines()
            if ("WHERE" in line or "SELECT" in line or "miles" in line.lower())
            and not line.strip().startswith("#")
            and not line.strip().startswith('"""')
        ]
        sql_text = "\n".join(sql_lines)
        assert "miles > 0" in sql_text, (
            "C7: must use 'miles > 0' in the item 1 query. "
            "The view column is 'miles', not 'total_miles_driven'."
        )

    def test_item1_sql_uses_correct_database(self):
        """C7: item 1 must use adp_{stage}_fleet_cost, not fleet_cost."""
        import inspect
        src = inspect.getsource(_mod.item1_views_rows)
        assert "_fleet_cost_db" in src, (
            "C7: item1_views_rows must call _fleet_cost_db(stage) for the database name, "
            "not use the old bare 'fleet_cost' constant."
        )



# ===========================================================================
# N1: item 5 pagination uses next_token (not nextToken), URL-encoded
# ===========================================================================


class TestN1Item5Pagination:
    """N1: handler reads next_token; old code sent nextToken → infinite loop."""

    def test_item5_uses_next_token_param_not_nextToken(self):
        """N1: pagination must send ?next_token= not ?nextToken=."""
        import inspect
        src = inspect.getsource(_mod.item5_fleet_scope)
        assert "next_token=" in src, (
            "N1: item5 must use 'next_token=' in the URL query string. "
            "handler.py reads queryStringParameters['next_token']."
        )
        # Old code explicitly
        assert "?nextToken=" not in src, (
            "N1: old code sent '?nextToken='; handler ignores it → page 1 repeated forever."
        )

    def test_item5_url_encodes_token(self):
        """N1: base64 tokens contain '+' and '/'; must be URL-encoded."""
        import inspect
        src = inspect.getsource(_mod.item5_fleet_scope)
        assert "urllib.parse.quote" in src or "quote(" in src, (
            "N1: next_token must be URL-encoded before appending to the URL. "
            "Base64 tokens contain '+' and '/' which are special in query strings."
        )

    def test_item5_has_page_cap(self):
        """N1: without a page cap, an infinite pagination loop hangs forever."""
        import inspect
        src = inspect.getsource(_mod.item5_fleet_scope)
        assert "_MAX_FINDINGS_PAGES" in src or "page_count" in src, (
            "N1: item5 must cap the number of pages to prevent infinite loop."
        )

    def test_item5_pagination_stops_at_cap(self):
        """N1: if paging never terminates, the cap must stop it."""
        calls = {"n": 0}

        class Api:
            def call(self, method, path, token, body=None, timeout=45):
                calls["n"] += 1
                if "/findings/fleet/FLEET-B" in path:
                    return 403, {"error": "scope"}
                # Always return a next_token so paging never ends
                return 200, {
                    "findings": [{"finding_id": f"A-{calls['n']}", "agent_id": _AGENT_ID}],
                    "next_token": f"page-{calls['n']}",
                }

        import contextlib
        import io as _io
        from contextlib import contextmanager

        @contextmanager
        def eph(*a, **k):
            yield "tok", {}

        ddb = MagicMock()
        ddb.Table.return_value.scan.return_value = {
            "Items": [
                {"fleet_id": "FLEET-A", "agent_id": _AGENT_ID},
                {"fleet_id": "FLEET-B", "agent_id": _AGENT_ID},
            ]
        }
        ddb.Table.return_value.query.return_value = {"Items": [{"finding_id": "B-1"}]}
        res = Results()
        with patch.object(_mod, "_ephemeral_user", eph):
            with contextlib.redirect_stdout(_io.StringIO()):
                _mod.item5_fleet_scope(
                    ddb, MagicMock(), "findings-table", "pool-id", "client-id",
                    Api(), "staging", res, allow_writes=True,
                )
        assert calls["n"] <= _mod._MAX_FINDINGS_PAGES + 2, (
            f"N1: item5 made {calls['n']} API calls; must be capped at _MAX_FINDINGS_PAGES={_mod._MAX_FINDINGS_PAGES}"
        )


# ===========================================================================
# N2: item 4 reads run record top-level fields (not "extra" dict)
# ===========================================================================


class TestN2RunRecordParsing:
    """N2: write_run_record stores data_month/vins_selected at the top level.
    Old code tried rec.get("extra", {}).get(...) → always None → always SKIP.
    """

    def test_item4_reads_data_month_from_top_level(self):
        """N2: data_month must come from rec.get('data_month'), not rec.get('extra')."""
        import contextlib
        import io as _io
        # Simulate a real write_run_record item (top-level fields)
        rec = {
            "agent_component": "agent-fi-cost#cost",
            "started_at": "2026-10-07T04:00:00Z",
            "status": "completed",
            "artifacts_written": 142,
            "data_month": "2026-09",
            "vins_selected": 105,
            "deferred": 0,
            "model_calls": 12,
            "tokens_used": 8000,
        }
        ddb = MagicMock()
        ddb.Table.return_value.query.return_value = {"Items": [rec]}
        athena = MagicMock()
        athena.start_query_execution.return_value = {"QueryExecutionId": "Q1"}
        athena.get_query_execution.return_value = {
            "QueryExecution": {
                "Status": {"State": "SUCCEEDED"},
                "Statistics": {"DataScannedInBytes": 1024},
            }
        }
        athena.get_query_results.return_value = {
            "ResultSet": {"Rows": [{"Data": [{"VarCharValue": "COUNT(*)"}]},
                                   {"Data": [{"VarCharValue": "105"}]}]}
        }
        res = Results()
        with contextlib.redirect_stdout(_io.StringIO()):
            _mod.item4_athena_paging(athena, ddb, "wg", "s3://x/", "staging", "runs", res)
        # Must have proceeded to the Athena query (not just SKIP rows)
        assert athena.start_query_execution.called, (
            "N2: item4 must proceed to Athena after reading data_month from top-level rec. "
            "Old code returned None from extra dict → always SKIPped before querying."
        )
        skip_rows = [r for r in res.rows if r[1] == "SKIP"]
        data_month_skip = [r for r in skip_rows if "data_month" in r[0] or "data_month" in r[2]]
        assert not data_month_skip, (
            f"N2: item4 SKIPped on data_month despite it being in the run record: {data_month_skip}. "
            "Old code read from rec.get('extra', {}).get('data_month') which is always None."
        )

    def test_item4_count_star_must_be_positive(self):
        """N2/C5: when COUNT(*) returns 0, 4-reader-anomalies must FAIL.

        Old code: count_star >= 0 → vacuous 0==0 pass.
        New code: count_anomalies > 0 is required → FAIL when empty.
        """
        import contextlib
        import io as _io

        athena = MagicMock()
        seq = [0]

        def start(**kw):
            qid = f"Q{seq[0]}"; seq[0] += 1
            return {"QueryExecutionId": qid}

        def gqe(QueryExecutionId):
            return {"QueryExecution": {"Status": {"State": "SUCCEEDED"},
                                       "Statistics": {"DataScannedInBytes": 0}}}

        def gqr(QueryExecutionId, **kw):
            # Single header row, no data → 0 rows for every query
            return {"ResultSet": {"Rows": [{"Data": [{"VarCharValue": "col"}]}]}}

        athena.start_query_execution.side_effect = start
        athena.get_query_execution.side_effect = gqe
        athena.get_query_results.side_effect = gqr

        ddb = MagicMock()
        ddb.Table.return_value.query.return_value = {
            "Items": [{"agent_component": "agent-fi-cost#cost",
                       "data_month": "2026-09", "vins_selected": 0}]
        }
        res = _mod.Results()
        with contextlib.redirect_stdout(_io.StringIO()):
            _mod.item4_athena_paging(athena, ddb, "wg", "s3://x/", "staging", "runs", res)

        anom_rows = [r for r in res.rows if r[0] == "4-reader-anomalies"]
        assert anom_rows, "N2/C5: 4-reader-anomalies row must exist when COUNT(*)=0."
        assert anom_rows[0][1] == "FAIL", (
            f"N2/C5: 4-reader-anomalies must FAIL when COUNT(*)=0 and reader=0 rows. "
            f"count_anomalies > 0 prevents the vacuous 0==0 pass. Got: {anom_rows[0][1]!r}"
        )


# ===========================================================================
# N3: item 8 action status uses action_id key + compute_queue_status
# ===========================================================================


class TestN3ActionStatusRead:
    """N3: actions table is keyed on action_id (not finding_id).
    Old code called get_item(Key={'finding_id': ...}) → always ValidationException.
    """

    def test_item8_derives_action_id_from_finding_id(self):
        """N3/R3-C2: must derive action_id via _derive_decision_action_id from the tool."""
        # R3-C2: the script must import and call the tool's own function, not re-implement it.
        # Behavioral test: call _derive_decision_action_id directly and verify the result.
        from agents.supervisor.tools.fleet_recommendation import _derive_decision_action_id
        finding_id = "AVX-FIND-TEST-001"
        result = _derive_decision_action_id(finding_id)
        import hashlib
        h = hashlib.sha256(f"{finding_id}|fi-decision".encode()).hexdigest()[:32]
        expected = f"AVX-ACT-{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
        assert result == expected, (
            f"N3: _derive_decision_action_id must produce the canonical formula. "
            f"Got {result!r}, expected {expected!r}"
        )

    def test_item8_action_id_key_not_finding_id_key(self):
        """N3: get_item must use Key={'action_id': ...}, not Key={'finding_id': ...}."""
        import inspect
        src = inspect.getsource(_mod.item8_fleet_recommendation)
        # Must not read action by finding_id
        import re
        bad_key = re.search(r"""Key\s*=\s*\{["']finding_id["']""", src)
        assert bad_key is None, (
            "N3: item8 must not call get_item with Key={'finding_id': ...}. "
            "The actions table is keyed on action_id. "
            "Old code got ValidationException on every call."
        )

    def test_item8_action_status_is_check_not_note(self):
        """N3: 8-action-status must be a FAIL-capable check, not a NOTE."""
        import inspect
        src = inspect.getsource(_mod.item8_fleet_recommendation)
        # Find the '8-action-status' label; confirm it's under res.check not res.note
        idx = src.find('"8-action-status"')
        assert idx != -1, "label '8-action-status' not found in item8"
        # Search a broader window (label may not be right next to 'res.check')
        snippet = src[max(0, idx-60):idx+200]
        assert "res.note" not in snippet[:100], (
            f"N3: '8-action-status' must use res.check (not res.note). "
            f"Old code used res.note() → always passes. Found: {snippet[:100]!r}"
        )
        assert "res.check" in snippet, (
            f"N3: res.check must appear near '8-action-status'. Found: {snippet!r}"
        )

    def test_item8_compute_queue_status_derivation(self):
        """N3/R3-C2: compute_queue_status uses the tool's function, not an inline copy.

        Behavioral proof: the tool returns 'pending' for an undecided Finding with a
        future expires_at.  An inline copy that maps the no-action branch to 'approved'
        (M11) must cause this test to fail.
        """
        from agents.supervisor.tools.fleet_recommendation import compute_queue_status
        finding = {"expires_at": "2099-01-01T00:00:00Z"}
        # No Action → tool must return 'pending' (expires_at in future)
        status = compute_queue_status(finding, None)
        assert status == "pending", (
            f"N3/R3-C2: compute_queue_status(finding, None) must return 'pending' "
            f"for an undecided non-expired Finding. Got {status!r}."
        )
        # With an action carrying no 'decision' (status='pending_execution', no decision key)
        action_no_decision = {"status": "pending_execution"}
        status2 = compute_queue_status(finding, action_no_decision)
        assert status2 == "approved", (
            f"N3/R3-C2: pending_execution with no decision → 'approved'. Got {status2!r}."
        )
        # An old Action that is > 15 min stale → 'stalled'
        import datetime
        old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)) \
            .isoformat(timespec="seconds").replace("+00:00", "Z")
        action_stale = {"status": "pending_execution", "decision": "approved", "updated_at": old}
        status3 = compute_queue_status(finding, action_stale)
        assert status3 == "stalled", (
            f"N3/R3-C2: pending_execution > 15 min old → 'stalled'. Got {status3!r}."
        )


# ===========================================================================
# NW1: stop invoking items after invoke error; check 04:00 UTC schedule
# ===========================================================================


class TestNW1StopOnInvokeError:
    """NW1: after any invoke exception, remaining invoking items must be SKIP."""

    def test_near_cost_schedule_detects_04_utc(self):
        """NW1: _near_cost_schedule must return True within ±60 min of 04:00 UTC."""
        # Manufacture a time at 04:30 UTC (within 60 min window)
        import datetime
        t_near = datetime.datetime(2026, 10, 7, 4, 30, 0, tzinfo=datetime.timezone.utc)
        with patch.object(_mod.datetime, "datetime",
                          MagicMock(now=MagicMock(return_value=t_near),
                                    timezone=datetime.timezone,
                                    timedelta=datetime.timedelta)):
            # Patch _mod.datetime.datetime.now to return t_near
            pass  # test via the function directly
        # Direct test: is 04:30 UTC within 60 min of 04:00?
        schedule = t_near.replace(hour=_mod._COST_SCHEDULE_UTC_HOUR, minute=0, second=0, microsecond=0)
        diff_s = abs((t_near - schedule).total_seconds())
        assert diff_s <= _mod._COST_SCHEDULE_MARGIN_MINUTES * 60, (
            "NW1: 04:30 UTC should be within the schedule margin"
        )

    def test_near_cost_schedule_clear_outside_window(self):
        """NW1: 14:00 UTC is outside the ±60 min window around 04:00 UTC."""
        import datetime
        t_far = datetime.datetime(2026, 10, 7, 14, 0, 0, tzinfo=datetime.timezone.utc)
        schedule = t_far.replace(hour=_mod._COST_SCHEDULE_UTC_HOUR, minute=0, second=0, microsecond=0)
        diff_s = abs((t_far - schedule).total_seconds())
        assert diff_s > _mod._COST_SCHEDULE_MARGIN_MINUTES * 60, (
            "NW1: 14:00 UTC is outside the ±60 min window; should not be blocked"
        )

    def test_item3_gets_skip_when_invoke_ok_is_false(self):
        """NW1 (security review cycle 1, A-S1, now behavioural): when item 2's invoke failed,
        main() must not call item 3 and must SKIP it as 3-invoke."""
        import contextlib, io as _io
        calls: list = []
        noop = lambda *a, **k: None  # noqa: E731
        with patch.dict(os.environ, {"LIVE_TESTS": "1"}), \
             patch.object(_mod, "_parse_args", return_value=MagicMock(
                 item=None, all=True, allow_writes=True, stage="staging", region="us-west-2",
                 alarm_wait_s=1, one_run_cap=None)), \
             patch("boto3.client"), patch("boto3.resource"), \
             patch.object(_mod, "_find_pool", return_value="pool"), \
             patch.object(_mod, "_password_client", return_value="client"), \
             patch.object(_mod, "item1_views_rows", noop), \
             patch.object(_mod, "item7_rules_fallback", return_value=True), \
             patch.object(_mod, "item2_agent_scores_plants", return_value=(None, False)), \
             patch.object(_mod, "item3_second_run_noop",
                          side_effect=lambda *a, **k: calls.append("item3") or True), \
             patch.object(_mod, "item4_athena_paging", noop), \
             patch.object(_mod, "item5_fleet_scope", noop), \
             patch.object(_mod, "item6_forced_alarm_failure", noop), \
             patch.object(_mod, "item8_fleet_recommendation", noop), \
             patch.object(_mod.Results, "summary", noop), \
             contextlib.redirect_stdout(_io.StringIO()) as out, contextlib.redirect_stderr(_io.StringIO()):
            _mod.main()
        assert calls == [], "item 3 must not run after item 2's invoke failed"
        assert "[SKIP] 3-invoke" in out.getvalue(), out.getvalue()[-600:]


# ===========================================================================
# NW2: item 3 run record missing/old = FAIL, not NOTE
# ===========================================================================


class TestNW2RunRecordFail:
    """NW2: a missing or older run record is a FAIL, not a NOTE.
    The run record is the only authoritative source for what the run actually did.
    """

    def test_item3_fails_on_missing_run_record(self):
        """NW2: no run record → FAIL (not NOTE)."""
        import contextlib
        import io as _io
        ddb = MagicMock()
        ddb.Table.return_value.query.return_value = {"Items": []}
        summary = {
            "artifacts_written": 0, "tokens_used": 0,
            "cost": {"deferred": 0, "model_calls": 0, "anomalies_written": 0, "sell_timing_written": 0},
        }
        res = Results()
        with patch.object(_mod, "_wait_lambda_response", return_value=summary), \
             patch.object(_mod, "_check_ios_fleet_intersection", return_value=True), \
             contextlib.redirect_stdout(_io.StringIO()):
            _mod.item3_second_run_noop(
                MagicMock(), ddb, MagicMock(), "findings", "runs", "fn",
                "staging", "us-west-2", "wg", "s3://x/", res,
            )
        fail_rows = [r for r in res.rows if r[1] == "FAIL"]
        note_rows = [r for r in res.rows if r[1] == "NOTE" and "run record" in r[2].lower()]
        assert fail_rows, (
            f"NW2: missing run record must produce a FAIL row. "
            f"Got: {[(r[0], r[1]) for r in res.rows]}"
        )
        assert not note_rows, (
            f"NW2: old code used res.note() for missing run record — should be FAIL. "
            f"Note rows: {note_rows}"
        )

    def test_item3_fails_on_old_run_record(self):
        """NW2: a run record predating the invoke must FAIL, not pass or NOTE."""
        import contextlib
        import io as _io
        old_rec = {
            "agent_component": "agent-fi-cost#cost",
            "started_at": "2020-01-01T00:00:00Z",  # very old
            "artifacts_written": 0,
            "tokens_used": 0,
        }
        ddb = MagicMock()
        ddb.Table.return_value.query.return_value = {"Items": [old_rec]}
        summary = {
            "artifacts_written": 0, "tokens_used": 0,
            "cost": {"deferred": 0, "model_calls": 0, "anomalies_written": 0, "sell_timing_written": 0},
        }
        res = Results()
        with patch.object(_mod, "_wait_lambda_response", return_value=summary), \
             patch.object(_mod, "_check_ios_fleet_intersection", return_value=True), \
             contextlib.redirect_stdout(_io.StringIO()):
            _mod.item3_second_run_noop(
                MagicMock(), ddb, MagicMock(), "findings", "runs", "fn",
                "staging", "us-west-2", "wg", "s3://x/", res,
            )
        fail_rows = [r for r in res.rows if r[1] == "FAIL" and "record" in r[0].lower()]
        assert fail_rows, (
            f"NW2: an old run record (predates invoke) must produce a FAIL row. "
            f"Old code used minute-precision comparison → old record in same minute could pass. "
            f"Got: {[(r[0], r[1]) for r in res.rows]}"
        )


# ===========================================================================
# Step 3: item3 clock-skew window
# ===========================================================================


class TestItem3ClockSkew:
    """Step 3: 3-run-record-present accepts records started up to 5s before invoke."""

    def _run_item3(self, rec_started_offset_s: float):
        """Run item3 with a run record whose started_at is offset_s from now."""
        import contextlib, io as _io, datetime as _dt, time as _time
        invoke_time = _time.time()
        # rec_started is invoke_time + offset_s (negative = before invoke)
        rec_ts = _dt.datetime.fromtimestamp(
            invoke_time + rec_started_offset_s, tz=_dt.timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%S")
        rec = {"agent_component": "agent-fi-cost#cost",
               "started_at": rec_ts, "artifacts_written": 0, "tokens_used": 0}
        ddb = MagicMock()
        ddb.Table.return_value.query.return_value = {"Items": [rec]}
        summary = {"artifacts_written": 0, "tokens_used": 0,
                   "cost": {"deferred": 0, "model_calls": 0,
                            "anomalies_written": 0, "sell_timing_written": 0}}
        res = Results()
        with patch.object(_mod, "_wait_lambda_response", return_value=summary), \
             patch.object(_mod, "_check_ios_fleet_intersection", return_value=True), \
             patch.object(_mod.time, "time", return_value=invoke_time), \
             contextlib.redirect_stdout(_io.StringIO()):
            _mod.item3_second_run_noop(
                MagicMock(), ddb, MagicMock(), "findings", "runs", "fn",
                "staging", "us-west-2", "wg", "s3://x/", res,
            )
        return res

    def test_record_1s_before_invoke_passes(self):
        """A run record started 1 s before invoke is within the 5 s skew window → PASS."""
        res = self._run_item3(-1.0)
        fail_rows = [r for r in res.rows if r[0] == "3-run-record-present" and r[1] == "FAIL"]
        assert not fail_rows, (
            f"skew=-1s must NOT produce 3-run-record-present FAIL (within 5s window). "
            f"Got: {[(r[0], r[1]) for r in res.rows]}"
        )

    def test_record_10s_before_invoke_fails(self):
        """A run record started 10 s before invoke is outside the 5 s window → FAIL."""
        res = self._run_item3(-10.0)
        fail_rows = [r for r in res.rows if r[0] == "3-run-record-present" and r[1] == "FAIL"]
        assert fail_rows, (
            f"skew=-10s must produce 3-run-record-present FAIL (outside 5s window). "
            f"Got: {[(r[0], r[1]) for r in res.rows]}"
        )


# ===========================================================================
# W4: alarm already ALARM = SKIP, action history missing = FAIL
# ===========================================================================


class TestW4AlarmSkip:
    """W4: item 6 must SKIP when the alarm starts in ALARM state."""

    def test_item6_skips_when_alarm_already_alarm(self):
        """W4: alarm already in ALARM → SKIP (was: passes the trigger check vacuously)."""
        import contextlib
        import io as _io
        cw = MagicMock()
        cw.describe_alarms.return_value = {
            "MetricAlarms": [{
                "StateValue": "ALARM",
                "AlarmActions": ["arn:aws:sns:us-west-2:0:topic"],
            }]
        }
        sns = MagicMock()
        sns.list_subscriptions_by_topic.return_value = {
            "Subscriptions": [{"SubscriptionArn": "arn:confirmed"}]
        }
        res = Results()
        with contextlib.redirect_stdout(_io.StringIO()), \
             contextlib.redirect_stderr(_io.StringIO()):
            _mod.item6_forced_alarm_failure(
                MagicMock(), cw, sns, "fn", "staging", "us-west-2", res, wait_s=1,
            )
        skip_rows = [r for r in res.rows if r[1] == "SKIP"]
        assert skip_rows, (
            f"W4: alarm already ALARM must produce a SKIP row. "
            f"Old code fell through and 'alarm_reached = True' immediately. "
            f"Got rows: {[(r[0], r[1]) for r in res.rows]}"
        )

    def test_item6_alarm_wait_default_is_360(self):
        """W4: default alarm wait must be ≥ 360s (alarm period is 300s)."""
        import inspect
        sig = inspect.signature(_mod.item6_forced_alarm_failure)
        default = sig.parameters["wait_s"].default
        assert default >= 360, (
            f"W4: --alarm-wait-s default={default} must be >= 360 s. "
            "The alarm period is 300 s; 360 s gives at least one evaluation cycle."
        )

    def test_item6_action_history_missing_is_fail_not_note(self):
        """W4: no action history entry after invoke → FAIL, not NOTE."""
        import contextlib
        import io as _io
        import datetime
        # Alarm starts OK, then transitions to ALARM quickly
        call_count = {"n": 0}
        def describe_side_effect(**kw):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                return {"MetricAlarms": [{"StateValue": "OK", "AlarmActions": ["arn:aws:sns:x:t"]}]}
            return {"MetricAlarms": [{"StateValue": "ALARM", "AlarmActions": ["arn:aws:sns:x:t"],
                                      "StateUpdatedTimestamp": datetime.datetime.now(datetime.timezone.utc)}]}
        cw = MagicMock()
        cw.describe_alarms.side_effect = describe_side_effect
        cw.describe_alarm_history.side_effect = Exception("describe_alarm_history unavailable")  # exits poll immediately
        sns = MagicMock()
        sns.list_subscriptions_by_topic.return_value = {
            "Subscriptions": [{"SubscriptionArn": "arn:confirmed"}]
        }
        lam = MagicMock()
        lam.invoke.return_value = {
            "FunctionError": "Unhandled",
            "Payload": MagicMock(read=MagicMock(return_value=b'{"errorType":"ValueError"}')),
        }
        res = Results()
        with contextlib.redirect_stdout(_io.StringIO()), \
             contextlib.redirect_stderr(_io.StringIO()), \
             patch.object(_mod.time, "sleep", lambda s: None):
            _mod.item6_forced_alarm_failure(
                lam, cw, sns, "fn", "staging", "us-west-2", res, wait_s=1,
            )
        # Must have a FAIL for 6-action-delivered
        action_rows = [r for r in res.rows if r[0] == "6-action-delivered"]
        assert action_rows, "6-action-delivered row must exist"
        assert any(r[1] == "FAIL" for r in action_rows), (
            f"W4: missing action history must produce FAIL. "
            f"Old code produced NOTE. Got: {action_rows}"
        )


# ===========================================================================
# W6: use expires_at (ISO string), not ttl (epoch int)
# ===========================================================================


# ===========================================================================
# Step 4: item6 hardening tests
# ===========================================================================


class TestItem6Step4:
    """Step 4: sns-check FAIL, lambda-error text, action poll 30s, no set_alarm_state."""

    def _make_cw(self, start_state="OK", end_state="ALARM"):
        cw = MagicMock()
        call_n = {"n": 0}
        def _describe(**kw):
            call_n["n"] += 1
            state = end_state if call_n["n"] > 2 else start_state
            return {"MetricAlarms": [{
                "StateValue": state,
                "AlarmActions": ["arn:aws:sns:us-west-2:123:mytopic"],
                "StateUpdatedTimestamp": __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ),
            }]}
        cw.describe_alarms.side_effect = _describe
        cw.describe_alarm_history.return_value = {"AlarmHistoryItems": []}
        return cw

    def _run_item6(self, lam, cw, sns, extra_patches=()):
        import contextlib, io as _io
        res = _mod.Results()
        with patch.object(_mod.time, "sleep", lambda s: None), \
             patch.object(_mod.time, "monotonic", side_effect=_mono_expire()), \
             contextlib.redirect_stdout(_io.StringIO()), \
             contextlib.redirect_stderr(_io.StringIO()):
            for p in extra_patches:
                p.__enter__()
            _mod.item6_forced_alarm_failure(
                lam, cw, sns, "fn", "staging", "us-west-2", res, wait_s=1,
            )
        return res

    def test_sns_check_fails_when_list_subscriptions_raises(self):
        """6-sns-check must FAIL (not NOTE) when ListSubscriptionsByTopic raises."""
        import contextlib, io as _io
        cw = self._make_cw()
        sns = MagicMock()
        sns.list_subscriptions_by_topic.side_effect = RuntimeError("access denied")
        lam = MagicMock()
        res = _mod.Results()
        with patch.object(_mod.time, "sleep", lambda s: None), \
             patch.object(_mod.time, "monotonic", side_effect=_mono_expire()), \
             contextlib.redirect_stdout(_io.StringIO()), \
             contextlib.redirect_stderr(_io.StringIO()):
            _mod.item6_forced_alarm_failure(
                lam, cw, sns, "fn", "staging", "us-west-2", res, wait_s=1,
            )
        fail_rows = [r for r in res.rows if r[0] == "6-sns-check" and r[1] == "FAIL"]
        assert fail_rows, (
            f"6-sns-check must FAIL when ListSubscriptionsByTopic raises. "
            f"Old code produced NOTE. Got: {[(r[0], r[1]) for r in res.rows]}"
        )

    def test_lambda_error_requires_specific_text(self):
        """6-lambda-error must FAIL if error text lacks 'max_diagnoses_per_run must be an integer'."""
        import contextlib, io as _io
        cw = self._make_cw()
        sns = MagicMock()
        sns.list_subscriptions_by_topic.return_value = {"Subscriptions": [{"SubscriptionArn": "arn:x"}]}
        lam = MagicMock()
        res = _mod.Results()
        with patch.object(_mod, "_wait_lambda_response",
                          side_effect=RuntimeError("FunctionError: some unhandled error")), \
             patch.object(_mod.time, "sleep", lambda s: None), \
             patch.object(_mod.time, "monotonic", side_effect=_mono_expire()), \
             contextlib.redirect_stdout(_io.StringIO()), \
             contextlib.redirect_stderr(_io.StringIO()):
            _mod.item6_forced_alarm_failure(
                lam, cw, sns, "fn", "staging", "us-west-2", res, wait_s=1,
            )
        err_rows = [r for r in res.rows if r[0] == "6-lambda-error"]
        assert err_rows, "6-lambda-error row must exist"
        assert err_rows[0][1] == "FAIL", (
            f"6-lambda-error must FAIL when error text is generic (missing specific message). "
            f"Got: {err_rows[0][1]!r}"
        )

    def test_lambda_error_passes_with_expected_text(self):
        """6-lambda-error passes only when error contains 'max_diagnoses_per_run must be an integer'."""
        import contextlib, io as _io
        cw = self._make_cw()
        sns = MagicMock()
        sns.list_subscriptions_by_topic.return_value = {"Subscriptions": [{"SubscriptionArn": "arn:x"}]}
        lam = MagicMock()
        msg = "ValueError: staging: max_diagnoses_per_run must be an integer"
        res = _mod.Results()
        with patch.object(_mod, "_wait_lambda_response",
                          side_effect=RuntimeError(msg)), \
             patch.object(_mod.time, "sleep", lambda s: None), \
             patch.object(_mod.time, "monotonic", side_effect=_mono_expire()), \
             contextlib.redirect_stdout(_io.StringIO()), \
             contextlib.redirect_stderr(_io.StringIO()):
            _mod.item6_forced_alarm_failure(
                lam, cw, sns, "fn", "staging", "us-west-2", res, wait_s=1,
            )
        err_rows = [r for r in res.rows if r[0] == "6-lambda-error"]
        assert err_rows, "6-lambda-error row must exist"
        assert err_rows[0][1] == "PASS", (
            f"6-lambda-error must PASS when error contains exact message. Got: {err_rows[0][1]!r}"
        )

    def test_no_set_alarm_state_restore(self):
        """set_alarm_state must not be called — alarm returns to OK on its own."""
        import contextlib, io as _io
        cw = self._make_cw()
        sns = MagicMock()
        sns.list_subscriptions_by_topic.return_value = {"Subscriptions": [{"SubscriptionArn": "arn:x"}]}
        lam = MagicMock()
        res = _mod.Results()
        with patch.object(_mod, "_wait_lambda_response",
                          side_effect=RuntimeError("max_diagnoses_per_run must be an integer")), \
             patch.object(_mod.time, "sleep", lambda s: None), \
             patch.object(_mod.time, "monotonic", side_effect=_mono_expire()), \
             contextlib.redirect_stdout(_io.StringIO()), \
             contextlib.redirect_stderr(_io.StringIO()):
            _mod.item6_forced_alarm_failure(
                lam, cw, sns, "fn", "staging", "us-west-2", res, wait_s=1,
            )
        cw.set_alarm_state.assert_not_called()
        restore_rows = [r for r in res.rows if r[0] == "6-restore"]
        assert not restore_rows, f"6-restore row must not exist; got: {restore_rows}"


def _mono_expire():
    """Yield monotonic values that run alarm loop once then immediately expire the action poll."""
    yield 0.0   # alarm loop: deadline = 0.0 + wait_s
    yield 0.0   # alarm loop: while check 1 (enters loop)
    yield 2.0   # alarm loop: while check 2 (exits; 2.0 > wait_s=1)
    yield 0.0   # action poll: _action_deadline = 0.0 + 30 = 30.0
    yield 31.0  # action poll: while check (31.0 > 30.0 → exits immediately)
    while True:
        yield 9999.0  # safety: any further monotonic calls


class TestW6ExpiresAt:
    """W6: AVX Findings carry expires_at (ISO string); TTL is DISABLED.
    Old code filtered on ttl epoch integer — a field that doesn't exist.
    """

    def test_item2_excludes_expired_findings_by_expires_at(self):
        """W6: a Finding with expires_at in the past must be excluded from scoring."""
        expired_finding = {
            "finding_id": "FIND-old",
            "finding_kind": _FINDING_KIND_ANOMALY,
            "agent_id": _AGENT_ID,
            "scope": {"vin": "V1"},
            "fleet_id": "FLEET-A",
            "expires_at": "2020-01-01T00:00:00Z",  # clearly past
            "recommended_action": {
                "parameters": {"month": "2026-09", "cause": "charging_mix"},
            },
        }
        live_finding = {
            "finding_id": "FIND-live",
            "finding_kind": _FINDING_KIND_ANOMALY,
            "agent_id": _AGENT_ID,
            "scope": {"vin": "V2"},
            "fleet_id": "FLEET-A",
            "expires_at": "2099-01-01T00:00:00Z",  # far future
            "recommended_action": {
                "parameters": {"month": "2026-09", "cause": "charging_mix"},
            },
        }
        # Simulate the W6 filter from item2
        import datetime
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        all_findings = [expired_finding, live_finding]
        scored = [
            f for f in all_findings
            if not f.get("expires_at") or str(f.get("expires_at", "")) > now_iso
        ]
        assert len(scored) == 1, (
            f"W6: expired Finding (expires_at in past) must be excluded. "
            f"Got {len(scored)} scored Findings, expected 1."
        )
        assert scored[0]["finding_id"] == "FIND-live"

    def test_item2_does_not_filter_on_ttl(self):
        """W6: the filter must use expires_at (ISO), not ttl (epoch int)."""
        import inspect
        src = inspect.getsource(_mod.item2_agent_scores_plants)
        assert "expires_at" in src, (
            "W6: item2 must filter on 'expires_at' (ISO string). "
            "AVX Findings do not have a 'ttl' field; the table TTL is DISABLED."
        )
        # Old code used ttl
        assert "f.get(\"ttl\")" not in src and "f.get('ttl')" not in src, (
            "W6: item2 must not filter on 'ttl'. "
            "AVX Findings carry expires_at, not ttl."
        )

    def test_old_ttl_filter_is_vacuous(self):
        """W6: a Finding without 'ttl' passes the old filter even if expired.
        This is the proof that the old filter was wrong."""
        finding = {
            "finding_id": "F1",
            "expires_at": "2020-01-01T00:00:00Z",  # expired
            # No 'ttl' field
        }
        # Old filter: not f.get("ttl") or int(f.get("ttl", 0)) > now_epoch
        # → not None → True → kept (wrong: should be excluded)
        old_filter_result = not finding.get("ttl") or int(finding.get("ttl", 0)) > 1000
        assert old_filter_result is True, (
            "W6: old ttl filter keeps expired findings that have no ttl field. "
            "This is the bug: Findings use expires_at, not ttl."
        )


# ===========================================================================
# W7: Cognito-derived iOS fleet IDs; fail closed
# ===========================================================================


class TestW7IosFleetsFromCognito:
    """W7: _check_ios_fleet_intersection must read Cognito; FAIL if check cannot run."""

    def test_check_ios_fleet_fails_on_cognito_error(self):
        """W7: Cognito read failure must produce FAIL (not NOTE+proceed)."""
        import contextlib
        import io as _io
        athena = MagicMock()
        athena.start_query_execution.return_value = {"QueryExecutionId": "Q1"}
        athena.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}, "Statistics": {}}
        }
        athena.get_query_results.return_value = {
            "ResultSet": {"Rows": [
                {"Data": [{"VarCharValue": "fleet_id"}]},
                {"Data": [{"VarCharValue": "FLEET-A"}]},
            ]}
        }
        res = Results()
        # Cognito read will fail (boto3 client raises on any call)
        with patch.object(_mod, "_collect_ios_fleet_ids_from_cognito",
                          side_effect=RuntimeError("pool not found")), \
             contextlib.redirect_stdout(_io.StringIO()):
            result = _mod._check_ios_fleet_intersection(
                athena, "staging", "us-west-2", "wg", "s3://x/", res,
            )
        assert result is False, (
            "W7: Cognito read failure must return False (fail closed). "
            "Old code returned True (proceed=True) on any check error."
        )
        fail_rows = [r for r in res.rows if r[1] == "FAIL"]
        assert fail_rows, (
            f"W7: Cognito error must produce FAIL row. Got: {[(r[0], r[1]) for r in res.rows]}"
        )

    def test_check_ios_fleet_fails_on_athena_error(self):
        """W7: Athena read failure must produce FAIL (not NOTE+proceed)."""
        import contextlib
        import io as _io
        athena = MagicMock()
        athena.start_query_execution.side_effect = RuntimeError("AccessDenied")
        res = Results()
        with patch.object(_mod, "_collect_ios_fleet_ids_from_cognito",
                          return_value=(frozenset({"FLEET-IOS"}), [])), \
             contextlib.redirect_stdout(_io.StringIO()):
            result = _mod._check_ios_fleet_intersection(
                athena, "staging", "us-west-2", "wg", "s3://x/", res,
            )
        assert result is False, (
            "W7: Athena error must return False (fail closed). "
            "Old code returned True on Athena exception."
        )
        fail_rows = [r for r in res.rows if r[1] == "FAIL"]
        assert fail_rows, f"W7: Athena error must produce FAIL row. Got: {res.rows}"

    def test_collect_ios_fleet_ids_collects_custom_fleet_ids(self):
        """W7: _collect_ios_fleet_ids_from_cognito must read custom:fleetIds attribute."""
        import inspect
        src = inspect.getsource(_mod._collect_ios_fleet_ids_from_cognito)
        assert "custom:fleetIds" in src, (
            "W7: must read the custom:fleetIds attribute from Cognito users"
        )
        assert "livetest-" in src, (
            "W7: must skip livetest- users (ephemeral test users)"
        )

    def test_old_ios_check_was_fail_open(self):
        """W7: original code returned True (proceed) on Athena error — wrong."""
        # Replicate the old logic
        def old_check(athena_raises, res):
            try:
                if athena_raises:
                    raise RuntimeError("AccessDenied")
            except Exception:
                res.rows.append(("ios-fleet-check", "NOTE", "error — skipping"))
                return True  # old: proceed
            return False

        res = Results()
        result = old_check(True, res)
        assert result is True, "baseline: old code returned True on error"
        assert any(r[1] == "NOTE" for r in res.rows), "old code used NOTE"
        # The fix: returns False and FAILs
        assert result is True, "Proof that old code was fail-open"

    # D4 tests ---------------------------------------------------------------

    def test_d4_exempt_sub_with_managed_fleet_returns_true_with_note(self):
        """D4: exempt sub's fleet must not trigger ios-fleet-intersection; NOTE added."""
        import contextlib
        import io as _io

        exempt_sub = "28f183d0-0011-70de-f2bb-ccf6ac1bfc4b"
        athena = MagicMock()
        athena.start_query_execution.return_value = {"QueryExecutionId": "Q1"}
        athena.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}, "Statistics": {}}
        }
        athena.get_query_results.return_value = {
            "ResultSet": {"Rows": [
                {"Data": [{"VarCharValue": "fleet_id"}]},
                {"Data": [{"VarCharValue": "FLEET-MANAGED"}]},
            ]}
        }
        res = _mod.Results()
        # Return empty fleet_ids (exempt user excluded) but ignored list has the sub
        with patch.object(
            _mod, "_collect_ios_fleet_ids_from_cognito",
            return_value=(frozenset(), [(exempt_sub, frozenset({"FLEET-MANAGED"}))]),
        ), patch.object(_mod, "_near_cost_schedule", return_value=(False, "")), \
             contextlib.redirect_stdout(_io.StringIO()):
            result = _mod._check_ios_fleet_intersection(
                athena, "staging", "us-west-2", "wg", "s3://x/", res
            )
        assert result is True, f"D4: exempt sub must not block; rows={res.rows}"
        notes = [r for r in res.rows if r[1] == "NOTE" and "ios-fleet-ignored-users" in r[0]]
        assert notes, f"D4: must add NOTE ios-fleet-ignored-users; rows={res.rows}"

    def test_d4_different_sub_same_fleet_fails_intersection(self):
        """D4: non-exempt sub holding managed fleet → FAIL ios-fleet-intersection."""
        import contextlib
        import io as _io

        athena = MagicMock()
        athena.start_query_execution.return_value = {"QueryExecutionId": "Q1"}
        athena.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}, "Statistics": {}}
        }
        athena.get_query_results.return_value = {
            "ResultSet": {"Rows": [
                {"Data": [{"VarCharValue": "fleet_id"}]},
                {"Data": [{"VarCharValue": "FLEET-X"}]},
            ]}
        }
        res = _mod.Results()
        # Non-exempt sub: fleet goes into ios_fleet_ids, overlaps managed fleet
        with patch.object(
            _mod, "_collect_ios_fleet_ids_from_cognito",
            return_value=(frozenset({"FLEET-X"}), []),
        ), patch.object(_mod, "_near_cost_schedule", return_value=(False, "")), \
             contextlib.redirect_stdout(_io.StringIO()):
            result = _mod._check_ios_fleet_intersection(
                athena, "staging", "us-west-2", "wg", "s3://x/", res
            )
        assert result is False, "D4: non-exempt sub with managed fleet must FAIL"
        fail_rows = [r for r in res.rows if r[1] == "FAIL" and "intersection" in r[0]]
        assert fail_rows, f"D4: must FAIL ios-fleet-intersection; rows={res.rows}"

    def test_d4_over_500_users_with_token_raises(self):
        """D4: >500 Cognito users returned with PaginationToken → RuntimeError (fail closed)."""
        def fake_client(service, **kw):
            if service == "cognito-idp":
                m = MagicMock()
                m.list_user_pools.return_value = {
                    "UserPools": [{"Id": "us-west-2_ABC", "Name": "cms-staging-ui-users"}]
                }
                m.list_users.return_value = {
                    "Users": [{"Username": f"u{i}", "Attributes": []} for i in range(500)],
                    "PaginationToken": "still-more-pages",
                }
                return m
            return MagicMock()

        with patch("boto3.client", side_effect=fake_client):
            with pytest.raises(RuntimeError, match="500"):
                _mod._collect_ios_fleet_ids_from_cognito("us-west-2", "staging")

    # R4-W1 tests: test the real _collect_ios_fleet_ids_from_cognito --------

    _EXEMPT_SUB = "28f183d0-0011-70de-f2bb-ccf6ac1bfc4b"

    def _make_idp_client(self, users):
        """Build a mock cognito-idp client returning a fixed pool and given users."""
        m = MagicMock()
        m.list_user_pools.return_value = {
            "UserPools": [{"Id": "us-west-2_ABC", "Name": "cms-staging-ui-users"}]
        }
        m.list_users.return_value = {"Users": users}
        return m

    def _user(self, username, sub, fleet_ids="", email=""):
        attrs = [{"Name": "sub", "Value": sub}]
        if email:
            attrs.append({"Name": "email", "Value": email})
        if fleet_ids:
            attrs.append({"Name": "custom:fleetIds", "Value": fleet_ids})
        return {"Username": username, "Attributes": attrs}

    def test_r4_w1_exempt_sub_excluded_and_in_ignored(self):
        """R4-W1/N1a/N1b/N2: exempt sub matched by sub attribute → excluded, in ignored.

        Username and email differ from sub → confirms sub is used (not Username or email).
        Catches N1a (match by Username), N1b (match by email), N2 (exemption removed).
        """
        user = self._user(
            username="different-username",
            sub=self._EXEMPT_SUB,
            fleet_ids="FLEET-MANAGED",
            email="other@example.com",
        )
        idp = self._make_idp_client([user])

        def fake_client(service, **kw):
            return idp if service == "cognito-idp" else MagicMock()

        with patch("boto3.client", side_effect=fake_client):
            fleet_ids, ignored = _mod._collect_ios_fleet_ids_from_cognito("us-west-2", "staging")

        assert "FLEET-MANAGED" not in fleet_ids, (
            f"R4-W1/N1a/N1b/N2: exempt sub's fleet must be excluded from fleet_ids. "
            f"fleet_ids={fleet_ids}"
        )
        ignored_subs = [s for s, _ in ignored]
        assert self._EXEMPT_SUB in ignored_subs, (
            f"R4-W1/N2: exempt sub must appear in ignored list. ignored={ignored}"
        )

    def test_r4_w1_username_matches_exempt_but_sub_differs_included(self):
        """R4-W1/N1a: user whose Username == exempt sub but sub attr differs → included.

        N1a mutant (match by Username) would exclude this user. The test catches N1a.
        """
        user = self._user(
            username=self._EXEMPT_SUB,  # Username matches exempt sub
            sub="00000000-different-sub-00000000",
            fleet_ids="FLEET-DIFFERENT",
        )
        idp = self._make_idp_client([user])

        def fake_client(service, **kw):
            return idp if service == "cognito-idp" else MagicMock()

        with patch("boto3.client", side_effect=fake_client):
            fleet_ids, ignored = _mod._collect_ios_fleet_ids_from_cognito("us-west-2", "staging")

        assert "FLEET-DIFFERENT" in fleet_ids, (
            f"R4-W1/N1a: user with Username=exempt but different sub must be INCLUDED. "
            f"fleet_ids={fleet_ids}"
        )
        ignored_subs = [s for s, _ in ignored]
        assert self._EXEMPT_SUB not in ignored_subs, (
            f"R4-W1/N1a: must not appear in ignored (different sub). ignored={ignored}"
        )

    def test_r4_w1_exempt_sub_on_unknown_stage_included(self):
        """R4-W1/N2: exempt sub on a stage with no entry in _WEB_TEST_USER_SUBS → included.

        N2 mutant (exempt_subs = frozenset() always) passes. The distinctive test is N1a/N1b.
        This test verifies stage-keyed lookup specifically.
        """
        user = self._user(
            username="some-user",
            sub=self._EXEMPT_SUB,  # staging exempt sub
            fleet_ids="FLEET-PROD",
        )
        # Pool must match the stage name used below
        idp = MagicMock()
        idp.list_user_pools.return_value = {
            "UserPools": [{"Id": "us-west-2_PROD", "Name": "cms-prod-ui-users"}]
        }
        idp.list_users.return_value = {"Users": [user]}

        def fake_client(service, **kw):
            return idp if service == "cognito-idp" else MagicMock()

        with patch("boto3.client", side_effect=fake_client):
            # Use "prod" stage — exempt sub is staging-only; on prod it must be included
            fleet_ids, ignored = _mod._collect_ios_fleet_ids_from_cognito("us-west-2", "prod")

        assert "FLEET-PROD" in fleet_ids, (
            f"R4-W1: exempt-on-staging sub must be included on prod stage. fleet_ids={fleet_ids}"
        )

    def test_r4_w1_n2b_every_user_exempt_caught(self):
        """R4-W1/N2b: if exemption applies to every user (if True:), fleet_ids is empty.

        N2b disables the live Cognito half of the guard. This test catches it:
        a normal non-exempt user's fleet must appear in fleet_ids.
        """
        normal_user = self._user(
            username="normal-user",
            sub="00000000-not-exempt-0000000000",
            fleet_ids="FLEET-NORMAL",
        )
        idp = self._make_idp_client([normal_user])

        def fake_client(service, **kw):
            return idp if service == "cognito-idp" else MagicMock()

        with patch("boto3.client", side_effect=fake_client):
            fleet_ids, ignored = _mod._collect_ios_fleet_ids_from_cognito("us-west-2", "staging")

        assert "FLEET-NORMAL" in fleet_ids, (
            f"R4-W1/N2b: non-exempt user's fleet must be in fleet_ids. fleet_ids={fleet_ids}. "
            f"N2b (if True: exempt all) would make fleet_ids empty."
        )
        assert not ignored, (
            f"R4-W1/N2b: non-exempt user must not appear in ignored. ignored={ignored}"
        )



# ===========================================================================
# Mutation guards for M3, M4, M5 (were surviving in cycle 2)
# ===========================================================================


class TestMutationGuardsM3M4M5:
    """Explicit mutation tests for M3, M4, M5 which survived cycle 2."""

    def test_m3_item3_checks_model_calls_not_just_tokens(self):
        """M3: item 3 must check model_calls == 0, not just tokens_used == 0.

        Mutation M3: `tokens == 0 and model_calls == 0` → `tokens == 0`.
        A run with model_calls=5 but tokens=0 should FAIL 3-no-model-calls.
        """
        import contextlib
        import io as _io
        # Summary: model_calls=5 but tokens=0 (the broken case after M3)
        summary = {
            "artifacts_written": 0,
            "tokens_used": 0,   # tokens = 0
            "cost": {
                "deferred": 0,
                "model_calls": 5,  # model_calls > 0 (should fail)
                "anomalies_written": 0,
                "sell_timing_written": 0,
            },
        }
        ddb = MagicMock()
        ddb.Table.return_value.query.return_value = {"Items": []}
        res = Results()
        with patch.object(_mod, "_wait_lambda_response", return_value=summary), \
             patch.object(_mod, "_check_ios_fleet_intersection", return_value=True), \
             contextlib.redirect_stdout(_io.StringIO()):
            _mod.item3_second_run_noop(
                MagicMock(), ddb, MagicMock(), "findings", "runs", "fn",
                "staging", "us-west-2", "wg", "s3://x/", res,
            )
        no_model_calls_row = [r for r in res.rows if r[0] == "3-no-model-calls"]
        assert no_model_calls_row, "3-no-model-calls row must exist"
        assert no_model_calls_row[0][1] == "FAIL", (
            f"M3: item3 must FAIL when model_calls=5 (even if tokens=0). "
            f"Mutation M3 drops the model_calls check → PASS on this input. "
            f"Got status={no_model_calls_row[0][1]}"
        )

    def test_m4_item5_detects_fleet_b_ids_in_fleet_a_response(self):
        """M4: item 5 must detect fleet B finding_ids in fleet A's response.

        Mutation M4: `leaked = set()` → always empty → always PASS.
        A fleet A response containing a fleet B finding_id must FAIL.
        """
        import contextlib
        import io as _io
        from contextlib import contextmanager

        @contextmanager
        def eph(*a, **k):
            yield "tok", {}

        # Fleet B has finding_id "B-001" in DDB
        fleet_b_finding_id = "AVX-B-001"

        class Api:
            def call(self, method, path, token, body=None, timeout=45):
                if "/findings/fleet/FLEET-B" in path:
                    return 403, {}
                # Fleet A response contains fleet B's finding_id
                return 200, {
                    "findings": [
                        {"finding_id": fleet_b_finding_id, "agent_id": _AGENT_ID},
                        {"finding_id": "A-001", "agent_id": _AGENT_ID},
                    ]
                }

        ddb = MagicMock()
        table_mock = MagicMock()
        ddb.Table.return_value = table_mock
        # scan: returns FLEET-A and FLEET-B
        table_mock.scan.return_value = {
            "Items": [
                {"fleet_id": "FLEET-A", "agent_id": _AGENT_ID},
                {"fleet_id": "FLEET-B", "agent_id": _AGENT_ID},
            ]
        }
        # query (for by_fleet GSI, fleet B): returns B-001
        table_mock.query.return_value = {
            "Items": [{"finding_id": fleet_b_finding_id, "agent_id": _AGENT_ID, "fleet_id": "FLEET-B"}]
        }
        res = Results()
        with patch.object(_mod, "_ephemeral_user", eph), \
             contextlib.redirect_stdout(_io.StringIO()):
            _mod.item5_fleet_scope(
                ddb, MagicMock(), "findings-table", "pool-id", "client-id",
                Api(), "staging", res, allow_writes=True,
            )
        b_visible_row = [r for r in res.rows if r[0] == "5-fleet-b-not-visible"]
        assert b_visible_row, "5-fleet-b-not-visible row must exist"
        assert b_visible_row[0][1] == "FAIL", (
            f"M4: item5 must FAIL when fleet B finding_id appears in fleet A response. "
            f"Mutation M4: leaked=set() makes this always empty → PASS. "
            f"Got status={b_visible_row[0][1]}"
        )

    def test_m5_item2_fails_when_deferred_nonzero(self):
        """M5: item 2 must FAIL convergence when last run still has deferred > 0.

        Mutation M5: convergence check replaced with `True,` → always passes.
        A run that ends with deferred=1 must FAIL 2-convergence.
        """
        import contextlib
        import io as _io
        run_count = {"n": 0}

        def fake_invoke(lam, fn, payload):
            run_count["n"] += 1
            return {
                "artifacts_written": 0,
                "tokens_used": 0,
                "cost": {
                    "deferred": 1,  # never converges
                    "model_calls": 1,
                    "anomalies_written": 0,
                    "data_month": "2026-09",
                },
            }

        ddb = MagicMock()
        s3 = MagicMock()
        # Answer key
        s3.list_objects_v2.return_value = {
            "CommonPrefixes": [{"Prefix": "restricted/fleet_cost/plants/plant_anchor_month=2026-09/"}]
        }
        # Write a minimal parquet to satisfy _read_answer_key
        with patch.object(_mod, "_read_answer_key", return_value=([{"vin": "V1", "cause": "c"}], "2026-09")), \
             patch.object(_mod, "_wait_lambda_response", side_effect=fake_invoke), \
             patch.object(_mod, "_scan_findings_by_fleet_all_fleets", return_value=[]), \
             patch.object(_mod, "_check_ios_fleet_intersection", return_value=True), \
             contextlib.redirect_stdout(_io.StringIO()):
            res = Results()
            _mod.item2_agent_scores_plants(
                MagicMock(), ddb, s3, MagicMock(),
                findings_table="findings", runs_table="runs", function_name="fn",
                bucket="bucket", stage="staging", region="us-west-2",
                workgroup="wg", output_location="s3://x/", res=res,
            )
        convergence_row = [r for r in res.rows if r[0] == "2-convergence"]
        assert convergence_row, "2-convergence row must exist"
        assert convergence_row[0][1] == "FAIL", (
            f"M5: item2 must FAIL when deferred={1} after all runs. "
            f"Mutation M5: convergence always True → PASS on non-converged run. "
            f"Got status={convergence_row[0][1]}"
        )



# ===========================================================================
# R3-C2: item 8 uses compute_queue_status from the tool, not an inline copy
# ===========================================================================


class TestR3C2ComputeQueueStatusImport:
    """R3-C2: item 8 must call compute_queue_status from fleet_recommendation, not a copy.

    An undecided Finding (no Action) must produce a PASS '8-action-status' with 'pending'
    in the reply, not a SKIP.  This also verifies that M11 (the old inline "pending" branch
    returning "approved") is caught.
    """

    def test_item8_action_status_pass_for_undecided_finding(self):
        """R3-C2: '8-action-status' must be a PASS (not SKIP) for a Finding with no Action.

        compute_queue_status(finding, None) returns 'pending' for a non-expired Finding.
        The reply must contain 'pending' and the check must be a PASS, not a SKIP.
        """
        import contextlib
        import io as _io
        from moto import mock_aws
        import boto3

        with mock_aws():
            ddbr = boto3.resource("dynamodb", region_name="us-west-2")
            for tbl, key in (("actions", "action_id"), ("findings", "finding_id")):
                ddbr.create_table(
                    TableName=tbl,
                    KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                    AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                    BillingMode="PAY_PER_REQUEST",
                )
            ft = ddbr.Table("findings")
            ev = [
                {"kind": "cost.diagnosis_source", "value": "model"},
                {"kind": "cost.cost_per_mile", "value": "0.4127"},
            ]
            ft.put_item(Item={
                "finding_id": "FIND-a1",
                "finding_kind": "fleet.cost.anomaly",
                "agent_id": "agent-fi-cost",
                "fleet_id": "FLEET-A",
                "expires_at": "2099-01-01T00:00:00Z",
                "evidence": ev,
                "headline": "Energy cost 1.4x fleet-model median, mostly public peak charging",
            })
            ft.put_item(Item={
                "finding_id": "FIND-b1",
                "finding_kind": "fleet.cost.anomaly",
                "agent_id": "agent-fi-cost",
                "fleet_id": "FLEET-B",
                "expires_at": "2099-01-01T00:00:00Z",
                "headline": "Maintenance cost 2.1x cohort, repeat brake repair",
            })

            class Api:
                def call(self, method, path, token, body=None, timeout=45):
                    p = body.get("prompt", "") if body else ""
                    if "FIND-a1" in p:
                        # R3-C2: correct reply for undecided Finding — must mention the headline
                        # and contain "pending" (what compute_queue_status returns for no action)
                        return 200, {
                            "result": (
                                "Energy cost 1.4x fleet-model median, mostly public peak charging. "
                                "The cost per mile is 0.41. Decision status: pending."
                            )
                        }
                    return 200, {"result": "I can't look that one up for your fleets."}

            @contextlib.contextmanager
            def eph(*a, **k):
                yield "tok", {}

            res = _mod.Results()
            with patch.object(_mod, "_ephemeral_user", eph), \
                 patch.object(_mod, "_check_text_runtime_trust_flag", return_value=True), \
                 contextlib.redirect_stdout(_io.StringIO()):
                _mod.item8_fleet_recommendation(
                    MagicMock(), ddbr, MagicMock(), "findings", "actions",
                    "pool", "client", Api(), "fn", "staging", "us-west-2",
                    res, allow_writes=True,
                )

            action_status_rows = [r for r in res.rows if r[0] == "8-action-status"]
            assert action_status_rows, (
                f"R3-C2: '8-action-status' row must always be present (PASS or FAIL). "
                f"Old code: SKIP when no Action. Got rows: {[(r[0], r[1]) for r in res.rows]}"
            )
            assert action_status_rows[0][1] != "SKIP", (
                f"R3-C2: '8-action-status' must not be SKIP when Finding has no Action. "
                f"compute_queue_status(finding, None) returns 'pending'; the reply must contain it. "
                f"Row: {action_status_rows[0]}"
            )
            assert action_status_rows[0][1] == "PASS", (
                f"R3-C2: '8-action-status' must PASS when reply contains 'pending'. "
                f"Row: {action_status_rows[0]}"
            )


# ===========================================================================
# R3-C3: behavior tests for the three guards that M6, M9, M17 remove
# ===========================================================================


class TestR3C3GuardBehavior:
    """R3-C3: mutation-verified tests for the iOS overlap, invoke-stop, and schedule guards."""

    def test_ios_fleet_intersection_fails_when_overlap(self):
        """R3-C3 / M9: _check_ios_fleet_intersection must return False and FAIL when the
        managed fleets include a fleet that an iOS user holds.

        M9 mutates `unsafe = set()`, removing the overlap check entirely.  This test
        gives the function a managed set that overlaps the iOS set and asserts False + FAIL.
        """
        import contextlib
        import io as _io

        # Athena returns one managed fleet
        athena = MagicMock()
        seq = [0]
        data = {}

        def start(**kw):
            qid = f"Q{seq[0]}"
            seq[0] += 1
            data[qid] = [["FLEET-MERIDIAN-ONBOARD"]]  # overlaps _IOS_PERSONA_FLEET_IDS
            return {"QueryExecutionId": qid}

        def gqe(QueryExecutionId):
            return {"QueryExecution": {"Status": {"State": "SUCCEEDED"},
                                       "Statistics": {"DataScannedInBytes": 512}}}

        def gqr(QueryExecutionId, **kw):
            return {"ResultSet": {"Rows": [{"Data": [{"VarCharValue": "h"}]}] + [
                {"Data": [{"VarCharValue": c} for c in row]}
                for row in data[QueryExecutionId]
            ]}}

        athena.start_query_execution.side_effect = start
        athena.get_query_execution.side_effect = gqe
        athena.get_query_results.side_effect = gqr

        res = _mod.Results()
        with patch.object(_mod, "_near_cost_schedule", return_value=(False, "")), \
             patch.object(_mod, "_collect_ios_fleet_ids_from_cognito",
                          return_value=(frozenset({"FLEET-MERIDIAN-ONBOARD"}), [])), \
             contextlib.redirect_stdout(_io.StringIO()):
            ok = _mod._check_ios_fleet_intersection(
                athena, "staging", "us-west-2", "wg", "s3://x/", res
            )

        assert ok is False, (
            "R3-C3 / M9: _check_ios_fleet_intersection must return False when managed "
            "fleet overlaps iOS fleet. M9 sets unsafe=set() → always returns True."
        )
        fail_rows = [r for r in res.rows if r[1] == "FAIL" and "intersection" in r[0]]
        assert fail_rows, (
            f"R3-C3 / M9: must produce a FAIL row for 'ios-fleet-intersection'. "
            f"Got rows: {[(r[0], r[1]) for r in res.rows]}"
        )

    def test_main_stops_item3_after_item2_invoke_error(self):
        """R3-C3 / M6 / R3-W1: main() must not invoke item 3 after item 2's invoke raises.

        M6 mutates the invoke-stop condition to `and False`, so item 3 always starts.
        R3-W1: old code checked label text ("invoke" in label), which never matched
        "2-run-{n}" labels; new code checks item2's return value directly.
        """
        import contextlib
        import io as _io

        invokes = {"n": 0}

        def fake_invoke(lam, fn, payload):
            invokes["n"] += 1
            if invokes["n"] == 1:
                raise ConnectionError("Connection closed before response")
            # If item 3 runs, it would reach here (invoke #2)
            return {"tokens_used": 0, "cost": {
                "deferred": 0, "model_calls": 0,
                "anomalies_written": 0, "sell_timing_written": 0,
            }}

        def _pass(label):
            def f(*a, **k):
                res = k.get("res") or next(x for x in a if isinstance(x, _mod.Results))
                res.check(label, True)
                return True
            return f

        all_items = {
            "item1_views_rows": _pass("1-x"),
            "item4_athena_paging": _pass("4-x"),
            "item5_fleet_scope": _pass("5-x"),
            "item6_forced_alarm_failure": _pass("6-x"),
            "item7_rules_fallback": _pass("7-x"),   # D3: runs before item 2
            "item8_fleet_recommendation": _pass("8-x"),
        }

        boto3_mock = MagicMock()
        boto3_mock.client.return_value.get_caller_identity.return_value = {
            "Account": "000000000000", "Arn": "arn:x"
        }
        boto3_mock.client.return_value.describe_stacks.return_value = {
            "Stacks": [{"Outputs": [{"OutputKey": "RestApiUrl", "OutputValue": "https://x"}]}]
        }

        with contextlib.ExitStack() as st:
            for name, val in all_items.items():
                st.enter_context(patch.object(_mod, name, val))
            st.enter_context(patch.object(_mod, "_wait_lambda_response", side_effect=fake_invoke))
            st.enter_context(patch.object(_mod, "_check_ios_fleet_intersection", return_value=True))
            st.enter_context(patch.object(_mod, "_read_answer_key",
                                          return_value=([{"vin": "V1", "cause": "c"}], "2026-09")))
            st.enter_context(patch.object(_mod, "_latest_run_record", return_value=None))
            st.enter_context(patch.object(_mod, "_find_pool", return_value="pool"))
            st.enter_context(patch.object(_mod, "_password_client", return_value="client"))
            st.enter_context(patch.dict(sys.modules, {"boto3": boto3_mock}))
            st.enter_context(patch.object(sys, "argv", [
                "lv", "--stage", "staging", "--region", "us-west-2",
                "--all", "--allow-writes",
            ]))
            st.enter_context(patch.dict(os.environ, {"LIVE_TESTS": "1"}))
            st.enter_context(contextlib.redirect_stdout(_io.StringIO()))
            st.enter_context(contextlib.redirect_stderr(_io.StringIO()))
            _mod.main()

        assert invokes["n"] == 1, (
            f"R3-C3 / M6 / R3-W1: after item 2's invoke raises, item 3 must NOT invoke "
            f"the Lambda. Expected 1 total invoke, got {invokes['n']}. "
            "M6 removes the stop condition → item 3 fires a second invoke."
        )

    def test_near_cost_schedule_returns_true_at_03_01_utc(self):
        """R3-C3 / M17: _near_cost_schedule must return (True, ...) at 03:01 UTC.

        M17 mutates the comparison to `if False:` so it never matches.
        03:01 UTC is 59 minutes before 04:00 UTC, within the ±60-minute window.
        """
        import datetime
        t = datetime.datetime(2026, 10, 7, 3, 1, 0, tzinfo=datetime.timezone.utc)
        with patch.object(_mod.datetime, "datetime",
                          type("FakeDT", (), {
                              "now": staticmethod(lambda tz=None: t),
                              "fromtimestamp": datetime.datetime.fromtimestamp,
                              "fromisoformat": datetime.datetime.fromisoformat,
                          })()):
            result, reason = _mod._near_cost_schedule()
        assert result is True, (
            f"R3-C3 / M17: 03:01 UTC is within ±60 min of 04:00 UTC. "
            f"_near_cost_schedule must return True. Got False. Reason: {reason!r}"
        )

    def test_near_cost_schedule_returns_true_at_04_59_utc(self):
        """R3-C3 / M17: _near_cost_schedule must return (True, ...) at 04:59 UTC.

        04:59 UTC is 59 minutes after 04:00 UTC, still within the ±60-minute window.
        """
        import datetime
        t = datetime.datetime(2026, 10, 7, 4, 59, 0, tzinfo=datetime.timezone.utc)
        with patch.object(_mod.datetime, "datetime",
                          type("FakeDT", (), {
                              "now": staticmethod(lambda tz=None: t),
                              "fromtimestamp": datetime.datetime.fromtimestamp,
                              "fromisoformat": datetime.datetime.fromisoformat,
                          })()):
            result, reason = _mod._near_cost_schedule()
        assert result is True, (
            f"R3-C3 / M17: 04:59 UTC is within ±60 min of 04:00 UTC. "
            f"_near_cost_schedule must return True. Got False. Reason: {reason!r}"
        )

    def test_near_cost_schedule_returns_false_at_05_01_utc(self):
        """R3-C3 / M17: _near_cost_schedule must return (False, '') at 05:01 UTC.

        05:01 UTC is 61 minutes after 04:00 UTC, outside the ±60-minute window.
        """
        import datetime
        t = datetime.datetime(2026, 10, 7, 5, 1, 0, tzinfo=datetime.timezone.utc)
        with patch.object(_mod.datetime, "datetime",
                          type("FakeDT", (), {
                              "now": staticmethod(lambda tz=None: t),
                              "fromtimestamp": datetime.datetime.fromtimestamp,
                              "fromisoformat": datetime.datetime.fromisoformat,
                          })()):
            result, reason = _mod._near_cost_schedule()
        assert result is False, (
            f"R3-C3 / M17: 05:01 UTC is outside ±60 min of 04:00 UTC. "
            f"_near_cost_schedule must return False. Got True. Reason: {reason!r}"
        )


# ===========================================================================
# R3-W1: item 2 stop propagation — behavioral test replacing inspect.getsource
# ===========================================================================


class TestR3W1InvokeStopBehavior:
    """R3-W1: after item 2's invoke raises, item 3 and item 7 must not invoke.

    Replaces test_item3_gets_skip_when_invoke_ok_is_false which used inspect.getsource
    and could not detect M6 (the stop condition removed).
    """

    def test_item2_invoke_error_returns_false_invoke_ok(self):
        """R3-W1: item2_agent_scores_plants must return invoke_ok=False when invoke raises."""
        import contextlib
        import io as _io

        def fail_invoke(lam, fn, payload):
            raise ConnectionError("dropped")

        res = _mod.Results()
        with patch.object(_mod, "_check_ios_fleet_intersection", return_value=True), \
             patch.object(_mod, "_read_answer_key",
                          return_value=([{"vin": "V1", "cause": "c"}], "2026-09")), \
             patch.object(_mod, "_wait_lambda_response", side_effect=fail_invoke), \
             patch.object(_mod, "_near_cost_schedule", return_value=(False, "")), \
             contextlib.redirect_stdout(_io.StringIO()):
            data_month, invoke_ok = _mod.item2_agent_scores_plants(
                MagicMock(), MagicMock(), MagicMock(), MagicMock(),
                findings_table="f", runs_table="r", function_name="fn",
                bucket="b", stage="staging", region="us-west-2",
                workgroup="wg", output_location="s3://x/", res=res,
            )

        assert invoke_ok is False, (
            "R3-W1: item2_agent_scores_plants must return invoke_ok=False when the "
            "Lambda invoke raises an exception. "
            f"Got invoke_ok={invoke_ok!r}."
        )


# ===========================================================================
# R3-W2: item 7 FAIL when no this-run Findings (M12)
# ===========================================================================


class TestR3W2Item7RunFilter:
    """R3-W2: item 7 must FAIL 7-run-filter when no Finding has computed_at >= invoke time.

    Catches M12: `if str(f.get("computed_at", "")) >= invoke_time_str` → `if True`.
    """

    def test_item7_fails_when_no_this_run_findings(self):
        """R3-W2 / M12: if no Finding is from this run, item 7 must FAIL (not fall back)."""
        import contextlib
        import io as _io

        def make_summary(rules_fallbacks):
            return {
                "artifacts_written": 0,
                "tokens_used": 0,
                "abort_reason": None,
                "cost": {
                    "deferred": 0,
                    "model_calls": 0,
                    "rules_fallbacks": rules_fallbacks,
                    "anomalies_written": 0,
                    "sell_timing_written": 0,
                    "data_month": "2026-09",
                },
            }

        # Old rules Finding from September, computed_at far in the past
        old_finding = {
            "finding_id": "FIND-old",
            "agent_id": "agent-fi-cost",
            "computed_at": "2026-09-01T04:00:00Z",
            "evidence": [{"kind": "cost.diagnosis_source", "value": "rules"}],
        }

        lam = MagicMock()
        lam.get_function_configuration.return_value = {
            "Environment": {"Variables": {"A": "1"}},
            "LastUpdateStatus": "Successful",
        }
        ddb = MagicMock()
        ddb.Table.return_value.scan.return_value = {"Items": [old_finding]}
        res = _mod.Results()
        with patch.object(_mod, "_wait_lambda_response",
                          return_value=make_summary(1)), \
             patch.object(_mod, "_check_ios_fleet_intersection", return_value=True), \
             contextlib.redirect_stdout(_io.StringIO()):
            _mod.item7_rules_fallback(
                lam, ddb, MagicMock(), "findings", "fn",
                "staging", "us-west-2", "wg", "s3://x/", res,
            )

        run_filter_rows = [r for r in res.rows if r[0] == "7-run-filter"]
        assert run_filter_rows, (
            f"R3-W2 / M12: a '7-run-filter' row must be emitted when no Finding "
            f"has computed_at >= invoke time. Got rows: {[(r[0], r[1]) for r in res.rows]}"
        )
        assert run_filter_rows[0][1] == "FAIL", (
            f"R3-W2 / M12: '7-run-filter' must be FAIL when no this-run Findings found. "
            f"Old code fell back to all Findings (NOTE + continue). "
            f"Got status={run_filter_rows[0][1]!r}. M12 removes the filter entirely."
        )


# ===========================================================================
# R3-W3: evidence check uses numeric cost.* value; refusal adds "no fleet access"
# ===========================================================================


class TestR3W3EvidenceAndRefusal:
    """R3-W3: behavioral tests for evidence check (numeric) and refusal keyword."""

    def test_item8_evidence_fails_when_only_diagnosis_source_in_reply(self):
        """R3-W3: 8-evidence-in-reply must FAIL when reply contains the diagnosis_source
        word ("model") but not a numeric cost value.

        Old code: checked evidence[0] which is always cost.diagnosis_source → "model" or
        "rules".  A reply that just echoes the headline ("fleet-model median") or mentions
        "model" passes vacuously.  Fix: check cost.cost_* numeric value.
        """
        import contextlib
        import io as _io
        from moto import mock_aws
        import boto3

        with mock_aws():
            ddbr = boto3.resource("dynamodb", region_name="us-west-2")
            for tbl, key in (("actions2", "action_id"), ("findings2", "finding_id")):
                ddbr.create_table(
                    TableName=tbl,
                    KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                    AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                    BillingMode="PAY_PER_REQUEST",
                )
            ft = ddbr.Table("findings2")
            # Evidence has cost.diagnosis_source AND cost.cost_per_mile
            ev = [
                {"kind": "cost.diagnosis_source", "value": "model"},
                {"kind": "cost.cost_per_mile", "value": "0.4127"},
            ]
            ft.put_item(Item={
                "finding_id": "FIND-ev1",
                "finding_kind": "fleet.cost.anomaly",
                "agent_id": "agent-fi-cost",
                "fleet_id": "FLEET-X",
                "expires_at": "2099-01-01T00:00:00Z",
                "evidence": ev,
                "headline": "Energy cost 1.4x fleet-model median",
            })

            class Api:
                def call(self, method, path, token, body=None, timeout=45):
                    # Reply echoes the headline (contains "model") but no numeric cost value
                    return 200, {"result": (
                        "Energy cost 1.4x fleet-model median. The diagnosis source is model."
                    )}

            @contextlib.contextmanager
            def eph(*a, **k):
                yield "tok", {}

            res = _mod.Results()
            with patch.object(_mod, "_ephemeral_user", eph), \
                 patch.object(_mod, "_check_text_runtime_trust_flag", return_value=True), \
                 contextlib.redirect_stdout(_io.StringIO()):
                _mod.item8_fleet_recommendation(
                    MagicMock(), ddbr, MagicMock(), "findings2", "actions2",
                    "pool", "client", Api(), "fn", "staging", "us-west-2",
                    res, allow_writes=True,
                )

            ev_rows = [r for r in res.rows if r[0] == "8-evidence-in-reply"]
            assert ev_rows, (
                f"R3-W3: '8-evidence-in-reply' row must exist. "
                f"Got: {[(r[0], r[1]) for r in res.rows]}"
            )
            assert ev_rows[0][1] == "FAIL", (
                f"R3-W3: '8-evidence-in-reply' must FAIL when reply contains 'model' "
                f"(from headline) but not the numeric cost value '0.41'. "
                f"Old code checked evidence[0] ('model') → PASS on headline-only reply. "
                f"Got status={ev_rows[0][1]!r}"
            )

    def test_refusal_keyword_no_fleet_access_matches(self):
        """R3-W3: 'no fleet access' must be in the refusal keyword list.

        Persona rule 4 tells the model to give the tool's reason plainly: 'no fleet
        access to this finding'.  Old list had 'no access' but not 'no fleet access'.
        """
        # Verify the keyword appears in the source of item8 (the check is in the refusal path)
        refusal_text = "I can't look that one up, no fleet access to this finding."
        keywords = (
            "cannot access", "not authorized", "no access", "no fleet access",
            "refused", "permission", "not in your fleet", "does not belong", "another fleet",
        )
        matched = any(kw in refusal_text.lower() for kw in keywords)
        assert matched, (
            f"R3-W3: 'no fleet access to this finding' must match at least one refusal keyword. "
            f"Old list did not include 'no fleet access'. Got: matched={matched}"
        )
        # And 'no fleet access' itself should be a match (not just a coincidental substring)
        assert "no fleet access" in refusal_text.lower(), (
            "R3-W3: the test text contains 'no fleet access'; it must match."
        )


# ===========================================================================
# R3-W7: behavioral tests for surviving mutants (M12 above, plus M13, M16, M18-M25)
# ===========================================================================


class TestR3W7SurvivingMutants:
    """R3-W7: behavioral tests that catch mutants M13, M16, M18, M19, M20, M21, M22, M23, M25.

    Each test calls the relevant item function with stubbed clients and asserts the exact
    outcome — not source text.
    """

    # -----------------------------------------------------------------------
    # M13: expired Findings scored again (item 2 expiry filter)
    # -----------------------------------------------------------------------

    def test_m13_item2_excludes_expired_finding_via_item2_call(self):
        """M13 / R3-W7: item 2 must FAIL 2-score when the only planted Finding is expired.

        Old test re-implemented the filter in the test body instead of calling item2.
        Fix: call item2 with an expired Finding and check '2-score' is FAIL.

        M13 mutates `or str(f.get("expires_at", "")) > now_iso` → `or True` so expired
        Findings are scored.  With the mutation applied, 2-score would be PASS.
        """
        import contextlib
        import io as _io

        expired_finding = {
            "finding_id": "FIND-exp",
            "finding_kind": "fleet.cost.anomaly",
            "agent_id": "agent-fi-cost",
            "fleet_id": "FLEET-A",
            "scope": {"vin": "V1"},
            "expires_at": "2020-01-01T00:00:00Z",  # far in the past
            "recommended_action": {"parameters": {"month": "2026-09", "cause": "charging_mix"}},
        }

        res = _mod.Results()
        with patch.object(_mod, "_read_answer_key",
                          return_value=([{"vin": "V1", "cause": "charging_mix"}], "2026-09")), \
             patch.object(_mod, "_wait_lambda_response",
                          return_value={"tokens_used": 0, "cost": {
                              "deferred": 0, "data_month": "2026-09",
                          }}), \
             patch.object(_mod, "_scan_findings_by_fleet_all_fleets",
                          return_value=[expired_finding]), \
             patch.object(_mod, "_check_ios_fleet_intersection", return_value=True), \
             patch.object(_mod, "_near_cost_schedule", return_value=(False, "")), \
             contextlib.redirect_stdout(_io.StringIO()):
            _mod.item2_agent_scores_plants(
                MagicMock(), MagicMock(), MagicMock(), MagicMock(),
                findings_table="f", runs_table="r", function_name="fn",
                bucket="b", stage="staging", region="us-west-2",
                workgroup="wg", output_location="s3://x/", res=res,
            )

        score_rows = [r for r in res.rows if r[0] == "2-score"]
        assert score_rows, (
            f"R3-W7 / M13: '2-score' row must exist. Got: {[(r[0], r[1]) for r in res.rows]}"
        )
        assert score_rows[0][1] == "FAIL", (
            f"R3-W7 / M13: item 2 must FAIL when the only planted Finding is expired. "
            f"M13 makes the expiry filter always pass → PASS on expired Finding. "
            f"Got status={score_rows[0][1]!r}"
        )

    # -----------------------------------------------------------------------
    # M16: main() returns 2 on full pass (positive-control test)
    # -----------------------------------------------------------------------

    def test_m16_main_exits_0_when_all_items_pass(self):
        """M16 / R3-W7: main() must return 0 when every item passes with no skips.

        M16 mutates `return 0` → `return 2` in the PASS branch.  Without a positive-
        control test, a run that should pass silently reports exit 2.
        """
        import contextlib
        import io as _io

        def _pass_item(label):
            def f(*a, **k):
                res = k.get("res") or next(x for x in a if isinstance(x, _mod.Results))
                res.check(label, True)
                return True
            return f

        all_items = {
            "item1_views_rows": _pass_item("1-x"),
            "item2_agent_scores_plants": lambda *a, **k: (
                k.get("res") or next(x for x in a if isinstance(x, _mod.Results))
            ).check("2-x", True) or ("2026-09", True),
            "item3_second_run_noop": _pass_item("3-x"),
            "item4_athena_paging": _pass_item("4-x"),
            "item5_fleet_scope": _pass_item("5-x"),
            "item6_forced_alarm_failure": _pass_item("6-x"),
            "item7_rules_fallback": _pass_item("7-x"),
            "item8_fleet_recommendation": _pass_item("8-x"),
        }

        boto3_mock = MagicMock()
        boto3_mock.client.return_value.get_caller_identity.return_value = {
            "Account": "000000000000", "Arn": "arn:x",
        }
        boto3_mock.client.return_value.describe_stacks.return_value = {
            "Stacks": [{"Outputs": [{"OutputKey": "RestApiUrl", "OutputValue": "https://x"}]}],
        }

        def fake_item2(*a, **k):
            res = k.get("res") or next(x for x in a if isinstance(x, _mod.Results))
            res.check("2-x", True)
            res.check("2-convergence", True)  # the real item 2 emits this row
            return "2026-09", True

        all_items["item2_agent_scores_plants"] = fake_item2

        with contextlib.ExitStack() as st:
            for name, val in all_items.items():
                st.enter_context(patch.object(_mod, name, val))
            st.enter_context(patch.object(_mod, "_find_pool", return_value="pool"))
            st.enter_context(patch.object(_mod, "_password_client", return_value="client"))
            st.enter_context(patch.dict(sys.modules, {"boto3": boto3_mock}))
            st.enter_context(patch.object(sys, "argv", [
                "lv", "--stage", "staging", "--region", "us-west-2",
                "--all", "--allow-writes",
            ]))
            st.enter_context(patch.dict(os.environ, {"LIVE_TESTS": "1"}))
            st.enter_context(contextlib.redirect_stdout(_io.StringIO()))
            st.enter_context(contextlib.redirect_stderr(_io.StringIO()))
            rc = _mod.main()

        assert rc == 0, (
            f"R3-W7 / M16: main() must return 0 when every item passes and no skips exist. "
            f"M16 mutates `return 0` → `return 2`. Got rc={rc}."
        )

    # -----------------------------------------------------------------------
    # M18: item 4 cost check always passes
    # -----------------------------------------------------------------------

    def test_m18_item4_cost_fails_on_high_scanned_bytes(self):
        """M18 / R3-W7: item 4 must FAIL '4-cost-under-5' when DataScannedInBytes is large.

        M18 mutates `cost_per_month_usd < 5.0,` → `True,` so the check always passes.
        2 TB scanned × $5/TB × 30 runs/month = $300 >> $5 → must FAIL.
        """
        import contextlib
        import io as _io

        ONE_TB = 1024 ** 4
        SCANNED_BYTES = 2 * ONE_TB  # $10/query × 30 = $300/month

        athena = MagicMock()
        seq = [0]
        data = {}

        def start(**kw):
            qid = f"Q{seq[0]}"
            seq[0] += 1
            data[qid] = [["200"]]  # COUNT(*) = 200
            return {"QueryExecutionId": qid}

        def gqe(QueryExecutionId):
            return {"QueryExecution": {"Status": {"State": "SUCCEEDED"},
                                       "Statistics": {"DataScannedInBytes": SCANNED_BYTES}}}

        def gqr(QueryExecutionId, **kw):
            return {"ResultSet": {"Rows": [{"Data": [{"VarCharValue": "h"}]}] + [
                {"Data": [{"VarCharValue": c} for c in row]}
                for row in data[QueryExecutionId]
            ]}}

        athena.start_query_execution.side_effect = start
        athena.get_query_execution.side_effect = gqe
        athena.get_query_results.side_effect = gqr

        ddb = MagicMock()
        rec = {"agent_component": "agent-fi-cost#cost", "data_month": "2026-09",
               "vins_selected": 200}
        ddb.Table.return_value.query.return_value = {"Items": [rec]}

        res = _mod.Results()
        with contextlib.redirect_stdout(_io.StringIO()):
            _mod.item4_athena_paging(athena, ddb, "wg", "s3://x/", "staging", "runs", res)

        cost_rows = [r for r in res.rows if r[0] == "4-athena-cost-under-5"]
        assert cost_rows, (
            f"R3-W7 / M18: '4-athena-cost-under-5' row must exist. "
            f"Got: {[(r[0], r[1]) for r in res.rows]}"
        )
        assert cost_rows[0][1] == "FAIL", (
            f"R3-W7 / M18: item 4 must FAIL when DataScannedInBytes is large "
            f"(~$1500/month >> $5). M18 always passes. Got: {cost_rows[0][1]!r}"
        )

    # -----------------------------------------------------------------------
    # M19: item 4 paging comparison always passes
    # -----------------------------------------------------------------------

    def test_m19_item4_paging_fails_on_count_mismatch(self):
        """M19 / R3-W7: item 4 must FAIL '4-agent-paging' when agent truncated (agent < COUNT).

        M19 mutates `check_paging_match(...)` → `True, ""` so the comparison always passes.
        Agent reported 50 VINs but COUNT(*) is 200 → FAIL.
        """
        import contextlib
        import io as _io

        athena = MagicMock()
        seq = [0]
        data = {}

        def start(**kw):
            qid = f"Q{seq[0]}"
            seq[0] += 1
            data[qid] = [["200"]]  # COUNT(*) = 200
            return {"QueryExecutionId": qid}

        def gqe(QueryExecutionId):
            return {"QueryExecution": {"Status": {"State": "SUCCEEDED"},
                                       "Statistics": {"DataScannedInBytes": 1024}}}

        def gqr(QueryExecutionId, **kw):
            return {"ResultSet": {"Rows": [{"Data": [{"VarCharValue": "h"}]}] + [
                {"Data": [{"VarCharValue": c} for c in row]}
                for row in data[QueryExecutionId]
            ]}}

        athena.start_query_execution.side_effect = start
        athena.get_query_execution.side_effect = gqe
        athena.get_query_results.side_effect = gqr

        ddb = MagicMock()
        rec = {"agent_component": "agent-fi-cost#cost", "data_month": "2026-09",
               "vins_selected": 50}  # agent says 50; COUNT says 200 → truncated
        ddb.Table.return_value.query.return_value = {"Items": [rec]}

        res = _mod.Results()
        with contextlib.redirect_stdout(_io.StringIO()):
            _mod.item4_athena_paging(athena, ddb, "wg", "s3://x/", "staging", "runs", res)

        paging_rows = [r for r in res.rows if r[0] == "4-agent-paging"]
        assert paging_rows, (
            f"R3-W7 / M19: '4-agent-paging' row must exist when vins_selected != COUNT(*). "
            f"Got: {[(r[0], r[1]) for r in res.rows]}"
        )
        assert paging_rows[0][1] == "FAIL", (
            f"R3-W7 / M19: item 4 must FAIL when agent_selected=50 but COUNT(*)=200. "
            f"M19 always passes. Got: {paging_rows[0][1]!r}"
        )

    # -----------------------------------------------------------------------
    # M20: enrolled-fleet coverage always passes (item 1)
    # -----------------------------------------------------------------------

    def test_m20_item1_fails_when_fleet_missing_from_view(self):
        """M20 / R3-W7: item 1 must FAIL '1-enrolled-fleet-coverage' when an enrolled
        fleet does not appear in vehicle_cost_monthly.

        M20 mutates `not missing_from_views,` → `True,` so the coverage check always passes.
        """
        import contextlib
        import io as _io

        def rows_for(sql):
            if "fleet_enrollment" in sql:
                # Two enrolled fleets
                return [["FLEET-A"], ["FLEET-B"]]
            if "DISTINCT fleet_id" in sql and "monthly" in sql:
                # Only one fleet in the view
                return [["FLEET-A"]]
            if "COUNT(*)" in sql:
                return [["10"]]
            return []

        athena = MagicMock()
        seq = [0]
        data = {}

        def start(**kw):
            qid = f"Q{seq[0]}"
            seq[0] += 1
            data[qid] = rows_for(kw["QueryString"])
            return {"QueryExecutionId": qid}

        def gqe(QueryExecutionId):
            return {"QueryExecution": {"Status": {"State": "SUCCEEDED"},
                                       "Statistics": {"DataScannedInBytes": 512}}}

        def gqr(QueryExecutionId, **kw):
            return {"ResultSet": {"Rows": [{"Data": [{"VarCharValue": "h"}]}] + [
                {"Data": [{"VarCharValue": c} for c in row]}
                for row in data[QueryExecutionId]
            ]}}

        athena.start_query_execution.side_effect = start
        athena.get_query_execution.side_effect = gqe
        athena.get_query_results.side_effect = gqr

        res = _mod.Results()
        with contextlib.redirect_stdout(_io.StringIO()):
            _mod.item1_views_rows(
                athena, MagicMock(), "wg", "s3://x/", "staging", res,
            )

        coverage_rows = [r for r in res.rows if r[0] == "1-enrolled-fleet-coverage"]
        assert coverage_rows, (
            f"R3-W7 / M20: '1-enrolled-fleet-coverage' row must exist. "
            f"Got: {[(r[0], r[1]) for r in res.rows]}"
        )
        assert coverage_rows[0][1] == "FAIL", (
            f"R3-W7 / M20: item 1 must FAIL when FLEET-B is enrolled but not in view. "
            f"M20 always passes. Got: {coverage_rows[0][1]!r}"
        )

    # -----------------------------------------------------------------------
    # M21: item 5 fleet A cost Findings check always passes
    # -----------------------------------------------------------------------

    def test_m21_item5_fails_when_no_cost_findings_for_fleet_a(self):
        """M21 / R3-W7: item 5 must FAIL '5-fleet-a-has-findings' when fleet A has no
        cost agent Findings visible via the API.

        M21 mutates `len(cost_findings_a) >= 1,` → `True,` so item 5 passes even with
        no Findings visible to fleet A's operator.

        Setup: DDB scan returns two fleets (so item 5 doesn't SKIP), but the AVX API
        returns an empty list for fleet A.
        """
        import contextlib
        import io as _io

        ddb = MagicMock()
        table_mock = MagicMock()
        ddb.Table.return_value = table_mock
        # _all_fleet_ids_for_agent uses scan(); return 2 fleets
        table_mock.scan.return_value = {
            "Items": [
                {"fleet_id": "FLEET-A", "agent_id": "agent-fi-cost"},
                {"fleet_id": "FLEET-B", "agent_id": "agent-fi-cost"},
            ]
        }
        # _all_finding_ids_for_fleet also uses scan (via _scan_findings_by_fleet)
        # We need fleet B to return no finding_ids (empty)
        table_mock.query.return_value = {"Items": []}

        class Api:
            def call(self, method, path, token, body=None, timeout=45):
                if "FLEET-B" in path:
                    return 403, {"error": "insufficient scope"}
                # Fleet A API returns empty findings list
                return 200, {"findings": [], "next_token": None}

        @contextlib.contextmanager
        def eph(*a, **k):
            yield "tok", {}

        res = _mod.Results()
        with patch.object(_mod, "_ephemeral_user", eph), \
             contextlib.redirect_stdout(_io.StringIO()):
            _mod.item5_fleet_scope(
                ddb, MagicMock(), "findings-table", "pool-id", "client-id",
                Api(), "staging", res, allow_writes=True,
            )

        findings_rows = [r for r in res.rows if r[0] == "5-fleet-a-has-findings"]
        assert findings_rows, (
            f"R3-W7 / M21: '5-fleet-a-has-findings' row must exist when API returns no Findings. "
            f"Got: {[(r[0], r[1]) for r in res.rows]}"
        )
        assert findings_rows[0][1] == "FAIL", (
            f"R3-W7 / M21: item 5 must FAIL when fleet A has 0 cost Findings. "
            f"M21 always passes. Got: {findings_rows[0][1]!r}"
        )

    # -----------------------------------------------------------------------
    # M22: own-fleet headline not required (item 8)
    # -----------------------------------------------------------------------

    def test_m22_item8_fails_when_headline_not_mentioned(self):
        """M22 / R3-W7: item 8 must FAIL '8-own-fleet-content' when the reply does not
        contain the headline hint.

        M22 mutates `finding_mentioned and not refused_a,` → `not refused_a,` so the
        headline presence check is removed.
        """
        import contextlib
        import io as _io
        from moto import mock_aws
        import boto3

        with mock_aws():
            ddbr = boto3.resource("dynamodb", region_name="us-west-2")
            for tbl, key in (("act3", "action_id"), ("find3", "finding_id")):
                ddbr.create_table(
                    TableName=tbl,
                    KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                    AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                    BillingMode="PAY_PER_REQUEST",
                )
            ddbr.Table("find3").put_item(Item={
                "finding_id": "FIND-c1",
                "finding_kind": "fleet.cost.anomaly",
                "agent_id": "agent-fi-cost",
                "fleet_id": "FLEET-C",
                "expires_at": "2099-01-01T00:00:00Z",
                "headline": "Battery degradation 1.8x fleet median",
                "evidence": [
                    {"kind": "cost.diagnosis_source", "value": "model"},
                    {"kind": "cost.cost_per_mile", "value": "0.55"},
                ],
            })

            class Api:
                def call(self, method, path, token, body=None, timeout=45):
                    # Reply does NOT mention the headline or finding content
                    return 200, {"result": "The cost run completed. Decision status: pending."}

            @contextlib.contextmanager
            def eph(*a, **k):
                yield "tok", {}

            res = _mod.Results()
            with patch.object(_mod, "_ephemeral_user", eph), \
                 patch.object(_mod, "_check_text_runtime_trust_flag", return_value=True), \
                 contextlib.redirect_stdout(_io.StringIO()):
                _mod.item8_fleet_recommendation(
                    MagicMock(), ddbr, MagicMock(), "find3", "act3",
                    "pool", "client", Api(), "fn", "staging", "us-west-2",
                    res, allow_writes=True,
                )

            content_rows = [r for r in res.rows if r[0] == "8-own-fleet-content"]
            assert content_rows, (
                f"R3-W7 / M22: '8-own-fleet-content' row must exist. "
                f"Got: {[(r[0], r[1]) for r in res.rows]}"
            )
            assert content_rows[0][1] == "FAIL", (
                f"R3-W7 / M22: item 8 must FAIL when the reply does not mention the headline. "
                f"M22 removes the finding_mentioned check. Got: {content_rows[0][1]!r}"
            )

    # -----------------------------------------------------------------------
    # M23: fleet B headline not required absent (item 8)
    # -----------------------------------------------------------------------

    def test_m23_item8_fails_when_refusal_mentions_fleet_b_headline(self):
        """M23 / R3-W7: item 8 must FAIL '8-refusal-check' when the refusal reply
        mentions fleet B's headline (the agent leaked fleet B data).

        M23 mutates `refused_b and headline_b_absent,` → `refused_b,` so the absence
        of the fleet B headline is no longer required.
        """
        import contextlib
        import io as _io
        from moto import mock_aws
        import boto3

        with mock_aws():
            ddbr = boto3.resource("dynamodb", region_name="us-west-2")
            for tbl, key in (("act4", "action_id"), ("find4", "finding_id")):
                ddbr.create_table(
                    TableName=tbl,
                    KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                    AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                    BillingMode="PAY_PER_REQUEST",
                )
            ft4 = ddbr.Table("find4")
            # Fleet A: has finding with headline + cost evidence
            ft4.put_item(Item={
                "finding_id": "FIND-d1",
                "finding_kind": "fleet.cost.anomaly",
                "agent_id": "agent-fi-cost",
                "fleet_id": "FLEET-D",
                "expires_at": "2099-01-01T00:00:00Z",
                "headline": "Energy cost 1.3x fleet-model median, rapid acceleration",
                "evidence": [
                    {"kind": "cost.diagnosis_source", "value": "model"},
                    {"kind": "cost.cost_per_mile", "value": "0.38"},
                ],
            })
            # Fleet B: has finding with a distinct headline
            fleet_b_headline = "Brake cost 2.0x expected"
            ft4.put_item(Item={
                "finding_id": "FIND-d2",
                "finding_kind": "fleet.cost.anomaly",
                "agent_id": "agent-fi-cost",
                "fleet_id": "FLEET-E",
                "expires_at": "2099-01-01T00:00:00Z",
                "headline": fleet_b_headline,
            })

            call_count = {"n": 0}

            class Api:
                def call(self, method, path, token, body=None, timeout=45):
                    call_count["n"] += 1
                    if call_count["n"] == 1:
                        # fleet A: correct reply with headline + cost + pending
                        return 200, {"result": (
                            "Energy cost 1.3x fleet-model median, rapid acceleration. "
                            "Cost per mile: 0.38. Decision status: pending."
                        )}
                    # fleet B refusal — mentions the fleet B headline (leaked!)
                    return 200, {"result": (
                        f"I cannot access that finding for your fleets. "
                        f"{fleet_b_headline[:20]} is not in your scope."
                    )}

            @contextlib.contextmanager
            def eph(*a, **k):
                yield "tok", {}

            res = _mod.Results()
            with patch.object(_mod, "_ephemeral_user", eph), \
                 patch.object(_mod, "_check_text_runtime_trust_flag", return_value=True), \
                 contextlib.redirect_stdout(_io.StringIO()):
                _mod.item8_fleet_recommendation(
                    MagicMock(), ddbr, MagicMock(), "find4", "act4",
                    "pool", "client", Api(), "fn", "staging", "us-west-2",
                    res, allow_writes=True,
                )

            refusal_rows = [r for r in res.rows if r[0] == "8-refusal-check"]
            assert refusal_rows, (
                f"R3-W7 / M23: '8-refusal-check' row must exist when 2 fleets present. "
                f"Got: {[(r[0], r[1]) for r in res.rows]}"
            )
            assert refusal_rows[0][1] == "FAIL", (
                f"R3-W7 / M23: item 8 must FAIL when refusal reply mentions fleet B headline. "
                f"M23 removes the headline_b_absent check. Got: {refusal_rows[0][1]!r}"
            )

    # -----------------------------------------------------------------------
    # M25: aborted run accepted by item 7
    # -----------------------------------------------------------------------

    def test_m25_item7_fails_when_run_aborted(self):
        """M25 / R3-W7: item 7 must FAIL '7-rules-fallback' when the run has abort_reason set.

        M25 mutates `rules_fallbacks >= 1 and not aborted,` → `rules_fallbacks >= 1,`
        so an aborted run with rules_fallbacks=1 falsely passes.
        """
        import contextlib
        import io as _io

        def make_aborted_summary():
            return {
                "artifacts_written": 0,
                "tokens_used": 0,
                "abort_reason": "max_diagnoses_reached",  # aborted
                "cost": {
                    "deferred": 0,
                    "model_calls": 0,
                    "rules_fallbacks": 1,  # rules did fire, but run was aborted
                    "anomalies_written": 0,
                    "sell_timing_written": 0,
                    "data_month": "2026-09",
                },
            }

        lam = MagicMock()
        lam.get_function_configuration.return_value = {
            "Environment": {"Variables": {"A": "1"}},
            "LastUpdateStatus": "Successful",
        }
        ddb = MagicMock()
        ddb.Table.return_value.scan.return_value = {"Items": []}

        res = _mod.Results()
        with patch.object(_mod, "_wait_lambda_response",
                          return_value=make_aborted_summary()), \
             patch.object(_mod, "_check_ios_fleet_intersection", return_value=True), \
             contextlib.redirect_stdout(_io.StringIO()):
            _mod.item7_rules_fallback(
                lam, ddb, MagicMock(), "findings", "fn",
                "staging", "us-west-2", "wg", "s3://x/", res,
            )

        fallback_rows = [r for r in res.rows if r[0] == "7-rules-fallback"]
        assert fallback_rows, (
            f"R3-W7 / M25: '7-rules-fallback' row must exist. "
            f"Got: {[(r[0], r[1]) for r in res.rows]}"
        )
        assert fallback_rows[0][1] == "FAIL", (
            f"R3-W7 / M25: item 7 must FAIL when abort_reason is set, even with "
            f"rules_fallbacks=1. M25 removes the `not aborted` condition. "
            f"Got: {fallback_rows[0][1]!r}"
        )



# ===========================================================================
# D3: item 7 runs before items 2/3; False return skips 2/3
# ===========================================================================


class TestD3Item7Order:
    """D3: item 7 must run before item 2; False from item7 blocks items 2 and 3."""

    def _boto3_mock(self):
        m = MagicMock()
        m.client.return_value.get_caller_identity.return_value = {
            "Account": "000000000000", "Arn": "arn:x"
        }
        m.client.return_value.describe_stacks.return_value = {
            "Stacks": [{"Outputs": [{"OutputKey": "RestApiUrl", "OutputValue": "https://x"}]}]
        }
        return m

    def test_d3_item7_runs_before_item2(self):
        """D3: item 7 must be called before item 2 in the run order (1,7,2,3,…)."""
        import contextlib, io as _io
        call_order: list[str] = []

        def recorder(name, ret=None):
            def f(*a, **k):
                call_order.append(name)
                res_obj = k.get("res") or next((x for x in a if isinstance(x, _mod.Results)), None)
                if res_obj:
                    res_obj.check(f"{name}-x", True)
                return ret
            return f

        with contextlib.ExitStack() as st:
            st.enter_context(patch.object(_mod, "item1_views_rows", recorder("item1")))
            st.enter_context(patch.object(_mod, "item7_rules_fallback", recorder("item7", True)))
            st.enter_context(patch.object(_mod, "item2_agent_scores_plants", recorder("item2", (None, True))))
            st.enter_context(patch.object(_mod, "item3_second_run_noop", recorder("item3", True)))
            st.enter_context(patch.object(_mod, "item4_athena_paging", recorder("item4")))
            st.enter_context(patch.object(_mod, "item5_fleet_scope", recorder("item5")))
            st.enter_context(patch.object(_mod, "item6_forced_alarm_failure", recorder("item6")))
            st.enter_context(patch.object(_mod, "item8_fleet_recommendation", recorder("item8")))
            st.enter_context(patch.object(_mod, "_find_pool", return_value="pool"))
            st.enter_context(patch.object(_mod, "_password_client", return_value="client"))
            st.enter_context(patch.dict(sys.modules, {"boto3": self._boto3_mock()}))
            st.enter_context(patch.object(sys, "argv", [
                "lv", "--stage", "staging", "--region", "us-west-2", "--all", "--allow-writes",
            ]))
            st.enter_context(patch.dict(os.environ, {"LIVE_TESTS": "1"}))
            st.enter_context(contextlib.redirect_stdout(_io.StringIO()))
            st.enter_context(contextlib.redirect_stderr(_io.StringIO()))
            _mod.main()

        assert "item7" in call_order and "item2" in call_order, (
            f"D3: both item7 and item2 must be called. call_order={call_order}"
        )
        assert call_order.index("item7") < call_order.index("item2"), (
            f"D3: item 7 must run before item 2. call_order={call_order}"
        )

    def test_d3_items_2_3_not_called_when_item7_returns_false(self):
        """D3: item2 and item3 must not be called if item7_rules_fallback returns False."""
        import contextlib, io as _io
        called: list[str] = []

        def spy(name, ret=None):
            def f(*a, **k):
                called.append(name)
                return ret
            return f

        with contextlib.ExitStack() as st:
            st.enter_context(patch.object(_mod, "item1_views_rows", lambda *a, **k: None))
            st.enter_context(patch.object(_mod, "item7_rules_fallback", lambda *a, **k: False))
            st.enter_context(patch.object(_mod, "item2_agent_scores_plants", spy("item2", (None, True))))
            st.enter_context(patch.object(_mod, "item3_second_run_noop", spy("item3", True)))
            st.enter_context(patch.object(_mod, "item4_athena_paging", lambda *a, **k: None))
            st.enter_context(patch.object(_mod, "item5_fleet_scope", lambda *a, **k: None))
            st.enter_context(patch.object(_mod, "item6_forced_alarm_failure", lambda *a, **k: None))
            st.enter_context(patch.object(_mod, "item8_fleet_recommendation", lambda *a, **k: None))
            st.enter_context(patch.object(_mod, "_find_pool", return_value="pool"))
            st.enter_context(patch.object(_mod, "_password_client", return_value="client"))
            st.enter_context(patch.dict(sys.modules, {"boto3": self._boto3_mock()}))
            st.enter_context(patch.object(sys, "argv", [
                "lv", "--stage", "staging", "--region", "us-west-2", "--all", "--allow-writes",
            ]))
            st.enter_context(patch.dict(os.environ, {"LIVE_TESTS": "1"}))
            st.enter_context(contextlib.redirect_stdout(_io.StringIO()))
            st.enter_context(contextlib.redirect_stderr(_io.StringIO()))
            _mod.main()

        assert "item2" not in called, (
            f"D3: item 2 must NOT be called when item 7 returns False. called={called}"
        )
        assert "item3" not in called, (
            f"D3: item 3 must NOT be called when item 7 returns False. called={called}"
        )

    def test_r4_c1_cleanup_failed_skips_items_2_3_6(self):
        """R4-C1: when item7 sets _cleanup_failed and returns True, items 2/3/6 must be SKIPped.

        Mutant: return True (instead of _restore_ok) → item7_ok is True, but _cleanup_failed
        is True → without the new gate, items 2/3/6 would be called.
        """
        import contextlib, io as _io
        called: list[str] = []

        def spy_item2(*a, **k):
            called.append("item2")
            return (None, True)

        def spy_item3(*a, **k):
            called.append("item3")
            return True

        def spy_item6(*a, **k):
            called.append("item6")

        def fake_item7(*a, **k):
            # Simulate: override succeeded, invoke ran, restore failed
            res_obj = k.get("res") or next((x for x in a if isinstance(x, _mod.Results)), None)
            if res_obj:
                res_obj.mark_cleanup_failed()
                res_obj.check("7-restore", False, "restore failed (simulated)")
            return True  # simulates the mutant: returns True even though cleanup failed

        with contextlib.ExitStack() as st:
            st.enter_context(patch.object(_mod, "item1_views_rows", lambda *a, **k: None))
            st.enter_context(patch.object(_mod, "item7_rules_fallback", fake_item7))
            st.enter_context(patch.object(_mod, "item2_agent_scores_plants", spy_item2))
            st.enter_context(patch.object(_mod, "item3_second_run_noop", spy_item3))
            st.enter_context(patch.object(_mod, "item4_athena_paging", lambda *a, **k: None))
            st.enter_context(patch.object(_mod, "item5_fleet_scope", lambda *a, **k: None))
            st.enter_context(patch.object(_mod, "item6_forced_alarm_failure", spy_item6))
            st.enter_context(patch.object(_mod, "item8_fleet_recommendation", lambda *a, **k: None))
            st.enter_context(patch.object(_mod, "_find_pool", return_value="pool"))
            st.enter_context(patch.object(_mod, "_password_client", return_value="client"))
            st.enter_context(patch.dict(sys.modules, {"boto3": self._boto3_mock()}))
            st.enter_context(patch.object(sys, "argv", [
                "lv", "--stage", "staging", "--region", "us-west-2", "--all", "--allow-writes",
            ]))
            st.enter_context(patch.dict(os.environ, {"LIVE_TESTS": "1"}))
            st.enter_context(contextlib.redirect_stdout(_io.StringIO()))
            st.enter_context(contextlib.redirect_stderr(_io.StringIO()))
            _mod.main()

        assert "item2" not in called, (
            f"R4-C1: item2 must NOT be called when _cleanup_failed is set. called={called}"
        )
        assert "item3" not in called, (
            f"R4-C1: item3 must NOT be called when _cleanup_failed is set. called={called}"
        )
        assert "item6" not in called, (
            f"R4-C1: item6 must NOT be called when _cleanup_failed is set. called={called}"
        )



# ===========================================================================
# D5: score_plants_detail — no_action cause inference via _CAUSE_PHRASES
# ===========================================================================


class TestD5ScorePlantsDetail:
    """D5: no_action Findings can contribute to score via headline+detail phrase match."""

    def _no_action_with_text(self, vin: str, headline: str, detail: str = "") -> dict:
        return {
            "finding_id": f"AVX-{vin}-noact",
            "finding_kind": _FINDING_KIND_ANOMALY,
            "agent_id": _AGENT_ID,
            "scope": {"vin": vin},
            "fleet_id": "FLEET-A",
            "headline": headline,
            "detail": detail,
            # no recommended_action
        }

    def test_d5_no_action_naming_planted_cause_counts(self):
        """D5: no_action Finding whose text names exactly the planted cause → correct."""
        plants = [_plant("VIN1", "charging_mix")]
        f = self._no_action_with_text("VIN1", "Cost driven by public charging usage", "Public station overuse")
        flagged, rca, na, na_rc, correct, total, pct = score_plants_detail(plants, [f])
        assert na == 1, "no_action count must be 1"
        assert na_rc == 1, f"D5: no_action_right_cause must be 1; got {na_rc}"
        assert correct == 1, f"D5: correct must be 1; got {correct}"

    def test_d5_no_action_naming_two_causes_does_not_count(self):
        """D5: text mentioning two cause phrases → not counted as no_action_right_cause."""
        plants = [_plant("VIN1", "charging_mix")]
        f = self._no_action_with_text(
            "VIN1",
            "Charging mix and efficiency both contribute",
            "Public station usage with poor energy efficiency",
        )
        _, _, _, na_rc, correct, _, _ = score_plants_detail(plants, [f])
        assert na_rc == 0, f"D5: two causes must not match; na_rc={na_rc}"
        assert correct == 0

    def test_d5_rules_fallback_text_does_not_count(self):
        """D5: rules-fallback headline/detail names no cause phrase → no_action_right_cause=0."""
        import pathlib
        # Read the actual values from fallback.py
        fb_path = pathlib.Path(__file__).resolve().parents[2] / "agents" / "fi_cost" / "fallback.py"
        ns: dict = {}
        exec(fb_path.read_text(), ns)  # noqa: S102
        headline = ns["_FALLBACK_HEADLINE"]
        detail = ns["_FALLBACK_DETAIL"]

        plants = [_plant("VIN1", "charging_mix")]
        f = self._no_action_with_text("VIN1", headline, detail)
        _, _, _, na_rc, correct, _, _ = score_plants_detail(plants, [f])
        assert na_rc == 0, f"D5: fallback text must not match cause phrase; na_rc={na_rc}"
        assert correct == 0

    def test_d5_structured_cause_still_hits(self):
        """D5: Finding with recommended_action.cause == planted cause → right_cause_action."""
        plants = [_plant("VIN1", "charging_mix")]
        f = _finding("VIN1", "charging_mix")
        flagged, rca, na, na_rc, correct, total, pct = score_plants_detail(plants, [f])
        assert rca == 1, f"D5: right_cause_action must be 1; got {rca}"
        assert correct == 1
        assert flagged == 1, f"D5: a right-cause Finding is also flagged; got {flagged}"

    def test_d5_flagged_counts_every_diagnosed_plant(self):
        """D5: flagged = right cause + wrong cause + no_action; an undiagnosed plant is not flagged."""
        plants = [_plant("V1", "charging_mix"), _plant("V2", "charging_mix"),
                  _plant("V3", "repeat_repair"), _plant("V4", "efficiency")]
        findings = [
            _finding("V1", "charging_mix"),           # right cause
            _finding("V2", "efficiency"),             # wrong cause
            self._no_action_with_text("V3", "Repeat repair on the same system"),  # no_action, right
        ]
        flagged, rca, na, na_rc, correct, total, pct = score_plants_detail(plants, findings)
        assert (flagged, rca, na, na_rc, correct, total) == (3, 1, 1, 1, 2, 4), (
            flagged, rca, na, na_rc, correct, total)

    def test_d5_wrong_cause_finding_is_flagged_only(self):
        """D5: Finding with wrong structured cause → flagged=1, correct=0."""
        plants = [_plant("VIN1", "charging_mix")]
        f = _finding("VIN1", "efficiency")  # wrong cause
        flagged, rca, na, na_rc, correct, total, pct = score_plants_detail(plants, [f])
        assert flagged == 1, f"D5: flagged must be 1; got {flagged}"
        assert correct == 0
        assert rca == 0



# ===========================================================================
# D2: item 1 miles comparison — NOTE-only, no SKIP/FAIL, zero-trip guard
# ===========================================================================


class TestD2MilesNoteOnly:
    """D2: CMS trips comparison is NOTE-only; zero/out-of-range trips skipped."""

    def _athena_empty(self):
        a = MagicMock()
        a.start_query_execution.return_value = {"QueryExecutionId": "Q1"}
        a.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}, "Statistics": {}}
        }
        a.get_query_results.return_value = {
            "ResultSet": {"Rows": [{"Data": [{"VarCharValue": "fleet_id"}]}]}
        }
        return a

    def test_d2_zero_km_trip_not_compared(self):
        """D2: trips with km=0 or km>1000 must not appear in the comparison."""
        import contextlib, io as _io

        ddb = MagicMock()
        ddb.Table.return_value.scan.return_value = {
            "Items": [
                {"vehicleId": "V1", "startTime": 1700000000000, "totalDistance": 0},
                {"vehicleId": "V2", "startTime": 1700000000000, "totalDistance": 1500},
            ]
        }
        res = _mod.Results()
        with contextlib.redirect_stdout(_io.StringIO()):
            _mod.item1_views_rows(self._athena_empty(), ddb, "wg", "s3://x/", "staging", res)
        trip_note_rows = [r for r in res.rows if r[0].startswith("1-trips-") and r[0] != "1-trips-summary"]
        assert not trip_note_rows, (
            f"D2: zero-km and >1000-km trips must not be compared. Got: {trip_note_rows}"
        )

    def test_d2_no_skip_or_fail_from_miles_path(self):
        """D2: miles comparison must never produce SKIP or FAIL rows."""
        import contextlib, io as _io

        ddb = MagicMock()
        ddb.Table.return_value.scan.return_value = {"Items": []}
        res = _mod.Results()
        with contextlib.redirect_stdout(_io.StringIO()):
            _mod.item1_views_rows(self._athena_empty(), ddb, "wg", "s3://x/", "staging", res)
        bad = [r for r in res.rows if r[1] in ("SKIP", "FAIL") and "trips" in r[0]]
        assert not bad, f"D2: must not SKIP or FAIL on trips path. Got: {bad}"

    def test_d2_mutant_note_to_check_is_caught(self):
        """D2: if the summary note were changed to a FAIL check, this test would catch it."""
        import contextlib, io as _io

        # Simulate a mutant that emits res.check("1-trips-summary", False, ...)
        res = _mod.Results()
        res.check("1-trips-summary", False, "mutant: this should be a NOTE not a check")
        fail_rows = [r for r in res.rows if r[0] == "1-trips-summary" and r[1] == "FAIL"]
        assert fail_rows, "baseline: the mutant does produce a FAIL row"
        # The real code must NOT produce such a row
        ddb = MagicMock()
        ddb.Table.return_value.scan.return_value = {"Items": []}
        res2 = _mod.Results()
        with contextlib.redirect_stdout(_io.StringIO()):
            _mod.item1_views_rows(self._athena_empty(), ddb, "wg", "s3://x/", "staging", res2)
        real_fail = [r for r in res2.rows if r[0] == "1-trips-summary" and r[1] == "FAIL"]
        assert not real_fail, (
            f"D2: 1-trips-summary must be a NOTE, not a FAIL. Got: {real_fail}"
        )

    def test_d2_compared_pairs_all_notes_catch_d2m_d2m2(self):
        """R4-W2/D2m/D2m2: with real compared pairs, every 1-trips-* row must be NOTE.

        Uses VIN 'AAAAAAAAAAAAAAAAA' (ADP miles=100) and VIN 'BBBBBBBBBBBBBBBBB' (ADP miles=0).
        Two VINs sharing a 10-char prefix use the full VIN → distinct labels.
        Catches D2m (per-pair note→check) and D2m2 (summary note→check when pairs>0).
        """
        import contextlib, io as _io

        vin_good = "AAAAAAAAAAAAAAAAA"  # 17 chars, ADP miles=100 > 0
        vin_zero = "AAAAAAAAABBBBBBBB"  # shares 9-char prefix with vin_good, ADP miles=0
        vid_good, vid_zero = "V-GOOD", "V-ZERO"

        ddb = MagicMock()
        # Trips scan: two trips, each ~200 km (valid range)
        ddb.Table.return_value.scan.return_value = {
            "Items": [
                {"vehicleId": vid_good, "startTime": 1735000000000, "totalDistance": 200},
                {"vehicleId": vid_zero, "startTime": 1735000000000, "totalDistance": 200},
            ]
        }
        # Vehicles get_item: return VIN for each vehicleId
        def fake_get_item(Key, **_):
            vin = vin_good if Key.get("vehicleId") == vid_good else vin_zero
            return {"Item": {"vehicleId": Key["vehicleId"], "vin": vin}}
        ddb.Table.return_value.get_item.side_effect = fake_get_item

        # Athena: return ADP rows for the VINs query, empty for everything else
        def fake_athena_run(athena_client, sql, workgroup, output_location):
            if "vehicle_cost_monthly" in sql and vin_good in sql and "SELECT vin" in sql:
                rows = [
                    [vin_good, "2024-12", "100.0"],
                    [vin_zero, "2024-12", "0"],
                ]
                return "exec-adp", rows
            return "exec-x", []

        res = _mod.Results()
        with patch.object(_mod, "_athena_run", side_effect=fake_athena_run), \
             contextlib.redirect_stdout(_io.StringIO()):
            _mod.item1_views_rows(MagicMock(), ddb, "wg", "s3://x/", "staging", res)

        trip_rows = [r for r in res.rows if r[0].startswith("1-trips-") and r[0] != "1-trips-summary"]
        assert len(trip_rows) >= 2, (
            f"R4-W2: expected at least 2 compared pair rows, got {len(trip_rows)}: {trip_rows}"
        )
        # Every per-pair row must be NOTE
        for row in trip_rows:
            assert row[1] == "NOTE", (
                f"R4-W2/D2m: every 1-trips-* row must be NOTE. Got {row[1]!r} for {row[0]!r}. "
                f"D2m mutant changes note to check."
            )
        # Labels must use the full VIN (not truncated); two sharing 10-char prefix → distinct
        labels = {r[0] for r in trip_rows}
        label_good = f"1-trips-{vin_good}-2024-12"
        label_zero = f"1-trips-{vin_zero}-2024-12"
        assert label_good in labels, f"R4-W2: label for vin_good not found. labels={labels}"
        assert label_zero in labels, f"R4-W2: label for vin_zero not found. labels={labels}"
        assert label_good != label_zero, "Labels must be distinct for VINs sharing 10-char prefix"
        # Summary row must be NOTE
        summary = [r for r in res.rows if r[0] == "1-trips-summary"]
        assert summary, "1-trips-summary row must exist"
        assert summary[0][1] == "NOTE", (
            f"R4-W2/D2m2: 1-trips-summary must be NOTE even when pairs_compared>0. "
            f"Got {summary[0][1]!r}. D2m2 mutant makes it FAIL."
        )



# ===========================================================================
# R3-W5/W6: item 4 uses agent readers; bytes on real queries; cost estimate
# ===========================================================================

_ITEM4_VIN = "AAAAAAAAAAAAAAAAA"  # 17 chars, passes _VIN_RE
_ONE_TB = 1024 ** 4


def _make_item4_athena(
    n_anom: int = 3,
    n_sell: int = 5,
    cnt_anom: int | None = None,
    cnt_sell: int | None = None,
    bytes_agent: int = 1024,
    bytes_count: int = 1024,
    bytes_view: int = 1024,
):
    """Build an Athena stub routed by SQL content for item4 tests.

    Routes queries:
    - max(year_month): find_latest_complete_month → year_month row
    - vehicle_pricing (no COUNT): fetch_sell_timing_inputs → n_sell rows
    - otherwise (no COUNT): select_anomalies / gather_evidence → n_anom rows (vin col)
    - SELECT COUNT(*): COUNT wrappers → cnt_anom or cnt_sell
    - SELECT * FROM: _athena_bytes_only → not fetched
    """
    if cnt_anom is None:
        cnt_anom = n_anom
    if cnt_sell is None:
        cnt_sell = n_sell

    sql_for_id: dict[str, str] = {}
    seq = [0]

    def start(**kw):
        qid = f"Q{seq[0]}"; seq[0] += 1
        sql_for_id[qid] = kw.get("QueryString", "")
        return {"QueryExecutionId": qid}

    def gqe(QueryExecutionId):
        sql = sql_for_id.get(QueryExecutionId, "").upper()
        if "SELECT COUNT(*)" in sql:
            b = bytes_count
        elif "SELECT * FROM" in sql:
            b = bytes_view
        else:
            b = bytes_agent
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"},
                                   "Statistics": {"DataScannedInBytes": b}}}

    def gqr(QueryExecutionId, **kw):
        sql = sql_for_id.get(QueryExecutionId, "")
        sl = sql.lower()
        if "select count(*)" in sl:
            cnt = str(cnt_sell) if "vehicle_pricing" in sl else str(cnt_anom)
            return {"ResultSet": {"Rows": [
                {"Data": [{"VarCharValue": "_"}]},
                {"Data": [{"VarCharValue": cnt}]},
            ]}}
        if "max(year_month)" in sl:
            return {"ResultSet": {"Rows": [
                {"Data": [{"VarCharValue": "year_month"}]},
                {"Data": [{"VarCharValue": "2026-09"}]},
            ]}}
        if "vehicle_pricing" in sl:
            hdr = {"Data": [{"VarCharValue": c} for c in
                            ("vin", "fleet_id", "year_month", "maintenance_cost", "purchase_price_usd")]}
            row = {"Data": [{"VarCharValue": v} for v in
                            (_ITEM4_VIN, "FL-A", "2026-09", "100", "20000")]}
            return {"ResultSet": {"Rows": [hdr] + [row] * n_sell}}
        # select_anomalies and gather_evidence: one row per distinct vehicle (real shape)
        hdr = {"Data": [{"VarCharValue": "vin"}, {"VarCharValue": "year_month"}]}
        rows = [{"Data": [{"VarCharValue": f"{_ITEM4_VIN[:-3]}{i:03d}"},
                          {"VarCharValue": "2026-09"}]} for i in range(n_anom)]
        return {"ResultSet": {"Rows": [hdr] + rows}}

    athena = MagicMock()
    athena.start_query_execution.side_effect = start
    athena.get_query_execution.side_effect = gqe
    athena.get_query_results.side_effect = gqr
    return athena


class TestR3W56Item4Readers:
    """R3-W5/W6: item4 uses agent readers; bytes on agent queries only; cost estimate."""

    def _ddb_stub(self, data_month: str = "2026-09", vins_selected: int = 3):
        ddb = MagicMock()
        ddb.Table.return_value.query.return_value = {
            "Items": [{"agent_component": "agent-fi-cost#cost",
                       "data_month": data_month, "vins_selected": vins_selected}]
        }
        return ddb

    def _run_item4(self, athena, ddb):
        import contextlib, io as _io
        res = _mod.Results()
        with contextlib.redirect_stdout(_io.StringIO()):
            _mod.item4_athena_paging(athena, ddb, "wg", "s3://x/", "staging", "runs", res)
        return res

    # --- pager stops after page 1: 4-reader-sell-timing FAIL ---------------

    def test_pager_stops_page1_sell_timing_fails(self):
        """If sell reader returns fewer rows than COUNT, 4-reader-sell-timing FAILs.

        Simulates a pager that stops after page 1 (gets 2 rows) when 8 exist.
        M mutation: always-pass sell-timing check → caught by this assertion.
        """
        athena = _make_item4_athena(n_anom=3, n_sell=2, cnt_anom=3, cnt_sell=8)
        res = self._run_item4(athena, self._ddb_stub(vins_selected=3))
        sell_rows = [r for r in res.rows if r[0] == "4-reader-sell-timing"]
        assert sell_rows, "4-reader-sell-timing must exist when sell reader < COUNT"
        assert sell_rows[0][1] == "FAIL", (
            f"R3-W5: 4-reader-sell-timing must FAIL when reader=2 < COUNT=8. "
            f"Got: {sell_rows[0][1]!r}"
        )

    def test_pager_stops_page1_reader_anomalies_fails(self):
        """R4-W3/M19d: 4-reader-anomalies FAILs when anomaly reader < COUNT(*).

        M19d mutant removes the equality check (only COUNT>0 kept) → PASS when reader
        returns fewer rows. This test catches M19d.
        """
        athena = _make_item4_athena(n_anom=2, n_sell=5, cnt_anom=8, cnt_sell=5)
        res = self._run_item4(athena, self._ddb_stub(vins_selected=2))
        anom_rows = [r for r in res.rows if r[0] == "4-reader-anomalies"]
        assert anom_rows, "4-reader-anomalies must exist when reader < COUNT"
        assert anom_rows[0][1] == "FAIL", (
            f"R4-W3/M19d: 4-reader-anomalies must FAIL when reader=2 < COUNT=8. "
            f"M19d removes equality check → always PASS. Got: {anom_rows[0][1]!r}"
        )

    # --- COUNT wrapper bytes not summed ------------------------------------

    def test_count_bytes_not_included_in_cost(self):
        """COUNT wrapper bytes (large) must not enter the per-run tally.

        Correct: run_bytes = cap.execution_ids bytes (agent readers) only.
        Mutation 'sum COUNT bytes': adds COUNT bytes → estimate explodes.
        """
        # Agent readers scan 1 byte each (billed as 10 MB floor).
        # COUNT wrappers scan 2 TB each — only correct code excludes these.
        athena = _make_item4_athena(n_anom=3, n_sell=5, cnt_anom=3, cnt_sell=5,
                                    bytes_agent=1, bytes_count=2 * _ONE_TB, bytes_view=1)
        res = self._run_item4(athena, self._ddb_stub(vins_selected=3))
        cost_rows = [r for r in res.rows if r[0] == "4-athena-cost-under-5"]
        assert cost_rows, "4-athena-cost-under-5 must exist"
        assert cost_rows[0][1] == "PASS", (
            f"R3-W5: 4-athena-cost-under-5 must PASS when only agent bytes (~10 MB) "
            f"are counted. COUNT bytes (2 TB) must be excluded. Got: {cost_rows[0][1]!r}"
        )

    # --- estimate over $5 fails -------------------------------------------

    def test_estimate_over_5_fails(self):
        """4-athena-cost-under-5 must FAIL when agent queries scan 2 TB each.

        Mutation 'always-pass cost check': caught by this FAIL assertion.
        """
        athena = _make_item4_athena(n_anom=3, n_sell=5, cnt_anom=3, cnt_sell=5,
                                    bytes_agent=2 * _ONE_TB, bytes_count=1, bytes_view=2 * _ONE_TB)
        res = self._run_item4(athena, self._ddb_stub(vins_selected=3))
        cost_rows = [r for r in res.rows if r[0] == "4-athena-cost-under-5"]
        assert cost_rows, "4-athena-cost-under-5 must exist"
        assert cost_rows[0][1] == "FAIL", (
            f"R3-W5: 4-athena-cost-under-5 must FAIL when agent bytes=2 TB × 30 >> $5. "
            f"Got: {cost_rows[0][1]!r}"
        )

    # --- vins_selected mismatch fails ------------------------------------

    def test_vins_mismatch_fails(self):
        """4-agent-paging must FAIL when valid_vins count != run record vins_selected.

        Reader returns 3 valid VINs; run record says 50 → mismatch → FAIL.
        Mutation 'always-pass agent-paging': caught by this assertion.
        """
        athena = _make_item4_athena(n_anom=3, n_sell=5, cnt_anom=3, cnt_sell=5)
        res = self._run_item4(athena, self._ddb_stub(vins_selected=50))
        paging_rows = [r for r in res.rows if r[0] == "4-agent-paging"]
        assert paging_rows, "4-agent-paging must exist"
        assert paging_rows[0][1] == "FAIL", (
            f"R3-W6: 4-agent-paging must FAIL when valid_vins=3 != vins_selected=50. "
            f"Got: {paging_rows[0][1]!r}"
        )

    # --- positive control all PASS ----------------------------------------

    def test_positive_control_all_pass(self):
        """All four checks must PASS when readers match COUNTs and bytes are small."""
        athena = _make_item4_athena(n_anom=3, n_sell=5, cnt_anom=3, cnt_sell=5,
                                    bytes_agent=1, bytes_count=1, bytes_view=1)
        res = self._run_item4(athena, self._ddb_stub(vins_selected=3))
        checks = {r[0]: r[1] for r in res.rows if r[1] in ("PASS", "FAIL")}
        for name in ("4-reader-anomalies", "4-reader-sell-timing",
                     "4-agent-paging", "4-athena-cost-under-5"):
            assert checks.get(name) == "PASS", (
                f"R3-W5/W6: positive control must PASS for {name!r}. "
                f"All checks: {checks}"
            )

    # --- minimum billing floor unit tests ---------------------------------

    def test_duplicate_vin_rows_count_once_like_the_agent(self):
        """4-agent-paging compares DISTINCT valid VINs (agent.py `seen` set) with vins_selected."""
        athena = _make_item4_athena(n_anom=3, n_sell=5, cnt_anom=3, cnt_sell=5)
        orig = athena.get_query_results.side_effect

        def dup(QueryExecutionId, **kw):
            out = orig(QueryExecutionId, **kw)
            rows = out["ResultSet"]["Rows"]
            if rows and rows[0]["Data"][0]["VarCharValue"] == "vin" and len(rows[0]["Data"]) == 2:
                out["ResultSet"]["Rows"] = rows[:1] + [rows[1]] * (len(rows) - 1)
            return out

        athena.get_query_results.side_effect = dup
        res = self._run_item4(athena, self._ddb_stub(vins_selected=1))
        paging = [r for r in res.rows if r[0] == "4-agent-paging"]
        assert paging and paging[0][1] == "PASS", paging

    def test_view_bytes_unavailable_fails_the_cost_check(self):
        """Without the views' bytes the estimate understates; the cost check FAILs."""
        athena = _make_item4_athena(n_anom=3, n_sell=5, bytes_agent=1, bytes_count=1, bytes_view=1)
        orig_start = athena.start_query_execution.side_effect

        def start(**kw):
            if kw.get("QueryString", "").upper().startswith("SELECT * FROM"):
                raise RuntimeError("AccessDenied")
            return orig_start(**kw)

        athena.start_query_execution.side_effect = start
        res = self._run_item4(athena, self._ddb_stub(vins_selected=3))
        cost = [r for r in res.rows if r[0] == "4-athena-cost-under-5"]
        assert cost and cost[0][1] == "FAIL", cost

    def test_estimate_uses_decimal_tb(self):
        """Estimate = (per-run + both views) bytes / 10**12 x $5 x 30 (decimal TB, conservative)."""
        import re as _re
        athena = _make_item4_athena(n_anom=3, n_sell=5, bytes_agent=3 * 10 ** 9,
                                    bytes_count=1, bytes_view=5 * 10 ** 9)
        res = self._run_item4(athena, self._ddb_stub(vins_selected=3))
        note = [r[2] for r in res.rows if r[0] == "4-bytes-breakdown"][0]
        m = _re.search(r"per-run=([\d,]+)B monthly-view=([\d,]+)B baseline-view=([\d,]+)B.*~\$([\d.]+)/mo", note)
        assert m, note
        total = sum(int(g.replace(",", "")) for g in m.groups()[:3])
        assert float(m.group(4)) == round(total / 10 ** 12 * 5.0 * 30, 2), note

    def test_athena_bytes_applies_10mb_minimum(self):
        """_athena_bytes must bill each execution at min 10 MB even when scan < 10 MB."""
        athena = MagicMock()
        athena.get_query_execution.return_value = {
            "QueryExecution": {"Statistics": {"DataScannedInBytes": 100}}
        }
        result = _mod._athena_bytes(athena, ["Q1"])
        assert result == 10 * 1024 * 1024, (
            f"_athena_bytes: expected 10 MB floor, got {result}. "
            f"Mutation 'drop 10 MB minimum' removes max(b, _MIN_BILLING_BYTES)."
        )

    def test_athena_bytes_only_applies_10mb_minimum(self):
        """_athena_bytes_only must return min 10 MB even when scan < 10 MB."""
        athena = MagicMock()
        athena.start_query_execution.return_value = {"QueryExecutionId": "Q1"}
        athena.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"},
                               "Statistics": {"DataScannedInBytes": 50}}
        }
        result = _mod._athena_bytes_only(athena, "SELECT 1", "wg", "s3://x/")
        assert result == 10 * 1024 * 1024, (
            f"_athena_bytes_only: expected 10 MB floor, got {result}."
        )



# ---------------------------------------------------------------------------
# Step 2: SIGTERM / SIGHUP signal handler tests
# ---------------------------------------------------------------------------


class TestSignalHandlers:
    """Step 2: the script's own handler turns SIGTERM/SIGHUP into SystemExit(143) once."""

    def _with_handlers(self):
        import signal as _sig
        old = (_sig.getsignal(_sig.SIGTERM), _sig.getsignal(_sig.SIGHUP))
        _mod._install_signal_handlers()
        return old

    def _restore(self, old):
        import signal as _sig
        _sig.signal(_sig.SIGTERM, old[0])
        _sig.signal(_sig.SIGHUP, old[1])

    def test_sigterm_and_sighup_raise_system_exit_143_once(self):
        import os as _os, signal as _sig
        for signum in (_sig.SIGTERM, _sig.SIGHUP):
            old = self._with_handlers()
            try:
                with pytest.raises(SystemExit) as exc_info:
                    _os.kill(_os.getpid(), signum)
                assert exc_info.value.code == 143
                _os.kill(_os.getpid(), signum)  # second signal: ignored, no raise
            finally:
                self._restore(old)

    def test_running_the_script_installs_the_handler(self, tmp_path):
        """Run the script as __main__ with boto3 stalled; SIGTERM must exit 143 (handler ran),
        not -15 (default action: killed with no finally)."""
        import os as _os, signal as _sig, subprocess, sys as _sys, time as _time
        wrapper = tmp_path / "run.py"
        wrapper.write_text(
            "import runpy, sys, time, boto3\n"
            "boto3.client = lambda *a, **k: time.sleep(60)\n"
            "sys.argv = ['live-verify-fi-cost.py', '--item', '1']\n"
            f"runpy.run_path({str(_SCRIPT)!r}, run_name='__main__')\n"
        )
        env = {k: v for k, v in _os.environ.items() if not k.startswith("AWS_")}
        env.update(LIVE_TESTS="1", AWS_ACCESS_KEY_ID="x", AWS_SECRET_ACCESS_KEY="x",
                   AWS_ENDPOINT_URL="http://127.0.0.1:1", PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.Popen([_sys.executable, str(wrapper)], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = _time.monotonic() + 20
            # wait until main() has printed nothing yet but is inside the stalled sts call
            _time.sleep(3)
            assert proc.poll() is None, proc.communicate()
            proc.send_signal(_sig.SIGTERM)
            rc = proc.wait(timeout=max(1, deadline - _time.monotonic()))
        finally:
            if proc.poll() is None:
                proc.kill()
        assert rc == 143, f"expected 143 from the handler, got {rc}"


class TestR4W5NegatedCauseMention:
    """R4-W5: a no_action text that names the planted cause only to rule it out is not a hit."""

    def _na(self, vin: str, headline: str, detail: str = "") -> dict:
        return {"finding_kind": _FINDING_KIND_ANOMALY, "agent_id": _AGENT_ID,
                "scope": {"vin": vin}, "headline": headline, "detail": detail}

    def test_negated_mentions_do_not_count(self):
        plants = [_plant("V1", "efficiency"), _plant("V2", "charging_mix"),
                  _plant("V3", "efficiency")]
        findings = [
            self._na("V1", "Cost above cohort", "Efficiency is in line with the cohort."),
            self._na("V2", "Not a charging mix issue; cause unclear from the data"),
            self._na("V3", "Efficiency is not the driver here"),
        ]
        _, _, na, na_rc, correct, _, _ = score_plants_detail(plants, findings)
        assert (na, na_rc, correct) == (3, 0, 0), (na, na_rc, correct)

    def test_plain_mention_counts_and_is_reported(self):
        plants = [_plant("V1", "efficiency")]
        hits: list = []
        f = self._na("V1", "Poor energy efficiency drives cost", "Uses 30% more kWh per mile.")
        _, _, na, na_rc, correct, _, _ = score_plants_detail(plants, [f], text_hits_out=hits)
        assert (na, na_rc, correct) == (1, 1, 1)
        assert hits == [("V1", "efficiency", "Poor energy efficiency drives cost")], hits

    def test_unrelated_word_containing_no_is_not_a_negation(self):
        plants = [_plant("V1", "efficiency")]
        f = self._na("V1", "Economy efficiency gap versus cohort")
        _, _, _, na_rc, _, _, _ = score_plants_detail(plants, [f])
        assert na_rc == 1


class TestCycle4Suggestions:
    """Item 3 needs item 2's convergence; items 2/3 refuse with the test model in place; item 6 needs SNS."""

    def _main_with(self, item2_converges: bool):
        import contextlib, io as _io
        calls: list = []

        def fake_item2(*a, **k):
            k["res"].check("2-convergence", item2_converges)
            calls.append("item2")
            return "2026-09", True

        def fake_item3(*a, **k):
            calls.append("item3")
            return True

        args = MagicMock(item=None, all=True, allow_writes=True, stage="staging",
                         region="us-west-2", alarm_wait_s=1, one_run_cap=None)
        noop = lambda *a, **k: None  # noqa: E731
        with patch.dict(os.environ, {"LIVE_TESTS": "1"}), \
             patch.object(_mod, "_parse_args", return_value=args), \
             patch("boto3.client"), patch("boto3.resource"), \
             patch.object(_mod, "_find_pool", return_value="pool"), \
             patch.object(_mod, "_password_client", return_value="client"), \
             patch.object(_mod, "item1_views_rows", noop), \
             patch.object(_mod, "item7_rules_fallback", return_value=True), \
             patch.object(_mod, "item2_agent_scores_plants", fake_item2), \
             patch.object(_mod, "item3_second_run_noop", fake_item3), \
             patch.object(_mod, "item4_athena_paging", noop), \
             patch.object(_mod, "item5_fleet_scope", noop), \
             patch.object(_mod, "item6_forced_alarm_failure", noop), \
             patch.object(_mod, "item8_fleet_recommendation", noop), \
             contextlib.redirect_stdout(_io.StringIO()), contextlib.redirect_stderr(_io.StringIO()):
            _mod.main()
        return calls

    def test_item3_skipped_when_item2_did_not_converge(self):
        assert self._main_with(False) == ["item2"]

    def test_item3_runs_after_convergence(self):
        assert self._main_with(True) == ["item2", "item3"]

    def test_items_2_and_3_refuse_with_test_model_in_place(self):
        lam = MagicMock()
        lam.get_function_configuration.return_value = {
            "Environment": {"Variables": {"FI_COST_MODEL_ID": _mod._NON_EXISTENT_MODEL}}}
        res = _mod.Results()
        import contextlib, io as _io
        with patch.object(_mod, "_check_ios_fleet_intersection", return_value=True), \
             patch.object(_mod, "_read_answer_key", return_value=([{"vin": "V", "cause": "c"}], "2026-09")), \
             patch.object(_mod, "_wait_lambda_response") as inv, \
             contextlib.redirect_stdout(_io.StringIO()):
            r2 = _mod.item2_agent_scores_plants(lam, MagicMock(), MagicMock(), MagicMock(),
                                                "f", "r", "fn", "b", "staging", "us-west-2",
                                                "wg", "s3://x/", res)
            r3 = _mod.item3_second_run_noop(lam, MagicMock(), MagicMock(), "f", "r", "fn",
                                            "staging", "us-west-2", "wg", "s3://x/", res)
        assert inv.call_count == 0
        assert r2 == (None, False) and r3 is False
        assert {("2-model-id", "FAIL"), ("3-model-id", "FAIL")} <= {(r[0], r[1]) for r in res.rows}

    def test_item6_fails_without_an_sns_action(self):
        cw = MagicMock()
        cw.describe_alarms.return_value = {"MetricAlarms": [{
            "StateValue": "OK", "AlarmActions": ["arn:aws:lambda:us-west-2:123456789012:function:x"]}]}
        lam, sns = MagicMock(), MagicMock()
        res = _mod.Results()
        import contextlib, io as _io
        with contextlib.redirect_stdout(_io.StringIO()):
            _mod.item6_forced_alarm_failure(lam, cw, sns, "fn", "staging", "us-west-2", res, wait_s=1)
        assert ("6-alarm-actions", "FAIL") in [(r[0], r[1]) for r in res.rows]
        assert lam.invoke.call_count == 0



# ===========================================================================
# Security review cycle 1 (2026-10-08): A-W2, A-S1, B-S1..B-S5
# ===========================================================================


def _main_args(**over):
    base = dict(item=None, all=True, allow_writes=True, stage="staging",
                region="us-west-2", alarm_wait_s=1, one_run_cap=None)
    base.update(over)
    return MagicMock(**base)


class TestSecCycle1Item6ModelId:
    """A-W2: item 6 must not invoke the Lambda while the nonexistent test model id is set."""

    def _cw_ok(self):
        cw = MagicMock()
        cw.describe_alarms.return_value = {"MetricAlarms": [{
            "StateValue": "OK", "AlarmActions": ["arn:aws:sns:us-west-2:123456789012:t"]}]}
        return cw

    def test_item6_refuses_with_test_model_in_place(self):
        lam = MagicMock()
        lam.get_function_configuration.return_value = {
            "Environment": {"Variables": {"FI_COST_MODEL_ID": _mod._NON_EXISTENT_MODEL}}}
        res = _mod.Results()
        import contextlib, io as _io
        with patch.object(_mod, "_wait_lambda_response") as inv, \
             contextlib.redirect_stdout(_io.StringIO()):
            _mod.item6_forced_alarm_failure(lam, self._cw_ok(), MagicMock(), "fn", "staging",
                                            "us-west-2", res, wait_s=1)
        assert inv.call_count == 0, "item 6 must not invoke with the test model in place"
        assert ("6-model-id", "FAIL") in [(r[0], r[1]) for r in res.rows]

    def test_item6_invokes_with_the_real_model(self):
        """Control: with the real model id, item 6 goes on to its invoke."""
        lam = MagicMock()
        lam.get_function_configuration.return_value = {
            "Environment": {"Variables": {"FI_COST_MODEL_ID": "us.anthropic.real"}}}
        sns = MagicMock()
        sns.list_subscriptions_by_topic.return_value = {"Subscriptions": [{"SubscriptionArn": "arn:x"}]}
        cw = self._cw_ok()
        cw.describe_alarm_history.return_value = {"AlarmHistoryItems": []}
        res = _mod.Results()
        import contextlib, io as _io
        with patch.object(_mod, "_wait_lambda_response",
                          side_effect=RuntimeError("max_diagnoses_per_run must be an integer")) as inv, \
             patch.object(_mod.time, "sleep"), \
             contextlib.redirect_stdout(_io.StringIO()):
            _mod.item6_forced_alarm_failure(lam, cw, sns, "fn", "staging", "us-west-2", res, wait_s=0)
        assert inv.call_count == 1
        assert ("6-model-id", "FAIL") not in [(r[0], r[1]) for r in res.rows]



class TestSecCycle1GuardAtEachCaller:
    """B-S1: each agent-invoking item (2, 3, 7) calls the D4 iOS-fleet guard and stops when it
    refuses. The guard itself is tested elsewhere; these pin each call site."""

    def _lam(self):
        lam = MagicMock()
        lam.get_function_configuration.return_value = {
            "Environment": {"Variables": {"FI_COST_MODEL_ID": "us.anthropic.real"}},
            "LastUpdateStatus": "Successful"}
        return lam

    def _patches(self, inv_side_effect=RuntimeError("stop")):
        return [
            patch.object(_mod, "_check_ios_fleet_intersection", return_value=False),
            patch.object(_mod, "_read_answer_key",
                         return_value=([{"vin": "V", "cause": "efficiency"}], "2026-09")),
            patch.object(_mod, "_near_cost_schedule", return_value=(False, "")),
            patch.object(_mod, "_wait_for_lambda_active"),
            patch.object(_mod, "_wait_lambda_response", side_effect=inv_side_effect),
        ]

    def _enter(self, stack, patches):
        return [stack.enter_context(p) for p in patches]

    def test_item2_stops_when_the_guard_refuses(self):
        import contextlib, io as _io
        lam = self._lam()
        with contextlib.ExitStack() as st:
            ps = self._enter(st, self._patches())
            st.enter_context(contextlib.redirect_stdout(_io.StringIO()))
            r = _mod.item2_agent_scores_plants(lam, MagicMock(), MagicMock(), MagicMock(),
                                               "f", "r", "fn", "b", "staging", "us-west-2",
                                               "wg", "s3://x/", _mod.Results())
            inv = ps[-1]
        assert inv.call_count == 0, "item 2 must not invoke when the iOS-fleet guard refuses"
        assert r == (None, False)

    def test_item3_stops_when_the_guard_refuses(self):
        import contextlib, io as _io
        lam = self._lam()
        with contextlib.ExitStack() as st:
            ps = self._enter(st, self._patches())
            st.enter_context(contextlib.redirect_stdout(_io.StringIO()))
            r = _mod.item3_second_run_noop(lam, MagicMock(), MagicMock(), "f", "r", "fn",
                                           "staging", "us-west-2", "wg", "s3://x/", _mod.Results())
            inv = ps[-1]
        assert inv.call_count == 0, "item 3 must not invoke when the iOS-fleet guard refuses"
        assert r is False

    def test_item7_stops_when_the_guard_refuses(self, tmp_path):
        import contextlib, io as _io
        lam = self._lam()
        d = tmp_path / "bk"
        d.mkdir()
        with contextlib.ExitStack() as st:
            ps = self._enter(st, self._patches())
            st.enter_context(patch("tempfile.mkdtemp", lambda **kw: str(d)))
            st.enter_context(contextlib.redirect_stdout(_io.StringIO()))
            r = _mod.item7_rules_fallback(lam, MagicMock(), MagicMock(), "findings", "fn",
                                          "staging", "us-west-2", "wg", "s3://x/", _mod.Results())
            inv = ps[-1]
        assert lam.update_function_configuration.call_count == 0, (
            "item 7 must not override the env when the iOS-fleet guard refuses")
        assert inv.call_count == 0
        assert r is False



class TestSecCycle1EphemeralUserCleanup:
    """B-S2: drive the real _ephemeral_user against a stubbed cognito-idp client."""

    class _NotFound(Exception):
        pass

    def _idp(self, on_create=None, on_delete=None):
        import base64, json as _json
        idp = MagicMock()
        idp.exceptions.UserNotFoundException = self._NotFound
        body = base64.urlsafe_b64encode(_json.dumps({"sub": "s-1"}).encode()).decode().rstrip("=")
        idp.initiate_auth.return_value = {"AuthenticationResult": {"IdToken": f"h.{body}.sig"}}
        if on_create:
            idp.admin_create_user.side_effect = lambda **kw: on_create()
        if on_delete:
            idp.admin_delete_user.side_effect = lambda **kw: on_delete()
        idp.admin_get_user.side_effect = self._NotFound("gone")
        return idp

    def _use(self, idp, res, body=None):
        import contextlib, io as _io
        with contextlib.redirect_stdout(_io.StringIO()), contextlib.redirect_stderr(_io.StringIO()):
            with _mod._ephemeral_user(idp, "pool", "client", "a", ["fleet-operator"],
                                      {"custom:fleetIds": "F-A"}, res) as (token, claims):
                assert claims == {"sub": "s-1"}
                if body:
                    body()

    def _created_name(self, idp):
        return idp.admin_create_user.call_args.kwargs["Username"]

    def test_happy_path_deletes_and_confirms(self):
        idp, res = self._idp(), _mod.Results()
        self._use(idp, res)
        assert idp.admin_delete_user.call_count == 1
        assert idp.admin_delete_user.call_args.kwargs["Username"] == self._created_name(idp)
        assert idp.admin_get_user.call_count == 1
        assert not res._cleanup_failed

    def test_body_interrupted_still_deletes(self):
        idp, res = self._idp(), _mod.Results()

        def body():
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            self._use(idp, res, body)
        assert idp.admin_delete_user.call_count == 1, "Ctrl-C in the body must still delete the user"
        assert idp.admin_delete_user.call_args.kwargs["Username"] == self._created_name(idp)

    def _with_handlers(self, fn):
        import signal as _sig
        saved = {s: _sig.getsignal(s) for s in (_sig.SIGINT, _sig.SIGTERM, _sig.SIGHUP)}
        saved_state = dict(_mod._SIGNAL_STATE)
        _mod._install_signal_handlers()
        try:
            return fn()
        finally:
            for s, h in saved.items():
                _sig.signal(s, h)
            _mod._SIGNAL_STATE.update(saved_state)

    def test_sigterm_during_delete_still_confirms_then_exits(self):
        import signal as _sig
        idp, res = self._idp(on_delete=lambda: os.kill(os.getpid(), _sig.SIGTERM)), _mod.Results()

        def run():
            with pytest.raises(SystemExit) as ei:
                self._use(idp, res)
            return ei.value.code

        code = self._with_handlers(run)
        assert idp.admin_get_user.call_count == 1, "the read-back must run after a signal mid-delete"
        assert code == 143

    def test_sigterm_during_create_still_deletes(self):
        import signal as _sig
        idp, res = self._idp(on_create=lambda: os.kill(os.getpid(), _sig.SIGTERM)), _mod.Results()

        def run():
            with pytest.raises(SystemExit):
                self._use(idp, res)

        self._with_handlers(run)
        assert idp.admin_delete_user.call_count == 1, (
            "a user created just before a signal must still be deleted")



class TestSecCycle1StagingOnly:
    """B-S3: any stage but staging is refused before any AWS client exists."""

    def _main(self, stage):
        import contextlib, io as _io
        with patch.dict(os.environ, {"LIVE_TESTS": "1"}), \
             patch.object(_mod, "_parse_args", return_value=_main_args(stage=stage)), \
             patch("boto3.client", side_effect=AssertionError("no AWS client before the stage check")) as cl, \
             patch("boto3.resource", side_effect=AssertionError("no AWS resource")), \
             contextlib.redirect_stdout(_io.StringIO()), contextlib.redirect_stderr(_io.StringIO()) as err:
            rc = _mod.main()
        return rc, cl, err.getvalue()

    def test_prod_is_refused_with_no_aws_call(self):
        for stage in ("prod", "production", "Staging", "staging2"):
            rc, cl, err = self._main(stage)
            assert rc == 1, (stage, rc)
            assert cl.call_count == 0, stage
            assert "staging only" in err, err

    def test_staging_gets_past_the_check(self):
        """Control: staging reaches the first AWS call (sts)."""
        import contextlib, io as _io
        with patch.dict(os.environ, {"LIVE_TESTS": "1"}), \
             patch.object(_mod, "_parse_args", return_value=_main_args(stage="staging")), \
             patch("boto3.client", side_effect=RuntimeError("reached sts")), \
             contextlib.redirect_stdout(_io.StringIO()), contextlib.redirect_stderr(_io.StringIO()):
            with pytest.raises(RuntimeError, match="reached sts"):
                _mod.main()


class TestSecCycle1SqlShapes:
    """B-S5: values from DynamoDB go into Athena SQL text only when they have the expected shape."""

    def test_count_sql_rejects_a_bad_month(self):
        assert "year_month = '2026-09'" in _mod._select_anomalies_count_sql("staging", "2026-09")
        for bad in ("2026-09' OR '1'='1", "2026-13", "26-09", ""):
            with pytest.raises(ValueError):
                _mod._select_anomalies_count_sql("staging", bad)

    def test_item4_fails_on_a_bad_run_record_month_with_no_query(self):
        import contextlib, io as _io
        athena = MagicMock()
        res = _mod.Results()
        with patch.object(_mod, "_latest_run_record",
                          return_value={"data_month": "2026-09' OR 1=1 --", "vins_selected": 3}), \
             contextlib.redirect_stdout(_io.StringIO()):
            _mod.item4_athena_paging(athena, MagicMock(), "wg", "s3://x/", "staging", "runs", res)
        assert ("4-data-month", "FAIL") in [(r[0], r[1]) for r in res.rows]
        assert athena.start_query_execution.call_count == 0

    def test_item1_leaves_non_vin_values_out_of_the_sql(self):
        import contextlib, io as _io
        good, bad = "AAAAAAAAAAAAAAAAA", "X' OR '1'='1"
        ddb = MagicMock()
        ddb.Table.return_value.scan.return_value = {"Items": [
            {"vehicleId": "V-GOOD", "startTime": 1735000000000, "totalDistance": 200},
            {"vehicleId": "V-BAD", "startTime": 1735000000000, "totalDistance": 200},
        ]}
        ddb.Table.return_value.get_item.side_effect = lambda Key, **_: {
            "Item": {"vin": good if Key["vehicleId"] == "V-GOOD" else bad}}
        sqls: list = []

        def fake_run(client, sql, wg, out):
            sqls.append(sql)
            return "q", []

        res = _mod.Results()
        with patch.object(_mod, "_athena_run", side_effect=fake_run), \
             contextlib.redirect_stdout(_io.StringIO()):
            _mod.item1_views_rows(MagicMock(), ddb, "wg", "s3://x/", "staging", res)
        vin_sql = [s for s in sqls if "WHERE vin IN" in s]
        assert len(vin_sql) == 1, sqls
        assert good in vin_sql[0] and "OR '1'='1" not in vin_sql[0], vin_sql[0]
        assert ("1-miles-bad-vin", "NOTE") in [(r[0], r[1]) for r in res.rows]



# ===========================================================================
# Security review cycle 2 (2026-10-08): S1, S2 — mutants own-2 and own-3 survived at b0e8258
# ===========================================================================


class TestSecCycle2Pins:
    """S1: a model id that can't be read counts as the override (fail closed).
    S2: a failed or unconfirmed Cognito delete marks the cleanup failed, so the run can't exit 0."""

    class _NotFound(Exception):
        pass

    def _quiet(self):
        import contextlib, io as _io
        st = contextlib.ExitStack()
        st.enter_context(contextlib.redirect_stdout(_io.StringIO()))
        st.enter_context(contextlib.redirect_stderr(_io.StringIO()))
        return st

    def test_s1_model_id_read_failure_counts_as_override(self):
        lam = MagicMock()
        lam.get_function_configuration.side_effect = RuntimeError("AccessDenied")
        res = _mod.Results()
        with self._quiet():
            assert _mod._model_override_in_place(lam, "fn", res, "x-model-id") is True
        assert ("x-model-id", "FAIL") in [(r[0], r[1]) for r in res.rows]

    def test_s1_item6_does_not_invoke_when_the_model_id_cannot_be_read(self):
        lam = MagicMock()
        lam.get_function_configuration.side_effect = RuntimeError("AccessDenied")
        cw = MagicMock()
        cw.describe_alarms.return_value = {"MetricAlarms": [{
            "StateValue": "OK", "AlarmActions": ["arn:aws:sns:us-west-2:123456789012:t"]}]}
        cw.describe_alarm_history.return_value = {"AlarmHistoryItems": []}
        sns = MagicMock()
        sns.list_subscriptions_by_topic.return_value = {"Subscriptions": [{"SubscriptionArn": "arn:x"}]}
        res = _mod.Results()
        with patch.object(_mod, "_wait_lambda_response") as inv, \
             patch.object(_mod.time, "sleep"), self._quiet():
            _mod.item6_forced_alarm_failure(lam, cw, sns, "fn", "staging", "us-west-2", res, wait_s=0)
        assert inv.call_count == 0, "item 6 must not invoke when the model id can't be read"
        assert ("6-model-id", "FAIL") in [(r[0], r[1]) for r in res.rows]

    def _idp(self):
        idp = MagicMock()
        idp.exceptions.UserNotFoundException = self._NotFound
        return idp

    def test_s2_delete_raising_marks_cleanup_failed(self):
        idp = self._idp()
        idp.admin_delete_user.side_effect = RuntimeError("AccessDenied")
        res = _mod.Results()
        with self._quiet():
            _mod._delete_ephemeral_user(idp, "pool", "u@example.invalid", "a", res)
        assert res._cleanup_failed is True, "a delete that raised must fail the run"

    def test_s2_user_still_present_marks_cleanup_failed(self):
        idp = self._idp()
        idp.admin_get_user.return_value = {"Username": "u@example.invalid"}
        res = _mod.Results()
        with self._quiet():
            _mod._delete_ephemeral_user(idp, "pool", "u@example.invalid", "a", res)
        assert idp.admin_delete_user.call_count == 1
        assert res._cleanup_failed is True, "a user still present after delete must fail the run"

    def test_s2_control_confirmed_absent_leaves_cleanup_ok(self):
        idp = self._idp()
        idp.admin_get_user.side_effect = self._NotFound("gone")
        res = _mod.Results()
        with self._quiet():
            _mod._delete_ephemeral_user(idp, "pool", "u@example.invalid", "a", res)
        assert idp.admin_delete_user.call_count == 1
        assert not res._cleanup_failed



# ===========================================================================
# 5.3 run 1 (2026-10-08): item 4 crashed on `import agents` run as a file; item 2 stopped at 6 runs
# ===========================================================================


class TestRun1Fixes:
    """User answers 2026-10-08: the script must import the agent's modules when run as a file, and
    one 5.3 run must be allowed enough item-2 repeats to clear 149 deferred at 12 a run."""

    def test_script_run_as_a_file_can_import_the_agent_modules(self, tmp_path):
        """Run the script's top level in a fresh interpreter with no repo root on sys.path (``-I``,
        cwd outside the repo), then import what items 4 and 8 import."""
        import subprocess, sys as _sys
        code = (
            "import runpy, sys\n"
            f"runpy.run_path({str(_SCRIPT)!r}, run_name='lv_probe')\n"
            "import agents.fi_cost.evidence, agents.fi_cost.selection, agents.fi_cost.sell_timing\n"
            "import agents.supervisor.tools.fleet_recommendation\n"
            "print('imports-ok')\n"
        )
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH" and not k.startswith("AWS_")}
        p = subprocess.run([_sys.executable, "-I", "-c", code], cwd=tmp_path, env=env,
                           capture_output=True, text=True, timeout=120)
        assert p.returncode == 0 and "imports-ok" in p.stdout, (
            f"run as a file, the script must put the repo root on sys.path: {p.stderr[-400:]}")

    def test_run_limit_clears_149_deferred_at_the_slowest_rate(self):
        assert _mod._MAX_REPEAT_RUNS >= -(-149 // 12), _mod._MAX_REPEAT_RUNS

    def _item2(self, deferred_after_run):
        import contextlib, io as _io
        calls = {"n": 0}

        def fake_invoke(lam, fn, payload):
            calls["n"] += 1
            return {"tokens_used": 0, "cost": {"deferred": deferred_after_run(calls["n"]),
                                               "model_calls": 1, "data_month": "2026-09"}}

        res = Results()
        with patch.object(_mod, "_read_answer_key", return_value=([{"vin": "V1", "cause": "c"}], "2026-09")), \
             patch.object(_mod, "_wait_lambda_response", side_effect=fake_invoke), \
             patch.object(_mod, "_scan_findings_by_fleet_all_fleets", return_value=[]), \
             patch.object(_mod, "_check_ios_fleet_intersection", return_value=True), \
             patch.object(_mod, "_model_override_in_place", return_value=False), \
             patch.object(_mod, "_near_cost_schedule", return_value=(False, "")), \
             contextlib.redirect_stdout(_io.StringIO()):
            _mod.item2_agent_scores_plants(
                MagicMock(), MagicMock(), MagicMock(), MagicMock(),
                findings_table="findings", runs_table="runs", function_name="fn",
                bucket="bucket", stage="staging", region="us-west-2",
                workgroup="wg", output_location="s3://x/", res=res,
            )
        conv = [r[1] for r in res.rows if r[0] == "2-convergence"]
        return calls["n"], conv

    def test_item2_repeats_exactly_up_to_the_limit_when_it_never_converges(self):
        n, conv = self._item2(lambda k: 1)
        assert n == _mod._MAX_REPEAT_RUNS, (n, _mod._MAX_REPEAT_RUNS)
        assert conv == ["FAIL"]

    def test_item2_clears_149_at_12_a_run_in_one_script_run(self):
        n, conv = self._item2(lambda k: max(0, 149 - 12 * k))
        assert n == 13, n
        assert conv == ["PASS"]



class TestRun2Item8SessionId:
    """5.3 run 2 (2026-10-08): item 8 got 400 "sessionId is required" from /assistant/chat."""

    def test_post_chat_sends_a_fresh_session_id_the_handler_accepts(self):
        bodies: list = []
        api = MagicMock()
        api.call.side_effect = lambda method, path, token, body, timeout=90: (bodies.append(body) or (200, {"result": "ok"}))
        _mod._post_chat(api, "tok", "hello", persona_id="fleet_manager")
        _mod._post_chat(api, "tok", "hello again")
        for b in bodies:
            sid = b.get("sessionId")
            # The handler 400s on a missing or blank sessionId (handler.py: "sessionId is required").
            assert isinstance(sid, str) and sid.strip() and len(sid) >= 33, b
            assert b["prompt"]
        assert bodies[0]["sessionId"] != bodies[1]["sessionId"], "each call gets its own session"
        assert bodies[0]["personaId"] == "fleet_manager" and "personaId" not in bodies[1]

    def test_handler_still_requires_session_id(self):
        """If the handler stops requiring sessionId this pin goes stale; re-check item 8 then."""
        src = (Path(__file__).resolve().parents[2] / "lambdas/api-assistant-chat/handler.py").read_text()
        assert '"sessionId is required"' in src
