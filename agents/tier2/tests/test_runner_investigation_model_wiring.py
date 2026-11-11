"""test_runner_investigation_model_wiring.py — task 4.3 stage A2

Covers:
- FI_INVESTIGATION_MODEL_ENABLED strict parse via _parse_model_enabled.
- Off path: boto3.client never asked for bedrock-runtime; run_investigation
  receives bedrock_client=None.
- On without guardrail: every record fails, run record written, no bedrock client.
- On path: run_investigation receives investigation_config (max_tokens from
  non-default TIER2_MAX_TOKENS_INVESTIGATION), a clock tied to the context, and
  its own model_usage_out dict per record.
- Run record sums two records' usage into model_calls/tokens_used/cost_usd.
- Deadline two-sided: 299,000 ms → record not started (batch item failure);
  301,000 ms → record started.
- Fakes bind to real run_investigation / write_run_record signatures.

No real AWS calls.
"""
from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import MagicMock

import pytest

from agents.fi_quality.job import run_investigation
from agents.tier2 import artifact_writer, runner
from agents.tier2.run_records import write_run_record

_REAL_RUN_INV_SIG = inspect.signature(run_investigation)
_REAL_WRR_SIG = inspect.signature(write_run_record)

_GUARDRAIL_ID = "guard-fi-inv"
_GUARDRAIL_VER = "2"

# ---------------------------------------------------------------------------
# Shared SQS env setup (mirrors _make_sqs_env from test_runner_service.py)
# ---------------------------------------------------------------------------

class _Cw:
    def put_metric_data(self, **_kw) -> None: ...


def _make_sqs_env(monkeypatch: Any, *, model: str | None = None,
                  guardrail_id: str = "", guardrail_version: str = "") -> dict:
    """Minimal env patch for _run_investigation_sqs; returns captured objects."""
    monkeypatch.setenv("VSA_STAGE", "staging")
    monkeypatch.setenv("ATHENA_RESULTS_BUCKET", "test-bucket")
    monkeypatch.setenv("AVX_FINDINGS_TABLE", "vsa-staging-avx-findings")
    monkeypatch.setenv("AVX_INVESTIGATIONS_TABLE", "vsa-staging-avx-investigations")
    monkeypatch.setenv("DYNAMODB_REGION", "us-west-2")
    monkeypatch.setenv("ADP_REGION", "us-east-1")
    monkeypatch.setenv("ATHENA_WORKGROUP", "cvx-staging-analytics")
    monkeypatch.delenv("FI_INVESTIGATION_MODEL_ENABLED", raising=False)
    monkeypatch.delenv("FI_COST_GUARDRAIL_ID", raising=False)
    monkeypatch.delenv("FI_COST_GUARDRAIL_VERSION", raising=False)
    if model is not None:
        monkeypatch.setenv("FI_INVESTIGATION_MODEL_ENABLED", model)
    if guardrail_id:
        monkeypatch.setenv("FI_COST_GUARDRAIL_ID", guardrail_id)
    if guardrail_version:
        monkeypatch.setenv("FI_COST_GUARDRAIL_VERSION", guardrail_version)

    fake_ddb = MagicMock(name="ddb")
    fake_athena = MagicMock(name="athena")
    fake_cw = _Cw()
    built: list[str] = []

    def fake_boto3_client(service, **_kw):
        built.append(service)
        if service == "dynamodb":
            return fake_ddb
        if service == "athena":
            return fake_athena
        if service == "cloudwatch":
            return fake_cw
        return MagicMock(name=service)

    monkeypatch.setattr("boto3.client", fake_boto3_client)

    class _FakeWriter:
        table_name = "vsa-staging-avx-findings"
        dynamodb_client = fake_ddb
        def __init__(self, **_kw): pass
        def write_finding(self, finding, *, standing=None, **_kw):
            return {"action": "put", "finding_id": "FIND-1"}

    monkeypatch.setattr(artifact_writer, "ArtifactWriter", _FakeWriter)

    return {"built": built, "cw": fake_cw}


def _one_record(n: int = 1) -> list[dict]:
    return [
        {"eventSource": "aws:sqs", "messageId": f"msg-{i}",
         "body": f"cccccccc-{i:04d}-{i:04d}-{i:04d}-{i:012d}"}
        for i in range(1, n + 1)
    ]


class _FreeContext:
    """Context with plenty of remaining time (well above 300 s margin)."""
    def get_remaining_time_in_millis(self): return 900_000


