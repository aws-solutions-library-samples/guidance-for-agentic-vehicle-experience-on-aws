"""API: POST /escalate

Starts an Amazon Connect chat contact for handing off a driver to a human
fleet-support agent, and (for P0) queues a roadside dispatch request.

Request (all strings unless noted):
    {
      "severity": "P0|P1|P2|P3",          (required)
      "vin": "1HGBH41JXMN000024",         (required)
      "vehicleId": "VEH-0025",             (required)
      "reason": "engine overheating",      (required)
      "summary": "Nova conversation summary",  (optional)
      "triageSessionId": "<uuid>",         (optional)
      "dispatchRoadside": true,            (optional; default governed by tenant flag.
                                            Either way, honoured only when
                                            triageSessionId names a recent P0 decision
                                            for this tenant + VIN — see _verified_p0_decision —
                                            AND the caller is the vehicle's assigned
                                            driver — see _caller_assigned_driver)
      "driverId": "DRV-0055",              (optional; if present it must equal the
                                            caller's custom:driverId claim, or the P0
                                            side effects are refused. The chat still opens)
      "displayName": "Stephanie Johnson",  (optional)
      "voiceSessionId": "<session-id>"     (optional)
    }

Response 200:
    {
      "contactId": "<uuid>",
      "participantId": "<uuid>",
      "participantToken": "<JWT>",
      "connectionExpiry": "<iso8601>",
      "severity": "P0",
      "rsaDispatched": true,
      "rsaActionId": "<uuid-or-null>",
      "dispatchId": "<uuid-or-null>"
    }

  rsaDispatched: true  means a REQUESTED row was written to vsa-{stage}-rsa-dispatch.
  dispatchId: the table PK for that row; use GET /rsa/dispatch/{dispatchId} to poll status.
  rsaActionId: same value as dispatchId (kept for backward compatibility).

Design notes
------------

- Tenant isolation: resolves the Connect instance / flow / queue from the
  caller's tenant config (vsa-prod-tenant-config) rather than hardcoded
  env vars, so the same Lambda works for every tenant we add later.
- Attributes: the full triage/driver context is passed into the contact
  flow as standard Amazon Connect attributes. Kevin's CCP can read these
  server-side to populate the EscalationContextPanel. Values must be
  strings per the Connect API contract.
- Severity priority bump: the "meridian-fleet-vsa-escalation-flow" contact flow
  reads $.Attributes.severity and routes P0 to a high-priority branch of
  the chat queue. That logic lives in Connect, not here — we just set
  the attribute faithfully.
- P0 RSA: best-effort write to vsa-{stage}-rsa-dispatch (env var
  RSA_DISPATCH_TABLE, required, no default) with status "REQUESTED".
  Before writing, the producer resolves the vehicle's position via a
  direct Lambda-to-Lambda invoke of api-vehicle-live-state (env var
  LIVE_STATE_FN_NAME, required). lat/lon are written only when the
  position fix is fresh (seconds since CMS Flink stored it, live-state locationAgeSeconds,
  <= RSA_POSITION_MAX_AGE_SECONDS, default 900s, env var optional) and came
  from Redis. A days-old
  DDB-fallback coordinate for a tow truck is worse than a refusal.
  On stale/absent/failed position, the row is still written with
  status=REQUESTED and no lat/lon so the consumer's existing fail-closed
  path produces FAILED with its reason. The human handoff is unaffected.
  Non-fatal: if the write fails, we still return the contactId so the
  agent can dispatch manually. Task 2.4 wires RSA_DISPATCH_TABLE from
  the new vsa-rsa-stack; until then the env var must be injected manually.
- ChatDurationInMinutes: capped at 60 for the demo. Documented Connect
  max is 10080 (7 days); 60 is plenty for a driver-side chat that
  either resolves or hands off to roadside within minutes.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

# ---------------------------------------------------------------------------
# Shared library path wiring
# ---------------------------------------------------------------------------
# In the deployed Lambda bundle, shared/rsa_client*.py is copied alongside
# handler.py by scripts/build-rsa-lambda.sh, so Lambda's default code-root
# (/var/task) on sys.path is sufficient for `from shared.rsa_client import`.
# In tests, lambdas/api-escalate/tests/conftest.py adds the lambdas/ dir
# so the same import resolves from the source tree.
from shared.rsa_client import LIFECYCLE_REQUESTED

# Customer Profiles + Cases (spec 2026-11-02-avx-connect-profiles-cases). crm.py
# sits next to handler.py in the bundle (build-rsa-lambda.sh copies the directory).
import crm

log = logging.getLogger()
log.setLevel(logging.INFO)

# Env
_TENANT_CONFIG_TABLE = os.environ["TENANT_CONFIG_TABLE"]
# RSA_DISPATCH_TABLE: required, no default. A missing value fails loudly at
# init rather than silently writing to a wrong table. Task 2.4 wires this
# from the new vsa-rsa-stack; tests inject the value directly.
_RSA_DISPATCH_TABLE = os.environ["RSA_DISPATCH_TABLE"]
_CHAT_DURATION_MINUTES = int(os.environ.get("CHAT_DURATION_MINUTES", "60"))

# The only escalationType values accepted. Exact match: no case-folding or
# stripping, so a confusable or padded value is rejected rather than mapped.
_ESCALATION_TYPES = frozenset({"safety", "service"})

# Security review Cycle 1 W1: roadside dispatch requires a PLATFORM-RECORDED P0.
# The Cognito authorizer accepts tokens from every tenant iOS pool, the CMS UI
# pool and any extra pool, and the body is written by the caller — so neither
# `dispatchRoadside: true` nor `severity: "P0"` proves anything. What does is a
# row in vsa-{stage}-triage-decisions: written only by the deterministic
# classifier Lambda and by the supervisor's driver-confirmation path, never by
# a client. A dispatch is honoured only when `triageSessionId` names such a row
# for the SAME tenant (from the verified JWT claim) and VIN, classified P0,
# decided within the window below.
#
# Optional at init on purpose: if the table name is not wired, every dispatch
# is refused (fail closed) while the human handoff keeps working. A missing
# env var must never turn into "dispatch without checking".
_TRIAGE_DECISIONS_TABLE = os.environ.get("TRIAGE_DECISIONS_TABLE")
try:
    _RSA_DECISION_MAX_AGE_SECONDS = float(os.environ.get("RSA_DECISION_MAX_AGE_SECONDS", "3600"))
except ValueError:
    _RSA_DECISION_MAX_AGE_SECONDS = -1.0  # typo → refuse every dispatch, never widen

# F5.D2: Live-state function name for position resolution.
# Required — no default. A missing value causes KeyError at init so a
# deploy missing the CDK wiring fails loudly rather than silently dispatching
# rows with no coordinates. The CDK wiring is Task F5.D1.
_LIVE_STATE_FN_NAME = os.environ["LIVE_STATE_FN_NAME"]

# F5.D2: Position freshness threshold in seconds.
# Compared with live-state's locationAgeSeconds, how long ago CMS Flink stored
# the position fix (not lastSeenAgoSeconds, the connection's age; issue
# 2026-11-07-escalate-gate-uses-connection-age-not-fix-age). An old fix may have
# come from the DDB fallback which bidi_app.py:2112 documents as able to lag
# actual telemetry by days. A days-old coordinate for a tow truck is worse
# than a refusal because it looks precise. Default 900s (15 min), a policy
# value (vsa-api-stack.ts rsaPositionMaxAgeSeconds JSDoc).
#
# F6.3: guard against a config typo causing a 502 on ALL /escalate requests.
# A misconfigured env var should degrade to "no dispatch" (fail closed on the
# gate) rather than raising ValueError at module load and 502'ing every call.
# The human handoff (chat contact) is unaffected — a location problem must
# never break it (same principle as F5.D2 Accept 4).
#
# Fix Group 13 (issue 2026-09-25-rsa-position-max-age-accepts-non-finite): the
# value must be finite, > 0 and <= the ceiling. `float()` accepts "nan", "inf"
# and "1e9", and `age > nan` / `age > inf` are always False, so those values
# turned the freshness check OFF and let a stale position dispatch. Anything
# outside (0, ceiling] now takes the same fail-closed sentinel as a typo.
# The ceiling is mirrored in infrastructure/lib/vsa-api-stack.ts (synth check)
# and the Makefile's _deploy-cdk guard; a test pins all three to one value.
_RSA_POSITION_MAX_AGE_CEILING_SECONDS = 3600.0
_RSA_POSITION_MAX_AGE_SECONDS: float
_rsa_age_raw = os.environ.get("RSA_POSITION_MAX_AGE_SECONDS")
if _rsa_age_raw is None:
    log.warning(
        "RSA_POSITION_MAX_AGE_SECONDS not set; defaulting to 900 seconds. "
        "Set this env var via CDK (F5.D1) to make the threshold explicit."
    )
    _RSA_POSITION_MAX_AGE_SECONDS = 900.0
else:
    try:
        _rsa_age_parsed: float | None = float(_rsa_age_raw)
    except (ValueError, TypeError):
        _rsa_age_parsed = None
    if (
        _rsa_age_parsed is not None
        and math.isfinite(_rsa_age_parsed)
        and 0 < _rsa_age_parsed <= _RSA_POSITION_MAX_AGE_CEILING_SECONDS
    ):
        _RSA_POSITION_MAX_AGE_SECONDS = _rsa_age_parsed
    else:
        log.warning(
            "RSA_POSITION_MAX_AGE_SECONDS=%r is not a finite number in (0, %d]; "
            "dispatch gate disabled (all positions will be refused). "
            "Fix the CDK config to restore dispatch.",
            _rsa_age_raw, int(_RSA_POSITION_MAX_AGE_CEILING_SECONDS),
        )
        # Use -1 as a sentinel: any age > -1 fails the gate, so dispatch
        # is refused for every vehicle until the config is corrected.
        # This ensures the human handoff is never broken by a typo.
        _RSA_POSITION_MAX_AGE_SECONDS = -1.0

# Lazy-initialized boto3 clients (init-time cost amortized across warm invocations)
_ddb = None
_connect = None
_lambda_client = None
# Chat client: connect 1 s, read 4 s, two attempts, so at most about 10 s.
# StartChatContact usually answers in 1-3 s; its idempotency token is reused
# across the retry. With the call's 4 s below, the worst case is about 14 s,
# inside the 15 s Lambda timeout and the bidi client's 15 s request timeout.
_CONNECT_CONFIG = Config(connect_timeout=1, read_timeout=4, retries={"total_max_attempts": 2})
# The P0 call runs BEFORE the chat, so it gets its own, tighter client: at most
# about 4 s (one attempt).
_CALLBACK_CONNECT_CONFIG = Config(connect_timeout=1, read_timeout=3, retries={"total_max_attempts": 1})
_callback_connect = None


def _get_ddb():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb")
    return _ddb


def _get_lambda():
    """Return a lazy-initialised Lambda client for direct Lambda-to-Lambda invocation."""
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda")
    return _lambda_client


def _is_p0(value: Any) -> bool:
    """True for a P0 `classification` as EITHER writer stores it.

    The two writers of vsa-{stage}-triage-decisions disagree on type:
      - the classifier Lambda serialises `Level`, an IntEnum where P0 == 0, so
        the row holds the NUMBER 0 (boto3 returns Decimal('0'));
      - the supervisor's driver-confirmation audit writes the STRING "P0".
    Comparing to "P0" alone refused every ordinary classifier P0 (review
    Cycle 3 Critical). `bool` is excluded explicitly: False == 0 in Python,
    and a boolean must never read as a P0.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, str):
        return value == "P0"
    if isinstance(value, (int, Decimal)):
        return value == 0
    return False


