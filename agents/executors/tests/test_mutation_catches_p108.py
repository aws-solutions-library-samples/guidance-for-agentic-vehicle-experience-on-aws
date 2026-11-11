"""Tests for executor mutants that survived prompt 100's sweep and its follow-up re-run.

Prompt 108 (2026-10-24) re-ran the 457 survivors of prompt 100's overnight sweep
against AVX main ``93df8252`` and ranked what still survived by path: grounding,
booking, warranty claim, move, refusal and fail-closed branches first. Each test
here pins the PROPERTY one or more of those survivors broke, and was proven by
applying the exact mutation and watching the test fail on its own assertion
(battery and results: the prompt 108 record in
``~/.kiro/portfolio/notes/2026-10-24-executor-sweep2/``).

Shapes the survivors had in common, and what the tests below pin instead:

* **The retry loops.** Every executor copies the same loop. No test counted the
  attempts or the sleeps on exhaustion, so a mutant that slept after the last
  attempt, never slept, used the wrong backoff step, started at attempt 1, or
  dropped the "no status" marker on a network failure survived on every copy.
  ``_assert_exhausted_with_backoff`` pins attempts == ``retry_count``, exactly
  ``retry_count - 1`` sleeps at ``[base, 2*base, 4*base]`` (jitter pinned to
  the window), and the recorded status. The policy has FOUR attempts, not the
  default three, so a mutant that ignores the injected policy shows up too.
* **Local refusals of unusable parameters.** Removing a ``return None`` or
  flipping ``or``/``and`` in a guard made the executor either POST a blank value
  or crash into ``@never_raises``. Both still come back as ``failed``, so tests
  that checked only the status passed. These assert the specific
  ``MissingDispatchParameter`` class AND that nothing was sent.
* **The success range.** ``< 300`` -> ``< 301`` survived on every path because
  no test sent a 3xx. A 300 is recorded as an error, never as executed.
* **Refusal vs failure.** A 4xx that means "our request or our plumbing" is
  ``failed`` (escalates); only a deliberate refusal is ``declined``.

Calls that must not raise are made inside ``try`` so a mutant that makes them
raise fails on this file's own assertion, not on a stray exception.
"""
from __future__ import annotations

import json
import sys
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional
from unittest.mock import MagicMock, patch

import pytest
import requests
import responses

from agents.executors.base import ActionStatus, ExecuteResult, FailureKind, RetryPolicy

# ---------------------------------------------------------------------------
# Fixtures shared by every path
# ---------------------------------------------------------------------------

_VIN = "MRDVEH00000000015"
_UUID = "deadbeef-1234-4000-8000-abcdef000108"
_ACTION_ID = f"AVX-ACT-{_UUID}"
_FINDING_ID = f"FND-{_UUID}"
_RECALL_ID = "MRD-2024-001"
_DEALER = "dealer-seattle"

_CMS = "https://cms-staging.test"
_DMS = "http://dms-staging.test"

_CLAIMS: dict[str, Any] = {
    "sub": "op-sub-p108-0001",
    "cognito:groups": ["fleet-operator"],
    "custom:fleetIds": "fleet-meridian-01",
    "token": "Bearer.p108.jwt",
}

#: Four attempts, not the default three: a mutant that reloads the policy from SSM
#: (and so gets the default) shows up as the wrong attempt count.
_ATTEMPTS = 4
#: One sleep between each pair of attempts, none after the last, each the full
#: backoff window ``min(cap, base * 2**i)`` for i = 0, 1, 2 (jitter pinned to 1.0).
_EXPECTED_SLEEPS = [0.1, 0.2, 0.4]

_CMS_PATHS = ("cost", "ground", "move")

_URLS = {
    "cost": f"{_CMS}/api/v1/fleet-intelligence/cost-actions",
    "ground": f"{_CMS}/api/v1/fleet-intelligence/groundings",
    "move": f"{_CMS}/api/v1/fleet-intelligence/moves",
    "recall_booking": f"{_DMS}/api/dms/fleet/repair-orders",
    "fleet_service": f"{_DMS}/api/dms/fleet/repair-orders",
    "warranty": f"{_DMS}/api/dms/fleet/warranty-claims",
}

_KINDS = {
    "cost": "apply_cost_action",
    "ground": "ground_vehicle",
    "move": "move_vehicles",
    "recall_booking": "book_recall_service",
    "fleet_service": "book_fleet_service",
    "warranty": "submit_warranty_claim",
}


def _parameters(path: str) -> dict[str, Any]:
    """A fresh, complete and valid parameter set for ``path``."""
    return {
        "cost": {
            "vehicle_vin": _VIN,
            "cost_action": "schedule_inspection",
            "cause": "repeat_repair",
            "month": "2026-10",
        },
        "ground": {"vehicle_vin": _VIN, "recall_id": _RECALL_ID},
        "move": {
            "fleet_id": "fleet-meridian-01",
            "from_location_id": "LAX-1",
            "to_location_id": "SFO-2",
            "forecast_week": "2026-W44",
            "vehicle_vins": [_VIN],
            "transfer_cost_usd": 120.0,
            "predicted_on_rent_hours_gain": 10.0,
            "predicted_turnaways_avoided": 2.0,
        },
        "recall_booking": {"vehicle_vin": _VIN, "dealer_id": _DEALER, "recall_id": _RECALL_ID},
        "fleet_service": {
            "vehicle_vin": _VIN,
            "dealer_id": _DEALER,
            "needs": [{"labor_op": "LO-EV-TIRE-ROT", "need_id": "NEED-0001"}],
        },
        "warranty": {
            "vehicle_vin": _VIN,
            "ro_id": "ro-p108-7001",
            "line_no": "3",
            "dealer_id": _DEALER,
            "labor_op": "LO-EV-BRAKE-001",
        },
    }[path]


def _action(path: str, **overrides: Any) -> dict[str, Any]:
    action: dict[str, Any] = {
        "action_id": _ACTION_ID,
        "finding_id": _FINDING_ID,
        "action_kind": _KINDS[path],
        "target_system": "cms" if path in _CMS_PATHS else "dms",
        "parameters": _parameters(path),
    }
    action.update(overrides)
    return action


def _policy() -> RetryPolicy:
    return RetryPolicy(retry_count=_ATTEMPTS, backoff_base_ms=100, backoff_cap_ms=10_000)


def _executor(path: str, sleeps: Optional[list] = None) -> Any:
    sleep_fn = sleeps.append if sleeps is not None else (lambda _s: None)
    if path in _CMS_PATHS:
        from agents.executors.cms import CmsExecutor

        return CmsExecutor(sleep_fn=sleep_fn, retry_policy=_policy())
    from agents.executors.dms import DmsExecutor

    ssm = MagicMock()
    ssm.get_parameter.side_effect = Exception("stubbed SSM")
    return DmsExecutor(
        enqueue_ios_owner=MagicMock(),
        events_client=MagicMock(),
        cloudwatch_client=MagicMock(),
        ssm_client=ssm,
        sleep_fn=sleep_fn,
        retry_policy=_policy(),
    )


