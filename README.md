# Guidance for Connected Vehicle Experience on AWS

> **Status: In Development** — Service stage (triage + DTC guidance) fully functional. Discover + Order + Build reference implementation (generic OEM with Meridian seed) deployable on staging. **Tier 2 Platform (autonomous vehicle health agent) live on staging** — battery, tire, and brake findings computed daily via scheduled agent, read by conversational agents on-demand.

An AI-powered customer engagement platform for automotive OEMs, fleet operators, and dealerships — built on Amazon Bedrock, Amazon Connect, and the [Connected Mobility](https://github.com/aws-solutions-library-samples/guidance-for-connected-mobility-on-aws) data platform.

## Overview

The Connected Vehicle Experience (CVX) provides intelligent, multi-channel interactions across the vehicle ownership lifecycle. It combines real-time vehicle telemetry with conversational AI to deliver personalized, context-aware experiences backed by a comprehensive automotive knowledge base. **New in v1:** a voice-first Discover journey (catalog exploration), a multi-step Order configurator, and a time-simulated Build tracker — all accessible from a new iOS "Buy" tab for conversational vehicle shopping.

### What It Does

A driver says: *"My check engine light is on and the car feels sluggish."*

The agent:
1. **Decodes the VIN** → 2022 Chevrolet Equinox 1.5T (from ADP Vehicle Identity Graph)
2. **Reads active DTCs** → P0299 Turbocharger Underboost (from CMS live vehicle state)
3. **Looks up the DTC** → severity P2, likely boost leak or wastegate (from Knowledge Base)
4. **Checks for TSBs** → TSB 2024-EN-0298 matches this exact issue (from Knowledge Base)
5. **Quotes the repair** → Wastegate actuator $180-$250, labor 1.2 hours (from ADP Parts Catalog)
6. **Finds a service center** → Rush Truck Center Dallas, 3.2 miles away, 1-day wait (from ADP Service Network)
7. **Books the appointment** → Thursday 2pm confirmed (via book() tool → CMS service-history)
8. **Checks warranty** → Covered under powertrain warranty, no cost to driver (from ADP Vehicle Lifecycle)

All in a single voice conversation.

**Some questions a conversation cannot answer fast enough.** *"How is my battery doing?"* needs
reasoning over millions of rows of the data platform's history — far outside the ~1 second budget a
voice turn allows. So that reasoning runs elsewhere: a scheduled agent computes vehicle health
findings daily and records each one with the evidence behind it, and the conversational agent reads
the finished finding in milliseconds and narrates it, including how fresh it is. See
[Tier 2 Platform](docs/ARCHITECTURE.md#tier-2-platform--autonomous-vehicle-health-reasoning) for
the split, the artifact contract, and — importantly — the stated limitations of the current data
sources.

The owner only hears a finding by asking. Nothing is delivered unprompted in this stage, and no
notification path exists to do so.

## Personas (CVX-on-foundation v1)

CVX v1 is grounded on real data from the Automotive Data Platform (ADP). Each persona combines tabular data (vehicle history, customer profile, service records) via **AgentCore Memory metadata-tagged LTM** with reference data (DTC guides, repair procedures, parts catalogs) via **Bedrock KB RAG**.

| Persona | Tenant Segment | Grounding | LTM Products | KB Filters | Mode |
|---------|---|---|---|---|---|
| **Fleet Driver** | fleet | LTM-only | vehicle_identity, service_records, charging_sessions, ota_campaigns, vehicle_telemetry_aggregated*, energy_usage* | — | voice, text |
| **Consumer** | rental | LTM-only | vehicle_identity, charging_sessions, customer_360, customer_interactions, ota_campaigns, energy_usage* | — | voice, text, web |
| **Retail Advisor** | oem | LTM-only | vehicle_identity, service_records, customer_360, customer_interactions | — | voice, text, web |
| **Vehicle Owner** | oem | LTM-only | vehicle_identity, service_records, charging_sessions, ota_campaigns, vehicle_telemetry_aggregated* | — | voice, text, web |
| **Service Advisor** | fleet | Mixed (LTM + KB) | service_records, customer_360, customer_interactions | tsb_recall, owner_manual, service_policy, charging_narrative | voice, text, web |
| **Technician** | fleet | Mixed (LTM + KB) | service_records, customer_360 | dtc_guide, parts_catalog, service_policy | voice, text, web |

*PySpark-gated: data available after `PySpark via Glue` spec ships.

### Contact Center Integration

| Role | Channel | Status |
|------|---------|--------|
| **Contact Center Agent** | Amazon Connect | ✅ Functional (escalation) |

Agents receive real-time vehicle context and customer interaction history via Customer Profiles lookups.

### Web Client Integration

The CVX agent is accessible via a web chat interface integrated into the Connected Mobility (CMS) Fleet Manager. The web route invokes the agent over a REST API with persona inference from CMS Cognito claims.

**Route:** `POST {vsaApiEndpoint}/assistant/chat`

**Request:**
```json
{
  "prompt": "What does DTC P0420 mean?",
  "sessionId": "session_1234567890_abc123",
  "vin": "1FMCU0F70MUA12345"
}
```

**Response:**
```json
{
  "result": "P0420 is a catalyst system efficiency below threshold...",
  "sessionId": "session_1234567890_abc123",
  "classification": "P2",
  "vin": "1FMCU0F70MUA12345",
  "citations": ["dtc-P0420.md", "tsb-2024-EN-0298.md"]
}
```

**Auth:** Cognito IdToken (Bearer). Persona is inferred server-side from CMS claims (`cognito:groups` / `custom:role`); see `docs/persona-mapping.md` for the mapping table. **Deployed to both staging and prod** — the prod cutover shipped 2026-07-17 via spec `2026-07-16-cvx-prod-cutover` (see `docs/DEPLOYMENT.md` § "Component 1c: CDK + AgentCore prod (us-east-1)" for the prod deploy sequence).

### Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  Clients                                                                     │
│  ┌──────────┐  ┌──────────┐  ┌──────────────┐  ┌───────────────────────┐   │
│  │ iOS App  │  │ Web App  │  │ Connect Agent│  │ Technician Portal     │   │
│  └────┬─────┘  └────┬─────┘  └──────┬───────┘  └────────┬──────────────┘   │
└───────┼──────────────┼───────────────┼───────────────────┼──────────────────┘
        │ SigV4 WS     │ REST          │ Chat              │
        ▼              ▼               ▼                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  CVX — AI Agent Layer (this repo)                                            │
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │  Bedrock AgentCore (Strands Agent + Nova Sonic Voice)                │    │
│  │                                                                      │    │
```

<!-- BEGIN GENERATED: agent-inventory-summary -->
> **27 registered LLM-visible tools across 7 surfaces** (+ 1 internal composition helper).
> See [`docs/AGENT-INVENTORY.md`](docs/AGENT-INVENTORY.md) for the full table.
>
> | Surface | Tool count |
> |---------|------------|
> | Fleet Driver | 13 |
> | Consumer | 21 |
> | Retail Advisor | 17 |
> | Vehicle Owner | 21 |
> | Service Advisor | 13 |
> | Technician | 10 |
> | Anonymous Kiosk (default-deny allow-list) | 10 |
<!-- END GENERATED: agent-inventory-summary -->

```
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │  REST API (API Gateway + Lambda)                                     │    │
│  │  /vehicles/{id}/service-history  /vehicles/{id}/live-state           │    │
│  │  /vehicles/{id}/trips            /vehicles/{id}/safety-events        │    │
│  │  /drivers/me                     /triage                             │    │
│  │  /escalate                                                           │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
        ┌──────────────────────────┼──────────────────────────┐
        ▼                          ▼                          ▼
┌──────────────────┐  ┌──────────────────────┐  ┌──────────────────────────┐
│  CMS             │  │  ADP                 │  │  Amazon Connect          │
│  (Connected      │  │  (Automotive Data    │  │  (Contact Center)        │
│   Mobility)      │  │   Platform)          │  │                          │
│                  │  │                      │  │  • P0/P1/P2 queues       │
│  • DynamoDB:     │  │  • Vehicle Identity  │  │  • Context lookup flow   │
│    vehicles,     │  │  • Parts Catalog     │  │  • Callback-to-driver    │
│    drivers,      │  │  • Service Network   │  │  • Customer Profiles     │
│    trips,        │  │  • Vehicle Lifecycle  │  │    (vehicle context)     │
│    service-hist  │  │  • Campaigns/Offers  │  │                          │
│  • Redis:        │  │  • Connected Svcs    │  │  Instance:               │
│    live state    │  │  • Bedrock KB:       │  │  ba0586ff-1566-418c-     │
│  • IoT Core     │  │    DTC guides, TSBs, │  │  b982-5e74e32e7474       │
│  • MSK/Flink    │  │    recalls, manuals, │  │                          │
│                  │  │    service policy    │  │                          │
└──────────────────┘  └──────────────────────┘  └──────────────────────────┘
```

#### CVX-on-foundation v1 — ADP Cross-Account Data Grounding

In v1, persona context data (vehicle history, customer profile, service records, charging events) flows from ADP's curated data products via **cross-account Lake Formation grants** and **AgentCore Memory metadata-tagged LTM**. Session-init loads persona-specific snapshots from ADP's us-east-1 Athena tables into CVX's us-west-2 Memory resource; tool dispatch retrieves records via structured metadata filters (persona_id, vehicle_id, customer_id). Reference data (DTC guides, repair procedures, parts catalogs) routes through **Bedrock KB cross-account Retrieve** with source-category filtering. This architecture supports split-account customer deployments via parameterized CDK context (`adpAccountId`, `cvxKbPrincipals`); same-account-cross-region is the v1 staging topology. Cross-account grants and KB resource policies ship via ADP v0.1.0+; CloudTrail data-event coverage logs all cross-account reads for compliance audit.

### AWS Services Used

| Service | Role |
|---------|------|
| **Amazon Bedrock** | Conversational AI (Claude Sonnet), embeddings (Titan) |
| **Bedrock AgentCore** | Agent runtime, bidirectional voice (Nova Sonic) |
| **Amazon Connect** | Contact center — escalation queues, callback, agent desktop |
| **Bedrock Knowledge Bases** | RAG over TSBs, recalls, DTC guides, manuals |
| **Amazon Cognito** | Multi-tenant authentication, OEM-branded sign-in |
| **API Gateway** | REST + WebSocket APIs |
| **DynamoDB** | Tenant config, triage decisions, action queue, Tier 2 health findings |
| **Lambda** | API handlers, triage classifier, Tier 2 health agent |
| **EventBridge Scheduler** | Daily Tier 2 runs, one schedule per vehicle component |
| **Amazon Athena** | Cross-account reads of the data platform's curated products (Lake Formation governed) |

### Partner Integration Points

| Domain | Partners | Integration Pattern |
|--------|----------|-------------------|
| Digital Commerce | Salesforce, Adobe | REST webhook from booking tools |
| Payments | Stripe, Plaid | Agent tool for payment initiation |
| Document Processing | CDK, Reynolds | Lambda for repair orders, invoices |
| Contract Management | Conga, Agiloft | Agent tool for warranty/contract lookup |
| Credit Decisioning | FICO, Experian | API call from finance persona |
| Subscription Management | Zuora | Webhook for connected service subscriptions |

## Prerequisites

| Requirement | Purpose |
|-------------|---------|
| [Connected Mobility (CMS)](https://github.com/aws-solutions-library-samples/guidance-for-connected-mobility-on-aws) deployed | Vehicle data (DynamoDB + Redis) |
| [Automotive Data Platform (ADP)](https://github.com/aws-solutions-library-samples/automotive-data-platform-on-aws) `guidance-for-vehicle-knowledge-base` seeded | Reference data (identity, parts, KB) |
| AWS CLI v2 | Account access |
| Node.js 18+ | CDK infrastructure |
| Python 3.12+ | Agent + Lambda code |
| AWS CDK CLI | `npm install -g aws-cdk` |
| Bedrock AgentCore CLI | `pip install bedrock-agentcore` |
| Bedrock model access | Claude Sonnet + Nova Sonic enabled |

## Quick Start

### 1. Deploy prerequisites

```bash
# CMS — if not already deployed
cd ~/connected-mobility-guidance-on-aws/deployment
make deploy-all
make bootstrap-demo

# ADP — seed data products
cd ~/automotive-data-platform-on-aws/guidance-for-vehicle-knowledge-base
make create-tables
make seed-all
make deploy        # Bedrock KB
make upload-data   # KB documents → S3
make sync-data     # vectorize
```

### 2. Configure CVX

```bash
cd ~/guidance-for-connected-vehicle-experience-on-aws
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env with values from CMS + ADP stack outputs
```

### 3. Build and deploy

```bash
# Build Lambda assets
scripts/build-triage-classifier.sh
scripts/build-api-ws-authorizer.sh
scripts/build-api-vehicle-live-state.sh

# Deploy CDK stacks
source .env
cd infrastructure && npm install && cdk deploy --all --require-approval never
```

### 4. Seed tenant and deploy agent

```bash
# Create tenant config + demo user
python3 scripts/seed-michelin.py
python3 scripts/seed-michelin-user.py

# Bootstrap local KB (standalone mode, or use ADP's KB_ID)
python3 scripts/bootstrap-local-kb.py

# Deploy voice agent to AgentCore
cp .bedrock_agentcore.yaml.example .bedrock_agentcore.yaml
# Edit with your account ID + execution role
agentcore deploy
```

### 5. Verify

```bash
# Text mode agent
python3 agents/supervisor/cli.py

# Voice smoke test
python3 scripts/smoke-test-voice.py

# REST API
curl -H "Authorization: Bearer $TOKEN" \
  https://YOUR_API/prod/vehicles/VEH-0025/service-history | jq .
```

## Project Structure

```
├── agents/supervisor/              # Bedrock AgentCore agent
│   ├── bidi_app.py                # Nova Sonic voice entrypoint
│   ├── agentcore_app.py           # Text-mode entrypoint
│   ├── cli.py                     # Local testing CLI
│   ├── config.py                  # Runtime configuration
│   ├── session.py                 # Session state management
│   ├── voice_prompt.py            # System prompt for voice persona
│   ├── triage_keywords.py         # Keyword-based pre-triage
│   ├── dtc_response_catalog.py    # DTC response templates
│   └── tools/                     # Agent tool implementations
│       ├── triage.py              # DTC severity classification
│       ├── book.py                # Service appointment booking
│       ├── escalate.py            # Human handoff via Connect
│       ├── knowledge.py           # RAG from Bedrock KB
│       ├── vehicle_identity.py    # VIN decode (ADP)
│       ├── service_network.py     # Find service centers (ADP)
│       └── parts_lookup.py        # Parts + pricing (ADP)
├── infrastructure/                 # CDK stacks (TypeScript)
│   ├── bin/vsa.ts                 # App entry point
│   └── lib/
│       ├── vsa-api-stack.ts       # REST API + Lambda endpoints
│       ├── vsa-core-stack.ts      # DynamoDB tables
│       ├── triage-classifier-stack.ts
│       └── tenant-pool.ts         # Per-tenant Cognito
├── lambdas/                        # Lambda function handlers
│   ├── api-drivers-me/            # GET /drivers/me
│   ├── api-escalate/              # POST /escalate (→ Connect)
│   ├── api-vehicle-context/       # GET /vehicles/{id}/context
│   ├── api-vehicle-live-state/    # GET /vehicles/{id}/live-state
│   ├── api-vehicle-safety-events/ # GET /vehicles/{id}/safety-events
│   ├── api-vehicle-service-history/
│   ├── api-vehicle-trips/         # GET /vehicles/{id}/trips
│   ├── api-triage/                # POST /triage
│   ├── triage-classifier/         # ML triage classification
│   └── shared/                    # CMS client, config, utilities
├── corpora/                        # Embedded KB (standalone mode) + KB source docs
│   ├── technical-reference/       # DTC interpretation guides
│   ├── tsb-recalls/               # TSBs + NHTSA recalls
│   ├── owner-manuals/             # Maintenance schedules
│   ├── service-policy/            # SLA + warranty + escalation
│   ├── vehicle_manufacturing/        # Build phase grounding (12 docs) — four-wheeler manufacturing stages
│   ├── vehicle_shopping_guide/       # Discover phase grounding (10 docs) — vehicle type, financing, test-drive guidance
│   ├── meridian_lineup_narrative/ # Meridian model pages (4 docs)
│   ├── meridian_owner_manual/     # Meridian ownership basics (4 docs)
│   ├── meridian_ive_package/      # In-vehicle-experience packages (4 docs)
│   └── README.md                  # Authoring conventions — read before adding docs
├── scripts/                        # Build, seed, and test scripts
│   ├── build-*.sh                 # Lambda asset bundlers
│   ├── seed-michelin.py           # Tenant config seeding
│   ├── seed-acquire-catalog.py    # Order/Build catalog seeding
│   ├── bootstrap-local-kb.py      # Create Bedrock KB from corpora/
│   ├── wire-connect-to-tenant.py  # Wire Connect outputs to config
│   └── smoke-test-voice.py        # Voice agent verification
└── schemas/                        # JSON schemas
```

## Configuration

All configuration via environment variables. See [`.env.example`](.env.example).

| Variable | Source | Purpose |
|----------|--------|---------|
| `CMS_STAGE` | CMS deployment | Derives DynamoDB table names |
| `CMS_KMS_KEY_ARN` | CMS KMS console | DynamoDB SSE decrypt |
| `REDIS_ENDPOINT` | CMS MSK stack output | Vehicle live state |
| `ADP_STAGE` | ADP deployment | Derives ADP table names (e.g., `staging`) |
| `VSA_ADP_ACCOUNT_ID` | ADP account ID | Cross-account ARN templating; defaults to local account |
| `VSA_ADP_REGION` | ADP region | ADP Athena/Glue region; defaults to `us-east-1` |
| `VSA_ATHENA_WORKGROUP` | CVX Athena stack output | Workgroup for cross-region ADP table queries |
| `VSA_ATHENA_RESULTS_BUCKET` | CVX Athena stack output | S3 bucket for Athena query results |
| `VSA_FOUNDATION_MEMORY_ID` | CVX foundation stack output | AgentCore Memory resource ID for persona context |
| `VSA_MEMORY_REGION` | Your choice | AgentCore Memory region; defaults to `us-west-2` |
| `VSA_PERSONA_LTM_TTL_MIN` | Your choice | LTM cache TTL in minutes; defaults to 15 |
| `VSA_ADP_KB_ID` | ADP KB stack output | Bedrock KB ID for cross-account Retrieve |
| `VSA_ACQUIRE_KB_SOURCE_CATEGORY` | Your choice | Acquire phase KB source category filter; defaults to `vehicle_manufacturing` |
| `VSA_DISCOVER_KB_SOURCE_CATEGORY` | Your choice | Discover phase KB source category filter; defaults to `vehicle_shopping_guide` |
| `VSA_KB_SOURCE_CATEGORY_FILTER` | Your choice | Enable persona-scoped KB filtering on `source_category` metadata. Code default `false`; **`make deploy-staging` now sets it `true`** because the ADP KB carries `source_category` metadata as of 2026-06-22 (ingestion `DAD31WBERT`). Leave unset/`false` only for KBs without the metadata tag |
| `BEDROCK_KB_ID` | ADP KB stack output | (Legacy) Knowledge base RAG — use `VSA_ADP_KB_ID` in v1 |
| `VSA_STAGE` | Your choice | CVX stack naming |
| `AWS_REGION` | Your choice | All resources |

## Dependency Model

```
ADP (optional, enhances)     CMS (required)
  │                            │
  │ KB_ID, identity,           │ DynamoDB tables,
  │ parts, network             │ Redis, Connect
  │                            │
  └────────────┬───────────────┘
               │
               ▼
         CVX (this repo)
```

- **Without ADP:** Agent triages, books, escalates using CMS data + embedded corpora. No VIN decode, parts pricing, or service center lookup.
- **Without Connect:** Escalation writes to action queue but doesn't route to a human. Chat/callback disabled.
- **Full stack:** Agent provides complete vehicle-aware, location-aware, price-aware responses with human handoff.

## Testing & Evals

CVX includes a tiered evaluation pipeline that catches regressions before deploy.

| Tier | Scope | Speed | Cost |
|------|-------|-------|------|
| 1 | Tool unit evals (24 cases across 8 tools) | <2s | $0 |
| 2 | Mocked-Nova conversation evals (18 cases) | <2s | $0 |
| 3 | Real-AgentCore E2E (5 cases, requires `RUNTIME_ID`) | ~10min | ~$1/run |

Quick start:

```bash
make eval-tier1   # tool unit evals (every PR)
make eval-tier2   # mocked-Nova conversation (every PR)
make eval-tier3   # real AgentCore E2E (nightly + pre-deploy) — requires RUNTIME_ID env
make eval-all     # all three tiers
```

See `docs/EVAL.md` for the full runbook (interpreting reports, updating baselines, adding cases, triaging failures).

## Roadmap

- [x] Fleet driver voice assistant (Nova Sonic + AgentCore)
- [x] DTC triage with severity classification
- [x] Service booking via agent tool
- [x] Escalation to Connect (chat + voice callback)
- [x] Multi-tenant Cognito authentication
- [x] iOS companion app (in CMS repo)
- [x] Knowledge Base RAG (DTC guides, TSBs, recalls, manuals)
- [x] VIN decode from ADP Vehicle Identity Graph
- [x] Service center lookup with distance + capability filtering
- [x] Parts pricing with 3-tier fleet/OEM/equivalent
- [x] Amazon Connect Customer Profiles (vehicle context on agent desktop)
- [ ] Vehicle owner persona + consumer-facing prompts
- [ ] Service advisor / technician personas
- [ ] Amazon Comprehend sentiment analysis
- [ ] Amazon Rekognition visual AI (damage detection, VIN scan)
- [ ] Partner integration framework (webhooks + tool extensions)
- [ ] Multi-language support (Nova Sonic native)

## Security

- All API endpoints require Cognito JWT authentication
- Cross-account DynamoDB access uses scoped IAM policies
- KMS decrypt restricted via `kms:ViaService` condition
- AgentCore runtime uses a dedicated execution role
- No secrets stored in source code
- Connect escalation requires authenticated session

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
