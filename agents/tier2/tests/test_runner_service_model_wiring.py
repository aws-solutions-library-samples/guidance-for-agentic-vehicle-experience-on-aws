"""Wiring tests for the fleet-health service model path (task 4.3 stage A1).

Covers:
- _parse_model_enabled: strict parse — absent False, true/false accepted,
  anything else RuntimeError naming the variable.
- _run_service_from_env off: no bedrock-runtime client built; run_service
  called with bedrock_client=None.
- _run_service_from_env on without guardrail: raises before any boto3.client call.
- _run_service_from_env on: run_service receives model_config with non-default
  cap + non-default TIER2_MAX_TOKENS_SERVICE, a ModelBudget, and a clock.
- write_finding_fn: forwards update_on_same_hash to avx_writer.write_finding.
- abort exit: run record carries model spend even when the loop raises.
- run-record key completeness: every forwarded key binds to write_run_record's
  signature.

No real AWS calls (boto3.client replaced by fakes or patched at module seam).
"""
from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import MagicMock

import pytest

import boto3 as _boto3_real

from agents.fi_fleet_health import loop as _fh_loop_mod
from agents.fi_fleet_health.llm import ModelBudget, ModelConfig
from agents.tier2 import artifact_writer, runner
from agents.tier2.run_records import write_run_record

_REAL_WRITE_RUN_RECORD_SIG = inspect.signature(write_run_record)
# Review g43 cycle 2 W1: captured at import, before any fixture replaces
# runner.run_service or fh_loop.run_health_loop with a fake that accepts **kw.
# Binding to inspect.signature(runner.run_service) inside a test read the fake's
# signature, so a stray kwarg bound and the check could not fail.
_REAL_RUN_SERVICE_SIG = inspect.signature(runner.run_service)
_REAL_RUN_HEALTH_LOOP_SIG = inspect.signature(_fh_loop_mod.run_health_loop)


def _bind_or_fail(sig: inspect.Signature, *args: Any, **kwargs: Any) -> None:
    """Fail the test (not just raise TypeError) when a call doesn't bind to the real signature."""
    try:
        sig.bind(*args, **kwargs)
    except TypeError as exc:
        pytest.fail(f"call does not bind to the real signature: {exc}")

_GUARDRAIL_ID = "guard-x"
_GUARDRAIL_VER = "3"

# ---------------------------------------------------------------------------
# Shared selection stubs (same set test_runner_service.py uses)
# ---------------------------------------------------------------------------

def _patch_selection(monkeypatch: Any) -> None:
    """Stub all Athena/DDB readers so run_service runs without real clients."""
    from agents.fi_fleet_health import selection as fh_sel, loop as fh_loop
    from agents.fi_recall_warranty import selection as rw_sel

    monkeypatch.setattr(fh_sel, "select_fault_rows", lambda *_a, **_kw: [])
    monkeypatch.setattr(fh_sel, "select_pm_rows", lambda *_a, **_kw: [])
    monkeypatch.setattr(fh_sel, "select_managed_vins", lambda *_a, **_kw: [])
    monkeypatch.setattr(fh_sel, "select_daily_usage", lambda *_a, **_kw: [])
    monkeypatch.setattr(fh_sel, "select_agent2_findings", lambda *_a, **_kw: ({}, 0))
    monkeypatch.setattr(fh_sel, "build_vehicle_rows", lambda **_kw: ([], {}))
    monkeypatch.setattr(rw_sel, "select_parts_stock", lambda *_a, **_kw: [])


class _AvxWriter:
    table_name = "vsa-staging-avx-findings"

    def __init__(self) -> None:
        self.dynamodb_client = MagicMock()
        self.write_calls: list[dict] = []

    def write_finding(self, finding, *, standing=None, refresh_on_noop=False,
                      update_on_same_hash=False) -> dict:
        self.write_calls.append({
            "finding": finding,
            "update_on_same_hash": update_on_same_hash,
        })
        return {"action": "put", "finding_id": finding.get("finding_id", "F1")}