@contextmanager
def _http() -> Iterator[responses.RequestsMock]:
    """Every request is intercepted; an unregistered URL raises ConnectionError."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock


@contextmanager
def _full_window_jitter() -> Iterator[None]:
    """``sleep_ms`` draws ``window * random()``; 1.0 makes each sleep the window."""
    with patch("agents.executors.base.random.random", return_value=1.0):
        yield


def _dispatch(path: str, action: dict[str, Any], *, claims: Optional[dict] = None,
              sleeps: Optional[list] = None) -> ExecuteResult:
    env = {"VSA_CMS_API_ENDPOINT": _CMS, "VSA_DMS_API_ENDPOINT": _DMS}
    with patch.dict("os.environ", env):
        return _executor(path, sleeps).dispatch(action, _CLAIMS if claims is None else claims)


def _describe(result: ExecuteResult) -> str:
    return (f"status={result.status} kind={result.failure_kind} class={result.last_error_class} "
            f"error_status={result.last_error_status} retry_count={result.retry_count}")


def _assert_refused_locally(result: ExecuteResult, mock: responses.RequestsMock, label: str,
                            error_class: str = "MissingDispatchParameter") -> None:
    assert result.status == ActionStatus.FAILED, f"{label}: {_describe(result)}"
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR, f"{label}: {_describe(result)}"
    assert result.last_error_class == error_class, f"{label}: {_describe(result)}"
    assert len(mock.calls) == 0, f"{label}: nothing may be sent, got {len(mock.calls)} request(s)"


def _assert_exhausted_with_backoff(path: str, mode: str) -> None:
    """Every attempt fails transiently: exactly ``_ATTEMPTS`` requests, a sleep
    between each pair and none after the last, and the right last error."""
    sleeps: list[float] = []
    with _http() as mock, _full_window_jitter():
        if mode == "5xx_then_network":
            # responses serves registrations in order and repeats the last one
            mock.add(responses.POST, _URLS[path], status=503, json={"error": "busy"})
        if mode in ("network", "5xx_then_network"):
            mock.add(responses.POST, _URLS[path],
                     body=requests.exceptions.ConnectionError("p108 transient"))
        else:
            mock.add(responses.POST, _URLS[path], status=503, json={"error": "busy"})
        result = _dispatch(path, _action(path), sleeps=sleeps)
        sent = len(mock.calls)

    assert result.status == ActionStatus.FAILED, _describe(result)
    assert result.failure_kind == FailureKind.EXECUTOR_EXHAUSTED_RETRIES, _describe(result)
    assert sent == _ATTEMPTS, f"expected {_ATTEMPTS} attempts, sent {sent}"
    assert result.retry_count == _ATTEMPTS, _describe(result)
    assert sleeps == pytest.approx(_EXPECTED_SLEEPS), f"sleeps {sleeps[:8]} (n={len(sleeps)})"
    if mode in ("network", "5xx_then_network"):
        # after a 503, a network failure must clear the status again
        assert result.last_error_status is None, _describe(result)
        assert result.last_error_class == "ConnectionError", _describe(result)
    else:
        assert result.last_error_status == 503, _describe(result)
        assert result.last_error_class == "HTTPError", _describe(result)


# ---------------------------------------------------------------------------
# Cross-path properties
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["cost", "recall_booking", "fleet_service"])
def test_a_non_transient_exception_is_terminal_after_one_attempt(path):
    """The three retry-loop sites prompt 100 left unpinned: cms.py ``dispatch``
    (~518), dms.py ``dispatch`` (~407) and ``_dispatch_fleet_service`` (~579).
    ``if is_transient_exception(exc)`` forced True retries a non-transient error;
    removing the terminal ``return`` reads an unbound response."""
    with _http() as mock:
        mock.add(responses.POST, _URLS[path], body=ValueError("p108 non-transient"))
        result = _dispatch(path, _action(path))
        sent = len(mock.calls)

    assert result.status == ActionStatus.FAILED, _describe(result)
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR, _describe(result)
    assert result.last_error_class == "ValueError", _describe(result)
    assert result.retry_count == 0, _describe(result)
    assert sent == 1, f"a non-transient error must not be retried; sent {sent}"


_HTTP_PATHS = ["cost", "ground", "move", "recall_booking", "warranty", "fleet_service"]


@pytest.mark.parametrize("mode", ["network", "5xx", "5xx_then_network"])
@pytest.mark.parametrize("path", _HTTP_PATHS)
def test_every_retry_loop_exhausts_the_policy_with_backoff(path, mode):
    """Each HTTP executor's copy of the retry loop: cms ``dispatch`` (cost action,
    ~494-551), ``_dispatch_ground_vehicle`` (~608-646), ``_move_attempts``
    (~745-786); dms ``dispatch`` (recall booking, ~373-493),
    ``_dispatch_warranty_claim`` (~661-705) and ``_dispatch_fleet_service``
    (~553-618). Pins the attempt start and increments, the attempt half of both
    sleep guards, the backoff step, ``last_status = None`` on a network error that
    follows a 503, the injected sleep function and the injected policy. The deadline
    half is pinned by the next test."""
    _assert_exhausted_with_backoff(path, mode)


def _clock_for(module: str, values: list[float]):
    """A ``time.monotonic`` that answers ``module``'s calls from ``values`` (then the
    last value forever) and everyone else's from the real clock."""
    real = time.monotonic
    queue = list(values)

    def fake() -> float:
        if sys._getframe(1).f_globals.get("__name__") == module:
            return queue.pop(0) if len(queue) > 1 else queue[0]
        return real()
    return fake


_MODULES = {"cost": "agents.executors.cms", "ground": "agents.executors.cms",
            "move": "agents.executors.cms", "recall_booking": "agents.executors.dms",
            "warranty": "agents.executors.dms", "fleet_service": "agents.executors.dms"}


@pytest.mark.parametrize("mode", ["network", "5xx"])
@pytest.mark.parametrize("path", _HTTP_PATHS)
def test_every_retry_loop_stops_at_the_deadline_without_sleeping_past_it(path, mode):
    """The deadline half of both sleep guards: when the budget has run out after an
    attempt, the loop neither sleeps nor tries again. The executor's clock reads 0
    (deadline 25 s ahead), 0 (first attempt), then 100: exactly one attempt and no
    sleep, although three attempts remain."""
    sleeps: list[float] = []
    with _http() as mock, patch("time.monotonic", _clock_for(_MODULES[path], [0, 0, 100])):
        if mode == "network":
            mock.add(responses.POST, _URLS[path],
                     body=requests.exceptions.ConnectionError("p108 transient"))
        else:
            mock.add(responses.POST, _URLS[path], status=503, json={})
        result = _dispatch(path, _action(path), sleeps=sleeps)
        sent = len(mock.calls)

    assert result.failure_kind == FailureKind.EXECUTOR_EXHAUSTED_RETRIES, _describe(result)
    assert (sent, result.retry_count) == (1, 1), f"sent={sent} {_describe(result)}"
    assert sleeps == [], f"no sleep once the deadline has passed; slept {sleeps}"


#: 0 to set the deadline, then 24 s (inside the 25 s budget, past the ~6.1 s per-attempt
#: timeout) for 40 reads, then past the deadline. Four attempts take at most ~9 reads.
_INSIDE_BUDGET_READS = 40
_INSIDE_BUDGET_CLOCK = [0] + [24] * _INSIDE_BUDGET_READS + [100]


