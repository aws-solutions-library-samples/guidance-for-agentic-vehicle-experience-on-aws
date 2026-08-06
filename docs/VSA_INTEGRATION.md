# VSA Integration — Contracts with CMS and ADP

Defines the **explicit contracts** between the Virtual Service Agent (VSA) and its two underlying platforms:

- **CMS** — [`connected-mobility-guidance-on-aws`](../../connected-mobility-guidance-on-aws) (live telemetry, DTCs, recalls, simulator, UI)
- **ADP** — [`automotive-data-platform-on-aws`](../..) (warranty, service history, parts, customer 360, predictive maintenance)

VSA does not own raw data. It reads from CMS and ADP, writes back only its own session artifacts (triage decisions, STAR-JSON tickets, escalation events, audit logs), and exposes a channel layer (Amazon Connect + companion app) on top.

This doc is the source of truth for what VSA consumes, what it produces, and the boundaries between the three systems.

---

## 1. Integration Map

```
┌─────────────────────────────────────────────────────────────────┐
│                        VSA (this guidance)                       │
│                                                                  │
│   Channels: Amazon Connect (voice, chat), Companion App (web)    │
│   Agents: Supervisor + Triage + Booking + Auth/Context           │
│   + reused VFO specialists                                        │
└──────┬───────────────────────────────────────────────┬──────────┘
       │ reads                                         │ reads
       ▼                                               ▼
┌─────────────────────────────┐        ┌─────────────────────────────┐
│       CMS (live layer)       │        │      ADP (analytics layer)  │
│                             │        │                             │
│ • Redis LKS (vehicle state) │        │ • Warranty claims           │
│ • DynamoDB: DTCs, trips     │        │ • Service history           │
│ • Recall integration service│        │ • Parts catalog             │
│ • Telemetry simulator       │        │ • Customer 360              │
│ • CMS UI (map, dashboards)  │        │ • Predictive maintenance    │
│ • WebSocket fanout          │        │ • VFO knowledge base        │
└─────────────────────────────┘        └─────────────────────────────┘
       ▲                                               ▲
       │ writes                                        │ writes
       │ (STAR-JSON tickets, triage decisions, audit)  │
       └───────────────────────────────────────────────┘
```

VSA is a consumer of CMS and ADP, and a producer of tickets, decisions, and audit events that both platforms may surface in their own UIs.

---

## 2. What VSA Consumes from CMS

### 2.1 Redis LKS — Last Known State

**Source.** CMS Flink telemetry processor writes every signal value to Redis on each message. Pattern documented in CMS `docs/redis-vehicle-state.md`.

**Consumer.** VSA `AuthContextAgent` and `TriageAgent`.

**Implementation status.** `lambdas/shared/cms_client_real.py` ships the real client. Unit-tested with `fakeredis`. Not yet activated in the deployed classifier Lambda — activation is a configuration change, not code.

**Activation checklist (once CMS us-east-1 is reachable).**

| Env var | Purpose | Example |
|---|---|---|
| `VSA_CMS_CLIENT` | Switch from fake to real | `real` |
| `CMS_REDIS_ENDPOINT` | ElastiCache cluster endpoint | `cms-prod.abc123.use1.cache.amazonaws.com` |
| `CMS_REDIS_PORT` | Cluster port | `6379` |
| `CMS_REDIS_TLS` | Enforce TLS | `true` |
| `CMS_VEHICLES_TABLE` | DDB table mapping VIN → vehicleId | `cms-prod-storage-vehicles` |
| `CMS_DTC_HISTORY_TABLE` | DDB table of DTC events | `cms-prod-storage-dtc-history` |
| `CMS_DTC_LOOKBACK_HOURS` | How far back to read DTCs | `24` |
| `CMS_RECALL_API_URL` | Optional recall service endpoint | `https://recalls.cms.internal/recalls` |

**CDK additions required (not yet in stack).**

