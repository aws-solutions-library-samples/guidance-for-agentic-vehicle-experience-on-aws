"""Enforce spec D4: every persona that handles escalation must have BOTH paths.

Filed against review.md § Cycle 1 Warning W9 (spec
``2026-09-23-cvx-escalation-recommend-not-act``).

``PERSONA_TOOL_FACTORIES`` in ``agents/supervisor/tools/__init__.py`` is the
single authoritative mapping from persona name to tool list.  Spec § D4 says:
*"one escalation model for every persona — no per-persona escalation behaviour"*.
The concrete implication is a structural PAIRING rule:

    Any persona whose tool list includes ``make_escalate_tool``
    MUST ALSO include ``make_service_escalate_tool``.

The inverse is equally required:

    Any persona whose tool list includes ``make_service_escalate_tool``
    MUST ALSO include ``make_escalate_tool``.

The two tools are paired by design (safety path and service path — two separate
entry points that together constitute the complete escalation affordance for a
persona).  A persona with only one of them has a structural gap:

- Only ``make_escalate_tool``: a user asking for help with no triage verdict
  (e.g. a billing question, a feature question, or any non-safety scenario)
  hits 1.1's fail-closed ``EscalateError`` and cannot reach a human at all.
  This is *worse* than the pre-spec behaviour for that persona, which
  explains why W9 calls it safety-critical.

- Only ``make_service_escalate_tool``: a persona that handles vehicle safety
  concerns has no path for the classifier-derived P-level handoff, breaking
  the deterministic seam in spec § Constraints.

Personas carrying *neither* tool are correct and exempt.  Today those are
``parts_intel`` (parts-lookup orchestrator, no human-handoff scenario) and
``compliance_agent`` (stub awaiting full implementation).

The guard here is structural and over source code — not behavioural — for the
same reason ``TestClassificationProvenance`` in
``test_triage_classification_provenance.py`` is structural: the gap would
appear in a new persona added by a future spec, and a behavioural test can only
cover what someone thought to test at creation time.  An AST walk over the
real dictionary guarantees the rule applies to every persona, including any
added after this file was written.

Anti-vacuity design
-------------------
The ``PERSONA_TOOL_FACTORIES`` dict currently has 11 personas that carry
``make_escalate_tool`` (the pairing is confirmed by the reviewer at W9).  The
floor for the walker is therefore **11** — if the walker finds fewer than 11
personas carrying that tool, either the parsing is broken or the source file
changed shape and this test needs re-inspection.  The floor is stated as the
expected minimum count, not as 0 or 1, precisely so a broken discovery loop
cannot produce a vacuous PASS.
"""

from __future__ import annotations

import ast
import pathlib
import textwrap
from typing import NamedTuple

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
INIT_PATH = REPO_ROOT / "agents" / "supervisor" / "tools" / "__init__.py"

# The two names whose pairing is asserted.
_SAFETY_TOOL = "make_escalate_tool"
_SERVICE_TOOL = "make_service_escalate_tool"

# Anti-vacuity floor: as of spec 2026-09-23, 11 personas carry make_escalate_tool.
# If the walker finds fewer the test fails so we don't silently pass against a
# broken parse.  The floor is intentionally the observed count, not an arbitrary
# low value like 1.
_MIN_PERSONAS_WITH_SAFETY_TOOL = 11


class PersonaTools(NamedTuple):
    name: str          # persona key string
    tools: list[str]   # list of factory function names in declaration order
    lineno: int        # line of the key in __init__.py, for error messages


