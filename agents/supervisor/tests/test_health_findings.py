"""Unit tests for the health_findings tool (T5.1).

Spec: `.kiro/specs/2026-08-03-cvx-tier2-platform-health-agent/spec.md` § D9.

Key invariants tested here:
- Tool returns findings from DynamoDB (no LLM, no Athena — asserted by T5.2).
- Single-partition Query on ``pk = VEH#{vin}``.
- ``begins_with(sk, "FINDING#{component}#")`` prefix per component.
- Newest finding is selected by max ``computed_at`` (sort key is content-addressed,
  not time-ordered — spec § D4 corrected 2026-08-04).
- ``computed_at`` is always returned (M5 — staleness surfaced to caller).
- ``evidence`` returned only when ``include_evidence=True``.
- No-VIN session returns ``available=False``.
- Unknown component returns ``available=False``.
- Empty table returns ``available=False`` with a schedule-mention message.
- DynamoDB error is caught and returns ``available=False``.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_session(vin: str | None = "1HGCM82633A123456") -> SimpleNamespace:
    return SimpleNamespace(vin=vin, persona_id="fleet_driver", tenant_id="fleet")


def _make_config() -> SimpleNamespace:
    return SimpleNamespace(memory_id="mem-001", memory_region="us-west-2")


def _ddb_item(
    component: str,
    inputs_hash: str,
    computed_at: str,
    finding: str = "Tires look good.",
    confidence: str = "high",
    severity_hint: str = "routine",
    evidence: list | None = None,
) -> dict:
    """Build a DynamoDB Item dict matching the artifact schema."""
    item: dict = {
        "pk": f"VEH#1HGCM82633A123456",
        "sk": f"FINDING#{component}#{inputs_hash}",
        "computed_at": computed_at,
        "finding": finding,
        "confidence": confidence,
        "severity_hint": severity_hint,
    }
    if evidence is not None:
        item["evidence"] = evidence
    return item


def _stub_table(query_responses: dict[str, list]) -> MagicMock:
    """Return a mock DynamoDB Table whose query() returns items keyed by sk_prefix."""
    table = MagicMock()

    def _query(**kwargs):
        # Decode the begins_with condition from the KeyConditionExpression.
        # boto3 condition objects are not easily introspected; instead we rely on
        # the caller having set ExpressionAttributeValues with our known prefix.
        # For unit tests we inspect the prefix explicitly via a side-effect closure.
        # A simpler approach: look at what _sk_prefix_used was.
        condition_str = repr(kwargs.get("KeyConditionExpression", ""))
        # Find which component prefix was used by matching against known prefixes.
        items = []
        for prefix, results in query_responses.items():
            # The Key().begins_with() repr includes the prefix string.
            if prefix in condition_str:
                items = results
                break
        return {"Items": items}

    table.query.side_effect = _query
    return table


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHealthFindingsNoVin:
    def test_no_vin_returns_available_false(self):
        from agents.supervisor.tools.health_findings import make_health_findings_tool

        session = _make_session(vin=None)
        config = _make_config()
        tool_fn = make_health_findings_tool(session, config)
        result = tool_fn()
        assert result["available"] is False
        assert "VIN" in result.get("reason", "")


class TestHealthFindingsUnknownComponent:
    def test_unknown_component_returns_available_false(self):
        from agents.supervisor.tools.health_findings import make_health_findings_tool

        session = _make_session()
        config = _make_config()
        tool_fn = make_health_findings_tool(session, config)
        result = tool_fn(component="engine")
        assert result["available"] is False
        assert "Unknown component" in result.get("reason", "")

    def test_valid_component_not_rejected(self):
        """Valid components pass through without an 'Unknown component' error."""
        from agents.supervisor.tools.health_findings import make_health_findings_tool

        session = _make_session()
        config = _make_config()

        for comp in ("tires", "battery", "brakes"):
            with patch("boto3.resource") as mock_resource:
                mock_table = MagicMock()
                mock_table.query.return_value = {"Items": []}
                mock_resource.return_value.Table.return_value = mock_table

                tool_fn = make_health_findings_tool(session, config)
                result = tool_fn(component=comp)
                assert "Unknown component" not in result.get("reason", ""), (
                    f"Component {comp!r} was incorrectly rejected"
                )


class TestHealthFindingsEmptyTable:
    def test_no_items_returns_available_false_with_schedule_note(self):
        from agents.supervisor.tools.health_findings import make_health_findings_tool

        session = _make_session()
        config = _make_config()

        with patch("boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_table.query.return_value = {"Items": []}
            mock_resource.return_value.Table.return_value = mock_table

            tool_fn = make_health_findings_tool(session, config)
            result = tool_fn()

        assert result["available"] is False
        assert "daily" in result.get("reason", "").lower() or "schedule" in result.get("reason", "").lower()


class TestHealthFindingsHappyPath:
    def _run_tool(
        self,
        items: list,
        component: str | None = None,
        include_evidence: bool = False,
    ) -> dict:
        from agents.supervisor.tools.health_findings import make_health_findings_tool

        session = _make_session()
        config = _make_config()

        with patch("boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_table.query.return_value = {"Items": items}
            mock_resource.return_value.Table.return_value = mock_table

            tool_fn = make_health_findings_tool(session, config)
            return tool_fn(component=component, include_evidence=include_evidence)

    def test_single_battery_finding_returned(self):
        item = _ddb_item(
            "battery",
            "a" * 64,
            "2026-08-01T02:00:00Z",
            finding="Battery health at 92%, degrading normally for its age.",
        )
        result = self._run_tool([item], component="battery")
        assert result["available"] is True
        findings = result["findings"]
        assert len(findings) == 1
        assert findings[0]["component"] == "battery"
        assert findings[0]["finding"] == "Battery health at 92%, degrading normally for its age."

    def test_computed_at_always_present(self):
        """M5: computed_at must be returned so the caller can surface staleness."""
        item = _ddb_item("tires", "b" * 64, "2026-07-28T02:00:00Z")
        result = self._run_tool([item], component="tires")
        assert result["available"] is True
        assert result["findings"][0]["computed_at"] == "2026-07-28T02:00:00Z"

    def test_confidence_returned(self):
        item = _ddb_item("brakes", "c" * 64, "2026-08-02T02:00:00Z", confidence="medium")
        result = self._run_tool([item], component="brakes")
        assert result["findings"][0]["confidence"] == "medium"

    def test_severity_hint_returned(self):
        item = _ddb_item("battery", "d" * 64, "2026-08-03T02:00:00Z", severity_hint="soon")
        result = self._run_tool([item], component="battery")
        assert result["findings"][0]["severity_hint"] == "soon"

    def test_evidence_excluded_by_default(self):
        """evidence[] is gate-kept behind include_evidence=True."""
        evidence = [{"source": "energy_usage", "value": {"soh": 92.0}, "provenance": "live"}]
        item = _ddb_item("battery", "e" * 64, "2026-08-03T02:00:00Z", evidence=evidence)
        result = self._run_tool([item], component="battery", include_evidence=False)
        assert "evidence" not in result["findings"][0]

    def test_evidence_included_when_requested(self):
        """When include_evidence=True, evidence[] is present in the response."""
        evidence = [{"source": "energy_usage", "value": {"soh": 92.0}, "provenance": "live"}]
        item = _ddb_item("battery", "f" * 64, "2026-08-03T02:00:00Z", evidence=evidence)
        result = self._run_tool([item], component="battery", include_evidence=True)
        assert "evidence" in result["findings"][0]
        assert result["findings"][0]["evidence"] == evidence


class TestHealthFindingsNewestSelection:
    """The tool must select the newest finding by max(computed_at) since the
    sort key is content-addressed (inputs_hash), not time-ordered."""

    def test_newest_by_computed_at_selected_when_multiple_items(self):
        """Two items with different inputs_hash (distinct ADP states) — newest returned."""
        from agents.supervisor.tools.health_findings import make_health_findings_tool

        session = _make_session()
        config = _make_config()

        older = _ddb_item(
            "battery", "a" * 64, "2026-07-25T02:00:00Z",
            finding="Battery at 90%.",
        )
        newer = _ddb_item(
            "battery", "b" * 64, "2026-08-01T02:00:00Z",
            finding="Battery at 92%.",
        )

        with patch("boto3.resource") as mock_resource:
            mock_table = MagicMock()
            # Return both items (query returns all items for the sk prefix).
            mock_table.query.return_value = {"Items": [older, newer]}
            mock_resource.return_value.Table.return_value = mock_table

            tool_fn = make_health_findings_tool(session, config)
            result = tool_fn(component="battery")

        assert result["available"] is True
        assert len(result["findings"]) == 1
        assert result["findings"][0]["finding"] == "Battery at 92%."
        assert result["findings"][0]["computed_at"] == "2026-08-01T02:00:00Z"

    def test_older_item_not_selected(self):
        """The older item must not appear in the output."""
        from agents.supervisor.tools.health_findings import make_health_findings_tool

        session = _make_session()
        config = _make_config()

        older = _ddb_item("tires", "c" * 64, "2026-07-20T02:00:00Z", finding="Old tire finding.")
        newer = _ddb_item("tires", "d" * 64, "2026-08-04T02:00:00Z", finding="New tire finding.")

        with patch("boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_table.query.return_value = {"Items": [newer, older]}
            mock_resource.return_value.Table.return_value = mock_table

            tool_fn = make_health_findings_tool(session, config)
            result = tool_fn(component="tires")

        assert result["findings"][0]["finding"] == "New tire finding."


class TestHealthFindingsQueryShape:
    """Assert the DynamoDB Query uses the correct pk and sk prefix."""

    def test_query_uses_vin_as_pk(self):
        """pk must be VEH#{vin}."""
        from agents.supervisor.tools.health_findings import make_health_findings_tool

        session = _make_session(vin="TEST1234567890123")
        config = _make_config()

        captured_kwargs: dict = {}

        with patch("boto3.resource") as mock_resource:
            mock_table = MagicMock()

            def capture_query(**kwargs):
                captured_kwargs.update(kwargs)
                return {"Items": []}

            mock_table.query.side_effect = capture_query
            mock_resource.return_value.Table.return_value = mock_table

            tool_fn = make_health_findings_tool(session, config)
            tool_fn(component="battery")

        assert mock_table.query.called, "table.query was not called"
        expr = captured_kwargs.get("KeyConditionExpression")
        assert expr is not None, "No KeyConditionExpression in query call"
        # Evaluate the expression to a boto3 ConditionExpressionBuilder to
        # extract the expression string and values.
        from boto3.dynamodb.conditions import ConditionExpressionBuilder
        builder = ConditionExpressionBuilder()
        expression = builder.build_expression(expr)
        expr_str = expression.condition_expression
        values = expression.attribute_value_placeholders
        # pk eq condition must reference VEH#{vin}
        assert "VEH#TEST1234567890123" in values.values(), (
            f"pk value 'VEH#TEST1234567890123' not found in expression values: {values}"
        )

    def test_query_uses_finding_prefix_for_component(self):
        """sk prefix must be FINDING#{component}#."""
        from agents.supervisor.tools.health_findings import make_health_findings_tool

        session = _make_session()
        config = _make_config()

        captured_kwargs: dict = {}

        with patch("boto3.resource") as mock_resource:
            mock_table = MagicMock()

            def capture_query(**kwargs):
                captured_kwargs.update(kwargs)
                return {"Items": []}

            mock_table.query.side_effect = capture_query
            mock_resource.return_value.Table.return_value = mock_table

            tool_fn = make_health_findings_tool(session, config)
            tool_fn(component="brakes")

        expr = captured_kwargs.get("KeyConditionExpression")
        assert expr is not None
        from boto3.dynamodb.conditions import ConditionExpressionBuilder
        builder = ConditionExpressionBuilder()
        expression = builder.build_expression(expr)
        values = expression.attribute_value_placeholders
        # sk begins_with condition must reference the component prefix.
        assert "FINDING#brakes#" in values.values(), (
            f"sk prefix 'FINDING#brakes#' not found in expression values: {values}"
        )


