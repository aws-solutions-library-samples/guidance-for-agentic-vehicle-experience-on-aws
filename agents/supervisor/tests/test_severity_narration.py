"""The assistant must narrate the classifier's severity (issue
issues/2026-10-05-narrated-severity-not-bound-to-classifier/).

These tests are the enforcement: the prompt rule alone did not hold on 2026-09-25,
when the classifier returned P3 and the assistant said "this is a P0-level
critical fault… stop driving immediately".
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agents.supervisor import agentcore_app, bidi_app
from agents.supervisor import severity_narration as sn
from agents.supervisor.dtc_response_catalog import RESPONSE_TEMPLATES, p0_directive
from agents.supervisor.tools import diy_repair_advisor as diy

OBSERVED_2026_09_25 = "This is a P0-level critical fault… stop driving immediately."

# ---------------------------------------------------------------------------
# severity_claims
# ---------------------------------------------------------------------------

CLAIM_CASES = [
    (OBSERVED_2026_09_25, {"stop"}),
    ("This is critical, pull over right now.", {"stop"}),
    ("Please do not drive the truck.", {"stop"}),
    ("Don't drive it until it's checked.", {"stop"}),
    ("It isn't safe to drive right now.", {"stop"}),
    ("It's not safe to drive.", {"stop"}),
    ("It's unsafe to drive.", {"stop"}),
    ("Don't keep driving on it.", {"stop"}),
    ("This is urgent: get it serviced within 48 hours.", {"urgent"}),
    ("Please get it in as soon as possible.", {"urgent"}),
    ("It's safe to keep driving.", {"routine"}),
    ("You're fine to drive to the shop.", {"routine"}),
    ("This is not urgent and it can wait.", {"routine"}),
    ("It isn't urgent.", {"routine"}),
    ("This is not an emergency.", {"not_stop"}),
    ("I wouldn't say it's critical.", {"not_stop"}),
    ("The systems didn't flag it as an emergency.", {"not_stop"}),
    ("Set the emergency brake before you get out.", set()),
    ("Your emergency flashers are working fine.", set()),
    ("It's critical to keep the tires at the right pressure.", set()),
    ("This is a monitor-only signal, nothing to worry about.", {"routine"}),
    ("No rush, you can continue driving.", {"routine"}),
    ("Your turbocharger has low boost pressure, code P0299.", set()),
    ("I can book you at AutoNation Halcyon on Thursday at 11 AM.", set()),
    ("", set()),
]


@pytest.mark.parametrize("text,expected", CLAIM_CASES)
def test_severity_claims(text, expected):
    assert sn.severity_claims(text) == expected


def test_dtc_codes_are_not_priority_codes():
    assert sn.severity_claims("Code P0420 and P1234 are stored.") == set()


# ---------------------------------------------------------------------------
# narration_conflict: every tier x claim class
# ---------------------------------------------------------------------------

MATRIX = {
    ("P0", "stop"): None, ("P0", "urgent"): None, ("P0", "routine"): "under", ("P0", "not_stop"): "under",
    ("P1", "stop"): "over", ("P1", "urgent"): None, ("P1", "routine"): "under", ("P1", "not_stop"): None,
    ("P2", "stop"): "over", ("P2", "urgent"): "over", ("P2", "routine"): None, ("P2", "not_stop"): None,
    ("P3", "stop"): "over", ("P3", "urgent"): "over", ("P3", "routine"): None, ("P3", "not_stop"): None,
}


@pytest.mark.parametrize("tier,claim", sorted(MATRIX))
def test_conflict_matrix(tier, claim):
    assert sn.narration_conflict({claim}, tier) == MATRIX[(tier, claim)]


@pytest.mark.parametrize("tier", [None, ""])
def test_no_verdict_is_never_a_conflict(tier):
    assert sn.narration_conflict({"stop", "routine"}, tier) is None


def test_no_claim_is_never_a_conflict():
    for tier in ("P0", "P1", "P2", "P3"):
        assert sn.narration_conflict(set(), tier) is None


def test_observed_sentence_on_p3_is_over():
    assert sn.check_turn(OBSERVED_2026_09_25, "P3") == "over"


def test_downplaying_a_p0_is_under():
    assert sn.check_turn("Nothing to worry about, it's safe to keep driving.", "P0") == "under"


# ---------------------------------------------------------------------------
# Platform lines are never read as the model's claim
# ---------------------------------------------------------------------------

def _all_p0_directives() -> list[str]:
    out = []
    for template_id in RESPONSE_TEMPLATES:
        fault = [{"code": "X", "template_id": template_id}]
        out.append(p0_directive(fault, None))
        out.append(p0_directive(fault, "Marcus"))
    return out


@pytest.mark.parametrize("directive", _all_p0_directives())
def test_every_p0_directive_is_consistent_on_p0(directive):
    assert sn.check_turn(directive, "P0", [directive]) is None
    # and on its own words, without being told it is a platform line
    assert sn.narration_conflict(sn.severity_claims(directive), "P0") is None


@pytest.mark.parametrize("directive", _all_p0_directives())
def test_repeating_a_directive_is_stripped(directive):
    """The model repeating the directive is not its own claim, whatever the tier
    (a stale tier must not turn the directive into a conflict)."""
    assert sn.check_turn(directive, "P3", [directive]) is None


@pytest.mark.parametrize("tier", ["P1", "P2", "P3"])
def test_the_model_repeating_a_correction_does_not_trigger_another(tier):
    for line in sn.CORRECTION_LINES:
        assert sn.check_turn(line, tier) is None


def test_a_claim_next_to_a_stripped_line_is_still_seen():
    directive = _all_p0_directives()[0]
    text = f"{directive} But honestly it's safe to keep driving."
    assert sn.check_turn(text, "P0", [directive]) == "under"


# ---------------------------------------------------------------------------
# Correction copy
# ---------------------------------------------------------------------------

def test_over_correction_never_reassures():
    """A correction never weakens a stop instruction into reassurance."""
    assert "routine" not in sn.severity_claims(sn.OVER_COPY)
    assert "safe to" not in sn.OVER_COPY.lower()


def test_over_correction_does_not_trip_its_own_detector():
    """Review cycle 1 W2: the copy, and a paraphrase of it, read as not_stop, so a
    model repeating it in its own words does not trigger another correction."""
    assert sn.severity_claims(sn.OVER_COPY) == {"not_stop"}
    for tier in ("P1", "P2", "P3"):
        assert sn.check_turn("The vehicle's systems didn't flag it as an emergency.", tier) is None


def test_a_correct_p1_sentence_is_not_flagged():
    """Review cycle 1 W1: 'not an emergency, but within 48 hours' is exactly P1."""
    assert sn.check_turn("This isn't an emergency, but it needs service within 48 hours.", "P1") is None


def test_not_an_emergency_on_a_p0_is_under():
    assert sn.check_turn("This is not an emergency.", "P0") == "under"


def test_p1_correction_states_urgency():
    assert sn.severity_claims(sn.P1_UNDER_COPY) == {"urgent"}


def test_p0_under_correction_is_the_directive_verbatim():
    d = _all_p0_directives()[0]
    assert sn.correction_line("under", "P0", d) == d
    assert sn.correction_line("under", "P1", d) == sn.P1_UNDER_COPY
    for tier in ("P1", "P2", "P3"):
        assert sn.correction_line("over", tier, d) == sn.OVER_COPY


# ---------------------------------------------------------------------------
# Bidi wiring
# ---------------------------------------------------------------------------

class _WS:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


class _IQ:
    def __init__(self):
        self.submitted = []

    async def submit(self, req):
        self.submitted.append(req)

    # The output callable also calls these on every turn.
    def clear_deferred_block(self):
        pass

    def cancel_deferred(self):
        pass

    def mark_response_complete(self):
        pass


def _bidi_state(classification, **extra):
    st = {
        "surfaced_classification": classification,
        "platform_note_tag": "abcd1234",
        "injection_queue": _IQ(),
        "active_dtcs_for_prompt": [],
        "awaiting_confirmation_category": None,
    }
    st.update(extra)
    return st


def _bidi_ctx():
    return SimpleNamespace(session_id="s1", driver_first_name=None, triage_active_dtcs=[])


def _run_check(text, state):
    ws = _WS()
    asyncio.run(bidi_app._check_narration_severity_on_turn_end(ws, _bidi_ctx(), state, text))
    return ws


def test_bidi_over_conflict_shows_the_correction_and_tells_the_model():
    state = _bidi_state("P3")
    ws = _run_check(OBSERVED_2026_09_25, state)
    assert len(ws.sent) == 1
    line = ws.sent[0]
    assert line["type"] == "transcript" and line["role"] == "assistant" and line["isFinal"] is True
    assert line["text"] == sn.OVER_COPY
    assert line["_platform_severity_correction"] is True
    assert "_platform_safety_directive" not in line
    (note,) = state["injection_queue"].submitted
    assert note.payload.startswith("Platform note [abcd1234]:")
    assert sn.OVER_COPY in note.payload


def test_bidi_p0_under_conflict_reshows_the_directive():
    directive = _all_p0_directives()[0]
    state = _bidi_state("P0", p0_directive=directive)
    ws = _run_check("Don't worry, it's safe to keep driving.", state)
    assert ws.sent[0]["text"] == directive
    assert ws.sent[0]["_platform_safety_directive"] is True


def test_bidi_p0_under_without_a_shown_directive_still_sends_one():
    state = _bidi_state("P0")
    ws = _run_check("It's fine to drive.", state)
    assert ws.sent and ws.sent[0]["text"].strip()


def test_bidi_consistent_turn_sends_nothing():
    state = _bidi_state("P3")
    ws = _run_check("This is a monitor-only signal, no ticket needed.", state)
    assert ws.sent == [] and state["injection_queue"].submitted == []


def test_bidi_one_correction_per_turn():
    state = _bidi_state("P3")
    _run_check(OBSERVED_2026_09_25, state)
    ws = _run_check(OBSERVED_2026_09_25, state)
    assert ws.sent == []


def test_bidi_no_correction_while_a_driver_confirmation_is_pending():
    state = _bidi_state("P3", awaiting_confirmation_category=object())
    ws = _run_check(OBSERVED_2026_09_25, state)
    assert ws.sent == []


def test_bidi_no_verdict_no_correction():
    ws = _run_check(OBSERVED_2026_09_25, _bidi_state(None))
    assert ws.sent == []


def test_bidi_logs_without_transcript_text(caplog):
    caplog.set_level("WARNING")
    _run_check(OBSERVED_2026_09_25, _bidi_state("P3"))
    lines = [r.getMessage() for r in caplog.records if "narration-severity" in r.getMessage()]
    assert lines == ["narration-severity: conflict=over classification=P3 session=s1"]


def test_bidi_turn_text_is_collected_across_deltas():
    """The claim is split across deltas; only the final delta ends the turn."""
    state = {}
    assert bidi_app._narration_take_turn_text(state, "This is a P0-level ", False) is None
    assert bidi_app._narration_take_turn_text(state, "critical fault, ", False) is None
    full = bidi_app._narration_take_turn_text(state, "let me find you a shop.", True)
    # The claim is only in the earlier deltas; the final delta alone makes none.
    assert sn.check_turn("let me find you a shop.", "P3") is None
    assert sn.check_turn(full, "P3") == "over"
    assert bidi_app._narration_take_turn_text(state, "Next turn.", True) == "Next turn."


# (A source-order test stood here; the behavioural output-path tests below
# replace it — review cycle 2 C1.)


# ---------------------------------------------------------------------------
# Text runtime wiring
# ---------------------------------------------------------------------------

def _text_ctx(classification, say_first=None):
    return SimpleNamespace(
        triage_classification=classification, p0_say_first_given=say_first,
        driver_first_name=None, session_id="t1",
    )


def test_text_over_conflict_appends_the_correction():
    out = agentcore_app._apply_narration_severity_correction(OBSERVED_2026_09_25, _text_ctx("P3"))
    assert out.startswith(OBSERVED_2026_09_25) and out.endswith(sn.OVER_COPY)


def test_text_p1_under_appends_the_p1_line():
    out = agentcore_app._apply_narration_severity_correction("No rush, it can wait.", _text_ctx("P1"))
    assert out.endswith(sn.P1_UNDER_COPY)


def test_text_p0_under_leads_with_the_directive_once():
    directive = _all_p0_directives()[0]
    reply = "It's safe to keep driving."
    out = agentcore_app._apply_narration_severity_correction(reply, _text_ctx("P0", directive))
    assert out.startswith(directive)
    led = agentcore_app._lead_with_p0_line(reply, _text_ctx("P0", directive))
    again = agentcore_app._apply_narration_severity_correction(led, _text_ctx("P0", directive))
    assert again.count(directive) == 1


def test_text_consistent_reply_unchanged():
    reply = "This is not urgent; I can book a convenient appointment."
    assert agentcore_app._apply_narration_severity_correction(reply, _text_ctx("P2")) == reply


def test_text_no_verdict_unchanged():
    assert agentcore_app._apply_narration_severity_correction(OBSERVED_2026_09_25, _text_ctx(None)) == OBSERVED_2026_09_25


def test_text_runtime_applies_the_check_after_the_p0_line():
    # Both steps live in _finish_reply since 2026-10-06 (a hazard report skips them).
    import inspect
    src = inspect.getsource(agentcore_app._finish_reply)
    assert src.index("result_text = _lead_with_p0_line(result_text, ctx)") < src.index(
        "_apply_narration_severity_correction(result_text, ctx, user_text)"
    )
    assert "result_text = _finish_reply(result_text, ctx, _user_text or prompt, _hazard)" in open(
        agentcore_app.__file__).read()


# ---------------------------------------------------------------------------
# No-DIY gate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tier", ["P0", "P1"])
def test_diy_refused_on_urgent_verdicts(tier):
    ctx = SimpleNamespace(triage_classification=tier)
    assert diy._diy_gate_check(ctx, "how do I top up washer fluid", "")["diyAllowed"] is False


def test_diy_refused_on_driver_confirmed_emergency():
    ctx = SimpleNamespace(triage_classification="P3", driver_confirmed_priority="P0")
    assert diy._diy_gate_check(ctx, "can I fix it", "") is not None


@pytest.mark.parametrize("code", ["P0299", "P0420", "C1234", "B0001", "U0100", "B0001_FIRE", "U3000_CRASH", "garbage"])
def test_diy_refused_for_any_named_code(code):
    ctx = SimpleNamespace(triage_classification="P3")
    assert diy._diy_gate_check(ctx, "walk me through it", code) is not None


@pytest.mark.parametrize("query", [
    "Can I fix P0299 myself?", "what does p0420 mean, can I do it", "code B0001_FIRE again",
])
def test_diy_refused_for_a_code_in_the_query(query):
    ctx = SimpleNamespace(triage_classification="P3")
    assert diy._diy_gate_check(ctx, query, "") is not None


def test_diy_allowed_for_a_quick_fix_with_no_code():
    ctx = SimpleNamespace(triage_classification="P3")
    assert diy._diy_gate_check(ctx, "my tire pressure is low, can I inflate it myself?", "") is None


def test_diy_refusal_is_a_copy():
    a = diy._diy_gate_check(SimpleNamespace(triage_classification="P0"), "", "")
    a["diyAllowed"] = True
    assert diy._DIY_BLOCKED_RESPONSE["diyAllowed"] is False


def test_diy_tool_refuses_before_any_lookup(monkeypatch):
    calls = []
    monkeypatch.setattr(diy, "make_sub_agent_tool", lambda cfg, ctx=None: (lambda **kw: calls.append(("sub", kw)) or {}))
    monkeypatch.setattr(diy, "_kb_dtc_snippets", lambda *a: calls.append(("kb", a)) or [])
    tool = diy.make_diy_repair_advisor_tool(SimpleNamespace(triage_classification="P3"), SimpleNamespace(adp_kb_id="kb"))
    fn = getattr(tool, "_tool_func", None) or getattr(tool, "__wrapped__", None) or tool
    out = fn(query="engine light P0299, can I fix it?", dtc_code="")
    assert out["diyAllowed"] is False
    assert calls == []


# ---------------------------------------------------------------------------
# Review cycle 1: a driver-reported emergency is never taken back
# ---------------------------------------------------------------------------

def test_user_final_starts_a_new_turn_and_records_a_reported_emergency():
    state = {"_narration_user_turn": 3}
    bidi_app._narration_on_user_final(state, "there is smoke coming from the hood")
    assert state["_narration_user_turn"] == 4
    assert state["_narration_driver_reported_emergency"] is True


def test_user_final_without_an_emergency_leaves_the_flag_unset():
    state = {}
    bidi_app._narration_on_user_final(state, "what time is my appointment")
    assert state["_narration_user_turn"] == 1
    assert not state.get("_narration_driver_reported_emergency")


def test_gate_rearm_lets_the_next_turn_be_corrected():
    """Review cycle 1 W3 (mutant R3 survived): without the re-arm the second
    turn's contradiction would go uncorrected."""
    state = _bidi_state("P3")
    _run_check(OBSERVED_2026_09_25, state)
    bidi_app._narration_on_user_final(state, "ok, what should I do")
    ws = _run_check(OBSERVED_2026_09_25, state)
    assert len(ws.sent) == 1


