# Virtual Service Agent

An agentic, multi-modal, multi-tenant tier-1 service experience for every automotive customer touchpoint — whether the caller is a Ford vehicle owner, an Avis rental customer, or a driver in a Michelin-managed fleet. The Virtual Service Agent (VSA) sits on top of the Virtual Fleet Operator (VFO) agent mesh and the Connected Mobility (CMS) + Automotive Data Platform (ADP) data layers, and exposes them through Amazon Connect (voice + chat) and mobile/web channels.

This document complements [DESIGN.md](DESIGN.md), [ARCHITECTURE.md](ARCHITECTURE.md), and [KNOWLEDGE_BASE_DESIGN.md](KNOWLEDGE_BASE_DESIGN.md). Where VFO describes the fleet-operator-facing supervisor and specialist agents, VSA describes the customer-facing channel, tenancy model, triage logic, and booking flow.

---

## 1. Purpose & Scope

**Primary persona — the Customer.** One user role, three operating contexts:

| Context | Example tenants | Who calls | Typical ask |
|---|---|---|---|
| OEM owner | Ford, GM, Stellantis | Vehicle owner | "Check engine light — is it safe to drive?" |
| Rental | Avis, Hertz, Enterprise | Rental customer | "Tire pressure warning mid-trip — what do I do?" |
| Fleet | Michelin, Ryder, logistics ops | Driver or fleet manager | "Brake noise on VEH-0025 — how urgent?" |

**Secondary persona — the Tier-2 human agent.** Receives escalations from VSA with full context pre-populated (vehicle state, DTCs, triage classification, conversation transcript, recommended action). Uses the same specialist agents as agent-assist during the live call.

**Explicitly out of scope.** In-bay technician workflows (deferred); direct retail parts sales; warranty litigation.

---

## 2. What VSA Does

1. **Authenticates** the caller and resolves them to a vehicle (VIN) and tenant.
2. **Fuses context**: live telemetry + DTC history + open recalls + warranty coverage + service history.
3. **Classifies criticality** on a fixed four-level taxonomy.
4. **Recommends an action** grounded in the tenant's service policies.
5. **Closes the loop**: creates a STAR-shaped service request, offers warm transfer to the tenant's booking line, or escalates to a tier-2 human when needed.

### Criticality taxonomy

| Level | Meaning | Default action |
|---|---|---|
| **P0 — Stop driving** | Safety-critical: brake failure, overheating, airbag fault, loss of steering assist | Pull over; dispatch roadside; escalate to tier-2 immediately |
| **P1 — Service now** | Active DTC in degraded mode; measurable risk if ignored > 24–48h | Schedule within 48h; create STAR ticket; offer warm transfer |
| **P2 — Service soon** | Non-critical DTC, wear indicator, recall outreach | Schedule within 1–2 weeks; create STAR ticket |
| **P3 — Monitor** | Informational; reset conditions documented | No action; log; educate caller |

Classification is a tool call, not a free-form LLM judgment. The classifier reads: active DTCs with severity codes, telemetry thresholds (coolant temp, brake wear, tire pressure delta), open recalls, and the tenant's policy overrides (e.g., Avis escalates any P1 to roadside by default).

---

## 3. Multi-Tenancy Model

Tenancy is first-class. One deployment, many tenants. Tenant ID is resolved at session start (Connect attributes, DNIS, phone number lookup, or explicit API key for mobile/web) and carried through every downstream call.

| Concern | How tenancy shapes it |
|---|---|
| Knowledge base | One Bedrock KB per tenant; shared KB for cross-tenant content (NHTSA recalls, VSS catalog) |
| Branding/voice | Connect contact flow selects tenant-specific greeting, voice (Nova Sonic voice profile), prompts, and hours-of-operation |
| Specialist agents | Same VFO specialists (Cost, Recall/Warranty, Maintenance) enabled per tenant; extensions (e.g., `RentalPolicyAgent` for Avis) loaded conditionally |
| Data access | Tenant-scoped IAM conditions; DynamoDB partition keys prefixed with tenant ID; Aurora row-level security |
| Booking | Each tenant configures: ticket sink (webhook, queue, Salesforce Service Cloud, ServiceNow), warm-transfer number, DMS endpoint (if any) |
| Escalation | Tenant-specific tier-2 queue in Connect; tenant-specific escalation rules |
| Observability | CloudWatch log groups + metrics dimensioned by tenant ID; per-tenant audit trails |

