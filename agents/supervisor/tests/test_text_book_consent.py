"""Driver-consent gate on the TEXT runtime (CMS web chat, /assistant/chat).

Issue: issues/2026-11-02-text-runtime-book-consent/ (parent
issues/2026-11-02-agent-books-unasked/, which gated the voice runtime only).

The text runtime builds a new SessionContext for every message, so the offer
(find_service_center's first center) is made in one invocation and the
driver's typed yes arrives in the next. These tests drive the real
agentcore_app.invoke() across several messages of one session, with the real
build_supervisor (Bedrock model patched out), the real Strands hook registry
and the real `book` tool. Only the model's choices are scripted.

Run from the repo root:
    VSA_BOOK_WRITE_CMS=false .venv/bin/python -m pytest \\
        agents/supervisor/tests/test_text_book_consent.py -q
"""

from __future__ import annotations

import base64
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from bedrock_agentcore.runtime.context import RequestContext
from strands.hooks import AfterToolCallEvent

import agents.supervisor.agentcore_app as app_mod
from agents.supervisor import booking_consent as bc
from agents.supervisor.session import SessionContext
from agents.supervisor.supervisor import build_supervisor as real_build_supervisor
from agents.supervisor.tools.book import BookError

os.environ.setdefault("VSA_BOOK_WRITE_CMS", "false")

VIN = "1FMCU0F70MUA12345"
SESSION = "vsa-sess-text-consent-0000000000000001"

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
CENTERS = {"A": CENTER_A, "B": CENTER_B}


def _jwt(sub: str) -> str:
    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'none'})}.{b64({'sub': sub, 'token_use': 'id'})}.sig"


def _fsc_result(center: dict, *, as_json_block: bool = False, location_known: bool = True) -> dict:
    data = {"found": True, "locationKnown": location_known,
            "centers": [center, CENTER_B if center is CENTER_A else CENTER_A]}
    block = {"json": data} if as_json_block else {"text": json.dumps(data)}
    return {"toolUseId": "t-fsc", "status": "success", "content": [block]}


class _Guardrail:
    """Stands in for KioskGuardrailEnforcer: blocks input or output only when told."""

    block_input = False
    block_output = False

    def __init__(self, *a, **k):
        pass

    def check_input(self, text):
        return {"error": "blocked", "blocked": True} if _Guardrail.block_input else None

    def check_output(self, text):
        return {"error": "blocked", "blocked": True} if _Guardrail.block_output else None


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("VSA_BOOK_WRITE_CMS", "false")
    monkeypatch.setattr(app_mod, "resolve_session_coords", lambda ctx, api_base: None)
    monkeypatch.setattr(app_mod, "hydrate_session_history", lambda **kw: [])
    monkeypatch.setattr(app_mod, "record_turn", lambda **kw: None)
    monkeypatch.setattr(app_mod, "check_and_increment", lambda session_id: (True, 1))
    monkeypatch.setattr(app_mod, "KioskGuardrailEnforcer", _Guardrail)
    _Guardrail.block_input = False
    _Guardrail.block_output = False
    bc._text_store.clear()
    yield
    bc._text_store.clear()