def test_over_is_not_corrected_after_the_driver_reports_an_emergency():
    """Review cycle 1 Critical: P3 on the first turn, then 'smoke from the hood';
    the model's 'pull over now' must not be taken back."""
    state = _bidi_state("P3")
    bidi_app._narration_on_user_final(state, "now there is smoke coming from the hood")
    ws = _run_check("Pull over now and get out of the vehicle.", state)
    assert ws.sent == [] and state["injection_queue"].submitted == []


def test_under_is_still_corrected_after_a_reported_emergency():
    directive = _all_p0_directives()[0]
    state = _bidi_state("P0", p0_directive=directive)
    bidi_app._narration_on_user_final(state, "smoke coming from the hood")
    # Review cycle 2 C2: the precondition is asserted, so the test cannot pass vacuously.
    assert state["_narration_driver_reported_emergency"] is True
    ws = _run_check("It's safe to keep driving.", state)
    assert ws.sent and ws.sent[0]["text"] == directive


def test_diy_does_not_read_english_words_as_codes():
    ctx = SimpleNamespace(triage_classification="P3")
    assert diy._diy_gate_check(ctx, "the light paced on and off and then ceded", "") is None
    assert diy._diy_gate_check(ctx, "what about P0420", "") is not None


# ---------------------------------------------------------------------------
# Review cycle 2
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,tier,expected", [
    ("It's not critical to stop right now.", "P0", "under"),
    ("It isn't critical to pull over.", "P0", "under"),
    ("No warning lights mean it's safe to drive.", "P3", None),
    ("There's no doubt this is an emergency.", "P0", None),
    ("The vehicle's systems didn't flag it as an emergency.", "P3", None),
])
def test_cycle2_negation_cases(text, tier, expected):
    assert sn.check_turn(text, tier) == expected


