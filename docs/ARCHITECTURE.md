# Virtual Fleet Operator — Agent & Knowledge Base Architecture

---

## Related: Virtual Service Agent (VSA)

VFO (this document) is **fleet-operator-facing**. VSA is the **customer-facing** layer that sits above it — tier-1 assistant for drivers, OEM owners, rental customers, and fleet managers across multiple tenants. VSA reuses the same specialist agents and knowledge base documented here, adding its own supervisor, triage classifier, and Amazon Connect channels. See [`VIRTUAL_SERVICE_AGENT.md`](VIRTUAL_SERVICE_AGENT.md) and the associated `VSA_*.md` docs.

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
│  │              cms-prod-vfo-action-queue                     │   │
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

## CVX-on-foundation v1 — Persona grounding

**Overview:** CVX v1 grounds its 6 personas (3 existing + 3 expansion) in ADP Foundation v1 data products via two grounding mechanisms — **AgentCore Memory metadata-tagged long-term memory (LTM)** for tabular data and **Bedrock Knowledge Base cross-account Retrieve** for reference documentation. Session initialization pre-fetches data from ADP tables via cross-region Athena, seeding an AgentCore Memory resource that persists for the session lifetime. All persona tools query Memory or KB at tool-invocation time, eliminating per-turn live queries.

### Architecture Delta (CVX-on-foundation v1)

