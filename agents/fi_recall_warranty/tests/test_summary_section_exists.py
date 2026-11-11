"""Guard: the code-comment reference to the summary.md section must resolve.

Review Cycle 1 Warning 2026-10-10: the two prompt-fix comments in
``agents/fi_recall_warranty/llm.py`` cite "§ 'Follow-on fix 2026-10-10 —
recall plan replies blocked on PII ADDRESS'" in the issue summary. If the
section goes away the comments point at thin air. This test keeps them in
lockstep: edit the heading, or delete the section, and the next test run
fails before the reviewer re-reads the code.

Mutant: delete the "## Follow-on fix 2026-10-10 — recall plan replies
blocked on PII ADDRESS" heading from the summary. The test fails.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[3]
_SUMMARY = _REPO / "issues" / "2026-10-10-cost-guardrail-blocks-replies" / "summary.md"
_LLM = _REPO / "agents" / "fi_recall_warranty" / "llm.py"


def _summary_text() -> str:
    # The public mirror ships neither issues/ nor .publish-exclude, so these tests skip
    # there. In the internal repo a missing summary.md still fails.
    if not _SUMMARY.exists() and not (_REPO / ".publish-exclude").exists():
        pytest.skip("issues/ is not in the public copy")
    return _SUMMARY.read_text()


def test_summary_md_carries_the_follow_on_fix_section():
    text = _summary_text()
    assert "## Follow-on fix 2026-10-10 — recall plan replies blocked on PII ADDRESS" in text, (
        "The summary.md must carry the Follow-on fix section the prompt-fix "
        "comments in llm.py reference. If the heading changed, update both the "
        "comments and this assertion in one commit."
    )


def test_llm_comments_reference_the_section_that_summary_md_actually_carries():
    # Both the llm.py comments and the summary.md must carry the same section
    # phrase, verbatim. The two anchor substrings below are the ones a reviewer
    # grep's for; a mutant that drops the heading from summary.md or edits one
    # side of the pair breaks the match.
    anchors = [
        "Follow-on fix 2026-10-10",
        "recall plan replies blocked on PII ADDRESS",
    ]
    code = " ".join(_LLM.read_text().replace("#", " ").split())
    summary = _summary_text()
    for a in anchors:
        assert a in code, (
            f"llm.py's prompt-fix comments no longer carry {a!r} — if the "
            f"wording moved, update both the comments and this test together."
        )
        assert a in summary, (
            f"summary.md no longer carries {a!r} — the llm.py comments cite "
            f"this heading; add it back or edit the comments."
        )