@pytest.mark.parametrize("path", _HTTP_PATHS)
def test_every_retry_loop_keeps_trying_inside_the_25_s_budget(path):
    """The deadline's size: it is the 25 s synchronous budget (``deadline_s``), not the
    ~6.1 s per-attempt timeout. The executor's clock reads 0 (deadline set), then 24 s:
    all four attempts and all three sleeps still happen. The clock jumps to 100 after
    ``_INSIDE_BUDGET_READS`` reads, so a loop that stops counting attempts fails here
    instead of spinning forever on a clock that never reaches the deadline."""
    sleeps: list[float] = []
    with _http() as mock, _full_window_jitter(), \
            patch("time.monotonic", _clock_for(_MODULES[path], _INSIDE_BUDGET_CLOCK)):
        mock.add(responses.POST, _URLS[path],
                 body=requests.exceptions.ConnectionError("p108 transient"))
        result = _dispatch(path, _action(path), sleeps=sleeps)
        sent = len(mock.calls)

    assert result.failure_kind == FailureKind.EXECUTOR_EXHAUSTED_RETRIES, _describe(result)
    assert (sent, result.retry_count) == (_ATTEMPTS, _ATTEMPTS), f"sent={sent} {_describe(result)}"
    assert sleeps == pytest.approx(_EXPECTED_SLEEPS), f"sleeps {sleeps}"


@pytest.mark.parametrize("path", ["cost", "ground", "recall_booking", "warranty", "fleet_service"])
def test_a_300_response_is_an_error_not_a_success(path):
    """The success range is 2xx only. Pins ``200 <= status < 300`` against
    ``< 301`` on each path (the move path's 3xx branch is pinned separately).

    T1.2: the ``cost`` and ``ground`` (CMS) paths now also have an explicit 3xx
    guard (``allow_redirects=False`` + UnexpectedRedirect terminal error) added
    before the success check, so a 300 response produces ``UnexpectedRedirect``
    rather than falling through to ``classify_status`` → ``HTTPError``.
    DMS paths (recall_booking, warranty, fleet_service) are unchanged.
    """
    # T1.2: CMS paths return UnexpectedRedirect for any 3xx; DMS paths still
    # return HTTPError (classify_status → client_error for a 3xx).
    expected_class = "UnexpectedRedirect" if path in ("cost", "ground") else "HTTPError"
    with _http() as mock:
        mock.add(responses.POST, _URLS[path], status=300,
                 json={"ro_id": "RO-P108-300", "thread_id": "thr-300"})
        result = _dispatch(path, _action(path))
        sent = len(mock.calls)

    assert result.status == ActionStatus.FAILED, _describe(result)
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR, _describe(result)
    assert result.last_error_status == 300, _describe(result)
    assert result.last_error_class == expected_class, _describe(result)
    assert sent == 1


@pytest.mark.parametrize("path", ["cost", "ground", "recall_booking", "warranty", "fleet_service"])
def test_parameters_that_are_not_an_object_are_refused_without_sending(path):
    """Each body builder returns None for non-object parameters and the executor
    refuses locally. Pins the non-object returns in ``cost_action_parameter_error``
    (~321), ``_grounding_body`` (~221), dms ``_request_body`` (~113),
    ``_warranty_claim_body`` (~233) and ``_fleet_service_body`` (~162), and the
    ``body is None`` guards after them."""
    with _http() as mock:
        result = _dispatch(path, _action(path, parameters=None))
        _assert_refused_locally(result, mock, path)


@pytest.mark.parametrize("path", ["warranty", "fleet_service"])
def test_a_missing_token_is_a_local_terminal_error(path):
    """No bearer token: refuse locally, send nothing (warranty ~649, fleet ~536)."""
    with _http() as mock:
        result = _dispatch(path, _action(path), claims={"sub": "op-sub-p108-0001"})
        _assert_refused_locally(result, mock, path, error_class="MissingJwt")


@pytest.mark.parametrize("path", ["cost", "recall_booking", "adp"])
def test_without_an_injected_policy_the_executor_reads_it_from_ssm(path):
    """The registry builds every executor without a ``retry_policy``, so production
    always takes the lazy SSM read. Every other test injects one. Pins the
    constructor default and the ``_retry_policy`` cache check (cms ~412/~426,
    dms ~283/~319, adp ~307/~318): the policy read from SSM (two attempts) is used."""
    from agents.executors.base import reset_policy_cache

    ssm = MagicMock()
    ssm.get_parameter.return_value = {"Parameter": {"Value": json.dumps(
        {"retry_count": 2, "backoff_base_ms": 0, "backoff_cap_ms": 0})}}
    reset_policy_cache()
    try:
        if path == "adp":
            from agents.executors.adp import AdpExecutor

            client = MagicMock()
            client.invoke.side_effect = _writer_503
            executor = AdpExecutor(thread_writer_arn=_ADP_ARN, lambda_client=client,
                                   sleep_fn=lambda _s: None)
            with patch("agents.executors.base._get_ssm", return_value=ssm):
                result = executor.dispatch(_adp_action(), _CLAIMS)
            sent = client.invoke.call_count
        else:
            if path == "cost":
                from agents.executors.cms import CmsExecutor

                executor = CmsExecutor(ssm_client=ssm, sleep_fn=lambda _s: None)
            else:
                from agents.executors.dms import DmsExecutor

                executor = DmsExecutor(ssm_client=ssm, sleep_fn=lambda _s: None)
            with _http() as mock:
                mock.add(responses.POST, _URLS[path], status=503, json={})
                with patch.dict("os.environ", {"VSA_CMS_API_ENDPOINT": _CMS,
                                               "VSA_DMS_API_ENDPOINT": _DMS}):
                    result = executor.dispatch(_action(path), _CLAIMS)
                sent = len(mock.calls)
    finally:
        reset_policy_cache()

    from agents.executors.base import retry_policy_param

    target = {"cost": "cms", "recall_booking": "dms", "adp": "adp"}[path]
    assert result.failure_kind == FailureKind.EXECUTOR_EXHAUSTED_RETRIES, _describe(result)
    assert sent == 2, f"the SSM policy says 2 attempts; sent {sent}"
    names = [c.kwargs.get("Name") for c in ssm.get_parameter.call_args_list]
    assert names == [retry_policy_param(target)], names


# ---------------------------------------------------------------------------
# CMS: grounding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label, parameters", [
    ("blank_vin", {"vehicle_vin": "   ", "recall_id": _RECALL_ID}),
    ("empty_fault_codes_and_no_recall", {"vehicle_vin": _VIN, "fault_codes": []}),
])
def test_ground_vehicle_refuses_unusable_parameters_without_sending(label, parameters):
    """``_grounding_body`` returns None and ``_dispatch_ground_vehicle`` refuses
    locally. Pins the VIN guard (~223-224), the fault-form branch (~248, ~256)
    and the ``body is None`` guard (~589-590)."""
    with _http() as mock:
        result = _dispatch("ground", _action("ground", parameters=parameters))
        _assert_refused_locally(result, mock, label)