def _verified_p0_decision(triage_session_id: Any, tenant_id: str, vin: str) -> bool:
    """True only if a platform-recorded P0 triage decision backs this dispatch.

    Security review Cycle 1 W1 — see `_TRIAGE_DECISIONS_TABLE`. Checks, all
    required: the id is a non-empty string; the row exists; its `tenantId`
    equals the caller's verified tenant claim; its `vin` equals the request
    VIN; its `classification` is P0 (the string "P0" from the driver-confirmation
    path, or the integer/Decimal 0 from the classifier Lambda — see `_is_p0`);
    its `decidedAt` parses and is
    no older than RSA_DECISION_MAX_AGE_SECONDS (and not in the future beyond
    a small clock skew). Any failure, including a DynamoDB error, returns
    False. Never raises — a refusal must not break the human handoff.

    Logs the reason, never the VIN (PII, see F8.1).
    """
    if not _TRIAGE_DECISIONS_TABLE:
        log.warning("rsa-gate: TRIAGE_DECISIONS_TABLE not configured — refusing dispatch")
        return False
    if not isinstance(triage_session_id, str) or not triage_session_id.strip():
        log.warning("rsa-gate: no triageSessionId — refusing dispatch")
        return False
    try:
        item = _get_ddb().Table(_TRIAGE_DECISIONS_TABLE).get_item(
            Key={"sessionId": triage_session_id}
        ).get("Item")
    except Exception as e:  # noqa: BLE001
        log.warning("rsa-gate: decision lookup failed (%s) — refusing dispatch", type(e).__name__)
        return False
    if not item:
        log.warning("rsa-gate: no decision row for triageSessionId — refusing dispatch")
        return False
    if item.get("tenantId") != tenant_id:
        log.warning("rsa-gate: decision tenant does not match caller — refusing dispatch")
        return False
    if item.get("vin") != vin:
        log.warning("rsa-gate: decision VIN does not match request — refusing dispatch")
        return False
    if not _is_p0(item.get("classification")):
        log.warning("rsa-gate: decision is %r, not P0 — refusing dispatch", item.get("classification"))
        return False
    decided_raw = item.get("decidedAt")
    try:
        decided = datetime.fromisoformat(str(decided_raw).replace("Z", "+00:00"))
        if decided.tzinfo is None:
            decided = decided.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        log.warning("rsa-gate: decision has no parseable decidedAt — refusing dispatch")
        return False
    age = (datetime.now(timezone.utc) - decided).total_seconds()
    if age < -60 or age > _RSA_DECISION_MAX_AGE_SECONDS:
        log.warning("rsa-gate: decision age %.0fs outside window — refusing dispatch", age)
        return False
    return True


