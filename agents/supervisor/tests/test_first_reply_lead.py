"""The first service-center answer after triage opens with fixed words: the fault in plain
language and triage's safe-to-drive verdict (built on ``demo/linkedin-video``, ported to main).

Recorded takes failed three ways: the model said the code's meaning "isn't in the knowledge
base", read out "reported via UDS 0x19 on signal_901", skipped both, or turned triage's
"limited" into "you should not continue driving". ``first_reply_lead`` holds the words;
``bidi_app`` quotes them in the tagged platform note it injects for the service-center
result. They are NOT also sent to the screen: iOS suppresses a transcript
flagged ``_synthetic_from_auto_book`` as its own echo and an unflagged one renders twice
(``test_nothing_is_sent_to_the_screen...`` pins that; do not reintroduce it).

The wording is PINNED LITERALLY below. That is deliberate: the sentence the driver hears is
the specification, so a change to a phrase must change a test. The mutants tried (39 by AVX
``e8b046db``, all caught) and their logs are in the CMS spec's ``lead-mutation-battery/`` (CMS
branch ``demo/linkedin-video``); ``run.py`` there applies to the commit named in its header.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

import agents.supervisor.bidi_app as bidi
from agents.supervisor import first_reply_lead as frl
from agents.supervisor.severity_narration import narration_conflict, severity_claims

_ROOT = Path(__file__).resolve().parents[3]
_TABLE = yaml.safe_load((_ROOT / "lambdas" / "shared" / "dtc_severity.yaml").read_text())

# The spoken words, pinned.
YES_PHRASE = "It's fine to keep using the car as normal for now."
LIMITED_PHRASE = (
    "You can still drive it, but only with care: keep trips short and avoid "
    "highway speeds until it has been checked."
)
NO_PHRASE = "Please don't drive it. Pull over somewhere safe and stop."
C0460_SENTENCE = "Your power steering has a fault, so the steering may feel heavier than usual."


def _parity():
    spec = importlib.util.spec_from_file_location(
        "check_dtc_severity_parity_for_lead", _ROOT / "scripts" / "check-dtc-severity-parity.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# The words
# ---------------------------------------------------------------------------


class TestVerdictPhrases:
    def test_each_verdict_value_has_its_pinned_phrase(self):
        assert frl.VERDICT_PHRASES == {"yes": YES_PHRASE, "limited": LIMITED_PHRASE, "no": NO_PHRASE}

    def test_every_verdict_value_in_the_table_has_a_phrase(self):
        """A new verdict value in dtc_severity.yaml without a phrase must fail here, not
        fall through to the model's own wording."""
        assert {v["safe_to_drive"] for v in _TABLE.values()} <= set(frl.VERDICT_PHRASES)

    def test_every_system_in_the_table_has_a_label(self):
        """The fallback sentence is built from `system`; an unlabelled one would speak a raw token."""
        assert {v["system"] for v in _TABLE.values()} <= set(frl.SYSTEM_LABELS)

    @pytest.mark.parametrize("verdict", ["yes", "limited"])
    def test_drivable_phrases_make_no_severity_claim(self, verdict):
        """So the platform's own line can never trigger severity_narration's correction.
        In particular no "safe to drive" / "keep driving" (a routine claim, which a P1 rejects)."""
        assert severity_claims(frl.VERDICT_PHRASES[verdict]) == set()

    def test_the_no_phrase_is_a_stop_claim(self):
        assert "stop" in severity_claims(frl.VERDICT_PHRASES["no"])

    def test_default_entry_matches_the_classifiers_default(self):
        classifier, _ = _parity().load_classifier()
        assert dict(frl.DEFAULT_ENTRY) == dict(classifier._DEFAULT_DTC)

    def test_the_table_path_is_the_one_the_classifier_reads(self):
        classifier, _ = _parity().load_classifier()
        assert frl.SEVERITY_TABLE_PATH.resolve() == classifier._SEVERITY_TABLE_PATH.resolve()
        assert frl.SEVERITY_TABLE_PATH.is_file()