@pytest.mark.parametrize("label, action_id", [("missing", None), ("blank", "   ")])
def test_ground_vehicle_without_an_action_id_still_sends_and_leaves_it_out(label, action_id):
    """``actionId`` is optional provenance: a missing or blank one is left out of the
    body, never sent blank and never a crash (pins ~240)."""
    action = _action("ground")
    if action_id is None:
        del action["action_id"]
    else:
        action["action_id"] = action_id
    with _http() as mock:
        mock.add(responses.POST, _URLS["ground"], status=201, json={})
        result = _dispatch("ground", action)
        bodies = [json.loads(c.request.body) for c in mock.calls]

    assert result.status == ActionStatus.EXECUTED, f"{label}: {_describe(result)}"
    assert bodies == [{"vin": _VIN, "findingId": _FINDING_ID, "campaignId": _RECALL_ID}], bodies


def test_ground_vehicle_403_fails_rather_than_declining():
    """A 403 is our token or plumbing: ``failed`` and escalated, not a CMS refusal
    (pins the ``client_error`` return, ~646)."""
    with _http() as mock:
        mock.add(responses.POST, _URLS["ground"], status=403, json={"error": "forbidden"})
        result = _dispatch("ground", _action("ground"))
        sent = len(mock.calls)

    assert result.status == ActionStatus.FAILED, _describe(result)
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR, _describe(result)
    assert result.last_error_status == 403, _describe(result)
    assert sent == 1


# ---------------------------------------------------------------------------
# CMS: cost action
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("month", ["2026-13", "2026-1", 202610])
def test_cost_action_parameter_error_names_a_bad_month(month):
    """``cost_action_parameter_error`` is what the approve route uses to answer 400
    before a decision is written, so it must name a bad month itself (~330-331).
    The executor's later re-check in ``_request_body`` can't stop a decision that
    then fails for good."""
    from agents.executors.cms import cost_action_parameter_error

    err = cost_action_parameter_error({**_parameters("cost"), "month": month})
    assert err is not None and "month" in err, f"month={month!r}: {err!r}"


# ---------------------------------------------------------------------------
# CMS: moves
# ---------------------------------------------------------------------------


def _blank_action_id(a: dict) -> None:
    a["action_id"] = "   "


def _drop_finding_id(a: dict) -> None:
    del a["finding_id"]


def _no_parameters(a: dict) -> None:
    a["parameters"] = None


def _drop_param(key: str):
    def _apply(a: dict) -> None:
        del a["parameters"][key]
    return _apply


def _set_param(key: str, value: Any):
    def _apply(a: dict) -> None:
        a["parameters"][key] = value
    return _apply


@pytest.mark.parametrize("label, mutate", [
    ("parameters_not_an_object", _no_parameters),
    ("missing_forecast_week", _drop_param("forecast_week")),
    ("blank_fleet_id", _set_param("fleet_id", "   ")),
    ("blank_vin_in_list", _set_param("vehicle_vins", [_VIN, "   "])),
    ("boolean_cost", _set_param("transfer_cost_usd", True)),
    ("text_cost", _set_param("transfer_cost_usd", "abc")),
    ("nan_cost", _set_param("transfer_cost_usd", float("nan"))),
    ("infinite_cost", _set_param("transfer_cost_usd", float("inf"))),
    ("blank_action_id", _blank_action_id),
    ("missing_finding_id", _drop_finding_id),
])
def test_move_body_refuses_unusable_values(label, mutate):
    """``_move_body`` returns None (never raises) for each unusable input, so the
    move is refused locally. Pins ``_move_body`` (~279-297) and ``_json_number``
    (~262-267)."""
    from agents.executors.cms import _move_body

    action = _action("move")
    mutate(action)
    try:
        body = _move_body(action)
    except Exception as exc:  # noqa: BLE001 - a raise is the defect under test
        raise AssertionError(f"{label}: _move_body must return None, raised "
                             f"{type(exc).__name__}: {exc}") from exc
    assert body is None, f"{label}: expected None, got {body!r}"


def test_the_signed_move_path_refuses_without_usable_credentials_or_endpoint():
    """A rule-approved move is signed with the running role's frozen credentials; with
    none it refuses (``ValueError``, nothing sent). Every signed-path test injects
    ``credentials_fn``, so ``_lambda_credentials`` itself (~157-164) was unpinned. An
    endpoint that isn't CMS's execute-api URL, or has anything after the stage segment,
    is refused with ``ValueError`` too (``_cms_internal_endpoint`` ~148-150, ``fullmatch``)."""
    from agents.executors import cms

    frozen = object()
    creds = MagicMock()
    creds.get_frozen_credentials.return_value = frozen
    session = MagicMock()
    session.get_credentials.return_value = creds
    with patch("botocore.session.get_session", return_value=session):
        try:
            got = cms._lambda_credentials()
        except Exception as exc:  # noqa: BLE001 - a raise is the defect under test
            raise AssertionError(f"credentials present, but raised {type(exc).__name__}: {exc}") from exc
    assert got is frozen

    session.get_credentials.return_value = None
    with patch("botocore.session.get_session", return_value=session):
        with pytest.raises(Exception) as raised:
            cms._lambda_credentials()
    assert type(raised.value) is ValueError, f"got {type(raised.value).__name__}: {raised.value}"
    assert "MissingAwsCredentials" in str(raised.value)

    with patch.dict("os.environ", {"VSA_CMS_FI_INTERNAL_API_ENDPOINT": "https://cms.example.test/prod"}):
        with pytest.raises(Exception) as bad_endpoint:
            cms._cms_internal_endpoint()
    assert type(bad_endpoint.value) is ValueError, (
        f"got {type(bad_endpoint.value).__name__}: {bad_endpoint.value}")
    assert "InvalidCmsInternalEndpoint" in str(bad_endpoint.value)

    # A valid prefix with more after the stage segment: the whole value must match, or
    # a signed move would go to another path on that API.
    extra = "https://yourapi000.execute-api.us-west-2.amazonaws.com/prod/extra"
    with patch.dict("os.environ", {"VSA_CMS_FI_INTERNAL_API_ENDPOINT": extra}):
        with pytest.raises(Exception) as trailing:
            cms._cms_internal_endpoint()
    assert type(trailing.value) is ValueError, f"got {type(trailing.value).__name__}: {trailing.value}"
    assert "InvalidCmsInternalEndpoint" in str(trailing.value)


@pytest.mark.parametrize("status", [300, 301])
def test_move_vehicles_3xx_is_an_unexpected_redirect_never_executed(status):
    """The move request carries the operator's token and never follows a redirect:
    any 3xx is ``UnexpectedRedirect``, after one request (pins ~773-777)."""
    with _http() as mock:
        mock.add(responses.POST, _URLS["move"], status=status,
                 headers={"Location": "https://elsewhere.test/moves"})
        result = _dispatch("move", _action("move"))
        sent = len(mock.calls)

    assert result.status == ActionStatus.FAILED, _describe(result)
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR, _describe(result)
    assert result.last_error_class == "UnexpectedRedirect", _describe(result)
    assert result.last_error_status == status, _describe(result)
    assert sent == 1


# ---------------------------------------------------------------------------
# DMS: warranty claim
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label, parameters", [
    ("blank_ro_id", {**_parameters("warranty"), "ro_id": "   "}),
    ("boolean_line_no", {**_parameters("warranty"), "line_no": True}),
])
def test_warranty_claim_refuses_unusable_parameters_without_sending(label, parameters):
    """Pins ``_warranty_claim_body`` (~233-241: only an int ``line_no`` is
    coerced, a bool never is) and the ``body is None`` guard (~654-655)."""
    with _http() as mock:
        result = _dispatch("warranty", _action("warranty", parameters=parameters))
        _assert_refused_locally(result, mock, label)