# ---------------------------------------------------------------------------
# Fixture: fresh snapshot dates (mirrors test_runner_service.py autouse)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_dates(monkeypatch: Any) -> None:
    import datetime
    from agents.tier2.snapshot_freshness import ADP_KEYS, SnapshotState, SNAPSHOT_TABLES
    today = datetime.datetime.now(datetime.timezone.utc).date()
    fresh = {
        k: SnapshotState(date=today, row_count=(1 if k in ADP_KEYS else None),
                         table_date=today)
        for k in SNAPSHOT_TABLES
    }
    monkeypatch.setattr(runner, "_read_snapshot_dates", lambda *_a, **_kw: fresh)


# ---------------------------------------------------------------------------
# 1. _parse_model_enabled strict parse
# ---------------------------------------------------------------------------

class TestParseModelEnabled:
    def test_absent_returns_false(self) -> None:
        assert runner._parse_model_enabled(None, "VAR") is False

    def test_false_variants_return_false(self) -> None:
        for v in ("false", "False", "FALSE", "  false  "):
            assert runner._parse_model_enabled(v, "VAR") is False, v

    def test_true_variants_return_true(self) -> None:
        for v in ("true", "True", "TRUE", "  true  "):
            assert runner._parse_model_enabled(v, "VAR") is True, v

    @pytest.mark.parametrize("bad", ["yes", "1", "0", "on", "", " ", "tru"])
    def test_bad_value_raises_naming_variable(self, bad: str) -> None:
        with pytest.raises(RuntimeError, match="MY_SERVICE_VAR"):
            runner._parse_model_enabled(bad, "MY_SERVICE_VAR")


# ---------------------------------------------------------------------------
# Fixture for _run_service_from_env tests (stubs boto3 + run_service seam)
# ---------------------------------------------------------------------------

class _FakeWriter:
    def __init__(self, table_name: str, dynamodb_client: Any) -> None:
        self.table_name = table_name
        self.dynamodb_client = dynamodb_client
    def write_finding(self, *a, **kw) -> dict:
        return {}


@pytest.fixture()
def from_env(monkeypatch: Any):
    """Patch boto3.client and runner.run_service to capture from-env wiring."""
    import boto3
    built: list[str] = []
    run_service_calls: list[dict] = []

    def fake_client(service, *a, **kw):
        built.append(service)
        return MagicMock(name=service)

    def fake_run_service(stage, **kw):
        # S6: bind to real run_service signature so a stray extra kwarg from
        # _run_service_from_env is caught here rather than silently dropped.
        _bind_or_fail(_REAL_RUN_SERVICE_SIG, stage, **kw)
        run_service_calls.append(kw)
        return {}

    monkeypatch.setattr(boto3, "client", fake_client)
    monkeypatch.setattr(artifact_writer, "ArtifactWriter", _FakeWriter)
    monkeypatch.setattr(runner, "run_service", fake_run_service)
    monkeypatch.setattr(runner, "resolve_standing_facts", lambda *a, **kw: None)
    monkeypatch.setattr(runner, "_assigned_driver_directory", lambda *a, **kw: {})
    monkeypatch.setenv("ATHENA_RESULTS_BUCKET", "bucket-test")
    monkeypatch.delenv("FI_SERVICE_MODEL_ENABLED", raising=False)
    monkeypatch.delenv("FI_COST_GUARDRAIL_ID", raising=False)
    monkeypatch.delenv("FI_COST_GUARDRAIL_VERSION", raising=False)
    monkeypatch.delenv("FI_HEALTH_MAX_MODEL_CALLS", raising=False)
    return {"built": built, "run_service_calls": run_service_calls}


# ---------------------------------------------------------------------------
# 2. off path: no bedrock-runtime, run_service called with bedrock_client=None
# ---------------------------------------------------------------------------

