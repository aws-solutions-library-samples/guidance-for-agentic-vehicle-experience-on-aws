"""Directive first: a driver's report of fire, smoke or brake loss gets the safety
directive before anything else, unconditioned, and is never questioned
(issues/2026-10-06-fire-report-doubted-before-directive/).

What these tests check is ORDER, not wording. A denylist of doubting phrases plus a
"directive present" check both pass a reply that doubts first and gives the directive
later (``test_a_denylist_and_presence_check_pass_a_doubt_first_reply`` shows it), so the
property tested here is: in what the driver receives after the report, the first
assistant item is the catalog directive, and model words that did not lead with the
directive are never delivered, whatever those words are.

Observed 2026-10-05 22:32Z (booking VSA-2026-10-05-7D9C): to "my engine is on fire and
there's smoke coming from the hood" the assistant first said "I don't have live sensor
data to confirm flames or heavy smoke from your vehicle at this moment."
"""

from __future__ import annotations

import asyncio
import random
import pathlib
import re
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.supervisor import agentcore_app, bidi_app
from agents.supervisor import hazard_directive as hd
from agents.supervisor.dtc_response_catalog import RESPONSE_TEMPLATES, p0_directive
from agents.supervisor.triage_keywords import EMERGENCY_CATEGORIES, match_emergency_keywords

FIRE_REPORT = "My engine is on fire and there's smoke coming from the hood."
OBSERVED_DOUBT = (
    "I don't have live sensor data to confirm flames or heavy smoke from your vehicle "
    "at this moment."
)
EXIT = p0_directive([{"template_id": "exit_vehicle"}], None)
BRAKE = p0_directive([{"template_id": "brake_emergency"}], None)
CATEGORY = {c.name: c for c in EMERGENCY_CATEGORIES}

# Replies that question or doubt the report before (or instead of) the directive.
# Deliberately in words no denylist would hold: the property must not depend on them.
DOUBT_FIRST = [
    OBSERVED_DOUBT,
    f"{OBSERVED_DOUBT} {EXIT}",
    "I'm not seeing anything on my end that shows a fire. Can you describe it?",
    "Hmm, the vehicle's systems look normal. Are you sure? " + EXIT,
    "Okay. " + EXIT,
    "Let me check that for you.",
    "Before anything else, can you tell me what you see?",
    "Nothing in the data suggests flames, but " + EXIT[0].lower() + EXIT[1:],
    "Quantum zebras marmalade the telemetry.",
    "",
]
# Replies that lead with the directive (in the model's own rendering).
LEADING = [
    EXIT,
    EXIT + " You can tap Talk to an agent on your screen.",
    EXIT.replace("nine-one-one", "911") + " Stay well away from it.",
    EXIT.replace(" — ", ". ").replace(", get out", ". Get out"),
]


def _random_replies(n=40, seed=20261006):
    """Sentences of random words: the property holds for words nobody predicted."""
    rng = random.Random(seed)
    vocab = ("sensor data confirm flames smoke vehicle moment sure check tell see normal "
             "maybe probably cannot verify reading engine looks fine okay sorry help "
             "emergency stop call nine one now get out away").split()
    out = []
    for _ in range(n):
        out.append(" ".join(rng.choice(vocab) for _ in range(rng.randint(1, 18))).capitalize() + ".")
    return out


# ---------------------------------------------------------------------------
# What the driver must say for the rule to apply
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    (FIRE_REPORT, "fire_or_smoke"),
    ("there's smoke coming from the hood", "fire_or_smoke"),
    ("I see flames", "fire_or_smoke"),
    ("something is burning", "fire_or_smoke"),
    ("what do I do my car is on fire", "fire_or_smoke"),
    ("is there smoke coming from the hood?", "fire_or_smoke"),
    ("I can't believe it, there's smoke coming from the hood", "fire_or_smoke"),
    ("my brakes failed", "brake_failure"),
    ("I lost my brakes", "brake_failure"),
    ("no brakes!", "brake_failure"),
    ("the brakes aren't working", "brake_failure"),
    ("the pedal goes to the floor", "brake_failure"),
    ("my brakes are gone and there are flames", "fire_or_smoke"),
])
def test_reports_of_fire_smoke_or_brake_loss(text, expected):
    c = hd.reported_hazard(text)
    assert c is not None and c.name == expected


@pytest.mark.parametrize("text", [
    "there's no smoke coming from the hood",
    "I don't see flames",
    "there are no flames",
    "never any smoke coming from it",
    "what does this light mean",
    "my brakes are a little spongy",
    "I crashed into a pole",          # crash keeps the confirmation question
    "the steering wheel locked",      # steering too
    "",
    None,
])
def test_denials_and_other_words_are_not_reports(text):
    assert hd.reported_hazard(text) is None


def test_the_rule_covers_exactly_fire_smoke_and_brake_loss():
    assert hd.DIRECTIVE_FIRST_CATEGORIES == {"fire_or_smoke", "brake_failure"}
    assert {name for _, name in hd._REPORTS} == {"fire_or_smoke", "brake_failure"}


# A confirmation phrase too loose to be a report on its own; with more words it is
# judged by the report patterns, and on the first turn the question still covers it.
LOOSE_CONFIRMATION_PHRASES = {"smoke coming from"}


def test_confirmation_phrases_said_alone_are_reports_except_the_loose_ones():
    for c in EMERGENCY_CATEGORIES:
        if c.name not in hd.DIRECTIVE_FIRST_CATEGORIES:
            continue
        for phrase in c.phrases:
            if phrase in LOOSE_CONFIRMATION_PHRASES:
                continue
            assert hd.reported_hazard(f"{phrase}.") is not None, phrase


def test_platform_words_never_trigger_a_report():
    """The platform note and every catalog directive must not read as the driver
    reporting a hazard (an echo of them would otherwise re-trigger)."""
    state = {"platform_note_tag": "a1b2c3d4"}
    for name in hd.DIRECTIVE_FIRST_CATEGORIES:
        for confirmed in [None] + list(CATEGORY.values()):
            note = bidi_app._hazard_note(state, CATEGORY[name], EXIT, confirmed)
            assert hd.reported_hazard(note.replace("Platform note [a1b2c3d4]: ", "")) is None, note
            assert bidi_app._hazard_from_user_text(state, note) is None
    assert set(hd.CATEGORY_WORDS) == set(CATEGORY)
    for t in RESPONSE_TEMPLATES.values():
        assert hd.reported_hazard(t["first_sentence"]) is None


# ---------------------------------------------------------------------------
# The order property, stated as a checker, and why phrase checks are not enough
# ---------------------------------------------------------------------------


def _first_assistant_item(frames: list[dict]) -> dict | None:
    """The first assistant transcript or audio chunk after the driver's report."""
    seen_report = False
    for f in frames:
        if f.get("type") == "transcript" and f.get("role") == "user" and f.get("isFinal"):
            seen_report = True
            continue
        if not seen_report:
            continue
        if f.get("type") == "audio.chunk" or (f.get("type") == "transcript" and f.get("role") == "assistant"):
            return f
    return None


def _naive_denylist_and_presence_check(reply: str, directive: str) -> bool:
    """The check the user warned about: no listed doubting phrase, directive present."""
    denylist = ("sensor data to confirm", "can't confirm", "cannot confirm", "are you sure")
    return directive in reply and not any(p in reply.lower() for p in denylist)


def test_a_denylist_and_presence_check_pass_a_doubt_first_reply():
    reply = "Nothing in the data suggests flames from where I sit. " + EXIT
    assert _naive_denylist_and_presence_check(reply, EXIT)  # passes the phrase check
    assert hd.leads_with(reply, EXIT) == "no"               # the order check fails it
    assert hd.lead_reply_with_directive(reply, EXIT) == EXIT


# ---------------------------------------------------------------------------
# Text runtime: the reply starts with the directive; model words follow only if
# they led with it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reply", DOUBT_FIRST + _random_replies())
def test_text_reply_not_led_by_the_directive_becomes_the_directive_alone(reply):
    if hd.leads_with(reply, EXIT) == "match":
        pytest.skip("generated text happens to lead with the directive")
    assert hd.lead_reply_with_directive(reply, EXIT) == EXIT


@pytest.mark.parametrize("reply", LEADING)
def test_text_reply_led_by_the_directive_keeps_the_model_words_after_it(reply):
    out = hd.lead_reply_with_directive(reply, EXIT)
    assert out.startswith(EXIT)
    rest = out[len(EXIT):].strip()
    original_rest = re.split(r"now\.", reply, maxsplit=1)[1].strip() if "now." in reply else ""
    assert rest == original_rest


def test_text_reply_with_the_drivers_name():
    d = p0_directive([{"template_id": "exit_vehicle"}], "Sam")
    assert hd.lead_reply_with_directive(EXIT + " Go now.", d, "Sam") == f"{d}\n\nGo now."
    assert hd.lead_reply_with_directive(d + " Go now.", d, "Sam") == f"{d}\n\nGo now."
    assert hd.lead_reply_with_directive("Sam, are you sure? " + EXIT, d, "Sam") == d


def _text_ctx(classification=None, say_first=None):
    return SimpleNamespace(
        triage_classification=classification, p0_say_first_given=say_first,
        driver_first_name=None, session_id="s-text", vin="VIN-T",
    )


@pytest.mark.parametrize("reply", DOUBT_FIRST + LEADING)
@pytest.mark.parametrize("classification", [None, "P3", "P2", "P1", "P0"])
def test_text_runtime_reply_always_starts_with_the_directive(reply, classification):
    """Through the runtime's own reply step (``_finish_reply``, what ``invoke`` calls).
    Unconditioned: every classifier result. On a classifier P0 the platform's P0 line
    must not vouch for model words that did not lead with the directive."""
    ctx = _text_ctx(classification, say_first=EXIT if classification == "P0" else None)
    hazard = hd.reported_hazard(FIRE_REPORT)
    out = agentcore_app._finish_reply(reply, ctx, FIRE_REPORT, hazard)
    assert out.startswith(EXIT)
    if hd.leads_with(reply, EXIT) != "match":
        assert out == EXIT
    # The DS-2 over-correction ("…have not flagged this as critical…") doubts the
    # report; it must never appear on such a turn.
    assert "have not flagged" not in out


def test_text_runtime_brake_loss_gets_the_brake_directive():
    hazard = hd.reported_hazard("I lost my brakes")
    out = agentcore_app._lead_hazard_reply("Hmm, I can't verify that.", _text_ctx("P3"), hazard)
    assert out == BRAKE


def test_text_runtime_more_protective_fault_line_wins():
    """Brake loss on a session whose P0 faults call for leaving the vehicle."""
    hazard = hd.reported_hazard("my brakes failed")
    out = agentcore_app._lead_hazard_reply("ok", _text_ctx("P0", say_first=EXIT), hazard)
    assert out == EXIT


def test_text_runtime_no_report_is_unchanged():
    assert agentcore_app._lead_hazard_reply("anything", _text_ctx("P3"), None) == "anything"


def test_text_over_correction_never_follows_a_report_the_ds2_matcher_misses():
    """"I lost my brakes" is a report the DS-2 regex does not know; the
    over-correction ("not flagged as critical") would doubt it."""
    from agents.supervisor import severity_narration as sn
    assert not sn.driver_reports_hazard("I lost my brakes")  # precondition
    out = agentcore_app._finish_reply(
        BRAKE + " Stop driving.", _text_ctx("P3"), "I lost my brakes",
        hd.reported_hazard("I lost my brakes"),
    )
    assert sn.OVER_COPY not in out and out.startswith(BRAKE)



# ---------------------------------------------------------------------------
# Text runtime end to end through invoke()
# ---------------------------------------------------------------------------

# Synthetic stub JWT (`.stub` signature) for auth-path tests — not a real token.
# nosemgrep: detected-jwt-token
_STUB_JWT = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJrZW4tZGV2aWNlLTAwMSIsImV4cCI6OTk5OTk5OTk5OX0.stub"


def _invoke(prompt, *, model_reply="", block_input=False, block_output=False, cap=(True, 1)):
    from bedrock_agentcore.runtime.context import RequestContext
    fake_result = MagicMock()
    fake_result.message = {"content": [{"text": model_reply}]}
    fake_agent = MagicMock(return_value=fake_result)
    denial = {"error": "I'm not able to provide that information here.", "blocked": True}
    enforcer = MagicMock()
    enforcer.check_input.return_value = denial if block_input else None
    enforcer.check_output.return_value = denial if block_output else None
    ctx = RequestContext(
        session_id="vsa-sess-test-hazard-000000000001",
        request_headers={"X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token": _STUB_JWT},
        request=None,
    )
    with patch("agents.supervisor.agentcore_app.KioskGuardrailEnforcer", return_value=enforcer), \
         patch("agents.supervisor.agentcore_app.build_supervisor", return_value=fake_agent), \
         patch("agents.supervisor.agentcore_app.resolve_session_coords"), \
         patch("agents.supervisor.agentcore_app.check_and_increment", return_value=cap), \
         patch("agents.supervisor.agentcore_app.hydrate_session_history", return_value=[]), \
         patch("agents.supervisor.agentcore_app.record_turn"), \
         patch("agents.supervisor.agentcore_app.Config.from_env", return_value=MagicMock(
             api_base="", supervisor_model_id="t", supervisor_max_tokens=100,
             memory_id="", memory_region="us-east-1",
         )):
        out = agentcore_app.invoke(payload={"prompt": prompt, "tenantId": "meridian"}, context=ctx)
    return out, fake_agent


@pytest.mark.parametrize("reply", DOUBT_FIRST + LEADING)
def test_invoke_fire_report_reply_starts_with_the_directive(reply):
    out, _ = _invoke(FIRE_REPORT, model_reply=reply)
    assert out["result"].startswith(EXIT)
    if hd.leads_with(reply, EXIT) != "match":
        assert out["result"] == EXIT


def test_invoke_blocked_input_on_a_report_still_gives_the_directive():
    out, agent = _invoke(FIRE_REPORT, block_input=True)
    assert out["result"] == EXIT and not out.get("blocked")
    agent.assert_not_called()


def test_invoke_blocked_output_on_a_report_still_gives_the_directive():
    out, _ = _invoke("my brakes failed", model_reply="whatever", block_output=True)
    assert out["result"] == BRAKE


def test_invoke_turn_cap_on_a_report_still_gives_the_directive():
    out, agent = _invoke(FIRE_REPORT, cap=(False, 26))
    assert out["result"] == EXIT
    agent.assert_not_called()


