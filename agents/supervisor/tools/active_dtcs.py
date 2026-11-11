"""get_active_dtcs tool — the active trouble codes on the vehicle in this session.

Before this tool, nothing gave the model a vehicle's codes. The classifier reads
them, but `triage` returns only the priority, and the voice prompt lists only
CRITICAL/HIGH codes (to prime the P0 first line). For a vehicle with MEDIUM or
LOW codes the model therefore told drivers it could not see any, which was false.
See issues/2026-09-29-agent-cannot-name-active-dtcs/.

Design:

- **No arguments.** The vehicle is the session's (`session.vehicle_id`, else the
  session VIN resolved through the vehicles table's `vin-index`). The model
  cannot choose another vehicle, so a driver only ever sees their own.
- **Same "active" rule as the classifier** (`lambdas/shared/cms_client_real.py`
  `get_active_dtcs`, enforced by `lambdas/shared/tests/test_dtc_cleared_rule_parity.py`):
  drop rows that `_row_is_cleared` marks cleared (status CLEARED, CLEARED_REMOTE
  or RESOLVED, or a `clearedDate` / `resetAt` / `clearedAt` marker); newest row
  per code wins. So the codes the model names are the codes the priority was
  computed from.
- **A failed read is not "no codes".** `available: false` with a reason on any
  read failure; `available: true, activeDtcs: []` only after a complete read
  that found nothing.
- **Fast and deterministic** (Tier 1): DynamoDB Query only, at most
  `_MAX_PAGES` pages, short botocore timeouts, no LLM. Nova Sonic's tool budget
  is about a second.
- **Codes, not verdicts.** It returns what the vehicle reports. What a code
  means comes from `retrieve`; how urgent it is comes from `triage`.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config as BotoConfig
from strands import tool

log = logging.getLogger(__name__)

_MAX_PAGES = 3
_MAX_CODES = 10
_DESCRIPTION_MAX = 160
# Numeric severities some producers write (4=CRITICAL ... 1=LOW), as in bidi_app.
_SEVERITY = {"4": "CRITICAL", "3": "HIGH", "2": "MEDIUM", "1": "LOW"}
_BOTO = BotoConfig(connect_timeout=1, read_timeout=2, retries={"max_attempts": 2, "mode": "standard"})

_ddb_resource: Any = None


def _ddb() -> Any:
    global _ddb_resource
    if _ddb_resource is None:
        region = os.environ.get("VSA_CMS_REGION") or os.environ.get("AWS_REGION", "us-west-2")
        _ddb_resource = boto3.resource("dynamodb", region_name=region, config=_BOTO)
    return _ddb_resource


def _table_name(kind: str) -> str:
    env = {"dtc": "CMS_DTC_HISTORY_TABLE", "vehicles": "CMS_VEHICLES_TABLE"}[kind]
    suffix = {"dtc": "dtc-history", "vehicles": "vehicles"}[kind]
    return os.environ.get(env) or f"cms-{os.environ.get('CMS_STAGE', 'dev')}-storage-{suffix}"


def resolve_vehicle_id(vin: str, ddb: Any) -> tuple[str | None, str]:
    """Map a VIN to its CMS vehicleId via `vin-index`.

    Returns ``(vehicle_id, "ok")``, ``(None, "unresolved")`` when the index holds
    no vehicle or more than one for the VIN, or ``(None, "failed")`` when the
    read raised.
    """
    try:
        resp = ddb.Table(_table_name("vehicles")).query(
            IndexName="vin-index",
            KeyConditionExpression=Key("vin").eq(vin),
            Limit=2,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("get_active_dtcs: vin-index lookup failed: %s", type(exc).__name__)
        return None, "failed"
    items = resp.get("Items") or []
    if len(items) != 1 or not items[0].get("vehicleId"):
        return None, "unresolved"
    return str(items[0]["vehicleId"]), "ok"


# A dtc-history row is cleared when its status says so, including CMS's vehicle-command clear
# (CLEARED_REMOTE), or when it carries a clear marker: clearedDate, resetAt, or clearedAt (a
# whole epoch-ms number, written by CMS services/commands/command_response_handler.py, which
# leaves clearedDate ""). This is a superset of what CMS's Flink replay guard reads (CMS
# 52eb2687). The guard ignores status and resetAt, skips an unparseable clearedDate or a
# non-Number clearedAt, and drops a clear dated too far ahead; this rule counts all of those
# as cleared, because it judges one row and a later real report writes a new ACTIVE row.
# It judges rows, not codes: an older ACTIVE row the clear did not mark still reads as active
# (CMS issue 2026-10-05-remote-clear-misses-legacy-active-rows).
# Without CLEARED_REMOTE/clearedAt, a fault cleared on the vehicle stayed "active" here
# (issue 2026-10-05-active-dtcs-reads-remote-clears-as-active).
_CLEARED_STATUSES = frozenset({"CLEARED", "CLEARED_REMOTE", "RESOLVED"})


def _row_is_cleared(item: dict) -> bool:
    """True when a dtc-history row is cleared (see the comment above)."""
    if str(item.get("status") or "").upper() in _CLEARED_STATUSES:
        return True
    if item.get("clearedDate") or item.get("resetAt"):
        return True
    cleared_at = item.get("clearedAt")
    if cleared_at is None or isinstance(cleared_at, bool):
        return False
    try:
        from decimal import Decimal, InvalidOperation
        value = Decimal(str(cleared_at))
    except (InvalidOperation, ValueError):
        return False
    return value.is_finite() and value == value.to_integral_value() and value > 0


def read_active_dtcs(vehicle_id: str, ddb: Any) -> tuple[list[dict[str, Any]], str]:
    """Return ``(codes, status)`` for a vehicle; status is complete, partial or failed.

    Codes are newest-first, one entry per code, capped at `_MAX_CODES`.
    """
    table = ddb.Table(_table_name("dtc"))
    items: list[dict[str, Any]] = []
    start_key: dict[str, Any] | None = None
    status = "complete"
    for page in range(_MAX_PAGES):
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("vehicleId").eq(vehicle_id),
            "ScanIndexForward": False,  # newest first
        }
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        try:
            resp = table.query(**kwargs)
        except Exception as exc:  # noqa: BLE001
            log.warning("get_active_dtcs: dtc-history query failed on page %d: %s", page + 1, type(exc).__name__)
            status = "partial" if page else "failed"
            break
        items.extend(resp.get("Items") or [])
        start_key = resp.get("LastEvaluatedKey")
        if not start_key:
            break
    else:
        # Pages left unread: the oldest rows. Report it rather than claim completeness.
        if start_key:
            status = "partial"

    if status == "failed":
        return [], status

    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        if _row_is_cleared(item):
            continue
        code = item.get("code")
        if not code or code in seen:
            continue
        seen.add(code)
        sev_raw = str(item.get("severity") or "").upper()
        entry: dict[str, Any] = {
            "code": str(code),
            "severity": _SEVERITY.get(sev_raw, sev_raw) or None,
            "description": str(item.get("description") or "").strip()[:_DESCRIPTION_MAX] or None,
        }
        if item.get("system"):
            entry["system"] = str(item["system"])
        out.append(entry)
        if len(out) >= _MAX_CODES:
            break
    return out, status


def make_active_dtcs_tool(session: Any, config: Any = None) -> Any:
    """Factory: a @tool bound to this session's vehicle."""

    @tool
    def get_active_dtcs() -> dict[str, Any]:
        """List the active diagnostic trouble codes (DTCs) on the vehicle in this
        session, with each code's severity and description as the vehicle reported
        it. Call this when the driver asks why a warning light (for example the
        check engine light) is on, what codes or faults the vehicle has, or what is
        wrong with it.

        Use the codes it returns: name them and say what they mean in plain words.
        For what a code means beyond its description, call `retrieve` with the
        code. For how urgent it is, call `triage`; do not judge urgency yourself.
        If `activeDtcs` is empty, say no active trouble codes were found. Only if
        `available` is false may you say you could not read the codes right now.
        If `readStatus` is "partial", say these are the codes found so far.

        Returns:
            available (bool), vehicleId, activeDtcs (list of {code, severity,
            description}), count, and readStatus ("complete" or "partial").
            When available is false: reason.
        """
        ddb = _ddb()
        vehicle_id = (getattr(session, "vehicle_id", None) or "").strip() or None
        if not vehicle_id:
            vin = (getattr(session, "vin", None) or "").strip()
            if not vin:
                return {"available": False, "reason": "No vehicle is selected in this conversation."}
            vehicle_id, lookup = resolve_vehicle_id(vin, ddb)
            if not vehicle_id:
                reason = ("Could not read the vehicle's trouble codes right now."
                          if lookup == "failed" else
                          "This vehicle is not in the fleet records, so its trouble codes cannot be read.")
                log.info("get_active_dtcs: vehicle %s for session=%s", lookup, getattr(session, "session_id", ""))
                return {"available": False, "reason": reason}

        codes, status = read_active_dtcs(vehicle_id, ddb)
        log.info(
            "get_active_dtcs: session=%s vehicle=%s status=%s count=%d",
            getattr(session, "session_id", ""), vehicle_id, status, len(codes),
        )
        if status == "failed" or (status == "partial" and not codes):
            # A partial read that found nothing may have missed an older,
            # long-running fault; it must not read as "no codes".
            return {"available": False, "reason": "Could not read the vehicle's trouble codes right now."}
        return {
            "available": True,
            "vehicleId": vehicle_id,
            "activeDtcs": codes,
            "count": len(codes),
            "readStatus": status,
        }

    return get_active_dtcs
