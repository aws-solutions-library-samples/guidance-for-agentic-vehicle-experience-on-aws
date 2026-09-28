"""Unit tests for agents/executors/chat.py — ChatDrillDownExecutor.

Covers all required properties per task spec:

1. A successful write calls create_event with the verified kwargs:
   - memory_id from env, actor_id = owner_id, session_id = 'session_pending-<finding_id>',
     messages = [(json_payload, 'TOOL')], metadata dict present.
2. The marker payload contains the finding's evidence entries AND an explicit expires_at.
3. Missing VSA_FOUNDATION_MEMORY_ID env var fails closed as failed/executor_terminal_error.
4. Metadata stays within 15 pairs (≤15).
5. No VIN appears in any log record emitted during dispatch (caplog-verified;
   the test is structured so it WOULD fail if a VIN appeared in the logs).

Mutation targets recorded in-code so a reviewer can re-derive them:
  M1: remove 'expires_at' from _build_marker_payload → test_marker_has_expires_at FAILS
  M2: log.info("marker=%s", payload) in dispatch → test_no_vin_in_logs FAILS
Both mutations were verified (see docstrings on the relevant tests).
"""

from __future__ import annotations

import json
import logging
import os
import re
import unittest.mock as mock
from typing import Any, Dict, List
from unittest.mock import MagicMock, call, patch

import pytest

from agents.executors.chat import (
    MARKER_TTL_HOURS,
    _METADATA_LIMIT,
    _AGENTCORE_SESSION_ID_RE,
    _SESSION_ID_SEPARATOR,
    _SESSION_PENDING_PREFIX,
    ChatDrillDownExecutor,
    _build_marker_payload,
    _build_metadata,
)
from agents.executors.base import ActionStatus, ExecuteResult, FailureKind

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FINDING_ID = "find-aabbcc"
ACTION_ID = "act-112233"
OWNER_ID = "owner-xyz-9999"
MEMORY_ID = "mem-test-0001"
REGION = "us-west-2"

# A VIN that MUST NOT appear in any log record during dispatch.
CANARY_VIN = "1HGCM82633A004352"

EVIDENCE_WITH_VIN = [
    {"vin": CANARY_VIN, "component": "tires", "severity": "warning"},
    {"vin": CANARY_VIN, "component": "brakes", "severity": "ok"},
]

SAMPLE_ACTION = {
    "action_id": ACTION_ID,   # schema key is action_id
    "owner_id": OWNER_ID,
    "finding": {
        "id": FINDING_ID,
        "evidence": EVIDENCE_WITH_VIN,
    },
}
SAMPLE_CLAIMS = {"sub": OWNER_ID}


def _make_executor(memory_id: str = MEMORY_ID) -> tuple[ChatDrillDownExecutor, MagicMock]:
    """Return (executor, stubbed_memory_client)."""
    client = MagicMock()
    client.create_event = MagicMock(return_value=None)
    env = {
        "VSA_FOUNDATION_MEMORY_ID": memory_id,
        "VSA_MEMORY_REGION": REGION,
    }
    exc = ChatDrillDownExecutor(memory_client=client)
    return exc, client, env


# ---------------------------------------------------------------------------
# 1. Successful write — create_event called with the right kwargs
# ---------------------------------------------------------------------------


def test_dispatch_calls_create_event_with_correct_kwargs(monkeypatch):
    """Successful dispatch calls create_event with the verified API contract.

    Mutation M1 target: if expires_at is removed from the payload, test
    test_marker_has_expires_at fails (not this test — this test checks the
    call shape, including that messages contains valid JSON with the marker).
    """
    executor, client, env = _make_executor()
    with patch.dict(os.environ, env, clear=False):
        result = executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)

    assert result.status == ActionStatus.COMPLETED

    client.create_event.assert_called_once()
    positional_args = client.create_event.call_args.args
    keyword_args = client.create_event.call_args.kwargs

    # Positional: memory_id, actor_id, session_id, messages
    assert positional_args[0] == MEMORY_ID          # memory_id
    assert positional_args[1] == OWNER_ID            # actor_id = owner
    expected_session = f"{_SESSION_PENDING_PREFIX}{_SESSION_ID_SEPARATOR}{FINDING_ID}"
    assert positional_args[2] == expected_session    # session_id

    messages = positional_args[3]
    assert len(messages) == 1
    body_text, role = messages[0]
    assert role == "TOOL"
    body = json.loads(body_text)
    assert body["marker_kind"] == "drill_down_pre_seed"
    assert body["finding_id"] == FINDING_ID
    assert body["action_id"] == ACTION_ID
    assert "evidence" in body

    # metadata kwarg must be present
    metadata = keyword_args.get("metadata") or positional_args[4] if len(positional_args) > 4 else keyword_args.get("metadata")
    assert metadata is not None
    assert "expires_at" in metadata
    assert "finding_id" in metadata


