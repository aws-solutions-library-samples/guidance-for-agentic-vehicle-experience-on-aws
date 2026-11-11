"""Structural guard for the `classification` wire-event `source` protocol.

Task 1.5a of spec `2026-09-23-cvx-escalation-recommend-not-act`, which resolves
review findings C4 and W1.

WHY THIS EXISTS
---------------
Review C4 found that the iOS render discriminator in `ios-affordance-design.md`
could not be built on either available convention:

  - `bidi_app.py`'s ordinary classifier emit sent **no** `source` key at all.
  - The only two sites that sent one sent `"driver-confirmed"` and
    `"driver-unclear-default"` — both themselves classifier-derived.
  - **Nothing emitted `"classifier"`**, and nothing emitted an LLM identifier.
  - iOS stated the inverse convention outright: `VSABidiClient.swift:509-510`
    notes `source` is optional because *"legacy classifier-only events don't
    carry them"*, and `VoiceSessionViewModel.swift:1540` prints
    `source ?? "classifier"`. So absent **meant** classifier.

Built as designed, the card never rendered. Built with the obvious local fix
(treat absent as classifier) there was no discriminator at all. The resolution
was a protocol change rather than a patch: `source` is now ALWAYS emitted with
an enumerated value.

The discriminator is therefore NOT classifier-vs-LLM — that distinction has no
LLM side left, because after the triage-401 fix and Group 1 the model cannot
produce a classification at all. It is **verdict recognised vs unknown protocol
shape**: absent or unrecognised `source` means fail closed.

WHY AST AND NOT GREP
--------------------
A grep for `"source"` cannot tell an emit site from the DynamoDB audit item at
`_write_driver_confirmation_audit`, which also carries a `"classification"` key
but is not a wire event. Keying on a dict literal whose `"type"` is
`"classification"` distinguishes them structurally.

WHY NOT IMPORT THE MODULE
-------------------------
`bidi_app` runs `assert_jwt_enforceable()` at import time (a concurrent
session's in-flight work). Parsing the file keeps this guard hermetic: a change
to that unrelated import-time assertion cannot turn this protocol test red and
send a future reader looking in the wrong place. One test below does import, as
a deliberate cross-check that the AST extraction reflects runtime reality.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_BIDI_APP_PATH = Path(__file__).resolve().parent.parent / "bidi_app.py"

# The floor is 3 because three emit sites exist today. It is an anti-vacuity
# guard: if the AST walk silently stops matching (a refactor moves the dicts
# behind a helper, say), every per-site assertion below would pass over an
# empty list. A test that validates nothing must fail, not pass.
_EXPECTED_MIN_EMIT_SITES = 3


def _module_ast() -> ast.Module:
    return ast.parse(_BIDI_APP_PATH.read_text(encoding="utf-8"))


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` assignments."""
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        out[target.id] = node.value.value
    return out


def _enum_members(tree: ast.Module) -> set[str]:
    """Resolve ``CLASSIFICATION_SOURCES = frozenset({NAME, ...})`` to strings.

    Resolved from source rather than imported so this guard and the emit-site
    walk read the same single artifact.
    """
    consts = _module_string_constants(tree)
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == "CLASSIFICATION_SOURCES" for t in targets):
            continue
        value = node.value
        # frozenset({...}) — unwrap the call
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
            if value.func.id == "frozenset" and value.args:
                value = value.args[0]
        members: set[str] = set()
        if isinstance(value, (ast.Set, ast.List, ast.Tuple)):
            for elt in value.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    members.add(elt.value)
                elif isinstance(elt, ast.Name) and elt.id in consts:
                    members.add(consts[elt.id])
                else:
                    pytest.fail(
                        "CLASSIFICATION_SOURCES contains a member this guard "
                        f"cannot resolve statically: {ast.dump(elt)}. Keep the "
                        "frozenset a flat set of module-level string constants "
                        "so it stays machine-checkable."
                    )
        return members
    pytest.fail(
        "CLASSIFICATION_SOURCES not found at module level in bidi_app.py. "
        "Task 1.5a requires a single module-level enum/frozenset as the only "
        "thing any emit site may draw from."
    )


