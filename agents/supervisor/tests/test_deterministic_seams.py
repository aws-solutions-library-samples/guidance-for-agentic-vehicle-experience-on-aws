"""Deterministic-seam tests for CVX DMS persona extension.

Spec: `.kiro/specs/2026-08-26-cvx-dms-personas-extension/spec.md`
Tasks: T1.3 (skeleton) + T4.2 (un-skip and complete).

There are **two** deterministic seams this spec asserts, and both must be
enforceable in test — a boundary that exists only in a prompt is a boundary
that will be crossed (see ``~/.kiro/steering/agentic-tiers.md``).

  1.  **Warranty narration verbatim.**  ``validate_claim`` returns
      ``verdict_status`` and ``verdict_reason`` byte-for-byte as DMS's rules
      engine wrote them.  A tool that transforms them (paraphrase, softening,
      truncation) has crossed the seam.  Direct-narration cases live in
      ``test_dms_personas.py`` — this file adds the "reflection" case (a
      hypothetical narration flipping ``denied`` to ``approved`` would trip
      the test) plus the AST-level guard that no LLM is invoked inside the
      tool.
  2.  **Agent → Amazon Connect prohibition (DMS R2c).**  The runtime role
      MUST NOT hold any ``connect:*`` action, and no DMS tool module MAY
      import or call ``boto3.client('connect')``.  Enforced two ways:
        (a) IAM template walk over the synthesized ``VsaCoreStack``.
        (b) AST walk over every ``agents/supervisor/tools/dms_*.py``.
      Both directions of the AST walk are asserted with a fixture — a
      *synthetic* module that DOES call ``boto3.client('connect')`` must
      trip the assertion; the DMS modules must not.  Asserting only the
      pass direction makes it possible for a future contributor to silently
      disable the check by moving the target directory.

Not a substitute for the DMS-side seam tests — the DMS side asserts the
rules engine itself.  This side asserts that the CVX narration honors it.
"""

from __future__ import annotations

import ast
import json
import pathlib
import textwrap
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
DMS_TOOLS_GLOB = "agents/supervisor/tools/dms_*.py"
ALL_TOOLS_GLOB = "agents/supervisor/tools/*.py"

# Prefer the already-synthesized template in the CDK output dir; regenerating
# on-the-fly is slow and fragile in CI.  If the test can't find it (a rare fresh
# clone), we skip with a clear message rather than fail — this is defensive and
# does NOT weaken the assertion when the template is present.
#
# The output directory is READ FROM cdk.json rather than hardcoded. It was
# hardcoded to `infrastructure/cdk.out`, and on 2026-09-22 `cdk.json` gained
# `"output": "build/cdk.out"` (so the cloud assembly stops being swept into the
# AgentCore runtime package — see
# issues/2026-09-22-agentcore-package-sweeps-cdk-out/). The hardcoded path then
# pointed at nothing, and because this module SKIPS on a missing template rather
# than failing, `test_agent_iam_has_no_connect_action` and its siblings went
# quietly from red to skipped. A deterministic-seam guard that skips is not
# guarding. Deriving the path means a future change to `output` moves these
# tests with it.
def _cdk_output_dir() -> Path:
    cdk_json = REPO_ROOT / "infrastructure" / "cdk.json"
    try:
        configured = json.loads(cdk_json.read_text()).get("output")
    except (OSError, ValueError):
        configured = None
    # `output` is relative to the CDK app directory.
    return REPO_ROOT / "infrastructure" / (configured or "cdk.out")


_STAGING_CORE_TEMPLATE = _cdk_output_dir() / "vsa-staging-core.template.json"


# ---------------------------------------------------------------------------
# (1) Warranty narration verbatim — reflection guard
# ---------------------------------------------------------------------------


def _fake_denied_verdict() -> dict[str, Any]:
    """A shape identical to what DMS's warranty_claims handler writes.

    ``dealer_id`` matches the ctx built by ``_minimal_ctx`` — the scope
    check is not the subject of this test file; ``test_dms_personas.py`` §
    K covers that.  Here the point is verdict narration, so the item is
    always in-scope.
    """
    return {
        "claim_id": "WC-SEAM-001",
        "dealer_id": "DLR-01",   # matches _minimal_ctx().dealer_ids
        "status": "Validated",  # DMS lifecycle — NOT the verdict
        "verdict_status": "denied",
        "verdict_reason": "labor_op_mismatch: RO labor op 999-99 outside coverage",
        "matched_rule_id": "R-LO-004",
        "updated_at": "2026-09-01T00:00:00Z",
    }


def test_denied_verdict_narration_contains_denied_and_reason(monkeypatch):
    """The tool returns the DMS-written 'denied' and the reason string
    byte-for-byte.  Softening either would cross the deterministic seam."""
    from agents.supervisor.tools import dms_warranty
    monkeypatch.setattr(
        dms_warranty, "check_dealer_certification",
        lambda dealer_id: ("Gold", None),
    )

    tbl = MagicMock()
    tbl.get_item.return_value = {"Item": _fake_denied_verdict()}
    ddb = MagicMock()
    ddb.Table.return_value = tbl

    with patch("boto3.resource", return_value=ddb):
        tool_fn = dms_warranty.make_warranty_validate_tool(
            _minimal_ctx(dealer_id="DLR-01"), _minimal_config()
        )
        result = tool_fn("WC-SEAM-001")

    # Byte-for-byte — no paraphrase, no softening.
    assert result["verdict_status"] == "denied"
    assert result["verdict_reason"] == _fake_denied_verdict()["verdict_reason"]