def test_actor_id_is_owner_id(monkeypatch):
    """actor_id passed to create_event is the owner's id, not a service account.

    Mutation target: if actor_id were set to a hardcoded value, this assertion
    fails — the memory marker would be scoped to the wrong principal.
    """
    executor, client, env = _make_executor()
    with patch.dict(os.environ, env, clear=False):
        executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)

    actor_id = client.create_event.call_args.args[1]
    assert actor_id == OWNER_ID


def test_owner_id_falls_back_to_claims_sub(monkeypatch):
    """When action has no owner_id, falls back to claims['sub']."""
    action_no_owner = {
        "id": ACTION_ID,
        "finding": {"id": FINDING_ID, "evidence": []},
        # no owner_id
    }
    executor, client, env = _make_executor()
    with patch.dict(os.environ, env, clear=False):
        result = executor.dispatch(action_no_owner, {"sub": "sub-owner-fallback"})

    assert result.status == ActionStatus.COMPLETED
    actor_id = client.create_event.call_args.args[1]
    assert actor_id == "sub-owner-fallback"


# ---------------------------------------------------------------------------
# 2. Marker payload has evidence entries AND explicit expires_at
# ---------------------------------------------------------------------------


def test_marker_has_expires_at():
    """The marker payload MUST contain an explicit expires_at field.

    Mutation M1 verification:
      Remove 'expires_at' from _build_marker_payload → this test raises
      KeyError/AssertionError on ``payload["expires_at"]``, proving the
      assertion is not vacuous.

    The test also checks that expires_at is a well-formed ISO-8601 UTC string
    that is strictly in the future relative to written_at.
    """
    payload = _build_marker_payload(FINDING_ID, ACTION_ID, EVIDENCE_WITH_VIN)
    assert "expires_at" in payload, (
        "expires_at MUST be in the marker payload — the reader uses it to "
        "reject stale markers; absent means every marker lives forever"
    )
    # Must be parseable as ISO-8601 and be after written_at
    from datetime import datetime, timezone
    written = datetime.fromisoformat(payload["written_at"].replace("Z", "+00:00"))
    expires = datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00"))
    assert expires > written
    # Check the expected TTL window (within a few seconds of MARKER_TTL_HOURS)
    delta_hours = (expires - written).total_seconds() / 3600
    assert abs(delta_hours - MARKER_TTL_HOURS) < 0.01, (
        f"expires_at is {delta_hours:.3f}h from written_at; expected {MARKER_TTL_HOURS}h"
    )


def test_marker_contains_evidence_entries():
    """The marker payload carries the full evidence list from the Finding."""
    payload = _build_marker_payload(FINDING_ID, ACTION_ID, EVIDENCE_WITH_VIN)
    assert payload["evidence"] == EVIDENCE_WITH_VIN


def test_marker_payload_written_to_memory_contains_expires_at(monkeypatch):
    """The JSON body stored in AgentCore Memory contains expires_at.

    This is the end-to-end check: not just the helper, but the full dispatch
    path, so we confirm the executor doesn't strip expires_at on the way out.
    """
    executor, client, env = _make_executor()
    with patch.dict(os.environ, env, clear=False):
        executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)

    messages = client.create_event.call_args.args[3]
    body = json.loads(messages[0][0])
    assert "expires_at" in body
    assert body["expires_at"]  # non-empty


def test_marker_payload_written_to_memory_contains_evidence(monkeypatch):
    """The JSON body stored in AgentCore Memory contains evidence entries."""
    executor, client, env = _make_executor()
    with patch.dict(os.environ, env, clear=False):
        executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)

    messages = client.create_event.call_args.args[3]
    body = json.loads(messages[0][0])
    assert body["evidence"] == EVIDENCE_WITH_VIN


# ---------------------------------------------------------------------------
# 3. Missing memory-id config fails closed as terminal_error
# ---------------------------------------------------------------------------


