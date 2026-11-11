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

import json
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
from agents.executors.target_reason import read_reason_field
from agents.executors.simulated_operator import is_simulated_decider as _is_simulated_decider

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
    # recall_id — carried for book_recall_service so DMS adds the recall line (spec D11).
    recall_id = parameters.get("recall_id")
    if isinstance(recall_id, str) and recall_id.strip():
        body["recall_id"] = recall_id.strip()
        # 11.3c: name the campaign when this is a recall booking (deterministic; 11.5 DMS
        # internal route rebuilds and compares with the same rule).
        if action.get("action_kind") == "book_recall_service":
            body["description"] = f"FI recall {recall_id.strip()}"
    return body


def _dms_fleet_service_endpoint() -> str:
    base = os.environ.get("VSA_DMS_API_ENDPOINT", "").rstrip("/")
    return f"{base}/api/dms/fleet/repair-orders"


#: DMS's IAM-only internal RestApi endpoint (spec D10 Decision 4).
#: Same shape as CMS's ``_INTERNAL_ENDPOINT``: https, one execute-api host, one stage
#: segment, optional trailing slash, region extracted from the host. Validated at
#: dispatch time; region comes from the host so requests are always signed for the
#: API they are sent to.
_INTERNAL_ENDPOINT_RE = re.compile(
    r"https://[a-z0-9]{1,32}\.execute-api\.([a-z]{2}(?:-[a-z]+)+-[0-9])\.amazonaws\.com"
    r"/[A-Za-z0-9_-]+/?")


def _dms_internal_endpoint() -> tuple[str, str]:
    """The validated DMS internal-API base URL and the region to sign for.

    Reads ``VSA_DMS_INTERNAL_API_ENDPOINT``, which must be exactly DMS's internal
    RestApi output: https, an execute-api host, one stage segment, at most one trailing
    slash, and nothing else (no credentials, port, query, fragment). The region comes
    from the host. Raises ``ValueError`` (``@never_raises`` → terminal error, nothing
    sent) otherwise.
    """
    raw = os.environ.get("VSA_DMS_INTERNAL_API_ENDPOINT", "")
    if not raw.strip():
        raise ValueError(
            "MissingDmsInternalEndpoint: VSA_DMS_INTERNAL_API_ENDPOINT is not set"
        )
    match = _INTERNAL_ENDPOINT_RE.fullmatch(raw)
    if match is None:
        raise ValueError(
            "InvalidDmsInternalEndpoint: VSA_DMS_INTERNAL_API_ENDPOINT must be an "
            "https://<id>.execute-api.<region>.amazonaws.com/<stage> URL; nothing was sent"
        )
    return (raw[:-1] if raw.endswith("/") else raw), match.group(1)


_MAX_BOOKING_DESC = 200

#: Words a dealer reads for a need's ``need_id`` prefix (``fi_fleet_health/bands.py``):
#: ``pm:<task>`` is scheduled maintenance; ``a2:<finding_kind>`` (e.g. ``health.battery``)
#: is shown by its finding kind alone. Unknown prefixes are shown as they are.
_NEED_KIND_LABELS = {"pm": "maintenance", "a2": ""}


