"""Fix Group G3-2 (review cycle 2, agent area C1-C4, W1-W3), through the real loops.

C1 warranty row with no coverage group is still an item (fixed sentence only).
C2 an Action DMS accepted (in_progress) is never rewritten or re-raised.
C3 the daily rewrite of an undecided item keeps its attempt number.
C4 the exhausted item: fixed headline, no recommended_action (never approvable).
W1 a fleet-locations read error keeps the vehicle id (grounding still checked), no origin.
W2 recall.item uses the contract's value attempts_exhausted, with the slot.
W3 informational items carry fixed detail, never the booking plan's text.
"""
from __future__ import annotations

import datetime

import pytest

from agents.fi_recall_warranty import attempts
from agents.fi_recall_warranty.selection import RegistryReadError
from agents.fi_recall_warranty.tests import test_items_fix_f2 as rec
from agents.fi_recall_warranty.tests import test_warranty_fix_f4a as war

_NOW = datetime.datetime(2026, 10, 7, 5, 0, tzinfo=datetime.timezone.utc)


def _value(finding: dict, kind: str):
    for e in finding.get("evidence") or []:
        if e.get("kind") == kind:
            return e.get("value")
    raise AssertionError(f"no {kind} evidence")


# ---- C2 --------------------------------------------------------------------------------------

def test_in_progress_action_maps_to_in_progress_and_is_skipped():
    qs, _ = attempts._map_action_to_queue_status({"status": "in_progress"}, _NOW)
    assert qs == "in_progress"
    entry = {"attempt_number": 1, "queue_status": qs, "decided_at": None}
    assert attempts.plan_attempt(entry, "safety", _NOW) == "skip"


def test_unknown_action_status_is_never_treated_as_no_action():
    qs, _ = attempts._map_action_to_queue_status({"status": "some_new_status"}, _NOW)
    assert qs != "none"
    assert attempts.plan_attempt({"attempt_number": 1, "queue_status": qs}, "safety", _NOW) == "skip"


def test_loop_writes_nothing_for_a_booking_in_progress_and_control_none_rewrites():
    f = rec._Fakes(history=rec._book_hist(attempt=1, qs="in_progress"))
    rec._run(f, [rec._row()])
    assert [w for w in f.written if "/book/" in w["subject_key"]] == []
    g = rec._Fakes(history=rec._book_hist(attempt=1, qs="none"))
    rec._run(g, [rec._row()])
    assert [w["subject_key"] for w in g.written] == [f"{rec._CAM}/book/1"]  # C3: same attempt


# ---- C3 --------------------------------------------------------------------------------------

def test_pending_rewrite_keeps_attempt_number():
    f = rec._Fakes(history=rec._book_hist(attempt=3, qs="pending"))
    rec._run(f, [rec._row()])
    assert [w["subject_key"] for w in f.written] == [f"{rec._CAM}/book/3"]
    assert _value(f.written[0], "recall.item")["attempt"] == 3


# ---- C4, W2, W3 (exhausted) --------------------------------------------------------------------

def test_exhausted_item_is_informational_with_fixed_text():
    f = rec._Fakes(history=rec._book_hist(attempt=attempts.MAX_ATTEMPTS, qs="failed"))
    rec._run(f, [rec._row()])
    (item,) = [w for w in f.written if "_exhausted/" in w["subject_key"]]
    assert item["subject_key"] == f"{rec._CAM}/book_exhausted/1"
    assert item["headline"] == attempts._INFORMATIONAL_HEADLINE
    assert "recommended_action" not in item
    assert _value(item, "recall.item") == {"campaign_id": rec._CAM, "item": "attempts_exhausted",
                                           "attempt": 1, "slot": "book"}
    assert item["detail"] == ("Five booking attempts for this recall did not complete. "
                              "Follow up with the dealer manually.")


# ---- W3 (parts expected) ----------------------------------------------------------------------

def test_parts_expected_detail_is_fixed_and_names_the_date():
    tomorrow = (rec._TODAY + datetime.timedelta(days=1)).isoformat()
    f = rec._Fakes()
    rec._run(f, [rec._row(remedy_from=tomorrow)])
    (item,) = f.written
    assert item["subject_key"].endswith("/parts_expected/1")
    assert item["detail"] == (f"Remedy parts are expected {tomorrow}. No booking is possible "
                              "until then; this item updates when the parts are available.")
    assert "recommended_action" not in item
    assert _value(item, "recall.item")["item"] == "parts_expected"


# ---- W1 --------------------------------------------------------------------------------------

class _LocErrFakes(rec._Fakes):
    def __init__(self):
        super().__init__()
        self.grounded_calls: list = []

    def locations_fn(self, fleet_id):
        raise RegistryReadError("ThrottlingException")

    def grounded_fn(self, vid):
        self.grounded_calls.append(vid)
        return False, 0


def test_locations_read_error_keeps_vehicle_id_and_counts():
    f = _LocErrFakes()
    stats = rec._run(f, [rec._row(do_not_drive=True)])          # p0: ground + book
    assert stats["grounding_read_errors"] == 1
    assert f.grounded_calls == [f"vid-{rec._VIN}"], "grounding must still be checked"
    keys = sorted(w["subject_key"].split("/")[1] for w in f.written)
    assert keys == ["book", "ground"]
    g = rec._Fakes()                                             # control: no error -> 0
    assert rec._run(g, [rec._row(do_not_drive=True)])["grounding_read_errors"] == 0


# ---- C1 (warranty) ----------------------------------------------------------------------------

