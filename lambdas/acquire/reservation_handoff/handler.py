"""POST /acquire/reservations

Writes a full order record to `vsa-acquire-orders` and bootstraps
the time-simulation pipeline.  Idempotent via DDB ConditionExpression.

The returned STAR envelope uses appointment_type="handover" per
spec.md § Risks R4.

Request (JSON) — full order payload matching acquire-catalog-schema.md § 3:
    {
      "tenantId":       string   required
      "modelId":        string   required  (via configuration)
      "configuration": {
        "categoryId":    string   optional — derived from model row when absent (decisions.md Q2)
        "modelId":       string,
        "variantId":     string,
        "colorId":       string,
        "accessoryIds":  [string]  — also accepted as selectedAccessoryIds (decisions.md Q2)
        "interiorId":    string   optional
        "ivePackage":    string   optional
      }
      "dealerRef":      string   optional — falls back to tenant config when absent
      "ownerContact": {
        "email":         string,
        "displayName":   string
      }
      "financing": {              optional — from finance_qualify response
        "tier": ..., "approvedAmount": ..., "apr": ...,
        "monthly": ..., "disclosure": ...
      }
      "payment": {                optional — from payment_initiate response
        "transactionRef": ..., "status": ..., "capturedAt": ...
      }
      "discoverLeadId": string   optional — lead identifier from the Discover
                                 phase (spec 2026-08-04-cvx-oem-discover-order-meridian,
                                 Group 2 Task 6).  When present, stamped onto
                                 the order item as `discoverLeadId` for
                                 Discover→Order audit trail.  Omitting this
                                 field leaves existing behaviour unchanged.
    }

    NOTE: `ownerSub` is derived server-side from the Cognito authorizer claim
    (decisions.md Q8 — the kiosk device's own Cognito subject, NOT from the
    request body).  A client-supplied ownerSub field is ignored.

Response:
    {
      "orderId":         "ORD-YYYYMMDD-<6hex>",
      "orderNumber":     "ORD-YYYYMMDD-<6hex>",   # human-readable alias (decisions.md Q3)
      "stage":           "orderPlaced",
      "nextTransitionAt": ISO-8601,
      "correlation": {
        "starEnvelope": {
          "appointment_type": "handover",
          "orderId": "...",
          "tenantId": "...",
          "stage": "orderPlaced"
        }
      }
    }

Auth: Cognito authorizer.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

_ORDERS_TABLE = os.environ.get("VSA_ACQUIRE_ORDERS_TABLE", "vsa-dev-acquire-orders")
_CATALOG_TABLE = os.environ.get("VSA_ACQUIRE_CATALOG_TABLE", "vsa-dev-acquire-catalog")
_EVENT_BUS_ARN = os.environ.get("VSA_ACQUIRE_EVENT_BUS_ARN", "")
_PUBLISH_ORDER_PLACED = os.environ.get("VSA_ACQUIRE_PUBLISH_ORDER_PLACED", "false").lower() == "true"
_SFN_STATE_MACHINE_ARN = os.environ.get("VSA_ACQUIRE_PROGRESSION_SFN_ARN", "")

# Default dwell (hours) when tenant config is missing.
_DEFAULT_DWELL_MIN_HOURS = 0.0
_DEFAULT_DWELL_MAX_HOURS = 1.0

_ddb = None
_events = None
_sfn = None


def _get_ddb():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb")
    return _ddb


def _get_events():
    global _events
    if _events is None:
        _events = boto3.client("events")
    return _events


def _get_sfn():
    global _sfn
    if _sfn is None:
        _sfn = boto3.client("stepfunctions")
    return _sfn


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return int(o) if o % 1 == 0 else float(o)
        return super().default(o)


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, cls=_DecimalEncoder),
    }


def _to_ddb_safe(value: Any) -> Any:
    """Convert floats to Decimal so DynamoDB accepts them."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_ddb_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_ddb_safe(v) for v in value]
    return value


