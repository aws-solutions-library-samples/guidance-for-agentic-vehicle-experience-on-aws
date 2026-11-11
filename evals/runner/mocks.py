"""Mock fixtures for external services used by the VSA supervisor tools.

Each mock class implements the same public interface as its production
counterpart but returns deterministic fixture data with no AWS calls.

Fixture vehicle: 2022 Ford F-150, VIN 1FTRX18W6XKB12345, VEH-0025
Fixture driver: Samantha Carter, drv-samantha-carter

Production file citations:
  CMS:     lambdas/shared/cms_client_real.py (RealCmsClient)
  ADP:     agents/supervisor/tools/vehicle_identity.py,
           agents/supervisor/tools/parts_lookup.py,
           agents/supervisor/tools/service_network.py
  KB:      agents/supervisor/tools/knowledge.py (_query_bedrock_kb)
  Connect: agents/supervisor/tools/escalate.py (call_escalate_endpoint)
  Memory:  agents/supervisor/tools/{vehicle_identity,service_history,...}.py
  Session: agents/supervisor/session.py (SessionContext)
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Path bootstrap — make triage_classifier importable.
# The source lives in lambdas/triage-classifier/ (hyphen in dir name), which
# Python cannot find via normal sys.path lookup. We register it via importlib
# so `from triage_classifier.types import ...` works without modifying
# sys.path with a hyphenated directory.
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parents[2]
_TC_DIR = str(_ROOT / "lambdas" / "triage-classifier")

if "triage_classifier" not in sys.modules:
    import importlib.util as _ilu

    _spec = _ilu.spec_from_file_location(
        "triage_classifier",
        str(_ROOT / "lambdas" / "triage-classifier" / "__init__.py"),
        submodule_search_locations=[_TC_DIR],
    )
    _mod = _ilu.module_from_spec(_spec)  # type: ignore[arg-type]
    sys.modules["triage_classifier"] = _mod
    _spec.loader.exec_module(_mod)  # type: ignore[union-attr]

from triage_classifier.types import ActiveDtc, OpenRecall, TelemetrySnapshot  # noqa: E402

# ---------------------------------------------------------------------------
# Fixture constants
# ---------------------------------------------------------------------------

FIXTURE_VIN = "1FTRX18W6XKB12345"
FIXTURE_VEHICLE_ID = "VEH-0025"
FIXTURE_DRIVER_ID = "drv-samantha-carter"
FIXTURE_TENANT_ID = "meridian-fleet"


# ---------------------------------------------------------------------------
# MockCMSClient
# ---------------------------------------------------------------------------


class MockCMSClient:
    """Mock of lambdas/shared/cms_client_real.py::RealCmsClient.

    Implements the same public protocol methods (get_telemetry,
    get_active_dtcs, get_open_recalls) with deterministic fixture data.
    No boto3, no Redis, no network calls.
    """

    def get_telemetry(self, vin: str) -> TelemetrySnapshot:
        """Return a fresh TelemetrySnapshot for the fixture VIN."""
        if vin != FIXTURE_VIN:
            return TelemetrySnapshot(stale=True)
        return TelemetrySnapshot(
            coolant_temp_c=88.0,
            tire_pressure_fl_psi=34.0,
            tire_pressure_fr_psi=34.0,
            tire_pressure_rl_psi=34.0,
            tire_pressure_rr_psi=34.0,
            tire_spec_psi=34.0,
            brake_pad_wear_mm_min=6.0,
            battery_soc_pct=82.0,
            oil_pressure_kpa=310.0,
            oil_temp_c=95.0,
            speed_mph=0.0,
            abs_status_ok=True,
            last_update="2026-05-25T10:00:00Z",
            stale=False,
        )

    def get_active_dtcs(self, vin: str) -> tuple[ActiveDtc, ...]:
        """Return active DTCs for the fixture VIN (P0299 turbo underboost)."""
        if vin != FIXTURE_VIN:
            return ()
        return (
            ActiveDtc(
                code="P0299",
                first_seen_at="2026-05-24T08:00:00Z",
                persistent=True,
            ),
        )

    def get_open_recalls(self, vin: str) -> tuple[OpenRecall, ...]:
        """Return open recalls for the fixture VIN."""
        return ()


# ---------------------------------------------------------------------------
# MockADPClient
# ---------------------------------------------------------------------------


class MockADPClient:
    """Mock of the ADP DynamoDB tables queried by:
      - agents/supervisor/tools/vehicle_identity.py (adp-*-vehicle-identity)
      - agents/supervisor/tools/parts_lookup.py (adp-*-parts-catalog)
      - agents/supervisor/tools/service_network.py (adp-*-service-network)

    The production tools call boto3.resource("dynamodb").Table(...) directly.
    This mock provides the same return shapes via its methods so tests can
    patch the DynamoDB calls or use the mock as a drop-in data source.
    """

    def decode_vin(self, vin: str) -> dict[str, Any]:
        """Return vehicle identity record matching adp-*-vehicle-identity schema."""
        if vin != FIXTURE_VIN:
            return {"found": False, "vin": vin, "message": "VIN not found in identity database."}
        return {
            "found": True,
            "vin": FIXTURE_VIN,
            "make": "Ford",
            "model": "F-150",
            "year": 2022,
            "trim": "XLT",
            "engine": "3.5L EcoBoost V6",
            "transmission": "10-speed automatic",
            "fuelType": "gasoline",
            "maintenanceGroup": "ford-f150-2022",
            "recallEligibility": True,
        }

    def lookup_parts(
        self,
        category: str,
        vehicle_make: str = "",
        vehicle_model: str = "",
    ) -> dict[str, Any]:
        """Return parts catalog entries matching adp-*-parts-catalog schema."""
        catalog: dict[str, list[dict[str, Any]]] = {
            "brakes": [
                {
                    "partNumber": "BRK-F150-2022-FR",
                    "description": "Front brake pad set — Ford F-150 2022",
                    "pricing": {"oem": 189.99, "equivalent": 89.99, "fleet": 74.99},
                    "inStock": True,
                    "leadTimeDays": 0,
                    "supplier": "Motorcraft",
                    "warrantyMonths": 24,
                }
            ],
            "tires": [
                {
                    "partNumber": "TIRE-LT275-65R18",
                    "description": "LT275/65R18 all-terrain tire",
                    "pricing": {"oem": 249.99, "equivalent": 179.99, "fleet": 159.99},
                    "inStock": True,
                    "leadTimeDays": 1,
                    "supplier": "Meridian Fleet",
                    "warrantyMonths": 60,
                }
            ],
            "engine": [
                {
                    "partNumber": "ENG-OIL-FILTER-F150",
                    "description": "Engine oil filter — 3.5L EcoBoost",
                    "pricing": {"oem": 14.99, "equivalent": 8.99, "fleet": 7.49},
                    "inStock": True,
                    "leadTimeDays": 0,
                    "supplier": "Motorcraft",
                    "warrantyMonths": 6,
                }
            ],
        }
        parts = catalog.get(category, [])
        return {"found": bool(parts), "parts": parts}

    def find_service_centers(
        self,
        capability: str,
        vehicle_make: str = "",
        latitude: float = 0.0,
        longitude: float = 0.0,
        max_results: int = 3,
    ) -> dict[str, Any]:
        """Return service center records matching adp-*-service-network schema."""
        centers = [
            {
                "centerId": "SC-FORD-DALLAS-001",
                "name": "Dallas Ford Service Center",
                "type": "dealer",
                "address": "1234 Commerce St, Dallas, TX 75201",
                "phone": "214-555-0100",
                "hours": {"weekday": "7am-6pm", "saturday": "8am-4pm"},
                "rating": 4.7,
                "averageWaitDays": 1,
                "fleetDiscount": True,
                "distanceMiles": 2.3,
                "nextAvailableSlots": [
                    "Tuesday May 26 at 9:00 AM",
                    "Wednesday May 27 at 2:00 PM",
                ],
                "capabilities": [
                    "brakes", "engine", "tires", "diagnostics",
                    "recall-service", "oil-change",
                ],
                "brandsServiced": ["Ford", "Lincoln"],
                "latitude": 32.7767,
                "longitude": -96.7970,
            },
            {
                "centerId": "SC-JIFFY-DALLAS-002",
                "name": "Jiffy Lube — Uptown Dallas",
                "type": "quick-service",
                "address": "567 McKinney Ave, Dallas, TX 75204",
                "phone": "214-555-0200",
                "hours": {"weekday": "8am-7pm", "saturday": "8am-5pm"},
                "rating": 4.2,
                "averageWaitDays": 0,
                "fleetDiscount": False,
                "distanceMiles": 3.1,
                "nextAvailableSlots": ["Tuesday May 26 at 10:00 AM"],
                "capabilities": [
                    "oil-change", "tires", "tire-pressure", "tire-inflation",
                ],
                "brandsServiced": [],
                "latitude": 32.7900,
                "longitude": -96.8010,
            },
        ]
        return {"found": True, "centers": centers[:max_results]}


# ---------------------------------------------------------------------------
# MockKBClient
# ---------------------------------------------------------------------------


class MockKBClient:
    """Mock of the Bedrock Knowledge Base queried by:
      agents/supervisor/tools/knowledge.py (_query_bedrock_kb)

    Returns deterministic passages + answer for known query keywords.
    Matches the return shape of boto3 bedrock-agent-runtime::retrieve().
    """

    _PASSAGES: dict[str, dict[str, Any]] = {
        "p0299": {
            "found": True,
            "passages": [
                {
                    "text": (
                        "DTC P0299 — Turbocharger/Supercharger Underboost Condition. "
                        "Indicates the turbocharger is not producing expected boost pressure. "
                        "Common causes: boost leak, wastegate stuck open, turbo failure. "
                        "Severity: P1 — schedule service within 48 hours."
                    ),
                    "source": "s3://vsa-kb/dtc/P0299.md",
                    "score": 0.95,
                }
            ],
            "answer": "P0299 indicates turbocharger underboost. Schedule service within 48 hours.",
        },
        "tire": {
            "found": True,
            "passages": [
                {
                    "text": (
                        "2022 Ford F-150 tire pressure specification: 35 PSI front and rear "
                        "for standard load LT275/65R18. Check cold (vehicle parked ≥3 hours). "
                        "TPMS warning triggers at 25% below spec (~26 PSI)."
                    ),
                    "source": "s3://vsa-kb/maintenance/tire-pressure-f150-2022.md",
                    "score": 0.92,
                }
            ],
            "answer": "2022 F-150 tire pressure: 35 PSI front and rear (cold).",
        },
        "recall": {
            "found": True,
            "passages": [
                {
                    "text": (
                        "NHTSA Recall 24V-567: 2021-2023 Ford F-150 — Rear axle shaft may "
                        "fracture under load. Remedy: dealer inspection and axle replacement "
                        "at no charge. Remedy available as of 2024-03-15."
                    ),
                    "source": "s3://vsa-kb/recalls/24V-567.md",
                    "score": 0.91,
                }
            ],
            "answer": "Recall 24V-567 affects 2021-2023 F-150 rear axle. Remedy available.",
        },
        "tsb": {
            "found": True,
            "passages": [
                {
                    "text": (
                        "TSB 2024-EN-0298: 2022 Ford F-150 EcoBoost — Intermittent rough idle "
                        "at cold start. Root cause: fuel injector deposit buildup. "
                        "Remedy: fuel system cleaning + injector calibration update."
                    ),
                    "source": "s3://vsa-kb/tsb/2024-EN-0298.md",
                    "score": 0.88,
                }
            ],
            "answer": "TSB 2024-EN-0298: rough idle at cold start — fuel injector cleaning required.",
        },
    }

    def retrieve(self, query: str, max_results: int = 3) -> dict[str, Any]:
        """Return deterministic passages for known keywords; empty result otherwise."""
        q = query.lower()
        for keyword, result in self._PASSAGES.items():
            if keyword in q:
                passages = result["passages"][:max_results]
                return {
                    "found": True,
                    "passages": passages,
                    "answer": result["answer"],
                }
        return {
            "found": False,
            "passages": [],
            "answer": "No relevant documents found.",
        }


# ---------------------------------------------------------------------------
# MockConnectClient
# ---------------------------------------------------------------------------


class MockConnectClient:
    """Mock of the Amazon Connect calls made by:
      agents/supervisor/tools/escalate.py (call_escalate_endpoint → requests.post)

    Returns deterministic contact IDs and participant credentials.
    No HTTP calls, no boto3.
    """

    CONTACT_ID = "mock-contact-id-abc123"
    PARTICIPANT_ID = "mock-participant-id-def456"
    PARTICIPANT_TOKEN = "mock-participant-token-ghi789"
    CONNECTION_EXPIRY = "2026-05-25T11:00:00Z"

    def start_chat_contact(
        self,
        *,
        severity: str,
        vin: str,
        vehicle_id: str,
        reason: str,
        session_id: str,
        driver_id: str | None = None,
        summary: str | None = None,
        dispatch_roadside: bool = False,
    ) -> dict[str, Any]:
        """Return a deterministic /escalate response shape."""
        return {
            "contactId": self.CONTACT_ID,
            "participantId": self.PARTICIPANT_ID,
            "participantToken": self.PARTICIPANT_TOKEN,
            "connectionExpiry": self.CONNECTION_EXPIRY,
            "severity": severity,
            "rsaDispatched": severity == "P0" and dispatch_roadside,
            "rsaActionId": (
                "mock-rsa-action-001" if severity == "P0" and dispatch_roadside else None
            ),
        }


# ---------------------------------------------------------------------------
# MockSessionContext
# ---------------------------------------------------------------------------


def MockSessionContext(
    *,
    tenant_id: str = FIXTURE_TENANT_ID,
    vehicle_id: str = FIXTURE_VEHICLE_ID,
    driver_id: str = FIXTURE_DRIVER_ID,
    vin: str = FIXTURE_VIN,
    jwt: str = "mock-jwt-token",
    session_id: str = "vsa-sess-mock000001",
    client_latitude: float = 32.7767,
    client_longitude: float = -96.7970,
    persona_id: str = "fleet_driver",
) -> Any:
    """Factory that produces a SessionContext populated with fixture values.

    Imports and instantiates the real SessionContext from
    agents.supervisor.session — does NOT redefine it.

    Default fixture: tenantId='meridian-fleet', vehicleId='VEH-0025',
    driverId='drv-samantha-carter', VIN='1FTRX18W6XKB12345' (2022 Ford F-150).
    """
    from agents.supervisor.session import SessionContext  # noqa: PLC0415

    return SessionContext(
        tenant_id=tenant_id,
        session_id=session_id,
        jwt=jwt,
        vin=vin,
        vehicle_id=vehicle_id,
        driver_id=driver_id,
        client_latitude=client_latitude,
        client_longitude=client_longitude,
        persona_id=persona_id,
    )


# ---------------------------------------------------------------------------
# MemoryStub — in-process AgentCore Memory for eval tests (Group 6.A)
# ---------------------------------------------------------------------------


import uuid as _uuid


class MemoryStub:
    """In-process stub for AgentCore Memory bedrock-agentcore boto3 client.

    Implements duck-typed versions of:
      - retrieve_memory_records
      - list_memory_records
      - create_event
      - batch_create_memory_records
      - get_memory_record

    Storage key: (memory_id, namespace, record_id)

    Metadata filter semantics (per docs/tech.md § 6):
      - operator EQUALS_TO: exact match on stringValue or numberValue
      - operator GREATER_THAN: numeric comparison (numberValue)
      - AND logic across all filters in metadataFilters list
      - max 5 filters per query (enforced via assertion in tests)
    """

    def __init__(self):
        # {(memory_id, namespace, record_id): {"content": {...}, "metadata": {...}, "recordId": str}}
        self._store: dict[tuple[str, str, str], dict] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_meta_value(self, meta_val: dict) -> object:
        """Extract the Python value from a MemoryMetadataValue union dict."""
        if "stringValue" in meta_val:
            return meta_val["stringValue"]
        if "numberValue" in meta_val:
            return meta_val["numberValue"]
        if "stringListValue" in meta_val:
            return meta_val["stringListValue"]
        return None

    def _matches_filters(self, record: dict, metadata_filters: list[dict]) -> bool:
        """Return True if record metadata satisfies ALL filters (AND logic)."""
        meta = record.get("metadata") or {}
        for f in metadata_filters:
            key = f["left"]["metadataKey"]
            op = f["operator"]
            right_val = self._get_meta_value(f["right"]["metadataValue"])
            rec_val = self._get_meta_value(meta.get(key) or {})
            if op == "EQUALS_TO":
                if rec_val != right_val:
                    return False
            elif op == "GREATER_THAN":
                try:
                    if not (float(rec_val) > float(right_val)):
                        return False
                except (TypeError, ValueError):
                    return False
            # EXISTS / other operators: pass-through (not needed for v1)
        return True

    def _filter_records(
        self,
        memory_id: str,
        namespace: str | None,
        metadata_filters: list[dict],
    ) -> list[dict]:
        results = []
        for (mid, ns, rid), rec in self._store.items():
            if mid != memory_id:
                continue
            if namespace and not ns.startswith(namespace):
                continue
            if metadata_filters and not self._matches_filters(rec, metadata_filters):
                continue
            results.append(rec)
        return results

    # ------------------------------------------------------------------
    # Public API (mirrors bedrock-agentcore boto3 client methods)
    # ------------------------------------------------------------------

    def retrieve_memory_records(self, **kwargs) -> dict:
        """Semantic-like retrieve: returns all matching records (no real vector search)."""
        memory_id = kwargs["memoryId"]
        criteria = kwargs.get("searchCriteria") or {}
        namespace = kwargs.get("namespace")
        filters = criteria.get("metadataFilters") or []
        top_k = criteria.get("topK") or kwargs.get("maxResults") or 20
        records = self._filter_records(memory_id, namespace, filters)[:top_k]
        return {"memoryRecords": records}

    def list_memory_records(self, **kwargs) -> dict:
        """List records matching optional namespace + metadata filters."""
        memory_id = kwargs["memoryId"]
        namespace = kwargs.get("namespace")
        filters = kwargs.get("metadataFilters") or []
        max_r = kwargs.get("maxResults") or 20
        records = self._filter_records(memory_id, namespace, filters)[:max_r]
        return {"memoryRecords": records, "nextToken": None}

    def create_event(self, **kwargs) -> dict:
        """Store event payload as a memory record; metadata is string-only."""
        memory_id = kwargs["memoryId"]
        actor_id = kwargs.get("actorId", "")
        metadata_raw = kwargs.get("metadata") or {}
        # create_event metadata: {key: {"stringValue": str}}
        metadata = {k: v for k, v in metadata_raw.items()}
        # Derive namespace from metadata if available
        tenant = metadata.get("tenant_id", {}).get("stringValue") or "default"
        persona = metadata.get("persona_id", {}).get("stringValue") or actor_id or "unknown"
        namespace = f"cvx/{tenant}/{persona}"
        # Extract text content from first payload item
        payload = kwargs.get("payload") or []
        text = ""
        if payload:
            first = payload[0]
            conv = first.get("conversational") or {}
            text = (conv.get("content") or {}).get("text", "")
        record_id = str(_uuid.uuid4())
        record = {
            "recordId": record_id,
            "content": {"text": text},
            "metadata": metadata,
            "namespace": namespace,
        }
        self._store[(memory_id, namespace, record_id)] = record
        return {"recordId": record_id}

    def batch_create_memory_records(self, **kwargs) -> dict:
        """Store multiple records with typed metadata (STRING / NUMBER)."""
        memory_id = kwargs["memoryId"]
        records_in = kwargs.get("records") or []
        results = []
        for rec_in in records_in:
            namespaces = rec_in.get("namespaces") or ["cvx/default/unknown"]
            namespace = namespaces[0]
            content = rec_in.get("content") or {}
            metadata = rec_in.get("metadata") or {}
            record_id = str(_uuid.uuid4())
            record = {
                "recordId": record_id,
                "content": content,
                "metadata": metadata,
                "namespace": namespace,
            }
            self._store[(memory_id, namespace, record_id)] = record
            results.append({"recordId": record_id, "status": "SUCCESS"})
        return {"records": results}

    def get_memory_record(self, **kwargs) -> dict:
        """Fetch a single record by ID (scans all namespaces for this memory)."""
        memory_id = kwargs["memoryId"]
        record_id = kwargs["memoryRecordId"]
        for (mid, ns, rid), rec in self._store.items():
            if mid == memory_id and rid == record_id:
                return {"memoryRecord": rec}
        from botocore.exceptions import ClientError
        raise ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": f"Record {record_id} not found"}},
            "GetMemoryRecord",
        )

    def make_client(self) -> "MemoryStub":
        """Return self as a duck-typed boto3-like client."""
        return self

    def reset(self) -> None:
        """Clear all stored records (useful between tests)."""
        self._store.clear()


# ---------------------------------------------------------------------------
# seed_persona_memory — preload fixture data into a MemoryStub
# ---------------------------------------------------------------------------


def seed_persona_memory(
    stub: MemoryStub,
    memory_id: str,
    persona_id: str,
    vehicle_id: str,
    customer_id: str,
    fixtures: dict,
    tenant_id: str = "fleet",
) -> None:
    """Seed a MemoryStub with persona data from a fixtures dict.

    Each key in *fixtures* is an ADP data_source name (e.g. "vehicle_identity",
    "service_records"). Values are lists of row dicts (or a single dict).

    Records are written via batch_create_memory_records with the standard
    CVX metadata schema (persona_id, vehicle_id, customer_id, data_source,
    last_updated, tenant_id).

    Group 6.D ships golden fixtures at
    agents/supervisor/tests/fixtures/persona_loader_golden/<product>.json.
    If that directory is absent, callers supply minimal inline dicts.
    """
    import time as _time
    import json as _json

    namespace = f"cvx/{tenant_id}/{persona_id}"
    now_ms = int(_time.time() * 1000)

    records_in = []
    for data_source, rows in fixtures.items():
        if isinstance(rows, dict):
            rows = [rows]
        for row in rows:
            metadata = {
                "persona_id": {"stringValue": persona_id},
                "vehicle_id": {"stringValue": vehicle_id},
                "customer_id": {"stringValue": customer_id},
                "data_source": {"stringValue": data_source},
                "last_updated": {"numberValue": float(now_ms)},
                "tenant_id": {"stringValue": tenant_id},
            }
            records_in.append({
                "namespaces": [namespace],
                "content": {"text": _json.dumps(row)},
                "metadata": metadata,
            })

    if records_in:
        stub.batch_create_memory_records(memoryId=memory_id, records=records_in)
