"""ADP executor — dispatches approved engineering-decision Actions to the ADP
digital-thread writer Lambda.

Spec D11/D14, contract § 3.7, § 4 (server-set parameters), § 5 (G5).
Tech doc: `docs/tech.md` § "FI fleet health Stage D" item 2.
ADP interface: `platform-foundation/source/data-products/engineering/interface.md` § 11.

Three Action kinds are routed here (all G5-gated to engineers only):

  ``record_engineering_decision``
      Invokes the writer with the approved decision payload.  The payload is
      built server-side from the Action and the Finding's structured evidence;
      no model text (reasoning, tradeoffs, detail) is ever copied to the writer.
      On 200 with a non-null ``draft_id``, the injected ``on_decision_recorded``
      hook is called (default no-op); a hook error is logged and does not change
      the Action status.

  ``issue_recall`` / ``issue_software_campaign``
      Invokes the writer with the campaign-issue payload (``thread_id`` and
      ``draft_id`` taken from the server-set Action parameters).

Response mapping (contract § 10a / ADP interface § 11):
  - 200 (Payload statusCode 200)             → ``executed``
  - 422 (Payload statusCode 422)             → ``refused`` / ``target_refused``
  - 400 (Payload statusCode 400)             → ``failed`` / ``executor_terminal_error``
  - 5xx (Payload statusCode 5xx or
      ``FunctionError``)                     → retry; on budget exhaustion
                                               → ``failed`` / ``executor_exhausted_retries``
  - Lambda throttle or service error (a ``ClientError``, modelled subclass or
      bare, with code ``TooManyRequestsException``, ``ServiceException``,
      ``EC2ThrottledException`` or ``EC2UnexpectedException``)
                                             → retry, same budget as 5xx
  - Bare ``ClientError`` (an unmodelled code, or a front-end 5xx with no
      parseable body) with HTTP status 5xx / 408 / 425 / 429
                                             → retry, same budget as 5xx
  - botocore ReadTimeoutError / ConnectTimeoutError (and other transient exceptions)
                                             → retry inside the RetryPolicy's deadline
  - Any other ClientError                    → ``failed`` / ``executor_terminal_error``

The boto3 Lambda client is injected in tests; in production it is constructed
lazily on first dispatch (no SSM I/O at import time — same discipline as
``DmsExecutor``).
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Mapping, Optional

from agents.executors.base import (
    ActionStatus,
    ExecuteResult,
    RetryPolicy,
    is_transient_exception,
    is_transient_status,
    load_retry_policy,
    never_raises,
)

log = logging.getLogger(__name__)

_TARGET_SYSTEM = "adp"

#: Retryable Lambda ``Invoke`` error codes (tech.md item 2), retried whatever the
#: exception's class. Every other modelled code is terminal, including
#: ``ResourceNotFoundException``, ``InvalidRequestContentException``,
#: ``RequestTooLargeException``, ``InvalidParameterValueException`` and
#: ``ResourceNotReadyException``; a bare ``ClientError`` is classified by HTTP
#: status (``_is_retryable_client_error``).
_RETRYABLE_CLIENT_ERROR_CODES = frozenset({
    "TooManyRequestsException",
    "ServiceException",
    "EC2ThrottledException",
    "EC2UnexpectedException",
})


def _is_retryable_client_error(exc: BaseException) -> bool:
    """True iff ``exc`` is a botocore ``ClientError`` worth retrying.

    Matched with ``isinstance``, never by class name: for a code the Lambda service
    model defines, boto3 raises a generated ``ClientError`` subclass named after the
    code (``TooManyRequestsException``); for anything else it raises a bare
    ``ClientError``. Both carry the code in ``exc.response["Error"]["Code"]``.

    * A retryable code (``_RETRYABLE_CLIENT_ERROR_CODES``), in either shape: retry.
    * Any other modelled subclass: terminal. Never retried by HTTP status, because
      Lambda sends permanent configuration errors (``ResourceNotReadyException``,
      the ``KMS*`` and ``EC2AccessDeniedException`` codes) as 502.
    * A bare ``ClientError``: a code the model doesn't define (an IAM denial, an
      expired token), or a front-end error with no parseable body, which botocore
      reports with the HTTP status as its code (``"502"``, ``"503"``, ``"504"``).
      Retried by HTTP status the way the CMS and DMS loops retry a response
      (5xx, 408, 425, 429: ``base.is_transient_status``); terminal otherwise.
    """
    # Imported here so this module stays importable without botocore; without
    # botocore nothing can raise a botocore ClientError.
    try:
        from botocore.exceptions import ClientError  # noqa: PLC0415
    except ImportError:
        return False
    if not isinstance(exc, ClientError):
        return False
    # Both reads below are defensive: a response of an unexpected shape is
    # terminal, never transient.
    response = exc.response
    try:
        code = response["Error"]["Code"]
    except Exception:  # noqa: BLE001
        code = None
    if code in _RETRYABLE_CLIENT_ERROR_CODES:
        return True
    if type(exc) is not ClientError:
        return False
    try:
        status = response["ResponseMetadata"]["HTTPStatusCode"]
    except Exception:  # noqa: BLE001
        return False
    return isinstance(status, int) and is_transient_status(status)


def _is_transient_for_lambda(exc: BaseException) -> bool:
    """True iff ``exc`` represents a transient failure worth retrying.

    Covers botocore timeouts (ReadTimeoutError, ConnectTimeoutError whose MRO
    includes ``ReadTimeout`` / ``ConnectTimeout``) and retryable ClientErrors
    (``_is_retryable_client_error``: a retryable code, or a bare ClientError with a
    transient HTTP status).
    """
    return is_transient_exception(exc) or _is_retryable_client_error(exc)


def _root_cause_evidence(finding: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """Return the ``value`` dict of the first ``quality.root_cause`` evidence entry.

    The investigation Finding stores ``candidate_id``, ``failure_mode_id``,
    ``evidence_refs``, ``diagnosis_source``, and ``agent_version`` here (spec D14,
    contract § 2.6).  Returns ``None`` when no such entry exists.
    """
    for entry in finding.get("evidence") or []:
        if isinstance(entry, Mapping) and entry.get("kind") == "quality.root_cause":
            val = entry.get("value")
            if isinstance(val, Mapping):
                return val
    return None


def _build_record_decision_payload(
    action: Mapping[str, Any],
    finding: Mapping[str, Any],
) -> Optional[dict[str, Any]]:
    """Build the ``record_engineering_decision`` Lambda payload.

    Returns ``None`` when a required field is absent.  All fields come from the
    Action (server-side) and the Finding's structured evidence; no model text
    (reasoning, tradeoffs, detail) is ever included.

    Payload keys (``decisions.md`` "Cycle 2: what the thread writer reads"):
    ``action_id``, ``action_kind``, ``decided_by``, ``decision``,
    ``investigation_finding_id``, ``pattern_id``, ``candidate_id``,
    ``failure_mode_id``, ``option_id``, ``evidence_refs``, ``diagnosis_source``,
    ``agent_version``, ``notes``.
    """
    # ── From the Action ─────────────────────────────────────────────────────
    action_id = action.get("action_id")
    action_kind = action.get("action_kind")
    if not action_id or not action_kind:
        return None

    # decided_by: forward kind + sub (interface § 11 uses 'sub', not 'id')
    decided_by_raw = action.get("decided_by") or {}
    kind_val = decided_by_raw.get("kind")
    sub_val = (
        decided_by_raw.get("sub")
        or decided_by_raw.get("id")
        or ""
    )
    if not kind_val:
        return None
    decided_by = {"kind": kind_val, "sub": sub_val}

    decision = action.get("decision", "approved")

    # option_id and notes: from action.parameters first, then finding's
    # recommended_action.parameters (the action's own parameters are the
    # operator's overrides; the finding's are the agent's suggestions).
    action_params = action.get("parameters") or {}
    rec_params = (
        (finding.get("recommended_action") or {}).get("parameters") or {}
    )
    option_id = action_params.get("option_id") or rec_params.get("option_id")
    notes = action_params.get("notes") or rec_params.get("notes") or ""

    # ── From the Finding ─────────────────────────────────────────────────────
    investigation_finding_id = finding.get("finding_id")
    if not investigation_finding_id:
        return None

    # pattern_id is in the finding's recommended_action.parameters (server-set)
    pattern_id = rec_params.get("pattern_id")
    if not pattern_id:
        return None

    # candidate_id, failure_mode_id, evidence_refs, diagnosis_source come from
    # the quality.root_cause evidence entry (spec D14, contract § 2.6). Model
    # text (reasoning) lives there too but is deliberately not extracted.
    # agent_version comes from the Finding's top-level field (schema property),
    # NOT from the root-cause evidence value — the root-cause value carries the
    # sub-agent version that ran the investigation; the Finding's top-level
    # agent_version is the canonical artifact-producer identifier (spec D14,
    # contract § 2.6 "artifact contract").
    rc_val = _root_cause_evidence(finding)
    if rc_val is None:
        return None

    candidate_id = rc_val.get("candidate_id")
    failure_mode_id = rc_val.get("failure_mode_id")
    evidence_refs = rc_val.get("evidence_refs") or []
    diagnosis_source = rc_val.get("diagnosis_source")
    agent_version = finding.get("agent_version")

    return {
        "action_id": action_id,
        "action_kind": action_kind,
        "decided_by": decided_by,
        "decision": decision,
        "investigation_finding_id": investigation_finding_id,
        "pattern_id": pattern_id,
        "candidate_id": candidate_id,
        "failure_mode_id": failure_mode_id,
        "option_id": option_id,
        "evidence_refs": evidence_refs,
        "diagnosis_source": diagnosis_source,
        "agent_version": agent_version,
        "notes": notes,
    }


def _build_issue_payload(action: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """Build the ``issue_recall`` / ``issue_software_campaign`` Lambda payload.

    Returns ``None`` when ``thread_id`` or ``draft_id`` is absent.
    """
    action_id = action.get("action_id")
    action_kind = action.get("action_kind")
    if not action_id or not action_kind:
        return None

    decided_by_raw = action.get("decided_by") or {}
    kind_val = decided_by_raw.get("kind")
    sub_val = decided_by_raw.get("sub") or decided_by_raw.get("id") or ""
    if not kind_val:
        return None

    params = action.get("parameters") or {}
    thread_id = params.get("thread_id")
    draft_id = params.get("draft_id")
    if not thread_id or not draft_id:
        return None

    return {
        "action_id": action_id,
        "action_kind": action_kind,
        "decided_by": {"kind": kind_val, "sub": sub_val},
        "thread_id": thread_id,
        "draft_id": draft_id,
    }


def _make_lambda_client(per_attempt_timeout_s: float) -> Any:
    """Construct a boto3 Lambda client for us-east-1 (the writer Lambda's region).

    ``per_attempt_timeout_s`` is derived from the ``RetryPolicy`` so the
    per-request read timeout fits the synchronous budget — a hardcoded 60s
    read timeout can outlast the 25s API-Gateway budget on its own.
    ``retries={'total_max_attempts': 1}`` disables botocore's own retry loop so
    only the executor's retry loop retries; botocore + executor both retrying
    multiplies the budget by the botocore attempt count rather than the
    executor's.

    Module-level so the conftest AWS-client guard can patch it in tests
    (the same pattern as ios_owner._make_ddb_resource).
    """
    import boto3  # noqa: PLC0415
    from botocore.config import Config  # noqa: PLC0415

    return boto3.client(
        "lambda",
        region_name="us-east-1",
        config=Config(
            read_timeout=per_attempt_timeout_s,
            connect_timeout=5,
            retries={"total_max_attempts": 1},
        ),
    )


class AdpExecutor:
    """Executor for ``target_system="adp"``.

    Invokes ``adp-{stage}-digital-thread-writer`` (us-east-1) synchronously,
    cross-region from the AVX Tier 2 Lambda (us-west-2).

    Constructor keyword arguments allow test injection of collaborators:

        thread_writer_arn   ARN of the digital-thread writer Lambda.  Resolved
                            lazily: if blank (e.g. the registry's ``""`` default),
                            ``ADP_THREAD_WRITER_ARN`` is read from the environment
                            on first dispatch.  Unset or blank at dispatch time →
                            terminal failure with zero invocations.
        lambda_client       Injected boto3 Lambda client.  In production,
                            constructed lazily on first dispatch.
        sleep_fn            callable(seconds: float) — injected in tests to
                            avoid real sleeps; default: ``time.sleep``
        retry_policy        Injected ``RetryPolicy`` — bypasses SSM in tests.
        on_decision_recorded  Hook ``(action, response) → None`` called on 200
                            with a non-null ``draft_id``.  Default: no-op.
                            A hook error is logged and counted; it does not
                            change the Action status.
    """

    target_system: str = _TARGET_SYSTEM

    def __init__(
        self,
        *,
        thread_writer_arn: str,
        lambda_client: Any = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
        retry_policy: Optional[RetryPolicy] = None,
        on_decision_recorded: Optional[Callable[..., None]] = None,
    ) -> None:
        self._arn = thread_writer_arn
        self._lambda_client_injected = lambda_client
        self._sleep = sleep_fn or time.sleep
        self._retry_policy_cached: Optional[RetryPolicy] = retry_policy
        self._on_decision_recorded = on_decision_recorded  # default: no-op

    @property
    def _retry_policy(self) -> RetryPolicy:
        if self._retry_policy_cached is None:
            self._retry_policy_cached = load_retry_policy(
                _TARGET_SYSTEM, ssm_client=None
            )
        return self._retry_policy_cached

    def _lambda_client(self, per_attempt_timeout_s: float) -> Any:
        """Return the boto3 Lambda client (injected or lazily-constructed).

        ``per_attempt_timeout_s`` is passed to ``_make_lambda_client`` so the
        client's read timeout matches the policy's per-attempt budget rather
        than a hardcoded value (defect 2 fix — botocore uses ``read_timeout``
        before the executor retry loop runs).  Ignored when a client is
        injected (test path).
        """
        if self._lambda_client_injected is not None:
            return self._lambda_client_injected
        # Lazy initialization — first dispatch creates the client once.
        # _make_lambda_client() is the module-level factory; it is patched
        # by the conftest AWS-client guard so tests never hit real Lambda.
        self._lambda_client_injected = _make_lambda_client(per_attempt_timeout_s)
        return self._lambda_client_injected

    def _get_writer_arn(self) -> str:
        """Resolve the digital-thread writer ARN, lazily reading the env if needed.

        ``thread_writer_arn=""`` (the registry default) defers the lookup to
        first dispatch.  Unset or blank at that point → caller returns terminal
        failure with zero Lambda invocations.
        """
        import os  # noqa: PLC0415 — lazy; avoids adding a top-level os import
        return self._arn or os.environ.get("ADP_THREAD_WRITER_ARN", "").strip()

    @never_raises
    def dispatch(
        self, action: Mapping[str, Any], claims: Mapping[str, Any]
    ) -> ExecuteResult:
        """Dispatch ``action`` to the ADP thread writer.  Never raises."""
        kind = action.get("action_kind")

        if kind == "record_engineering_decision":
            return self._dispatch_record_decision(action, claims)

        if kind in ("issue_recall", "issue_software_campaign"):
            return self._dispatch_issue(action, claims)

        # Defence-in-depth: refuse unrecognised kinds.  The approve route's
        # policy check and G5 should prevent these from arriving; the executor
        # refuses independently (spec § "Executor dispatch abstraction").
        return ExecuteResult.refused(
            note=f"AdpExecutor: action_kind {kind!r} is not executable here"
        )

    # ── record_engineering_decision ─────────────────────────────────────────

    def _dispatch_record_decision(
        self, action: Mapping[str, Any], claims: Mapping[str, Any]
    ) -> ExecuteResult:
        finding = action.get("investigation_finding")
        if not isinstance(finding, Mapping):
            return ExecuteResult.terminal_error(
                last_error_class="MissingFinding",
                note="record_engineering_decision requires action.investigation_finding",
            )

        payload = _build_record_decision_payload(action, finding)
        if payload is None:
            return ExecuteResult.terminal_error(
                last_error_class="MissingDispatchParameter",
                note=(
                    "record_engineering_decision requires: action_id, action_kind, "
                    "decided_by, finding.finding_id, finding.recommended_action."
                    "parameters.pattern_id, and a quality.root_cause evidence entry"
                ),
            )

        return self._invoke(action, payload, is_record_decision=True)

    # ── issue_recall / issue_software_campaign ───────────────────────────────

    def _dispatch_issue(
        self, action: Mapping[str, Any], claims: Mapping[str, Any]
    ) -> ExecuteResult:
        payload = _build_issue_payload(action)
        if payload is None:
            return ExecuteResult.terminal_error(
                last_error_class="MissingDispatchParameter",
                note="issue_recall / issue_software_campaign requires thread_id and draft_id",
            )
        return self._invoke(action, payload, is_record_decision=False)

    # ── shared invoke / retry loop ───────────────────────────────────────────

    def _invoke(
        self,
        action: Mapping[str, Any],
        payload: dict[str, Any],
        *,
        is_record_decision: bool,
    ) -> ExecuteResult:
        """Invoke the writer Lambda and map the response per contract § 10a."""
        policy = self._retry_policy

        # Defect 3 fix: resolve ARN lazily; terminal failure with zero invocations
        # when ADP_THREAD_WRITER_ARN is unset or blank.
        arn = self._get_writer_arn()
        if not arn:
            return ExecuteResult.terminal_error(
                last_error_class="MissingWriterArn",
                note=(
                    "ADP_THREAD_WRITER_ARN is not configured; "
                    "set it before dispatching ADP actions"
                ),
            )

        raw_payload = json.dumps(payload).encode("utf-8")
        # Defect 2 fix: per_attempt_timeout_s derived from the RetryPolicy so
        # one stalled attempt cannot outlast the 25s synchronous budget.
        client = self._lambda_client(policy.per_attempt_timeout_s)

        last_exc: Optional[BaseException] = None
        last_status: Optional[int] = None
        attempt = 0
        deadline = time.monotonic() + policy.deadline_s

        while attempt < policy.retry_count:
            if time.monotonic() >= deadline:
                log.warning(
                    "adp executor: deadline exceeded after %d attempt(s)", attempt
                )
                break

            try:
                response = client.invoke(
                    FunctionName=arn,
                    InvocationType="RequestResponse",
                    Payload=raw_payload,
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                last_status = None
                if _is_transient_for_lambda(exc):
                    attempt += 1
                    if attempt < policy.retry_count and time.monotonic() < deadline:
                        self._sleep(policy.sleep_ms(attempt - 1) / 1000.0)
                    continue
                # Non-transient (ResourceNotFoundException, etc.) → terminal
                return ExecuteResult.terminal_error(
                    last_error_class=type(exc).__name__,
                    retry_count=attempt,
                )

            # Lambda service returned a response.  Check for FunctionError
            # (tech.md item 2 error shape 2): the Lambda itself threw an
            # unhandled exception.  Treat as transient — the function may
            # recover on retry (cold-start OOM, init timeout, etc.).
            if response.get("FunctionError"):
                last_status = None
                attempt += 1
                if attempt < policy.retry_count and time.monotonic() < deadline:
                    self._sleep(policy.sleep_ms(attempt - 1) / 1000.0)
                continue

            # Read the application-level response body from the writer.
            try:
                payload_bytes = response["Payload"].read()
                outer = json.loads(payload_bytes.decode("utf-8"))
                status_code = int(outer.get("statusCode", 0))
                body_raw = outer.get("body", "{}")
                body = json.loads(body_raw) if isinstance(body_raw, str) else body_raw
            except Exception as exc:  # noqa: BLE001
                return ExecuteResult.terminal_error(
                    last_error_class="InvalidWriterResponse",
                    retry_count=attempt,
                    note=f"writer response unparseable: {type(exc).__name__}",
                )

            last_status = status_code

            # ── 200: success ─────────────────────────────────────────────────
            if status_code == 200:
                result = ExecuteResult.success(
                    target_ref=body.get("thread_id"),
                    status=ActionStatus.EXECUTED,
                )
                # Call the on_decision_recorded hook when draft_id is non-null.
                # The hook is task 3.3's quality.issue writer.  A hook error
                # is logged and does not change the Action status.
                if is_record_decision and body.get("draft_id") is not None:
                    self._call_hook(action, body)
                return result

            # ── 422: writer refused (unknown pattern, invalid option, etc.) ──
            if status_code == 422:
                return ExecuteResult.refused(
                    last_error_status=422,
                    last_error_class="WriterRefused",
                )

            # ── 400: malformed request ────────────────────────────────────────
            if status_code == 400:
                return ExecuteResult.terminal_error(
                    last_error_status=400,
                    last_error_class="MalformedWriterRequest",
                    retry_count=attempt,
                )

            # ── 5xx: transient writer error ───────────────────────────────────
            if 500 <= status_code < 600:
                attempt += 1
                if attempt < policy.retry_count and time.monotonic() < deadline:
                    self._sleep(policy.sleep_ms(attempt - 1) / 1000.0)
                continue

            # Unexpected status code → terminal
            return ExecuteResult.terminal_error(
                last_error_status=status_code,
                last_error_class="UnexpectedWriterStatus",
                retry_count=attempt,
            )

        # ── Retry budget exhausted ─────────────────────────────────────────
        err_class = type(last_exc).__name__ if last_exc else "HTTPError"
        return ExecuteResult.exhausted(
            retry_count=attempt,
            last_error_class=err_class,
            last_error_status=last_status,
        )

    def _call_hook(self, action: Mapping[str, Any], response: dict[str, Any]) -> None:
        """Call ``on_decision_recorded``; log any error without changing the Action status.

        W4 (G3F1.5): the hook itself (agents/fi_quality/issue.py) emits a CloudWatch
        metric (QualityIssueHookFailure) on final failure; this method only logs.
        """
        if self._on_decision_recorded is None:
            return
        try:
            self._on_decision_recorded(action, response)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "adp executor: on_decision_recorded hook raised %s for action %s; "
                "Action status unchanged",
                type(exc).__name__,
                action.get("action_id"),
            )
