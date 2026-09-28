"""DMS executor — dispatches an approved Action to the DMS repair-order API.

Spec: `.kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/spec.md`
      § "Executor dispatch abstraction", § "DMS executor", Addendum D5.

Behaviour (Addendum D5):
  - Terminal 4xx refusal (422, and any other code the shared classifier calls a
    refusal) → ExecuteResult.refused (declined / target_refused, retry_count=0,
    escalate=False).
  - Client-error 4xx (400, 401, 403, 404, 405, 409, 415) →
    ExecuteResult.terminal_error (failed / executor_terminal_error,
    escalate=True). Nothing reached a human; the owner must not be told the
    dealer refused. 400 and 409 are DMS-specific: its create-RO route uses them
    only for request validation and for an avx_action_id conflict (D-G10-8,
    review Cycles 1 W3 and 2 N1).
  - Retries are safe because DMS deduplicates AVX bookings on avx_action_id
    (DMS e2085e1). An Action with no expressible correlation gets one attempt.
  - Transient 5xx / 408 / 425 / 429 / connection / timeout → retry with
    exponential backoff + jitter per
    /vsa/{stage}/avx/executor/dms/retry-policy SSM config (staging defaults:
    retry_count=3, backoff_base_ms=1000, backoff_cap_ms=4000); on budget
    exhaustion → ExecuteResult.exhausted (failed / executor_exhausted_retries,
    retry_count=<attempts>, escalate=True).
  - Malformed response / non-transient executor-side error
                   → ExecuteResult.terminal_error (escalate=True).
  - On any escalate=True result this executor does NOT escalate itself: it
    returns the result and `registry.dispatch()` fires the D5 dual escalation
    once, centrally (review Cycle 2 Critical 3). Escalation used to live here,
    while chat, ios_owner and the @never_raises catch-all had no call sites at
    all — so the paths most likely to fail unexpectedly escalated to nobody, and
    AVXActionTerminalFailure could only ever carry target_system=dms despite
    being dimensioned by it. Callers must therefore route through
    `registry.dispatch()` rather than reaching into `EXECUTORS` directly.

HARD CONSTRAINTS:
  - MUST NOT call sns:Publish directly.
  - MUST NOT import any SNS client.
  - No hardcoded retry count or backoff — both read from SSM at cold start.
  - No hardcoded per-attempt timeout — derived from policy.per_attempt_timeout_s.
  - The overall retry loop must not exceed policy.deadline_s seconds so the
    synchronous approve route stays within API Gateway's 29-second limit.
  - A missing JWT is a LOCAL terminal error — do not send an empty token and
    let the target produce a 401 that then reads as a client error.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Callable, Mapping, Optional

import requests

from agents.executors.base import (
    ActionStatus,
    ExecuteResult,
    RetryPolicy,
    classify_status,
    is_transient_exception,
    load_escalation_config,
    load_retry_policy,
    never_raises,
)

log = logging.getLogger(__name__)

_TARGET_SYSTEM = "dms"
_METRIC_NAMESPACE = "VSA/AVX"


def _dms_endpoint() -> str:
    base = os.environ.get("VSA_DMS_API_ENDPOINT", "").rstrip("/")
    return f"{base}/api/dms/fleet/repair-orders"


#: CVX Action ids are ``AVX-ACT-{uuid}`` (action.schema.json ``action_id`` pattern).
_ACTION_ID_PREFIX = "AVX-ACT-"

#: DMS validates ``avx_action_id`` against exactly this shape
#: (``repair_orders.py:78`` ``_AVX_ACTION_ID_RE``) and answers 400 otherwise.
_DMS_CORRELATION_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

#: Fields DMS ``create_fleet_repair_order`` rejects the request without
#: (``repair_orders.py:1234-1239``). Read from ``action["parameters"]``: the
#: approve handler writes ``vehicle_vin`` from the Finding's own scope, and
#: ``dealer_id`` is the rooftop the caller chose (D-G10-2).
_REQUIRED_PARAMETERS = ("vehicle_vin", "dealer_id")


def _dms_correlation_id(action_id: Any) -> Optional[str]:
    """Return the id DMS will accept as ``avx_action_id``, or ``None``.

    DMS stores a bare UUID; CVX ids carry an ``AVX-ACT-`` prefix. Sending the
    prefixed id made every dispatch a 400 (D-G10-5). ``None`` means "omit the
    field": DMS treats it as optional, so a correlation we cannot express must
    not cost the owner the repair order itself.
    """
    if not isinstance(action_id, str):
        return None
    candidate = action_id[len(_ACTION_ID_PREFIX):] if action_id.startswith(_ACTION_ID_PREFIX) else action_id
    return candidate if _DMS_CORRELATION_RE.match(candidate) else None


def _request_body(action: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """Build the DMS request body, or ``None`` if a required field is missing."""
    parameters = action.get("parameters")
    if not isinstance(parameters, Mapping):
        return None
    body: dict[str, Any] = {}
    for field in _REQUIRED_PARAMETERS:
        value = parameters.get(field)
        if not isinstance(value, str) or not value.strip():
            return None
        body[field] = value.strip()
    correlation = _dms_correlation_id(action.get("action_id"))
    if correlation is not None:
        body["avx_action_id"] = correlation
    else:
        log.warning(
            "dms executor: action_id is not expressible as a DMS avx_action_id; "
            "omitting the correlation field (the repair order is still requested)"
        )
    return body


class DmsExecutor:
    """Dispatches an approved Action to the DMS repair-order API.

    Constructor keyword arguments allow test injection of collaborators:

        enqueue_ios_owner   callable(finding, claims) passed to escalation.fire()
                            as the enqueue_owner_card argument; default: None
                            (escalation.fire uses its own lazy default)
        events_client       UNUSED (escalation is fired by registry.dispatch)
                            default: lazy boto3.client("events")
        cloudwatch_client   UNUSED (escalation is fired by registry.dispatch)
                            default: lazy boto3.client("cloudwatch")
        ssm_client          boto3 SSM client forwarded to load_retry_policy /
                            load_escalation_config; default: None (lazy)
        sleep_fn            callable(seconds: float) — injected in tests to avoid
                            real sleeps; default: time.sleep
        retry_policy        injected RetryPolicy — bypasses SSM in tests
        escalation_cfg      injected escalation config — bypasses SSM in tests
    """

    target_system: str = _TARGET_SYSTEM

    def __init__(
        self,
        *,
        enqueue_ios_owner: Optional[Callable[..., Any]] = None,
        events_client: Any = None,
        cloudwatch_client: Any = None,
        ssm_client: Any = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
        retry_policy: Optional[RetryPolicy] = None,
        escalation_cfg: Optional[dict[str, Any]] = None,
    ) -> None:
        # Stored as enqueue_owner_card to match escalation.fire()'s kwarg name.
        self._enqueue_owner_card = enqueue_ios_owner
        # Retained as accepted-but-unused kwargs: escalation moved to
        # registry.dispatch(), so this executor no longer publishes events or
        # metrics. Kept so existing callers and tests do not break on an
        # unexpected-keyword error; the clients they pass are simply not used
        # here. registry.dispatch(escalation_clients=...) is where to inject now.
        self._events_client = events_client  # unused; see note above
        self._cloudwatch_client = cloudwatch_client  # unused; see note above
        self._ssm_client = ssm_client
        self._sleep = sleep_fn or time.sleep

        # Policy is loaded LAZILY, on first dispatch — deliberately not here.
        #
        # `registry.py` instantiates all six executors at module import, so an
        # eager load in this constructor turned `import agents.executors.registry`
        # into live SSM traffic: several `get_parameter` round-trips before any
        # Action existed, against whatever `VSA_STAGE` the process happened to
        # carry. Two concrete harms, both observed:
        #   - In an environment where SSM is unreachable, each call must exhaust
        #     boto3's retries before import can finish, so importing the registry
        #     blocks rather than failing fast.
        #   - The stage is read at import time, so a process with a stray
        #     VSA_STAGE=prod silently read prod policy while doing nothing else
        #     prod-related.
        # base.py caches parameters for the process lifetime, so the first
        # dispatch still pays exactly one read and every later one is free —
        # the cold-start-read intent is preserved, the import-time I/O is not.
        self._retry_policy_cached: Optional[RetryPolicy] = retry_policy
        self._escalation_cfg_cached: Optional[dict[str, Any]] = escalation_cfg

    @property
    def _retry_policy(self) -> RetryPolicy:
        if self._retry_policy_cached is None:
            self._retry_policy_cached = load_retry_policy(
                _TARGET_SYSTEM, ssm_client=self._ssm_client
            )
        return self._retry_policy_cached

    @property
    def _escalation_cfg(self) -> dict[str, Any]:
        if self._escalation_cfg_cached is None:
            self._escalation_cfg_cached = load_escalation_config(
                _TARGET_SYSTEM, ssm_client=self._ssm_client
            )
        return self._escalation_cfg_cached

    @never_raises
    def dispatch(self, action: Mapping[str, Any], claims: Mapping[str, Any]) -> ExecuteResult:
        """Call POST .../repair-orders and return an ExecuteResult.

        Never raises (enforced by @never_raises).
        """
        # --- Guard: a missing JWT is a LOCAL terminal error. Do not send an
        # empty Authorization header and let the target produce a 401 that would
        # then be (correctly) classified as executor_terminal_error — that wastes
        # a round-trip and makes the audit trail look like we reached the target.
        token = claims.get("token") or claims.get("id_token")
        if not token:
            result = ExecuteResult.terminal_error(
                last_error_class="MissingJwt",
                note="jwt_unavailable: no token or id_token in claims",
            )
            return result

        endpoint = _dms_endpoint()

        # --- Guard: DMS rejects a request without vehicle_vin / dealer_id with a
        # 400, which D5 classifies as `declined` — "the dealer refused". Nobody at
        # a dealer saw it; the request was ours and incomplete. Caught locally so
        # the audit trail says what actually happened.
        body = _request_body(action)
        if body is None:
            return ExecuteResult.terminal_error(
                last_error_class="MissingDispatchParameter",
                note="parameters.vehicle_vin and parameters.dealer_id are required for dms",
            )

        policy = self._retry_policy
        last_exc: Optional[Exception] = None
        last_status: Optional[int] = None
        attempt = 0
        deadline = time.monotonic() + policy.deadline_s

        # Retries are safe only because DMS deduplicates AVX bookings on
        # avx_action_id (DMS e2085e1: deterministic ro_id + conditional put, replay
        # returns the existing RO). Without the correlation field DMS cannot tell a
        # retry from a new booking, so a transient failure gets exactly one attempt
        # rather than a chance to book twice (review Cycle 1 W1, D-G10-10).
        max_attempts = policy.retry_count if "avx_action_id" in body else 1

        while attempt < max_attempts:
            # Enforce the overall deadline so the synchronous approve route
            # cannot exceed API Gateway's 29-second limit regardless of how many
            # retries remain.
            if time.monotonic() >= deadline:
                log.warning(
                    "dms executor: deadline exceeded after %d attempt(s); "
                    "treating as exhausted",
                    attempt,
                )
                break

            try:
                resp = requests.post(
                    endpoint,
                    json=body,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=policy.per_attempt_timeout_s,
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                last_status = None
                if is_transient_exception(exc):
                    attempt += 1
                    if attempt < max_attempts and time.monotonic() < deadline:
                        sleep_s = policy.sleep_ms(attempt - 1) / 1000.0
                        self._sleep(sleep_s)
                    continue
                # Non-transient network error → terminal
                result = ExecuteResult.terminal_error(
                    last_error_class=type(exc).__name__,
                    retry_count=attempt,
                )
                return result

            last_status = resp.status_code

            # ---------------------------------------------------------------
            # Success (2xx)
            # ---------------------------------------------------------------
            if 200 <= resp.status_code < 300:
                try:
                    body = resp.json()
                    if not isinstance(body, dict):
                        # Valid JSON but not an object (e.g. []) — label
                        # correctly so the audit trail points the operator at
                        # the right thing.
                        raise TypeError(f"expected a JSON object, got {type(body).__name__}")
                    ro_id = body.get("ro_id")
                except (ValueError, TypeError) as exc:
                    # last_error_status OMITTED — this is inside a 2xx branch and
                    # base.py rejects a success code on a failure kind. Passing it
                    # raised inside the loop and @never_raises degraded the
                    # specific class to 'ValueError'.
                    # A descriptive class, not the bare Python exception name: the
                    # audit trail is read by an operator diagnosing a downstream
                    # problem, and "TypeError" sends them after the wrong thing.
                    result = ExecuteResult.terminal_error(
                        last_error_class="InvalidJsonResponse",
                        retry_count=attempt,
                        note=(
                            f"HTTP {resp.status_code} body unusable "
                            f"({type(exc).__name__}: not a JSON object)"
                        ),
                    )
                    return result

                # A 2xx with no ro_id means we have no handle to join against
                # the dms.ro.status_changed event — the card can never advance
                # beyond in_progress. Treat it as an executor terminal error
                # rather than returning a None target_ref that silently stalls.
                # Note: last_error_status is omitted here — base.py's
                # _validate_error_status() rejects a 2xx on any failure kind,
                # because a 2xx is not an error status code. The audit trail
                # gets the class name as the signal instead.
                if ro_id is None:
                    result = ExecuteResult.terminal_error(
                        last_error_class="MissingRoId",
                        retry_count=attempt,
                    )
                    return result

                return ExecuteResult.success(
                    target_ref=ro_id,
                    status=ActionStatus.IN_PROGRESS,
                )

            # ---------------------------------------------------------------
            # Classify the non-2xx status via the shared single source of truth
            # ---------------------------------------------------------------
            disposition = classify_status(resp.status_code)

            # W3 (review Cycle 1) and N1 (Cycle 2): D5 reserves `declined` for
            # "the dealer said no". DMS `create_fleet_repair_order` has no such
            # answer. Its 400s are request validation (missing vehicle_vin /
            # dealer_id, a dealer_id outside the roster, a malformed avx_action_id
            # or evidence). Its only 409 (DMS e2085e1) means "this avx_action_id
            # already booked a DIFFERENT vehicle or dealer", which CVX's own
            # re-approve 409 should make unreachable, so seeing it is an anomaly an
            # operator must look at. Both are therefore `executor_terminal_error`
            # (escalates), the same as 401/403/404. Recording either as `declined`
            # would tell the owner a dealer refused, with no operator signal. 422
            # keeps the shared `refused` mapping in case a real refusal is added.
            if resp.status_code in (400, 409):
                disposition = "client_error"

            if disposition == "transient":
                attempt += 1
                if attempt < max_attempts and time.monotonic() < deadline:
                    sleep_s = policy.sleep_ms(attempt - 1) / 1000.0
                    self._sleep(sleep_s)
                continue

            if disposition == "client_error":
                # Auth failure, unknown route, wrong method/content-type — our
                # fault, nothing reached a human.
                result = ExecuteResult.terminal_error(
                    last_error_status=resp.status_code,
                    last_error_class="HTTPError",
                    retry_count=attempt,
                )
                return result

            # disposition == "refused" — genuine business refusal
            return ExecuteResult.refused(
                last_error_status=resp.status_code,
                last_error_class="HTTPError",
            )

        # ----------------------------------------------------------------
        # Retry budget exhausted
        # ----------------------------------------------------------------
        err_class = type(last_exc).__name__ if last_exc else "HTTPError"
        result = ExecuteResult.exhausted(
            retry_count=attempt,
            last_error_class=err_class,
            last_error_status=last_status,
        )
        return result

    # ------------------------------------------------------------------
    # Dual escalation (spec § "Retry count + escalation target")
    # ------------------------------------------------------------------