class _Session:
    """One web-chat session: each say() is one /assistant/chat message."""

    def __init__(self, monkeypatch, *, actor: str = "actor-1", session_id: str = SESSION,
                 persona: str = "fleet_driver"):
        self.monkeypatch = monkeypatch
        self.actor = actor
        self.session_id = session_id
        self.persona = persona
        self.outcomes: list[tuple[str, object]] = []
        self.ctxs: list[SessionContext] = []

    def say(self, text: str, steps: list[str], *, user_text: str | None = None) -> dict:
        """Send `text`; the scripted model then runs `steps` in order.

        Steps: "triage:P3" (the triage tool set the grade), "fsc:A" (the
        find_service_center tool returned center A first), "book" (the model
        called book), "raise" (the model call failed).
        """
        outcomes = self.outcomes
        ctxs = self.ctxs

        def fake_build(ctx, config, **kw):
            with patch("agents.supervisor.supervisor.BedrockModel", return_value=MagicMock()):
                agent = real_build_supervisor(ctx, config, **kw)
            ctxs.append(ctx)

            def run(prompt):
                for step in steps:
                    if step.startswith("triage:"):
                        ctx.triage_classification = step.split(":", 1)[1]
                        ctx.triage_decision_id = "triage-test-001"
                    elif step.startswith(("fsc:", "fsc-unranked:")):
                        kind, name = step.split(":", 1)
                        agent.hooks.invoke_callbacks(AfterToolCallEvent(
                            agent=agent,
                            selected_tool=None,
                            tool_use={"toolUseId": "t-fsc", "name": "find_service_center", "input": {}},
                            invocation_state={},
                            result=_fsc_result(CENTERS[name], location_known=(kind == "fsc")),
                        ))
                    elif step == "book":
                        tool = agent.tool_registry.registry["book"]
                        try:
                            res = tool(
                                reportedSymptom="Tire pressure warning light",
                                customerActionsTaken=["book-appointment"],
                                narrative="Tire pressure light is on; scheduled for inspection.",
                            )
                            outcomes.append(("booked", res))
                        except BookError as e:
                            outcomes.append(("refused", str(e)))
                    elif step == "raise":
                        raise RuntimeError("model call failed")
                    else:  # pragma: no cover - test bug
                        raise AssertionError(step)
                return SimpleNamespace(message={"content": [{"text": "ok"}]})

            return run

        self.monkeypatch.setattr(app_mod, "build_supervisor", fake_build)
        payload = {
            "prompt": text,
            "tenantId": "meridian-fleet",
            "vin": VIN,
            "vehicleId": "VEH-TEST-001",
            "personaId": self.persona,
        }
        if user_text is not None:
            payload["userText"] = user_text
        return app_mod.invoke(
            payload=payload,
            context=RequestContext(
                session_id=self.session_id,
                request_headers={"X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token": _jwt(self.actor)},
                request=None,
            ),
        )

    def last(self) -> tuple[str, object]:
        assert self.outcomes, "book was never called"
        return self.outcomes[-1]


def _booked(outcome) -> dict:
    kind, value = outcome
    assert kind == "booked", f"expected a booking, got {kind}: {value}"
    return value  # type: ignore[return-value]


def _refused(outcome, match: str = "has not confirmed") -> None:
    kind, value = outcome
    assert kind == "refused", f"expected a refusal, got a booking: {value}"
    assert match in str(value)


# ---------------------------------------------------------------------------
# The gate is on for every text session
# ---------------------------------------------------------------------------


def test_invoke_turns_the_gate_on(monkeypatch):
    s = _Session(monkeypatch)
    s.say("hello", [])
    ctx = s.ctxs[-1]
    assert ctx.booking_requires_driver_consent is True
    assert ctx.booking_p0_without_consent is True


def test_book_with_no_offer_is_refused(monkeypatch):
    s = _Session(monkeypatch)
    s.say("book me in", ["triage:P3", "book"])
    _refused(s.last())


def test_offer_and_book_in_the_same_message_is_refused(monkeypatch):
    s = _Session(monkeypatch)
    s.say("my tire light is on", ["triage:P3", "fsc:A", "book"])
    _refused(s.last())


# ---------------------------------------------------------------------------
# The yes in the next message books the offered center
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("yes", [
    "yes", "Yes.", "yes please", "Yes, book it", "ok, go ahead and book it", "sure", "ok",
    "Let's do it.", "Sounds good, book it", "That works", "go ahead and do it", "Great, book it",
    "Yes, book it for me", "please book it",
])
def test_typed_yes_after_the_offer_books_the_offered_center(monkeypatch, yes):
    s = _Session(monkeypatch)
    s.say("my tire light is on", ["triage:P3", "fsc:A"])
    s.say(yes, ["triage:P3", "book"])
    res = _booked(s.last())
    preferred = res["envelope"]["payload"]["serviceCenter"]["preferred"]
    assert preferred["id"] == CENTER_A["centerId"]
    assert preferred["name"] == CENTER_A["name"]


