"""The triage classification may only come from the triage classifier.

Filed against ``issues/2026-09-23-triage-401-llm-improvises-classification/``.

``ctx.triage_classification`` is the platform's deterministic P0–P3 seam. Every
downstream safety decision reads it: ``book`` derives ``safeToDrive``, the SLA
window, and the recommended action from it (``tools/book.py`` ``_PRIORITY_*``
tables); the iOS drawer renders it; the STAR envelope ships it to the dealer.

On 2026-09-23 two separate code paths were found assigning a **literal** ``"P3"``
to that field as a "best-effort fallback" when ``triage`` was unavailable:

  1. ``tools/book.py`` — defaulted to P3 when no triage call had run, added to
     work around Nova Sonic stalling before calling ``triage``.
  2. ``bidi_app.py`` ``_auto_book_*`` — set P3 in the ``except`` arm when the
     inline triage call raised, "so book can proceed without blocking the demo
     on a transient classifier hiccup".

The first one fired in staging (session ``session_1790101469102_mhpvti4vs-x``,
12:22 UTC): ``POST /triage`` 401'd, the LLM improvised a P0 pull-over
instruction, and the silent default supplied "P3" so the booking succeeded —
producing a real ticket whose priority (``safeToDrive: "yes"``, 14-day window)
directly contradicted the emergency narration the driver had just received.

Both are the same defect class, and it is the one
``~/.kiro/steering/agentic-tiers.md`` § "What stays deterministic, and why"
names: *a safety property should not be probabilistic*, and *a boundary that
exists only in a prompt is a boundary that will be crossed*. A prompt rule
telling the model not to invent a priority cannot help when the **platform**
invents one.

So this is a structural guard rather than a behavioural one. A behavioural test
can only cover the paths someone thought to test; the defect's whole character
is that it appears in a quiet ``except`` arm on a path nobody exercises. The
invariant asserted here is provenance:

    Only ``agents/supervisor/tools/triage.py`` may assign
    ``triage_classification`` / ``triage_decision_id``, and only from the
    classifier's response — never from a literal.

Both directions are asserted. A synthetic module carrying the bad pattern MUST
trip the walker (otherwise the walker could be silently broken and every
assertion here would pass vacuously), and the real ``triage.py`` assignments
MUST be found (otherwise the walker is looking in the wrong place).
"""

from __future__ import annotations

import ast
import pathlib
import textwrap

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

# The two session fields that carry the deterministic verdict.
GUARDED_FIELDS = ("triage_classification", "triage_decision_id")

# The ONLY module permitted to assign them. It reads the classifier's HTTP
# response and assigns from `body.get(...)` — provenance is the classifier.
#
# This is a named allowlist, not a pattern. Adding an entry is a deliberate act
# that shows up in review, and `test_allowlist_entries_are_still_real` below
# fails if an entry stops being a genuine assigner — so a stale exemption
# cannot quietly widen the guard.
ALLOWED_ASSIGNERS = frozenset({"agents/supervisor/tools/triage.py"})

# Anything that looks like a hardcoded verdict.
_LITERAL_VERDICTS = {"P0", "P1", "P2", "P3"}


def _source_files() -> list[pathlib.Path]:
    """Non-test, non-vendored Python under the two source roots."""
    out: list[pathlib.Path] = []
    for root in ("agents", "lambdas"):
        for p in (REPO_ROOT / root).rglob("*.py"):
            s = str(p)
            if "__pycache__" in s or "/tests/" in s or "/test_" in s:
                continue
            if "/.venv/" in s or "/node_modules/" in s or "/cdk.out/" in s:
                continue
            out.append(p)
    return sorted(out)


def _find_guarded_assignments(tree: ast.AST) -> list[tuple[int, str, str, str]]:
    """Return (lineno, field, value_node_type, unparsed_value) per assignment.

    Covers plain ``x.f = v``, annotated ``x.f: T = v``, and walrus-free
    augmented forms. Tuple-unpacking targets are walked too, because
    ``a.f, b.g = "P3", "x"`` is the obvious way to slip past a naive check.
    """
    found: list[tuple[int, str, str, str]] = []

    def _targets(node: ast.AST):
        if isinstance(node, ast.Assign):
            yield from node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            yield node.target

    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            continue
        value = getattr(node, "value", None)
        if value is None:
            continue
        for target in _targets(node):
            # Unwrap tuple/list targets so multi-assignment is covered.
            leaves = (
                list(target.elts)
                if isinstance(target, (ast.Tuple, ast.List))
                else [target]
            )
            for leaf in leaves:
                if isinstance(leaf, ast.Attribute) and leaf.attr in GUARDED_FIELDS:
                    found.append(
                        (
                            node.lineno,
                            leaf.attr,
                            type(value).__name__,
                            ast.unparse(value),
                        )
                    )
    return found