class _DecreasingContext:
    """Context whose remaining time decreases by step_ms on each call (W3)."""
    def __init__(self, start_ms: int = 900_000, step_ms: int = 100_000) -> None:
        self._ms = start_ms
        self._step = step_ms

    def get_remaining_time_in_millis(self) -> int:
        v = self._ms
        self._ms -= self._step
        return v


# ---------------------------------------------------------------------------
# 1. Strict parse guard
# ---------------------------------------------------------------------------

class TestModelEnabledStrictParse:
    @pytest.mark.parametrize("bad", ["yes", "1", "on", "", " ", "tru"])
    def test_bad_value_fails_all_records_and_writes_run_record(
        self, monkeypatch: Any, bad: str
    ) -> None:
        caps = _make_sqs_env(monkeypatch, model=bad)
        run_inv_calls: list = []
        from agents.fi_quality import job as _job
        monkeypatch.setattr(_job, "run_investigation",
                            lambda *a, **kw: run_inv_calls.append(kw))
        records_out: list = []
        monkeypatch.setattr(runner, "_emit_run_record",
                            lambda *a, **kw: records_out.append(kw))

        result = runner._run_investigation_sqs(_one_record(2), _FreeContext())

        assert len(result["batchItemFailures"]) == 2, "both records must fail"
        assert run_inv_calls == [], "run_investigation must not be called on bad model flag"
        assert len(records_out) == 1, "exactly one run record must be emitted"
        assert "bedrock-runtime" not in caps["built"], "no bedrock client on bad flag"


# ---------------------------------------------------------------------------
# 2. Off path: no bedrock-runtime; run_investigation receives bedrock_client=None
# ---------------------------------------------------------------------------

class TestModelOff:
    @pytest.fixture()
    def inv_calls(self, monkeypatch: Any) -> list[dict]:
        _make_sqs_env(monkeypatch)  # model absent → off
        calls: list[dict] = []
        from agents.fi_quality import job as _job
        monkeypatch.setattr(_job, "run_investigation",
                            lambda *_a, **kw: calls.append(kw))
        monkeypatch.setattr(runner, "_emit_run_record", lambda *_a, **_kw: None)
        runner._run_investigation_sqs(_one_record(), _FreeContext())
        return calls

    def test_no_bedrock_client_built(self, monkeypatch: Any) -> None:
        caps = _make_sqs_env(monkeypatch)
        from agents.fi_quality import job as _job
        monkeypatch.setattr(_job, "run_investigation", lambda *_a, **_kw: None)
        monkeypatch.setattr(runner, "_emit_run_record", lambda *_a, **_kw: None)
        runner._run_investigation_sqs(_one_record(), _FreeContext())
        assert "bedrock-runtime" not in caps["built"]

    def test_switch_absent_is_off_even_with_a_valid_guardrail(self, monkeypatch: Any) -> None:
        """Review g43 cycle 2 S3: with FI_INVESTIGATION_MODEL_ENABLED absent and a valid
        guardrail set, no Bedrock client is built and the job gets no client, clock or
        usage dict. The tests above set no guardrail, so a flipped default failed them
        on tuple unpacking rather than on an assertion."""
        caps = _make_sqs_env(monkeypatch, guardrail_id=_GUARDRAIL_ID,
                             guardrail_version=_GUARDRAIL_VER)
        calls: list[dict] = []
        from agents.fi_quality import job as _job
        monkeypatch.setattr(_job, "run_investigation", lambda *_a, **kw: calls.append(kw))
        monkeypatch.setattr(runner, "_emit_run_record", lambda *_a, **_kw: None)
        runner._run_investigation_sqs(_one_record(), _FreeContext())
        assert "bedrock-runtime" not in caps["built"], (
            "FI_INVESTIGATION_MODEL_ENABLED absent must mean off: a bedrock-runtime client was built")
        assert len(calls) == 1, f"the job must run once with the switch off; got {len(calls)} calls"
        assert calls[0]["bedrock_client"] is None
        assert calls[0]["model_usage_out"] is None

    def test_run_investigation_receives_bedrock_client_none(
        self, inv_calls: list[dict]
    ) -> None:
        (call,) = inv_calls
        assert call["bedrock_client"] is None

    def test_run_investigation_call_binds_to_real_signature(
        self, inv_calls: list[dict]
    ) -> None:
        (call,) = inv_calls
        # inject the positional arg that the lambda swallowed
        _REAL_RUN_INV_SIG.bind("cccccccc-0001-0001-0001-000000000001", **call)