# ---------------------------------------------------------------------------
# Choosing the lead
# ---------------------------------------------------------------------------


class TestBuildLead:
    def test_c0460_gets_the_plain_sentence_and_the_limited_phrase(self):
        lead = frl.build_lead(["C0460"], "P1")
        assert lead is not None
        assert (lead.code, lead.verdict) == ("C0460", "limited")
        assert lead.text == f"{C0460_SENTENCE} {LIMITED_PHRASE}"

    def test_the_plain_sentence_never_carries_protocol_text(self):
        lead = frl.build_lead(["C0460"], "P1")
        for token in ("UDS", "0x", "signal", "C0460", "knowledge base"):
            assert token not in lead.text

    def test_p0_has_no_lead_the_directive_owns_it(self):
        assert frl.build_lead(["C1234"], "P0") is None

    @pytest.mark.parametrize("classification", [None, "", "P4", "p1"])
    def test_no_usable_classification_has_no_lead(self, classification):
        assert frl.build_lead(["C0460"], classification) is None

    @pytest.mark.parametrize("codes", [None, [], ["", "  "], [None]])
    def test_no_active_code_has_no_lead(self, codes):
        assert frl.build_lead(codes, "P1") is None

    @pytest.mark.parametrize("grade", ["P1", "P2", "P3"])
    def test_an_unreadable_table_has_no_lead_it_never_guesses_a_verdict(self, grade):
        """P2 matters: with no table every code falls to the default entry, which implies P2,
        so the agreement check alone would not refuse (a hypothetical moderate/limited code
        that really grades P2 would be told "fine to keep using the car")."""
        assert frl.build_lead(["C0460"], grade, table={}) is None

    def test_the_strictest_verdict_leads(self):
        lead = frl.build_lead(["P0420", "C0460"], "P1")
        assert (lead.code, lead.verdict) == ("C0460", "limited")

    def test_input_order_breaks_a_tie(self):
        a = frl.build_lead(["C0460", "C1001"], "P1")
        b = frl.build_lead(["C1001", "C0460"], "P1")
        assert (a.code, b.code) == ("C0460", "C1001")

    def test_an_unlisted_code_gets_the_classifiers_default_verdict(self):
        lead = frl.build_lead(["B1234"], "P2")
        assert lead.verdict == "yes"
        assert lead.verdict_phrase == YES_PHRASE
        assert lead.fault_sentence == "Your car has reported a fault."

    def test_an_uncurated_code_is_described_by_its_system(self):
        assert _TABLE["C1001"]["system"] == "abs"
        lead = frl.build_lead(["C1001"], "P1")
        assert lead.fault_sentence == "Your car has reported a fault in the anti-lock brake system."

    def test_every_fallback_sentence_reads_as_english(self):
        """No raw token and no "a" before a vowel sound, for every system in the table."""
        for system in {v["system"] for v in _TABLE.values()}:
            s = frl.fault_sentence("X9999", {"system": system})
            assert s == f"Your car has reported a fault in the {frl.SYSTEM_LABELS[system]} system."
            assert "_" not in s

    def test_a_table_verdict_outside_the_phrases_falls_back_to_the_default(self):
        table = {"C0460": {"severity": "high", "system": "steering", "safe_to_drive": "maybe"}}
        # The default entry (moderate / yes) implies P2: that is the grade it can lead for.
        lead = frl.build_lead(["C0460"], "P2", table=table)
        assert lead.verdict == "yes"
        assert frl.build_lead(["C0460"], "P1", table=table) is None


