"""The four AVX readers of CMS dtc-history share one "is this row cleared" rule.

Issue 2026-10-05-active-dtcs-reads-remote-clears-as-active: the readers had drifted (one checked
no clear marker, one skipped resetAt, none knew CLEARED_REMOTE/clearedAt). Each keeps its own copy
because they ship in different bundles (supervisor runtimes, Tier 2 Lambda, triage and
vehicle-context Lambdas); this test fails when one copy changes without the others.
"""
from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
READERS = [
    "lambdas/shared/cms_client_real.py",
    "agents/supervisor/tools/active_dtcs.py",
    "agents/supervisor/bidi_app.py",
    "lambdas/api-vehicle-context/handler.py",
]


def _source(path: str, name: str) -> str:
    tree = ast.parse((ROOT / path).read_text())
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.Assign)):
            target = node.name if isinstance(node, ast.FunctionDef) else getattr(node.targets[0], "id", None)
            if target == name:
                return ast.unparse(node)
    raise AssertionError(f"{name} not found in {path}")


def test_every_reader_has_the_same_rule():
    for name in ("_CLEARED_STATUSES", "_row_is_cleared"):
        copies = {p: _source(p, name) for p in READERS}
        assert len(set(copies.values())) == 1, f"{name} differs: {sorted(copies)}"


def test_every_reader_uses_the_rule():
    for p in READERS:
        src = (ROOT / p).read_text()
        assert src.count("_row_is_cleared(item)") == 1, p
        assert '("CLEARED", "RESOLVED")' not in src, f"{p} still has the old status tuple"


def _rule():
    ns: dict = {}
    exec(compile(_source(READERS[0], "_CLEARED_STATUSES") + "\n" + _source(READERS[0], "_row_is_cleared"),
                 "rule", "exec"), ns)
    return ns["_row_is_cleared"]


def test_rule_table():
    cleared = _rule()
    yes = [{"status": "CLEARED"}, {"status": "cleared_remote"}, {"status": "RESOLVED"},
           {"status": "ACTIVE", "clearedDate": "2026-10-05"}, {"status": "ACTIVE", "resetAt": "x"},
           {"status": "ACTIVE", "clearedAt": Decimal("1790799119132")},
           {"status": "ACTIVE", "clearedAt": 1790799119132}, {"status": "ACTIVE", "clearedAt": "1790799119132"}]
    no = [{"status": "ACTIVE"}, {"status": "ACTIVE", "clearedDate": ""}, {"status": "ACTIVE_NO_DTC"},
          {"status": "ACTIVE", "clearedAt": None}, {"status": "ACTIVE", "clearedAt": "abc"},
          {"status": "ACTIVE", "clearedAt": Decimal("1.5")}, {"status": "ACTIVE", "clearedAt": 0},
          {"status": "ACTIVE", "clearedAt": True}, {"status": "ACTIVE", "clearedAt": Decimal("NaN")},
          {"status": "ACTIVE", "clearedAt": "Infinity"}, {"status": "ACTIVE", "clearedAt": "sNaN"},
          {}]
    assert all(cleared(r) for r in yes), [r for r in yes if not cleared(r)]
    assert not any(cleared(r) for r in no), [r for r in no if cleared(r)]


def test_future_cleared_at_still_counts_as_cleared():
    """Deliberate difference from CMS's Flink replay guard, which drops a clear dated more than
    5 minutes ahead. A reader judges one row: a later real report writes a new ACTIVE row, so a
    skewed clock on the clear cannot hide a fault that comes back."""
    import time

    cleared = _rule()
    one_day_ahead_ms = int(time.time() * 1000) + 86_400_000
    assert cleared({"status": "ACTIVE", "clearedAt": Decimal(one_day_ahead_ms)})
    assert cleared({"status": "ACTIVE", "clearedAt": Decimal("9999999999999")})