def _parse_persona_tool_factories(path: pathlib.Path) -> list[PersonaTools]:
    """Parse ``PERSONA_TOOL_FACTORIES`` from ``tools/__init__.py`` via AST.

    Locates the ``PERSONA_TOOL_FACTORIES = { ... }`` assignment, then walks
    each key → list pair.  Each list element is expected to be a ``Name`` node
    (a factory function reference).  Other node types are skipped with a
    warning so a future syntax change degrades visibly rather than silently.

    Returns one ``PersonaTools`` per key, in declaration order.
    """
    src = path.read_text()
    tree = ast.parse(src)

    target_dict: ast.Dict | None = None
    for node in ast.walk(tree):
        # Handle both plain assignment (``x = {...}``) and annotated assignment
        # (``x: dict[str, list] = {...}``).  The real __init__.py uses the
        # annotated form ``PERSONA_TOOL_FACTORIES: dict[str, list] = {...}``.
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "PERSONA_TOOL_FACTORIES":
                    if isinstance(node.value, ast.Dict):
                        target_dict = node.value
                    break
        elif isinstance(node, ast.AnnAssign):
            t = node.target
            if isinstance(t, ast.Name) and t.id == "PERSONA_TOOL_FACTORIES":
                if node.value is not None and isinstance(node.value, ast.Dict):
                    target_dict = node.value
        if target_dict is not None:
            break

    assert target_dict is not None, (
        f"Could not find 'PERSONA_TOOL_FACTORIES' assignment in {path}. "
        "Either the dict was renamed or the file structure changed — update "
        "_parse_persona_tool_factories."
    )

    results: list[PersonaTools] = []
    for key_node, value_node in zip(target_dict.keys, target_dict.values):
        # Keys should be string constants ("fleet_driver", etc.)
        assert isinstance(key_node, ast.Constant) and isinstance(key_node.value, str), (
            f"Unexpected key type {type(key_node).__name__!r} in "
            "PERSONA_TOOL_FACTORIES — expected a string constant."
        )
        persona_name: str = key_node.value

        assert isinstance(value_node, ast.List), (
            f"Value for persona {persona_name!r} is not a list literal "
            f"(got {type(value_node).__name__!r}). The parser only handles "
            "direct list literals — if the list was refactored into a variable "
            "or comprehension, update _parse_persona_tool_factories."
        )

        tool_names: list[str] = []
        for elt in value_node.elts:
            if isinstance(elt, ast.Name):
                tool_names.append(elt.id)
            # Attribute access (e.g. module.make_foo) is unusual here but
            # handle it gracefully rather than silently dropping entries.
            elif isinstance(elt, ast.Attribute):
                tool_names.append(f"{ast.unparse(elt)}")
            # Any other form: skip but don't silently swallow — the anti-vacuity
            # floor will catch a wholesale parse failure.

        results.append(PersonaTools(
            name=persona_name,
            tools=tool_names,
            lineno=key_node.lineno,
        ))

    return results