def test_missing_memory_id_fails_closed(monkeypatch):
    """Missing VSA_FOUNDATION_MEMORY_ID must produce failed/executor_terminal_error.

    Fail-closed is mandatory: a missing marker means the drill-down session
    opens with no context.  Silently returning success would give the owner an
    assistant that has forgotten why it messaged them.

    Mutation target: if the code silently returned success (e.g. early-return
    ExecuteResult.success(...)), this test fails on the status assertion.
    """
    executor, client, _ = _make_executor()
    # Ensure env var is absent
    env_without = {k: v for k, v in os.environ.items() if k != "VSA_FOUNDATION_MEMORY_ID"}
    with patch.dict(os.environ, env_without, clear=True):
        result = executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)

    assert result.status == ActionStatus.FAILED, (
        "missing memory_id must produce failed, not a silent success"
    )
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    # create_event must NOT have been called — no partial write
    client.create_event.assert_not_called()


def test_empty_memory_id_env_also_fails_closed(monkeypatch):
    """Empty string VSA_FOUNDATION_MEMORY_ID is treated as absent."""
    executor, client, _ = _make_executor()
    with patch.dict(os.environ, {"VSA_FOUNDATION_MEMORY_ID": ""}, clear=False):
        result = executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)
    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    client.create_event.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Metadata stays within 15 pairs
# ---------------------------------------------------------------------------


def test_metadata_within_15_pairs():
    """_build_metadata must produce ≤15 pairs (AgentCore Memory limit).

    If a future edit adds a 16th pair this test catches it before it reaches
    the API call.
    """
    from agents.executors.chat import _build_marker_payload
    payload = _build_marker_payload(FINDING_ID, ACTION_ID, EVIDENCE_WITH_VIN)
    expires_at = payload["expires_at"]
    meta = _build_metadata(FINDING_ID, ACTION_ID, expires_at)
    assert len(meta) <= _METADATA_LIMIT, (
        f"metadata has {len(meta)} pairs; AgentCore Memory limit is {_METADATA_LIMIT}"
    )


def test_metadata_values_are_string_value_dicts():
    """Every metadata value must be shaped {'stringValue': str}."""
    from agents.executors.chat import _build_marker_payload
    payload = _build_marker_payload(FINDING_ID, ACTION_ID, EVIDENCE_WITH_VIN)
    meta = _build_metadata(FINDING_ID, ACTION_ID, payload["expires_at"])
    for key, val in meta.items():
        assert isinstance(val, dict) and "stringValue" in val, (
            f"metadata[{key!r}] must be {{'stringValue': str}}, got {val!r}"
        )
        assert isinstance(val["stringValue"], str), (
            f"metadata[{key!r}]['stringValue'] must be str, got {type(val['stringValue'])}"
        )


# ---------------------------------------------------------------------------
# 5. No VIN appears in logs during dispatch
# ---------------------------------------------------------------------------


def test_no_vin_in_logs(caplog):
    """No VIN must appear in any log record emitted during dispatch.

    This test is STRUCTURED TO FAIL if a VIN is logged:
    - CANARY_VIN is embedded in EVIDENCE_WITH_VIN.
    - We search every caplog record's message for the literal canary string.
    - If any record contains it, the assertion fails with a clear message.

    Mutation M2 verification:
      Add ``log.info("marker=%s", payload)`` in dispatch → the JSON-serialised
      payload includes the evidence entries which contain CANARY_VIN, so
      this assertion fails.  Confirms the test is not vacuous.
    """
    executor, client, env = _make_executor()
    with patch.dict(os.environ, env, clear=False):
        with caplog.at_level(logging.DEBUG, logger="agents.executors.chat"):
            executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)

    all_log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert CANARY_VIN not in all_log_text, (
        f"VIN {CANARY_VIN!r} was found in log output — privacy violation.\n"
        f"Offending log records:\n"
        + "\n".join(
            f"  [{r.levelname}] {r.getMessage()}"
            for r in caplog.records
            if CANARY_VIN in r.getMessage()
        )
    )