def test_invoke_without_a_report_is_unchanged():
    out, _ = _invoke("what does this light mean", model_reply="It is the tire light.")
    assert out["result"] == "It is the tire light."
    blocked, _ = _invoke("what does this light mean", block_input=True)
    assert blocked.get("blocked") is True
    capped, _ = _invoke("what does this light mean", cap=(False, 26))
    assert capped["result"] == agentcore_app.SESSION_LIMIT_RESPONSE


# ---------------------------------------------------------------------------
# Voice: the gate, as a state machine
# ---------------------------------------------------------------------------


def _gate(directive=EXIT, **kw):
    return hd.DirectiveFirstGate(directive, kw.pop("name", None), started_at=0.0, **kw)


def _t(text):
    return {"text": text}


def test_gate_opens_on_a_segment_that_leads_with_the_directive():
    g = _gate()
    assert g.feed("text", _t(EXIT), 1) == [("text", _t(EXIT))]
    assert not g.active and g.opened
    assert g.feed("audio", "a", 2) == [("audio", "a")]


def test_gate_drops_a_doubting_segment_whole_then_opens_on_the_directive():
    g = _gate()
    assert g.feed("text", _t(OBSERVED_DOUBT), 1) == []
    assert g.feed("audio", "a1", 1) == []
    assert g.feed("final", _t(OBSERVED_DOUBT), 1) == []
    out = g.feed("text", _t(EXIT), 2)
    assert out == [("text", _t(EXIT))]
    assert g.dropped_segments == 1


def test_gate_drops_a_block_that_starts_with_anything_else_even_if_the_directive_follows():
    """'Okay.' and the directive in one speculative block share one audio stream."""
    g = _gate()
    assert g.feed("text", _t("Okay."), 1) == []
    assert g.feed("text", _t(EXIT), 1) == []  # same block: still dropped
    assert g.feed("audio", "a", 1) == []
    assert g.active


def test_gate_holds_a_directive_split_across_segments_then_delivers_in_order():
    g = _gate()
    first, second = "This is an emergency —", EXIT.split("— ", 1)[1]
    assert g.feed("text", _t(first), 1) == []
    assert g.feed("audio", "a1", 1) == []
    assert g.feed("final", _t(first), 1) == []
    out = g.feed("text", _t(second), 2)
    assert [k for k, _ in out] == ["text", "audio", "final", "text"]
    assert g.opened


def test_gate_drops_a_held_start_that_does_not_continue():
    g = _gate()
    g.feed("text", _t("This is an emergency."), 1)
    g.feed("audio", "a1", 1)
    assert g.feed("text", _t("But I can't confirm it."), 2) == []
    assert g.active and g.dropped_segments >= 1


def test_gate_drops_audio_it_cannot_attribute():
    g = _gate()
    assert g.feed("audio", "a", 1) == []
    assert g.feed("final", _t("whatever"), 1) == []
    assert g.unattributed_events == 2


def test_gate_times_out_only_at_a_segment_boundary():
    g = _gate(limit_s=5)
    g.feed("text", _t(OBSERVED_DOUBT), 1)
    assert g.feed("audio", "a", 6) == []           # still the dropped segment
    assert g.feed("final", _t(OBSERVED_DOUBT), 6) == []
    assert g.feed("text", _t("Next."), 7) == [("text", _t("Next."))]
    assert g.closed_reason == "timeout"


def test_gate_close_and_interrupt():
    g = _gate()
    g.feed("text", _t("This is an"), 1)
    g.interrupted()
    assert g.feed("audio", "a", 1) == []  # the held start is gone; audio unattributed
    g.close("driver-turn")
    assert g.feed("audio", "b", 2) == [("audio", "b")]


def test_gate_overflowing_hold_is_dropped():
    g = _gate(max_held=3)
    g.feed("text", _t("This is an"), 1)
    for i in range(5):
        assert g.feed("audio", i, 1) == []
    assert g.feed("text", _t("emergency — stop"), 2) == []  # same block? no: new block, judged alone
    assert g.active


def test_gate_accepts_the_drivers_name_first():
    d = p0_directive([{"template_id": "exit_vehicle"}], "Sam")
    g = hd.DirectiveFirstGate(d, "Sam", started_at=0.0)
    assert g.feed("text", _t("Sam, " + EXIT.replace("nine-one-one", "911")), 1)
    assert g.opened



# ---------------------------------------------------------------------------
# Voice: through the real bidi output callable
# ---------------------------------------------------------------------------


class _Queue:
    def __init__(self):
        self.items = []

    async def submit(self, req):
        self.items.append(req)

    def clear_deferred_block(self):
        pass

    def cancel_deferred(self):
        pass

    def mark_response_complete(self):
        pass

    def mark_response_start(self):
        pass


def _bidi_state(**extra):
    st = {
        "platform_note_tag": "a1b2c3d4", "event_count": 0, "type_counts": {},
        "tool_use_names": {}, "escalation_tool_in_flight_at": None,
        "auto_triage_fired": True, "last_classification": "P3",
        "surfaced_classification": "P3", "active_dtcs_for_prompt": [],
        "awaiting_confirmation_category": None, "_narration_user_turn": 0,
        "_narration_turn_text": "", "_narration_corrected_turn": None,
        "outbound_audio_chunks": 0, "vehicle_ctx": {}, "primed": True,
        "injection_queue": _Queue(),
    }
    st.update(extra)
    return st


