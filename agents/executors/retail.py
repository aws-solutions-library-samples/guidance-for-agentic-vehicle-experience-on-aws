"""RetailExecutor — dispatch retail actions to the VSA acquire API.

Spec: `.kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/spec.md`
      § "Executor dispatch abstraction", § "Retail action kinds"

Supported action_kinds
----------------------
``lead_capture``
    POST ${VSA_API_BASE}/acquire/leads.  Cognito-authorized.
    Required body field: ``tenantId`` — sourced from the **caller's claims**,
    never from the action payload (sourcing it from the payload would let a
    caller submit a lead under another tenant's identity).

``configurator_recommend``
    Also POST ${VSA_API_BASE}/acquire/leads, carrying the chosen configuration in
    ``savedConfig``; ``target_ref`` is the returned ``leadId``.

    Why the same endpoint, and not a new one: ``configurator_recommend`` in
    ``agents/supervisor/tools/`` is a **pure read** — it queries the catalog table,
    ranks, and returns ``{"recommendations": [...]}`` with zero write operations.
    A recommendation is therefore a *derived ranking*, not a durable entity, which
    is why no ``config_id`` exists to return. The configuration concept that IS
    durable in this system is ``savedConfig``, an attribute of a lead. So an owner
    approving a recommended configuration registers interest in that configuration
    — which is a lead carrying a savedConfig.

    ``savedConfig`` is REQUIRED for this action_kind. Dispatching without it would
    create a lead indistinguishable from a bare ``lead_capture``, silently losing
    the configuration the owner actually approved; this executor fails closed
    instead. Per R4's precedent it does not synthesise one from ``modelId``.

    Decided in ``.kiro/specs/2026-09-20-avx-acquire-configurator-target/spec.md``
    (Option A). An earlier revision of this docstring claimed the tool "writes
    DynamoDB directly" and that a new ``config_id``-minting endpoint was required;
    both were wrong, and the follow-on they justified would have invented a durable
    entity the domain does not have.

Offer-term / disclosure invariant
-----------------------------------
This executor passes ``contactInfo``, ``modelId``, ``savedConfig``,
``idempotencyKey``, and ``discoverSessionId`` through from the action payload
without inspection.  It MUST NOT compute, alter, or re-narrate any price, rate, or
disclosure string — those fields are deterministic seams owned by the downstream
system.  If they appear in the action payload at all they are opaque forwards.
"""

from __future__ import annotations

import logging
import os
import time as time_module
from time import monotonic as _monotonic
from typing import Any, Callable, Mapping, Optional

from agents.executors.base import (
    ActionStatus,
    ExecuteResult,
    classify_status,
    is_transient_exception,
    load_retry_policy,
    never_raises,
)

log = logging.getLogger(__name__)

#: Target system identifier — matched by the registry and used in audit entries.
TARGET_SYSTEM = "retail"

#: action_kinds this executor handles end-to-end. Both POST /acquire/leads; they
#: differ only in whether a savedConfig is required. See the module docstring.
_SUPPORTED_KINDS = frozenset({"lead_capture", "configurator_recommend"})

#: action_kinds that additionally REQUIRE a savedConfig in parameters. Dispatching
#: one without it would create a lead indistinguishable from a bare lead_capture,
#: silently dropping the configuration the owner approved — so it fails closed.
_CONFIGURATOR_KINDS = frozenset({"configurator_recommend"})


def _api_base() -> str:
    """VSA API base URL.  Must not end with a slash."""
    return os.environ.get("VSA_API_BASE", "").rstrip("/")


def _leads_url() -> str:
    return f"{_api_base()}/acquire/leads"


def _extract_tenant_id(claims: Mapping[str, Any]) -> Optional[str]:
    """Return the caller's tenantId from claims.

    The handler checks both ``custom:tenantId`` (Cognito custom attribute
    shape) and ``tenantId`` (plain claim shape).  We mirror that look-up so
    the executor and the Lambda agree on the source of truth.
    """
    return claims.get("custom:tenantId") or claims.get("tenantId") or None


