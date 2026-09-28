"""Executor contract — Protocol, result type, failure taxonomy, policy config.

Spec: `.kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/spec.md`
      § "Executor dispatch abstraction", § "Cross-surface Action data model",
      Addendum D5 ("Terminal executor failure lands on `failed`, not `declined`")

This module is the frozen contract that all six executors implement. It carries
no target-specific logic.

Three things here are load-bearing and each is mutation-tested by the executor
that depends on it:

1.  **The 4xx / 5xx split (Addendum D5).** `declined` and `failed` are *different
    terminal states with different meanings*: `declined` is "the dealer said no"
    (the downstream target refused the work, terminal 4xx, no retry, no
    escalation); `failed` is "we could not reach the dealer" (transient 5xx /
    network / timeout, retry budget exhausted, escalation fires). Collapsing them
    would tell an owner their service request was refused when in fact nobody
    ever saw it. `ExecuteResult` cannot represent a `failed` without a
    `failure_kind`, and cannot represent a `declined` that escalates.

2.  **`dispatch()` never raises.** The caller is the synchronous approve route;
    an exception there would leave the Action row written but statusless, with
    the owner's card stuck on `pending_execution` forever. `@never_raises`
    converts any escape into `failed` / `executor_terminal_error` so the state
    machine always advances. This is an invariant, not a convenience: it is why
    the Protocol's docstring says "Never raises".

3.  **No policy literal lives in code.** `retry_count`, backoff bounds, and the
    escalation shape are read from SSM (Addenda D3/D5) so the future admin
    configurator can drive them without a deploy. A hardcoded retry count would
    silently outrank the operator's configuration.

Config source of truth: `deployment/config/avx-policy-defaults.json`, seeded into
SSM by `deployment/scripts/seed_avx_policy_params.sh`.
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Protocol, runtime_checkable

log = logging.getLogger(__name__)

#: API Gateway's hard synchronous timeout. The approve route runs inside it, so
#: the entire retry sequence (requests + backoff) must finish first. 25s leaves
#: ~4s of headroom for the route's own work either side of dispatch.
SYNCHRONOUS_BUDGET_S = 25.0

#: Floor for a derived per-attempt timeout, so a pathological policy cannot
#: produce a timeout too short for any real request to complete.
_MIN_ATTEMPT_TIMEOUT_S = 2.0

#: Upper bounds enforced when loading policy from SSM. An operator raising a
#: number in Parameter Store must not be able to push the synchronous approve
#: route past the gateway limit — the clamp is logged, not silent.
_MAX_RETRY_COUNT = 5
_MAX_BACKOFF_CAP_MS = 8000


# ---------------------------------------------------------------------------
# Action status + failure taxonomy (spec § "Cross-surface Action data model")
# ---------------------------------------------------------------------------


class ActionStatus:
    """The `Action.status` enum values an executor may write.

    `cancelled` and `dismissed` are owner-driven transitions written by the
    HTTP routes, never by an executor, and are therefore absent here — an
    executor that could write `dismissed` could forge owner intent.
    """

    PENDING_EXECUTION = "pending_execution"
    IN_PROGRESS = "in_progress"
    EXECUTED = "executed"
    COMPLETED = "completed"
    DECLINED = "declined"
    FAILED = "failed"


#: Statuses an executor is permitted to return. Enforced by `ExecuteResult`.
EXECUTOR_WRITABLE_STATUSES = frozenset(
    {
        ActionStatus.IN_PROGRESS,
        ActionStatus.EXECUTED,
        ActionStatus.COMPLETED,
        ActionStatus.DECLINED,
        ActionStatus.FAILED,
    }
)

#: Terminal statuses — no further transition follows.
TERMINAL_STATUSES = frozenset({ActionStatus.COMPLETED, ActionStatus.DECLINED, ActionStatus.FAILED})


class FailureKind:
    """`audit_trail[].failure_kind` values (Addendum D5)."""

    #: Downstream target refused the work. Terminal 4xx. No retry, no escalation.
    TARGET_REFUSED = "target_refused"
    #: Retry budget exhausted against a transient failure. Escalation fires.
    EXECUTOR_EXHAUSTED_RETRIES = "executor_exhausted_retries"
    #: Executor-side terminal error — malformed response, TLS failure, credential
    #: decode error, or any unexpected escape caught by `@never_raises`.
    EXECUTOR_TERMINAL_ERROR = "executor_terminal_error"


#: `declined` carries exactly this kind; `failed` carries one of the other two.
_KIND_FOR_STATUS: dict[str, frozenset[str]] = {
    ActionStatus.DECLINED: frozenset({FailureKind.TARGET_REFUSED}),
    ActionStatus.FAILED: frozenset(
        {FailureKind.EXECUTOR_EXHAUSTED_RETRIES, FailureKind.EXECUTOR_TERMINAL_ERROR}
    ),
}


# ---------------------------------------------------------------------------
# ExecuteResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecuteResult:
    """What an executor returns. Immutable; validated on construction.

    The validation is the point. A result that says `status=failed` with no
    `failure_kind`, or `status=declined` with `escalate=True`, is a D5 violation
    and is rejected here rather than being written to the Action row and
    surfaced to an owner as a wrong explanation of what happened.
    """

    status: str
    target_ref: Optional[str] = None
    failure_kind: Optional[str] = None
    retry_count: int = 0
    last_error_class: Optional[str] = None
    last_error_status: Optional[int] = None
    #: True iff the D5 dual escalation must fire. Only ever True for `failed`.
    escalate: bool = False
    note: Optional[str] = None

    def __post_init__(self) -> None:
        if self.status not in EXECUTOR_WRITABLE_STATUSES:
            raise ValueError(
                f"ExecuteResult.status={self.status!r} is not executor-writable. "
                f"Permitted: {sorted(EXECUTOR_WRITABLE_STATUSES)}. "
                "Owner-driven statuses (cancelled, dismissed) are written by the "
                "HTTP routes, never by an executor."
            )

        expected_kinds = _KIND_FOR_STATUS.get(self.status)
        if expected_kinds is not None:
            if self.failure_kind is None:
                raise ValueError(
                    f"status={self.status!r} requires a failure_kind "
                    f"(one of {sorted(expected_kinds)}) — Addendum D5 makes the "
                    "audit trail the only place an owner-facing card can tell "
                    "'dealer refused' apart from 'we could not reach the dealer'."
                )
            if self.failure_kind not in expected_kinds:
                raise ValueError(
                    f"failure_kind={self.failure_kind!r} is not valid for "
                    f"status={self.status!r}; expected one of {sorted(expected_kinds)}. "
                    "Addendum D5: target_refused belongs to `declined`; "
                    "exhausted_retries / terminal_error belong to `failed`."
                )
        elif self.failure_kind is not None:
            raise ValueError(
                f"status={self.status!r} is a success status and must not carry "
                f"failure_kind={self.failure_kind!r}."
            )

        # Escalation is a property of `failed` alone (Addendum D5: "`failed`
        # triggers escalation per config; `declined` does not"). Both directions
        # are checked: an earlier version guarded only declined-must-not-escalate,
        # and that asymmetry meant a `failed` result could silently carry
        # escalate=False — which is exactly how five of six executors shipped a
        # terminal failure that escalated to nobody.
        if self.escalate and self.status != ActionStatus.FAILED:
            raise ValueError(
                f"escalate=True is only valid with status={ActionStatus.FAILED!r}, "
                f"got {self.status!r}. Addendum D5: `declined` does not escalate — "
                "escalating a refusal would tell the owner we failed to reach a "
                "dealer who in fact answered and said no."
            )
        if self.status == ActionStatus.FAILED and not self.escalate:
            raise ValueError(
                "status=failed requires escalate=True. Addendum D5: '`failed` "
                "triggers escalation per config'. A terminal failure that "
                "escalates to nobody leaves the owner with a dead card and no "
                "operator signal — construct via ExecuteResult.exhausted() or "
                ".terminal_error(), which set this correctly."
            )

        if self.retry_count < 0:
            raise ValueError(f"retry_count must be >= 0, got {self.retry_count}")

        # A refusal is answered on the first attempt by definition — there is no
        # retry on 4xx. A non-zero count here means a 4xx was retried.
        if self.failure_kind == FailureKind.TARGET_REFUSED and self.retry_count != 0:
            raise ValueError(
                f"failure_kind=target_refused requires retry_count=0, got "
                f"{self.retry_count}. Terminal 4xx is never retried (Addendum D5)."
            )

        self._validate_error_status()

    def _validate_error_status(self) -> None:
        """Cross-check `last_error_status` against `failure_kind`.

        The status code and the failure kind are two statements about the same
        event, and a disagreement between them means one of the two is lying to
        whoever reads the audit trail. Three incoherent pairs are rejected:

        * **A 2xx on any failure status.** A successful response recorded as a
          failure. This is not hypothetical — executors that gated success on
          `status_code == 201` were reporting a 200-with-valid-payload as
          `declined`, discarding the reference id.
        * **`target_refused` with a non-refusal code.** A 5xx is "we could not
          reach them", never "they said no"; and 408/425/429 are transient.
          Refusal means a 4xx the target chose to send.
        * **`executor_exhausted_retries` with a terminal 4xx.** The budget can
          only be exhausted against something retryable, so a terminal 4xx here
          means a refusal was retried.

        `executor_terminal_error` deliberately accepts any non-2xx (or None): it
        is the kind that carries auth/plumbing failures (401/403/404) and local
        errors that never produced a status at all.
        """
        code = self.last_error_status
        if code is None:
            return
        if self.failure_kind is None:
            return

        if 200 <= code < 300:
            raise ValueError(
                f"last_error_status={code} is a success code but failure_kind="
                f"{self.failure_kind!r}. A 2xx recorded as a failure means the "
                "response was misread — check the success range, not just == 201."
            )

        if self.failure_kind == FailureKind.TARGET_REFUSED:
            if not (400 <= code < 500):
                raise ValueError(
                    f"failure_kind=target_refused requires a 4xx, got {code}. "
                    "A 5xx is 'we could not reach the target', not 'the target "
                    "refused' — that is the whole point of Addendum D5."
                )
            if code in _NON_REFUSAL_4XX:
                raise ValueError(
                    f"last_error_status={code} is a transient 4xx and must not be "
                    f"recorded as target_refused. {sorted(_NON_REFUSAL_4XX)} are "
                    "retryable (timeout / too-early / rate-limited), not refusals."
                )

        if self.failure_kind == FailureKind.EXECUTOR_EXHAUSTED_RETRIES:
            if 400 <= code < 500 and code not in _NON_REFUSAL_4XX:
                raise ValueError(
                    f"failure_kind=executor_exhausted_retries with "
                    f"last_error_status={code}: a terminal 4xx is never retried, "
                    "so the retry budget cannot have been exhausted against it. "
                    "Either the code is wrong or a refusal was retried."
                )

    # -- convenience constructors -------------------------------------------

    @classmethod
    def success(cls, *, target_ref: Optional[str], status: str = ActionStatus.IN_PROGRESS,
                note: Optional[str] = None) -> "ExecuteResult":
        return cls(status=status, target_ref=target_ref, note=note)

    @classmethod
    def refused(cls, *, last_error_status: Optional[int] = None,
                last_error_class: Optional[str] = None,
                note: Optional[str] = None) -> "ExecuteResult":
        """Terminal 4xx — the target refused. No retry, no escalation."""
        return cls(
            status=ActionStatus.DECLINED,
            failure_kind=FailureKind.TARGET_REFUSED,
            retry_count=0,
            last_error_status=last_error_status,
            last_error_class=last_error_class,
            escalate=False,
            note=note,
        )

    @classmethod
    def exhausted(cls, *, retry_count: int, last_error_class: Optional[str] = None,
                  last_error_status: Optional[int] = None,
                  note: Optional[str] = None) -> "ExecuteResult":
        """Transient failure, retry budget spent. Escalation fires."""
        return cls(
            status=ActionStatus.FAILED,
            failure_kind=FailureKind.EXECUTOR_EXHAUSTED_RETRIES,
            retry_count=retry_count,
            last_error_class=last_error_class,
            last_error_status=last_error_status,
            escalate=True,
            note=note,
        )

    @classmethod
    def terminal_error(cls, *, last_error_class: Optional[str] = None,
                       last_error_status: Optional[int] = None,
                       retry_count: int = 0,
                       note: Optional[str] = None) -> "ExecuteResult":
        """Executor-side terminal error. Escalation fires (owner is still owed
        an explanation that the work did not reach the target)."""
        return cls(
            status=ActionStatus.FAILED,
            failure_kind=FailureKind.EXECUTOR_TERMINAL_ERROR,
            retry_count=retry_count,
            last_error_class=last_error_class,
            last_error_status=last_error_status,
            escalate=True,
            note=note,
        )

    # -- audit trail --------------------------------------------------------

    def audit_entry(
        self,
        *,
        by: str,
        from_status: str = ActionStatus.PENDING_EXECUTION,
        at: Optional[str] = None,
    ) -> dict[str, Any]:
        """Build the `Action.audit_trail[-1]` entry for this transition.

        Shape per spec § Action: `{at, by, from_status, to_status, note?,
        failure_kind?, retry_count?}`. `failure_kind` and `retry_count` are
        populated only on a failure transition, so a success entry stays clean.
        """
        entry: dict[str, Any] = {
            "at": at or utc_now_iso(),
            "by": by,
            "from_status": from_status,
            "to_status": self.status,
        }
        if self.failure_kind is not None:
            entry["failure_kind"] = self.failure_kind
            entry["retry_count"] = self.retry_count
            if self.last_error_class is not None:
                entry["last_error_class"] = self.last_error_class
            # An explicit null is meaningful: a connection error / timeout has no
            # HTTP status, and the operator reading the trail needs to see that
            # rather than an absent key they must guess about.
            if self.failure_kind != FailureKind.TARGET_REFUSED:
                entry["last_error_status"] = self.last_error_status
            elif self.last_error_status is not None:
                entry["last_error_status"] = self.last_error_status
        if self.note:
            entry["note"] = self.note
        return entry


def utc_now_iso() -> str:
    """ISO-8601 UTC, seconds precision, `Z`-suffixed — matches spec fixtures."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# The Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Executor(Protocol):
    """Dispatches an approved Action to its downstream target system.

    Called by `POST /findings/{id}/approve` *after* the Action row is written,
    so a dispatch failure never loses the owner's intent — the row already
    exists and the returned `ExecuteResult` only advances its status.

    MUST NOT be called from any Tier 2 agent (test-enforced by
    `tests/test_registry.py`). Tier 2 writes Findings; it does not act on them.

    Implementations MUST NOT raise — see `@never_raises`.
    """

    target_system: str

    def dispatch(self, action: Mapping[str, Any], claims: Mapping[str, Any]) -> ExecuteResult:
        ...