class TestFromEnvOff:
    def test_no_bedrock_client_built(self, from_env: dict) -> None:
        runner._run_service_from_env("staging")
        assert "bedrock-runtime" not in from_env["built"]

    def test_run_service_receives_bedrock_client_none(self, from_env: dict) -> None:
        runner._run_service_from_env("staging")
        (call,) = from_env["run_service_calls"]
        assert call["bedrock_client"] is None

    def test_run_service_receives_model_config_none(self, from_env: dict) -> None:
        runner._run_service_from_env("staging")
        (call,) = from_env["run_service_calls"]
        assert call["model_config"] is None

    def test_run_service_receives_remaining_seconds_none(self, from_env: dict) -> None:
        runner._run_service_from_env("staging")
        (call,) = from_env["run_service_calls"]
        assert call["remaining_seconds"] is None

    def test_switch_absent_is_off_even_with_a_valid_guardrail(
        self, from_env: dict, monkeypatch: Any
    ) -> None:
        """Review g43 cycle 2 W2: with FI_SERVICE_MODEL_ENABLED absent and a valid
        guardrail set, no Bedrock client is built and run_service gets no client or
        config. The tests above delete the guardrail, so if absent meant on they failed
        on the guardrail RuntimeError rather than on an assertion."""
        monkeypatch.setenv("FI_COST_GUARDRAIL_ID", _GUARDRAIL_ID)
        monkeypatch.setenv("FI_COST_GUARDRAIL_VERSION", _GUARDRAIL_VER)
        runner._run_service_from_env("staging")
        assert "bedrock-runtime" not in from_env["built"], (
            "FI_SERVICE_MODEL_ENABLED absent must mean off: a bedrock-runtime client was built")
        (call,) = from_env["run_service_calls"]
        assert call["bedrock_client"] is None
        assert call["model_config"] is None


# ---------------------------------------------------------------------------
# 3. on without guardrail: raises before any boto3.client call
# ---------------------------------------------------------------------------