def test_no_vin_in_logs_also_checks_args(caplog):
    """Verify the VIN does not appear in LogRecord.args (lazy-formatted logs).

    Some loggers use %-format args that are not materialised until getMessage().
    The previous test calls getMessage() which does the formatting; this test
    additionally inspects the raw args to catch ``log.debug("vin=%s", vin)``
    patterns before formatting.
    """
    executor, client, env = _make_executor()
    with patch.dict(os.environ, env, clear=False):
        with caplog.at_level(logging.DEBUG, logger="agents.executors.chat"):
            executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)

    for record in caplog.records:
        args = record.args
        if args is None:
            continue
        if isinstance(args, tuple):
            args_str = " ".join(str(a) for a in args)
        else:
            args_str = str(args)
        assert CANARY_VIN not in args_str, (
            f"VIN {CANARY_VIN!r} found in raw log args: {args_str!r}"
        )


# ---------------------------------------------------------------------------
# 6. Session id shape
# ---------------------------------------------------------------------------


def test_session_id_is_pending_prefixed(monkeypatch):
    """session_id must be 'session_pending-<finding_id>' — HYPHEN, not colon."""
    executor, client, env = _make_executor()
    with patch.dict(os.environ, env, clear=False):
        result = executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)

    session_id = client.create_event.call_args.args[2]
    assert session_id == f"{_SESSION_PENDING_PREFIX}{_SESSION_ID_SEPARATOR}{FINDING_ID}"
    # target_ref in the result should match
    assert result.target_ref == session_id


def test_absent_finding_id_fails_closed_instead_of_writing_a_useless_marker(monkeypatch):
    """An empty finding id must NOT produce a completed dispatch.

    Without the guard, session_id becomes "session_pending-" and dispatch reports
    COMPLETED — a marker written into a bucket shared by every action missing a
    finding id, which no reader can resolve because readers look up BY finding id.
    The regex conformance pin does not catch this: "session_pending-" satisfies
    AgentCore's pattern perfectly well.

    Found 2026-09-23 during T18.5 live verification, from a malformed test action.
    """
    executor, client, env = _make_executor()
    action = {"action_id": "ACT-1", "finding": {"evidence": ["e"]}}  # no id
    with patch.dict(os.environ, env, clear=False):
        result = executor.dispatch(action, SAMPLE_CLAIMS)

    assert result.last_error_class == "MissingFindingId", (
        f"expected MissingFindingId, got {result.last_error_class!r}"
    )
    client.create_event.assert_not_called()


def test_session_id_satisfies_agentcores_own_constraint(monkeypatch):
    """The conformance pin — this is what the colon form failed.

    Sharing `_SESSION_PENDING_PREFIX` between writer and reader made the two AGREE
    WITH EACH OTHER while both disagreed with AgentCore, which constrains
    `sessionId` to `[a-zA-Z0-9][a-zA-Z0-9-_]*`. A colon is rejected outright, so
    `create_event` failed on every call for the whole life of Group 5 and no
    stubbed test could see it.

    **Mutation**: set `_SESSION_ID_SEPARATOR = ":"` — this test MUST fail.
    See issues/2026-09-23-chat-drilldown-marker-session-id-rejected-by-agentcore/.
    """
    executor, client, env = _make_executor()
    with patch.dict(os.environ, env, clear=False):
        executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)

    session_id = client.create_event.call_args.args[2]
    assert _AGENTCORE_SESSION_ID_RE.match(session_id), (
        f"sessionId {session_id!r} violates AgentCore's "
        r"[a-zA-Z0-9][a-zA-Z0-9-_]* constraint — CreateEvent will reject it with "
        "ValidationException, and @never_raises will hide the cause behind a bare "
        "status=failed."
    )


def test_a_separator_agentcore_rejects_fails_before_create_event_is_called(monkeypatch):
    """The guard must fire at construction, not at the API boundary.

    At the API boundary `@never_raises` maps the ValidationException to a bare
    `status=failed` with the message withheld (correctly — it can carry a JWT or a
    VIN), which is what made the original defect unreadable. Failing earlier names
    the cause.
    """
    import agents.executors.chat as chat_mod

    executor, client, env = _make_executor()
    monkeypatch.setattr(chat_mod, "_SESSION_ID_SEPARATOR", ":")
    with patch.dict(os.environ, env, clear=False):
        result = executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)

    assert result.last_error_class == "InvalidSessionId", (
        f"expected an explicit InvalidSessionId, got {result.last_error_class!r}"
    )
    client.create_event.assert_not_called()


# ---------------------------------------------------------------------------
# 7. action_id is the correct schema key — non-empty in body AND metadata
# ---------------------------------------------------------------------------