def _booking_description_from_needs(needs: list) -> str:
    """Build a human-readable description from a needs list; max 200 chars.

    Format: ``FI fleet service: fault codes P0A0D, P0A7F; pm TIRE_ROTATION; a2 health.battery``
    Fault codes (``need_id`` ``fault:<code>``) are listed first, grouped under "fault codes".
    Non-fault needs follow in order, each as ``<kind> <code>`` (kind = prefix before ``:``,
    or the whole ``need_id`` when there is no colon).  Fault codes are de-duplicated by value,
    preserving first-seen order.  If the full description would exceed 200 chars, items are
    dropped from the end and replaced with "and N more".

    This is a pure function of the Action parameters; DMS 11.5 rebuilds it with the same rule
    and compares (spec 11.3c / 11.5).
    """
    fault_codes: list[str] = []
    other_items: list[str] = []
    seen_faults: set[str] = set()

    for need in needs:
        if not isinstance(need, Mapping):
            continue
        need_id = need.get("need_id")
        if not isinstance(need_id, str) or not need_id:
            continue
        if need_id.startswith("fault:"):
            code = need_id[6:]
            if code and code not in seen_faults:
                fault_codes.append(code)
                seen_faults.add(code)
        else:
            sep = need_id.find(":")
            if sep >= 0:
                kind = need_id[:sep].strip()
                rest = need_id[sep + 1:].strip()
                label = _NEED_KIND_LABELS.get(kind, kind)
                item = (f"{label} {rest}" if label else rest) if rest else kind
            else:
                item = need_id.strip()
            if item:
                other_items.append(item)

    prefix = "FI fleet service"
    if not fault_codes and not other_items:
        return prefix

    # All display tokens: individual fault codes first, then other items
    all_tokens: list[str] = fault_codes + other_items
    total = len(all_tokens)

    def _assemble(fault_subset: list[str], other_subset: list[str], suffix: str = "") -> str:
        sections: list[str] = []
        if fault_subset:
            sections.append("fault codes " + ", ".join(fault_subset))
        sections.extend(other_subset)
        return f"{prefix}: {'; '.join(sections)}{suffix}"

    full = _assemble(fault_codes, other_items)
    if len(full) <= _MAX_BOOKING_DESC:
        return full

    # Truncate: try decreasing token counts until it fits
    for n in range(total - 1, 0, -1):
        f_n = fault_codes[:n] if n <= len(fault_codes) else fault_codes
        o_n = other_items[:max(0, n - len(fault_codes))]
        omitted = total - n
        candidate = _assemble(f_n, o_n, f" and {omitted} more")
        if len(candidate) <= _MAX_BOOKING_DESC:
            return candidate

    # Worst case: all items omitted
    return f"{prefix} and {total} more"


def _booking_evidence_from_needs(needs: list) -> Optional[dict[str, Any]]:
    """Return ``{"dtcs": [...]}`` when at least one fault need exists, else ``None``.

    Fault codes are taken from ``need_id`` values that start with ``fault:``, in need
    order, de-duplicated (first occurrence wins), capped at 50.  DMS validates and stores
    this: ``repair_orders.py`` ``_validate_evidence`` accepts ``dtcs`` as a list ≤ 50
    entries; the fleet page renders them via ``_fleet_evidence``.
    """
    dtcs: list[str] = []
    seen: set[str] = set()
    for need in needs:
        if not isinstance(need, Mapping):
            continue
        need_id = need.get("need_id")
        if not isinstance(need_id, str):
            continue
        if need_id.startswith("fault:"):
            code = need_id[6:]
            if code and code not in seen:
                dtcs.append(code)
                seen.add(code)
            if len(dtcs) >= 50:
                break
    return {"dtcs": dtcs} if dtcs else None


