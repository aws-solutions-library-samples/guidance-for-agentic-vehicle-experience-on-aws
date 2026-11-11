"""Queue tolerance in ``agents/tier2/adp_source.run_query`` (CMS issue
``2026-10-08-fi-lifecycle-refresh-athena-load``).

Athena's on-demand queue is shared regional capacity: at the start of the hour queries
wait minutes before they run (measured 2026-10-08: p95 241 s, max 908 s). ``run_query``
used to count those queued polls against ``max_polls`` (60 one-second polls), so a read
gave up while its query was still waiting, which is how cost B's live check 5.3 failed.

The rule pinned here: polls while the query is ``QUEUED`` do not spend ``max_polls``
(which bounds the time the query RUNS, as before); time spent queued has its own bound,
``max_queue_seconds``.
"""

from __future__ import annotations

import re

import pytest

import agents.tier2.adp_source as adp_source
from agents.tier2.adp_source import AthenaTimeoutError, run_query
from agents.tier2.tests.test_adp_source import (
    EXECUTION_ID,
    RESULTS_PATH,
    SAMPLE_SQL,
    WORKGROUP,
    _athena_client,
    _execution_status,
    _results_resp,
    _start_resp,
)


class _Clock:
    """Monotonic clock that advances ``step`` seconds per read."""

    def __init__(self, step: float) -> None:
        self.t = 1000.0
        self.step = step

    def __call__(self) -> float:
        self.t += self.step
        return self.t


def _stub(stubber, states: list[str], *, succeed: bool) -> None:
    stubber.add_response("start_query_execution", _start_resp())
    for state in states:
        stubber.add_response("get_query_execution", _execution_status(state))
    if succeed:
        stubber.add_response("get_query_execution", _execution_status("SUCCEEDED"))
        stubber.add_response("get_query_results", _results_resp(["a"], [["1"]]))


class _EndlessClient:
    """Athena stand-in whose query never finishes: ``states`` first, then ``forever``.

    A loop that should stop but doesn't fails this client's own assertion at
    ``limit`` polls instead of hanging (or of running out of Stubber responses).
    """

    def __init__(self, states: list[str], forever: str, limit: int = 2000) -> None:
        self.states, self.forever, self.limit, self.polls = list(states), forever, limit, 0

    def start_query_execution(self, **_kw):
        return {"QueryExecutionId": EXECUTION_ID}

    def get_query_execution(self, **_kw):
        self.polls += 1
        assert self.polls <= self.limit, f"run_query still polling after {self.limit} polls"
        state = self.states.pop(0) if self.states else self.forever
        return {"QueryExecution": {"QueryExecutionId": EXECUTION_ID, "Status": {"State": state}}}


def test_queued_polls_do_not_spend_the_running_budget():
    """10 QUEUED polls then 2 RUNNING then SUCCEEDED, with max_polls=3: rows come back."""
    client, stubber = _athena_client()
    _stub(stubber, ["QUEUED"] * 10 + ["RUNNING"] * 2, succeed=True)
    with stubber:
        rows = run_query(SAMPLE_SQL, athena_client=client, workgroup=WORKGROUP,
                         results_s3_path=RESULTS_PATH, max_polls=3, poll_interval=0,
                         max_queue_seconds=240, clock=_Clock(1.0))
    assert rows == [{"a": "1"}]
    stubber.assert_no_pending_responses()


def test_running_polls_are_still_bounded_by_max_polls_after_a_queue():
    """After the queue, exactly max_polls RUNNING polls are allowed, as before."""
    client = _EndlessClient(["QUEUED"] * 5, forever="RUNNING")
    with pytest.raises(AthenaTimeoutError) as exc:
        run_query(SAMPLE_SQL, athena_client=client, workgroup=WORKGROUP,
                  results_s3_path=RESULTS_PATH, max_polls=3, poll_interval=0,
                  max_queue_seconds=240, clock=_Clock(1.0))
    assert "3 polls" in str(exc.value) and EXECUTION_ID in str(exc.value)
    assert client.polls == 5 + 3


def test_time_in_the_queue_is_bounded_by_max_queue_seconds():
    """A query QUEUED past max_queue_seconds raises, naming the queue and the id.

    The clock advances 10 s per read: the start reads it once (t0), each poll once.
    Polls at +10, +20, +30 are within 30 s; the poll at +40 is past it and raises,
    so exactly four polls are made.
    """
    client = _EndlessClient([], forever="QUEUED")
    with pytest.raises(AthenaTimeoutError) as exc:
        run_query(SAMPLE_SQL, athena_client=client, workgroup=WORKGROUP,
                  results_s3_path=RESULTS_PATH, max_polls=60, poll_interval=0,
                  max_queue_seconds=30, clock=_Clock(10.0))
    message = str(exc.value)
    assert "QUEUED" in message and EXECUTION_ID in message
    assert client.polls == 4


