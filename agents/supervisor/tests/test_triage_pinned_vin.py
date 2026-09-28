"""Fix Group 18 (spec 2026-09-23-cvx-escalation-recommend-not-act): a session
pinned to the caller's claim vehicle always triages that vehicle.

Review Cycle 1 W3: the /assistant/chat Lambda pins the first payload, but
`triage` takes its VIN from the model and writes it to `ctx.vin`. A VIN from the
page (CMS puts the page VIN into the booking chip's prompt) or from the driver
would then replace the claim vehicle, and a later `book` would write a row keyed
on the claim vehicle carrying another vehicle's triage. With `ctx.vin_pinned`
the model-facing tool classifies `ctx.vin` whatever VIN the model passes.

Run: pytest agents/supervisor/tests/test_triage_pinned_vin.py -v
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agents.supervisor.config import Config
from agents.supervisor.session import SessionContext
from agents.supervisor.tools import triage as triage_mod

PINNED = "MRDN0000000000015"
OTHER = "MRDN0000000000003"


@pytest.fixture
def config() -> Config:
    return Config.from_env()


def _ctx(vin, pinned):
    return SessionContext(
        tenant_id="meridian", session_id="s-pin", jwt="fake", persona_id="fleet_driver",
        vin=vin, vehicle_id="VEH-MRDN-0015", vin_pinned=pinned,
    )


def _decision_id(vin: str) -> str:
    """A triage decision id that names the VIN it was made for."""
    return f"t-{vin[-2:]}"


def _run(ctx, config, model_vin, classification="P2"):
    """Call the model-facing tool; return (result, VINs posted to /triage)."""
    def respond(*_a, json, **_kw):
        body = {"classification": classification, "sessionId": _decision_id(json["vin"]),
                "decidedAt": "2026-09-28T16:00:00Z", "latencyMs": 5, "activeDtcs": []}
        return SimpleNamespace(status_code=200, json=lambda: body, text="")

    post = MagicMock(side_effect=respond)
    tool_fn = triage_mod.make_triage_tool(ctx, config)
    with patch.object(triage_mod.requests, "post", post):
        result = tool_fn(vin=model_vin)
    posted = [c.kwargs["json"]["vin"] for c in post.call_args_list]
    return result, posted


class TestPinnedSession:
    def test_a_different_vin_is_replaced_by_the_pinned_one(self, config):
        ctx = _ctx(PINNED, True)
        result, posted = _run(ctx, config, OTHER)
        assert posted == [PINNED]
        assert ctx.vin == PINNED
        assert result["vinNote"] == triage_mod.PINNED_VIN_NOTE

    def test_the_pinned_vin_in_another_case_needs_no_note(self, config):
        ctx = _ctx(PINNED, True)
        result, posted = _run(ctx, config, PINNED.lower())
        assert posted == [PINNED]
        assert "vinNote" not in result

    def test_unknown_pinned_vin_refuses_without_calling_triage(self, config):
        ctx = _ctx(None, True)
        post = MagicMock()
        tool_fn = triage_mod.make_triage_tool(ctx, config)
        with patch.object(triage_mod.requests, "post", post):
            with pytest.raises(triage_mod.TriageError) as err:
                tool_fn(vin=OTHER)
        assert str(err.value) == triage_mod.PINNED_VIN_UNKNOWN
        post.assert_not_called()
        assert ctx.vin is None

    def test_a_p0_still_gets_the_directive(self, config):
        ctx = _ctx(PINNED, True)
        result, _ = _run(ctx, config, OTHER, classification="P0")
        assert result["sayFirst"]
        assert result["vinNote"] == triage_mod.PINNED_VIN_NOTE


class TestUnpinnedSessionIsUnchanged:
    def test_the_model_vin_is_used(self, config):
        ctx = _ctx(PINNED, False)
        result, posted = _run(ctx, config, OTHER)
        assert posted == [OTHER]
        assert ctx.vin == OTHER
        assert "vinNote" not in result

    def test_no_vin_on_ctx_is_fine(self, config):
        ctx = _ctx(None, False)
        _, posted = _run(ctx, config, OTHER)
        assert posted == [OTHER]


def test_vin_pinned_defaults_to_false():
    assert SessionContext(tenant_id="t", session_id="s", jwt="j", persona_id="fleet_driver").vin_pinned is False


def test_direct_callers_are_not_affected(config):
    """call_triage_endpoint (bidi's direct callers pass trusted VINs) has no pin check."""
    ctx = _ctx(PINNED, True)
    body = {"classification": "P2", "sessionId": "t", "decidedAt": "x", "latencyMs": 1}
    post = MagicMock(return_value=SimpleNamespace(status_code=200, json=lambda: body, text=""))
    with patch.object(triage_mod.requests, "post", post):
        triage_mod.call_triage_endpoint(ctx, config, OTHER, None)
    assert post.call_args.kwargs["json"]["vin"] == OTHER


def test_triage_then_book_on_a_pinned_session_writes_one_claim_vehicle_row(config, monkeypatch):
    """Review Cycle 2 S1: the end-to-end form of W3. triage(OTHER) then book on a
    pinned session writes exactly one row, and every vehicle field in it is the
    claim vehicle's."""
    from agents.supervisor.tools import book as book_mod

    monkeypatch.setenv("VSA_BOOK_WRITE_CMS", "true")
    puts: list[dict] = []

    class _Table:
        def __init__(self, name):
            self.name = name

        def put_item(self, *, Item):
            puts.append({"table": self.name, **Item})

        def get_item(self, *, Key, **_kw):
            assert Key == {"vehicleId": "VEH-MRDN-0015"}
            return {"Item": {"vehicleId": "VEH-MRDN-0015", "vin": PINNED, "make": "Meridian"}}

    monkeypatch.setattr(book_mod, "_get_ddb", lambda: SimpleNamespace(Table=_Table))

    ctx = _ctx(PINNED, True)
    _run(ctx, config, OTHER, classification="P2")
    book_fn = book_mod.make_book_tool(ctx, config)
    result = book_fn(reportedSymptom="brake light", customerActionsTaken=["create-ticket"], narrative="n")

    rows = [p for p in puts if p["table"] == book_mod.CMS_SERVICE_HISTORY_TABLE]
    assert len(rows) == 1
    assert rows[0]["vehicleId"] == "VEH-MRDN-0015"
    assert rows[0]["vin"] == PINNED
    # The decision on the row is the claim vehicle's, not the other VIN's.
    assert rows[0]["triageDecisionId"] == _decision_id(PINNED)
    assert rows[0]["triageDecisionId"] != _decision_id(OTHER)
    assert OTHER not in str(result)