def _fleet_service_body(action: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """Build the DMS fleet-service request body for ``book_fleet_service``.

    Lines are built exclusively from the server-set ``needs`` list in the
    Action parameters.  The operator may not supply raw ``lines`` — only
    ``dealer_id`` and ``urgency`` can differ from the agent's recommendation
    (contract § 4 / spec D9).

    Each need in ``parameters.needs`` becomes a line with:
      ``labor_op``   from ``need["labor_op"]`` (required)
      ``description``  from ``need.get("description")`` (optional)
      ``pay_type``   always ``"customer_pay"`` (spec D9)
      ``need_id``    from ``need["need_id"]``, sent on every line whose need has one; every
                     need AVX builds has one (``fi_fleet_health/bands.py``). DMS's dealer
                     stand-in recognises a fleet-health booking only by a line carrying
                     ``need_id`` (DMS ``is_fi_fleet_health_booking``), so dropping it leaves
                     the booking at ``Draft``; pinned by ``tests/test_fleet_service_need_id.py``.

    Returns ``None`` when ``vehicle_vin``, ``dealer_id``, or ``needs`` is missing.
    """
    parameters = action.get("parameters")
    if not isinstance(parameters, Mapping):
        return None

    vin = parameters.get("vehicle_vin")
    if not isinstance(vin, str) or not vin.strip():
        return None

    dealer_id = parameters.get("dealer_id")
    if not isinstance(dealer_id, str) or not dealer_id.strip():
        return None

    needs = parameters.get("needs")
    if not isinstance(needs, list) or not needs:
        return None

    lines = []
    for idx, need in enumerate(needs):
        if not isinstance(need, Mapping):
            # W2 (G3F1.5): malformed need at executor level is a terminal error —
            # the approve route (C1 fix) should have caught this already, but
            # defend independently per executor dispatch abstraction.
            return None  # caller maps None to MissingDispatchParameter / terminal_error
        labor_op = need.get("labor_op")
        if not isinstance(labor_op, str) or not labor_op.strip():
            return None  # same: malformed need → refuse the whole booking
        line: dict[str, Any] = {
            "line_no": idx + 1,  # W3 c fix: DMS stores NUMBER — stage C G3-6 string rule
            # is for the warranty-claim *request* field only (decisions.md G3 cycle 2 answer 7).
            "labor_op": labor_op.strip(),
            "pay_type": "customer_pay",  # spec D9: always customer_pay for fleet health
        }
        description = need.get("description")
        if isinstance(description, str) and description.strip():
            line["description"] = description.strip()
        need_id = need.get("need_id")
        if isinstance(need_id, str) and need_id.strip():
            line["need_id"] = need_id.strip()
        lines.append(line)

    if not lines:
        return None

    body: dict[str, Any] = {
        "vehicle_vin": vin.strip(),
        "dealer_id": dealer_id.strip(),
        "lines": lines,
        "description": _booking_description_from_needs(needs),
    }

    # evidence.dtcs: present only when at least one fault need exists (spec 11.3c).
    evidence = _booking_evidence_from_needs(needs)
    if evidence is not None:
        body["evidence"] = evidence

    # Optional fields
    urgency = parameters.get("urgency")
    if isinstance(urgency, str) and urgency.strip():
        body["urgency"] = urgency.strip()

    correlation = _dms_correlation_id(action.get("action_id"))
    if correlation is not None:
        body["avx_action_id"] = correlation

    return body


def _dms_warranty_claims_endpoint() -> str:
    base = os.environ.get("VSA_DMS_API_ENDPOINT", "").rstrip("/")
    return f"{base}/api/dms/fleet/warranty-claims"


_WARRANTY_CLAIM_REQUIRED = ("vehicle_vin", "ro_id", "line_no", "dealer_id", "labor_op")


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


def _warranty_claim_body(action: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """Build the DMS warranty-claims request body, or None if a required field is missing."""
    parameters = action.get("parameters")
    if not isinstance(parameters, Mapping):
        return None
    body: dict[str, Any] = {}
    for field in _WARRANTY_CLAIM_REQUIRED:
        value = parameters.get(field)
        # G3-6: line_no travels as a string; an int (DMS's own type) is accepted too.
        if field == "line_no" and isinstance(value, int) and not isinstance(value, bool):
            value = str(value)
        if not isinstance(value, str) or not value.strip():
            return None
        body[field] = value.strip()
    for opt in ("fault_code", "notes"):
        value = parameters.get(opt)
        if isinstance(value, str) and value.strip():
            body[opt] = value.strip()
    correlation = _dms_correlation_id(action.get("action_id"))
    if correlation is not None:
        body["avx_action_id"] = correlation
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
        credentials_fn: Optional[Callable[[], Any]] = None,
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
        # Signs simulated-operator decisions (spec D10 Decision 4); injected in tests.
        self._credentials = credentials_fn or _lambda_credentials

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
        # Route submit_warranty_claim to its own handler (spec D11).
        if action.get("action_kind") == "submit_warranty_claim":
            # D10e: simulated decider goes to the internal signed route before the token guard.
            if _is_simulated_decider(action.get("decided_by")):
                return self._dispatch_warranty_claim_signed(action)
            return self._dispatch_warranty_claim(action, claims)

        # Route book_fleet_service to its own handler (spec D9 / Stage D task 3.7).
        if action.get("action_kind") == "book_fleet_service":
            # D10e: simulated decider goes to the internal signed route before the token guard.
            if _is_simulated_decider(action.get("decided_by")):
                return self._dispatch_fleet_service_signed(action)
            # D7 / G8-AVX-2: rule decider goes to the signed route (no bearer token).
            # Local import avoids circular dependency (registry imports dms).
            from agents.executors.registry import _is_valid_rule_decider  # noqa: PLC0415
            if _is_valid_rule_decider(action.get("decided_by")):
                return self._dispatch_fleet_service_signed(action)
            return self._dispatch_fleet_service(action, claims)

        # D10e: simulated decider for book_recall_service goes to the internal signed
        # route before the token guard. Operator/other deciders fall through to the
        # main loop below, which uses _request_body (preserving existing behaviour).
        if (action.get("action_kind") == "book_recall_service"
                and _is_simulated_decider(action.get("decided_by"))):
            return self._dispatch_recall_service_signed(action)

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

    def _dispatch_fleet_service(
        self, action: Mapping[str, Any], claims: Mapping[str, Any]
    ) -> ExecuteResult:
        """POST /api/dms/fleet/repair-orders for ``book_fleet_service``.

        Lines are built exclusively from the server-set ``needs`` list — the
        operator may only change ``dealer_id`` and ``urgency`` (contract § 4).
        Response mapping: 2xx → in_progress; 4xx → refused/terminal; 5xx → retry.
        """
        token = claims.get("token") or claims.get("id_token")
        if not token:
            return ExecuteResult.terminal_error(
                last_error_class="MissingJwt",
                note="jwt_unavailable: no token or id_token in claims",
            )

        body = _fleet_service_body(action)
        if body is None:
            return ExecuteResult.terminal_error(
                last_error_class="MissingDispatchParameter",
                note=(
                    "book_fleet_service requires parameters.vehicle_vin, "
                    "parameters.dealer_id, and a non-empty parameters.needs list"
                ),
            )

        endpoint = _dms_fleet_service_endpoint()
        policy = self._retry_policy
        last_exc: Optional[Exception] = None
        last_status: Optional[int] = None
        attempt = 0
        deadline = time.monotonic() + policy.deadline_s

        # Retries are safe because DMS deduplicates on avx_action_id.
        max_attempts = policy.retry_count if "avx_action_id" in body else 1

        while attempt < max_attempts:
            if time.monotonic() >= deadline:
                log.warning(
                    "dms executor: book_fleet_service deadline exceeded after %d attempt(s)",
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
                        self._sleep(policy.sleep_ms(attempt - 1) / 1000.0)
                    continue
                return ExecuteResult.terminal_error(
                    last_error_class=type(exc).__name__,
                    retry_count=attempt,
                )

            last_status = resp.status_code

            if 200 <= resp.status_code < 300:
                try:
                    ro_body = resp.json()
                    ro_id = ro_body.get("ro_id") if isinstance(ro_body, dict) else None
                except (ValueError, TypeError):
                    ro_id = None
                if ro_id is None:
                    return ExecuteResult.terminal_error(
                        last_error_class="MissingRoId",
                        retry_count=attempt,
                    )
                return ExecuteResult.success(
                    target_ref=ro_id,
                    status=ActionStatus.IN_PROGRESS,
                )

            # DMS 400/409 are request validation, not refusals (same as book_recall_service).
            if resp.status_code in (400, 409):
                return ExecuteResult.terminal_error(
                    last_error_status=resp.status_code,
                    last_error_class="HTTPError",
                    retry_count=attempt,
                )

            disposition = classify_status(resp.status_code)
            if disposition == "transient":
                attempt += 1
                if attempt < max_attempts and time.monotonic() < deadline:
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
            )

        err_class = type(last_exc).__name__ if last_exc else "HTTPError"
        return ExecuteResult.exhausted(
            retry_count=attempt,
            last_error_class=err_class,
            last_error_status=last_status,
        )

    def _dispatch_warranty_claim(
        self, action: Mapping[str, Any], claims: Mapping[str, Any]
    ) -> ExecuteResult:
        """POST /api/dms/fleet/warranty-claims.

        201/202 → executed; 422 → refused; 400 → failed (spec D11).
        DMS deduplicates on avx_action_id so retries are safe.
        """
        token = claims.get("token") or claims.get("id_token")
        if not token:
            return ExecuteResult.terminal_error(
                last_error_class="MissingJwt",
                note="jwt_unavailable: no token or id_token in claims",
            )
        body = _warranty_claim_body(action)
        if body is None:
            return ExecuteResult.terminal_error(
                last_error_class="MissingDispatchParameter",
                note="submit_warranty_claim requires: " + ", ".join(_WARRANTY_CLAIM_REQUIRED),
            )
        endpoint = _dms_warranty_claims_endpoint()
        policy = self._retry_policy
        last_exc: Optional[Exception] = None
        last_status: Optional[int] = None
        attempt = 0
        deadline = time.monotonic() + policy.deadline_s
        while attempt < policy.retry_count:
            if time.monotonic() >= deadline:
                log.warning(
                    "dms executor: warranty-claim deadline exceeded after %d attempt(s)", attempt
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
                    if attempt < policy.retry_count and time.monotonic() < deadline:
                        self._sleep(policy.sleep_ms(attempt - 1) / 1000.0)
                    continue
                return ExecuteResult.terminal_error(
                    last_error_class=type(exc).__name__, retry_count=attempt
                )
            last_status = resp.status_code
            if 200 <= resp.status_code < 300:
                # 201 (approved/Submitted) and 202 (requires_review/Validated) are
                # both "executed" — DMS has the claim (spec D11, decisions 2026-10-05 call 9).
                return ExecuteResult.success(target_ref=None, status=ActionStatus.EXECUTED)
            if resp.status_code == 400:
                # Validation failure (spec D11 → D-G10-8): failed, not refused.
                return ExecuteResult.terminal_error(
                    last_error_status=resp.status_code,
                    last_error_class="HTTPError",
                    retry_count=attempt,
                )
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
            # 422: coverage denied (spec D11 → refused / target_refused).
            # DMS's verdict_reason is its deterministic warranty verdict (a reason
            # code such as miles_term_expired), passed through word for word
            # (contract § 10b). Read only on a 422: this duplicates
            # registry.dispatch()'s DMS-422 gate on purpose, so the body of any
            # other refusal is never even parsed.
            return ExecuteResult.refused(
                last_error_status=resp.status_code,
                last_error_class="HTTPError",
                target_reason=(
                    read_reason_field(resp, "verdict_reason")
                    if resp.status_code == 422 else None
                ),
            )
        err_class = type(last_exc).__name__ if last_exc else "HTTPError"
        return ExecuteResult.exhausted(
            retry_count=attempt,
            last_error_class=err_class,
            last_error_status=last_status,
        )

    # ------------------------------------------------------------------
    # Dual escalation (spec § "Retry count + escalation target")
    # ------------------------------------------------------------------

    def _dispatch_fleet_service_signed(self, action: Mapping[str, Any]) -> ExecuteResult:
        """POST DMS's internal repair-orders route, SigV4-signed (spec D10 Decision 4 / D10e).

        For ``decided_by.kind == "simulated_fleet_operator"`` only, ``book_fleet_service``.
        No bearer token is ever sent. Body from ``_fleet_service_body``.
        2xx → in_progress; 4xx → refused/failed; 5xx after retries → failed.
        """
        body = _fleet_service_body(action)
        if body is None:
            return ExecuteResult.terminal_error(
                last_error_class="MissingDispatchParameter",
                note=(
                    "book_fleet_service (simulated) requires "
                    "parameters.vehicle_vin, parameters.dealer_id, and a non-empty "
                    "parameters.needs list"
                ),
            )
        return self._signed_ro_post(body, action_id_label="fleet-service")

    def _dispatch_recall_service_signed(self, action: Mapping[str, Any]) -> ExecuteResult:
        """POST DMS's internal repair-orders route, SigV4-signed (spec D10 Decision 4 / D10e).

        For ``decided_by.kind == "simulated_fleet_operator"`` only, ``book_recall_service``.
        No bearer token is ever sent. Body from ``_request_body`` (the same body the operator
        route sends: vehicle_vin, dealer_id, avx_action_id, and recall_id).
        2xx → in_progress; 4xx → refused/failed; 5xx after retries → failed.
        """
        body = _request_body(action)
        if body is None:
            return ExecuteResult.terminal_error(
                last_error_class="MissingDispatchParameter",
                note="book_recall_service (simulated) requires: "
                     + ", ".join(_REQUIRED_PARAMETERS),
            )
        return self._signed_ro_post(body, action_id_label="recall-service")

    def _signed_ro_post(self, body: dict, *, action_id_label: str) -> ExecuteResult:
        """Shared signed POST to the internal repair-orders route. Never raises."""
        base, region = _dms_internal_endpoint()
        endpoint = f"{base}/api/dms/internal/fleet/repair-orders"
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        credentials = self._credentials()
        policy = self._retry_policy
        last_exc: Optional[Exception] = None
        last_status: Optional[int] = None
        attempt = 0
        max_attempts = policy.retry_count if "avx_action_id" in body else 1
        deadline = time.monotonic() + policy.deadline_s
        while attempt < max_attempts:
            if time.monotonic() >= deadline:
                log.warning(
                    "dms executor: %s (signed) deadline exceeded after %d attempt(s)",
                    action_id_label, attempt,
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
                    if attempt < max_attempts and time.monotonic() < deadline:
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
                try:
                    ro_body = resp.json()
                    ro_id = ro_body.get("ro_id") if isinstance(ro_body, dict) else None
                except (ValueError, TypeError):
                    ro_id = None
                if ro_id is None:
                    return ExecuteResult.terminal_error(
                        last_error_class="MissingRoId", retry_count=attempt
                    )
                return ExecuteResult.success(target_ref=ro_id, status=ActionStatus.IN_PROGRESS)
            if resp.status_code in (400, 409):
                return ExecuteResult.terminal_error(
                    last_error_status=resp.status_code,
                    last_error_class="HTTPError",
                    retry_count=attempt,
                )
            disposition = classify_status(resp.status_code)
            if disposition == "transient":
                attempt += 1
                if attempt < max_attempts and time.monotonic() < deadline:
                    self._sleep(policy.sleep_ms(attempt - 1) / 1000.0)
                continue
            if disposition == "client_error":
                return ExecuteResult.terminal_error(
                    last_error_status=resp.status_code,
                    last_error_class="HTTPError",
                    retry_count=attempt,
                )
            return ExecuteResult.refused(
                last_error_status=resp.status_code, last_error_class="HTTPError"
            )
        err_class = type(last_exc).__name__ if last_exc else "HTTPError"
        return ExecuteResult.exhausted(
            retry_count=attempt, last_error_class=err_class, last_error_status=last_status
        )

    def _dispatch_warranty_claim_signed(self, action: Mapping[str, Any]) -> ExecuteResult:
        """POST DMS's internal warranty-claims route, SigV4-signed (spec D10 Decision 4 / D10e).

        For ``decided_by.kind == "simulated_fleet_operator"`` only. No bearer token is ever
        sent. 2xx → executed; 422 → refused; 400 → failed.
        """
        body = _warranty_claim_body(action)
        if body is None:
            return ExecuteResult.terminal_error(
                last_error_class="MissingDispatchParameter",
                note="submit_warranty_claim (simulated) requires: "
                     + ", ".join(_WARRANTY_CLAIM_REQUIRED),
            )
        base, region = _dms_internal_endpoint()
        endpoint = f"{base}/api/dms/internal/fleet/warranty-claims"
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
                    "dms executor: warranty-claim (signed) deadline exceeded after "
                    "%d attempt(s)", attempt
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
            if resp.status_code == 400:
                return ExecuteResult.terminal_error(
                    last_error_status=resp.status_code,
                    last_error_class="HTTPError",
                    retry_count=attempt,
                )
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
                last_error_status=resp.status_code, last_error_class="HTTPError",
                target_reason=(
                    read_reason_field(resp, "verdict_reason")
                    if resp.status_code == 422 else None
                ),
            )
        err_class = type(last_exc).__name__ if last_exc else "HTTPError"
        return ExecuteResult.exhausted(
            retry_count=attempt, last_error_class=err_class, last_error_status=last_status
        )