def never_raises(fn: Callable[..., ExecuteResult]) -> Callable[..., ExecuteResult]:
    """Enforce the Protocol's "Never raises" clause.

    Any exception escaping `dispatch` becomes `failed` /
    `executor_terminal_error`, because the alternative is an Action row stuck on
    `pending_execution` with an owner-facing card that never resolves.

    The exception is logged by **class name only**, with no message and no
    traceback. This is not fastidiousness: exception messages routinely echo
    request and response content, so `requests` and `botocore` messages here
    would carry forwarded JWTs, response bodies, and VINs out of the marker
    payload. An earlier version of this wrapper used `log.exception`, which is
    `log.error(..., exc_info=True)` and therefore emitted the whole traceback
    including `ExceptionClass: <message>` — contradicting this very docstring.
    Do not reintroduce `exc_info`; `test_never_raises_does_not_log_secrets`
    fails if you do.
    """

    def _wrapped(self: Any, *args: Any, **kwargs: Any) -> ExecuteResult:
        try:
            return fn(self, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — deliberate catch-all, see docstring
            # Class name only. No %s of the exception, no exc_info=True.
            log.error(
                "executor %s: unhandled %s escaped dispatch; mapping to failed/"
                "executor_terminal_error (message withheld: it can carry a JWT, "
                "a response body, or a VIN)",
                getattr(self, "target_system", type(self).__name__),
                type(exc).__name__,
            )
            return ExecuteResult.terminal_error(last_error_class=type(exc).__name__)

    _wrapped.__name__ = getattr(fn, "__name__", "dispatch")
    _wrapped.__doc__ = fn.__doc__
    _wrapped.__wrapped__ = fn  # type: ignore[attr-defined]
    return _wrapped


# ---------------------------------------------------------------------------
# Policy configuration (Addenda D3, D5) — read from SSM, never hardcoded
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryPolicy:
    """Per-target transient-failure retry policy, with a wall-clock budget.

    `retry_count` is the *total* attempt budget (3 = initial attempt + 2
    retries), matching the config's own rationale note.

    The wall-clock fields exist because the sub-29s property is load-bearing,
    not advisory: the approve route is synchronous behind API Gateway, whose hard
    limit is 29 seconds. The config's own "~6 seconds worst case" estimate counts
    *backoff only* and omits the request timeouts, which dominate — three stalled
    attempts at a 10s per-request timeout is 30s before any backoff at all, so
    the route dies at the gateway with the Action still `pending_execution`.

    `per_attempt_timeout_s` and `deadline_s` are therefore derived from the
    budget rather than hardcoded per executor, and `retry_count` /
    `backoff_cap_ms` are clamped so an operator cannot configure the route past
    the gateway limit by raising a number in SSM.
    """

    retry_count: int = 3
    backoff_base_ms: int = 1000
    backoff_cap_ms: int = 4000

    def __post_init__(self) -> None:
        if self.retry_count < 1:
            raise ValueError(f"retry_count must be >= 1 (got {self.retry_count})")
        if self.backoff_base_ms < 0 or self.backoff_cap_ms < 0:
            raise ValueError("backoff values must be >= 0")
        if self.backoff_cap_ms < self.backoff_base_ms:
            raise ValueError(
                f"backoff_cap_ms ({self.backoff_cap_ms}) < backoff_base_ms "
                f"({self.backoff_base_ms}) — the cap would truncate the first sleep"
            )
        self._fit_to_budget()

    def _fit_to_budget(self) -> None:
        """Shrink the backoff window until the worst case fits the budget.

        The budget must hold for **every** constructible combination, not just the
        staging default. An earlier version derived `per_attempt_timeout_s` from
        whatever the backoff left over — but floored it at
        `_MIN_ATTEMPT_TIMEOUT_S`, and that floor silently defeated the
        derivation: once backoff ate the budget, the floor kept handing out 2s
        per attempt anyway, so `retry_count * 2s + backoff` ran past the limit.
        Five reachable combinations exceeded it, the worst at 42s against a 29s
        gateway timeout — and `backoff_base_ms` was never bounded at all.

        Backoff is reduced rather than `retry_count` because attempts are the
        thing the policy is *for*; waiting longer between them is the negotiable
        part. If even a zero backoff cannot fit the requested attempts, the
        attempt count is reduced as a last resort — an operator who asks for more
        attempts than the synchronous budget can hold gets as many as will fit,
        loudly, rather than a route the gateway kills mid-dispatch.

        `object.__setattr__` because the dataclass is frozen; this runs once, at
        construction, before anything can observe the old values.
        """
        floor_total = self.retry_count * _MIN_ATTEMPT_TIMEOUT_S
        allowance_ms = max(0.0, (SYNCHRONOUS_BUDGET_S - floor_total)) * 1000.0
        sleeps = max(0, self.retry_count - 1)

        if sleeps and self.max_backoff_total_s * 1000.0 > allowance_ms:
            # Largest cap whose worst-case sum fits: sum_{i<sleeps} min(cap, base*2^i)
            # is monotonic in cap, so a direct per-sleep share is a safe bound.
            new_cap = int(allowance_ms // sleeps)
            object.__setattr__(self, "backoff_cap_ms", max(0, new_cap))
            object.__setattr__(
                self, "backoff_base_ms", min(self.backoff_base_ms, self.backoff_cap_ms)
            )
            log.warning(
                "retry policy backoff reduced to fit the %.0fs synchronous budget "
                "(retry_count=%d -> backoff_cap_ms=%d, backoff_base_ms=%d); the "
                "approve route runs inside API Gateway's timeout",
                SYNCHRONOUS_BUDGET_S,
                self.retry_count,
                self.backoff_cap_ms,
                self.backoff_base_ms,
            )

        # Last resort: even at this backoff the attempts alone overrun.
        while (
            self.retry_count > 1
            and self.retry_count * _MIN_ATTEMPT_TIMEOUT_S + self.max_backoff_total_s
            > SYNCHRONOUS_BUDGET_S
        ):
            object.__setattr__(self, "retry_count", self.retry_count - 1)
            log.warning(
                "retry_count reduced to %d: the requested attempt count cannot fit "
                "the %.0fs synchronous budget even at minimum per-attempt timeout",
                self.retry_count,
                SYNCHRONOUS_BUDGET_S,
            )

    @property
    def worst_case_wall_clock_s(self) -> float:
        """Upper bound on a full retry sequence: all attempts plus all backoff."""
        return self.retry_count * self.per_attempt_timeout_s + self.max_backoff_total_s

    @property
    def max_backoff_total_s(self) -> float:
        """Worst-case total time spent sleeping across the whole budget."""
        return sum(
            min(self.backoff_cap_ms, self.backoff_base_ms * (2 ** i))
            for i in range(max(0, self.retry_count - 1))
        ) / 1000.0

    @property
    def deadline_s(self) -> float:
        """Total wall-clock the retry loop may consume, backoff included."""
        return SYNCHRONOUS_BUDGET_S

    @property
    def per_attempt_timeout_s(self) -> float:
        """Per-request timeout that keeps `retry_count` attempts inside the budget.

        Derived, not chosen: whatever is left of the budget after worst-case
        backoff, divided across the attempts. Floored so a pathological config
        cannot produce a timeout so small that nothing can ever succeed.
        """
        usable = max(0.0, SYNCHRONOUS_BUDGET_S - self.max_backoff_total_s)
        return max(_MIN_ATTEMPT_TIMEOUT_S, usable / max(1, self.retry_count))

    def sleep_ms(self, attempt: int, *, rand: Optional[Callable[[], float]] = None) -> float:
        """Exponential backoff with full jitter for a 0-indexed `attempt`.

        `min(cap, base * 2**attempt)` then jittered into `[0, window]`. Full
        jitter (rather than a fixed sleep) is what stops N concurrent approvals
        retrying in lockstep and re-DoSing a downstream that is already
        struggling.
        """
        window = min(self.backoff_cap_ms, self.backoff_base_ms * (2 ** max(0, attempt)))
        draw = (rand or random.random)()
        return window * draw


#: Staging defaults, mirroring `deployment/config/avx-policy-defaults.json`.
#: Used only when SSM is unreachable or the parameter is absent — a missing
#: parameter must not wedge the approve route.
DEFAULT_RETRY_POLICY = RetryPolicy()

DEFAULT_ESCALATION: dict[str, Any] = {
    "owner_visible": {
        "executor": "ios-owner",
        "category": "service",
        "severity": "informational",
    },
    "operator_visible": {
        "eventbridge_source": "vsa.avx.executor",
        "eventbridge_detail_type": "TerminalFailure",
        "cloudwatch_metric": "AVXActionTerminalFailure",
        "cloudwatch_dimension": "target_system",
    },
}


#: Stages this package will resolve policy for. An unrecognised value is a
#: misconfiguration, not a new environment: a typo'd stage silently reads
#: parameters that do not exist and every executor falls back to defaults, with
#: only a warning. A reviewer's shell carrying VSA_STAGE=prod resolved
#: /vsa/prod/... parameters while doing nothing else prod-related.
KNOWN_STAGES = frozenset({"staging", "prod", "dev", "test"})


def _stage() -> str:
    stage = os.environ.get("VSA_STAGE", "staging")
    if stage not in KNOWN_STAGES:
        log.warning(
            "VSA_STAGE=%r is not one of %s; policy parameter paths will not "
            "resolve and every executor will fall back to built-in defaults",
            stage, sorted(KNOWN_STAGES),
        )
    return stage


def retry_policy_param(target: str, *, stage: Optional[str] = None) -> str:
    return f"/vsa/{stage or _stage()}/avx/executor/{target}/retry-policy"


def escalation_param(target: str, *, stage: Optional[str] = None) -> str:
    return f"/vsa/{stage or _stage()}/avx/executor/{target}/escalation"


# Cold-start cache. Keyed by full parameter path so two targets never collide.
_PARAM_CACHE: dict[str, Any] = {}
_PARAM_CACHE_LOCK = threading.Lock()
_ssm_client: Any = None


def _get_ssm() -> Any:
    global _ssm_client
    if _ssm_client is None:
        import boto3  # noqa: PLC0415 — lazy so unit tests need no credentials

        _ssm_client = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "us-west-2"))
    return _ssm_client