class TestAgreementWithTheGrade:
    def test_the_level_table_is_the_classifiers(self):
        classifier, _ = _parity().load_classifier()
        assert dict(frl.TABLE_LEVELS) == {k: v.name for k, v in classifier._DTC_TO_LEVEL.items()}

    @pytest.mark.parametrize("codes,grade", [(["P0420"], "P1"), (["C0460"], "P2"), (["C0460"], "P3"), (["P0420"], "P3")])
    def test_a_grade_the_table_does_not_imply_has_no_lead(self, codes, grade):
        assert frl.build_lead(codes, grade) is None

    def test_the_most_severe_code_sets_the_implied_level(self):
        assert frl.implied_level(["P0420", "C0460"], _TABLE) == "P1"
        assert frl.implied_level(["P0420", "P0442"], _TABLE) == "P2"

    def test_a_mixed_vehicle_leads_with_the_strictest_code_at_the_shown_grade(self):
        lead = frl.build_lead(["P0420", "C0460"], "P1")
        assert (lead.code, lead.verdict) == ("C0460", "limited")


class TestTieBreaks:
    TABLE = {
        "A1": {"severity": "high", "system": "steering", "safe_to_drive": "limited"},
        "A2": {"severity": "critical", "system": "brakes", "safe_to_drive": "limited"},
    }

    def test_same_verdict_the_more_severe_code_leads_in_either_order(self):
        assert frl.pick_primary_code(["A1", "A2"], self.TABLE)[0] == "A2"
        assert frl.pick_primary_code(["A2", "A1"], self.TABLE)[0] == "A2"

    def test_a_padded_code_is_matched(self):
        lead = frl.build_lead([" C0460 "], "P1")
        assert lead is not None and lead.code == "C0460"


class TestVerdictBeforeSeverity:
    TABLE = {
        "H1": {"severity": "high", "system": "steering", "safe_to_drive": "yes"},
        "M1": {"severity": "moderate", "system": "brakes", "safe_to_drive": "limited"},
    }

    def test_a_stricter_verdict_beats_a_more_severe_code_in_either_order(self):
        assert frl.pick_primary_code(["H1", "M1"], self.TABLE)[0] == "M1"
        assert frl.pick_primary_code(["M1", "H1"], self.TABLE)[0] == "M1"


class TestImpliedLevelOnAMalformedTable:
    def test_an_unmapped_severity_has_no_implied_level_so_no_lead(self):
        table = {"Z1": {"severity": "catastrophic", "system": "steering", "safe_to_drive": "limited"}}
        assert frl.implied_level(["Z1"], table) is None
        assert frl.build_lead(["Z1"], "P1", table=table) is None
        assert frl.build_lead(["Z1"], "P2", table=table) is None

    def test_one_unmapped_code_beside_a_mappable_one_still_has_no_level(self):
        """Skipping the malformed row would let the mappable code's P1 stand in for a
        vehicle whose true level the table cannot say."""
        table = {
            "Z1": {"severity": "catastrophic", "system": "steering", "safe_to_drive": "limited"},
            "C0460": {"severity": "high", "system": "steering", "safe_to_drive": "limited"},
        }
        assert frl.implied_level(["Z1", "C0460"], table) is None
        assert frl.build_lead(["Z1", "C0460"], "P1", table=table) is None


class TestTableLoading:
    def test_an_empty_file_is_not_a_table_and_is_not_cached(self, monkeypatch, tmp_path):
        empty = tmp_path / "dtc_severity.yaml"
        empty.write_text("")
        frl._TABLE_CACHE.clear()
        monkeypatch.setattr(frl, "SEVERITY_TABLE_PATH", empty)
        assert frl.load_severity_table() == {}
        assert "table" not in frl._TABLE_CACHE

    def test_a_failed_read_is_not_cached(self, monkeypatch):
        frl._TABLE_CACHE.clear()
        monkeypatch.setattr(frl, "SEVERITY_TABLE_PATH", Path("/nonexistent/dtc_severity.yaml"))
        assert frl.load_severity_table() == {}
        monkeypatch.undo()
        # No cache clearing here: the point is that the failure left nothing behind.
        assert frl.load_severity_table().get("C0460", {}).get("safe_to_drive") == "limited"

    def test_a_good_read_is_cached(self):
        frl._TABLE_CACHE.clear()
        first = frl.load_severity_table()
        assert frl.load_severity_table() is first