def _make_order_id(tenant_id: str, owner_sub: str, placed_at: datetime) -> str:
    """Deterministic orderId from (tenantId, ownerSub, second-truncated timestamp).

    Format: ORD-YYYYMMDD-<6-hex>

    Two reservations from the same customer within the same second for
    the same tenant return the same orderId, which is intentional —
    the PutItem ConditionExpression will reject the duplicate.

    Per decisions.md Q8: ownerSub is the kiosk DEVICE's Cognito subject
    (not a shared sentinel), so determinism is preserved within a device
    while keeping order ids non-computable by outsiders who don't hold
    the device sub.
    """
    date_str = placed_at.strftime("%Y%m%d")
    seed = f"{tenant_id}|{owner_sub}|{placed_at.strftime('%Y%m%dT%H%M%S')}"
    hex_suffix = hashlib.sha256(seed.encode()).hexdigest()[:6]
    return f"ORD-{date_str}-{hex_suffix}"


def _get_tenant_config(tenant_id: str) -> dict:
    """Fetch META#tenantConfig from the catalog table.

    Returns the config item dict, or {} on any error.  Callers fall
    back gracefully on an empty return value.
    """
    try:
        resp = _get_ddb().Table(_CATALOG_TABLE).get_item(
            Key={"tenantId": tenant_id, "itemKey": "META#tenantConfig"}
        )
        return resp.get("Item") or {}
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "reservation_handoff: failed to fetch tenantConfig for %s: %s — "
            "using fallback values",
            tenant_id, exc,
        )
        return {}


def _get_model_item(tenant_id: str, model_id: str) -> dict:
    """Look up a model row from the catalog table.

    Model items have itemKey = "MODEL#<categoryId>#<modelId>".
    We query the catalog table for items where:
      - PK = tenantId
      - SK begins_with "MODEL#" and ends with "#<modelId>"
    Since DDB doesn't support ends_with, we query begins_with "MODEL#" and
    filter.  For catalogs with O(10) model items this is cost-effective.

    Returns the first matching item dict, or {} if not found / error.
    """
    if not model_id:
        return {}
    try:
        # Query all MODEL# items for this tenant, then filter by modelId suffix.
        # In production catalogs, the expected count is O(10) items.
        table = _get_ddb().Table(_CATALOG_TABLE)
        resp = table.query(
            KeyConditionExpression=(
                Key("tenantId").eq(tenant_id)
                & Key("itemKey").begins_with("MODEL#")
            ),
        )
        for item in resp.get("Items") or []:
            # itemKey format: "MODEL#<categoryId>#<modelId>"
            parts = (item.get("itemKey") or "").split("#")
            if len(parts) == 3 and parts[2] == model_id:
                return item
        return {}
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "reservation_handoff: model lookup failed for tenant=%s modelId=%s: %s",
            tenant_id, model_id, exc,
        )
        return {}


def _derive_category_id(model_item: dict) -> str | None:
    """Derive categoryId from a model catalog row.

    The model's itemKey is "MODEL#<categoryId>#<modelId>".  We also look
    at categoryKey ("CAT#<categoryId>") as a fallback.
    """
    item_key = model_item.get("itemKey") or ""
    parts = item_key.split("#")
    if len(parts) == 3:
        return parts[1]  # "MODEL#<categoryId>#<modelId>" → categoryId at index 1

    category_key = model_item.get("categoryKey") or ""
    if category_key.startswith("CAT#"):
        return category_key[4:]

    return None


def _compute_first_next_transition(tenant_config: dict, now: datetime) -> str:
    """Compute the first nextTransitionAt from the tenant's deliveryCadence.

    Reads the "orderPlacedToPayment" band from deliveryCadence.  Falls back
    to a random dwell in [_DEFAULT_DWELL_MIN_HOURS, _DEFAULT_DWELL_MAX_HOURS]
    when the config is absent — today's demo-safe behaviour.

    Closes the TODO that existed in the original handler.
    """
    cadence = tenant_config.get("deliveryCadence") or {}
    band = cadence.get("orderPlacedToPayment") or {}
    if band:
        min_h = float(band.get("minHours", _DEFAULT_DWELL_MIN_HOURS))
        max_h = float(band.get("maxHours", _DEFAULT_DWELL_MAX_HOURS))
    else:
        min_h = _DEFAULT_DWELL_MIN_HOURS
        max_h = _DEFAULT_DWELL_MAX_HOURS

    dwell = random.uniform(min_h, max_h)
    return (now + timedelta(hours=dwell)).isoformat()


