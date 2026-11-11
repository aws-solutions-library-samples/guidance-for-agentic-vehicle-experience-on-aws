"""Fix Group G3-1 F4b (review cycle 1 W6): the skip rule's HASH half, through the real loops.

Both loops decide through runner.skip_unchanged (one definition). For each loop:
  (a) model-written live row, SAME hash      -> no write
  (b) model-written live row, DIFFERENT hash -> written (a changed vehicle never keeps stale
      model text; mutants ME / MG)
  (c) rules-written live row, same hash      -> written (task 4.1 may upgrade it)
The matching hash is read from a first run with no live row. Warranty writes also carry
refresh_on_noop=True (G3-14; F1's mutant F1cw).
"""
from __future__ import annotations

from typing import Any, Optional

from agents.fi_recall_warranty.tests import test_items_fix_f2 as rec
from agents.fi_recall_warranty.tests import test_warranty_fix_f4a as war
from agents.fi_recall_warranty.warranty import run_warranty_loop


def _live(first: dict, *, source: str, kind: str, inputs_hash: Optional[str] = None) -> dict:
    """The stored Finding as the live lookup returns it, diagnosis source in schema form."""
    live = dict(first)
    live["inputs_hash"] = inputs_hash or first["inputs_hash"]
    ev = [e for e in first.get("evidence") or [] if e.get("kind") != kind]
    live["evidence"] = ev + [{"kind": kind, "value": source}]
    return live


# ---- recall loop ---------------------------------------------------------------------------

class _RecallFakes(rec._Fakes):
    def __init__(self, live: Optional[dict] = None):
        super().__init__()
        self.live = live

    def lookup_live(self, key: str):
        return self.live


def _recall_first() -> dict:
    f = _RecallFakes()
    rec._run(f, [rec._row()])
    book = [w for w in f.written if w["subject_key"].split("/")[1] == "book"]
    assert book, "fixture must write a book item"
    return book[0]


def _recall_written(live: dict) -> list:
    f = _RecallFakes(live)
    rec._run(f, [rec._row()])
    return [w for w in f.written if w["subject_key"].split("/")[1] == "book"]


class TestRecallLoopSkipHashHalf:
    def test_model_written_same_hash_skipped(self):
        """CHANGED by FI stage C Group 4 (call 52, architect-authorized): the model call is
        skipped, but the item is still written once, from the LIVE row's model content and
        with refresh_on_noop=True, so it doesn't expire on day 4. The Group 3 contract here
        was "no write", which let an unchanged model-written item lapse after its 3-day
        expiry (Group 3 carried Suggestion "the model-path skip should still refresh")."""
        first = _recall_first()
        live = _live(first, source="model", kind="recall.diagnosis_source")
        live["detail"] = "LIVE MODEL DETAIL"
        f = _RecallFakes(live)
        flags: list = []
        base = f.write_finding

        def _write(finding, standing, refresh_on_noop=False, **kw):
            flags.append((refresh_on_noop, kw.get("update_on_same_hash", False)))
            return base(finding, standing, refresh_on_noop=refresh_on_noop)

        f.write_finding = _write
        stats = rec._run(f, [rec._row()])
        book = [w for w in f.written if w["subject_key"].split("/")[1] == "book"]
        assert len(book) == 1, "the unchanged model-written item must still be refreshed"
        assert book[0]["detail"] == "LIVE MODEL DETAIL", "the refresh must keep the model text"
        assert (True, False) in flags, "the refresh must pass refresh_on_noop=True only"
        assert stats["items_refreshed_from_live"] >= 1

    def test_model_written_different_hash_rewritten(self):
        first = _recall_first()
        live = _live(first, source="model", kind="recall.diagnosis_source", inputs_hash="0" * 64)
        assert len(_recall_written(live)) == 1, "changed inputs must replace the model text"

    def test_rules_written_same_hash_rewritten(self):
        first = _recall_first()
        assert len(_recall_written(_live(first, source="rules", kind="recall.diagnosis_source"))) == 1


# ---- warranty loop -------------------------------------------------------------------------

class _WarrantyFakes(war._Fakes):
    def __init__(self, live: Optional[dict] = None):
        super().__init__()
        self.live = live
        self.flags: list = []

    def lookup_live(self, key: str):
        return self.live

    def write_finding(self, finding: dict, standing: Any, **kw) -> None:
        self.flags.append(kw.get("refresh_on_noop", False))
        self.written.append(finding)


def _warranty_run(live: Optional[dict] = None) -> _WarrantyFakes:
    f = _WarrantyFakes(live)
    run_warranty_loop(candidate_rows=[war._row()], computed_at=war._COMPUTED_AT,
                      standing_fn=f.standing_fn, lookup_live=f.lookup_live,
                      write_finding=f.write_finding, warranty_terms=war._DEFAULT_TERMS)
    return f


class TestWarrantyLoopSkipHashHalf:
    def _first(self) -> dict:
        f = _warranty_run()
        assert len(f.written) == 1, "fixture must write one warranty Finding"
        return f.written[0]

    def test_model_written_same_hash_skipped(self):
        """CHANGED by FI stage C Group 4 (S3a step 4, architect-authorized, same contract
        as recall's test_model_written_same_hash_skipped): the model call is skipped,
        but the item is still written once from the LIVE row's model content with
        refresh_on_noop=True, so it does not expire on day 4.  The Group 3 contract
        ("written == []") let an unchanged model-written warranty item lapse after its
        3-day TTL whenever the model path is off."""
        first = self._first()
        live = _live(first, source="model", kind="warranty.diagnosis_source")
        live["detail"] = "LIVE MODEL DETAIL"
        f = _WarrantyFakes(live)
        stats = run_warranty_loop(
            candidate_rows=[war._row()], computed_at=war._COMPUTED_AT,
            standing_fn=f.standing_fn, lookup_live=f.lookup_live,
            write_finding=f.write_finding, warranty_terms=war._DEFAULT_TERMS,
        )
        assert len(f.written) == 1, "unchanged model-written item must still be refreshed once"
        assert f.written[0]["detail"] == "LIVE MODEL DETAIL", "refresh must keep the live model text"
        assert True in f.flags, "refresh must pass refresh_on_noop=True"
        assert stats["items_refreshed_from_live"] >= 1

    def test_model_written_different_hash_rewritten(self):
        live = _live(self._first(), source="model", kind="warranty.diagnosis_source",
                     inputs_hash="0" * 64)
        assert len(_warranty_run(live).written) == 1, "changed inputs must replace the model text"

    def test_rules_written_same_hash_rewritten(self):
        live = _live(self._first(), source="rules", kind="warranty.diagnosis_source")
        assert len(_warranty_run(live).written) == 1

    def test_every_warranty_write_refreshes_on_noop(self):
        f = _warranty_run()
        assert f.flags == [True], f"warranty writes must pass refresh_on_noop=True; got {f.flags}"