class TestEveryCodeInTheTable:
    """Across all codes, against the real classifier's grade."""

    def test_no_code_makes_the_lead_contradict_its_own_classification(self):
        parity = _parity()
        checked = 0
        for code in _TABLE:
            level = parity.level_for(code)
            lead = frl.build_lead([code], level)
            if level == "P0":
                assert lead is None
                continue
            assert lead is not None, code
            assert lead.verdict == _TABLE[code]["safe_to_drive"], code
            assert narration_conflict(severity_claims(lead.text), level) is None, (code, level)
            # bidi_app quotes the lead in its note; a `"` inside would end the span early
            # (review cycle 2, Suggestion 2).
            assert '"' not in lead.text, (code, lead.text)
            checked += 1
        assert checked > 20


# ---------------------------------------------------------------------------
# Delivery: driven through the real output callable
# ---------------------------------------------------------------------------


def _center():
    return {
        "name": "Meridian Midtown Atlanta",
        "address": "3200 Peachtree Road NE, Atlanta GA",
        "distanceMiles": 0.4,
        "nextAvailableSlots": ["Friday Oct 30 at 3:00 PM"],
    }


_REAL = object()  # "the tier the client was shown is the classifier's grade" (what a live session has)


def _drive(*, classification="P1", codes=("C0460",), state_extra=None, ctx_extra=None, calls=1, found=True,
           surfaced=_REAL):
    """Drive find_service_center results through the real output callable.

    ``surfaced`` is ``state["surfaced_classification"]``. By default it is the classifier's
    grade, which is what the classification mirror has set by the time the model's tool
    result arrives; ``surfaced=None`` is the instant before the mirror ran.
    """
    ws = AsyncMock()
    iq = SimpleNamespace(submit=AsyncMock(), cancel_deferred=lambda: None, mark_response_complete=lambda: None)
    state = {
        "event_count": 0,
        "type_counts": {},
        "tool_use_names": {},
        "injection_queue": iq,
        # The callable mirrors a change of ctx.triage_classification to the client; start in sync.
        "last_classification": classification,
        # A live session always has one (the prompt names it); the lead note must carry it.
        "platform_note_tag": TAG,
    }
    state["surfaced_classification"] = classification if surfaced is _REAL else surfaced
    state.update(state_extra or {})
    ctx = SimpleNamespace(
        session_id="test-lead",
        triage_classification=classification,
        triage_active_dtcs=list(codes),
        last_offered_service_center=None,
        vin="VIN-TEST",
    )
    for k, v in (ctx_extra or {}).items():
        setattr(ctx, k, v)
    result = {"found": found, "centers": [_center()] if found else []}

    async def _go():
        out = bidi._make_output_callable(ws, ctx, state, SimpleNamespace(), SimpleNamespace(send=AsyncMock()))
        for i in range(calls):
            await out({
                "type": "tool_use_stream",
                "current_tool_use": {"name": "find_service_center", "toolUseId": f"tu-{i}", "input": {}},
            })
            await out({
                "type": "tool_result",
                "tool_result": {"toolUseId": f"tu-{i}", "content": [{"json": result}]},
            })

    asyncio.run(_go())
    return ws, iq, state, ctx


def _transcripts(ws):
    return [
        c.args[0] for c in ws.send_json.await_args_list
        if c.args and c.args[0].get("type") == "transcript"
    ]


def _payloads(iq):
    return [c.args[0].payload for c in iq.submit.await_args_list]


