"""DMS warranty-claim verdict vocabulary — the single CVX-side declaration.

Spec: `.kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/tasks.md`
§ Fix Group 19.
Issue: `issues/2026-09-23-warranty-verdict-enum-fabricated-in-cvx-narration-guard/`.

## Why this module exists

Agent 2 narrates a DMS warranty verdict **verbatim** — it does not adjudicate it. That
makes the verdict a deterministic-seam output in the sense of
`~/.kiro/steering/agentic-tiers.md` § "What stays deterministic, and why", and it makes the
*exact spelling* of each verdict load-bearing: narrating "declined" when DMS said "denied"
crosses the boundary while appearing to respect it.

Before this module existed the vocabulary was restated by hand in prose, in fixtures, and
in two separately-hardcoded frozensets inside test bodies. Two of the three values were
invented. `declined` — used at 19 of the 26 verdict-phrase sites then present, one of them
annotated ``# LITERAL — canonical DMS denial phrase`` — appears in **zero** DMS source
files. (Counts corrected in review Cycle 1: an earlier draft said "22 of 29", which came
from a regex scan that double-counted across selectors. The AST scan finds 26 sites against
the pre-fix tree, and 29 after Cycle 1 added a fourth selector for verdicts reached through
a loop variable.)

The root cause is worth stating because it will recur: `declined` **is** a real status in
this subsystem. It is an AVX **Action** terminal status
(`lambdas/api-avx-actions/handler.py:104-112` —
``_TERMINAL_STATUSES = {completed, cancelled, declined, dismissed, failed}``). Two adjacent
enums, one token present in only one of them, and no test comparing either against its
producer.

## This is a hand-restated mirror, not an import

CVX cannot import from the DMS repo — they are separate repositories with no build-time
dependency. The values below are transcribed from:

    ~/guidance-for-dealer-management-system-on-aws/source/_lib/warranty_rules.py:79

        status: str  # 'approved' | 'denied' | 'requires_review'

whose own ``Verdict`` class docstring states the contract this guard protects:

    "The agent NARRATES this verdict verbatim — it does not recompute or override it."

Because it is a mirror and not an import, **nothing detects drift automatically.** If DMS
adds or renames a verdict, this file goes stale silently. The only mechanical protection is
`tests/test_dms_verdict_enum.py`, which pins every CVX-side verdict fixture against this
constant — so a DMS-side rename surfaces as "CVX fixtures use a value DMS no longer emits"
the next time someone re-reads the source of truth, not as a passing test suite.

Verified against the DMS source 2026-09-23. Re-verify when touching the warranty path.
"""
from __future__ import annotations

#: The complete set of verdict values DMS's warranty-claim validator can emit.
#:
#: Mirrored by hand from `source/_lib/warranty_rules.py:79` in the DMS repo — see the
#: module docstring for why this is a mirror rather than an import, and what that costs.
#:
#: Consumed by:
#:   - `tests/test_dms_verdict_enum.py` — pins every verdict fixture in `agents/health/`
#:   - `tests/test_coverage_verdict_verbatim.py` — out-of-vocabulary mutation reasoning
#:   - `tests/test_post_service_visit.py` — same
#:
#: NOT consumed by `assert_coverage_verdict_verbatim` in `agent.py`, deliberately: that
#: guard is agent-agnostic and accepts *any* non-empty phrase, which is what lets Agent 3
#: (Spec 1b) and the `warranty_agent` persona reuse it unchanged. Validating the phrase
#: against this enum inside the guard would couple the guard to DMS and break that reuse.
#: The enum's job is to keep *fixtures and prompt examples* honest, not to restrict callers.
DMS_VERDICT_ENUM: frozenset[str] = frozenset(
    {
        "approved",
        "denied",
        "requires_review",
    }
)

#: DMS's denial verdict, spelled as DMS spells it.
#:
#: Named separately because it is the value most narration tests exercise — a denial is the
#: verdict a customer is most likely to dispute, so it is the one whose verbatim narration
#: matters most. It is **not** ``"declined"`` (AVX Action vocabulary) and **not**
#: ``"rejected"`` (not in any enum here).
DMS_VERDICT_DENIED: str = "denied"

#: DMS's manual-review verdict. Underscored, matching the producer.
#:
#: It is **not** ``"under_review"`` (the pre-Fix-Group-19 invention) and must not be
#: confused with the softened narration phrase ``"under review"`` (with a space), which
#: several tests use precisely *because* it is outside this vocabulary.
DMS_VERDICT_REQUIRES_REVIEW: str = "requires_review"

#: DMS's approval verdict. The one value the pre-Fix-Group-19 vocabulary got right.
DMS_VERDICT_APPROVED: str = "approved"
