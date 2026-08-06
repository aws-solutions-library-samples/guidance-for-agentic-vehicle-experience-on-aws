"""VSA triage classifier Lambda handler.

Fetches the tenant config, vehicle telemetry, DTCs, and recalls; calls the
deterministic classifier; persists the decision. Pure I/O at the edges; the
classifier remains a pure function.

Expected event shape:
  {
    "sessionId": "vsa-sess-...",
    "tenantId":  "michelin",
    "vin":       "1FMCU0F70MUA12345",
    "extra":     {"fleet_sla": "tier-1"}        # optional; fed to tenant rules
  }
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict
from decimal import Decimal
from typing import Any

import boto3

from shared import cms_client, tenant_config
from triage_classifier.classifier import classify
from triage_classifier.types import TriageContext

_ddb = None


def _decisions_table():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource(
            "dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1")
        ).Table(os.environ["VSA_TRIAGE_DECISIONS_TABLE"])
    return _ddb


def _decision_to_item(d: Any) -> dict[str, Any]:
    # Dataclass → JSON → Decimal-typed dict so DynamoDB accepts floats.
    raw = json.loads(json.dumps(asdict(d), default=str), parse_float=Decimal)
    # Table partition key is `sessionId` (camelCase); dataclass uses snake_case.
    # Rename top-level keys for DDB; nested keys are opaque blobs, leave alone.
    renamed = {
        "sessionId": raw.pop("session_id"),
        "tenantId": raw.pop("tenant_id"),
        "vin": raw.pop("vin"),
        "classification": raw.pop("classification"),
        "reason": raw.pop("reason"),
        "forcedActions": raw.pop("forced_actions"),
        "classifierVersion": raw.pop("classifier_version"),
        "decidedAt": raw.pop("decided_at"),
        "inputsSnapshot": raw.pop("inputs_snapshot"),
    }
    return renamed


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    tenant_id = event["tenantId"]
    vin = event["vin"]
    session_id = event.get("sessionId") or f"vsa-sess-{uuid.uuid4().hex[:12]}"
    extra = event.get("extra") or {}

    started = time.perf_counter()

    raw_tenant = tenant_config.load_raw(tenant_id)
    tenant_triage = tenant_config.build_triage_config(raw_tenant)

    cms = cms_client.get_client()
    telemetry = cms.get_telemetry(vin)
    dtcs = cms.get_active_dtcs(vin)
    recalls = cms.get_open_recalls(vin)

    ctx = TriageContext(
        session_id=session_id,
        tenant_id=tenant_id,
        tenant_segment=raw_tenant.get("segment", "fleet"),
        vin=vin,
        active_dtcs=dtcs,
        telemetry=telemetry,
        open_recalls=recalls,
        predictive_signals=(),
        tenant_triage=tenant_triage,
        extra=extra,
    )

    decision = classify(ctx)

    # Persist with classifier latency so we can track P99 budget over time.
    latency_ms = int((time.perf_counter() - started) * 1000)
    item = _decision_to_item(decision)
    item["latencyMs"] = latency_ms
    item["ttl"] = int(time.time()) + 30 * 24 * 3600
    _decisions_table().put_item(Item=item)

    return {
        "statusCode": 200,
        "classification": decision.classification.name,
        "sessionId": decision.session_id,
        "decidedAt": decision.decided_at,
        "latencyMs": latency_ms,
        "activeDtcs": decision.inputs_snapshot.get("activeDtcs", []),
        "topReason": str(decision.reason[0]) if decision.reason else "",
    }