def reset_policy_cache() -> None:
    """Drop the cold-start cache. For tests, and for an on-demand refresh."""
    with _PARAM_CACHE_LOCK:
        _PARAM_CACHE.clear()


def _read_json_param(path: str, *, ssm_client: Any = None) -> Optional[dict[str, Any]]:
    """Read + parse a JSON-encoded SSM parameter. Returns None on any failure.

    Every failure mode collapses to None so the caller falls back to the
    documented default: an unreachable Parameter Store must not make an owner's
    approval fail. The failure is logged at warning so it is visible rather than
    silent.
    """
    with _PARAM_CACHE_LOCK:
        if path in _PARAM_CACHE:
            return _PARAM_CACHE[path]

    parsed: Optional[dict[str, Any]] = None
    try:
        client = ssm_client or _get_ssm()
        resp = client.get_parameter(Name=path)
        raw = (resp.get("Parameter") or {}).get("Value")
        if raw:
            candidate = json.loads(raw)
            if isinstance(candidate, dict):
                parsed = candidate
            else:
                log.warning("policy param %s parsed to %s, expected object", path, type(candidate).__name__)
    except Exception as exc:  # noqa: BLE001
        log.warning("policy param %s unreadable (%s); using default", path, type(exc).__name__)

    # Only a successful parse is cached. Caching a failure would let one
    # throttle or ParameterNotFound on the first dispatch pin the built-in
    # defaults for the whole container lifetime, with nothing but a warning to
    # say the operator's configuration was never applied.
    if parsed is not None:
        with _PARAM_CACHE_LOCK:
            _PARAM_CACHE[path] = parsed
    return parsed


