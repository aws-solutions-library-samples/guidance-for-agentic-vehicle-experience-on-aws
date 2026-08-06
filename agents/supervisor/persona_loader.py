"""Session-init persona data loader for CVX-on-foundation v1.

Implements `load_persona` per spec § "Design ▸ Persona-loader module design".
Internal flow: TTL gate → Athena fetch → BatchCreateMemoryRecords ingest.

Decision Q1 (decisions.md): Uses `batch_create_memory_records` (data-plane
`bedrock-agentcore` client) with `MemoryRecordMetadataMap` for typed metadata
(NUMBER `last_updated` needed for TTL filter; `CreateEvent` only supports
string metadata values).

Decision Q6 (decisions.md): `bedrock-agentcore==1.9.1` pinned in requirements.txt.

R15 — Athena SQL injection guard (spec § Internal flow step 2):
  Athena does NOT support native `?` prepared-statement placeholders via the
  standard `StartQueryExecution` API (PreparedStatements require a separate
  `CreatePreparedStatement` + `EXECUTE` call path that adds latency). v1 uses
  STRICT REGEX VALIDATION: `vehicle_id` and `customer_id` MUST match
  `^[A-Za-z0-9_-]{1,64}$` before interpolation. Any value that fails
  validation causes the product fetch to be skipped with a WARNING log.
  The validator `_validate_id` is called inline before every query string
  is built — it is not optional or bypassable.

See `.kiro/specs/2026-06-15-cvx-on-foundation-v1/spec.md` for full contract.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

import boto3

from agents.supervisor.persona_definitions import (
    PERSONA_PRODUCT_BINDINGS,
    PERSONA_TENANT_DEFAULTS,
    PYSPARK_GATED_PRODUCTS,
)

log = logging.getLogger(__name__)

# R15 — id validation regex (vehicle_id, customer_id)
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# R15 (extended 2026-08-03) — stage validation regex.
#
# `adp_stage` is interpolated into Athena *identifiers* (schema and table names,
# e.g. `adp_{stage}_vehicle_identity.vehicle_identity`), not into string
# literals. Identifiers cannot be defensively quoted the way `'{vehicle_id}'`
# is, so a bad value here is strictly worse than a bad id. Today the value
# resolves from `os.environ["VSA_ADP_STAGE"]` at config-build time and is not
# reachable from the request path, so this is hardening rather than a live
# vulnerability — but it was the one interpolation in `_athena_query` with no
# guard at all. Hyphen is deliberately excluded (unlike `_ID_RE`): a hyphen is
# not valid in an unquoted SQL identifier.
# See issues/2026-08-03-cvx-persona-loader-sql-guard/report.md.
_STAGE_RE = re.compile(r"^[A-Za-z0-9_]{1,32}$")


def _validate_id(value: str | None, name: str) -> bool:
    """Return True iff value is safe for Athena string interpolation."""
    if not value:
        return False
    if not _ID_RE.match(value):
        log.warning("persona_loader: %s=%r fails safety regex — skipping product", name, value)
        return False
    return True


def _validate_stage(value: str | None) -> bool:
    """Return True iff value is safe to interpolate as a SQL identifier fragment."""
    if not value:
        return False
    if not _STAGE_RE.match(value):
        log.warning(
            "persona_loader: adp_stage=%r fails safety regex — skipping product", value
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Athena query templates (R15: ids are validated before interpolation)
# ---------------------------------------------------------------------------
def _athena_query(product: str, adp_stage: str, vehicle_id: str | None, customer_id: str | None) -> str | None:
    """Build the Athena SQL for `product`. Returns None if required id is missing/invalid.

    Every value interpolated below is validated first — `adp_stage` here, and
    `vehicle_id`/`customer_id` in each product branch. Validation is fail-closed:
    a bad value returns None, which the caller treats as "skip this product"
    rather than falling through to an unguarded query.

    The `# nosec B608` pragmas on each return are adjudicated false positives.
    bandit's B608 is flow-insensitive and cannot see the guards above; Athena's
    `StartQueryExecution` accepts no bind parameters, so allowlist validation is
    the mitigation (R15, see module docstring). Do NOT remove a pragma without
    also confirming the corresponding validation still runs — and do not add a
    new product branch without a validation call.
    See issues/2026-08-03-cvx-persona-loader-sql-guard/report.md.
    """
    if not _validate_stage(adp_stage):
        return None
    s = adp_stage
    if product == "vehicle_identity":
        if not _validate_id(vehicle_id, "vehicle_id"):
            return None
        return (
            f"SELECT * FROM adp_{s}_vehicle_identity.vehicle_identity "  # nosec B608
            f"WHERE vin = '{vehicle_id}' LIMIT 1"
        )
    if product == "service_records":
        if not _validate_id(vehicle_id, "vehicle_id"):
            return None
        return (
            f"SELECT * FROM adp_{s}_service_records.service_records "  # nosec B608
            f"WHERE vin = '{vehicle_id}' "
            f"AND service_month >= DATE_ADD('month', -12, CURRENT_DATE)"
        )
    if product == "charging_sessions":
        if not _validate_id(vehicle_id, "vehicle_id"):
            return None
        return (
            f"SELECT * FROM adp_{s}_charging_sessions.charging_sessions "  # nosec B608
            f"WHERE vin = '{vehicle_id}' "
            f"AND session_date >= DATE_ADD('day', -30, CURRENT_DATE) "
            f"ORDER BY session_date DESC LIMIT 100"
        )
    if product == "customer_360":
        if not _validate_id(customer_id, "customer_id"):
            return None
        return (
            f"SELECT * FROM adp_{s}_customer_360.customer_360 "  # nosec B608
            f"WHERE customer_id = '{customer_id}' "
            f"AND snapshot_date = ("
            f"SELECT MAX(snapshot_date) FROM adp_{s}_customer_360.customer_360)"
        )
    if product == "customer_interactions":
        if not _validate_id(customer_id, "customer_id"):
            return None
        return (
            f"SELECT * FROM adp_{s}_customer_interactions.customer_interactions "  # nosec B608
            f"WHERE customer_id = '{customer_id}' "
            f"AND interaction_date >= DATE_ADD('day', -90, CURRENT_DATE) "
            f"ORDER BY interaction_date DESC LIMIT 50"
        )
    if product == "ota_campaigns":
        if not _validate_id(vehicle_id, "vehicle_id"):
            return None
        # Schema reference: `ota_campaigns` header has `campaign_name`,
        # `release_version`, `category`, `severity`, `status`. There is
        # no `campaign_type` column — the spec authoring guess used the
        # wrong name; corrected to `category` after Group 7.B.3 smoke
        # 2026-06-16 surfaced COLUMN_NOT_FOUND.
        return (
            f"SELECT e.*, h.campaign_name, h.category FROM "  # nosec B608
            f"adp_{s}_ota_campaigns.ota_campaign_events e "
            f"JOIN adp_{s}_ota_campaigns.ota_campaigns h "
            f"ON h.campaign_id = e.campaign_id "
            f"WHERE e.vin = '{vehicle_id}' "
            f"AND e.dispatch_date >= DATE_ADD('month', -6, CURRENT_DATE) "
            f"ORDER BY e.dispatch_time DESC LIMIT 50"
        )
    # PySpark-gated products handled separately
    return None


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

@dataclass
class PersonaSnapshot:
    """Result of a persona-load operation."""
    persona_id: str
    vehicle_id: str | None
    customer_id: str | None
    data_sources: dict[str, dict] | None = None
    fetched_at: int = 0

    def __post_init__(self) -> None:
        if self.data_sources is None:
            self.data_sources = {}


@dataclass
class PersonaLoaderConfig:
    """Runtime configuration for `load_persona`."""
    memory_id: str
    adp_account_id: str
    adp_region: str = "us-east-1"
    adp_stage: str = "staging"
    athena_workgroup: str = "cvx-staging-analytics"
    athena_results_bucket: str = ""
    ttl_minutes: int = 15
    adp_kb_id: str = ""
    adp_kb_arn: str = ""
    memory_region: str = "us-west-2"


def _normalize_row(product: str, athena_row: dict) -> dict:
    """Schema-conform an Athena row into the JSON shape expected by tools.

    Per spec § Risks R6: the shape MUST NOT break existing eval baselines.
    The normalised output is the content stored in AgentCore Memory and
    returned by Memory-backed tools.

    For v1 the mapping is pass-through with `found=True` injected so that
    tool callers can detect populated vs missing records uniformly.
    Golden fixtures (Group 6.D) validate specific shapes per product.
    """
    row = dict(athena_row)
    row["found"] = True
    row["data_source"] = product
    return row


async def load_persona(
    persona_id: str,
    vehicle_id: str | None,
    customer_id: str | None,
    *,
    config: PersonaLoaderConfig,
) -> PersonaSnapshot:
    """Cross-region Athena fetch + AgentCore Memory ingest.

    Idempotent per product slice: if Memory already has a fresh record
    (last_updated within config.ttl_minutes), Athena is skipped for
    that product.

    Failure mode: Athena/Memory errors are logged at WARNING; the
    snapshot returns with that product missing from data_sources —
    NEVER raises.
    """
    snapshot = PersonaSnapshot(
        persona_id=persona_id,
        vehicle_id=vehicle_id,
        customer_id=customer_id,
        fetched_at=int(time.time() * 1000),
    )

    products = PERSONA_PRODUCT_BINDINGS.get(persona_id, [])
    if not products:
        log.warning("persona_loader: unknown persona_id=%r — no products bound", persona_id)
        return snapshot

    tenant_id = PERSONA_TENANT_DEFAULTS.get(persona_id, "fleet")
    namespace = f"cvx/{tenant_id}/{persona_id}"
    ttl_ms = config.ttl_minutes * 60 * 1000
    now_ms = snapshot.fetched_at

    sem = asyncio.Semaphore(4)

    async def _fetch_product(product: str) -> None:
        async with sem:
            try:
                result = await asyncio.get_event_loop().run_in_executor(
                    None, _fetch_product_sync, product, namespace, ttl_ms, now_ms, config,
                    vehicle_id, customer_id, tenant_id,
                )
                if result is not None:
                    snapshot.data_sources[product] = result
                    log.info(
                        "persona_loader: product=%s outcome=loaded",
                        product,
                        extra={"metric": f"cvx.persona_loader.{product}.loaded"},
                    )
                else:
                    log.info(
                        "persona_loader: product=%s outcome=skipped",
                        product,
                        extra={"metric": f"cvx.persona_loader.{product}.skipped"},
                    )
            except Exception as exc:
                log.warning(
                    "persona_loader: product=%s outcome=error error=%s",
                    product, exc,
                    extra={"metric": f"cvx.persona_loader.{product}.error"},
                )

    await asyncio.gather(*[_fetch_product(p) for p in products])
    return snapshot


def _fetch_product_sync(
    product: str,
    namespace: str,
    ttl_ms: int,
    now_ms: int,
    config: PersonaLoaderConfig,
    vehicle_id: str | None,
    customer_id: str | None,
    tenant_id: str,
) -> dict | None:
    """Sync worker: TTL gate → Athena fetch → Memory ingest.

    Returns the normalised row dict on success, or None if skipped/error.
    Raises on unexpected errors (caught by the async wrapper).
    """
    memory_client = boto3.client("bedrock-agentcore", region_name=config.memory_region)

    # ------------------------------------------------------------------ #
    # Step 1: TTL gate — skip Athena if Memory has a fresh record         #
    # ------------------------------------------------------------------ #
    stale_threshold_ms = now_ms - ttl_ms
    try:
        list_resp = memory_client.list_memory_records(
            memoryId=config.memory_id,
            namespace=namespace,
            maxResults=1,
            metadataFilters=[
                {
                    "left": {"metadataKey": "data_source"},
                    "operator": "EQUALS_TO",
                    "right": {"metadataValue": {"stringValue": product}},
                },
                {
                    "left": {"metadataKey": "last_updated"},
                    "operator": "GREATER_THAN",
                    "right": {"metadataValue": {"numberValue": float(stale_threshold_ms)}},
                },
            ],
        )
        if list_resp.get("memoryRecords"):
            # Cache hit — skip Athena
            return None
    except Exception as exc:
        log.warning("persona_loader: TTL gate failed for product=%s: %s — proceeding to Athena", product, exc)

    # ------------------------------------------------------------------ #
    # Step 3: PySpark gate — check Glue table existence before querying   #
    # ------------------------------------------------------------------ #
    if product in PYSPARK_GATED_PRODUCTS:
        glue_client = boto3.client("glue", region_name=config.adp_region)
        db_name = f"adp_{config.adp_stage}_{product}"
        try:
            glue_client.get_table(DatabaseName=db_name, Name=product)
        except glue_client.exceptions.EntityNotFoundException:
            log.info(
                "persona_loader: PySpark-gated product=%s not yet registered in Glue — skipping",
                product,
            )
            return None
        except Exception as exc:
            log.warning("persona_loader: Glue check for product=%s failed: %s — skipping", product, exc)
            return None

    # ------------------------------------------------------------------ #
    # Step 2: Athena fetch                                                 #
    # ------------------------------------------------------------------ #
    sql = _athena_query(product, config.adp_stage, vehicle_id, customer_id)
    if sql is None:
        log.warning("persona_loader: could not build query for product=%s (missing/invalid ids) — skipping", product)
        return None

    results_bucket = config.athena_results_bucket
    if not results_bucket:
        import os
        acct = boto3.client("sts").get_caller_identity()["Account"]
        results_bucket = f"cvx-{config.adp_stage}-athena-results-{acct}-{config.adp_region}"

    athena = boto3.client("athena", region_name=config.adp_region)
    start = athena.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Catalog": "AwsDataCatalog"},
        WorkGroup=config.athena_workgroup,
        ResultConfiguration={"OutputLocation": f"s3://{results_bucket}/athena-results/"},
    )
    execution_id = start["QueryExecutionId"]

    # Poll until complete (max ~30s)
    for _ in range(30):
        # Bounded poll, not a debug sleep: Athena has no blocking-wait or boto3
        # waiter for query completion, so callers must poll get_query_execution.
        # 1s x 30 gives a ~30s ceiling, enforced by the for/else TimeoutError
        # below. This is runtime code and was reviewed as such — the sleep is
        # load-bearing. See issues/2026-08-03-cvx-probe-critical-triage/.
        # nosemgrep: arbitrary-sleep
        time.sleep(1)
        status = athena.get_query_execution(QueryExecutionId=execution_id)
        state = status["QueryExecution"]["Status"]["State"]
        if state == "SUCCEEDED":
            break
        if state in ("FAILED", "CANCELLED"):
            reason = status["QueryExecution"]["Status"].get("StateChangeReason", "unknown")
            raise RuntimeError(f"Athena query {state} for product={product}: {reason}")
    else:
        raise TimeoutError(f"Athena query timed out for product={product}")

    # Athena GetQueryResults always returns the column-header as Row 0;
    # data rows start at index 1. Request 2 to capture header + first
    # data row. (MaxResults=1 only returns the header, even when data
    # exists — surfaced by Group 7.B.3 smoke 2026-06-16.)
    result = athena.get_query_results(QueryExecutionId=execution_id, MaxResults=2)
    rows = result.get("ResultSet", {}).get("Rows", [])
    if len(rows) < 2:
        # No data rows (only header row, or no rows at all)
        return None

    # Build dict from header + first data row
    headers = [c.get("VarCharValue", "") for c in rows[0]["Data"]]
    values = [c.get("VarCharValue", "") for c in rows[1]["Data"]]
    athena_row = dict(zip(headers, values))

    # Step 4: Schema-conform
    normalised = _normalize_row(product, athena_row)

    # ------------------------------------------------------------------ #
    # Step 5: BatchCreateMemoryRecords ingest                             #
    # ------------------------------------------------------------------ #
    # Step 5: BatchCreateMemoryRecords ingest                             #
    # (Decision Q1: use BatchCreateMemoryRecords for typed metadata.
    # Group 7.B.3 smoke 2026-06-16 surfaced that this API lives on the
    # DATA-PLANE client `bedrock-agentcore`, NOT the control-plane
    # client `bedrock-agentcore-control` — Group 1.A research had the
    # client wrong; corrected here.
    # Two API-shape gotchas the research also missed:
    # - `timestamp` must be a datetime.datetime, NOT an int epoch-ms.
    #   The boto3 service model converts to ISO 8601 internally.
    # - `metadata.<key>.stringValue` cannot be the empty string. When
    #   the session has no customer_id (vehicle-keyed personas), omit
    #   the metadata key entirely rather than passing "".)
    # ------------------------------------------------------------------ #
    last_updated_ms = int(time.time() * 1000)
    last_updated_ts = datetime.datetime.fromtimestamp(
        last_updated_ms / 1000, tz=datetime.timezone.utc
    )
    metadata: dict[str, dict] = {
        "persona_id": {"stringValue": _extract_persona_id(namespace) or "_unknown_"},
        "data_source": {"stringValue": product},
        "last_updated": {"numberValue": float(last_updated_ms)},
        "tenant_id": {"stringValue": _extract_tenant_id(namespace) or "_unknown_"},
    }
    if vehicle_id:
        metadata["vehicle_id"] = {"stringValue": vehicle_id}
    if customer_id:
        metadata["customer_id"] = {"stringValue": customer_id}
    data_client = boto3.client("bedrock-agentcore", region_name=config.memory_region)
    data_client.batch_create_memory_records(
        memoryId=config.memory_id,
        records=[
            {
                "requestIdentifier": str(uuid.uuid4()),
                "namespaces": [namespace],
                "content": {"text": json.dumps(normalised)},
                "timestamp": last_updated_ts,
                "metadata": metadata,
            }
        ],
    )

    return normalised


def _extract_persona_id(namespace: str) -> str:
    parts = namespace.split("/")
    return parts[-1] if parts else ""


def _extract_tenant_id(namespace: str) -> str:
    parts = namespace.split("/")
    return parts[1] if len(parts) >= 3 else ""
