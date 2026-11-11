# Virtual Fleet Operator — Agent & Knowledge Base Architecture

---

## Related: Virtual Service Agent (VSA)

VFO (this document) is **fleet-operator-facing**. VSA is the **customer-facing** layer that sits above it — tier-1 assistant for drivers, OEM owners, rental customers, and fleet managers across multiple tenants. VSA reuses the same specialist agents and knowledge base documented here, adding its own supervisor, triage classifier, and Amazon Connect channels. See the top-level [`README.md`](../README.md) and [`WELL_ARCHITECTED.md`](../WELL_ARCHITECTED.md) for the customer-facing surface, and the "Tier 2 Platform" section further down for the autonomous-agent layer that sits alongside VSA.

Top-level relationship:

```
    CUSTOMER (driver, owner, renter, fleet manager)
             │ voice / chat / companion app
             ▼
    ┌─────────────────────────────────────────┐
    │   VSA Supervisor (multi-tenant)          │
    │   • Triage (P0–P3, deterministic)        │
    │   • Booking (STAR-JSON)                  │
    │   • Auth/Context                         │
    └────────┬────────────────────────────────┘
             │ invokes
             ▼
    ┌─────────────────────────────────────────┐
    │   VFO Supervisor (fleet operator)        │  ← this document below
    │   + reused specialists                    │
    └─────────────────────────────────────────┘
```

---

## Agent Orchestration Flow

```
                    ┌─────────────────────────────────────┐
                    │         FLEET OPERATOR (Human)       │
                    │                                     │
                    │  "Why is VEH-0025 costing so much?" │
                    │  "Approve brake service for 12 vehs"│
                    │  "What does the warranty cover?"    │
                    └──────────────┬──────────────────────┘
                                   │
                    ┌──────────────▼──────────────────────┐
                    │      SUPERVISOR AGENT (VFO)          │
                    │      Amazon Bedrock — Claude Sonnet 4│
                    │      Role: SUPERVISOR                │
                    │                                      │
                    │  1. Classifies intent                │
                    │  2. Selects specialist(s)            │
                    │  3. Delegates with context            │
                    │  4. Synthesizes cross-domain answer   │
                    │  5. Writes actions to queue           │
                    │                                      │
                    │  Direct tools:                        │
                    │  • get_fleet_summary                  │
                    │  • get_vehicles                       │
                    │  • get_safety_events                  │
                    │  • get_data_counts                    │
                    │  • search_documents (KB)              │
                    └──┬─────────┬─────────┬─────────┬─────┘
                       │         │         │         │
          ┌────────────▼──┐ ┌───▼────────┐│┌────────▼────────┐
          │  COST AGENT    │ │ REBALANCING││ │ RECALL/WARRANTY │
          │                │ │   AGENT    │││    AGENT         │
          │ "Analyze costs │ │            │││                  │
          │  for VEH-0025" │ │ "Check     │││ "Is this repair  │
          │                │ │  Region X  │││  under warranty?" │
          │ Tools:         │ │  util"     │││                  │
          │ • service_hist │ │            │││ Tools:           │
          │ • warranty_clm │ │ Tools:     │││ • warranty_claims│
          │ • vehicles     │ │ • fleet_sum│││ • service_hist   │
          │ • search_docs  │ │ • vehicles │││ • dtc_codes      │
          └───────┬────────┘ │ • trips    │││ • search_docs    │
                  │          └─────┬──────┘│└────────┬─────────┘
                  │                │       │         │
                  │          ┌─────▼───────▼┐        │
                  │          │  MAINTENANCE  │        │
                  │          │    AGENT      │        │
                  │          │              │        │
                  │          │ "What DTCs   │        │
                  │          │  are active  │        │
                  │          │  on VEH-0025"│        │
                  │          │              │        │
                  │          │ Tools:       │        │
                  │          │ • maint_alrt │        │
                  │          │ • dtc_codes  │        │
                  │          │ • telemetry  │        │
                  │          │ • service_hst│        │
                  │          └──────┬───────┘        │
                  │                 │                 │
                  └────────┬───────┴────────┬────────┘
                           │                │
                           ▼                ▼
              ┌─────────────────┐  ┌─────────────────┐
              │  STRUCTURED DATA │  │ KNOWLEDGE BASE   │
              │  (DynamoDB)      │  │ (S3 PDFs)        │
              └─────────────────┘  └─────────────────┘
```

---