# Keep the private helper name for backward-compatibility with tests that
# patch or call it directly.
def _sample_next_transition(min_hours: float, max_hours: float, now: datetime) -> str:
    """Sample a uniform dwell in [min_hours, max_hours] and return an ISO-8601 timestamp."""
    dwell = random.uniform(min_hours, max_hours)
    transition_time = now + timedelta(hours=dwell)
    return transition_time.isoformat()


def _publish_order_placed(order_id: str, tenant_id: str, event_bus_arn: str) -> None:
    """Publish order.placed to EventBridge.  Fire-and-forget — never raises.

    Gated by VSA_ACQUIRE_PUBLISH_ORDER_PLACED env var.  A publish failure
    logs a warning but does NOT fail the reservation request.
    """
    try:
        _get_events().put_events(
            Entries=[
                {
                    "EventBusName": event_bus_arn,
                    "Source": "vsa.acquire.reservation_handoff",
                    "DetailType": "order.placed",
                    "Detail": json.dumps({"orderId": order_id, "tenantId": tenant_id}),
                }
            ]
        )
        log.info(
            "reservation_handoff: published order.placed for orderId=%s", order_id
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "reservation_handoff: failed to publish order.placed for orderId=%s: %s "
            "(continuing — fire-and-forget)",
            order_id, exc,
        )


def _start_progression_execution(order_id: str, sfn_arn: str) -> None:
    """Start a Step Functions execution for this order.  Never raises.

    The execution is started AFTER a successful DDB write, so the progression
    machine always advances an order that is already in the store.
    """
    if not sfn_arn:
        log.debug(
            "reservation_handoff: VSA_ACQUIRE_PROGRESSION_SFN_ARN not set — "
            "skipping Step Functions start for orderId=%s",
            order_id,
        )
        return
    try:
        _get_sfn().start_execution(
            stateMachineArn=sfn_arn,
            name=order_id,  # Use orderId as execution name — deterministic, idempotent-ish
            input=json.dumps({"orderId": order_id}),
        )
        log.info(
            "reservation_handoff: started SFN execution for orderId=%s", order_id
        )
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code == "ExecutionAlreadyExists":
            log.info(
                "reservation_handoff: SFN execution already exists for orderId=%s — ok",
                order_id,
            )
        else:
            log.warning(
                "reservation_handoff: failed to start SFN execution for orderId=%s: %s",
                order_id, exc,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "reservation_handoff: unexpected error starting SFN for orderId=%s: %s",
            order_id, exc,
        )