def load_retry_policy(
    target: str, *, ssm_client: Any = None, stage: Optional[str] = None
) -> RetryPolicy:
    """Load the per-target retry policy from SSM, falling back to the default.

    A partially-populated parameter is honoured field-by-field rather than
    discarded, so an operator can tune `retry_count` alone without restating
    the backoff window.
    """
    raw = _read_json_param(retry_policy_param(target, stage=stage), ssm_client=ssm_client)
    if not raw:
        return DEFAULT_RETRY_POLICY
    try:
        requested_retries = int(raw.get("retry_count", DEFAULT_RETRY_POLICY.retry_count))
        requested_cap = int(raw.get("backoff_cap_ms", DEFAULT_RETRY_POLICY.backoff_cap_ms))
        retries = max(1, min(_MAX_RETRY_COUNT, requested_retries))
        cap = max(0, min(_MAX_BACKOFF_CAP_MS, requested_cap))
        if retries != requested_retries or cap != requested_cap:
            log.warning(
                "retry-policy for target=%s clamped (retry_count %s->%s, "
                "backoff_cap_ms %s->%s): the approve route is synchronous behind "
                "API Gateway's %.0fs limit and configuration must not exceed it",
                target, requested_retries, retries, requested_cap, cap,
                SYNCHRONOUS_BUDGET_S,
            )
        base_ms = int(raw.get("backoff_base_ms", DEFAULT_RETRY_POLICY.backoff_base_ms))
        return RetryPolicy(
            retry_count=retries,
            backoff_base_ms=min(base_ms, cap),
            backoff_cap_ms=cap,
        )
    except (TypeError, ValueError) as exc:
        log.warning(
            # Class name only, never str(exc): a malformed-value message can
            # quote the offending parameter content back into the log. Same
            # defect class as Cycle 1 Critical 1 (@never_raises), smaller site.
            "retry-policy param for target=%s malformed (%s); using default",
            target,
            type(exc).__name__,
        )
        return DEFAULT_RETRY_POLICY


