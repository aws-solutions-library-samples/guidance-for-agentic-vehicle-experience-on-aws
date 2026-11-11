"""CMS executor — spec D4, contract § 4.

Stage B adds ``apply_cost_action`` (spec D9, task 3.3): the executor forwards
the operator's Cognito token to

    POST /api/v1/fleet-intelligence/cost-actions

and maps the response per spec D9 / contract § 10a:

  - 2xx → ``completed`` (the cost action is recorded; "executed" per spec D9)
  - 4xx → ``declined`` / ``target_refused`` with the HTTP status
  - 5xx / 408 / 425 / 429 / connection failure → retry with exponential backoff
    (policy from ``/vsa/{stage}/avx/executor/cms/retry-policy`` SSM or defaults);
    on budget exhaustion → ``failed`` / ``executor_exhausted_retries``
  - Non-transient executor error → ``failed`` / ``executor_terminal_error``

All other CMS ``action_kind`` values are not executable in Stage B; they are
refused as a defence-in-depth safeguard (the policy table marks them
``executable=False`` so the approve route never reaches the executor for them,
but the executor refuses independently).

Unimplemented kinds are refused with a ``target_refused`` result; the operator-facing
sentence comes from ``failure_reason.py`` (contract § 10a row 11 is chosen by
``target_refused`` + ``cms`` + no status), never from the refusal note.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit

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
from agents.executors.target_reason import read_reason_field
from agents.executors.simulated_operator import is_simulated_decider as _is_simulated_decider

log = logging.getLogger(__name__)

_TARGET_SYSTEM = "cms"

#: The one kind this executor handles in Stage B.
_COST_ACTION_KIND = "apply_cost_action"

#: Stage C: ground a vehicle (spec D11).
_GROUND_VEHICLE_KIND = "ground_vehicle"

#: Stage E: move vehicles between a fleet's locations (spec D11, task 4.3).
_MOVE_VEHICLES_KIND = "move_vehicles"

#: The parameters a move needs, all server-set at approve (G4-D11) except the VIN list.
_MOVE_REQUIRED = ("fleet_id", "from_location_id", "to_location_id", "vehicle_vins",
                  "forecast_week", "transfer_cost_usd", "predicted_on_rent_hours_gain",
                  "predicted_turnaways_avoided")

#: Closed list of valid cost_action values (spec D9).
_VALID_COST_ACTIONS = frozenset({
    "shift_to_depot_off_peak",
    "schedule_inspection",
    "reassign_or_remarket",
    "driver_coaching",
})

#: Closed list of valid cause values (spec D6 step 4 / validate.py VALID_CAUSES).
#: Mirrored here so the local guard runs independently of agents.fi_cost.
_VALID_CAUSES = frozenset({
    "charging_rate",
    "charging_mix",
    "repeat_repair",
    "low_utilisation",
    "efficiency",
    "unclear",
})

#: Month format required by CMS (task 1.6 red tests).
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

#: All five fields CMS requires (task 1.6 / decisions item 6).
#: Checked locally so an incomplete request is not recorded as a CMS refusal.
_REQUIRED_PARAMS = ("vehicle_vin", "cost_action", "month", "cause", "finding_id")



def _cms_base_endpoint() -> str:
    """Return the validated CMS API base URL (S2: single helper for all three kinds).

    Reads ``VSA_CMS_API_ENDPOINT``, validates that it is an https URL with a
    named host and no credentials, and returns the stripped base.  Raises
    ``ValueError`` on misconfiguration; ``@never_raises`` on ``dispatch()``
    converts it to ``terminal_error`` with a clear ``last_error_class``.

    This is the single source of truth so a future cert-pinning or proxy change
    only needs to be made here.
    """
    base = os.environ.get("VSA_CMS_API_ENDPOINT", "").strip().rstrip("/")
    if not base:
        raise ValueError(
            "MissingCmsEndpoint: VSA_CMS_API_ENDPOINT is not set"
        )
    # The request carries the approving operator's bearer token, so the endpoint must be
    # HTTPS to a named host before anything is sent (Group 4 security review cycle 1).
    # The message names the problem, never the value.
    parsed = urlsplit(base)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(
            "InvalidCmsEndpoint: VSA_CMS_API_ENDPOINT must be an https:// URL with a host "
            "and no credentials; nothing was sent"
        )
    return base


#: CMS's IAM-only internal API (stage E Group 8, U6; spec group8-notes G8-D1/D8).
_INTERNAL_MOVES_PATH = "/api/v1/fleet-intelligence/internal/moves"
#: CMS's IAM-only internal groundings route (spec D10 Decision 4).
_INTERNAL_GROUNDINGS_PATH = "/api/v1/fleet-intelligence/internal/groundings"
#: CMS's ``FiInternalApiEndpoint`` output, ``https://<id>.execute-api.<region>.amazonaws.com/
#: <stage>/``. The same shape as the synth check in ``vsa-tier2-stack.ts`` and the seeder
#: (``scripts/seed-stage-parameters.sh``); group 1 is the region.
_INTERNAL_ENDPOINT = re.compile(
    r"https://[a-z0-9]{1,32}\.execute-api\.([a-z]{2}(?:-[a-z]+)+-[0-9])\.amazonaws\.com"
    r"/[A-Za-z0-9_-]+/?")


def _cms_internal_endpoint() -> tuple[str, str]:
    """The validated internal-API base URL and the region to sign for.

    Reads ``VSA_CMS_FI_INTERNAL_API_ENDPOINT``, which must be exactly CMS's
    ``FiInternalApiEndpoint`` output (``_INTERNAL_ENDPOINT``): https, an execute-api host, one
    stage segment, at most one trailing slash, and nothing else (no credentials, port, query,
    fragment, upper case or surrounding whitespace). The region comes from the host, so a
    request is only ever signed for the API it is sent to. The auto-approver's
    ``execute-api:Invoke`` grant is built from the same value at deploy (``vsa-tier2-stack.ts``),
    so a value the grant can't match is refused here rather than sent (review c2 W1, c3 S2).
    Raises ``ValueError`` (``@never_raises`` → terminal error, nothing sent) otherwise.
    """
    raw = os.environ.get("VSA_CMS_FI_INTERNAL_API_ENDPOINT", "")
    if not raw.strip():
        raise ValueError("MissingCmsInternalEndpoint: VSA_CMS_FI_INTERNAL_API_ENDPOINT is not set")
    match = _INTERNAL_ENDPOINT.fullmatch(raw)
    if match is None:
        raise ValueError(
            "InvalidCmsInternalEndpoint: VSA_CMS_FI_INTERNAL_API_ENDPOINT must be an "
            "https://<id>.execute-api.<region>.amazonaws.com/<stage> URL; nothing was sent"
        )
    return (raw[:-1] if raw.endswith("/") else raw), match.group(1)


def _lambda_credentials() -> Any:
    """The running role's frozen credentials (the Lambda execution role in AWS)."""
    import botocore.session  # noqa: PLC0415

    creds = botocore.session.get_session().get_credentials()
    if creds is None:
        raise ValueError("MissingAwsCredentials: no credentials to sign with; nothing was sent")
    return creds.get_frozen_credentials()