OLD_NARRATION = (
    "I found Meridian Midtown Atlanta at 3200 Peachtree Road NE, Atlanta GA."
    " That's about 0.4 miles from you. Would you like me to book that for you?"
)
LEAD_TEXT = f"{C0460_SENTENCE} {LIMITED_PHRASE}"
# Pinned literally: the session's platform tag, the note that introduces the fixed words, and
# the opening quote. Only the lead is quoted; the tool's offer follows "Then tell the driver:".
TAG = "tag-lead-test"
LEAD_NOTE = (
    f"Platform note [{TAG}]: "
    'Say this to the driver word for word, exactly as written, adding and changing nothing: "'
)
LEAD_PAYLOAD = f'{LEAD_NOTE}{LEAD_TEXT}" Then tell the driver: {OLD_NARRATION}'


class TestDelivery:
    def test_the_model_is_told_to_say_the_fault_the_verdict_then_the_offer(self):
        _ws, iq, _state, _ctx = _drive()
        assert _payloads(iq) == [LEAD_PAYLOAD]

    def test_the_note_with_a_lead_says_word_for_word_and_the_one_without_does_not(self):
        """The strict lead-in belongs to fixed words only; every other service-center answer keeps
        the ordinary note."""
        _ws, iq, _state, _ctx = _drive()
        assert _payloads(iq)[0].startswith(LEAD_NOTE)
        _ws, iq2, _state2, _ctx2 = _drive(classification=None, codes=())
        assert _payloads(iq2) == [f"Tell the driver: {OLD_NARRATION}"]
        assert "word for word" not in _payloads(iq2)[0]

    def test_the_injection_keeps_its_priority_and_reason(self):
        _ws, iq, _state, _ctx = _drive()
        req = iq.submit.await_args_list[0].args[0]
        assert (req.priority, req.reason) == (bidi.InjectionPriority.IMMEDIATE, "service-center-found")

    def test_nothing_is_sent_to_the_screen_ios_would_suppress_or_double_it(self):
        """Review cycle 1, Critical: a transcript flagged `_synthetic_from_auto_book` is
        suppressed by iOS as its own echo, and an unflagged one renders twice beside the
        model's sentences. The words go through the injection only."""
        ws, _iq, _state, _ctx = _drive()
        assert _transcripts(ws) == []

    def test_the_verdict_comes_from_the_codes_triage_used(self):
        _ws, iq, _state, _ctx = _drive(classification="P2", codes=("P0420",))
        assert _payloads(iq)[0].startswith(
            LEAD_NOTE + "Your car has reported a fault in the emissions system. " + YES_PHRASE
        )

    def test_once_per_session(self):
        _ws, iq, state, _ctx = _drive(calls=2)
        payloads = _payloads(iq)
        assert payloads[0].startswith(LEAD_NOTE + C0460_SENTENCE)
        assert payloads[1] == f"Tell the driver: {OLD_NARRATION}"
        assert state["first_reply_lead_sent"] is True

    def test_it_is_the_first_service_center_answer_not_the_first_turn(self):
        """Documents the limit: a session whose first service-center answer comes on a
        later turn opens that answer with the fault."""
        _ws, iq, _state, _ctx = _drive(state_extra={"_narration_user_turn": 3})
        assert _payloads(iq) == [LEAD_PAYLOAD]

    def test_it_fires_in_the_state_a_live_session_has_the_tier_already_mirrored(self):
        """Review cycle 2, Warning 2: with the mirrored tier equal to the grade (what
        `_drive` now defaults to) the lead must fire; a guard that stands down on any
        surfaced tier would silently switch the feature off on the real take."""
        _ws, iq, state, _ctx = _drive(surfaced="P1")
        assert state["surfaced_classification"] == "P1"
        assert _payloads(iq) == [LEAD_PAYLOAD]

    def test_it_also_fires_before_the_tier_has_been_mirrored(self):
        _ws, iq, _state, _ctx = _drive(surfaced=None)
        assert _payloads(iq) == [LEAD_PAYLOAD]

    def test_p2_fires_when_the_mirrored_tier_is_p2(self):
        _ws, iq, _state, _ctx = _drive(classification="P2", codes=("P0420",), surfaced="P2")
        assert "fine to keep using the car" in _payloads(iq)[0]

    def test_the_booking_offer_state_is_unchanged(self):
        _ws, _iq, state, ctx = _drive()
        assert state["service_center_offered"]["name"] == "Meridian Midtown Atlanta"
        assert ctx.last_offered_service_center["name"] == "Meridian Midtown Atlanta"