def load_escalation_config(
    target: str, *, ssm_client: Any = None, stage: Optional[str] = None
) -> dict[str, Any]:
    """Load the per-target escalation config from SSM, or the default."""
    raw = _read_json_param(escalation_param(target, stage=stage), ssm_client=ssm_client)
    if not raw:
        return dict(DEFAULT_ESCALATION)
    merged = dict(DEFAULT_ESCALATION)
    for half in ("owner_visible", "operator_visible"):
        if isinstance(raw.get(half), dict):
            merged[half] = {**DEFAULT_ESCALATION[half], **raw[half]}
    return merged


# ---------------------------------------------------------------------------
# Transient-vs-terminal classification (shared by every HTTP executor)
# ---------------------------------------------------------------------------

#: Exception class *names* treated as transient. Matched by name rather than by
#: isinstance so this module needs no `requests` import (keeping it importable
#: in a Lambda that does not bundle it).
TRANSIENT_EXCEPTION_NAMES = frozenset(
    {
        "ConnectionError",
        "ConnectTimeout",
        "ReadTimeout",
        "Timeout",
        "TimeoutError",
        "ChunkedEncodingError",
        "RemoteDisconnected",
        "ProtocolError",
        "SSLError",
    }
)


#: 4xx codes that are NOT refusals. Each is retryable, so none may be recorded
#: as `target_refused`:
#:   408 Request Timeout — a timeout, which the spec's own transient list names
#:   425 Too Early       — "retry this later" by definition
#:   429 Too Many Requests — "you are going too fast", not "we say no"
_NON_REFUSAL_4XX = frozenset({408, 425, 429})