@pytest.mark.parametrize("text", [
    "I smell smoke", "there is smoke", "the brakes stopped working", "my brakes failed",
    "I can't steer", "there's a fire", "I was in a crash", "it's not safe to drive",
])
def test_driver_hazard_words(text):
    assert sn.driver_reports_hazard(text)


@pytest.mark.parametrize("text", ["what time is my appointment", "my tire pressure is low", ""])
def test_no_driver_hazard(text):
    assert not sn.driver_reports_hazard(text)


def _drive_output(events, state):
    """Drive events through the real bidi output callable (review cycle 2 C1)."""
    from unittest.mock import AsyncMock
    ws = AsyncMock()
    ctx = SimpleNamespace(session_id="s-out", triage_classification="P3", vin="VIN-TEST",
                          driver_first_name=None, triage_active_dtcs=[])

    async def _go():
        out = bidi_app._make_output_callable(ws, ctx, state, SimpleNamespace(), SimpleNamespace())
        for e in events:
            await out(e)
        for t in list(state.get("_narration_tasks") or []):
            await t

    asyncio.run(_go())
    return [c.args[0] for c in ws.send_json.call_args_list]


def _out_state():
    st = _bidi_state("P3")
    st.update({"event_count": 0, "type_counts": {}, "tool_use_names": {},
               "escalation_tool_in_flight_at": None, "auto_triage_fired": True,
               "last_classification": None})
    return st