class TestClassificationProvenance:
    """No module outside the allowlist may write the verdict fields."""

    def test_no_unauthorised_module_assigns_the_verdict(self):
        offenders: list[str] = []
        for path in _source_files():
            rel = str(path.relative_to(REPO_ROOT))
            if rel in ALLOWED_ASSIGNERS:
                continue
            try:
                tree = ast.parse(path.read_text())
            except SyntaxError:
                continue
            for lineno, field, _kind, val in _find_guarded_assignments(tree):
                offenders.append(f"{rel}:{lineno}  {field} = {val}")

        assert not offenders, (
            "Only the triage classifier may set the verdict fields. These "
            "assignments substitute a platform-invented priority for a "
            "classifier verdict, which is the 2026-09-23 staging defect:\n  "
            + "\n  ".join(offenders)
            + "\n\nIf triage is unavailable the correct behaviour is to FAIL "
            "CLOSED — leave the field None, tell the user the priority could "
            "not be verified, and do not book. See "
            "issues/2026-09-23-triage-401-llm-improvises-classification/."
        )

    def test_allowed_assigner_never_uses_a_literal_verdict(self):
        """Even the allowlisted module must derive the value, not hardcode it."""
        for rel in sorted(ALLOWED_ASSIGNERS):
            tree = ast.parse((REPO_ROOT / rel).read_text())
            for lineno, field, kind, val in _find_guarded_assignments(tree):
                if kind == "Constant":
                    literal = val.strip("\"'")
                    assert literal not in _LITERAL_VERDICTS, (
                        f"{rel}:{lineno} assigns {field} = {val} — a hardcoded "
                        "verdict even inside the allowlisted module is still a "
                        "platform-invented classification."
                    )


class TestWalkerIsNotVacuous:
    """If the walker silently stops matching, every test above passes for free.

    These two tests are the anti-vacuity floor: one proves the walker finds a
    known-bad pattern, the other proves it finds the known-good real code.
    """

    def test_walker_catches_the_exact_removed_pattern(self, tmp_path):
        # Verbatim shape of the two fail-opens removed on 2026-09-23.
        bad = textwrap.dedent(
            """
            def handler(ctx):
                if ctx.triage_classification is None:
                    ctx.triage_classification = "P3"
                    ctx.triage_decision_id = ctx.session_id
            """
        )
        found = _find_guarded_assignments(ast.parse(bad))
        fields = {f for _l, f, _k, _v in found}
        assert fields == {"triage_classification", "triage_decision_id"}, (
            "The walker must catch the exact pattern that was removed. If it "
            "does not, the guard above is vacuous and the defect can return."
        )

    def test_walker_catches_tuple_unpacked_assignment(self):
        # The obvious way to evade a naive single-target check.
        bad = "ctx.triage_classification, ctx.triage_decision_id = 'P3', ctx.session_id"
        found = _find_guarded_assignments(ast.parse(bad))
        assert len(found) == 2, (
            "The walker must unwrap tuple targets, or a two-line fail-open can "
            "be rewritten as one line to slip past it."
        )

    def test_walker_finds_the_real_legitimate_assignments(self):
        # Positive control against real code: triage.py genuinely assigns both
        # fields. If this returns nothing, the walker is pointed at the wrong
        # place (or triage.py was refactored and the allowlist is now stale).
        tree = ast.parse((REPO_ROOT / "agents/supervisor/tools/triage.py").read_text())
        found = _find_guarded_assignments(tree)
        fields = {f for _l, f, _k, _v in found}
        assert fields == {"triage_classification", "triage_decision_id"}, (
            "Expected triage.py to assign both verdict fields from the "
            f"classifier response; walker found {sorted(fields)}. Either the "
            "walker is broken or triage.py changed shape."
        )

    def test_allowlist_entries_are_still_real(self):
        """A stale allowlist entry silently widens the guard."""
        for rel in sorted(ALLOWED_ASSIGNERS):
            path = REPO_ROOT / rel
            assert path.exists(), f"allowlisted path {rel} no longer exists"
            found = _find_guarded_assignments(ast.parse(path.read_text()))
            assert found, (
                f"{rel} is allowlisted as a verdict assigner but no longer "
                "assigns those fields. Remove it from ALLOWED_ASSIGNERS — a "
                "stale exemption is an unguarded module."
            )


