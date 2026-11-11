"""Refusal words cancel a recorded yes, on the voice and the text runtime.

Issue: issues/2026-11-02-text-runtime-book-consent/ (parent
issues/2026-11-02-agent-books-unasked/, security review cycle 1 S3).

Before the fix "never mind", "nevermind", "forget it" and "refuse" left the
driver's consent in place: a yes followed by "never mind" still booked.

Run from the repo root:
    VSA_BOOK_WRITE_CMS=false .venv/bin/python -m pytest \\
        agents/supervisor/tests/test_booking_refusal_words.py -q
"""

from __future__ import annotations

import os

import pytest

from agents.supervisor import booking_consent as bc
from agents.supervisor.bidi_app import _record_booking_answer
from agents.supervisor.config import Config
from agents.supervisor.session import SessionContext
from agents.supervisor.tools.book import BookError, make_book_tool

os.environ.setdefault("VSA_BOOK_WRITE_CMS", "false")

CENTER_A: dict = {
    "centerId": "SVC-TEST-001",
    "name": "Fast Fleet Service",
    "address": "100 Main St, Dallas TX",
    "type": "fleet-service",
}

# The words the user asked for (prompt 211), written the ways a driver types or
# a transcriber spells them.
NEW_REFUSALS = [
    "never mind",
    "Never mind.",
    "Never-mind",
    "oh, never mind, thanks",
    "nevermind",
    "Nevermind!",
    "forget it",
    "Forget it.",
    "ok forget it",
    "refuse",
    "I refuse.",
]

# Words that refused before the change must still refuse.
OLD_REFUSALS = ["no", "No thanks", "nope", "nah", "not now", "don't", "cancel", "stop", "wait", "later"]

# Must not read as a refusal: "never" and "mind" and "forget" on their own are
# ordinary words.
NOT_REFUSALS = [
    "yes",
    "never had this light before",
    "mind if I ask the price?",
    "I forgot my keys",
    "forgetting it is easy",
    "refuel first",
]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("VSA_BOOK_WRITE_CMS", "false")
    bc._text_store.clear()
    yield
    bc._text_store.clear()


def _voice_ctx() -> SessionContext:
    ctx = SessionContext(
        tenant_id="meridian-fleet",
        session_id="vsa-sess-refusal-words",
        jwt="j",
        persona_id="fleet_driver",
        vin="1FMCU0F70MUA12345",
        vehicle_id="VEH-TEST-001",
        booking_requires_driver_consent=True,
    )
    ctx.triage_classification = "P3"
    ctx.triage_decision_id = "triage-test-001"
    ctx.booked_by_priority = {}
    ctx.last_offered_service_center = dict(CENTER_A)
    ctx.booking_offer = dict(CENTER_A)
    ctx.booking_consent = dict(CENTER_A)
    return ctx


def _state() -> dict:
    return {"platform_note_tag": "a1b2c3d4"}


def _book(ctx):
    return make_book_tool(ctx, Config.from_env())(
        reportedSymptom="Tire pressure warning light",
        customerActionsTaken=["book-appointment"],
        narrative="Tire pressure light is on; scheduled for inspection.",
    )


# ---------------------------------------------------------------------------
# The shared check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", NEW_REFUSALS + OLD_REFUSALS)
def test_is_booking_refusal_true(text):
    assert bc.is_booking_refusal(text) is True


@pytest.mark.parametrize("text", NOT_REFUSALS)
def test_is_booking_refusal_false(text):
    assert bc.is_booking_refusal(text) is False


def test_a_yes_with_never_mind_is_a_refusal():
    assert bc.is_booking_refusal("yes, actually never mind") is True


# ---------------------------------------------------------------------------
# Voice: _record_booking_answer clears the consent, and book is then refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", NEW_REFUSALS)
def test_voice_refusal_word_clears_consent_and_book_is_refused(text):
    ctx = _voice_ctx()
    _record_booking_answer(ctx, _state(), text)
    assert ctx.booking_consent is None
    assert ctx.booking_offer is None
    with pytest.raises(BookError, match="has not confirmed"):
        _book(ctx)


def test_voice_consent_survives_a_non_refusal():
    ctx = _voice_ctx()
    _record_booking_answer(ctx, _state(), "never had this light before")
    assert ctx.booking_consent == CENTER_A
    result = _book(ctx)
    assert result["requestNumber"].startswith("VSA-")


def test_bidi_keeps_its_old_names():
    """Other code and tests import these names from bidi_app."""
    from agents.supervisor import bidi_app

    assert bidi_app._BOOKING_REFUSAL_TOKENS is bc.BOOKING_REFUSAL_TOKENS
    assert bidi_app._is_affirmation is bc.is_affirmation
    assert bidi_app._AFFIRMATIVE_WORDS is bc.AFFIRMATIVE_WORDS
    assert bidi_app._AFFIRMATIVE_PHRASES is bc.AFFIRMATIVE_PHRASES


# ---------------------------------------------------------------------------
# Text: begin_text_turn clears a stored consent on a refusal word
# ---------------------------------------------------------------------------


def _text_ctx() -> SessionContext:
    ctx = SessionContext(
        tenant_id="meridian-fleet",
        session_id="vsa-sess-refusal-words-text",
        jwt="j",
        persona_id="fleet_driver",
        vin="1FMCU0F70MUA12345",
        vehicle_id="VEH-TEST-001",
        booking_requires_driver_consent=True,
        booking_p0_without_consent=True,
    )
    ctx.triage_classification = "P3"
    ctx.triage_decision_id = "triage-test-001"
    ctx.booked_by_priority = {}
    return ctx


@pytest.mark.parametrize("text", NEW_REFUSALS)
def test_text_refusal_word_clears_stored_consent_and_book_is_refused(text):
    key = bc.text_key("actor-1", "sess-refusal")
    # Turn 1: the offer.
    t1 = _text_ctx()
    seen = bc.begin_text_turn(t1, key, "my tire light is on")
    bc.record_offer_from_tool_result(t1, "find_service_center", bc_result(CENTER_A))
    bc.end_text_turn(t1, key, ok=True, seen_version=seen)
    # Turn 2: the yes.
    t2 = _text_ctx()
    seen = bc.begin_text_turn(t2, key, "yes")
    assert t2.booking_consent == CENTER_A
    bc.end_text_turn(t2, key, ok=True, seen_version=seen)
    # Turn 3: the change of mind.
    t3 = _text_ctx()
    seen = bc.begin_text_turn(t3, key, text)
    assert t3.booking_consent is None
    assert t3.booking_offer is None
    with pytest.raises(BookError, match="has not confirmed"):
        _book(t3)
    bc.end_text_turn(t3, key, ok=True, seen_version=seen)
    assert key not in bc._text_store


def bc_result(center: dict) -> dict:
    import json

    return {
        "toolUseId": "t-1",
        "status": "success",
        "content": [{"text": json.dumps({"found": True, "locationKnown": True, "centers": [center]})}],
    }