def _user(text):
    return {"type": "bidi_transcript_stream", "role": "user", "text": text, "is_final": True}


def _assistant(text):
    return {"type": "bidi_transcript_stream", "role": "assistant", "text": text, "is_final": True}


def _corrections(sent):
    return [m for m in sent if isinstance(m, dict) and m.get("_platform_severity_correction")]


def test_output_path_reported_emergency_keeps_the_stop_instruction():
    state = _out_state()
    sent = _drive_output([_user("now there is smoke coming from the hood"),
                          _assistant("Pull over now and get out of the vehicle.")], state)
    assert state["_narration_driver_reported_emergency"] is True
    assert _corrections(sent) == []


def test_output_path_corrects_when_no_emergency_was_reported():
    state = _out_state()
    sent = _drive_output([_user("what does this light mean"),
                          _assistant("This is a P0-level critical fault, stop driving.")], state)
    assert len(_corrections(sent)) == 1


def test_output_path_rearms_the_gate_each_user_turn():
    state = _out_state()
    sent = _drive_output([_user("what does this light mean"),
                          _assistant("This is critical, stop driving."),
                          _user("really?"),
                          _assistant("Yes, this is critical, stop driving.")], state)
    assert len(_corrections(sent)) == 2


def test_text_runtime_keeps_a_stop_after_a_reported_hazard():
    reply = "Pull over now. This is an emergency."
    out = agentcore_app._apply_narration_severity_correction(reply, _text_ctx("P3"), "I smell smoke")
    assert out == reply


