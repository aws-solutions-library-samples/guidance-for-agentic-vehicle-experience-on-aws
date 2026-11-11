"""WebSocket: $connect / $disconnect

Writes/deletes rows in a connection tracking table. No message producer yet —
iOS will connect to this endpoint and receive no server-side pushes until the
mock-telemetry or real CMS producer is added.
"""
from __future__ import annotations

import os
import time
from typing import Any

import boto3

_ddb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))
_table = _ddb.Table(os.environ["WS_CONNECTIONS_TABLE"])


def _route(event: dict[str, Any]) -> str:
    return (event.get("requestContext") or {}).get("routeKey") or ""


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    rc = event.get("requestContext") or {}
    connection_id = rc.get("connectionId")
    route = _route(event)

    if route == "$connect":
        # tenantId is surfaced by the JWT authorizer into request context.
        authorizer = rc.get("authorizer") or {}
        tenant_id = authorizer.get("tenantId")
        if not tenant_id or not connection_id:
            return {"statusCode": 401}
        _table.put_item(
            Item={
                "connectionId": connection_id,
                "tenantId": tenant_id,
                "connectedAt": int(time.time()),
                "ttl": int(time.time()) + 4 * 3600,
            }
        )
        return {"statusCode": 200}

    if route == "$disconnect":
        if connection_id:
            _table.delete_item(Key={"connectionId": connection_id})
        return {"statusCode": 200}

    # $default — client sent a message. Not supported yet; acknowledge only.
    return {"statusCode": 200, "body": '{"ack":true}'}
