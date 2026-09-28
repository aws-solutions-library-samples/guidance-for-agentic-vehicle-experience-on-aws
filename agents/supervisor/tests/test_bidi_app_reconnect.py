"""Tests for the voice runtime's model-stream reconnect + audio instrumentation.

Context: 31 `ModelStreamErrorException`s in 14 days on the staging voice runtime,
with 52 of 58 session-ends being errors. The exception is Bedrock's mid-stream
500, whose own message says to retry, but it was caught by a broad
`except Exception` that closed the driver's WebSocket. These tests cover the two
changes made in response:

  1. `_is_retryable_model_stream_error` — which failures reconnect vs terminate.
  2. `_log_audio_pipeline_summary` — the measurement that will confirm or kill the
     working hypothesis that the echo gate starves Nova's input.

The reconnect loop itself lives inside `websocket_handler`, which needs a
Starlette-style WebSocket plus a live BidiAgent; per this module's sibling
docstring that is not unit-testable here, so these tests pin the decision logic
and the arithmetic the loop reports. The loop's own wiring is verified by
`test_reconnect_contract_is_wired` reading the source, which is deliberately a
weaker check and says so.

Run:
    pytest agents/supervisor/tests/test_bidi_app_reconnect.py -v
"""

from __future__ import annotations

import inspect
import logging

from agents.supervisor.bidi_app import (
    MAX_MODEL_STREAM_RETRIES,
    NOVA_SONIC_IDLE_TIMEOUT_SECONDS,
    _client_error_message,
    _is_idle_timeout_error,
    _is_retryable_model_stream_error,
    _log_audio_pipeline_summary,
    websocket_handler,
)


# ---------------------------------------------------------------------------
# Which errors reconnect
# ---------------------------------------------------------------------------

class ModelStreamErrorException(Exception):
    """Stand-in with the same class name the AWS SDK raises.

    The production predicate matches on class NAME rather than importing
    `aws_sdk_bedrock_runtime.models`, so a locally-defined class with the same
    name is a faithful stand-in — and this test would fail if someone "tidied"
    the predicate into an `isinstance` check against the real import, which is
    the regression worth guarding.
    """


def test_bedrock_mid_stream_error_is_retryable():
    assert _is_retryable_model_stream_error(
        ModelStreamErrorException("The system encountered an unexpected error")
    )


def test_unrelated_failures_are_not_retryable():
    # A ValidationException means we sent something the service rejected;
    # retrying sends it again. Only the generic mid-stream 500 is retried.
    class ValidationException(Exception):
        pass

    for exc in (
        ValidationException("bad input"),
        RuntimeError("model not started"),
        ValueError("nope"),
        KeyboardInterrupt(),
    ):
        assert not _is_retryable_model_stream_error(exc), type(exc).__name__


def test_retry_budget_is_bounded_and_small():
    """A permanently-failing stream must not loop forever."""
    assert isinstance(MAX_MODEL_STREAM_RETRIES, int)
    assert 1 <= MAX_MODEL_STREAM_RETRIES <= 5


# ---------------------------------------------------------------------------
# What the client is told — issues/2026-09-22-bidi-validationexception-message-discarded
# ---------------------------------------------------------------------------
# The handler used to send `f"server-error: {type(e).__name__}"`, discarding
# `str(e)`. On 2026-09-22 a session died with the single word
# `ValidationException` while the service had actually said "Timed out waiting
# for audio bytes … less than 55 seconds" — diagnosable only from CloudWatch.
#
# The iOS client assigns this string verbatim to `lastError` and its `.error(_)`
# state, so these assertions are about what a DRIVER sees, not just a log line.

#: The real service text, verbatim from the CloudWatch event on 2026-09-22.
_REAL_IDLE_TIMEOUT_TEXT = (
    "RequestId=0c2b4db5-08c8-467e-bc24-9c3666a3a240 : Timed out waiting for "
    "audio bytes or interactive content. Please ensure gaps between audio bytes "
    "and interactive content are less than 55 seconds."
)


class ValidationException(Exception):
    """Same stand-in rationale as ModelStreamErrorException above."""


def test_error_payload_carries_the_service_message_not_just_the_class_name():
    """The regression that cost a CloudWatch round-trip to diagnose.

    Asserts the MESSAGE TEXT survives, not merely that the payload is non-empty
    — a class-name-only payload is non-empty too, so a presence check would pass
    against the exact bug this guards.
    """
    msg = _client_error_message(ValidationException("guardrail 'xyz' not found"))
    assert "guardrail 'xyz' not found" in msg, msg
    # Class name retained so the existing `server-error: <Class>` grep still works.
    assert "ValidationException" in msg, msg