# ---------------------------------------------------------------------------
# 3. On without guardrail: every record fails, run record written, no bedrock client
# ---------------------------------------------------------------------------

class TestModelOnWithoutGuardrail:
    def _run(self, monkeypatch: Any, *, guardrail_id: str = "",
             guardrail_version: str = "") -> tuple[dict, list]:
        caps = _make_sqs_env(monkeypatch, model="true",
                             guardrail_id=guardrail_id,
                             guardrail_version=guardrail_version)
        run_inv_calls: list = []
        from agents.fi_quality import job as _job
        monkeypatch.setattr(_job, "run_investigation",
                            lambda *a, **kw: run_inv_calls.append(kw))
        records_out: list = []
        monkeypatch.setattr(runner, "_emit_run_record",
                            lambda *a, **kw: records_out.append(kw))
        result = runner._run_investigation_sqs(_one_record(2), _FreeContext())
        return result, caps["built"], run_inv_calls, records_out

    def test_missing_guardrail_id_fails_all_records(
        self, monkeypatch: Any
    ) -> None:
        result, built, ri, recs = self._run(monkeypatch, guardrail_version=_GUARDRAIL_VER)
        assert len(result["batchItemFailures"]) == 2
        assert "bedrock-runtime" not in built
        assert ri == []
        assert len(recs) == 1

    def test_missing_guardrail_version_fails_all_records(
        self, monkeypatch: Any
    ) -> None:
        result, built, ri, recs = self._run(monkeypatch, guardrail_id=_GUARDRAIL_ID)
        assert len(result["batchItemFailures"]) == 2
        assert "bedrock-runtime" not in built
        assert ri == []
        assert len(recs) == 1

    def test_whitespace_only_guardrail_id_fails_all_records(
        self, monkeypatch: Any
    ) -> None:
        """S2: whitespace-only FI_COST_GUARDRAIL_ID must be treated as unset.

        A value like '   ' passes ``if not guardrail_id`` but .strip() makes it
        empty, so the guardrail check fires: every record fails and a run record
        is written.  No bedrock-runtime client must be built.
        """
        caps = _make_sqs_env(monkeypatch, model="true",
                             guardrail_version=_GUARDRAIL_VER)
        monkeypatch.setenv("FI_COST_GUARDRAIL_ID", "   ")

        run_inv_calls: list = []
        from agents.fi_quality import job as _job
        monkeypatch.setattr(_job, "run_investigation",
                            lambda *a, **kw: run_inv_calls.append(kw))
        records_out: list = []
        monkeypatch.setattr(runner, "_emit_run_record",
                            lambda *a, **kw: records_out.append(kw))

        try:
            result = runner._run_investigation_sqs(_one_record(2), _FreeContext())
        except Exception as exc:  # noqa: BLE001 — escaping is the defect under test
            raise AssertionError(
                f"decision 9: a bad guardrail config must fail the records, not raise ({exc!r})"
            ) from exc

        assert len(result["batchItemFailures"]) == 2, "every record must fail"
        assert "bedrock-runtime" not in caps["built"], "no bedrock client built"
        assert run_inv_calls == [], "run_investigation must not be called"
        assert len(records_out) == 1, "exactly one run record emitted"

    def test_config_refused_by_model_config_fails_all_records(
        self, monkeypatch: Any
    ) -> None:
        """S2: a guardrail config ModelConfig refuses gets decision 9's shape too.

        investigation_config raising ValueError (what ModelConfig does on a value it
        refuses) must fail every record and write the run record, not escape the handler.
        """
        caps = _make_sqs_env(monkeypatch, model="true", guardrail_id=_GUARDRAIL_ID,
                             guardrail_version=_GUARDRAIL_VER)
        from agents.fi_quality import llm as _fq_llm

        def _refuse(**_kw: Any) -> Any:
            raise ValueError("guardrail_id must be set")

        monkeypatch.setattr(_fq_llm, "investigation_config", _refuse)
        from agents.fi_quality import job as _job
        run_inv_calls: list = []
        monkeypatch.setattr(_job, "run_investigation",
                            lambda *a, **kw: run_inv_calls.append(kw))
        records_out: list = []
        monkeypatch.setattr(runner, "_emit_run_record",
                            lambda *a, **kw: records_out.append(kw))

        try:
            result = runner._run_investigation_sqs(_one_record(2), _FreeContext())
        except Exception as exc:  # noqa: BLE001 — escaping is the defect under test
            raise AssertionError(
                f"decision 9: a refused config must fail the records, not raise ({exc!r})"
            ) from exc

        assert len(result["batchItemFailures"]) == 2
        assert "bedrock-runtime" not in caps["built"]
        assert run_inv_calls == []
        assert len(records_out) == 1