def test_requires_review_row_without_coverage_group_is_written_with_fixed_sentence():
    row = {**war._row(), "coverage_group": None, "verdict_status": "requires_review"}
    stats, written = war._run([row])
    assert stats["rows_without_terms"] == 0 and len(written) == 1
    assert written[0]["disclosure_txt"] == "DMS decides coverage when the claim is filed."


def test_control_row_with_a_group_but_no_terms_is_refused():
    stats, written = war._run([war._row()], warranty_terms={})
    assert written == [] and stats["rows_without_terms"] == 1


# ---- review cycle 3 (agent C1, W1, W2; Suggestions) ------------------------------------------

class _FlagFakes(rec._Fakes):
    """Records the refresh_on_noop flag of every write (cycle 3 Suggestion A6)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.flags: list = []

    def write_finding(self, f, standing, refresh_on_noop=False):
        self.flags.append((f["subject_key"], refresh_on_noop))
        return super().write_finding(f, standing, refresh_on_noop=refresh_on_noop)


def test_ground_slot_exhausted_is_informational_with_fixed_text():
    """Cycle 3 agent C1 (A1, A2): the ground slot's exhausted item, through the loop on a p0 row,
    is never approvable (no recommended_action, so no sixth grounding) and keeps its fixed
    detail, including that the vehicle should not be driven."""
    f = _FlagFakes(history=rec._ground_hist(attempt=attempts.MAX_ATTEMPTS, qs="failed"))
    rec._run(f, [rec._row(do_not_drive=True)])
    (item,) = [w for w in f.written if "_exhausted/" in w["subject_key"]]
    assert item["subject_key"] == f"{rec._CAM}/ground_exhausted/1"
    assert item["headline"] == attempts._INFORMATIONAL_HEADLINE
    assert "recommended_action" not in item
    assert item["detail"] == ("Five grounding attempts for this recall did not complete. "
                              "Follow up manually; the vehicle should not be driven.")
    assert _value(item, "recall.item") == {"campaign_id": rec._CAM, "item": "attempts_exhausted",
                                           "attempt": 1, "slot": "ground"}
    assert all(flag for _, flag in f.flags), f.flags   # A6: informational items refresh too


def test_parts_expected_write_refreshes_on_noop():
    tomorrow = (rec._TODAY + datetime.timedelta(days=1)).isoformat()
    f = _FlagFakes()
    rec._run(f, [rec._row(remedy_from=tomorrow)])
    assert f.flags == [(f"{rec._CAM}/parts_expected/1", True)]


@pytest.mark.parametrize("kind", ["client_error", "botocore_error"])
def test_real_locations_reader_error_is_counted_not_raised(kind):
    """Cycle 3 agent W1 (A3) and fourth review W3 (A7): the REAL lookup_fleet_locations wraps
    both botocore error classes, an HTTP ClientError and a connection-level BotoCoreError, so
    the loop counts it and keeps the vehicle id instead of aborting."""
    from unittest import mock

    import boto3
    from botocore.exceptions import EndpointConnectionError
    from botocore.stub import Stubber

    from agents.fi_recall_warranty import selection

    stub = None
    if kind == "client_error":
        ddb = boto3.client("dynamodb", region_name="us-west-2", aws_access_key_id="x",
                           aws_secret_access_key="x")
        stub = Stubber(ddb)
        stub.add_client_error("query",
                              service_error_code="ProvisionedThroughputExceededException")
        stub.activate()
    else:
        ddb = mock.Mock(spec=["query"])
        ddb.query.side_effect = EndpointConnectionError(endpoint_url="https://ddb.invalid")

    class _RealLoc(_LocErrFakes):
        def locations_fn(self, fleet_id):
            return selection.lookup_fleet_locations(fleet_id, ddb_client=ddb,
                                                    fleet_locations_table="cms-test-locations")

    from botocore.exceptions import BotoCoreError, ClientError

    f = _RealLoc()
    try:
        stats = rec._run(f, [rec._row(do_not_drive=True)])
    except (ClientError, BotoCoreError) as exc:  # fail on this test's own line (G3-17)
        pytest.fail(f"a fleet-locations read error must not abort the run: {exc!r}")
    if stub is not None:
        stub.assert_no_pending_responses()
    else:  # the real reader made the call that raised
        assert ddb.query.call_count == 1
        assert ddb.query.call_args.kwargs["TableName"] == "cms-test-locations"
    assert stats["grounding_read_errors"] == 1
    assert f.grounded_calls == [f"vid-{rec._VIN}"]


def test_no_group_row_is_a_claim_candidate_with_its_action():
    """Cycle 3 agent W2 (A5): the no-coverage-group requires_review row is a claimable item."""
    row = {**war._row(), "coverage_group": None, "verdict_status": "requires_review"}
    _, written = war._run([row])
    (f,) = written
    assert f["headline"] == f"Warranty claim candidate: requires_review — {row['labor_op']}"
    assert f["recommended_action"]["action_kind"] == "submit_warranty_claim"


def test_term_ends_on_29_february():
    from agents.fi_recall_warranty.warranty import build_warranty_evidence
    terms = {"term_years": 3, "term_miles": 36_000}
    leap = {"term_years": 4, "term_miles": 36_000}

    def ends(t):
        ev = build_warranty_evidence({"in_service_date": "2024-02-29", "odometer_km": "100"},
                                     "rules", warranty_terms_row=t)
        # .get: an absent term_ends_on fails on the equality below (fourth review S1)
        return next(e["warranty.coverage"] for e in ev
                    if "warranty.coverage" in e).get("term_ends_on")

    assert ends(terms) == "2027-02-28"
    assert ends(leap) == "2028-02-29"