class TestFromEnvOnWithoutGuardrail:
    def test_raises_when_guardrail_id_missing(
        self, from_env: dict, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("FI_SERVICE_MODEL_ENABLED", "true")
        monkeypatch.setenv("FI_COST_GUARDRAIL_VERSION", "1")
        with pytest.raises(RuntimeError, match="FI_COST_GUARDRAIL"):
            runner._run_service_from_env("staging")
        assert not from_env["built"], "boto3.client must not be called before guardrail check"

    def test_raises_when_guardrail_version_missing(
        self, from_env: dict, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("FI_SERVICE_MODEL_ENABLED", "true")
        monkeypatch.setenv("FI_COST_GUARDRAIL_ID", _GUARDRAIL_ID)
        with pytest.raises(RuntimeError, match="FI_COST_GUARDRAIL"):
            runner._run_service_from_env("staging")
        assert not from_env["built"], "boto3.client must not be called before guardrail check"


# ---------------------------------------------------------------------------
# 4. on: run_service receives correct model_config, bedrock_client, clock
# ---------------------------------------------------------------------------

class TestFromEnvOn:
    @pytest.fixture()
    def on_call(self, from_env: dict, monkeypatch: Any) -> dict:
        monkeypatch.setenv("FI_SERVICE_MODEL_ENABLED", "true")
        monkeypatch.setenv("FI_COST_GUARDRAIL_ID", _GUARDRAIL_ID)
        monkeypatch.setenv("FI_COST_GUARDRAIL_VERSION", _GUARDRAIL_VER)
        monkeypatch.setenv("FI_HEALTH_MAX_MODEL_CALLS", "7")
        monkeypatch.setattr(runner, "_MAX_TOKENS_SERVICE", 88_000)
        runner._run_service_from_env("staging")
        (call,) = from_env["run_service_calls"]
        return call

    def test_cap_from_env(self, on_call: dict) -> None:
        assert on_call["model_config"].max_model_calls == 7

    def test_max_tokens_from_patched_constant(self, on_call: dict) -> None:
        assert on_call["model_config"].max_tokens == 88_000

    def test_guardrail_wired(self, on_call: dict) -> None:
        mc = on_call["model_config"]
        assert mc.guardrail_id == _GUARDRAIL_ID
        assert mc.guardrail_version == _GUARDRAIL_VER

    def test_bedrock_client_built(self, from_env: dict, on_call: dict) -> None:
        assert "bedrock-runtime" in from_env["built"]
        assert on_call["bedrock_client"] is not None

    def test_remaining_seconds_is_callable(self, on_call: dict) -> None:
        fn = on_call["remaining_seconds"]
        assert callable(fn)
        assert isinstance(fn(), float)

    def test_bedrock_client_config_timeouts_and_retries(
        self, from_env: dict, monkeypatch: Any
    ) -> None:
        """W5: bedrock-runtime client must use read_timeout 30, connect_timeout 5,
        total_max_attempts 1 (Carried 1 constraint)."""
        import boto3
        monkeypatch.setenv("FI_SERVICE_MODEL_ENABLED", "true")
        monkeypatch.setenv("FI_COST_GUARDRAIL_ID", _GUARDRAIL_ID)
        monkeypatch.setenv("FI_COST_GUARDRAIL_VERSION", _GUARDRAIL_VER)

        captured_config: list = []
        _orig = boto3.client  # already the fake installed by the from_env fixture

        def capturing_client(service, *a, **kw):
            if service == "bedrock-runtime":
                captured_config.append(kw.get("config"))
            return _orig(service, *a, **kw)

        monkeypatch.setattr(boto3, "client", capturing_client)
        runner._run_service_from_env("staging")

        assert len(captured_config) == 1, "bedrock-runtime must be built exactly once"
        cfg = captured_config[0]
        assert cfg.read_timeout == 30
        assert cfg.connect_timeout == 5
        assert cfg.retries["total_max_attempts"] == 1

    def test_remaining_seconds_reads_live_monotonic(
        self, from_env: dict, monkeypatch: Any
    ) -> None:
        """W3: service clock must derive from live monotonic reads, not a snapshot.
        M6 (deadline 10× _DEFAULT_MAX_WALL_SECONDS) fails the first assertion;
        a frozen-value clock fails the second."""
        import time as _time_mod
        monkeypatch.setenv("FI_SERVICE_MODEL_ENABLED", "true")
        monkeypatch.setenv("FI_COST_GUARDRAIL_ID", _GUARDRAIL_ID)
        monkeypatch.setenv("FI_COST_GUARDRAIL_VERSION", _GUARDRAIL_VER)

        tick = [0.0]
        monkeypatch.setattr(_time_mod, "monotonic", lambda: tick[0])

        runner._run_service_from_env("staging")
        (call,) = from_env["run_service_calls"]
        fn = call["remaining_seconds"]

        v1 = fn()
        assert abs(v1 - runner._DEFAULT_MAX_WALL_SECONDS) < 1.0, (
            f"expected ~{runner._DEFAULT_MAX_WALL_SECONDS}, got {v1}"
        )
        tick[0] = 50.0
        v2 = fn()
        assert v2 < v1, f"clock must decrease as time advances; v1={v1} v2={v2}"


# ---------------------------------------------------------------------------
# 5. write_finding_fn forwards update_on_same_hash
# ---------------------------------------------------------------------------

class TestWriteFindingForwardsUpdateOnSameHash:
    def test_update_on_same_hash_forwarded(self, monkeypatch: Any) -> None:
        from agents.fi_fleet_health import loop as fh_loop
        _patch_selection(monkeypatch)
        avx = _AvxWriter()

        write_calls: list[dict] = []

        def fake_loop(**kw: Any) -> dict:
            wf = kw["write_finding"]
            # Bind against real signature to catch missing kwargs.
            _bind_or_fail(_REAL_RUN_HEALTH_LOOP_SIG, **kw)
            wf({"finding_id": "F1"}, None, update_on_same_hash=True)
            return {"vins_selected": 0, "vins_processed": 0, "items_written": 0,
                    "items_skipped_unchanged": 0, "vins_refused": 0,
                    "grounding_read_errors": 0, "exhausted_items_written": 0,
                    "by_item_type": {}, "book_items_without_rooftops": 0,
                    "p0_directive_fallbacks": 0}

        monkeypatch.setattr(fh_loop, "run_health_loop", fake_loop)
        runner.run_service(
            "staging",
            athena_client=object(), workgroup="wg", results_s3_path="s3://r/",
            ddb_client=object(), findings_table="ft", actions_table="at",
            groundings_table="gt", vehicles_table="vt", fleet_locations_table="lt",
            avx_writer=avx, owner_resolver=None,
        )
        assert avx.write_calls, "write_finding must have been called"
        assert avx.write_calls[0]["update_on_same_hash"] is True


# ---------------------------------------------------------------------------
# 6. abort exit: model spend recorded even when loop raises
# ---------------------------------------------------------------------------

class TestAbortRecordsModelSpend:
    def test_spend_in_record_after_loop_raises(self, monkeypatch: Any) -> None:
        from agents.fi_fleet_health import loop as fh_loop
        _patch_selection(monkeypatch)

        mc = ModelConfig(
            guardrail_id=_GUARDRAIL_ID, guardrail_version=_GUARDRAIL_VER,
            max_model_calls=10, max_tokens=50_000,
        )

        def fake_loop_raises(**kw: Any) -> dict:
            budget: ModelBudget = kw["model_budget"]
            # Simulate a model call having been made before the abort.
            budget.calls += 1
            budget.input_tokens += 300
            budget.output_tokens += 50
            budget.guardrail_errors += 1
            raise RuntimeError("simulated abort")

        monkeypatch.setattr(fh_loop, "run_health_loop", fake_loop_raises)
        records: list[dict] = []
        with pytest.raises(RuntimeError, match="simulated abort"):
            runner.run_service(
                "staging",
                athena_client=object(), workgroup="wg", results_s3_path="s3://r/",
                ddb_client=object(), findings_table="ft", actions_table="at",
                groundings_table="gt", vehicles_table="vt", fleet_locations_table="lt",
                avx_writer=_AvxWriter(), owner_resolver=None,
                run_record_writer=lambda **k: records.append(k),
                bedrock_client=MagicMock(),
                model_config=mc,
            )
        (rec,) = records
        from agents.fi_fleet_health import llm
        assert rec["model_calls"] == 1
        assert rec["tokens_used"] == 350
        assert rec["guardrail_errors"] == 1
        assert rec["cost_usd"] == llm.cost_usd(300, 50), (
            "M8 hardcodes cost_usd=0.0; asserting the exact llm.cost_usd(300, 50) value catches it."
        )
        assert "deferred" not in rec, (
            "S4: deferred count is unknown on the abort exit — it must be omitted."
        )


# ---------------------------------------------------------------------------
# 6b. W4/M7: success exit stores exact model counter values
# ---------------------------------------------------------------------------


class TestSuccessPathModelCounters:
    """W4/M7: success exit records exact budget counter values.

    M7 sets _model_deferred=0 instead of stats["model_deferred"].  Asserting
    rec["deferred"] == 3 catches it.  The other counters are bumped to non-zero
    values so M5-style deletions are also caught.
    """

    def test_model_counters_stored_exactly(self, monkeypatch: Any) -> None:
        from agents.fi_fleet_health import loop as fh_loop
        _patch_selection(monkeypatch)

        mc = ModelConfig(
            guardrail_id=_GUARDRAIL_ID, guardrail_version=_GUARDRAIL_VER,
            max_model_calls=10, max_tokens=50_000,
        )

        def fake_loop_with_spend(**kw: Any) -> dict:
            budget: ModelBudget = kw["model_budget"]
            budget.calls += 2
            budget.input_tokens += 400
            budget.output_tokens += 100
            budget.guardrail_interventions += 1
            budget.guardrail_errors += 2
            budget.rules_fallbacks += 3
            return {
                "vins_selected": 0, "vins_processed": 0, "items_written": 0,
                "items_skipped_unchanged": 0, "vins_refused": 0,
                "grounding_read_errors": 0, "exhausted_items_written": 0,
                "by_item_type": {}, "book_items_without_rooftops": 0,
                "p0_directive_fallbacks": 0, "model_deferred": 3,
            }

        monkeypatch.setattr(fh_loop, "run_health_loop", fake_loop_with_spend)
        records: list[dict] = []
        runner.run_service(
            "staging",
            athena_client=object(), workgroup="wg", results_s3_path="s3://r/",
            ddb_client=object(), findings_table="ft", actions_table="at",
            groundings_table="gt", vehicles_table="vt", fleet_locations_table="lt",
            avx_writer=_AvxWriter(), owner_resolver=None,
            run_record_writer=lambda **k: records.append(k),
            bedrock_client=MagicMock(),
            model_config=mc,
        )
        (rec,) = records
        from agents.fi_fleet_health import llm
        assert rec["model_calls"] == 2
        assert rec["tokens_used"] == 500
        # Review g43 cycle 2 W4: the success record's cost is pinned too (only the
        # abort record's was); a hardcoded 0.0 would record $0 for every model run.
        assert rec["cost_usd"] == llm.cost_usd(400, 100), (
            f"success-path cost_usd={rec['cost_usd']!r}, expected llm.cost_usd(400, 100)")
        assert rec["guardrail_interventions"] == 1
        assert rec["guardrail_errors"] == 2
        assert rec["rules_fallbacks"] == 3
        assert rec["deferred"] == 3, (
            "M7 sets _model_deferred=0; asserting deferred==3 catches it."
        )


# ---------------------------------------------------------------------------
# 7. run-record keys bind to write_run_record's signature
# ---------------------------------------------------------------------------

class TestRunRecordKeysBind:
    def test_success_record_keys_bind(self, monkeypatch: Any) -> None:
        _patch_selection(monkeypatch)
        mc = ModelConfig(
            guardrail_id=_GUARDRAIL_ID, guardrail_version=_GUARDRAIL_VER,
            max_model_calls=10, max_tokens=50_000,
        )
        records: list[dict] = []
        runner.run_service(
            "staging",
            athena_client=object(), workgroup="wg", results_s3_path="s3://r/",
            ddb_client=object(), findings_table="ft", actions_table="at",
            groundings_table="gt", vehicles_table="vt", fleet_locations_table="lt",
            avx_writer=_AvxWriter(), owner_resolver=None,
            run_record_writer=lambda **k: records.append(k),
            bedrock_client=MagicMock(),
            model_config=mc,
        )
        (rec,) = records
        # Every key the caller passes must be a valid parameter of write_run_record.
        # (bind_partial: don't require all mandatory params — ddb_client/table_name
        # are only passed on the production path, not to the injected writer.)
        _REAL_WRITE_RUN_RECORD_SIG.bind_partial(**rec)

    def test_abort_record_keys_bind(self, monkeypatch: Any) -> None:
        from agents.fi_fleet_health import loop as fh_loop
        _patch_selection(monkeypatch)
        mc = ModelConfig(
            guardrail_id=_GUARDRAIL_ID, guardrail_version=_GUARDRAIL_VER,
            max_model_calls=10, max_tokens=50_000,
        )

        def raise_loop(**kw: Any) -> dict:
            raise RuntimeError("abort")

        monkeypatch.setattr(fh_loop, "run_health_loop", raise_loop)
        records: list[dict] = []
        with pytest.raises(RuntimeError):
            runner.run_service(
                "staging",
                athena_client=object(), workgroup="wg", results_s3_path="s3://r/",
                ddb_client=object(), findings_table="ft", actions_table="at",
                groundings_table="gt", vehicles_table="vt", fleet_locations_table="lt",
                avx_writer=_AvxWriter(), owner_resolver=None,
                run_record_writer=lambda **k: records.append(k),
                bedrock_client=MagicMock(),
                model_config=mc,
            )
        (rec,) = records
        _REAL_WRITE_RUN_RECORD_SIG.bind_partial(**rec)



# ---------------------------------------------------------------------------
# 8. S6: from_env call binds to the real run_service signature
# ---------------------------------------------------------------------------

class TestFromEnvCallBindsToRealRunService:
    """S6: analogous to test_call_binds_to_real_run_investigation_signature on the
    investigation path.  Verifies _run_service_from_env passes only kwargs that
    run_service actually accepts."""

    def test_from_env_call_binds(self, from_env: dict) -> None:
        runner._run_service_from_env("staging")
        (kw,) = from_env["run_service_calls"]
        # Fails if any kwarg is unknown in the REAL run_service's signature
        # (captured at import; the fixture has replaced runner.run_service by now).
        _bind_or_fail(_REAL_RUN_SERVICE_SIG, "staging", **kw)


# ---------------------------------------------------------------------------
# 9. W2: run_service forwards bedrock_client, remaining_seconds and budget limits
# ---------------------------------------------------------------------------

class TestRunHealthLoopPassThrough:
    """W2: run_service must forward bedrock_client, remaining_seconds, and the
    model_config limits (via ModelBudget) to run_health_loop.  Non-default values
    (max_model_calls=7, max_tokens=50_000) ensure the assertions are not vacuous."""

    def test_client_clock_and_budget_limits_forwarded(self, monkeypatch: Any) -> None:
        from agents.fi_fleet_health import loop as fh_loop
        _patch_selection(monkeypatch)

        mc = ModelConfig(
            guardrail_id=_GUARDRAIL_ID, guardrail_version=_GUARDRAIL_VER,
            max_model_calls=7, max_tokens=50_000,
        )
        fake_client = MagicMock(name="bedrock-runtime")

        def fake_clock() -> float:
            return 99.0

        captured: list[dict] = []

        def fake_loop(**kw: Any) -> dict:
            _bind_or_fail(_REAL_RUN_HEALTH_LOOP_SIG, **kw)
            captured.append(kw)
            return {"vins_selected": 0, "vins_processed": 0, "items_written": 0,
                    "items_skipped_unchanged": 0, "vins_refused": 0,
                    "grounding_read_errors": 0, "exhausted_items_written": 0,
                    "by_item_type": {}, "book_items_without_rooftops": 0,
                    "p0_directive_fallbacks": 0}

        monkeypatch.setattr(fh_loop, "run_health_loop", fake_loop)
        runner.run_service(
            "staging",
            athena_client=object(), workgroup="wg", results_s3_path="s3://r/",
            ddb_client=object(), findings_table="ft", actions_table="at",
            groundings_table="gt", vehicles_table="vt", fleet_locations_table="lt",
            avx_writer=_AvxWriter(), owner_resolver=None,
            run_record_writer=lambda **_k: None,
            bedrock_client=fake_client,
            model_config=mc,
            remaining_seconds=fake_clock,
        )
        (kw,) = captured
        assert kw["bedrock_client"] is fake_client
        assert kw["remaining_seconds"] is fake_clock
        # Review g43 cycle 2 S2: the loop must get run_service's own ModelBudget (the one
        # the run record reads), not None (the loop would then build a budget nobody reads).
        assert isinstance(kw["model_budget"], ModelBudget), (
            f"run_health_loop got model_budget={kw['model_budget']!r}, not a ModelBudget")
        assert kw["model_budget"].max_calls == 7
        assert kw["model_budget"].max_tokens == 50_000


# ---------------------------------------------------------------------------
# 10. W3: service clock reads live monotonic, not a snapshot
# ---------------------------------------------------------------------------