def test_the_offer_can_come_in_a_json_content_block(monkeypatch):
    ctx = SessionContext(tenant_id="t", session_id="s", jwt="j", persona_id="fleet_driver",
                         booking_requires_driver_consent=True)
    bc.record_offer_from_tool_result(ctx, "find_service_center", _fsc_result(CENTER_B, as_json_block=True))
    assert ctx.booking_offer == CENTER_B
    assert ctx.last_offered_service_center == CENTER_B


@pytest.mark.parametrize("not_yes", [
    "yes but tomorrow", "thanks", "what time?", "how much will it cost", "maybe",
    "ok thanks", "ok thank you", "book?", "That works?", "good",
    "yes?", "ok?", "sure?", "book it?", "go ahead?", "Yes please?",
    "yes yes yes yes yes yes yes yes yes yes yes yes",  # over the 40-character cap
])
def test_a_message_that_is_not_a_yes_does_not_book(monkeypatch, not_yes):
    s = _Session(monkeypatch)
    s.say("my tire light is on", ["triage:P3", "fsc:A"])
    s.say(not_yes, ["triage:P3", "book"])
    _refused(s.last())


def test_a_yes_two_messages_after_the_offer_does_not_count(monkeypatch):
    s = _Session(monkeypatch)
    s.say("my tire light is on", ["triage:P3", "fsc:A"])
    s.say("is it safe to drive?", ["triage:P3"])
    s.say("yes", ["triage:P3", "book"])
    _refused(s.last())


def test_a_yes_with_no_offer_does_not_book(monkeypatch):
    s = _Session(monkeypatch)
    s.say("my tire light is on", ["triage:P3"])
    s.say("yes", ["triage:P3", "book"])
    _refused(s.last())


def test_consent_given_survives_a_follow_up_message(monkeypatch):
    """Yes, then the model asks one more thing, then books: still the driver's yes."""
    s = _Session(monkeypatch)
    s.say("my tire light is on", ["triage:P3", "fsc:A"])
    s.say("yes", ["triage:P3"])
    s.say("the front left one", ["triage:P3", "book"])
    _booked(s.last())


def test_never_mind_after_a_yes_cancels_it(monkeypatch):
    s = _Session(monkeypatch)
    s.say("my tire light is on", ["triage:P3", "fsc:A"])
    s.say("yes", ["triage:P3"])
    s.say("never mind", ["triage:P3", "book"])
    _refused(s.last())


def test_no_to_the_offer_does_not_book(monkeypatch):
    s = _Session(monkeypatch)
    s.say("my tire light is on", ["triage:P3", "fsc:A"])
    s.say("no thanks", ["triage:P3", "book"])
    _refused(s.last())


def test_one_yes_is_one_booking(monkeypatch):
    s = _Session(monkeypatch)
    s.say("my tire light is on", ["triage:P3", "fsc:A"])
    s.say("yes", ["triage:P3", "book"])
    _booked(s.last())
    s.say("book it again", ["triage:P3", "book"])
    _refused(s.last())


# ---------------------------------------------------------------------------
# A second find_service_center call in the yes message
# ---------------------------------------------------------------------------


def test_same_center_again_keeps_the_yes(monkeypatch):
    s = _Session(monkeypatch)
    s.say("my tire light is on", ["triage:P3", "fsc:A"])
    s.say("yes", ["triage:P3", "fsc:A", "book"])
    res = _booked(s.last())
    assert res["envelope"]["payload"]["serviceCenter"]["preferred"]["id"] == CENTER_A["centerId"]


def test_a_different_center_drops_the_yes(monkeypatch):
    s = _Session(monkeypatch)
    s.say("my tire light is on", ["triage:P3", "fsc:A"])
    s.say("yes", ["triage:P3", "fsc:B", "book"])
    _refused(s.last())


def test_after_a_different_center_a_new_yes_books_it(monkeypatch):
    s = _Session(monkeypatch)
    s.say("my tire light is on", ["triage:P3", "fsc:A"])
    s.say("yes", ["triage:P3", "fsc:B"])
    s.say("yes", ["triage:P3", "book"])
    res = _booked(s.last())
    assert res["envelope"]["payload"]["serviceCenter"]["preferred"]["id"] == CENTER_B["centerId"]