def _resolve_vehicle_position(vehicle_id: str, expected_vin: str | None = None) -> tuple[float, float] | None:
    """Resolve the vehicle's current position via the api-vehicle-live-state Lambda.

    Copies the invoke pattern from api-vehicle-context/handler.py:335.
    The live-state handler is explicitly written for both API Gateway and direct
    Lambda invocations (accepts the same API-Gateway-shaped event).

    Returns (lat, lon) as floats only when the position fix is fresh (seconds
    since CMS Flink stored it, live-state locationAgeSeconds, <= RSA_POSITION_MAX_AGE_SECONDS) AND it
    came from Redis. If Redis was
    unreachable the position came from the DDB fallback, which bidi_app.py:2112
    documents as able to lag actual telemetry by days — a stale coordinate for
    a tow truck is worse than a refusal because it looks precise.

    Returns None on any of: invoke failure, non-200 response, stale position,
    Redis unreachable, missing lat/lon fields, or invalid coordinate values.
    Never raises — resolution failure must not break the human handoff.

    F5.D2 mutation guard (a): make the gate accept any age → a test MUST fail.
    F5.D2 mutation guard (b): raise on lookup failure instead of returning None
        → a test MUST fail.
    F5.D2 mutation guard (c): return a coordinate when the lookup failed
        → a test MUST fail.

    PII NOTE: vehicle_id is not personally identifiable by itself; the returned
    lat/lon are location data and must not be logged.
    """
    try:
        payload = json.dumps({
            "pathParameters": {"vehicleId": vehicle_id},
            "httpMethod": "GET",
        }).encode("utf-8")
        resp = _get_lambda().invoke(
            FunctionName=_LIVE_STATE_FN_NAME,
            InvocationType="RequestResponse",
            Payload=payload,
        )
        envelope = json.loads(resp["Payload"].read().decode("utf-8"))
        if envelope.get("statusCode") != 200:
            log.warning(
                "rsa-position: live-state non-200 for vehicle=%s: %s",
                vehicle_id, str(envelope.get("body"))[:200],
            )
            return None
        body = json.loads(envelope.get("body") or "{}")
    except Exception as e:  # noqa: BLE001
        # F8.3: use type(e).__name__ rather than str(e) — the live-state response
        # body may carry lat/lon fragments, which str(exc) would echo to CloudWatch.
        log.warning("rsa-position: live-state invoke failed for vehicle=%s: %s", vehicle_id, type(e).__name__)
        return None

    # F6.1: Gate on locationSource, not redisReachable.
    # `redisReachable` is `r is not None` — it tells you the Redis client connected.
    # It says nothing about whether Redis had a location for this vehicle. The
    # `_live_location` function in live-state falls through to DDB regardless of
    # Redis reachability, so `redisReachable=True` with no location signals or an
    # empty signal_catalog:reverse still yields a DDB coordinate.
    # `locationSource == "redis"` is the authoritative signal: it is set only when
    # a signal alias in the live signals hash supplied the coordinate.
    location_source = body.get("locationSource")
    if location_source != "redis":
        log.info(
            "rsa-position: locationSource=%r for vehicle=%s — coordinate not Redis-backed; "
            "refusing to use it for dispatch (DDB fallback may be days old)",
            location_source, vehicle_id,
        )
        return None

    # F7.2: Cross-check the caller-supplied vin against the vin the live-state
    # Lambda read from the vehicles row.  A mismatch means the request's vehicleId
    # and vin describe different vehicles — the position belongs to vehicle A but
    # the dispatch row carries vehicle B's VIN.  A truck dispatched to vehicle A's
    # location under vehicle B's VIN is a physical-dispatch safety error.
    #
    # Refuse (return None, no lat/lon) in two cases:
    #   1. Live-state reports a vin that does not match expected_vin.
    #   2. Live-state reports no vin at all (version-skew / old record).  Treat
    #      "vin absent" the same as "vin mismatched" — identity cannot be confirmed.
    if expected_vin is not None:
        live_state_vin = body.get("vin")
        if not live_state_vin:
            log.warning(
                "rsa-position: live-state returned no vin for vehicle=%s — "
                "cannot confirm vehicleId↔VIN identity; refusing position",
                vehicle_id,
            )
            return None
        if live_state_vin != expected_vin:
            # F8.1: Do NOT log either VIN value — vin is PII per the Protocol.request_dispatch
            # docstring (F3.2).  Any principal with logs:GetLogEvents on this group could harvest
            # (vehicleId, real VIN) pairs from the mismatch path.  The event is the diagnostic;
            # the values are not needed to act on it.
            log.warning(
                "rsa-position: vin mismatch for vehicle=%s — "
                "vehicleId↔VIN identity cannot be confirmed; "
                "refusing position to prevent wrong-vehicle dispatch",
                vehicle_id,
            )
            return None

    # Gate on how long ago the position fix was stored, not on the connection's age
    # (issues/2026-11-07-escalate-gate-uses-connection-age-not-fix-age/). This gate
    # used `lastSeenAgoSeconds`, the age of meta lastConnectedAt/lastSeenAt. CMS
    # rewrites lastConnectedAt on every FleetWise check-in, which carries no
    # position, so a car that checked in but sent no fix passed with a fix up to
    # 7 days old. `locationAgeSeconds` is how long ago the older of the two axes'
    # fixes was stored, set by live-state only when locationSource is "redis". That
    # time is CMS Flink's processing clock, not the car's sample time, so a fix the
    # car uploaded late (store-and-forward) still reads as new: a known residual,
    # recorded in the issue's decisions.md.
    #
    # F6.2: fail closed on a missing, boolean, string, non-numeric, non-finite or
    # negative age: no position, so the row is written without lat/lon and the
    # consumer fails it, exactly as for a stale fix. live-state sends an int or null.
    fix_age = body.get("locationAgeSeconds")
    if fix_age is None:
        log.info("rsa-position: locationAgeSeconds absent for vehicle=%s; refusing", vehicle_id)
        return None
    if isinstance(fix_age, (bool, str)):
        log.warning("rsa-position: locationAgeSeconds is a %s for vehicle=%s; refusing",
                    type(fix_age).__name__, vehicle_id)
        return None
    try:
        age_seconds = float(fix_age)
    except (TypeError, ValueError, OverflowError) as e:
        # F8.3: log exception class only — the value came from the live-state response body;
        # avoiding any response value in CloudWatch is consistent with the PII rule.
        log.warning("rsa-position: invalid locationAgeSeconds for vehicle=%s: %s", vehicle_id, type(e).__name__)
        return None
    if not math.isfinite(age_seconds):
        log.warning("rsa-position: locationAgeSeconds is not finite for vehicle=%s; refusing", vehicle_id)
        return None
    if age_seconds < 0:
        log.warning(
            "rsa-position: locationAgeSeconds is negative (%.1f) for vehicle=%s; refusing",
            age_seconds, vehicle_id,
        )
        return None

    if age_seconds > _RSA_POSITION_MAX_AGE_SECONDS:
        log.info(
            "rsa-position: position stale for vehicle=%s (fix age=%.0fs > threshold=%.0fs); "
            "refusing to use it for dispatch",
            vehicle_id, age_seconds, _RSA_POSITION_MAX_AGE_SECONDS,
        )
        return None

    # Extract the coordinates.
    raw_lat = body.get("latitude")
    raw_lon = body.get("longitude")
    if raw_lat is None or raw_lon is None:
        log.info("rsa-position: lat/lon absent for vehicle=%s", vehicle_id)
        return None
    try:
        lat = float(raw_lat)
        lon = float(raw_lon)
    except (TypeError, ValueError, OverflowError) as e:
        # F8.3: type name only — the exception message from float() includes the raw string
        # value, which could be a lat/lon fragment from the live-state response body.
        log.warning("rsa-position: invalid lat/lon for vehicle=%s: %s", vehicle_id, type(e).__name__)
        return None

    # F6.4: Validate coordinate range. Out-of-range values indicate bad telemetry or
    # a seeding bug. A tow truck dispatched to lat=999, lon=-999 is a safety error.
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        log.warning(
            "rsa-position: coordinates out of range for vehicle=%s "
            "(lat=%.4f, lon=%.4f); refusing dispatch",
            vehicle_id, lat, lon,
        )
        return None

    return (lat, lon)


def _get_connect(region: str | None = None, *, callback: bool = False):
    """Return a Connect client targeting the instance's region.

    Amazon Connect instances are regional. This Lambda may run in a
    different region (e.g. us-west-2) than the tenant's Connect instance
    (e.g. us-east-1), so we must target the instance's region explicitly —
    parsed from its ARN — instead of relying on the Lambda's default region.
    Calling StartChatContact against the wrong region returns
    "Resource not found of Type: INSTANCE".
    """
    # ``callback=True`` is the P0 call's own client (``_CALLBACK_CONNECT_CONFIG``):
    # the call runs before StartChatContact, so a stalled call must not eat the
    # Lambda's 15 s before the chat starts.
    global _connect, _callback_connect
    cached = _callback_connect if callback else _connect
    if cached is None or (region and getattr(cached.meta, "region_name", None) != region):
        kwargs = {"config": _CALLBACK_CONNECT_CONFIG if callback else _CONNECT_CONFIG}
        if region:
            kwargs["region_name"] = region
        cached = boto3.client("connect", **kwargs)
        if callback:
            _callback_connect = cached
        else:
            _connect = cached
    return cached


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "*"),
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
        },
        "body": json.dumps(body),
    }


# ---------------------------------------------------------------------------
# Tenant config lookup
# ---------------------------------------------------------------------------