#: 4xx codes that mean *our* request was wrong or our route does not exist, so
#: nothing ever reached a human at the target. These must land on
#: `executor_terminal_error` (which escalates), never on `declined` — telling an
#: owner "the dealer refused your service request" when in fact our token had
#: expired is a false statement about another party, and it escalates to nobody.
CLIENT_ERROR_4XX = frozenset({401, 403, 404, 405, 415})


def classify_status(status_code: int) -> str:
    """Map an HTTP status to the D5 disposition: transient | refused | client_error.

    Single source of truth so the six executors cannot disagree about what a 403
    means — they did, in the first cut of this package, and two of them pinned
    the disagreement in their tests.

    * `"transient"` — retry it (5xx, 408, 425, 429)
    * `"client_error"` — our fault or our plumbing; terminal, escalates
    * `"refused"` — the target deliberately said no; terminal, does not escalate
    """
    if 200 <= status_code < 300:
        raise ValueError(
            f"classify_status({status_code}) called on a success code; the caller "
            "should have treated this as success."
        )
    if 500 <= status_code < 600 or status_code in _NON_REFUSAL_4XX:
        return "transient"
    if status_code in CLIENT_ERROR_4XX:
        return "client_error"
    if 400 <= status_code < 500:
        return "refused"
    return "client_error"