# ---------------------------------------------------------------------------
# Who and which session
# ---------------------------------------------------------------------------


def test_another_user_on_the_same_session_id_cannot_use_the_offer(monkeypatch):
    s1 = _Session(monkeypatch, actor="actor-1")
    s1.say("my tire light is on", ["triage:P3", "fsc:A"])
    s2 = _Session(monkeypatch, actor="actor-2")
    s2.say("yes", ["triage:P3", "book"])
    _refused(s2.last())


def test_another_session_of_the_same_user_cannot_use_the_offer(monkeypatch):
    s1 = _Session(monkeypatch, session_id=SESSION)
    s1.say("my tire light is on", ["triage:P3", "fsc:A"])
    s2 = _Session(monkeypatch, session_id=SESSION.replace("0001", "0002"))
    s2.say("yes", ["triage:P3", "book"])
    _refused(s2.last())


# ---------------------------------------------------------------------------
# Failed turns keep nothing
# ---------------------------------------------------------------------------


def test_a_failed_model_call_keeps_no_offer(monkeypatch):
    s = _Session(monkeypatch)
    with pytest.raises(RuntimeError):
        s.say("my tire light is on", ["triage:P3", "fsc:A", "raise"])
    s.say("yes", ["triage:P3", "book"])
    _refused(s.last())


def test_a_failed_model_call_drops_a_given_yes(monkeypatch):
    s = _Session(monkeypatch)
    s.say("my tire light is on", ["triage:P3", "fsc:A"])
    s.say("yes", ["triage:P3"])
    with pytest.raises(RuntimeError):
        s.say("the front left one", ["triage:P3", "raise"])
    s.say("ok", ["triage:P3", "book"])
    _refused(s.last())


def test_a_blocked_reply_keeps_no_offer(monkeypatch):
    s = _Session(monkeypatch)
    _Guardrail.block_output = True
    out = s.say("my tire light is on", ["triage:P3", "fsc:A"])
    assert out.get("blocked") is True
    _Guardrail.block_output = False
    s.say("yes", ["triage:P3", "book"])
    _refused(s.last())


def test_an_old_offer_expires(monkeypatch):
    clock = {"t": 1_000.0}
    monkeypatch.setattr(bc, "_now", lambda: clock["t"])
    s = _Session(monkeypatch)
    s.say("my tire light is on", ["triage:P3", "fsc:A"])
    clock["t"] += bc.TEXT_CONSENT_TTL_SECONDS + 1
    s.say("yes", ["triage:P3", "book"])
    _refused(s.last())


def test_an_offer_inside_the_ttl_still_counts(monkeypatch):
    clock = {"t": 1_000.0}
    monkeypatch.setattr(bc, "_now", lambda: clock["t"])
    s = _Session(monkeypatch)
    s.say("my tire light is on", ["triage:P3", "fsc:A"])
    clock["t"] += bc.TEXT_CONSENT_TTL_SECONDS - 1
    s.say("yes", ["triage:P3", "book"])
    _booked(s.last())


def test_the_store_stays_bounded(monkeypatch):
    for i in range(bc.TEXT_STORE_MAX_SESSIONS + 25):
        ctx = SessionContext(tenant_id="t", session_id=f"s{i}", jwt="j", persona_id="fleet_driver",
                             booking_requires_driver_consent=True)
        key = bc.text_key("a", f"s{i}")
        seen = bc.begin_text_turn(ctx, key, "hi")
        bc.record_offer_from_tool_result(ctx, "find_service_center", _fsc_result(CENTER_A))
        bc.end_text_turn(ctx, key, ok=True, seen_version=seen)
    assert len(bc._text_store) == bc.TEXT_STORE_MAX_SESSIONS
    # The oldest went first.
    assert bc.text_key("a", "s0") not in bc._text_store
    assert bc.text_key("a", f"s{bc.TEXT_STORE_MAX_SESSIONS + 24}") in bc._text_store


# ---------------------------------------------------------------------------
# P0: the classifier's stop-driving grade books without a yes, as on voice
# ---------------------------------------------------------------------------