def test_text_runtime_still_corrects_without_a_reported_hazard():
    reply = "Pull over now. This is an emergency."
    out = agentcore_app._apply_narration_severity_correction(reply, _text_ctx("P3"), "what does this light mean")
    assert out.endswith(sn.OVER_COPY)


def test_text_runtime_directive_mid_reply_is_not_added_again():
    """Review cycle 2 M4: the directive anywhere in the reply counts."""
    directive = _all_p0_directives()[0]
    reply = f"Okay. {directive} Also it's safe to keep driving."
    out = agentcore_app._apply_narration_severity_correction(reply, _text_ctx("P0", directive), "")
    assert out.count(directive) == 1


# ---------------------------------------------------------------------------
# Review cycle 2 W4: the chat failed but the P0 call was placed
# ---------------------------------------------------------------------------

def test_escalate_tool_reports_a_placed_call_on_a_502():
    from unittest.mock import MagicMock, patch
    from agents.supervisor.tools import escalate as esc
    resp = MagicMock(status_code=502)
    resp.json.return_value = {"callbackInitiated": True, "error": "chat failed"}
    resp.text = '{"callbackInitiated": true}'
    ctx = SimpleNamespace(jwt="t", session_id="s", tenant_id="meridian", vin="V", vehicle_id="VEH-1",
                          driver_id="D", triage_classification="P0", triage_decision_id="d")
    with patch.object(esc.requests, "post", return_value=resp):
        with pytest.raises(esc.EscalateCallbackPlacedError) as exc:
            esc.call_escalate_endpoint(ctx, SimpleNamespace(api_base="https://api.invalid"),
                                       severity="P0", reason="r", summary="s")
    assert str(exc.value) == "The in-app chat couldn't open, but a support agent is calling your phone now."


