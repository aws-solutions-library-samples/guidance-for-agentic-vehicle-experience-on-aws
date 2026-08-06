"""API: POST /escalate

Starts an Amazon Connect chat contact for handing off a driver to a human
fleet-support agent, and (for P0) queues a roadside dispatch action.

Request (all strings unless noted):
    {
      "severity": "P0|P1|P2|P3",          (required)
      "vin": "1HGBH41JXMN000024",         (required)
      "vehicleId": "VEH-0025",             (required)
      "reason": "engine overheating",      (required)
      "summary": "Nova conversation summary",  (optional)
      "triageSessionId": "<uuid>",         (optional)
      "dispatchRoadside": true,            (optional; default true for P0)
      "driverId": "DRV-0055",              (optional)
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
      "rsaActionId": "<uuid-or-null>"
    }

Design notes
------------

- Tenant isolation: resolves the Connect instance / flow / queue from the
  caller's tenant config (vsa-prod-tenant-config) rather than hardcoded
  env vars, so the same Lambda works for every tenant we add later.
- Attributes: the full triage/driver context is passed into the contact
  flow as standard Amazon Connect attributes. Kevin's CCP can read these
  server-side to populate the EscalationContextPanel. Values must be
  strings per the Connect API contract.
- Severity priority bump: the "michelin-vsa-escalation-flow" contact flow
  reads $.Attributes.severity and routes P0 to a high-priority branch of
  the chat queue. That logic lives in Connect, not here — we just set
  the attribute faithfully.
- P0 RSA: best-effort write to cms-prod-vfo-action-queue with status
  "PENDING". Non-fatal: if the write fails, we still return the
  contactId so the agent can dispatch manually.
- ChatDurationInMinutes: capped at 60 for the demo. Documented Connect
  max is 10080 (7 days); 60 is plenty for a driver-side chat that
  either resolves or hands off to roadside within minutes.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

# Env
_TENANT_CONFIG_TABLE = os.environ["TENANT_CONFIG_TABLE"]
_RSA_QUEUE_TABLE = os.environ.get("RSA_QUEUE_TABLE", f"cms-{os.environ.get('CMS_STAGE', 'dev')}-vfo-action-queue")
_CHAT_DURATION_MINUTES = int(os.environ.get("CHAT_DURATION_MINUTES", "60"))

# Lazy-initialized boto3 clients (init-time cost amortized across warm invocations)
_ddb = None
_connect = None


def _get_ddb():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb")
    return _ddb


def _get_connect(region: str | None = None):
    """Return a Connect client targeting the instance's region.

    Amazon Connect instances are regional. This Lambda may run in a
    different region (e.g. us-west-2) than the tenant's Connect instance
    (e.g. us-east-1), so we must target the instance's region explicitly —
    parsed from its ARN — instead of relying on the Lambda's default region.
    Calling StartChatContact against the wrong region returns
    "Resource not found of Type: INSTANCE".
    """
    global _connect
    if _connect is None or (region and getattr(_connect.meta, "region_name", None) != region):
        _connect = boto3.client("connect", region_name=region) if region else boto3.client("connect")
    return _connect


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
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

    return {
        "instanceArn": instance_arn,
        "instanceId": _arn_trailing_id(instance_arn),
        "instanceRegion": _arn_region(instance_arn),
        "contactFlowId": contact_flow_id,
        "queueArn": queue_arn,
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
        "vin": vin,
        "vehicleId": vehicle_id,
        "reason": reason[:500],  # Connect caps per-value at 32k but keep it sane
        "tenantId": tenant_id,
        # Source discriminator so agents can tell VSA-driven chats from
        # other chat sources (web widget, etc.) at a glance.
        "vsaSource": "voice-escalation",
    }
    if summary:
        attrs["summary"] = summary[:2000]
    if triage_session_id:
        attrs["triageSessionId"] = triage_session_id
    if driver_id:
        attrs["driverId"] = driver_id
    if display_name:
        attrs["driverName"] = display_name
    if voice_session_id:
        attrs["voiceSessionId"] = voice_session_id
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
) -> str | None:
    """Best-effort PENDING row in cms-prod-vfo-action-queue. Returns the
    action id on success, or None on failure. Never raises."""
    action_id = f"rsa-{uuid.uuid4()}"
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    item: dict[str, Any] = {
        "actionId": action_id,
        "createdAt": now,
        "status": "PENDING",
        "actionType": "ROADSIDE_DISPATCH",
        "severity": "P0",
        "source": "vsa-escalate",
        "tenantId": tenant_id,
        "vehicleId": vehicle_id,
        "vin": vin,
        "reason": reason[:500],
        "connectContactId": contact_id,
    }
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
        _get_ddb().Table(_RSA_QUEUE_TABLE).put_item(Item=item)
        log.info(
            "rsa-queue: wrote PENDING action %s (vehicle=%s, contact=%s)",
            action_id, vehicle_id, contact_id,
        )
        return action_id
    except (BotoCoreError, ClientError) as e:
        log.exception(
            "rsa-queue: put_item failed for vehicle=%s contact=%s: %s",
            vehicle_id, contact_id, e,
        )
        return None


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
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

    # dispatchRoadside: default True for P0, False otherwise. The @tool
    # side usually passes this explicitly, but honor the default for
    # direct callers.
    if "dispatchRoadside" in body:
        dispatch_roadside = bool(body["dispatchRoadside"])
    else:
        dispatch_roadside = severity == "P0"
    # Only honor for P0 — dispatching roadside for a P3 tire-pressure
    # warning would be silly (and expensive).
    if severity != "P0":
        dispatch_roadside = False

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

    # -------- StartChatContact --------
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
    )

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
            ContactFlowId=cfg["contactFlowId"],
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
                "error": "Amazon Connect start_chat_contact failed",
                "detail": e.response.get("Error", {}).get("Message", str(e))[:300],
            },
        )
    except BotoCoreError as e:
        log.exception("start_chat_contact transport error: %s", e)
        return _response(502, {"error": "Amazon Connect transport error"})

    contact_id = start_resp.get("ContactId") or ""
    participant_id = start_resp.get("ParticipantId") or ""
    participant_token = start_resp.get("ParticipantToken") or ""
    if not (contact_id and participant_id and participant_token):
        # Shouldn't happen per the API contract, but guard anyway.
        log.error("start_chat_contact missing fields: %s", start_resp)
        return _response(502, {"error": "Connect returned incomplete StartChatContact response"})

    # ConnectionExpiry for the client UI: tied to the chat duration we
    # requested. The participant token itself lives as long as the
    # contact does, but we want iOS to surface an expiry consistent
    # with chat-session lifetime.
    connection_expiry = (
        datetime.now(timezone.utc) + timedelta(minutes=_CHAT_DURATION_MINUTES)
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    log.info(
        "escalate: contact=%s severity=%s tenant=%s vehicle=%s",
        contact_id, severity, caller_tenant, vehicle_id,
    )

    # -------- P0 RSA queue write (best-effort) --------
    rsa_action_id: str | None = None
    if dispatch_roadside:
        rsa_action_id = _queue_rsa_action(
            tenant_id=caller_tenant,
            vin=vin,
            vehicle_id=vehicle_id,
            driver_id=driver_id,
            display_name=display_name,
            reason=reason,
            summary=summary,
            triage_session_id=triage_session_id,
            contact_id=contact_id,
            voice_session_id=voice_session_id,
        )

    # -------- P0 outbound callback (best-effort) --------
    # If the tenant has a callback flow configured and we have the driver's
    # phone number, trigger Connect to call the driver back. This ensures
    # the driver gets a human on the line even if they lose the chat session.
    callback_initiated = False
    if severity == "P0" and cfg.get("callbackFlowId"):
        driver_phone = _resolve_driver_phone(driver_id)
        if driver_phone:
            callback_initiated = _initiate_callback(
                instance_id=cfg["instanceId"],
                instance_region=cfg["instanceRegion"],
                contact_flow_id=cfg["callbackFlowId"],
                driver_phone=driver_phone,
                escalation_id=rsa_action_id or contact_id,
            )

    return _response(200, {
        "contactId": contact_id,
        "participantId": participant_id,
        "participantToken": participant_token,
        "connectionExpiry": connection_expiry,
        "severity": severity,
        "rsaDispatched": rsa_action_id is not None,
        "rsaActionId": rsa_action_id,
    })


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
        lines.append("Roadside dispatch queued (PENDING).")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Outbound callback helpers
# ---------------------------------------------------------------------------

_CMS_DRIVERS_TABLE = os.environ.get(
    "CMS_DRIVERS_TABLE",
    f"cms-{os.environ.get('CMS_STAGE', 'dev')}-storage-drivers"
)


def _resolve_driver_phone(driver_id: str | None) -> str | None:
    """Look up the driver's phone number from CMS. Returns None on failure."""
    if not driver_id:
        return None
    try:
        table = _get_ddb().Table(_CMS_DRIVERS_TABLE)
        resp = table.get_item(Key={"driverId": driver_id})
        item = resp.get("Item")
        return item.get("phone") if item else None
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
) -> bool:
    """Start an outbound voice contact to the driver. Best-effort, never raises."""
    try:
        _get_connect(instance_region).start_outbound_voice_contact(
            InstanceId=instance_id,
            ContactFlowId=contact_flow_id,
            DestinationPhoneNumber=driver_phone,
            Attributes={"escalationId": escalation_id},
        )
        log.info(
            "callback: initiated outbound call to %s (escalation=%s)",
            driver_phone[:4] + "****",
            escalation_id,
        )
        return True
    except Exception as e:
        log.warning("callback: failed to initiate outbound call: %s", e)
        return False