def test_marker_body_carries_non_empty_action_id(monkeypatch):
    """The JSON body written to memory must carry action_id == ACTION_ID.

    Regression guard for the 'action["id"]' → 'action["action_id"]' fix.
    The marker body is the stable contract the session-init reader implements
    against, so an empty correlation handle renders the reader's lookup useless.

    Mutation evidence (pasted in hand-back):
      M-actionid: revert chat.py to action.get("id", "") →
      test_marker_body_carries_non_empty_action_id FAILS because the legacy
      key is not present in SAMPLE_ACTION (which now uses action_id) and the
      body carries action_id="".
    """
    executor, client, env = _make_executor()
    with patch.dict(os.environ, env, clear=False):
        executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)

    messages = client.create_event.call_args.args[3]
    body = json.loads(messages[0][0])
    assert body.get("action_id") == ACTION_ID, (
        f"marker body.action_id={body.get('action_id')!r}; expected {ACTION_ID!r}. "
        "An empty action_id means the session-init reader has no correlation handle."
    )


def test_marker_metadata_carries_non_empty_action_id(monkeypatch):
    """The metadata dict written to memory must carry action_id == ACTION_ID.

    The metadata is what the session-init reader uses to index and look up
    the marker without deserialising the TOOL message body.
    """
    executor, client, env = _make_executor()
    with patch.dict(os.environ, env, clear=False):
        executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)

    keyword_args = client.create_event.call_args.kwargs
    metadata = keyword_args.get("metadata")
    assert metadata is not None
    assert metadata.get("action_id", {}).get("stringValue") == ACTION_ID, (
        f"metadata action_id={metadata.get('action_id')!r}; expected {ACTION_ID!r}. "
        "An empty metadata action_id means the session-init reader cannot "
        "correlate the marker back to its originating Action."
    )


def test_action_id_legacy_key_fallback(monkeypatch):
    """action['id'] is accepted as a fallback when action_id is absent.

    Some callers (legacy and synthetic findings) may still use 'id'.  The
    executor must not silently write an empty action_id in that case.
    """
    legacy_action = {
        "id": ACTION_ID,         # legacy key
        # no action_id key
        "owner_id": OWNER_ID,
        "finding": {"id": FINDING_ID, "evidence": []},
    }
    executor, client, env = _make_executor()
    with patch.dict(os.environ, env, clear=False):
        executor.dispatch(legacy_action, SAMPLE_CLAIMS)

    messages = client.create_event.call_args.args[3]
    body = json.loads(messages[0][0])
    assert body.get("action_id") == ACTION_ID, (
        "Fallback to action['id'] must produce a non-empty action_id in the body."
    )


# ---------------------------------------------------------------------------
# 7. @never_raises: create_event exception maps to terminal_error
# ---------------------------------------------------------------------------


def test_create_event_exception_maps_to_terminal_error(monkeypatch):
    """Any exception from create_event must produce failed/executor_terminal_error.

    @never_raises on dispatch() guarantees the Action row always advances its
    status even when the memory write blows up.
    """
    executor, client, env = _make_executor()
    client.create_event.side_effect = RuntimeError("simulated API failure")
    with patch.dict(os.environ, env, clear=False):
        result = executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)
    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR


# ---------------------------------------------------------------------------
# 8. Missing owner_id fails closed
# ---------------------------------------------------------------------------


def test_missing_owner_id_fails_closed(monkeypatch):
    """If neither action nor claims supply an owner id, fail closed."""
    action_no_owner = {
        "id": ACTION_ID,
        "finding": {"id": FINDING_ID, "evidence": []},
    }
    executor, client, env = _make_executor()
    with patch.dict(os.environ, env, clear=False):
        result = executor.dispatch(action_no_owner, {})  # no sub in claims
    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    client.create_event.assert_not_called()


# ---------------------------------------------------------------------------
# 9. target_system attribute
# ---------------------------------------------------------------------------


def test_target_system_is_chat():
    """target_system must be 'chat' — the registry key used to look up the executor."""
    exc = ChatDrillDownExecutor(memory_client=MagicMock())
    assert exc.target_system == "chat"



