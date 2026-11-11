"""
test_cancel_mapping.py — D12 contract for the DMS event consumer's cancel mapping.

Contract source: `.kiro/specs/2026-10-05-fi-recall-warranty-stage-c/spec.md` D12,
and `docs/fi-action-queue-contract.md` § 3.

DMS has NO 'Cancelled' status.  The cancel path is a `Closed` event that
arrives while work was still in progress — the RO was administratively
closed before the repair was done.

The consumer reads `from_status` as well as `to_status` (both are already in
DMS's event):

  to_status         from_status                       Action status
  ──────────────── ──────────────────────────────── ─────────────
  Draft             any                               in_progress  (unchanged)
  Ready             any                               completed    (unchanged)
  Delivered         any                               completed    (unchanged)
  Invoiced          any                               completed    (NEW — Stage C)
  Closed            Ready / Delivered / Invoiced      completed    (NEW — Stage C)
  Closed            Draft / InProgress / AwaitingParts  cancelled  (NEW — Stage C)
  Closed            anything else or missing           ignored     (NEW code path)
  anything else     —                                 ignored     (unchanged)

Negative tests: 'Cancelled' and 'COMPLETED' (wrong case) are NOT DMS status
words and are both ignored.

Cancel audit entry (NEW — Stage C): when Closed-from-active maps to `cancelled`,
the UpdateItem call must append an audit entry with:
  from_status   (copied from the event)
  to_status     "cancelled"
  at            ISO-8601 UTC timestamp
  by            "dms-event"
  note          "ro_closed_before_work"
and must NOT contain `failure_reason` (the Action schema allows it only on
transitions to `declined` or `failed`; no schema change is needed).

Out-of-order delivery:
  Closed-from-InProgress fires FIRST, then Ready arrives →
    final status = cancelled  (terminal protection blocks Ready)
  Ready fires FIRST, then Closed-from-Ready arrives →
    final status = completed  (completed is already terminal; UNCHANGED behaviour)

NEW-behaviour tests are xfail(strict=True, reason="FI stage C task 1.4"): they fail
with today's handler (which maps only Draft/Ready/Delivered and does not read
from_status). They must not pass until Stage C is implemented.

Tests of today's UNCHANGED behaviour are plain tests (no xfail) and must pass now.

Run:
    .venv/bin/python -m pytest lambdas/api-avx-dms-event-consumer/tests/ -q
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Path setup — same approach as test_handler.py
# ---------------------------------------------------------------------------
_LAMBDA_ROOT = os.path.join(os.path.dirname(__file__), "..")
_LAMBDAS_ROOT = os.path.dirname(_LAMBDA_ROOT)
for _p in (_LAMBDA_ROOT, _LAMBDAS_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ["AVX_ACTIONS_TABLE"] = "vsa-test-avx-actions"

import handler as h  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# Two distinct DMS ids so FI-decision and non-FI-booking tests use independent
# Action rows and never collide with test_handler.py's IDs.
# ---------------------------------------------------------------------------

#: Represents an FI decision Action (book_recall_service, decided by fleet-operator).
_DMS_FI   = "d12f0001-aaaa-4000-b000-000000000001"
_KEY_FI   = f"AVX-ACT-{_DMS_FI}"

#: Represents a non-FI booking Action (book_combined_visit, owner booking).
_DMS_BOOK = "d12b0002-bbbb-4000-c000-000000000002"
_KEY_BOOK = f"AVX-ACT-{_DMS_BOOK}"


def _cancel_table(status: str = "in_progress"):
    """MagicMock table holding an Action in ``status``. The cancel path (Fix Group G3-1 F7,
    G3-2) tries each non-terminal status as ``#s = :prior``; this fake raises the conditional
    failure unless ``:prior`` is the held status, as DynamoDB would."""
    from botocore.exceptions import ClientError as _CE

    t = MagicMock()
    state = {"status": status}

    def _update(**kw):
        values = kw.get("ExpressionAttributeValues") or {}
        prior = values.get(":prior")
        if prior is not None and prior != state["status"]:
            raise _CE({"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}},
                      "UpdateItem")
        state["status"] = values.get(":new_status", state["status"])
        return {}

    t.update_item.side_effect = _update
    return t


def _eb_event(
    *,
    avx_action_id: str | None = _DMS_FI,
    to_status: str = "Draft",
    from_status: str = "InProgress",
    ro_id: str = "ro-d12-001",
    vin: str = "MRDVEH00000000001",
    dealer_id: str = "dealer-meridian-01",
) -> dict:
    """Build a minimal EventBridge dms.ro.status_changed event for D12 tests.

    Both `from_status` and `to_status` are included because DMS's publisher
    always emits both (`RO_STATUS_CHANGED_FIELDS`).  The current handler reads
    only `to_status`; Stage C will also read `from_status`.

    Placeholder account (123456789012) per public-mirror safety — same
    note as test_handler.py.
    """
    detail: dict = {
        "ro_id": ro_id,
        "dealer_id": dealer_id,
        "vin": vin,
        "from_status": from_status,
        "to_status": to_status,
        "changed_at": "2026-10-05T05:00:00Z",
        "customer_channel_pref": "email",
        "quiet_hours_applicable": False,
    }
    if avx_action_id is not None:
        detail["avx_action_id"] = avx_action_id
    return {
        "version": "0",
        "id": "test-d12-event",
        "detail-type": "dms.ro.status_changed",
        "source": "dms.service-lane",
        "account": "123456789012",
        "time": "2026-10-05T05:00:00Z",
        "region": "us-west-2",
        "resources": [],
        "detail": detail,
    }


def _make_ccf() -> ClientError:
    """Simulate DynamoDB ConditionalCheckFailedException."""
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException",
                   "Message": "The conditional request failed"}},
        "UpdateItem",
    )


def _make_stateful_mock(
    action_key: str,
    initial_status: str = "in_progress",
    initial_row: dict | None = None,
) -> tuple[MagicMock, dict]:
    """Return (mock_table, row_store) that enforces the ConditionExpression.

    The side-effect simulates the terminal-state exclusions that real DynamoDB
    evaluates atomically.  Same pattern as test_handler.py's
    TestIdempotency.test_duplicate_event_produces_one_projection.

    initial_row: the full row dict (default: {"status": initial_status}). Pass a row
    with a "decision" attribute to simulate an FI decision Action; without it to simulate
    a non-FI booking Action (used for M5b guard tests).
    """
    if initial_row is None:
        initial_row = {"status": initial_status}
    else:
        initial_row = dict(initial_row)
        initial_row.setdefault("status", initial_status)
    row_store: dict[str, dict] = {action_key: initial_row}

    def stateful_update_item(**kwargs):
        key = kwargs["Key"]["action_id"]
        new_status = kwargs["ExpressionAttributeValues"][":new_status"]
        condition = kwargs.get("ConditionExpression", "")
        if condition:
            if "attribute_exists(action_id)" in condition and key not in row_store:
                raise _make_ccf()
            if "#s <> :new_status" in condition:
                if row_store.get(key, {}).get("status") == new_status:
                    raise _make_ccf()
            for term in ("completed", "cancelled", "declined", "dismissed", "failed"):
                if f"#s <> :{term}" in condition:
                    if row_store.get(key, {}).get("status") == term:
                        raise _make_ccf()
            # Evaluate attribute_exists(decision): if the condition requires the attribute
            # to exist but the row does not have it, raise CCF.
            if "attribute_exists(decision)" in condition:
                if "decision" not in row_store.get(key, {}):
                    raise _make_ccf()
            # Fix Group G3-1/G3-2: the cancel's `#s = :prior` (the status the Action leaves).
            if "#s = :prior" in condition:
                prior = kwargs["ExpressionAttributeValues"][":prior"]
                if row_store.get(key, {}).get("status") != prior:
                    raise _make_ccf()
        row_store.setdefault(key, {})["status"] = new_status

    mock_table = _cancel_table()
    mock_table.update_item.side_effect = stateful_update_item
    return mock_table, row_store


# ============================================================================
# PASSING TESTS — today's unchanged behaviour
# ============================================================================

class TestUnchangedProjections:
    """Draft/Ready/Delivered map unchanged.  Covers one FI decision Action (Draft)
    and one non-FI booking Action (Ready) per the D12 requirement that the rule
    applies to every DMS-booked Action type."""

    def test_draft_projects_to_in_progress_fi_decision_action(self):
        """Draft → in_progress (FI decision Action).  Unchanged by Stage C."""
        mock_table = _cancel_table()
        with patch.object(h, "_table", return_value=mock_table):
            resp = h.handler(
                _eb_event(avx_action_id=_DMS_FI, to_status="Draft",
                          from_status="Scheduled"),
                None,
            )
        assert resp == {"status": "projected"}
        mock_table.update_item.assert_called_once()
        kwargs = mock_table.update_item.call_args.kwargs
        assert kwargs["Key"] == {"action_id": _KEY_FI}
        assert kwargs["ExpressionAttributeValues"][":new_status"] == "in_progress"

    def test_ready_projects_to_completed_non_fi_booking_action(self):
        """Ready → completed (non-FI booking Action).  Unchanged by Stage C."""
        mock_table = _cancel_table()
        with patch.object(h, "_table", return_value=mock_table):
            resp = h.handler(
                _eb_event(avx_action_id=_DMS_BOOK, to_status="Ready",
                          from_status="InProgress"),
                None,
            )
        assert resp == {"status": "projected"}
        kwargs = mock_table.update_item.call_args.kwargs
        assert kwargs["Key"] == {"action_id": _KEY_BOOK}
        assert kwargs["ExpressionAttributeValues"][":new_status"] == "completed"

    def test_delivered_projects_to_completed(self):
        """Delivered → completed.  Unchanged by Stage C."""
        mock_table = _cancel_table()
        with patch.object(h, "_table", return_value=mock_table):
            resp = h.handler(
                _eb_event(avx_action_id=_DMS_FI, to_status="Delivered",
                          from_status="Ready"),
                None,
            )
        assert resp == {"status": "projected"}
        assert (
            mock_table.update_item.call_args.kwargs["ExpressionAttributeValues"][
                ":new_status"
            ]
            == "completed"
        )


class TestNegativeIgnoredStatuses:
    """'Cancelled' and 'COMPLETED' are not DMS status words — both ignored now and
    after Stage C.  Negative tests confirming the contract boundaries."""

    def test_to_status_cancelled_is_not_a_dms_status(self):
        """DMS has NO 'Cancelled' status (D12 analysis).

        The AVX Action `cancelled` state is produced by the consumer
        processing Closed-from-active events, never by a DMS 'Cancelled'
        status word.  D12 explicitly rejects adding one:
        'Why not a new DMS Cancelled status: no advisor can set one today.'
        """
        mock_table = _cancel_table()
        with patch.object(h, "_table", return_value=mock_table):
            resp = h.handler(
                _eb_event(to_status="Cancelled", from_status="InProgress"), None
            )
        assert resp == {"status": "no_op_unmapped_status"}
        mock_table.update_item.assert_not_called()

    def test_to_status_completed_wrong_case_is_ignored(self):
        """'COMPLETED' (wrong case) is not a DMS status word and is ignored."""
        mock_table = _cancel_table()
        with patch.object(h, "_table", return_value=mock_table):
            resp = h.handler(
                _eb_event(to_status="COMPLETED", from_status="Ready"), None
            )
        assert resp == {"status": "no_op_unmapped_status"}
        mock_table.update_item.assert_not_called()


class TestClosedWithUnrecognizedFromStatus:
    """Closed + unrecognized or missing from_status → ignored.

    Today: Closed is not in _STATUS_PROJECTION → no DDB write (no_op_unmapped_status).
    After Stage C: Closed + from_status not in either recognised set → still no DDB
    write.  Both code paths produce the same observable result (no write), so these
    tests pass both before and after the Stage C implementation.
    """

    def test_closed_with_unknown_from_status_is_ignored(self):
        """Closed-from-Scheduled: 'Scheduled' is not in either active-status set."""
        mock_table = _cancel_table()
        with patch.object(h, "_table", return_value=mock_table):
            h.handler(_eb_event(to_status="Closed", from_status="Scheduled"), None)
        mock_table.update_item.assert_not_called()

    def test_closed_with_missing_from_status_is_ignored(self):
        """Event that carries no from_status field at all → ignored."""
        detail: dict = {
            "avx_action_id": _DMS_FI,
            "ro_id": "ro-d12-no-from",
            "dealer_id": "dealer-meridian-01",
            "vin": "MRDVEH00000000001",
            "to_status": "Closed",
            # from_status deliberately absent
            "changed_at": "2026-10-05T05:00:00Z",
            "customer_channel_pref": "email",
            "quiet_hours_applicable": False,
        }
        event = {
            "version": "0", "id": "test-d12-no-from",
            "detail-type": "dms.ro.status_changed", "source": "dms.service-lane",
            "account": "123456789012", "time": "2026-10-05T05:00:00Z",
            "region": "us-west-2", "resources": [], "detail": detail,
        }
        mock_table = _cancel_table()
        with patch.object(h, "_table", return_value=mock_table):
            h.handler(event, None)
        mock_table.update_item.assert_not_called()


class TestTerminalProtectionUnchanged:
    """Terminal-state protection is unchanged.  The pass-now case: Ready fires
    first, Closed-from-Ready fires later — completed stays completed."""

    def test_ready_then_closed_from_ready_stays_completed(self):
        """Ready → completed; then Closed-from-Ready arrives.  Final = completed.

        Today: Ready → projected(completed). Closed → no_op_unmapped (Closed is
        unmapped). Row stays completed.

        After Stage C: Ready → projected(completed). Closed-from-Ready tries to
        project completed, but CCF fires because completed is terminal. Row still
        stays completed.

        Both code paths produce the same observable outcome, so this test passes
        in both worlds.  It verifies the terminal-protection invariant is intact.
        """
        mock_table, row_store = _make_stateful_mock(_KEY_FI, initial_status="in_progress")
        with patch.object(h, "_table", return_value=mock_table):
            resp1 = h.handler(
                _eb_event(avx_action_id=_DMS_FI, to_status="Ready",
                          from_status="InProgress"),
                None,
            )
            _resp2 = h.handler(
                _eb_event(avx_action_id=_DMS_FI, to_status="Closed",
                          from_status="Ready"),
                None,
            )

        assert resp1 == {"status": "projected"}
        assert row_store[_KEY_FI]["status"] == "completed", (
            f"Expected completed to survive. Got {row_store[_KEY_FI]['status']!r}."
        )


# ============================================================================
# XFAIL TESTS — new behaviour (Stage C / D12)
# ============================================================================

class TestInvoicedMapsToCompleted:
    """to_status=Invoiced → completed (new in Stage C; Invoiced is not in the
    current _STATUS_PROJECTION)."""

    def test_invoiced_maps_to_completed_fi_decision_action(self):
        """Invoiced → completed for an FI decision Action.

        Currently: Invoiced is not in _STATUS_PROJECTION → no_op_unmapped_status.
        After Stage C: Invoiced must map to completed.
        """
        mock_table = _cancel_table()
        with patch.object(h, "_table", return_value=mock_table):
            resp = h.handler(
                _eb_event(avx_action_id=_DMS_FI, to_status="Invoiced",
                          from_status="Ready"),
                None,
            )
        assert resp == {"status": "projected"}, (
            f"Invoiced must map to completed (D12). Got {resp!r}. "
            "'Invoiced' is not yet in _STATUS_PROJECTION."
        )
        assert (
            mock_table.update_item.call_args.kwargs["ExpressionAttributeValues"][
                ":new_status"
            ]
            == "completed"
        )

    def test_invoiced_maps_to_completed_regardless_of_from_status(self):
        """from_status is irrelevant when to_status=Invoiced (non-FI booking Action)."""
        mock_table = _cancel_table()
        with patch.object(h, "_table", return_value=mock_table):
            resp = h.handler(
                _eb_event(avx_action_id=_DMS_BOOK, to_status="Invoiced",
                          from_status="Draft"),
                None,
            )
        assert resp == {"status": "projected"}
        assert (
            mock_table.update_item.call_args.kwargs["ExpressionAttributeValues"][
                ":new_status"
            ]
            == "completed"
        )


class TestClosedFromCompletedActiveStatus:
    """Closed + from_status ∈ {Ready, Delivered, Invoiced} → completed (Stage C new).

    Closed is not currently in _STATUS_PROJECTION, so all three tests fail with
    no_op_unmapped_status today.
    """

    def test_closed_from_ready_maps_to_completed(self):
        """Closed-from-Ready: the repair was done; DMS closed the RO afterward."""
        mock_table = _cancel_table()
        with patch.object(h, "_table", return_value=mock_table):
            resp = h.handler(
                _eb_event(avx_action_id=_DMS_FI, to_status="Closed",
                          from_status="Ready"),
                None,
            )
        assert resp == {"status": "projected"}, (
            f"Closed-from-Ready must map to completed. Got {resp!r}."
        )
        assert (
            mock_table.update_item.call_args.kwargs["ExpressionAttributeValues"][
                ":new_status"
            ]
            == "completed"
        )

    def test_closed_from_delivered_maps_to_completed(self):
        """Closed-from-Delivered: vehicle was delivered; administrative close."""
        mock_table = _cancel_table()
        with patch.object(h, "_table", return_value=mock_table):
            resp = h.handler(
                _eb_event(avx_action_id=_DMS_FI, to_status="Closed",
                          from_status="Delivered"),
                None,
            )
        assert resp == {"status": "projected"}
        assert (
            mock_table.update_item.call_args.kwargs["ExpressionAttributeValues"][
                ":new_status"
            ]
            == "completed"
        )

    def test_closed_from_invoiced_maps_to_completed(self):
        """Closed-from-Invoiced: invoice was raised; administrative close."""
        mock_table = _cancel_table()
        with patch.object(h, "_table", return_value=mock_table):
            resp = h.handler(
                _eb_event(avx_action_id=_DMS_FI, to_status="Closed",
                          from_status="Invoiced"),
                None,
            )
        assert resp == {"status": "projected"}
        assert (
            mock_table.update_item.call_args.kwargs["ExpressionAttributeValues"][
                ":new_status"
            ]
            == "completed"
        )


class TestClosedFromNonCompletedActiveStatus:
    """Closed + from_status ∈ {Draft, InProgress, AwaitingParts} → cancelled (Stage C new).

    Covers both an FI decision Action and a non-FI booking Action: D12 says the
    rule applies to every DMS-booked Action, including owner bookings such as
    book_combined_visit.
    """

    def test_closed_from_draft_maps_to_cancelled_fi_decision_action(self):
        """RO closed from Draft: work never started.  FI decision Action.

        Currently: Closed is not in _STATUS_PROJECTION → no_op_unmapped_status.
        After Stage C: Closed-from-Draft → cancelled.
        """
        mock_table = _cancel_table()
        with patch.object(h, "_table", return_value=mock_table):
            resp = h.handler(
                _eb_event(avx_action_id=_DMS_FI, to_status="Closed",
                          from_status="Draft"),
                None,
            )
        assert resp == {"status": "projected"}, (
            f"Closed-from-Draft must map to cancelled (D12). Got {resp!r}. "
            "The handler does not yet read from_status."
        )
        kwargs = mock_table.update_item.call_args.kwargs
        assert kwargs["Key"] == {"action_id": _KEY_FI}
        assert kwargs["ExpressionAttributeValues"][":new_status"] == "cancelled"

    def test_closed_from_inprogress_maps_to_cancelled(self):
        """RO closed from InProgress: work started but not finished."""
        mock_table = _cancel_table()
        with patch.object(h, "_table", return_value=mock_table):
            resp = h.handler(
                _eb_event(avx_action_id=_DMS_FI, to_status="Closed",
                          from_status="InProgress"),
                None,
            )
        assert resp == {"status": "projected"}
        assert (
            mock_table.update_item.call_args.kwargs["ExpressionAttributeValues"][
                ":new_status"
            ]
            == "cancelled"
        )

    def test_closed_from_awaitingparts_maps_to_cancelled(self):
        """RO closed from AwaitingParts: parts ordered but never arrived."""
        mock_table = _cancel_table()
        with patch.object(h, "_table", return_value=mock_table):
            resp = h.handler(
                _eb_event(avx_action_id=_DMS_FI, to_status="Closed",
                          from_status="AwaitingParts"),
                None,
            )
        assert resp == {"status": "projected"}
        assert (
            mock_table.update_item.call_args.kwargs["ExpressionAttributeValues"][
                ":new_status"
            ]
            == "cancelled"
        )

    def test_closed_from_inprogress_maps_to_cancelled_non_fi_booking_action(self):
        """D12 applies to non-FI booking Actions (owner book_combined_visit) too.

        M5b guard: uses _make_stateful_mock without 'decision' attribute on the row.
        A handler that restricts cancel to attribute_exists(decision) would skip this
        Action and fail to write 'cancelled'.
        """
        mock_table, row_store = _make_stateful_mock(
            _KEY_BOOK,
            initial_status="in_progress",
            initial_row={"status": "in_progress"},  # no 'decision' attr — non-FI booking
        )
        with patch.object(h, "_table", return_value=mock_table):
            resp = h.handler(
                _eb_event(avx_action_id=_DMS_BOOK, to_status="Closed",
                          from_status="InProgress"),
                None,
            )
        assert resp == {"status": "projected"}, (
            f"Non-FI booking Action must be cancelled. Got {resp!r}. "
            "M5b guard: must not restrict cancel to attribute_exists(decision)."
        )
        kwargs = mock_table.update_item.call_args.kwargs
        assert kwargs["Key"] == {"action_id": _KEY_BOOK}
        assert kwargs["ExpressionAttributeValues"][":new_status"] == "cancelled"


class TestCancelAuditEntry:
    """Cancel audit entry shape (new; handler currently writes no audit entry).

    D12: 'The cancel's audit entry carries from_status, to_status: cancelled,
    at, by: dms-event and note: ro_closed_before_work, and no failure_reason
    (the schema allows it only on declined and failed). No schema change.'

    The audit entry is expected in the UpdateItem call's ExpressionAttributeValues
    under the key ':audit_entry' as a list containing one dict.
    """

    def test_cancel_audit_entry_fields_fi_decision_action(self):
        """Closed-from-Draft: audit entry on an FI decision Action has all required
        fields, uses list_append (not SET overwrite), and carries no failure_reason.

        M5a guard: a handler that uses SET audit_trail = :audit_entry (overwrites instead
        of appending) must fail this test. The UpdateExpression must contain list_append.
        M5b guard: the FI decision row has a 'decision' attribute so the cancel applies.
        """
        mock_table = _cancel_table()
        with patch.object(h, "_table", return_value=mock_table):
            resp = h.handler(
                _eb_event(avx_action_id=_DMS_FI, to_status="Closed",
                          from_status="Draft"),
                None,
            )
        assert resp == {"status": "projected"}, (
            f"Handler must write the cancel; got {resp!r}. "
            "The handler does not yet read from_status."
        )
        kwargs = mock_table.update_item.call_args.kwargs
        eav = kwargs["ExpressionAttributeValues"]

        assert ":audit_entry" in eav, (
            "No ':audit_entry' in ExpressionAttributeValues. "
            "Handler does not yet append an audit entry on cancel."
        )
        entries = eav[":audit_entry"]
        assert len(entries) == 1, f"Expected one audit entry, got {len(entries)}"
        entry = entries[0]

        assert entry.get("from_status") == "in_progress", (
            f"from_status is the Action's prior status (G3-18; schema enum), not DMS's. "
            f"Got: {entry.get('from_status')!r}"
        )
        assert entry.get("to_status") == "cancelled", (
            f"to_status in the audit entry must be 'cancelled', not the DMS "
            f"to_status. Got: {entry.get('to_status')!r}"
        )
        assert entry.get("by") == "dms-event", (
            f"by must be 'dms-event'. Got: {entry.get('by')!r}"
        )
        assert entry.get("note") == "ro_closed_before_work", (
            f"note must be 'ro_closed_before_work'. Got: {entry.get('note')!r}"
        )
        assert "at" in entry, (
            "Audit entry must carry an 'at' timestamp."
        )
        # ISO-8601 UTC: must end with 'Z' or '+00:00' and contain 'T'.
        at_val = entry.get("at", "")
        assert "T" in at_val and (at_val.endswith("Z") or "+00:00" in at_val), (
            f"'at' must be ISO-8601 UTC (e.g. '2026-10-05T05:00:00Z'). Got {at_val!r}."
        )
        assert "failure_reason" not in entry, (
            "failure_reason must NOT appear in a cancel audit entry. "
            "The Action schema allows failure_reason only on transitions to "
            "'declined' or 'failed'; cancel is neither."
        )
        # M5a guard: the UpdateExpression must use list_append, not SET overwrite.
        update_expr = kwargs.get("UpdateExpression", "")
        assert "list_append" in update_expr, (
            "The UpdateExpression must use list_append(...) to append the audit entry "
            "to audit_trail, not 'SET audit_trail = :audit_entry' (which overwrites). "
            "M5a guard."
        )

    def test_cancel_audit_entry_fields_non_fi_booking_action(self):
        """Closed-from-AwaitingParts: audit entry on a non-FI booking Action
        (book_combined_visit) has the same required fields.

        M5b guard: uses _make_stateful_mock with a row that has no 'decision' attribute
        (simulating a non-FI booking Action). A handler that only cancels rows with
        attribute_exists(decision) would skip this Action, leaving the status unchanged.
        """
        mock_table, row_store = _make_stateful_mock(
            _KEY_BOOK,
            initial_status="in_progress",
            initial_row={"status": "in_progress"},  # no 'decision' attr — non-FI booking
        )
        with patch.object(h, "_table", return_value=mock_table):
            resp = h.handler(
                _eb_event(avx_action_id=_DMS_BOOK, to_status="Closed",
                          from_status="AwaitingParts"),
                None,
            )
        assert resp == {"status": "projected"}, (
            f"Non-FI booking Action must be cancelled on Closed-from-active. "
            f"Got {resp!r}. M5b guard: handler must not restrict cancel to rows with "
            "attribute_exists(decision)."
        )
        eav = mock_table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert ":audit_entry" in eav, (
            "No ':audit_entry' in ExpressionAttributeValues for non-FI booking."
        )
        entry = eav[":audit_entry"][0]
        assert entry.get("from_status") == "in_progress"  # Action's prior status (G3-18)
        assert entry.get("to_status") == "cancelled"
        assert entry.get("by") == "dms-event"
        assert entry.get("note") == "ro_closed_before_work"
        assert "at" in entry
        assert "failure_reason" not in entry


class TestOutOfOrderDelivery:
    """Out-of-order event delivery with the new Closed handling."""

    def test_closed_from_inprogress_then_ready_leaves_cancelled(self):
        """Closed-from-InProgress fires FIRST; Ready arrives later.

        Expected: final status = cancelled.

        Today: Closed → no_op_unmapped_status (ignored). Row stays in_progress.
               Ready → projected(completed). Final = completed.
               Assertion fails → xfailed.

        After Stage C: Closed-from-InProgress → projected(cancelled). Terminal
               protection blocks the later Ready event. Final = cancelled.
        """
        mock_table, row_store = _make_stateful_mock(_KEY_FI, initial_status="in_progress")
        with patch.object(h, "_table", return_value=mock_table):
            h.handler(
                _eb_event(avx_action_id=_DMS_FI, to_status="Closed",
                          from_status="InProgress"),
                None,
            )
            h.handler(
                _eb_event(avx_action_id=_DMS_FI, to_status="Ready",
                          from_status="InProgress"),
                None,
            )

        assert row_store[_KEY_FI]["status"] == "cancelled", (
            f"Expected 'cancelled' after Closed-from-InProgress then Ready. "
            f"Got {row_store[_KEY_FI]['status']!r}. "
            f"The Closed-from-InProgress event is not yet projected to cancelled "
            f"(from_status is not read), so Ready overwrote the status."
        )