## Knowledge Base Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    KNOWLEDGE BASE (Amazon S3)                    │
│                    cms-prod-vfo-knowledge-base                   │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │                 SERVICE INVOICES (200 PDFs)              │    │
│  │                                                         │    │
│  │  INV-{serviceId}_{vehicleId}_{type}.pdf                 │    │
│  │                                                         │    │
│  │  ┌─────────────────────────────────────────────┐        │    │
│  │  │ FLEET SERVICE INVOICE                       │        │    │
│  │  │ Invoice #: INV-010d87a6                     │        │    │
│  │  │ Provider: Rush Truck Center — Dallas        │        │    │
│  │  │ Vehicle: VEH-0043 (Freightliner Cascadia)   │        │    │
│  │  │                                             │        │    │
│  │  │ PARTS & LABOR                               │        │    │
│  │  │ Starter Motor Assembly  P-STR-ASM  $285.00  │        │    │
│  │  │ Starter Solenoid        P-STR-SOL   $65.00  │        │    │
│  │  │ Labor (3.2 hrs @ $125/hr)          $400.00  │        │    │
│  │  │                                             │        │    │
│  │  │ Subtotal: $750.00                           │        │    │
│  │  │ Tax: $61.88                                 │        │    │
│  │  │ Warranty: -$0.00                            │        │    │
│  │  │ TOTAL: $811.88                              │        │    │
│  │  └─────────────────────────────────────────────┘        │    │
│  │                                                         │    │
│  │  Linked to: DynamoDB service-history record             │    │
│  │  (same serviceId, vehicleId, cost, provider)            │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │                 WORK ORDERS (66 PDFs)                    │    │
│  │                                                         │    │
│  │  WO-{serviceId}_{vehicleId}_{type}.pdf                  │    │
│  │  • Work order number, priority, assigned technician      │    │
│  │  • Inspection checklist (fluids, tires, brakes, etc.)    │    │
│  │  • Labor hours, cost, customer/tech signatures           │    │
│  │  Linked to: every 3rd service-history record             │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │                WARRANTY CLAIMS (72 PDFs)                 │    │
│  │                                                         │    │
│  │  {claimId}_{vehicleId}.pdf                              │    │
│  │  • Claim ID, status (PAID/OPEN/DENIED/UNDER_REVIEW)     │    │
│  │  • Component, failure code, mileage at failure           │    │
│  │  • Claim amount, paid amount, warranty limit             │    │
│  │  • Evidence summary from telemetry/predictive model      │    │
│  │  Linked to: DynamoDB warranty-claims record              │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │                PARTS LISTINGS (10 PDFs)                  │    │
│  │                                                         │    │
│  │  {service_type}_parts_catalog.pdf                       │    │
│  │  One per service type: OIL_CHANGE, BRAKE_PADS,          │    │
│  │  TRANSMISSION, STARTER_MOTOR, ALTERNATOR, etc.          │    │
│  │  • Part name, part number, list price, fleet price      │    │
│  │  • 15% fleet discount applied                           │    │
│  │  • Stock availability                                   │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │              FLEET OPERATIONS (5 Markdown docs)          │    │
│  │                                                         │    │
│  │  fleet-context/                                         │    │
│  │  ├── fleet-composition.md    (vehicles by fleet/make)   │    │
│  │  ├── fleet-kpis-and-targets.md (utilization, cost, etc)│    │
│  │  └── agent-glossary.md       (TCO, DTC, NHTSA, etc.)   │    │
│  │                                                         │    │
│  │  fleet-operations/                                      │    │
│  │  ├── cross-domain-escalation-playbook.md                │    │
│  │  │   Rule 1: Safety always wins                         │    │
│  │  │   Rule 2: Recall cascade → rebalance → cost → warr  │    │
│  │  │   Rule 3: Cost spike → check maintenance → DTC → tel│    │
│  │  │   Rule 4: Cross-domain plans require human approval  │    │
│  │  └── fleet-operations-handbook.md                       │    │
│  │      Daily ops, dispatch, incident response, reporting  │    │
│  └─────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
```

---

## Autonomous Monitoring & Action Pipeline

> **Retired.** This section describes the VFO pipeline, retired on 2026-09-06 (CMS spec
> `2026-09-05-cms-vfo-teardown`). Its action-queue table was exported and deleted on
> 2026-10-04/05 (CMS issue `2026-10-23-vfo-action-queue-last-references`). The Fleet
> Intelligence action queue replaces it: see
> [Fleet Intelligence Action Queue (Stage A)](#fleet-intelligence-action-queue-stage-a).

```
┌──────────────────────────────────────────────────────────────────┐
│                    AUTONOMOUS MONITORING                          │
│                                                                  │
│  Amazon EventBridge                                              │
│  ┌──────────────────┐  ┌──────────────────┐                     │
│  │ Every 6 hours    │  │ Daily 6 AM ET    │                     │
│  │ Fleet monitoring │  │ Morning briefing │                     │
│  └────────┬─────────┘  └────────┬─────────┘                     │
│           └──────────┬──────────┘                                │
│                      ▼                                           │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │              FLEET MONITOR (AWS Lambda)                    │   │
│  │              Timeout: 5 min | Memory: 512 MB              │   │
│  │                                                          │   │
│  │  For each domain (with 15s delay between calls):          │   │
│  │                                                          │   │
│  │  ┌─────────────┐  "Analyze critical maintenance issues"  │   │
│  │  │ Maintenance  │──────────────────────────────────────►  │   │
│  │  └─────────────┘                                    │     │   │
│  │  ┌─────────────┐  "Identify cost anomalies"        │     │   │
│  │  │ Cost         │──────────────────────────────►    │     │   │
│  │  └─────────────┘                              │    │     │   │
│  │  ┌─────────────┐  "Review safety events"      │    │     │   │
│  │  │ Safety       │────────────────────────►     │    │     │   │
│  │  └─────────────┘                         │    │    │     │   │
│  │  ┌─────────────┐  "Check utilization"    │    │    │     │   │
│  │  │ Utilization  │──────────────────►      │    │    │     │   │
│  │  └─────────────┘                    │    │    │    │     │   │
│  │  ┌─────────────┐  "Review warranty" │    │    │    │     │   │
│  │  │ Warranty     │────────────►       │    │    │    │     │   │
│  │  └─────────────┘              │    │    │    │    │     │   │
│  │                               ▼    ▼    ▼    ▼    ▼     │   │
│  │                        SUPERVISOR AGENT                  │   │
│  │                     (delegates to specialists)            │   │
│  │                               │                          │   │
│  └───────────────────────────────┼──────────────────────────┘   │
│                                  ▼                               │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │              ACTION QUEUE (DynamoDB)                       │   │
│  │              (retired 2026-10-05)                          │   │
│  │                                                          │   │
│  │  ┌────────┬────────────┬──────────┬───────────────────┐  │   │
│  │  │ Status │ Domain     │ Priority │ Agent Response     │  │   │
│  │  ├────────┼────────────┼──────────┼───────────────────┤  │   │
│  │  │PENDING │Maintenance │ HIGH     │ "VEH-0034 has     │  │   │
│  │  │        │            │          │  critical brake    │  │   │
│  │  │        │            │          │  alert. Recommend  │  │   │
│  │  │        │            │          │  immediate service"│  │   │
│  │  ├────────┼────────────┼──────────┼───────────────────┤  │   │
│  │  │PENDING │Cost        │ MEDIUM   │ "VEH-0006 battery │  │   │
│  │  │        │            │          │  replacement $662  │  │   │
│  │  │        │            │          │  — check warranty" │  │   │
│  │  ├────────┼────────────┼──────────┼───────────────────┤  │   │
│  │  │APPROVED│Safety      │ HIGH     │ "VEH-0049 speed   │  │   │
│  │  │        │            │          │  violations — flag │  │   │
│  │  │        │            │          │  for retraining"   │  │   │
│  │  ├────────┼────────────┼──────────┼───────────────────┤  │   │
│  │  │REJECTED│Utilization │ MEDIUM   │ "Transfer 3 vehs  │  │   │
│  │  │        │            │          │  from FLEET-005"   │  │   │
│  │  └────────┴────────────┴──────────┴───────────────────┘  │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                  │                               │
│                                  ▼                               │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │              FLEET COMMAND CENTER (UI)                     │   │
│  │                                                          │   │
│  │  ┌─────────────────────────────────────────────────────┐ │   │
│  │  │ Pending Actions                          12 pending │ │   │
│  │  │                                                     │ │   │
│  │  │ ⚠ HIGH  Maintenance  2026-03-31 19:36              │ │   │
│  │  │ VEH-0034 has critical brake alert...               │ │   │
│  │  │                          [Approve] [Reject]         │ │   │
│  │  │                                                     │ │   │
│  │  │ ℹ MED   Cost         2026-03-31 19:37              │ │   │
│  │  │ VEH-0006 battery replacement — check warranty...   │ │   │
│  │  │                          [Approve] [Reject]         │ │   │
│  │  └─────────────────────────────────────────────────────┘ │   │
│  │                                                          │   │
│  │  Operator clicks [Approve] → status = APPROVED           │   │
│  │  Operator clicks [Reject]  → status = REJECTED           │   │
│  │  Action moves to Agent Activity feed                      │   │
│  └──────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘
```

---

## Cross-Domain Reasoning Example

```
Operator: "VEH-0025 had a starter motor repair last month. Was it covered under warranty?"

┌─────────────────────────────────────────────────────────────────┐
│ SUPERVISOR receives question                                     │
│                                                                 │
│ Intent: warranty eligibility check + service history lookup      │
│ Delegates to: Recall/Warranty Agent + Cost Agent                 │
└──────────┬──────────────────────────────────┬───────────────────┘
           │                                  │
           ▼                                  ▼
┌─────────────────────┐            ┌─────────────────────┐
│ RECALL/WARRANTY AGENT│            │ COST AGENT           │
│                     │            │                     │
│ 1. get_service_hist │            │ 1. search_documents │
│    (VEH-0025)       │            │    (service-invoices│
│    → Found: starter │            │     VEH-0025)       │
│    motor, $662,     │            │    → Found: INV-    │
│    no warranty      │            │    0ad0d582, $662   │
│                     │            │    Rush Truck Center│
│ 2. get_warranty_clms│            │                     │
│    → No claim filed │            │ 2. get_service_hist │
│                     │            │    → Provider: Rush │
│ 3. get_dtc_codes    │            │    Truck Center     │
│    (VEH-0025)       │            │    Dallas, 38922 mi │
│    → P0520 oil      │            │                     │
│    pressure sensor   │            │                     │
│                     │            │                     │
│ Response:           │            │ Response:           │
│ "No warranty claim  │            │ "Invoice INV-       │
│  was filed. Vehicle │            │  0ad0d582 shows     │
│  at 38,922 mi,      │            │  $662 at Rush Truck │
│  Ram warranty is    │            │  Center. No warranty│
│  36,000 mi — likely │            │  discount applied." │
│  out of coverage."  │            │                     │
└──────────┬──────────┘            └──────────┬──────────┘
           │                                  │
           └──────────────┬───────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│ SUPERVISOR synthesizes:                                          │
│                                                                 │
│ "VEH-0025's starter motor repair on 2024-08-29 cost $662 at    │
│  Rush Truck Center Dallas. No warranty claim was filed. The     │
│  vehicle was at 38,922 miles — Ram's powertrain warranty covers │
│  up to 36,000 miles, so this repair was ~2,900 miles past       │
│  coverage. However, I found DTC P0520 (oil pressure sensor)     │
│  which may indicate a related issue. Recommendation: review     │
│  whether the starter failure was caused by the oil pressure     │
│  issue — if so, it may qualify for a goodwill warranty claim."  │
│                                                                 │
│ → Writes to Action Queue:                                        │
│   ACT-WARR-xxxx | PENDING | "Review VEH-0025 for goodwill      │
│   warranty claim — starter failure potentially related to       │
│   DTC P0520"                                                    │
└─────────────────────────────────────────────────────────────────┘
```

---

## Structured ↔ Unstructured Data Integration

```
┌─────────────────────────────────────────────────────────────────┐
│                                                                 │
│   STRUCTURED (DynamoDB)              UNSTRUCTURED (S3 PDFs)     │
│                                                                 │
│   service-history table              service-invoices/          │
│   ┌──────────────────┐              ┌──────────────────┐       │
│   │ serviceId: 0ad0  │──────────────│ INV-0ad0_VEH-    │       │
│   │ vehicleId: 0025  │   same IDs   │ 0025_starter.pdf │       │
│   │ cost: $662       │──────────────│ Total: $662      │       │
│   │ provider: Rush   │              │ Parts: P-STR-ASM │       │
│   │ type: STARTER    │              │ Labor: 2.5 hrs   │       │
│   └──────────────────┘              └──────────────────┘       │
│          │                                    │                 │
│          │  Agent queries DDB                 │  Agent searches │
│          │  for quick lookups                 │  S3 for details │
│          │                                    │                 │
│          ▼                                    ▼                 │
│   "VEH-0025 had a                    "The invoice shows        │
│    starter repair,                    part P-STR-ASM at        │
│    $662, at Rush                      $285 + solenoid          │
│    Truck Center"                      P-STR-SOL at $65         │
│                                       + 2.5 hrs labor"         │
│                                                                 │
│   warranty-claims table              warranty-claims/           │
│   ┌──────────────────┐              ┌──────────────────┐       │
│   │ claimId: CLM-988 │──────────────│ CLM-2026-988_    │       │
│   │ vehicleId: 0005  │   same IDs   │ VEH-0005.pdf     │       │
│   │ status: PAID     │──────────────│ Status: PAID     │       │
│   │ amount: $947     │              │ Evidence: DTC    │       │
│   │ component: DEF   │              │ P20EE at 26881mi │       │
│   └──────────────────┘              └──────────────────┘       │
│                                                                 │
│   Vehicle detail page:                                          │
│   Service tab → "View PDF" → opens invoice in modal             │
│   Documents page → browse all PDFs by category                  │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## Agent Decision Matrix

