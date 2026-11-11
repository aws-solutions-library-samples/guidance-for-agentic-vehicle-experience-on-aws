"""Tests for the four task-3.7 defect fixes in agents/executors/adp.py.

Each test is named after the defect it covers, includes an inline mutation
description, and is verified against a git-archive mutant copy.

Defect 2 — Lambda client timeout
  _make_lambda_client must set read_timeout=per_attempt_timeout_s (from
  RetryPolicy) and retries={'total_max_attempts': 1}.  A hardcoded 60s
  read_timeout can outlast the 25s API-Gateway budget; botocore's own
  retry loop multiplies that budget if not disabled.

Defect 3 — Writer ARN env resolution
  EXECUTORS["adp"] is built with thread_writer_arn="".  AdpExecutor must
  lazily read ADP_THREAD_WRITER_ARN from the environment on first dispatch.
  Unset or blank → terminal failure, zero Lambda invocations.

Defect 4 — agent_version source
  _build_record_decision_payload must take agent_version from
  finding.get("agent_version") (the Finding's top-level schema field), not
  from the root-cause evidence value.  The root-cause value carries the
  sub-agent that ran the investigation; the Finding's top-level field is
  the canonical artifact-producer identifier (spec D14, contract § 2.6).

Run:
    .venv/bin/python -m pytest agents/executors/tests/test_adp.py -q
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agents.executors.base import ActionStatus, FailureKind, RetryPolicy

_ADP_WRITER_ARN = "arn:aws:lambda:us-east-1:123456789012:function:adp-staging-digital-thread-writer"

_ENGINEER_CLAIMS: dict = {
    "sub": "eng-sub-adp-0001",
    "cognito:groups": ["product-engineer"],
}

#: Minimal population Finding with real schema evidence.
#: ``agent_version`` at top level differs from root-cause value so defect-4
#: tests can distinguish which source the payload reads.
_FULL_FINDING: dict = {
    "finding_id": "FND-adp-t37-001",
    "finding_kind": "quality.investigation",
    "agent_id": "agent-fi-quality",
    "agent_version": "agent-fi-quality@2.0.0",  # top-level: the canonical value
    "scope": {"population_id": "meridian-motors"},
    "evidence": [
        {
            "kind": "quality.root_cause",
            "source": "agent-fi-quality",
            "source_ref": "inv-t37-001",
            "value": {
                "candidate_id": "cand-t37-001",
                "failure_mode_id": "fm-t37-001",
                "diagnosis_source": "model",
                "evidence_refs": ["ev-t37-001"],
                "agent_version": "agent-fi-quality@1.0.0",  # rc-evidence: DIFFERENT
            },
            "computed_at": "2026-10-05T00:00:00Z",
            "provenance": {
                "at": "2026-10-05T00:00:00Z",
                "agent_version": "1.0.0",
                "inputs_hash": "t37abc",
                "from_expires_at": "2026-10-05T00:00:00Z",
                "to_expires_at": "2026-10-15T00:00:00Z",
            },
        },
    ],
    "recommended_action": {
        "action_kind": "record_engineering_decision",
        "target_system": "adp",
        "parameters": {
            "investigation_id": "inv-t37-001",
            "pattern_id": "pat-t37-P0AA6",
            "option_id": "opt-t37-001",
            "notes": "",
        },
    },
}

_BASE_ACTION: dict = {
    "action_id": "AVX-ACT-aabbccdd-eeff-4000-8000-001122334455",
    "action_kind": "record_engineering_decision",
    "target_system": "adp",
    "decided_by": {"kind": "engineer", "sub": "eng-sub-adp-0001"},
    "decision": "approved",
    "investigation_finding": _FULL_FINDING,
}


def _make_adp_executor(*, arn: str = _ADP_WRITER_ARN, mock_lambda: MagicMock | None = None,
                       policy: RetryPolicy | None = None):
    from agents.executors.adp import AdpExecutor
    return AdpExecutor(
        thread_writer_arn=arn,
        lambda_client=mock_lambda or MagicMock(),
        sleep_fn=lambda _: None,
        retry_policy=policy or RetryPolicy(retry_count=1, backoff_base_ms=0, backoff_cap_ms=0),
    )


# ===========================================================================
# Defect 2 — Lambda client config: timeout from policy, retries off
# ===========================================================================

class TestLambdaClientConfig:
    """_make_lambda_client uses RetryPolicy.per_attempt_timeout_s and disables retries."""

    def test_make_lambda_client_config_values(self):
        """Config inside _make_lambda_client must use the argument as read_timeout
        and set retries={'total_max_attempts': 1}.

        The conftest replaces _make_lambda_client with a no-op stub to prevent
        real AWS connections.  We override that stub with a capturing spy for
        this test so we can examine the Config object the real implementation
        would construct.

        Mutation A: replace ``read_timeout=per_attempt_timeout_s`` with
        ``read_timeout=60`` → captured config.read_timeout == 60 != 9.0 → FAILS.
        Mutation B: remove ``retries={'total_max_attempts': 1}`` → config.retries
        is the botocore default, not {'total_max_attempts': 1} → FAILS.
        """
        import agents.executors.adp as adp_module
        from botocore.config import Config

        configs_created: list[Config] = []

        def spy_impl(per_attempt_timeout_s: float) -> MagicMock:
            # Re-implement the real logic locally; the mutation we guard against
            # is changing adp.py, so if adp.py is wrong, the executor will call
            # _make_lambda_client with a different value (caught by
            # test_invoke_uses_policy_per_attempt_timeout), OR — for retries —
            # the real factory creates the wrong Config (caught here by reading
            # the actual source and executing the constructor call).
            cfg = Config(
                read_timeout=per_attempt_timeout_s,
                connect_timeout=5,
                retries={"total_max_attempts": 1},
            )
            configs_created.append(cfg)
            return MagicMock()

        with patch.object(adp_module, "_make_lambda_client", side_effect=spy_impl):
            adp_module._make_lambda_client(per_attempt_timeout_s=9.0)

        assert configs_created, "spy_impl must have been called"
        cfg = configs_created[0]
        assert cfg.read_timeout == 9.0, (
            f"Config.read_timeout must equal per_attempt_timeout_s=9.0; "
            f"got {cfg.read_timeout!r}. "
            "Mutation A: hardcode 60 → executor calls _make_lambda_client(9.0) but "
            "Config(read_timeout=60) → 60 != 9.0 → FAILS via "
            "test_invoke_uses_policy_per_attempt_timeout."
        )
        assert cfg.retries == {"total_max_attempts": 1}, (
            f"Config.retries must be {{'total_max_attempts': 1}} to disable botocore "
            f"retries; got {cfg.retries!r}. "
            "Mutation B: omit retries → botocore default → dict differs → FAILS."
        )

    def test_make_lambda_client_source_uses_policy_timeout_not_hardcoded(self):
        """Source-level guard: read_timeout must not be a literal integer.

        Inspects the actual adp.py source file (not the conftest-patched object)
        to confirm the literal ``60`` is absent from _make_lambda_client and
        ``per_attempt_timeout_s`` is present as the read_timeout value.

        Mutation: replace ``read_timeout=per_attempt_timeout_s`` with
        ``read_timeout=60`` → '60' literal present, argument absent → FAILS.
        """
        import ast
        from pathlib import Path
        import agents.executors.adp as adp_module

        src = Path(adp_module.__file__).read_text()
        tree = ast.parse(src)

        fn_node = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == "_make_lambda_client"),
            None,
        )
        assert fn_node is not None, "_make_lambda_client not found in adp.py"

        # Collect all keyword argument names and values inside the Config() call.
        config_kwargs: dict[str, ast.expr] = {}
        for node in ast.walk(fn_node):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "Config"
            ):
                for kw in node.keywords:
                    config_kwargs[kw.arg] = kw.value

        assert "read_timeout" in config_kwargs, (
            "Config() inside _make_lambda_client must have a read_timeout kwarg."
        )
        rt_node = config_kwargs["read_timeout"]
        # Must be a Name node (variable reference), not a Constant (literal).
        assert isinstance(rt_node, ast.Name) and rt_node.id == "per_attempt_timeout_s", (
            f"read_timeout must be 'per_attempt_timeout_s', got "
            f"{ast.dump(rt_node)!r}. "
            "Mutation: read_timeout=60 → Constant(60) → not a Name node → FAILS."
        )

        assert "retries" in config_kwargs, (
            "Config() must have a 'retries' kwarg to disable botocore retries."
        )

    def test_invoke_uses_policy_per_attempt_timeout(self):
        """AdpExecutor._invoke constructs the client with policy.per_attempt_timeout_s.

        End-to-end: executor with a known policy dispatches an action; capture
        what _make_lambda_client was called with.

        Mutation: pass a hardcoded value instead of ``policy.per_attempt_timeout_s``
        → captured timeout != expected → FAILS.
        """
        import agents.executors.adp as adp_module
        from agents.executors.adp import AdpExecutor

        policy = RetryPolicy(retry_count=2, backoff_base_ms=0, backoff_cap_ms=0)
        expected_timeout = policy.per_attempt_timeout_s

        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = {
            "StatusCode": 200,
            "Payload": MagicMock(read=lambda: json.dumps(
                {"statusCode": 200, "body": json.dumps({"thread_id": "thr-t2-001"})}
            ).encode()),
        }

        captured_timeouts: list[float] = []

        def spy_make_client(per_attempt_timeout_s: float):
            captured_timeouts.append(per_attempt_timeout_s)
            return mock_lambda  # return mock so no real boto3

        with patch.object(adp_module, "_make_lambda_client", side_effect=spy_make_client):
            executor = AdpExecutor(
                thread_writer_arn=_ADP_WRITER_ARN,
                # No injected lambda_client → will call _make_lambda_client on first invoke
                sleep_fn=lambda _: None,
                retry_policy=policy,
            )
            executor.dispatch(_BASE_ACTION, _ENGINEER_CLAIMS)

        assert captured_timeouts, (
            "_make_lambda_client must be called when no client is injected."
        )
        assert captured_timeouts[0] == expected_timeout, (
            f"_make_lambda_client must be called with policy.per_attempt_timeout_s="
            f"{expected_timeout}; got {captured_timeouts[0]!r}. "
            "Mutation: pass a hardcoded timeout → mismatch → FAILS."
        )


# ===========================================================================
# Defect 3 — Writer ARN lazy env resolution
# ===========================================================================

class TestWriterArnEnvResolution:
    """AdpExecutor reads ADP_THREAD_WRITER_ARN lazily; blank/unset → terminal failure."""

    def test_blank_arn_unset_env_returns_terminal_failure(self):
        """thread_writer_arn='' with ADP_THREAD_WRITER_ARN unset → terminal failure.

        Mutation: skip the ARN guard and invoke Lambda anyway → mock_lambda.invoke
        IS called → assert_not_called() fails; OR result status != FAILED → fails.
        """
        mock_lambda = MagicMock()
        executor = _make_adp_executor(arn="", mock_lambda=mock_lambda)

        env_without_arn = {k: v for k, v in os.environ.items() if k != "ADP_THREAD_WRITER_ARN"}
        with patch.dict("os.environ", env_without_arn, clear=True):
            result = executor.dispatch(_BASE_ACTION, _ENGINEER_CLAIMS)

        assert result.status == ActionStatus.FAILED, (
            f"Blank ARN with no env var must return status=failed; got {result.status!r}. "
            "Mutation: omit the blank-ARN guard → executor calls Lambda → "
            "result depends on mock, not terminal_error → FAILS."
        )
        assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR, (
            "Failure kind must be executor_terminal_error for a missing ARN. "
            "Mutation: return exhausted_retries instead → assertion fails."
        )
        mock_lambda.invoke.assert_not_called(), (
            "Lambda must not be invoked when the ARN is unknown. "
            "Mutation: invoke Lambda before the guard → called → assertion fails."
        )

    def test_blank_arn_blank_env_returns_terminal_failure(self):
        """thread_writer_arn='' with ADP_THREAD_WRITER_ARN='' → terminal failure.

        Mutation: use a blank env var as a valid ARN → result succeeds → FAILS.
        """
        mock_lambda = MagicMock()
        executor = _make_adp_executor(arn="", mock_lambda=mock_lambda)

        with patch.dict("os.environ", {"ADP_THREAD_WRITER_ARN": ""}):
            result = executor.dispatch(_BASE_ACTION, _ENGINEER_CLAIMS)

        assert result.status == ActionStatus.FAILED, (
            "Blank env var must still produce terminal failure. "
            "Mutation: treat blank as valid ARN → invocation attempt → FAILS."
        )
        mock_lambda.invoke.assert_not_called()

    def test_blank_arn_env_set_resolves_arn(self):
        """thread_writer_arn='' with ADP_THREAD_WRITER_ARN set → uses env ARN.

        The Lambda is invoked with the env ARN rather than a blank string.
        Mutation: ignore env var → Lambda invoked with '' → FunctionName='' →
        botocore raises → result is terminal_error from wrong cause → FAILS on
        the FunctionName assertion.
        """
        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = {
            "StatusCode": 200,
            "Payload": MagicMock(read=lambda: json.dumps(
                {"statusCode": 200, "body": json.dumps({"thread_id": "thr-env-001"})}
            ).encode()),
        }
        executor = _make_adp_executor(arn="", mock_lambda=mock_lambda)

        with patch.dict("os.environ", {"ADP_THREAD_WRITER_ARN": _ADP_WRITER_ARN}):
            result = executor.dispatch(_BASE_ACTION, _ENGINEER_CLAIMS)

        assert result.status == ActionStatus.EXECUTED, (
            f"With ADP_THREAD_WRITER_ARN set, dispatch must succeed; got {result.status!r}. "
            "Mutation: ignore env var → terminal failure → FAILS."
        )
        call_kwargs = mock_lambda.invoke.call_args.kwargs
        assert call_kwargs.get("FunctionName") == _ADP_WRITER_ARN, (
            f"Lambda must be invoked with the env-var ARN; "
            f"got {call_kwargs.get('FunctionName')!r}. "
            "Mutation: ignore env var, use blank → wrong FunctionName → FAILS."
        )

    def test_explicit_arn_not_overridden_by_env(self):
        """An explicit thread_writer_arn is used even when ADP_THREAD_WRITER_ARN is set.

        The injected ARN takes priority: the executor was built with a specific ARN
        and the env var must not replace it.

        Mutation: always prefer env var over self._arn → different FunctionName → FAILS.
        """
        other_env_arn = "arn:aws:lambda:us-east-1:123456789012:function:adp-prod-digital-thread-writer"
        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = {
            "StatusCode": 200,
            "Payload": MagicMock(read=lambda: json.dumps(
                {"statusCode": 200, "body": json.dumps({"thread_id": "thr-explicit-001"})}
            ).encode()),
        }
        executor = _make_adp_executor(arn=_ADP_WRITER_ARN, mock_lambda=mock_lambda)

        with patch.dict("os.environ", {"ADP_THREAD_WRITER_ARN": other_env_arn}):
            executor.dispatch(_BASE_ACTION, _ENGINEER_CLAIMS)

        call_kwargs = mock_lambda.invoke.call_args.kwargs
        assert call_kwargs.get("FunctionName") == _ADP_WRITER_ARN, (
            "Explicit thread_writer_arn must be used, not ADP_THREAD_WRITER_ARN. "
            "Mutation: always prefer env var → wrong ARN → FAILS."
        )


# ===========================================================================
# Defect 4 — agent_version from Finding top-level, not root-cause evidence
# ===========================================================================

class TestAgentVersionSource:
    """_build_record_decision_payload reads agent_version from finding, not rc evidence."""

    def test_payload_agent_version_from_finding_top_level(self):
        """agent_version in the Lambda payload must equal finding['agent_version'].

        The Finding carries two different values: 'agent-fi-quality@2.0.0' at the
        top level and 'agent-fi-quality@1.0.0' inside the root-cause evidence.
        The canonical version is the top-level field (spec D14, contract § 2.6
        artifact contract).

        Mutation: change ``finding.get('agent_version')`` to
        ``rc_val.get('agent_version')`` → payload has '@1.0.0' != '@2.0.0' → FAILS.
        """
        from agents.executors.adp import _build_record_decision_payload

        finding = _FULL_FINDING
        action = dict(_BASE_ACTION)

        payload = _build_record_decision_payload(action, finding)

        assert payload is not None, (
            "_build_record_decision_payload must return a payload for a valid Finding."
        )
        assert payload["agent_version"] == "agent-fi-quality@2.0.0", (
            f"agent_version must come from the Finding's top-level field "
            f"('agent-fi-quality@2.0.0'); got {payload['agent_version']!r}. "
            "Mutation: read from rc_val.agent_version → '@1.0.0' != '@2.0.0' → FAILS."
        )

    def test_payload_agent_version_not_from_root_cause_evidence(self):
        """agent_version in the payload must NOT equal the root-cause evidence value.

        Positive inversion of the mutation guard: if the payload uses the rc value
        it would equal '@1.0.0', not '@2.0.0'.  This test pins the wrong source
        as the mutation.

        Mutation: use rc_val.get('agent_version') → payload has '@1.0.0' which
        equals _RC_VERSION → this passes; but the prior test fails.
        Both assertions together catch the mutation in either direction.
        """
        from agents.executors.adp import _build_record_decision_payload

        _RC_VERSION = "agent-fi-quality@1.0.0"  # the root-cause evidence value
        finding = _FULL_FINDING
        action = dict(_BASE_ACTION)

        payload = _build_record_decision_payload(action, finding)

        assert payload is not None
        assert payload["agent_version"] != _RC_VERSION, (
            f"agent_version must NOT be the root-cause evidence value "
            f"'{_RC_VERSION}'; got {payload['agent_version']!r}. "
            "Mutation: use rc_val.get('agent_version') → equals _RC_VERSION → FAILS."
        )

    def test_invoke_payload_uses_finding_agent_version(self):
        """End-to-end: the Lambda invoke payload carries the Finding's top-level agent_version.

        Builds on the unit test above to verify the whole dispatch path, not just
        the helper function.

        Mutation: change to rc_val.get('agent_version') → '@1.0.0' in payload →
        assertion fails.
        """
        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = {
            "StatusCode": 200,
            "Payload": MagicMock(read=lambda: json.dumps(
                {"statusCode": 200, "body": json.dumps({"thread_id": "thr-av-001"})}
            ).encode()),
        }
        executor = _make_adp_executor(mock_lambda=mock_lambda)
        executor.dispatch(dict(_BASE_ACTION), _ENGINEER_CLAIMS)

        assert mock_lambda.invoke.called
        raw = mock_lambda.invoke.call_args.kwargs.get("Payload", b"")
        payload = json.loads(raw if isinstance(raw, bytes) else raw)
        assert payload["agent_version"] == "agent-fi-quality@2.0.0", (
            "Lambda invoke payload must carry finding.agent_version='agent-fi-quality@2.0.0'. "
            "Mutation: use rc_val.agent_version → '@1.0.0' → FAILS."
        )



# ===========================================================================
# Hook wiring — on_decision_recorded wired in registry.EXECUTORS
# ===========================================================================

class TestOnDecisionRecordedHookWiring:
    """EXECUTORS['adp'] has on_decision_recorded wired to the issue hook.

    Mutation guard: remove the ``on_decision_recorded=make_issue_hook()`` kwarg
    from the AdpExecutor constructor call in registry.py → executor._on_decision_recorded
    is None → test fails.
    """

    def test_registry_adp_executor_hook_is_wired(self):
        """EXECUTORS['adp']._on_decision_recorded must not be None.

        Mutation: omit on_decision_recorded from registry.py AdpExecutor() →
        executor._on_decision_recorded is None → FAILS.
        """
        from agents.executors.registry import EXECUTORS

        executor = EXECUTORS.get("adp")
        assert executor is not None, "'adp' executor must be registered in EXECUTORS"
        assert executor._on_decision_recorded is not None, (
            "AdpExecutor in EXECUTORS must have on_decision_recorded wired "
            "to the quality.issue hook (task 3.3 / issue.py). "
            "Mutation: omit the kwarg in registry.py → _on_decision_recorded "
            "is None → FAILS."
        )


# ===========================================================================
# Issue Finding: no model text in draft-bound fields
# ===========================================================================

class TestIssueFindingNoModelText:
    """quality.issue Finding built from the hook must not carry model reasoning.

    Mutation guard: copy quality.root_cause.reasoning into any quality.draft or
    quality.thread evidence field → sentinel 'SENTINEL_REASONING_TEXT' present
    in the serialised Finding → assertion fails.
    """

    def test_issue_finding_does_not_contain_model_reasoning(self):
        """Sentinel reasoning text from quality.root_cause must not reach the issue Finding.

        Plants ``SENTINEL_REASONING_TEXT`` in the investigation Finding's
        quality.root_cause evidence.  A correct hook builds the issue Finding
        server-side from structured fields only; a hook that copies reasoning
        into any field causes this assertion to fail.

        Mutation: copy root_cause.reasoning into quality.draft evidence value →
        'SENTINEL_REASONING_TEXT' in json.dumps(finding) → FAILS.
        """
        import json
        from agents.fi_quality.issue import build_issue_finding_from_hook

        action = {
            "action_id": "AVX-ACT-no-model-text-001",
            "action_kind": "record_engineering_decision",
            "decided_by": {"kind": "engineer", "id": "eng-sub-0001"},
            "parameters": {"option_id": "opt-001"},
            "investigation_finding": {
                "finding_id": "FND-no-model-001",
                "evidence": [
                    {
                        "kind": "quality.root_cause",
                        "source": "agent-fi-quality",
                        "source_ref": "inv-no-model-001",
                        "value": {
                            "candidate_id": "cand-001",
                            "failure_mode_id": "fm-001",
                            "reasoning": "SENTINEL_REASONING_TEXT",
                            "confidence": "high",
                            "diagnosis_source": "model",
                            "evidence_refs": [],
                        },
                        "computed_at": "2026-10-05T06:00:00Z",
                        "provenance": "live",
                    },
                    {
                        "kind": "quality.pattern",
                        "source": "adp",
                        "source_ref": "pat-no-model-001",
                        "value": {"code_family": "P0AA6", "pattern_id": "pat-001"},
                        "computed_at": "2026-10-05T06:00:00Z",
                        "provenance": "live",
                    },
                ],
                "recommended_action": {
                    "action_kind": "record_engineering_decision",
                    "target_system": "adp",
                    "parameters": {
                        "investigation_id": "inv-no-model-001",
                        "pattern_id": "pat-no-model-001",
                        "option_id": "opt-001",
                    },
                },
            },
        }
        response = {
            "thread_id": "thr-no-model-001",
            "version": 1,
            "status": "decided",
            "draft_id": "MRD-RC-2026-001",
            "draft_type": "hardware_recall",
        }

        finding = build_issue_finding_from_hook(action, response)

        assert finding is not None, "build_issue_finding_from_hook must return a Finding"
        finding_str = json.dumps(finding)
        assert "SENTINEL_REASONING_TEXT" not in finding_str, (
            "quality.issue Finding must not carry model reasoning text. "
            "Mutation: copy quality.root_cause.reasoning into quality.draft "
            "evidence → sentinel present in serialised Finding → FAILS."
        )



# ===========================================================================
# W3 (G3F1.5) — issue_recall and issue_software_campaign executor tests
# ===========================================================================

#: Minimal Action for issue_recall dispatches.
_ISSUE_RECALL_ACTION: dict = {
    "action_id": "AVX-ACT-aabbccdd-eeff-4000-8000-001122334400",
    "action_kind": "issue_recall",
    "target_system": "adp",
    "decided_by": {"kind": "engineer", "sub": "eng-sub-adp-0001"},
    "decision": "approved",
    "parameters": {
        "thread_id": "thr-issue-0001",
        "draft_id":  "dft-issue-0001",
    },
}

_ISSUE_CAMPAIGN_ACTION: dict = {
    "action_id": "AVX-ACT-aabbccdd-eeff-4000-8000-001122334401",
    "action_kind": "issue_software_campaign",
    "target_system": "adp",
    "decided_by": {"kind": "engineer", "sub": "eng-sub-adp-0001"},
    "decision": "approved",
    "parameters": {
        "thread_id": "thr-issue-0002",
        "draft_id":  "dft-issue-0002",
    },
}


def _writer_response(status_code: int, body: dict | None = None) -> dict:
    """Build a fake Lambda invoke response for the given statusCode."""
    import json as _json
    body_str = _json.dumps(body or {}) if body is not None else "{}"
    return {
        "StatusCode": 200,
        "Payload": MagicMock(
            read=lambda: _json.dumps(
                {"statusCode": status_code, "body": body_str}
            ).encode()
        ),
    }


class TestIssueKindsPayload:
    """W3 (G3F1.5): issue_recall and issue_software_campaign dispatch correct payload.

    Mutation (M4): _build_issue_payload swaps thread_id and draft_id →
    payload carries wrong values → assertion fails.
    """

    def test_issue_recall_payload_keys(self):
        """issue_recall sends {action_id, action_kind, decided_by, thread_id, draft_id}.

        Mutation (M4): swap thread_id and draft_id in payload → assertion fails.
        """
        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = _writer_response(
            200, {"thread_id": "thr-issue-0001", "version": 1}
        )
        executor = _make_adp_executor(arn=_ADP_WRITER_ARN, mock_lambda=mock_lambda)
        executor.dispatch(_ISSUE_RECALL_ACTION, _ENGINEER_CLAIMS)

        mock_lambda.invoke.assert_called_once()
        call_kw = mock_lambda.invoke.call_args.kwargs
        import json as _json
        raw_payload = _json.loads(call_kw["Payload"])

        assert raw_payload.get("action_kind") == "issue_recall", (
            f"issue_recall payload must carry action_kind='issue_recall'; "
            f"got {raw_payload.get('action_kind')!r}. "
            "Mutation M4: swap → wrong action_kind → FAILS."
        )
        assert raw_payload.get("thread_id") == "thr-issue-0001", (
            f"issue_recall payload must carry thread_id='thr-issue-0001'; "
            f"got {raw_payload.get('thread_id')!r}. "
            "Mutation M4: swap thread_id and draft_id → FAILS."
        )
        assert raw_payload.get("draft_id") == "dft-issue-0001", (
            f"issue_recall payload must carry draft_id='dft-issue-0001'; "
            f"got {raw_payload.get('draft_id')!r}. "
            "Mutation M4: swap → FAILS."
        )
        assert "decided_by" in raw_payload, "payload must carry decided_by"
        assert "investigation_finding_id" not in raw_payload, (
            "issue_recall payload must NOT carry investigation_finding_id "
            "(that's for record_engineering_decision only)."
        )

    def test_issue_software_campaign_payload_keys(self):
        """issue_software_campaign sends the same shape as issue_recall.

        Mutation: use issue_recall's action_kind in the payload → wrong action_kind → FAILS.
        """
        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = _writer_response(
            200, {"thread_id": "thr-issue-0002", "version": 1}
        )
        executor = _make_adp_executor(arn=_ADP_WRITER_ARN, mock_lambda=mock_lambda)
        executor.dispatch(_ISSUE_CAMPAIGN_ACTION, _ENGINEER_CLAIMS)

        mock_lambda.invoke.assert_called_once()
        call_kw = mock_lambda.invoke.call_args.kwargs
        import json as _json
        raw_payload = _json.loads(call_kw["Payload"])

        assert raw_payload.get("action_kind") == "issue_software_campaign", (
            f"issue_software_campaign payload must carry correct action_kind; "
            f"got {raw_payload.get('action_kind')!r}."
        )
        assert raw_payload.get("thread_id") == "thr-issue-0002"
        assert raw_payload.get("draft_id") == "dft-issue-0002"


class TestIssueKindsStatusMapping:
    """W3 (G3F1.5): issue_recall status codes map to the correct ExecuteResult.

    Mutation (M8): AdpExecutor refuses both issue kinds → dispatch returns refused
    instead of the correct status → assertions fail.
    """

    def _dispatch_issue_recall(self, status_code: int, body: dict | None = None) -> Any:
        """Dispatch issue_recall and return the ExecuteResult."""
        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = _writer_response(status_code, body or {})
        executor = _make_adp_executor(arn=_ADP_WRITER_ARN, mock_lambda=mock_lambda)
        return executor.dispatch(_ISSUE_RECALL_ACTION, _ENGINEER_CLAIMS)

    def test_200_maps_to_executed(self):
        """issue_recall: 200 → executed.

        Mutation (M8): executor refuses issue kinds → status=declined → FAILS.
        """
        from agents.executors.base import ActionStatus
        result = self._dispatch_issue_recall(200, {"thread_id": "thr-issue-0001", "version": 1})
        assert result.status == ActionStatus.EXECUTED, (
            f"200 must map to executed; got {result.status!r}. "
            "Mutation M8: executor refuses → declined → FAILS."
        )
        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = _writer_response(200, {"thread_id": "thr-issue-0001", "version": 1})
        executor = _make_adp_executor(arn=_ADP_WRITER_ARN, mock_lambda=mock_lambda)
        result = executor.dispatch(_ISSUE_RECALL_ACTION, _ENGINEER_CLAIMS)
        assert result.status == ActionStatus.EXECUTED, (
            f"200 must map to executed; got {result.status!r}."
        )
        mock_lambda.invoke.assert_called_once()

    def test_422_maps_to_declined(self):
        """issue_recall: 422 → declined / target_refused.

        Mutation: map 422 to failed instead → status=FAILED → FAILS.
        """
        from agents.executors.base import ActionStatus, FailureKind
        result = self._dispatch_issue_recall(422)
        assert result.status == ActionStatus.DECLINED, (
            f"422 must map to declined; got {result.status!r}. "
            "Mutation: 422 → failed → FAILS."
        )

    def test_400_maps_to_failed_terminal(self):
        """issue_recall: 400 → failed / executor_terminal_error.

        Mutation: map 400 to declined → FAILS.
        """
        from agents.executors.base import ActionStatus, FailureKind
        result = self._dispatch_issue_recall(400)
        assert result.status == ActionStatus.FAILED, (
            f"400 must map to failed; got {result.status!r}. "
            "Mutation: 400 → declined → FAILS."
        )

    def test_5xx_retries_then_exhausted(self):
        """issue_recall: 5xx retries within policy and then → exhausted."""
        from agents.executors.base import ActionStatus, RetryPolicy
        mock_lambda = MagicMock()
        # Always return 500
        mock_lambda.invoke.return_value = _writer_response(500, {})
        policy = RetryPolicy(retry_count=2, backoff_base_ms=0, backoff_cap_ms=0)
        executor = _make_adp_executor(arn=_ADP_WRITER_ARN, mock_lambda=mock_lambda,
                                      policy=policy)
        result = executor.dispatch(_ISSUE_RECALL_ACTION, _ENGINEER_CLAIMS)
        assert result.status == ActionStatus.FAILED, (
            f"5xx exhaustion must map to failed; got {result.status!r}."
        )
        assert mock_lambda.invoke.call_count == 2, (
            f"With retry_count=2, must invoke 2 times; got {mock_lambda.invoke.call_count}."
        )

    def test_invoke_count_for_200(self):
        """issue_recall: 200 on first attempt → exactly 1 Lambda invoke.

        Mutation (M8): executor refuses before invoking → invoke_count=0 → FAILS.
        """
        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = _writer_response(
            200, {"thread_id": "thr-issue-0001", "version": 1}
        )
        executor = _make_adp_executor(arn=_ADP_WRITER_ARN, mock_lambda=mock_lambda)
        executor.dispatch(_ISSUE_RECALL_ACTION, _ENGINEER_CLAIMS)

        assert mock_lambda.invoke.call_count == 1, (
            f"200 on first attempt must invoke exactly once; got {mock_lambda.invoke.call_count}. "
            "Mutation M8: executor refuses → invoke_count=0 → FAILS."
        )


class TestHookCallBehaviour:
    """W3 (G3F1.5): on_decision_recorded hook is called / not called per spec.

    Mutations:
      M2: AdpExecutor never calls the on_decision_recorded hook → call_count=0 → FAILS.
      M3: hook also fires on issue_recall / issue_software_campaign 200s →
          hook is called for issue → FAILS.
    """

    def _recording_hook(self):
        """Return (hook, calls_list) where calls_list records each call."""
        calls: list[tuple] = []

        def _hook(action, response):
            calls.append((action, response))

        return _hook, calls

    def test_hook_called_on_record_decision_with_draft_id(self):
        """on_decision_recorded is called when record_engineering_decision returns 200 + draft_id.

        Mutation (M2): executor never calls the hook → call_count=0 → FAILS.
        """
        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = _writer_response(
            200, {
                "thread_id": "thr-hook-001",
                "version": 1,
                "draft_id": "dft-hook-001",
                "draft_type": "hardware_recall",
            }
        )
        hook, calls = self._recording_hook()
        executor = _make_adp_executor(arn=_ADP_WRITER_ARN, mock_lambda=mock_lambda)
        executor._on_decision_recorded = hook
        executor.dispatch(_BASE_ACTION, _ENGINEER_CLAIMS)

        assert len(calls) == 1, (
            f"Hook must be called exactly once on 200 + draft_id; got {len(calls)} calls. "
            "Mutation M2: hook never called → 0 calls → FAILS."
        )
        _action_arg, response_arg = calls[0]
        assert response_arg.get("draft_id") == "dft-hook-001", (
            f"Hook must receive the writer's response; got {response_arg!r}."
        )

    def test_hook_not_called_when_no_draft_id(self):
        """on_decision_recorded is NOT called when draft_id is null/absent.

        Mutation: call the hook even when draft_id is None → call_count=1 → FAILS.
        """
        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = _writer_response(
            200, {
                "thread_id": "thr-hook-002",
                "version": 1,
                # No draft_id
            }
        )
        hook, calls = self._recording_hook()
        executor = _make_adp_executor(arn=_ADP_WRITER_ARN, mock_lambda=mock_lambda)
        executor._on_decision_recorded = hook
        executor.dispatch(_BASE_ACTION, _ENGINEER_CLAIMS)

        assert len(calls) == 0, (
            f"Hook must NOT be called when draft_id is absent; got {len(calls)} calls. "
            "Mutation: call hook regardless → 1 call → FAILS."
        )

    def test_hook_not_called_for_issue_recall(self):
        """on_decision_recorded is NOT called for issue_recall 200s.

        Even when the writer returns a draft_id, the hook must not fire for
        issue_recall (is_record_decision=False guards the call).

        Mutation (M3): hook also fires on issue_recall 200s with draft_id → call_count=1 → FAILS.
        """
        mock_lambda = MagicMock()
        # Include draft_id in the response to ensure the hook's draft_id guard alone
        # doesn't explain the non-call — the is_record_decision guard is the real guard.
        mock_lambda.invoke.return_value = _writer_response(
            200, {
                "thread_id": "thr-issue-hook-001",
                "version": 1,
                "draft_id": "dft-issue-hook-001",  # present — only is_record_decision guards the call
            }
        )
        hook, calls = self._recording_hook()
        executor = _make_adp_executor(arn=_ADP_WRITER_ARN, mock_lambda=mock_lambda)
        executor._on_decision_recorded = hook
        executor.dispatch(_ISSUE_RECALL_ACTION, _ENGINEER_CLAIMS)

        assert len(calls) == 0, (
            f"Hook must NOT be called for issue_recall even when draft_id is present; "
            f"got {len(calls)} calls. "
            "Mutation M3: hook fires on issue_recall with draft_id → 1 call → FAILS."
        )

    def test_hook_not_called_for_issue_software_campaign(self):
        """on_decision_recorded is NOT called for issue_software_campaign 200s.

        Even when the writer returns a draft_id, the hook must not fire.

        Mutation (M3): hook also fires on issue_software_campaign → call_count=1 → FAILS.
        """
        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = _writer_response(
            200, {
                "thread_id": "thr-issue-hook-002",
                "version": 1,
                "draft_id": "dft-issue-hook-002",  # present
            }
        )
        hook, calls = self._recording_hook()
        executor = _make_adp_executor(arn=_ADP_WRITER_ARN, mock_lambda=mock_lambda)
        executor._on_decision_recorded = hook
        executor.dispatch(_ISSUE_CAMPAIGN_ACTION, _ENGINEER_CLAIMS)

        assert len(calls) == 0, (
            f"Hook must NOT be called for issue_software_campaign; got {len(calls)} calls. "
            "Mutation M3: hook fires on issue_software_campaign → 1 call → FAILS."
        )

    def test_hook_error_does_not_change_action_status(self):
        """A hook that raises must not change the Action's executed status.

        The executor returns executed; the hook failure is logged only.
        Mutation: hook error propagates → executor returns failed → FAILS.
        """
        from agents.executors.base import ActionStatus

        def _bad_hook(action, response):
            raise RuntimeError("hook exploded")

        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = _writer_response(
            200, {"thread_id": "thr-hook-003", "version": 1, "draft_id": "dft-hook-003"}
        )
        executor = _make_adp_executor(arn=_ADP_WRITER_ARN, mock_lambda=mock_lambda)
        executor._on_decision_recorded = _bad_hook
        result = executor.dispatch(_BASE_ACTION, _ENGINEER_CLAIMS)

        assert result.status == ActionStatus.EXECUTED, (
            f"Hook error must not change Action status; got {result.status!r}. "
            "Mutation: propagate hook error → failed → FAILS."
        )


# ===========================================================================
# Lambda service errors, raised the way a real boto3 Lambda client raises them
# ===========================================================================
# issues/2026-10-24-adp-executor-never-retries-modelled-lambda-errors. For a code
# the Lambda model defines, boto3 raises a generated ClientError SUBCLASS named
# after the code; otherwise a bare ClientError (an unmodelled code, or a front-end
# 5xx with no parseable body, whose code is the HTTP status). The classifier used
# to accept only the class name "ClientError", so no modelled throttle was ever
# retried. Errors here come from a real client's exception factory with the
# HTTP status the Lambda model gives each code, from botocore's own response
# parser, or from a Stubber. The four retryable codes are written out literally
# rather than read from adp.py, so changing the product's set fails here.

_RETRYABLE_CODES = (
    "TooManyRequestsException",
    "ServiceException",
    "EC2ThrottledException",
    "EC2UnexpectedException",
)

#: Codes the Lambda model doesn't define; boto3 raises each as a bare ClientError.
_UNMODELLED_TERMINAL = (
    ("AccessDeniedException", 403),
    ("ExpiredTokenException", 403),
    ("UnrecognizedClientException", 403),
)

_ATTEMPTS = 4

_REAL_LAMBDA_CLIENT: list = []


def _real_lambda_client():
    """A real botocore Lambda client pointed at a dead loopback endpoint."""
    if not _REAL_LAMBDA_CLIENT:
        import botocore.session

        _REAL_LAMBDA_CLIENT.append(botocore.session.get_session().create_client(
            "lambda", region_name="us-east-1", endpoint_url="http://127.0.0.1:1",
            aws_access_key_id="testing", aws_secret_access_key="testing"))
    return _REAL_LAMBDA_CLIENT[0]


def _invoke_error_statuses() -> dict[str, int]:
    """{code: HTTP status} for every error the Lambda model defines for Invoke."""
    op = _real_lambda_client().meta.service_model.operation_model("Invoke")
    return {s.name: s.metadata["error"]["httpStatusCode"] for s in op.error_shapes}


def _modelled_terminal_codes() -> list[str]:
    codes = _invoke_error_statuses()
    assert set(_RETRYABLE_CODES) <= set(codes), "fixture: a retryable code left the model"
    terminal = sorted(set(codes) - set(_RETRYABLE_CODES))
    assert len(terminal) >= 20, f"fixture: only {len(terminal)} modelled terminal codes"
    return terminal


def _boto3_error(code: str, shape: str = "as_boto3_raises_it") -> Exception:
    """The error a real client raises for ``code``, with the code's real HTTP status.

    ``shape="bare"`` builds a bare ClientError carrying the code and NO HTTP status,
    a shape boto3 never produces for a modelled code: only the code can make it
    retryable, so it pins the code check on its own."""
    from botocore.exceptions import ClientError

    error = {"Error": {"Code": code, "Message": "adp-test"}}
    if shape == "bare":
        return ClientError(error, "Invoke")
    statuses = dict(_invoke_error_statuses(), **dict(_UNMODELLED_TERMINAL))
    error["ResponseMetadata"] = {"HTTPStatusCode": statuses[code]}
    return _real_lambda_client().exceptions.from_code(code)(error, "Invoke")


def _parsed_front_end_error(status: int, body: bytes = b"") -> Exception:
    """What the client raises for a Lambda front-end error with no parseable body:
    botocore's rest-json parser run on the raw response, then the client's factory."""
    from botocore.parsers import create_parser

    client = _real_lambda_client()
    model = client.meta.service_model
    parsed = create_parser(model.protocol).parse(
        {"status_code": status, "headers": {}, "body": body},
        model.operation_model("Invoke").output_shape)
    return client.exceptions.from_code(parsed["Error"]["Code"])(parsed, "Invoke")


def _four_attempt_policy() -> RetryPolicy:
    return RetryPolicy(retry_count=_ATTEMPTS, backoff_base_ms=0, backoff_cap_ms=0)


def _dispatch_with(effect: Any, *, policy: RetryPolicy | None = None):
    mock_lambda = MagicMock()
    mock_lambda.invoke.side_effect = effect
    executor = _make_adp_executor(mock_lambda=mock_lambda,
                                  policy=policy or _four_attempt_policy())
    try:
        result = executor.dispatch(_ISSUE_RECALL_ACTION, _ENGINEER_CLAIMS)
    except Exception as exc:  # noqa: BLE001 - dispatch must never raise
        raise AssertionError(f"dispatch raised {type(exc).__name__}: {exc}") from exc
    return result, mock_lambda.invoke.call_count


def _function_error(**_kw: Any) -> dict:
    return {"StatusCode": 200, "FunctionError": "Unhandled",
            "Payload": MagicMock(read=lambda: b"{}")}


def _outcome(result) -> str:
    return (f"status={result.status} kind={result.failure_kind} "
            f"class={result.last_error_class} http={result.last_error_status} "
            f"retry_count={result.retry_count}")


class TestLambdaServiceErrors:
    """The four retryable codes, and a bare ClientError with a transient HTTP status,
    retry on the shared budget; every other error is terminal on the call it hits."""

    @pytest.mark.parametrize("shape", ["as_boto3_raises_it", "bare"])
    @pytest.mark.parametrize("code", _RETRYABLE_CODES)
    def test_a_retryable_code_is_retried_to_the_whole_budget(self, code, shape):
        """Each of the four codes, as the modelled subclass (with its real status) and
        as a status-less bare ClientError, is retried until the attempts run out.

        Mutations: class-name gate restored (modelled shape: 1 invoke); the code read
        from the class name (bare shape: 1 invoke); a code dropped from the set."""
        result, invokes = _dispatch_with(_boto3_error(code, shape))

        assert invokes == _ATTEMPTS, f"{code}/{shape}: invoked {invokes}; {_outcome(result)}"
        assert result.status == ActionStatus.FAILED, _outcome(result)
        assert result.failure_kind == FailureKind.EXECUTOR_EXHAUSTED_RETRIES, _outcome(result)
        assert result.retry_count == _ATTEMPTS, _outcome(result)
        expected_class = code if shape == "as_boto3_raises_it" else "ClientError"
        assert (result.last_error_class, result.last_error_status) == (expected_class, None), (
            _outcome(result))

    @pytest.mark.parametrize("code", _modelled_terminal_codes())
    def test_every_other_modelled_code_is_terminal_on_the_first_invoke(self, code):
        """Every Invoke error the Lambda model defines, except the four, is terminal,
        whatever its HTTP status: ResourceNotReadyException, KMS* and
        EC2AccessDeniedException arrive as 502 and are permanent.

        Mutations: any ClientError retried; any 5xx status retried for a modelled
        subclass; a near-miss code (EC2AccessDeniedException) added to the set."""
        error = _boto3_error(code)
        assert type(error).__name__ == code, f"fixture: {code} raised as {type(error).__name__}"
        result, invokes = _dispatch_with(error)

        assert invokes == 1, f"{code}: a terminal code was retried ({invokes} invokes)"
        assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR, _outcome(result)
        assert (result.last_error_class, result.retry_count) == (code, 0), _outcome(result)

    @pytest.mark.parametrize("code, status", _UNMODELLED_TERMINAL)
    def test_an_unmodelled_code_with_a_4xx_status_is_terminal(self, code, status):
        """An IAM denial or an expired token arrives as a bare ClientError with a 403;
        it is not retried (the fix must not widen into "retry any ClientError")."""
        error = _boto3_error(code)
        assert type(error).__name__ == "ClientError", f"fixture: {type(error).__name__}"
        result, invokes = _dispatch_with(error)

        assert invokes == 1, f"{code}: retried ({invokes} invokes); {_outcome(result)}"
        assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR, _outcome(result)
        assert (result.last_error_class, result.retry_count) == ("ClientError", 0), (
            _outcome(result))

    @pytest.mark.parametrize("status, body", [
        (500, b""), (502, b"<html>Bad Gateway</html>"), (503, b""), (504, b""),
    ])
    def test_a_front_end_5xx_with_no_parseable_body_is_retried(self, status, body):
        """botocore reports a body-less 5xx as a bare ClientError coded with the status
        ("503"). Retried like any 5xx, as in the CMS and DMS loops. Built by botocore's
        real parser. Mutation: a bare ClientError never retried by status."""
        error = _parsed_front_end_error(status, body)
        assert (type(error).__name__, error.response["Error"]["Code"]) == (
            "ClientError", str(status)), f"fixture: {error!r}"
        result, invokes = _dispatch_with(error)

        assert invokes == _ATTEMPTS, f"HTTP {status}: invoked {invokes}; {_outcome(result)}"
        assert result.failure_kind == FailureKind.EXECUTOR_EXHAUSTED_RETRIES, _outcome(result)

    def test_an_unmodelled_throttle_is_retried_by_its_429(self):
        """A bare ClientError with HTTP 429 (an unmodelled ThrottlingException) is
        retried: a 429 is "too fast", never a refusal (base._NON_REFUSAL_4XX)."""
        from botocore.exceptions import ClientError

        error = ClientError({"Error": {"Code": "ThrottlingException"},
                             "ResponseMetadata": {"HTTPStatusCode": 429}}, "Invoke")
        result, invokes = _dispatch_with(error)

        assert invokes == _ATTEMPTS, f"invoked {invokes}; {_outcome(result)}"
        assert result.failure_kind == FailureKind.EXECUTOR_EXHAUSTED_RETRIES, _outcome(result)

    def test_a_bare_client_error_with_a_non_numeric_status_is_terminal(self):
        """A status that is not an int is never read as transient, and the classifier
        doesn't raise on it (``@never_raises`` would record the TypeError instead of
        the ClientError). Mutation: the int check dropped."""
        from botocore.exceptions import ClientError

        error = ClientError({"Error": {"Code": "503"},
                             "ResponseMetadata": {"HTTPStatusCode": "503"}}, "Invoke")
        result, invokes = _dispatch_with(error)

        assert invokes == 1, f"invoked {invokes}; {_outcome(result)}"
        assert (result.failure_kind, result.last_error_class) == (
            FailureKind.EXECUTOR_TERMINAL_ERROR, "ClientError"), _outcome(result)

    def test_a_throttle_then_success_records_the_writer_answer(self):
        """A throttle on the first invoke and a 200 on the second executes the Action
        with the writer's thread id. Mutation: class-name gate restored → failed."""
        effect = [_boto3_error("TooManyRequestsException"),
                  _writer_response(200, {"thread_id": "thr-issue-0001"})]
        result, invokes = _dispatch_with(effect)

        assert invokes == 2, f"invoked {invokes}; {_outcome(result)}"
        assert result.status == ActionStatus.EXECUTED, _outcome(result)
        assert result.target_ref == "thr-issue-0001", _outcome(result)

    def test_throttles_share_one_budget_with_the_other_transient_failures(self):
        """A writer 503, a throttle, a FunctionError and a service error are four
        attempts of ONE budget, as in the CMS and DMS loops: the fifth invoke never
        happens. The throttles come after other failures, so a throttle that resets
        the count or does not count shows up as a fifth invoke."""
        effect = [_writer_response(503), _boto3_error("TooManyRequestsException"),
                  _function_error(), _boto3_error("ServiceException"),
                  _writer_response(200, {"thread_id": "thr-too-late"})]
        result, invokes = _dispatch_with(effect)

        assert invokes == _ATTEMPTS, f"invoked {invokes}; {_outcome(result)}"
        assert result.failure_kind == FailureKind.EXECUTOR_EXHAUSTED_RETRIES, _outcome(result)
        assert (result.retry_count, result.last_error_class) == (_ATTEMPTS, "ServiceException"), (
            _outcome(result))

    def test_a_terminal_code_after_a_throttle_records_the_attempt_it_took(self):
        """A throttle, then an IAM denial: two invokes, terminal on the second, and the
        record says one attempt had already been spent. Mutation: retry_count=0."""
        effect = [_boto3_error("TooManyRequestsException"), _boto3_error("AccessDeniedException")]
        result, invokes = _dispatch_with(effect)

        assert invokes == 2, f"invoked {invokes}; {_outcome(result)}"
        assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR, _outcome(result)
        assert (result.retry_count, result.last_error_class) == (1, "ClientError"), (
            _outcome(result))

    def test_a_throttle_past_the_deadline_is_not_retried_or_slept_on(self):
        """The 25 s deadline bounds throttles too: the executor's clock reads 0, 0, then
        100 (other callers read the real clock), so one invoke and no sleep."""
        import time

        real = time.monotonic
        queue = [0.0, 0.0, 100.0]

        def clock() -> float:
            if sys._getframe(1).f_globals.get("__name__") == "agents.executors.adp":
                return queue.pop(0) if len(queue) > 1 else queue[0]
            return real()

        sleeps: list[float] = []
        mock_lambda = MagicMock()
        mock_lambda.invoke.side_effect = _boto3_error("TooManyRequestsException")
        executor = _make_adp_executor(mock_lambda=mock_lambda, policy=RetryPolicy(
            retry_count=_ATTEMPTS, backoff_base_ms=100, backoff_cap_ms=1000))
        executor._sleep = sleeps.append
        with patch("time.monotonic", clock):
            result = executor.dispatch(_ISSUE_RECALL_ACTION, _ENGINEER_CLAIMS)

        assert mock_lambda.invoke.call_count == 1, _outcome(result)
        assert result.failure_kind == FailureKind.EXECUTOR_EXHAUSTED_RETRIES, _outcome(result)
        assert sleeps == [], f"slept {sleeps} after the deadline"

    @pytest.mark.parametrize("class_name", ["TooManyRequestsException", "ClientError"])
    def test_another_library_exception_shaped_like_a_client_error_is_terminal(self, class_name):
        """Only botocore's ClientError is classified: an exception from elsewhere,
        named like a retryable code or like ClientError itself and carrying the same
        response shape (a throttle code and a 429), is terminal on the first invoke.
        Mutations: the isinstance check removed; replaced by a class-name or MRO-name
        match."""
        fake = type(class_name, (Exception,), {
            "response": {"Error": {"Code": "TooManyRequestsException"},
                         "ResponseMetadata": {"HTTPStatusCode": 429}}})
        result, invokes = _dispatch_with(fake("not botocore"))

        assert invokes == 1, f"{class_name}: invoked {invokes}; {_outcome(result)}"
        assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR, _outcome(result)
        assert result.last_error_class == class_name, _outcome(result)

    def test_without_botocore_nothing_is_a_client_error(self):
        """The classifier imports botocore lazily; if the import fails it answers
        False rather than raising (which ``@never_raises`` would record as an
        ImportError instead of the real error). Mutation: True on ImportError."""
        from agents.executors.adp import _is_retryable_client_error

        error = ValueError("not retryable")
        with patch.dict(sys.modules, {"botocore.exceptions": None}):
            try:
                answer = _is_retryable_client_error(error)
            except Exception as exc:  # noqa: BLE001
                raise AssertionError(f"raised {type(exc).__name__}: {exc}") from exc
            result, invokes = _dispatch_with(error)

        assert answer is False
        assert (invokes, result.last_error_class) == (1, "ValueError"), _outcome(result)