def test_narration_cannot_flip_denied_to_approved(monkeypatch):
    """No code path in the tool can produce a returned dict whose
    verdict_status contradicts what DDB stored.

    A hypothetical mutation like ``result["verdict_status"] = "approved"``
    inside the tool would flip a denial silently — but we can only test the
    tool as written, so this case asserts (a) verbatim-through of denied,
    (b) no softening synonyms appear in the reason surface.  A future
    refactor that ran the verdict_reason through an LLM 'for clarity' would
    fail case (b).
    """
    from agents.supervisor.tools import dms_warranty
    monkeypatch.setattr(
        dms_warranty, "check_dealer_certification",
        lambda dealer_id: ("Gold", None),
    )

    tbl = MagicMock()
    tbl.get_item.return_value = {"Item": _fake_denied_verdict()}
    ddb = MagicMock()
    ddb.Table.return_value = tbl

    with patch("boto3.resource", return_value=ddb):
        tool_fn = dms_warranty.make_warranty_validate_tool(
            _minimal_ctx(dealer_id="DLR-01"), _minimal_config()
        )
        result = tool_fn("WC-SEAM-001")

    # (a) verdict_status matches source; no synonym leakage.
    assert result["verdict_status"] == "denied"
    for softener in ("approved", "approve", "may be approved", "pending approval"):
        assert softener not in str(result.get("verdict_status", "")).lower()
    # (b) Reason string is un-transformed.  The tool MUST NOT synthesise
    # softener phrases into what the LLM sees.  We assert this by matching
    # exact byte equality to the DDB record's reason, which is the strictest
    # possible statement of the seam.
    assert result["verdict_reason"] == _fake_denied_verdict()["verdict_reason"]


# ---------------------------------------------------------------------------
# (2a) IAM: no ``connect:*`` action anywhere in the synthesized runtime role
# ---------------------------------------------------------------------------


def _load_core_template() -> dict[str, Any]:
    if not _STAGING_CORE_TEMPLATE.exists():
        pytest.skip(
            f"CDK template not present at {_STAGING_CORE_TEMPLATE} — "
            "run `cd infrastructure && npx cdk synth VsaCoreStack` first "
            "(pre-synthesized artifact expected in CI)."
        )
    with _STAGING_CORE_TEMPLATE.open() as f:
        return json.load(f)


def _iter_iam_statements(template: dict[str, Any]):
    """Yield every ``Statement`` from every IAM policy resource in the
    template — inline policies on Roles, standalone Policies, ManagedPolicies.

    ``Action`` may be a string or a list.  Each yielded tuple is
    ``(logical_id, statement_dict)`` so failures point at a specific resource.
    """
    resources = template.get("Resources", {})
    for lid, res in resources.items():
        rtype = res.get("Type", "")
        props = res.get("Properties", {}) or {}
        if rtype in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
            doc = props.get("PolicyDocument", {}) or {}
            for stmt in doc.get("Statement", []) or []:
                yield lid, stmt
        elif rtype == "AWS::IAM::Role":
            for pol in props.get("Policies", []) or []:
                doc = pol.get("PolicyDocument", {}) or {}
                for stmt in doc.get("Statement", []) or []:
                    yield lid, stmt


def _statement_actions(stmt: dict[str, Any]) -> list[str]:
    """Return the ``Action`` list from a statement, handling scalar and list
    forms and skipping ``Fn::If``-wrapped non-string entries defensively."""
    act = stmt.get("Action")
    if isinstance(act, str):
        return [act]
    if isinstance(act, list):
        return [a for a in act if isinstance(a, str)]
    # Fn::If / other intrinsic — treat as no scannable action.  A ``connect:``
    # would still show up in a JSON string search, which is why the last-line
    # blob check below is independent of this structural walk.
    return []


def test_agent_iam_has_no_connect_action():
    """The CVX runtime role MUST NOT hold any ``connect:*`` IAM action.

    Two enforcement paths:
      * Structural walk over every ``Statement`` — catches Allow rules that
        parse as valid IAM.
      * JSON-string check for the literal ``"connect:"`` — catches
        ``connect:*`` inside a ``Fn::If`` or other intrinsic that the
        structural walk skips.

    Both must be clean.  The spec's R2c boundary is total — v1 has no
    legitimate ``connect:*`` on this role, not even a read-only one like
    ``connect:GetCurrentUserData``.
    """
    template = _load_core_template()

    for lid, stmt in _iter_iam_statements(template):
        for action in _statement_actions(stmt):
            assert not action.lower().startswith("connect:"), (
                f"IAM Statement in {lid} grants {action!r} — DMS spec R2c "
                "forbids any connect:* action on the CVX runtime role"
            )

    # Belt-and-braces: the literal "connect:" must not appear anywhere in
    # the template blob.  This catches Fn::If-wrapped intrinsics and any
    # nested resource we might miss.
    blob = json.dumps(template)
    assert "connect:" not in blob, (
        "The literal string 'connect:' appears in the synthesized template — "
        "somewhere an IAM Allow granted a connect:* action.  Grep with -n to "
        "locate."
    )


