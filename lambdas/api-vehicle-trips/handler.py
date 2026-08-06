"""
GET /vehicles/{vehicleId}/trips

Returns a list of recent trips for the vehicle, newest first, via the
cms-prod-storage-trips GSI vehicleId-startTime-index. Route array is
stripped to keep the response small — iOS only needs the summary.

Query string:
    limit — max trips to return (default 10, max 50)

Response:
    {
      "vehicleId": "VEH-0025",
      "trips": [TripSummary, ...],
      "generatedAt": "..."
    }
"""
from __future__ import annotations

import json
import logging
import os
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

log = logging.getLogger()
log.setLevel(logging.INFO)

_TRIPS_TABLE = os.environ.get("CMS_TRIPS_TABLE", f"cms-{os.environ.get('CMS_STAGE', 'dev')}-storage-trips")
_GSI = os.environ.get("CMS_TRIPS_GSI", "vehicleId-startTime-index")
_DEFAULT_LIMIT = int(os.environ.get("DEFAULT_LIMIT", "10"))
_MAX_LIMIT = int(os.environ.get("MAX_LIMIT", "50"))

# Route arrays can have 100+ GPS points each; ~10KB+ per trip. iOS renders
# summaries only today, so strip the route from each row. Opt-in via ?full=1
# query param for debugging.
_SUMMARY_STRIP_FIELDS = ("route",)

_ddb = None


def _get_ddb():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb")
    return _ddb


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return int(o) if o % 1 == 0 else float(o)
        return super().default(o)


def _respond(status: int, body: dict | str) -> dict[str, Any]:
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


def _parse_int(qs: dict, key: str, default: int, ceiling: int) -> int:
    raw = (qs or {}).get(key)
    if raw is None:
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(ceiling, v))


def handler(event, context):
    path_params = event.get("pathParameters") or {}
    vehicle_id = path_params.get("vehicleId")
    if not vehicle_id:
        return _respond(400, {"error": "missing vehicleId path parameter"})

    qs = event.get("queryStringParameters") or {}
    limit = _parse_int(qs, "limit", _DEFAULT_LIMIT, _MAX_LIMIT)
    include_full = (qs.get("full") == "1")

    table = _get_ddb().Table(_TRIPS_TABLE)
    try:
        resp = table.query(
            IndexName=_GSI,
            KeyConditionExpression=Key("vehicleId").eq(vehicle_id),
            ScanIndexForward=False,  # newest first
            Limit=limit,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("trips query failed for vehicleId=%s: %s", vehicle_id, e)
        return _respond(500, {"error": f"trips query failed: {e}"})

    items = resp.get("Items") or []
    if not include_full:
        for item in items:
            for field in _SUMMARY_STRIP_FIELDS:
                item.pop(field, None)

    from datetime import datetime, timezone
    generated = datetime.now(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")

    return _respond(200, {
        "vehicleId": vehicle_id,
        "trips": items,
        "generatedAt": generated,
    })