def handler(event: dict, _context) -> dict:
    """Lambda entry point."""
    # --- 1. Extract claims from Cognito authorizer (Q8) ---
    claims: dict = (
        (event.get("requestContext") or {})
        .get("authorizer", {})
        .get("claims") or {}
    )

    # ownerSub comes from the JWT claim, NOT from the request body (decisions.md Q8).
    # This is the kiosk device's own Cognito subject — one distinct principal per tablet.
    owner_sub: str = claims.get("sub", "").strip()
    if not owner_sub:
        return _resp(401, {"error": "Missing authentication claim: sub"})

    # --- 2. Parse body ---
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _resp(400, {"error": "Body must be JSON"})

    # tenantId is still required in the body (the claim does not carry it for
    # non-assistant routes; cross-check below against body value if both present).
    if not body.get("tenantId"):
        return _resp(400, {"error": "Missing required fields: ['tenantId']"})

    tenant_id: str = body["tenantId"]

    # --- 3. Resolve configuration (Q2) ---
    configuration = dict(body.get("configuration") or {})

    # Accept selectedAccessoryIds as an alias for accessoryIds (decisions.md Q2).
    if "selectedAccessoryIds" in configuration and "accessoryIds" not in configuration:
        configuration["accessoryIds"] = configuration.pop("selectedAccessoryIds")
    elif "selectedAccessoryIds" in configuration:
        # Both present — drop the alias to avoid duplication.
        configuration.pop("selectedAccessoryIds")

    # Required configuration fields (categoryId is now optional — Q2).
    required_config = ["modelId", "variantId", "colorId"]
    missing_config = [k for k in required_config if not configuration.get(k)]
    if missing_config:
        return _resp(400, {"error": f"Missing configuration fields: {missing_config}"})

    model_id: str = configuration["modelId"]

    # --- 4. Fetch tenant config (needed for cadence + dealerRef fallback) ---
    tenant_config = _get_tenant_config(tenant_id)

    # --- 5. Derive categoryId when absent (Q2) ---
    if not configuration.get("categoryId"):
        model_item = _get_model_item(tenant_id, model_id)
        derived_category_id = _derive_category_id(model_item)
        if derived_category_id:
            configuration["categoryId"] = derived_category_id
        # If still absent after lookup, store without it — not a hard error.
        # The catalog table may not yet have the model item for new tenants.

    # --- 6. Resolve dealerRef (falls back to tenant config when absent) ---
    dealer_ref: str = (
        body.get("dealerRef")
        or tenant_config.get("dealerRef")
        or ""
    )

    # --- 7. Build order ---
    now = datetime.now(timezone.utc)
    placed_at = now
    order_id = _make_order_id(tenant_id, owner_sub, placed_at)
    placed_at_iso = placed_at.isoformat()

    tenant_owner_key = f"{tenant_id}#{owner_sub}"

    # Compute first nextTransitionAt from deliveryCadence (closes TODO).
    next_transition_at = _compute_first_next_transition(tenant_config, now)

    stage_history = [{"stage": "orderPlaced", "enteredAt": placed_at_iso}]

    owner_contact = body.get("ownerContact") or {}
    financing = body.get("financing") or {}
    payment = body.get("payment") or {}

    item: dict[str, Any] = {
        "orderId": order_id,
        "tenantId": tenant_id,
        "tenantOwnerKey": tenant_owner_key,
        "ownerSub": owner_sub,
        "ownerContact": owner_contact,
        "configuration": configuration,
        "dealerRef": dealer_ref,
        "financing": financing,
        "payment": payment,
        "orderPlacedAt": placed_at_iso,
        "stage": "orderPlaced",
        "stageHistory": stage_history,
        "nextTransitionAt": next_transition_at,
        "correlation": {
            "acquireOrderKey": {
                "orderId": order_id,
                "tenantId": tenant_id,
            }
        },
    }

    # Additive Discover→Order back-reference (spec 2026-08-04-cvx-oem-discover-order-meridian,
    # Group 2 Task 6).
    discover_lead_id = body.get("discoverLeadId")
    if discover_lead_id:
        item["discoverLeadId"] = discover_lead_id

    # STAR envelope per spec.md § Risks R4 — appointment_type="handover"
    star_envelope = {
        "appointment_type": "handover",
        "orderId": order_id,
        "tenantId": tenant_id,
        "stage": "orderPlaced",
    }

    # --- 8. Persist ---
    try:
        _get_ddb().Table(_ORDERS_TABLE).put_item(
            Item=_to_ddb_safe(item),
            ConditionExpression="attribute_not_exists(orderId)",
        )
        log.info(
            "reservation_handoff: wrote orderId=%s tenantId=%s ownerSub=%s",
            order_id, tenant_id, owner_sub,
        )
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code == "ConditionalCheckFailedException":
            # Idempotent: the order already exists (duplicate submission).
            # Return success — the caller gets the same orderId back.
            log.info(
                "reservation_handoff: duplicate submission for orderId=%s — "
                "returning existing record",
                order_id,
            )
        else:
            log.exception(
                "reservation_handoff: DDB PutItem failed for orderId=%s: %s",
                order_id, exc,
            )
            return _resp(500, {"error": "Failed to persist reservation"})
    except (BotoCoreError, Exception) as exc:  # noqa: BLE001
        log.exception(
            "reservation_handoff: unexpected error for orderId=%s: %s",
            order_id, exc,
        )
        return _resp(500, {"error": "Internal server error"})

    # --- 9. Start Step Functions execution (after successful write) ---
    _start_progression_execution(order_id, _SFN_STATE_MACHINE_ARN)

    # --- 10. Publish order.placed to EventBridge (fire-and-forget) ---
    if _PUBLISH_ORDER_PLACED and _EVENT_BUS_ARN:
        _publish_order_placed(order_id, tenant_id, _EVENT_BUS_ARN)

    # orderNumber is a human-readable alias for orderId (decisions.md Q3).
    # Swift declares ReservationResponse.orderNumber as non-optional, so its
    # absence makes every reservation fail to decode client-side.
    return _resp(201, {
        "orderId": order_id,
        "orderNumber": order_id,
        "stage": "orderPlaced",
        "nextTransitionAt": next_transition_at,
        "correlation": {
            "starEnvelope": star_envelope,
        },
    })