def test_text_p0_books_without_a_yes(monkeypatch):
    s = _Session(monkeypatch)
    s.say("smoke from the engine", ["triage:P0", "book"])
    res = _booked(s.last())
    assert res["envelope"]["payload"]["priority"] == "P0"


@pytest.mark.parametrize("grade", ["P1", "P2", "P3"])
def test_text_non_p0_still_needs_a_yes(monkeypatch, grade):
    s = _Session(monkeypatch)
    s.say("my tire light is on", [f"triage:{grade}", "book"])
    _refused(s.last())


def test_voice_p0_without_a_yes_is_still_refused():
    """The P0 exception is text-only: voice books P0 on the platform path."""
    from agents.supervisor.config import Config
    from agents.supervisor.tools.book import make_book_tool

    ctx = SessionContext(
        tenant_id="meridian-fleet", session_id="vsa-sess-voice-p0", jwt="j", persona_id="fleet_driver",
        vin=VIN, vehicle_id="VEH-TEST-001", booking_requires_driver_consent=True,
    )
    ctx.triage_classification = "P0"
    ctx.triage_decision_id = "triage-test-001"
    ctx.booked_by_priority = {}
    with pytest.raises(BookError, match="has not confirmed"):
        make_book_tool(ctx, Config.from_env())(
            reportedSymptom="smoke", customerActionsTaken=["create-ticket"], narrative="P0 smoke.",
        )


# ---------------------------------------------------------------------------
# Wiring: the hook, the legacy tool list, the prompt
# ---------------------------------------------------------------------------


def _build(ctx):
    from agents.supervisor.config import Config

    with patch("agents.supervisor.supervisor.BedrockModel", return_value=MagicMock()):
        return real_build_supervisor(ctx, Config.from_env())


def _fire_fsc(agent, center):
    agent.hooks.invoke_callbacks(AfterToolCallEvent(
        agent=agent, selected_tool=None,
        tool_use={"toolUseId": "t-fsc", "name": "find_service_center", "input": {}},
        invocation_state={}, result=_fsc_result(center),
    ))


def test_build_supervisor_records_offers_when_the_gate_is_on():
    ctx = SessionContext(tenant_id="t", session_id="s", jwt="j", persona_id="fleet_driver", vin=VIN,
                         booking_requires_driver_consent=True)
    agent = _build(ctx)
    _fire_fsc(agent, CENTER_A)
    assert ctx.booking_offer == CENTER_A
    assert ctx.booking_offer_made_this_turn is True


def test_build_supervisor_records_nothing_when_the_gate_is_off():
    """cli.py and other callers that leave the gate off are unchanged."""
    ctx = SessionContext(tenant_id="t", session_id="s", jwt="j", persona_id="fleet_driver", vin=VIN)
    agent = _build(ctx)
    _fire_fsc(agent, CENTER_A)
    assert ctx.booking_offer is None


def test_other_tools_and_failed_calls_record_no_offer():
    ctx = SessionContext(tenant_id="t", session_id="s", jwt="j", persona_id="fleet_driver",
                         booking_requires_driver_consent=True)
    bc.record_offer_from_tool_result(ctx, "retrieve", _fsc_result(CENTER_A))
    assert ctx.booking_offer is None
    failed = dict(_fsc_result(CENTER_A), status="error")
    bc.record_offer_from_tool_result(ctx, "find_service_center", failed)
    assert ctx.booking_offer is None
    none_found = {"toolUseId": "t", "status": "success",
                  "content": [{"text": json.dumps({"found": False, "centers": []})}]}
    bc.record_offer_from_tool_result(ctx, "find_service_center", none_found)
    assert ctx.booking_offer is None
    bc.record_offer_from_tool_result(ctx, "find_service_center", RuntimeError("boom"))
    assert ctx.booking_offer is None


def test_legacy_text_path_can_find_a_service_center():
    ctx = SessionContext(tenant_id="t", session_id="s", jwt="j", persona_id="fleet_driver", vin=VIN)
    assert "find_service_center" in _build(ctx).tool_names


