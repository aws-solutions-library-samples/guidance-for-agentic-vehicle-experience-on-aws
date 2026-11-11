"""``write_finding(update_on_same_hash=True)`` — FI stage C call 59.

``inputs_hash`` excludes how a Finding's text was written (``diagnosis_source``), so a
model-written plan that replaces a rules-written item with unchanged inputs carries the
SAME hash. The default same-hash branch is a no-op (or an expiry refresh), which would
drop the model's content. With the flag the writer updates the row's content in place.

Mutations (each fails a test here on its own assertion):
  - the flag ignored (``not update_on_same_hash`` dropped) → the update test fails;
  - the flag inverted (``if live_row is not None and update_on_same_hash``) → the default
    no-op and refresh tests fail;
  - ``ArtifactWriter`` not forwarding the flag → the forwarding test fails.
"""
from __future__ import annotations

from agents.tier2 import writer as writer_mod
from agents.tier2.artifact_writer import ArtifactWriter


class _Client:
    """Low-level DynamoDB client double: one live row for every query, records writes."""

    def __init__(self, live):
        self.live = live
        self.updates: list[dict] = []
        self.puts: list[dict] = []

    def query(self, **kw):
        return {"Items": [writer_mod._to_ddb_item(self.live)] if self.live else []}

    def update_item(self, **kw):
        self.updates.append(kw)
        return {}

    def put_item(self, **kw):
        self.puts.append(kw)
        return {}


def _finding(**over):
    f = {"finding_key": "k", "finding_id": "FIND-new", "agent_id": "agent-fi-recall-warranty",
         "scope": {"vin": "VIN1"}, "finding_kind": "fleet.recall.vehicle", "inputs_hash": "h1",
         "expires_at": "2026-10-11T05:00:00Z", "computed_at": "2026-10-08T05:00:00Z",
         "headline": "Recall MRD-2026-001: book the remedy at DLR-1",
         "detail": "Model detail: book at DLR-1 with the brake inspection.",
         "evidence": [], "agent_version": "0.1.0", "audit_trail": []}
    f.update(over)
    return f


def _live():
    return {"finding_id": "FIND-live", "inputs_hash": "h1",
            "expires_at": "2026-10-10T05:00:00Z",
            "detail": "Book the recall service at the nearest available dealer."}


_NOW = "2026-10-08T05:00:00Z"


def test_default_same_hash_is_a_noop_and_writes_nothing():
    c = _Client(_live())
    out = writer_mod.write_finding(_finding(), table_name="t", dynamodb_client=c,
                                   utc_now_fn=lambda: _NOW)
    assert out == {"action": "noop", "finding_id": "FIND-live"}
    assert c.updates == [] and c.puts == []


def test_default_refresh_on_noop_still_only_extends_expiry():
    c = _Client(_live())
    out = writer_mod.write_finding(_finding(), table_name="t", dynamodb_client=c,
                                   refresh_on_noop=True, utc_now_fn=lambda: _NOW)
    assert out["action"] == "refresh"
    (u,) = c.updates
    assert "Model detail" not in str(u["ExpressionAttributeValues"]), (
        "a plain refresh must not rewrite the content")


def test_update_on_same_hash_rewrites_the_content_in_place():
    c = _Client(_live())
    out = writer_mod.write_finding(_finding(), table_name="t", dynamodb_client=c,
                                   update_on_same_hash=True, refresh_on_noop=True,
                                   utc_now_fn=lambda: _NOW)
    assert out == {"action": "update", "finding_id": "FIND-live"}
    assert c.puts == []
    (u,) = c.updates
    assert u["Key"] == {"finding_id": {"S": "FIND-live"}}
    values = str(u["ExpressionAttributeValues"])
    assert "Model detail: book at DLR-1 with the brake inspection." in values, (
        "the model's detail must land on the live row")
    assert "audit_trail" in u["UpdateExpression"]


def test_update_on_same_hash_with_no_live_row_puts_as_usual():
    c = _Client(None)
    out = writer_mod.write_finding(_finding(), table_name="t", dynamodb_client=c,
                                   update_on_same_hash=True, utc_now_fn=lambda: _NOW)
    assert out["action"] == "put" and len(c.puts) == 1 and c.updates == []


def test_artifact_writer_forwards_the_flag():
    c = _Client(_live())
    aw = ArtifactWriter(table_name="t", dynamodb_client=c)
    out = aw.write_finding(_finding(), update_on_same_hash=True, utc_now_fn=lambda: _NOW)
    assert out["action"] == "update"
    assert "Model detail" in str(c.updates[0]["ExpressionAttributeValues"])
