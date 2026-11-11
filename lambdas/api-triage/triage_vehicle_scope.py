"""Vehicle scope enforcement for POST /triage.

DS-1 (issues/2026-10-05-bidi-and-triage-accept-any-vehicle). Called from
handler.py after the tenant check. Mirrors the pattern from
lambdas/api-assistant-chat/vehicle_scope.py adapted for the triage surface.

Rules (one rule, applied here):

| Caller claims                | Allowed body VIN                                         |
|------------------------------|----------------------------------------------------------|
| custom:vehicleId (valid)     | only the VIN of record for that vehicle (GetItem by key) |
| custom:vehicleId (malformed) | none — refuse 403                                        |
| custom:fleetIds              | only VINs whose every matching row's fleetId is in list  |
| neither                      | unchanged (tenant check only, no scope enforcement)      |

Returns a VehicleScopeOutcome. Never raises.
Callers map STATUS_DENY_* to 403 and STATUS_ERROR to 503.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

log = logging.getLogger()

VEHICLE_ID_CLAIM = "custom:vehicleId"
FLEET_IDS_CLAIM = "custom:fleetIds"

# Mirrors the regex in lambdas/api-assistant-chat/vehicle_scope.py.
_VEHICLE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# ------------------------------------------------------------------
# Status constants — callers do exact comparisons, so keep stable.
# ------------------------------------------------------------------
STATUS_PASS = "pass"
STATUS_DENY_VIN_MISMATCH = "deny:vin_mismatch"
STATUS_DENY_NOT_IN_FLEET = "deny:not_in_fleet"
STATUS_DENY_MALFORMED_CLAIM = "deny:malformed_claim"
STATUS_ERROR = "error:lookup_failed"   # 503 — table unreachable or no VIN of record
STATUS_UNCHANGED = "unchanged"          # neither claim present; pass through


@dataclass(frozen=True)
class VehicleScopeOutcome:
    status: str
    detail: str = ""
    # The VIN of record for a vehicle-bound caller; the handler forwards it to the
    # classifier so a differently-cased body VIN can't key a decision row.
    vin_of_record: str = ""

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_PASS, STATUS_UNCHANGED)


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _extract_fleet_ids(claims: dict) -> list[str]:
    """Parse CSV or JSON fleet ids from the claim. Returns [] when absent or blank.

    Logic mirrors _extract_claim_list + _trusted_claim_list in avx_scope.py:
    require at least one alphanumeric character to guard blank/separator values.
    """
    raw = claims.get(FLEET_IDS_CLAIM, "")
    if isinstance(raw, list):
        values: list[str] = [str(v) for v in raw]
    else:
        raw = str(raw or "").strip()  # a non-string, non-list value must not raise
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                values = [str(v) for v in parsed]
            else:
                # JSON scalar — treat as single value
                values = [str(parsed)]
        except (json.JSONDecodeError, TypeError):
            # CSV fallback: Cognito sends custom:fleetIds as comma-separated plain string.
            values = [v.strip() for v in raw.split(",")]
    return [v for v in values if any(ch.isalnum() for ch in v)]


def _vin_from_item(item: dict[str, Any]) -> str:
    """Extract the VIN string from a low-level DynamoDB GetItem Item dict.

    The item has the form ``{"vin": {"S": "MRDN..."}, ...}``.
    Returns an empty string when the attribute is absent or empty.
    """
    vin_attr = item.get("vin") or {}
    if isinstance(vin_attr, dict):
        return (vin_attr.get("S") or "").strip()
    # Defensive: fall through to empty rather than blow up on unexpected shapes.
    return ""


def _fleet_id_from_item(item: dict[str, Any]) -> str:
    """Extract fleetId from a low-level DynamoDB item dict."""
    flt = item.get("fleetId") or {}
    if isinstance(flt, dict):
        return (flt.get("S") or "").strip()
    return ""


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def check(
    claims: dict,
    body_vin: str,
    ddb_client: Any,
    cms_vehicles_table: str,
) -> VehicleScopeOutcome:
    """Enforce vehicle scope for a /triage request.

    Args:
        claims: Verified JWT claims from the Cognito authorizer.
        body_vin: The ``vin`` from the request body.
        ddb_client: A boto3 DynamoDB low-level client (or Stubber-compatible).
        cms_vehicles_table: Name of the CMS vehicles table.

    Returns a VehicleScopeOutcome. Never raises.
    """
    claim_vid_raw = str(claims.get(VEHICLE_ID_CLAIM) or "").strip()
    body_vin = str(body_vin or "").strip()
    bound = VEHICLE_ID_CLAIM in claims or FLEET_IDS_CLAIM in claims

    # A scoped caller is never let through on a missing table name: that would
    # turn a configuration fault into "no check at all".
    if bound and not cms_vehicles_table:
        log.error("triage vehicle-scope: CMS_VEHICLES_TABLE not configured; refusing scoped caller")
        return VehicleScopeOutcome(STATUS_ERROR, "vehicles table not configured")

    # ── Vehicle-bound caller ──────────────────────────────────────────────────

    # Presence of the claim binds the caller, even when its value is blank or
    # malformed: a bound caller never falls through to a less specific branch.
    if VEHICLE_ID_CLAIM in claims:
        if not claim_vid_raw or not _VEHICLE_ID_RE.match(claim_vid_raw):
            log.warning(
                "triage vehicle-scope: malformed vehicleId claim; refusing sub=%s",
                claims.get("sub", ""),
            )
            return VehicleScopeOutcome(
                STATUS_DENY_MALFORMED_CLAIM,
                "vehicleId claim is malformed",
            )

        try:
            resp = ddb_client.get_item(
                TableName=cms_vehicles_table,
                Key={"vehicleId": {"S": claim_vid_raw}},
                ProjectionExpression="vin",
            )
        except Exception as exc:  # noqa: BLE001
            log.error(
                "triage vehicle-scope: GetItem failed for claim vehicle=%s error=%s",
                claim_vid_raw, type(exc).__name__,
            )
            return VehicleScopeOutcome(
                STATUS_ERROR,
                f"vehicles table unavailable ({type(exc).__name__})",
            )

        item = resp.get("Item") or {}
        vin_of_record = _vin_from_item(item)

        if not vin_of_record:
            log.error(
                "triage vehicle-scope: claim vehicle %s has no VIN of record; refusing",
                claim_vid_raw,
            )
            return VehicleScopeOutcome(
                STATUS_ERROR,
                "claim vehicle has no VIN of record",
            )

        if body_vin.upper() != vin_of_record.upper():
            log.warning(
                "triage vehicle-scope: VIN mismatch claim_vehicle=%s "
                "vin_of_record=<redacted> sub=%s",
                claim_vid_raw, claims.get("sub", ""),
            )
            return VehicleScopeOutcome(
                STATUS_DENY_VIN_MISMATCH,
                "vehicle not in caller scope",
            )

        return VehicleScopeOutcome(STATUS_PASS, vin_of_record=vin_of_record)

    # ── Fleet-bound caller ────────────────────────────────────────────────────

    if FLEET_IDS_CLAIM in claims:
        fleet_ids = _extract_fleet_ids(claims)

        if not fleet_ids:
            # Present but parses to nothing — A claim that is present but yields no
            # valid fleet ids allows no vehicle (report scope contract table).
            log.warning(
                "triage vehicle-scope: fleetIds claim present but empty; refusing sub=%s",
                claims.get("sub", ""),
            )
            return VehicleScopeOutcome(
                STATUS_DENY_NOT_IN_FLEET,
                "vehicle not in caller scope",
            )

        # vin-index is KEYS_ONLY (it projects vehicleId and vin, not fleetId),
        # so the query only finds the vehicles; each one's fleetId comes from a
        # GetItem on the table. Every page is read: a VIN on several rows must
        # have ALL of them in scope.
        vehicle_ids: list[str] = []
        try:
            kwargs: dict[str, Any] = {
                "TableName": cms_vehicles_table,
                "IndexName": "vin-index",
                "KeyConditionExpression": "vin = :vin",
                "ExpressionAttributeValues": {":vin": {"S": body_vin}},
            }
            while True:
                resp = ddb_client.query(**kwargs)
                for row in resp.get("Items") or []:
                    vid_attr = row.get("vehicleId") or {}
                    vid = (vid_attr.get("S") or "").strip() if isinstance(vid_attr, dict) else ""
                    if vid:
                        vehicle_ids.append(vid)
                lek = resp.get("LastEvaluatedKey")
                if not lek:
                    break
                kwargs["ExclusiveStartKey"] = lek
        except Exception as exc:  # noqa: BLE001
            log.error(
                "triage vehicle-scope: Query vin-index failed error=%s",
                type(exc).__name__,
            )
            return VehicleScopeOutcome(
                STATUS_ERROR,
                f"vehicles table unavailable ({type(exc).__name__})",
            )

        if not vehicle_ids:
            log.warning(
                "triage vehicle-scope: no rows for VIN in vin-index sub=%s",
                claims.get("sub", ""),
            )
            return VehicleScopeOutcome(
                STATUS_DENY_NOT_IN_FLEET,
                "vehicle not in caller scope",
            )

        for vid in vehicle_ids:
            try:
                item = ddb_client.get_item(
                    TableName=cms_vehicles_table,
                    Key={"vehicleId": {"S": vid}},
                    ProjectionExpression="fleetId",
                ).get("Item") or {}
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "triage vehicle-scope: GetItem failed for fleet check error=%s",
                    type(exc).__name__,
                )
                return VehicleScopeOutcome(
                    STATUS_ERROR,
                    f"vehicles table unavailable ({type(exc).__name__})",
                )
            if _fleet_id_from_item(item) not in fleet_ids:
                log.warning(
                    "triage vehicle-scope: row fleetId not in caller fleet_ids sub=%s",
                    claims.get("sub", ""),
                )
                return VehicleScopeOutcome(
                    STATUS_DENY_NOT_IN_FLEET,
                    "vehicle not in caller scope",
                )

        return VehicleScopeOutcome(STATUS_PASS)

    # ── Neither claim — pass through (tenant check only) ─────────────────────
    return VehicleScopeOutcome(STATUS_UNCHANGED)