def test_kiosk_text_path_has_no_service_center_or_book():
    ctx = SessionContext(tenant_id="t", session_id="s", jwt="j", persona_id="consumer", is_kiosk=True)
    names = _build(ctx).tool_names
    assert "find_service_center" not in names
    assert "book" not in names


def test_book_refusal_tells_the_model_to_offer_a_center_first():
    """The model learns to offer a center first from book's refusal text, not a
    prompt edit (rule 3 stays byte-identical across the voice and text templates;
    test_prompt_content.py::...::test_rule3a_is_byte_identical_across_templates)."""
    from agents.supervisor.config import Config
    from agents.supervisor.tools.book import make_book_tool

    ctx = SessionContext(tenant_id="t", session_id="s", jwt="j", persona_id="fleet_driver", vin=VIN,
                         booking_requires_driver_consent=True)
    ctx.triage_classification = "P3"
    ctx.triage_decision_id = "td"
    ctx.booked_by_priority = {}

    with pytest.raises(BookError) as exc:
        make_book_tool(ctx, Config.from_env())(
            reportedSymptom="x", customerActionsTaken=["book-appointment"], narrative="x",
        )
    assert "find_service_center" in str(exc.value)
    assert "say yes" in str(exc.value)


# ---------------------------------------------------------------------------
# Review cycle 1 (issues/2026-11-02-text-runtime-book-consent/review-cycle1.md)
# ---------------------------------------------------------------------------

from decimal import Decimal  # noqa: E402

# Shaped like a live adp-*-service-network item: the DynamoDB resource layer
# returns every number as Decimal, so Strands can't json.dumps the tool's
# return and sends a Python repr instead (review cycle 1 Critical).
_LIVE_ITEM = {
    "centerId": "SVC-DAL-001",
    "name": "Fast Fleet Service",
    "type": "fleet-service",
    "address": "100 Main St",
    "city": "Dallas",
    "state": "TX",
    "zipCode": "75201",
    "phone": "+15550000001",
    "hours": {"weekday": "7am-6pm", "saturdayHours": Decimal("4")},
    "rating": Decimal("4.6"),
    "averageWaitDays": Decimal("1.5"),
    "fleetDiscount": True,
    "capabilities": ["tires"],
    "brandsServiced": ["Ford"],
    "latitude": Decimal("32.78"),
    "longitude": Decimal("-96.80"),
}


def _real_fsc_result(ctx, *, coords: bool = True) -> dict:
    """Run the real @tool find_service_center through Strands' own tool stream
    (the wrapper the agent loop uses), with DynamoDB returning live-shaped items."""
    import asyncio

    from agents.supervisor.tools.service_network import make_service_network_tool

    if coords:
        ctx.client_latitude, ctx.client_longitude = 32.77, -96.79
    table = MagicMock()
    table.scan.return_value = {"Items": [dict(_LIVE_ITEM)]}
    resource = MagicMock()
    resource.Table.return_value = table
    tool = make_service_network_tool(ctx)
    tool_use = {"toolUseId": "t-1", "name": "find_service_center", "input": {"capability": "tires"}}

    async def run():
        last = None
        async for ev in tool.stream(tool_use, {}):
            last = ev
        return getattr(last, "tool_result", last)

    with patch("agents.supervisor.tools.service_network.boto3.resource", return_value=resource):
        return asyncio.run(run())


def test_a_real_find_service_center_result_with_decimals_is_an_offer():
    ctx = SessionContext(tenant_id="t", session_id="s", jwt="j", persona_id="fleet_driver",
                         booking_requires_driver_consent=True)
    result = _real_fsc_result(ctx)
    assert result["status"] == "success"
    bc.record_offer_from_tool_result(ctx, "find_service_center", result)
    assert ctx.booking_offer is not None
    assert ctx.booking_offer["centerId"] == "SVC-DAL-001"
    assert ctx.booking_offer_made_this_turn is True