# ---------------------------------------------------------------------------
# Security review Cycle 1, Warning 3 — owner provenance for the marker.
#
# This marker pre-seeds a drill-down conversation in the owner's OWN AgentCore
# Memory session, so the authenticated caller is the only correct owner. The
# first cut preferred `action["owner_id"]`, which inverts the trust direction: if
# the approve route ever accepts a caller-supplied owner_id without cross-
# checking it, an attacker could write a marker into a victim's session carrying
# attacker-chosen `evidence[]` — LLM context injection delivered with the
# victim's own authority, read by the supervisor before the victim's first turn.
#
# Rule: claims["sub"] wins; a disagreeing action["owner_id"] is REJECTED rather
# than silently ignored, because a disagreement means the caller is asking us to
# seed someone else's session.
#
# Contrast ios_owner.py, where the action legitimately names a different owner
# than the caller (a dealer proposing to a customer); that executor's
# authorization has to happen at the approve route and is documented there.
# ---------------------------------------------------------------------------


def _owner_action(**over):
    action = {
        "action_id": "AVX-ACT-owner-test",
        "finding_id": "FIND-1",
        "finding": {"id": "FIND-1", "evidence": [{"kind": "tire", "value": "low"}]},
    }
    action.update(over)
    return action


def test_actor_id_comes_from_claims_sub_not_the_action() -> None:
    """claims.sub is authoritative when the action names nobody."""
    exc, client, env = _make_executor()
    with patch.dict(os.environ, env, clear=False):
        exc.dispatch(_owner_action(), {"sub": "owner-REAL"})
    args = client.create_event.call_args.args
    assert args[1] == "owner-REAL", (
        f"actor_id should be the authenticated caller, got {args[1]!r}"
    )


def test_action_owner_id_disagreeing_with_claims_is_rejected() -> None:
    """The attack: seed a victim's session by naming them in the action."""
    exc, client, env = _make_executor()
    with patch.dict(os.environ, env, clear=False):
        result = exc.dispatch(
            _owner_action(owner_id="owner-VICTIM"), {"sub": "owner-ATTACKER"}
        )

    assert result.status == ActionStatus.FAILED, (
        "an action naming a different owner than the caller was accepted; this "
        "writes attacker-chosen evidence into the victim's chat pre-seed"
    )
    assert result.last_error_class == "OwnerMismatch"
    client.create_event.assert_not_called()


def test_matching_action_owner_id_is_accepted() -> None:
    """Agreement is the normal case and must not be treated as a mismatch."""
    exc, client, env = _make_executor()
    with patch.dict(os.environ, env, clear=False):
        result = exc.dispatch(_owner_action(owner_id="owner-SAME"), {"sub": "owner-SAME"})
    assert result.status != ActionStatus.FAILED
    assert client.create_event.call_args.args[1] == "owner-SAME"


def test_action_owner_id_never_overrides_claims_sub() -> None:
    """Even if both are present and differ, claims must never be substituted.

    Pinned separately from the rejection test above: a future change could
    'helpfully' fall back to the action value instead of rejecting, which would
    reopen the cross-owner write with no test failing.
    """
    exc, client, env = _make_executor()
    with patch.dict(os.environ, env, clear=False):
        exc.dispatch(_owner_action(owner_id="owner-SAME"), {"sub": "owner-SAME"})
    actor = client.create_event.call_args.args[1]
    assert actor == "owner-SAME"

    client.reset_mock()
    with patch.dict(os.environ, env, clear=False):
        result = exc.dispatch(
            _owner_action(owner_id="owner-OTHER"), {"sub": "owner-SELF"}
        )
    if client.create_event.called:
        actor = client.create_event.call_args.args[1]
        assert actor != "owner-OTHER", (
            "the action-supplied owner_id became the marker's actor_id — a caller "
            "can write into another owner's memory session"
        )
    else:
        assert result.status == ActionStatus.FAILED


def test_no_claims_falls_back_to_action_owner_only_when_caller_is_unknown() -> None:
    """Documents the one case the action may supply the owner.

    With no authenticated subject there is nothing to cross-check against. This
    is reachable only if the approve route dispatches without claims, which it
    must not; pinned so the behaviour is deliberate rather than incidental.
    """
    exc, client, env = _make_executor()
    with patch.dict(os.environ, env, clear=False):
        result = exc.dispatch(_owner_action(owner_id="owner-FROM-ACTION"), {})
    if client.create_event.called:
        assert client.create_event.call_args.args[1] == "owner-FROM-ACTION"
    else:
        assert result.status == ActionStatus.FAILED