def test_warranty_claim_carries_the_correlation_only_when_dms_can_read_it():
    """DMS dedupes on a bare UUID: an ``AVX-ACT-<uuid>`` id is sent as the UUID; an
    id DMS cannot express is left out entirely, never sent as null (pins ~248)."""
    from agents.executors.dms import _warranty_claim_body

    with_uuid = _warranty_claim_body(_action("warranty"))
    without = _warranty_claim_body(_action("warranty", action_id="manual-claim-7"))

    assert with_uuid is not None and with_uuid.get("avx_action_id") == _UUID, with_uuid
    assert without is not None and "avx_action_id" not in without, without


@pytest.mark.parametrize("status, expected", [
    (200, ActionStatus.EXECUTED),
    (201, ActionStatus.EXECUTED),
    (202, ActionStatus.EXECUTED),
    (403, ActionStatus.FAILED),
])
def test_warranty_claim_outcome_by_status(status, expected):
    """Any 2xx means DMS has the claim; a 403 is a failure that escalates, never a
    coverage refusal (pins ~690 and the ``client_error`` return ~708)."""
    with _http() as mock:
        mock.add(responses.POST, _URLS["warranty"], status=status, json={})
        result = _dispatch("warranty", _action("warranty"))
        sent = len(mock.calls)

    assert result.status == expected, _describe(result)
    assert sent == 1
    if expected == ActionStatus.FAILED:
        assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR, _describe(result)
        assert result.last_error_status == status


# ---------------------------------------------------------------------------
# DMS: fleet-service booking
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label, parameters", [
    ("blank_vin", {**_parameters("fleet_service"), "vehicle_vin": "   "}),
    ("blank_dealer", {**_parameters("fleet_service"), "dealer_id": "   "}),
    ("no_needs", {"vehicle_vin": _VIN, "dealer_id": _DEALER}),
])
def test_fleet_service_refuses_unusable_parameters_without_sending(label, parameters):
    """Pins ``_fleet_service_body``'s guards (~162-174)."""
    with _http() as mock:
        result = _dispatch("fleet_service", _action("fleet_service", parameters=parameters))
        _assert_refused_locally(result, mock, label)


@pytest.mark.parametrize("urgency, sent_urgency", [(None, None), (" soon ", "soon"), ("   ", None)])
def test_fleet_service_body_is_exactly_the_server_set_fields(urgency, sent_urgency):
    """The posted booking: stripped VIN and dealer, one line per need, the correlation
    id, the operator's urgency when set and no key when absent or blank (pins the body
    dict ~203 and the urgency branch ~211)."""
    parameters = {
        "vehicle_vin": f" {_VIN} ",
        "dealer_id": f" {_DEALER} ",
        "needs": [{"labor_op": "LO-EV-TIRE-ROT", "need_id": "NEED-0001"}],
    }
    if urgency is not None:
        parameters["urgency"] = urgency
    with _http() as mock:
        mock.add(responses.POST, _URLS["fleet_service"], status=201, json={"ro_id": "RO-P108-1"})
        result = _dispatch("fleet_service", _action("fleet_service", parameters=parameters))
        bodies = [json.loads(c.request.body) for c in mock.calls]

    assert result.status == ActionStatus.IN_PROGRESS, _describe(result)
    assert result.target_ref == "RO-P108-1"
    expected: dict[str, Any] = {
        "vehicle_vin": _VIN,
        "dealer_id": _DEALER,
        "lines": [{"line_no": 1, "labor_op": "LO-EV-TIRE-ROT",
                   "pay_type": "customer_pay", "need_id": "NEED-0001"}],
        "description": "FI fleet service: NEED-0001",  # 11.3c: built from needs
        "avx_action_id": _UUID,
    }
    if sent_urgency is not None:
        expected["urgency"] = sent_urgency
    assert bodies == [expected]


@pytest.mark.parametrize("action_id, attempts", [
    (_ACTION_ID, _ATTEMPTS),
    ("manual-booking-7", 1),
    (None, 1),
])
def test_fleet_service_retries_only_when_dms_can_dedupe(action_id, attempts):
    """D-G10-10: without ``avx_action_id`` DMS can't tell a retry from a new booking,
    so a transient failure gets ONE attempt (a retry could book twice). Pins the
    correlation branch (~215), ``max_attempts`` (~559) and ``_dms_correlation_id``
    (~104)."""
    with _http() as mock:
        mock.add(responses.POST, _URLS["fleet_service"], status=503, json={})
        result = _dispatch("fleet_service", _action("fleet_service", action_id=action_id))
        bodies = [json.loads(c.request.body) for c in mock.calls]

    assert result.failure_kind == FailureKind.EXECUTOR_EXHAUSTED_RETRIES, _describe(result)
    assert len(bodies) == attempts, f"expected {attempts} attempt(s), sent {len(bodies)}"
    has_correlation = {"avx_action_id" in b for b in bodies}
    assert has_correlation == {attempts > 1}, bodies[:2]


@pytest.mark.parametrize("status, body, target_ref", [
    (200, {"ro_id": "RO-P108-1"}, "RO-P108-1"),
    (201, {"ro_id": "RO-P108-1"}, "RO-P108-1"),
    (201, [], None),
    (201, "not json", None),
    (201, {}, None),
])
def test_fleet_service_success_needs_a_repair_order_id(status, body, target_ref):
    """A 2xx carrying an ``ro_id`` is in progress on that RO; a 2xx without one is
    ``MissingRoId`` (the card could never advance). Pins ~591-602."""
    with _http() as mock:
        if isinstance(body, str):
            mock.add(responses.POST, _URLS["fleet_service"], status=status, body=body)
        else:
            mock.add(responses.POST, _URLS["fleet_service"], status=status, json=body)
        result = _dispatch("fleet_service", _action("fleet_service"))

    if target_ref is not None:
        assert result.status == ActionStatus.IN_PROGRESS, _describe(result)
        assert result.target_ref == target_ref
    else:
        assert result.status == ActionStatus.FAILED, _describe(result)
        assert result.last_error_class == "MissingRoId", _describe(result)
        assert result.target_ref is None


@pytest.mark.parametrize("status, outcome", [
    (400, ActionStatus.FAILED),
    (409, ActionStatus.FAILED),
    (403, ActionStatus.FAILED),
    (422, ActionStatus.DECLINED),
])
def test_fleet_service_4xx_validation_fails_and_only_a_refusal_declines(status, outcome):
    """DMS 400/409 are request validation and 403 is our plumbing: ``failed``,
    escalated, one request. Only 422 is a dealer refusal (``declined``, no
    escalation). Pins ~608-627."""
    with _http() as mock:
        mock.add(responses.POST, _URLS["fleet_service"], status=status, json={})
        result = _dispatch("fleet_service", _action("fleet_service"))
        sent = len(mock.calls)

    assert result.status == outcome, _describe(result)
    assert result.last_error_status == status, _describe(result)
    assert sent == 1, f"a {status} must not be retried; sent {sent}"
    if outcome == ActionStatus.FAILED:
        assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR, _describe(result)
    else:
        assert result.failure_kind == FailureKind.TARGET_REFUSED, _describe(result)