def test_a_real_result_with_the_location_unknown_is_not_an_offer():
    """The tool names no center then ("can you confirm your city?")."""
    ctx = SessionContext(tenant_id="t", session_id="s", jwt="j", persona_id="fleet_driver",
                         booking_requires_driver_consent=True)
    result = _real_fsc_result(ctx, coords=False)
    assert result["status"] == "success"
    bc.record_offer_from_tool_result(ctx, "find_service_center", result)
    assert ctx.booking_offer is None
    assert ctx.booking_offer_made_this_turn is False


def test_a_yes_to_confirm_your_city_books_nothing(monkeypatch):
    """Location unknown: the reply asks for the city; the driver's yes is no consent."""
    s = _Session(monkeypatch)
    s.say("my tire light is on", ["triage:P3", "fsc-unranked:A"])
    s.say("yes", ["triage:P3", "book"])
    _refused(s.last())


def test_a_repr_that_is_not_a_literal_is_not_an_offer():
    """The repr is read with ast.literal_eval: code in it is never run."""
    ctx = SessionContext(tenant_id="t", session_id="s", jwt="j", persona_id="fleet_driver",
                         booking_requires_driver_consent=True)
    bad = {"toolUseId": "t", "status": "success",
           "content": [{"text": "{'found': True, 'locationKnown': True, "
                                "'centers': [dict(centerId='SVC-EVIL', name='Evil')]}"}]}
    bc.record_offer_from_tool_result(ctx, "find_service_center", bad)
    assert ctx.booking_offer is None


def test_a_result_without_location_known_is_not_an_offer():
    ctx = SessionContext(tenant_id="t", session_id="s", jwt="j", persona_id="fleet_driver",
                         booking_requires_driver_consent=True)
    no_flag = {"toolUseId": "t", "status": "success",
               "content": [{"text": json.dumps({"found": True, "centers": [CENTER_A]})}]}
    bc.record_offer_from_tool_result(ctx, "find_service_center", no_flag)
    assert ctx.booking_offer is None


WRAPPED = (
    "Vehicle facts (do NOT ask the customer for anything already stated here): VIN on file.\n"
    "Earlier turns: assistant offered Fast Fleet Service. Not guidance.\n"
    "Customer: {u}"
)


def test_the_yes_is_read_from_user_text_when_the_client_wraps_the_prompt(monkeypatch):
    s = _Session(monkeypatch)
    s.say(WRAPPED.format(u="my tire light is on"), ["triage:P3", "fsc:A"], user_text="my tire light is on")
    s.say(WRAPPED.format(u="yes"), ["triage:P3", "book"], user_text="yes")
    _booked(s.last())


def test_a_refusal_in_user_text_wins_over_a_wrapped_prompt(monkeypatch):
    s = _Session(monkeypatch)
    s.say(WRAPPED.format(u="my tire light is on"), ["triage:P3", "fsc:A"], user_text="my tire light is on")
    s.say("yes", ["triage:P3", "book"], user_text="never mind")
    _refused(s.last())


def test_a_blocked_message_ends_the_offer(monkeypatch):
    s = _Session(monkeypatch)
    s.say("my tire light is on", ["triage:P3", "fsc:A"])
    _Guardrail.block_input = True
    out = s.say("something the guardrail blocks", [])
    assert out.get("blocked") is True
    _Guardrail.block_input = False
    s.say("yes", ["triage:P3", "book"])
    _refused(s.last())


def test_a_turn_capped_message_ends_the_offer(monkeypatch):
    s = _Session(monkeypatch)
    s.say("my tire light is on", ["triage:P3", "fsc:A"])
    monkeypatch.setattr(app_mod, "check_and_increment", lambda session_id: (False, 26))
    s.say("hello again", [])
    monkeypatch.setattr(app_mod, "check_and_increment", lambda session_id: (True, 1))
    s.say("yes", ["triage:P3", "book"])
    _refused(s.last())


def _ctx_t():
    return SessionContext(tenant_id="t", session_id="s", jwt="j", persona_id="fleet_driver",
                          booking_requires_driver_consent=True)


def _seed_offer(key):
    c = _ctx_t()
    seen = bc.begin_text_turn(c, key, "hi")
    bc.record_offer_from_tool_result(c, "find_service_center", _fsc_result(CENTER_A))
    bc.end_text_turn(c, key, ok=True, seen_version=seen)


