"""
GET /vehicles/{vehicleId}/service-history

Returns scheduled + completed service records for a vehicle, read directly
from the CMS-side cms-prod-storage-service-history table.

Response shape:
    {
      "vehicleId": "VEH-0025",
      "scheduled": [ServiceRecord, ...],   // status = "scheduled"
      "completed": [ServiceRecord, ...],   // status = "COMPLETED"
      "generatedAt": "2026-04-30T15:30:00.000Z"
    }

Each ServiceRecord is a pass-through of the CMS row with Decimal→float
conversion for JSON. We don't reshape to a smaller DTO so that new fields
added server-side appear on iOS without a redeploy.

Environment:
    CMS_SERVICE_HISTORY_TABLE — defaults to "cms-prod-storage-service-history"
    SCHEDULED_LIMIT — max scheduled rows returned (default 10)
    COMPLETED_LIMIT — max completed rows returned (default 20)
"""
from __future__ import annotations

import json
import logging
import os
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr, Key

log = logging.getLogger()
log.setLevel(logging.INFO)

_TABLE_NAME = os.environ.get("CMS_SERVICE_HISTORY_TABLE", f"cms-{os.environ.get('CMS_STAGE', 'dev')}-storage-service-history")
_SCHEDULED_LIMIT = int(os.environ.get("SCHEDULED_LIMIT", "10"))
_COMPLETED_LIMIT = int(os.environ.get("COMPLETED_LIMIT", "20"))

_ddb = None


def _get_table():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb")
    return _ddb.Table(_TABLE_NAME)


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            # Integers stay integers; floats become floats. DDB's Decimal(1)
            # and Decimal("0.5") both round-trip correctly.
            if o % 1 == 0:
                return int(o)
            return float(o)
        return super().default(o)


def _respond(status: int, body: dict | list | str) -> dict[str, Any]:
    """Build an API Gateway proxy response."""
    payload = body if isinstance(body, str) else json.dumps(body, cls=_DecimalEncoder)
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
        },
        "body": payload,
    }


def handler(event, context):
    path_params = event.get("pathParameters") or {}
    vehicle_id = path_params.get("vehicleId")
    if not vehicle_id:
        return _respond(400, {"error": "missing vehicleId path parameter"})

    method = (event.get("httpMethod") or "GET").upper()

    # DELETE — purges VSA-created service rows for this vehicle. Used by
    # the iOS Reset Demo button so the next demo run starts with a
    # clean Service tab. Filters strictly to source=voice-assistant
    # so seeded historical rows (oil changes, recalls, etc.) survive.
    # Deletes BOTH "scheduled" and "COMPLETED" VSA rows because the
    # demo flow can produce either depending on timing — a cleanup
    # tool that only nukes "scheduled" would still leave a trail of
    # past demo bookings on the Completed list.
    if method == "DELETE":
        return _delete_vsa_records(vehicle_id)

    return _get_service_history(event, vehicle_id)


def _delete_vsa_records(vehicle_id: str) -> dict[str, Any]:
    """Delete all rows where vehicleId=<id> AND source='voice-assistant'.

    Rows in this table are keyed (vehicleId HASH, serviceDate RANGE).
    DynamoDB lacks a native query-and-delete, so we query for the keys
    matching the filter, then issue per-row delete_item calls.
    """
    table = _get_table()
    deleted = 0
    failed = 0
    last_evaluated_key: dict[str, Any] | None = None
    try:
        # Loop pagination defensively in case more than ~1MB of items
        # match the filter. Each demo cycle adds 1-2 rows so 47 stale
        # rows fit in a single page today, but the loop costs nothing.
        while True:
            kwargs: dict[str, Any] = {
                "KeyConditionExpression": Key("vehicleId").eq(vehicle_id),
                "FilterExpression": Attr("source").eq("voice-assistant"),
                "ProjectionExpression": "vehicleId, serviceDate",
            }
            if last_evaluated_key:
                kwargs["ExclusiveStartKey"] = last_evaluated_key
            resp = table.query(**kwargs)
            for row in resp.get("Items") or []:
                try:
                    table.delete_item(
                        Key={
                            "vehicleId": row["vehicleId"],
                            "serviceDate": row["serviceDate"],
                        },
                    )
                    deleted += 1
                except Exception as e:  # noqa: BLE001
                    failed += 1
                    log.warning(
                        "delete_item failed for vehicleId=%s serviceDate=%s: %s",
                        row.get("vehicleId"), row.get("serviceDate"), e,
                    )
            last_evaluated_key = resp.get("LastEvaluatedKey")
            if not last_evaluated_key:
                break
    except Exception as e:  # noqa: BLE001
        log.exception("VSA delete failed for vehicleId=%s: %s", vehicle_id, e)
        return _respond(500, {"error": f"delete failed: {e}", "deleted": deleted})

    log.info(
        "vsa-cleanup: vehicleId=%s deleted=%d failed=%d",
        vehicle_id, deleted, failed,
    )
    return _respond(200, {
        "vehicleId": vehicle_id,
        "deleted": deleted,
        "failed": failed,
    })


def _get_service_history(event: dict, vehicle_id: str) -> dict[str, Any]:

    # Optional query-string filter to return only one bucket.
    qs = event.get("queryStringParameters") or {}
    wanted_status = (qs.get("status") or "").lower() or None  # "scheduled" | "completed" | None

    table = _get_table()
    scheduled: list[dict] = []
    completed: list[dict] = []

    try:
        # Scheduled: rows with status = "scheduled" (lowercase — matches book.py write).
        if wanted_status in (None, "scheduled"):
            resp = table.query(
                KeyConditionExpression=Key("vehicleId").eq(vehicle_id),
                FilterExpression=Attr("status").eq("scheduled"),
                ScanIndexForward=False,  # newest first
                Limit=_SCHEDULED_LIMIT * 2,  # over-fetch because FilterExpression applies post-scan
            )
            scheduled = (resp.get("Items") or [])[:_SCHEDULED_LIMIT]

        # Completed: the seeded historical rows use status = "COMPLETED".
        if wanted_status in (None, "completed"):
            resp = table.query(
                KeyConditionExpression=Key("vehicleId").eq(vehicle_id),
                FilterExpression=Attr("status").eq("COMPLETED"),
                ScanIndexForward=False,
                Limit=_COMPLETED_LIMIT * 2,
            )
            completed = (resp.get("Items") or [])[:_COMPLETED_LIMIT]

    except Exception as e:  # noqa: BLE001
        log.exception("service-history query failed for vehicleId=%s: %s", vehicle_id, e)
        return _respond(500, {"error": f"service-history query failed: {e}"})

    from datetime import datetime, timezone
    generated = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    return _respond(200, {
        "vehicleId": vehicle_id,
        "scheduled": scheduled,
        "completed": completed,
        "generatedAt": generated,
    })