def _bidi_ctx(**kw):
    base = dict(
        session_id="s-hz", triage_classification="P3", triage_decision_id="d-1",
        vin="VIN-HZ", vehicle_id="VEH-HZ", tenant_id="meridian", driver_id="DRV-1",
        driver_first_name=None, triage_active_dtcs=[], driver_confirmed_priority=None,
        driver_confirmation_pending=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _user(text):
    return {"type": "bidi_transcript_stream", "role": "user", "text": text, "is_final": True}


def _segments(reply: str, one_block: bool = False) -> list[str]:
    if one_block or not reply:
        return [reply] if reply else []
    return [s for s in re.split(r"(?<=[.!?])\s+", reply) if s]


def _model_events(reply: str, one_block: bool = False) -> list[dict]:
    """Nova's order per segment: speculative text, audio, final text."""
    events = []
    for i, seg in enumerate(_segments(reply, one_block)):
        events.append({"type": "bidi_transcript_stream", "role": "assistant", "text": seg, "is_final": False})
        events.extend({"type": "bidi_audio_stream", "audio": f"seg{i}-a{j}", "sample_rate": 24000} for j in range(3))
        events.append({"type": "bidi_transcript_stream", "role": "assistant", "text": seg, "is_final": True})
    return events


def _expected_audio(reply: str, directive: str, one_block: bool = False) -> list[str]:
    """Independent of the gate: the audio of every segment from the first one at which
    the remaining speech leads with the directive; none if there is no such segment."""
    segs = _segments(reply, one_block)
    for k in range(len(segs)):
        if hd.leads_with(" ".join(segs[k:]), directive) == "match":
            return [f"seg{i}-a{j}" for i in range(k, len(segs)) for j in range(3)]
    return []


_BOOKING = {"requestNumber": "VSA-TEST-HZ01", "status": "created", "messageId": "m-1"}


def _drive(events, *, ctx=None, state=None, triage=None, inputs=None, after=()):
    """Drive the real output callable (and optionally the input callable) with
    booking, audit and triage stubbed. Returns (frames, ctx, state, mocks)."""
    ctx = ctx or _bidi_ctx()
    state = state or _bidi_state()
    ws = AsyncMock()
    if inputs:
        ws.receive_json = AsyncMock(side_effect=list(inputs))
    book = MagicMock(return_value=dict(_BOOKING))
    audit = AsyncMock()
    triage_fn = triage or MagicMock(return_value={"classification": ctx.triage_classification})

    async def _go():
        out = bidi_app._make_output_callable(ws, ctx, state, SimpleNamespace(), SimpleNamespace(send=AsyncMock()))
        inp = bidi_app._make_input_callable(ws, state, ctx, SimpleNamespace())
        returned = []
        for _ in (inputs or []):
            returned.append(await inp())
        for e in events:
            await out(e)
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.wait(pending, timeout=5)
        for e in after:  # events that arrive once background work is done
            await out(e)
        return returned

    with patch.object(bidi_app, "call_book_endpoint", book), \
         patch.object(bidi_app, "_write_driver_confirmation_audit", audit), \
         patch.object(bidi_app, "call_triage_endpoint", triage_fn):
        returned = asyncio.run(_go())
    frames = [c.args[0] for c in ws.send_json.call_args_list]
    return frames, ctx, state, SimpleNamespace(book=book, audit=audit, triage=triage_fn, returned=returned)


def _model_frames(frames):
    audio = [f["data"] for f in frames if f.get("type") == "audio.chunk"]
    text = [f["text"] for f in frames if f.get("type") == "transcript" and f.get("role") == "assistant"
            and not f.get("_platform_safety_directive") and not f.get("isFinal")]
    return audio, text


def _assert_directive_first(frames, directive):
    first = _first_assistant_item(frames)
    assert first is not None, "nothing reached the driver after the report"
    assert first.get("type") == "transcript" and first.get("_platform_safety_directive"), first
    assert first["text"] == directive  # the catalog sentence, verbatim


@pytest.mark.parametrize("one_block", [False, True])
@pytest.mark.parametrize("reply", DOUBT_FIRST + LEADING + _random_replies(15))
def test_voice_directive_first_and_nothing_unled_is_delivered(reply, one_block):
    frames, _, _, _ = _drive([_user(FIRE_REPORT)] + _model_events(reply, one_block))
    _assert_directive_first(frames, EXIT)
    audio, text = _model_frames(frames)
    assert audio == _expected_audio(reply, EXIT, one_block)
    if text:
        assert hd.leads_with(" ".join(text), EXIT) == "match"


def test_voice_observed_session_replays_with_the_directive_first():
    """The 2026-10-05 session: the doubt line is never delivered."""
    frames, _, _, _ = _drive([_user(FIRE_REPORT)] + _model_events(OBSERVED_DOUBT))
    _assert_directive_first(frames, EXIT)
    assert OBSERVED_DOUBT not in [f.get("text") for f in frames]
    assert _model_frames(frames) == ([], [])


@pytest.mark.parametrize("report,directive", [
    (FIRE_REPORT, EXIT), ("I lost my brakes", BRAKE), ("there's smoke coming from the dash", EXIT),
])
@pytest.mark.parametrize("classification", ["P0", "P1", "P2", "P3", "error"])
def test_voice_the_words_are_unconditioned_on_the_classifier(report, directive, classification):
    """First turn: the directive is on screen before the triage call returns, for every
    classifier result. That is the WORDS half, and it is unconditioned.

    The ACTIONS half is not (user decision, 2026-10-06): see
    `test_split_*` below. Here only the words are asserted, plus the one thing the
    actions must never do without the classifier or a yes — write the row.
    """
    calls = []

    def _triage(c, *_a, **_kw):
        calls.append(time.monotonic())
        time.sleep(0.05)
        if classification == "error":
            raise RuntimeError("classifier down")
        c.triage_classification = classification
        c.triage_decision_id = "d-2"
        return {"classification": classification, "triageDecisionId": "d-2", "latencyMs": 5}

    ctx = _bidi_ctx(triage_classification=None, triage_decision_id=None)
    state = _bidi_state(auto_triage_fired=False, last_classification=None, surfaced_classification=None)
    frames, ctx, state, m = _drive(
        [_user(report)] + _model_events(OBSERVED_DOUBT), ctx=ctx, state=state, triage=_triage,
        after=[{"type": "bidi_usage", "inputTokens": 1, "outputTokens": 1, "totalTokens": 2}],
    )
    _assert_directive_first(frames, directive)
    # Before the classifier's verdict reached the client.
    i_dir = next(i for i, f in enumerate(frames) if f.get("_platform_safety_directive"))
    i_tri = [i for i, f in enumerate(frames) if f.get("type") == "tool.call" and f.get("name") == "triage"]
    assert all(i_dir < i for i in i_tri)
    # The model is told to lead with it and not to question the report, every time.
    notes = [r.payload for r in state["injection_queue"].items]
    hazard_notes = [n for n in notes if "Do not question" in n]
    assert hazard_notes and directive in hazard_notes[0]
    # The doubting answer never reached the driver, whatever the classifier said.
    assert _model_frames(frames)[0] == []
    # And on no classifier grade below P0 is the roadside-eligible row written.
    if classification != "P0":
        assert not any(c.kwargs.get("verdict") == "reported" for c in m.audit.await_args_list)


# ---------------------------------------------------------------------------
# The split: the detector decides the WORDS, never the ACTIONS
# (user decision, 2026-10-06, recorded in the issue's build-notes).
#
# The actions are the P0 booking, the `verdict="reported"` decision row that
# `lambdas/api-escalate/handler.py` verifies, and the session P0 pin. A verified P0 row
# places the outbound P0 callback and, for a tenant with `autoRequestRoadsideOnP0`, a
# roadside dispatch with no tap — so a detector false positive must not write it. They
# are unlocked by the classifier's own P0 or by the driver's yes, and by nothing else.
# ---------------------------------------------------------------------------

def _assert_no_actions(frames, ctx, state, m):
    """The actions did NOT happen: no ticket, no roadside-eligible row, no P0 pin, no
    driver-confirmed P0 on screen."""
    m.book.assert_not_called()
    assert not any(c.kwargs.get("verdict") == "reported" for c in m.audit.await_args_list)
    assert ctx.driver_confirmed_priority is None
    assert not any(
        f.get("type") == "classification" and f.get("source") == "driver-confirmed"
        for f in frames
    )


def _assert_asked(frames, state, category_name):
    """…and the confirmation question was asked instead, after the directive."""
    asked = [f for f in frames if f.get("type") == "tool.call" and f.get("name") == "driver_confirmation"]
    assert len(asked) == 1, asked
    assert asked[0]["input"]["matchedCategory"] == category_name
    i_dir = next(i for i, f in enumerate(frames) if f.get("_platform_safety_directive"))
    i_ask = frames.index(asked[0])
    assert i_dir < i_ask, "the question must come after the directive, never before it"
    assert state["awaiting_confirmation_category"].name == category_name


@pytest.mark.parametrize("classification", ["P1", "P2", "P3"])
@pytest.mark.parametrize("report,directive,cat", [
    (FIRE_REPORT, EXIT, "fire_or_smoke"),
    ("I lost my brakes", BRAKE, "brake_failure"),
])
def test_split_below_p0_the_directive_is_shown_and_nothing_is_booked(
    report, directive, cat, classification,
):
    def _triage(c, *_a, **_kw):
        c.triage_classification = classification
        c.triage_decision_id = "d-split"
        return {"classification": classification, "triageDecisionId": "d-split", "latencyMs": 5}

    ctx = _bidi_ctx(triage_classification=None, triage_decision_id=None)
    state = _bidi_state(auto_triage_fired=False, last_classification=None, surfaced_classification=None)
    frames, ctx, state, m = _drive(
        [_user(report)] + _model_events(OBSERVED_DOUBT), ctx=ctx, state=state, triage=_triage,
    )
    _assert_directive_first(frames, directive)
    assert _model_frames(frames)[0] == []
    _assert_no_actions(frames, ctx, state, m)
    _assert_asked(frames, state, cat)


@pytest.mark.parametrize("classification", ["P1", "P2", "P3"])
def test_split_the_drivers_yes_then_unlocks_the_actions(classification):
    """The yes is what books. Two turns: the report arms the question, and the next
    turn's "yes" reaches `_handle_confirmation_response` with the report's own category
    and the classifier tier it was asked at (that function owns the booking path)."""
    def _triage(c, *_a, **_kw):
        c.triage_classification = classification
        c.triage_decision_id = "d-yes"
        return {"classification": classification, "triageDecisionId": "d-yes", "latencyMs": 5}

    ctx = _bidi_ctx(triage_classification=None, triage_decision_id=None)
    state = _bidi_state(auto_triage_fired=False, last_classification=None, surfaced_classification=None)
    _, ctx, state, _ = _drive(
        [_user(FIRE_REPORT)] + _model_events(OBSERVED_DOUBT), ctx=ctx, state=state, triage=_triage,
    )
    assert state["awaiting_confirmation_category"].name == "fire_or_smoke"
    assert state["classifier_tier_before_confirmation"] == classification
    assert ctx.driver_confirmation_pending is True

    with patch.object(bidi_app, "_handle_confirmation_response", AsyncMock()) as confirm:
        _drive([_user("yes, there is fire")], ctx=ctx, state=state, triage=_triage)
    # It reads the pending category from state, which the two asserts above pin.
    confirm.assert_awaited_once()


def test_split_on_a_classifier_p0_session_the_report_writes_no_row_of_its_own():
    """The classifier's own P0 path owns the actions, so a later report there does the
    WORDS and nothing else.

    The security review of the split found the earlier shape here: with the gate
    written as "classifier P0 unlocks the actions", a sticky `triage_classification`
    of P0 from an earlier turn let the detector write a `verdict="reported"` row
    carrying its own wording — on a false positive, wording an operator reviewing
    driver-confirmed escalations would read as a report the driver never made. The
    classifier's row is the authoritative one; a second row adds only that risk.
    """
    ctx = _bidi_ctx(triage_classification="P0", triage_decision_id="d-p0")
    state = _bidi_state(last_classification="P0", surfaced_classification="P0")
    frames, ctx, state, m = _drive([_user(FIRE_REPORT)] + _model_events(OBSERVED_DOUBT),
                                   ctx=ctx, state=state)
    _assert_directive_first(frames, EXIT)
    assert not any(c.kwargs.get("verdict") == "reported" for c in m.audit.await_args_list)
    # …and the question is not asked either: the classifier already agrees, and asking
    # would be the doubt this issue exists to remove.
    assert not any(f.get("name") == "driver_confirmation" for f in frames)



def test_split_the_question_is_held_until_the_directive_has_been_said():
    """The question must not be injected at the report: IMMEDIATE would preempt the
    directive, and DEFERRED is cancelled by the model's first chunk (cycle 2, MD). It
    is held and released when the gate opens or ends."""
    def _triage(c, *_a, **_kw):
        c.triage_classification = "P3"
        c.triage_decision_id = "d-held"
        return {"classification": "P3", "triageDecisionId": "d-held", "latencyMs": 5}

    ctx = _bidi_ctx(triage_classification=None, triage_decision_id=None)
    state = _bidi_state(auto_triage_fired=False, last_classification=None, surfaced_classification=None)
    # The model never leads with the directive, so the gate ends on the driver's next
    # turn — which is when the question must go out.
    _, _, state, _ = _drive(
        [_user(FIRE_REPORT)] + _model_events(OBSERVED_DOUBT) + [_user("what should I do")],
        ctx=ctx, state=state, triage=_triage,
    )
    reasons = [r.reason for r in state["injection_queue"].items]
    assert any(r.startswith("confirm-fire_or_smoke") for r in reasons), reasons
    q = next(r for r in state["injection_queue"].items if r.reason.startswith("confirm-"))
    assert q.priority is bidi_app.InjectionPriority.IMMEDIATE
    # …and it is behind the directive note, never in front of it.
    items = state["injection_queue"].items
    i_note = next(i for i, r in enumerate(items) if r.reason.startswith("hazard-directive-"))
    i_q = next(i for i, r in enumerate(items) if r.reason.startswith("confirm-"))
    assert i_note < i_q


def test_split_a_new_report_does_not_release_the_old_categorys_question():
    """A held question belongs to the report that caused it. A second, different
    report supersedes it rather than firing it."""
    def _triage(c, *_a, **_kw):
        c.triage_classification = "P3"
        c.triage_decision_id = "d-two"
        return {"classification": "P3", "triageDecisionId": "d-two", "latencyMs": 5}

    ctx = _bidi_ctx(triage_classification=None, triage_decision_id=None)
    state = _bidi_state(auto_triage_fired=False, last_classification=None, surfaced_classification=None)
    _, _, state, _ = _drive(
        [_user("I lost my brakes"), _user(FIRE_REPORT)], ctx=ctx, state=state, triage=_triage,
    )
    confirms = [r.reason for r in state["injection_queue"].items if r.reason.startswith("confirm-")]
    assert "confirm-brake_failure" not in confirms, confirms


def test_voice_a_pending_confirmation_question_is_withdrawn_by_a_report():
    from agents.supervisor.triage_keywords import EMERGENCY_CATEGORIES as cats
    crash = next(c for c in cats if c.name == "crash")
    state = _bidi_state(awaiting_confirmation_category=crash, awaiting_confirmation_keyword="I hit a pole")
    ctx = _bidi_ctx(driver_confirmation_pending=True)
    with patch.object(bidi_app, "_handle_confirmation_response", AsyncMock()) as confirm:
        frames, ctx, state, _ = _drive([_user("now there's smoke coming from the hood")], ctx=ctx, state=state)
    confirm.assert_not_called()
    _assert_directive_first(frames, EXIT)
    # The crash question is gone; the report's own question takes its place (the
    # session is P3, so the actions are still locked).
    assert state["awaiting_confirmation_category"].name == "fire_or_smoke"


def test_voice_driver_speaking_again_ends_the_gate():
    events = [_user(FIRE_REPORT)] + _model_events(OBSERVED_DOUBT) + [_user("okay I'm out of the car")] \
        + _model_events("Good. Stay away from the vehicle.")
    frames, _, state, _ = _drive(events)
    audio, _ = _model_frames(frames)
    assert audio == [f"seg{i}-a{j}" for i in range(2) for j in range(3)]  # only the second reply
    assert state.get("hazard_gate") is None


def test_voice_a_later_report_on_a_p0_classifier_session_is_not_shown_a_lower_grade():
    """The no-downgrade mirror protects a P0 that was actually established — by the
    classifier, or by the driver's yes. After the split a report alone establishes
    none, so the pin here is the one the yes branch would have set."""
    state = _bidi_state(last_classification="P0", surfaced_classification="P0")
    ctx = _bidi_ctx(triage_classification="P0", triage_decision_id="d-p0",
                    driver_confirmed_priority="P0")
    frames, ctx, state, _ = _drive([_user("I lost my brakes")], ctx=ctx, state=state)
    # The model's own triage returns P3 later: the screen keeps the P0.
    ctx.triage_classification = "P3"
    frames2, _, state, _ = _drive(_model_events(BRAKE), ctx=ctx, state=state)
    assert not any(f.get("type") == "classification" for f in frames2)
    assert state["surfaced_classification"] == "P0"


def test_voice_typed_report_shows_the_directive_before_nova_gets_the_words():
    frames, _, state, m = _drive(
        _model_events(OBSERVED_DOUBT + " " + EXIT),
        inputs=[{"type": "text.input", "text": "my car is on fire"}],
    )
    assert m.returned[0] == {"type": "bidi_text_input", "text": "my car is on fire", "role": "user"}
    assert frames[0].get("_platform_safety_directive") and frames[0]["text"] == EXIT
    audio, _ = _model_frames(frames)
    assert audio == [f"seg1-a{j}" for j in range(3)]  # the doubt segment never went out
    m.book.assert_not_called()  # words only until the classifier's P0 or a yes


def test_voice_echo_of_a_typed_report_is_not_handled_twice():
    frames, _, _, m = _drive(
        [_user("my car is on fire")],
        inputs=[{"type": "text.input", "text": "my car is on fire"}],
    )
    assert sum(1 for f in frames if f.get("_platform_safety_directive")) == 1
    m.book.assert_not_called()


def test_voice_without_a_report_nothing_is_gated():
    frames, _, state, m = _drive([_user("what does this light mean")] + _model_events(OBSERVED_DOUBT))
    assert state.get("hazard_gate") is None
    assert not any(f.get("_platform_safety_directive") for f in frames)
    audio, _ = _model_frames(frames)
    assert audio == [f"seg0-a{j}" for j in range(3)]
    m.book.assert_not_called()


def test_dropped_audio_does_not_close_the_microphone():
    """Audio the driver never hears must not set the echo gate that mutes their mic.
    Checked mid-segment: the segment's final transcript would clear it anyway."""
    events = [_user(FIRE_REPORT)] + _model_events(OBSERVED_DOUBT)[:-1]  # no final yet
    frames, _, state, _ = _drive(events)
    assert not state.get("assistant_speaking")
    assert state.get("echo_gate_watchdog") is None
    assert state["outbound_audio_chunks"] == 0



def test_gate_retargets_to_a_more_protective_directive():
    g = _gate(BRAKE)
    g.feed("text", _t("This is critical —"), 1)  # held start of the brake line
    g.retarget(EXIT)
    assert g.feed("audio", "a", 1) == []          # the held segment is dropped
    assert g.feed("text", _t(BRAKE), 2) == []     # the old line no longer opens it
    assert g.feed("text", _t("ok"), 2) == []
    g.feed("final", _t("ok"), 2)
    assert g.feed("text", _t(EXIT), 3) == [("text", _t(EXIT))]


def test_voice_brake_report_then_a_fire_fault_requires_the_fire_line():
    """Brake loss reported; the classifier then reads a fire code (P0). The screen
    moves to the exit line, and the model must lead with that one."""
    def _triage(c, *_a, **_kw):
        c.triage_classification = "P0"
        c.triage_decision_id = "d-3"
        c.triage_active_dtcs = ["B0001_FIRE"]
        return {"classification": "P0", "triageDecisionId": "d-3", "latencyMs": 5}

    ctx = _bidi_ctx(triage_classification=None, triage_decision_id=None)
    state = _bidi_state(auto_triage_fired=False, last_classification=None, surfaced_classification=None)
    frames, _, state, _ = _drive([_user("I lost my brakes")], ctx=ctx, state=state, triage=_triage)
    lines = [f["text"] for f in frames if f.get("_platform_safety_directive")]
    assert lines[0] == BRAKE and lines[-1] == EXIT
    frames2, _, _, _ = _drive(_model_events(BRAKE + " " + EXIT), ctx=ctx, state=state)
    audio, _ = _model_frames(frames2)
    assert audio == [f"seg1-a{j}" for j in range(3)]  # only the segment that led with EXIT



# ---------------------------------------------------------------------------
# Review cycle 1 (issues/2026-10-06-fire-report-doubted-before-directive/review-cycle1.md)
# ---------------------------------------------------------------------------

NEAR_MISS_REPORTS = [
    "I can't believe my car is on fire",
    "I don't know why there's smoke coming from the hood",
    "oh no the engine is on fire",
    "no no no the car is on fire",
    "oh no smoke is coming out of the hood",
    "there is no doubt smoke is coming from the hood",
]


@pytest.mark.parametrize("text", NEAR_MISS_REPORTS)
def test_c1_a_negation_that_does_not_govern_the_hazard_is_not_a_denial(text):
    assert hd.reported_hazard(text) is not None


@pytest.mark.parametrize("text", [
    "I can't see any smoke coming from the hood",
    "it isn't smoke coming from the hood, it's steam",
    "I didn't notice any flames",
    "no smoke is coming from the hood",
])
def test_c1_a_negation_that_governs_the_hazard_is_a_denial(text):
    assert hd.reported_hazard(text) is None


@pytest.mark.parametrize("text", NEAR_MISS_REPORTS[:3])
def test_c1_near_miss_reports_get_the_directive_first_through_the_output_path(text):
    """The reviewer's reproduction: first turn, classifier P3."""
    def _triage(c, *_a, **_kw):
        c.triage_classification = "P3"
        c.triage_decision_id = "d-4"
        return {"classification": "P3", "triageDecisionId": "d-4", "latencyMs": 5}

    ctx = _bidi_ctx(triage_classification=None, triage_decision_id=None)
    state = _bidi_state(auto_triage_fired=False, last_classification=None, surfaced_classification=None)
    frames, _, state, _ = _drive([_user(text)] + _model_events(OBSERVED_DOUBT), ctx=ctx, state=state, triage=_triage)
    _assert_directive_first(frames, EXIT)
    assert _model_frames(frames) == ([], [])
    # The question is asked now — after the directive — because it is the action gate.
    _assert_asked(frames, state, "fire_or_smoke")


def test_w1_an_upgraded_line_is_also_given_to_the_model():
    """Brake loss reported on a P2 vehicle whose triage reads a fire code: the screen
    moves to the exit line AND the model is told the exit line, so the gate is not
    waiting for words the model was never given."""
    def _triage(c, *_a, **_kw):
        c.triage_classification = "P2"
        c.triage_decision_id = "d-5"
        c.triage_active_dtcs = ["B0001_FIRE"]
        return {"classification": "P2", "triageDecisionId": "d-5", "latencyMs": 5}

    ctx = _bidi_ctx(triage_classification=None, triage_decision_id=None)
    state = _bidi_state(auto_triage_fired=False, last_classification=None, surfaced_classification=None)
    frames, _, state, _ = _drive([_user("I lost my brakes")], ctx=ctx, state=state, triage=_triage)
    lines = [f["text"] for f in frames if f.get("_platform_safety_directive")]
    assert lines[0] == BRAKE and lines[-1] == EXIT
    notes = [r.payload for r in state["injection_queue"].items]
    assert any(EXIT in n and "Do not question" in n for n in notes)
    frames2, _, _, _ = _drive(_model_events(EXIT), ctx=ctx, state=state)
    assert _model_frames(frames2)[0] == [f"seg0-a{j}" for j in range(3)]


def test_w2_typed_first_turn_report_runs_one_follow_up_despite_the_echo():
    calls = []

    def _triage(c, *_a, **_kw):
        calls.append(1)
        c.triage_classification = "P3"
        c.triage_decision_id = "d-6"
        return {"classification": "P3", "triageDecisionId": "d-6", "latencyMs": 5}

    ctx = _bidi_ctx(triage_classification=None, triage_decision_id=None)
    state = _bidi_state(auto_triage_fired=False, last_classification=None, surfaced_classification=None)
    frames, _, _, m = _drive(
        [_user("my car is on fire")], ctx=ctx, state=state, triage=_triage,
        inputs=[{"type": "text.input", "text": "my car is on fire"}],
    )
    assert len(calls) == 1
    # One follow-up, not two: at P3 it asks once and takes no action.
    asked = [f for f in frames if f.get("name") == "driver_confirmation"]
    assert len(asked) == 1
    assert m.audit.await_count == 0
    assert not any(f.get("type") == "classification" and f.get("value") == "P0" for f in frames)
    m.book.assert_not_called()


def test_split_the_only_unlock_in_the_followup_is_the_drivers_yes():
    """Stated as the complement of the two tests above: with no pending question and
    no classifier P0, the follow-up writes nothing at every tier it can see."""
    for tier in ("P1", "P2", "P3", None):
        ctx = _bidi_ctx(triage_classification=tier, triage_decision_id="d-x" if tier else None)
        state = _bidi_state(last_classification=tier, surfaced_classification=tier)
        _, ctx, _, m = _drive([_user(FIRE_REPORT)] + _model_events(OBSERVED_DOUBT),
                              ctx=ctx, state=state)
        assert not any(c.kwargs.get("verdict") for c in m.audit.await_args_list), tier
        m.book.assert_not_called()
        assert ctx.driver_confirmed_priority is None, tier


def test_w3_yes_to_a_pending_crash_question_with_a_hazard_keeps_the_crash_line():
    crash = CATEGORY["crash"]
    state = _bidi_state(awaiting_confirmation_category=crash, awaiting_confirmation_keyword="I hit a pole",
                        classifier_tier_before_confirmation="P3")
    ctx = _bidi_ctx(driver_confirmation_pending=True)
    frames, ctx, state, m = _drive([_user("yes, and my brakes are gone")], ctx=ctx, state=state)
    crash_line = p0_directive([{"template_id": "stay_call_911"}], None)
    _assert_directive_first(frames, crash_line)
    kw = m.audit.await_args.kwargs
    assert kw["category"].name == "crash" and kw["verdict"] == "yes" and kw["final_classification"] == "P0"
    cls = [f for f in frames if f.get("type") == "classification"]
    assert cls[-1]["value"] == "P0" and cls[-1]["category"] == "crash"


def test_w3_a_no_to_a_pending_question_with_a_hazard_is_superseded_by_the_report():
    """A "no" that also reports a hazard: the report supersedes the crash question, the
    brake directive is shown, and the P3 narration ("not an emergency") is not sent.
    After the split the report writes no row — the brake question takes the crash
    question's place and waits for a yes."""
    crash = CATEGORY["crash"]
    state = _bidi_state(awaiting_confirmation_category=crash, awaiting_confirmation_keyword="I hit a pole")
    frames, ctx, state, m = _drive([_user("no, but my brakes are gone")], state=state)
    _assert_directive_first(frames, BRAKE)
    _assert_no_actions(frames, ctx, state, m)
    assert state["awaiting_confirmation_category"].name == "brake_failure"
    notes = [r.payload for r in state["injection_queue"].items]
    assert not any("not an emergency" in n for n in notes)


def test_r1_a_typed_report_keeps_the_stop_instruction_from_being_taken_back():
    _, _, state, _ = _drive([], inputs=[{"type": "text.input", "text": "I lost my brakes"}])
    assert state["_narration_driver_reported_emergency"] is True


def test_r2_words_the_driver_was_not_sent_are_not_severity_checked():
    """A dropped segment claiming 'safe to keep driving' on a P0 would otherwise get a
    severity correction for words the driver never heard."""
    state = _bidi_state(surfaced_classification="P0", last_classification="P3")
    ctx = _bidi_ctx(driver_confirmed_priority="P0")
    frames, _, _, _ = _drive(
        [_user(FIRE_REPORT)] + _model_events("I think it's safe to keep driving for now."),
        ctx=ctx, state=state,
    )
    assert not any(f.get("_platform_severity_correction") for f in frames)


def test_r3_a_compliant_model_before_triage_returns_is_not_gated_again():
    def _triage(c, *_a, **_kw):
        c.triage_classification = "P3"
        c.triage_decision_id = "d-7"
        return {"classification": "P3", "triageDecisionId": "d-7", "latencyMs": 5}

    ctx = _bidi_ctx(triage_classification=None, triage_decision_id=None)
    state = _bidi_state(auto_triage_fired=False, last_classification=None, surfaced_classification=None)
    frames, _, state, _ = _drive([_user(FIRE_REPORT)] + _model_events(EXIT), ctx=ctx, state=state, triage=_triage)
    assert sum(1 for f in frames if f.get("_platform_safety_directive")) == 1
    assert state.get("hazard_gate") is None
    frames2, _, _, _ = _drive(_model_events("Stay well back from it."), ctx=ctx, state=state)
    assert _model_frames(frames2)[0] == [f"seg0-a{j}" for j in range(3)]


def test_r4_a_gate_error_delivers_nothing():
    with patch.object(hd.DirectiveFirstGate, "feed", side_effect=RuntimeError("boom")):
        frames, _, _, _ = _drive([_user(FIRE_REPORT)] + _model_events(OBSERVED_DOUBT))
    _assert_directive_first(frames, EXIT)
    assert _model_frames(frames) == ([], [])


def test_r5_text_judges_the_drivers_words_not_the_client_prompt():
    prompt_with_scaffolding = "Vehicle facts: 2026 Meridian Halcyon. The visitor says:"
    out, _ = _invoke_with_user_text(prompt_with_scaffolding, "my engine is on fire", model_reply="Hmm.")
    assert out["result"] == EXIT


def test_r5_a_replayed_hazard_turn_in_the_prompt_also_gets_the_directive():
    """Cycle 1's R5 and security cycle 1's W2 pin opposite behaviours here, and this is
    the resolution.

    R5 wanted the prompt NOT judged, so a replay of an earlier hazard turn would not
    make every later reply the directive. W2 showed that judging only `userText` lets a
    crafted body give the model a hazard the check never sees — the control bypassed on
    its own surface. Both are real; the span cannot be both guarded and unguarded.

    The safety property wins over the narration property: a hazard in EITHER span gets
    the directive. The cost is R5's — after a hazard turn, later replies in that session
    are the directive alone until the replay rolls past it. It is paid in annoyance, not
    in a missed directive, and nothing irreversible happens on the text path (no
    booking, no audit row, no session pin). For every client shipped today the question
    is moot: none sends `userText`, so `prompt` is already the judged span. The durable
    fix is to assemble the context server-side
    (cvx/issues/2026-07-28-text-runtime-context-plumbing), after which there is one span.
    """
    replay = "Earlier the visitor said: my engine is on fire. Now:"
    out, _ = _invoke_with_user_text(replay, "what's the range?", model_reply="About 300 miles.")
    assert out["result"] == EXIT


def _invoke_with_user_text(prompt, user_text, *, model_reply=""):
    from bedrock_agentcore.runtime.context import RequestContext
    fake_result = MagicMock()
    fake_result.message = {"content": [{"text": model_reply}]}
    fake_agent = MagicMock(return_value=fake_result)
    enforcer = MagicMock()
    enforcer.check_input.return_value = None
    enforcer.check_output.return_value = None
    ctx = RequestContext(
        session_id="vsa-sess-test-hazard-000000000002",
        request_headers={"X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token": _STUB_JWT},
        request=None,
    )
    with patch("agents.supervisor.agentcore_app.KioskGuardrailEnforcer", return_value=enforcer), \
         patch("agents.supervisor.agentcore_app.build_supervisor", return_value=fake_agent), \
         patch("agents.supervisor.agentcore_app.resolve_session_coords"), \
         patch("agents.supervisor.agentcore_app.check_and_increment", return_value=(True, 1)), \
         patch("agents.supervisor.agentcore_app.hydrate_session_history", return_value=[]), \
         patch("agents.supervisor.agentcore_app.record_turn"), \
         patch("agents.supervisor.agentcore_app.Config.from_env", return_value=MagicMock(
             api_base="", supervisor_model_id="t", supervisor_max_tokens=100,
             memory_id="", memory_region="us-east-1",
         )):
        out = agentcore_app.invoke(
            payload={"prompt": prompt, "tenantId": "meridian", "userText": user_text}, context=ctx,
        )
    return out, fake_agent



# ---------------------------------------------------------------------------
# Review cycle 2 (issues/2026-10-06-fire-report-doubted-before-directive/review-cycle2.md)
# ---------------------------------------------------------------------------

NOT_REPORTS_CYCLE2 = [
    "I can't stop by the dealer until Friday, can we do Saturday?",
    "I cannot stop at a charger right now, how far can I go?",
    "I can't stop thinking about that rattle",
    "there's a fire on the highway ahead",
    "the car in front of me has smoke coming from its tailpipe",
    "smoke coming from the car in front of me",
    "I don't have any smoke coming from the hood",
    "there's no sign of smoke coming from the hood",
    "I didn't say the car is on fire",
    "no no flames",
    "what should I do if my car is on fire?",
    "a car is on fire over there",
    "is there a brake fluid issue? no brake fluid leak",
    "there's a wildfire near the road",
    "I'm smoking a cigarette in the car",
    "I don't think there's smoke coming from the hood",
]
REPORTS_CYCLE2 = [
    ("my car's on fire", "fire_or_smoke"), ("my truck is on fire", "fire_or_smoke"),
    ("it's on fire", "fire_or_smoke"), ("I smell smoke", "fire_or_smoke"),
    ("there's smoke in the cabin", "fire_or_smoke"), ("fire!", "fire_or_smoke"),
    ("Engine is on fire!", "fire_or_smoke"), ("Smoke is coming out!", "fire_or_smoke"),
    ("I'm on the highway and smoke is coming from my hood", "fire_or_smoke"),
    ("I pulled onto the shoulder, the engine is smoking", "fire_or_smoke"),
    ("smoke is coming from the hood, what if it catches fire", "fire_or_smoke"),
    ("no no smoke coming from the hood", "fire_or_smoke"),  # repeated "no" is an exclamation
    ("my brakes stopped working", "brake_failure"), ("the brakes don't work", "brake_failure"),
    ("my brakes went out", "brake_failure"), ("brakes are out", "brake_failure"),
    ("I can't stop the car", "brake_failure"), ("the car won't stop", "brake_failure"),
    ("I can't stop!", "brake_failure"), ("I can't brake", "brake_failure"),
]


@pytest.mark.parametrize("text", NOT_REPORTS_CYCLE2)
def test_cycle2_ordinary_words_are_not_reports(text):
    assert hd.reported_hazard(text) is None


@pytest.mark.parametrize("text,expected", REPORTS_CYCLE2)
def test_cycle2_natural_reports(text, expected):
    c = hd.reported_hazard(text)
    assert c is not None and c.name == expected


def test_cycle2_a_scheduling_sentence_on_a_later_turn_books_nothing():
    """The reviewer's reproduction: turn 2, 'I can't stop by the dealer…'."""
    frames, ctx, state, m = _drive(
        [_user("I can't stop by the dealer until Friday, can we do Saturday?")]
        + _model_events("Saturday at 10 works. I can book that."),
    )
    assert not any(f.get("_platform_safety_directive") for f in frames)
    assert _model_frames(frames)[0] == [f"seg{i}-a{j}" for i in range(2) for j in range(3)]
    m.book.assert_not_called()
    m.audit.assert_not_called()
    assert ctx.driver_confirmed_priority is None
    assert state.get("surfaced_classification") == "P3"


def test_cycle2_md_the_upgrade_note_is_immediate():
    def _triage(c, *_a, **_kw):
        c.triage_classification = "P2"
        c.triage_decision_id = "d-8"
        c.triage_active_dtcs = ["B0001_FIRE"]
        return {"classification": "P2", "triageDecisionId": "d-8", "latencyMs": 5}

    ctx = _bidi_ctx(triage_classification=None, triage_decision_id=None)
    state = _bidi_state(auto_triage_fired=False, last_classification=None, surfaced_classification=None)
    _, _, state, _ = _drive([_user("I lost my brakes")], ctx=ctx, state=state, triage=_triage)
    upgrade = [r for r in state["injection_queue"].items if r.reason.startswith("hazard-directive-upgrade")]
    assert upgrade and all(r.priority == bidi_app.InjectionPriority.IMMEDIATE for r in upgrade)
    assert EXIT in upgrade[0].payload


def test_cycle2_mf_an_unclear_answer_to_a_pending_question_is_not_a_yes():
    """An unclear answer must not be recorded as the yes the driver never gave. After
    the split that is stronger: no row is written at all, for either category, and the
    crash question is replaced by the brake one rather than answered."""
    crash = CATEGORY["crash"]
    state = _bidi_state(awaiting_confirmation_category=crash, awaiting_confirmation_keyword="I hit a pole")
    frames, ctx, state, m = _drive([_user("my brakes are gone")], state=state)
    _assert_directive_first(frames, BRAKE)
    assert not any(c.kwargs.get("verdict") == "yes" for c in m.audit.await_args_list)
    _assert_no_actions(frames, ctx, state, m)
    assert state["awaiting_confirmation_category"].name == "brake_failure"


def test_cycle2_mb_a_typed_message_ends_the_gate():
    frames, _, state, _ = _drive(
        _model_events("Good, stay back from it."),
        inputs=[{"type": "text.input", "text": "my car is on fire"},
                {"type": "text.input", "text": "ok I'm out of the car"}],
    )
    assert state.get("hazard_gate") is None
    assert _model_frames(frames)[0] == [f"seg0-a{j}" for j in range(3)]


def test_cycle2_me_a_spoken_report_said_again_is_handled_again():
    """Only Nova's echo of a TYPED report is skipped; the driver repeating it is a turn."""
    frames, _, _, _ = _drive([_user(FIRE_REPORT), _user(FIRE_REPORT)])
    assert sum(1 for f in frames if f.get("_platform_safety_directive")) == 2


def test_cycle2_a_crash_yes_session_also_keeps_p0_on_screen():
    """The no-downgrade mirror rule covers every driver-confirmed P0, not only reports."""
    ctx = _bidi_ctx(driver_confirmed_priority="P0", triage_classification="P3")
    state = _bidi_state(last_classification=None, surfaced_classification="P0")
    frames, _, state, _ = _drive(_model_events("Help is a tap away."), ctx=ctx, state=state)
    assert not any(f.get("type") == "classification" for f in frames)
    assert state["surfaced_classification"] == "P0"


# ---------------------------------------------------------------------------
# Review cycle 3 (issues/2026-10-06-fire-report-doubted-before-directive/review-cycle3.md)
# ---------------------------------------------------------------------------

REPORTS_CYCLE3 = [
    "My engine is on fire and there's smoke coming from the hood.",
    "there's smoke coming from the hood",
    'I see flames',
    'something is burning',
    'what do I do my car is on fire',
    'is there smoke coming from the hood?',
    "I can't believe it, there's smoke coming from the hood",
    'my brakes failed',
    'I lost my brakes',
    'no brakes!',
    "the brakes aren't working",
    'the pedal goes to the floor',
    'my brakes are gone and there are flames',
    "I can't believe my car is on fire",
    "I don't know why there's smoke coming from the hood",
    'oh no the engine is on fire',
    'no no no the car is on fire',
    'oh no smoke is coming out of the hood',
    'there is no doubt smoke is coming from the hood',
    "my car's on fire",
    'my truck is on fire',
    "it's on fire",
    'I smell smoke',
    "there's smoke in the cabin",
    'my brakes stopped working',
    "the brakes don't work",
    'my brakes went out',
    'brakes are out',
    'fire!',
    'smoke',
    "I can't stop the car",
    "the car won't stop",
    "I can't stop!",
    "I can't brake",
    'smoke is coming from the hood, what if it catches fire',
    "I'm on the highway and smoke is coming from my hood",
    'I pulled onto the shoulder, the engine is smoking',
    'the battery is on fire',
    'flames are coming out of the wheel well',
    "there's a fire under the hood",
    'Engine is on fire!',
    'Smoke is coming out!',
    'no no smoke coming from the hood',
    'another car hit me and now my car is on fire',
    "I don't know if I should get out, the engine is on fire",
    'my tires are on fire',
    'the brakes are smoking',
    "my car's still smoking",
    'my car is literally on fire',
    'my brakes are failing',
    'brakes not working',
    "I can't slow down",
    'smoke everywhere',
    'my sedan is on fire',
    'my brakes went out on the freeway',
    'my brakes failed on the highway',
    "the brakes aren't stopping the car",
    "my brakes won't stop the truck",
    'there are flames coming from under the hood',
    'I have an engine fire',
    'the cabin is filling with smoke',
    'my van is smoking badly',
    'help my car is on fire',
    'my brake pedal went to the floor',
    'it just caught fire',
    'the car is on fire, what do I do',
    'I smell something burning',
    'it smells like smoke in here',
    'the dash is on fire',
]

NOT_REPORTS_CYCLE3 = [
    "there's no smoke coming from the hood",
    "I don't see flames",
    'there are no flames',
    'never any smoke coming from it',
    'what does this light mean',
    'my brakes are a little spongy',
    'I crashed into a pole',
    'the steering wheel locked',
    "I can't see any smoke coming from the hood",
    "it isn't smoke coming from the hood, it's steam",
    "I didn't notice any flames",
    'no smoke is coming from the hood',
    "I can't stop by the dealer until Friday, can we do Saturday?",
    'I cannot stop at a charger right now, how far can I go?',
    "I can't stop thinking about that rattle",
    "there's a fire on the highway ahead",
    'the car in front of me has smoke coming from its tailpipe',
    'smoke coming from the car in front of me',
    "I don't have any smoke coming from the hood",
    "there's no sign of smoke coming from the hood",
    "I didn't say the car is on fire",
    'no no flames',
    'what should I do if my car is on fire?',
    'a car is on fire over there',
    'is there a brake fluid issue? no brake fluid leak',
    "there's a wildfire near the road",
    "I'm smoking a cigarette in the car",
    "I don't think there's smoke coming from the hood",
    "my car won't stop beeping",
    "my brakes won't stop squeaking",
    'my engine is burning oil',
    "there's smoke from the wildfires, should I use recirculation?",
    'is my car part of the battery fire recall?',
    'I put the pedal to the floor to pass',
    'my brakes failed inspection',
    'my brakes are out of alignment',
    'I see smoke over the hills',
    'I see flames on the hillside',
    "there's a car fire on the highway",
    'the brake failure warning light came on',
    "I'm losing brake fluid",
    'I lost my brake light',
    'the car ahead is on fire',
    'smoke is pouring over the ridge',
    'my brakes quit squeaking',
    "I can't stop at the light, it's broken",
    'that truck is smoking',
    'what happens when my brakes fail?',
    'the buildings are on fire',
    'I saw a car fire on the news',
    'my car is smoking hot in this sun',
]


# The cycle-3 reviewer's W1 sentences, plus ordinary sentences written afresh for the
# same classes (stop / burning / smoke / fire / pedal / brakes followed by anything).
NOT_REPORTS_CYCLE3 += [
    "my car won't stop beeping, what does that mean?",
    "my engine is burning oil, when should I get it looked at?",
    "my car won't stop charging", "my car won't stop making that noise",
    "the truck won't stop pulling to the right", "my car isn't stopping at the charger",
    "I couldn't stop the car from rolling back on the hill", "I can't stop the car alarm",
    "my car is burning through gas", "my ev is burning through battery",
    "the engine is burning hot", "the hood is burning hot, is that normal?",
    "the car is burning up inside, turn on the AC",
    "there's a lot of smoke outside, how do I turn on recirculation?",
    "there's smoke on the highway ahead, can you reroute me", "I see smoke up ahead on the highway",
    "I smell smoke from the forest fires",
    "blue smoke from the exhaust when I start it", "my car is blowing white smoke from the exhaust",
    "the exhaust is smoking a bit on cold mornings", "can I smoke in the car",
    "what's the battery fire risk on this ev?", "there was a car fire on I-5, is traffic bad?",
    "what should I do in a car fire?", "I see fire trucks ahead",
    "I put the gas pedal to the floor to merge", "I had the pedal to the floor and it wouldn't accelerate",
    "my brakes are out of warranty", "the grinding in the brakes is gone now",
    "my brakes quit squeaking after the service",
    "the smoke coming from the factory down the road is bad today",
    "my car keeps stopping at red lights, is auto start-stop normal?", "I can't stop the wipers",
    "my brakes are squeaking again", "my brakes have been making noise on the highway",
    "there's a burning rubber smell from the car next to me", "I'm burning through tires",
    "there's a fire truck behind me, should I pull over?", "what's the fire risk with lithium batteries",
    "push the pedal to the floor in sport mode", "my car isn't stopping to charge on the route",
    "the recall says the brakes could fail", "will the brakes fail if I keep driving like this?",
    "my friend's brakes failed last week", "my neighbor's car caught fire last year",
    "I read about a car fire",
]
REPORTS_CYCLE3 += [
    "my car is on fire in front of the building",          # "my" outranks an exclusion noun
    "another car hit us and now the engine is on fire",    # the exclusion stays in its clause
    "What if it's serious? My car is on fire.",            # the "if" stays in its sentence
    "what if it's serious? the engine is on fire",
    "my brakes failed yesterday and now my car is on fire",
    "I saw flames coming from the hood", "my car's brakes failed", "the car's brakes are gone",
    "I can't stop the car, help", "I can't stop the car at all", "my car won't stop!",
    "we have smoke in the cabin", "smoke is in the cabin",
]


def _category_of_report(text: str) -> str:
    """Independent of the detector: fire wording means the fire category (its
    directive is the more protective one), otherwise brake loss."""
    return "fire_or_smoke" if re.search(r"fire|flame|smok|burn", text.lower()) else "brake_failure"


@pytest.mark.parametrize("text", NOT_REPORTS_CYCLE3)
def test_cycle3_ordinary_sentences_are_not_reports(text):
    assert hd.reported_hazard(text) is None


@pytest.mark.parametrize("text", REPORTS_CYCLE3)
def test_cycle3_reports(text):
    c = hd.reported_hazard(text)
    assert c is not None and c.name == _category_of_report(text)


# The reviewer's reproductions, through the real output callable on a later turn and
# through the text runtime: no directive, no ticket, no audit row, the answer delivered.
_W1_THROUGH_THE_PATHS = [
    "my car won't stop beeping, what does that mean?",
    "my brakes won't stop squeaking",
    "my engine is burning oil, when should I get it looked at?",
    "there's smoke from the wildfires, should I use recirculation?",
    "is my car part of the battery fire recall?",
    "I put the pedal to the floor to pass",
    "my brakes failed inspection",
    "the smoke coming from the factory down the road is bad today",
    "can I smoke in the car",
    "the grinding in the brakes is gone now",
]
_ORDINARY_REPLY = "That is usually nothing to worry about. I can book a check if you like."


@pytest.mark.parametrize("text", _W1_THROUGH_THE_PATHS)
def test_cycle3_voice_ordinary_sentence_on_a_later_turn_is_answered_normally(text):
    frames, ctx, state, m = _drive([_user(text)] + _model_events(_ORDINARY_REPLY))
    assert not any(f.get("_platform_safety_directive") for f in frames)
    assert state.get("hazard_gate") is None
    assert _model_frames(frames)[0] == [f"seg{i}-a{j}" for i in range(2) for j in range(3)]
    m.book.assert_not_called()
    m.audit.assert_not_called()
    assert ctx.driver_confirmed_priority is None
    assert not any(f.get("type") == "classification" for f in frames)


@pytest.mark.parametrize("text", _W1_THROUGH_THE_PATHS)
def test_cycle3_text_ordinary_sentence_reply_is_unchanged(text):
    out, _ = _invoke_with_user_text(f"[vehicle facts] {text}", text, model_reply=_ORDINARY_REPLY)
    assert out["result"] == _ORDINARY_REPLY


# MC3-1: fire or brake wording the detector does not accept still gets the first-turn
# yes/no question from the keyword matcher, as before the fix. It is the only coverage
# for the detector's misses on the first turn.
@pytest.mark.parametrize("text", [
    "smoke coming from the car in front of me",
    "the car in front of me has smoke coming from its tailpipe",
])
def test_cycle3_a_wording_the_detector_rejects_still_gets_the_first_turn_question(text):
    assert hd.reported_hazard(text) is None
    assert match_emergency_keywords(text) is CATEGORY["fire_or_smoke"]

    def _triage(c, *_a, **_kw):
        c.triage_classification = "P3"
        c.triage_decision_id = "d-q"
        return {"classification": "P3", "triageDecisionId": "d-q", "latencyMs": 5}

    ctx = _bidi_ctx(triage_classification=None, triage_decision_id=None)
    state = _bidi_state(auto_triage_fired=False, last_classification=None, surfaced_classification=None)
    frames, ctx, state, m = _drive([_user(text)], ctx=ctx, state=state, triage=_triage)
    asked = [f for f in frames if f.get("type") == "tool.call" and f.get("name") == "driver_confirmation"]
    assert len(asked) == 1 and asked[0]["input"]["matchedCategory"] == "fire_or_smoke"
    assert state["awaiting_confirmation_category"] is CATEGORY["fire_or_smoke"]
    assert ctx.driver_confirmation_pending is True
    assert not any(f.get("_platform_safety_directive") for f in frames)
    m.book.assert_not_called()
    m.audit.assert_not_called()


# MC3-5: a report on a first turn the classifier grades P0 keeps the ordinary P0 path:
# the escalation slot is reserved and the human chat opens when the tenant opted in,
# and no "reported" row stands in for the classifier's own P0.
def test_cycle3_a_report_on_a_classifier_p0_first_turn_keeps_the_ordinary_p0_path():
    def _triage(c, *_a, **_kw):
        c.triage_classification = "P0"
        c.triage_decision_id = "d-p0"
        return {"classification": "P0", "triageDecisionId": "d-p0", "latencyMs": 5}

    ctx = _bidi_ctx(triage_classification=None, triage_decision_id=None, auto_open_human_chat_on_p0=True)
    state = _bidi_state(auto_triage_fired=False, last_classification=None, surfaced_classification=None)
    with patch.object(bidi_app, "_run_escalate_in_background", AsyncMock()) as escalate:
        frames, ctx, state, m = _drive([_user(FIRE_REPORT)], ctx=ctx, state=state, triage=_triage)
    _assert_directive_first(frames, EXIT)
    escalate.assert_called_once()
    assert escalate.call_args.kwargs["classification"] == "P0"
    assert m.book.call_count == 1
    assert m.book.call_args.kwargs["narrative"].startswith("P0 auto-escalation")
    assert not any(c.kwargs.get("verdict") == "reported" for c in m.audit.await_args_list)


# MC3-3: sentences are judged one at a time, so a question in one sentence does not
# cancel the report in the next.
def test_cycle3_an_if_in_an_earlier_sentence_does_not_cancel_the_report():
    assert hd.reported_hazard("What if it's serious? The engine is on fire.") is CATEGORY["fire_or_smoke"]
    assert hd.reported_hazard("What if it's serious, the engine is on fire") is CATEGORY["fire_or_smoke"]
    assert hd.reported_hazard("If it's on fire what do I do") is None



# ---------------------------------------------------------------------------
# Review cycle 4 (issues/2026-10-06-fire-report-doubted-before-directive/review-cycle4.md)
#
# Cycle 4 asked one question — does the rewritten detector DROP a genuine report? —
# and the answer was yes: 26 of the reviewer's 76 reports, 12 of them reports the
# previous commit detected, and 23 of the 26 reached no mechanism at all on any turn
# in either runtime. The fixes are in hazard_directive.py; what is pinned here is the
# property, in two halves that have to hold together:
#
#   * a COVERAGE FLOOR (REPORTS_CYCLE4): the reviewer's whole corpus, stored, so an
#     anchor added later for a false positive cannot silently shrink report coverage.
#     This is the test that would have failed on the cycle-3 commit. Mutation testing
#     could not: it only probes the behaviour the examples name, and the regressions
#     were outside every example list (cycle 4 Tests note);
#   * the mechanisms, each with its ordinary-sentence counterpart in the same test, so
#     neither direction can be "fixed" by giving up the other.
# ---------------------------------------------------------------------------

# The 12 reports the cycle-3 commit lost (cycle 4 Critical 1). Named separately from
# the corpus because a regression is worse than a gap: these worked one commit earlier.
REGRESSED_CYCLE3 = [
    ("my brakes went out going down the hill", "brake_failure"),
    ("my brakes went out on the hill", "brake_failure"),
    ("my brakes went out as i came off the exit", "brake_failure"),
    ("my brakes gave out halfway down the ramp", "brake_failure"),
    ("my brakes went out im scared", "brake_failure"),
    ("my engine is burning i can see it", "fire_or_smoke"),
    ("the hood is burning i need help", "fire_or_smoke"),
    ("my car is smoking really bad", "fire_or_smoke"),
    ("my car is smoking pretty heavily right now", "fire_or_smoke"),
    ("there is smoke coming up through the floor", "fire_or_smoke"),
    ("my brakes failed on the hill coming into town", "brake_failure"),
    ("my brakes went out i am going to hit something", "brake_failure"),
]

# The cycle-4 reviewer's corpus, verbatim, as the coverage floor. Four classes:
# A varied phrasing and nouns, B words after the hazard word, C two hazards in one
# sentence, D speech-to-text noise.
REPORTS_CYCLE4 = [
    # A: varied phrasing and vehicle/part nouns
    ("my car is on fire", "fire_or_smoke"),
    ("my pickup is on fire", "fire_or_smoke"),
    ("my minivan is on fire", "fire_or_smoke"),
    ("my hatchback is on fire", "fire_or_smoke"),
    ("the engine compartment is on fire", "fire_or_smoke"),
    ("my glove box is on fire", "fire_or_smoke"),
    ("my steering wheel is smoking", "fire_or_smoke"),
    ("my tires are on fire", "fire_or_smoke"),
    ("the brakes are smoking", "fire_or_smoke"),
    ("my battery pack is on fire", "fire_or_smoke"),
    ("smoke is pouring out of the vents", "fire_or_smoke"),
    ("smoke is filling the cabin", "fire_or_smoke"),
    ("i can smell something burning", "fire_or_smoke"),
    ("there is a burning smell in here", "fire_or_smoke"),
    ("flames are coming out of the hood", "fire_or_smoke"),
    ("i see flames under the dash", "fire_or_smoke"),
    ("my car is full of smoke", "fire_or_smoke"),
    ("smoke everywhere", "fire_or_smoke"),
    ("my brakes went out", "brake_failure"),
    ("my brakes just failed", "brake_failure"),
    ("i have no brakes", "brake_failure"),
    ("i lost my brakes", "brake_failure"),
    ("the brake pedal went to the floor", "brake_failure"),
    ("my brakes are completely gone", "brake_failure"),
    ("i cannot slow down", "brake_failure"),
    ("the brakes are not responding", "brake_failure"),
    ("my brakes are failing", "brake_failure"),
    ("pressing the brake does nothing", "brake_failure"),
    # B: words AFTER the hazard word
    ("my car is on fire and i am pulling over", "fire_or_smoke"),
    ("my brakes failed coming down the grade", "brake_failure"),
    ("my brakes are failing badly", "brake_failure"),
    ("my car is on fire near the building", "fire_or_smoke"),
    ("my car caught fire while i was driving", "fire_or_smoke"),
    # D: speech-to-text noise
    ("my cars on fire", "fire_or_smoke"),
    ("my cars on fire i need help now", "fire_or_smoke"),
    ("theres smoke coming from the hood", "fire_or_smoke"),
    ("theres smoke in the cabin", "fire_or_smoke"),
    ("i cant stop", "brake_failure"),
    ("i cant stop the car", "brake_failure"),
    ("my brakes arent working", "brake_failure"),
    ("uh my car is on fire", "fire_or_smoke"),
    ("um i think my engine is on fire", "fire_or_smoke"),
    ("uh um theres smoke coming out of the dash", "fire_or_smoke"),
    ("my my car is on fire", "fire_or_smoke"),
    ("the the brakes went out", "brake_failure"),
    ("my car is is on fire", "fire_or_smoke"),
    ("help my car is on fire i cant stop it", "fire_or_smoke"),
    ("i cant see anything theres smoke everywhere", "fire_or_smoke"),
    ("i cant see my car is on fire", "fire_or_smoke"),
    ("i dont know if i should get out the engine is on fire", "fire_or_smoke"),
    ("oh god my car is on fire", "fire_or_smoke"),
    ("my brakes went out i cant stop", "brake_failure"),
    ("theres flames under the hood", "fire_or_smoke"),
    ("my engines on fire", "fire_or_smoke"),
    ("my brakes dont work", "brake_failure"),
    ("i got no brakes", "brake_failure"),
    ("my hoods on fire", "fire_or_smoke"),
    ("smoke is coming out of my hood i am pulling over now", "fire_or_smoke"),
] + REGRESSED_CYCLE3

# C: two hazards in one sentence. Both categories must be found and fire must win,
# so a brake clause can never pull the directive down from the fire line.
TWO_HAZARDS_CYCLE4 = [
    "my car is on fire and my brakes are gone",
    "my brakes went out and now the engine is on fire",
    "there is smoke in the cabin and i have no brakes",
    "my brakes failed and smoke is coming from the hood",
    "i lost my brakes and my car is smoking",
    "flames are coming from the engine and i cannot stop the car",
]


@pytest.mark.parametrize("text,expected", REPORTS_CYCLE4)
def test_cycle4_coverage_floor(text, expected):
    """The coverage floor. A pattern anchor that drops any of these is a regression,
    whatever it was added to fix."""
    c = hd.reported_hazard(text)
    assert c is not None and c.name == expected


def test_cycle4_the_coverage_floor_is_not_empty():
    """A floor that can be emptied is not a floor: the corpus itself is the artefact,
    so its size and the regression set's membership are asserted, not assumed."""
    assert len(REPORTS_CYCLE4) >= 70, len(REPORTS_CYCLE4)
    assert len(REGRESSED_CYCLE3) == 12
    assert set(REGRESSED_CYCLE3) <= set(REPORTS_CYCLE4)
    assert len(TWO_HAZARDS_CYCLE4) >= 6
    # every class the brief named is represented
    assert any("uh" in t.split() for t, _ in REPORTS_CYCLE4)          # filler
    assert any("'" not in t and "cant" in t for t, _ in REPORTS_CYCLE4)  # no apostrophe
    assert {c for _, c in REPORTS_CYCLE4} == {"fire_or_smoke", "brake_failure"}


@pytest.mark.parametrize("text,expected", REGRESSED_CYCLE3)
def test_cycle4_c1_the_cycle3_regressions_are_reports_again(text, expected):
    c = hd.reported_hazard(text)
    assert c is not None and c.name == expected


@pytest.mark.parametrize("text", TWO_HAZARDS_CYCLE4)
def test_cycle4_two_hazards_in_one_sentence_find_both_and_fire_wins(text):
    names = set()
    for sentence in hd._SENTENCE_SPLIT.split(hd._normalise(text)):
        names.update(hd._sentence_reports(sentence))
    assert names == {"fire_or_smoke", "brake_failure"}, names
    assert hd.reported_hazard(text) is CATEGORY["fire_or_smoke"]


# C1 mechanism 1 and 3: what follows the hazard word is narration, and narration is
# open-ended, so the continuation is an idiom DENYLIST and not a next-word allowlist.
# Both directions in one test: the report must survive any continuation the denylist
# does not name, and each named idiom must still be rejected.
@pytest.mark.parametrize("report,ordinary", [
    ("my brakes went out {}", "my brakes are out of alignment"),
    ("my brakes failed {}", "my brakes failed inspection"),
    ("my brakes are failing {}", "my brakes are failing its emissions test"),
    ("my engine is burning {}", "my engine is burning oil"),
    ("my car is smoking {}", "my car is smoking hot in this sun"),
    ("my brakes quit {}", "my brakes quit squeaking after the service"),
])
@pytest.mark.parametrize("continuation", [
    "", "going down the hill", "as i came off the exit", "halfway down the ramp",
    "im scared", "i need help", "what do i do", "really bad", "right now",
])
def test_cycle4_c1_narration_after_the_hazard_word_is_still_a_report(report, ordinary, continuation):
    text = report.format(continuation).strip()
    assert hd.reported_hazard(text) is not None, text
    assert hd.reported_hazard(ordinary) is None, ordinary


# C1 mechanism 2: a brake pattern's match starts at "brakes" and so never contains
# "my", which is why the clause exclusion reached every brake report. Ownership is
# read from the clause up to the match — but "my friend's brakes" is still not ours.
@pytest.mark.parametrize("text,expected", [
    ("my brakes went out on the hill", "brake_failure"),
    ("my brakes failed on the hillside road", "brake_failure"),
    ("our brakes went out outside the school", "brake_failure"),
    ("my car is on fire near the building", "fire_or_smoke"),
    ("my brakes went out ahead of the bridge", "brake_failure"),
])
def test_cycle4_c1_an_exclusion_noun_does_not_reach_the_drivers_own_hazard(text, expected):
    c = hd.reported_hazard(text)
    assert c is not None and c.name == expected


@pytest.mark.parametrize("text", [
    "my friends brakes failed last week",
    "my friend's brakes failed last week",
    "my neighbors car caught fire last year",
    "my brothers car is on fire over there",
    "i read about a car fire",
])
def test_cycle4_someone_elses_vehicle_is_still_not_a_report(text):
    assert hd.reported_hazard(text) is None


# C2: the transcript may arrive with no apostrophes. The punctuated and unpunctuated
# forms of the same utterance must give the SAME answer — Nova's punctuation of a
# panicking driver must not decide whether the directive is shown. Written as an
# equivalence over pairs, not as a list of unpunctuated examples, because the defect
# was an asymmetry between two spellings of the same rule.
@pytest.mark.parametrize("punctuated,bare", [
    ("my car's on fire", "my cars on fire"),
    ("my engine's on fire", "my engines on fire"),
    ("my hood's on fire", "my hoods on fire"),
    ("it's on fire", "its on fire"),
    ("there's smoke coming from the hood", "theres smoke coming from the hood"),
    ("my brakes don't work", "my brakes dont work"),
    ("my brakes aren't working", "my brakes arent working"),
    ("the car won't stop", "the car wont stop"),
    ("I can't stop the car", "i cant stop the car"),
    ("something's burning", "somethings burning"),
])
def test_cycle4_c2_an_apostrophe_cannot_decide_whether_it_is_a_report(punctuated, bare):
    got = hd.reported_hazard(bare)
    want = hd.reported_hazard(punctuated)
    assert want is not None, punctuated
    assert got is want, f"{bare!r} -> {got and got.name}, {punctuated!r} -> {want.name}"


# W1: a doubled word is a transcript artefact, except "no" — a repeated "no" is an
# exclamation and is deliberately a report, which is what cycle 2's MA pinned.
@pytest.mark.parametrize("text,expected", [
    ("my my car is on fire", "fire_or_smoke"),
    ("my car is is on fire", "fire_or_smoke"),
    ("my my my car is on fire", "fire_or_smoke"),
    ("the the brakes went out", "brake_failure"),
    ("my brakes brakes went out", "brake_failure"),
    ("no no smoke coming from the hood", "fire_or_smoke"),
    ("no no no the car is on fire", "fire_or_smoke"),
])
def test_cycle4_w1_a_doubled_word_is_a_transcript_artefact(text, expected):
    c = hd.reported_hazard(text)
    assert c is not None and c.name == expected


def test_cycle4_w1_collapsing_doubles_does_not_turn_a_denial_into_a_report():
    """The counterpart: a single "no" before the hazard is still a denial."""
    assert hd.reported_hazard("no smoke coming from the hood") is None
    assert hd.reported_hazard("there's no smoke coming from the hood") is None


# W2: a hedge cancels a report only when it GOVERNS it. Unpunctuated is the shape the
# voice runtime actually receives, and a frightened driver hedges.
@pytest.mark.parametrize("text,expected", [
    ("i dont know if i should get out the engine is on fire", "fire_or_smoke"),
    ("i dont know if this matters but my car is on fire", "fire_or_smoke"),
    ("not sure if im overreacting my brakes went out", "brake_failure"),
])
def test_cycle4_w2_a_hedge_with_its_own_clause_does_not_cancel_the_report(text, expected):
    c = hd.reported_hazard(text)
    assert c is not None and c.name == expected


@pytest.mark.parametrize("text", [
    "if its on fire what do i do",
    "what should i do if my car is on fire",
    "what happens when my brakes fail",
    "what should i do in a car fire",
    "in case my car catches fire what do i do",
])
def test_cycle4_w2_a_hedge_that_governs_the_hazard_still_cancels_it(text):
    assert hd.reported_hazard(text) is None


# A match that names the driver's own vehicle is its own clause, not the object of an
# upstream perception verb. Both directions, because the denial path is what cycle 1
# and 2 built and it must survive.
def test_cycle4_a_perception_negation_denies_an_object_not_a_following_clause():
    assert hd.reported_hazard("i cant see my car is on fire") is CATEGORY["fire_or_smoke"]
    assert hd.reported_hazard("i cant see any smoke coming from the hood") is None
    assert hd.reported_hazard("i dont see flames") is None
    assert hd.reported_hazard("i didnt say the car is on fire") is None


def test_cycle4_a_question_about_the_future_costs_only_the_words():
    """The possibility-modal guard was dropped with the split (2026-10-06): a question
    about whether a hazard COULD happen now gets the directive, which is one unneeded
    sentence, and cannot book. "if" still cancels, because it directly governs."""
    assert hd.reported_hazard("will the brakes fail if i keep driving like this") is None
    assert hd.reported_hazard("my brakes failed") is CATEGORY["brake_failure"]
    frames, ctx, state, m = _drive(
        [_user("could my brakes fail")] + _model_events(_ORDINARY_REPLY),
    )
    _assert_no_actions(frames, ctx, state, m)


# W3: a brake report that also names the consequence must reach the BRAKE directive.
# Before the fix the detector missed it and the keyword matcher graded it `crash`, so
# the driver got the crash yes/no question and never the directive they needed.
def test_cycle4_w3_a_brake_report_naming_the_consequence_gets_the_brake_directive():
    text = "my brakes went out i am going to hit something"
    assert hd.reported_hazard(text) is CATEGORY["brake_failure"]
    frames, ctx, state, m = _drive([_user(text)] + _model_events("Let me check that."))
    _assert_directive_first(frames, BRAKE)
    _assert_no_actions(frames, ctx, state, m)


# The newly-detected reports through the real paths, not only through the detector:
# one per fixed mechanism. Directive first, P0 booking, the row /escalate honours.
@pytest.mark.parametrize("text,directive", [
    ("my brakes went out going down the hill", BRAKE),   # C1 narration
    ("my brakes went out on the hill", BRAKE),           # C1 exclusion reach
    ("my engine is burning i can see it", EXIT),         # C1 end anchor
    ("my cars on fire", EXIT),                           # C2 apostrophe
    ("my brakes dont work", BRAKE),                      # C2 apostrophe
    ("my car is is on fire", EXIT),                      # W1 doubled word
    ("i dont know if i should get out the engine is on fire", EXIT),  # W2 hedge
    ("smoke is filling the cabin", EXIT),                # W1 missing form
    ("pressing the brake does nothing", BRAKE),          # W1 missing form
    ("my steering wheel is smoking", EXIT),              # W1 missing part
])
def test_cycle4_voice_a_newly_detected_report_leads_with_the_directive(text, directive):
    frames, ctx, state, m = _drive([_user(text)] + _model_events(OBSERVED_DOUBT))
    _assert_directive_first(frames, directive)
    assert _model_frames(frames)[0] == []  # the doubting answer never reached the driver
    # Words only: the session is P3, so the actions stay locked (the split).
    _assert_no_actions(frames, ctx, state, m)


@pytest.mark.parametrize("text,directive", [
    ("my brakes went out going down the hill", BRAKE),
    ("my cars on fire", EXIT),
    ("i dont know if i should get out the engine is on fire", EXIT),
])
def test_cycle4_text_a_newly_detected_report_replaces_a_doubting_reply(text, directive):
    out, _ = _invoke_with_user_text(f"[vehicle facts] {text}", text, model_reply=OBSERVED_DOUBT)
    assert out["result"] == directive



# ---------------------------------------------------------------------------
# Security review cycle 1
# (issues/2026-10-06-fire-report-doubted-before-directive/security-review-cycle1.md)
#
# 0 Critical, 2 Warnings. W1: the cycle-4 rewrite accepted twelve utterances that are
# not first-person present-tense reports but still booked a P0, wrote the audit row
# `/escalate` honours and pinned P0 for the session — a driver reading a dashboard
# warning aloud, quoting the assistant back, asking whether a hazard CAN happen, and an
# unpunctuated hedge ending in "what do i do". W2: the text runtime judged only the
# narrowed span, so a crafted body could give the model a hazard the check never saw.
#
# Each W1 row is a class, not a string, so the tests below assert the mechanism (speech
# or display attribution, possibility modal, attribution after the hazard, the trailing
# interrogative frame) together with the genuine report it must not take with it.
# ---------------------------------------------------------------------------

NOT_REPORTS_SEC1 = [
    # quoting the assistant or another person
    "did you say my car is on fire",
    "are you telling me my car is on fire",
    "no one has told me my car is on fire",
    "i havent said my car is on fire",
    "did you just tell me my brakes failed",
    # reading an indication aloud
    "the dashboard says my car is on fire",
    "my dashboard says the engine is on fire",
    "the warning says my engine is on fire",
    "the sensor says the engine is on fire",
    "the alert said my car is on fire",
    "the warning says my brakes failed",
    "the screen shows my brakes are gone",
    "my car is on fire according to the dashboard",
    "my brakes failed according to the app",
    # asking whether it can happen
    "can my brakes fail",
    "can my car catch fire",
    "could my brakes fail",
    # an unpunctuated hedge that ends by asking what to do
    "if im driving my car and the engine is on fire what do i do",
    "if my brakes went out going down a hill what should i do",
    "suppose my car is on fire what do i do",
]


@pytest.mark.parametrize("text", NOT_REPORTS_SEC1)
def test_sec1_w1_relayed_quoted_and_hypothetical_wording_takes_no_action(text):
    """These twenty utterances are the security reviewer's cycle-1 list: a driver
    reading a warning aloud, quoting the assistant, asking whether a hazard CAN
    happen, hedging. Cycle 1 asked for them to stop being reports; four guards did
    that, and cycle 2 showed the guards swallowing genuine reports.

    The user's decision (2026-10-06) is the other half of the trade: they ARE reports
    for the purpose of the words — one unneeded safety sentence is a cheap error — and
    they take no action. No ticket, no roadside-eligible row, no session P0 pin. The
    confirmation question is asked, and a yes is what unlocks anything."""
    def _triage(c, *_a, **_kw):
        c.triage_classification = "P3"
        c.triage_decision_id = "d-sec1"
        return {"classification": "P3", "triageDecisionId": "d-sec1", "latencyMs": 5}

    ctx = _bidi_ctx(triage_classification=None, triage_decision_id=None)
    state = _bidi_state(auto_triage_fired=False, last_classification=None, surfaced_classification=None)
    frames, ctx, state, m = _drive(
        [_user(text)] + _model_events(_ORDINARY_REPLY), ctx=ctx, state=state, triage=_triage,
    )
    _assert_no_actions(frames, ctx, state, m)


@pytest.mark.parametrize("text", NOT_REPORTS_SEC1)
def test_sec1_w1_the_cost_of_such_a_turn_is_at_most_one_sentence(text):
    """What it DOES cost, stated so the trade is visible: at most the directive is
    shown and the model's answer is held back until it leads with it. That is the price
    the user accepted for recall, and it is bounded to words — never a booking."""
    frames, _, _, m = _drive([_user(text)] + _model_events(_ORDINARY_REPLY))
    shown = [f for f in frames if f.get("_platform_safety_directive")]
    assert len(shown) <= 1
    if shown:
        assert shown[0]["text"] in (EXIT, BRAKE)
    m.book.assert_not_called()

@pytest.mark.parametrize("text", NOT_REPORTS_SEC1)
def test_split_a_false_positive_writes_no_row_even_on_a_sticky_p0_session(text):
    """The security review's Warning, as a property over its own corpus: on a session
    the classifier graded P0 earlier, none of the relayed, quoted or possibility
    wording may put its words into a driver-confirmation row."""
    ctx = _bidi_ctx(triage_classification="P0", triage_decision_id="d-sticky")
    state = _bidi_state(last_classification="P0", surfaced_classification="P0")
    _, ctx, _, m = _drive([_user(text)] + _model_events(_ORDINARY_REPLY), ctx=ctx, state=state)
    assert not any(c.kwargs.get("verdict") == "reported" for c in m.audit.await_args_list)


# The counterpart for each W1 mechanism: the genuine report it must not take with it.
@pytest.mark.parametrize("text,expected", [
    ("my car is on fire", "fire_or_smoke"),                              # no attribution
    ("what do i do my car is on fire", "fire_or_smoke"),                 # question first, no supposition
    ("i dont know if i should get out the engine is on fire", "fire_or_smoke"),  # hedge, no trailing question
    ("my brakes failed", "brake_failure"),
    ("i cant stop", "brake_failure"),
    ("i cant slow down", "brake_failure"),
    ("i cant see my car is on fire", "fire_or_smoke"),                   # perception, not quotation
    ("i can smell something burning", "fire_or_smoke"),                  # "can" is not the modal here
    ("smoke is coming from the hood, what if it catches fire", "fire_or_smoke"),
])
def test_sec1_w1_the_counterpart_report_still_reports(text, expected):
    c = hd.reported_hazard(text)
    assert c is not None and c.name == expected


# W2: the text runtime must judge BOTH spans. A crafted body that sends benign
# `userText` while the model receives the hazard in `prompt` still gets the directive.
def test_sec1_w2_a_hazard_in_the_prompt_is_guarded_even_when_user_text_is_benign():
    out, _ = _invoke_with_user_text(
        "[vehicle facts] my car is on fire", "hello there", model_reply=OBSERVED_DOUBT,
    )
    assert out["result"] == EXIT


def test_sec1_w2_a_hazard_in_user_text_is_guarded_when_the_prompt_is_benign():
    out, _ = _invoke_with_user_text(
        "[vehicle facts] tire pressure is 34 psi", "my car is on fire",
        model_reply=OBSERVED_DOUBT,
    )
    assert out["result"] == EXIT


def test_sec1_w2_an_ordinary_turn_on_both_spans_is_untouched():
    out, _ = _invoke_with_user_text(
        "[vehicle facts] tire pressure is 34 psi", "when is my next service",
        model_reply=_ORDINARY_REPLY,
    )
    assert out["result"] == _ORDINARY_REPLY


# Suggestion 1: the detector's fail direction is None, and that is only safe if it
# never raises. Pinned over every corpus plus adversarial shapes, rather than assumed
# from the bare `except Exception`.
@pytest.mark.parametrize("text", [
    None, 42, b"bytes", [], {}, "", "   ", "\x00\x00", "\n\n\n",
    "my " * 5000 + "car is on fire",
    "a" * 50000,
    "," * 5000,
    "if " * 2000 + "my car is on fire",
    "no " * 2000 + "smoke coming from the hood",
    "\u00e9\u00e8\u00ea smoke coming from the hood \U0001f525",
    "MY CAR IS ON FIRE",
])
def test_sec1_s1_the_detector_never_raises(text):
    hd.reported_hazard(text)  # must not raise; the value is not the point here


def test_sec1_s1_every_corpus_string_is_safe_to_judge():
    for text, _ in REPORTS_CYCLE4 + REPORTS_CYCLE2:
        hd.reported_hazard(text)
    for text in NOT_REPORTS_CYCLE2 + NOT_REPORTS_CYCLE3 + NOT_REPORTS_SEC1 + REPORTS_CYCLE3:
        hd.reported_hazard(text)


def test_sec1_s1_an_internal_failure_still_fails_to_none():
    """The two tests above assert the detector does not raise on anything we can think
    of to feed it, which cannot distinguish "never raises" from "would propagate". The
    safety property is the fail DIRECTION, so inject the failure: a broken internal
    must leave `reported_hazard` returning None rather than taking the turn down.
    (Security mutation MS7 — re-raising instead of returning None — survived both of
    the above, because neither makes anything raise.)"""
    def _boom(*_a, **_kw):
        raise RuntimeError("injected")

    with patch.object(hd, "_sentence_reports", _boom):
        assert hd.reported_hazard("my car is on fire") is None
    with patch.object(hd, "_normalise", _boom):
        assert hd.reported_hazard("my car is on fire") is None
    # and the real thing still works once the failure is removed
    assert hd.reported_hazard("my car is on fire") is CATEGORY["fire_or_smoke"]


def test_sec1_s1_a_broken_detector_does_not_break_the_turn():
    """The same property one level up: with the detector failing, the voice turn is an
    ordinary turn — the model is heard, nothing is booked — rather than an exception
    inside the output callable."""
    def _boom(*_a, **_kw):
        raise RuntimeError("injected")

    with patch.object(hd, "_sentence_reports", _boom):
        frames, ctx, _, m = _drive([_user(FIRE_REPORT)] + _model_events(_ORDINARY_REPLY))
    assert not any(f.get("_platform_safety_directive") for f in frames)
    assert _model_frames(frames)[0] == [f"seg{i}-a{j}" for i in range(2) for j in range(3)]
    m.book.assert_not_called()
    assert ctx.driver_confirmed_priority is None


# Suggestion 2: the platform's own words must never read as a report, or a released
# transcript or an echoed note could re-trigger the whole path. Pinned over the
# catalog's first sentences and every category word, so a later copy change fails here
# instead of in production.
def test_sec1_s2_no_catalog_directive_or_category_word_is_a_report():
    from agents.supervisor.dtc_response_catalog import RESPONSE_TEMPLATES

    for template_id, template in RESPONSE_TEMPLATES.items():
        first = template["first_sentence"]
        assert hd.reported_hazard(first) is None, template_id
        assert hd.reported_hazard(first[:1].upper() + first[1:]) is None, template_id
    for name, words in hd.CATEGORY_WORDS.items():
        assert hd.reported_hazard(words) is None, name
        assert hd.reported_hazard(f"the driver confirmed {words}") is None, name
        assert hd.reported_hazard(f"say this word for word first: {words}") is None, name



# ---------------------------------------------------------------------------
# Security review cycle 2
# (issues/2026-10-06-fire-report-doubted-before-directive/security-review-cycle2.md)
#
# Cycle 1's quotation guard was keyed on the VERB, so it took 17 genuine reports with
# it: the same verbs carry first-person reporting frames. The guard is now keyed on
# WHOSE speech it is — a first-person speech act is a report, and only a device, a
# third party, the assistant being quoted, or a negated first-person speech act is
# quotation. Both lists below are the test, together: the reject list is cycle 1's
# W1, the report list is cycle 2's W1, and no single-sided fix can satisfy both.
# ---------------------------------------------------------------------------

REPORTS_SEC2 = [
    # "tell" as perception, not as quotation
    ("i can tell my car is on fire", "fire_or_smoke"),
    ("i can tell my brakes are gone", "brake_failure"),
    # first-person reporting frames
    ("i can tell you my car is on fire", "fire_or_smoke"),
    ("let me tell you my car is on fire", "fire_or_smoke"),
    ("im telling you my car is on fire", "fire_or_smoke"),
    ("i wanted to tell you my car is on fire", "fire_or_smoke"),
    ("i have to tell you my car is on fire", "fire_or_smoke"),
    ("i got to tell you my car is on fire", "fire_or_smoke"),
    ("i want to tell you my car is on fire", "fire_or_smoke"),
    ("telling you my car is on fire", "fire_or_smoke"),
    ("i gotta say my car is on fire", "fire_or_smoke"),
    ("i would say my car is on fire", "fire_or_smoke"),
    # saying it again is still saying it
    ("i already said my car is on fire", "fire_or_smoke"),
    ("as i said my car is on fire", "fire_or_smoke"),
    ("like i said my car is on fire", "fire_or_smoke"),
    ("i already told you my car is on fire", "fire_or_smoke"),
    ("reporting my car is on fire", "fire_or_smoke"),
    ("im reporting my car is on fire", "fire_or_smoke"),
    # a driver who says what they see AND mentions the indication
    ("my car is on fire and the dashboard is lit up", "fire_or_smoke"),
    ("the dashboard is screaming and my car is on fire", "fire_or_smoke"),
    # the question comes after the report, not before it
    ("my car is on fire what do i do", "fire_or_smoke"),
    ("my brakes went out what should i do", "brake_failure"),
]


@pytest.mark.parametrize("text,expected", REPORTS_SEC2)
def test_sec2_w1_a_first_person_speech_act_is_still_a_report(text, expected):
    c = hd.reported_hazard(text)
    assert c is not None and c.name == expected


@pytest.mark.parametrize("text,expected", REPORTS_SEC2)
def test_sec2_w1_such_a_report_leads_with_the_directive_in_the_voice_runtime(text, expected):
    """Not only the detector: the directive must actually reach the driver first."""
    directive = EXIT if expected == "fire_or_smoke" else BRAKE
    frames, ctx, state, m = _drive([_user(text)] + _model_events(OBSERVED_DOUBT))
    _assert_directive_first(frames, directive)
    assert _model_frames(frames)[0] == []
    # The words, not the actions: this session is P3, so nothing is booked.
    _assert_no_actions(frames, ctx, state, m)


def test_sec2_w1_cycle_1s_class_differs_in_actions_not_in_words():
    """The cycle-1 and cycle-2 lists are no longer separated by the detector — the
    split separates them by consequence instead. Cycle 2's genuine reports and cycle
    1's relayed wording may both get the directive; neither books without the
    classifier's P0 or a yes, and that is the property that matters."""
    for text in NOT_REPORTS_SEC1:
        frames, ctx, state, m = _drive([_user(text)] + _model_events(_ORDINARY_REPLY))
        _assert_no_actions(frames, ctx, state, m), text



# The split's one deliberate exception, and its boundary. Refusing do-it-yourself
# repair guidance costs only words, so it follows the REPORT, not the confirmation —
# waiting for a yes to refuse DIY advice to a driver whose car is on fire would be the
# wrong way round. It is carried on its own flag (`ctx.driver_reported_hazard`), not by
# re-coupling the P0 pin, so nothing irreversible rides along with it.
def test_split_a_report_refuses_diy_guidance_without_waiting_for_the_yes():
    from agents.supervisor.tools import diy_repair_advisor as diy

    def _triage(c, *_a, **_kw):
        c.triage_classification = "P3"
        c.triage_decision_id = "d-diy"
        return {"classification": "P3", "triageDecisionId": "d-diy", "latencyMs": 5}

    ctx = _bidi_ctx(triage_classification=None, triage_decision_id=None)
    state = _bidi_state(auto_triage_fired=False, last_classification=None, surfaced_classification=None)
    # Before the report, a P3 session may get DIY guidance.
    assert diy._diy_gate_check(_bidi_ctx(triage_classification="P3"), "how do I check the oil", "") is None

    frames, ctx, state, m = _drive(
        [_user(FIRE_REPORT)] + _model_events(OBSERVED_DOUBT), ctx=ctx, state=state, triage=_triage,
    )
    assert ctx.driver_reported_hazard is True
    assert diy._diy_gate_check(ctx, "how do I put it out myself", "") is not None
    # …and the actions are still locked: the refusal did not bring the pin with it.
    _assert_no_actions(frames, ctx, state, m)
    assert ctx.driver_confirmed_priority is None


def test_split_the_words_side_flag_does_not_unlock_any_action():
    """The boundary. `driver_reported_hazard` must not be read by anything that books,
    escalates or writes a decision row — only by refusals and narration."""
    # `call_book_endpoint`'s effective priority is the most urgent of the explicit
    # override, the P0 pin and the classifier's grade. This flag is none of those, and
    # the booking path must not learn to read it.
    book_src = pathlib.Path(bidi_app.__file__).with_name("tools").joinpath("book.py").read_text()
    assert "driver_reported_hazard" not in book_src
    esc = pathlib.Path(bidi_app.__file__).resolve().parents[2] / "lambdas/api-escalate/handler.py"
    if esc.exists():
        assert "driver_reported_hazard" not in esc.read_text()
    # …and a reported-but-unconfirmed P3 session books at P3, not P0.
    ctx = _bidi_ctx(triage_classification="P3", driver_reported_hazard=True)
    assert getattr(ctx, "driver_confirmed_priority", None) is None



# The hold-and-release mechanism, unit-tested directly.
#
# The `_drive` harness awaits background tasks only after every event, so by the time
# the follow-up asks, the gate has already ended and the question is released on the
# no-gate path. That makes the harness blind to the gate-release path itself: split
# mutations MP6 and MP7 (a new report releasing the old category's question; the
# release never happening at all) both SURVIVED the session-level tests above. These
# two call the real functions with the state that production has at that moment.

def test_split_mp7_ending_the_gate_releases_the_held_question():
    """MP7: without the release in `_end_hazard_gate`, a question held behind the
    directive would never reach the model and the actions could never unlock."""
    ctx = _bidi_ctx()
    state = _bidi_state()
    state["hazard_gate"] = hd.DirectiveFirstGate(EXIT, None, started_at=time.monotonic())
    state["hazard_held_question"] = (CATEGORY["fire_or_smoke"], "Do you see flames? yes or no.")

    async def _go():
        bidi_app._end_hazard_gate(state, ctx, "driver-turn")
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.wait(pending, timeout=5)

    asyncio.run(_go())
    reasons = [r.reason for r in state["injection_queue"].items]
    assert "confirm-fire_or_smoke" in reasons, reasons
    q = next(r for r in state["injection_queue"].items if r.reason == "confirm-fire_or_smoke")
    assert q.priority is bidi_app.InjectionPriority.IMMEDIATE
    assert "hazard_held_question" not in state  # released once, not on every gate end


def test_split_mp6_a_new_report_discards_the_previous_held_question():
    """MP6: a held question belongs to the report that caused it. A different report
    arriving first must discard it, not fire it — otherwise the driver is asked about
    brakes while the screen shows the fire directive."""
    ctx = _bidi_ctx()
    state = _bidi_state()
    state["hazard_gate"] = hd.DirectiveFirstGate(BRAKE, None, started_at=time.monotonic())
    state["hazard_held_question"] = (CATEGORY["brake_failure"], "Have you lost braking? yes or no.")
    ws = AsyncMock()

    async def _go():
        handled = await bidi_app._on_driver_hazard_report(
            ws, ctx, state, CATEGORY["fire_or_smoke"], FIRE_REPORT,
        )
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.wait(pending, timeout=5)
        return handled

    assert asyncio.run(_go()) is True
    reasons = [r.reason for r in state["injection_queue"].items]
    assert "confirm-brake_failure" not in reasons, reasons
    assert state.get("hazard_held_question") is None
    # the fire directive is what is on screen and what the gate now wants
    assert state["p0_directive"] == EXIT
    assert state["hazard_gate"].directive == EXIT



# The two Suggestions taken from the security review of the split, pinned directly.
# Mutations MQ3 and MQ4 both survived the session-level tests for the same reason MP6
# and MP7 did: the harness runs the follow-up after the gate has already gone, so the
# "gate present but spent" and "question already held" states never occur in it.

def test_split_mq3_a_spent_gate_does_not_trap_the_question():
    """A gate that has already opened is not holding anything back, so the question
    must go out at once. Held instead, the driver is never asked and the actions can
    never unlock — a driver who needs a tow would not get one."""
    ctx = _bidi_ctx(triage_classification="P3", triage_decision_id="d-spent")
    state = _bidi_state()
    gate = hd.DirectiveFirstGate(EXIT, None, started_at=time.monotonic())
    gate.feed("text", {"text": EXIT}, time.monotonic())  # the model led with it
    assert gate.opened and not gate.active
    state["hazard_gate"] = gate
    ws = AsyncMock()

    async def _go():
        await bidi_app._ask_confirmation_question(
            ws, ctx, state, CATEGORY["fire_or_smoke"], FIRE_REPORT, "P3",
        )
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.wait(pending, timeout=5)

    asyncio.run(_go())
    reasons = [r.reason for r in state["injection_queue"].items]
    assert "confirm-fire_or_smoke" in reasons, reasons
    assert state.get("hazard_held_question") is None


def test_split_mq4_a_second_report_does_not_queue_a_second_question():
    """Two reports close together must leave one question, not two: the driver cannot
    answer two yes/no questions with one yes, and the second would be recorded against
    whichever category the state happened to hold."""
    ctx = _bidi_ctx(triage_classification="P3", triage_decision_id="d-two-q")
    state = _bidi_state()
    state["hazard_gate"] = hd.DirectiveFirstGate(EXIT, None, started_at=time.monotonic())
    state["hazard_held_question"] = (CATEGORY["fire_or_smoke"], "Do you see flames? yes or no.")
    ws = AsyncMock()

    async def _go():
        await bidi_app._ask_confirmation_question(
            ws, ctx, state, CATEGORY["brake_failure"], "I lost my brakes", "P3",
        )
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.wait(pending, timeout=5)

    asyncio.run(_go())
    held = state.get("hazard_held_question")
    assert held is not None and held[0].name == "fire_or_smoke"
    reasons = [r.reason for r in state["injection_queue"].items]
    assert "confirm-brake_failure" not in reasons, reasons
    assert not any(
        c.args and isinstance(c.args[0], dict) and c.args[0].get("name") == "driver_confirmation"
        for c in ws.send_json.call_args_list
    )



# Security review cycle 4: the Warning-1 fix's premise was false on the typed path.
# `_auto_triage_and_inject` runs on spoken FINAL transcripts only, so on a typed first
# turn — and on a spoken turn whose auto-triage call failed — this follow-up is the
# only booker. Returning early on any classifier P0 left a driver whose vehicle the
# classifier itself grades P0 with a directive and no ticket, and no question either.
# The gate now distinguishes a P0 that was already on the session (someone else acted)
# from one this follow-up's own triage call discovered (nobody will).

def test_sec4_a_typed_report_whose_own_triage_returns_p0_is_still_booked():
    calls = []

    def _triage(c, *_a, **_kw):
        calls.append(1)
        c.triage_classification = "P0"
        c.triage_decision_id = "d-typed-p0"
        return {"classification": "P0", "triageDecisionId": "d-typed-p0", "latencyMs": 5}

    ctx = _bidi_ctx(triage_classification=None, triage_decision_id=None)
    state = _bidi_state(auto_triage_fired=False, last_classification=None, surfaced_classification=None)
    frames, ctx, state, m = _drive(
        [], ctx=ctx, state=state, triage=_triage,
        inputs=[{"type": "text.input", "text": "my car is on fire"}],
    )
    assert calls, "the follow-up must run triage itself on a typed first turn"
    assert frames[0].get("_platform_safety_directive") and frames[0]["text"] == EXIT
    assert m.book.call_args.kwargs["priority"] == "P0"
    assert m.audit.await_args.kwargs["verdict"] == "reported"
    assert m.audit.await_args.kwargs["final_classification"] == "P0"
    assert ctx.driver_confirmed_priority == "P0"


def test_sec4_a_typed_report_below_p0_is_still_only_asked():
    def _triage(c, *_a, **_kw):
        c.triage_classification = "P3"
        c.triage_decision_id = "d-typed-p3"
        return {"classification": "P3", "triageDecisionId": "d-typed-p3", "latencyMs": 5}

    ctx = _bidi_ctx(triage_classification=None, triage_decision_id=None)
    state = _bidi_state(auto_triage_fired=False, last_classification=None, surfaced_classification=None)
    frames, ctx, state, m = _drive(
        [], ctx=ctx, state=state, triage=_triage,
        inputs=[{"type": "text.input", "text": "my car is on fire"}],
    )
    assert frames[0].get("_platform_safety_directive")
    _assert_no_actions(frames, ctx, state, m)
    assert state["awaiting_confirmation_category"].name == "fire_or_smoke"


def test_sec4_a_sticky_p0_still_writes_no_row_of_its_own():
    """The cycle-3 Warning must stay closed: a P0 already on the session when the
    report arrived is someone else's to act on, so no second row is written."""
    ctx = _bidi_ctx(triage_classification="P0", triage_decision_id="d-sticky-2")
    state = _bidi_state(last_classification="P0", surfaced_classification="P0")
    frames, ctx, state, m = _drive([_user(FIRE_REPORT)] + _model_events(OBSERVED_DOUBT),
                                   ctx=ctx, state=state)
    _assert_directive_first(frames, EXIT)
    assert not any(c.kwargs.get("verdict") == "reported" for c in m.audit.await_args_list)
    m.book.assert_not_called()


def test_sec4_the_two_p0_cases_are_told_apart_by_what_was_known_before():
    """The distinction the gate turns on, stated so a later editor cannot collapse the
    two branches back together: identical final classification, opposite outcome."""
    def _triage(c, *_a, **_kw):
        c.triage_classification = "P0"
        c.triage_decision_id = "d-fresh"
        return {"classification": "P0", "triageDecisionId": "d-fresh", "latencyMs": 5}

    # fresh: nothing known before the report -> the follow-up acts
    ctx = _bidi_ctx(triage_classification=None, triage_decision_id=None)
    state = _bidi_state(auto_triage_fired=False, last_classification=None, surfaced_classification=None)
    _, ctx_a, _, m_a = _drive(
        [], ctx=ctx, state=state, triage=_triage,
        inputs=[{"type": "text.input", "text": "my car is on fire"}],
    )
    # sticky: the same P0, known before the report -> the follow-up does not
    ctx_b = _bidi_ctx(triage_classification="P0", triage_decision_id="d-fresh")
    state_b = _bidi_state(last_classification="P0", surfaced_classification="P0")
    _, ctx_b, _, m_b = _drive([_user(FIRE_REPORT)], ctx=ctx_b, state=state_b, triage=_triage)

    assert ctx_a.triage_classification == ctx_b.triage_classification == "P0"
    assert m_a.book.called and not m_b.book.called