def _sigv4_headers(credentials: Any, url: str, payload: bytes, region: str) -> dict[str, str]:
    """SigV4 (``execute-api``) headers for a JSON POST of exactly ``payload`` to ``url``."""
    from botocore.auth import SigV4Auth  # noqa: PLC0415
    from botocore.awsrequest import AWSRequest  # noqa: PLC0415

    request = AWSRequest(method="POST", url=url, data=payload,
                         headers={"Content-Type": "application/json"})
    SigV4Auth(credentials, "execute-api", region).add_auth(request)
    return dict(request.headers.items())


def _decided_by_kind(action: Mapping[str, Any]) -> Optional[str]:
    decided_by = action.get("decided_by")
    return decided_by.get("kind") if isinstance(decided_by, Mapping) else None


def _cms_cost_actions_endpoint() -> str:
    """Return the CMS cost-actions endpoint URL.

    Delegates validation to ``_cms_base_endpoint``; raises ``ValueError`` on
    misconfiguration so ``@never_raises`` on ``dispatch()`` converts it to
    ``terminal_error``.
    """
    return f"{_cms_base_endpoint()}/api/v1/fleet-intelligence/cost-actions"


def _cms_groundings_endpoint() -> str:
    """Return the CMS groundings endpoint URL.

    Same https/named-host validation via ``_cms_base_endpoint`` — the token
    is forwarded for both kinds (spec D11 "keep existing check before token is sent").
    """
    return f"{_cms_base_endpoint()}/api/v1/fleet-intelligence/groundings"