# ---------------------------------------------------------------------------
# 4. On path: config, clock, per-record usage dict
# ---------------------------------------------------------------------------

class TestModelOn:
    @pytest.fixture()
    def on_call(self, monkeypatch: Any) -> tuple[dict, dict]:
        """Returns (run_investigation kwargs, captured objects)."""
        caps = _make_sqs_env(monkeypatch, model="true",
                             guardrail_id=_GUARDRAIL_ID,
                             guardrail_version=_GUARDRAIL_VER)
        # Use a non-default token cap so the test is not trivially satisfied.
        monkeypatch.setattr(runner, "_MAX_TOKENS_INVESTIGATION", 77_777)

        inv_kwargs_list: list[dict] = []

        def fake_run_investigation(inv_id, **kw):
            inv_kwargs_list.append(kw)

        from agents.fi_quality import job as _job
        monkeypatch.setattr(_job, "run_investigation", fake_run_investigation)
        monkeypatch.setattr(runner, "_emit_run_record", lambda *a, **kw: None)

        runner._run_investigation_sqs(_one_record(), _FreeContext())
        (call,) = inv_kwargs_list
        return call, caps

    def test_max_tokens_from_patched_constant(self, on_call: tuple) -> None:
        call, _ = on_call
        assert call["model_config"].max_tokens == 77_777

    def test_guardrail_wired(self, on_call: tuple) -> None:
        call, _ = on_call
        mc = call["model_config"]
        assert mc.guardrail_id == _GUARDRAIL_ID
        assert mc.guardrail_version == _GUARDRAIL_VER

    def test_bedrock_client_built_and_passed(self, on_call: tuple) -> None:
        call, caps = on_call
        assert "bedrock-runtime" in caps["built"]
        assert call["bedrock_client"] is not None

    def test_remaining_seconds_is_callable_tied_to_context(
        self, on_call: tuple
    ) -> None:
        call, _ = on_call
        rs = call["remaining_seconds"]
        assert callable(rs)
        val = rs()
        assert isinstance(val, float) and val == 900.0

    def test_model_usage_out_is_fresh_dict(self, on_call: tuple) -> None:
        call, _ = on_call
        mu = call["model_usage_out"]
        assert isinstance(mu, dict)

    def test_call_binds_to_real_run_investigation_signature(
        self, on_call: tuple
    ) -> None:
        call, _ = on_call
        _REAL_RUN_INV_SIG.bind("cccccccc-0001-0001-0001-000000000001", **call)

    def test_remaining_seconds_reads_live_context(
        self, monkeypatch: Any
    ) -> None:
        """W3: investigation clock must read the context on every call, not once.
        M4 (captures context value at build time and returns it frozen) fails here
        because both calls return the same value instead of the second being lower."""
        _make_sqs_env(monkeypatch, model="true",
                      guardrail_id=_GUARDRAIL_ID,
                      guardrail_version=_GUARDRAIL_VER)

        inv_kwargs: list[dict] = []

        def fake_run_investigation(inv_id: str, **kw: Any) -> None:
            inv_kwargs.append(kw)

        from agents.fi_quality import job as _job
        monkeypatch.setattr(_job, "run_investigation", fake_run_investigation)
        monkeypatch.setattr(runner, "_emit_run_record", lambda *a, **kw: None)

        # Deadline check consumes the first read (900→800), then our two
        # explicit reads: v1=800.0, v2=700.0 — or v1=v2=frozen for M4.
        ctx = _DecreasingContext(start_ms=900_000, step_ms=100_000)
        runner._run_investigation_sqs(_one_record(), ctx)

        (call,) = inv_kwargs
        rs = call["remaining_seconds"]
        v1 = rs()
        v2 = rs()
        assert v2 < v1, (
            f"clock must reflect live context reads; got v1={v1} v2={v2} (M4 frozen?)"
        )


