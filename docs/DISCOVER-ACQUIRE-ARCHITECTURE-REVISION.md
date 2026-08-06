# Discover / Acquire Architecture Revision — single-OEM grounding, two entry paths, entity resolution

Status: DRAFT — supersedes the Discover/Find and Acquire/Finance composition
in `the-customer-journey.adoc` as shipped by spec
`2026-07-15-cvx-ig-agentic-depth-refresh`, and the corresponding composition
in Diagram Baselines 5 and 6. This document is the design record; the IG
prose and diagram baselines are updated to match after this is settled.

## Why this revision exists

The Discover/Find and Acquire/Finance composition shipped in
`2026-07-15-cvx-ig-agentic-depth-refresh` was derived by matching prose to
Diagram Baselines 5 and 6, which themselves composed Discover/Acquire by
reusing Service's (Diagram 1/2/3) architecture — same AgentCore Runtime,
same AgentCore Memory LTM pattern, same Amazon Connect channel, same
escalation triangle, with only tool names swapped in. That reuse was never
independently derived from what Discover and Acquire customers actually
need; it was inherited from diagrams authored for a different stage.

Working backward from what a **single OEM** actually owns and controls
(not a multi-brand marketplace) surfaces three concrete corrections:

1. **Amazon Connect is the wrong default channel for Discover.** Connect's
   value (telephony, ACD queueing, hands-free-while-driving) has no
   analog for a pre-purchase browse conversation. The OEM's own website/app
   is the surface; the agent is embedded there directly.
2. **The persona model has no context for a pre-relationship visitor.**
   `personas-and-tenancy.adoc` only names OEM owner / rental / fleet — all
   post-relationship. Discover's dominant traffic (anonymous website
   visitors) has no home in that model.
3. **Credit decisioning and contract execution were framed as
   build-from-scratch tool shells against generic partners** (FICO/Experian
   as peers, Conga/Agiloft for contracts) when the dominant real-world case
   for a single OEM is the OEM's own captive finance arm (Ford Credit /
   GM Financial pattern) and the dealer's existing DMS e-contracting
   capability — both of which already exist and are already regulated/
   audited outside CVX.

## Two entry paths into Discover/Acquire

The corrected architecture recognizes two structurally different entry
paths, not one undifferentiated "Discover stage."

### Path A — Anonymous / new-prospect Discover

A visitor with no existing OEM relationship lands on the OEM's public
website or app and starts a conversation with the embedded sales agent.

- **Channel.** Nova 2 Sonic (voice) or text chat invoked client-side from
  the OEM's website/app via API Gateway. **No Amazon Connect.** The agent
  behaves like a knowledgeable dealership salesperson at the configurator
  — feature questions, tow-capacity/pricing/lease-offer questions — not
  like a Service-stage triage bot.
- **Identity.** None required to start. An ephemeral session/device
  identifier is created at first interaction; no Cognito precondition.
- **Data.** `catalog_query()` and `offers_lookup()` against the OEM's own
  inventory (ADP Vehicle Identity Graph, sourced from dealer DMS feeds)
  and the OEM's own incentive/offer programs. No third-party
  digital-commerce-platform delegation alternate — for a single OEM, the
  OEM's own site *is* the commerce platform; there is no third-party
  inventory system of record to delegate to. (This removes the
  `catalog_query()` delegate-to-partner alternate `[24b]` and the
  Digital-commerce-partner box `E` from Diagram Baseline 5 — dropped as a
  marketplace-aggregator pattern that doesn't apply to a single-OEM
  deployment.)
- **Memory.** Session-scoped only. No LTM hydration at session start —
  there is no persona to hydrate yet for an anonymous visitor. AgentCore
  Memory (short-term, session-scoped) holds the ephemeral pre-link state:
  stated preferences, viewed configurations, current turn context.
