# Guidance for Agentic Vehicle Experience on AWS

> **Status: v1 architecture complete. Reference implementation of a two-tier
> agentic platform for automotive.**
>
> **What this Guidance includes**: Tier 1 conversational agents (Amazon Nova
> Sonic voice, text/HTTP, and web chat embedded in a Connected Mobility fleet
> manager); one Tier 2 autonomous agent (Vehicle Health — battery, tire, and
> brake findings computed daily, plus a diagnostics component that turns active
> CMS fault codes into findings every 5 minutes); the **AVX core** Findings and
> Actions data models and API (contract v2.3), with read routes scoped by
> vehicle, owner, fleet, dealership, and district; an approve path that
> dispatches synchronously to the configured executor (including a live
> dealer-management repair-order path); and a delivery broker that records a
> channel decision per Finding (rate limit, consent, severity threshold,
> dismissal feedback). Three dealer-management personas
> (`warranty_agent`, `dm_copilot`, `comms_triage`) run on the text runtime,
> with safety-critical prompt properties pinned by tests.
>
> **Known limitations**: the iOS AVX Cards tab and APNs push delivery
> integrate with a separate Connected Mobility client and are not shipped in
> this repository; APNs push configuration is operator-owned (see
> "APNs and SNS mobile push" below). The dealer-facing UI surfaces are
> delivered by the paired Dealer Management System guidance and are not
> included here. Roadside assistance dispatch is a **simulator**: the
> supervisor recommends a target, and the executor stub writes a dispatch
> record — no real roadside vendor is contacted.
>
> **Landing use cases** (design targets, not all of them shipped today):
> range anxiety, warranty coverage, service booking, onboarding, DTC triage,
> fleet cost planning, and dealer-scoped operations. DTC triage and service
> booking work today via the Tier 1 path.

