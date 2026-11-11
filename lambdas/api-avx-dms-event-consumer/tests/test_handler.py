"""
Unit tests for lambdas/api-avx-dms-event-consumer/handler.py (DMS Spec 2 T5.4).

Mutation ledger for T5.4 (per `.kiro/specs/2026-09-18-dms-avx-agentic-surface/decisions.md`
§ "2026-09-21 — T5.4 + T5.5 design"):

  M1 (idempotency): Remove the `ConditionExpression='... AND #s <> :new_status'`.
      Expected: `test_duplicate_event_produces_one_projection` FAILS because a
      second delivery of the same (action_id, to_status) will write a second
      UpdateItem instead of no-op'ing.

  MS1-MS7 (correlation-id shape, 2026-10-01): see `TestActionIdShape`. All
      run 2026-10-01 and CAUGHT: bare-id pass-through (the shipped bug), double
      prefix, no shape validation, `match`+`$` instead of `fullmatch`, the
      handler bypassing `_action_key`, no lower-casing, malformed id at INFO.

Run:
    python3 -m pytest lambdas/api-avx-dms-event-consumer/tests -v
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

# Allow direct import from lambda root
_LAMBDA_ROOT = os.path.join(os.path.dirname(__file__), "..")
_LAMBDAS_ROOT = os.path.dirname(_LAMBDA_ROOT)
for _p in (_LAMBDA_ROOT, _LAMBDAS_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Env var must be set BEFORE handler import (module-level guard reads it).
# Direct assignment (not setdefault) so a leaked env var from another test
# session doesn't silently override the fixture value.
os.environ["AVX_ACTIONS_TABLE"] = "vsa-test-avx-actions"

import handler as h  # noqa: E402

# ---------------------------------------------------------------------------
# Id shapes. DMS emits the BARE uuid (it stores and validates nothing else);
# the AVX Action row is keyed AVX-ACT-{uuid}. Fixtures use the real shapes:
# the previous "act-123" fixture matched neither, which is how the handler
# shipped looking up the bare id against a table that never holds one.
# ---------------------------------------------------------------------------

_DMS_ID = "4c822581-9598-4638-b25c-26fedf272a3c"          # as seen live 2026-09-24
_ACTION_KEY = f"AVX-ACT-{_DMS_ID}"
_TERMINAL_DMS_ID = "0000aaaa-1111-2222-3333-444455556666"
_TERMINAL_KEY = f"AVX-ACT-{_TERMINAL_DMS_ID}"
_NORMAL_DMS_ID = "9999bbbb-8888-7777-6666-555544443333"
_NORMAL_KEY = f"AVX-ACT-{_NORMAL_DMS_ID}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _eb_event(
    *,
    avx_action_id: str | None = _DMS_ID,
    to_status: str | None = "Draft",
    ro_id: str | None = "ro-456",
    dealer_id: str = "dealer-1",
    vin: str = "1FA00000000000001",
    from_status: str = "Scheduled",
    changed_at: str = "2026-09-21T10:00:00Z",
    customer_channel_pref: str = "sms",
    quiet_hours_applicable: bool = False,
) -> dict:
    """
    Build an EventBridge-shaped event matching the DMS
    `dms.service-lane` / `dms.ro.status_changed` contract. Only `avx_action_id`
    and `to_status` drive the handler; the rest of the fields match the DMS
    `RO_STATUS_CHANGED_FIELDS` allowlist so the fixture doubles as a
    contract-shape reference.
    """
    detail: dict = {
        "ro_id": ro_id,
        "dealer_id": dealer_id,
        "vin": vin,
        "from_status": from_status,
        "to_status": to_status,
        "changed_at": changed_at,
        "customer_channel_pref": customer_channel_pref,
        "quiet_hours_applicable": quiet_hours_applicable,
    }
    if avx_action_id is not None:
        detail["avx_action_id"] = avx_action_id
    return {
        "version": "0",
        "id": "test-event-id",
        "detail-type": "dms.ro.status_changed",
        "source": "dms.service-lane",
        # AWS docs placeholder account, deliberately NOT this deployment's real
        # account ID — this file ships to the public mirror, and a real 12-digit
        # ID here fails scripts/lib/test_publish_scan_exclude_invariant.py. The
        # handler never reads `account` (same-account topology in v1), so the
        # value is shape-only. Do not "correct" it to a real account.
        "account": "123456789012",
        "time": changed_at,
        "region": "us-west-2",
        "resources": [],
        "detail": detail,
    }


def _make_conditional_check_failed_error() -> ClientError:
    return ClientError(
        error_response={"Error": {"Code": "ConditionalCheckFailedException",
                                  "Message": "The conditional request failed"}},
        operation_name="UpdateItem",
    )


# ---------------------------------------------------------------------------
# Happy-path projections
# ---------------------------------------------------------------------------

class TestStatusProjections:

    def test_draft_projects_to_in_progress(self):
        mock_table = MagicMock()
        with patch.object(h, "_table", return_value=mock_table):
            resp = h.handler(_eb_event(to_status="Draft"), None)
        assert resp == {"status": "projected"}
        mock_table.update_item.assert_called_once()
        kwargs = mock_table.update_item.call_args.kwargs
        assert kwargs["Key"] == {"action_id": _ACTION_KEY}
        assert kwargs["ExpressionAttributeValues"][":new_status"] == "in_progress"

    def test_ready_projects_to_completed(self):
        mock_table = MagicMock()
        with patch.object(h, "_table", return_value=mock_table):
            resp = h.handler(_eb_event(to_status="Ready"), None)
        assert resp == {"status": "projected"}
        assert mock_table.update_item.call_args.kwargs[
            "ExpressionAttributeValues"
        ][":new_status"] == "completed"

    def test_delivered_projects_to_completed(self):
        mock_table = MagicMock()
        with patch.object(h, "_table", return_value=mock_table):
            resp = h.handler(_eb_event(to_status="Delivered"), None)
        assert resp == {"status": "projected"}
        assert mock_table.update_item.call_args.kwargs[
            "ExpressionAttributeValues"
        ][":new_status"] == "completed"


# ---------------------------------------------------------------------------
# No-op paths
# ---------------------------------------------------------------------------

class TestGracefulNoOps:

    def test_avx_action_id_absent_graceful_no_op(self):
        """Non-AVX-originated RO — event carries no avx_action_id. No DDB call."""
        mock_table = MagicMock()
        with patch.object(h, "_table", return_value=mock_table):
            resp = h.handler(_eb_event(avx_action_id=None), None)
        assert resp == {"status": "no_op_no_avx_action_id"}
        mock_table.update_item.assert_not_called()

    def test_unmapped_status_is_ignored(self):
        """DMS to_status not in the projection map — no DDB call."""
        mock_table = MagicMock()
        with patch.object(h, "_table", return_value=mock_table):
            resp = h.handler(_eb_event(to_status="Scheduled"), None)
        assert resp == {"status": "no_op_unmapped_status"}
        mock_table.update_item.assert_not_called()

    def test_intermediate_status_inprogress_is_ignored(self):
        """DMS 'InProgress' is an intermediate state, distinct from Action 'in_progress'.

        This is a lurking-collision test: the two vocabularies happen to have
        similar-looking values, but the projection map deliberately does NOT
        route DMS.InProgress → Action.in_progress (it would rename a
        state-machine element, not model a transition).
        """
        mock_table = MagicMock()
        with patch.object(h, "_table", return_value=mock_table):
            resp = h.handler(_eb_event(to_status="InProgress"), None)
        assert resp == {"status": "no_op_unmapped_status"}
        mock_table.update_item.assert_not_called()

    def test_cancelled_status_is_ignored(self):
        """DMS 'Cancelled' maps to no Action status.

        Rationale (see decisions.md § design → Status projection table): AVX
        Action's cancelled/dismissed terminals are owner-driven, not
        dealer-driven. A DMS Cancel is NOT the same as owner dismissal, and
        collapsing them would erase the distinction between "customer said no"
        (dismissed) and "dealer cancelled" (out of scope for this handler).
        """
        mock_table = MagicMock()
        with patch.object(h, "_table", return_value=mock_table):
            resp = h.handler(_eb_event(to_status="Cancelled"), None)
        assert resp == {"status": "no_op_unmapped_status"}
        mock_table.update_item.assert_not_called()


# ---------------------------------------------------------------------------
# Idempotency (T5.4 mutation target)
# ---------------------------------------------------------------------------

class TestIdempotency:

    def test_duplicate_event_produces_one_projection(self):
        """
        MUTATION TARGET for M1.

        Uses a STATEFUL DDB mock that enforces the ConditionExpression the
        same way real DynamoDB would: an in-memory row is updated only when
        `attribute_exists(action_id) AND #s <> :new_status` holds. Without
        this, the test is testing "the mock returned what I told it to
        return" — which passes even if the handler omits the
        ConditionExpression, and defeats the purpose of the mutation ledger.

        The property this test enforces: the SECOND UpdateItem call, if the
        handler still makes one, will be a no-op in real DDB because the
        row's status already matches the projected value. If the handler
        omits the ConditionExpression, real DDB would OVERWRITE the row
        (identical value, non-idempotent-in-audit-shape write) — the stateful
        mock reproduces this and the test fails because the second call
        returns "projected" instead of "no_op_conditional_check_failed".
        """
        # In-memory row store, keyed by action_id
        row_store: dict[str, dict] = {_ACTION_KEY: {"status": "pending_execution"}}

        def stateful_update_item(**kwargs):
            action_id = kwargs["Key"]["action_id"]
            new_status = kwargs["ExpressionAttributeValues"][":new_status"]
            condition = kwargs.get("ConditionExpression", "")

            # Simulate DDB's evaluation of the ConditionExpression clauses.
            # If the handler omits the ConditionExpression entirely, we skip
            # this evaluation and let the write proceed — which is the exact
            # non-idempotent behavior the mutation introduces.
            if condition:
                if "attribute_exists(action_id)" in condition:
                    if action_id not in row_store:
                        raise _make_conditional_check_failed_error()
                if "#s <> :new_status" in condition:
                    current = row_store.get(action_id, {}).get("status")
                    if current == new_status:
                        raise _make_conditional_check_failed_error()
                # Five terminal-state exclusions — refuse to overwrite any
                # terminal state per AVX core's _TERMINAL_STATUSES contract.
                # If any of these clauses is present in the ConditionExpression
                # AND the current status matches its right-hand side, raise
                # CCF. This is what makes terminal-state-overwrite tests catch
                # the M5 mutation (removing one of the exclusions).
                for terminal in ("completed", "cancelled", "declined",
                                 "dismissed", "failed"):
                    clause = f"#s <> :{terminal}"
                    if clause in condition:
                        current = row_store.get(action_id, {}).get("status")
                        if current == terminal:
                            raise _make_conditional_check_failed_error()

            # Write proceeds.
            row_store.setdefault(action_id, {})["status"] = new_status
            return None

        mock_table = MagicMock()
        mock_table.update_item.side_effect = stateful_update_item

        with patch.object(h, "_table", return_value=mock_table):
            resp1 = h.handler(_eb_event(to_status="Ready"), None)
            resp2 = h.handler(_eb_event(to_status="Ready"), None)

        assert resp1 == {"status": "projected"}
        # Property under test: SECOND delivery of the same (action_id,
        # to_status) is a no-op. If the handler removes the ConditionExpression,
        # the stateful mock lets the write through and this assertion fails.
        assert resp2 == {"status": "no_op_conditional_check_failed"}, (
            f"Expected second delivery to no-op, got {resp2}. "
            f"This usually means the handler omitted the ConditionExpression "
            f"(mutation M1)."
        )
        assert mock_table.update_item.call_count == 2

    def test_condition_expression_present(self):
        """
        Structural pin on the ConditionExpression — belt-and-braces for M1.

        This asserts the SHAPE of the DDB call, not just the outcome. If
        someone removes `#s <> :new_status` from the ConditionExpression
        while preserving handler-side idempotency logic (a common wrong-fix),
        the outcome-level test above might still pass. This test won't.
        """
        mock_table = MagicMock()
        with patch.object(h, "_table", return_value=mock_table):
            h.handler(_eb_event(to_status="Draft"), None)
        cond = mock_table.update_item.call_args.kwargs["ConditionExpression"]
        assert "attribute_exists(action_id)" in cond
        assert "#s <> :new_status" in cond

    def test_condition_expression_forbids_all_terminal_states(self):
        """
        Structural pin on the terminal-state exclusions (review Cycle 1
        Warning fix). If a future contributor removes one of the five
        exclusions, this test fails immediately — this is the shape half
        of the mutation-M5 guard.
        """
        mock_table = MagicMock()
        with patch.object(h, "_table", return_value=mock_table):
            h.handler(_eb_event(to_status="Draft"), None)
        cond = mock_table.update_item.call_args.kwargs["ConditionExpression"]
        for terminal in ("completed", "cancelled", "declined",
                         "dismissed", "failed"):
            assert f"#s <> :{terminal}" in cond, (
                f"terminal-state exclusion for {terminal!r} missing from "
                f"ConditionExpression — Actions in {terminal!r} state could "
                f"be silently overwritten by this consumer, erasing owner or "
                f"executor intent (see AVX core _TERMINAL_STATUSES in "
                f"lambdas/api-avx-actions/handler.py:106-113)."
            )
        # Also assert the values are wired.
        eav = mock_table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        for terminal in ("completed", "cancelled", "declined",
                         "dismissed", "failed"):
            assert eav.get(f":{terminal}") == terminal


# ---------------------------------------------------------------------------
# Terminal-state protection (review Cycle 1 Warning fix; T5.4 M5 target)
# ---------------------------------------------------------------------------

class TestTerminalStateProtection:
    """
    The consumer MUST refuse to overwrite any AVX-Action terminal state.
    Terminal states are `completed`, `cancelled`, `declined`, `dismissed`,
    `failed` — matches AVX core's `_TERMINAL_STATUSES` frozenset.

    Concrete scenario (from review Cycle 1): owner dismisses an Action
    (`status=dismissed`) between DMS RO Draft-fired and DMS RO Ready-fired
    events; the Ready event would otherwise overwrite `dismissed` with
    `completed`, silently erasing owner intent — the D4 semantic
    distinction the schema draws deliberately.

    Each of the five cases below is independent: any one exclusion being
    accidentally removed produces a class of state-machine erasure. The
    stateful-mock behavioural checks and the shape-pin above are both
    catchers; the stateful checks are what verifies real DDB semantics
    reproduce the guard correctly.
    """

    @pytest.mark.parametrize("current_terminal", [
        "dismissed",   # owner tapped Dismiss
        "cancelled",   # owner or system cancelled
        "declined",    # target refused (executor terminal 4xx)
        "failed",      # executor exhausted retries (executor terminal)
        "completed",   # already completed; no back-transitions
    ])
    def test_terminal_state_not_overwritten_by_in_progress(self, current_terminal):
        row_store: dict[str, dict] = {_TERMINAL_KEY: {"status": current_terminal}}

        def stateful_update_item(**kwargs):
            new_status = kwargs["ExpressionAttributeValues"][":new_status"]
            condition = kwargs.get("ConditionExpression", "")
            if condition:
                for term in ("completed", "cancelled", "declined",
                             "dismissed", "failed"):
                    if f"#s <> :{term}" in condition:
                        current = row_store[kwargs["Key"]["action_id"]]["status"]
                        if current == term:
                            raise _make_conditional_check_failed_error()
                if "#s <> :new_status" in condition:
                    current = row_store[kwargs["Key"]["action_id"]]["status"]
                    if current == new_status:
                        raise _make_conditional_check_failed_error()
            row_store[kwargs["Key"]["action_id"]]["status"] = new_status

        mock_table = MagicMock()
        mock_table.update_item.side_effect = stateful_update_item
        with patch.object(h, "_table", return_value=mock_table):
            resp = h.handler(
                _eb_event(avx_action_id=_TERMINAL_DMS_ID, to_status="Draft"),
                None,
            )
        # Handler correctly identifies as ConditionalCheckFailed → no_op.
        assert resp == {"status": "no_op_conditional_check_failed"}, (
            f"expected no-op when overwriting terminal state {current_terminal!r} "
            f"with in_progress; handler produced {resp}"
        )
        # And the stateful row store still shows the terminal state — proving
        # the property (not just the return code).
        assert row_store[_TERMINAL_KEY]["status"] == current_terminal, (
            f"terminal state {current_terminal!r} was overwritten by "
            f"{row_store[_TERMINAL_KEY]['status']!r} — CCF should have "
            f"prevented the write; if the mutation removed a terminal "
            f"exclusion, this assertion fails."
        )

    @pytest.mark.parametrize("current_terminal", [
        "dismissed", "cancelled", "declined", "failed",
    ])
    def test_terminal_state_not_overwritten_by_completed(self, current_terminal):
        """
        Same protection, `to_status=Ready` (→ completed). Cases distinct
        from the previous test because different `to_status` values traverse
        different code paths; if a future refactor branches per new_status,
        both paths must respect the terminal-state exclusion.
        """
        row_store: dict[str, dict] = {_TERMINAL_KEY: {"status": current_terminal}}

        def stateful_update_item(**kwargs):
            new_status = kwargs["ExpressionAttributeValues"][":new_status"]
            condition = kwargs.get("ConditionExpression", "")
            if condition:
                for term in ("completed", "cancelled", "declined",
                             "dismissed", "failed"):
                    if f"#s <> :{term}" in condition:
                        current = row_store[kwargs["Key"]["action_id"]]["status"]
                        if current == term:
                            raise _make_conditional_check_failed_error()
                if "#s <> :new_status" in condition:
                    current = row_store[kwargs["Key"]["action_id"]]["status"]
                    if current == new_status:
                        raise _make_conditional_check_failed_error()
            row_store[kwargs["Key"]["action_id"]]["status"] = new_status

        mock_table = MagicMock()
        mock_table.update_item.side_effect = stateful_update_item
        with patch.object(h, "_table", return_value=mock_table):
            resp = h.handler(
                _eb_event(avx_action_id=_TERMINAL_DMS_ID, to_status="Ready"),
                None,
            )
        assert resp == {"status": "no_op_conditional_check_failed"}
        assert row_store[_TERMINAL_KEY]["status"] == current_terminal

    def test_normal_progression_still_works(self):
        """
        Sanity: the normal in_progress → completed transition still proceeds.
        This is the property terminal-state protection MUST NOT break.
        """
        row_store: dict[str, dict] = {_NORMAL_KEY: {"status": "in_progress"}}

        def stateful_update_item(**kwargs):
            new_status = kwargs["ExpressionAttributeValues"][":new_status"]
            condition = kwargs.get("ConditionExpression", "")
            if condition:
                for term in ("completed", "cancelled", "declined",
                             "dismissed", "failed"):
                    if f"#s <> :{term}" in condition:
                        current = row_store[kwargs["Key"]["action_id"]]["status"]
                        if current == term:
                            raise _make_conditional_check_failed_error()
                if "#s <> :new_status" in condition:
                    current = row_store[kwargs["Key"]["action_id"]]["status"]
                    if current == new_status:
                        raise _make_conditional_check_failed_error()
            row_store[kwargs["Key"]["action_id"]]["status"] = new_status

        mock_table = MagicMock()
        mock_table.update_item.side_effect = stateful_update_item
        with patch.object(h, "_table", return_value=mock_table):
            resp = h.handler(
                _eb_event(avx_action_id=_NORMAL_DMS_ID, to_status="Ready"),
                None,
            )
        assert resp == {"status": "projected"}
        assert row_store[_NORMAL_KEY]["status"] == "completed"


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------

class TestFailureModes:

    def test_action_row_missing_is_graceful_no_op(self):
        """
        ConditionalCheckFailedException for attribute_exists(action_id) — the
        Action row does not exist yet. Handler logs INFO and returns no-op.
        """
        mock_table = MagicMock()
        mock_table.update_item.side_effect = _make_conditional_check_failed_error()
        with patch.object(h, "_table", return_value=mock_table):
            resp = h.handler(_eb_event(to_status="Draft"), None)
        assert resp == {"status": "no_op_conditional_check_failed"}

    def test_other_ddb_error_raises(self):
        """
        A non-idempotency DDB error (e.g., ProvisionedThroughputExceededException)
        raises so EventBridge retries.
        """
        mock_table = MagicMock()
        mock_table.update_item.side_effect = ClientError(
            error_response={"Error": {"Code": "ProvisionedThroughputExceededException",
                                      "Message": "throttled"}},
            operation_name="UpdateItem",
        )
        with patch.object(h, "_table", return_value=mock_table):
            with pytest.raises(ClientError):
                h.handler(_eb_event(to_status="Draft"), None)

    def test_missing_table_env_raises_runtime_error(self):
        """
        A misconfigured Lambda (no AVX_ACTIONS_TABLE env var) refuses to run
        rather than guess a stage — the same defensive pattern as the DMS
        publisher's EVENT_BUS_NAME check.
        """
        # Temporarily null out the module-level constant.
        original = h._ACTIONS_TABLE_NAME
        h._ACTIONS_TABLE_NAME = None
        try:
            with pytest.raises(RuntimeError, match="AVX_ACTIONS_TABLE"):
                h.handler(_eb_event(to_status="Draft"), None)
        finally:
            h._ACTIONS_TABLE_NAME = original


# ---------------------------------------------------------------------------
# Contract sanity — projection map is exhaustive for the DMS→AVX transitions
# ---------------------------------------------------------------------------

class TestProjectionMapContract:

    def test_projection_map_covers_expected_transitions(self):
        """Sanity: the map contains exactly the four transitions the spec names.

        Stage C (D12) added Invoiced → completed.
        """
        assert set(h._STATUS_PROJECTION.keys()) == {"Draft", "Ready", "Delivered", "Invoiced"}
        assert h._STATUS_PROJECTION == {
            "Draft":     "in_progress",
            "Ready":     "completed",
            "Delivered": "completed",
            "Invoiced":  "completed",
        }

    def test_projected_values_are_valid_action_statuses(self):
        """The right-hand side of the projection is a subset of AVX Action's terminal-ish states.

        This is a soft coupling — the Action schema is in AVX core's
        `action.schema.json` and enumerates `pending_execution`, `in_progress`,
        `completed`, `dismissed`, `failed`, `declined`. This test pins that we
        only project into two of those (`in_progress`, `completed`), so if a
        future contributor adds `dismissed` here it fails and forces a
        deliberate design decision rather than a silent state-machine change.
        """
        allowed_projected = {"in_progress", "completed"}
        assert set(h._STATUS_PROJECTION.values()) <= allowed_projected



# ---------------------------------------------------------------------------
# Correlation-id shape (issues/2026-10-01-dms-event-consumer-bare-uuid-key)
#
# Live 2026-09-24: DMS emitted avx_action_id=ef2407cf-…; the Action row is
# AVX-ACT-ef2407cf-…; the handler looked up the bare id and logged
# no_op_conditional_check_failed. Every Action booked through DMS stayed
# in_progress. Mutation ledger for this block:
#
#   MS1 pass the bare id through (the shipped bug)      -> reprefix + moto + round-trip FAIL
#   MS2 always prepend, even to a prefixed id            -> already-prefixed + double-prefix FAIL
#   MS3 drop shape validation (prefix anything)          -> malformed cases FAIL
#   MS4 regex .match with a trailing $ instead of fullmatch -> trailing-newline case FAILS
#   MS5 handler bypasses _action_key, uses the raw id   -> reprefix + moto FAIL
#   MS6 no lower-casing (review 2026-10-01)              -> normalisation + schema-pattern FAIL
#   MS7 malformed id logged at INFO again                -> warning test FAILS
# ---------------------------------------------------------------------------

def _load_by_path(name: str, rel: str):
    import importlib.util

    from pathlib import Path

    repo = str(Path(__file__).resolve().parents[3])
    for p in (repo, os.path.join(repo, "lambdas")):
        if p not in sys.path:
            sys.path.insert(0, p)
    spec = importlib.util.spec_from_file_location(name, os.path.join(repo, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestActionIdShape:

    def test_dms_bare_uuid_is_looked_up_under_the_avx_action_key(self):
        mock_table = MagicMock()
        with patch.object(h, "_table", return_value=mock_table):
            resp = h.handler(_eb_event(avx_action_id=_DMS_ID, to_status="Ready"), None)
        assert resp == {"status": "projected"}
        key = mock_table.update_item.call_args.kwargs["Key"]
        assert key == {"action_id": f"AVX-ACT-{_DMS_ID}"}, (
            f"looked up {key!r}; DMS emits the bare uuid, the Action row is "
            "AVX-ACT-{uuid}. A bare-id lookup is the 2026-09-24 live defect."
        )

    def test_already_prefixed_id_is_used_unchanged(self):
        assert h._action_key(_ACTION_KEY) == _ACTION_KEY

    def test_uppercase_hex_is_normalised_to_the_schema_form(self):
        # action.schema.json admits only lowercase hex in action_id; DMS's regex
        # admits A-F and dedupes case-insensitively. So an upper-case echo can only
        # name the lower-case row.
        upper = _DMS_ID.upper()
        assert h._action_key(upper) == f"AVX-ACT-{_DMS_ID}"
        assert h._action_key(f"AVX-ACT-{upper}") == f"AVX-ACT-{_DMS_ID}"

    def test_action_key_matches_the_schema_pattern(self):
        import json
        import re as _re
        from pathlib import Path

        schema = json.loads((Path(__file__).resolve().parents[3]
                             / "agents/tier2/schemas/action.schema.json").read_text())
        pattern = schema["properties"]["action_id"]["pattern"]
        for raw in (_DMS_ID, _DMS_ID.upper(), _ACTION_KEY):
            assert _re.fullmatch(pattern, h._action_key(raw)), raw

    def test_malformed_id_logs_a_warning(self, caplog):
        mock_table = MagicMock()
        with caplog.at_level("INFO"), patch.object(h, "_table", return_value=mock_table):
            h.handler(_eb_event(avx_action_id="act-123", to_status="Ready"), None)
        hits = [r for r in caplog.records if "no_op_malformed_avx_action_id" in r.getMessage()]
        assert hits and all(r.levelname == "WARNING" for r in hits)

    @pytest.mark.parametrize("to_status", ["Ready", "Bogus\nprojected action_id=forged"])
    def test_event_fields_cannot_inject_log_lines(self, caplog, to_status):
        # Security review 2026-10-01 Suggestion: ro_id / to_status are event-supplied.
        mock_table = MagicMock()
        with caplog.at_level("INFO"), patch.object(h, "_table", return_value=mock_table):
            h.handler(_eb_event(ro_id="ro-1\nprojected action_id=forged", to_status=to_status),
                      None)
        for record in caplog.records:
            assert "\n" not in record.getMessage(), record.getMessage()

    @pytest.mark.parametrize("bad", [
        "act-123",                              # the old fixture
        "ACT-" + _DMS_ID,                       # pre-schema prefix
        "AVX-ACT-",                             # prefix only
        "AVX-ACT-AVX-ACT-" + _DMS_ID,           # double prefix
        _DMS_ID + "\n",                         # $ matches before a trailing newline
        _DMS_ID + "x",
        " " + _DMS_ID,
        _DMS_ID.replace("-", ""),               # 32 hex, no dashes
        12345,
        ["list"],
    ])
    def test_malformed_id_is_a_no_op_with_no_ddb_call(self, bad):
        mock_table = MagicMock()
        with patch.object(h, "_table", return_value=mock_table):
            resp = h.handler(_eb_event(avx_action_id=bad, to_status="Ready"), None)
        assert resp == {"status": "no_op_malformed_avx_action_id"}, (bad, resp)
        mock_table.update_item.assert_not_called()

    def test_round_trip_with_approve_path_and_dms_executor(self):
        """The contract, end to end in code: approve-path id -> executor strip -> consumer.

        `_derive_action_id` writes the Action row's key; the DMS executor sends
        `_dms_correlation_id` of it to DMS; DMS echoes that value in its event.
        The consumer must map it back to the exact key the approve path wrote.
        """
        findings = _load_by_path("avx_findings_handler_for_shape_test",
                                 "lambdas/api-avx-findings/handler.py")
        from agents.executors.dms import _dms_correlation_id

        for finding_id, kind, owner in (
            ("FIND-00000000-0000-0000-0000-000000000001", "book_combined_visit", "owner-a"),
            ("FIND-11111111-2222-3333-4444-555555555555", "book_service", "owner-b"),
        ):
            action_id = findings._derive_action_id(finding_id, kind, owner)
            sent_to_dms = _dms_correlation_id(action_id)
            assert sent_to_dms is not None and not sent_to_dms.startswith("AVX-ACT-")
            assert h._action_key(sent_to_dms) == action_id

    def test_projection_against_real_ddb_semantics(self, monkeypatch):
        """moto, not MagicMock: a MagicMock accepts any Key, which is how this shipped."""
        import boto3
        from moto import mock_aws

        for var, val in (("AWS_ACCESS_KEY_ID", "testing"), ("AWS_SECRET_ACCESS_KEY", "testing"),
                         ("AWS_SESSION_TOKEN", "testing"), ("AWS_DEFAULT_REGION", "us-west-2")):
            monkeypatch.setenv(var, val)
        with mock_aws():
            table = boto3.resource("dynamodb", region_name="us-west-2").create_table(
                TableName="vsa-test-avx-actions",
                KeySchema=[{"AttributeName": "action_id", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "action_id", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
            # As the approve path + DMS executor leave it.
            table.put_item(Item={"action_id": _ACTION_KEY, "status": "in_progress",
                                 "target_system": "dms"})
            with patch.object(h, "_table", return_value=table):
                resp = h.handler(_eb_event(avx_action_id=_DMS_ID, to_status="Ready"), None)

            assert resp == {"status": "projected"}
            row = table.get_item(Key={"action_id": _ACTION_KEY})["Item"]
            assert row["status"] == "completed"
            assert row.get("updated_at")
            assert "Item" not in table.get_item(Key={"action_id": _DMS_ID}), (
                "a row appeared under the bare id"
            )