def _load_tenant_connect_config(tenant_id: str) -> dict[str, Any] | None:
    """Return a dict with instanceArn, contactFlowId, tier2QueueArn, or None.

    Follows the same pointer-to-version pattern the api-tenant-config
    Lambda uses: (tenantId, "current") → activeVersion → full row.
    """
    table = _get_ddb().Table(_TENANT_CONFIG_TABLE)
    pointer = table.get_item(
        Key={"tenantId": tenant_id, "version": "current"}
    ).get("Item")
    if not pointer or "activeVersion" not in pointer:
        return None
    item = table.get_item(
        Key={"tenantId": tenant_id, "version": pointer["activeVersion"]}
    ).get("Item")
    if not item:
        return None

    channels = item.get("channels") or {}
    connect = channels.get("connect") or {}
    booking = item.get("booking") or {}
    warm = booking.get("warmTransfer") or {}
    escalation = item.get("escalation") or {}

    instance_arn = connect.get("instanceArn")
    contact_flow_id = connect.get("contactFlowId")
    # Prefer the escalation tier-2 queue, fall back to warm-transfer queue.
    queue_arn = escalation.get("tier2QueueArn") or warm.get("queueArn")

    if not instance_arn or not contact_flow_id:
        return None

    # Tenant flags for P0 auto-actions — both default false.
    # Per spec D3: roadside and human-chat are user-confirmed by default.
    # Some fleet duty-of-care rules require auto-escalation; flipping
    # these is a one-line tenant-config change with no structural consequence.
    auto_rsa = bool(escalation.get("autoRequestRoadsideOnP0", False))
    auto_chat = bool(escalation.get("autoOpenHumanChatOnP0", False))

    # P0 outbound callback (escalation.callback). Previously never projected,
    # so `cfg.get("callbackFlowId")` was always None and the callback branch
    # was unreachable outside tests. An outbound call needs a flow AND a caller
    # ID: either a queue that carries an outbound caller-ID number, or a
    # claimed source number. See docs/VSA_TENANT_CONFIG.md § Callback.
    callback = escalation.get("callback")
    callback = callback if isinstance(callback, dict) else {}
    callback_queue_arn = callback.get("queueArn")
    # S4: if queueArn is present but not a string (e.g. a DynamoDB Number),
    # _arn_trailing_id would raise AttributeError. The callback block is
    # optional — a malformed field must not break the human handoff at every
    # severity. Treat non-string as absent (the tenant can fix their config).
    if not isinstance(callback_queue_arn, str):
        callback_queue_arn = None

    return {
        "instanceArn": instance_arn,
        "instanceId": _arn_trailing_id(instance_arn),
        "instanceRegion": _arn_region(instance_arn),
        "contactFlowId": contact_flow_id,
        "queueArn": queue_arn,
        "autoRequestRoadsideOnP0": auto_rsa,
        "autoOpenHumanChatOnP0": auto_chat,
        "callbackFlowId": callback.get("contactFlowId") or None,
        "callbackQueueId": _arn_trailing_id(callback_queue_arn) if callback_queue_arn else None,
        "callbackSourcePhoneNumber": callback.get("sourcePhoneNumber") or None,
        # DS-4: optional chat flow to use AFTER a P0 callback is placed.
        # When set and the outbound call succeeds, the chat is started with this
        # flow (which routes to a delayed queue) so the agent is offered the call
        # first. Non-string values are treated as absent — same guard as
        # callback_queue_arn above; a malformed field must not break the human handoff.
        "chatContactFlowId": callback.get("chatContactFlowId") if isinstance(callback.get("chatContactFlowId"), str) else None,
    }


def _arn_trailing_id(arn: str) -> str:
    """Extract the trailing resource ID from an ARN (last / segment)."""
    return arn.split("/")[-1]


def _arn_region(arn: str) -> str:
    """Extract the region (4th colon-delimited field) from an ARN.

    arn:aws:connect:<region>:<account>:instance/<id> -> <region>.
    Returns "" if the ARN is malformed (caller falls back to default region).
    """
    parts = arn.split(":")
    return parts[3] if len(parts) > 3 else ""


# ---------------------------------------------------------------------------
# Contact-attribute packing
# ---------------------------------------------------------------------------


def _build_attributes(
    *,
    severity: str,
    vin: str,
    vehicle_id: str,
    reason: str,
    summary: str | None,
    triage_session_id: str | None,
    driver_id: str | None,
    display_name: str | None,
    voice_session_id: str | None,
    tenant_id: str,
    escalation_type: str,
) -> dict[str, str]:
    """Build the Amazon Connect contact-attribute map.

    All values MUST be strings. Attribute keys may only contain
    alphanumeric, dash, and underscore characters. The contact flow
    reads `$.Attributes.severity` directly for priority bumping, so
    that key must be exactly "severity".

    We also pack all the context fields the CMS UI's
    EscalationContextPanel displays to Kevin, so he doesn't have to
    context-switch to see what's going on.
    """
    attrs: dict[str, str] = {
        "severity": severity,
        # Caps on every value, not just the free-text ones. Connect caps the
        # whole attribute MAP at 32KB, so an unbounded field is a per-request
        # denial: one oversized value and `StartChatContact` refuses, which on
        # the P0 path means no human. `reason`, `summary` and `escalationType`
        # were capped; these six were not (security Cycle 2 W3). Limits are set
        # well above any real value — a VIN is 17 characters, ids are UUID-ish —
        # so nothing legitimate truncates; they exist to bound the worst case.
        # Same reasoning as escalationType's cap below: no caller of this helper
        # can put an unbounded string into a third-party record.
        "vin": vin[:32],
        "vehicleId": vehicle_id[:64],
        "reason": reason[:500],  # Connect caps per-value at 32k but keep it sane
        "tenantId": tenant_id[:64],
        # Source discriminator so agents can tell VSA-driven chats from
        # other chat sources (web widget, etc.) at a glance.
        "vsaSource": "voice-escalation",
        # Safety-vs-service discriminator. Without this a sales or billing
        # enquiry arrives indistinguishable from a P3 vehicle fault, because
        # the service path still sends severity="P3" on the wire (see
        # tools/escalate.py's escalate_for_service for why that value cannot
        # simply be dropped). The tier-2 agent and the CMS
        # EscalationContextPanel read this to tell them apart.
        # Allowlisted in handler(); the cap is belt-and-braces so no caller of
        # this helper can put an unbounded string into a third-party record.
        "escalationType": escalation_type[:16],
    }
    if summary:
        attrs["summary"] = summary[:2000]
    if triage_session_id:
        attrs["triageSessionId"] = triage_session_id[:128]
    if driver_id:
        attrs["driverId"] = driver_id[:64]
    if display_name:
        attrs["driverName"] = display_name[:128]
    if voice_session_id:
        attrs["voiceSessionId"] = voice_session_id[:128]
    return attrs


# ---------------------------------------------------------------------------
# P0 roadside-dispatch queue write
# ---------------------------------------------------------------------------


