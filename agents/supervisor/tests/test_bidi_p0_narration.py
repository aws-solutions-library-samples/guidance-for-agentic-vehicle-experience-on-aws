"""The P0 injected narration must not assert things that are not true.

Filed against `issues/2026-09-23-p0-narration-claims-roadside-dispatched/`.

WHY A GUARD HERE AND NOT IN THE PROMPT
--------------------------------------
`supervisor.py`'s rule 9 (Task 2.1 of spec
`2026-09-23-cvx-escalation-recommend-not-act`) forbids the model from describing an
escalation as "arranged, under way, or completed". It cannot help here, and the reason
is structural: rule 9 governs what the model claims **on its own initiative**, while an
injected system note **instructs the model to say a specific thing**. A prompt rule
cannot defend a boundary that the server's own injected text crosses.

So the boundary is enforced here, against the source that builds the injection, rather
than against a prompt constant.

WHAT WENT WRONG
---------------
On every P0 the injection told the driver roadside had been dispatched and help was on
the way. Neither was true. F1.1 removed the `dispatch_roadside=True` pin so the tenant
flag (default false) governs, and nothing consumes the dispatch queue regardless
(`issues/2026-09-23-p0-roadside-dispatch-is-a-dead-letterbox/`). The fix stopped the
request; it did not stop the sentence. Nothing asserted on the injection's content, so
the claim survived a fix cycle and a review cycle.

TWO THINGS THIS FILE HAS TO GET RIGHT
-------------------------------------
1. **Comments must be stripped before scanning.** The fix's own explanatory comments
   quote the removed phrases verbatim — a guard written in terms of the forbidden string
   trips on the note explaining why the string is forbidden. Same lesson as
   `cms/.../main_api/test_no_hardcoded_stage_tables.py:73`.
2. **Implicit string concatenation must be stitched.** The original defect read
   `f"...roadside dispatch has "` / `f"been scheduled..."` across two adjacent literals.
   Scanning raw source line-by-line would not match the phrase, and the guard would pass
   against the very bug it exists to catch. `test_guard_would_have_caught_the_original_defect`
   pins this using the real historical text.
"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

import pytest

_BIDI_APP_PATH = Path(__file__).resolve().parent.parent / "bidi_app.py"

# Assertion-shaped claims. Each is a statement that something HAS happened.
#
# Deliberately phrased so the module's own corrective text does not match: the fix tells
# the model not to claim roadside is "en route, already sent, or arranged", which shares
# no literal with any entry below. A denylist that its own remediation trips is a
# denylist that gets weakened until it passes.
_FORBIDDEN_ASSERTIONS = (
    "has been dispatched",
    "dispatch has been scheduled",
    "dispatch has been arranged",
    "dispatch scheduled",
    "help is on the way",
    "is on its way",
)


def _scannable_code(src: str) -> str:
    """Comment-free, concatenation-stitched, whitespace-normalised source.

    Three transforms, each load-bearing:
      1. drop COMMENT tokens — via `tokenize`, not a naive `split("#")`, so a `#`
         inside a string literal cannot truncate real code;
      2. stitch implicit string concatenation, so a phrase spanning two adjacent
         literals is found;
      3. collapse whitespace, so a phrase broken across lines is found.
    """
    pieces: list[str] = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            continue
        pieces.append(tok.string)
    text = " ".join(pieces)
    # `" f"` / `" "` seams between adjacent literals, single or double quoted.
    text = re.sub(r"""["']\s*[frbu]*\s*["']""", "", text)
    return re.sub(r"\s+", " ", text)


@pytest.fixture(scope="module")
def code() -> str:
    return _scannable_code(_BIDI_APP_PATH.read_text(encoding="utf-8"))


class TestNoFalseDispatchAssertion:
    def test_no_dispatch_assertion_in_injected_narration(self, code: str) -> None:
        hits = [phrase for phrase in _FORBIDDEN_ASSERTIONS if phrase in code]
        assert not hits, (
            "bidi_app.py asserts that dispatch or help has already happened:\n  "
            + "\n  ".join(hits)
            + "\n\nNothing dispatches roadside today — F1.1 removed the request and "
            "nothing consumes the queue. An injected note instructs the model "
            "directly, so this cannot be corrected by a prompt rule. Offer roadside "
            "as a recommendation instead. If a future change makes dispatch real "
            "(spec 2026-09-23-cvx-rsa-stub-dispatch), the claim must still not "
            "present a SIMULATED dispatch as a real one — see that spec's R4."
        )

    def test_roadside_is_still_offered_as_a_recommendation(self, code: str) -> None:
        """Absence of the false claim must not become silence.

        On a P0 the driver should still learn roadside is available to them. A fix
        that deleted the sentence outright would pass the test above and quietly
        remove useful safety guidance.
        """
        assert "Roadside assistance may be appropriate" in code, (
            "the P0 narration no longer mentions roadside at all. Removing the false "
            "'already dispatched' claim should leave a recommendation behind, not "
            "nothing — the driver still needs to know they can request it."
        )

    def test_model_is_explicitly_told_not_to_claim_dispatch(self, code: str) -> None:
        """The injected note carries the prohibition, not just the omission.

        Omitting the claim is not sufficient: the model has vehicle context and a P0
        verdict, and will reach for 'help is on the way' unprompted. The note has to
        forbid it at the point of instruction.
        """
        assert "Do NOT claim it is en route" in code, (
            "the injected note no longer forbids the model from claiming roadside is "
            "en route. Silence is not a guardrail here — rule 9 in supervisor.py "
            "cannot reach an injected instruction, which is the whole reason this "
            "test file exists."
        )

    def test_human_chat_narration_is_gated_on_a_named_condition(self, code: str) -> None:
        """Task 2.2's seam must stay a single named thing.

        The narration asserts a human chat is being opened. T2.2 gates the actual
        opening on a tenant flag. If the narration's condition is inline rather than
        named, the two drift and the driver is told a human is connecting when none
        is — which is the same defect class as the roadside claim, one flag away.

        Asserts the **branch uses** the name, not merely that the name appears. An
        earlier version of this test checked `"human_chat_opening" in code`, and a
        mutation that inlined the condition passed it — the assignment was still
        present, so the substring was still there while nothing consumed it. Presence
        is not the property.
        """
        raw = _BIDI_APP_PATH.read_text(encoding="utf-8")
        assert "if human_chat_opening:" in raw, (
            "the human-chat narration branch is no longer gated on the named "
            "`human_chat_opening` condition. Task 2.2 must be able to change the "
            "gate in ONE place; an inline condition guarantees the narration and the "
            "escalate task diverge, and the driver gets told a human is connecting "
            "when the tenant flag says none is."
        )
        assert code.count("human_chat_opening") >= 2, (
            "`human_chat_opening` appears fewer than twice — it should be assigned "
            "once and consumed at least once. One occurrence means it is defined and "
            "unused, which is the state the inlining mutation produces."
        )


class TestGuardIsNotVacuous:
    def test_guard_would_have_caught_the_original_defect(self) -> None:
        """Positive control, using the real historical source text.

        This is the strongest available control: the exact code that shipped the bug,
        including its split across adjacent f-string literals. If the scanner cannot
        find the phrase in this, it could not have found it in `bidi_app.py` either.
        """
        historical = (
            '        injection_parts.append(\n'
            '            f"A service ticket has been created and roadside dispatch has "\n'
            '            f"been scheduled. Tell the driver help is on the way. "\n'
            '            f"Do not read the ticket number ({ticket}) out loud."\n'
            '        )\n'
            '        rsa_phrase = " Roadside assistance has been dispatched to the driver\'s location."\n'
        )
        scanned = _scannable_code(historical)
        hits = [phrase for phrase in _FORBIDDEN_ASSERTIONS if phrase in scanned]
        assert "dispatch has been scheduled" in hits, (
            "the scanner did not find a phrase split across two adjacent f-string "
            "literals. Implicit-concatenation stitching is broken, and every "
            "assertion in this file would pass against the original bug.\n"
            f"scanned text: {scanned!r}"
        )
        assert "help is on the way" in hits
        assert "has been dispatched" in hits

    def test_comment_stripping_does_not_also_strip_code(self, code: str) -> None:
        """Anti-vacuity floor.

        If `_scannable_code` returned little or nothing, every forbidden-phrase check
        would pass for free. Assert it still contains code that is definitely there.
        """
        assert len(code) > 50_000, (
            f"scannable code is only {len(code)} chars — bidi_app.py is a large "
            "module, so the tokenizer or the stitching regex has eaten real content "
            "and the scan is running over a fragment."
        )
        assert "def _auto_triage_and_inject" in code, (
            "the function that builds the injection is missing from the scanned "
            "text — the scan is not looking at the code it claims to guard."
        )

    def test_corrective_comments_do_not_trip_the_guard(self) -> None:
        """The comments explaining the fix quote the forbidden phrases verbatim.

        That is deliberate — the next reader needs to know what was removed. This
        test pins that comment-stripping is what makes it safe, so nobody 'fixes' a
        future failure by deleting the explanation instead of the assertion.
        """
        raw = _BIDI_APP_PATH.read_text(encoding="utf-8")
        assert "help is on the way" in raw, (
            "expected the corrective comment quoting the removed phrase to still be "
            "present in the raw source. If it was deleted, the next reader loses the "
            "record of what was wrong."
        )
        assert "help is on the way" not in _scannable_code(raw), (
            "comment-stripping is not working: the phrase survives into scannable "
            "code from a comment, so the main guard is testing comments rather than "
            "behaviour."
        )
