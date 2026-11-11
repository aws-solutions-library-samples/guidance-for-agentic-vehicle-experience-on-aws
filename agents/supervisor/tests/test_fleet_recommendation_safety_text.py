"""Tests for fleet_recommendation D18: safety_text verbatim on recall Findings.

Spec: `.kiro/specs/2026-10-05-fi-recall-warranty-stage-c/decisions.md` call 58.

Invariants tested:
- safety_text equals disclosure_txt byte-for-byte for fleet.recall.vehicle
  (including '\\n\\n' separators, trailing space, trailing newline).
- absent for cost, warranty, and other kinds even when disclosure_txt is set.
- absent when fleet standing fails (available=False path).
- absent when the Finding read fails (available=False path).
- absent when disclosure_txt is empty string.
- No bedrock client created (pure DynamoDB read — no model call).
- FLEET_MANAGER_PROMPT contains the safety_text verbatim-reading rule.

Mutation record (all four mutants caught on their own assertion):
  See /tmp/2026-10-05-fi-recall-warranty-stage-c-g4-s5/note.md
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import agents.supervisor.tools.fleet_recommendation as fr_mod
from agents.supervisor.tools.fleet_recommendation import make_fleet_recommendation_tool
from agents.supervisor.persona_definitions import FLEET_MANAGER_PROMPT

# ---------------------------------------------------------------------------
# Re-use fixtures from the existing test module (decisions.md call 58: "reuse
# the existing fixtures' style").
# ---------------------------------------------------------------------------

from agents.supervisor.tests.test_fleet_recommendation import (  # noqa: E402
    _make_jwt,
    _make_finding,
    _make_action,
    _ddb_stubs,
)

# Recall disclosure text that exercises the byte-for-byte contract:
# contains '\n\n' paragraph break and a trailing newline.
_RECALL_DISCLOSURE = (
    "Do not drive this vehicle until the recall repair has been completed.\n\n"
    "Park outside and away from structures. Contact your dealer immediately.\n"
)

_RECALL_FINDING_ID = "FIND-recall01-1234-5678-abcd-ef1234567890"


def _make_recall_finding(**kw) -> dict:
    """A _make_finding with finding_kind='fleet.recall.vehicle' and disclosure_txt set."""
    f = _make_finding(finding_id=_RECALL_FINDING_ID, **kw)
    f["finding_kind"] = "fleet.recall.vehicle"
    f["disclosure_txt"] = _RECALL_DISCLOSURE
    return f


def _call_with_finding(finding: dict, *, groups: list[str] | None = None) -> dict:
    jwt_str, env = _make_jwt(
        groups=groups if groups is not None else ["fleet-operator"],
        fleet_ids=["FLEET-001"],
        trust_fleet_ids=True,
    )
    ctx = SimpleNamespace(jwt=jwt_str)
    cfg = SimpleNamespace()
    resource_mock = _ddb_stubs(finding)
    with patch.dict("os.environ", env), \
         patch.object(fr_mod.boto3, "resource", return_value=resource_mock):
        tool = make_fleet_recommendation_tool(ctx, cfg)
        return tool(finding["finding_id"])


# ===========================================================================
# safety_text presence — recall Finding with rich disclosure text
# ===========================================================================

class TestSafetyTextRecallFinding:
    """safety_text equals disclosure_txt byte-for-byte on fleet.recall.vehicle."""

    def test_safety_text_equals_disclosure_txt_byte_for_byte(self) -> None:
        """Preserves '\\n\\n', trailing space, trailing newline — no strip."""
        result = _call_with_finding(_make_recall_finding())
        assert result["available"] is True
        assert "safety_text" in result, "safety_text must be present for recall Findings"
        assert result["safety_text"] == _RECALL_DISCLOSURE, (
            "safety_text must equal disclosure_txt byte for byte — "
            "no strip, no normalisation"
        )
        # Verify the trailing newline is preserved
        assert result["safety_text"].endswith("\n"), "trailing newline must be preserved"

    def test_safety_text_is_top_level(self) -> None:
        """safety_text must be at the top level of the result, not nested."""
        result = _call_with_finding(_make_recall_finding())
        assert "safety_text" in result
        assert "safety_text" not in result.get("finding", {})


# ===========================================================================
# safety_text absent for non-recall kinds
# ===========================================================================

class TestSafetyTextAbsentForOtherKinds:
    """safety_text must be absent for every non-recall finding_kind."""

    def test_absent_for_cost_kind(self) -> None:
        f = _make_finding()  # default: fleet.cost.anomaly
        f["disclosure_txt"] = "Some cost disclosure"
        result = _call_with_finding(f)
        assert result["available"] is True
        assert "safety_text" not in result, (
            "safety_text must be absent for cost findings even when disclosure_txt is set"
        )

    def test_absent_for_warranty_kind(self) -> None:
        f = _make_finding()
        f["finding_kind"] = "fleet.warranty.claim_candidate"
        f["disclosure_txt"] = "Some warranty disclosure"
        result = _call_with_finding(f)
        assert result["available"] is True
        assert "safety_text" not in result, (
            "safety_text must be absent for warranty findings"
        )

    def test_absent_for_other_kind_with_disclosure(self) -> None:
        f = _make_finding()
        f["finding_kind"] = "fleet.health.alert"
        f["disclosure_txt"] = "Some health disclosure"
        result = _call_with_finding(f)
        assert result["available"] is True
        assert "safety_text" not in result


# ===========================================================================
# safety_text absent on unavailable paths
# ===========================================================================

class TestSafetyTextAbsentOnUnavailablePaths:
    """safety_text must never appear when available=False."""

    def test_absent_when_standing_fails(self) -> None:
        """Fleet standing check fails → available=False, no safety_text."""
        f = _make_recall_finding()
        jwt_str, env = _make_jwt(
            groups=["fleet-operator"],
            fleet_ids=["FLEET-999"],  # wrong fleet
            trust_fleet_ids=True,
        )
        ctx = SimpleNamespace(jwt=jwt_str)
        cfg = SimpleNamespace()
        resource_mock = _ddb_stubs(f)
        with patch.dict("os.environ", env), \
             patch.object(fr_mod.boto3, "resource", return_value=resource_mock):
            tool = make_fleet_recommendation_tool(ctx, cfg)
            result = tool(_RECALL_FINDING_ID)
        assert result["available"] is False
        assert "safety_text" not in result

    def test_absent_when_finding_read_fails(self) -> None:
        """DynamoDB error → available=False, no safety_text."""
        jwt_str, env = _make_jwt(
            groups=["fleet-operator"], fleet_ids=["FLEET-001"], trust_fleet_ids=True
        )
        ctx = SimpleNamespace(jwt=jwt_str)
        cfg = SimpleNamespace()
        resource_mock = _ddb_stubs(
            finding=None, finding_error=RuntimeError("DDB unavailable")
        )
        with patch.dict("os.environ", env), \
             patch.object(fr_mod.boto3, "resource", return_value=resource_mock):
            tool = make_fleet_recommendation_tool(ctx, cfg)
            result = tool(_RECALL_FINDING_ID)
        assert result["available"] is False
        assert "safety_text" not in result

    def test_absent_when_disclosure_txt_is_empty(self) -> None:
        """Empty string disclosure_txt → no safety_text (absent, not '')."""
        f = _make_recall_finding()
        f["disclosure_txt"] = ""
        result = _call_with_finding(f)
        assert result["available"] is True
        assert "safety_text" not in result, (
            "Empty disclosure_txt must not produce a safety_text key"
        )


# ===========================================================================
# No bedrock client created
# ===========================================================================

class TestNoBedrockClient:
    """The tool must never create a Bedrock client — pure DynamoDB read."""

    def test_no_bedrock_client_on_recall_path(self) -> None:
        """Patch boto3.client; assert no service name starting with 'bedrock'."""
        recorded: list[str] = []
        orig_client = fr_mod.boto3.client

        def _recording_client(service_name: str, **_kw) -> MagicMock:
            recorded.append(service_name)
            return MagicMock()

        jwt_str, env = _make_jwt(
            groups=["fleet-operator"], fleet_ids=["FLEET-001"], trust_fleet_ids=True
        )
        ctx = SimpleNamespace(jwt=jwt_str)
        cfg = SimpleNamespace()
        resource_mock = _ddb_stubs(_make_recall_finding())

        with patch.dict("os.environ", env), \
             patch.object(fr_mod.boto3, "resource", return_value=resource_mock), \
             patch.object(fr_mod.boto3, "client", side_effect=_recording_client):
            tool = make_fleet_recommendation_tool(ctx, cfg)
            tool(_RECALL_FINDING_ID)

        bedrock_calls = [s for s in recorded if s.startswith("bedrock")]
        assert bedrock_calls == [], (
            f"fleet_recommendation must not create a Bedrock client; got: {bedrock_calls}"
        )


# ===========================================================================
# Prompt content: safety_text rule
# ===========================================================================

class TestFleetManagerPromptSafetyTextRule:
    """FLEET_MANAGER_PROMPT must contain the safety_text verbatim-reading rule."""

    def test_prompt_contains_safety_text_key(self) -> None:
        assert "safety_text" in FLEET_MANAGER_PROMPT, (
            "FLEET_MANAGER_PROMPT must reference 'safety_text' to direct the "
            "assistant to read it. See spec D18 and decisions.md call 58."
        )

    def test_prompt_contains_word_for_word(self) -> None:
        assert "word for word" in FLEET_MANAGER_PROMPT, (
            "FLEET_MANAGER_PROMPT must say 'word for word' to enforce verbatim "
            "recall safety disclosure. See spec D18 and decisions.md call 58."
        )