class TestBookRefusesWithoutAVerdict:
    """The enforcement point must stay reachable.

    ``call_book_endpoint`` raises when the verdict fields are unset. That guard
    was previously unreachable from the ``@tool`` path because ``book`` filled
    the fields in first — so the guard existed and never fired.
    """

    def test_guard_is_present_in_call_book_endpoint(self):
        src = (REPO_ROOT / "agents/supervisor/tools/book.py").read_text()
        tree = ast.parse(src)
        fn = next(
            (
                n
                for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "call_book_endpoint"
            ),
            None,
        )
        assert fn is not None, "call_book_endpoint not found in book.py"
        body_src = ast.unparse(fn)
        assert "triage_classification is None" in body_src, (
            "call_book_endpoint must still check that the verdict is set. This "
            "is the last line of defence between an unverified priority and a "
            "ticket on a real dealer queue."
        )
        assert "BookError" in body_src, (
            "call_book_endpoint's missing-verdict check must RAISE, not warn."
        )



# ---------------------------------------------------------------------------
# Escalation-severity guards (task 1.4 of spec
# 2026-09-23-cvx-escalation-recommend-not-act).
#
# Two mutations were run in wave 1 and NOT caught because these classes did
# not exist yet. Both are now pinned here:
#
#   (a) re-adding `severity` to escalate_to_human's @tool signature →
#       TestEscalateToolSignature.test_no_tool_in_tools_dir_accepts_severity_param
#
#   (b) injecting a severity instruction back into a persona string →
#       TestEscalatePersonaSeverity.test_no_persona_prompt_contains_severity_instruction
#
# Design rules (not to violate on future edits):
#
#  1. Scope to the signature or rule BODY — never a whole-file substring.
#     An unscoped `"severity" in source` passes on any file mentioning
#     the word (e.g. `call_escalate_endpoint`'s docstring). That exact
#     vacuity trap was caught twice in this spec and is the canonical
#     example in the task description.
#
#  2. `call_escalate_endpoint` is exempt by design: it keeps an explicit
#     `severity` parameter for the internal, classifier-gated caller at
#     `bidi_app.py:3919`. Asserting it fails closed would assert the wrong
#     contract. The guard belongs at the LLM-facing @tool boundary only.
#
#  3. `escalate_for_service` is also a @tool and may NOT acquire a severity
#     param. The test covers all @tool-decorated functions in tools/.
# ---------------------------------------------------------------------------

import re

_TOOLS_DIR = REPO_ROOT / "agents" / "supervisor" / "tools"
_PERSONA_DEFS = REPO_ROOT / "agents" / "supervisor" / "persona_definitions.py"

# Pattern that catches the exact shape of the old per-persona instruction
# that was removed by task 1.3: 'Use severity "P3"', 'severity "P0"', etc.
# Anchored to the P-level vocabulary — a general "severity" mention (e.g. in
# a tool-list blurb) is NOT a problem; a severity P-value instruction IS.
_PERSONA_SEVERITY_RE = re.compile(r'severity\s+["\']P[0-3]["\']', re.IGNORECASE)


def _find_tool_decorated_functions_with_severity(
    tree: ast.AST, path: pathlib.Path
) -> list[tuple[str, int]]:
    """Return (func_name, lineno) for every @tool-decorated function whose
    parameter list contains a 'severity' argument.

    Scoped deliberately: walks only @tool functions' *signatures*, not
    function bodies — so a tool body that calls an internal helper with
    severity= is not flagged, only LLM-visible parameter declarations.
    `call_escalate_endpoint` is a plain def with no @tool decorator, so
    it is structurally excluded without any allowlist.
    """
    hits: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        has_tool_deco = any(
            (isinstance(d, ast.Name) and d.id == "tool")
            or (isinstance(d, ast.Attribute) and d.attr == "tool")
            for d in node.decorator_list
        )
        if not has_tool_deco:
            continue
        param_names = [a.arg for a in node.args.args]
        if "severity" in param_names:
            hits.append((node.name, node.lineno))
    return hits