An **agentic multi-surface** platform for automotive OEMs, fleet operators, and dealerships — conversational AI that reasons about customer data (Tier 1, <1 sec, voice/text/iOS) paired with autonomous agents that run off-conversation (Tier 2, daily scheduled) — all grounded in [Connected Mobility](https://github.com/aws-solutions-library-samples/guidance-for-connected-mobility-on-aws) telemetry and [Automotive Data Platform](https://github.com/aws-solutions-library-samples/guidance-for-automotive-data-platform-on-aws) curated products.

## Overview

The Agentic Vehicle Experience (AVX) delivers intelligent **Findings** (autonomous agent reasoning) and **Actions** (customer approvals) across native iOS cards, conversational chat, and dealer workflows. It combines real-time vehicle telemetry with LLM reasoning to produce evidence-carrying artifacts (compute-once, read-many) that the Tier 1 conversational agents then narrate and route.

**The split is architectural**: Tier 2 agents (LLM reasoning) run on a daily schedule against the data platform's full history. Tier 1 agents (conversational) read the finished Findings and handle approval loops, cross-surface dispatch, and drill-down explanation. This buys down latency (conversational reads are <1 sec DDB lookups, not inference) and determinism (reasoning happens once with full evidence trail, not regenerated per user question).

### What It Does — Two-Tier Example

**Scenario: winter battery drain**

*Tier 2 agent (overnight)* → Reasons about temperature-adjusted expected range vs. observed drop, produces a Finding: *"Your winter range drop is normal for the ambient temperature and charge pattern you've seen this week."* The Finding carries evidence: the battery's state-of-health curve, ambient-temperature data, the past 7 days' charge cycles. Stored with `computed_at`, `confidence`, `evidence[]`, `tokens_used` (LLM call overhead).

*Customer wakes up, taps iOS card* → Tier 1 agent reads the finished Finding in milliseconds and narrates it: *"Your battery's looking good. The 12% range drop you noticed is expected in 28°F temperatures. No action needed."* The Finding's evidence and metadata surface on the card (temp data chip, "computed yesterday" timestamp, confidence level).

*Same customer, next chat* → Different question: *"Should I get a service appointment?"* Tier 1 agent reads the same Finding again (no new reasoning needed), narrates it in context of CMS service history and ADP warranty coverage, and if appropriate, routes to booking. The Finding is reusable across surfaces because it carries its own evidence and freshness metadata.

Compare to a conversational-only model where each question re-invokes the LLM to reason about the same data—slow, latency-budget-breaking, non-deterministic, and evidence-opaque to the user.

**On the limits of what is deployed.** The example above describes the two-tier design. What runs on
staging today is its vehicle-health slice: a scheduled agent computes battery / tire / brake / diagnostics findings
daily and records the evidence behind each one, and the conversational agent reads the finished
finding in milliseconds and narrates it, including how fresh it is. See
[Tier 2 Platform](docs/ARCHITECTURE.md#tier-2-platform--autonomous-vehicle-health-reasoning) for the
split, the artifact contract, and — importantly — the stated limitations of the current data sources.

The owner only hears a finding by asking. **Nothing is delivered unprompted in the deployed stage**:
there is no card feed, no push notification and no proactive outreach today. The iOS cards, APNs
push and broker described further down this README are the target design of AVX Specs 1a/1b/2, not
current behaviour.

### Architecture at a Glance

**Tier 1 — Conversational** (Tier 1 supervisor agent + tools + voice/text/iOS routing)
- **Runtime**: Bedrock AgentCore (Strands supervisor, Nova Sonic voice, text HTTP)
- **Personas**: consumer, vehicle_owner, fleet_owner, service_advisor, technician, dm_copilot, etc. (40 registered tools across 14 surfaces, from the drift-tested registry) <!-- verify: head -5 docs/AGENT-INVENTORY.md | grep 'registered tools'; python3 scripts/generate_agent_inventory.py --check -->
- **Surfaces**: iOS cards (new), web chat, Amazon Connect voice, DMS web UI
- **Tools**: 40 LLM-visible tools across 14 surfaces (vehicle state, booking, finance, escalation, Finding + Action reads) — see [`docs/AGENT-INVENTORY.md`](docs/AGENT-INVENTORY.md), regenerated + drift-checked in CI <!-- verify: awk '/^## All Registered Tools/,/^## /' docs/AGENT-INVENTORY.md | grep -c '^| `' -->
- **Deterministic seams**: triage P0 classifier (stop-driving rule), DMS warranty verdicts, payment authorization, offers disclosure (all narrated verbatim, test-enforced)

**Tier 2 — Autonomous** (daily-scheduled agents, one per capability)
- **Deployed as a reference**: Agent 2 (Vehicle Health & Service; battery, tire, brake, diagnostics) — the only Tier 2 agent this reference implementation ships
- **Specified as follow-on capability, not shipped in this release**: Agent 1-lite (Energy & Charging; range-anxiety pre-empt), Agent 3 (Ownership Cost & Coverage; warranty gaps, recalls, prepaid, trade-in), and Agent 4-lite (Onboarding; first-90-days advisories) — see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) § "Tier 2 Platform" for the artifact contract these follow-ons plug into
- **Data**: ADP curated products + CMS telemetry + DMS warranty via cross-account reads (Lake Formation, Bedrock KB RAG)
- **Artifact contract**: `computed_at`, `confidence`, `evidence[]`, `agent_version`, `inputs_hash`, plus `finding_key` for stable-identity update-in-place
- **Execution**: scheduled per-VIN via EventBridge Scheduler; Tier 1 reads via thin DDB lookups

**Cross-Surface Action Framework**
- **Findings** (Tier 2 output) ← stored in `vsa-{stage}-avx-findings` DDB table with scope GSIs (vehicle, owner, fleet, dealership, district)
- **Actions** (customer approvals) ← stored in `vsa-{stage}-avx-actions`, linked by `finding_id`, with state machine (pending → executed → completed) and audit trail
- **Executors** (6 pluggable targets):
  - `dms` → DMS booking API for RO creation
  - `vehicle-command` → CMS remote commands (lock, charging schedule, etc.)
  - `retail` → AVX Acquire lead-capture and offer configuration
  - `chat` → drill-down in Tier 1 conversational with pre-seeded evidence
  - `ios-owner` → iOS action feed (for dealer-originated proposals)
  - `none` → advisory-only (record + dismiss)
- **Broker** (DMS-side, AVX-side): rate limit (10 card/day, 3/hour default), priority threshold, cardinality cap, per-category consent gating, dismissal feedback for ranking adjustment

### Multi-Surface UX Primitive: Cards, Not Chat

> **Findings and Actions layer — what this Guidance ships.** The Findings /
> Actions data models, the approve dispatcher, and the delivery broker's
> per-Finding channel decisions are implemented in this repository. The
> **iOS card UI** is delivered by the paired Connected Mobility client and
> is not included here. The **dealer-facing surfaces** (DistrictView briefs,
> RO pre-brief cards) are delivered by the paired Dealer Management System
> guidance. The bullets below describe the target UX those surfaces build on
> this data layer.

AVX v1 surfaces Findings primarily as **native iOS cards** in a new AVX Cards tab (consumer + vehicle owner + fleet owner surfaces). Each card shows headline, evidence chips, approve/dismiss/drill-down affordances. Drill-down opens chat pre-seeded with the Finding's evidence. **Chat is drill-down only, not the primary surface** — aggressive on native cards, not chat-heavy.

- **App-icon badge**: silent APNs push updates the unread count (closed-app delivery) — APNs certificate provisioning is operator-owned; see the "APNs and SNS mobile push" prerequisite below
- **P0 notification** (system-level banner): consent-gated by finding category (safety, coverage, service); only for P0 severity findings
- **In-app card**: always created; no notification
- **Below-threshold cards**: no card creation (feedback-driven suppression)

Dealer workflows (DMS) use similar patterns: DistrictView briefing cards, RO pre-brief cards at start-of-day, and a "propose to customer" affordance that creates an `ios-owner` Action visible on the owner's iOS feed (reverse direction).

### Cross-Surface Findings & Actions: Data Contracts and Integrations

The Findings/Actions data layer in this repository serves three surfaces:
- **iOS Cards**: delivered by the paired Connected Mobility client (`guidance-for-connected-mobility-on-aws`), which renders Findings and posts approvals through this repository's API.
- **Dealer Workflows**: delivered by the paired Dealer Management System guidance (`guidance-for-dealer-management-system-on-aws`), which covers the dealer surfaces and the `dms.ro.status_changed` flowback onto Actions.
- **APNs Push Delivery**: operator-owned integration. The delivery broker in this repository decides the channel per Finding; wiring the decision to a real APNs certificate + SNS platform application is a per-deployment step.

**API contract**: `agents/tier2/schemas/finding.schema.json`, `agents/tier2/schemas/action.schema.json` and `docs/api/avx-findings-actions.openapi.yaml` (contract v2.3), byte-pinned by `tests/schemas/test_contract_fixtures_byte_identical.py`. <!-- verify: python3 -m pytest tests/schemas/test_contract_fixtures_byte_identical.py -q --> Read routes are `/findings/{vehicle,owner,owner-fleet,fleet,dealership,district}/{id}` plus the `/actions/*` routes, and approval is `POST /findings/{finding_id}/approve`. Authorization is Cognito groups plus standing claims (`custom:customerId`, `custom:vehicleId`, `custom:fleetIds`), each honoured only when the stage attests it is not client-writable; see `docs/identity-and-standing-contract.md`. Cross-system access (ADP, CMS, DMS) is documented in `docs/runbooks/avx-cross-account-iam.md`.

## Personas (AVX v1)

AVX v1 is grounded on real data from the Automotive Data Platform (ADP). Each persona combines tabular data (vehicle history, customer profile, service records) via **AgentCore Memory metadata-tagged LTM** with reference data (DTC guides, repair procedures, parts catalogs) via **Bedrock KB RAG**. The three DMS personas (`warranty_agent`, `dm_copilot`, `comms_triage`) are registered in the agent inventory. <!-- verify: python3 scripts/generate_agent_inventory.py --check && grep -c "warranty_agent\|dm_copilot\|comms_triage" docs/AGENT-INVENTORY.md --> Consumer-facing personas reach their Findings through voice, text or web chat; the iOS card feed is delivered by the paired Connected Mobility client.

| Persona | Tenant Segment | UX Surface | Primary Findings | Mode |
|---------|---|---|---|---|
| **Fleet Driver** | fleet | iOS card feed + chat drill-down | Vehicle health, compliance, range anxiety | voice, text, cards |
| **Consumer** | rental | iOS card feed + chat drill-down | Energy, coverage, onboarding | voice, text, cards |
| **Vehicle Owner** | oem | iOS card feed + chat drill-down | Health, warranty, trade-in timing, onboarding | voice, text, cards |
| **Fleet Owner** | fleet (small-business) | iOS card feed + chart summary + chat | Rolled-up coverage exposure, cost planning, compliance | text, cards |
| **Service Advisor** | fleet | DMS web UI (cards on RO/warranty views) + DMS chat | Booking context, warranty pre-fill, customer history | web, text |
| **Technician** | fleet | DMS web UI (cards on RO view) + tools | Service advisor pre-brief, DTC guidance, parts | web, text |
| **District Manager** | fleet | DMS DistrictView (briefing cards) + DMS chat | Daily attention list, geographic consolidation, EOL compliance | web, text |

*Cards-first means*: Tier 2 agent Findings appear on the iOS Cards tab automatically (approved findings, deduplicated by `finding_kind` + `vehicle`). No proactive notifications by default (Tier 1 chat or Tier 2 push require explicit user approval per `/approval/findings/{id}`). P0 safety findings may trigger system-level notifications if the user has opted into that category.

### Contact Center Integration

| Role | Channel | Status |
|------|---------|--------|
| **Contact Center Agent** | Amazon Connect | ✅ Functional (escalation) |

Agents receive real-time vehicle context and customer interaction history via Customer Profiles lookups.

### Web Client Integration

The AVX agent is accessible via a web chat interface integrated into the Connected Mobility (CMS) Fleet Manager. The web route invokes the agent over a REST API with persona inference from CMS Cognito claims.

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

**Auth:** Cognito IdToken (Bearer). Persona is inferred server-side from CMS claims (`cognito:groups` / `custom:role`); see `docs/persona-mapping.md` for the mapping table. The reference deploys to both a staging and a production environment; see `docs/DEPLOYMENT.md` § "Component 1c: CDK + AgentCore prod" for the production deploy sequence.

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
│  AVX — AI Agent Layer (this repo)                                            │
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │  Bedrock AgentCore (Strands Agent + Nova Sonic Voice)                │    │
│  │                                                                      │    │
```

<!-- BEGIN GENERATED: agent-inventory-summary -->
> **40 registered LLM-visible tools across 14 surfaces** (+ 2 internal composition helper).
> See [`docs/AGENT-INVENTORY.md`](docs/AGENT-INVENTORY.md) for the full table.
>
> | Surface | Tool count |
> |---------|------------|
> | Fleet Driver | 15 |
> | Consumer | 23 |
> | Retail Advisor | 18 |
> | Vehicle Owner | 22 |
> | Service Advisor | 15 |
> | Technician | 12 |
> | fleet_manager | 9 |
> | warranty_agent | 5 |
> | dm_copilot | 6 |
> | comms_triage | 4 |
> | parts_intel | 1 |
> | compliance_agent | 1 |
> | fleet_owner | 11 |
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
│    live state    │  │  • Bedrock KB:       │  │  <connect-instance-id>   │
│  • IoT Core     │  │    DTC guides, TSBs, │  │                          │
│  • MSK/Flink    │  │    recalls, manuals, │  │                          │
│                  │  │    service policy    │  │                          │
└──────────────────┘  └──────────────────────┘  └──────────────────────────┘
```

#### AVX-on-foundation v1 — ADP Cross-Account Data Grounding

In v1, persona context data (vehicle history, customer profile, service records, charging events) flows from ADP's curated data products via **cross-account Lake Formation grants** and **AgentCore Memory metadata-tagged LTM**. Session-init loads persona-specific snapshots from ADP's us-east-1 Athena tables into AVX's us-west-2 Memory resource; tool dispatch retrieves records via structured metadata filters (persona_id, vehicle_id, customer_id). Reference data (DTC guides, repair procedures, parts catalogs) routes through **Bedrock KB cross-account Retrieve** with source-category filtering. This architecture supports split-account customer deployments via parameterized CDK context (`adpAccountId`, `cvxKbPrincipals`); same-account-cross-region is the v1 staging topology. Cross-account grants and KB resource policies ship via ADP v0.1.0+; CloudTrail data-event coverage logs all cross-account reads for compliance audit.

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
| [Automotive Data Platform (ADP)](https://github.com/aws-solutions-library-samples/automotive-data-platform-on-aws) `guidance-for-vehicle-knowledge-base` seeded | Reference data (identity, parts, KB); curated products cross-account reads |
| AWS CLI v2 | Account access |
| Node.js 18+ | CDK infrastructure |
| Python 3.12+ | Agent + Lambda code |
| **iOS APNs setup** (for production deployments) | Push notification certificates + keys for iOS card feed. Provision an APNs authentication key (or per-app cert) in the [Apple Developer portal](https://developer.apple.com/account/), then load the credentials into the SNS platform application below. Staging defaults to silent push (badge updates); production requires explicit APNs configuration. |
| **Amazon SNS platform application** (staging/prod) | Created by CDK per stage; carries APNs credentials; target for device registrations. See `infrastructure/lib/notification-stack.ts`. |
| AWS CDK CLI | `npm install -g aws-cdk` |
| Bedrock AgentCore CLI | `pip install bedrock-agentcore` |
| Bedrock model access | Claude Sonnet + Nova Sonic enabled |

### Region availability

This Guidance is currently pinned to **US AWS regions** (`us-east-1`, `us-west-2`).
The Bedrock cross-region inference-profile prefix `us.anthropic.claude-sonnet-4-6`
is hardcoded in three places:

- `infrastructure/lib/vsa-core-stack.ts` and `infrastructure/lib/vsa-tier2-stack.ts` (CDK inference-profile ARN construction)
- `.env.example` (`VSA_MODEL_ID`)
- `agents/supervisor/config.py` (`model_id` default)

To deploy in a non-US region, update the profile prefix to match your region's
Bedrock inference-profile catalog — for example `eu.anthropic.claude-sonnet-4-6`
for European regions or `apac.anthropic.claude-sonnet-4-6` for Asia Pacific.
Nova Sonic (voice) availability also varies by region; verify service
availability before selecting a non-US region.

## Quick Start

The steps below assume you have cloned the two prerequisite repositories
alongside this one — for example under a common parent directory:

```
~/aws-guidance/
├── guidance-for-connected-mobility-on-aws/
├── guidance-for-automotive-data-platform-on-aws/
└── guidance-for-agentic-vehicle-experience-on-aws/    # this repo
```

Substitute your own parent path in the `cd` commands below.

### 1. Deploy prerequisites

```bash
# CMS — if not already deployed
cd ../guidance-for-connected-mobility-on-aws/deployment
make deploy-all
make bootstrap-demo

# ADP — seed data products
cd ../../guidance-for-automotive-data-platform-on-aws/guidance-for-vehicle-knowledge-base
make create-tables
make seed-all
make deploy        # Bedrock KB
make upload-data   # KB documents → S3
make sync-data     # vectorize
```

### 2. Configure AVX

```bash
cd ../../guidance-for-agentic-vehicle-experience-on-aws
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
python3 scripts/seed-meridian-fleet.py
python3 scripts/seed-meridian-fleet-user.py

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
│   ├── seed-meridian-fleet.py           # Tenant config seeding
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
| `VSA_ATHENA_WORKGROUP` | AVX Athena stack output | Workgroup for cross-region ADP table queries |
| `VSA_ATHENA_RESULTS_BUCKET` | AVX Athena stack output | S3 bucket for Athena query results |
| `VSA_FOUNDATION_MEMORY_ID` | AVX foundation stack output | AgentCore Memory resource ID for persona context |
| `VSA_MEMORY_REGION` | Your choice | AgentCore Memory region; defaults to `us-west-2` |
| `VSA_PERSONA_LTM_TTL_MIN` | Your choice | LTM cache TTL in minutes; defaults to 15 |
| `VSA_ADP_KB_ID` | ADP KB stack output | Bedrock KB ID for cross-account Retrieve |
| `VSA_ACQUIRE_KB_SOURCE_CATEGORY` | Your choice | Acquire phase KB source category filter; defaults to `vehicle_manufacturing` |
| `VSA_DISCOVER_KB_SOURCE_CATEGORY` | Your choice | Discover phase KB source category filter; defaults to `vehicle_shopping_guide` |
| `VSA_KB_SOURCE_CATEGORY_FILTER` | Your choice | Enable persona-scoped KB filtering on `source_category` metadata. Code default `false`; **`make deploy-staging` now sets it `true`** because the ADP KB carries `source_category` metadata as of 2026-06-22 (ingestion `DAD31WBERT`). Leave unset/`false` only for KBs without the metadata tag |
| `BEDROCK_KB_ID` | ADP KB stack output | (Legacy) Knowledge base RAG — use `VSA_ADP_KB_ID` in v1 |
| `VSA_STAGE` | Your choice | AVX stack naming |
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
         AVX (this repo)
```

- **Without ADP:** Agent triages, books, escalates using CMS data + embedded corpora. No VIN decode, parts pricing, or service center lookup.
- **Without Connect:** Escalation fails: `/escalate` opens the Connect chat before it records a roadside request, so without a Connect channel it returns an error and records no roadside request.
- **Full stack:** Agent provides complete vehicle-aware, location-aware, price-aware responses with human handoff.

## Testing & Evals

AVX includes a tiered evaluation pipeline that catches regressions before deploy.

| Tier | Scope | Speed | Cost |
|------|-------|-------|------|
| 1 | Tool unit evals (24 cases across 8 tools) | <2s | $0 |
| 2 | Mocked-Nova conversation evals (18 cases) | <2s | $0 |
| 3 | Real-AgentCore E2E (6 cases, requires `RUNTIME_ID`) | ~10min | ~$1/run |

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

### Scope CORS before production

Cross-Origin Resource Sharing on the AVX REST API is scoped to a single
origin per stage, read from the `/cvx/{stage}/allowed-origin` SSM
parameter.

The single-origin decision is deliberate: the only browser-cross-origin caller
of AVX APIs is the CMS UI SPA. The iOS client is a native app (no `Origin`
header). Amazon Connect and AgentCore invoke server-side via SigV4.

**Operator setup, once per stage before first deploy**:

```bash
aws ssm put-parameter \
  --name /cvx/staging/allowed-origin \
  --type String \
  --value 'https://your-cms-cloudfront-domain.example' \
  --region us-west-2
```

Set to `*` explicitly if you want demo-mode wildcard behavior — that's a
knowing opt-in, not the default. Missing the parameter will cause the CFN
deploy to fail at parameter resolution (fail-closed, matching other
SSM-backed values in this stack).

**Migration for existing deployments** — set the SSM parameter to `*` first,
deploy, verify behavior unchanged, then flip to the real CloudFront origin.
No code deploy required to flip.

The 14 handlers now emit `Access-Control-Allow-Origin` via
`os.environ.get("ALLOWED_ORIGIN", "*")` — the `"*"` fallback is only
exercised in tests + local dev; the CDK stack sets `ALLOWED_ORIGIN` on every
Lambda from the resolved SSM value. `tests/lint/test_cors_no_literal_wildcard.py`
prevents future handlers from reintroducing a literal wildcard.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