A tenant is defined by a config record in DynamoDB (`vsa-tenant-config`) with KB IDs, enabled agents, branding, policy overrides, ticket sink, escalation rules. No code changes to onboard a tenant.

---

## 4. Channels

| Channel | Stack | Use |
|---|---|---|
| Voice | Amazon Connect → Contact Lens → Lambda → Nova 2 Sonic (speech-to-speech) → VSA supervisor | Primary — drivers on the road, hands-free |
| Chat | Amazon Connect Chat → Lambda → VSA supervisor | Text channel; also supports image uploads (dashboard photo) via Nova Pro vision |
| Mobile/Web | Tenant app → API Gateway → Lambda → VSA supervisor | Self-service; sessions can escalate to Connect chat/voice |
| Agent-assist (tier-2) | Connect agent desktop → Lambda widget → VSA supervisor (read-only + co-pilot prompts) | Human tier-2 sees context, ranked next-best-actions, and can invoke the same specialist agents |

All channels terminate at a single VSA supervisor entry point. Channel type is an attribute on the session, not a fork in the agent graph.

---

## 5. Agent Mesh

VSA reuses the VFO specialist agents and adds one new supervisor plus three VSA-specific collaborators.

```
              ┌────────────────────────────────────┐
              │   VSA SUPERVISOR (per session)      │
              │   Bedrock Agent · Claude Sonnet 4   │
              │   • Persona: customer-facing        │
              │   • Tenant-aware prompts & tools    │
              │   • Calls Triage first, then others │
              └──────────┬──────────────────────────┘
                         │
   ┌─────────────┬───────┴────────┬──────────────┬──────────────────┐
   │             │                │              │                  │
┌──▼──────┐ ┌───▼───────┐ ┌───────▼──────┐ ┌─────▼─────┐ ┌──────────▼─────┐
│ TRIAGE  │ │ BOOKING   │ │ AUTH/CTX     │ │  VFO      │ │ TENANT-SPECIFIC│
│ AGENT   │ │ AGENT     │ │ AGENT        │ │ SPECIALIST│ │  AGENTS (opt)  │
│         │ │           │ │              │ │  AGENTS   │ │                │
│ Inputs: │ │ • STAR    │ │ • Phone→VIN  │ │ (reused)  │ │ • RentalPolicy │
│ DTCs,   │ │   ticket  │ │ • Tenant ID  │ │           │ │ • FleetSLA     │
│ telem,  │ │ • Warm    │ │ • Consent &  │ │ Cost,     │ │ • OEM-Warranty │
│ recalls │ │   xfer    │ │   auth       │ │ Recall/   │ │                │
│         │ │ • Calendar│ │ • Session    │ │ Warranty, │ │                │
│ Output: │ │   (v2)    │ │   state      │ │ Maint.,   │ │                │
│ P0–P3   │ │ • DMS (v3)│ │              │ │ Rebal.    │ │                │
│ + reason│ │           │ │              │ │           │ │                │
└─────────┘ └───────────┘ └──────────────┘ └───────────┘ └────────────────┘
```

**Shared with VFO.** Cost, Recall/Warranty, Maintenance, Rebalancing agents and the shared Bedrock KBs (FSBs, service manuals, warranty terms, recalls). No duplication.

**New for VSA:**
- **Triage Agent.** Deterministic-first: a tool that executes the criticality taxonomy against DTCs + telemetry + recalls + tenant policy and returns a P0–P3 classification with a structured reason trace. LLM only narrates the result.
- **Booking Agent.** Hybrid booking per Section 6.
- **Auth/Context Agent.** Resolves phone/account/VIN → tenant, enforces consent, loads session state from Redis (same LKS pattern CMS uses).
- **Tenant-specific agents (optional).** Loaded per config: `RentalPolicyAgent` (mileage caps, swap-vehicle flows), `FleetSLAAgent` (downtime budgets), `OEMWarrantyAgent` (brand-specific terms).