def _find_persona_severity_instructions(tree: ast.AST) -> list[tuple[str, int, str]]:
    """Return (var_name, lineno, matched_text) for every *_PROMPT string
    constant that contains a severity P-level instruction.

    Scoped to the BODY of each prompt string — the *_PROMPT variable
    assignments — so a comment line or import-time reference in the module
    header cannot produce a false positive.  The pattern matches only
    'severity "P0"' / 'severity "P3"' / ... shapes, not generic uses of
    the word severity.
    """
    hits: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not (isinstance(target, ast.Name) and target.id.endswith("_PROMPT")):
                continue
            if not (
                isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                continue
            m = _PERSONA_SEVERITY_RE.search(node.value.value)
            if m:
                hits.append((target.id, node.lineno, m.group()))
    return hits


class TestEscalateToolSignature:
    """No @tool-decorated function in tools/ may accept a ``severity`` param.

    D2 (spec): severity is removed from the LLM-facing tool signature
    entirely and read from session context.  An LLM that cannot name a
    severity cannot misname one.

    The exempt function is `call_escalate_endpoint` (plain def, no @tool
    decorator) — it keeps an explicit severity for the internal
    classifier-gated caller at bidi_app.py:3919.
    """

    def test_no_tool_in_tools_dir_accepts_severity_param(self):
        offenders: list[str] = []
        for path in sorted(_TOOLS_DIR.glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                tree = ast.parse(path.read_text())
            except SyntaxError:
                continue
            rel = str(path.relative_to(REPO_ROOT))
            for fn_name, lineno in _find_tool_decorated_functions_with_severity(
                tree, path
            ):
                offenders.append(f"{rel}:{lineno} @tool {fn_name}(severity=...)")

        assert not offenders, (
            "The following @tool-decorated functions expose a 'severity' "
            "parameter to the LLM. Per spec D2, severity must be read from "
            "ctx.triage_classification inside the body — never accepted as "
            "an LLM-supplied argument. Remove the parameter and read "
            "ctx.triage_classification directly:\n  "
            + "\n  ".join(offenders)
        )


class TestEscalatePersonaSeverity:
    """No persona definition string may instruct the model to pass a severity
    value to an escalation tool.

    D4 (spec): one escalation model for every persona — no per-persona
    escalation behaviour, severity conventions, or prompt clauses. The old
    'Use severity \"P3\". This is a sales enquiry ...' block in the retail
    persona (lines 669-679) was deleted by task 1.3 and must not return.
    """

    def test_no_persona_prompt_contains_severity_instruction(self):
        tree = ast.parse(_PERSONA_DEFS.read_text())
        hits = _find_persona_severity_instructions(tree)
        offenders = [
            f"persona_definitions.py:{lineno} {var}: contains {match!r}"
            for var, lineno, match in hits
        ]
        assert not offenders, (
            "The following persona prompt strings contain an escalation-severity "
            "instruction. Per spec D4, no persona may tell the model which "
            "P-level to use — severity is derived from ctx.triage_classification "
            "by the tool, never from a persona prompt. Remove the instruction:\n  "
            + "\n  ".join(offenders)
            + "\n\nIf the persona genuinely needs different escalation behaviour, "
            "that is a structural change (a new tool factory), not a prompt clause."
        )


class TestEscalateFailClosed:
    """``escalate_to_human`` (the @tool) must raise when
    ``ctx.triage_classification`` is absent.

    This is the guard at escalate.py:360 that was added by task 1.1.  It is
    the structural parallel to ``call_book_endpoint``'s existing guard (tested
    in ``TestBookRefusesWithoutAVerdict`` above), and the same reasoning
    applies: a guard that exists but is structurally unreachable provides no
    protection.

    Note: ``call_escalate_endpoint`` is NOT the target here — it deliberately
    keeps an explicit severity parameter for the internal caller.  The guard
    belongs at the ``@tool`` boundary, which is the only LLM-reachable path.
    """

    def test_escalate_to_human_body_checks_triage_classification(self):
        src = (REPO_ROOT / "agents" / "supervisor" / "tools" / "escalate.py").read_text()
        tree = ast.parse(src)

        # Find the escalate_to_human function (inner function inside make_escalate_tool).
        fn = next(
            (
                n
                for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "escalate_to_human"
            ),
            None,
        )
        assert fn is not None, (
            "escalate_to_human not found in escalate.py. If the function was "
            "renamed, update this test."
        )

        body_src = ast.unparse(fn)
        assert "triage_classification is None" in body_src, (
            "escalate_to_human must check that ctx.triage_classification is set "
            "before proceeding. Without this guard the tool can run — and reach "
            "the Connect endpoint — when no triage classifier verdict exists, "
            "which is the same class of defect as the book.py P3-default removed "
            "on 2026-09-23. See escalate.py:360."
        )
        assert "EscalateError" in body_src, (
            "escalate_to_human's missing-triage-classification check must RAISE "
            "EscalateError, not merely warn or log. A check that does not raise "
            "is not fail-closed."
        )


class TestEscalateGuardsAreNotVacuous:
    """Anti-vacuity floor for the two new walkers above.

    If either walker silently stops matching, every assertion in
    TestEscalateToolSignature and TestEscalatePersonaSeverity passes for free.
    These tests prove the walkers detect the exact patterns they are meant to
    catch, and that the real source files are found by them.
    """

    def test_tool_walker_catches_severity_in_tool_signature(self, tmp_path):
        """A synthetic @tool function with severity= MUST be flagged."""
        bad = """
from strands import tool

@tool
def escalate_to_human(reason: str, severity: str, summary: str = "") -> dict:
    pass
"""
        tree = ast.parse(bad)
        hits = _find_tool_decorated_functions_with_severity(
            tree, tmp_path / "bad_tool.py"
        )
        assert hits, (
            "The @tool-signature walker must detect a severity parameter. "
            "If it does not, TestEscalateToolSignature passes vacuously — "
            "the mutation (a) would not be caught."
        )
        names = {n for n, _l in hits}
        assert "escalate_to_human" in names, (
            f"Expected escalate_to_human in hits, got {names!r}"
        )

    def test_tool_walker_ignores_non_tool_functions_with_severity(self):
        """call_escalate_endpoint is a plain def — must NOT be flagged."""
        # Use a minimal excerpt that looks like the real call_escalate_endpoint.
        ok = """
def call_escalate_endpoint(ctx, config, *, severity: str, reason: str) -> dict:
    pass
"""
        tree = ast.parse(ok)
        hits = _find_tool_decorated_functions_with_severity(tree, pathlib.Path("."))
        assert not hits, (
            "call_escalate_endpoint has no @tool decorator and must NOT be "
            "flagged by the signature walker. Asserting it fails closed would "
            "assert the wrong contract — it is the internal, classifier-gated "
            "caller path, not the LLM-facing boundary."
        )

    def test_tool_walker_finds_real_tool_functions_in_escalate_py(self):
        """Positive control: walker must find the real @tool functions."""
        tree = ast.parse(
            (REPO_ROOT / "agents" / "supervisor" / "tools" / "escalate.py").read_text()
        )
        # The walker should find escalate_to_human and escalate_for_service.
        # Neither should have severity, but the walker must return them with
        # empty hit lists — that is, it must have *examined* them.
        # We verify by checking the function exists in the AST:
        tool_fns = [
            n.name
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef)
            and any(
                (isinstance(d, ast.Name) and d.id == "tool")
                or (isinstance(d, ast.Attribute) and d.attr == "tool")
                for d in n.decorator_list
            )
        ]
        assert "escalate_to_human" in tool_fns, (
            "escalate_to_human @tool not found in escalate.py. Either the "
            "function was renamed or the decorator was changed — update this test."
        )
        assert "escalate_for_service" in tool_fns, (
            "escalate_for_service @tool not found in escalate.py. Either the "
            "function was renamed or the decorator was changed — update this test."
        )

    def test_persona_walker_catches_severity_instruction_in_prompt_string(
        self, tmp_path
    ):
        """A synthetic *_PROMPT string with 'severity "P0"' MUST be flagged."""
        bad_src = textwrap.dedent(
            '''
            RETAIL_ADVISOR_PROMPT = """
            You are a retail advisor.
            For sales enquiries, call escalate_to_human. Use severity "P0".
            """
            '''
        )
        tree = ast.parse(bad_src)
        hits = _find_persona_severity_instructions(tree)
        assert hits, (
            "The persona-severity walker must detect 'severity \"P0\"' inside "
            "a *_PROMPT string constant. If it does not, "
            "TestEscalatePersonaSeverity passes vacuously — mutation (b) would "
            "not be caught."
        )
        vars_hit = {v for v, _l, _m in hits}
        assert "RETAIL_ADVISOR_PROMPT" in vars_hit, (
            f"Expected RETAIL_ADVISOR_PROMPT in hits, got {vars_hit!r}"
        )

    def test_persona_walker_finds_real_prompt_constants(self):
        """Positive control: the real persona_definitions.py must be parseable
        and must contain at least one *_PROMPT entry (otherwise the guard
        would trivially pass if the file were accidentally emptied)."""
        tree = ast.parse(_PERSONA_DEFS.read_text())
        prompt_vars = [
            target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name) and target.id.endswith("_PROMPT")
        ]
        assert len(prompt_vars) >= 5, (
            f"Expected at least 5 *_PROMPT entries in persona_definitions.py; "
            f"found {len(prompt_vars)}: {prompt_vars}. Either the file is "
            "incomplete or the walker is looking in the wrong place."
        )