| Operator Question | Primary Agent | Secondary Agent(s) | Data Sources | Action Generated |
|---|---|---|---|---|
| "Why is VEH-0025 expensive?" | Cost | Maintenance | service_history, invoices, DTC | Review warranty eligibility |
| "Which vehicles need service?" | Maintenance | — | maintenance_alerts, DTC | Schedule critical repairs |
| "Is this repair under warranty?" | Recall/Warranty | Cost | warranty_claims, service_history, claim PDFs | File warranty claim |
| "Why is Region X underperforming?" | Rebalancing | Cost, Maintenance | vehicles, trips, alerts | Transfer vehicles |
| "What happened to VEH-0049?" | Maintenance | Safety | DTC, telemetry, safety_events | Driver retraining |
| "Show me the invoice for..." | Cost | — | S3 invoices (PDF) | — (informational) |
| "What are our fleet KPIs?" | Supervisor (direct) | — | fleet-context/KPIs.md | — (informational) |
| "What should I focus on today?" | Supervisor | All 4 specialists | All tables + KB | Prioritized action list |

---

## AVX-on-foundation v1 — Persona grounding

**Overview:** AVX v1 grounds its 6 personas (3 existing + 3 expansion) in ADP Foundation v1 data products via two grounding mechanisms — **AgentCore Memory metadata-tagged long-term memory (LTM)** for tabular data and **Bedrock Knowledge Base cross-account Retrieve** for reference documentation. Session initialization pre-fetches data from ADP tables via cross-region Athena, seeding an AgentCore Memory resource that persists for the session lifetime. All persona tools query Memory or KB at tool-invocation time, eliminating per-turn live queries.

### Architecture Delta (AVX-on-foundation v1)

```
┌─────────────────────────────────────────────────────────────────────┐
│ AVX us-west-2 (123456789012)                                        │
│                                                                     │
│  AgentCore Runtime (vsa_supervisor_bidi_staging)                    │
│   └─ session-init:                                                  │
│      ├─ persona_loader.load_persona(persona_id, vehicle_id, …)     │
│      │   ├─ Athena query (cross-region us-west-2→us-east-1)        │
│      │   │  (vs. old: DDB scan adp-{stage}-vehicle-identity)       │
│      │   └─ → AgentCore Memory CreateEvent (us-west-2, colocal)    │
│      │        metadata: persona_id, vehicle_id, data_source, …     │
│      └─ agent.run() [Nova Sonic v2 + tool dispatch]:               │
│         ├─ Tabular tools (vehicle_identity, service_history, …)    │
│         │  └─ RetrieveMemoryRecords(metadataFilters={…})           │
│         │     (vs. old: DynamoDB direct read)                      │
│         │                                                           │
│         └─ Reference tools (retrieve, parts_lookup, …)             │
│            └─ bedrock:Retrieve(kbArn=adp-staging-vehicle-…)        │
│               (cross-account, cross-region us-west-2→us-east-1)    │
│               (vs. old: local markdown fixtures)                   │
│                                                                     │
│  New infrastructure:                                                │
│  • AgentCore Memory (us-west-2) — vsa-{stage}-cvx-foundation-mem   │
│  • Athena workgroup (us-east-1) — cvx-{stage}-analytics            │
│  • Athena results S3 bucket (us-east-1) — region-suffixed          │
└──────────────────────────────────────────────────────────────────────┘
                             │
                             │ cross-region
                             │ (same-account;
                             │ cross-account-API
                             │ patterns for
                             │ customer deploys)
                             ▼
┌──────────────────────────────────────────────────────────────────────┐
│ ADP us-east-1 (123456789012)                                        │
│                                                                     │
│  Glue catalog (adp_staging_*)                                       │
│   • 6 in-scope databases w/ Iceberg tables                          │
│   • LF v4 wildcard cross-account share to AVX account               │
│                                                                     │
│  Bedrock Knowledge Base                                             │
│   • adp-staging-vehicle-knowledge                                   │
│   • Cross-account resource policy (principals: AVX roles)           │
│                                                                     │
│  Data lake (S3)                                                     │
│   • adp-staging-foundation-lake-* (SSE-KMS encrypted)              │
└──────────────────────────────────────────────────────────────────────┘
```

### Per-Persona Data Flow

| Data Product | Fleet Driver | Consumer | Retail Advisor | Vehicle Owner | Service Advisor | Technician |
|---|:--:|:--:|:--:|:--:|:--:|:--:|
| **Tabular (LTM)** |
| `vehicle_identity` | ✓ LTM | ✓ LTM | ✓ LTM | ✓ LTM | ✓ LTM | ✓ LTM |
| `service_records` | ✓ LTM | — | — | ✓ LTM | ✓ LTM | ✓ LTM |
| `charging_sessions` | ✓ LTM | ✓ LTM | — | ✓ LTM | — | — |
| `customer_360` | — | ✓ LTM | ✓ LTM | ✓ LTM | ✓ LTM | — |
| `customer_interactions` | — | ✓ LTM | ✓ LTM | — | ✓ LTM | — |
| `ota_campaigns` | ✓ LTM | ✓ LTM | — | ✓ LTM | — | — |
| `vehicle_telemetry_aggregated` | ✓ LTM* | — | — | ✓ LTM* | — | — |
| `energy_usage` | ✓ LTM* | ✓ LTM* | — | — | — | — |
| **Reference (KB)** |
| `vehicle_knowledge_base` | — | — | — | — | ✓ KB (TSB/policy/manual) | ✓ KB (DTC/parts/SOP) |

*PySpark-gated: table placeholder in v1; tool returns "not yet available" until `PySpark via Glue` spec ships and ADP extends the cross-account LF share to include the source databases.

### Cross-Account Topology

**Staging (v1):** single AWS account 123456789012, two regions:
- AVX: us-west-2 (AgentCore Runtime, Memory, Nova Sonic inference)
- ADP: us-east-1 (Glue catalog, Bedrock KB, data lake)

**Cross-account-API patterns wired from day 1:**
- Lake Formation v4 cross-account share grants AVX's AgentCore execution role `lakeformation:GetDataAccess` on the 6 in-scope ADP databases
- Bedrock KB resource policy explicitly authorizes AVX role ARN for `bedrock-agent-runtime:Retrieve`
- All cross-account credentials are temporary (STS AssumeRole via LF + LF-issued session tokens for S3, or direct IAM Bedrock permission)

**Customer multi-account deploy:** change CDK context:
- `-c adpAccountId=<ADP-account>` (AVX side)
- `-c cvxAccountId=<AVX-account>` (ADP side)
- `-c cvxKbPrincipals=<role-ARN-list>` (ADP side, for KB resource policy)

The same code path supports both same-account and cross-account; only the ARNs and IAM trust boundaries change.

---

## AgentCore Memory Schema

**Resource:** `vsa-{stage}-cvx-foundation-memory` (single Memory resource, shared across all 6 personas)

**Indexed keys** (created at Memory resource creation time; immutable):

| Key | Type | Purpose |
|---|---|---|
| `persona_id` | STRING | Route by persona (e.g., `fleet_driver`, `service_advisor`) |
| `vehicle_id` | STRING | Scope tabular reads to a specific VIN |
| `customer_id` | STRING | Scope customer-derived data by customer ID |
| `data_source` | STRING | Filter by ADP product origin (e.g., `vehicle_identity`, `service_records`, `ota_campaigns`) |
| `last_updated` | NUMBER | Track freshness; enable TTL-based lazy-refresh on miss (v1 default: 15 min, configurable) |
| `tenant_id` | STRING | Multi-tenant isolation (future-proofing for customer deploys) |