# ---------------------------------------------------------------------------
# Refusals: the operator's failure sentence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label, kwargs, sentence", [
    ("adp_refusal", dict(target_system="adp", failure_kind="target_refused", error_status=422),
     "ADP refused the request (HTTP 422)."),
    ("g5_block", dict(target_system="adp", failure_kind="target_refused", guardrail="G5"),
     "Blocked: engineering decisions need a product engineer."),
    ("g5_with_a_stray_case", dict(target_system="adp", failure_kind="target_refused",
                                  guardrail="G5", guardrail_case="no_ref"),
     "AVX could not complete this action."),
    ("unknown_guardrail_with_a_g4_case", dict(target_system="dms", failure_kind="target_refused",
                                              guardrail="G9", guardrail_case="no_ref"),
     "AVX could not complete this action."),
    ("stray_ro_status_without_a_guardrail", dict(target_system="dms",
                                                 failure_kind="executor_terminal_error",
                                                 ro_status="Ready"),
     "AVX could not complete this action."),
    ("unknown_target", dict(target_system="acme", failure_kind="executor_terminal_error"),
     "AVX could not complete this action."),
])
def test_failure_reason_sentences_on_the_refusal_paths(label, kwargs, sentence):
    """The § 10a sentence for ADP refusals and the G5 block, and the fallback for
    inputs outside the table; never raises. Pins failure_reason.py's ADP name (~22),
    the G5 branch (~142-144), the unknown-guardrail return (~148), the stray-field
    return (~165-166) and the unknown-target return (~173-174)."""
    from agents.executors.failure_reason import failure_reason

    try:
        got = failure_reason(**kwargs)
    except Exception as exc:  # noqa: BLE001 - "never raises" is the property
        raise AssertionError(f"{label}: raised {type(exc).__name__}: {exc}") from exc
    assert got == sentence, f"{label}: {got!r}"


# ---------------------------------------------------------------------------
# ADP: the digital-thread writer
# ---------------------------------------------------------------------------

_ADP_ARN = "arn:aws:lambda:us-east-1:123456789012:function:adp-test-digital-thread-writer"


def _adp_action() -> dict[str, Any]:
    return {
        "action_id": _ACTION_ID,
        "action_kind": "issue_recall",
        "target_system": "adp",
        "decided_by": {"kind": "engineer", "sub": "eng-sub-p108-0001"},
        "parameters": {"thread_id": "thr-p108-1", "draft_id": "drf-p108-1"},
    }


def _writer_response(status_code: int) -> dict[str, Any]:
    payload = json.dumps({"statusCode": status_code, "body": "{}"}).encode()
    return {"StatusCode": 200, "Payload": MagicMock(read=lambda: payload)}


def _bare_client_error(code: str) -> Exception:
    """A bare ``ClientError`` carrying ``code`` and no HTTP status. boto3 raises this
    shape only for an unknown or missing code; for a modelled code it raises the
    subclass (``_modelled_error``). With no status, only the code can make it
    retryable (a bare ClientError is also retried by a transient HTTP status)."""
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": "p108"}}, "Invoke")


_LAMBDA_CLIENT: list = []


def _modelled_error(code: str) -> Exception:
    """The exception a real boto3 Lambda client raises for ``code``: the modelled
    subclass from the client's exception factory, e.g. ``TooManyRequestsException``."""
    if not _LAMBDA_CLIENT:
        import botocore.session

        _LAMBDA_CLIENT.append(botocore.session.get_session().create_client(
            "lambda", region_name="us-east-1", endpoint_url="http://127.0.0.1:1",
            aws_access_key_id="testing", aws_secret_access_key="testing"))
    cls = _LAMBDA_CLIENT[0].exceptions.from_code(code)
    return cls({"Error": {"Code": code, "Message": "p108"}}, "Invoke")


def _adp_dispatch(invoke_effect: Any, sleeps: Optional[list] = None) -> tuple[ExecuteResult, int]:
    from agents.executors.adp import AdpExecutor

    client = MagicMock()
    client.invoke.side_effect = invoke_effect
    executor = AdpExecutor(
        thread_writer_arn=_ADP_ARN,
        lambda_client=client,
        sleep_fn=sleeps.append if sleeps is not None else (lambda _s: None),
        retry_policy=_policy(),
    )
    return executor.dispatch(_adp_action(), _CLAIMS), client.invoke.call_count


@pytest.mark.parametrize("outcome, expected", [
    (422, (ActionStatus.DECLINED, "WriterRefused", 422)),
    (400, (ActionStatus.FAILED, "MalformedWriterRequest", 400)),
    (418, (ActionStatus.FAILED, "UnexpectedWriterStatus", 418)),
    (499, (ActionStatus.FAILED, "UnexpectedWriterStatus", 499)),
    ("ResourceNotFoundException", (ActionStatus.FAILED, "ResourceNotFoundException", None)),
    # Not in the Lambda model, so boto3 raises a bare ClientError (an IAM denial on the
    # invoke), with no HTTP status: neither its code nor a status makes it retryable.
    ("AccessDeniedException", (ActionStatus.FAILED, "ClientError", None)),
    ("error_without_a_code", (ActionStatus.FAILED, "ClientError", None)),
    ("non_aws_exception", (ActionStatus.FAILED, "ValueError", None)),
    ("unparseable_writer_response", (ActionStatus.FAILED, "InvalidWriterResponse", None)),
])
def test_adp_writer_terminal_outcomes_are_answered_on_the_first_invoke(outcome, expected):
    """422 is the writer refusing (``declined``); 400, an unexpected status, a
    non-retryable, unmodelled or code-less Lambda error, any other exception and an
    unparseable writer response are terminal; none is retried. Pins ``_invoke``
    (~480-554), ``_is_transient_for_lambda`` and ``_is_retryable_client_error``.
    Error codes are built the way a real Lambda client builds them (``_modelled_error``)."""
    if isinstance(outcome, int):
        result, invokes = _adp_dispatch(lambda **_kw: _writer_response(outcome))
    elif outcome == "error_without_a_code":
        from botocore.exceptions import ClientError

        result, invokes = _adp_dispatch(ClientError({}, "Invoke"))
    elif outcome == "non_aws_exception":
        result, invokes = _adp_dispatch(ValueError("p108 non-transient"))
    elif outcome == "unparseable_writer_response":
        result, invokes = _adp_dispatch(
            lambda **_kw: {"StatusCode": 200, "Payload": MagicMock(read=lambda: b"not json")})
    else:
        result, invokes = _adp_dispatch(_modelled_error(outcome))

    assert (result.status, result.last_error_class, result.last_error_status) == expected, (
        _describe(result))
    assert invokes == 1, f"a terminal outcome must not be retried; invoked {invokes} times"


def _function_error_response(**_kw: Any) -> dict[str, Any]:
    return {"StatusCode": 200, "FunctionError": "Unhandled",
            "Payload": MagicMock(read=lambda: b"{}")}


def _writer_503(**_kw: Any) -> dict[str, Any]:
    return _writer_response(503)


def _writer_503_then_function_error() -> Any:
    calls: list = []

    def _effect(**_kw: Any) -> dict[str, Any]:
        calls.append(1)
        return _writer_response(503) if len(calls) == 1 else _function_error_response()
    return _effect