# ---------------------------------------------------------------------------
# 5. Run record sums two records' usage
# ---------------------------------------------------------------------------

class TestRunRecordSumsUsage:
    def test_two_records_usage_summed_in_run_record(
        self, monkeypatch: Any
    ) -> None:
        _make_sqs_env(monkeypatch, model="true",
                      guardrail_id=_GUARDRAIL_ID,
                      guardrail_version=_GUARDRAIL_VER)

        call_count = 0

        def fake_run_investigation(inv_id, *, model_usage_out=None, **kw):
            nonlocal call_count
            call_count += 1
            if model_usage_out is not None:
                # Simulate two distinct usage dicts
                model_usage_out.update({
                    "calls": 2,
                    "input_tokens": 100 * call_count,
                    "output_tokens": 50 * call_count,
                })

        from agents.fi_quality import job as _job
        monkeypatch.setattr(_job, "run_investigation", fake_run_investigation)

        records_out: list[dict] = []
        monkeypatch.setattr(runner, "_emit_run_record",
                            lambda *a, **kw: records_out.append(kw))

        runner._run_investigation_sqs(_one_record(2), _FreeContext())

        assert call_count == 2
        (rec,) = records_out
        extra = rec.get("extra") or {}
        # calls: 2+2=4; input: 100+200=300; output: 50+100=150
        assert extra["model_calls"] == 4
        assert extra["tokens_used"] == 300 + 150
        assert isinstance(extra["cost_usd"], float)
        # Key correctness: all extra keys bind to write_run_record signature
        _REAL_WRR_SIG.bind_partial(**extra)

    def test_run_record_has_no_model_extra_when_off(
        self, monkeypatch: Any
    ) -> None:
        _make_sqs_env(monkeypatch)  # model off
        from agents.fi_quality import job as _job
        monkeypatch.setattr(_job, "run_investigation", lambda *a, **kw: None)
        records_out: list[dict] = []
        monkeypatch.setattr(runner, "_emit_run_record",
                            lambda *a, **kw: records_out.append(kw))
        runner._run_investigation_sqs(_one_record(), _FreeContext())
        (rec,) = records_out
        # extra must be None when model is off
        assert rec.get("extra") is None

    def test_spend_counted_when_record_raises_after_model_calls(
        self, monkeypatch: Any
    ) -> None:
        """W1: model spend must appear in the run record even when the record fails.

        run_investigation fills model_usage_out right after the model calls, before
        the Finding write and completion steps.  Any of those can raise, but the
        spend already happened and must be counted in the finally block.
        """
        _make_sqs_env(monkeypatch, model="true",
                      guardrail_id=_GUARDRAIL_ID,
                      guardrail_version=_GUARDRAIL_VER)

        def fake_run_investigation(inv_id, *, model_usage_out=None, **kw):
            if model_usage_out is not None:
                model_usage_out.update({"calls": 2, "input_tokens": 1000, "output_tokens": 500})
            raise RuntimeError("record failed after model calls")

        from agents.fi_quality import job as _job
        monkeypatch.setattr(_job, "run_investigation", fake_run_investigation)

        records_out: list[dict] = []
        monkeypatch.setattr(runner, "_emit_run_record",
                            lambda *a, **kw: records_out.append(kw))
        runner._run_investigation_sqs(_one_record(), _FreeContext())

        (rec,) = records_out
        extra = rec.get("extra") or {}
        from agents.fi_fleet_health.llm import cost_usd as _cost_usd
        assert extra["model_calls"] == 2
        assert extra["tokens_used"] == 1500
        assert extra["cost_usd"] == _cost_usd(1000, 500)

    def test_a_record_without_calls_adds_nothing_after_one_with_calls(
        self, monkeypatch: Any
    ) -> None:
        """Review g43 cycle 2 W3: the W1 finally sums record_usage on every record,
        which is right only while each record gets a new, empty dict. Record 1 fills
        its dict; record 2 fills nothing. The totals must equal record 1's, and record 2
        must have received a different, empty dict."""
        _make_sqs_env(monkeypatch, model="true",
                      guardrail_id=_GUARDRAIL_ID,
                      guardrail_version=_GUARDRAIL_VER)

        received: list[tuple[int, dict]] = []  # (id of the dict, a copy at call time)

        def fake_run_investigation(inv_id, *, model_usage_out=None, **kw):
            assert model_usage_out is not None, "model on: each record gets a usage dict"
            received.append((id(model_usage_out), dict(model_usage_out)))
            if len(received) == 1:
                model_usage_out.update({"calls": 2, "input_tokens": 700, "output_tokens": 300})
            # record 2 makes no model call and fills nothing

        from agents.fi_quality import job as _job
        monkeypatch.setattr(_job, "run_investigation", fake_run_investigation)
        records_out: list[dict] = []
        monkeypatch.setattr(runner, "_emit_run_record",
                            lambda *a, **kw: records_out.append(kw))

        runner._run_investigation_sqs(_one_record(2), _FreeContext())

        assert len(received) == 2
        (id1, _), (id2, snap2) = received
        assert id1 != id2, "each record must get its own usage dict"
        assert snap2 == {}, f"record 2's usage dict must start empty; got {snap2}"
        (rec,) = records_out
        extra = rec.get("extra") or {}
        from agents.fi_fleet_health.llm import cost_usd as _cost_usd
        assert extra["model_calls"] == 2, f"only record 1 made calls; got {extra['model_calls']}"
        assert extra["tokens_used"] == 1000
        assert extra["cost_usd"] == _cost_usd(700, 300)


