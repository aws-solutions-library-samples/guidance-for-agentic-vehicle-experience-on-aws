"""VehicleCommandExecutor — dispatch an approved remote-command Action to the CMS commands API.

Spec: `.kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/`
Tech reference: `docs/tech.md` § "CMS remote commands (VehicleCommandExecutor target)"

Endpoint:
    POST ${VSA_CMS_COMMANDS_ENDPOINT}/api/commands/{vehicleId}

Auth:
    The owner's Cognito JWT, forwarded as ``Authorization: Bearer <token>``.
    There is NO M2M credential path — fleet membership is validated server-side
    by the CMS commands Lambda using the caller's JWT.

Behaviour:
    - 2xx with ``commandId``  → ExecuteResult.success(target_ref=commandId)
    - 2xx *without* commandId → ExecuteResult.terminal_error  (malformed response)
    - 401/403/404/405/415     → ExecuteResult.terminal_error  (client error, escalates)
    - 400/409/422/other 4xx   → ExecuteResult.refused         (target said no, no escalation)
    - 408/425/429 / 5xx / network exception → retry per policy, then ExecuteResult.exhausted

Addendum D5 applies: ``declined`` means the target refused; ``failed`` means we could
not reach it. These are distinct states with different owner-facing meanings.

Environment:
    VSA_CMS_COMMANDS_ENDPOINT — base URL of the CMS commands API (no trailing slash).
                                 Example: https://9xpiwl2269.execute-api.us-west-2.amazonaws.com/prod
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Mapping, Optional

import requests

from agents.executors.base import (
    ActionStatus,
    ExecuteResult,
    RetryPolicy,
    classify_status,
    is_transient_exception,
    load_retry_policy,
    never_raises,
)

log = logging.getLogger(__name__)

_TARGET = "vehicle-command"


#: Strict whitelist for the one action-supplied value that reaches the request
#: PATH. Alphanumerics, hyphen, underscore and dot only, and '..' is rejected
#: separately below by the absence of any path separator. Deliberately a
#: whitelist rather than a blacklist of dangerous characters: a blacklist of
#: path/query separators has to enumerate every encoding ('%2F', '%2e%2e') and
#: will miss one.
#:
#: Must START with an alphanumeric, which rejects a bare ".." (accepted by a
#: naive [A-Za-z0-9._-]+ and normalising to the parent path: "/api/commands/.."
#: resolves to "/api/"). A dot-only or leading-dot value has no legitimate form.
_VEHICLE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _endpoint() -> str:
    """Read the commands base URL from environment. Raises if unset."""
    url = os.environ.get("VSA_CMS_COMMANDS_ENDPOINT", "").rstrip("/")
    if not url:
        raise RuntimeError(
            "VSA_CMS_COMMANDS_ENDPOINT is not set — cannot dispatch vehicle command"
        )
    return url


class VehicleCommandExecutor:
    """Dispatch an approved remote-command Action to the CMS commands API.

    Parameters
    ----------
    retry_policy:
        Override the SSM-loaded policy (used in tests to avoid SSM calls and
        disable real sleeps by injecting a zero-backoff policy).
    _sleep:
        Injectable sleep callable for testing. Defaults to ``time.sleep``.
        Tests MUST inject a no-op lambda to avoid real sleeps.
    _events_client, _cloudwatch_client:
        Injectable AWS clients for testing escalation behaviour without real AWS
        calls.
    """

    target_system: str = _TARGET

    def __init__(
        self,
        *,
        retry_policy: Optional[RetryPolicy] = None,
        _sleep: Any = None,
        _events_client: Any = None,
        _cloudwatch_client: Any = None,
    ) -> None:
        # Lazy policy load — see the equivalent note in dms.py. `registry.py`
        # builds every executor at import, so an eager `load_retry_policy()`
        # here makes importing the registry perform live SSM reads against
        # whatever VSA_STAGE the process carries.
        self._retry_policy_override = retry_policy
        self._retry_policy_cached: Optional[RetryPolicy] = None
        self._sleep = _sleep if _sleep is not None else time.sleep
        self._events_client = _events_client
        self._cloudwatch_client = _cloudwatch_client

    @property
    def _retry_policy(self) -> RetryPolicy:
        if self._retry_policy_override is not None:
            return self._retry_policy_override
        if self._retry_policy_cached is None:
            self._retry_policy_cached = load_retry_policy(_TARGET)
        return self._retry_policy_cached

    # ------------------------------------------------------------------
    # Protocol implementation
    # ------------------------------------------------------------------

    @never_raises
    def dispatch(self, action: Mapping[str, Any], claims: Mapping[str, Any]) -> ExecuteResult:
        """Send the command to the CMS commands API.

        ``action`` must contain:
        - ``vehicle_id`` (str) — target vehicle identifier
        - ``params`` (dict) — actuator body forwarded as POST body
          (keys: ``commandName`` required; ``value``, ``signalId``, ``vin``,
          ``timeout`` optional — see tech.md § "POST /api/commands/{vehicleId}")

        ``claims`` must contain:
        - ``id_token`` or ``jwt`` (str) — the owner's Cognito ID token

        The JWT is forwarded verbatim as ``Authorization: Bearer <token>``.
        Fleet membership is validated server-side; the executor does NOT
        validate group membership itself.
        """
        vehicle_id: str = action.get("vehicle_id", "")
        if not vehicle_id:
            return ExecuteResult.terminal_error(
                last_error_class="MissingVehicleId",
                note="action.vehicle_id is required",
            )
        if not _VEHICLE_ID_RE.fullmatch(vehicle_id) or ".." in vehicle_id:
            # `vehicle_id` is interpolated into the request path below, and the
            # request carries the owner's forwarded JWT. A value containing '/',
            # '?', '#' or '..' would steer that credential to a different CMS
            # route than the one this executor is authorised to call — e.g.
            # "VEH-1/../../geofences" or "VEH-1?admin=1". A non-empty check is
            # not enough.
            #
            # The value reaches here from a Tier 2-authored Finding. The Tier 2
            # boundary scan stops Tier 2 calling an executor directly; it does
            # not stop Tier 2 writing a Finding whose fields flow here, so this
            # is validated at the point of use rather than trusted upstream.
            return ExecuteResult.terminal_error(
                last_error_class="InvalidVehicleId",
                note=(
                    "action.vehicle_id failed strict validation; it is "
                    "interpolated into the request path and must not contain "
                    "path or query separators"
                ),
            )

        jwt: str = claims.get("id_token") or claims.get("jwt", "")
        if not jwt:
            # A locally-detected missing JWT is an executor-side terminal error,
            # consistent with the missing-vehicle_id and missing-commandName
            # preconditions eleven lines either side.  It is NOT a `refused`:
            # no request was sent and no 4xx was received, so the target never
            # saw this action — telling the owner "the vehicle refused" when our
            # token was simply absent is a false statement, and it escalates to
            # nobody (C5 fix).
            return ExecuteResult.terminal_error(
                last_error_class="MissingJWT",
                note="jwt_unavailable: no id_token/jwt in claims — cannot forward owner JWT",
            )

        params: dict[str, Any] = dict(action.get("params") or {})
        if not params.get("commandName"):
            return ExecuteResult.terminal_error(
                last_error_class="MissingCommandName",
                note="action.params.commandName is required",
            )

        url = f"{_endpoint()}/api/commands/{vehicle_id}"
        headers = {
            "Authorization": f"Bearer {jwt}",
            "Content-Type": "application/json",
        }

        policy = self._retry_policy
        # Per-attempt timeout derived from policy so three stalled attempts do
        # not exceed the overall synchronous budget (C7 fix).
        attempt_timeout = policy.per_attempt_timeout_s
        deadline = policy.deadline_s
        loop_start = time.monotonic()
        last_exc: Optional[BaseException] = None
        last_status: Optional[int] = None

        for attempt in range(policy.retry_count):
            # Enforce overall deadline before each attempt (C7 fix).
            if time.monotonic() - loop_start >= deadline:
                result = ExecuteResult.exhausted(
                    retry_count=attempt,
                    last_error_class=type(last_exc).__name__ if last_exc else "DeadlineExceeded",
                    last_error_status=last_status,
                    note="overall synchronous budget exceeded before attempt",
                )
                # Escalation is fired centrally by registry.dispatch() — see its
                # docstring. Firing here too double-escalated every failure:
                # two owner cards and two AVXActionTerminalFailure datapoints.
                return result

            try:
                resp = requests.post(
                    url,
                    json=params,
                    headers=headers,
                    timeout=attempt_timeout,  # per-attempt, derived from policy (C7 fix)
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                last_status = None
                if is_transient_exception(exc) and attempt < policy.retry_count - 1:
                    sleep_ms = policy.sleep_ms(attempt)
                    self._sleep(sleep_ms / 1000.0)
                    continue
                # Transient exception on the final attempt, or a non-transient
                # exception at any attempt.
                #
                # C8 fix: a transient exception that falls off the final attempt
                # must yield executor_exhausted_retries with retry_count equal to
                # the total budget, not executor_terminal_error — the loop
                # exhausted against transient network failures, the same outcome
                # as an exhausted status retry.
                if is_transient_exception(exc):
                    result = ExecuteResult.exhausted(
                        retry_count=policy.retry_count,
                        last_error_class=type(exc).__name__,
                        note="transient exception on final attempt",
                    )
                else:
                    result = ExecuteResult.terminal_error(
                        last_error_class=type(exc).__name__,
                        retry_count=attempt,
                    )
                # Escalation is fired centrally by registry.dispatch() — see its
                # docstring. Firing here too double-escalated every failure:
                # two owner cards and two AVXActionTerminalFailure datapoints.
                return result

            last_status = resp.status_code

            if 200 <= resp.status_code < 300:
                # C6 fix: accept the full 2xx range, not only 201.
                # commandId must be present in the body regardless of exact status.
                try:
                    body = resp.json()
                    if not isinstance(body, dict):
                        # Valid JSON but not an object (e.g. `[]`). Without this,
                        # body.get() below raises AttributeError, escapes to
                        # @never_raises, and the audit trail records
                        # "AttributeError" — pointing the operator at a Python
                        # type error rather than at a malformed upstream response.
                        raise TypeError(
                            f"expected a JSON object, got {type(body).__name__}"
                        )
                except Exception as exc:
                    result = ExecuteResult.terminal_error(
                        last_error_class="InvalidJsonResponse",
                        # last_error_status is deliberately OMITTED: this branch is
                        # inside a 2xx, and base.py rejects a success code on any
                        # failure kind. Passing it raised a ValueError INSIDE the
                        # retry loop, which @never_raises then caught — degrading a
                        # precise failure class to 'ValueError' and, before
                        # centralisation, skipping escalation entirely because the
                        # raise preceded the fire() call. The status is in the note.
                        retry_count=attempt,
                        note=(
                            f"HTTP {resp.status_code} but body is not a JSON object: "
                            f"{type(exc).__name__}"
                        ),
                    )
                    # Escalation is fired centrally by registry.dispatch() — see its
                    # docstring. Firing here too double-escalated every failure:
                    # two owner cards and two AVXActionTerminalFailure datapoints.
                    return result

                command_id = body.get("commandId")
                if not command_id:
                    result = ExecuteResult.terminal_error(
                        last_error_class="MissingCommandId",
                        # last_error_status is deliberately OMITTED: this branch is
                        # inside a 2xx, and base.py rejects a success code on any
                        # failure kind. Passing it raised a ValueError INSIDE the
                        # retry loop, which @never_raises then caught — degrading a
                        # precise failure class to 'ValueError' and, before
                        # centralisation, skipping escalation entirely because the
                        # raise preceded the fire() call. The status is in the note.
                        retry_count=attempt,
                        note=f"HTTP {resp.status_code} response did not contain commandId",
                    )
                    # Escalation is fired centrally by registry.dispatch() — see its
                    # docstring. Firing here too double-escalated every failure:
                    # two owner cards and two AVXActionTerminalFailure datapoints.
                    return result

                return ExecuteResult.success(
                    target_ref=command_id,
                    status=ActionStatus.IN_PROGRESS,
                )

            # C4 fix: route ALL non-2xx through classify_status so 401/403/404/408
            # never collapse to `declined`.  The old code used is_transient_status()
            # which treated every non-transient 4xx (including auth failures) as a
            # target refusal, telling the owner "the vehicle refused your command"
            # when in fact our token had expired or the route did not exist.
            disposition = classify_status(resp.status_code)

            if disposition == "transient":
                # 408/425/429/5xx — retry if budget allows
                if attempt < policy.retry_count - 1:
                    sleep_ms = policy.sleep_ms(attempt)
                    self._sleep(sleep_ms / 1000.0)
                    continue
                # Budget exhausted on transient status
                result = ExecuteResult.exhausted(
                    retry_count=policy.retry_count,
                    last_error_class="HTTPError",
                    last_error_status=resp.status_code,
                )
                # Escalation is fired centrally by registry.dispatch() — see its
                # docstring. Firing here too double-escalated every failure:
                # two owner cards and two AVXActionTerminalFailure datapoints.
                return result

            if disposition == "client_error":
                # 401/403/404/405/415 — our fault or plumbing; terminal, escalates.
                # Nothing reached a human, so `declined` (which says "the vehicle
                # refused") would be a false statement about another party.
                result = ExecuteResult.terminal_error(
                    last_error_class="HTTPError",
                    last_error_status=resp.status_code,
                    retry_count=attempt,
                )
                # Escalation is fired centrally by registry.dispatch() — see its
                # docstring. Firing here too double-escalated every failure:
                # two owner cards and two AVXActionTerminalFailure datapoints.
                return result

            # disposition == "refused": genuine target refusal (400, 409, 422, …)
            return ExecuteResult.refused(
                last_error_status=resp.status_code,
                last_error_class="HTTPError",
            )

        # This line is reachable when the retry loop completes without returning
        # inside the loop — which happens when the deadline guard fires on the
        # very first iteration (attempt==0, elapsed>=deadline).  In practice
        # the deadline block returns early, but the compiler cannot see that, and
        # a fall-through here would leave the caller with an unhandled case.
        # Keep it as a genuine safety net rather than dead code.
        result = ExecuteResult.exhausted(
            retry_count=policy.retry_count,
            last_error_class=type(last_exc).__name__ if last_exc else "Unknown",
            last_error_status=last_status,
        )
        # Escalation is fired centrally by registry.dispatch() — see its
        # docstring. Firing here too double-escalated every failure:
        # two owner cards and two AVXActionTerminalFailure datapoints.
        return result
