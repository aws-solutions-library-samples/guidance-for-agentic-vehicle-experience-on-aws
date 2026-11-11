"""M13-runner-recall-no-standing / M13w-runner-warranty-no-standing.

run_recall and run_warranty thread the standing returned by owner_resolver through
the write closure to avx_writer.write_finding (identity check — not None).
"""
from unittest.mock import MagicMock

import agents.fi_recall_warranty.recall as _rw_recall
import agents.fi_recall_warranty.selection as _rw_sel
import agents.fi_recall_warranty.warranty as _rw_warranty
from agents.tier2 import runner

_SENTINEL = object()
_FINDING = {"finding_id": "f-standing-1", "finding_kind": "fleet.recall.vehicle"}


class _Writer:
    table_name = "vsa-staging-avx-findings"
    def __init__(self):
        self.dynamodb_client = MagicMock(**{"query.return_value": {"Items": []}})
        self.standings: list = []
    def write_finding(self, _f, *, standing=None, refresh_on_noop=False,
                      update_on_same_hash=False):
        self.standings.append(standing)
        return {}


def _base(writer):
    return dict(athena_client=object(), workgroup="cvx-staging-analytics",
                results_s3_path="s3://x/", avx_writer=writer,
                owner_resolver=lambda _vin: _SENTINEL,
                run_record_writer=lambda **_: None)


def test_run_recall_standing_identity(monkeypatch):
    """write closure passes standing (not None) to avx_writer.write_finding."""
    monkeypatch.setattr(_rw_sel, "select_unremedied_recalls", lambda *a, **kw: ([], 0))
    monkeypatch.setattr(_rw_sel, "select_parts_stock", lambda *a, **kw: [])

    def fake_loop(*, write_finding, standing_fn=None, **_):
        write_finding(_FINDING, standing_fn("VIN-R1") if standing_fn else None)
        return {"items_written": 1, "items_skipped_unchanged": 0}

    monkeypatch.setattr(_rw_recall, "run_recall_loop", fake_loop)
    w = _Writer()
    runner.run_recall("staging", **_base(w),
                      ddb_client=object(), vehicles_table="t",
                      fleet_locations_table="t2", groundings_table="t3",
                      actions_table="t4")
    assert w.standings == [_SENTINEL], f"expected [sentinel], got {w.standings}"


def test_run_warranty_standing_identity(monkeypatch):
    """write closure passes standing (not None) to avx_writer.write_finding."""
    monkeypatch.setattr(_rw_sel, "select_warranty_candidates", lambda *a, **kw: [])
    monkeypatch.setattr(_rw_sel, "select_warranty_terms", lambda *a, **kw: {})

    def fake_loop(*, write_finding, standing_fn=None, **_):
        write_finding(_FINDING, standing_fn("VIN-W1") if standing_fn else None)
        return {"items_written": 1, "items_skipped_unchanged": 0}

    monkeypatch.setattr(_rw_warranty, "run_warranty_loop", fake_loop)
    w = _Writer()
    runner.run_warranty("staging", **_base(w))
    assert w.standings == [_SENTINEL], f"expected [sentinel], got {w.standings}"