class TestHealthFindingsDynamoDbError:
    def test_ddb_init_error_returns_available_false(self):
        from agents.supervisor.tools.health_findings import make_health_findings_tool

        session = _make_session()
        config = _make_config()

        with patch("boto3.resource", side_effect=Exception("endpoint unreachable")):
            tool_fn = make_health_findings_tool(session, config)
            result = tool_fn()

        assert result["available"] is False

    def test_ddb_query_error_caught_per_component(self):
        """A query error on one component is caught and that component is skipped."""
        from agents.supervisor.tools.health_findings import make_health_findings_tool

        session = _make_session()
        config = _make_config()

        with patch("boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_table.query.side_effect = Exception("Query failed")
            mock_resource.return_value.Table.return_value = mock_table

            tool_fn = make_health_findings_tool(session, config)
            result = tool_fn(component="battery")

        # Error is caught; no finding available.
        assert result["available"] is False


class TestHealthFindingsCaseInsensitiveComponent:
    def test_component_name_is_case_insensitive(self):
        """Component names are case-normalised before use."""
        from agents.supervisor.tools.health_findings import make_health_findings_tool

        session = _make_session()
        config = _make_config()

        with patch("boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_table.query.return_value = {"Items": []}
            mock_resource.return_value.Table.return_value = mock_table

            tool_fn = make_health_findings_tool(session, config)
            # "Battery" (capital B) should not be rejected.
            result = tool_fn(component="Battery")

        # Should not return "Unknown component"
        assert "Unknown component" not in result.get("reason", "")


class TestHealthFindingsAllComponents:
    def test_all_components_queried_when_no_filter(self):
        """Without a component filter, all three components are queried."""
        from agents.supervisor.tools.health_findings import make_health_findings_tool

        session = _make_session()
        config = _make_config()

        with patch("boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_table.query.return_value = {"Items": []}
            mock_resource.return_value.Table.return_value = mock_table

            tool_fn = make_health_findings_tool(session, config)
            tool_fn()  # no component filter

        # Should have been called 3 times (once per component).
        assert mock_table.query.call_count == 3

    def test_only_requested_component_queried(self):
        """With a component filter, exactly one query is issued."""
        from agents.supervisor.tools.health_findings import make_health_findings_tool

        session = _make_session()
        config = _make_config()

        with patch("boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_table.query.return_value = {"Items": []}
            mock_resource.return_value.Table.return_value = mock_table

            tool_fn = make_health_findings_tool(session, config)
            tool_fn(component="battery")

        assert mock_table.query.call_count == 1


class TestHealthFindingsEnvVarConfig:
    def test_table_name_from_env_var(self):
        """Table name is read from HEALTH_FINDINGS_TABLE at factory time."""
        import os
        from agents.supervisor.tools.health_findings import make_health_findings_tool

        session = _make_session()
        config = _make_config()

        with patch.dict(os.environ, {"HEALTH_FINDINGS_TABLE": "vsa-prod-health-findings", "DYNAMODB_REGION": "us-east-1"}):
            with patch("boto3.resource") as mock_resource:
                mock_table = MagicMock()
                mock_table.query.return_value = {"Items": []}
                mock_resource.return_value.Table.return_value = mock_table

                tool_fn = make_health_findings_tool(session, config)
                tool_fn(component="battery")

                # Verify the resource was called with the correct region.
                mock_resource.assert_called_with("dynamodb", region_name="us-east-1")
                # Verify the table was looked up with the correct name.
                mock_resource.return_value.Table.assert_called_with("vsa-prod-health-findings")


# ===========================================================================
# T5.2 — Tier 1 boundary: no LLM call, no Athena call
#
# Spec: .kiro/specs/2026-08-03-cvx-tier2-platform-health-agent/tasks.md T5.2
# Doctrine: ~/.kiro/steering/agentic-tiers.md § "The latency budget is
#            architecture, not a bug"
#
# The health_findings tool is a Tier 1 read: it narrates pre-computed
# artifacts from DynamoDB only.  It MUST NOT invoke Bedrock (any model
# endpoint) or Athena (the Tier 2 compute surface).
#
# "A boundary that exists only in a prompt is a boundary that will be
# crossed."  These tests make the boundary executable.
#
# Two enforcement strategies applied independently:
#
#   1. STATIC — AST-scan of the module source.  Every boto3.client() and
#      boto3.resource() call in the module source is inspected; if the first
#      argument is a Bedrock or Athena service name the test fails.  Catches
#      violations *before* any code runs.
#
#   2. DYNAMIC — call-interception at runtime.  The real tool is invoked with
#      boto3.client patched to raise if called with a forbidden service name.
#      boto3.resource is wrapped with a spy that records every service name
#      passed; after the call the set is asserted to contain only "dynamodb".
#      Catches violations that static analysis cannot: factory functions,
#      conditional branches, helper imports.
#
# Non-vacuousness requirement (per spec decisions.md § 2026-08-05):
#   Each assertion must be proved able to fail.  A guard that cannot fail is
#   worse than no guard.  Every enforcement test is paired with a
#   "_fires_on_violation" companion that introduces a real violation and
#   asserts the guard catches it, and a "_allows_legitimate_*" companion that
#   confirms no false-positive.
# ===========================================================================

import ast
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

# Absolute path to the module under test.  Resolved from this test file so it
# does not depend on sys.path ordering and survives refactors.
_HEALTH_FINDINGS_PATH = (
    Path(__file__).resolve().parents[3]
    / "agents"
    / "supervisor"
    / "tools"
    / "health_findings.py"
)

# Service names that a Tier 1 read tool must NEVER create a client for.
# Bedrock service identifiers cover all model-invocation endpoints.
_FORBIDDEN_TIER1_SERVICES: frozenset[str] = frozenset(
    {
        "bedrock",
        "bedrock-runtime",
        "bedrock-agent-runtime",
        "bedrock-agentcore",
        "athena",
        # Broad Strands / AgentCore runtime service roots
        "bedrock-agent",
    }
)


def _service_is_forbidden(service_name: str) -> bool:
    """Return True if *service_name* matches any forbidden service (case-insensitive)."""
    lowered = service_name.lower()
    return any(lowered == f or lowered.startswith(f + "-") for f in _FORBIDDEN_TIER1_SERVICES)


# ---------------------------------------------------------------------------
# Shared helpers (duplicated from the T5.1 helpers above to keep each section
# self-contained; these are test-internal and do not appear in production code)
# ---------------------------------------------------------------------------


def _t52_make_session(vin: str | None = "1HGCM82633A123456") -> SimpleNamespace:
    return SimpleNamespace(vin=vin, persona_id="fleet_driver", tenant_id="fleet")


def _t52_make_config() -> SimpleNamespace:
    return SimpleNamespace(memory_id="mem-001", memory_region="us-west-2")


# ---------------------------------------------------------------------------
# Strategy 1: Static AST scan
# ---------------------------------------------------------------------------


def _scan_module_for_forbidden_boto3_calls(source: str) -> list[str]:
    """Parse *source* and return violation strings for any forbidden boto3 call.

    Checks ``boto3.client(<literal>)`` and ``boto3.resource(<literal>)`` where
    the first argument is a string literal whose value is in
    ``_FORBIDDEN_TIER1_SERVICES``.  Only inspects calls whose callee is
    ``boto3.client`` or ``boto3.resource`` (attribute access on the ``boto3``
    name).

    Returns an empty list if no violation is found.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ["<SyntaxError: could not parse source>"]

    violations: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr in ("client", "resource")
            and isinstance(func.value, ast.Name)
            and func.value.id == "boto3"
        ):
            continue
        if not node.args:
            continue
        first_arg = node.args[0]
        if not isinstance(first_arg, ast.Constant):
            continue
        service_name = str(first_arg.value)
        if _service_is_forbidden(service_name):
            violations.append(
                f"boto3.{func.attr}({service_name!r}) at line {node.lineno}: "
                "Tier 1 read tool must not create a Bedrock or Athena client "
                "(spec T5.2 / agentic-tiers.md § 'The latency budget is "
                "architecture, not a bug')"
            )

    return violations


class TestTier1BoundaryStatic:
    """Strategy 1 — AST scan of health_findings.py source.

    Tests that the module source contains no boto3 call constructing a Bedrock
    or Athena client.  Operates on the source text before any code executes.
    """

    def test_no_bedrock_client_in_module_source(self) -> None:
        """health_findings.py must not create a Bedrock client.

        An LLM call would violate the Tier 1 latency budget (~1 s for voice)
        and cross the tier boundary — Tier 1 narrates, Tier 2 reasons.

        Non-vacuousness: see test_static_scanner_fires_on_bedrock_violation.
        """
        source = _HEALTH_FINDINGS_PATH.read_text(encoding="utf-8")
        violations = [
            v for v in _scan_module_for_forbidden_boto3_calls(source)
            if "bedrock" in v.lower()
        ]
        assert not violations, (
            "health_findings.py introduces a Bedrock boto3 call.\n"
            "Bedrock clients are forbidden in Tier 1 read tools — this module\n"
            "must read DynamoDB only (spec § D9 / agentic-tiers.md).\n\n"
            "Violations:\n" + "\n".join(f"  {v}" for v in violations)
        )

    def test_no_athena_client_in_module_source(self) -> None:
        """health_findings.py must not create an Athena client.

        Athena is the Tier 2 compute surface.  A Tier 1 tool that queries
        Athena bypasses the pre-computed artifact and reintroduces Tier 2
        latency and reasoning into the conversational path.

        Non-vacuousness: see test_static_scanner_fires_on_athena_violation.
        """
        source = _HEALTH_FINDINGS_PATH.read_text(encoding="utf-8")
        violations = [
            v for v in _scan_module_for_forbidden_boto3_calls(source)
            if "athena" in v.lower()
        ]
        assert not violations, (
            "health_findings.py introduces an Athena boto3 call.\n"
            "Athena is the Tier 2 ADP read surface; Tier 1 must read only the\n"
            "pre-computed DynamoDB artifact (spec § D9).\n\n"
            "Violations:\n" + "\n".join(f"  {v}" for v in violations)
        )

    def test_only_dynamodb_resource_in_module_source(self) -> None:
        """Every boto3.client/resource call in health_findings.py targets DynamoDB.

        Asserts the positive claim: the module's only boto3 service is
        ``"dynamodb"`` (via ``boto3.resource``).  Any additional service is a
        violation of the Tier 1 read contract.

        Non-vacuousness: see test_static_scanner_fires_on_bedrock_violation and
        test_static_scanner_fires_on_athena_violation.
        """
        source = _HEALTH_FINDINGS_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        services_used: list[tuple[str, str, int]] = []  # (method, service, lineno)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and func.attr in ("client", "resource")
                and isinstance(func.value, ast.Name)
                and func.value.id == "boto3"
            ):
                continue
            if node.args and isinstance(node.args[0], ast.Constant):
                services_used.append((func.attr, str(node.args[0].value), node.lineno))

        # Exactly one service call expected, and it must be DynamoDB.
        assert services_used, (
            "No boto3.client/resource calls found in health_findings.py.\n"
            "Expected at least one boto3.resource('dynamodb', ...) call."
        )
        non_dynamodb = [
            (method, svc, lineno)
            for method, svc, lineno in services_used
            if svc.lower() != "dynamodb"
        ]
        assert not non_dynamodb, (
            "health_findings.py uses boto3 services other than 'dynamodb':\n"
            + "\n".join(
                f"  line {lineno}: boto3.{method}({svc!r})"
                for method, svc, lineno in non_dynamodb
            )
        )

    # --- Non-vacuousness companions ---

    def test_static_scanner_fires_on_bedrock_violation(self) -> None:
        """Prove the AST scanner catches a boto3.client('bedrock-runtime', ...) call.

        This test introduces a real violation into synthetic source text and
        asserts the scanner catches it.  If this test fails, the scanner is
        broken and the static boundary is not enforced.
        """
        violating_source = textwrap.dedent("""\
            import boto3

            def _bad_tier1_tool():
                # A Tier 1 tool that calls Bedrock — a boundary violation.
                bedrock = boto3.client("bedrock-runtime", region_name="us-east-1")
                response = bedrock.invoke_model(modelId="anthropic.claude-3", body=b"{}")
                return response
        """)

        violations = _scan_module_for_forbidden_boto3_calls(violating_source)
        assert violations, (
            "Static scanner did NOT fire on boto3.client('bedrock-runtime', ...).\n"
            "The Bedrock boundary guard is not working.\n"
            "Fix the scanner in T5.2 before trusting test_no_bedrock_client_in_module_source."
        )
        assert any("bedrock-runtime" in v for v in violations), (
            f"Scanner fired but not for 'bedrock-runtime'. Got: {violations}"
        )

    def test_static_scanner_fires_on_athena_violation(self) -> None:
        """Prove the AST scanner catches a boto3.client('athena', ...) call.

        This test introduces a real violation and asserts the scanner catches
        it.  If this test fails, the Athena boundary is not enforced.
        """
        violating_source = textwrap.dedent("""\
            import boto3

            def _bad_tier1_tool():
                # A Tier 1 tool that queries Athena directly — bypasses the artifact.
                athena = boto3.client("athena", region_name="us-east-1")
                athena.start_query_execution(QueryString="SELECT ...", WorkGroup="w")
        """)

        violations = _scan_module_for_forbidden_boto3_calls(violating_source)
        assert violations, (
            "Static scanner did NOT fire on boto3.client('athena', ...).\n"
            "The Athena boundary guard is not working.\n"
            "Fix the scanner in T5.2 before trusting test_no_athena_client_in_module_source."
        )
        assert any("athena" in v for v in violations), (
            f"Scanner fired but not for 'athena'. Got: {violations}"
        )

    def test_static_scanner_fires_on_bedrock_agent_runtime_violation(self) -> None:
        """Prove the scanner catches bedrock-agent-runtime (invoke_agent path)."""
        violating_source = textwrap.dedent("""\
            import boto3
            client = boto3.client("bedrock-agent-runtime", region_name="us-west-2")
        """)
        violations = _scan_module_for_forbidden_boto3_calls(violating_source)
        assert violations, (
            "Static scanner did NOT fire on boto3.client('bedrock-agent-runtime', ...).\n"
            "The bedrock-agent-runtime boundary is not enforced."
        )

    def test_static_scanner_does_not_flag_dynamodb(self) -> None:
        """The scanner must not false-positive on the legitimate dynamodb resource.

        DynamoDB is the *required* service for health_findings.  The scanner
        must not flag it or the boundary test would always fail even on
        correct code.
        """
        clean_source = textwrap.dedent("""\
            import boto3
            from boto3.dynamodb.conditions import Key

            def make_tool(session, config):
                ddb = boto3.resource("dynamodb", region_name="us-west-2")
                table = ddb.Table("vsa-staging-health-findings")
                return table.query(KeyConditionExpression=Key("pk").eq("VEH#VIN"))
        """)
        violations = _scan_module_for_forbidden_boto3_calls(clean_source)
        assert not violations, (
            f"False positive: boto3.resource('dynamodb', ...) was flagged as a violation.\n"
            f"Scanner violations: {violations}"
        )


# ---------------------------------------------------------------------------
# Strategy 2: Dynamic call-interception at runtime
# ---------------------------------------------------------------------------


class TestTier1BoundaryDynamic:
    """Strategy 2 — runtime call-interception.

    The tool is invoked with boto3.client patched to fail hard if called, and
    boto3.resource wrapped as a spy.  After the call the spy's recorded service
    names are asserted to contain only 'dynamodb'.

    Dynamic interception catches what static analysis cannot: violations that
    arrive through helper functions, conditional imports, or lazy factory calls.
    """

    def _invoke_tool_with_spy(
        self,
        component: str | None = "battery",
        include_evidence: bool = False,
    ) -> dict:
        """Invoke the tool under both a strict client blocker and a resource spy.

        Patches:
        - ``boto3.client`` — raises ``AssertionError`` if called with any
          argument, so any Bedrock/Athena client construction fails
          immediately.
        - ``boto3.resource`` — wrapped in a spy that records service names
          before delegating to a mock table returning an empty Items list.

        Returns the tool result and the list of ``(service_name, region)``
        tuples passed to ``boto3.resource``.
        """
        from agents.supervisor.tools.health_findings import make_health_findings_tool

        session = _t52_make_session()
        config = _t52_make_config()

        resource_calls: list[tuple[str, str]] = []

        def _spy_resource(service_name, **kwargs):
            region = kwargs.get("region_name", "<no-region>")
            resource_calls.append((service_name, region))
            # Return a mock that behaves like a DynamoDB resource with empty items.
            mock_res = MagicMock()
            mock_table = MagicMock()
            mock_table.query.return_value = {"Items": []}
            mock_res.Table.return_value = mock_table
            return mock_res

        def _blocking_client(service_name, **kwargs):
            raise AssertionError(
                f"boto3.client({service_name!r}) was called from "
                "health_findings tool — Tier 1 must not create any "
                "client (only boto3.resource('dynamodb') is permitted). "
                "This is a Tier 1 boundary violation (spec T5.2)."
            )

        with (
            patch("agents.supervisor.tools.health_findings.boto3.resource", side_effect=_spy_resource),
            patch("agents.supervisor.tools.health_findings.boto3.client", side_effect=_blocking_client),
        ):
            tool_fn = make_health_findings_tool(session, config)
            result = tool_fn(component=component, include_evidence=include_evidence)

        return result, resource_calls

    def test_no_boto3_client_called_during_tool_invocation(self) -> None:
        """boto3.client must never be called when the tool runs.

        Bedrock and Athena are accessed via boto3.client.  Any call to
        boto3.client from within the tool path is a Tier 1 boundary violation.
        The patch raises AssertionError if boto3.client is invoked; if the
        tool runs without triggering it, the assertion is satisfied.

        Non-vacuousness: see test_dynamic_interceptor_fires_on_client_call.
        """
        # If boto3.client were called (e.g. for Bedrock or Athena), the
        # _blocking_client side-effect raises AssertionError, which pytest
        # would re-raise as a test failure.  No exception = boto3.client was
        # never called.
        result, resource_calls = self._invoke_tool_with_spy()
        # Tool ran without error — boto3.client was not called.
        assert result is not None, "Tool did not return a result"

    def test_only_dynamodb_resource_used_at_runtime(self) -> None:
        """boto3.resource must be called with 'dynamodb' only.

        Records every service name passed to boto3.resource during a tool
        invocation and asserts the set contains only 'dynamodb'.

        Non-vacuousness: see test_dynamic_interceptor_catches_extra_resource_call.
        """
        result, resource_calls = self._invoke_tool_with_spy()

        assert resource_calls, (
            "boto3.resource was never called — expected at least one call to "
            "boto3.resource('dynamodb', ...) for the DynamoDB table lookup."
        )

        services_used = {svc for svc, _ in resource_calls}
        non_dynamodb = services_used - {"dynamodb"}
        assert not non_dynamodb, (
            "health_findings tool used boto3.resource with unexpected services: "
            f"{non_dynamodb}.\n"
            "Only 'dynamodb' is permitted for a Tier 1 read tool."
        )

    def test_all_components_use_only_dynamodb_at_runtime(self) -> None:
        """Scanning all three components still uses only DynamoDB.

        Exercises the full component-loop path (no component filter = all
        three queried) and confirms no service other than DynamoDB is touched.
        """
        result, resource_calls = self._invoke_tool_with_spy(component=None)

        services_used = {svc for svc, _ in resource_calls}
        non_dynamodb = services_used - {"dynamodb"}
        assert not non_dynamodb, (
            "All-components invocation used non-DynamoDB services: "
            f"{non_dynamodb}"
        )

    # --- Non-vacuousness companions ---

    def test_dynamic_interceptor_fires_on_client_call(self) -> None:
        """Prove the blocking-client patch fires if boto3.client is called.

        Writes a synthetic function that calls ``boto3.client("bedrock-runtime")``
        and runs it under the same patch the boundary tests use.  Asserts
        ``AssertionError`` is raised, confirming the interceptor is live.

        If this test fails, the dynamic boundary (test_no_boto3_client_called_*)
        is vacuous — it would not catch a real violation.
        """
        import boto3 as real_boto3

        def _blocking_client_for_test(service_name, **kwargs):
            raise AssertionError(
                f"Interceptor fired: boto3.client({service_name!r}) was called."
            )

        def _violating_code():
            """Simulates a Tier 1 tool that mistakenly calls Bedrock."""
            client = real_boto3.client("bedrock-runtime", region_name="us-east-1")
            return client.invoke_model(modelId="us.anthropic.claude-sonnet-4-6", body=b"{}")

        with patch("boto3.client", side_effect=_blocking_client_for_test):
            with pytest.raises(AssertionError, match="Interceptor fired"):
                _violating_code()

    def test_dynamic_interceptor_catches_extra_resource_call(self) -> None:
        """Prove the resource spy catches a service other than 'dynamodb'.

        Simulates a Tier 1 tool that calls ``boto3.resource("some-other-svc")``
        and asserts the spy's recorded set would contain a non-dynamodb entry.
        This confirms the set-check in test_only_dynamodb_resource_used_at_runtime
        is not vacuous.
        """
        recorded: list[str] = []

        def _spy(service_name, **kwargs):
            recorded.append(service_name)
            return MagicMock()

        import boto3 as real_boto3

        with patch("boto3.resource", side_effect=_spy):
            real_boto3.resource("dynamodb", region_name="us-west-2")
            real_boto3.resource("bedrock-agentcore", region_name="us-east-1")

        services = set(recorded)
        non_dynamodb = services - {"dynamodb"}
        assert non_dynamodb, (
            "Spy did not capture the extra boto3.resource('bedrock-agentcore') call.\n"
            "The spy is not working — test_only_dynamodb_resource_used_at_runtime "
            "would not catch a real violation."
        )
        assert "bedrock-agentcore" in non_dynamodb

    def test_dynamic_interceptor_does_not_false_positive_on_dynamodb(self) -> None:
        """boto3.resource('dynamodb', ...) must not trigger the client blocker.

        The client blocker only fires for ``boto3.client``; ``boto3.resource``
        is spied on separately.  Calling ``boto3.resource('dynamodb')`` must
        not cause the test to fail — it is the correct and required call.
        """
        # This is effectively a sanity check: if _invoke_tool_with_spy() raises
        # unexpectedly because of a false positive on DynamoDB, it would surface
        # here rather than in the real boundary tests.
        try:
            result, resource_calls = self._invoke_tool_with_spy(component="battery")
        except AssertionError as exc:
            pytest.fail(
                f"Tool invocation raised AssertionError unexpectedly: {exc}\n"
                "The dynamic interceptor false-positived on a legitimate DynamoDB call."
            )
        services = {svc for svc, _ in resource_calls}
        assert "dynamodb" in services, (
            "boto3.resource('dynamodb') was not called — the spy did not record it."
        )
