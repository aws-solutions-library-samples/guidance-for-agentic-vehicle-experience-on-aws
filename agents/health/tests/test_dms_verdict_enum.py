"""Pin every CVX-side warranty-verdict fixture against DMS's real enum.

Spec: `.kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/tasks.md` § Fix Group 19.
Issue: `issues/2026-09-23-warranty-verdict-enum-fabricated-in-cvx-narration-guard/`.

## What this file is for

`agents/health/dms_verdicts.py` is a hand-restated mirror of DMS's verdict enum — CVX
cannot import from the DMS repo. A mirror with no consumer is just a comment. This file is
what makes the mirror load-bearing: it scans every verdict-phrase site under
`agents/health/tests/` and fails if any uses a value DMS cannot emit.

That is precisely the defect this Fix Group closes. Before it, 22 of 29 verdict sites used
`declined`, which appears in zero DMS source files, and two test bodies each hardcoded their
own frozenset asserting `declined` was canonical. The suite was green throughout.

## Why the scan is AST-based, and selects by position rather than by token

A scan for the bare token `declined` would be wrong in both directions. `declined` is a
legitimate AVX **Action** terminal status
(`lambdas/api-avx-actions/handler.py:104-112`), so a token-level scan across the repo either
false-positives on the Action domain or has to exempt it — and an exemption is not an
assertion. The scan therefore selects only values in *verdict-phrase position*: the
`warranty_verdict_phrase=` keyword argument, the first argument of
`assert_coverage_verdict_verbatim`, and `_VERDICT_*` module constants.

It reads those positions from the **AST**, not with regexes over raw text. A text scan has a
structural false-positive: any docstring or comment that *names* a bad value — for example
one explaining why a fixture was changed — trips it. That is not hypothetical; the
regex-based first draft of this file failed against its own explanatory docstring in
`test_coverage_verdict_verbatim.py`. Since the thing being guarded is what the tests
*execute*, the AST is the correct surface: prose about a value is not a use of it.

## Anti-vacuity

A scan that matches nothing passes. `test_scan_finds_the_expected_fixture_population`
asserts a floor and `test_every_selector_matches_at_least_once` asserts each selector is
individually productive, so a selector broken by a future refactor fails loudly instead of
silently measuring itself. The floor is 22 against a population of 29, leaving real margin
for legitimately deleting a test — see `_MIN_EXPECTED_SITES` for why the first draft's
margin was one site, not four.

## Three layers, because a position scan only covers shapes someone thought of

`TestVerdictFixturesUseRealValues` scans verdict *positions* in the tests tree and so catches
any out-of-enum value, including fabrications nobody has invented yet.
`test_the_fabricated_tokens_appear_nowhere_as_string_literals` scans the tests tree for the
two *known* fabricated tokens in any **single string literal**, whatever position it occupies.

That boundary is deliberate and worth stating precisely, because an earlier draft of this
docstring claimed "any AST shape at all" and Cycle 3 showed that to be false. A token
assembled at runtime defeats both layers — `warranty_verdict_phrase="decl" + "ined"` passes
the position scan (selector 1 requires an `ast.Constant`) *and* the token scan (neither
operand equals a fabricated token). So do `.join()`, `%`-formatting, and bytes literals.
Accepted as residual rather than closed: constant-folding arbitrary expressions is a large
amount of machinery to defend against a construction nobody writes by accident, and the
defect this Fix Group closes was an honest mistake, not an evasion. Recorded so the next
reader knows the limit rather than trusting an overclaim.
`test_the_fabricated_tokens_appear_nowhere_in_module_source` scans `agents/health/` excluding
tests — the surface that reaches the live model — with substring matching, because prompt
text embeds the token in a sentence rather than passing it bare.

Each layer was added because a mutation defeated the previous set: Cycle 1 found the position
scan blind to verdicts reached through a loop variable (now selector 4), Cycle 2 found the
tests-tree backstop blind to a different function in an exempt file and to subdirectories,
and Cycle 3 found the source scan itself non-recursive and waving through docstrings that are
runtime-readable via `__doc__`. Both trees are now walked recursively, and both kinds of
exemption — token sites and docstring sites — are keyed individually with a stated reason and
an anti-vacuity test.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from agents.health.dms_verdicts import (
    DMS_VERDICT_APPROVED,
    DMS_VERDICT_DENIED,
    DMS_VERDICT_ENUM,
    DMS_VERDICT_REQUIRES_REVIEW,
)

# ---------------------------------------------------------------------------
# Scan configuration
# ---------------------------------------------------------------------------

_TESTS_DIR = pathlib.Path(__file__).parent

#: Selector names, used for per-selector anti-vacuity reporting.
_SEL_KEYWORD = "warranty_verdict_phrase= keyword"
_SEL_POSITIONAL = "assert_coverage_verdict_verbatim 1st arg"
_SEL_CONSTANT = "_VERDICT_* module constant"
_SEL_LOOP = "verdict literal in a loop collection"

_SELECTOR_NAMES: tuple[str, ...] = (
    _SEL_KEYWORD,
    _SEL_POSITIONAL,
    _SEL_CONSTANT,
    _SEL_LOOP,
)

#: The guard whose first positional argument is a verdict phrase.
_GUARD_FN_NAME = "assert_coverage_verdict_verbatim"

#: Values that are deliberately OUTSIDE the DMS enum, with the reason each is exempt.
#:
#: This is a named allowlist, not a blanket suppression — every entry is a specific test
#: construction that depends on the value being out-of-vocabulary. Three anti-vacuity tests
#: below prove each entry is still a real candidate, that the allowlist has not quietly
#: grown to cover every site, and that it does not overlap the enum.
_DELIBERATE_NON_VERDICTS: dict[str, str] = {
    "": (
        "The empty-phrase case: the guard raises ValueError on a falsy phrase. "
        "Not a verdict, and must never be added to the enum to satisfy this scan."
    ),
    "service required": (
        "TestGuardIsAgentAgnostic::test_guard_callable_with_any_non_empty_phrase — "
        "a deliberately non-DMS phrase proving the guard is not hard-coded to any enum, "
        "which is what lets Agent 3 (Spec 1b) reuse it unchanged."
    ),
}

#: The two values DMS never emits. Named once; used by both backstop layers.
_FABRICATED_TOKENS: tuple[str, ...] = ("declined", "under_review")

#: Directory holding the module's source (not tests) — `agent.py`, `prompt.py`, etc.
_SOURCE_DIR = _TESTS_DIR.parent

#: Site-level exemptions for the token backstop, keyed by (filename, enclosing function).
#:
#: Keyed by **function**, not by file. Review Cycle 2 (M8 route A) showed a file-level key
#: exempted `test_coverage_verdict_verbatim.py` wholesale — the module's richest
#: verdict-fixture file — so a fabricated token could be reintroduced anywhere in it and stay
#: invisible. Function-granularity is precise to the one legitimate site and stable under
#: line drift, which a lineno key would not be.
_TOKEN_BACKSTOP_EXEMPT_SITES: dict[tuple[str, str], str] = {
    ("test_coverage_verdict_verbatim.py", "test_guard_does_not_read_prose"): (
        "Lists 'declined' as a paraphrase the guard MUST reject — the whole point being "
        "that it is not a DMS verdict, and that it is the most likely softening given the "
        "adjacent AVX Action vocabulary. See that test's docstring."
    ),
    ("test_dms_warranty_source.py", "test_out_of_enum_verdict_is_a_hard_error"): (
        "Fix Group 21 task 1. Parametrized negative inputs proving the evidence source "
        "REJECTS out-of-enum verdicts rather than passing them through — 'declined' and "
        "'under_review' are there precisely because they are the two known fabrications, "
        "so they are the highest-value cases to prove rejected. The test also asserts each "
        "input is absent from DMS_VERDICT_ENUM before using it, so it cannot silently "
        "become a positive fixture."
    ),
    (
        "test_dms_warranty_source.py",
        "test_verdict_error_names_the_offending_value_and_the_two_causes",
    ): (
        "Fix Group 21 task 1. Asserts the raised DmsWarrantyVerdictError names the rejected "
        "value, so an operator reading the log learns which verdict was refused. Requires "
        "the fabricated token as both the input and the expected substring."
    ),
    (
        "test_run_health_agent_verdict_pass.py",
        "test_verdict_is_narrated_verbatim_not_softened",
    ): (
        "Fix Group 21 task 2. Asserts the narration does NOT contain 'declined' — the "
        "softening the adjacent AVX Action vocabulary makes most likely once a verdict "
        "actually reaches the narration path. Naming it is required to assert its absence."
    ),
    (
        "test_dms_warranty_source_live.py",
        "test_narration_carries_the_real_verdict_verbatim",
    ): (
        "Fix Group 21 task 4 (live verification). Same assertion as the unit-level sibling "
        "above, but against a verdict read from real DynamoDB: the narration must not soften "
        "'denied' into 'declined'. Live is where it matters most — the unit test proves the "
        "guard works on a literal the author chose, this one proves it on bytes DMS's table "
        "actually returned."
    ),
}


#: Docstring sites in module source that legitimately name the fabricated tokens, keyed by
#: (filename, enclosing scope).
#:
#: The source-tree scan skips docstrings on the principle that prose about a value is not a
#: use of it. Review Cycle 3 (M9/W2) pointed out that this is not quite true: a docstring is
#: runtime-readable via ``__doc__``, so a function whose docstring teaches ``declined`` and
#: returns ``__doc__`` is a live-model route that the exclusion waves through. Not contrived
#: as an accident either — the three existing prompt constants are long triple-quoted strings
#: that become docstrings if moved to a function head.
#:
#: So the exclusion is now an allowlist rather than a blanket rule: these two sites are
#: exempt, and any *other* docstring naming a fabricated token is an offender.
_SOURCE_DOCSTRING_EXEMPT_SITES: dict[tuple[str, str], str] = {
    ("agent.py", "CoverageVerdictVerbatimError"): (
        "Design rule 2 names both fabricated values to warn against reverting the "
        "Fix Group 19 correction — the 'do not reword it back' pattern used elsewhere in "
        "this spec. Adjacent to the correct enum and its citation."
    ),
    ("dms_verdicts.py", "<module>"): (
        "The module docstring documents the two values it exists to exclude, and the "
        "two-adjacent-enums root cause. Naming them is the point."
    ),
}


def _docstring_sites(tree: ast.AST) -> dict[int, str]:
    """Map docstring Constant node id -> the name of the scope it documents.

    Returns the owning scope's name (`<module>`, a class name, or a function name) so callers
    can allowlist specific docstring sites rather than skipping docstrings wholesale. See
    `_SOURCE_DOCSTRING_EXEMPT_SITES` for why wholesale was wrong (Cycle 3 / M9 W2).
    """
    sites: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    scope = getattr(node, "name", "<module>")
                    sites[id(body[0].value)] = scope
    return sites


def _enclosing_functions(tree: ast.AST) -> dict[int, str]:
    """Map Constant node id -> nearest enclosing function name (or '<module>')."""
    owner: dict[int, str] = {}

    def walk(node: ast.AST, current: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = (
                child.name
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                else current
            )
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                owner[id(child)] = current
            walk(child, name)

    walk(tree, "<module>")
    return owner


def _fabricated_literals(
    path: pathlib.Path, *, skip_docstrings: bool, substring: bool = False
):
    """Yield (lineno, value, enclosing_function) for fabricated-token literals.

    *substring* selects the matching mode, and the two trees genuinely need different ones:

    - **Tests tree — exact match.** Fixtures pass a bare token (``warranty_verdict_phrase=
      "declined"``), so exact matching is precise and avoids flagging an assertion message
      that merely mentions a token inside a longer sentence.
    - **Source tree — substring.** Prompt text embeds the token in prose ("if the verdict is
      declined, say declined"), so exact matching cannot see it. Review Cycle 2's M8 route C
      was caught by the position scan's absence and missed by the first draft of the source
      scan for exactly this reason: a second prompt clause teaching ``declined`` passed
      18/18 green, on the one surface that reaches the live model.

    Substring mode is only safe because docstring *sites* are allowlisted rather than skipped
    wholesale, and because it was verified to produce zero hits against the current source —
    `agent.py`'s error message says "under review" with a space, deliberately not
    ``under_review``.

    When *skip_docstrings* is true, a docstring is skipped only if its (filename, scope) is in
    `_SOURCE_DOCSTRING_EXEMPT_SITES`; every other docstring is reported, because a docstring is
    runtime-readable via ``__doc__`` (Cycle 3 / M9 W2).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstring_sites = _docstring_sites(tree) if skip_docstrings else {}
    owners = _enclosing_functions(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docstring_sites:
            scope = docstring_sites[id(node)]
            if (path.name, scope) in _SOURCE_DOCSTRING_EXEMPT_SITES:
                continue
        if substring:
            hit = any(token in node.value for token in _FABRICATED_TOKENS)
        else:
            hit = node.value in _FABRICATED_TOKENS
        if not hit:
            continue
        yield node.lineno, node.value, owners.get(id(node), "<module>")


def _test_files() -> list[pathlib.Path]:
    """Every test module under the tests tree, recursively.

    `rglob`, not `glob`. Review Cycle 2 (M8 route B): pytest recurses into subdirectories
    and a non-recursive glob did not, so a bare `declined` in verdict position inside
    `agents/health/tests/warranty/` would have been collected by pytest and missed by both
    layers — the author's own M1 mutation passing purely by living one directory down.
    """
    return sorted(
        p
        for p in _TESTS_DIR.rglob("*.py")
        if "__pycache__" not in p.parts and p.name != pathlib.Path(__file__).name
    )


def _source_files() -> list[pathlib.Path]:
    """Every module-source file under `agents/health/`, recursively, excluding tests.

    Recursive for the same reason `_test_files` is, and fixed for the same reason twice:
    review Cycle 3 (M9/W1) found that Cycle 2 applied `rglob` to the tests tree and left
    `_SOURCE_DIR.glob("*.py")` non-recursive here. That is M8 route B again, on the **more**
    consequential surface — route B planted a bad fixture, whereas a new
    `agents/health/sources/warranty_prompt.py` teaching `declined` plants an executable
    literal in module source, the surface Cycle 2 itself called load-bearing. Verified: it
    gave 365 passed / 0 failed before this fix.

    The tests tree is excluded because it has its own layer with different matching rules
    (exact, plus site-level exemptions for the legitimate paraphrase fixture). Including it
    here would double-report and would false-positive on that exemption.
    """
    return sorted(
        p
        for p in _SOURCE_DIR.rglob("*.py")
        if "__pycache__" not in p.parts and "tests" not in p.parts
    )


#: Floor for the anti-vacuity check.
#:
#: 29 sites exist as of review Cycle 1 (17 keyword + 6 positional + 3 constant + 3 loop).
#: Set to 22, leaving 7 sites of margin so that legitimately deleting a verdict test does
#: not fail this file for the wrong reason.
#:
#: Corrected in Cycle 1 (W2): the first draft claimed 29 sites and set the floor at 25.
#: The 29 was an artefact of the regex draft double-counting across selectors — the AST
#: scan found 26, against both HEAD and the change, so the margin was 1, not 4. A floor
#: one site below the population would have tripped on the next deleted test, which is
#: precisely the failure the docstring claimed to avoid. The count is 29 again only
#: because selector 4 legitimately added 3.
_MIN_EXPECTED_SITES = 22


class _VerdictSiteVisitor(ast.NodeVisitor):
    """Collect string literals appearing in verdict-phrase position.

    Four selectors. The first three read a literal sitting *directly* at a verdict
    position. The fourth exists because that is not the only way a verdict reaches the
    parameter: a test may hold verdicts in a collection literal and pass them through a
    loop variable, in which case the position holds an ``ast.Name`` and the literals are
    one indirection away. Review Cycle 1 (W1) found exactly that at
    ``test_post_service_visit.py``'s ``test_multiple_distinct_verdicts_exercise_different_phrases``
    — the module's only collection-of-verdicts, and the one place where reintroducing
    ``declined`` produced no signal at all.
    """

    def __init__(self) -> None:
        self.sites: list[tuple[int, str, str]] = []  # (lineno, selector, value)
        #: name -> collection literal, for resolving loop iterables. Populated by
        #: visit_Assign / visit_AnnAssign, which run before visit_For at module scope
        #: only if the assignment precedes the loop — so _resolve also accepts the
        #: literal inline, which is the robust case.
        self._collections: dict[str, ast.expr] = {}

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _is_verdict_kw(kw: ast.keyword) -> bool:
        return kw.arg in ("warranty_verdict_phrase", "verdict_phrase")

    def _record(self, node: ast.AST, selector: str, value: str) -> None:
        self.sites.append((getattr(node, "lineno", 0), selector, value))

    def _note_collection(self, targets: list[ast.expr], value: ast.expr | None) -> None:
        if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
            for target in targets:
                if isinstance(target, ast.Name):
                    self._collections[target.id] = value

    def _strings_for_target_index(
        self, collection: ast.expr, index: int | None
    ) -> list[ast.Constant]:
        """Extract string constants bound to a loop target.

        *index* is ``None`` for a plain ``for x in [...]`` target, or the tuple position
        for ``for a, b in [(...), ...]``.
        """
        out: list[ast.Constant] = []
        elements = getattr(collection, "elts", [])
        for element in elements:
            if index is None:
                if isinstance(element, ast.Constant) and isinstance(element.value, str):
                    out.append(element)
            elif isinstance(element, (ast.Tuple, ast.List)) and len(element.elts) > index:
                candidate = element.elts[index]
                if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str):
                    out.append(candidate)
        return out

    # -- visitors ---------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        # Selector 1: any call passing warranty_verdict_phrase="..."
        for kw in node.keywords:
            if kw.arg == "warranty_verdict_phrase" and isinstance(kw.value, ast.Constant):
                if isinstance(kw.value.value, str):
                    self._record(kw.value, _SEL_KEYWORD, kw.value.value)

        # Selector 2: assert_coverage_verdict_verbatim's verdict phrase, whether
        # passed positionally or by its `verdict_phrase=` keyword.
        called = node.func
        name = (
            called.attr
            if isinstance(called, ast.Attribute)
            else called.id
            if isinstance(called, ast.Name)
            else None
        )
        if name == _GUARD_FN_NAME:
            if node.args and isinstance(node.args[0], ast.Constant):
                first = node.args[0]
                if isinstance(first.value, str):
                    self._record(first, _SEL_POSITIONAL, first.value)
            for kw in node.keywords:
                if kw.arg == "verdict_phrase" and isinstance(kw.value, ast.Constant):
                    if isinstance(kw.value.value, str):
                        self._record(kw.value, _SEL_POSITIONAL, kw.value.value)

        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        # Selector 3: _VERDICT_* = "..." module/class constants.
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.startswith("_VERDICT_"):
                    self._record(node.value, _SEL_CONSTANT, node.value.value)
        self._note_collection(node.targets, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """Annotated assignment — the form `dms_verdicts.py` itself uses.

        Review Cycle 1 (W1, second instance): `visit_Assign` does not fire for
        `x: T = ...`, so an annotated `_VERDICT_* ` constant or an annotated collection
        was invisible.
        """
        if (
            isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and isinstance(node.target, ast.Name)
            and node.target.id.startswith("_VERDICT_")
        ):
            self._record(node.value, _SEL_CONSTANT, node.value.value)
        self._note_collection([node.target], node.value)
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        """Selector 4: verdict literals reached through a loop variable.

        Only fires when a loop-target name is actually used in verdict position inside
        the body — so an unrelated loop over unrelated strings is not collected.
        """
        # Which names does this loop bind, and at which tuple index?
        bound: dict[str, int | None] = {}
        if isinstance(node.target, ast.Name):
            bound[node.target.id] = None
        elif isinstance(node.target, (ast.Tuple, ast.List)):
            for i, element in enumerate(node.target.elts):
                if isinstance(element, ast.Name):
                    bound[element.id] = i

        # Which of them are used in verdict position in the body?
        used_in_verdict_position: set[str] = set()
        for descendant in ast.walk(node):
            if not isinstance(descendant, ast.Call):
                continue
            for kw in descendant.keywords:
                if self._is_verdict_kw(kw) and isinstance(kw.value, ast.Name):
                    if kw.value.id in bound:
                        used_in_verdict_position.add(kw.value.id)
            called = descendant.func
            fn_name = (
                called.attr
                if isinstance(called, ast.Attribute)
                else called.id
                if isinstance(called, ast.Name)
                else None
            )
            if fn_name == _GUARD_FN_NAME and descendant.args:
                first = descendant.args[0]
                if isinstance(first, ast.Name) and first.id in bound:
                    used_in_verdict_position.add(first.id)

        if used_in_verdict_position:
            iterable = node.iter
            if isinstance(iterable, ast.Name):
                iterable = self._collections.get(iterable.id)  # type: ignore[assignment]
            if iterable is not None:
                for loop_name in used_in_verdict_position:
                    for const in self._strings_for_target_index(iterable, bound[loop_name]):
                        self._record(const, _SEL_LOOP, const.value)

        self.generic_visit(node)


def _scan_verdict_sites() -> list[tuple[pathlib.Path, int, str, str]]:
    """Return every verdict-phrase-position literal under this tests directory.

    Reads the AST, so values named only in docstrings or comments are correctly
    ignored — prose about a value is not a use of it.

    Returns
    -------
    list of (path, line_number, selector_name, value)
    """
    found: list[tuple[pathlib.Path, int, str, str]] = []
    for path in _test_files():
        visitor = _VerdictSiteVisitor()
        visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        for lineno, selector, value in visitor.sites:
            found.append((path, lineno, selector, value))
    return found


# ---------------------------------------------------------------------------
# The enum itself
# ---------------------------------------------------------------------------


class TestDmsVerdictEnum:
    """The mirror matches DMS's source of truth."""

    def test_enum_has_exactly_the_three_dms_values(self):
        """Transcribed from DMS `source/_lib/warranty_rules.py:79`.

        Written as an equality, not a superset check: an extra value here would be a
        second fabricated verdict, which is the defect this Fix Group closes.
        """
        assert DMS_VERDICT_ENUM == frozenset({"approved", "denied", "requires_review"}), (
            "DMS_VERDICT_ENUM has drifted from the DMS source of truth "
            "(source/_lib/warranty_rules.py:79 — 'approved' | 'denied' | 'requires_review')."
        )

    def test_named_constants_are_members(self):
        """The convenience constants dereference real members."""
        for name, value in (
            ("DMS_VERDICT_APPROVED", DMS_VERDICT_APPROVED),
            ("DMS_VERDICT_DENIED", DMS_VERDICT_DENIED),
            ("DMS_VERDICT_REQUIRES_REVIEW", DMS_VERDICT_REQUIRES_REVIEW),
        ):
            assert value in DMS_VERDICT_ENUM, f"{name}={value!r} is not in DMS_VERDICT_ENUM."

    def test_the_fabricated_values_are_absent(self):
        """The two invented values must never reappear in the enum.

        `declined` is AVX **Action** vocabulary
        (`lambdas/api-avx-actions/handler.py:104-112`), not a DMS verdict. `under_review`
        was invented outright; DMS spells it `requires_review`.

        Pinned by value rather than left implicit in the equality test above, so that a
        future edit widening the enum fails with a message naming the specific
        two-adjacent-enums confusion that caused the original defect.
        """
        for fabricated, real in (("declined", "denied"), ("under_review", "requires_review")):
            assert fabricated not in DMS_VERDICT_ENUM, (
                f"{fabricated!r} is not a DMS warranty verdict — DMS emits {real!r}. "
                "See issues/2026-09-23-warranty-verdict-enum-fabricated-in-cvx-"
                "narration-guard/report.md."
            )


# ---------------------------------------------------------------------------
# The fixture scan — the actual prevention
# ---------------------------------------------------------------------------


class TestVerdictFixturesUseRealValues:
    """Every verdict-phrase site in `agents/health/tests/` uses a value DMS can emit."""

    def test_no_fixture_uses_an_out_of_enum_verdict(self):
        """MUTATION TARGET.

        Reintroduce `warranty_verdict_phrase="declined"` anywhere under
        `agents/health/tests/` and this test MUST fail, naming the file and line.
        """
        offenders = [
            (path, line_no, selector, value)
            for path, line_no, selector, value in _scan_verdict_sites()
            if value not in DMS_VERDICT_ENUM and value not in _DELIBERATE_NON_VERDICTS
        ]
        assert not offenders, (
            "Verdict-phrase sites use values DMS cannot emit:\n"
            + "\n".join(
                f"  {path.name}:{line_no} [{selector}] {value!r}"
                for path, line_no, selector, value in offenders
            )
            + f"\n\nDMS emits exactly {sorted(DMS_VERDICT_ENUM)} "
            "(source/_lib/warranty_rules.py:79). Fix the fixture — do NOT add the value "
            "to DMS_VERDICT_ENUM or to _DELIBERATE_NON_VERDICTS to make this pass."
        )

    def test_scan_finds_the_expected_fixture_population(self):
        """ANTI-VACUITY. MUTATION TARGET.

        Break the AST visitor so it collects nothing and this test MUST fail. Without it,
        `test_no_fixture_uses_an_out_of_enum_verdict` would pass on an empty scan and
        measure its own visitor rather than the fixtures.
        """
        sites = _scan_verdict_sites()
        assert len(sites) >= _MIN_EXPECTED_SITES, (
            f"Verdict-site scan found only {len(sites)} sites, expected "
            f">= {_MIN_EXPECTED_SITES}. A selector regex has probably gone stale against a "
            "refactor, which would make the out-of-enum check vacuous."
        )

    def test_every_selector_matches_at_least_once(self):
        """ANTI-VACUITY, per-selector.

        The population floor above can be met by one productive selector while another is
        silently dead. Each selector must earn its place.
        """
        sites = _scan_verdict_sites()
        for selector_name in _SELECTOR_NAMES:
            matched = [s for s in sites if s[2] == selector_name]
            assert matched, (
                f"Selector {selector_name!r} matched nothing. Either the construction it "
                "targets was refactored away (delete the selector) or the AST visitor is "
                "stale (fix it) — a dead selector silently narrows the scan."
            )


class TestAllowlistIsNotVacuous:
    """The deliberate-non-verdict allowlist is load-bearing, not a blanket suppression."""

    def test_each_allowlist_entry_is_still_a_real_candidate(self):
        """An allowlist entry for a value no longer present is dead weight.

        Dead entries are how an allowlist grows into a suppression: nobody prunes them, and
        the next person reads the list as "these are fine" rather than "these are exempt for
        a stated reason".
        """
        present = {value for _p, _l, _s, value in _scan_verdict_sites()}
        for exempt in _DELIBERATE_NON_VERDICTS:
            assert exempt in present, (
                f"Allowlist entry {exempt!r} no longer appears at any verdict site — "
                "remove it so the allowlist keeps naming only live exemptions."
            )

    def test_allowlist_does_not_cover_every_site(self):
        """If the allowlist covered every site the scan would be decorative.

        This is the mutation-resistance check on the allowlist itself: adding entries until
        the scan passes must not be a viable way to close a failure.
        """
        sites = _scan_verdict_sites()
        real = [s for s in sites if s[3] in DMS_VERDICT_ENUM]
        assert real, (
            "No verdict site uses a real DMS enum value — the allowlist has swallowed the "
            "entire scan and the out-of-enum check proves nothing."
        )
        assert len(real) > len(_DELIBERATE_NON_VERDICTS), (
            f"Only {len(real)} site(s) use real enum values against "
            f"{len(_DELIBERATE_NON_VERDICTS)} allowlisted exemptions — the exemptions are "
            "carrying more of the scan than the assertions are."
        )

    def test_enum_and_allowlist_are_disjoint(self):
        """A value cannot be both a real verdict and a deliberate non-verdict.

        Overlap would mean the allowlist is masking a real value, so removing the
        allowlist entry would not change the scan's verdict — an exemption that cannot
        be tested is an exemption that should not exist.
        """
        overlap = DMS_VERDICT_ENUM & set(_DELIBERATE_NON_VERDICTS)
        assert not overlap, (
            f"Values are in both DMS_VERDICT_ENUM and _DELIBERATE_NON_VERDICTS: {overlap}. "
            "An allowlist entry for a real verdict masks nothing and hides intent."
        )

    def test_the_fabricated_values_can_never_be_allowlisted(self):
        """MUTATION TARGET. Closes the one way to defeat the fixture scan.

        The scan can be satisfied two ways: fix the fixture, or allowlist the value.
        Reintroducing `declined` *and* adding it to `_DELIBERATE_NON_VERDICTS` would pass
        every other check in this file — the value is genuinely present, so
        `test_each_allowlist_entry_is_still_a_real_candidate` is satisfied; real enum values
        still exist elsewhere, so `test_allowlist_does_not_cover_every_site` is satisfied.

        `test_no_fixture_uses_an_out_of_enum_verdict`'s error message tells the reader not to
        do this, and a rule that exists only as prose in an error message is a rule that gets
        crossed. This makes it executable: the two known-fabricated values are never
        legitimate exemptions, because they are not merely out-of-enum — they are the
        specific values that caused the defect.

        Add `"declined"` to `_DELIBERATE_NON_VERDICTS` and this test MUST fail.
        """
        for fabricated, real in (("declined", "denied"), ("under_review", "requires_review")):
            assert fabricated not in _DELIBERATE_NON_VERDICTS, (
                f"{fabricated!r} is in _DELIBERATE_NON_VERDICTS. It is not a legitimate "
                f"exemption — it is the fabricated value Fix Group 19 removed, and DMS "
                f"spells this {real!r}. Fix the fixture instead of exempting it."
            )


# ---------------------------------------------------------------------------
# The prompt clause — the one surface that reaches the live model
# ---------------------------------------------------------------------------


class TestPromptClauseUsesRealVocabulary:
    """`WARRANTY_VERDICT_PROMPT_CLAUSE` is injected into the live system prompt.

    This is the load-bearing surface of the whole defect. The other four are documentation
    and fixtures; this one teaches the model. While it carried `declined` as its only worked
    example, a model receiving a real `denied` had been instructed — in-context, by example —
    to emit `declined`, which the verbatim guard would then correctly reject. The defect
    degraded every real denial.
    """

    def test_clause_does_not_teach_the_fabricated_vocabulary(self):
        """MUTATION TARGET.

        Put `declined` back into the clause and this test MUST fail.
        """
        from agents.health.prompt import WARRANTY_VERDICT_PROMPT_CLAUSE as clause

        for fabricated, real in (("declined", "denied"), ("under_review", "requires_review")):
            assert fabricated not in clause, (
                f"WARRANTY_VERDICT_PROMPT_CLAUSE contains {fabricated!r}, which DMS never "
                f"emits — it teaches the model a vocabulary the producer does not have. "
                f"DMS spells this {real!r}."
            )

    def test_clause_names_the_real_denial_verdict(self):
        """The clause needs a worked example, and it must be a real one.

        Asserting presence rather than only absence: a clause stripped of examples would
        pass the absence test above while losing the instruction's clarity.
        """
        from agents.health.prompt import WARRANTY_VERDICT_PROMPT_CLAUSE as clause

        assert DMS_VERDICT_DENIED in clause, (
            f"WARRANTY_VERDICT_PROMPT_CLAUSE must use {DMS_VERDICT_DENIED!r} as its worked "
            "example — the verdict whose verbatim narration matters most."
        )

    def test_clause_still_pins_verbatim_narration(self):
        """Regression guard on the pre-existing invariant.

        Fix Group 19 edits this clause's vocabulary; it must not weaken the instruction.
        """
        from agents.health.prompt import WARRANTY_VERDICT_PROMPT_CLAUSE as clause

        assert any(tok in clause.lower() for tok in ("verbatim", "exact", "word for word")), (
            "WARRANTY_VERDICT_PROMPT_CLAUSE no longer contains an unambiguous "
            "verbatim-narration instruction."
        )

    def test_clause_retains_the_paraphrase_denylist(self):
        """`rejected` is a forbidden paraphrase and must stay forbidden.

        It is not in DMS's enum either, which makes its presence in the denylist more
        correct after this Fix Group, not less. Keeping this pinned stops a well-meaning
        cleanup from deleting the denylist along with the fabricated values.
        """
        from agents.health.prompt import WARRANTY_VERDICT_PROMPT_CLAUSE as clause

        assert "rejected" in clause, (
            "The paraphrase denylist lost 'rejected' — it is the most plausible softening "
            "of 'denied' and is not a DMS verdict."
        )


class TestNoHardcodedVerdictEnumsRemain:
    """The two in-test frozensets that each hardcoded the fabricated enum are gone."""

    def test_the_fabricated_tokens_appear_nowhere_as_string_literals(self):
        """MUTATION TARGET. Token-level backstop for the position-based scan, tests tree.

        Added in review Cycle 1 (W1), hardened in Cycle 2 (M8 routes A and B). The
        position-based scan reads literals at verdict positions, and Cycle 1 found a real
        shape it could not see: verdicts held in a collection literal and passed through a
        loop variable. Selector 4 now covers that specific shape — but the lesson
        generalises, because the position scan can only cover shapes someone thought of. An
        f-string, a dict value, a concatenation, a ``@pytest.mark.parametrize`` argument, or
        a helper's default would each need another selector.

        This test takes the complementary approach: the two **known-fabricated** tokens must
        not appear as a bare string literal anywhere under ``agents/health/tests/``, in any
        **single string literal**, whatever position it occupies. That boundary is exact and
        deliberate — see this module's docstring for the runtime-assembly constructions
        (``"decl" + "ined"``, ``.join()``, ``%``-formatting, bytes) that both layers accept as
        residual, and why.

        A token-level scan is only safe here because of scope. Repo-wide it would be wrong —
        ``declined`` is a legitimate AVX **Action** terminal status in ``agents/executors/``
        and ``lambdas/api-avx-actions/`` — but ``agents/health/`` has no Action-status
        surface, verified by census at scoping time (all 109 pre-fix occurrences were
        warranty-domain) and re-verified independently in Cycle 2.

        Two Cycle 2 corrections, both of which had left this test not holding its stated
        property:

        - **Exemptions are keyed by (file, enclosing function), not by file.** A file-level
          key exempted the whole of ``test_coverage_verdict_verbatim.py`` — the module's
          richest verdict-fixture file — so a fabricated token could be reintroduced
          anywhere in it and stay invisible.
        - **The file list is recursive.** ``glob("*.py")`` does not descend; pytest does. A
          bare ``declined`` in verdict position inside ``agents/health/tests/warranty/``
          would have been collected and run by pytest while both layers missed it.
        """
        offenders: list[str] = []
        for path in _test_files():
            for lineno, value, func in _fabricated_literals(path, skip_docstrings=False):
                if (path.name, func) in _TOKEN_BACKSTOP_EXEMPT_SITES:
                    continue
                offenders.append(f"  {path.name}:{lineno} in {func}() {value!r}")
        assert not offenders, (
            "Fabricated verdict token(s) present as string literals:\n"
            + "\n".join(offenders)
            + "\n\nDMS emits 'denied' and 'requires_review'. If this is a legitimate "
            "paraphrase-rejection fixture, add its (filename, function) to "
            "`_TOKEN_BACKSTOP_EXEMPT_SITES` with a reason — do not exempt the whole file "
            "and do not delete the assertion."
        )

    def test_the_fabricated_tokens_appear_nowhere_in_module_source(self):
        """MUTATION TARGET. Third layer — the source tree, added in Cycle 2 (M8 route C).

        Neither Cycle 1 layer scanned ``agents/health/*.py`` at all, and the prompt-clause
        test pins a single constant *by name*. So a **second** prompt clause teaching
        ``declined`` would have shipped green — on the surface this Fix Group itself calls
        "the load-bearing site", and the only one of the five that reaches the live model.
        That is the most consequential of the three routes Cycle 2 found.

        Docstrings are excluded: ``agent.py`` deliberately names both fabricated values in a
        design note warning against reverting, and ``dms_verdicts.py`` documents them as the
        values it exists to exclude. Prose about a value is not a use of it — the same
        principle that made the position scan AST-based rather than regex-based. An
        executable literal is a use, and there are currently none.
        """
        offenders: list[str] = []
        for path in _source_files():
            for lineno, value, func in _fabricated_literals(
                path, skip_docstrings=True, substring=True
            ):
                offenders.append(f"  {path.name}:{lineno} in {func}() {value[:120]!r}")
        assert not offenders, (
            "Fabricated verdict token(s) present as executable string literals in module "
            "source:\n"
            + "\n".join(offenders)
            + "\n\nDMS emits 'denied' and 'requires_review'. A literal here reaches the "
            "runtime — if it is in a prompt clause it teaches the live model a vocabulary "
            "the producer does not have. Historical notes belong in docstrings, which this "
            "scan deliberately skips."
        )

    def test_the_token_backstop_exemptions_are_still_earned(self):
        """ANTI-VACUITY for the backstop's exemption map.

        An exemption whose site no longer contains the token is a stale exemption, and stale
        exemptions are how an allowlist becomes a blanket suppression. Rewritten in Cycle 2
        to check the (file, function) site rather than just the file.
        """
        assert _TOKEN_BACKSTOP_EXEMPT_SITES, (
            "The exemption map is empty — if no exemption is needed, delete the map and the "
            "skip logic rather than leaving an unused hook."
        )
        for (filename, func), reason in _TOKEN_BACKSTOP_EXEMPT_SITES.items():
            path = _TESTS_DIR / filename
            assert path.exists(), f"Exempt file {filename} no longer exists — remove the entry."
            present = any(
                f == func
                for _lineno, _value, f in _fabricated_literals(path, skip_docstrings=False)
            )
            assert present, (
                f"Exemption ({filename}, {func}) no longer contains a fabricated token "
                "literal — remove the entry so the map keeps naming only live exemptions.\n"
                f"Stated reason was: {reason}"
            )

    def test_the_source_docstring_exemptions_are_still_earned(self):
        """ANTI-VACUITY for the source-tree docstring exemption map.

        Same discipline as the token backstop's exemption map: an exemption whose site no
        longer names a fabricated token is a stale exemption, and stale exemptions turn an
        allowlist into a blanket suppression — which is exactly what the wholesale docstring
        skip was before Cycle 3 (M9/W2).
        """
        assert _SOURCE_DOCSTRING_EXEMPT_SITES, (
            "The docstring exemption map is empty — if no exemption is needed, remove the map "
            "and stop skipping docstrings rather than leaving an unused hook."
        )
        for (filename, scope), reason in _SOURCE_DOCSTRING_EXEMPT_SITES.items():
            path = _SOURCE_DIR / filename
            assert path.exists(), f"Exempt file {filename} no longer exists — remove the entry."
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            sites = _docstring_sites(tree)
            named = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and sites.get(id(node)) == scope
                and any(token in node.value for token in _FABRICATED_TOKENS)
            ]
            assert named, (
                f"Docstring exemption ({filename}, {scope}) no longer names a fabricated "
                "token — remove the entry so the map keeps naming only live exemptions.\n"
                f"Stated reason was: {reason}"
            )

    def test_no_test_body_declares_its_own_verdict_enum(self):
        """MUTATION TARGET.

        Before Fix Group 19, `test_coverage_verdict_verbatim.py:585` and
        `test_post_service_visit.py:641` each declared
        `_DMS_VERDICT_ENUM = frozenset({"approved", "declined", "under_review"})`
        locally. Two copies, both wrong, neither reachable from the other. Re-add such a
        local declaration and this test MUST fail.

        AST-based, so the docstrings in those two tests that now *describe* the removed
        declaration are correctly ignored — describing a defect is not committing it.
        """
        offenders: list[str] = []
        for path in _test_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "_DMS_VERDICT_ENUM":
                        offenders.append(f"  {path.name}:{target.lineno}")
        assert not offenders, (
            "Local _DMS_VERDICT_ENUM declaration(s) found:\n"
            + "\n".join(offenders)
            + "\n\nImport DMS_VERDICT_ENUM from agents.health.dms_verdicts instead — a "
            "per-test copy of a mirrored enum is how the original two copies drifted."
        )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