def is_transient_status(status_code: Optional[int]) -> bool:
    """True if `status_code` should be retried.

    5xx plus the three non-refusal 4xx (408 Request Timeout, 425 Too Early, 429
    Too Many Requests).

    **This deliberately deviates from spec.md**, which says "any 4xx is terminal
    `declined` without retry". A timeout and a rate-limit are not a dealer
    refusing work, and 401/403/404/405/415 are our own fault rather than a
    refusal, so they route to `failed` instead (see `classify_status`).

    Recorded in `decisions-group5-fragment.md` in this spec's directory, NOT in
    `decisions.md` — a concurrent session holds that file, so the fragment is
    merged by the architect. An earlier version of this docstring claimed the
    deviation was already in `decisions.md` when it was not, which is worse than
    silence: it tells the next reader the question has been settled elsewhere and
    discourages them from checking. `spec.md`'s wording still needs the
    corresponding correction; that is a handback item, not something this module
    can fix.
    """
    if status_code is None:
        return False
    return 500 <= status_code < 600 or status_code in _NON_REFUSAL_4XX


def is_transient_exception(exc: BaseException) -> bool:
    """True if `exc` is a network-layer transient worth retrying."""
    names = {type(exc).__name__}
    for base in type(exc).__mro__:
        names.add(base.__name__)
    return bool(names & TRANSIENT_EXCEPTION_NAMES)