**Namespace strategy:** `cvx/{tenant_id}/{persona_id}` — semantic + metadata filters isolate reads further.

**Record creation:** deterministic-metadata writes via either `bedrock-agentcore-control:CreateEvent` (with `metadata` parameter) or `bedrock-agentcore-control:BatchCreateMemoryRecords` (chosen based on Group 1.A API research; see `decisions.md`). All metadata is supplied by `persona_loader.py` at session-init ingest time — no LLM extraction. This ensures exact-match retrieval for tabular data and deterministic session-to-data binding.

**Capacity notes:**
- 6 indexed keys; max quota is 10 per memory — comfortable headroom for v1.1 additions
- Per-session data footprint: ≤100 KiB (all 9 ADP products, all filter rows, JSON-serialized) — well under typical AgentCore session storage
- TTL + lazy-refresh strategy: 15-minute default window covers typical session lifetime (avg ~8 min for Nova Sonic bidi) with cache hit for repeated queries within a session

**Future warning** (for implementers): the `bedrock-agentcore-control` API surface evolved in May/June 2026. Verify that the chosen API (`CreateEvent` vs `BatchCreateMemoryRecords`) matches the deployed boto3 SDK version. See `decisions.md Q1/R2` for spike resolution.

---

## Data Sources — v1 Update

**Pre-v1 (seed-data model):** Tools read from AVX-owned DynamoDB tables (`adp-{stage}-*` stubs) or local markdown fixtures seeded by `scripts/seed-personas.py`, `scripts/seed-service-network.py`, `scripts/bootstrap-local-kb.py`.

**v1 (ADP-grounded model):**
- **Tabular data:** replaced by AgentCore Memory LTM + metadata-filtered Retrieve. Session-init persona-loader populates Memory from cross-region Athena queries against ADP Glue tables. Tools no longer read DynamoDB; they read from Memory via `bedrock-agentcore-control:RetrieveMemoryRecords(metadataFilters={...})`.
- **Reference data:** replaced by Bedrock KB cross-account Retrieve. Tools call `bedrock:Retrieve(kbArn=adp-staging-vehicle-knowledge-base, filter={source_category:...})` instead of local markdown search.
- **Unchanged:** AVX-owned writes (e.g., booking, escalation actions) still go to CMS DynamoDB; AVX-owned rules (triage, DTC lookup logic) still use local catalogs.

**Graceful degradation:** if Memory returns no records or KB is unavailable, tools return structured "not yet available" messages — sessions degrade but don't break. PySpark-gated products explicitly return "pending prerequisite" messages with tracking link.

---

## Tool Dispatch — v1 Repointing

| Tool | Pre-v1 Source | v1 Source | Notes |
|---|---|---|---|
| `vehicle_identity` | DDB scan | Memory LTM (filter: `vehicle_id`) | All personas |
| `service_history` | n/a (new) | Memory LTM (filter: `vehicle_id`, `data_source=service_records`) | Tabular, 12 most-recent |
| `charging_history` | n/a (new) | Memory LTM (filter: `vehicle_id`, `data_source=charging_sessions`) | Tabular, 30-day window |
| `customer_profile` | n/a (new) | Memory LTM (filter: `customer_id`, `data_source=customer_360`) | Latest snapshot |
| `interaction_history` | n/a (new) | Memory LTM (filter: `customer_id`, `data_source=customer_interactions`) | 50 most-recent |
| `ota_history` | n/a (new) | Memory LTM (filter: `vehicle_id`, `data_source=ota_campaigns`) | 6-month window, joined header+events |
| `retrieve` | Local markdown | Bedrock KB Retrieve (cross-account) | Reference grounding; KB filter by `source_category` |
| `parts_lookup` | Local DDB stub | Bedrock KB Retrieve (filter: `source_category=parts_catalog`) | KB chunks reference the ADP parts ontology |
| `knowledge` | Local markdown | Bedrock KB Retrieve (filter: `source_category=owner_manual,service_policy,charging_narrative`) | Persona-specific filter selection |
| `diy_repair_advisor` | Local catalog | Local catalog + KB Retrieve augmentation (filter: `source_category=dtc_guide`) | Hybrid: local rules + KB enhancement |
| `triage` | Local rules | Local rules (unchanged) | Rules-based, not data-grounded |
| `book` | CMS DDB write | CMS DDB write (unchanged) | AVX-to-CMS integration unchanged |
| `escalate` | Local rules | Local rules (unchanged) | Rules-based, not data-grounded |
| `find_service_center` | Local seed | Local seed (unchanged) | v2 candidate; ADP integration pending |

---

## Tier 2 Platform — Autonomous Vehicle Health Reasoning

### The Two-Tier Split

AVX implements a two-tier agent architecture: Tier 1 conversational agents narrate and route inside a strict latency budget (~1s for voice); Tier 2 autonomous agents run off-conversation on a schedule, doing the multi-step reasoning and cross-account data joins that would blow the Tier 1 budget, and writing evidence-carrying artifacts that Tier 1 then reads via fast lookups. This section covers how the pattern is applied to vehicle health findings.

**Tier 1: Conversational Agent (synchronous, latency-bound)**
- The supervisor agent and its tools live here — they read and respond in the ~1 second latency budget for voice (Amazon Nova Sonic v2 bidi)
- The `health_findings()` tool is a fast read: a single-partition DynamoDB Query, no LLM in the path
- Returns the latest health findings per component, with evidence for deep-dive conversation