def _read_timeout() -> Exception:
    """What botocore raises when an invoke outlasts the client's read timeout."""
    from botocore.exceptions import ReadTimeoutError

    return ReadTimeoutError(endpoint_url="https://lambda.us-east-1.amazonaws.com")


def _writer_503_then_timeout() -> Any:
    calls: list = []

    def _effect(**_kw: Any) -> dict[str, Any]:
        calls.append(1)
        if len(calls) == 1:
            return _writer_response(503)
        raise _read_timeout()
    return _effect


@pytest.mark.parametrize("mode, last_class, last_status", [
    ("bare_client_error_with_a_retryable_code", "ClientError", None),
    ("timeout", "ReadTimeoutError", None),
    ("function_error", "HTTPError", None),
    ("5xx", "HTTPError", 503),
    ("5xx_then_function_error", "HTTPError", None),
    ("5xx_then_timeout", "ReadTimeoutError", None),
])
def test_adp_writer_exhausts_the_policy_with_backoff(mode, last_class, last_status):
    """The transient shapes ``_invoke`` retries: a bare ``ClientError`` with a
    retryable code, a read timeout, a ``FunctionError`` response and a writer 5xx.
    Pins the attempt half of the three sleep guards, ``last_status = None`` after a 503
    on both the exception branch (~479) and the ``FunctionError`` branch (~497), and the
    last error (~459-549). The modelled throttle is the test below."""
    effect = {
        "bare_client_error_with_a_retryable_code": _bare_client_error("TooManyRequestsException"),
        "timeout": _read_timeout(),
        "function_error": _function_error_response,
        "5xx": _writer_503,
        "5xx_then_function_error": _writer_503_then_function_error(),
        "5xx_then_timeout": _writer_503_then_timeout(),
    }[mode]
    sleeps: list[float] = []
    with _full_window_jitter():
        result, invokes = _adp_dispatch(effect, sleeps)

    assert result.failure_kind == FailureKind.EXECUTOR_EXHAUSTED_RETRIES, _describe(result)
    assert invokes == _ATTEMPTS, f"expected {_ATTEMPTS} invokes, got {invokes}"
    assert result.retry_count == _ATTEMPTS, _describe(result)
    assert sleeps == pytest.approx(_EXPECTED_SLEEPS), f"sleeps {sleeps[:8]} (n={len(sleeps)})"
    assert (result.last_error_class, result.last_error_status) == (last_class, last_status), (
        _describe(result))


@pytest.mark.parametrize("code", ["TooManyRequestsException", "ServiceException"])
def test_adp_writer_retries_a_throttle_raised_the_way_boto3_raises_it(code):
    """A real Lambda client raises the modelled subclass for a known code. The executor
    retries it like any transient failure, to the full budget. Was a strict xfail until
    issues/2026-10-24-adp-executor-never-retries-modelled-lambda-errors was fixed (the
    classifier matched the class name 'ClientError' exactly)."""
    sleeps: list[float] = []
    with _full_window_jitter():
        result, invokes = _adp_dispatch(_modelled_error(code), sleeps)

    assert invokes == _ATTEMPTS, f"{code}: expected {_ATTEMPTS} invokes, got {invokes}"
    assert result.failure_kind == FailureKind.EXECUTOR_EXHAUSTED_RETRIES, _describe(result)
    assert result.retry_count == _ATTEMPTS, _describe(result)
    assert sleeps == pytest.approx(_EXPECTED_SLEEPS), f"{code}: sleeps {sleeps[:8]}"
    assert (result.last_error_class, result.last_error_status) == (code, None), (
        _describe(result))


@pytest.mark.parametrize("mode", ["5xx", "function_error", "timeout"])
def test_adp_writer_stops_at_the_deadline_without_sleeping_past_it(mode):
    """The deadline half of all three of ``_invoke``'s sleep guards (exception ~482,
    ``FunctionError`` ~498, 5xx ~549): clock 0, 0, then 100 means one invoke and no
    sleep. The timeout case is the branch a slow writer takes in production."""
    sleeps: list[float] = []
    effect = {"5xx": _writer_503, "function_error": _function_error_response,
              "timeout": _read_timeout()}[mode]
    with patch("time.monotonic", _clock_for("agents.executors.adp", [0, 0, 100])):
        result, invokes = _adp_dispatch(effect, sleeps)

    assert result.failure_kind == FailureKind.EXECUTOR_EXHAUSTED_RETRIES, _describe(result)
    assert (invokes, result.retry_count) == (1, 1), f"invokes={invokes} {_describe(result)}"
    assert sleeps == [], f"no sleep once the deadline has passed; slept {sleeps}"


def test_adp_writer_keeps_trying_inside_the_25_s_budget():
    """ADP's twin of the HTTP budget-size test: the deadline is the 25 s budget, not
    the ~6.1 s per-attempt timeout (which is also the writer client's read timeout, so
    one slow invoke would spend a shrunken budget). Clock 0, then 24 s (bounded as in
    the HTTP test); timeouts throughout: four invokes and all three sleeps (~462)."""
    sleeps: list[float] = []
    with _full_window_jitter(), \
            patch("time.monotonic", _clock_for("agents.executors.adp", _INSIDE_BUDGET_CLOCK)):
        result, invokes = _adp_dispatch(_read_timeout(), sleeps)

    assert result.failure_kind == FailureKind.EXECUTOR_EXHAUSTED_RETRIES, _describe(result)
    assert (invokes, result.retry_count) == (_ATTEMPTS, _ATTEMPTS), (
        f"invokes={invokes} {_describe(result)}")
    assert sleeps == pytest.approx(_EXPECTED_SLEEPS), f"sleeps {sleeps}"
    assert result.last_error_class == "ReadTimeoutError", _describe(result)



def _adp_capture(action: dict[str, Any]) -> tuple[ExecuteResult, list]:
    """Dispatch ``action`` to an ADP executor whose writer answers 200; return the
    result and the decoded payload of every invoke."""
    from agents.executors.adp import AdpExecutor

    payloads: list = []

    def _invoke(**kwargs: Any) -> dict[str, Any]:
        payloads.append(json.loads(kwargs["Payload"]))
        body = json.dumps({"thread_id": "thr-p108-1"})
        raw = json.dumps({"statusCode": 200, "body": body}).encode()
        return {"StatusCode": 200, "Payload": MagicMock(read=lambda: raw)}

    client = MagicMock()
    client.invoke.side_effect = _invoke
    executor = AdpExecutor(thread_writer_arn=_ADP_ARN, lambda_client=client,
                           sleep_fn=lambda _s: None, retry_policy=_policy())
    return executor.dispatch(action, _CLAIMS), payloads


def _without(key: str):
    def _apply(a: dict) -> None:
        del a[key]
    return _apply


def _without_param(key: str):
    def _apply(a: dict) -> None:
        del a["parameters"][key]
    return _apply


def _decided_by(value: dict):
    def _apply(a: dict) -> None:
        a["decided_by"] = value
    return _apply


def _kind(value: str):
    def _apply(a: dict) -> None:
        a["action_kind"] = value
    return _apply