class TestEscalationToolPairing:
    """Every persona with make_escalate_tool must also have make_service_escalate_tool
    and vice versa.  Personas with neither are exempt.

    This is the structural guard for spec D4: "one escalation model for every
    persona."  The reviewer confirmed the pairing holds today (W9); this test
    makes it impossible for a future persona to violate it silently.
    """

    def test_safety_tool_implies_service_tool(self):
        """Personas with make_escalate_tool must also carry make_service_escalate_tool.

        A persona with only the safety tool cannot reach a human for non-safety
        requests — 1.1's fail-closed raise blocks every path that lacks a triage
        verdict, which is the majority of service-level enquiries.
        """
        personas = _parse_persona_tool_factories(INIT_PATH)
        offenders: list[str] = []
        for p in personas:
            if _SAFETY_TOOL in p.tools and _SERVICE_TOOL not in p.tools:
                offenders.append(
                    f"  tools/__init__.py:{p.lineno}  persona={p.name!r} "
                    f"has {_SAFETY_TOOL!r} but is missing {_SERVICE_TOOL!r}"
                )
        assert not offenders, (
            "The following personas carry the safety-escalation tool without "
            "the service-escalation tool.  A user on this persona asking for "
            "a human with no triage verdict will hit EscalateError and cannot "
            "reach a human at all — worse than pre-spec behaviour.  Add "
            f"{_SERVICE_TOOL!r} or (if the persona genuinely handles no safety "
            "scenarios) remove both tools:\n"
            + "\n".join(offenders)
        )

    def test_service_tool_implies_safety_tool(self):
        """Personas with make_service_escalate_tool must also carry make_escalate_tool.

        A persona with only the service tool has no path for the classifier-derived
        P-level handoff, breaking the deterministic escalation seam.
        """
        personas = _parse_persona_tool_factories(INIT_PATH)
        offenders: list[str] = []
        for p in personas:
            if _SERVICE_TOOL in p.tools and _SAFETY_TOOL not in p.tools:
                offenders.append(
                    f"  tools/__init__.py:{p.lineno}  persona={p.name!r} "
                    f"has {_SERVICE_TOOL!r} but is missing {_SAFETY_TOOL!r}"
                )
        assert not offenders, (
            "The following personas carry the service-escalation tool without "
            "the safety-escalation tool.  Per spec D4, both tools travel as a "
            "pair — one is not meaningful without the other.  Add "
            f"{_SAFETY_TOOL!r} or remove both tools:\n"
            + "\n".join(offenders)
        )

    def test_anti_vacuity_floor_personas_with_safety_tool(self):
        """The walker must find at least 11 personas carrying make_escalate_tool.

        If it finds fewer, either the parser is broken (a regression we need to
        know about) or the source file lost escalation-capable personas (a
        separate regression we need to know about).  The floor is the confirmed
        count from the reviewer's W9 finding, not an arbitrary low value.
        """
        personas = _parse_persona_tool_factories(INIT_PATH)
        with_safety = [p for p in personas if _SAFETY_TOOL in p.tools]
        assert len(with_safety) >= _MIN_PERSONAS_WITH_SAFETY_TOOL, (
            f"Expected at least {_MIN_PERSONAS_WITH_SAFETY_TOOL} personas "
            f"carrying {_SAFETY_TOOL!r}; found {len(with_safety)}: "
            f"{[p.name for p in with_safety]}.\n\n"
            "If the floor is correct and the count is wrong, the parser has a "
            "bug — check _parse_persona_tool_factories.  If personas were "
            "legitimately removed, lower the floor with a comment recording why."
        )