def _grounding_body(action: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """Build the CMS groundings request body.

    Contract § 4 v2.7: exactly one of ``recall_id`` (Stage C form) or
    ``fault_codes`` (Stage D fault form) must be present in
    ``parameters``.  Having both is a schema error — the executor
    refuses with ``terminal_error`` (the approve route will have already
    validated this, but the executor guards independently).

    Recall form (Stage C): body = {vin, campaignId, findingId?, actionId?}
    Fault form (Stage D):  body = {vin, fault_codes, findingId?, actionId?}

    Returns ``None`` when ``vehicle_vin`` is missing, or when the form cannot
    be determined (neither recall_id nor fault_codes present).
    Returns the sentinel dict ``{"_both_present": True}`` when both are
    present — the caller maps this to a terminal error.
    """
    parameters = action.get("parameters")
    if not isinstance(parameters, Mapping):
        return None
    vin = parameters.get("vehicle_vin")
    if not isinstance(vin, str) or not vin.strip():
        return None

    has_recall = isinstance(parameters.get("recall_id"), str) and parameters["recall_id"].strip()
    has_faults = isinstance(parameters.get("fault_codes"), list) and bool(parameters["fault_codes"])

    # Contract § 4: exactly one of the two — having both is a schema error.
    if has_recall and has_faults:
        return {"_both_present": True}

    body: dict[str, Any] = {"vin": vin.strip()}

    # Optional provenance fields (from the Action).
    finding_id = action.get("finding_id")
    if isinstance(finding_id, str) and finding_id.strip():
        body["findingId"] = finding_id.strip()
    action_id = action.get("action_id")
    if isinstance(action_id, str) and action_id.strip():
        body["actionId"] = action_id.strip()

    if has_recall:
        # Stage C recall form.
        body["campaignId"] = parameters["recall_id"].strip()
        return body

    if has_faults:
        # Stage D fault form: forward fault_codes as camelCase faultCodes.
        # Design answer 6 (decisions.md "Group 3 review cycle 1"): AVX sends
        # Stage C's VIN form with camelCase codes {vin, faultCodes, findingId, actionId}.
        body["faultCodes"] = list(parameters["fault_codes"])
        return body

    # Neither present — cannot determine the form.
    return None


def _json_number(value: Any) -> Optional[float]:
    """A JSON-safe number from an Action value (DynamoDB gives Decimal), or None."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _move_body(action: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """Build the CMS moves request body, or ``None`` if anything it needs is missing.

    CMS reads the server-set values from the Action itself and uses the body's
    ``vehicle_vins`` (snake_case, as its route test pins); the rest is sent so the record
    can be checked against it. Decimal values from DynamoDB become JSON numbers.
    """
    parameters = action.get("parameters")
    if not isinstance(parameters, Mapping):
        return None
    if any(parameters.get(k) in (None, "") for k in _MOVE_REQUIRED):
        return None
    for key in ("fleet_id", "from_location_id", "to_location_id", "forecast_week"):
        if not isinstance(parameters[key], str) or not parameters[key].strip():
            return None
    vins = parameters["vehicle_vins"]
    if (not isinstance(vins, (list, tuple)) or not vins
            or not all(isinstance(v, str) and v.strip() for v in vins)):
        return None
    numbers = {k: _json_number(parameters[k]) for k in
               ("transfer_cost_usd", "predicted_on_rent_hours_gain", "predicted_turnaways_avoided")}
    if any(v is None for v in numbers.values()):
        return None
    action_id = action.get("action_id")
    finding_id = action.get("finding_id")
    if not (isinstance(action_id, str) and action_id.strip()
            and isinstance(finding_id, str) and finding_id.strip()):
        return None
    return {
        "actionId": action_id.strip(),
        "findingId": finding_id.strip(),
        "fleetId": parameters["fleet_id"],
        "fromLocationId": parameters["from_location_id"],
        "toLocationId": parameters["to_location_id"],
        "vehicle_vins": [v.strip() for v in vins],
        "forecastWeek": parameters["forecast_week"],
        "transferCostUsd": numbers["transfer_cost_usd"],
        "predictedOnRentHoursGain": numbers["predicted_on_rent_hours_gain"],
        "predictedTurnawaysAvoided": int(numbers["predicted_turnaways_avoided"]),
    }


def cost_action_parameter_error(parameters: Mapping[str, Any]) -> Optional[str]:
    """Why ``parameters`` can't be sent to CMS as a cost action, or ``None`` if they can.

    Shared by the AVX approve route (400 before anything is written, so an override
    with a missing or unknown value can't record a decision that then fails for good)
    and by ``_request_body`` (the executor's own guard).  Checks ``vehicle_vin`` (present),
    ``cost_action`` and ``cause`` (closed lists) and ``month`` (YYYY-MM).
    """
    if not isinstance(parameters, Mapping):
        return "parameters must be an object"
    vin = parameters.get("vehicle_vin")
    if not isinstance(vin, str) or not vin.strip():
        return "parameters.vehicle_vin is required (server-set from the Finding's scope)"
    if parameters.get("cost_action") not in _VALID_COST_ACTIONS:
        return f"parameters.cost_action must be one of {sorted(_VALID_COST_ACTIONS)}"
    if parameters.get("cause") not in _VALID_CAUSES:
        return f"parameters.cause must be one of {sorted(_VALID_CAUSES)}"
    month = parameters.get("month")
    if not isinstance(month, str) or not _MONTH_RE.match(month):
        return "parameters.month must be YYYY-MM"
    return None


def _request_body(action: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """Build the CMS cost-actions request body, or ``None`` if a required field is missing.

    All five fields CMS requires are validated locally (task 1.6 / decisions item 6):
      vehicle_vin, cost_action, month (YYYY-MM), cause (closed list), finding_id.
    An incomplete or invalid request returns ``None`` so the executor records it as
    ``MissingDispatchParameter`` rather than letting CMS produce a 4xx that would read
    as a CMS refusal.
    """
    parameters = action.get("parameters")
    if cost_action_parameter_error(parameters) is not None:
        return None

    body: dict[str, Any] = {}

    # vehicle_vin — always required; AVX fills it from the Finding's scope (§ 4).
    vin = parameters.get("vehicle_vin")
    if not isinstance(vin, str) or not vin.strip():
        return None
    body["vehicle_vin"] = vin.strip()

    # cost_action — required and must be one of the closed list (spec D9).
    cost_action = parameters.get("cost_action")
    if cost_action not in _VALID_COST_ACTIONS:
        return None
    body["cost_action"] = cost_action

    # month — required; must match YYYY-MM exactly (CMS task 1.6 red tests).
    month = parameters.get("month")
    if not isinstance(month, str) or not _MONTH_RE.match(month):
        return None
    body["month"] = month

    # cause — required; must be one of the six valid values (spec D6 step 4).
    # An operator-omitted or unknown cause causes a CMS 400; catching locally
    # so the audit trail names MissingDispatchParameter, not CMS refusal.
    cause = parameters.get("cause")
    if cause not in _VALID_CAUSES:
        return None
    body["cause"] = cause

    # finding_id — required so CMS can record the provenance and enforce idempotency
    # on its own record table (decisions item 6).
    finding_id = action.get("finding_id")
    if not isinstance(finding_id, str) or not finding_id.strip():
        return None
    body["finding_id"] = finding_id.strip()

    return body


class CmsExecutor:
    """Executor for ``target_system="cms"``.

    Stage B: handles ``apply_cost_action`` by forwarding the operator's token
    to ``POST /api/v1/fleet-intelligence/cost-actions``.

    All other CMS kinds are refused as a defence-in-depth safeguard — the
    approve route marks them ``executable=False`` and refuses them 400 before
    reaching here, but the executor refuses independently.

    Constructor keyword arguments allow test injection of collaborators:

        ssm_client     boto3 SSM client forwarded to ``load_retry_policy``;
                       default: None (lazy)
        sleep_fn       callable(seconds: float) — injected in tests to avoid
                       real sleeps; default: ``time.sleep``
        retry_policy   injected ``RetryPolicy`` — bypasses SSM in tests
    """

    target_system: str = _TARGET_SYSTEM

    def __init__(
        self,
        *,
        ssm_client: Any = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
        retry_policy: Optional[RetryPolicy] = None,
        credentials_fn: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._ssm_client = ssm_client
        self._sleep = sleep_fn or time.sleep
        # Signs rule-approved moves (G8-D8); injected in tests.
        self._credentials = credentials_fn or _lambda_credentials
        # Policy is loaded LAZILY on first dispatch — not at construction —
        # so that importing the registry produces no SSM I/O.  See DmsExecutor
        # for the full rationale; the same apply here.
        self._retry_policy_cached: Optional[RetryPolicy] = retry_policy

    @property
    def _retry_policy(self) -> RetryPolicy:
        if self._retry_policy_cached is None:
            self._retry_policy_cached = load_retry_policy(
                _TARGET_SYSTEM, ssm_client=self._ssm_client
            )
        return self._retry_policy_cached

    @never_raises
    def dispatch(
        self, action: Mapping[str, Any], claims: Mapping[str, Any]
    ) -> ExecuteResult:
        """Dispatch ``action`` to CMS. Never raises (enforced by ``@never_raises``)."""
        kind = action.get("action_kind")

        # ---- Defence-in-depth: refuse unimplemented kinds -----------------
        # The approve route catches these first (policy.executable=False → 400),
        # but the executor refuses independently.  failure_reason.py picks the
        # operator sentence (§ 10a row 11) from the result kind; the note is not read.
        if kind not in (_COST_ACTION_KIND, _GROUND_VEHICLE_KIND, _MOVE_VEHICLES_KIND):
            return ExecuteResult.refused(
                note="CmsExecutor: only apply_cost_action, ground_vehicle and move_vehicles "
                     "are executable"
            )

        # ---- A rule-approved move has no caller: CMS's IAM-signed internal route ----
        # (stage E Group 8, U6). Routed before the token guard and never given a token;
        # every other decider keeps the token path below (G8-D8).
        if kind == _MOVE_VEHICLES_KIND and _decided_by_kind(action) == "rule":
            return self._dispatch_move_vehicles_signed(action)

        # ---- A simulated-operator grounding: CMS's internal groundings route, signed ----
        # (spec D10 Decision 4 / D10e). Routed before the token guard; no bearer is ever
        # sent even when claims carry a token.
        if kind == _GROUND_VEHICLE_KIND and _is_simulated_decider(action.get("decided_by")):
            return self._dispatch_ground_vehicle_signed(action)

        # ---- Guard: a missing JWT is a LOCAL terminal error ---------------
        # Do not send an empty Authorization header and let CMS produce a 401
        # that would then read as a refusal.  A 401 from CMS means the operator
        # was absent to begin with — a local error, not CMS saying no.
        token = claims.get("token") or claims.get("id_token")
        if not token:
            return ExecuteResult.terminal_error(
                last_error_class="MissingJwt",
                note="jwt_unavailable: no token or id_token in claims",
            )

        # Route ground_vehicle to its own handler (spec D11).
        if kind == _GROUND_VEHICLE_KIND:
            return self._dispatch_ground_vehicle(action, token)
        if kind == _MOVE_VEHICLES_KIND:
            return self._dispatch_move_vehicles(action, token)

        # ---- Guard: missing or invalid parameters -------------------------
        # A request missing vehicle_vin / cost_action / month would produce a
        # CMS 4xx that the spec maps to `declined` — but the request was ours
        # and incomplete, not CMS refusing on grounds of fleet policy.  Caught
        # locally so the audit trail names the correct failure.
        body = _request_body(action)
        if body is None:
            return ExecuteResult.terminal_error(
                last_error_class="MissingDispatchParameter",
                note=(
                    "apply_cost_action requires parameters.vehicle_vin, "
                    "parameters.cost_action (one of: "
                    + ", ".join(sorted(_VALID_COST_ACTIONS))
                    + "), parameters.month (YYYY-MM), "
                    "parameters.cause (one of: "
                    + ", ".join(sorted(_VALID_CAUSES))
                    + "), and action.finding_id"
                ),
            )

        endpoint = _cms_cost_actions_endpoint()
        policy = self._retry_policy
        last_exc: Optional[Exception] = None
        last_status: Optional[int] = None
        attempt = 0
        deadline = time.monotonic() + policy.deadline_s

        while attempt < policy.retry_count:
            if time.monotonic() >= deadline:
                log.warning(
                    "cms executor: deadline exceeded after %d attempt(s); "
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
                    allow_redirects=False,
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                last_status = None
                if is_transient_exception(exc):
                    attempt += 1
                    if attempt < policy.retry_count and time.monotonic() < deadline:
                        sleep_s = policy.sleep_ms(attempt - 1) / 1000.0
                        self._sleep(sleep_s)
                    continue
                # Non-transient network error → terminal
                return ExecuteResult.terminal_error(
                    last_error_class=type(exc).__name__,
                    retry_count=attempt,
                )

            last_status = resp.status_code

            # ---- 3xx: a redirect is never a valid CMS response to a POST ---
            # Passing allow_redirects=False keeps the operator token here;
            # following a redirect would send it to an unintended destination.
            # Same pattern as _move_attempts (~:776).
            if 300 <= resp.status_code < 400:
                return ExecuteResult.terminal_error(
                    last_error_class="UnexpectedRedirect",
                    last_error_status=resp.status_code,
                )

            # ---- 2xx: the cost action was recorded on CMS -----------------
            # "Executed" means the operator's decision is recorded where the
            # fleet's own systems would act on it (spec D9).  The record is
            # immediately complete — there is no separate completion step — so
            # the status is COMPLETED (not IN_PROGRESS).
            if 200 <= resp.status_code < 300:
                return ExecuteResult.success(
                    target_ref=None,
                    status=ActionStatus.COMPLETED,
                )

            # ---- Non-2xx: Stage A's single classification (base.classify_status) ----
            # transient (5xx, 408, 425, 429): retry; client_error (401, 403, 404, 405, 415):
            # our token or our plumbing, terminal and escalates; any other 4xx: CMS
            # deliberately refused, declined/target_refused (spec D9; decisions.md
            # 2026-10-05 "Group 3 review cycle 1" item 8).
            disposition = classify_status(resp.status_code)
            if disposition == "transient":
                attempt += 1
                if attempt < policy.retry_count and time.monotonic() < deadline:
                    sleep_s = policy.sleep_ms(attempt - 1) / 1000.0
                    self._sleep(sleep_s)
                continue
            if disposition == "client_error":
                return ExecuteResult.terminal_error(
                    last_error_class="HTTPError",
                    last_error_status=resp.status_code,
                    retry_count=attempt,
                )
            # CMS's own sentence ({"error": ...}) is passed through word for word
            # (contract § 10b); registry.dispatch() decides whether it is recorded.
            return ExecuteResult.refused(
                last_error_status=resp.status_code,
                last_error_class="HTTPError",
                target_reason=read_reason_field(resp, "error"),
            )

        # ---- Retry budget exhausted ---------------------------------------
        err_class = type(last_exc).__name__ if last_exc else "HTTPError"
        return ExecuteResult.exhausted(
            retry_count=attempt,
            last_error_class=err_class,
            last_error_status=last_status,
        )

    def _dispatch_ground_vehicle(
        self, action: Mapping[str, Any], token: str
    ) -> ExecuteResult:
        """POST /api/v1/fleet-intelligence/groundings.

        Handles both the Stage C recall form (recall_id) and the Stage D fault
        form (fault_codes).  Having both in the parameters is a schema error
        that is refused as a terminal error (contract § 4).

        2xx → executed; 4xx → refused; 5xx after retries → failed (spec D11).
        """
        body = _grounding_body(action)
        if body is None:
            return ExecuteResult.terminal_error(
                last_error_class="MissingDispatchParameter",
                note=(
                    "ground_vehicle requires parameters.vehicle_vin and exactly one of "
                    "parameters.recall_id (recall form) or parameters.fault_codes (fault form)"
                ),
            )
        # Contract § 4: having both recall_id and fault_codes is a schema error.
        if body.get("_both_present"):
            return ExecuteResult.terminal_error(
                last_error_class="AmbiguousGroundingForm",
                note=(
                    "ground_vehicle received both recall_id and fault_codes; "
                    "exactly one of the two is allowed (contract § 4 v2.7)"
                ),
            )
        endpoint = _cms_groundings_endpoint()
        policy = self._retry_policy
        last_exc: Optional[Exception] = None
        last_status: Optional[int] = None
        attempt = 0
        deadline = time.monotonic() + policy.deadline_s
        while attempt < policy.retry_count:
            if time.monotonic() >= deadline:
                log.warning(
                    "cms executor: ground_vehicle deadline exceeded after %d attempt(s)", attempt
                )
                break
            try:
                resp = requests.post(
                    endpoint,
                    json=body,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=policy.per_attempt_timeout_s,
                    allow_redirects=False,
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                last_status = None
                if is_transient_exception(exc):
                    attempt += 1
                    if attempt < policy.retry_count and time.monotonic() < deadline:
                        self._sleep(policy.sleep_ms(attempt - 1) / 1000.0)
                    continue
                return ExecuteResult.terminal_error(
                    last_error_class=type(exc).__name__, retry_count=attempt
                )
            last_status = resp.status_code
            # ---- 3xx: redirect on a POST that carries the operator token ---
            if 300 <= resp.status_code < 400:
                return ExecuteResult.terminal_error(
                    last_error_class="UnexpectedRedirect",
                    last_error_status=resp.status_code,
                )
            if 200 <= resp.status_code < 300:
                return ExecuteResult.success(target_ref=None, status=ActionStatus.EXECUTED)
            disposition = classify_status(resp.status_code)
            if disposition == "transient":
                attempt += 1
                if attempt < policy.retry_count and time.monotonic() < deadline:
                    self._sleep(policy.sleep_ms(attempt - 1) / 1000.0)
                continue
            if disposition == "client_error":
                return ExecuteResult.terminal_error(
                    last_error_status=resp.status_code,
                    last_error_class="HTTPError",
                    retry_count=attempt,
                )
            # CMS's own sentence ({"error": ...}) is passed through word for word
            # (contract § 10b); registry.dispatch() decides whether it is recorded.
            return ExecuteResult.refused(
                last_error_status=resp.status_code,
                last_error_class="HTTPError",
                target_reason=read_reason_field(resp, "error"),
            )
        err_class = type(last_exc).__name__ if last_exc else "HTTPError"
        return ExecuteResult.exhausted(
            retry_count=attempt,
            last_error_class=err_class,
            last_error_status=last_status,
        )

    def _get_cms_endpoint(self) -> str:
        """The CMS API base URL, validated before any token is sent.

        Delegates to the module-level ``_cms_base_endpoint`` helper (S2) so the
        https/named-host/no-credentials guard lives in one place.
        ``@never_raises`` on ``dispatch`` turns a ``ValueError`` into a terminal
        error, nothing sent.
        """
        return _cms_base_endpoint()

    def _dispatch_move_vehicles(
        self, action: Mapping[str, Any], token: str
    ) -> ExecuteResult:
        """POST /api/v1/fleet-intelligence/moves with the operator's token (spec D11).

        2xx → executed (CMS recorded the move and changed the assigned locations); 409
        on the first attempt → refused (§ 10a row 12, CMS deliberately declined);
        5xx after retries → failed; 401/403/404 and any 3xx → terminal error.
        Redirects are never followed: the request carries the operator's bearer token.

        W10 replay safety: if a prior attempt ended in a transient exception (the HTTP
        request may have reached CMS and committed before the response was lost) or a
        5xx (CMS may have committed but failed before sending 2xx), a subsequent 409
        is treated as ``exhausted``/``FAILED`` with an "outcome unknown" note rather
        than ``refused``.  A 409 on the very first attempt is still ``refused``.
        """
        body = _move_body(action)
        if body is None:
            return ExecuteResult.terminal_error(
                last_error_class="MissingDispatchParameter",
                note="move_vehicles requires action_id, finding_id and parameters "
                     + ", ".join(_MOVE_REQUIRED),
            )
        endpoint = f"{self._get_cms_endpoint()}/api/v1/fleet-intelligence/moves"

        def _post(timeout: float) -> Any:
            return requests.post(
                endpoint,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
                timeout=timeout,
                allow_redirects=False,
            )

        return self._move_attempts(body["actionId"], _post)

    def _dispatch_move_vehicles_signed(self, action: Mapping[str, Any]) -> ExecuteResult:
        """POST CMS's internal move route, SigV4-signed by this role (stage E Group 8, U6).

        For ``decided_by.kind == "rule"`` only. No bearer token is sent; the role's
        credentials sign exactly the bytes posted, fresh for each attempt. CMS re-checks
        the rule in AVX's tables before acting (group8-notes G8-D4). Responses map exactly
        as on the operator path (``_move_attempts``).
        """
        body = _move_body(action)
        if body is None:
            return ExecuteResult.terminal_error(
                last_error_class="MissingDispatchParameter",
                note="move_vehicles requires action_id, finding_id and parameters "
                     + ", ".join(_MOVE_REQUIRED),
            )
        base, region = _cms_internal_endpoint()
        endpoint = f"{base}{_INTERNAL_MOVES_PATH}"
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        credentials = self._credentials()

        def _post(timeout: float) -> Any:
            return requests.post(
                endpoint,
                data=payload,
                headers=_sigv4_headers(credentials, endpoint, payload, region),
                timeout=timeout,
                allow_redirects=False,
            )

        return self._move_attempts(body["actionId"], _post)

    def _dispatch_ground_vehicle_signed(self, action: Mapping[str, Any]) -> ExecuteResult:
        """POST CMS's internal groundings route, SigV4-signed (spec D10 Decision 4 / D10e).

        For ``decided_by.kind == "simulated_fleet_operator"`` only. No bearer token is
        ever sent; the role's credentials sign exactly the bytes posted. CMS re-reads the
        Action from AVX's tables before acting. Response mapping is identical to the
        operator-token path (``_dispatch_ground_vehicle``): 2xx → executed; 4xx → refused;
        5xx after retries → failed. Redirects are never followed.
        """
        body = _grounding_body(action)
        if body is None:
            return ExecuteResult.terminal_error(
                last_error_class="MissingDispatchParameter",
                note=(
                    "ground_vehicle requires parameters.vehicle_vin and exactly one of "
                    "parameters.recall_id (recall form) or parameters.fault_codes (fault form)"
                ),
            )
        if body.get("_both_present"):
            return ExecuteResult.terminal_error(
                last_error_class="AmbiguousGroundingForm",
                note=(
                    "ground_vehicle received both recall_id and fault_codes; "
                    "exactly one of the two is allowed (contract § 4 v2.7)"
                ),
            )
        base, region = _cms_internal_endpoint()
        endpoint = f"{base}{_INTERNAL_GROUNDINGS_PATH}"
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        credentials = self._credentials()
        policy = self._retry_policy
        last_exc: Optional[Exception] = None
        last_status: Optional[int] = None
        attempt = 0
        deadline = time.monotonic() + policy.deadline_s
        while attempt < policy.retry_count:
            if time.monotonic() >= deadline:
                log.warning(
                    "cms executor: ground_vehicle (signed) deadline exceeded after %d attempt(s)",
                    attempt,
                )
                break
            try:
                resp = requests.post(
                    endpoint,
                    data=payload,
                    headers=_sigv4_headers(credentials, endpoint, payload, region),
                    timeout=policy.per_attempt_timeout_s,
                    allow_redirects=False,
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                last_status = None
                if is_transient_exception(exc):
                    attempt += 1
                    if attempt < policy.retry_count and time.monotonic() < deadline:
                        self._sleep(policy.sleep_ms(attempt - 1) / 1000.0)
                    continue
                return ExecuteResult.terminal_error(
                    last_error_class=type(exc).__name__, retry_count=attempt
                )
            last_status = resp.status_code
            if 300 <= resp.status_code < 400:
                return ExecuteResult.terminal_error(
                    last_error_class="UnexpectedRedirect",
                    last_error_status=resp.status_code,
                )
            if 200 <= resp.status_code < 300:
                return ExecuteResult.success(target_ref=None, status=ActionStatus.EXECUTED)
            disposition = classify_status(resp.status_code)
            if disposition == "transient":
                attempt += 1
                if attempt < policy.retry_count and time.monotonic() < deadline:
                    self._sleep(policy.sleep_ms(attempt - 1) / 1000.0)
                continue
            if disposition == "client_error":
                return ExecuteResult.terminal_error(
                    last_error_status=resp.status_code,
                    last_error_class="HTTPError",
                    retry_count=attempt,
                )
            return ExecuteResult.refused(
                last_error_status=resp.status_code,
                last_error_class="HTTPError",
                target_reason=read_reason_field(resp, "error"),
            )
        err_class = type(last_exc).__name__ if last_exc else "HTTPError"
        return ExecuteResult.exhausted(
            retry_count=attempt,
            last_error_class=err_class,
            last_error_status=last_status,
        )

    def _move_attempts(self, action_id: str, post: Callable[[float], Any]) -> ExecuteResult:
        """The move request's retry loop and response mapping, for both deciders."""
        policy = self._retry_policy
        last_exc: Optional[Exception] = None
        last_status: Optional[int] = None
        attempt = 0
        # True once an attempt ended in a transient exception or a 5xx — the move
        # request may have been processed by CMS even though we never got 2xx.
        uncertain_commit = False
        deadline = time.monotonic() + policy.deadline_s
        while attempt < policy.retry_count:
            if time.monotonic() >= deadline:
                log.warning("cms executor: move_vehicles deadline exceeded after %d attempt(s)",
                            attempt)
                break
            try:
                resp = post(policy.per_attempt_timeout_s)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                last_status = None
                if is_transient_exception(exc):
                    # The HTTP request may have been received and committed by CMS even
                    # though the response was never received.  Mark commit uncertain.
                    uncertain_commit = True
                    attempt += 1
                    if attempt < policy.retry_count and time.monotonic() < deadline:
                        self._sleep(policy.sleep_ms(attempt - 1) / 1000.0)
                    continue
                return ExecuteResult.terminal_error(
                    last_error_class=type(exc).__name__, retry_count=attempt)
            last_status = resp.status_code
            if 200 <= resp.status_code < 300:
                return ExecuteResult.success(target_ref=action_id,
                                             status=ActionStatus.EXECUTED)
            if 300 <= resp.status_code < 400:
                return ExecuteResult.terminal_error(
                    last_error_class="UnexpectedRedirect",
                    last_error_status=resp.status_code, retry_count=attempt)
            disposition = classify_status(resp.status_code)
            if disposition == "transient":
                # 5xx: CMS may have committed but failed before it could send 2xx.
                uncertain_commit = True
                attempt += 1
                if attempt < policy.retry_count and time.monotonic() < deadline:
                    self._sleep(policy.sleep_ms(attempt - 1) / 1000.0)
                continue
            if disposition == "client_error":
                return ExecuteResult.terminal_error(
                    last_error_status=resp.status_code, last_error_class="HTTPError",
                    retry_count=attempt)
            # "refused" 4xx (400, 409, etc.)
            if resp.status_code == 409 and uncertain_commit:
                # A prior attempt's outcome is unknown: the move may have committed.
                # Report as exhausted/FAILED so the operator is alerted to verify,
                # rather than as refused (which would suggest nothing happened).
                # Note: last_error_status is not carried here — the validator rejects
                # EXECUTOR_EXHAUSTED_RETRIES with a terminal 4xx status, and the
                # uncertainty came from the prior attempt, not this 409.
                return ExecuteResult.exhausted(
                    retry_count=attempt,
                    last_error_class="HTTPError",
                    note="outcome unknown: a prior attempt may have committed; "
                         "verify CMS before retrying",
                )
            # CMS's own sentence ({"error": ...}), word for word (contract § 10b).
            return ExecuteResult.refused(
                last_error_status=resp.status_code, last_error_class="HTTPError",
                target_reason=read_reason_field(resp, "error"))
        err_class = type(last_exc).__name__ if last_exc else "HTTPError"
        return ExecuteResult.exhausted(
            retry_count=attempt, last_error_class=err_class, last_error_status=last_status)