class RetailExecutor:
    """Dispatches retail AVX actions to the VSA acquire API.

    Implements the :class:`~agents.executors.base.Executor` Protocol.

    The ``dispatch`` method is wrapped with ``@never_raises`` so any
    unhandled exception is converted to ``failed / executor_terminal_error``
    rather than propagating to the approve route and leaving the Action row
    stuck on ``pending_execution``.
    """

    target_system: str = TARGET_SYSTEM

    def __init__(
        self,
        *,
        http_client: Any = None,
        ssm_client: Any = None,
        sleep: Callable[[float], None] = time_module.sleep,
    ) -> None:
        """
        Parameters
        ----------
        http_client:
            Injected ``requests.Session`` (or compatible).  When omitted a
            real session is created lazily on first dispatch.  Tests MUST
            inject a stub — this executor never touches the network in tests.
        ssm_client:
            Injected SSM client for ``load_retry_policy``.  When omitted the
            default boto3 SSM client is used.
        sleep:
            Injectable sleep callable.  Defaults to ``time.sleep``.  Tests
            inject a no-op to avoid real delays, consistent with the other
            two HTTP executors.
        """
        self._http = http_client
        self._ssm = ssm_client
        self._sleep = sleep

    def _get_http(self) -> Any:
        if self._http is None:
            import requests  # noqa: PLC0415 — lazy; Lambda bundles requests

            self._http = requests.Session()
        return self._http

    # ------------------------------------------------------------------
    # Core dispatch — wrapped with @never_raises below
    # ------------------------------------------------------------------

    def _dispatch_inner(
        self, action: Mapping[str, Any], claims: Mapping[str, Any]
    ) -> ExecuteResult:
        action_kind = action.get("action_kind", "")

        # ── Fail closed for unsupported kinds ─────────────────────────────
        if action_kind not in _SUPPORTED_KINDS:
            return ExecuteResult.terminal_error(
                last_error_class="UnsupportedActionKind",
                note=(
                    f"action_kind={action_kind!r} is not supported by RetailExecutor. "
                    f"Supported: {sorted(_SUPPORTED_KINDS)}."
                ),
            )

        # ── configurator_recommend requires the configuration it approves ──
        #
        # Without savedConfig this would POST a lead carrying no configuration —
        # indistinguishable from a bare lead_capture, and the owner's actual choice
        # would be silently lost while the Action reported success. R4's precedent:
        # refuse rather than synthesise. It is NOT derived from modelId, because a
        # model id is not a configuration and inventing one would be exactly the
        # fabrication R4 forbade.
        if action_kind in _CONFIGURATOR_KINDS:
            saved_config = (action.get("parameters") or {}).get("savedConfig")
            if not saved_config:
                return ExecuteResult.terminal_error(
                    last_error_class="MissingSavedConfig",
                    note=(
                        f"action_kind={action_kind!r} requires a non-empty "
                        "parameters.savedConfig — it is the configuration the owner "
                        "approved, and the lead is meaningless without it. Refusing "
                        "rather than submitting a lead with no configuration."
                    ),
                )

        # ── both kinds POST /acquire/leads ────────────────────────────────
        return self._dispatch_lead_capture(action, claims)

    def _dispatch_lead_capture(
        self, action: Mapping[str, Any], claims: Mapping[str, Any]
    ) -> ExecuteResult:
        """POST /acquire/leads — create a lead record.

        ``tenantId`` is sourced from the caller's claims, not from the action
        payload.  Sourcing it from the payload would let a caller submit a
        lead against another tenant's account by supplying a foreign tenantId.
        """
        # ── Caller JWT — hard requirement ─────────────────────────────────
        #
        # POST /acquire/leads sits behind the same API Gateway Cognito authorizer
        # as every other route in this API; there is no M2M credential path. The
        # first cut of this executor sent no Authorization header at all, which is
        # a fail-closed functional bug (every request 401s, retries, and burns the
        # budget into a terminal failure) and, had the route been reachable by the
        # Lambda's own role instead, a confused deputy: the executor would have
        # acted with its own authority on behalf of a caller whose entitlement was
        # never checked. Forwarded, like dms and vehicle-command do.
        jwt: str = claims.get("token") or claims.get("id_token") or claims.get("jwt") or ""
        if not jwt:
            missing_jwt = ExecuteResult.terminal_error(
                last_error_class="MissingJWT",
                note=(
                    "no token/id_token/jwt in claims — cannot forward the caller's "
                    "credential to the Cognito-authorized Acquire API"
                ),
            )
            # Fire the escalation, matching dms.py. terminal_error sets
            # escalate=True, so returning without firing reproduces exactly the
            # present-but-inert shape that Cycle 1 Critical 2 was about: the
            # result claims an escalation that never happens.
            # Escalation is fired centrally by registry.dispatch() — see its
            # docstring. Firing here too double-escalated every failure:
            # two owner cards and two AVXActionTerminalFailure datapoints.
            return missing_jwt

        # ── Tenant ID from claims — hard requirement ───────────────────────
        tenant_id = _extract_tenant_id(claims)
        if not tenant_id:
            return ExecuteResult.terminal_error(
                last_error_class="MissingTenantClaim",
                note=(
                    "Caller claims contain no tenantId (checked 'custom:tenantId' "
                    "and 'tenantId'). Cannot submit lead without a verified tenant "
                    "identity — failing closed to prevent cross-tenant data leakage."
                ),
            )

        # ── Build request body (pass-through only; no re-narration) ───────
        params = action.get("parameters") or {}
        body: dict[str, Any] = {"tenantId": tenant_id}

        # Optional forwarded fields — opaque pass-through; executor MUST NOT
        # inspect or transform any price/rate/disclosure string that may appear
        # inside contactInfo.
        for field_name in (
            "sessionId", "contactInfo", "modelId", "discoverSessionId",
            # savedConfig carries the approved configuration (configurator_recommend);
            # idempotencyKey lets a caller pin the lead's identity explicitly rather
            # than relying on the endpoint's payload-hash derivation. Both are opaque
            # forwards — this executor does not read inside either.
            "savedConfig", "idempotencyKey",
        ):
            if params.get(field_name) is not None:
                body[field_name] = params[field_name]

        # ── Retry loop ────────────────────────────────────────────────────
        policy = load_retry_policy(TARGET_SYSTEM, ssm_client=self._ssm)
        http = self._get_http()
        url = _leads_url()
        last_exc_name: Optional[str] = None
        last_status: Optional[int] = None

        # Overall deadline. The per-attempt timeout bounds a single request; the
        # deadline bounds the SEQUENCE, which is what API Gateway actually kills.
        # Review Cycle 2 Critical 4: this executor had a per-attempt timeout but
        # no deadline at all, so a run of slow-but-not-timing-out attempts could
        # still overrun the synchronous budget. dms and vehicle_command both
        # enforce one; retail did not.
        deadline = _monotonic() + policy.deadline_s

        for attempt in range(policy.retry_count):
            # Sleep before retry attempts (not before the first attempt).
            if attempt > 0:
                sleep_s = policy.sleep_ms(attempt - 1) / 1000.0
                self._sleep(sleep_s)

            if _monotonic() >= deadline:
                # Out of budget mid-sequence. Report exhaustion rather than
                # letting the gateway kill the route with the Action still on
                # pending_execution.
                return ExecuteResult.exhausted(
                    retry_count=attempt,
                    last_error_class=last_exc_name or "DeadlineExceeded",
                    last_error_status=last_status if last_status else None,
                    note=(
                        f"retry sequence abandoned after {attempt} attempt(s): "
                        f"{policy.deadline_s:.0f}s synchronous budget exhausted"
                    ),
                )

            try:
                resp = http.post(
                    url,
                    json=body,
                    timeout=policy.per_attempt_timeout_s,
                    headers={
                        "Authorization": f"Bearer {jwt}",
                        "Content-Type": "application/json",
                    },
                )
                last_status = resp.status_code

                # ── 2xx success ───────────────────────────────────────────
                if 200 <= resp.status_code < 300:
                    # Wrapped, so a malformed 2xx body lands on the SAME class its
                    # siblings report. Without the try, the guard below raised into
                    # @never_raises and this executor recorded 'TypeError' /
                    # 'JSONDecodeError' where dms and vehicle_command record
                    # 'InvalidJsonResponse' — an operator reading two audit trails
                    # for one underlying failure saw two different records.
                    try:
                        data = resp.json()
                        if not isinstance(data, dict):
                            raise TypeError(
                                f"expected a JSON object, got {type(data).__name__}"
                            )
                    except (ValueError, TypeError) as exc:
                        # last_error_status omitted: this is a 2xx, and base.py
                        # rejects a success code on a failure kind.
                        return ExecuteResult.terminal_error(
                            last_error_class="InvalidJsonResponse",
                            retry_count=attempt,
                            note=(
                                f"HTTP {resp.status_code} body unusable "
                                f"({type(exc).__name__}: not a JSON object)"
                            ),
                        )
                    lead_id = data.get("leadId")
                    if not lead_id:
                        # A 2xx without a leadId is unreconcilable: the lead
                        # may or may not have been captured on the dealer side,
                        # and there is no handle to join against the Acquire
                        # surface.  Treat as a terminal executor error so the
                        # operator is alerted and the owner's card does not
                        # report a spurious success.
                        #
                        # last_error_status is intentionally omitted: the new
                        # ExecuteResult validation correctly rejects a 2xx code
                        # on a failure kind (a 2xx recorded as failure means the
                        # response was misread).  The HTTP status is captured in
                        # the note for the operator audit trail.
                        return ExecuteResult.terminal_error(
                            last_error_class="MissingLeadId",
                            note=(
                                f"POST /acquire/leads returned {resp.status_code} "
                                "but the response body contained no 'leadId'. "
                                "The lead cannot be reconciled; failing as "
                                "executor_terminal_error."
                            ),
                        )
                    return ExecuteResult.success(
                        target_ref=lead_id,
                        status=ActionStatus.COMPLETED,
                        note=f"Lead captured: leadId={lead_id}",
                    )

                # ── Non-2xx — classify via the shared D5 function ─────────
                disposition = classify_status(resp.status_code)

                if disposition == "transient":
                    # 5xx or non-refusal 4xx (408/425/429) — retry.
                    log.warning(
                        "retail executor: transient status=%d attempt=%d/%d",
                        resp.status_code,
                        attempt + 1,
                        policy.retry_count,
                    )
                    continue

                if disposition == "client_error":
                    # 401/403/404/405/415 — our fault or our plumbing; terminal,
                    # escalates. Never "dealer refused" — nothing reached a human.
                    log.warning(
                        "retail executor: client error status=%d for tenantId=%s; "
                        "terminal, escalating",
                        resp.status_code,
                        tenant_id,
                    )
                    result = ExecuteResult.terminal_error(
                        last_error_class="HTTPError",
                        last_error_status=resp.status_code,
                        note=(
                            f"POST /acquire/leads returned {resp.status_code}: "
                            "auth/routing failure, not a dealer refusal"
                        ),
                    )
                    # Escalation is fired centrally by registry.dispatch() — see its
                    # docstring. Firing here too double-escalated every failure:
                    # two owner cards and two AVXActionTerminalFailure datapoints.
                    return result

                # disposition == "refused" — 4xx the target deliberately chose.
                log.warning(
                    "retail executor: lead capture refused status=%d for tenantId=%s",
                    resp.status_code,
                    tenant_id,
                )
                return ExecuteResult.refused(
                    # HTTPError, matching dms and vehicle_command. retail left this
                    # None, so one underlying failure produced two different audit
                    # records depending on which executor handled it.
                    last_error_class="HTTPError",
                    last_error_status=resp.status_code,
                    note=f"POST /acquire/leads returned {resp.status_code}",
                )

            except Exception as exc:  # noqa: BLE001
                if is_transient_exception(exc):
                    last_exc_name = type(exc).__name__
                    log.warning(
                        "retail executor: transient %s attempt=%d/%d",
                        last_exc_name,
                        attempt + 1,
                        policy.retry_count,
                    )
                    continue
                # Non-transient network exception — re-raise so @never_raises
                # catches it and maps to terminal_error.
                raise

        # ── Budget exhausted ──────────────────────────────────────────────
        result = ExecuteResult.exhausted(
            retry_count=policy.retry_count,
            # Fall back to HTTPError when the budget was spent on statuses rather
            # than exceptions, so the class matches the siblings.
            last_error_class=last_exc_name or ("HTTPError" if last_status else None),
            last_error_status=last_status,
            note=(
                f"POST /acquire/leads: retry budget of {policy.retry_count} "
                "attempts exhausted"
            ),
        )
        # Escalation is fired centrally by registry.dispatch() — see its
        # docstring. Firing here too double-escalated every failure:
        # two owner cards and two AVXActionTerminalFailure datapoints.
        return result

    @never_raises
    def dispatch(
        self, action: Mapping[str, Any], claims: Mapping[str, Any]
    ) -> ExecuteResult:
        """Dispatch a retail action.  Never raises — see ``@never_raises``."""
        return self._dispatch_inner(action, claims)
