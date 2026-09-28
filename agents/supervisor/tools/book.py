"""book tool — stub STAR ServiceRequestAdd emitter.

Phase 3 local testing only. Builds a STAR-shaped envelope per
VSA_STAR_JSON_SCHEMA.md §2 (common envelope) and §3 (ServiceRequestAdd
payload). No sink delivery — the envelope is returned for inspection.

Option 2 tool shape (narrow input, server fills rest): the LLM provides
only the fields it actually knows from the conversation. This tool
synthesizes vehicle, serviceCenter, warranty, and costEstimate from
fixtures and from ctx state populated by prior triage/retrieve calls.

When this moves to AgentCore, the "fill rest" logic becomes real lookups
against CMS, ADP, and tenant config. The @tool signature stays stable.
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, Field
from strands import tool

from agents.supervisor.config import Config
from agents.supervisor.session import SessionContext, utc_now_iso

log = logging.getLogger(__name__)

# --- CMS write config --------------------------------------------------------
#
# book() persists a "scheduled" row into cms-prod-storage-service-history
# so the iOS app and CMS UI can surface the voice-booked appointment.
# The write is env-gated so unit tests (and any environment where CMS isn't
# reachable) can skip it without special-casing.
#
# VSA_BOOK_WRITE_CMS: "true" (default) enables the CMS write. Anything else
#   disables it; book() still returns the envelope with status
#   "draft-created" so upstream callers (reasoning drawer, smoke test) see
#   the booking.
# VSA_CMS_SERVICE_HISTORY_TABLE: override the target table name. Defaults
#   to "cms-prod-storage-service-history" to match the deployed CMS stack.
# VSA_CMS_VEHICLES_TABLE: source table for make/model/odometer enrichment.
# VSA_CMS_REGION: DynamoDB region. All three CMS tables live in us-east-1
#   as of 2026-04-30.
CMS_SERVICE_HISTORY_TABLE = os.environ.get(
    "VSA_CMS_SERVICE_HISTORY_TABLE", f"cms-{os.environ.get('CMS_STAGE', 'dev')}-storage-service-history"
)
CMS_VEHICLES_TABLE = os.environ.get(
    "VSA_CMS_VEHICLES_TABLE", f"cms-{os.environ.get('CMS_STAGE', 'dev')}-storage-vehicles"
)
CMS_REGION = os.environ.get("VSA_CMS_REGION", "us-east-1")


def _cms_write_enabled() -> bool:
    """Read the env flag lazily so tests can toggle it per-test."""
    return os.environ.get("VSA_BOOK_WRITE_CMS", "true").lower() == "true"


# Lazy DDB resource cache. Not a module-level boto3 call so `import book`
# stays free for tests that don't have AWS credentials configured.
_ddb_resource: Any = None


def _get_ddb():
    global _ddb_resource
    if _ddb_resource is None:
        _ddb_resource = boto3.resource("dynamodb", region_name=CMS_REGION)
    return _ddb_resource


def _to_ddb_safe(value: Any) -> Any:
    """Convert floats to Decimal for DDB. All other types pass through."""
    if isinstance(value, float):
        # str() conversion avoids the Decimal(float) precision trap.
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_ddb_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_ddb_safe(v) for v in value]
    return value


# --- end CMS config ----------------------------------------------------------


CustomerAction = Literal[
    "create-ticket",
    "warm-transfer-to-fleet-ops",
    "warm-transfer-to-dealer",
    "schedule-roadside-if-vehicle-fails",
    "propose-slot",
    "book-appointment",
]


class BookingResult(BaseModel):
    requestNumber: str
    messageId: str
    status: Literal["draft-created", "sink-accepted", "sink-error"]
    envelope: dict = Field(
        ..., description="Full STAR ServiceRequestAdd envelope for audit"
    )


def call_book_endpoint(
    ctx: SessionContext,
    config: Config,
    vin: str,
    reported_symptom: str,
    customer_actions_taken: list[str],
    narrative: str,
    priority: str | None = None,
) -> dict[str, Any]:
    """Create a STAR envelope directly, bypassing the @tool wrapper.

    Parallel to agents.supervisor.tools.triage.call_triage_endpoint —
    exists so bidi_app.py can run book() server-side when P0
    classification fires, since Nova Sonic v2 doesn't reliably invoke
    registered tools (Strands #2021).

    Same contract as the @tool version: requires ctx.triage_classification
    and ctx.triage_decision_id to be set (enforced), validates actions,
    returns the BookingResult dict.

    ``priority`` overrides the priority used for the STAR payload, the
    service-history row's ``triagePriority`` / ``scheduledFor``, and the
    idempotency key. When None (the default the @tool wrapper uses so
    Nova cannot set priority itself), ``ctx.triage_classification`` is
    used. The driver-confirmed P0 path in
    ``_handle_confirmation_response`` passes ``priority="P0"`` because
    ``ctx.triage_classification`` there still holds the classifier's
    lower grade (issues/2026-09-27-driver-confirmed-p0-books-at-
    classifier-grade/); a driver-confirmed emergency must not book at
    the sensor-side non-urgent tier.

    **Idempotent per (session, effective_priority).** A subsequent call
    on the same ``ctx`` at the same priority returns the first call's
    result byte-identically — no new ``request_number``, no new DDB row,
    no new STAR envelope (issues/2026-09-28-p0-double-booking-auto-book-
    plus-model-book/). Both the auto-triage P0 auto-book at
    ``bidi_app._auto_triage_and_inject`` and Nova Sonic's own ``book``
    tool call reach this function during a single P0 session; without
    the guard, each minted a fresh ``request_number`` and a fresh row
    in ``cms-*-storage-service-history``. The cache lives on
    ``ctx.booked_by_priority`` (see ``SessionContext``) and stores the
    result for the effective priority; a legitimate escalation from
    P2/P3 to P0 during the session is not blocked.
    """
    if ctx.triage_classification is None or ctx.triage_decision_id is None:
        raise BookError(
            "Cannot book without a prior triage call. Call triage first."
        )

    valid_actions = set(CustomerAction.__args__)  # type: ignore[attr-defined]
    for action in customer_actions_taken:
        if action not in valid_actions:
            raise BookError(
                f"Invalid action {action!r}. Must be one of {sorted(valid_actions)}"
            )

    # Effective priority: an explicit override wins over the classifier's
    # verdict on ctx. The @tool wrapper never sets `priority`, so Nova
    # cannot pick its own; server-side callers (auto-triage P0 auto-book
    # in `_auto_triage_and_inject`; driver-confirmed P0 in
    # `_handle_confirmation_response`) may override.
    effective_priority = priority or ctx.triage_classification

    # Idempotency per (session_id, effective_priority). Populated below
    # after a completed call.
    cached_by_priority = getattr(ctx, "booked_by_priority", None)
    if isinstance(cached_by_priority, dict):
        cached = cached_by_priority.get(effective_priority)
        if cached is not None:
            log.info(
                "book: idempotent replay session=%s priority=%s "
                "requestNumber=%s status=%s",
                ctx.session_id,
                effective_priority,
                cached.get("requestNumber"),
                cached.get("status"),
            )
            return cached

    now = datetime.now(timezone.utc)
    date_slug = now.strftime("%Y-%m-%d")
    seq = uuid.uuid4().hex[:4].upper()
    request_number = f"VSA-{date_slug}-{seq}"
    message_id = f"msg-{uuid.uuid4()}"

    payload = _build_payload(
        request_number=request_number,
        vin=vin,
        priority=effective_priority,
        reported_symptom=reported_symptom,
        actions_taken=customer_actions_taken,
        narrative=narrative,
        tenant_id=ctx.tenant_id,
        # Pull the actually-offered center from session context (set in
        # bidi_app's tool_result handler when find_service_center
        # returned). Falls back to fixture if no center was offered
        # (e.g. P0 auto-escalation books without a prior find_service_center).
        offered_service_center=ctx.last_offered_service_center,
    )

    envelope = {
        "schemaVersion": "1.0.0",
        "messageType": "ServiceRequestAdd",
        "messageId": message_id,
        "sessionId": ctx.session_id,
        "tenantId": ctx.tenant_id,
        "createdAt": utc_now_iso(),
        "createdBy": "vsa-supervisor-v1",
        "correlation": {
            "triageDecisionId": ctx.triage_decision_id,
            "vin": vin,
            "customerId": None,
        },
        "payload": payload,
        "signature": _fake_signature(message_id),
    }

    log.info(
        "book direct: created draft %s priority=%s actions=%s",
        request_number,
        effective_priority,
        customer_actions_taken,
    )

    # Persist the booking as a "scheduled" row in CMS service-history so
    # the iOS Upcoming Service section and CMS UI can surface it. This
    # is best-effort: on failure we still return the envelope so the
    # reasoning drawer and smoke test see the booking, but the status
    # becomes "sink-error" to make the failure visible.
    cms_ok, service_date_iso = _write_cms_service_history(
        ctx,
        request_number=request_number,
        priority=effective_priority,
        reported_symptom=reported_symptom,
        narrative=narrative,
        vin=vin,
    )

    if cms_ok:
        envelope["correlation"]["cmsServiceHistoryKey"] = {
            "vehicleId": ctx.vehicle_id,
            "serviceDate": service_date_iso,
        }
        status: Literal["draft-created", "sink-accepted", "sink-error"] = (
            "sink-accepted"
        )
    elif _cms_write_enabled() and ctx.vehicle_id:
        # We tried and failed — make the failure visible in the result.
        status = "sink-error"
    else:
        # CMS write intentionally skipped (tests, or no vehicle_id in
        # context yet). Preserve the legacy "draft-created" status so
        # existing smoke tests and callers see no change in behavior.
        status = "draft-created"

    result = BookingResult(
        requestNumber=request_number,
        messageId=message_id,
        status=status,
        envelope=envelope,
    ).model_dump()

    # Populate the (session_id, effective_priority) idempotency cache so a
    # subsequent call at the same priority short-circuits above. Cache the
    # result irrespective of `status`: on sink-error, replaying the call
    # would generate a second envelope with the same failure and a fresh
    # `request_number`, which is exactly the class of duplicate this guard
    # exists to prevent — Nova retrying after "sink-error" should see the
    # same error it already saw, not a new ticket-number claim.
    if isinstance(cached_by_priority, dict):
        cached_by_priority[effective_priority] = result

    return result


def make_book_tool(ctx: SessionContext, config: Config):
    """Build the book tool bound to a session context."""

    @tool
    def book(
        reportedSymptom: str,
        customerActionsTaken: list[str],
        narrative: str,
    ) -> dict[str, Any]:
        """Create a STAR-shaped ServiceRequestAdd envelope for the current
        session. Only call this AFTER triage has returned AND the customer
        has confirmed they want a ticket created.

        You must supply the reportedSymptom in the caller's words, the
        narrative as a 1–3 sentence human-facing summary, and the list of
        actions the customer agreed to. The tool will fill in the VIN,
        vehicle, service center, warranty, and cost estimate automatically
        from the active session — do NOT pass VIN, you do not need to
        provide it.

        Args:
            reportedSymptom: The caller's own description of the problem,
                summarized in one sentence. Do not paraphrase into
                technical language.
            customerActionsTaken: List of actions the customer accepted.
                Allowed values: create-ticket, warm-transfer-to-fleet-ops,
                warm-transfer-to-dealer, schedule-roadside-if-vehicle-fails,
                propose-slot, book-appointment. Use only what the customer
                actually agreed to in the conversation.
            narrative: 1–3 sentence human-facing summary for the ticket.
                Include the classification and the recommended action.

        Returns:
            A dict with requestNumber, messageId, status, and the full
            envelope. Envelope follows VSA_STAR_JSON_SCHEMA.md §2–§3.
        """
        if not ctx.vin:
            raise BookError(
                "Cannot book: session VIN is not set. Driver context is missing."
            )
        # DO NOT default the classification. Removed 2026-09-23 — see
        # issues/2026-09-23-triage-401-llm-improvises-classification/.
        #
        # This block used to read:
        #
        #     if ctx.triage_classification is None:
        #         ctx.triage_classification = "P3"
        #     if ctx.triage_decision_id is None:
        #         ctx.triage_decision_id = ctx.session_id
        #
        # It was added to work around Nova Sonic stalling at "I need to
        # check the severity first... One moment please" without actually
        # calling `triage`. The workaround is a fail-OPEN on the platform's
        # only deterministic P0–P3 seam, and it fired in staging on
        # 2026-09-23: `triage` returned 401, the LLM narrated a P0
        # pull-over instruction of its own invention, and this default
        # silently supplied "P3" so the booking succeeded — producing a
        # real ticket (VSA-2026-09-23-9736) whose priority contradicted
        # the safety narrative the driver was just given. It also
        # fabricated `triage_decision_id` from the session id, so the
        # STAR envelope claimed correlation with a triage decision that
        # never happened.
        #
        # The guard at the top of `call_book_endpoint` is the enforcement
        # point and must stay reachable: no triage, no booking. Per
        # ~/.kiro/steering/agentic-tiers.md § "What stays deterministic,
        # and why", a safety classification must not be probabilistic and
        # must not be silently defaulted. If the Nova voice-stall recurs,
        # that is a bug in the voice-side triage invocation path
        # (bidi_app.py's auto-triage), NOT a licence to fail open here.
        #
        # Regression-guarded by tests/test_triage_error_fail_closed.py
        # (mutation-verified: re-adding the default makes those tests fail).

        # Delegate to the standalone helper. We always use ctx.vin here
        # — the LLM-facing schema deliberately doesn't accept vin (Nova
        # was previously passing the literal string "VIN_PLACEHOLDER",
        # corrupting downstream service-history rows). The session VIN
        # is the authoritative source.
        result = call_book_endpoint(
            ctx,
            config,
            ctx.vin,
            reportedSymptom,
            customerActionsTaken,
            narrative,
        )
        # Add a `narration` field so Nova has a deterministic string to
        # speak — Nova Sonic v2 was observed going silent on complex
        # dict tool returns (the IMMEDIATE injection workaround in the
        # full bidi_app.py was the previous fix). Prompt instructs
        # Nova to speak the `narration` field verbatim.
        offered = ctx.last_offered_service_center or {}
        center_name = offered.get("name") or "the service center"
        slots = offered.get("nextAvailableSlots") or []
        slot_phrase = f" on {slots[0]}" if slots else ""
        result["narration"] = (
            f"Done — you're booked at {center_name}{slot_phrase}."
        )
        return result

    return book


class BookError(RuntimeError):
    """book tool precondition violated."""


# ---------------------------------------------------------------------------
# CMS service-history write
# ---------------------------------------------------------------------------


# Priority → scheduling offset for the new service row. The demo flow
# is P0 reactive booking, but we handle the rest defensively.
_PRIORITY_WINDOW_DAYS = {
    "P0": 0,    # same-day dispatch (roadside)
    "P1": 2,    # within 48 hours
    "P2": 7,    # within a week
    "P3": 14,   # within two weeks
}


def _fetch_vehicle_for_enrichment(vehicle_id: str) -> dict[str, Any]:
    """Best-effort read of vehicle make/model/odometer from CMS.

    Used to populate the new service-history row's make/model/
    mileageAtService fields so the iOS list renderer can treat the
    new "scheduled" row identically to the 15 pre-seeded "COMPLETED"
    rows. Returns an empty dict on any failure — enrichment is nice-
    to-have, not required for the write to succeed.
    """
    try:
        table = _get_ddb().Table(CMS_VEHICLES_TABLE)
        resp = table.get_item(Key={"vehicleId": vehicle_id})
        item = resp.get("Item") or {}
        return {
            "make": item.get("make", ""),
            "model": item.get("model", ""),
            "vin": item.get("vin", ""),
            # Existing rows use "mileageAtService"; vehicles table stores it
            # as "odometer" (and also as "mileage" — prefer odometer).
            "mileageAtService": item.get("odometer") or item.get("mileage") or 0,
        }
    except (BotoCoreError, ClientError) as e:
        log.warning(
            "book: vehicle enrichment failed for %s: %s",
            vehicle_id,
            e,
        )
        return {}


def _write_cms_service_history(
    ctx: SessionContext,
    *,
    request_number: str,
    priority: str,
    reported_symptom: str,
    narrative: str,
    vin: str,
) -> tuple[bool, str | None]:
    """Persist a "scheduled" service-history row for the booking.

    Returns (ok, service_date_iso). On failure returns (False, None) and
    logs; never raises — the voice session must not break when CMS is
    unreachable or the write fails for any reason.

    The row schema mirrors the existing seeded COMPLETED rows (vehicleId
    HASH + serviceDate RANGE, plus make/model/mileageAtService) so the
    iOS list renderer and CMS UI can display new rows identically. The
    distinguishing fields are:

      status      = "scheduled"  (vs COMPLETED for past service)
      category    = "UNSCHEDULED" (reactive triage, not preventive)
      serviceType = "VSA_VOICE_TRIAGE" (distinct from OIL_CHANGE etc.)
      source      = "voice-assistant"
      createdVia  = "vsa-supervisor-bidi"

    Plus VSA-side audit fields: requestNumber, sessionId,
    triageDecisionId, triagePriority, reportedSymptom, narrative.
    """
    if not _cms_write_enabled():
        log.info(
            "book: CMS write disabled (VSA_BOOK_WRITE_CMS!=true), "
            "skipping persistence for %s",
            request_number,
        )
        return (False, None)

    if not ctx.vehicle_id:
        log.warning(
            "book: ctx.vehicle_id not set, cannot write to CMS "
            "service-history (request_number=%s). Provide vehicle_id "
            "at session start.",
            request_number,
        )
        return (False, None)

    now = datetime.now(timezone.utc)
    offset_days = _PRIORITY_WINDOW_DAYS.get(priority, 7)
    scheduled_for = now + timedelta(days=offset_days)
    # We use the current timestamp (not the scheduled-for date) as the
    # RANGE key so the row sorts chronologically by when it was created
    # in history views, matching how past rows are keyed. The scheduled
    # date lives in its own field.
    service_date_iso = now.isoformat()

    enrichment = _fetch_vehicle_for_enrichment(ctx.vehicle_id)

    item: dict[str, Any] = {
        # --- primary key ---
        "vehicleId": ctx.vehicle_id,
        "serviceDate": service_date_iso,
        # --- status / categorization (distinguishes this row as upcoming) ---
        "status": "scheduled",
        "category": "UNSCHEDULED",
        "serviceType": "VSA_VOICE_TRIAGE",
        "description": f"Voice-triage booking: {reported_symptom[:200]}",
        "notes": narrative[:500],
        "scheduledFor": scheduled_for.isoformat(),
        # --- VSA audit trail ---
        "requestNumber": request_number,
        "sessionId": ctx.session_id,
        "triageDecisionId": ctx.triage_decision_id or "",
        "triagePriority": priority,
        "reportedSymptom": reported_symptom[:500],
        "narrative": narrative[:1000],
        "driverId": ctx.driver_id or "",
        "source": "voice-assistant",
        "createdVia": "vsa-supervisor-bidi",
        "tenantId": ctx.tenant_id,
        # --- enrichment (make/model/odometer from vehicles table) ---
        "vin": enrichment.get("vin") or vin,
        "make": enrichment.get("make", ""),
        "model": enrichment.get("model", ""),
        "mileageAtService": enrichment.get("mileageAtService", 0),
        # --- cost/labor: unknown at booking time ---
        "cost": 0,
        "laborHours": 0,
        "warrantyApplied": False,
        "warrantyCoverage": 0,
        "provider": "Pending dispatch",
        "providerType": "Pending",
        # --- serviceId: short hash for uniqueness within the composite key ---
        "serviceId": uuid.uuid4().hex[:8],
    }

    try:
        table = _get_ddb().Table(CMS_SERVICE_HISTORY_TABLE)
        table.put_item(Item=_to_ddb_safe(item))
        log.info(
            "book: wrote scheduled row to %s (vehicleId=%s, "
            "requestNumber=%s, serviceDate=%s)",
            CMS_SERVICE_HISTORY_TABLE,
            ctx.vehicle_id,
            request_number,
            service_date_iso,
        )
        return (True, service_date_iso)
    except (BotoCoreError, ClientError) as e:
        log.exception(
            "book: failed to write scheduled row to %s for %s: %s",
            CMS_SERVICE_HISTORY_TABLE,
            request_number,
            e,
        )
        return (False, None)


# ---------------------------------------------------------------------------
# Payload builder — stubbed data sources
# ---------------------------------------------------------------------------


_URGENCY_BY_PRIORITY = {
    "P0": ("stop-driving", 0, {"safeToDrive": "no", "restrictions": []}),
    "P1": (
        "service-now",
        48,
        {
            "safeToDrive": "limited",
            "restrictions": ["Avoid extended highway driving until resolved"],
        },
    ),
    "P2": ("service-soon", 168, {"safeToDrive": "yes", "restrictions": []}),
    "P3": ("monitor", 336, {"safeToDrive": "yes", "restrictions": []}),
}


def _build_payload(
    *,
    request_number: str,
    vin: str,
    priority: str,
    reported_symptom: str,
    actions_taken: list[str],
    narrative: str,
    tenant_id: str,
    offered_service_center: dict | None = None,
) -> dict[str, Any]:
    """Assemble the ServiceRequestAdd payload.

    Everything not LLM-provided is stubbed from the Cascadia fixtures.
    When this runs against CMS/ADP, the stubbed blocks become real
    lookups.

    `offered_service_center` is the find_service_center result (the
    closest center we offered to the driver). When present, it
    overrides the fixture default so the booking payload reflects the
    center that was actually offered/booked. Without this override the
    payload always shows the fixture (Rush Truck Center — Dallas
    North) regardless of where the driver was actually booked, which
    confuses both downstream systems and any audit trail that reads
    serviceCenter.preferred.
    """
    classification, window_hours, safety = _URGENCY_BY_PRIORITY.get(
        priority, _URGENCY_BY_PRIORITY["P2"]
    )

    if offered_service_center:
        # Map find_service_center result fields → STAR serviceCenter schema.
        # find_service_center returns: centerId, name, address,
        # distanceMiles, phone, type (fleet-service|dealer|...).
        # STAR expects: id, name, address, distanceMiles, contact,
        # networkTier (preferred|authorized|...).
        c = offered_service_center
        # Best-effort networkTier mapping. Anything we don't recognize
        # falls back to "authorized" so we don't fabricate "preferred"
        # status.
        type_to_tier = {
            "fleet-service": "preferred",
            "dealer": "authorized",
            "independent": "independent",
        }
        # distanceMiles can come back as a string (e.g. "8.1") from the
        # tool's JSON. Coerce to float for the STAR payload.
        try:
            distance = float(c.get("distanceMiles")) if c.get("distanceMiles") is not None else None
        except (TypeError, ValueError):
            distance = None
        service_center = {
            "preferred": {
                "id": c.get("centerId") or c.get("id"),
                "name": c.get("name"),
                "address": c.get("address"),
                "distanceMiles": distance,
                "contact": c.get("phone") or c.get("contact"),
                "networkTier": type_to_tier.get(c.get("type"), "authorized"),
            },
            # Alternatives are not tracked on the offered center; leave
            # empty rather than synthesizing them.
            "alternatives": [],
        }
    else:
        # Stubbed service center for Cascadia/Dallas. Real impl queries
        # tenant-scoped service center registry. Used as a last-resort
        # fallback when no find_service_center call preceded book().
        service_center = {
            "preferred": {
                "id": "SVC-DAL-014",
                "name": "Rush Truck Center — Dallas North",
                "address": "2101 Main St, Dallas, TX 75208",
                "distanceMiles": 18,
                "contact": "+12145550100",
                "networkTier": "preferred",
            },
            "alternatives": [
                {
                    "id": "SVC-DAL-022",
                    "name": "Authorized Diesel Service",
                    "distanceMiles": 31,
                    "networkTier": "authorized",
                }
            ],
        }

    return {
        "requestNumber": request_number,
        "priority": priority,
        "urgency": {
            "classification": classification,
            "recommendedWindowHours": window_hours,
            "safetyAssessment": safety,
        },
        "customer": {
            "customerId": None,
            "tenantId": tenant_id,
            "name": None,
            "contact": {
                "phone": None,
                "email": None,
                "preferredChannel": "voice",
                "locale": "en-US",
            },
        },
        "vehicle": {
            "vin": vin,
            "vehicleId": None,
            "make": None,
            "model": None,
            "modelYear": None,
            "odometerMiles": None,
            "fleetContext": None,
        },
        "serviceCenter": service_center,
        "symptoms": {
            "reported": reported_symptom,
            "observedByVsa": [],
            "relatedRecalls": [],
        },
        "workScope": {
            "primaryComplaint": reported_symptom,
            "recommendedInspections": [],
            "partsPlanningHint": [],
            "estimatedLaborHours": None,
        },
        "warranty": {
            "preCheckStatus": "pending",
            "coverageType": None,
            "expiresAt": None,
            "deductible": None,
            "claimPreCheckMessageId": None,
            "notes": None,
        },
        "costEstimate": {
            "estimatedTotalUsd": None,
            "basis": "Pending specialist agent resolution",
            "confidence": "low",
        },
        "actions": {
            "recommended": _recommended_action(priority),
            "offeredToCustomer": sorted(
                set(actions_taken) | {"create-ticket"}
            ),
            "takenByCustomer": actions_taken,
        },
        "narrative": narrative,
    }


def _recommended_action(priority: str) -> str:
    if priority == "P0":
        return "dispatch-roadside"
    if priority == "P1":
        return "schedule-at-preferred"
    if priority == "P2":
        return "schedule-at-preferred"
    return "monitor"


def _fake_signature(message_id: str) -> dict[str, str]:
    """Placeholder signature so the envelope shape is complete.

    Real signing uses an HMAC key in Secrets Manager. For local testing
    we just hash the message id — it's not verified anywhere.
    """
    digest = hashlib.sha256(message_id.encode()).hexdigest()
    return {
        "algo": "HMAC-SHA256",
        "keyId": "vsa-local-stub",
        "value": digest,
    }