- **Identity promotion (the entity-resolution boundary).** If the visitor
  saves a configuration, requests a callback, or authenticates:
  1. The ephemeral session is **linked**, not looked up — a deliberate
     write operation, not an automatic identity resolution. The join key
     is contact information (email or phone) captured at the moment of
     linking, or a Cognito `sub` if the visitor authenticates into an
     existing or new OEM account.
  2. A structured **lead/reservation record** is written to the **OEM's
     own CRM / lead-management system** — this is the OEM's existing
     infrastructure (every OEM of scale already runs one), not new CVX
     infrastructure. The record carries: contact info, saved
     configuration, dealer of choice (if selected), and an OEM-issued
     lead ID.
  3. **CVX does not write directly into a dealer's DMS.** DMS systems
     (Reynolds, CDK Global, Tekion, Dealertrack) are dealer-owned and
     per-vendor — the IG's own Service-stage `book()` v3 phase already
     names this as "per-tenant, per-DMS setup," i.e., a solved problem the
     OEM's CRM already owns via its existing dealer integrations. CVX's
     integration surface is the one system it can reasonably own talking
     to: the OEM's CRM. The OEM's CRM's fan-out to N dealer DMS vendors is
     the OEM's existing, already-built integration — not something CVX
     is responsible for building or maintaining.
- **Handoff shape.** A warm lead (structured record, async consumption),
  the same shape as `book()`'s STAR-JSON pattern — not a live `escalate()`
  transfer. No Connect queue involved in this path at all.

### Path B — Existing-owner re-engagement

A current OEM owner, already known to CVX via LTM from the Onboard/Own
stage, receives a proactively pushed lease-maturity or upgrade offer and
enters a new Discover/Acquire cycle from a warm start.

- **Channel.** Push notification into the OEM's existing app — the owner
  already has it; no new channel needed.
- **Identity.** Already resolved. This is the *same* "OEM owner" persona
  from `personas-and-tenancy.adoc`, simply re-entering a different journey
  stage — not a new persona category.
- **Trigger.** A business-rule event (lease-maturity date, mileage
  threshold suggesting trade-in value, new-model-year launch matching
  trim history), evaluated via the *same* Flink → MSK → threshold-eval →
  push pipeline already built for maintenance reminders in Onboard/Own —
  a different rule set and offer source, same mechanism. This reuse is
  legitimate (unlike the original Discover/Acquire reuse) because the
  precondition — an existing owner with LTM already hydrated — is
  actually satisfied here.
- **What follows is Discover-shaped.** Once the pushed offer opens a
  conversation, the *tool catalog* is the same as Path A
  (`catalog_query()`, `offers_lookup()`) — but the *session bootstrap* is
  completely different: warm, LTM-hydrated, VIN-and-persona-known, versus
  Path A's cold, session-only, anonymous start.

### Persona-model correction

`personas-and-tenancy.adoc` needs exactly one addition, not a broad
rewrite: an **anonymous / session-scoped visitor** context for Path A,
with no Cognito precondition — sitting alongside, not replacing, the
existing OEM owner / rental / fleet taxonomy. Path B requires **no**
persona-model change; it's the existing OEM-owner persona re-entering a
different stage.

## Acquire/Finance — corrected composition

Once a transaction actually starts (from either path):

- **Financing.** `finance_qualify()`'s primary target is the **OEM's own
  captive finance arm** (Ford Credit / GM Financial / Toyota Financial
  Services pattern) — an already-existing, already-regulated underwriting
  system outside CVX. Third-party lenders (credit unions, aggregators)
  are a secondary path for OEMs without a captive arm, not a peer default
  alongside FICO/Experian as previously framed.
- **Contract execution.** CVX hands off a **reservation/pre-qualification
  package** to the **dealer's existing DMS e-contracting capability**
  (Reynolds, CDK, Tekion all have this today) rather than CVX itself
  calling a generic contract-management partner (Conga/Agiloft) to
  generate a contract from scratch. This is over-scoped for what a single
  OEM needs — the dealer's DMS already does e-contracting.
- **Escalation.** This is the one place Amazon Connect legitimately
  re-enters the architecture — a live handoff to a human during a
  regulated finance conversation is a real requirement, unlike Discover's
  browse phase. The receiving party is the **dealer's F&I (finance &
  insurance) desk**, not a generic OEM-level tier-2 queue as previously
  framed — the dealer is who actually closes finance deals in most OEM
  models (direct-to-consumer OEMs like Tesla/Rivian/Lucid are the
  exception and would route to an OEM-side desk instead; tenant config
  should carry this as a routing choice, not assume one model).