def test_default_queue_budget_is_240_seconds():
    assert adp_source._DEFAULT_MAX_QUEUE_SECONDS == 240.0
    assert adp_source._DEFAULT_MAX_POLLS == 60


class _FakeTime:
    """Stands in for the ``time`` module inside adp_source: no real sleeping."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept = 0.0

    def sleep(self, seconds: float) -> None:
        self.slept += seconds
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def test_defaults_wait_out_a_queue_longer_than_the_poll_budget(monkeypatch):
    """With every default (60 one-second polls), a 100 s queue no longer fails the read.

    This is the 5.3 item 4 failure: one query SUCCEEDED after 68 s queued, the other
    was still QUEUED after 176 s, and both reads gave up at 60 polls.
    """
    fake = _FakeTime()
    monkeypatch.setattr(adp_source, "time", fake)
    client, stubber = _athena_client()
    _stub(stubber, ["QUEUED"] * 100 + ["RUNNING"] * 5, succeed=True)
    with stubber:
        rows = run_query(SAMPLE_SQL, athena_client=client, workgroup=WORKGROUP,
                         results_s3_path=RESULTS_PATH)
    assert rows == [{"a": "1"}]
    assert fake.slept == pytest.approx(106.0)   # 105 non-terminal polls + the SUCCEEDED one
    stubber.assert_no_pending_responses()


def test_defaults_still_give_up_past_the_queue_budget(monkeypatch):
    """With defaults, a query still QUEUED after 240 s raises (the run does not hang)."""
    fake = _FakeTime()
    monkeypatch.setattr(adp_source, "time", fake)
    client = _EndlessClient([], forever="QUEUED")
    with pytest.raises(AthenaTimeoutError):
        run_query(SAMPLE_SQL, athena_client=client, workgroup=WORKGROUP,
                  results_s3_path=RESULTS_PATH)
    assert client.polls == 241   # 1 s per poll; the first poll past 240 s raises


def test_live_check_reader_with_defaults_waits_out_the_queue(monkeypatch):
    """The 5.3 live check called ``fetch_sell_timing_inputs`` with no ``max_polls``."""
    from agents.fi_cost.sell_timing import fetch_sell_timing_inputs

    fake = _FakeTime()
    monkeypatch.setattr(adp_source, "time", fake)
    client, stubber = _athena_client()
    _stub(stubber, ["QUEUED"] * 100, succeed=True)
    with stubber:
        rows = fetch_sell_timing_inputs(client, WORKGROUP, RESULTS_PATH,
                                        year_month="2026-09", stage="staging")
    assert rows == [{"a": "1"}]
    stubber.assert_no_pending_responses()



def test_a_long_queue_wait_is_logged_with_the_execution_id(caplog):
    """The post-deploy live check reads queue waits from the logs as well as Athena."""
    import logging

    client, stubber = _athena_client()
    _stub(stubber, ["QUEUED"] * 12, succeed=True)
    with caplog.at_level(logging.INFO, logger="agents.tier2.adp_source"), stubber:
        run_query(SAMPLE_SQL, athena_client=client, workgroup=WORKGROUP,
                  results_s3_path=RESULTS_PATH, poll_interval=0, clock=_Clock(1.0))
    lines = [r.getMessage() for r in caplog.records if "Athena queue" in r.getMessage()]
    assert len(lines) == 1, lines
    # The whole message, so no query text, VIN or result data can be added to it
    # (CloudWatch Logs readers are wider than the lake's; security review S1).
    assert re.fullmatch(
        rf"adp_source: query waited \d+ s in the Athena queue \(execution_id={EXECUTION_ID}\)",
        lines[0],
    ), lines[0]



# ---------------------------------------------------------------------------
# General review cycle 1: the cost agent's reads keep spec D13 (queue for the
# run's budget), CANCELLED is terminal, any other state spends max_polls.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("queued_seconds", [300, 450])
def test_cost_agent_read_may_queue_for_its_whole_budget(monkeypatch, queued_seconds):
    """agent.py passes max_polls = the run's remaining seconds (here 570 of a 600 s run).

    Spec D13: such a read may wait in the queue for that whole budget; the agent's
    ``_DeadlineAthena`` ends it at the run's deadline. A fixed 240 s queue bound raised
    ``AthenaTimeoutError`` with 359 s of the run left (general review cycle 1, W1).
    """
    from agents.fi_cost.sell_timing import fetch_sell_timing_inputs

    fake = _FakeTime()
    monkeypatch.setattr(adp_source, "time", fake)
    client, stubber = _athena_client()
    _stub(stubber, ["QUEUED"] * queued_seconds, succeed=True)
    with stubber:
        rows = fetch_sell_timing_inputs(client, WORKGROUP, RESULTS_PATH,
                                        year_month="2026-09", stage="staging", max_polls=570)
    assert rows == [{"a": "1"}]
    stubber.assert_no_pending_responses()


def test_queue_bound_is_the_larger_of_240_s_and_the_running_budget(monkeypatch):
    fake = _FakeTime()
    monkeypatch.setattr(adp_source, "time", fake)
    client = _EndlessClient([], forever="QUEUED")
    with pytest.raises(AthenaTimeoutError) as exc:
        run_query(SAMPLE_SQL, athena_client=client, workgroup=WORKGROUP,
                  results_s3_path=RESULTS_PATH, max_polls=300, poll_interval=1.0)
    assert client.polls == 301 and "max_queue_seconds=300" in str(exc.value)


def test_cancelled_is_terminal_on_the_first_poll():
    from agents.tier2.adp_source import AthenaCursorError

    client = _EndlessClient([], forever="CANCELLED")
    with pytest.raises(AthenaCursorError):
        run_query(SAMPLE_SQL, athena_client=client, workgroup=WORKGROUP,
                  results_s3_path=RESULTS_PATH, poll_interval=0, clock=_Clock(1.0))
    assert client.polls == 1


def test_a_state_other_than_queued_spends_max_polls():
    """Fail safe: an unexpected non-terminal state is treated as running, never as queued."""
    client = _EndlessClient([], forever="SOMETHING_NEW")
    with pytest.raises(AthenaTimeoutError) as exc:
        run_query(SAMPLE_SQL, athena_client=client, workgroup=WORKGROUP,
                  results_s3_path=RESULTS_PATH, max_polls=3, poll_interval=0,
                  clock=_Clock(1.0))
    assert client.polls == 3 and "3 polls" in str(exc.value)



# ---------------------------------------------------------------------------
# General review cycle 2 suggestions: every cost read forwards its budget, and
# the budget is in seconds only while the two poll intervals agree.
# ---------------------------------------------------------------------------

def _cost_reads():
    import datetime

    from agents.fi_cost import evidence, selection, sell_timing

    vin = "1HGCM82633A004352"
    return [
        ("find_latest_complete_month", selection, lambda: selection.find_latest_complete_month(
            "ath", "wg", "s3://r/", now=datetime.datetime(2026, 10, 8, tzinfo=datetime.timezone.utc),
            max_polls=570)),
        ("select_anomalies", selection, lambda: selection.select_anomalies(
            "ath", "wg", "s3://r/", 50, "2026-09", max_polls=570)),
        ("gather_evidence", evidence, lambda: evidence.gather_evidence(
            [vin], "2026-09", "ath", "wg", "s3://r/", max_polls=570)),
        ("fetch_sell_timing_inputs", sell_timing, lambda: sell_timing.fetch_sell_timing_inputs(
            "ath", "wg", "s3://r/", year_month="2026-09", max_polls=570)),
    ]


@pytest.mark.parametrize("index", range(4), ids=[
    "find_latest_complete_month", "select_anomalies", "gather_evidence", "fetch_sell_timing_inputs"])
def test_each_cost_read_forwards_the_runs_budget_to_run_query(monkeypatch, index):
    """agent.py passes max_polls = remaining seconds; run_query's queue bound grows with it
    (spec D13). A reader that dropped the forwarding would fall back to 240 s queued."""
    name, module, call = _cost_reads()[index]
    seen: list[dict] = []

    def _record(*args, **kwargs):
        seen.append(kwargs)
        return []

    monkeypatch.setattr(module, "run_query", _record)
    call()
    assert seen, f"{name} made no Athena read"
    assert all(kw.get("max_polls") == 570 for kw in seen), (name, seen)


def test_cost_agent_poll_interval_matches_run_query_default():
    """The agent turns seconds into polls with its own interval; its readers poll at
    run_query's default. The queue bound equals the run budget only if they agree."""
    from agents.fi_cost import agent

    assert agent.ATHENA_POLL_INTERVAL_S == adp_source._DEFAULT_POLL_INTERVAL


def test_queue_bound_counts_seconds_not_polls(monkeypatch):
    """max_polls=200 at a 2 s interval is a 400 s budget, so the query may queue 400 s."""
    fake = _FakeTime()
    monkeypatch.setattr(adp_source, "time", fake)
    client = _EndlessClient([], forever="QUEUED")
    with pytest.raises(AthenaTimeoutError) as exc:
        run_query(SAMPLE_SQL, athena_client=client, workgroup=WORKGROUP,
                  results_s3_path=RESULTS_PATH, max_polls=200, poll_interval=2.0)
    assert client.polls == 201 and "max_queue_seconds=400" in str(exc.value)
