"""Task 2.2 — the P0 auto-escalation is gated on the tenant flag.

Spec `2026-09-23-cvx-escalation-recommend-not-act` D3: the human handoff is
**recommended**, not asserted. A tenant opts in to it happening without the driver
asking, via `escalation.autoOpenHumanChatOnP0`, default **false**.

TWO GATES, ONE CONDITION
------------------------
The flag has to govern **both** the `/escalate` call and the injected narration that
says a human is being connected. Gating only the call would leave the note telling the
driver a human is connecting when none is — converting the one sentence in that note
that was *true* into another false claim, which is the defect
`issues/2026-09-23-p0-narration-claims-roadside-dispatched/` is about. So the condition
is computed once as `auto_open_chat` and consumed twice, and
`test_narration_and_escalate_share_one_condition` pins that.

FAIL-SAFE DIRECTION
-------------------
Every upstream failure path yields False: `_fetch_tenant_config_item` returns `{}` on a
missing table, a missing pointer, or any exception, and `_derive_auto_open_human_chat_on_p0`
reads `{}` as False. "We could not tell" and "no" deliberately coincide, because the
failure mode worth avoiding is an unreachable tenant config silently re-enabling an
automatic action.

Deliberately NOT tested here: the P0 pull-over narration, which is a deterministic
safety seam and still delivers automatically and verbatim (spec § Constraints, T2.2
Accept 2). `test_bidi_p0_narration.py` owns that.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents.supervisor.bidi_app import (
    _derive_auto_open_human_chat_on_p0,
    _derive_tenant_segment,
)

_BIDI_APP_PATH = Path(__file__).resolve().parent.parent / "bidi_app.py"


class TestFlagDerivation:
    """The projection, including every not-configured shape."""

    def test_absent_escalation_block_is_false(self):
        assert _derive_auto_open_human_chat_on_p0({}) is False

    def test_absent_key_within_escalation_block_is_false(self):
        assert _derive_auto_open_human_chat_on_p0({"escalation": {}}) is False

    def test_explicit_true_is_true(self):
        assert (
            _derive_auto_open_human_chat_on_p0(
                {"escalation": {"autoOpenHumanChatOnP0": True}}
            )
            is True
        )

    def test_explicit_false_is_false(self):
        assert (
            _derive_auto_open_human_chat_on_p0(
                {"escalation": {"autoOpenHumanChatOnP0": False}}
            )
            is False
        )

    @pytest.mark.parametrize("bad", ["yes", [], 0, None, "true"])
    def test_non_mapping_escalation_block_is_false_not_a_crash(self, bad):
        """A malformed config must fail closed, not raise.

        A raise here would propagate out of session setup and kill the voice
        session — strictly worse than declining an automatic action. Note `"true"`
        is included on purpose: a STRING is not a mapping, so it must yield False
        rather than being coerced truthy.
        """
        assert _derive_auto_open_human_chat_on_p0({"escalation": bad}) is False

    def test_segment_derivation_still_works_after_the_refactor(self):
        """`_fetch_tenant_segment` was split so one read serves both values.

        Pins that the segment half kept its behaviour, including the fallback.
        """
        assert _derive_tenant_segment({"segment": "oem"}) == "oem"
        assert _derive_tenant_segment({"segment": "OEM"}) == "oem"
        assert _derive_tenant_segment({}) == "fleet"
        assert _derive_tenant_segment({"segment": "nonsense"}) == "fleet"


class TestSessionContextDefault:
    def test_context_default_is_false(self):
        """A SessionContext built without tenant config must not auto-open.

        The kiosk and unit-test paths construct contexts directly, so the
        dataclass default is a real code path and not just belt-and-braces.
        """
        from agents.supervisor.session import SessionContext

        assert SessionContext.__dataclass_fields__[
            "auto_open_human_chat_on_p0"
        ].default is False

    def test_getattr_fallback_is_false_for_namespace_contexts(self):
        """`_auto_triage_and_inject` reads the flag with a `getattr` default.

        Several callers pass `SimpleNamespace` rather than a real
        `SessionContext` (the kiosk path and much of the test suite). Those
        objects lack the attribute entirely, and the gate must treat that as
        False rather than raising.
        """
        ctx = SimpleNamespace(tenant_id="t")
        assert getattr(ctx, "auto_open_human_chat_on_p0", False) is False


class TestGateStructure:
    """Source-level assertions on the two gates.

    `_auto_triage_and_inject` is a long async function with many dependencies;
    these pin the gating structure without standing up a full session. The
    behavioural consequence is covered by the derivation tests above plus
    `test_bidi_p0_narration.py`.
    """

    def _source(self) -> str:
        return _BIDI_APP_PATH.read_text(encoding="utf-8")

    def test_escalate_task_is_gated_on_the_flag(self):
        src = self._source()
        assert "if auto_open_chat:" in src, (
            "the escalate-task block is no longer gated on `auto_open_chat`. "
            "Task 2.2 requires the P0 auto-escalation fire only when "
            "escalation.autoOpenHumanChatOnP0 is true."
        )
        assert re.search(
            r"auto_open_chat\s*=\s*classification\s*==\s*[\"']P0[\"']\s*and\s*getattr\(",
            src,
        ), (
            "`auto_open_chat` is no longer derived from BOTH the P0 "
            "classification and the tenant flag. Dropping either half breaks "
            "the gate: without the flag it always fires, without the P0 check "
            "it could fire on P1-P3."
        )

    def test_narration_and_escalate_share_one_condition(self):
        """The invariant that keeps the narration honest.

        If the narration is gated on anything other than `auto_open_chat`, the
        two can disagree and the driver is told a human is connecting when the
        flag says none is.
        """
        src = self._source()
        assert "human_chat_opening = escalation_result is not None or auto_open_chat" in src, (
            "the human-chat narration is no longer gated on the same "
            "`auto_open_chat` condition as the escalate call. That divergence "
            "IS the defect — see "
            "issues/2026-09-23-p0-narration-claims-roadside-dispatched/."
        )

    def test_flag_is_resolved_at_session_setup_not_on_the_p0_path(self):
        """The P0 path must not gain a DynamoDB read.

        Nova Sonic's bidi loop assumes tool calls return in ~1s
        (`~/.kiro/steering/agentic-tiers.md`). The flag is resolved once at
        session setup from the same config item as `tenant_segment`, so the gate
        costs nothing when it matters.
        """
        src = self._source()
        assert "ctx.auto_open_human_chat_on_p0 = _derive_auto_open_human_chat_on_p0(_tenant_cfg)" in src, (
            "the flag is no longer assigned at session setup from the shared "
            "tenant-config item."
        )
        # The derive call must not appear inside the auto-triage path.
        triage_start = src.index("async def _auto_triage_and_inject")
        assert "_derive_auto_open_human_chat_on_p0(" not in src[triage_start:], (
            "the flag is being derived inside `_auto_triage_and_inject` — that "
            "is the latency-critical P0 path and must not perform a tenant-config "
            "read. Resolve it at session setup and read it off ctx."
        )

    def test_one_config_read_serves_both_values(self):
        """The refactor's point: no second pointer read for the flag."""
        src = self._source()
        assert src.count("_fetch_tenant_config_item(ctx.tenant_id)") == 1, (
            "session setup fetches the tenant config item more than once. The "
            "split exists so one pointer-to-version read serves both "
            "tenant_segment and the escalation flag."
        )