@pytest.mark.parametrize("refusal_first", [True, False])
def test_overlapping_messages_keep_nothing(refusal_first):
    key = bc.text_key("a", "overlap")
    _seed_offer(key)
    yes_ctx, no_ctx = _ctx_t(), _ctx_t()
    yes_seen = bc.begin_text_turn(yes_ctx, key, "yes")
    no_seen = bc.begin_text_turn(no_ctx, key, "never mind")
    assert yes_ctx.booking_consent == CENTER_A
    if refusal_first:
        bc.end_text_turn(no_ctx, key, ok=True, seen_version=no_seen)
        bc.end_text_turn(yes_ctx, key, ok=True, seen_version=yes_seen)
    else:
        bc.end_text_turn(yes_ctx, key, ok=True, seen_version=yes_seen)
        bc.end_text_turn(no_ctx, key, ok=True, seen_version=no_seen)
    assert key not in bc._text_store


def test_a_save_that_fails_keeps_nothing(monkeypatch):
    key = bc.text_key("a", "save-fails")
    _seed_offer(key)
    assert key in bc._text_store
    c = _ctx_t()
    seen = bc.begin_text_turn(c, key, "yes")

    def boom(**kw):
        raise RuntimeError("store write failed")

    monkeypatch.setattr(bc, "_TextBookingState", boom)
    bc.end_text_turn(c, key, ok=True, seen_version=seen)
    assert key not in bc._text_store


def test_a_refusal_clears_the_offer_as_well_as_the_yes():
    key = bc.text_key("a", "refusal-clears-offer")
    _seed_offer(key)
    c = _ctx_t()
    seen = bc.begin_text_turn(c, key, "no thanks")
    assert c.booking_offer is None
    assert c.booking_consent is None
    assert c.last_offered_service_center is None
    bc.end_text_turn(c, key, ok=True, seen_version=seen)
    assert key not in bc._text_store


# ---------------------------------------------------------------------------
# Review cycle 2 (review-cycle2.md Suggestions)
# ---------------------------------------------------------------------------


def test_an_overlapping_new_offer_is_not_overwritten_by_a_slower_yes():
    """RC1: the first message to finish saves a new offer (center B); the slower
    yes to A ends after it and must not keep its consent."""
    key = bc.text_key("a", "overlap-new-offer")
    _seed_offer(key)
    yes_ctx, other = _ctx_t(), _ctx_t()
    yes_seen = bc.begin_text_turn(yes_ctx, key, "yes")
    other_seen = bc.begin_text_turn(other, key, "what about the dealer?")
    bc.record_offer_from_tool_result(other, "find_service_center", _fsc_result(CENTER_B))
    bc.end_text_turn(other, key, ok=True, seen_version=other_seen)
    assert bc._text_store[key].offer == CENTER_B
    bc.end_text_turn(yes_ctx, key, ok=True, seen_version=yes_seen)
    assert key not in bc._text_store


HAZARD = "There's smoke coming from under the hood"


def test_a_blocked_hazard_message_also_ends_the_offer(monkeypatch):
    """RC2: the hazard early return comes after the offer is dropped."""
    assert app_mod._reported_hazard(HAZARD) is not None
    s = _Session(monkeypatch)
    s.say("my tire light is on", ["triage:P3", "fsc:A"])
    _Guardrail.block_input = True
    s.say(HAZARD, [])
    _Guardrail.block_input = False
    s.say("yes", ["triage:P3", "book"])
    _refused(s.last())


def test_a_turn_capped_hazard_message_also_ends_the_offer(monkeypatch):
    """RC3."""
    s = _Session(monkeypatch)
    s.say("my tire light is on", ["triage:P3", "fsc:A"])
    monkeypatch.setattr(app_mod, "check_and_increment", lambda session_id: (False, 26))
    s.say(HAZARD, [])
    monkeypatch.setattr(app_mod, "check_and_increment", lambda session_id: (True, 1))
    s.say("yes", ["triage:P3", "book"])
    _refused(s.last())