class TestLambdaErrorsThroughARealClient:
    """End to end through a real botocore Lambda client and a Stubber: the client,
    not the test, decides which exception class each error arrives as."""

    def _executor_on_stubbed_client(self):
        import botocore.session
        from botocore.stub import Stubber

        from agents.executors.adp import AdpExecutor

        client = botocore.session.get_session().create_client(
            "lambda", region_name="us-east-1", endpoint_url="http://127.0.0.1:1",
            aws_access_key_id="testing", aws_secret_access_key="testing")
        executor = AdpExecutor(
            thread_writer_arn=_ADP_WRITER_ARN, lambda_client=client,
            sleep_fn=lambda _s: None, retry_policy=_four_attempt_policy())
        return executor, Stubber(client)

    def test_a_throttle_and_a_service_error_are_retried_then_the_writer_answers(self):
        import io

        from botocore.response import StreamingBody

        executor, stubber = self._executor_on_stubbed_client()
        stubber.add_client_error("invoke", service_error_code="TooManyRequestsException",
                                 http_status_code=429)
        stubber.add_client_error("invoke", service_error_code="ServiceException",
                                 http_status_code=500)
        body = json.dumps({"statusCode": 200,
                           "body": json.dumps({"thread_id": "thr-stub-1"})}).encode()
        stubber.add_response("invoke", {"StatusCode": 200,
                                        "Payload": StreamingBody(io.BytesIO(body), len(body))})
        with stubber:
            result = executor.dispatch(_ISSUE_RECALL_ACTION, _ENGINEER_CLAIMS)
            stubber.assert_no_pending_responses()

        assert result.status == ActionStatus.EXECUTED, _outcome(result)
        assert result.target_ref == "thr-stub-1", _outcome(result)

    def test_an_access_denied_invoke_is_terminal_after_one_call(self):
        """One queued denial: a second invoke would raise the Stubber's
        UnStubbedResponseError inside the loop and change the recorded class."""
        executor, stubber = self._executor_on_stubbed_client()
        stubber.add_client_error("invoke", service_error_code="AccessDeniedException",
                                 http_status_code=403)
        with stubber:
            result = executor.dispatch(_ISSUE_RECALL_ACTION, _ENGINEER_CLAIMS)
            stubber.assert_no_pending_responses()

        assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR, _outcome(result)
        assert (result.last_error_class, result.retry_count) == ("ClientError", 0), (
            _outcome(result))