- VSA classifier Lambda placed in a VPC with routes to the CMS VPC (peering or Transit Gateway).
- Lambda security group allowed by the CMS Redis security group on port 6379.
- IAM grants: read on `cms-prod-storage-vehicles`, query on `cms-prod-storage-dtc-history`.
- IAM grant `elasticache:Connect` on the cluster ARN (if IAM auth is enabled on the cluster).

**Keys VSA reads (per CMS schema):**

| Pattern | Purpose |
|---|---|
| `vehicle:{vehicleId}:signals` | Hash of signal_id → value (integers as keys; catalog resolves to names) |
| `vehicle:{vehicleId}:meta` | Hash with `connectionStatus`, `lastSeenAt` (used for stale detection) |
| `signal_catalog:map` | Hash name → signal_id (loaded once per container cold start) |

**VIN → vehicleId mapping.** CMS Redis keys use `vehicleId` (`VEH-0025`), not VIN. VSA accepts VIN as the external identifier (STAR-JSON, warranty, audit), then performs one `GetItem` against `cms-prod-storage-vehicles` to resolve. Cached per-session.

**Unit conversions.** CMS stores `coolant_temp` and `oil_temp` in Fahrenheit per `services/data_processing/signal-catalog.json`. VSA's classifier expects Celsius; conversion happens at the client boundary.

**DTC location.** DTCs are **not** in Redis. They are in DynamoDB `cms-prod-storage-dtc-history`, keyed by VIN + timestamp. VSA reads the last 24 hours (configurable), collapses to the most-recent row per code, and excludes rows with `resetAt` set.

**Failure mode.** If Redis is unavailable, stale, or the vehicle key is missing, the telemetry snapshot returns `stale=True` with null values. Classifier degrades gracefully (relies on DTCs + recalls only) rather than blocking.

### 2.2 DTC history — DynamoDB

**Source.** CMS DTC table (populated by Flink maintenance processor).

**Table.** `cms-{env}-dtc-history` (existing in CMS storage stack).

**Access.** Cross-account read via IAM role or read-only IAM policy if same account.

**Keys VSA reads.**

| Field | Use |
|---|---|
| `VIN` (PK) + `timestamp` (SK) | Paginated history lookup for last 30 days |
| `code`, `severity`, `frequency` | Inputs to triage classifier |
| `resetAt`, `resetConditions` | Distinguishes one-shot faults from persistent |

**Query pattern.** `Query(VIN, timestamp >= now - 30d)`, limit 100. VSA caches the result in the session for the duration of the call.

### 2.3 Recalls — CMS recall-integration service

**Source.** CMS `services/recall-integration` (NHTSA poller + OEM TSB ingestion).

**Consumer.** VSA `TriageAgent` and reused VFO `RecallWarrantyAgent`.

**Access.** HTTP GET against the recall-integration internal API, or direct DynamoDB read of the recall table.

**Contract.**

```
GET /recalls?vin={VIN}&status=open
→ 200 OK
{
  "vin": "1FMCU0F70MUA12345",
  "recalls": [
    {
      "nhtsaId": "26V123456",
      "description": "Coolant temperature sensor may provide inaccurate readings",
      "severity": "moderate",
      "remedyAvailable": true,
      "openedAt": "2026-02-14T00:00:00Z",
      "affectedSystems": ["powertrain", "engine-management"]
    }
  ]
}
```

**Failure mode.** If recall service unavailable, triage proceeds without recall enrichment, trace notes `recall_lookup: unavailable`, no classification downgrade.

### 2.4 Simulator — fault injection for demo

**Source.** CMS `services/simulation`. Specifically `test_forced_alerts.py`, `safety_alert_scenarios.py`, and the `simulation_api.py` REST endpoints.

**Consumer.** VSA companion app's hidden presenter control panel (`?debug=1`).

**Contract.** VSA issues calls to the CMS simulator API to trigger scenarios:

