"""VSA triage classifier Lambda handler.

Fetches the tenant config, vehicle telemetry, DTCs, and recalls; calls the
deterministic classifier; persists the decision. Pure I/O at the edges; the
classifier remains a pure function.

Expected event shape:
  {
    "sessionId": "vsa-sess-...",
    "tenantId":  "vehicle-owner",
    "vin":       "1FMCU0F70MUA12345",
    "extra":     {"fleet_sla": "tier-1"}        # optional; fed to tenant rules
  }
"""
from __future__ import annotations

import json
import os
import sys
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


#: Read outcomes that mean a DTC read which should have worked did not. All
#: outcomes are stored; these also emit a metric. `vehicle_unresolved` (the VIN
#: is not in CMS) is deliberately absent: it is a data fact, not a failure.
_DTC_READ_DEGRADED = frozenset({"partial", "failed", "vehicle_lookup_failed"})


def _dtc_read_outcome(cms: Any) -> dict[str, Any]:
    """The client's report on its last ``get_active_dtcs`` call, JSON-safe.

    ``RealCmsClient`` sets ``last_dtc_read``; the fake client does not, which is
    recorded as ``unreported`` rather than guessed as complete.
    """
    outcome = getattr(cms, "last_dtc_read", None)
    if not isinstance(outcome, dict) or not outcome.get("status"):
        return {"status": "unreported"}
    return {k: outcome[k] for k in ("status", "pages", "rows") if k in outcome}


def _emit_dtc_read_degraded(status: str) -> None:
    """One EMF line counting a degraded DTC read, by status.

    Nothing alarms on this yet; the metric is the signal. Never raises: a
    metric failure must not fail triage.
    """
    try:
        sys.stdout.write(json.dumps({
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [{
                    "Namespace": "vsa/triage-classifier",
                    "Dimensions": [["dtc_read_status"]],
                    "Metrics": [{"Name": "dtc_read_degraded", "Unit": "Count"}],
                }],
            },
            "dtc_read_status": status,
            "dtc_read_degraded": 1,
        }) + "\n")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    # Warmup ping — keeps an execution environment hot and returns before doing
    # any real work.
    #
    # Why this exists: cold invocations measured 2510-2600 ms (init 433-477 ms plus
    # ~2100 ms of first-call boto3/DynamoDB setup) against Amazon Nova Sonic's ~1 s
    # bidi tool budget. Warm, the same function returns in 2-143 ms. So the first
    # DTC question after any idle period made the voice assistant go silent, which
    # presents to a user as a broken app rather than a slow one, and was
    # misdiagnosed twice as a prompt problem before the numbers were measured.
    #
    # Deliberately BEFORE reading tenantId/vin: a warm ping must not need to carry
    # plausible business identifiers, or the warmer becomes a source of fake triage
    # decisions. It also touches no DDB table, so nothing is written.
    #
    # Returns 'warm' rather than raising so a scheduler failure metric means a real
    # failure, not a rejected ping.
    if event.get("warmup") is True:
        return {"status": "warm"}

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
    # Recorded, not acted on: a failed or partial read still classifies on what
    # was read, so triage never blocks. What it must not do is store that as an
    # ordinary decision. Whether the agent should also say so is an open product
    # question (issues/2026-09-25-triage-drops-active-dtcs-first-seen-over-24h/).
    dtc_read = _dtc_read_outcome(cms)
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
    item["inputsSnapshot"]["dtcRead"] = dtc_read
    item["latencyMs"] = latency_ms
    item["ttl"] = int(time.time()) + 30 * 24 * 3600
    _decisions_table().put_item(Item=item)
    if dtc_read["status"] in _DTC_READ_DEGRADED:
        _emit_dtc_read_degraded(dtc_read["status"])

    return {
        "statusCode": 200,
        "classification": decision.classification.name,
        "sessionId": decision.session_id,
        "decidedAt": decision.decided_at,
        "latencyMs": latency_ms,
        "activeDtcs": decision.inputs_snapshot.get("activeDtcs", []),
        "topReason": str(decision.reason[0]) if decision.reason else "",
    }
