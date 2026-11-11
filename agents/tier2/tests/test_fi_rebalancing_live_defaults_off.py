"""Pin ``FI_REBALANCING_LIVE`` in ``infrastructure/lib/fi-rebalancing-live.ts`` (spec D8,
task 5.2 constraint; flipped to ``true`` by Group 6 task 6.2, 2026-10-17).

The flag shipped disabled until Group 6's deploy, after ADP's fleet_bookings/forecast views
landed (6.1). It is now pinned ``true``: a session that turns it back off, or on in another
branch, changes the 06:00 UTC Scheduler schedule on the next tier2 deploy, so the value is a
reviewed edit of this test and the constant together (mutation M5, now in the other direction).

Python, because this directory runs in the gating CI job (verify_fi_tier2_pins)
and jest runs in none.
"""
from __future__ import annotations

import os
import re


_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_LIVE_TS = os.path.join(
    _REPO_ROOT, "infrastructure", "lib", "fi-rebalancing-live.ts"
)


def _live_const_value() -> str:
    with open(_LIVE_TS) as fh:
        src = fh.read()
    m = re.search(
        r"export\s+const\s+FI_REBALANCING_LIVE\s*(?::\s*boolean)?\s*=\s*(true|false)\s*;",
        src,
    )
    assert m, (
        f"FI_REBALANCING_LIVE declaration not found in {_LIVE_TS}; the file "
        "must export `export const FI_REBALANCING_LIVE = <bool>;`"
    )
    return m.group(1)


def test_fi_rebalancing_live_defaults_off():
    """M5 anchor (name kept for the CI pin list): the constant is ``true`` since Group 6 task
    6.2; flipping it back to ``false`` fails this test. Turning rebalancing off is a reviewed
    edit of this test and the constant together, as turning it on was.
    """
    assert _live_const_value() == "true", (
        "FI_REBALANCING_LIVE is true since Group 6 task 6.2 (2026-10-17); turning it off "
        "is a reviewed change of the constant and this test together.  Spec D8, task 5.2."
    )


def test_fi_rebalancing_live_file_exists_and_has_doc_comment():
    """The feature-flag file carries the "why" so a future reader does not
    delete it as unused.  Keeps the doc comment pinned."""
    with open(_LIVE_TS) as fh:
        src = fh.read()
    assert "FI_REBALANCING_LIVE" in src
    # The comment must say what the flag does and when it may flip.
    assert "Group 6" in src
    assert "DISABLED" in src.upper()