```
┌─────────────────────────────────────────────────────────────────────┐
│ CVX us-west-2 (123456789012)                                        │
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
│   • LF v4 wildcard cross-account share to CVX account               │
│                                                                     │
│  Bedrock Knowledge Base                                             │
│   • adp-staging-vehicle-knowledge                                   │
│   • Cross-account resource policy (principals: CVX roles)           │
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
- CVX: us-west-2 (AgentCore Runtime, Memory, Nova Sonic inference)
- ADP: us-east-1 (Glue catalog, Bedrock KB, data lake)

**Cross-account-API patterns wired from day 1:**
- Lake Formation v4 cross-account share grants CVX's AgentCore execution role `lakeformation:GetDataAccess` on the 6 in-scope ADP databases
- Bedrock KB resource policy explicitly authorizes CVX role ARN for `bedrock-agent-runtime:Retrieve`
- All cross-account credentials are temporary (STS AssumeRole via LF + LF-issued session tokens for S3, or direct IAM Bedrock permission)

**Customer multi-account deploy:** change CDK context:
- `-c adpAccountId=<ADP-account>` (CVX side)
- `-c cvxAccountId=<CVX-account>` (ADP side)
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

**Pre-v1 (seed-data model):** Tools read from CVX-owned DynamoDB tables (`adp-{stage}-*` stubs) or local markdown fixtures seeded by `scripts/seed-personas.py`, `scripts/seed-service-network.py`, `scripts/bootstrap-local-kb.py`.

**v1 (ADP-grounded model):**
- **Tabular data:** replaced by AgentCore Memory LTM + metadata-filtered Retrieve. Session-init persona-loader populates Memory from cross-region Athena queries against ADP Glue tables. Tools no longer read DynamoDB; they read from Memory via `bedrock-agentcore-control:RetrieveMemoryRecords(metadataFilters={...})`.
- **Reference data:** replaced by Bedrock KB cross-account Retrieve. Tools call `bedrock:Retrieve(kbArn=adp-staging-vehicle-knowledge-base, filter={source_category:...})` instead of local markdown search.
- **Unchanged:** CVX-owned writes (e.g., booking, escalation actions) still go to CMS DynamoDB; CVX-owned rules (triage, DTC lookup logic) still use local catalogs.

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
| `book` | CMS DDB write | CMS DDB write (unchanged) | CVX-to-CMS integration unchanged |
| `escalate` | Local rules | Local rules (unchanged) | Rules-based, not data-grounded |
| `find_service_center` | Local seed | Local seed (unchanged) | v2 candidate; ADP integration pending |

---

## Tier 2 Platform — Autonomous Vehicle Health Reasoning

### The Two-Tier Split

CVX implements the two-tier agent architecture documented in `~/.kiro/steering/agentic-tiers.md`. This section covers how it is applied to vehicle health findings.

**Tier 1: Conversational Agent (synchronous, latency-bound)**
- The supervisor agent and its tools live here — they read and respond in the ~1 second latency budget for voice (Amazon Nova Sonic v2 bidi)
- The `health_findings()` tool is a fast read: a single-partition DynamoDB Query, no LLM in the path
- Returns the latest health findings per component, with evidence for deep-dive conversation

**Tier 2: Autonomous Platform (scheduled, nobody waiting)**
- Runs daily on EventBridge Scheduler, invoked once per component (battery, tires, brakes)
- Performs cross-account Athena queries against ADP curated data products
- Applies judgment: does this matter for this owner now, how urgent is it, should findings be bundled, what should be narrated
- Writes an artifact (DynamoDB item) carrying its own evidence for audit and later deep-dives
- **Critically: Tier 2 computes no model and derives no thresholds.** ADP infers; CVX judges. (See D3, D3a limitations below.)

**As deployed on staging, the judgment path is deterministic.** The runner wires the
deterministic assessment function; the Strands/Bedrock enrichment path exists in the codebase but
has no caller, and the Lambda bundles no third-party packages, so every run record reports
`tokens_used=0` and `estimated_cost_usd=0.0` — correct values, not a broken meter. The execution
role does hold Bedrock `InvokeModel` grants; they are unexercised rather than absent. Wiring that
path or removing it (and its grants) is a filed follow-on. The tier split does not depend on an
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

**Limitation — Two identically-named columns, one wrong**: ADP exposes `state_of_health_pct` in both `energy_usage` (the correct source, ~100% new → ~86% at 7 years, varies per vehicle) and `vehicle_telemetry_aggregated` (generated from a global row index, ~95→90, effectively identical for all vehicles). Reading the telemetry column would make per-owner judgment impossible while looking superficially correct. Tier 2 explicitly queries `energy_usage`; this is test-enforced.

**Limitation — Training label, not model**: ADP's own schema describes `needs_replacement` (in the tire product) as a "supervised label" — an ML training target, not a deployed model's inference output. Consuming it as though it were a prediction is a category slip. It is acceptable here because the value is still computed by the data platform rather than by CVX, so the doctrinal seam holds where it matters. The upgrade path is ADP publishing predictions as a curated, catalogued product.

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

### Cross-Account Architecture

- **Tier 2 Lambda** (us-west-2 staging): reads from ADP lake via cross-account Athena in the ADP account (us-east-1)
- **Lake Formation grants**: read-only SELECT/DESCRIBE, no grant option, on the `energy_usage`, `tire_health`, and `service_records` databases. The ADP lake is strict-mode Lake Formation, so IAM alone is insufficient — the grants are a separate, required authority
- **IAM**: the Tier 2 role holds S3 and KMS grants scoped to the ADP lake bucket prefixes and key, with `kms:Decrypt` bounded by a `ViaService` condition. It also holds Bedrock `InvokeModel` grants that are currently unexercised (see the deployment note above). Worth stating for anyone auditing the policy against observed behaviour: the policy JSON and the deployed behaviour genuinely disagree here, and a compromised runtime role would inherit the Bedrock entitlement even though no code path uses it — which is why "wire it or remove it" is a filed follow-on rather than a cosmetic one
- **Result storage**: Athena writes result CSVs to the workgroup's output location in `cvx-{stage}-athena-results-{account}-{region}`. Two things worth knowing: the workgroup enforces its own output location, so a client-supplied prefix is ignored; and these CSVs contain a `vin` column, under SSE-S3 with a 30-day lifecycle. That is the one place raw VINs come to rest outside the findings table — deliberately recorded here, since VINs are otherwise kept out of logs, exception messages, and partial artifacts

### Platform Leverage (M2) — Measurement Deferred

Tier 2 is extracted as a shared `agents/tier2/` package (contract, writer, budget, observability, scheduler wrapper, base ADP reader). Whether this platform materially reduces the cost of agent N+1 cannot be measured until a later stage. The claim is falsifiable later; S1 documents the public surface without claiming victory now.
