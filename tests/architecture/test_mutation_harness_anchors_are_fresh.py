"""Every mutation in the Fix Group 15 harness must still have a live anchor.

A mutation harness that cannot find its anchor does not fail — it reports
``unapplied`` and moves on. So a refactor silently retires whichever properties it
displaced, and the harness keeps printing a reassuring "all caught" for the
mutations that still apply.

That happened in this repo on 2026-09-23: broadening ``resolve_owner_id`` to
``resolve_standing_facts`` and ``_owner_projection`` to ``_standing_projection``
moved the anchors of **7 of 15** mutations at once, retiring the guards on the
empty-string check, the VIN-equality check, the projection-missing case, the
write-time integrity assertion, and the stale-projection REMOVE. Every one was
re-anchored, and this test exists so the next refactor fails loudly at test time
instead of quietly at harness time.

This is the same class as ``test_external_table_exemptions_are_still_real_call_sites``
in ``test_ddb_index_names_match_cdk.py``: a mechanism whose failure mode is
silence needs a test that its inputs still bind.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_HARNESS = _REPO_ROOT / "scripts" / "fg15-mutations.py"


def _load_harness():
    """Import the harness by path — its filename is not a valid module name."""
    spec = importlib.util.spec_from_file_location("fg15_mutations", _HARNESS)
    assert spec and spec.loader, f"cannot load {_HARNESS}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fg15_mutations"] = mod
    spec.loader.exec_module(mod)
    return mod


def _all_mutations(mod):
    out = []
    for name in ("MUTATIONS", "HANDLER_MUTATIONS", "STANDING_MUTATIONS"):
        out.extend(getattr(mod, name, []))
    return out


def test_harness_file_exists():
    assert _HARNESS.is_file(), (
        f"{_HARNESS.relative_to(_REPO_ROOT)} is missing. If the harness was "
        "deliberately retired, delete this test in the same commit — a guard for a "
        "mechanism that no longer exists is worse than no guard."
    )


def test_every_mutation_anchor_is_present_exactly_once():
    """Each mutation's ``old`` text must appear exactly once in its target file.

    Zero occurrences means the harness silently skips it (the failure this test
    exists for). More than one means the harness would mutate an arbitrary site,
    so "caught" would not identify which property was actually broken.
    """
    mod = _load_harness()
    mutations = _all_mutations(mod)
    assert mutations, "the harness declares no mutations — it has been gutted"

    problems: list[str] = []
    for m in mutations:
        target = _REPO_ROOT / m.path
        if not target.is_file():
            problems.append(f"{m.mid}: target file {m.path} does not exist")
            continue
        n = target.read_text().count(m.old)
        if n != 1:
            problems.append(
                f"{m.mid}: anchor occurs {n}x in {m.path} (need exactly 1) "
                f"— first line: {m.old.splitlines()[0][:70]!r}"
            )

    assert not problems, (
        "mutation anchors have gone stale, so the harness is silently skipping "
        "these properties:\n  " + "\n  ".join(problems)
        + "\n\nRe-anchor them onto the current code. Do NOT delete a mutation to "
        "make this pass unless the property it guards is genuinely gone."
    )


def test_every_mutation_names_at_least_one_test():
    mod = _load_harness()
    for m in _all_mutations(mod):
        assert m.tests, f"{m.mid} names no test, so 'caught' could never be decided"


def test_mutation_ids_are_unique():
    """A duplicate id makes the harness's summary ambiguous about what was checked."""
    mod = _load_harness()
    ids = [m.mid for m in _all_mutations(mod)]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert not dupes, f"duplicate mutation ids: {dupes}"


def test_harness_is_non_vacuous():
    """Guards the guard: a plausible floor on how many properties are covered.

    Without this, a harness trimmed to one mutation would satisfy every assertion
    above. The floor is deliberately well below the current count so ordinary
    additions do not trip it, and a large deletion does.
    """
    mod = _load_harness()
    n = len(_all_mutations(mod))
    assert n >= 15, (
        f"only {n} mutations declared. Fix Groups 15/20/22/23/25 recorded 22; a "
        "count this low means mutations were removed rather than re-anchored."
    )