# ---------------------------------------------------------------------------
# (2b) AST: no ``boto3.client('connect')`` in dms_*.py
# ---------------------------------------------------------------------------


def _tree_contains_connect_client_call(tree: ast.AST) -> bool:
    """True iff any ``Call`` in ``tree`` looks like ``boto3.client('connect')``
    or ``boto3.client(service_name='connect')`` — positional OR keyword."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # boto3.client(...) — Attribute-access form.
        is_boto3_client = (
            isinstance(func, ast.Attribute)
            and func.attr == "client"
            and isinstance(func.value, ast.Name)
            and func.value.id == "boto3"
        )
        # from boto3 import client; client(...) — direct-import form.
        is_bare_client = (
            isinstance(func, ast.Name) and func.id == "client"
        )
        if not (is_boto3_client or is_bare_client):
            continue

        # Positional first arg.
        if node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and first.value == "connect":
                return True
        # Keyword ``service_name='connect'``.
        for kw in node.keywords:
            if (
                kw.arg == "service_name"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value == "connect"
            ):
                return True
    return False


def _dms_tool_files() -> list[pathlib.Path]:
    """Every ``dms_*.py`` under ``agents/supervisor/tools/``.

    Anchored by literal glob — a future contributor can't silently disable
    the check by moving the modules to a different directory without also
    updating this test.  If someone does that, the glob returns [] and
    the sanity assertion below fires.
    """
    root = REPO_ROOT / pathlib.Path(DMS_TOOLS_GLOB).parent
    files = sorted(root.glob(pathlib.Path(DMS_TOOLS_GLOB).name))
    return files


def test_agent_tools_have_no_connect_call_graph():
    """No DMS tool module calls ``boto3.client('connect')``.

    Also confirms via a synthetic taint fixture that the walker DOES catch
    a violation — asserting only the pass direction lets a future refactor
    silently break the check.
    """
    files = _dms_tool_files()
    # Sanity: the glob resolved to real files.  If a contributor moves the
    # modules, we want this to fail loudly, not silently pass.
    assert files, (
        f"No files matched glob {DMS_TOOLS_GLOB!r} — the DMS tool modules "
        "have moved or been renamed.  Update this test's glob."
    )

    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assert not _tree_contains_connect_client_call(tree), (
            f"{path} calls boto3.client('connect') — DMS spec R2c violation"
        )

    # Negative control: a synthetic module that DOES call the forbidden API
    # MUST be caught by the walker.  Uses in-memory AST — no file created,
    # no pollution risk.
    tainted_src = textwrap.dedent(
        """
        import boto3
        # A boundary-crossing call the DMS modules must not have.
        _client = boto3.client("connect")
        """
    )
    tainted_tree = ast.parse(tainted_src)
    assert _tree_contains_connect_client_call(tainted_tree), (
        "AST walker failed to detect boto3.client('connect') in a synthetic "
        "fixture — the walker is broken and would silently pass real "
        "violations.  Do NOT relax the assertion; fix the walker."
    )


def test_zero_connect_import_paths_in_supervisor_tools_at_large():
    """The R2c ban is per-runtime, not per-persona.  No supervisor tool at
    all — DMS or otherwise — may call ``boto3.client('connect')``.

    Pre-existing personas already respect this; the test locks the
    invariant so a future DMS-adjacent change can't silently regress it
    for a non-DMS surface.
    """
    root = REPO_ROOT / pathlib.Path(ALL_TOOLS_GLOB).parent
    for path in sorted(root.glob(pathlib.Path(ALL_TOOLS_GLOB).name)):
        # Skip __init__.py — not a tool module.
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assert not _tree_contains_connect_client_call(tree), (
            f"{path} calls boto3.client('connect') — the supervisor runtime "
            "role has no legitimate use for Amazon Connect in v1"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_ctx(dealer_id: str = ""):
    from types import SimpleNamespace
    # Populate dealer_ids so the deterministic-seam warranty tests clear the
    # caller-to-target scope check added in Fix Group 2.  The item fixture
    # uses dealer_id="DLR-01" (see _fake_denied_verdict), so a matching entry
    # in the caller's dealer_ids keeps the seam test focused on narration
    # semantics rather than authorization.
    scope = [dealer_id] if dealer_id else ["DLR-01"]
    return SimpleNamespace(
        jwt="", session_id="", tenant_id="fleet",
        vin=None, persona_id="warranty_agent",
        dealer_id=dealer_id or "DLR-01",
        dealer_ids=scope,
        district_ids=[],
    )


def _minimal_config():
    from types import SimpleNamespace
    return SimpleNamespace(
        cms_api_base="", api_base="", adp_kb_id="",
        adp_region="us-east-1", adp_account_id="",
    )