def test_idle_timeout_is_recognised_from_the_real_service_text():
    assert _is_idle_timeout_error(ValidationException(_REAL_IDLE_TIMEOUT_TEXT))
    # Case-insensitive: the marker is matched against a lowered string, and the
    # service capitalises the sentence.
    assert _is_idle_timeout_error(ValidationException(_REAL_IDLE_TIMEOUT_TEXT.upper()))


def test_a_different_validation_exception_is_not_called_an_idle_timeout():
    """The three causes that used to be indistinguishable must stay distinct.

    Content filter (voice_prompt.py, 2026-05-07) and short runtimeSessionId
    (CVX 2026-06-22) are both ValidationExceptions and neither is a timeout.
    """
    content_filter = ValidationException(
        "This request has been blocked by our content filters."
    )
    short_session = ValidationException(
        "runtimeSessionId must have length greater than or equal to 33"
    )
    for exc in (content_filter, short_session):
        assert not _is_idle_timeout_error(exc), str(exc)
        # …and each still reaches the client with its own text.
        assert str(exc)[:30] in _client_error_message(exc)


def test_idle_timeout_message_is_actionable_for_a_driver():
    msg = _client_error_message(ValidationException(_REAL_IDLE_TIMEOUT_TEXT))
    assert msg.startswith("session-timeout:"), msg
    # Names the cause and the bound, and tells the driver what to do.
    assert str(NOVA_SONIC_IDLE_TIMEOUT_SECONDS) in msg, msg
    assert "microphone" in msg.lower(), msg
    assert "tap to start a new session" in msg.lower(), msg
    # The raw service text is still carried for whoever is debugging.
    assert "Timed out waiting for audio bytes" in msg, msg


def test_idle_timeout_stays_non_retryable():
    """Reconnecting against a stopped capture path would loop every 55s.

    The driver would hear nothing while the logs showed a healthy retry cycle,
    which is a dead microphone masked as a flaky network.
    """
    assert not _is_retryable_model_stream_error(
        ValidationException(_REAL_IDLE_TIMEOUT_TEXT)
    )


def test_error_payload_is_bounded():
    """This crosses a WebSocket to a phone; service messages can embed a lot."""
    msg = _client_error_message(ValidationException("x" * 5000))
    assert len(msg) < 600, len(msg)


def test_handler_sends_the_classified_message_and_a_distinct_close_reason():
    """Source-level, and honest about being weaker than an execution test.

    The reconnect loop lives inside `websocket_handler`, which needs a live
    BidiAgent — see this module's docstring. Without this the two functions
    above could be perfectly correct and never called, which is the
    'correct, tested and uncalled' shape that defeated a fix in the sibling
    cs-trip-simulator-parity spec.
    """
    src = inspect.getsource(websocket_handler)
    assert "_client_error_message(e)" in src
    assert "_is_idle_timeout_error(e)" in src
    assert '"idle-timeout"' in src
    # The discarded-message form must not come back.
    assert 'f"server-error: {type(e).__name__}"' not in src


# ---------------------------------------------------------------------------
# The measurement
# ---------------------------------------------------------------------------

def _state(**over):
    base = {
        "chunk_count": 0,
        "total_audio_bytes": 0,
        "gated_chunks": 0,
        "gated_bytes": 0,
        "outbound_audio_chunks": 0,
        "first_passed_monotonic": None,
        "last_passed_monotonic": None,
        "max_passed_gap_s": 0.0,
        "stream_retries": 0,
    }
    base.update(over)
    return base


class _Ctx:
    session_id = "vsa-test-session"


def _summary_fields(caplog, state, outcome="clean") -> dict[str, str]:
    caplog.clear()
    with caplog.at_level(logging.INFO):
        _log_audio_pipeline_summary(state, _Ctx(), outcome)
    line = next(r.getMessage() for r in caplog.records if "audio-pipeline" in r.getMessage())
    out = {}
    for token in line.split():
        if "=" in token:
            k, _, v = token.partition("=")
            out[k] = v
    return out


