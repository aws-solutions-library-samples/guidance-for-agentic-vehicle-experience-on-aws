"""Driver-consent gate for the `book` tool.

The model's @tool book must refuse unless the driver explicitly agreed to
booking the center that is about to be used.

Issue: issues/2026-11-02-agent-books-unasked/
Fix plan: same file, § "Fix plan".

Before the fix these tests FAIL (or error on import). After the fix they
all pass.

Run (from the repo root with the correct .venv activated):
    VSA_BOOK_WRITE_CMS=false \\
    nice -n 19 .venv-python -m pytest \\
        agents/supervisor/tests/test_book_driver_consent.py -v
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio.plugin
from bedrock_agentcore.runtime.context import RequestContext

from agents.supervisor import bidi_app
from agents.supervisor.bidi_app import (
    _auto_book_on_affirmation,
    _establish_session,
    _record_booking_answer,   # new helper — red until the code is written
)
from agents.supervisor.config import Config
from agents.supervisor.jwt_verify import ENV_VERIFY_MODE, MODE_OBSERVE
from agents.supervisor.session import SessionContext
from agents.supervisor.tools.book import BookError, make_book_tool


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

# Ensure tests never attempt a real CMS write.
os.environ.setdefault("VSA_BOOK_WRITE_CMS", "false")


@pytest.fixture(autouse=True)
def _disable_cms_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VSA_BOOK_WRITE_CMS", "false")


@pytest.fixture
def _require_asyncio_plugin(request):
    assert request.config.pluginmanager.is_registered(pytest_asyncio.plugin), (
        "pytest-asyncio is not loaded"
    )


CENTER_A: dict = {
    "centerId": "SVC-TEST-001",
    "name": "Fast Fleet Service",
    "address": "100 Main St, Dallas TX",
    "distanceMiles": 5.2,
    "phone": "+15550000001",
    "type": "fleet-service",
}
CENTER_B: dict = {
    "centerId": "SVC-TEST-002",
    "name": "Dealer Center",
    "address": "200 Oak Ave, Dallas TX",
    "distanceMiles": 12.0,
    "phone": "+15550000002",
    "type": "dealer",
}

_CONFIG = Config.from_env()


def _ctx(**kw) -> SessionContext:
    """SessionContext with triage done; all new consent fields unset unless overridden."""
    ctx = SessionContext(
        tenant_id="meridian-fleet",
        session_id="vsa-sess-consent-test",
        jwt="fake-jwt-not-used",
        persona_id="fleet_driver",
        vin="1FMCU0F70MUA12345",
        vehicle_id="VEH-TEST-001",
    )
    ctx.triage_classification = "P3"
    ctx.triage_decision_id = "triage-test-001"
    ctx.booked_by_priority = {}
    for k, v in kw.items():
        setattr(ctx, k, v)
    return ctx


def _book_tool(ctx: SessionContext):
    return make_book_tool(ctx, _CONFIG)


def _call_book(tool):
    return tool(
        reportedSymptom="Tire pressure warning light",
        customerActionsTaken=["book-appointment"],
        narrative="Tire pressure light is on; scheduled for inspection.",
    )


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


def _state(**extra):
    st = {
        "platform_note_tag": "a1b2c3d4",
        "event_count": 0,
        "type_counts": {},
        "tool_use_names": {},
        "escalation_tool_in_flight_at": None,
        "auto_triage_fired": True,
        "last_classification": "P3",
        "surfaced_classification": "P3",
        "active_dtcs_for_prompt": [],
        "awaiting_confirmation_category": None,
        "_narration_user_turn": 0,
        "_narration_turn_text": "",
        "_narration_corrected_turn": None,
        "outbound_audio_chunks": 0,
        "vehicle_ctx": {},
        "primed": True,
        "injection_queue": _Queue(),
        "service_center_offered": None,
    }
    st.update(extra)
    return st


def _ns_ctx(**kw):
    """SimpleNamespace ctx for bidi_app writer tests — matches fields accessed by
    bidi_app's output callable."""
    base = dict(
        session_id="s-consent",
        triage_classification="P3",
        triage_decision_id="d-1",
        vin="VIN-CONSENT",
        vehicle_id="VEH-C",
        tenant_id="meridian",
        driver_id="DRV-1",
        driver_first_name=None,
        triage_active_dtcs=[],
        driver_confirmed_priority=None,
        driver_confirmation_pending=False,
        booking_requires_driver_consent=True,
        booking_offer=None,
        booking_consent=None,
        last_offered_service_center=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _drive(events, *, ctx=None, state=None):
    """Drive the real _make_output_callable with booking, audit and triage stubbed."""
    ctx = ctx or _ns_ctx()
    state = state or _state()
    ws = AsyncMock()
    book = MagicMock(return_value={"requestNumber": "VSA-TEST", "status": "draft-created"})
    audit = AsyncMock()
    triage_fn = MagicMock(return_value={"classification": "P3"})

    async def _go():
        out = bidi_app._make_output_callable(
            ws, ctx, state, SimpleNamespace(), SimpleNamespace(send=AsyncMock())
        )
        for e in events:
            await out(e)
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.wait(pending, timeout=5)

    with patch.object(bidi_app, "call_book_endpoint", book), \
         patch.object(bidi_app, "_write_driver_confirmation_audit", audit), \
         patch.object(bidi_app, "call_triage_endpoint", triage_fn):
        asyncio.run(_go())

    return [c.args[0] for c in ws.send_json.call_args_list], ctx, state


def _drive_input(inputs, *, ctx=None, state=None):
    """Drive _make_input_callable with text.input messages."""
    ctx = ctx or _ns_ctx()
    state = state or _state()
    ws = AsyncMock()
    ws.receive_json = AsyncMock(side_effect=list(inputs))
    book = MagicMock(return_value={"requestNumber": "VSA-TEST"})
    audit = AsyncMock()

    async def _go():
        inp = bidi_app._make_input_callable(ws, state, ctx, SimpleNamespace())
        for _ in inputs:
            try:
                await inp()
            except Exception:  # noqa: BLE001
                break

    with patch.object(bidi_app, "call_book_endpoint", book), \
         patch.object(bidi_app, "_write_driver_confirmation_audit", audit):
        asyncio.run(_go())

    return ctx, state


def _fsc_use():
    """Nova starting a find_service_center call."""
    return {
        "type": "tool_use_stream",
        "current_tool_use": {
            "name": "find_service_center",
            "input": {},
            "toolUseId": "tu-fsc-001",
        },
    }


def _fsc_result(centers, found=True):
    return {
        "type": "tool_result",
        "tool_result": {
            "toolUseId": "tu-fsc-001",
            "content": [{"json": {"found": found, "centers": centers}}],
        },
    }


def _user_final(text: str):
    return {
        "type": "bidi_transcript_stream",
        "role": "user",
        "text": text,
        "is_final": True,
    }


# ===========================================================================
# 1. _record_booking_answer — unit tests
# ===========================================================================


def test_rba_affirmation_with_offer_moves_to_consent():
    ctx = _ctx(booking_offer=CENTER_A, booking_consent=None)
    state = _state()
    _record_booking_answer(ctx, state, "yes please")
    assert ctx.booking_consent == CENTER_A


def test_rba_affirmation_without_offer_is_a_no_op():
    ctx = _ctx(booking_offer=None, booking_consent=None)
    state = _state()
    _record_booking_answer(ctx, state, "yes")
    assert ctx.booking_consent is None


@pytest.mark.parametrize("text", [
    "no", "nope", "nah", "cancel", "stop", "wait", "later",
    "not now", "don't book", "dont",
])
def test_rba_refusal_clears_offer_and_consent(text):
    ctx = _ctx(booking_offer=CENTER_A, booking_consent=CENTER_A)
    state = _state()
    _record_booking_answer(ctx, state, text)
    assert ctx.booking_offer is None
    assert ctx.booking_consent is None


def test_rba_empty_text_is_a_no_op():
    ctx = _ctx(booking_offer=CENTER_A, booking_consent=None)
    state = _state()
    _record_booking_answer(ctx, state, "")
    assert ctx.booking_offer == CENTER_A
    assert ctx.booking_consent is None


def test_rba_safety_question_yes_does_not_become_booking_consent():
    """A 'yes' given while awaiting_confirmation_category is set answers the
    safety question, not the booking offer."""
    ctx = _ctx(booking_offer=CENTER_A, booking_consent=None)
    state = _state(awaiting_confirmation_category=MagicMock())
    _record_booking_answer(ctx, state, "yes")
    assert ctx.booking_consent is None


def test_rba_driver_confirmation_pending_yes_does_not_count():
    ctx = _ctx(booking_offer=CENTER_A, booking_consent=None,
               driver_confirmation_pending=True)
    state = _state()
    _record_booking_answer(ctx, state, "yes")
    assert ctx.booking_consent is None


def test_rba_platform_note_is_ignored():
    ctx = _ctx(booking_offer=CENTER_A, booking_consent=None)
    state = _state(platform_note_tag="a1b2c3d4")
    note = "Platform note [a1b2c3d4]: Tell the driver yes we can book."
    _record_booking_answer(ctx, state, note)
    assert ctx.booking_consent is None


def test_rba_indifferent_text_changes_nothing():
    ctx = _ctx(booking_offer=CENTER_A, booking_consent=None)
    state = _state()
    _record_booking_answer(ctx, state, "what is the address?")
    assert ctx.booking_offer == CENTER_A
    assert ctx.booking_consent is None


# ===========================================================================
# 2. @tool book — consent check
# ===========================================================================


def test_tool_refuses_when_flag_set_no_offer_no_consent():
    ctx = _ctx(booking_requires_driver_consent=True,
               booking_offer=None, booking_consent=None)
    book = _book_tool(ctx)
    with pytest.raises(BookError, match="(?i)driver|consent|confirm"):
        _call_book(book)


def test_tool_refuses_when_flag_set_offer_present_but_no_consent():
    ctx = _ctx(
        booking_requires_driver_consent=True,
        booking_offer=CENTER_A,
        booking_consent=None,
        last_offered_service_center=CENTER_A,
    )
    book = _book_tool(ctx)
    with pytest.raises(BookError):
        _call_book(book)


def test_tool_allows_when_flag_is_false():
    """Text runtime default: flag off, no consent needed."""
    ctx = _ctx(booking_requires_driver_consent=False,
               booking_offer=None, booking_consent=None)
    book = _book_tool(ctx)
    result = _call_book(book)
    assert result.get("requestNumber")


def test_tool_allows_with_matching_consent():
    ctx = _ctx(
        booking_requires_driver_consent=True,
        booking_offer=CENTER_A,
        booking_consent=CENTER_A,
        last_offered_service_center=CENTER_A,
    )
    book = _book_tool(ctx)
    result = _call_book(book)
    assert result.get("requestNumber")


def test_tool_refuses_consent_for_different_center_by_id():
    """consent.centerId != offered.centerId → refusal."""
    ctx = _ctx(
        booking_requires_driver_consent=True,
        booking_offer=CENTER_A,
        booking_consent=CENTER_B,         # driver said yes to B
        last_offered_service_center=CENTER_A,  # booking uses A
    )
    book = _book_tool(ctx)
    with pytest.raises(BookError, match="(?i)different|center"):
        _call_book(book)


def test_tool_refuses_after_yes_then_no():
    """yes moves to consent; no clears both; subsequent book call fails."""
    ctx = _ctx(
        booking_requires_driver_consent=True,
        booking_offer=CENTER_A,
        booking_consent=None,
        last_offered_service_center=CENTER_A,
    )
    state = _state()
    # Simulate yes → consent set
    _record_booking_answer(ctx, state, "yes")
    assert ctx.booking_consent == CENTER_A
    # Simulate no → clears consent
    _record_booking_answer(ctx, state, "no")
    assert ctx.booking_consent is None
    book = _book_tool(ctx)
    with pytest.raises(BookError):
        _call_book(book)


def test_tool_refuses_when_consent_set_but_offered_center_none():
    """Fail-closed when booking_consent is a valid dict but
    last_offered_service_center is None/missing/cleared.

    The gate holds today by observation: `_same_center(a, None)` short-circuits
    at its first line (`if b is None: return False`), and the model's book path
    then raises BookError via the "different service center" branch. This test
    pins that property as an assertion rather than observation — a future
    refactor that drops the `None` short-circuit (e.g. "simplify chained guards")
    would otherwise survive the whole suite and surface at staging as an
    uncaught AttributeError on None.get("centerId"). Security-review-cycle1.md
    W1; mutation MG recorded in mutants.md.
    """
    ctx = _ctx(
        booking_requires_driver_consent=True,
        booking_offer=CENTER_A,
        booking_consent=CENTER_A,
        last_offered_service_center=None,  # cleared, never set, or missing
    )
    book = _book_tool(ctx)
    with pytest.raises(BookError, match="(?i)center|consent"):
        _call_book(book)


def test_consent_consumed_after_booking():
    """One yes = one booking. After minting, consent and offer are cleared."""
    ctx = _ctx(
        booking_requires_driver_consent=True,
        booking_offer=CENTER_A,
        booking_consent=CENTER_A,
        last_offered_service_center=CENTER_A,
    )
    book = _book_tool(ctx)
    _call_book(book)
    assert ctx.booking_consent is None
    assert ctx.booking_offer is None


def test_second_booking_refuses_after_first_consumed_consent():
    """After the first booking consumes consent, a new mint attempt (different
    priority so idempotency replay doesn't hide the check) is refused."""
    ctx = _ctx(
        booking_requires_driver_consent=True,
        booking_offer=CENTER_A,
        booking_consent=CENTER_A,
        last_offered_service_center=CENTER_A,
    )
    book = _book_tool(ctx)
    _call_book(book)                    # mints P3, consumes consent
    # Force a new mint: change priority so the idempotency cache won't replay
    ctx.triage_classification = "P2"
    ctx.booked_by_priority = {}
    with pytest.raises(BookError):
        _call_book(book)


def test_replay_bypasses_consent_check():
    """Idempotent replays at the same priority return the cached booking
    without calling the consent check."""
    ctx = _ctx(
        booking_requires_driver_consent=True,
        booking_offer=CENTER_A,
        booking_consent=CENTER_A,
        last_offered_service_center=CENTER_A,
    )
    book = _book_tool(ctx)
    first = _call_book(book)           # mints P3; consent consumed
    # consent is now None; idempotency cache has P3
    second = _call_book(book)          # should replay, not re-check consent
    assert second["requestNumber"] == first["requestNumber"]


# (test_nothing_written_on_refusal removed — superseded by
# test_nothing_written_on_refusal_no_cms_write below, which covers the
# same assertions plus the no-CMS-write property; review-cycle2.md S3)


# ===========================================================================
# 3. Writer: find_service_center tool_result → booking_offer on ctx
# ===========================================================================


def test_fsc_result_sets_booking_offer():
    _, ctx, _ = _drive([_fsc_use(), _fsc_result([CENTER_A])])
    assert ctx.booking_offer == CENTER_A


def test_fsc_result_clears_prior_consent():
    """A new center offer invalidates any prior consent — driver must re-affirm."""
    ctx_start = _ns_ctx(
        booking_offer=CENTER_B,
        booking_consent=CENTER_B,
        last_offered_service_center=CENTER_B,
    )
    _, ctx, _ = _drive([_fsc_use(), _fsc_result([CENTER_A])], ctx=ctx_start)
    assert ctx.booking_offer == CENTER_A
    assert ctx.booking_consent is None


def test_fsc_no_centers_does_not_set_booking_offer():
    """find_service_center with empty centers list must not set booking_offer."""
    _, ctx, _ = _drive([_fsc_use(), _fsc_result([])])
    assert ctx.booking_offer is None


# ===========================================================================
# 4. Writer: spoken user-final transcript → _record_booking_answer called
# ===========================================================================


def test_spoken_yes_sets_consent():
    ctx_start = _ns_ctx(booking_offer=CENTER_A, booking_consent=None,
                        last_offered_service_center=CENTER_A)
    _, ctx, _ = _drive([_user_final("yes please")], ctx=ctx_start)
    assert ctx.booking_consent == CENTER_A


def test_spoken_no_clears_offer_and_consent():
    ctx_start = _ns_ctx(booking_offer=CENTER_A, booking_consent=CENTER_A,
                        last_offered_service_center=CENTER_A)
    _, ctx, _ = _drive([_user_final("no thanks")], ctx=ctx_start)
    assert ctx.booking_offer is None
    assert ctx.booking_consent is None


# ===========================================================================
# 5. Writer: text.input path → _record_booking_answer called
# ===========================================================================


def test_typed_yes_sets_consent():
    ctx_start = _ns_ctx(booking_offer=CENTER_A, last_offered_service_center=CENTER_A)
    ctx, _ = _drive_input([{"type": "text.input", "text": "yes"}], ctx=ctx_start)
    assert ctx.booking_consent == CENTER_A


def test_typed_no_clears_offer_and_consent():
    ctx_start = _ns_ctx(booking_offer=CENTER_A, booking_consent=CENTER_A,
                        last_offered_service_center=CENTER_A)
    ctx, _ = _drive_input(
        [{"type": "text.input", "text": "cancel"}],
        ctx=ctx_start,
    )
    assert ctx.booking_offer is None
    assert ctx.booking_consent is None


# ===========================================================================
# 6. book tool_result handler: consent + offer cleared after booking
# ===========================================================================


def test_book_tool_result_clears_consent_and_offer():
    """When the output callable processes a successful book tool_result,
    ctx.booking_consent and ctx.booking_offer are cleared."""
    ctx_start = _ns_ctx(
        booking_offer=CENTER_A,
        booking_consent=CENTER_A,
        last_offered_service_center=CENTER_A,
    )
    # Register the tool_use first so tool_result can look up the name
    tool_use_event = {
        "type": "tool_use_stream",
        "current_tool_use": {
            "name": "book",
            "input": {},
            "toolUseId": "tu-book-001",
        },
    }
    tool_result_event = {
        "type": "tool_result",
        "tool_result": {
            "toolUseId": "tu-book-001",
            "content": [{"json": {
                "requestNumber": "VSA-2026-TEST-1234",
                "status": "draft-created",
            }}],
        },
    }
    _, ctx, _ = _drive([tool_use_event, tool_result_event], ctx=ctx_start)
    assert ctx.booking_consent is None
    assert ctx.booking_offer is None


# ===========================================================================
# 7. _establish_session sets booking_requires_driver_consent = True
# ===========================================================================


@pytest.mark.asyncio
async def test_establish_session_sets_booking_requires_driver_consent(monkeypatch):
    """The bidi runtime's session factory must set the flag so the tool can
    enforce consent. A mutant that omits the assignment must fail this test."""
    monkeypatch.setenv(ENV_VERIFY_MODE, MODE_OBSERVE)
    # No vehicle/fleet claims → vehicle_scope.resolve falls through to
    # STATUS_UNCHANGED and does not call lookup_row (no DDB needed).
    rc = RequestContext(
        session_id="vsa-sess-consent-flag",
        request_headers={
            "x-amzn-bedrock-agentcore-runtime-custom-user-token": "fake.jwt.token",
            "x-amzn-bedrock-agentcore-runtime-custom-tenant-id": "meridian-fleet",
            "x-amzn-bedrock-agentcore-runtime-custom-vin": "1FMCU0F70MUA12345",
        },
        request=None,
    )
    ws = AsyncMock()
    ws.receive_json = AsyncMock(side_effect=AssertionError("should not be called"))

    session = await _establish_session(ws, rc, "vsa-sess-consent-flag")
    assert session.booking_requires_driver_consent is True



# ===========================================================================
# C1. Also asserts no CMS write (extends test_nothing_written_on_refusal,
# which is superseded and removed — see below)
# ===========================================================================


def test_nothing_written_on_refusal_no_cms_write(monkeypatch):
    """When the consent check raises, booked_by_priority stays empty AND
    _write_cms_service_history is never called (mutant MB: check after CMS write)."""
    monkeypatch.setenv("VSA_BOOK_WRITE_CMS", "true")
    ctx = _ctx(
        booking_requires_driver_consent=True,
        booking_offer=None,
        booking_consent=None,
        vehicle_id="VEH-TEST-001",
    )
    book = _book_tool(ctx)

    import agents.supervisor.tools.book as _book_mod
    with patch.object(_book_mod, "_write_cms_service_history") as mock_write:
        with pytest.raises(BookError):
            _call_book(book)
        mock_write.assert_not_called()

    assert not ctx.booked_by_priority


# ===========================================================================
# W1. No-downgrade replay with flag on, no consent → returns held booking
# ===========================================================================

_HELD_P0 = {
    "requestNumber": "VSA-2026-P0-HELD",
    "status": "draft-created",
    "messageId": "msg-held",
}


def test_no_downgrade_replay_flag_on_returns_held_without_consent():
    """Flag on, no consent, but a P0 booking is already held.
    The no-downgrade replay returns the held booking without reaching
    the consent gate (mutant MA: check between replays → would refuse)."""
    ctx = _ctx(
        booking_requires_driver_consent=True,
        booking_offer=None,
        booking_consent=None,   # no consent!
        booked_by_priority={"P0": _HELD_P0},
        triage_classification="P3",
    )
    book = _book_tool(ctx)
    result = _call_book(book)
    assert result["requestNumber"] == "VSA-2026-P0-HELD"


def test_idempotent_replay_with_driver_confirmed_p0_returns_held_without_consent():
    """Idempotent replay: flag on, driver_confirmed_priority=P0, same effective
    priority → returns cached without reaching consent gate."""
    ctx = _ctx(
        booking_requires_driver_consent=True,
        booking_offer=None,
        booking_consent=None,
        driver_confirmed_priority="P0",
        booked_by_priority={"P0": _HELD_P0},
        triage_classification="P3",
    )
    book = _book_tool(ctx)
    result = _call_book(book)
    assert result["requestNumber"] == "VSA-2026-P0-HELD"


# ===========================================================================
# W2. _same_center: tests without centerId
# ===========================================================================

CENTER_NO_ID_A: dict = {
    "name": "Garage Nord",
    "address": "99 Rue de la Paix, Paris",
    "distanceMiles": 3.1,
}
CENTER_NO_ID_B_DIFF_ADDR: dict = {
    "name": "Garage Nord",
    "address": "12 Avenue du Général, Paris",  # different address
    "distanceMiles": 3.1,
}
CENTER_WITH_ID: dict = {
    "centerId": "SVC-PARIS-001",
    "name": "Garage Nord",
    "address": "99 Rue de la Paix, Paris",
}
CENTER_BARE: dict = {
    "distanceMiles": 9.9,  # no centerId, no name, no address
}


def test_same_center_no_centerid_same_name_address_books():
    """No centerId on either side; name+address match → books."""
    ctx = _ctx(
        booking_requires_driver_consent=True,
        booking_offer=CENTER_NO_ID_A,
        booking_consent=CENTER_NO_ID_A,
        last_offered_service_center=CENTER_NO_ID_A,
    )
    book = _book_tool(ctx)
    result = _call_book(book)
    assert "requestNumber" in result


def test_same_center_no_centerid_different_address_refuses():
    """No centerId; same name but different address → refused.
    Catches mutant MF (name+address fallback always returns True)."""
    ctx = _ctx(
        booking_requires_driver_consent=True,
        booking_offer=CENTER_NO_ID_A,
        booking_consent=CENTER_NO_ID_A,
        last_offered_service_center=CENTER_NO_ID_B_DIFF_ADDR,
    )
    book = _book_tool(ctx)
    with pytest.raises(BookError, match="(?i)center|consent"):
        _call_book(book)


def test_same_center_one_side_centerid_other_name_address():
    """Consent has centerId; offered has only name+address → falls back
    to name+address comparison (no centerId on offered side)."""
    offered_no_id = {"name": "Garage Nord", "address": "99 Rue de la Paix, Paris"}
    ctx = _ctx(
        booking_requires_driver_consent=True,
        booking_offer=CENTER_WITH_ID,
        booking_consent=CENTER_WITH_ID,
        last_offered_service_center=offered_no_id,
    )
    book = _book_tool(ctx)
    # Both have name+address so the fallback path matches — books.
    result = _call_book(book)
    assert "requestNumber" in result


def test_same_center_bare_dicts_refuses():
    """Two dicts with no centerId, name or address must NOT match
    (guard against accidentally matching unrelated proximity records)."""
    ctx = _ctx(
        booking_requires_driver_consent=True,
        booking_offer=CENTER_BARE,
        booking_consent=CENTER_BARE,
        last_offered_service_center={"distanceMiles": 9.9},
    )
    book = _book_tool(ctx)
    with pytest.raises(BookError, match="(?i)center|consent"):
        _call_book(book)


# ===========================================================================
# W3. _auto_book_on_affirmation clears ctx.booking_consent and
#     ctx.booking_offer (not last_offered_service_center) after a successful
#     auto-book, so a later model @tool book cannot use stale consent
#     (issues/2026-11-02-agent-books-unasked/review-cycle2.md W1).
# ===========================================================================


def test_auto_book_on_affirmation_clears_consent_and_offered_center():
    """After _auto_book_on_affirmation books, booking_consent and
    booking_offer are None so a later model @tool book cannot use stale
    consent to mint a second booking (W3). last_offered_service_center
    must NOT be cleared here — it is read by _build_payload for every
    platform booking (review-cycle2.md W1)."""
    ctx_start = _ns_ctx(
        booking_offer=CENTER_A,
        booking_consent=CENTER_A,
        last_offered_service_center=CENTER_A,
    )
    ctx_start.triage_classification = "P3"
    ctx_start.triage_decision_id = "d1"
    state = _state(service_center_offered=CENTER_A)

    async def _run():
        ws = AsyncMock()
        with patch.object(bidi_app, "call_book_endpoint",
                          MagicMock(return_value={"requestNumber": "VSA-W3", "status": "draft-created", "messageId": "m1"})), \
             patch.object(bidi_app, "call_triage_endpoint",
                          MagicMock(return_value={"classification": "P3", "triageDecisionId": "d1"})):
            await _auto_book_on_affirmation(
                ws, ctx_start, _CONFIG, state, "yes"
            )

    asyncio.run(_run())

    assert ctx_start.booking_consent is None
    assert ctx_start.booking_offer is None
    # last_offered_service_center must be unchanged — _build_payload reads it
    assert ctx_start.last_offered_service_center == CENTER_A


# ===========================================================================
# W5. Composed flows: offer → yes → book on one real SessionContext
# (cycle-1 W5; cycle-2 W2 — previously used hand-set fields, now runs
# entirely through real code so MW5 is caught)
# ===========================================================================


def test_composed_spoken_offer_yes_books():
    """offer (fsc tool_result) + spoken 'yes' + @tool book all on ONE real
    SessionContext with no hand-set consent fields. Tests the seam that
    consent originates from booking_offer and book checks it against
    last_offered_service_center. A mutant (MW5) that normalises centerId
    in last_offered_service_center but not in booking_offer is caught here
    and not by a test that sets both fields by hand from the same dict.
    (review-cycle2.md W2)"""
    ctx = _ctx(booking_requires_driver_consent=True)

    # One _drive call: FSC result sets booking_offer + last_offered_service_center;
    # spoken "yes" calls _record_booking_answer which sets booking_consent.
    _drive(
        [_fsc_use(), _fsc_result([CENTER_A]), _user_final("yes")],
        ctx=ctx,
    )
    assert ctx.booking_offer is not None, "fsc result should set booking_offer"
    assert ctx.booking_consent is not None, "spoken yes should set booking_consent"

    # @tool book on the same ctx — consent and offer came from real code paths.
    book = _book_tool(ctx)
    result = _call_book(book)
    assert "requestNumber" in result


def test_composed_typed_offer_yes_books():
    """offer (fsc tool_result) + typed 'yes please' + @tool book all on ONE
    real SessionContext. Same MW5 guard as the spoken test: _drive sets
    booking_offer and last_offered_service_center from the fsc writer;
    _drive_input sets booking_consent from the typed-input path.
    (review-cycle2.md W2)"""
    ctx = _ctx(booking_requires_driver_consent=True)

    # FSC result sets booking_offer + last_offered_service_center
    _drive([_fsc_use(), _fsc_result([CENTER_A])], ctx=ctx)
    assert ctx.booking_offer is not None, "fsc result should set booking_offer"

    # Typed "yes please" sets booking_consent
    _drive_input(
        [{"type": "text.input", "text": "yes please"}],
        ctx=ctx,
    )
    assert ctx.booking_consent is not None, "typed yes should set booking_consent"

    # @tool book on the same ctx
    book = _book_tool(ctx)
    result = _call_book(book)
    assert "requestNumber" in result


def test_composed_no_yes_refuses():
    """offer + no answer → @tool book raises BookError."""
    ctx = _ctx(
        booking_requires_driver_consent=True,
        booking_offer=CENTER_A,
        booking_consent=None,
        last_offered_service_center=CENTER_A,
    )
    book = _book_tool(ctx)
    with pytest.raises(BookError, match="(?i)driver|consent|confirm"):
        _call_book(book)


# ===========================================================================
# S1. Refusal during a pending safety question clears consent
# ===========================================================================


def test_s1_refusal_during_safety_question_clears_consent():
    """A 'no' spoken while awaiting_confirmation_category is set should
    clear booking_consent (S1: refusal check moved before safety guard)."""
    ctx = _ctx(
        booking_offer=CENTER_A,
        booking_consent=CENTER_A,
    )
    # Safety question is pending
    state = _state(awaiting_confirmation_category=MagicMock())
    _record_booking_answer(ctx, state, "no don't book anything")
    assert ctx.booking_consent is None
    assert ctx.booking_offer is None


def test_s1_yes_during_safety_question_does_not_count_as_booking_yes():
    """A 'yes' spoken during a safety question still does NOT set consent
    (the refusal-first reorder must not accidentally grant affirmations)."""
    ctx = _ctx(booking_offer=CENTER_A, booking_consent=None)
    state = _state(awaiting_confirmation_category=MagicMock())
    _record_booking_answer(ctx, state, "yes")
    assert ctx.booking_consent is None