def _classification_emit_sites(tree: ast.Module) -> list[tuple[int, dict[str, ast.expr]]]:
    """Every dict literal whose ``"type"`` is ``"classification"``.

    Returns (lineno, {key: value_node}). Excludes the DDB audit item, which
    carries a ``"classification"`` KEY but no ``"type"`` key.
    """
    sites: list[tuple[int, dict[str, ast.expr]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys: dict[str, ast.expr] = {}
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                keys[k.value] = v
        type_node = keys.get("type")
        if (
            isinstance(type_node, ast.Constant)
            and type_node.value == "classification"
        ):
            sites.append((node.lineno, keys))
    return sites


def _resolve_source(value: ast.expr, consts: dict[str, str]) -> str | None:
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    if isinstance(value, ast.Name):
        return consts.get(value.id)
    return None


class TestClassificationSourceProtocol:
    def test_emit_sites_are_discoverable(self):
        """Anti-vacuity floor — every other test iterates this list."""
        sites = _classification_emit_sites(_module_ast())
        assert len(sites) >= _EXPECTED_MIN_EMIT_SITES, (
            f"Found only {len(sites)} classification emit site(s); expected at "
            f"least {_EXPECTED_MIN_EMIT_SITES}. Either sites were removed, or "
            "the AST walk no longer matches how they are written — in which "
            "case every per-site assertion in this file is passing over an "
            "empty list and this protocol is unguarded."
        )

    def test_no_emit_site_omits_source(self):
        """Accept 2: no site may omit `source`.

        Omitting it is a protocol violation, not a backwards-compatible
        default — iOS reads absent `source` as "unknown server shape, fail
        closed" after this change.
        """
        sites = _classification_emit_sites(_module_ast())
        missing = [lineno for lineno, keys in sites if "source" not in keys]
        assert not missing, (
            "classification emit site(s) at bidi_app.py line(s) "
            f"{missing} do not set `source`. Every site must. An absent "
            "`source` makes the event indistinguishable from one sent by a "
            "server predating this contract, so iOS will refuse to render "
            "the affordance rather than guess."
        )

    def test_every_emit_site_draws_from_the_enum(self):
        """Accept 2: no site may send a literal outside CLASSIFICATION_SOURCES.

        Deliberately validated against the enum resolved from source, NOT
        against a list of strings re-typed into this test. A test that
        re-listed the three values would keep passing when a fourth site is
        added with a fourth literal, which is the exact regression this
        guards.
        """
        tree = _module_ast()
        allowed = _enum_members(tree)
        consts = _module_string_constants(tree)
        offenders: list[str] = []
        for lineno, keys in _classification_emit_sites(tree):
            if "source" not in keys:
                continue  # reported by test_no_emit_site_omits_source
            resolved = _resolve_source(keys["source"], consts)
            if resolved is None:
                offenders.append(
                    f"line {lineno}: `source` is not a string literal or a "
                    "module-level string constant, so it cannot be checked "
                    "statically"
                )
            elif resolved not in allowed:
                offenders.append(
                    f"line {lineno}: source={resolved!r} is not in "
                    f"CLASSIFICATION_SOURCES ({sorted(allowed)})"
                )
        assert not offenders, (
            "classification emit site(s) send a `source` outside the "
            "enumerated set:\n  " + "\n  ".join(offenders) + "\n\n"
            "Add the value to CLASSIFICATION_SOURCES only as a deliberate "
            "protocol change — iOS fails closed on values it does not know, "
            "so a new value silently stops rendering on older clients."
        )

    def test_ordinary_classifier_site_emits_the_classifier_value(self):
        """The specific C4 fix, asserted as a property rather than by presence.

        The site mirroring `ctx.triage_classification` is the one that
        previously sent no `source`. It must send exactly `"classifier"` — a
        site that satisfied the enum check with `"driver-confirmed"` would be
        actively wrong, since no driver confirmed anything on that path.
        """
        tree = _module_ast()
        consts = _module_string_constants(tree)
        matches = [
            (lineno, keys)
            for lineno, keys in _classification_emit_sites(tree)
            if isinstance(keys.get("value"), ast.Attribute)
            and getattr(keys["value"], "attr", None) == "triage_classification"
        ]
        assert matches, (
            "Could not find the emit site whose `value` is "
            "`ctx.triage_classification` — that is the ordinary classifier "
            "verdict path and the one review C4 was filed against."
        )
        for lineno, keys in matches:
            resolved = _resolve_source(keys.get("source"), consts)
            assert resolved == "classifier", (
                f"bidi_app.py line {lineno} mirrors ctx.triage_classification "
                f"but sends source={resolved!r}. It must send 'classifier': "
                "nothing else is true of that path, and mislabelling it would "
                "tell iOS a driver confirmed a verdict they never saw."
            )

    def test_ordinary_classifier_site_does_not_fabricate_a_category(self):
        """Accept 3: `category` stays optional.

        Only the two driver-confirmation paths have a real category. Inventing
        one for the ordinary classifier path would be fabricating data, so its
        absence is asserted rather than left to drift.
        """
        tree = _module_ast()
        for lineno, keys in _classification_emit_sites(tree):
            value = keys.get("value")
            if isinstance(value, ast.Attribute) and value.attr == "triage_classification":
                assert "category" not in keys, (
                    f"bidi_app.py line {lineno} sets `category` on the "
                    "ordinary classifier emit. That path has no category — "
                    "supplying one fabricates data. Per Task 1.5a Accept 3, "
                    "`category` is optional and only the driver-confirmation "
                    "paths carry it."
                )

    def test_docstring_contract_documents_every_enum_member(self):
        """Accept 4: the module's wire-protocol block must not go stale.

        The old block documented `{"type": "classification", "value": ...}`
        with no `source` at all, and that stale contract is what licensed C4.
        Derived from the enum rather than re-listing the values, so adding a
        member forces the documentation to be updated in the same change.
        """
        tree = _module_ast()
        docstring = ast.get_docstring(tree) or ""
        assert docstring, "bidi_app.py lost its module docstring."
        missing = sorted(m for m in _enum_members(tree) if m not in docstring)
        assert not missing, (
            "bidi_app.py's module docstring wire-protocol block does not "
            f"document these CLASSIFICATION_SOURCES members: {missing}. The "
            "docstring is the contract iOS is written against; a value that "
            "ships undocumented is one a client has no reason to recognise."
        )

    def test_docstring_states_the_fail_closed_rule(self):
        """The contract's load-bearing sentence, not merely its value list.

        A reader who sees three enumerated values but not the fail-closed rule
        will re-derive iOS's old `source ?? "classifier"` convention, which is
        precisely the defect C4 identified.
        """
        docstring = ast.get_docstring(_module_ast()) or ""
        assert "fail closed" in docstring.lower(), (
            "bidi_app.py's wire-protocol block does not state that clients "
            "must fail closed on an absent or unrecognised `source`. Without "
            "it, the next reader reinstates the absent-means-classifier "
            "convention this task removed."
        )

    def test_ast_extraction_matches_runtime(self):
        """Cross-check: the statically-resolved enum equals the real one.

        Every other test reads the file rather than importing it. If the AST
        resolution were wrong, they could agree with each other and all be
        wrong together. This is the one test that imports.
        """
        from agents.supervisor.bidi_app import CLASSIFICATION_SOURCES

        assert _enum_members(_module_ast()) == set(CLASSIFICATION_SOURCES), (
            "The enum resolved from source does not match the imported "
            "CLASSIFICATION_SOURCES. This file's static analysis is not "
            "seeing what Python sees, so its other assertions cannot be "
            "trusted."
        )


class TestDetectorPositiveControls:
    """Proof the detectors fire. Without these, a walk that matched nothing
    would make every test above pass for free."""

    def test_detector_flags_a_site_missing_source(self):
        tree = ast.parse(
            'await _safe_send_json(ws, {"type": "classification", "value": v})\n'
        )
        sites = _classification_emit_sites(tree)
        assert len(sites) == 1, "detector failed to find the synthetic site"
        assert "source" not in sites[0][1], (
            "detector reported a `source` key on a synthetic site that has "
            "none — test_no_emit_site_omits_source would never fire."
        )

    def test_detector_flags_an_out_of_enum_literal(self):
        tree = ast.parse(
            'x = {"type": "classification", "value": v, "source": "nova"}\n'
        )
        sites = _classification_emit_sites(tree)
        assert len(sites) == 1
        resolved = _resolve_source(sites[0][1]["source"], {})
        assert resolved == "nova", "detector did not resolve the literal"
        assert resolved not in _enum_members(_module_ast()), (
            "'nova' resolved as an allowed source — the enum check would not "
            "reject an invented LLM identifier."
        )

    def test_detector_ignores_the_ddb_audit_item(self):
        """The audit item has a `classification` KEY but no `type` key.

        If the walk matched it, the guard would demand a `source` field on a
        DynamoDB row, and a well-meaning fix would add one — putting a wire
        concern into an audit record.
        """
        tree = ast.parse(
            'item = {"sessionId": s, "classification": final, '
            '"confirmationSource": "driver"}\n'
        )
        assert _classification_emit_sites(tree) == [], (
            "detector matched a dict that merely has a `classification` key. "
            "It must key on `\"type\": \"classification\"` so audit records "
            "and wire events stay distinguishable."
        )