---

## 6. Booking — Hybrid, Phased

Option F from the design discussion. All three phases produce STAR-shaped payloads so tenants already speaking STAR get a clean integration path from day 1.

| Phase | Capability | What the caller experiences | Tenant integration required |
|---|---|---|---|
| **v1** | Recommend + Ticket + Warm transfer | "You should service within 48h. I've created ticket VSA-4821 with your dealer. I can also connect you now — say 'connect me'." | Subscribe to VSA ticket queue (SQS, webhook, or Salesforce/ServiceNow connector); publish warm-transfer DID |
| **v2** | + Calendar slot proposal | "Your dealer has openings Tuesday 10am or Wednesday 2pm — which works?" | Tenant exposes availability feed (iCal, simple API, or DMS partial) |
| **v3** | + Full DMS booking | "Booked — confirmation AB-991-2026, Tuesday 10am, 2101 Main St." | Direct DMS integration (CDK Global, Reynolds, Tekion, Dealertrack, Fleetio, Whip Around). Per-tenant, per-DMS. |

**STAR payload.** Every booking artifact (ticket, slot request, confirmed appointment) is emitted as **STAR-shaped JSON** mapping to `ServiceRepairOrderAdd` / `AppointmentAdd` / `WarrantyClaimPreCheck` message structures. An optional STAR XML converter is available per tenant for legacy DMS consumers.

**Escalation path.** If confidence is low, the customer requests a human, the classification is P0, or the booking agent encounters a policy exception, the session is handed to tier-2 via a Connect queue configured per tenant. The handoff carries: transcript, triage output, VIN + vehicle context, open recalls, warranty coverage summary, recommended action, and the draft STAR ticket.

---

## 7. Data & Authentication

- **Caller → VIN resolution.** Phone number or account ID → tenant account record → list of associated vehicles → if one, auto-select; if many, disambiguate by voice or chat.
- **Live vehicle state.** Redis LKS (already populated by CMS Flink telemetry processor) — sub-millisecond reads per session.
- **DTC + trip + service history.** DynamoDB (CMS and ADP tables), queried per VIN.
- **Recalls.** Reuses CMS `recall-integration` service.
- **Warranty + service records + parts.** ADP `guidance-for-recall-warranty-management` and ADP service tables.
- **Consent & privacy.** Per-tenant consent prompts; session recordings gated by tenant config; PII redaction in logs by default.

---

## 8. Non-Functional

- **Latency budget.** Voice: < 1.5s from user end-of-speech to first agent token (Nova Sonic streaming). Chat: < 3s time-to-first-token.
- **Availability.** Channel SLOs inherit from Connect; supervisor Lambda deployed multi-AZ; tenant config cached in ElastiCache.
- **Cost guardrails.** Per-tenant monthly budgets and per-session token caps (classification first, expensive synthesis only when needed).
- **Safety.** P0 detection always triggers human handoff; LLM cannot downgrade a deterministic P0 classification.
- **Auditability.** Every classification and booking decision logs: inputs consulted, tool calls made, tenant config version, model IDs, prompt hash.

---

## 9. Phased Delivery

| Phase | Scope | Exit criteria |
|---|---|---|
| **v1** | Fleet tenant (e.g., Michelin), voice + chat via Amazon Connect **and** a custom tenant-branded mobile/web app, triage + v1 booking, Connect-native tier-2 queues, agent-assist read-only | End-to-end call: driver → auth → triage → recommendation → STAR-JSON ticket → warm transfer option |
| **v2** | Multi-tenant (add OEM + rental), calendar slot proposal, tenant-specific agents, agent-assist co-pilot actions | New tenants onboardable via config only |
| **v3** | DMS-integrated full booking for priority tenants, image input (dashboard photos via Nova Pro), advanced personalization | At least one fleet + one OEM + one rental in DMS-booked production |