# ---------------------------------------------------------------------------
# 6. Deadline two-sided boundary
# ---------------------------------------------------------------------------

class _ContextMs:
    def __init__(self, ms: int): self._ms = ms
    def get_remaining_time_in_millis(self): return self._ms


class TestDeadlineBoundary:
    def _run_with_ms(self, monkeypatch: Any, ms: int) -> dict:
        _make_sqs_env(monkeypatch)
        from agents.fi_quality import job as _job
        monkeypatch.setattr(_job, "run_investigation", lambda *a, **kw: None)
        monkeypatch.setattr(runner, "_emit_run_record", lambda *a, **kw: None)
        return runner._run_investigation_sqs(_one_record(), _ContextMs(ms))

    def test_below_margin_record_fails(self, monkeypatch: Any) -> None:
        # 299,000 ms < 300,000 ms → not started → batch item failure
        result = self._run_with_ms(monkeypatch, 299_000)
        assert len(result["batchItemFailures"]) == 1

    def test_at_margin_record_starts(self, monkeypatch: Any) -> None:
        # 300,000 ms == 300 s == margin; condition is strict < so 300.0 is NOT < 300.0
        # → record starts (no failure).  Tests < vs <= boundary.
        result = self._run_with_ms(monkeypatch, 300_000)
        assert result == {"batchItemFailures": []}, (
            "At exactly the margin (300,000 ms) the record must start; "
            "condition is strict <, not <=."
        )

    def test_above_margin_record_starts(self, monkeypatch: Any) -> None:
        # 301,000 ms > 300,000 ms → started (no failure)
        result = self._run_with_ms(monkeypatch, 301_000)
        assert result == {"batchItemFailures": []}



# ---------------------------------------------------------------------------
# 7. W5: bedrock-runtime client config — read_timeout, connect_timeout, retries
# ---------------------------------------------------------------------------

class TestBedrockClientConfigInvestigation:
    """W5: both new Bedrock clients must use read_timeout 30, connect_timeout 5,
    total_max_attempts 1 (Carried 1 constraint).  The existing fake drops **_kw;
    this class wraps it to capture the Config object."""

    def test_read_timeout_connect_timeout_retries(self, monkeypatch: Any) -> None:
        import boto3
        caps = _make_sqs_env(monkeypatch, model="true",
                             guardrail_id=_GUARDRAIL_ID,
                             guardrail_version=_GUARDRAIL_VER)

        captured_config: list = []
        _orig = boto3.client  # already the fake installed by _make_sqs_env

        def capturing_client(service, **kw):
            if service == "bedrock-runtime":
                captured_config.append(kw.get("config"))
            return _orig(service, **kw)

        monkeypatch.setattr(boto3, "client", capturing_client)

        from agents.fi_quality import job as _job
        monkeypatch.setattr(_job, "run_investigation", lambda *a, **kw: None)
        monkeypatch.setattr(runner, "_emit_run_record", lambda *a, **kw: None)
        runner._run_investigation_sqs(_one_record(), _FreeContext())

        assert len(captured_config) == 1, "bedrock-runtime must be built exactly once"
        cfg = captured_config[0]
        assert cfg.read_timeout == 30
        assert cfg.connect_timeout == 5
        assert cfg.retries["total_max_attempts"] == 1