def test_escalate_tool_plain_error_without_a_call():
    from unittest.mock import MagicMock, patch
    from agents.supervisor.tools import escalate as esc
    resp = MagicMock(status_code=502)
    resp.json.return_value = {"callbackInitiated": False, "error": "chat failed"}
    resp.text = "x"
    ctx = SimpleNamespace(jwt="t", session_id="s", tenant_id="meridian", vin="V", vehicle_id="VEH-1",
                          driver_id="D", triage_classification="P0", triage_decision_id="d")
    with patch.object(esc.requests, "post", return_value=resp):
        with pytest.raises(esc.EscalateError) as exc:
            esc.call_escalate_endpoint(ctx, SimpleNamespace(api_base="https://api.invalid"),
                                       severity="P0", reason="r", summary="s")
    assert not isinstance(exc.value, esc.EscalateCallbackPlacedError)


@pytest.mark.parametrize("text", ["I smell smoke", "the brakes stopped working"])
def test_bidi_records_hazards_the_confirmation_matcher_misses(text):
    """Review cycle 2: the confirmation matcher needs exact phrases; these reports
    must still keep the model's stop instruction from being taken back."""
    from agents.supervisor.triage_keywords import match_emergency_keywords
    assert match_emergency_keywords(text) is None  # precondition: the matcher misses it
    state = {}
    bidi_app._narration_on_user_final(state, text)
    assert state["_narration_driver_reported_emergency"] is True