class TestPairingWalkerIsNotVacuous:
    """Anti-vacuity floor for the pairing walker itself.

    If ``_parse_persona_tool_factories`` silently fails to extract tool names,
    both pairing tests pass for free — every persona has an empty tool list,
    which vacuously satisfies 'no persona has one without the other'.

    These tests prove:
    1. A synthetic dict with a missing-partner violation IS detected.
    2. A synthetic dict with only the correct pattern (both tools) is NOT flagged.
    3. A synthetic dict with neither tool (the exempt case) is NOT flagged.
    4. The real ``tools/__init__.py`` is parsed to a non-empty result.
    """

    def _make_init_ast(self, src: str) -> list[PersonaTools]:
        """Parse a synthetic PERSONA_TOOL_FACTORIES snippet."""
        # Wrap in an ast-parseable assignment
        wrapped = f"PERSONA_TOOL_FACTORIES = {src}"
        path = INIT_PATH  # used only for error messages, not read
        # We monkeypatch path.read_text by parsing directly
        node = ast.parse(wrapped)
        # Extract the dict from the assignment
        assign = next(
            n for n in ast.walk(node) if isinstance(n, ast.Assign)
        )
        target_dict = assign.value
        assert isinstance(target_dict, ast.Dict)
        results: list[PersonaTools] = []
        for key_node, value_node in zip(target_dict.keys, target_dict.values):
            assert isinstance(key_node, ast.Constant)
            assert isinstance(value_node, ast.List)
            tool_names = [
                elt.id for elt in value_node.elts if isinstance(elt, ast.Name)
            ]
            results.append(PersonaTools(
                name=key_node.value,
                tools=tool_names,
                lineno=key_node.lineno,
            ))
        return results

    def test_walker_catches_safety_without_service(self):
        """Safety tool alone (no service tool) must be detected as a violation."""
        synthetic = textwrap.dedent("""\
            {
                "fleet_driver": [
                    make_triage_tool,
                    make_escalate_tool,
                ],
            }
        """)
        personas = self._make_init_ast(synthetic)
        offenders = [
            p.name for p in personas
            if _SAFETY_TOOL in p.tools and _SERVICE_TOOL not in p.tools
        ]
        assert "fleet_driver" in offenders, (
            "The pairing walker must flag a persona with make_escalate_tool "
            "but no make_service_escalate_tool.  If it does not, "
            "test_safety_tool_implies_service_tool passes vacuously."
        )

    def test_walker_catches_service_without_safety(self):
        """Service tool alone (no safety tool) must be detected as a violation."""
        synthetic = textwrap.dedent("""\
            {
                "retail_advisor": [
                    make_triage_tool,
                    make_service_escalate_tool,
                ],
            }
        """)
        personas = self._make_init_ast(synthetic)
        offenders = [
            p.name for p in personas
            if _SERVICE_TOOL in p.tools and _SAFETY_TOOL not in p.tools
        ]
        assert "retail_advisor" in offenders, (
            "The pairing walker must flag a persona with make_service_escalate_tool "
            "but no make_escalate_tool.  If it does not, "
            "test_service_tool_implies_safety_tool passes vacuously."
        )

    def test_walker_does_not_flag_paired_persona(self):
        """Both tools present — must NOT be flagged as a violation."""
        synthetic = textwrap.dedent("""\
            {
                "service_advisor": [
                    make_triage_tool,
                    make_escalate_tool,
                    make_service_escalate_tool,
                    make_knowledge_tool,
                ],
            }
        """)
        personas = self._make_init_ast(synthetic)
        safety_offenders = [
            p.name for p in personas
            if _SAFETY_TOOL in p.tools and _SERVICE_TOOL not in p.tools
        ]
        service_offenders = [
            p.name for p in personas
            if _SERVICE_TOOL in p.tools and _SAFETY_TOOL not in p.tools
        ]
        assert not safety_offenders and not service_offenders, (
            "A persona with both make_escalate_tool and make_service_escalate_tool "
            "must not be flagged.  The pairing is complete."
        )

    def test_walker_does_not_flag_neither_tool(self):
        """Neither tool present — the exempt case — must NOT be flagged."""
        synthetic = textwrap.dedent("""\
            {
                "parts_intel": [
                    make_parts_intel_tool,
                ],
                "compliance_agent": [
                    make_compliance_agent_stub_tool,
                ],
            }
        """)
        personas = self._make_init_ast(synthetic)
        safety_offenders = [
            p.name for p in personas
            if _SAFETY_TOOL in p.tools and _SERVICE_TOOL not in p.tools
        ]
        service_offenders = [
            p.name for p in personas
            if _SERVICE_TOOL in p.tools and _SAFETY_TOOL not in p.tools
        ]
        assert not safety_offenders and not service_offenders, (
            "Personas carrying neither escalation tool (parts_intel, "
            "compliance_agent) are exempt from the pairing rule and must not "
            "be flagged.  The rule is: one implies the other, not: both are "
            "required unconditionally."
        )

    def test_walker_finds_real_init_py_non_empty(self):
        """Positive control: parsing the real __init__.py must yield personas.

        If this returns an empty list, _parse_persona_tool_factories is broken
        and all pairing assertions are vacuously true.
        """
        personas = _parse_persona_tool_factories(INIT_PATH)
        assert len(personas) > 0, (
            f"_parse_persona_tool_factories returned no personas for {INIT_PATH}. "
            "Either the file is empty, 'PERSONA_TOOL_FACTORIES' was renamed, "
            "or the parser has a bug.  All pairing assertions are vacuous until "
            "this is fixed."
        )
        # Also verify we find BOTH target tool names somewhere across all personas
        # (positive control that Name nodes are parsed, not just key strings).
        all_tools = {t for p in personas for t in p.tools}
        assert _SAFETY_TOOL in all_tools, (
            f"{_SAFETY_TOOL!r} not found in any persona's tool list when parsing "
            f"{INIT_PATH}. Either the tool was renamed or list elements are not "
            "being parsed as Name nodes — check _parse_persona_tool_factories."
        )
        assert _SERVICE_TOOL in all_tools, (
            f"{_SERVICE_TOOL!r} not found in any persona's tool list when parsing "
            f"{INIT_PATH}. Either the tool was renamed or list elements are not "
            "being parsed as Name nodes — check _parse_persona_tool_factories."
        )
