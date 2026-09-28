"""Addendum D5 dual escalation — behaviour, not flags.

Review Cycle 1 Critical 2 was that escalation existed in `dms` alone while five
executors returned `escalate=True` and nothing fired, and Cycle 1's test-coverage
finding was that every non-dms escalation assertion read
`assert result.escalate is True` — a property of a dataclass, not evidence that
anything escalated. These tests assert the calls.

They also cover two invariants the review did not raise, both of which are
properties of this module's design rather than of any executor:

* **No recursion through `ios-owner`.** The owner-visible half dispatches via
  `IosOwnerExecutor`; escalating an `ios-owner` failure the same way would
  escalate through the thing that just failed, and since the failure would
  recur, unboundedly.
* **Half-independence.** The operator half is the only half a human is alarmed
  on, so it must fire even when the owner half is impossible.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agents.executors import base, escalation
from agents.executors.base import ExecuteResult

ACTION: dict[str, Any] = {
    "action_id": "AVX-ACT-esc-1",
    "owner_id": "owner-42",
    "action_kind": "book_combined_visit",
    "target_system": "dms",
}
CLAIMS: dict[str, Any] = {"sub": "owner-42", "token": "jwt-value"}


def _failed() -> ExecuteResult:
    return ExecuteResult.exhausted(
        retry_count=3, last_error_class="HTTPError", last_error_status=503
    )


@pytest.fixture(autouse=True)
def _no_ambient_clients() -> Any:
    """Isolate both kinds of process-global state this module touches.

    `escalation` caches boto3 clients and `base` caches SSM parameters for the
    process lifetime. Resetting only the clients leaves a policy cached by an
    earlier test, so a test that stubs SSM never has its stub read — it passed in
    isolation and failed in the suite, which is the tell.
    """
    escalation.reset_clients()
    base.reset_policy_cache()
    yield
    escalation.reset_clients()
    base.reset_policy_cache()


def _fire(**over: Any) -> tuple[dict[str, bool], MagicMock, MagicMock, MagicMock]:
    enqueue, events, cw = MagicMock(), MagicMock(), MagicMock()
    kwargs: dict[str, Any] = dict(
        action=ACTION,
        claims=CLAIMS,
        target_system="dms",
        result=_failed(),
        enqueue_owner_card=enqueue,
        events_client=events,
        cloudwatch_client=cw,
        ssm_client=MagicMock(get_parameter=MagicMock(side_effect=Exception("no ssm"))),
    )
    kwargs.update(over)
    outcome = escalation.fire(**kwargs)
    return outcome, enqueue, events, cw


class TestBothHalvesFire:
    def test_owner_and_operator_halves_both_fire(self) -> None:
        outcome, enqueue, events, cw = _fire()
        assert outcome == {"owner_visible": True, "operator_visible": True}
        enqueue.assert_called_once()
        events.put_events.assert_called_once()
        cw.put_metric_data.assert_called_once()

    def test_event_detail_matches_the_spec_shape(self) -> None:
        _, _, events, _ = _fire()
        entry = events.put_events.call_args.kwargs["Entries"][0]
        assert entry["Source"] == "vsa.avx.executor"
        assert entry["DetailType"] == "TerminalFailure"
        detail = json.loads(entry["Detail"])
        assert detail == {
            "action_id": "AVX-ACT-esc-1",
            "target_system": "dms",
            "action_kind": "book_combined_visit",
            "retry_count": 3,
            "last_error_class": "HTTPError",
            "last_error_status": 503,
        }

    def test_metric_is_dimensioned_by_target_system(self) -> None:
        """The dimension is only meaningful if more than one target emits it."""
        for target in ("dms", "vehicle-command", "retail", "chat"):
            escalation.reset_clients()
            _, _, _, cw = _fire(target_system=target)
            md = cw.put_metric_data.call_args.kwargs["MetricData"][0]
            assert md["MetricName"] == "AVXActionTerminalFailure"
            assert md["Dimensions"] == [{"Name": "target_system", "Value": target}]
            assert md["Value"] == 1

    def test_owner_card_carries_a_non_empty_owner_id(self) -> None:
        """Critical 3: the card was previously addressed to nobody."""
        _, enqueue, _, _ = _fire()
        finding = enqueue.call_args.args[0]
        assert finding["owner_id"] == "owner-42"
        assert finding["scope"]["owner_id"] == "owner-42"
        assert finding["severity"] == "informational"
        assert finding["category"] == "service"
        assert "couldn't reach" in finding["headline"]


class TestNonEscalatingResults:
    def test_declined_fires_nothing(self) -> None:
        """Addendum D5: `declined` does not escalate."""
        outcome, enqueue, events, cw = _fire(result=ExecuteResult.refused(last_error_status=400))
        assert outcome == {"owner_visible": False, "operator_visible": False}
        enqueue.assert_not_called()
        events.put_events.assert_not_called()
        cw.put_metric_data.assert_not_called()

    def test_success_fires_nothing(self) -> None:
        outcome, enqueue, events, cw = _fire(result=ExecuteResult.success(target_ref="RO-1"))
        assert outcome == {"owner_visible": False, "operator_visible": False}
        enqueue.assert_not_called()
        events.put_events.assert_not_called()


class TestNoRecursionThroughOwnerFeed:
    """An ios-owner failure must not escalate through ios-owner."""

    def test_ios_owner_target_skips_the_owner_half(self) -> None:
        outcome, enqueue, events, cw = _fire(target_system="ios-owner")
        assert outcome["owner_visible"] is False, (
            "the owner-visible half fired for a failing ios-owner dispatch — it "
            "escalates through the very executor that just failed, and because "
            "that failure recurs, it does so unboundedly."
        )
        enqueue.assert_not_called()

    def test_ios_owner_target_still_fires_the_operator_half(self) -> None:
        """Skipping the owner half must not silence the operator signal.

        If we cannot write to the owner's feed, the operator is the only party who
        can learn about it.
        """
        outcome, _, events, cw = _fire(target_system="ios-owner")
        assert outcome["operator_visible"] is True
        events.put_events.assert_called_once()
        cw.put_metric_data.assert_called_once()
        md = cw.put_metric_data.call_args.kwargs["MetricData"][0]
        assert md["Dimensions"] == [{"Name": "target_system", "Value": "ios-owner"}]


class TestHalvesAreIndependent:
    @pytest.mark.parametrize("bad_owner", [{"owner_id": ""}, {"owner_id": None}, {}])
    def test_missing_owner_id_skips_owner_half_but_keeps_operator_half(
        self, bad_owner: dict
    ) -> None:
        action = {k: v for k, v in ACTION.items() if k != "owner_id"}
        action.update(bad_owner)
        outcome, enqueue, events, _ = _fire(action=action)
        assert outcome["owner_visible"] is False
        enqueue.assert_not_called()
        assert outcome["operator_visible"] is True, (
            "an unaddressable owner card suppressed the operator signal too"
        )
        events.put_events.assert_called_once()

    def test_owner_half_exception_does_not_suppress_operator_half(self) -> None:
        boom = MagicMock(side_effect=RuntimeError("owner feed down"))
        outcome, _, events, cw = _fire(enqueue_owner_card=boom)
        assert outcome["owner_visible"] is False
        assert outcome["operator_visible"] is True
        events.put_events.assert_called_once()
        cw.put_metric_data.assert_called_once()

    def test_operator_half_exception_is_swallowed_not_raised(self) -> None:
        """Escalation runs on an already-failing path and must never raise.

        An exception escaping here would be caught by the caller's
        @never_raises and would overwrite a precise
        `executor_exhausted_retries` (carrying the retry count the audit trail
        and the escalation event both need) with a vague
        `executor_terminal_error`.
        """
        events = MagicMock()
        events.put_events.side_effect = RuntimeError("eventbridge down")
        outcome, enqueue, _, _ = _fire(events_client=events)
        assert outcome["operator_visible"] is False
        assert outcome["owner_visible"] is True
        enqueue.assert_called_once()

    def test_fire_never_raises_even_with_everything_broken(self) -> None:
        events = MagicMock(put_events=MagicMock(side_effect=RuntimeError("x")))
        cw = MagicMock(put_metric_data=MagicMock(side_effect=RuntimeError("y")))
        enqueue = MagicMock(side_effect=RuntimeError("z"))
        outcome = escalation.fire(
            action=ACTION,
            claims=CLAIMS,
            target_system="dms",
            result=_failed(),
            enqueue_owner_card=enqueue,
            events_client=events,
            cloudwatch_client=cw,
            ssm_client=MagicMock(get_parameter=MagicMock(side_effect=Exception())),
        )
        assert outcome == {"owner_visible": False, "operator_visible": False}


class TestEscalationDoesNotLogSecrets:
    def test_no_jwt_in_log_records(self, caplog: pytest.LogCaptureFixture) -> None:
        """claims carry a bearer token; failure logging must not echo it."""
        jwt = "eyJhbGciOiJSUzI1NiJ9.ESCALATION_CANARY.sig"
        events = MagicMock(put_events=MagicMock(side_effect=RuntimeError("boom")))
        with caplog.at_level(logging.DEBUG):
            escalation.fire(
                action=ACTION,
                claims={"sub": "owner-42", "token": jwt},
                target_system="dms",
                result=_failed(),
                enqueue_owner_card=MagicMock(side_effect=RuntimeError(f"failed with {jwt}")),
                events_client=events,
                cloudwatch_client=MagicMock(),
                ssm_client=MagicMock(get_parameter=MagicMock(side_effect=Exception())),
            )
        blob = "\n".join(
            [r.getMessage() for r in caplog.records]
            + [str(r.args) for r in caplog.records]
            + [r.exc_text or "" for r in caplog.records]
        )
        assert jwt not in blob, f"JWT leaked via escalation logging:\n{blob[:500]}"



# ---------------------------------------------------------------------------
# Security review Cycle 1, Warning 4 — operator-alarm routing is not remotely
# reconfigurable.
#
# `load_escalation_config` merges an SSM `operator_visible` dict over the
# defaults. Before this guard, an actor holding `ssm:PutParameter` on the
# escalation parameter could rename `eventbridge_source` /
# `eventbridge_detail_type` / `cloudwatch_metric` / `cloudwatch_dimension` and
# silence the only operator alarm Addendum D5 provides — no code change, no
# deploy, nothing in a diff for anyone to notice. Not exploitable without an IAM
# compromise, but the operator alarm is by construction D5's last line of
# defence, and the spec identifies no benefit to making the address itself
# tunable. The owner-facing category/severity stay tunable.
# ---------------------------------------------------------------------------


class TestOperatorRoutingIsPinned:
    @staticmethod
    def _fire_with_ssm_override(override: dict) -> MagicMock:
        events, cw = MagicMock(), MagicMock()
        ssm = MagicMock()
        ssm.get_parameter.return_value = {
            "Parameter": {"Value": json.dumps({"operator_visible": override})}
        }
        escalation.reset_clients()
        base.reset_policy_cache()
        escalation.fire(
            action=ACTION,
            claims=CLAIMS,
            target_system="dms",
            result=_failed(),
            enqueue_owner_card=MagicMock(),
            events_client=events,
            cloudwatch_client=cw,
            ssm_client=ssm,
        )
        return events, cw

    def test_ssm_cannot_redirect_the_eventbridge_source(self) -> None:
        events, _ = self._fire_with_ssm_override({"eventbridge_source": "attacker.sink"})
        entry = events.put_events.call_args.kwargs["Entries"][0]
        assert entry["Source"] == "vsa.avx.executor", (
            "an SSM parameter redirected the operator escalation event to "
            f"{entry['Source']!r}; the alarm address must not be remotely "
            "reconfigurable"
        )

    def test_ssm_cannot_rename_the_detail_type(self) -> None:
        events, _ = self._fire_with_ssm_override({"eventbridge_detail_type": "Ignored"})
        entry = events.put_events.call_args.kwargs["Entries"][0]
        assert entry["DetailType"] == "TerminalFailure"

    def test_ssm_cannot_rename_the_cloudwatch_metric(self) -> None:
        """Renaming the metric is the cheapest way to silence the alarm."""
        _, cw = self._fire_with_ssm_override({"cloudwatch_metric": "Ignored"})
        md = cw.put_metric_data.call_args.kwargs["MetricData"][0]
        assert md["MetricName"] == "AVXActionTerminalFailure", (
            f"an SSM parameter renamed the alarmed metric to {md['MetricName']!r}, "
            "which silences the operator alarm without any code change"
        )

    def test_ssm_cannot_rename_the_dimension(self) -> None:
        _, cw = self._fire_with_ssm_override({"cloudwatch_dimension": "ignored"})
        md = cw.put_metric_data.call_args.kwargs["MetricData"][0]
        assert md["Dimensions"][0]["Name"] == "target_system"

    def test_owner_facing_fields_remain_tunable(self) -> None:
        """The guard must pin the alarm address only, not freeze the whole config.

        Without this, pinning could be over-applied and the owner-facing
        category/severity — which the spec explicitly wants an admin
        configurator to drive — would silently stop being configurable.
        """
        enqueue = MagicMock()
        ssm = MagicMock()
        ssm.get_parameter.return_value = {
            "Parameter": {
                "Value": json.dumps(
                    {"owner_visible": {"category": "recall", "severity": "attention"}}
                )
            }
        }
        escalation.reset_clients()
        base.reset_policy_cache()
        escalation.fire(
            action=ACTION,
            claims=CLAIMS,
            target_system="dms",
            result=_failed(),
            enqueue_owner_card=enqueue,
            events_client=MagicMock(),
            cloudwatch_client=MagicMock(),
            ssm_client=ssm,
        )
        finding = enqueue.call_args.args[0]
        assert finding["category"] == "recall"
        assert finding["severity"] == "attention"



def test_the_no_real_aws_guard_is_active() -> None:
    """Assert the conftest autouse guard is in force.

    Without this, the guard could be removed, renamed, or stop applying (e.g. the
    factory is renamed and `raising=False` silently makes the patch a no-op) and
    nothing would fail — tests would quietly resume constructing real clients and
    emitting real EventBridge events and CloudWatch datapoints into whatever
    account the shell points at. A guard whose own absence is undetectable is not
    a guard.
    """
    import inspect

    for factory_name in ("_get_events", "_get_cloudwatch"):
        factory = getattr(escalation, factory_name)
        src = ""
        try:
            src = inspect.getsource(factory)
        except (OSError, TypeError):
            pass  # a lambda/mock with no retrievable source is the patched case
        assert "boto3" not in src, (
            f"escalation.{factory_name} is the REAL factory during a test run — "
            "the autouse guard in conftest.py is not applying, so an un-stubbed "
            "escalation path can construct a live AWS client"
        )

    # And the fallback genuinely yields a stub rather than a live client.
    assert isinstance(escalation._get_events(), MagicMock)
    assert isinstance(escalation._get_cloudwatch(), MagicMock)


def test_fire_without_injected_clients_touches_no_real_aws() -> None:
    """The un-stubbed path a forgetful test would take must stay inert.

    `escalation.fire` swallows all exceptions by design, so a missing stub is
    silent — which is exactly why this needs asserting rather than assuming.
    """
    outcome = escalation.fire(
        action=ACTION,
        claims=CLAIMS,
        target_system="dms",
        result=_failed(),
        enqueue_owner_card=MagicMock(),
        # deliberately NO events_client / cloudwatch_client
    )
    # The guard's mocks accept the calls, so both halves report success without
    # any real traffic. The point is that it completes against stubs.
    assert outcome["operator_visible"] is True



class TestOwnerVisibleOutcomeIsHonest:
    """`owner_visible` must reflect whether the card was WRITTEN.

    `IosOwnerExecutor.dispatch` is @never_raises-wrapped, so a failed DynamoDB
    write returns a `failed` ExecuteResult and raises nothing. An earlier version
    set `owner_visible=True` on any return at all, and therefore reported a
    delivered owner card for a write that never landed — the same
    reports-success-for-work-that-did-not-happen shape this module exists to
    prevent, sited in the module itself.

    Found while verifying the conftest AWS guard, not by review: the ad-hoc probe
    showed `owner_visible: True` next to a log line saying the put_item had
    failed.
    """

    @staticmethod
    def _fire_with_enqueue_result(enqueued: Any) -> dict[str, bool]:
        return escalation.fire(
            action=ACTION,
            claims=CLAIMS,
            target_system="dms",
            result=_failed(),
            enqueue_owner_card=MagicMock(return_value=enqueued),
            events_client=MagicMock(),
            cloudwatch_client=MagicMock(),
            ssm_client=MagicMock(get_parameter=MagicMock(side_effect=Exception())),
        )

    def test_failed_card_write_reports_owner_visible_false(self) -> None:
        failed_write = ExecuteResult.terminal_error(last_error_class="ClientError")
        outcome = self._fire_with_enqueue_result(failed_write)
        assert outcome["owner_visible"] is False, (
            "the owner card write FAILED but escalation reported it delivered; the "
            "owner was never told the dispatch failed and the system recorded that "
            "they were"
        )
        assert outcome["operator_visible"] is True, (
            "a failed owner card must not suppress the operator signal"
        )

    def test_declined_card_write_also_reports_false(self) -> None:
        outcome = self._fire_with_enqueue_result(
            ExecuteResult.refused(last_error_status=400)
        )
        assert outcome["owner_visible"] is False

    def test_successful_card_write_reports_true(self) -> None:
        outcome = self._fire_with_enqueue_result(
            ExecuteResult.success(target_ref="AVX-ACT-x#escalation")
        )
        assert outcome["owner_visible"] is True

    def test_collaborator_returning_none_is_treated_as_success(self) -> None:
        """A plain callable or test stub has no verdict to offer."""
        assert self._fire_with_enqueue_result(None)["owner_visible"] is True



class TestDefaultEnqueueOwnerCardWritesTheRightRow:
    """Cover `_default_enqueue_owner_card` — the real owner-card path.

    Review Cycle 3 Warning 10: this function had ZERO test references, and every
    escalation test injected `enqueue_owner_card`, so the only code that actually
    writes the owner card was never executed. That gap is not incidental — it is
    the proximate cause of two separate defects:

    * Cycle 1 C3: the card was built with no `owner_id`, written addressed to
      nobody, and reported as success.
    * Cycle 3 W2: `ios_owner` overwrote the card's `source_action_id` with its own
      derived key, so the stored row pointed at itself and the link back to the
      failing Action was lost.

    Both were invisible because the assertions stopped at *the dict handed to a
    mock*. These tests assert on the DynamoDB item that is actually written, which
    is the only thing a reader of that table will ever see.
    """

    @staticmethod
    def _capture_written_item(action: dict) -> dict:
        """Drive the real default path, stubbing only the DDB resource."""
        table = MagicMock()
        ddb = MagicMock()
        ddb.Table.return_value = table

        from agents.executors import ios_owner as ios_owner_mod

        with patch.object(ios_owner_mod, "_make_ddb_resource", lambda: ddb):
            escalation.fire(
                action=action,
                claims=CLAIMS,
                target_system="dms",
                result=_failed(),
                # NO enqueue_owner_card — exercise the real default.
                events_client=MagicMock(),
                cloudwatch_client=MagicMock(),
                ssm_client=MagicMock(get_parameter=MagicMock(side_effect=Exception())),
            )

        assert table.put_item.called, (
            "the default enqueue path wrote nothing — escalation reported an owner "
            "card that does not exist"
        )
        return table.put_item.call_args.kwargs["Item"]

    def test_written_row_does_not_collide_with_the_failing_action(self) -> None:
        """Cycle 2 C1, asserted on the STORED key rather than the built dict.

        `avx-actions` is keyed on `action_id` alone, so a colliding key makes this
        write a full replace of the Action — destroying its `audit_trail`, i.e.
        the D5 failure record, as a side effect of reporting the failure.
        """
        item = self._capture_written_item(
            {"action_id": "ACT-123", "owner_id": "owner-9", "action_kind": "book"}
        )
        assert item["action_id"] != "ACT-123", (
            f"the stored escalation row uses action_id={item['action_id']!r}, the "
            "same key as the Action it reports on — this put_item REPLACES that "
            "Action and erases its audit_trail"
        )
        assert item["action_id"] == "ACT-123#escalation"

    def test_written_row_links_back_to_the_failing_action(self) -> None:
        """Cycle 3 W2: the stored `source_action_id` must not point at itself."""
        item = self._capture_written_item(
            {"action_id": "ACT-123", "owner_id": "owner-9", "action_kind": "book"}
        )
        assert item.get("source_action_id") == "ACT-123", (
            f"stored source_action_id={item.get('source_action_id')!r} — it must "
            "reference the failing Action, not the escalation row itself, or the "
            "link escalation.py promises to preserve is lost in the write"
        )

    def test_written_row_is_addressed_to_the_owner(self) -> None:
        """Cycle 1 C3, asserted on the stored row."""
        item = self._capture_written_item(
            {"action_id": "ACT-123", "owner_id": "owner-9", "action_kind": "book"}
        )
        assert item.get("owner_id") == "owner-9", (
            f"stored owner_id={item.get('owner_id')!r}; an unaddressed row is "
            "invisible to every owner-scoped feed query"
        )

    def test_no_row_is_written_when_the_owner_is_unknown(self) -> None:
        """Fail closed rather than writing a card nobody can see."""
        table, ddb = MagicMock(), MagicMock()
        ddb.Table.return_value = table
        from agents.executors import ios_owner as ios_owner_mod

        with patch.object(ios_owner_mod, "_make_ddb_resource", lambda: ddb):
            outcome = escalation.fire(
                action={"action_id": "ACT-123", "action_kind": "book"},  # no owner_id
                claims=CLAIMS,
                target_system="dms",
                result=_failed(),
                events_client=MagicMock(),
                cloudwatch_client=MagicMock(),
                ssm_client=MagicMock(get_parameter=MagicMock(side_effect=Exception())),
            )

        table.put_item.assert_not_called()
        assert outcome["owner_visible"] is False
        assert outcome["operator_visible"] is True