---

## 10. Locked Decisions

1. **v1 tenant segment**: **fleets first** (Michelin-style B2B fleet operator). Rental and OEM follow in v2+.
2. **Tier-2 escalation**: **Amazon Connect-native queues only** in v1. Third-party ACD integration (Genesys, NICE, Five9) deferred.
3. **Channels in v1**: **Amazon Connect (voice + chat) AND a custom tenant-branded mobile/web app**. Both terminate at the same VSA supervisor.
4. **STAR payload format**: **STAR-shaped JSON** for all tickets, slot requests, and appointment confirmations. Optional STAR XML converter per tenant for legacy DMS consumers.
5. **Agent stack**: **Strands Agents SDK** on **Amazon Bedrock AgentCore Runtime**. Supervisor uses Strands `BidiAgent` for bidirectional voice/chat streaming.
6. **Voice model**: **Amazon Nova 2 Sonic** (`amazon.nova-2-sonic-v1:0`) speech-to-speech, sub-700ms latency, native Amazon Connect integration. Fallback: **Polly + Transcribe** when Nova 2 Sonic is unavailable in a region.
7. **Supervisor LLM**: **Claude Sonnet 4.6** (`anthropic.claude-sonnet-4-6`) via Bedrock on-demand inference.
8. **Deployment region**: All components — VSA, CMS, ADP, Amazon Connect, Bedrock Agents — deployed to **us-east-1**. Single-region, co-located. Nova 2 Sonic + Connect native integration available there.

---

## 11. Demo & Companion App

The v1 mobile/web channel doubles as the primary demo surface for exec briefings and customer meetings. It is a single reskinnable React app — one codebase, per-tenant themes loaded from config.

**Audience orientation.** Built for live exec/customer briefings first; re:Invent booth and workshop variants follow.

**Theming model.** A tenant config (`/themes/<tenant>.json`) defines logo, color palette, font, voice profile, greeting, and map style. The app applies it via CSS custom properties and a `TenantContext`. Three themes ship in v1: `michelin` (fleet, primary), `ford` (OEM preview), `avis` (rental preview). Theme is selected by subdomain or `?tenant=` parameter — no rebuild required.

**Single-screen layout.**

| Region | Content |
|---|---|
| Header | Tenant logo, vehicle ID, driver name |
| Left pane | Live vehicle state from CMS Redis LKS — speed, RPM, coolant, map with route, active DTCs (red), open recalls (orange) |
| Right pane | Conversation — voice (mic) or chat, same VSA supervisor, same agent calls |
| Bottom drawer | **Reasoning panel** (collapsible): triage classifier inputs and P0–P3 output with trace, tool calls in order with latencies, STAR-JSON ticket draft |

The reasoning drawer is the agentic-AI proof point — it turns "the AI said so" into an auditable decision path, which is what lands with executive audiences.

**Demo script** — three back-to-back scenarios against the same VIN:

1. **P3 Monitor** (~90s) — tire pressure dip within tolerance; VSA says no action needed
2. **P1 Service Now** (~2m) — coolant overtemp + active DTC + open recall; VSA creates STAR-JSON ticket, offers warm transfer to the tenant's Connect queue, transfers with full context
3. **P0 Stop Driving** (~90s) — brake fault; VSA deterministic P0, instructs pull-over, auto-escalates

**Fault injection.** Reuses CMS simulator's `test_forced_alerts.py` and `safety_alert_scenarios.py`. The presenter triggers each scenario from a hidden control panel; telemetry flows through the real CMS pipeline.

**Reasoning feed.** A thin WebSocket Lambda streams triage classifier events and tool-call traces to the UI, following the same pattern as CMS `ws-fanout`.

**Tech stack.** React + Vite, Tailwind with tenant theming, Amazon Connect Chat SDK for chat, Connect Streams API (or direct phone) for voice, WebSocket reasoning feed. No backend rewrite — the app is a view into the VSA supervisor Lambda.