def _queue_rsa_action(
    *,
    tenant_id: str,
    vin: str,
    vehicle_id: str,
    driver_id: str | None,
    display_name: str | None,
    reason: str,
    summary: str | None,
    triage_session_id: str | None,
    contact_id: str,
    voice_session_id: str | None,
    service_type: str = "roadside",
) -> str | None:
    """Best-effort REQUESTED row in vsa-{stage}-rsa-dispatch (RSA_DISPATCH_TABLE).

    Returns the dispatchId (table PK) on success, or None on failure. Never
    raises. The caller surfaces this as both rsaActionId (back-compat) and
    dispatchId (new status-read key) in the response.

    Status is LIFECYCLE_REQUESTED ("REQUESTED") — distinct from the retired
    VFO queue's "PENDING" approval semantics. Spec R1.

    F4.2: writes serviceType and callbackContact (resolved via _resolve_driver_phone).

    F5.D2 (Option D): resolves the vehicle's position via a direct invoke of the
    api-vehicle-live-state Lambda (LIVE_STATE_FN_NAME env var, wired by F5.D1).
    lat/lon are written to the row only when the position fix is fresh (seconds
    since CMS Flink stored it, live-state locationAgeSeconds, <= RSA_POSITION_MAX_AGE_SECONDS) AND it
    came from Redis. If the lookup fails,
    times out, or returns a stale position, the row is still written with no
    lat/lon so the consumer's existing fail-closed path produces FAILED with its
    reason. A days-old DDB-fallback coordinate for a tow truck is worse than a
    refusal — it looks precise but is wrong.

    Resolution failure does NOT break the human handoff. The RSA write is already
    best-effort (vsa-api-stack.ts:895); the human chat contact is started regardless.

    The consumer is the second line of defence (it re-checks for lat/lon and fails
    closed if absent). This producer is the first line: it tries to supply the
    coordinates so the consumer has something to work with.
    """
    dispatch_id = f"rsa-{uuid.uuid4()}"
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    item: dict[str, Any] = {
        "dispatchId": dispatch_id,
        "createdAt": now,
        "status": LIFECYCLE_REQUESTED,
        "severity": "P0",
        "source": "vsa-escalate",
        "tenantId": tenant_id,
        "vehicleId": vehicle_id,
        "vin": vin,
        "reason": reason[:500],
        "connectContactId": contact_id,
        # F4.2: serviceType is needed by the consumer to book the correct service.
        # For /escalate, all P0 dispatches are roadside; this path is structurally
        # impossible for service escalations (force-False above).
        "serviceType": service_type,
    }

    # F5.D2: Resolve vehicle position via direct Lambda invoke (Option D).
    # _resolve_vehicle_position applies the freshness gate and returns None when
    # the position is stale, Redis was unreachable, or the invoke failed.
    # On None, we still write the row — the consumer's fail-closed path handles it.
    # F7.2: pass vin so the position is refused if vehicleId↔VIN identity cannot
    # be confirmed (mismatch or absent vin in the vehicles row).
    position = _resolve_vehicle_position(vehicle_id, expected_vin=vin)
    if position is not None:
        item["lat"] = str(position[0])
        item["lon"] = str(position[1])
        log.info(
            "rsa-dispatch: resolved position for vehicle=%s (dispatch=%s)",
            vehicle_id, dispatch_id,
        )
    else:
        log.info(
            "rsa-dispatch: no fresh position for vehicle=%s (dispatch=%s); "
            "writing row without lat/lon — consumer will fail closed",
            vehicle_id, dispatch_id,
        )

    # F4.2: callbackContact — resolve the driver's phone number.
    # _resolve_driver_phone is already called for the outbound callback path, but
    # the consumer also needs it for the provider call. Resolve here (best-effort;
    # absent phone is not a blocker for dispatch row creation, but the consumer
    # will log it as missing context).
    # Assignment-checked like the outbound call: the RSA row's contact number
    # must be the driver of this vehicle, not whichever driverId was sent.
    callback_contact = _resolve_driver_phone(driver_id, vehicle_id) or ""
    if callback_contact:
        item["callbackContact"] = callback_contact
    if driver_id:
        item["driverId"] = driver_id
    if display_name:
        item["driverName"] = display_name
    if summary:
        item["summary"] = summary[:2000]
    if triage_session_id:
        item["triageSessionId"] = triage_session_id
    if voice_session_id:
        item["voiceSessionId"] = voice_session_id

    try:
        _get_ddb().Table(_RSA_DISPATCH_TABLE).put_item(Item=item)
        log.info(
            "rsa-dispatch: wrote REQUESTED dispatch %s (vehicle=%s, contact=%s)",
            dispatch_id, vehicle_id, contact_id,
        )
        return dispatch_id
    except (BotoCoreError, ClientError) as e:
        log.exception(
            "rsa-dispatch: put_item failed for vehicle=%s contact=%s: %s",
            vehicle_id, contact_id, e,
        )
        return None


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    # Start of the Customer Profiles / Cases budget (crm.Budget): those steps
    # make no call once the handoff could be put at risk.
    started = crm.clock()
    # -------- Auth: pull tenantId from JWT claims --------
    claims = (event.get("requestContext") or {}).get("authorizer", {}).get("claims") or {}
    caller_tenant = claims.get("custom:tenantId") or claims.get("tenantId")
    if not caller_tenant:
        return _response(401, {"error": "missing tenantId claim"})

    # -------- Parse + validate body --------
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "invalid JSON"})

    severity = (body.get("severity") or "").strip()
    if severity not in ("P0", "P1", "P2", "P3"):
        return _response(400, {"error": "severity must be one of P0, P1, P2, P3"})

    vin = (body.get("vin") or "").strip()
    vehicle_id = (body.get("vehicleId") or "").strip()
    reason = (body.get("reason") or "").strip()
    if not vin:
        return _response(400, {"error": "vin required"})
    if not vehicle_id:
        return _response(400, {"error": "vehicleId required"})
    if not reason:
        return _response(400, {"error": "reason required"})

    summary = body.get("summary")
    triage_session_id = body.get("triageSessionId")
    driver_id = body.get("driverId")
    display_name = body.get("displayName")
    voice_session_id = body.get("voiceSessionId")
    # escalationType: "safety" (default/back-compat when ABSENT) or "service".
    # The service path carries no P-level semantics — roadside dispatch
    # is structurally impossible for it regardless of any other field.
    #
    # Allowlisted, not coerced (security review Cycle 1 W2). The value is
    # stamped into Amazon Connect contact attributes read by a contact flow
    # and the CMS EscalationContextPanel, neither in this repo. Previously any
    # unrecognised value — a typo like "servic", a 40KB string, a confusable —
    # silently became safety semantics (outbound callback on P0) and was
    # written verbatim into the contact. Absent keeps its safety default;
    # present-but-unknown is rejected.
    raw_escalation_type = body.get("escalationType")
    if raw_escalation_type is None:
        escalation_type = "safety"
    elif isinstance(raw_escalation_type, str) and raw_escalation_type in _ESCALATION_TYPES:
        escalation_type = raw_escalation_type
    else:
        return _response(400, {"error": "escalationType must be one of: safety, service"})
    is_service_escalation = escalation_type == "service"

    # -------- Load tenant Connect config --------
    try:
        cfg = _load_tenant_connect_config(caller_tenant)
    except (BotoCoreError, ClientError) as e:
        log.exception("tenant-config read failed for %s: %s", caller_tenant, e)
        return _response(500, {"error": "tenant config lookup failed"})
    if not cfg:
        return _response(
            500,
            {"error": f"tenant {caller_tenant} has no connect channel configured"},
        )

    # dispatchRoadside default:
    #   - service escalations: always False (structural, cannot be overridden)
    #   - safety escalations: True only for P0 AND when tenant flag is set
    # An explicit dispatchRoadside in the request body still wins for the
    # safety path — that is the user-confirmed tap in Phase A iOS.
    if is_service_escalation:
        # Service path: force False regardless of anything in the request body.
        # A caller supplying dispatchRoadside:true on a service escalation is
        # silently ignored — there is no scenario in which a purchase enquiry
        # should dispatch a roadside truck.
        dispatch_roadside = False
    elif "dispatchRoadside" in body:
        # Explicit value from the request wins (user-confirmed tap path).
        dispatch_roadside = bool(body["dispatchRoadside"])
    else:
        # Safety path default: P0 + tenant flag enables auto-dispatch.
        # Default false per spec D3 — tenant must opt in.
        dispatch_roadside = (severity == "P0" and cfg.get("autoRequestRoadsideOnP0", False))

    # Only honor for P0 — dispatching roadside for a P3 tire-pressure
    # warning would be silly (and expensive).
    if severity != "P0":
        dispatch_roadside = False

    # Security W1: the two P0 side effects that reach the physical world — a
    # roadside dispatch (from the explicit body value or the tenant auto flag)
    # and an outbound phone call to the driver — each need a platform-recorded
    # P0 behind them. The body's severity and dispatch flag are caller-written
    # and prove nothing on their own. Checked once, only when one of the two
    # could fire. The chat still opens either way.
    #
    # The decision row binds tenant + VIN; the side effects act on `vehicleId`
    # (live-state position, driver assignment). Both come from the body, so the
    # vehicle must also be shown to BE that VIN — otherwise a verified P0 on
    # vehicle A could dispatch to, or ring the driver of, vehicle B.
    p0_verified = (
        severity == "P0"
        and not is_service_escalation
        and bool(dispatch_roadside or cfg.get("callbackFlowId"))
        and _verified_p0_decision(triage_session_id, caller_tenant, vin)
        and _vehicle_matches_vin(vehicle_id, vin)
    )
    # Security W1, option (b) (user decision 2026-09-25): the caller must BE
    # the driver assigned to this vehicle. The decision row above is
    # caller-authored (`/triage` keys it on a body `sessionId`), so a signed-in
    # tenant user could seed a real P0 row for a vehicle they do not drive and
    # pass every other gate. Checked last, so non-P0 and unverified requests
    # never read the drivers table. No delegated escalation in v1: a
    # dispatcher escalating for a driver gets the chat, not the side effects.
    caller_driver_id: str | None = None
    if p0_verified:
        caller_driver_id = _caller_assigned_driver(claims, driver_id, vehicle_id)
        p0_verified = caller_driver_id is not None
    if dispatch_roadside and not p0_verified:
        dispatch_roadside = False

    # DS-4: mint a correlation id BEFORE either contact is created.
    # When a P0 callback applies, this id is passed to the outbound call
    # as `escalationId` (rather than the chat's contact id, which does not
    # exist yet at call time) and stamped on the chat as
    # `p0CallbackEscalationId` so an agent can tie the two contacts together.
    # For all non-callback paths this is unused and costs nothing.
    p0_correlation_id = str(uuid.uuid4())

    # DS-4: determine whether to attempt the callback BEFORE the chat.
    # The callback path needs p0_verified (already computed), a configured
    # flow, and a resolvable driver phone. Resolve the phone here so we can
    # use it for both the call and the p0CallbackPlaced attribute decision.
    callback_flow_id: str | None = cfg.get("callbackFlowId") if p0_verified else None
    driver_phone_for_callback: str | None = None
    if callback_flow_id:
        driver_phone_for_callback = _resolve_driver_phone(caller_driver_id, vehicle_id)

    will_place_call = bool(callback_flow_id and driver_phone_for_callback)

    # -------- Customer Profiles: started alongside the P0 callback --------
    # Issue 2026-11-03-p0-callback-starves-crm-profile: run after the call, the
    # profile step found the 2.5 s pre-chat budget used up by a cold call (2.3 s), so
    # the P0 chat, the one this matters most for, carried no profile. Run before the
    # call, it would delay the call (the safety path). So it runs on its own thread,
    # started here: nothing else new happens before the call on this thread. Its
    # calls keep the same budget from handler entry (crm.PRE_CHAT_CUTOFF_S), and the
    # wait below ends at crm.PRE_CHAT_DEADLINE_S, so the chat starts no later than
    # before. Started on every path (P1-P3 wait for it straight away).
    crm_budget = crm.Budget(started)
    profile_step = _ProfileStep(
        budget=crm_budget,
        tenant_id=caller_tenant,
        tenant_instance_arn=cfg.get("instanceArn"),
        claims=claims,
        body_driver_id=driver_id,
        display_name=display_name,
    ).start()

    # -------- P0 outbound callback (BEFORE chat) — DS-4 --------
    # When a P0 callback applies, place the outbound call first so Connect
    # can offer it to an agent before the chat arrives. The chat uses the
    # caller id `p0_correlation_id` instead of the (not-yet-created) chat
    # contact id. On failure the chat still starts with the normal flow.
    #
    # Gated like the dispatch (security W1): `p0_verified` is the same
    # multi-factor gate as before. The number dialled is the CALLER's own
    # (the verified assigned driver above), never a body-named driver.
    callback_initiated = False
    if will_place_call:
        callback_initiated = _initiate_callback(
            instance_id=cfg["instanceId"],
            instance_region=cfg["instanceRegion"],
            contact_flow_id=callback_flow_id,  # type: ignore[arg-type]
            driver_phone=driver_phone_for_callback,  # type: ignore[arg-type]
            escalation_id=p0_correlation_id,
            queue_id=cfg.get("callbackQueueId"),
            source_phone_number=cfg.get("callbackSourcePhoneNumber"),
        )

    # DS-4: choose the chat flow.
    # When the call was successfully placed AND the tenant has configured a
    # dedicated after-callback chat flow, use that flow (it routes to the
    # delayed queue so the agent sees the call first).
    # In all other cases (no call, call failed, key not set) use the normal
    # chat flow exactly as before — no change for any non-P0-callback path.
    if callback_initiated and cfg.get("chatContactFlowId"):
        chat_flow_id = cfg["chatContactFlowId"]
    else:
        chat_flow_id = cfg["contactFlowId"]

    # -------- StartChatContact --------
    # DS-4: add p0CallbackPlaced and p0CallbackEscalationId to the attributes
    # so the agent can see whether a call was placed and match the two contacts.
    attrs = _build_attributes(
        severity=severity,
        vin=vin,
        vehicle_id=vehicle_id,
        reason=reason,
        summary=summary,
        triage_session_id=triage_session_id,
        driver_id=driver_id,
        display_name=display_name,
        voice_session_id=voice_session_id,
        tenant_id=caller_tenant,
        escalation_type=escalation_type,
    )
    # DS-4: stamp callback status and correlation id. "true"/"false" — Connect
    # attributes must be strings. Only added when the callback path was even
    # attempted (i.e. p0_verified + flow configured); non-P0 paths are unchanged.
    if will_place_call:
        attrs["p0CallbackPlaced"] = "true" if callback_initiated else "false"
        attrs["p0CallbackEscalationId"] = p0_correlation_id

    # -------- Customer Profiles (BEFORE the chat) --------
    # Spec 2026-11-02-avx-connect-profiles-cases. The chat carries the profile id as
    # `customerProfileId` and as the workspace's search attributes
    # (`profileSearchKey=_profileId`, `profileSearchValue`), which point the agent
    # workspace's profile search at the driver's profile when the chat arrives (no
    # flow block; Connect's API refuses the "Associate contact to profile" action;
    # spec task 6.6 checks the workspace itself). Best-effort: the step (started
    # above, alongside the callback) never raises and makes no call once
    # crm.PRE_CHAT_CUTOFF_S has passed, and this wait ends at crm.PRE_CHAT_DEADLINE_S,
    # so the chat below always starts in time.
    crm_cfg = crm.config_for(caller_tenant, cfg.get("instanceArn"))
    crm_driver_id = crm.claim_driver_id(claims, driver_id) if crm_cfg else None
    crm_driver_row, crm_profile_id = profile_step.result()
    if crm_profile_id:
        attrs.update(crm.chat_profile_attributes(crm_profile_id))

    participant_display_name = display_name or "Driver"
    # Initial message surfaces in Kevin's transcript at the top of the
    # chat so he can see context even before he clicks into the panel.
    # Keep it short; the full story is in attributes.
    initial_message = _build_initial_message(
        severity=severity,
        vin=vin,
        display_name=display_name,
        reason=reason,
        summary=summary,
        rsa_pending=dispatch_roadside,
    )

    try:
        start_resp = _get_connect(cfg["instanceRegion"]).start_chat_contact(
            InstanceId=cfg["instanceId"],
            ContactFlowId=chat_flow_id,
            ParticipantDetails={"DisplayName": participant_display_name[:256]},
            Attributes=attrs,
            InitialMessage={
                "Content": initial_message,
                "ContentType": "text/plain",
            },
            ChatDurationInMinutes=_CHAT_DURATION_MINUTES,
            SupportedMessagingContentTypes=["text/plain", "text/markdown"],
        )
    except ClientError as e:
        log.exception(
            "start_chat_contact failed for tenant=%s vehicle=%s: %s",
            caller_tenant, vehicle_id, e,
        )
        return _response(
            502,
            {
                # First, and the detail kept short: the escalate tool reads
                # this body, and the P0 call placed before the chat may
                # already be ringing (callers must not report a lost handoff).
                "callbackInitiated": callback_initiated,
                "p0CallbackEscalationId": p0_correlation_id if callback_initiated else None,
                "error": "Amazon Connect start_chat_contact failed",
                "detail": e.response.get("Error", {}).get("Message", str(e))[:200],
            },
        )
    except BotoCoreError as e:
        log.exception("start_chat_contact transport error: %s", e)
        return _response(502, {"error": "Amazon Connect transport error",
                               "callbackInitiated": callback_initiated})

    contact_id = start_resp.get("ContactId") or ""
    participant_id = start_resp.get("ParticipantId") or ""
    participant_token = start_resp.get("ParticipantToken") or ""
    if not (contact_id and participant_id and participant_token):
        # Shouldn't happen per the API contract, but guard anyway.
        log.error("start_chat_contact missing fields: %s", start_resp)
        return _response(502, {"error": "Connect returned incomplete StartChatContact response",
                               "callbackInitiated": callback_initiated})

    # ConnectionExpiry for the client UI: tied to the chat duration we
    # requested. The participant token itself lives as long as the
    # contact does, but we want iOS to surface an expiry consistent
    # with chat-session lifetime.
    connection_expiry = (
        datetime.now(timezone.utc) + timedelta(minutes=_CHAT_DURATION_MINUTES)
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    log.info(
        "escalate: contact=%s severity=%s tenant=%s vehicle=%s callback_initiated=%s",
        contact_id, severity, caller_tenant, vehicle_id, callback_initiated,
    )

    # -------- P0 RSA queue write (best-effort) --------
    # DS-4: use contact_id (chat) for the RSA row's connectContactId; the
    # call's correlation is tracked via p0_correlation_id on the chat attrs.
    rsa_action_id: str | None = None
    if dispatch_roadside:
        rsa_action_id = _queue_rsa_action(
            tenant_id=caller_tenant,
            vin=vin,
            vehicle_id=vehicle_id,
            driver_id=caller_driver_id,
            display_name=display_name,
            reason=reason,
            summary=summary,
            triage_session_id=triage_session_id,
            contact_id=contact_id,
            voice_session_id=voice_session_id,
        )

    # -------- Vehicle object, case, contact link (AFTER the chat) --------
    # Best-effort and budgeted (crm.POST_CHAT_CUTOFF_S); the chat already exists.
    if crm_cfg:
        _record_crm_after_chat(
            crm_cfg=crm_cfg,
            budget=crm_budget,
            tenant_id=caller_tenant,
            driver_id=crm_driver_id,
            driver_row=crm_driver_row,
            profile_id=crm_profile_id,
            vin=vin,
            vehicle_id=vehicle_id,
            severity=severity,
            reason=reason,
            summary=summary,
            triage_session_id=triage_session_id,
            voice_session_id=voice_session_id,
            escalation_type=escalation_type,
            contact_id=contact_id,
        )

    # T1.6a: p0Callback tells the bidi runtime whether the call was placed so it
    # can emit "platform.callback.ringing" to iOS. Only present when a callback
    # was even attempted (will_place_call); never present on non-callback paths.
    # placed: the ground-truth flag the handler logs as callback_initiated.
    # maskedNumber: only when placed; derived from the driver number that was
    # dialled (driver_phone_for_callback). Full E.164 number never leaves here.
    response_body: dict[str, Any] = {
        "contactId": contact_id,
        "participantId": participant_id,
        "participantToken": participant_token,
        "connectionExpiry": connection_expiry,
        "severity": severity,
        "rsaDispatched": rsa_action_id is not None,
        "rsaActionId": rsa_action_id,
        "dispatchId": rsa_action_id,
    }
    if will_place_call:
        p0cb: dict[str, Any] = {"placed": callback_initiated}
        if callback_initiated and driver_phone_for_callback:
            # Mask all but the last 4 digits. Extract digits only (E.164 starts
            # with "+", which is not a digit) to guard against exotic formats.
            digits = "".join(c for c in driver_phone_for_callback if c.isdigit())
            last4 = digits[-4:] if len(digits) >= 4 else digits
            p0cb["maskedNumber"] = f"\u2022\u2022\u2022-\u2022\u2022\u2022-{last4}"
        response_body["p0Callback"] = p0cb
    return _response(200, response_body)


def _build_initial_message(
    *,
    severity: str,
    vin: str,
    display_name: str | None,
    reason: str,
    summary: str | None,
    rsa_pending: bool,
) -> str:
    """Build the first message Kevin sees in his chat transcript.

    Keep it scannable: severity, driver, symptom, roadside status.
    The contact attributes carry the full structured data; this is just
    the human-readable hook so the agent knows what's going on before
    he clicks anything.
    """
    lines: list[str] = []
    lines.append(f"[VSA {severity}] {display_name or 'Driver'} — {reason[:200]}")
    if summary:
        lines.append(f"Summary: {summary[:500]}")
    lines.append(f"VIN {vin}")
    if rsa_pending:
        lines.append("Roadside dispatch queued (REQUESTED).")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Outbound callback helpers
# ---------------------------------------------------------------------------

def _cms_table(env_var: str, suffix: str) -> str | None:
    """Explicit env var, else derived from CMS_STAGE, else None.

    The old default was `cms-dev-storage-drivers` whenever CMS_STAGE was unset
    — which it was on the deployed Lambda — so every lookup read a table that
    does not exist and the callback silently never fired. None makes the
    unconfigured case explicit; callers fail closed on it.
    """
    explicit = os.environ.get(env_var)
    if explicit:
        return explicit
    stage = os.environ.get("CMS_STAGE")
    return f"cms-{stage}-{suffix}" if stage else None


_CMS_DRIVERS_TABLE = _cms_table("CMS_DRIVERS_TABLE", "storage-drivers")
_CMS_VEHICLES_TABLE = _cms_table("CMS_VEHICLES_TABLE", "storage-vehicles")

# Build the Customer Profiles / Cases / DynamoDB clients at Lambda init, so their
# construction never counts against an escalation's budget. No-op when off.
crm.warm_clients()


def _vehicle_matches_vin(vehicle_id: str | None, vin: str | None) -> bool:
    """True only if the CMS vehicles row for `vehicle_id` carries `vin`.

    Binds the two body-supplied vehicle identifiers to each other before any
    P0 side effect. Fails closed on an unset table, a missing row, a missing
    `vin` attribute, or any DynamoDB error. Never logs the VIN (PII, F8.1).
    """
    if not _CMS_VEHICLES_TABLE:
        log.warning("vehicle-binding: CMS_VEHICLES_TABLE not configured — refusing")
        return False
    if not vehicle_id or not vin:
        return False
    try:
        item = _get_ddb().Table(_CMS_VEHICLES_TABLE).get_item(
            Key={"vehicleId": vehicle_id}
        ).get("Item")
    except Exception as e:  # noqa: BLE001
        log.warning("vehicle-binding: lookup failed for %s (%s) — refusing", vehicle_id, type(e).__name__)
        return False
    if not item or item.get("vin") != vin:
        log.warning("vehicle-binding: vehicle %s does not carry the request VIN — refusing", vehicle_id)
        return False
    return True


def _caller_assigned_driver(
    claims: dict[str, Any], body_driver_id: Any, vehicle_id: str | None
) -> str | None:
    """The caller's own driverId, only if that driver is assigned to `vehicle_id`.

    Security W1, option (b). Identity comes from the `custom:driverId` claim,
    which API Gateway's Cognito authorizer has verified and which is immutable
    and excluded from every app client's `WriteAttributes` in both pools the
    API trusts, so a caller cannot set it on their own account. A body
    `driverId` is caller-written: it may repeat the claim but never replace it.

    Returns None (refuse the P0 side effects) on: no claim, a body driverId that
    names someone else, an unset drivers table, a missing row, an assignment to
    a different vehicle, or any DynamoDB error. Logs the reason, never a phone.
    """
    caller_driver = claims.get("custom:driverId")
    if not isinstance(caller_driver, str) or not caller_driver.strip():
        log.warning("p0-driver-gate: refused reason=no_driver_claim vehicle=%s", vehicle_id)
        return None
    if body_driver_id not in (None, "") and body_driver_id != caller_driver:
        log.warning("p0-driver-gate: refused reason=body_driver_not_caller vehicle=%s", vehicle_id)
        return None
    if not _CMS_DRIVERS_TABLE or not vehicle_id:
        log.warning("p0-driver-gate: refused reason=unconfigured vehicle=%s", vehicle_id)
        return None
    try:
        item = _get_ddb().Table(_CMS_DRIVERS_TABLE).get_item(
            Key={"driverId": caller_driver}
        ).get("Item")
    except Exception as e:  # noqa: BLE001
        log.warning("p0-driver-gate: refused reason=lookup_failed (%s) vehicle=%s",
                    type(e).__name__, vehicle_id)
        return None
    if not item or item.get("assignedVehicleId") != vehicle_id:
        log.warning("p0-driver-gate: refused reason=not_assigned_driver vehicle=%s", vehicle_id)
        return None
    return caller_driver


def _resolve_driver_phone(driver_id: str | None, vehicle_id: str | None) -> str | None:
    """The phone of `driver_id`, only if that driver is assigned to `vehicle_id`.

    Both ids come from the request body. The assignment check means the call
    goes to the driver of the vehicle the escalation is about, not to whoever
    the caller names. Returns None on any failure or mismatch.
    """
    if not driver_id or not vehicle_id:
        return None
    if not _CMS_DRIVERS_TABLE:
        log.warning("callback: CMS_DRIVERS_TABLE not configured — no phone lookup")
        return None
    try:
        table = _get_ddb().Table(_CMS_DRIVERS_TABLE)
        resp = table.get_item(Key={"driverId": driver_id})
        item = resp.get("Item")
        if not item:
            return None
        if item.get("assignedVehicleId") != vehicle_id:
            log.warning("callback: driver %s is not assigned to vehicle %s — not calling",
                        driver_id, vehicle_id)
            return None
        return item.get("phone")
    except Exception as e:
        log.warning("Failed to resolve driver phone for %s: %s", driver_id, e)
        return None


def _initiate_callback(
    *,
    instance_id: str,
    instance_region: str,
    contact_flow_id: str,
    driver_phone: str,
    escalation_id: str,
    queue_id: str | None = None,
    source_phone_number: str | None = None,
) -> bool:
    """Start an outbound voice contact to the driver. Best-effort, never raises.

    Connect needs a caller ID: a queue whose outbound caller-ID number is set
    (`queue_id`), or a claimed number (`source_phone_number`). The call used to
    pass neither. With neither configured this refuses before calling Connect,
    with a log line naming what to configure, rather than failing inside it.
    """
    if not queue_id and not source_phone_number:
        log.warning(
            "callback: no caller ID configured (escalation.callback.queueArn or "
            ".sourcePhoneNumber) — not calling"
        )
        return False
    kwargs: dict[str, Any] = {
        "InstanceId": instance_id,
        "ContactFlowId": contact_flow_id,
        "DestinationPhoneNumber": driver_phone,
        "Attributes": {"escalationId": escalation_id},
    }
    if queue_id:
        kwargs["QueueId"] = queue_id
    if source_phone_number:
        kwargs["SourcePhoneNumber"] = source_phone_number
    try:
        _get_connect(instance_region, callback=True).start_outbound_voice_contact(**kwargs)
        log.info(
            "callback: initiated outbound call to %s (escalation=%s)",
            driver_phone[:4] + "****",
            escalation_id,
        )
        return True
    except Exception as e:
        log.warning("callback: failed to initiate outbound call: %s", e)
        return False


# ---------------------------------------------------------------------------
# Customer Profiles + Cases helpers (spec 2026-11-02-avx-connect-profiles-cases)
# ---------------------------------------------------------------------------
# Each read here is one GetItem the role already holds (CmsVehicleDriverRead,
# TriageDecisionRead), bounded by crm._CRM_CLIENT_CONFIG (0.5 s connect, 1.0 s read,
# one attempt) so it stays inside the budget: the driver read through crm.ddb_client(),
# the other two through crm.ddb(). All return None on failure, never raise.


def _crm_driver_row(driver_id: str) -> dict[str, Any] | None:
    """The CMS drivers row for the caller's own (claim) driverId, or None.

    Runs on the profile step's thread (_ProfileStep), so it reads through a plain
    low-level client (crm.ddb_client()): boto3 clients are thread-safe, resources aren't,
    and the handler's thread may use crm.ddb() after the chat while a late step still
    runs. The key goes out in AttributeValue form and the item comes back in it, hence
    the deserializer (issue 2026-11-04-escalate-crm-driver-row-unread: through
    crm.ddb().meta.client the key was typed twice and every read failed).
    """
    if not _CMS_DRIVERS_TABLE or not driver_id:
        return None
    try:
        item = crm.ddb_client().get_item(
            TableName=_CMS_DRIVERS_TABLE, Key={"driverId": {"S": driver_id}}
        ).get("Item")
        if not item:
            return None
        deserialize = TypeDeserializer().deserialize
        return {k: deserialize(v) for k, v in item.items()}
    except Exception as e:  # noqa: BLE001
        log.warning("escalate-crm: driver lookup failed (%s)", type(e).__name__)
        return None


class _ProfileStep:
    """The pre-chat Customer Profiles step on its own thread (issue
    2026-11-03-p0-callback-starves-crm-profile).

    Started just before the P0 callback, so a slow call no longer uses up the step's
    budget and the call starts no later than before (nothing else new runs before it
    on the handler's thread). The step's calls keep the budget from handler entry
    (crm.PRE_CHAT_CUTOFF_S). `result()` waits for it until crm.PRE_CHAT_DEADLINE_S
    from entry at most, then gives up: the chat starts without a profile and a late
    result is never read. Never raises.
    """

    def __init__(
        self,
        *,
        budget: Any,
        tenant_id: str,
        tenant_instance_arn: Any,
        claims: dict[str, Any],
        body_driver_id: Any,
        display_name: Any,
    ) -> None:
        self._budget = budget
        self._tenant_id = tenant_id
        self._tenant_instance_arn = tenant_instance_arn
        self._claims = claims
        self._body_driver_id = body_driver_id
        self._display_name = display_name
        self._done = threading.Event()
        self._driver_row: dict[str, Any] | None = None
        self._profile_id: str | None = None

    def start(self) -> "_ProfileStep":
        try:
            threading.Thread(target=self._run, name="escalate-crm-profile", daemon=True).start()
        except Exception as e:  # noqa: BLE001 — e.g. "can't start new thread": no profile, chat goes on
            log.warning("escalate-crm: profile step not started (%s)", type(e).__name__)
            self._done.set()
        return self

    def _run(self) -> None:
        try:
            cfg = crm.config_for(self._tenant_id, self._tenant_instance_arn)
            driver_id = crm.claim_driver_id(self._claims, self._body_driver_id) if cfg else None
            if cfg and driver_id:
                row = _crm_driver_row(driver_id) if self._budget.pre_chat_ok() else None
                profile_id = crm.find_or_create_profile(
                    cfg,
                    self._budget,
                    tenant_id=self._tenant_id,
                    driver_id=driver_id,
                    driver_row=row,
                    display_name=self._display_name,
                )
                self._driver_row, self._profile_id = row, profile_id
        except Exception as e:  # noqa: BLE001 — never block the handoff
            log.warning("escalate-crm: profile step failed (%s)", type(e).__name__)
        finally:
            self._done.set()

    def result(self) -> tuple[dict[str, Any] | None, str | None]:
        """(driver row, profile id) once the step is done, or (None, None) at the deadline."""
        wait_s = max(0.0, crm.PRE_CHAT_DEADLINE_S - self._budget.elapsed())
        if not self._done.wait(timeout=wait_s):
            log.info("crm: skipped step=profile-wait reason=deadline")
            return None, None
        return self._driver_row, self._profile_id


def _crm_vehicle_row(vehicle_id: str, vin: str) -> dict[str, Any] | None:
    """The CMS vehicles row, only if it carries the request VIN (vehicleId↔VIN binding)."""
    if not _CMS_VEHICLES_TABLE or not vehicle_id or not vin:
        return None
    try:
        item = crm.ddb().Table(_CMS_VEHICLES_TABLE).get_item(Key={"vehicleId": vehicle_id}).get("Item")
    except Exception as e:  # noqa: BLE001
        log.warning("escalate-crm: vehicle lookup failed (%s)", type(e).__name__)
        return None
    if not item or item.get("vin") != vin:
        return None
    return item


def _crm_triage_item(triage_session_id: Any) -> dict[str, Any] | None:
    """The triage decision row named by triageSessionId, or None."""
    if not _TRIAGE_DECISIONS_TABLE or not isinstance(triage_session_id, str) or not triage_session_id.strip():
        return None
    try:
        return crm.ddb().Table(_TRIAGE_DECISIONS_TABLE).get_item(
            Key={"sessionId": triage_session_id}
        ).get("Item")
    except Exception as e:  # noqa: BLE001
        log.warning("escalate-crm: triage lookup failed (%s)", type(e).__name__)
        return None


def _record_crm_after_chat(
    *,
    crm_cfg: dict[str, Any],
    budget: Any,
    tenant_id: str,
    driver_id: str | None,
    driver_row: dict[str, Any] | None,
    profile_id: str | None,
    vin: str,
    vehicle_id: str,
    severity: str,
    reason: str,
    summary: Any,
    triage_session_id: Any,
    voice_session_id: Any,
    escalation_type: str,
    contact_id: str,
) -> None:
    """Vehicle object, case and contact link. Never raises: the chat already exists.

    The vehicle object needs a profile, the vehicleId↔VIN binding and the caller
    assigned to that vehicle; open faults come only from a triage decision row
    bound to this tenant + VIN.
    """
    try:
        vehicle_row = _crm_vehicle_row(vehicle_id, vin) if budget.post_chat_ok() else None
        decision = _crm_triage_item(triage_session_id) if budget.post_chat_ok() else None
        faults = crm.open_faults_from_decision(decision, tenant_id, vin)

        vehicle_key = None
        if (
            profile_id
            and driver_id
            and vehicle_row is not None
            and (driver_row or {}).get("assignedVehicleId") == vehicle_id
        ):
            vehicle_key = crm.put_vehicle(
                crm_cfg,
                budget,
                tenant_id=tenant_id,
                driver_id=driver_id,
                vin=vin,
                vehicle_id=vehicle_id,
                vehicle_row=vehicle_row,
                open_faults=faults,
            )

        key = crm.escalation_key(
            tenant_id=tenant_id,
            vin=vin,
            escalation_type=escalation_type,
            severity=severity,
            triage_session_id=triage_session_id,
            voice_session_id=voice_session_id,
            reason=reason,
        )
        fields = crm.case_fields(
            crm_cfg,
            key=key,
            severity=severity,
            reason=reason,
            summary=summary,
            vin=vin,
            vehicle_id=vehicle_id,
            model=crm.model_label(vehicle_row),
            fault_codes=faults,
            triage_session_id=triage_session_id,
            escalation_type=escalation_type,
            profile_id=profile_id,
        )
        case_id = crm.find_or_create_case(crm_cfg, budget, key=key, fields=fields)
        linked = crm.link_contact(crm_cfg, budget, case_id=case_id, contact_id=contact_id) if case_id else False
        log.info(
            "escalate-crm: contact=%s profile=%s vehicle_object=%s case=%s linked=%s elapsed=%.2fs",
            contact_id, profile_id, bool(vehicle_key), case_id, linked, budget.elapsed(),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("escalate-crm: after-chat step failed (%s)", type(e).__name__)