| Scenario | CMS simulator call |
|---|---|
| P3 tire pressure dip | `POST /simulator/inject { "vin": "...", "signal": "tire_pressure_fl", "value": 32, "duration_s": 60 }` |
| P1 coolant overtemp + DTC | `POST /simulator/inject { "vin": "...", "signal": "coolant_temp", "value": 118 }` + `POST /simulator/dtc { "vin": "...", "code": "P0217" }` |
| P0 brake fault | `POST /simulator/dtc { "vin": "...", "code": "C1234", "severity": "critical" }` + `POST /simulator/inject { "vin": "...", "signal": "abs_status", "value": 0 }` |

VSA does not modify CMS simulator code. It only calls existing endpoints.

### 2.5 Vehicle metadata

**Source.** CMS `vehicle` DynamoDB table (VIN → make, model, year, OEM, fleet ID).

**Consumer.** VSA `AuthContextAgent` to resolve VIN → tenant and fuel enrichment for prompts.

**Access pattern.** Single `GetItem(VIN)` per session.

### 2.6 CMS UI — optional handoff

VSA does **not** embed in or replace the CMS UI. When a fleet manager persona (v2+) wants to deep-link from VSA into the CMS fleet view, VSA returns a URL pointing to the CMS UI with pre-filled query params (`?vin=...&view=telemetry`). CMS UI authentication is the user's responsibility.

---

## 3. What VSA Consumes from ADP

### 3.1 Warranty coverage

**Source.** ADP `guidance-for-recall-warranty-management`.

**Consumer.** VSA reused VFO `RecallWarrantyAgent`, invoked by VSA Supervisor and Booking Agent.

**Contract.** Existing VFO tool `check_warranty(vin, component, date)` — no changes required. VSA passes the driver's VIN and the component implicated by the current triage (e.g., `coolant_sensor` when DTC P0217 is active).

**Returns:**

```json
{
  "vin": "...",
  "component": "coolant_sensor",
  "covered": true,
  "coverageType": "powertrain-5yr-60k",
  "expiresAt": "2028-03-15T00:00:00Z",
  "deductible": 0,
  "dealerNetwork": ["preferred", "authorized"]
}
```

### 3.2 Service history

**Source.** ADP service history tables + `guidance-for-virtual-fleet-operator` KB (service invoices, work orders).

**Consumer.** VSA Booking Agent (to populate STAR-JSON ticket with recent service context) and reused VFO `MaintenanceAgent`.

**Access.** Same tools already used by VFO (`get_service_history(vin, lookback_days)`). No new interface.

### 3.3 Parts catalog

**Source.** ADP parts data (used by VFO cost and recall/warranty agents).

**Consumer.** VSA Booking Agent — resolves DTC → likely-needed parts so the STAR-JSON ticket includes a parts-planning hint for the receiving dealer/service center.

**Contract.** Existing tool `lookup_parts_by_component(make, model, year, component)`. Read-only.

### 3.4 Customer 360

**Source.** ADP `guidance-for-agentic-customer-360`.

**Consumer.** VSA `AuthContextAgent` — resolves caller phone/account → customer record → associated vehicles.

**Contract.** New ADP-side tool (may need to be added to the C360 agent surface):

```
GET /customer360/by-phone?phoneNumber=+15551234567&tenantId=michelin
→ 200 OK
{
  "customerId": "cust-1234",
  "tenantId": "michelin",
  "name": "T. Hassan",
  "consentFlags": { "recording": true, "smsOutreach": true },
  "vehicles": [
    { "vin": "...", "vehicleId": "VEH-0025", "role": "assigned-driver" }
  ],
  "preferredServiceCenters": ["SVC-DAL-014"],
  "fleetContext": { "region": "TX", "downtimeBudgetHoursMonth": 4 }
}
```

If this endpoint does not exist yet in C360, it is a v1 deliverable on the ADP side, owned by the C360 guidance team.