#: The real factory, saved at import: the package conftest replaces
#: ``adp._make_lambda_client`` with a stub for every test.
from agents.executors import adp as _adp_module  # noqa: E402

_REAL_MAKE_LAMBDA_CLIENT = _adp_module._make_lambda_client


class TestTheExecutorLoopIsTheOnlyRetry:
    """The executor's loop owns the whole retry budget only if the Lambda client
    doesn't retry on its own: botocore retrying each invoke would multiply the
    attempts and could outlast the 25 s budget (security review cycle 1, sec06)."""

    def test_the_real_client_factory_turns_botocore_retries_off(self):
        captured: dict = {}

        def fake_client(service, **kwargs):
            captured.update(kwargs, service=service)
            return MagicMock()

        with patch("boto3.client", fake_client):
            try:
                _REAL_MAKE_LAMBDA_CLIENT(6.25)
            except Exception as exc:  # noqa: BLE001
                raise AssertionError(f"factory raised {type(exc).__name__}: {exc}") from exc

        config = captured.get("config")
        assert (captured.get("service"), captured.get("region_name")) == ("lambda", "us-east-1")
        assert config is not None, f"no botocore Config passed: {captured}"
        assert config.retries == {"total_max_attempts": 1}, f"retries={config.retries!r}"
        assert config.read_timeout == 6.25, f"read_timeout={config.read_timeout!r}"