class TestNoLeadLeavesTheOldNarration:
    def _assert_old(self, **kw):
        ws, iq, state, _ctx = _drive(**kw)
        assert _transcripts(ws) == []
        assert _payloads(iq) == [f"Tell the driver: {OLD_NARRATION}"]
        assert "first_reply_lead_sent" not in state

    def test_classifier_p0(self):
        self._assert_old(classification="P0", codes=("C1234",))

    def test_classifier_p0_even_when_the_codes_alone_imply_p1(self):
        """P0 can come from another input (telemetry, a tenant rule) while the code is a P1
        one. The hook must pass the classifier's own grade through, not rely on the table."""
        self._assert_old(classification="P0", codes=("C0460",))

    def test_no_triage_yet(self):
        self._assert_old(classification=None, codes=())

    def test_triage_with_no_active_code(self):
        self._assert_old(classification="P3", codes=())

    @pytest.mark.parametrize("classification,codes", [("P2", ("P0420",)), ("P3", ("P0442",))])
    def test_an_unclear_safety_answer_surfaced_p1_over_a_lower_grade(self, classification, codes):
        """Review cycle 2, Warning 1: `_handle_confirmation_response` surfaces P1 over the
        classifier's P2/P3 when the driver's answer is unclear and says "treat this as P1"."""
        self._assert_old(classification=classification, codes=codes, surfaced="P1")

    def test_the_surfaced_tier_is_p0_though_the_classifier_graded_lower(self):
        """Review cycle 1, Critical: a driver-confirmed or reported emergency keeps
        surfaced_classification at P0 while ctx.triage_classification holds the lower grade."""
        self._assert_old(surfaced="P0")

    def test_the_driver_confirmed_p0(self):
        self._assert_old(ctx_extra={"driver_confirmed_priority": "P0"})

    def test_the_driver_reported_a_hazard(self):
        self._assert_old(ctx_extra={"driver_reported_hazard": True})

    def test_a_safety_question_is_waiting_for_the_answer(self):
        self._assert_old(state_extra={"awaiting_confirmation_category": object()})

    def test_a_driver_confirmation_is_pending(self):
        self._assert_old(ctx_extra={"driver_confirmation_pending": True})

    def test_the_grade_is_not_the_one_the_codes_imply(self):
        """P0420 grades P2 from the table; a P1 came from another input, so 'fine to keep
        using the car' would sit beside an urgent grade."""
        self._assert_old(classification="P1", codes=("P0420",))

    def test_no_center_found_sends_nothing_and_keeps_the_lead_for_later(self):
        ws, iq, state, _ctx = _drive(found=False)
        assert _transcripts(ws) == []
        assert _payloads(iq) == []
        assert "first_reply_lead_sent" not in state

    def test_a_failure_building_the_lead_does_not_cost_the_driver_the_answer(self, monkeypatch):
        def boom(*_a, **_k):
            raise RuntimeError("table exploded")

        monkeypatch.setattr(bidi, "build_first_reply_lead", boom)
        try:
            ws, iq, state, _ctx = _drive()
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"a failure building the lead reached the driver's answer: {exc!r}")
        assert _transcripts(ws) == []
        assert _payloads(iq) == [f"Tell the driver: {OLD_NARRATION}"]
        assert "first_reply_lead_sent" not in state