### 3.5 Predictive maintenance signals

**Source.** ADP `guidance-for-predictive-maintenance`.

**Consumer.** VSA `TriageAgent` — if the vehicle has a pending predictive alert (e.g., slow tire leak trending over 7 days), the triage classifier escalates the current session by one tier (P3 → P2, P2 → P1) to reflect cumulative risk.

**Contract.** Existing predictive inference API or DynamoDB read; exact shape TBD based on predictive maintenance deployment. Fallback: triage proceeds without predictive enrichment.

### 3.6 VFO knowledge base

**Source.** ADP VFO KB described in `KNOWLEDGE_BASE_DESIGN.md`.

**Consumer.** VSA Supervisor + reused VFO specialists.

**Contract.** Bedrock KB ID is referenced by the VSA supervisor agent as a retrieval source. **Tenant scoping is layered on top** — see `VSA_TENANT_CONFIG.md` for per-tenant KB IDs and the overlay pattern.

---

## 4. What VSA Produces

### 4.1 STAR-JSON tickets

**Shape.** Defined in `VSA_STAR_JSON_SCHEMA.md`. Each session that triages to P1 or P0 produces one ticket; P2 optionally produces one; P3 does not.

**Sink.** Per tenant, configured in tenant config (see `VSA_TENANT_CONFIG.md`). Options:

| Sink type | Mechanism |
|---|---|
| SQS queue | VSA publishes to tenant-provided SQS queue ARN |
| Webhook | VSA POSTs JSON to tenant-provided HTTPS endpoint with HMAC signature |
| Salesforce Service Cloud | VSA uses Salesforce REST API with tenant-provided credentials |
| ServiceNow | VSA uses ServiceNow Incident API with tenant-provided credentials |
| DynamoDB (default fallback) | `vsa-tickets-{tenant}` table — tenant polls or subscribes via DynamoDB Streams |

**Reliability.** Every ticket is persisted to the DynamoDB fallback table before external delivery. Delivery failures retry with exponential backoff; permanent failures surface in the tenant's audit log and trigger an internal alarm.

### 4.2 Triage decisions

**Shape.**

```json
{
  "sessionId": "vsa-sess-a1b2c3",
  "tenantId": "michelin",
  "vin": "...",
  "classification": "P1",
  "reason": [
    { "input": "dtc.P0217", "weight": "primary", "tenantThreshold": "active_critical_dtc" },
    { "input": "telemetry.coolant_temp=118", "weight": "primary", "tenantThreshold": ">115" },
    { "input": "recall.26V123456.open=true", "weight": "escalator" }
  ],
  "decidedAt": "2026-04-27T14:22:19Z",
  "classifierVersion": "1.0.0"
}
```

**Sink.** DynamoDB `vsa-triage-decisions` (VSA-owned). Also published to EventBridge bus `vsa-events` for tenants that subscribe.

### 4.3 Escalation events

**Shape.**

```json
{
  "sessionId": "vsa-sess-a1b2c3",
  "tenantId": "michelin",
  "reason": "classification=P0",
  "channel": "voice",
  "tier2QueueArn": "arn:aws:connect:...:queue/tier2-michelin",
  "contextBundle": {
    "transcript": "...",
    "triageDecision": { ... },
    "vehicleSnapshot": { ... },
    "draftTicket": { ... }
  },
  "escalatedAt": "2026-04-27T14:22:45Z"
}
```

**Sink.** Connect contact attributes (for in-flight voice/chat handoff) + EventBridge `vsa-events`.

### 4.4 Audit log

Every classification, tool call, booking decision, and escalation is persisted to CloudWatch Logs under per-tenant log groups (`/aws/vsa/{tenantId}/{channel}`) with PII redaction enforced by a VSA-owned middleware Lambda. Retention follows tenant config (default 90 days).

### 4.5 Session recordings

