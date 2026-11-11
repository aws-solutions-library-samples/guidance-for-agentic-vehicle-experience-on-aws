"""Fix Group G3-1 F7 / G3-2 (G3-18; review cycle 1 routes W3, cycle 2 routes C1): the cancel
audit entry is schema-valid and names the Action's own prior status, WITHOUT a read (the
consumer role holds dynamodb:UpdateItem only). Each non-terminal status is tried as a
conditional update ``#s = :prior`` in the order an Action moves through them.

The table fake has NO get_item (a read would raise AttributeError) and evaluates the
condition the way DynamoDB does.
"""
from __future__ import annotations

import json
import pathlib
import re
from unittest.mock import patch

import jsonschema
import pytest
from botocore.exceptions import ClientError

from .test_cancel_mapping import _DMS_FI, _eb_event, h

_SCHEMA = json.loads((pathlib.Path(__file__).resolve().parents[3]
                      / "agents/tier2/schemas/action.schema.json").read_text())
_AUDIT_ENTRY = {"$defs": _SCHEMA["$defs"], "$ref": "#/$defs/audit_entry"}
_TERMINAL = ("completed", "cancelled", "declined", "dismissed", "failed")


class _Table:
    """UpdateItem-only fake: evaluates attribute_exists, the terminal exclusions and
    ``#s = :prior``. ``moves`` maps call number -> status to switch to BEFORE that call
    (a concurrent forward move)."""

    def __init__(self, status, moves=None):
        self.status, self.moves, self.calls = status, dict(moves or {}), []

    def update_item(self, **kw):
        self.calls.append(kw)
        if len(self.calls) in self.moves:
            self.status = self.moves[len(self.calls)]
        cond, vals = kw["ConditionExpression"], kw["ExpressionAttributeValues"]
        ok = self.status is not None and self.status not in _TERMINAL
        if re.search(r"#s = :prior\b", cond):
            ok = ok and self.status == vals[":prior"]
        if not ok:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException",
                                         "Message": "x"}}, "UpdateItem")
        self.status = vals[":new_status"]
        return {}


def _cancel(table):
    with patch.object(h, "_table", return_value=table):
        return h.handler(_eb_event(avx_action_id=_DMS_FI, to_status="Closed",
                                   from_status="Draft"), None)


def _entry(kw):
    return kw["ExpressionAttributeValues"][":audit_entry"][0]


@pytest.mark.parametrize("prior,calls", [("pending_execution", 1), ("executed", 2),
                                         ("in_progress", 3)])
def test_entry_is_schema_valid_and_names_the_prior_action_status(prior, calls):
    t = _Table(prior)
    assert _cancel(t) == {"status": "projected"}
    assert t.status == "cancelled" and len(t.calls) == calls
    last = t.calls[-1]
    entry = _entry(last)
    jsonschema.validate(entry, _AUDIT_ENTRY)
    assert entry["from_status"] == prior
    assert "Draft" not in json.dumps(last["ExpressionAttributeValues"])
    assert last["ConditionExpression"].endswith(" AND #s = :prior")


def test_dms_status_in_entry_would_be_schema_invalid():
    """Control for the schema check: the old entry shape (DMS's 'Draft') is rejected."""
    t = _Table("in_progress")
    _cancel(t)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**_entry(t.calls[-1]), "from_status": "Draft"}, _AUDIT_ENTRY)


@pytest.mark.parametrize("status", [*_TERMINAL, None])
def test_terminal_or_missing_action_is_a_no_op(status):
    t = _Table(status)
    assert _cancel(t) == {"status": "no_op_conditional_check_failed"}
    assert t.status == status and len(t.calls) == 3


def test_a_concurrent_forward_move_is_met_by_a_later_attempt():
    t = _Table("pending_execution", moves={1: "executed"})  # moved just before call 1 lands
    assert _cancel(t) == {"status": "projected"}
    assert _entry(t.calls[-1])["from_status"] == "executed" and len(t.calls) == 2



def test_the_cancel_never_reads_the_actions_table():
    """Cycle 2 routes C1: AvxDmsEventConsumerRole grants dynamodb:UpdateItem only (pinned in
    infrastructure/test); any GetItem/Query/Scan would be AccessDenied once deployed."""
    reads: list = []

    class _Spy(_Table):
        def get_item(self, **kw):
            reads.append(("get_item", kw))
            return {"Item": {"status": "in_progress"}}

        def query(self, **kw):
            reads.append(("query", kw))
            return {"Items": []}

        def scan(self, **kw):
            reads.append(("scan", kw))
            return {"Items": []}

    t = _Spy("in_progress")
    assert _cancel(t) == {"status": "projected"}
    assert reads == []


class _Raising(_Table):
    """Raises ``ClientError(code)`` on call number ``on_call``, in place of the fake's own
    condition checks (they never run for that call); other calls behave as ``_Table``."""

    def __init__(self, status, on_call, code):
        super().__init__(status)
        self.on_call, self.code = on_call, code

    def update_item(self, **kw):
        if len(self.calls) + 1 == self.on_call:
            self.calls.append(kw)
            raise ClientError({"Error": {"Code": self.code, "Message": "x"}}, "UpdateItem")
        return super().update_item(**kw)


@pytest.mark.parametrize("on_call,code", [(2, "ProvisionedThroughputExceededException"),
                                          (1, "AccessDeniedException")])
def test_a_non_conditional_error_is_raised_for_eventbridge_to_retry(on_call, code):
    """Cycle 3 routes W1: only ConditionalCheckFailed means 'try the next status'; any other
    DynamoDB error propagates (EventBridge retries), never a silent no-op."""
    t = _Raising("in_progress", on_call=on_call, code=code)
    with pytest.raises(ClientError) as exc:
        _cancel(t)
    assert exc.value.response["Error"]["Code"] == code
    assert len(t.calls) == on_call and t.status == "in_progress"


def test_cancellable_set_is_the_schema_enum_minus_the_terminal_statuses():
    """Cycle 3 routes Suggestion: the tried statuses are exactly the non-terminal Action
    statuses, in the order an Action moves through them."""
    enum = _SCHEMA["properties"]["status"]["enum"]
    assert set(h._CANCELLABLE_IN_ORDER) == set(enum) - set(_TERMINAL)
    assert h._CANCELLABLE_IN_ORDER == ("pending_execution", "executed", "in_progress")