- **Session/state.** Qualification result + reservation state is a
  bounded, resumable transaction — "working with a specific dealer on a
  specific reservation" — closer to a short-lived workflow than to
  open-ended LTM hydration. AgentCore Memory can still hold this
  transactional state for session-continuity purposes, but it should be
  modeled explicitly as transaction state with a lifecycle (created →
  qualified → contracted → closed/abandoned), not as a generic persona
  snapshot extended with extra fields.

## Full identity lifecycle across the revised architecture

```
ephemeral session (Path A, anonymous)
        │
        │ visitor saves config / requests callback / authenticates
        ▼
lead/reservation record (join key: email or phone + OEM lead ID)
        │
        ▼
OEM CRM / lead-management system (OEM-owned, pre-existing)
        │
        │ OEM's existing CRM→DMS integration (per-dealer, per-DMS-vendor;
        │ NOT built or owned by CVX — same "per-tenant, per-DMS setup"
        │ constraint the IG already names for book() v3)
        ▼
dealer DMS (sale closes)
        │
        │ VehicleOwnershipTransfer fires — VIN titled to customer
        ▼
VIN-linked Cognito identity (durable, CVX-native)
        │
        ▼
Onboard/Own LTM world begins — same mechanism Path B re-enters from
```

The join key that survives the CVX↔dealer boundary is **contact
information + an OEM-issued lead/reservation ID** — not a single
canonical CVX identity. CVX's identity space (Cognito `sub`) and the
dealer's identity space (DMS customer record) are genuinely separate
systems that only agree on contact info and a lead reference, until the
sale closes and a VIN-linked Cognito identity can be established with
certainty.

## Open question — flagged, not assumed away

The **OEM CRM → dealer DMS integration** is treated here as "the OEM's
existing, already-built integration — not CVX's to build." This is a
reasoned architectural boundary (grounded in the IG's own precedent that
DMS integration is per-tenant/per-vendor), but it is **not verified
against how any specific real OEM's CRM-to-DMS integration actually
works**. Before this becomes load-bearing in a shipped spec, validate
against how CVX's actual target OEM partners run this handoff — the
specific mechanics (real-time API vs. batch, what fields the DMS expects,
whether some OEMs route Discover leads to a national contact center
first) may differ from the shape assumed here.

## What changes downstream

- **Diagram Baseline 5 (Discover/Find)** — replace wholesale: drop
  Amazon Connect as ingress, drop the digital-commerce-partner delegate
  alternate (`[24b]`, box `E`), drop AgentCore Memory LTM hydration at
  session start, add the ephemeral-session model, add the lead-record
  write to an OEM CRM box, split into Path A (anonymous) and Path B
  (existing-owner re-engagement, reusing the proactive-engagement
  pipeline) as two distinct flows rather than one.
- **Diagram Baseline 6 (Acquire/Finance)** — retarget `finance_qualify()`
  to an OEM captive-finance box as primary (credit-decisioning partners
  as secondary/fallback), retarget `contract_generate()` to dealer DMS
  e-contracting rather than a generic contract-management partner, retarget
  the escalation triangle's tier-2 destination to a dealer F&I desk,
  model Acquire's AgentCore Memory usage explicitly as bounded transaction
  state rather than persona-snapshot-extended LTM.
- **`the-customer-journey.adoc`** — Discover/Find and Acquire/Finance
  composition subsections rewritten per this document. Chapter-summary
  table's Partner Integration Points columns update accordingly (drop
  Salesforce Commerce Cloud/Adobe as Discover partners; add OEM captive
  finance as the primary Acquire financing partner category).
- **`personas-and-tenancy.adoc`** — add the anonymous/session-scoped
  visitor context alongside the existing OEM owner / rental / fleet
  taxonomy.
- **`agentic-architecture.adoc`** — the one-line forward-reference sentence
  added by spec `2026-07-15-cvx-ig-agentic-depth-refresh` ("The same
  AgentCore Runtime, Nova 2 Sonic bidi transport, and Amazon Connect voice
  ingress... compose for all customer-journey stages") becomes **false**
  for Discover under this revision (no Connect ingress at Discover) and
  needs correcting to scope the claim to the stages where it actually
  holds (Acquire's escalation path, Onboard, Service) — not all four.