_ISSUE_PAYLOAD = {
    "action_id": _ACTION_ID,
    "action_kind": "issue_recall",
    "decided_by": {"kind": "engineer", "sub": "eng-sub-p108-0001"},
    "thread_id": "thr-p108-1",
    "draft_id": "drf-p108-1",
}


@pytest.mark.parametrize("label, change, expected", [
    ("complete", None, _ISSUE_PAYLOAD),
    ("decider_named_by_id", _decided_by({"kind": "engineer", "id": "eng-id-p108"}),
     {**_ISSUE_PAYLOAD, "decided_by": {"kind": "engineer", "sub": "eng-id-p108"}}),
    ("no_thread_id", _without_param("thread_id"), "missing"),
    ("no_draft_id", _without_param("draft_id"), "missing"),
    ("no_decider_kind", _decided_by({"sub": "eng-sub-p108-0001"}), "missing"),
    ("no_action_id", _without("action_id"), "missing"),
    ("unknown_kind", _kind("issue_rollout_plan"), "refused"),
])
def test_adp_issue_payload_is_exactly_the_decision_or_nothing_is_sent(label, change, expected):
    """``issue_recall`` sends the decision and who made it, nothing else; a missing
    field is refused locally with no invoke; an unknown ADP kind is refused. Pins
    ``_build_issue_payload`` (~223-241), ``_dispatch_issue`` (~402-403) and the kind
    routing in ``dispatch`` (~361)."""
    action = _adp_action()
    if change is not None:
        change(action)
    result, payloads = _adp_capture(action)

    if expected == "missing":
        assert (result.status, result.last_error_class) == (
            ActionStatus.FAILED, "MissingDispatchParameter"), f"{label}: {_describe(result)}"
        assert payloads == [], f"{label}: nothing may be sent, sent {payloads}"
    elif expected == "refused":
        assert result.status == ActionStatus.DECLINED, f"{label}: {_describe(result)}"
        assert payloads == [], f"{label}: nothing may be sent, sent {payloads}"
    else:
        assert result.status == ActionStatus.EXECUTED, f"{label}: {_describe(result)}"
        assert payloads == [expected], f"{label}: {payloads}"


def _investigation_finding() -> dict[str, Any]:
    """An investigation Finding whose FIRST evidence entry is not the root cause, so
    the payload must pick the ``quality.root_cause`` entry by kind."""
    return {
        "finding_id": "FND-p108-quality-1",
        "agent_version": "agent-fi-quality@2.0.0",
        "evidence": [
            {"kind": "quality.signal", "value": {"candidate_id": "cand-WRONG",
                                                 "failure_mode_id": "fm-WRONG"}},
            {"kind": "quality.root_cause", "value": {
                "candidate_id": "cand-p108-1",
                "failure_mode_id": "fm-p108-1",
                "diagnosis_source": "model",
                "evidence_refs": ["ev-p108-1"],
                "agent_version": "agent-fi-quality@1.0.0",
            }},
        ],
        "recommended_action": {"parameters": {"pattern_id": "pat-p108-1",
                                              "option_id": "opt-agent", "notes": "agent note"}},
    }


def _decision_action() -> dict[str, Any]:
    return {
        "action_id": _ACTION_ID,
        "action_kind": "record_engineering_decision",
        "target_system": "adp",
        "decided_by": {"kind": "engineer", "sub": "eng-sub-p108-0001"},
        # The operator's option and notes differ from the agent's, so the approve
        # route records this decision as "overridden" (action.schema.json).
        "decision": "overridden",
        "parameters": {"option_id": "opt-operator", "notes": "operator note"},
        "investigation_finding": _investigation_finding(),
    }


_DECISION_PAYLOAD = {
    "action_id": _ACTION_ID,
    "action_kind": "record_engineering_decision",
    "decided_by": {"kind": "engineer", "sub": "eng-sub-p108-0001"},
    "decision": "overridden",
    "investigation_finding_id": "FND-p108-quality-1",
    "pattern_id": "pat-p108-1",
    "candidate_id": "cand-p108-1",
    "failure_mode_id": "fm-p108-1",
    "option_id": "opt-operator",
    "evidence_refs": ["ev-p108-1"],
    "diagnosis_source": "model",
    "agent_version": "agent-fi-quality@2.0.0",
    "notes": "operator note",
}


def _finding_change(fn):
    def _apply(a: dict) -> None:
        fn(a["investigation_finding"])
    return _apply


def _no_root_cause(f: dict) -> None:
    f["evidence"] = f["evidence"][:1]


def _root_cause_not_an_object(f: dict) -> None:
    f["evidence"][1]["value"] = "cand-p108-1"


def _no_pattern(f: dict) -> None:
    del f["recommended_action"]["parameters"]["pattern_id"]


def _no_finding_id(f: dict) -> None:
    del f["finding_id"]


def _no_finding(a: dict) -> None:
    del a["investigation_finding"]


def _agent_choice(a: dict) -> None:
    """The operator approved the agent's option as recommended."""
    a["parameters"] = {}
    a["decision"] = "approved"


@pytest.mark.parametrize("label, change, expected", [
    ("complete", None, _DECISION_PAYLOAD),
    ("agent_option_and_notes", _agent_choice,
     {**_DECISION_PAYLOAD, "decision": "approved", "option_id": "opt-agent",
      "notes": "agent note"}),
    ("decider_named_by_id", _decided_by({"kind": "engineer", "id": "eng-id-p108"}),
     {**_DECISION_PAYLOAD, "decided_by": {"kind": "engineer", "sub": "eng-id-p108"}}),
    ("decider_with_sub_and_id", _decided_by({"kind": "engineer", "sub": "eng-sub-p108-0001",
                                             "id": "eng-id-p108"}), _DECISION_PAYLOAD),
    ("no_action_id", _without("action_id"), "MissingDispatchParameter"),
    ("no_decider_kind", _decided_by({"sub": "eng-sub-p108-0001"}), "MissingDispatchParameter"),
    ("no_finding_id", _finding_change(_no_finding_id), "MissingDispatchParameter"),
    ("no_pattern_id", _finding_change(_no_pattern), "MissingDispatchParameter"),
    ("no_root_cause", _finding_change(_no_root_cause), "MissingDispatchParameter"),
    ("root_cause_not_an_object", _finding_change(_root_cause_not_an_object),
     "MissingDispatchParameter"),
    ("no_investigation_finding", _no_finding, "MissingFinding"),
])
def test_adp_decision_record_carries_the_decider_and_the_root_cause(label, change, expected):
    """The engineering-decision record sends the decision and who made it (``sub`` over
    ``id``), the operator's own option and notes over the agent's (the agent's when the
    operator gave none), and the root-cause evidence picked by kind; anything
    missing is refused locally with no invoke. Pins ``_build_record_decision_payload``
    (~144-195), ``_root_cause_evidence`` (~118-122) and ``_dispatch_record_decision``
    (~378-385)."""
    action = _decision_action()
    if change is not None:
        change(action)
    result, payloads = _adp_capture(action)

    if isinstance(expected, str):
        assert (result.status, result.last_error_class) == (ActionStatus.FAILED, expected), (
            f"{label}: {_describe(result)}")
        assert payloads == [], f"{label}: nothing may be sent, sent {payloads}"
    else:
        assert result.status == ActionStatus.EXECUTED, f"{label}: {_describe(result)}"
        assert payloads == [expected], f"{label}: {payloads}"