# ---------------------------------------------------------------------------
# Review cycle 3 W1: only REPORTS of a hazard switch the over-correction off
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "my engine light came on, is this critical?", "is this an emergency", "what does P0 mean?",
    "there's no smoke", "the app crashed", "forward collision warning light is on",
])
def test_questions_denials_and_lamp_names_are_not_reports(text):
    assert not sn.driver_reports_hazard(text)


@pytest.mark.parametrize("text", [
    "is there smoke? no. but I smell fuel", "I was in a crash", "my brakes failed",
])
def test_a_report_after_a_question_still_counts(text):
    assert sn.driver_reports_hazard(text)


def test_output_path_a_question_does_not_switch_the_correction_off():
    """Reviewer's reproduction: a question, then the 2026-09-25 sentence on P3."""
    state = _out_state()
    sent = _drive_output([_user("my engine light came on, is this critical?"),
                          _assistant(OBSERVED_2026_09_25)], state)
    assert not state.get("_narration_driver_reported_emergency")
    assert len(_corrections(sent)) == 1


def test_text_runtime_a_question_does_not_switch_the_correction_off():
    out = agentcore_app._apply_narration_severity_correction(
        OBSERVED_2026_09_25, _text_ctx("P3"), "my engine light came on, is this critical?",
    )
    assert out.endswith(sn.OVER_COPY)


def test_text_runtime_checks_user_text_before_the_prompt():
    src = open(agentcore_app.__file__).read()
    assert "_finish_reply(result_text, ctx, _user_text or prompt, _hazard)" in src


def test_bidi_failure_event_carries_the_call_correlation_id():
    from unittest.mock import AsyncMock, patch
    from agents.supervisor.tools.escalate import EscalateCallbackPlacedError
    err = EscalateCallbackPlacedError("support is calling")
    err.correlation_id = "corr-1"
    ws = AsyncMock()
    ctx = SimpleNamespace(session_id="s", tenant_id="t", vin="V", vehicle_id="VEH-1", jwt="j",
                          driver_id="D", triage_classification="P0", triage_decision_id="d",
                          driver_display_name=None)

    async def _go():
        with patch.object(bidi_app, "call_escalate_endpoint", side_effect=err):
            await bidi_app._run_escalate_in_background(
                websocket=ws, ctx=ctx, config=SimpleNamespace(), user_utterance="smoke",
                booking_result=None, classification="P0",
            )

    asyncio.run(_go())
    ev = next(c.args[0] for c in ws.send_json.call_args_list if c.args[0].get("type") == "escalation")
    assert ev["callbackInitiated"] is True and ev["p0CallbackEscalationId"] == "corr-1"