def test_gate_drop_ratio_reports_the_starved_case(caplog):
    """The observed failure shape: a handful of chunks through, most dropped.

    1 passed against 24 gated is a 96% drop, which is what a ~5s cadence against
    a ~200ms capture rate implies.
    """
    f = _summary_fields(caplog, _state(chunk_count=1, gated_chunks=24))
    assert f["gate_drop_ratio"] == "0.960"
    assert f["passed"] == "1"
    assert f["gated"] == "24"


def test_gate_drop_ratio_is_zero_when_nothing_is_dropped(caplog):
    f = _summary_fields(caplog, _state(chunk_count=50, gated_chunks=0))
    assert f["gate_drop_ratio"] == "0.000"


def test_no_audio_at_all_does_not_divide_by_zero(caplog):
    """A session that dies before the driver speaks must still log a summary."""
    f = _summary_fields(caplog, _state())
    assert f["gate_drop_ratio"] == "0.000"
    assert f["realtime_ratio"] == "0.000"
    assert f["wall_s"] == "0.0"


def test_realtime_ratio_detects_a_starved_stream(caplog):
    """1.6s of audio spread over 160s of wall clock is 1% of real time.

    16kHz 16-bit mono = 32000 bytes/sec, so 51200 bytes is 1.6 seconds.
    Nova Sonic expects roughly 1.0; the failures ran at 0.01-0.04.
    """
    f = _summary_fields(caplog, _state(
        chunk_count=32, total_audio_bytes=51_200,
        first_passed_monotonic=1_000.0, last_passed_monotonic=1_160.0,
    ))
    assert f["passed_audio_s"] == "1.6"
    assert f["wall_s"] == "160.0"
    assert f["realtime_ratio"] == "0.010"


def test_realtime_ratio_is_about_one_for_a_healthy_stream(caplog):
    """A stream keeping pace with wall clock. This is the control case.

    Without it, a starved-stream assertion proves only that the arithmetic runs.
    """
    f = _summary_fields(caplog, _state(
        chunk_count=300, total_audio_bytes=32_000 * 60,
        first_passed_monotonic=500.0, last_passed_monotonic=560.0,
    ))
    assert f["realtime_ratio"] == "1.000"


def test_summary_carries_the_fields_needed_to_compare_sessions(caplog):
    """Every field the clean-vs-failed comparison depends on is present."""
    f = _summary_fields(caplog, _state(
        chunk_count=5, gated_chunks=5, total_audio_bytes=16_000,
        outbound_audio_chunks=412, max_passed_gap_s=5.03, stream_retries=2,
        first_passed_monotonic=10.0, last_passed_monotonic=35.0,
    ), outcome="model-stream-error-attempt-2")
    for key in ("outcome", "session", "passed", "gated", "gate_drop_ratio",
                "passed_audio_s", "wall_s", "realtime_ratio",
                "max_passed_gap_s", "outbound_audio_chunks", "stream_retries"):
        assert key in f, f"summary is missing {key}"
    assert f["outcome"] == "model-stream-error-attempt-2"
    assert f["outbound_audio_chunks"] == "412"
    assert f["stream_retries"] == "2"


# ---------------------------------------------------------------------------
# Loop wiring
# ---------------------------------------------------------------------------

def test_reconnect_contract_is_wired():
    """Source-level check that the loop actually uses the pieces above.

    Weaker than an execution test, and included as a guard rather than proof:
    `websocket_handler` needs a Starlette WebSocket and a live Bedrock stream, so
    the loop cannot run here. What this catches is the realistic regression —
    someone reverting to a bare `except Exception` that closes the socket, or
    dropping the summary from an exit path — without pretending to have exercised
    a reconnect.
    """
    src = inspect.getsource(websocket_handler)

    assert "_is_retryable_model_stream_error" in src, "retry predicate not consulted"
    assert "agent.model = _new_nova_model()" in src, "no fresh model on reconnect"
    assert "MAX_MODEL_STREAM_RETRIES" in src, "retry budget not enforced"
    # Every exit path reports, otherwise clean-vs-failed cannot be compared.
    assert src.count("_log_audio_pipeline_summary") >= 4, "an exit path does not report"
    # The echo gate must be released, or a latched flag drops the new stream's audio.
    assert 'state["assistant_speaking"] = False' in src, "echo gate not reset on reconnect"
    # The gate's drops must be counted, not silently `continue`d.
    assert "gated_chunks" in inspect.getsource(
        __import__("agents.supervisor.bidi_app", fromlist=["_make_input_callable"])
        ._make_input_callable
    ), "echo gate still drops audio without counting it"