**Tier 2: Autonomous Platform (scheduled or on request, nobody waiting)**
- Runs on EventBridge Scheduler, invoked once per component; the one exception is the Stage D quality
  agent's `investigation` component, which runs from an SQS queue when a product engineer asks. Three components run daily from
  ADP's batch (battery, tires, brakes); `diagnostics` runs every 5 minutes from CMS's active fault
  codes, because a fault that clears should stop being advertised within minutes, not the next day.
  The Fleet Intelligence `cost` component runs daily at 04:00 UTC (see "Fleet Intelligence cost
  agent (Stage B)" below), the FI `recall` and `warranty` components at 05:00 and 05:20 UTC (see
  "Fleet Intelligence recall and warranty agent (Stage C)"), the FI fleet-health `service`
  component at 05:40 UTC, and the FI `rebalance` component at 06:00 UTC (see "Fleet Intelligence
  rebalancing agent (Stage E)"). <!-- verify: grep -cE "\{ component: '[a-z]+', +hour: '" infrastructure/lib/vsa-tier2-stack.ts (daily, prints 8); grep -c "ScheduleExpression.rate(" infrastructure/lib/vsa-tier2-stack.ts (diagnostics, prints 1) -->
- Performs cross-account Athena queries against ADP curated data products
- Applies judgment: does this matter for this owner now, how urgent is it, should findings be bundled, what should be narrated
- Writes an artifact (DynamoDB item) carrying its own evidence for audit and later deep-dives
- **Critically: Tier 2 computes no model and derives no thresholds.** ADP infers; AVX judges. (See D3, D3a limitations below.)

**As deployed on staging, the health components' judgment path is deterministic.** The runner
wires the deterministic assessment function for battery, tires, brakes and diagnostics; the
Strands/Bedrock enrichment path exists in the codebase but has no caller for them, so their run
records report `tokens_used=0` and `estimated_cost_usd=0.0`, correct values, not a broken meter.
The FI `cost` component in the same Lambda does call Bedrock (one Converse call per changed
vehicle, through the role's Sonnet 4.6 grants), and its run records carry real token counts; see
"Fleet Intelligence cost agent (Stage B)". The FI `recall` and `warranty` components do too (one
or two calls per changed booking or claim within the per-run caps; Stage C), and the FI `rebalance`
component does (one call per metro with a candidate plus at most one retry, at most 12 per run;
Stage E). <!-- verify: grep -n "FI_REBAL_MAX_MODEL_CALLS:" infrastructure/lib/vsa-tier2-stack.ts; grep -n "max_retries=1" agents/fi_rebalancing/llm.py --> The tier split does not depend on an
LLM being in the Tier 2 path — it depends on nobody waiting.

### Why This Matters

Reasoning over substantial data — millions of rows, cross-account queries, multiple dependent
steps — does not fit inside a ~1 second voice turn. The Tier 1 tool reads a pre-computed artifact
in milliseconds. The separation prevents voice latency degradation, enables batch-economic
reasoning, and decouples the reasoning lifecycle from the conversation lifecycle — one update per
day vs. one per user request.

### Artifact Contract (M4)

Tier 2 writes findings to `vsa-{stage}-health-findings` DynamoDB table. Each item carries:

| Field | Purpose |
|---|---|
| `pk` = `VEH#{vin}` | Partition key — all findings for a vehicle in one partition |
| `sk` = `FINDING#{component}#{inputs_hash}` | Sort key, **content-addressed**: identical inputs produce identical sort key, enabling idempotency. The component is carried here rather than in a separate attribute, which is why a Tier 1 read filters by `sk` prefix |
| `computed_at` | ISO-8601 UTC; returned by Tier 1 read so agents can say "as of last Tuesday" |
| `confidence` | `high` \| `medium` \| `low` |
| `evidence[]` | Array of input signals and intermediate findings that produced the conclusion — the audit trail |
| `agent_version` | Which reasoning version produced this artifact |
| `inputs_hash` | SHA-256 over normalised ADP input rows — the idempotency key |
| `finding` | Plain-language conclusion, owner-facing |
| `severity_hint` | **Advisory only**, canonical vocabulary: `routine` \| `soon` \| `attention`. **Explicitly NOT** a P-level triage classification — it informs scheduling urgency, not safety classification. See deterministic seams below. |
| `ttl` | DynamoDB expiry attribute; findings age out after configured TTL |

**Idempotency**: Re-running Tier 2 over unchanged ADP data produces the same `inputs_hash` and sort key. The writer performs a conditional put that does not create a second row, making daily schedules safe to retry.

### Tier 2 Components: Sources and Limitations

#### Battery Health

**Source**: ADP `energy_usage` product (age-correlated state-of-health, ~2% decline per year)

For each VIN the check reads the newest `energy_usage` row by `usage_date` (then `battery_age_days`),
not the row with the largest battery age. For CMS managed cars ADP computes that row's battery age
from the car's in-service date and its health from the car's starting health (CMS spec
`2026-10-22-vehicle-data-consistency` § D2), and CMS's vehicle page shows the same row's values,
so the finding and the page agree.
<!-- verify: grep -n 'ORDER BY usage_date DESC' agents/health/adp_battery_source.py -->

**Limitation — Two identically-named columns, one wrong**: ADP exposes `state_of_health_pct` in both `energy_usage` (the correct source, ~100% new → ~86% at 7 years, varies per vehicle) and `vehicle_telemetry_aggregated` (generated from a global row index, ~95→90, effectively identical for all vehicles). Reading the telemetry column would make per-owner judgment impossible while looking superficially correct. Tier 2 explicitly queries `energy_usage`; this is test-enforced.

**Limitation — Training label, not model**: ADP's own schema describes `needs_replacement` (in the tire product) as a "supervised label" — an ML training target, not a deployed model's inference output. Consuming it as though it were a prediction is a category slip. It is acceptable here because the value is still computed by the data platform rather than by AVX, so the doctrinal seam holds where it matters. The upgrade path is ADP publishing predictions as a curated, catalogued product.

**Honest output on this data is reassurance, not alarm**: No synthetic vehicle crosses a typical ~70% EV battery warranty floor at ~86% age-7-year degradation. Battery findings deliver context ("your winter range drop is normal at this temperature") rather than urgency. This is the point, not a gap.

#### Tire Health

**Source**: ADP `tire_health` product (`needs_replacement`, `wear_category`, `wear_rate_mm_per_1k_km`)

**Limitation — Training label**: As above — `needs_replacement` is a supervised label. Acceptable for the same reason; ADP computes it.

#### Brake Service History

**Source**: ADP `service_records` filtered to `service_type='brake_service'` — service date, DTCs, complaint, warranty coverage, cost, and open/resolved status

**Limitation — No trend analysis**: ~95% of vehicles (452,446 of 475,657 measured live) have exactly one brake record. A trend is not computable; findings surface recent work and open issues, not wear progression.

**Limitation — KB grounding is partial**: Of 10 ADP brake DTC codes, only `C0161` and `C0040` have deployed Bedrock KB guides. Roughly 1 in 9 brake findings can be grounded in published content. Tier 2 reports other codes but cannot explain them — it does not invent explanations.

**Safety boundary — P0 verbatim**: ADP's KB defines `C0161` (critical brake pressure loss) as P0 with the remedy *"Critical brake pressure loss detected. Pull over immediately."* Where this phrase appears, it is narrated **verbatim**. An agent that preserves the verdict while softening it has crossed the boundary while appearing to respect it. This is test-enforced, not prompt-instructed.

### Tier 1 Read Surface

`health_findings()` is a Tier 1 tool registered on fleet_driver, vehicle_owner, consumer, and service_advisor personas — **not** on the anonymous kiosk (a kiosk visitor owns no vehicle). The tool:
- Queries the DynamoDB table by `pk = VEH#{vin}`, retrieves newest per component via `max(computed_at)`
- Returns `finding`, `computed_at`, `confidence`, `severity_hint`, and `evidence[]` on request
- **Contains no LLM** — a single-partition Query with no reasoning in the Tier 1 path
- The no-LLM property is test-enforced two ways: statically, and by intercepting calls at runtime so a future edit that introduces an LLM call fails the test rather than quietly spending the latency budget. Note this asserts *no LLM in the path*, which is the structural cause of Tier 1 latency — it is not a wall-clock latency assertion, and no test measures response time

### Fleet Intelligence Action Queue (Stage A)

Spec `2026-10-02-fi-action-queue-stage-a`; the contract CMS builds against is
`docs/fi-action-queue-contract.md` (v2.5). Stage A ships the queue's plumbing; the Fleet
Intelligence agents arrive in later stages, the cost agent first (Stage B, below).

- **Queue item.** A Finding with a `recommended_action` (contract § 1.1). It gets at most one
  decision Action, whose id is derived from the `finding_id`, so the first decision wins; a
  pending or expired item has none. The decision is `approved` or `overridden` (approve route),
  `declined` (decline route) or `auto_approved` (a rule). CMS derives the status an operator sees
  from the Finding and that Action (§ 3).
- **Routes** (`vsa-{stage}-api`): `POST /findings/{finding_id}/decline` is new; the approve route
  gains the queue-item rules in § 4; `GET /approval-rules/fleet/{fleet_id}` and `PUT`/`DELETE
  /approval-rules/fleet/{fleet_id}/{rule_id}` are served by the `vsa-{stage}-api-avx-approval-rules`
  Lambda (§ 6). `PATCH /actions/{action_id}` accepts an optional `note` but does not store it; a
  non-string note returns 400 `note_invalid` and one over 2,000 characters 400 `note_too_long`. A
  PATCH that moves an Action to `declined` records `failure_kind: target_refused` (the owner, as
  target, refused). Rules need fleet membership,
  with no `platform-admin` bypass, and are always created disabled.
- **Rule decider id.** A rule-approved Action's `decided_by.id` is `<fleet_id>/<rule_id>`:
  `fleet_id` is 1–64 of `A-Z a-z 0-9 _ -`, and `rule_id` is 1–128 characters with no `/`, no
  whitespace and no control characters. The whole id must also pass `str.isprintable()`.
  `registry.dispatch()` declines any other shape with `failure_kind: target_refused`
  (`agents/executors/registry.py` `_is_valid_rule_decider`).
- **Rule engine** (`vsa-{stage}-tier2`): the `vsa-{stage}-avx-auto-approver-{region}` Lambda reads the
  findings table's stream, filtered to items with a `recommended_action` and a `fleet_id`. On a
  match with an enabled rule in `vsa-{stage}-avx-approval-rules` it writes the decision Action
  (`decided_by.kind: rule`) and dispatches it. Executors forward the caller's token and a rule has
  none, so Stage A allows only `acknowledge_only` to be auto-approved (§ 6).
- **Guardrails.** Four checks in `registry.dispatch()` run before any executor, for every Action
  however it was created: G1–G3 refuse `exempt_recall`, `ground_vehicle` and
  `submit_warranty_claim` unless an operator decided; G4 refuses `complete_recall` unless an
  operator decided and a DMS repair order for the same VIN is `Ready`, `Delivered` or `Invoiced`.
  A refusal is `declined` with `failure_kind: target_refused`, the guardrail id and a sentence
  from a fixed vocabulary, and shows as `blocked` (§§ 10, 10a). After G1–G5 the same function
  re-checks the decider for the CMS fleet kinds: `apply_cost_action` needs an operator, and
  `move_vehicles` an operator or a well-formed rule decider; anything else is declined with
  `target_refused` and no guardrail id.
- **Executor redirects.** The CMS executor sends its POSTs with `allow_redirects=False` (they carry
  the operator's token) and treats any 3xx as a terminal error (`UnexpectedRedirect`), never a retry.
- **Run records** (`vsa-{stage}-avx-agent-runs`, pk `agent_component`, sk `started_at`): the
  Tier 2 health components write one per run and the evaluator one per batch, so CMS can show
  "last ran at" or "has not run yet" rather than an empty queue. Run-staleness alarms watch the
  scheduled components (§ 8). `started_at` is millisecond-precision ISO-8601 UTC
  (`...T02:00:06.881Z`). Each record is written with a conditional put
  (`attribute_not_exists(agent_component)`), so it never overwrites another run's record; on a
  collision it retries once at `started_at` + 1 ms. Records expire 400 days after `finished_at`
  (DynamoDB TTL). A reader that bounds `started_at` with a whole-second `...Z` string misses runs
  that started in that second (`.` sorts below `Z`): drop the `Z` from the bound.
- **CMS.** CMS main_api reads the findings, actions, run-record and rule tables directly within
  the caller's fleet scope, and forwards decisions and rule changes to this API with the caller's
  own token; this API checks the fleet a second time (§§ 7, 11, 11a). The browser never calls AVX.


### Fleet Intelligence cost agent (Stage B)

Spec `2026-10-02-fi-cost-stage-b`; contract `docs/fi-cost-contract.md`. The `cost` component
(`agent-fi-cost`) is a Tier 2 component of the same runner, scheduled daily at 04:00 UTC.

- **Inputs.** ADP's `adp_{stage}_fleet_cost` views, through the same cross-region Athena
  workgroup as the health components: `vehicle_cost_monthly` (cost per vehicle and month) and
  `vehicle_cost_baseline` (outlier flags against the vehicle's own months and its fleet-model
  cohort). ADP decides what is an outlier, with thresholds kept as configuration in ADP; the agent
  decides why and what to do. The role is denied ADP's `restricted/` prefix, where the planted
  anomalies' answer key lives.
- **Judgment.** One Bedrock Converse call (Sonnet 4.6) per vehicle whose evidence changed, at most
  one retry, then a deterministic rules fallback, so a model failure still writes a Finding. The
  model's headline and detail pass the `vsa-{stage}-fi-cost-guardrail` Guardrail (STANDARD topic
  tier through the US cross-Region guardrail profile, so each check runs in us-east-1, us-east-2
  or us-west-2). The estimated
  saving is a formula over the evidence, never model output. At most
  `/vsa/{stage}/avx/fi-cost/max-diagnoses-per-run` diagnoses per run (default 50); the rest wait for
  the next run. Unchanged vehicles cost no model call. <!-- verify: grep -n "^DEFAULT_MAX_DIAGNOSES_PER_RUN\|^MAX_DIAGNOSES_CEILING" agents/fi_cost/agent.py -->
- **Output.** VIN-scoped Findings, one per vehicle and data month: `fleet.cost.anomaly` with a
  cause and a recommended `apply_cost_action`, and `fleet.cost.sell_timing` when the vehicle's cost
  crossover is still 1 to 3 months ahead (`SELL_TIMING_HORIZON_MONTHS`). They go to the fleet's
  queue only: hidden from iOS and skipped by the push broker.
  <!-- verify: grep -n "SELL_TIMING_HORIZON_MONTHS" agents/fi_cost/sell_timing.py -->
- **Action.** An operator approves in CMS; AVX's `cms` executor calls CMS's
  `POST /api/v1/fleet-intelligence/cost-actions` with the operator's token
  (`/cvx/{stage}/cms-api-endpoint`), and the action counts as executed once CMS records it. Cost
  actions are operator-only.
- **Tier 1.** The `fleet_recommendation` tool (fleet_manager, text runtime) reads the stored
  Finding and its decision; it never recomputes. The text runtime trusts `custom:fleetIds`
  (`AVX_FLEET_IDS_CLAIM_TRUSTED=true`); the bidi runtime does not.
- **Run records and alarms.** Each run writes a record to `vsa-{stage}-avx-agent-runs`; the
  `vsa-{stage}-avx-run-stale-cost-<region>` alarm fires after 27 hours without one.
  <!-- verify: grep -n "component: 'cost', emptyHours" infrastructure/lib/vsa-tier2-stack.ts -->

### Fleet Intelligence recall and warranty agent (Stage C)

Spec `2026-10-05-fi-recall-warranty-stage-c`; contracts `docs/fi-recall-warranty-contract.md` (the two
Finding kinds) and `docs/fi-action-queue-contract.md` (Stage C's changes are v2.6: § 3 `cancelled`, § 4
server-set parameters per kind; the file is now at a later version). One agent, `agent-fi-recall-warranty`, runs as two components of the same Tier 2 runner:
`recall` at 05:00 UTC and `warranty` at 05:20 UTC. Both schedules and their alarms follow
`FI_RECALL_WARRANTY_LIVE` in `infrastructure/lib/fi-recall-warranty-live.ts`.
<!-- verify: grep -n "component: 'recall'\|component: 'warranty'" infrastructure/lib/vsa-tier2-stack.ts -->

- **Inputs.** ADP decides which vehicles a recall covers and whether it is remedied: the recall
  component reads `adp_{stage}_fleet_recall.recall_vehicle_status` joined with
  `recall_campaigns_received`. It also reads DMS's `parts_inventory` and `rooftops` from the DMS lake
  (stock and location per rooftop), CMS's fleet-locations and vehicle-groundings tables, and AVX's own
  actions table for earlier attempts. DMS decides warranty eligibility: the warranty component reads
  `dms_{stage}.warranty_candidates` (DMS's nightly candidates job) and `warranty_terms`.
  <!-- verify: grep -h -E 'FROM|JOIN' agents/fi_recall_warranty/sql/*.sql -->
- **Judgment.** For each booking or claim whose evidence changed: a Bedrock Converse call (Sonnet
  4.6) through Stage B's Guardrail (`FI_COST_GUARDRAIL_ID`; the runner refuses to start without it)
  and a validator, one retry if the attempt fails (a Converse error, no tool call, a validation or
  Guardrail failure; so at most two calls per item), then a deterministic rules fallback. `FI_RECALL_MAX_MODEL_CALLS` (60) and `FI_WARRANTY_MAX_MODEL_CALLS`
  (30) cap how many items per run take the model path, not writes: items past the cap are written on
  the rules path. An unchanged item costs no model call.
  <!-- verify: grep -n "_FI_RECALL_DEFAULT_CAP =\|_FI_WARRANTY_DEFAULT_CAP =" agents/tier2/runner.py -->
  Short-lived sibling Findings (`_VOLATILE_FINDING_KINDS` in `agents/fi_recall_warranty/attempts.py`,
  today only `health.diagnostics`) are left out of the evidence, so their churn doesn't change
  `inputs_hash`. A kind belongs in that set if and only if its `ttl_seconds` is 24 hours or less.
- **Output.** `fleet.recall.vehicle`: one Finding per vehicle, campaign and action (`book` or
  `ground`), with severity from a fixed band table and the campaign's safety text copied verbatim into
  `disclosure_txt`. `fleet.warranty.claim_candidate`: one per repair-order line, carrying DMS's
  amount and verdict.
- **Actions.** `book_recall_service` (DMS repair order with the recall line), `ground_vehicle` (CMS
  `POST /api/v1/fleet-intelligence/groundings`, with the operator's token) and
  `submit_warranty_claim` (DMS `POST /api/dms/fleet/warranty-claims`). All three need an operator;
  none can be auto-approved; their vehicle, campaign and repair-order fields are set by AVX from the
  Finding. `complete_recall` and `exempt_recall` stay non-executable: a recall is complete when a
  service record says so.
- **A booking DMS doesn't carry out.** AVX's DMS event consumer
  (`lambdas/api-avx-dms-event-consumer`) marks the Action `cancelled` when DMS closes the repair
  order from `Draft`, `InProgress` or `AwaitingParts` (`note: ro_closed_before_work`), and `completed`
  for `Ready`, `Delivered` or `Invoiced`. The next daily run writes a new attempt (a new Finding) for a
  cancelled, failed or refused booking while the vehicle is unremedied, after 14 days for a safety
  recall the operator declined (30 otherwise), at most 5 attempts (contract § 4).
- **Never on a phone.** The push broker (`agents/supervisor/lambdas/avx_broker.py`,
  `_is_fi_finding`) skips any Finding whose `agent_id` starts with `agent-fi-` or that carries a
  `recommended_action` (`skip_reason=fi_item`) before it resolves an owner.
- **Tier 1.** The `fleet_recommendation` tool returns a recall Finding's `disclosure_txt` as
  `safety_text`, byte for byte, and the prompt reads it first. Stage D's stop-driving faults get the
  same contract (below).
- **Contract copies.** CMS iOS and DMS carry copies of `finding.schema.json` and
  `action.schema.json`. `scripts/check-contract-copies.py` exits 0 only when every copy matches AVX's
  bytes (`CONTRACT_COPIES=1` runs it from `tests/schemas/test_contract_copies.py`).
- **Run records and alarms.** Each run writes a record to `vsa-{stage}-avx-agent-runs`; the
  `vsa-{stage}-avx-run-stale-recall-<region>` and `-warranty-` alarms fire after 27 hours without one.
  <!-- verify: grep -n "component: 'recall',   emptyHours\|component: 'warranty', emptyHours" infrastructure/lib/vsa-tier2-stack.ts -->

### Fleet Intelligence fleet health and quality agents (Stage D)

Spec `.kiro/specs/2026-10-05-fi-fleet-health-stage-d/`; contracts `docs/fi-fleet-health-contract.md`
(the three Finding kinds, their keys and rules) and `docs/fi-action-queue-contract.md` (Stage D's
changes are v2.7). Two agents run in the Tier 2 health agent Lambda (stack `vsa-{stage}-tier2`).

- **Fleet health** (`agent-fi-fleet-health`, component `service`): daily 05:40 UTC, every vehicle with
  an open need; above `TIER2_MAX_VINS_SERVICE` (500) the most severe go first, ties rotated daily by a
  VIN hash. Reads ADP's daily snapshot tables in `adp_{stage}_fleet_health`
  (`vehicle_fault_status_daily`, `pm_vehicle_status_daily`, `vehicle_utilisation_daily`, with
  `snapshot_runs` for freshness and `fault_codes`), `adp_{stage}_fleet_recall.managed_vehicles`, Agent
  2's and Stage C's Findings, DMS's `parts_inventory` and `rooftops`, and CMS's vehicles,
  fleet-locations and groundings tables (`agents/tier2/runner.py` `run_service`). Writes `fleet.health.service` Findings, VIN-scoped
  into the vehicle's fleet queue: a booking item (`book_fleet_service`, executed by DMS) for any open
  need, and for an open stop-driving (`p0`) fault a grounding item (`ground_vehicle` in its fault form,
  executed by CMS). A band table, not the model, sets severity and grounding. Run-staleness alarm
  `vsa-{stage}-avx-run-stale-service-<region>` (27 hours).
  <!-- verify: grep -n "component: 'service'" infrastructure/lib/vsa-tier2-stack.ts -->
- **Quality** (`agent-fi-quality`, component `investigation`): not scheduled. A product engineer starts
  an investigation from the Connected Services portal; CMS forwards the engineer's token to AVX
  `POST /investigations` (`vsa-{stage}-api-avx-investigations`, which also serves
  `GET /investigations/{investigationId}`). The route writes `vsa-{stage}-avx-investigations` and
  queues the request on SQS `vsa-{stage}-avx-investigations` (DLQ `-dlq`, two receives), which the
  health agent Lambda consumes one message at a time, two at once at most. It reads ADP's engineering,
  fleet-health, OTA-campaign and recall tables (`fault_patterns`, `vehicle_build_components`,
  `engineering_failure_modes`, `engineering_remedy_options`, `ota_campaigns` and others; the full list
  is in `agents/fi_quality/readers.py`) and writes population-scoped
  `quality.investigation` and `quality.issue` Findings, which are in no fleet queue. Only product
  engineers decide them; the `adp` executor sends the decision and the campaign issue to ADP's
  digital-thread writer, under guardrail G5. Alarms `vsa-{stage}-avx-investigations-dlq-depth-<region>`,
  `-investigations-mark-failed-error-` (the route) and `-investigations-hook-failure-` (the findings
  Lambda's `quality.issue` hook); no run-staleness alarm, since nothing runs without a request.
- **Switches** in `infrastructure/lib/fi-fleet-health-live.ts`: `FI_FLEET_HEALTH_LIVE` (the `service`
  schedule, the SQS event source, the `service` alarm's actions, and the analytics stack's Stage D Lake
  Formation grants; the investigation alarms act regardless), `FI_FAULT_GROUNDING_LIVE` (env
  `FAULT_GROUNDING_ENABLED`: grounding items) and `FI_FLEET_HEALTH_MODEL_LIVE` (env
  `FI_SERVICE_MODEL_ENABLED` and `FI_INVESTIGATION_MODEL_ENABLED`). The first two are `true` (staging
  since 2026-10-21 and 2026-10-23); the model switch is `false`, so both agents take their rules path
  and make no Bedrock call.
  <!-- verify: grep -n "export const" infrastructure/lib/fi-fleet-health-live.ts -->
- **Releasing a fault grounding.** CMS refuses return to service until ADP's `vehicle_fault_status`
  shows a `repair_ref` for every stop-driving code: a DMS repair order at `Ready`, `Delivered` or
  `Invoiced` with a labor op mapped to the code, dated on or after the code's latest sighting
  (contract § 4). Nothing lifts a grounding automatically.
- **Tier 1: the directive is read word for word.** For a `fleet.health.service` Finding with
  severity `p0` (booking and grounding items alike), `fleet_recommendation` returns the fault
  catalog's directive (`disclosure_txt`) as `safety_text`, byte for byte, as it does for recalls;
  items below `p0` and other kinds get none. The tool description and the fleet_manager prompt tell
  the assistant to read it first, unchanged. This is a prompt rule plus an eval, not a runtime
  guarantee: `scripts/live-verify-stop-driving-verbatim.py` runs the eval case
  `evals/cases/fi/fleet-health-stop-driving-text-001.yaml` against the deployed text runtime
  (issue `2026-10-27-stop-driving-verbatim-in-fleet-chat`).
- **Of ADP's databases, AVX reads only tables ADP's own jobs write, never `cms_ingest`** (user
  decision 2026-10-09).

### Fleet Intelligence rebalancing agent (Stage E)

Spec `.kiro/specs/2026-10-06-fi-rebalancing-stage-e/`; contract `docs/fi-rebalancing-contract.md`.

- **Identity.** Component `rebalance` / `agent-fi-rebalancing`, daily 06:00 UTC, after ADP's bookings and forecast jobs (04:15, 04:45 UTC) and its snapshot job (05:15 UTC) (contract § 1).
- **Inputs (contract § 1a).** ADP: `supply_demand_gaps_snapshot`, `move_outcomes_snapshot`, `fleet_bookings_forecast`, `vehicle_days`, `vehicle_utilization_daily`, `booking_requests`, `metro_events`, `service_records`; DMS: latest `repair_orders` image per RO; CMS: `fleet-locations` (metro, coordinates, type, parking, hours), vehicles `vin-index` and `vehicle-groundings`; AVX findings table `by_vin` index. Every Athena read pages `NextToken` to completion. A grounding that can't be read, a VIN without exactly one `vehicleId`, or any other unknown makes the vehicle ineligible.
  <!-- verify: grep -ohE 'FROM [a-z_${}]+\.[a-z_]+' agents/fi_rebalancing/sql/*.sql | sort -u (the 8 ADP tables and dms repair_orders) -->
- **Two finding kinds (contract § 2), both fleet-scoped.** `fleet.rebalance.move`: move named vehicles between two locations in the same metro of a rental fleet, into a location forecast to be short, with predicted on-rent hours gained and turnaways avoided; it carries a `recommended_action` (`move_vehicles`). `fleet.rebalance.gap`: informational, for a shortfall with no move, a model hold, or a surplus with nowhere to send vehicles; no `recommended_action`.
- **Judgment.** One Bedrock Converse call (Claude Sonnet 4.6, tool `record_rebalance_plan`) per metro that has a candidate, with one retry when validation or the Guardrail rejects the reply, then the rules fallback (contract § 5). `FI_REBAL_MAX_MODEL_CALLS` caps calls per run at 12, retries included. The tool returns only candidate ids, vehicle lists, timing, detail, evidence references and holds; the agent computes the money figures (contract § 7), the headline, severity and confidence. Confidence is `high` (destination forecast MAPE ≤ 15%), `medium` (≤ 30%) or `low` (> 30%), lowered one band when the median actual-to-predicted ratio of the last 3 executed moves into the destination is below 0.5. Severity is `attention` (turned-away share ≥ 30%), `soon` (≥ 15%) or `routine` (< 15%), **never** `p0`.
  <!-- verify: grep -n "FI_REBAL_MAX_MODEL_CALLS:" infrastructure/lib/vsa-tier2-stack.ts; grep -n "max_retries=1" agents/fi_rebalancing/llm.py; grep -n "<= 0.15\|<= 0.30\|< 0.5\|>= 0.30\|>= 0.15" agents/fi_rebalancing/formulas.py -->
- **Output.** One move item per origin, destination and ISO week of the first deficit day, and one gap item per location and week (`subject_key`, contract § 2). Each daily run refreshes an item while the need persists; it expires 2 days after its last refresh.
  <!-- verify: grep -n "subject_key = f\|timedelta(days=2)" agents/fi_rebalancing/write.py -->
- **Action.** `move_vehicles` through `CmsExecutor`, decided by an operator or by an enabled rule within its limit (spec D11, contract § 10). The approve handler sets the fleet, locations, week and figures from the Finding (contract § 4). An operator may drop vehicles, which scales the figures; a rule can't. A move names at most `max_vehicles_per_move` vehicles (10, AVX policy), and a rule-approved move at most `max_vehicles_per_auto_move` (5). <!-- verify: python3 -c "import json;print({p['_key_template']:p['staging_default'] for p in json.load(open('deployment/config/avx-policy-defaults.json'))['parameters'] if 'fi-rebalancing' in p.get('_key_template','')})" -->
- **Dispatch.** An operator's decision goes to CMS's `POST /api/v1/fleet-intelligence/moves` with the operator's token. A rule's decision goes to `<VSA_CMS_FI_INTERNAL_API_ENDPOINT>/api/v1/fleet-intelligence/internal/moves`, SigV4-signed by the evaluator's role with no token, only while `FI_REBALANCING_AUTO_APPROVAL_LIVE` is on (contract § 10).
- **Auto-approval bounds.** `/vsa/{stage}/avx/fi-rebalancing/auto-approval-bounds` holds exactly `max_limit_usd` and `max_vehicles_per_auto_move`, seeded by `deployment/scripts/seed_avx_policy_params.sh` (staging: 500 and 5). A `move_vehicles` rule carries a `limit` of at most `max_limit_usd`. The evaluator approves a move only when an enabled rule for the fleet has a `limit` at or above the move's `transfer_cost_usd`, the cost is within `max_limit_usd`, and the move names at most `max_vehicles_per_auto_move` vehicles (contract § 10). Unreadable bounds approve nothing, and CMS's internal route re-checks the same bounds.
  <!-- verify: python3 -c "import json;print([p['staging_default'] for p in json.load(open('deployment/config/avx-policy-defaults.json'))['parameters'] if p.get('_key_template','').endswith('auto-approval-bounds')])" -->
- **Move records and outcomes.** CMS checks fleet scope, then that no VIN is grounded, every VIN is still at the origin and the destination has parking; a grounding, origin or parking failure is a 409. It then writes the move to `cms-{stage}-storage-vehicle-moves`, keyed on the Action id so a replay is a no-op (contract §§ 8, 9). ADP's `move_outcomes` view reads moves through `cms_ingest.vehicle_moves` and scores each one 7 days after its effective date; the agent reads the daily snapshot to set confidence.
  <!-- verify: grep -n "storage-vehicle-moves" scripts/live-verify-fi-rebalancing.py -->
- **Never on a phone.** The push broker skips any Finding whose `agent_id` starts with `agent-fi-` (`skip_reason=fi_item`).
- **Tier 1.** Nothing new: the fleet_manager `fleet_recommendation(finding_id)` tool reads any Finding in a fleet the caller has standing on, so it returns move and gap items unchanged.
- **Run records and alarm.** Each run writes a record keyed `agent-fi-rebalancing#rebalance` to `vsa-{stage}-avx-agent-runs`. The `vsa-{stage}-avx-run-stale-rebalance-<region>` alarm fires after 27 hours with no run record and notifies the `vsa-{stage}-tier2-operator-alerts` topic.
  <!-- verify: grep -n "component: 'rebalance', emptyHours" infrastructure/lib/vsa-tier2-stack.ts; grep -n "avx-run-stale-" infrastructure/lib/vsa-tier2-stack.ts -->

### Deterministic Seams (M9)

Tier 2 preserves deterministic components that existed before:
- The triage classifier (`lambdas/triage-classifier/`) — unmodified, returns its P-level classifications deterministically
- `offers_lookup` — unmodified. It returns vehicle offer terms and their mandatory disclosure text, which is presented verbatim; no LLM sits in the path that generates offer terms
- ADP prediction models — Tier 2 consumes their outputs, computes no model of its own

**Where a finding coincides with a triage P0**, Tier 2's `severity_hint` is advisory metadata only — it cannot override or soften the P0 verdict. Test-enforced: `severity_hint` is drawn from a non-P vocabulary and an artifact carrying a P-level there is rejected at write time; and where a P0 phrase is present, it is narrated verbatim. Stated precisely because the distinction matters if the LLM enrichment path is ever wired: the write-time rejection covers `severity_hint`, and the broader all-fields scan matches whole-token P-levels rather than substrings — so it is not a general guarantee that no artifact text can ever contain P-level language.

### Pull-Only: No Delivery Path Exists

Tier 2 **writes artifacts and exposes one Tier 1 read surface. It does not call any notification API.** No message broker exists in this stage — that is a later stage (S2), and the requirement it will satisfy is that no agent ever calls a channel directly. Test-enforced today: the `agents/tier2/` and `agents/health/` packages import no SNS, SES, Pinpoint, Connect, or End User Messaging client. The check carries positive-control tests that prove the detector actually catches a violating file, so it cannot silently degrade into a no-op.

An owner hears about vehicle health only by asking, through the conversational agent. This is M13, and it is deliberate de-risking: the reasoning is validated against real data on staging before any of it is ever pushed to a customer unprompted.

### EventBridge Scheduler Integration

Daily schedule at 02:00 / 02:20 / 02:40 UTC (us-west-2 staging), one invocation per component:
- Scheduler passes `{"stage": "staging", "component": "battery"}` as the event payload
- Lambda target: `agents/tier2/runner.py:lambda_handler`
- Runs against a **bounded, deterministic VIN set** — 25 VINs per run by default, deterministically selected via `ORDER BY vin LIMIT n`, so the same VINs are processed each day. Against live fleet sizes of 100k (battery), 25k (tires), and 475k (brakes), this is a current limitation stated plainly rather than described as fleet-wide coverage.
- Per-run budget cap: hard token ceiling + wall-clock ceiling; abort with a recorded partial result rather than running unbounded

The FI components run later the same morning: cost 04:00, recall 05:00, warranty 05:20, service 05:40, rebalance 06:00 UTC.

### Cross-Account Architecture

- **Tier 2 Lambda** (us-west-2 staging): reads from ADP lake via cross-account Athena in the ADP account (us-east-1)
- **Lake Formation grants**: read-only SELECT/DESCRIBE, no grant option, on the databases the health components read (among them `energy_usage`, `tire_health`, `service_records` and `vehicle_identity`), and for the FI cost component on `fleet_cost` and `fleet_ops` (all tables) and the named tables `dimensions.vins` and `vehicle_pricing`, for Stage C on `fleet_recall`, and for Stage D on `fleet_health`, `engineering` and `ota_campaigns` (`vsa-analytics-stack.ts`; later stages' grants are listed in their own sections above). The ADP lake is strict-mode Lake Formation, so IAM alone is insufficient — the grants are a separate, required authority
- **IAM**: the Tier 2 role holds S3 and KMS grants scoped to the ADP lake bucket prefixes and key, with `kms:Decrypt` bounded by a `ViaService` condition. It also holds Bedrock `InvokeModel` grants on the Sonnet 4.6 inference profile, which the FI cost component uses and the health components don't (see the deployment note above), and `bedrock:ApplyGuardrail` on the cost agent's Guardrail.
- **Result storage**: Athena writes result CSVs to the workgroup's output location in `cvx-{stage}-athena-results-{account}-{region}`. Two things worth knowing: the workgroup enforces its own output location, so a client-supplied prefix is ignored; and these CSVs contain a `vin` column, under SSE-S3 with a 30-day lifecycle. That is the one place raw VINs come to rest outside the findings table — deliberately recorded here, since VINs are otherwise kept out of logs, exception messages, and partial artifacts

### Platform Leverage (M2) — Measurement Deferred

Tier 2 is extracted as a shared `agents/tier2/` package (contract, writer, budget, observability, scheduler wrapper, base ADP reader). Whether this platform materially reduces the cost of agent N+1 cannot be measured until a later stage. The claim is falsifiable later; S1 documents the public surface without claiming victory now.