Connect recordings go to a VSA-owned S3 bucket scoped per tenant. Access is tenant-gated via IAM conditions. Tenants with `consentFlags.recording: false` disable recording on session creation.

---

## 5. Authentication and Authorization Boundary

| Boundary | Mechanism |
|---|---|
| VSA → CMS (read) | Cross-account IAM role `vsa-cms-read`, scoped to specific Redis keys and DynamoDB tables listed in §2 |
| VSA → ADP (read) | Cross-account IAM role `vsa-adp-read`, scoped to warranty/service/parts/C360 tables and VFO KB retrieve action |
| VSA → Tenant ticket sinks | Per-tenant credentials stored in AWS Secrets Manager, prefix `vsa/tenant/{tenantId}/sink` |
| Caller → VSA (voice) | Phone number lookup against ADP C360 + voice biometrics optional |
| Caller → VSA (companion app) | Tenant SSO (Cognito federated to tenant IdP) or short-lived signed link from tenant's own portal |
| Tier-2 agent → VSA (agent-assist) | Connect agent auth + Cognito group `vsa-tier2-{tenantId}` |

No direct caller-initiated writes to CMS or ADP exist. All state changes VSA triggers are mediated by VSA-owned Lambdas, which enforce tenant scoping before writing.

---

## 6. Network and Data Residency

- VSA, CMS, and ADP are all deployed to **us-east-1**. Single-region, co-located — no cross-region hops, no cross-region Bedrock inference.
- VPC: VSA runs in its own VPC; access to CMS Redis via PrivateLink or VPC peering within the region.
- Connect instance region matches the data layer region.
- Per-tenant data residency is enforced via tenant config; tenants requiring non-us-east-1 residency are deferred to future phases.

---

## 7. Failure Modes and Degradation

| Failure | VSA behavior |
|---|---|
| CMS Redis unavailable | Session continues with stale-state warning; triage uses DTC + recall only; no real-time telemetry in reasoning drawer |
| CMS DTC table unavailable | Triage marked `DEGRADED`; classifier uses Redis DTC set only; session logged for later review |
| CMS recall service down | Triage proceeds; recall enrichment omitted; trace notes unavailability |
| ADP warranty unavailable | Booking proceeds; STAR-JSON ticket marked `warrantyCheck: pending`; dealer resolves on arrival |
| ADP C360 unavailable | Falls back to VIN-entry flow; caller is asked to state VIN or plate |
| VFO specialist agent unavailable | Supervisor falls back to direct tools; answer quality degraded, marked in trace |
| Tenant ticket sink down | DynamoDB fallback sink always captures; retry loop to external sink |
| Connect instance outage | Companion app unaffected; voice channel down until Connect recovers |

In all cases, a session that cannot classify with confidence above threshold auto-escalates to tier-2 rather than guessing.

---

## 8. Versioning and Change Management

- **Classifier version** — emitted with every triage decision; rolled forward via blue/green with side-by-side scoring on a replay corpus.
- **STAR-JSON schema version** — emitted as `schemaVersion` on every ticket; consumers branch on version.
- **Tenant config version** — every active tenant config has a version hash; every session records which version it resolved against.
- **Contract changes with CMS/ADP** — breaking changes require a coordinated release; non-breaking additions are opt-in via tenant config feature flags.

---

## 9. Open Integration Tasks

1. Confirm C360 exposes (or adds) `GET /customer360/by-phone` — tracked against ADP C360 team.
2. Confirm Redis cross-VPC access pattern with CMS (PrivateLink preferred over peering).
3. Confirm cross-account IAM pattern for VSA → CMS and VSA → ADP; assume three accounts (VSA, CMS, ADP).
4. Confirm predictive maintenance access pattern — API or direct DynamoDB.
5. Define the tenant onboarding runbook — one-pager covering IAM, Secrets, tenant config, KB build, Connect instance provisioning.
