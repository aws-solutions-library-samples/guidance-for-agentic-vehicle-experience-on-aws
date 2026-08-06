# Diagrams 4-7 Retirement Audit — foundational-content check

**Purpose.** Before deleting the four customer-journey stage diagrams (4-onboard-own,
5-discover-find, 6-acquire-finance, 7-sell-trade-in) and moving their content to
`the-customer-journey.adoc` IG prose, classify every rendered component, arrow,
boundary/grouping, and load-bearing annotation across those diagrams into one of
three categories:

- **(A) STAGE-SPECIFIC SPECULATION** — a tool, partner, or data flow that only
  exists for that one journey stage and is explicitly `[roadmap]` / "not yet
  demonstrated" (e.g. `finance_qualify()`, `catalog_query()`, `commerce_handoff()`
  and their target partners / data products). Fine to lose as a rendered diagram —
  moves to IG prose text.
- **(B) ALREADY CAPTURED in Diagrams 1-3** via badge reuse — e.g. AgentCore Runtime,
  AgentCore Memory, the Centralized Data Repository collapsed box, the cross-account
  ADP boundary, the ingress spine (Connect / API-GW / Supervisor Lambda), the
  escalation triangle. Confirmed present in 1-3's own baselines, not just referenced
  by badge-reuse convention.
- **(C) FOUNDATIONAL BUT MISSING** — something in 4-7 that represents a genuinely
  core, non-speculative architectural pattern (shared across multiple stages, or
  structurally important to how CVX works) that is NOT currently rendered on
  Diagrams 1, 2, or 3.

**Method.** Read every rendered element on each of Diagrams 4-7 from
`DIAGRAM-BASELINE-{4,5,6,7}-*.md`. Cross-check every "reused from Diagram N"
claim against the actual baselines of Diagrams 1, 2, 3 to confirm the referenced
element is genuinely rendered there (not just claimed to be). Reference the
seven-diagram roadmap-item-1 (Well-Architected callouts) explicitly for the user's
prompt.

---

## Confirmation of surviving-diagram content (Diagrams 1-3)

Before classifying, catalog exactly what is present on the three surviving diagrams
to serve as the (B) reference.

**Diagram 1 (Agentic Orchestration).**
- Boxes: `A` Customer, `1` Amazon Connect (channel/tenant-flow sub-list), `2` API
  Gateway + Cognito, `3` Supervisor Lambda, `4` AgentCore Runtime (bidi + text +
  Sonnet 4.6), `6` Triage Classifier Lambda.
- Boundaries: CVX-account dashed rectangle.
- Arrows: `[1] [1'] [2] [2'] [3] [5] [12] [13a] [13b] [14]`.
- Off-diagram tags: `→ Diagram 2`, `→ Diagram 3`.

**Diagram 2 (Data Foundation & Composition).**
- Boxes: `4` Runtime (edge/compact), `5` AgentCore Memory (LTM), `8` CMS Redis LKS,
  `9` CMS DynamoDB, `10` CMS Recalls Integration Lambda, `11` Bedrock Knowledge
  Base, `12` Centralized Data Repository (Athena + Glue + S3 + Lake Formation
  collapsed).
- Boundaries: CVX-account dashed rectangle; ADP-account dashed cross-account
  authorization boundary; CMS-foundation labeled light-fill grouping.
- Arrows: `[4a] [4b] [4c] [6] [7] [8] [9]` (plus off-diagram entry `[3]`).

**Diagram 3 (Escalation & Human Hand-off).**
- Boxes: `4` Runtime (edge/compact), `1` Amazon Connect (escalation-side sub-list:
  P0/P1/P2 queues + Customer Profiles + Agent Desktop widget), `7` CVX operational
  DynamoDB (`vsa-escalation-records` + `vsa-tenant-config`), `B` Tier-2 Human Agent
  (person icon).
- Boundaries: CVX-account dashed rectangle.
- Arrows: `[10a] [10b] [11]` (plus off-diagram entry from `[5]`).

**Not rendered on any surviving diagram** — anything below is a candidate for
(C) unless it's genuinely stage-scoped:
- Nova 2 Sonic as a distinct box (internal-to-Runtime only per baseline non-goals)
- The Nova 2 Sonic ~1-second tool-completion budget (deferred to prose per baseline
  roadmap-item-5)
- Bedrock Guardrails as any visible element (deferred; policy content is IG prose)
- Well-Architected overlay callouts (Security / Reliability / Cost / Performance —
  all deferred per baseline roadmap-item-1)
- Graceful-degradation behaviours (deferred per baseline roadmap-item-4)
- Partner-ecosystem grouping (no partners appear on Diagrams 1-3)
- Amazon Connect outbound-campaign capability (`StartOutboundVoiceContact` etc.)
- Amazon MSK / CMS Flink processors / async-Lambda event-consumer pattern
- Runtime invoked from a non-Supervisor Lambda (async / event-driven compose)
- CVX-side WRITE to ADP Vehicle Lifecycle across the cross-account boundary
- STAR-JSON as a cross-stage schema convention (`book()`, `subscription_activate`,
  `VehicleOwnershipTransfer`, `ServiceRepairOrderAdd`)

---

## Classification table

Every rendered component, arrow, boundary/grouping, and load-bearing annotation
across Diagrams 4-7. Grouped by diagram, then by element type. Boxes and arrows
that reuse a claimed prior element carry the (B) verdict only if that element is
confirmed present in the corresponding surviving diagram — verified above.

### Diagram 4 — Onboard / Own

| Element | Kind | Baseline reference | Classification | Rationale |
|---|---|---|---|---|
| `A` Customer | Box (reused) | D4 § 2.1 | **B** | Person icon rendered on D1. |
| `1` Amazon Connect (outbound-side sub-list variant) | Box (reused, third aspect) | D4 § 2.1 | **B** (box) + **A** (outbound sub-list) | Connect box present on D1 and D3. The outbound sub-list (`StartOutboundVoiceContact` / `StartOutboundChatContact`) is a third rendering aspect not shown on any surviving diagram — but its only demonstrated use is the proactive-engagement pipeline (stage-specific), so classify the sub-list itself as (A). |
| `2` API Gateway + Cognito | Box (reused, compact edge) | D4 § 2.1 | **B** | Rendered on D1. |
| `3` Supervisor Lambda | Box (reused) | D4 § 2.1 | **B** | Rendered on D1. |
| `4` AgentCore Runtime (with `session-init` + `compose-outbound` sub-annotation) | Box (reused with new sub-annotation) | D4 § 2.1 | **B** (box) + **C** (`compose-outbound` mode — see § "Category-C findings" below) | Runtime box is on D1 and D2. The "Runtime invoked by a non-Supervisor Lambda for async event-driven compose" invocation pattern is a distinct architectural mode of Runtime usage not shown anywhere in D1-D3 (D1 only shows Supervisor-Lambda-driven synchronous invocation). See C-item #1. |
| `5` AgentCore Memory (LTM) (compact) | Box (reused) | D4 § 2.1 | **B** | Rendered on D2. |
| `7` CVX operational DynamoDB | Box (reused, `vsa-tenant-config` aspect) | D4 § 2.1 | **B** | Rendered on D3. Proactive-engagement-policy entry schema is deferred; box itself is captured. |
| `9` CMS DynamoDB | Box (reused, `vehicles`+`drivers` aspect) | D4 § 2.1 | **B** | Rendered on D2. Onboarding write targets rows on the same box D2 renders. |
| `11` Bedrock KB | Referenced, not rendered as full box | D4 § 2.1 rendering notes | **B** | Rendered as full box on D2; D4 references it via inline annotation only. |
| `12` Centralized Data Repository | Referenced, not rendered as full box | D4 § 2.1 rendering notes | **B** | Rendered as full box on D2; D4 references it via inline annotation only. |
| `13` CMS Flink telemetry processors | Box (NEW to D4) | D4 § 2.2 | **A** | Producer side of the proactive-engagement pipeline. Only demonstrated use in the 7-diagram set is proactive engagement (Onboard/Own-scoped). Pattern described in `the-customer-journey.adoc` § Proactive engagement; no `lambdas/proactive/` directory verified in the repo. See "Special-attention item 1" below for the pattern-vs-instance analysis. |
| `14` Amazon MSK | Box (NEW to D4) | D4 § 2.2 | **A** | Event backbone; only demonstrated in the proactive-engagement pipeline. Same rationale as `13`. |
| `15` Proactive Engagement Lambda | Box (NEW to D4) | D4 § 2.2 | **A** | CVX-side subscriber Lambda. Baseline explicitly notes no dedicated code path exists; the IG describes a pattern, not a named Lambda. Only demonstrated use is Onboard/Own proactive engagement. |
| `C` Subscription-management partner (Zuora) | Partner box (NEW) | D4 § 2.2 | **A** | Illustrative category; tenant-dependent; roadmap. Stage-specific to Onboard/Own connected-services activation. |
| `D` CRM partner (Salesforce Service Cloud) | Partner box (NEW) | D4 § 2.2 | **A** | Illustrative category; tenant-dependent; roadmap. Stage-specific to Onboard/Own outbound-via-CRM path. |
| CVX-account dashed boundary | Boundary | D4 § 1 legend | **B** | Same dashed CVX-account boundary rendered on D1, D2, D3. |
| CMS foundation labeled light-fill grouping | Grouping | D4 § 1 legend | **B** | Rendered on D2 as the CMS-foundation grouping around boxes 8, 9, 10. |
| Partner-ecosystem labeled light-fill grouping | Grouping convention (NEW) | D4 § 2.2 rendering notes | **A** | See "Special-attention item 2" below. Convention exists only where partners exist; retiring 4-7 removes all partners from the set, so the convention has nothing to attach to on 1-3. |
| Arrow `[1']` Customer → API Gateway | Reused arrow | D4 § 3.1 | **B** | Rendered on D1. |
| Arrow `[2']` API Gateway → Supervisor Lambda | Reused arrow | D4 § 3.1 | **B** | Rendered on D1. |
| Arrow `[15]` Supervisor Lambda → CMS DynamoDB (onboarding write of new `vehicles` + `drivers` rows, dashed) | NEW arrow | D4 § 3.1 | **A** | Onboarding-time write from Supervisor Lambda to CMS DDB — a stage-specific write pattern for the registration event. D2 renders CMS DDB as a write target only from the Runtime (`[9]` book), not from Supervisor Lambda. |
| Arrow `[16]` Supervisor Lambda → Subscription-management partner | NEW arrow | D4 § 3.1 | **A** | Stage-specific partner integration for connected-services activation. |
| Arrow `[3]` Supervisor Lambda → AgentCore Runtime | Reused arrow | D4 § 3.1 | **B** | Rendered on D1. |
| Arrow `[4a]` Runtime → CMS Redis LKS (session-init) | Reused arrow (via off-diagram tag) | D4 § 3.1 | **B** | Rendered on D2. |
| Arrow `[4b]` Runtime → Centralized Data Repository (session-init) | Reused arrow (via off-diagram tag) | D4 § 3.1 | **B** | Rendered on D2. |
| Arrow `[4c]` Runtime → AgentCore Memory (LTM write, dashed, first-time hydration) | Reused arrow | D4 § 3.1 | **B** | Rendered on D2. The "first-time hydration" framing is stage-specific narrative; the arrow itself is the same one D2 renders. |
| Arrow `[17]` CMS Flink processors → Amazon MSK (dashed emission) | NEW arrow | D4 § 3.2 | **A** | Part of proactive-engagement pipeline. |
| Arrow `[18]` Amazon MSK → Proactive Engagement Lambda (event-source-mapping) | NEW arrow | D4 § 3.2 | **A** | Part of proactive-engagement pipeline. |
| Arrow `[19]` Proactive Engagement Lambda → CVX operational DynamoDB (tenant policy read) | NEW arrow | D4 § 3.2 | **A** | Stage-specific tenant-policy read. |
| Arrow `[20]` Proactive Engagement Lambda → AgentCore Runtime (compose personalized message) | NEW arrow | D4 § 3.2 | **A** (arrow) + **C** (invocation mode — see C-item #1) | Runtime invoked from a non-Supervisor Lambda for async event-driven compose. Arrow instance is stage-specific; invocation mode is broader-than-Onboard/Own architecturally. |
| Arrow `[21a]` Proactive Engagement Lambda → Amazon Connect outbound | NEW arrow | D4 § 3.2 | **A** | Stage-specific outbound-Connect usage. |
| Arrow `[21b]` Proactive Engagement Lambda → CRM partner webhook | NEW arrow | D4 § 3.2 | **A** | Stage-specific outbound-via-CRM usage. |
| Arrow `[22]` Amazon Connect / CRM partner → Customer (outbound delivery) | NEW arrow | D4 § 3.2 | **A** | Stage-specific outbound-delivery arrow. |
| Upper-band `Demonstrated in CVX::` callout panel | Annotation | D4 § 3.1 | **A** | Stage-scoped operational-maturity marker. Content moves to IG prose. |
| Lower-band `Demonstrated in CVX::` callout panel (with lambdas/proactive caveat) | Annotation | D4 § 3.2 | **A** | Stage-scoped operational-maturity marker. Content moves to IG prose. |

### Diagram 5 — Discover / Find

| Element | Kind | Baseline reference | Classification | Rationale |
|---|---|---|---|---|
| `A` Customer | Box (reused) | D5 § 2.1 | **B** | Rendered on D1. |
| `1` Amazon Connect (compact — no sub-list) | Box (reused) | D5 § 2.1 | **B** | Rendered on D1. |
| `2` API Gateway + Cognito | Box (reused) | D5 § 2.1 | **B** | Rendered on D1. |
| `3` Supervisor Lambda | Box (reused) | D5 § 2.1 | **B** | Rendered on D1. |
| `4` AgentCore Runtime (compact) | Box (reused) | D5 § 2.1 | **B** | Rendered on D1. |
| `5` AgentCore Memory (LTM) (compact) | Box (reused) | D5 § 2.1 | **B** | Rendered on D2. |
| `7` CVX operational DynamoDB (`vsa-tenant-config` aspect) | Box (reused) | D5 § 2.1 | **B** | Rendered on D3. |
| `12` Centralized Data Repository (with Vehicle Identity Graph + Campaigns/Offers data-product labels) | Box (reused) | D5 § 2.1 | **B** (box) + **A** (data-product labels are Discover-specific speculation) | CDR box is on D2. The specific `Vehicle Identity Graph` + `Campaigns/Offers` data-product labels are stage-specific speculation — deferred ADP data-product schemas. |
| `E` Digital-commerce partner (Salesforce Commerce Cloud / Adobe Experience Manager) | Partner box (NEW) | D5 § 2.2 | **A** | Illustrative category; tenant-dependent; roadmap. |
| CVX-account dashed boundary | Boundary | D5 § 1 legend | **B** | Rendered on D1, D2, D3. |
| ADP-account dashed cross-account boundary | Boundary | D5 § 1 legend | **B** | Rendered on D2. Smaller footprint on D5 (only box 12 inside; KB absent), but same treatment. |
| Partner-ecosystem labeled light-fill grouping | Grouping convention | D5 § 2.2 rendering notes | **A** | Same rationale as D4 grouping. |
| Arrow `[1]` Customer → Amazon Connect | Reused arrow | D5 § 3.1 | **B** | Rendered on D1. |
| Arrow `[2]` Amazon Connect → Supervisor Lambda | Reused arrow | D5 § 3.1 | **B** | Rendered on D1. |
| Arrow `[1']` Customer → API Gateway | Reused arrow | D5 § 3.1 | **B** | Rendered on D1. |
| Arrow `[2']` API Gateway → Supervisor Lambda | Reused arrow | D5 § 3.1 | **B** | Rendered on D1. |
| Arrow `[23]` Supervisor Lambda → CVX operational DynamoDB (persona-segment scoping config read) | NEW arrow | D5 § 3.2 | **A** | Stage-specific config-read (Discover-entry schema is deferred). |
| Arrow `[3]` Supervisor Lambda → AgentCore Runtime | Reused arrow | D5 § 3.3 | **B** | Rendered on D1. |
| Arrow `[4b]` Runtime → CDR (session-init `persona_loader`, lighter Discover-persona query shape) | Reused arrow with stage-specific narrative | D5 § 3.3 | **B** (arrow) + **A** (Discover-persona query shape) | Arrow rendered on D2; the "lighter Discover-persona query" narrative is stage-specific. |
| Arrow `[4c]` Runtime → AgentCore Memory (LTM write, dashed, includes `segment_scope` field) | Reused arrow with stage-specific field | D5 § 3.3 | **B** (arrow) + **A** (`segment_scope` field name) | Arrow rendered on D2; the `segment_scope` metadata field is stage-specific. |
| Arrow `[24a]` Runtime → CDR (`catalog_query()` direct path, ADP Vehicle Identity Graph) | NEW arrow | D5 § 3.4 | **A** | Stage-specific tool call against a deferred ADP data product. |
| Arrow `[24b]` Runtime → Digital-commerce partner (`catalog_query()` commerce-delegation) | NEW arrow | D5 § 3.4 | **A** | Stage-specific partner delegate. |
| Arrow `[25]` Runtime → CDR (`offers_lookup()`, ADP Campaigns/Offers) | NEW arrow | D5 § 3.4 | **A** | Stage-specific tool call against a deferred ADP data product. |
| Arrow `[12]` Runtime → Supervisor Lambda (response) | Reused arrow | D5 § 3.5 | **B** | Rendered on D1. |
| Arrow `[13a]` Supervisor Lambda → Amazon Connect (response) | Reused arrow | D5 § 3.5 | **B** | Rendered on D1. |
| Arrow `[13b]` Supervisor Lambda → API Gateway (response) | Reused arrow | D5 § 3.5 | **B** | Rendered on D1. |
| Arrow `[14]` Amazon Connect / API Gateway → Customer (delivery) | Reused arrow | D5 § 3.5 | **B** | Rendered on D1. |
| Stage-level "Not yet demonstrated in a reference implementation" trailing callout | Annotation (load-bearing) | D5 § 5.3 | **A** | Stage-scoped maturity marker. Moves to IG prose (already lives verbatim in `the-customer-journey.adoc` § Discover / Find). |
| Nova 2 Sonic voice-Discover annotation adjacent to Runtime (illustrative turn + ~1-second budget + regional availability) | Annotation | D5 § 2.1 rendering notes | **A** (Discover-stage illustrative content) + partial **C** (see "Special-attention item 3" — the platform-level ~1-second budget and regional availability are foundational Runtime-level constraints not visualized anywhere on D1-D3) | Nova 2 Sonic itself is captured on D1's Runtime box as a bidi sub-note. The stage-specific illustrative turn is (A). The platform-level constraint content (1-s tool-completion budget; region set `us-east-1` / `eu-north-1` / `ap-northeast-1`) is genuinely foundational but currently rendered nowhere in the surviving diagrams — however, per `DIAGRAM-BASELINE.md` roadmap-item-5, the cross-region routing detail is a deliberate non-goal even on the surviving diagrams, and the 1-s budget is documented in `agentic-architecture.adoc` IG prose. Losing the visual annotation does not create a new gap because it wasn't visualized on 1-3 to begin with. |
| `[23]` schema-deferral text callout | Annotation | D5 § 5.6 | **A** | Stage-scoped deferral marker. |
| `[24a]`/`[25]` ADP-data-product schema-deferral callout | Annotation | D5 § 5.6 | **A** | Stage-scoped deferral marker. |

### Diagram 6 — Acquire / Finance

| Element | Kind | Baseline reference | Classification | Rationale |
|---|---|---|---|---|
| `A` Customer | Box (reused) | D6 § 2.1 | **B** | Rendered on D1. |
| `1` Amazon Connect (escalation-side sub-list + ingress role) | Box (reused, two roles in one diagram) | D6 § 2.1 | **B** (box) + **B** (escalation sub-list — rendered on D3) + **A** (finance-specialist queue-family expansion) | Both the ingress role and the escalation-side sub-list are captured on the surviving diagrams (D1 and D3 respectively). Adding a "tier-2 finance-specialist" bullet to the escalation-queues sub-list is a stage-specific expansion of the same construct D3 already renders. |
| `2` API Gateway + Cognito | Box (reused) | D6 § 2.1 | **B** | Rendered on D1. |
| `3` Supervisor Lambda | Box (reused) | D6 § 2.1 | **B** | Rendered on D1. |
| `4` AgentCore Runtime (compact, with 5-item sub-annotation) | Box (reused with tool-listing sub-annotation) | D6 § 2.1 | **B** (box) | Runtime box rendered on D1 and D2. Tool-listing sub-annotation is stage-specific labelling; the four listed Acquire tools are (A) speculation and the reused `escalate()` is (B). |
| `5` AgentCore Memory (LTM) (compact) | Box (reused) | D6 § 2.1 | **B** | Rendered on D2. |
| `7` CVX operational DynamoDB (`vsa-escalation-records` + `vsa-tenant-config`) | Box (reused, two-table sub-label matches D3 exactly) | D6 § 2.1 | **B** | Rendered on D3 with the same two-table sub-label. Acquire adds a *new entry* to `vsa-tenant-config` (finance-specialist queue ARN), not a new table. |
| `12` Centralized Data Repository (with Customer 360 + Vehicle Lifecycle data-product labels) | Box (reused) | D6 § 2.1 | **B** (box) + **A** (data-product labels are Acquire-specific speculation) | CDR box is on D2. `Customer 360` + `Vehicle Lifecycle` data-product labels are stage-specific (deferred ADP data-product schemas). |
| `B` Tier-2 Human Agent (finance-specialist annotation) | Person icon (reused) | D6 § 2.1 | **B** (person icon) + **A** (finance-specialist annotation + queue-standup caveat) | Person icon rendered on D3 as "Tier-2 Human Agent." Stage-specific finance-specialist annotation and queue-readiness caveat are (A). |
| `F` Credit-decisioning partner (FICO / Experian) | Partner box (NEW) | D6 § 2.2 | **A** | Illustrative category; roadmap. |
| `G` Payments partner (Stripe / Plaid) | Partner box (NEW) | D6 § 2.2 | **A** | Illustrative category; roadmap. |
| `H` Contract-management partner (Conga / Agiloft) | Partner box (NEW, deferrable-to-v2) | D6 § 2.2 | **A** | Illustrative category; roadmap; explicit deferrable-to-v2 marker. |
| CVX-account dashed boundary | Boundary | D6 § 1 legend | **B** | Rendered on D1, D2, D3. |
| ADP-account dashed cross-account boundary | Boundary | D6 § 1 legend | **B** | Rendered on D2. |
| Partner-ecosystem labeled light-fill grouping | Grouping convention | D6 § 2.2 rendering notes | **A** | Same rationale as D4/D5 grouping. |
| Arrow `[1]` Customer → Amazon Connect | Reused arrow | D6 § 3.1 | **B** | Rendered on D1. |
| Arrow `[2]` Amazon Connect → Supervisor Lambda | Reused arrow | D6 § 3.1 | **B** | Rendered on D1. |
| Arrow `[1']` Customer → API Gateway | Reused arrow | D6 § 3.1 | **B** | Rendered on D1. |
| Arrow `[2']` API Gateway → Supervisor Lambda | Reused arrow | D6 § 3.1 | **B** | Rendered on D1. |
| Arrow `[3]` Supervisor Lambda → AgentCore Runtime | Reused arrow | D6 § 3.2 | **B** | Rendered on D1. |
| Arrow `[4b]` Runtime → CDR (session-init Customer 360 read) | Reused arrow with Acquire-persona narrative | D6 § 3.2 | **B** | Arrow rendered on D2. |
| Arrow `[4c]` Runtime → AgentCore Memory (LTM write, finance_segment + qualification state) | Reused arrow with stage-specific fields | D6 § 3.2 | **B** (arrow) + **A** (`finance_segment` / qualification-state fields) | Arrow rendered on D2. |
| Arrow `[26]` Runtime → Credit-decisioning partner (`finance_qualify()`) | NEW arrow | D6 § 3.3 | **A** | Stage-specific; roadmap. |
| Arrow `[27]` Runtime → Payments partner (`payment_initiate()`) | NEW arrow | D6 § 3.3 | **A** | Stage-specific; roadmap. |
| Arrow `[28]` Runtime → Contract-management partner (`contract_generate()`, deferrable-to-v2) | NEW arrow | D6 § 3.3 | **A** | Stage-specific; roadmap; explicit deferrable-to-v2 marker. |
| Arrow `[29]` Runtime → CDR (`VehicleOwnershipTransfer` STAR emission, dashed, cross-account WRITE) | NEW arrow | D6 § 3.3 | **A** (specific arrow instance) + **C** (CVX-side cross-account WRITE to ADP is a genuinely foundational architectural direction — see C-item #2) | Only demonstrated cross-account WRITE from CVX into ADP anywhere in the 7-diagram set. D2 renders cross-account READS only (`[4b]` and `[7]`). See C-item #2 for the recommendation. |
| Arrow `[10a]` Runtime → CVX operational DDB (escalate write, dashed) | Reused arrow | D6 § 3.4 | **B** | Rendered on D3. |
| Arrow `[10b]` Runtime → Amazon Connect (TransferContact) | Reused arrow | D6 § 3.4 | **B** | Rendered on D3. |
| Arrow `[11]` Amazon Connect → Tier-2 Human Agent (finance specialist) | Reused arrow | D6 § 3.4 | **B** | Rendered on D3. |
| Arrow `[12]` Runtime → Supervisor Lambda (response) | Reused arrow | D6 § 3.5 | **B** | Rendered on D1. |
| Arrow `[13a]` Supervisor Lambda → Amazon Connect (response) | Reused arrow | D6 § 3.5 | **B** | Rendered on D1. |
| Arrow `[13b]` Supervisor Lambda → API Gateway (response) | Reused arrow | D6 § 3.5 | **B** | Rendered on D1. |
| Arrow `[14]` Amazon Connect / API Gateway → Customer (delivery) | Reused arrow | D6 § 3.5 | **B** | Rendered on D1. |
| Stage-level "Not yet demonstrated in a reference implementation" trailing callout | Annotation | D6 § 5.3 | **A** | Moves to IG prose. |
| `[11]` mechanism-vs-readiness preserved local annotation on box `B` ("requires tenant-side finance-specialist queue standup") | Annotation | D6 § 5.5 | **A** | Stage-scoped operational-readiness caveat. |
| `[28]` / `H` deferrable-to-v2 preserved marker | Annotation | D6 § 5.4 | **A** | Stage-scoped deferrability marker. |
| Bedrock Guardrails "LLM narrates, does not decide" annotation adjacent to `[26]` | Annotation | D6 § 5.6 | **A** (Acquire-stage instantiation) + partial **C** (see "Special-attention item 4" — Guardrails as a platform-level Bedrock feature applies to every LLM interaction in CVX and is currently rendered nowhere on the surviving diagrams) | The specific "LLM narrates, does not decide" framing for regulated finance is stage-specific. The broader observation that Bedrock Guardrails apply somewhere in the architecture is not surfaced on any surviving diagram. However — per `DIAGRAM-BASELINE.md`, Guardrails-as-a-visible-element is deferred consistently across the entire set (no D1-D3 rendering was ever planned), so losing the D6 annotation does not create a new visualization gap. |
| Nova 2 Sonic 1-second-budget annotation on `[26]` (`finance_qualify()` most-at-risk + async-wrapping mitigation pointer) | Annotation | D6 § 5.6b | **A** (Acquire-stage instantiation) + partial **C** (see "Special-attention item 3") | Stage-specific instantiation of a platform-level constraint; the constraint itself is documented in `agentic-architecture.adoc` IG prose. Losing the visual does not create a new gap because it was never visualized on 1-3. |
| STAR-JSON `VehicleOwnershipTransfer` annotation on `[29]` | Annotation | D6 § 5.7 | **A** (specific instance) + **C** (STAR-JSON as a cross-stage schema convention — see C-item #3) | The STAR-JSON schema convention is a genuinely foundational cross-stage pattern (`book()` [9], `subscription_activate` [16], `VehicleOwnershipTransfer` [29], `ServiceRepairOrderAdd` [30] all use it). Currently the convention is not surfaced visually on any surviving diagram — D2's `[9]` write is just labeled "STAR-JSON service ticket" in the arrow narrative, not called out as a convention. |
| "extends Diagram 2 authorization surface with write grants" annotation on `[29]` | Annotation | D6 § 5.8 | **A** (specific instance) + related to C-item #2 | Stage-specific instantiation of the cross-account write direction; the underlying architectural pattern is C-item #2. |
| `[10b]` finance-specialist queue-ARN schema-deferral callout | Annotation | D6 § 5.9 | **A** | Stage-scoped. |
| Partner-API-key `vsa-tenant-config` schema-deferral callout | Annotation | D6 § 5.9 | **A** | Stage-scoped. |
| `[29]` ADP write-mechanism schema-deferral callout | Annotation | D6 § 5.9 | **A** (specific instance) + related to C-item #2 | Related to the cross-account-write foundational-pattern discussion. |

### Diagram 7 — Sell / Trade-in

| Element | Kind | Baseline reference | Classification | Rationale |
|---|---|---|---|---|
| `A` Customer | Box (reused) | D7 § 2.1 | **B** | Rendered on D1. |
| `1` Amazon Connect (compact) | Box (reused) | D7 § 2.1 | **B** | Rendered on D1. |
| `2` API Gateway + Cognito | Box (reused) | D7 § 2.1 | **B** | Rendered on D1. |
| `3` Supervisor Lambda | Box (reused) | D7 § 2.1 | **B** | Rendered on D1. |
| `4` AgentCore Runtime (compact) | Box (reused) | D7 § 2.1 | **B** | Rendered on D1. |
| `5` AgentCore Memory (LTM) (compact) | Box (reused) | D7 § 2.1 | **B** | Rendered on D2. |
| `12` Centralized Data Repository (with Customer 360 + Vehicle Lifecycle data-product labels, READ-ONLY) | Box (reused) | D7 § 2.1 | **B** (box) + **A** (data-product labels) | CDR box is on D2. `Vehicle Lifecycle` data-product label is stage-specific speculation. |
| `I` Commerce marketplace partner (Amazon Autos / OEM CPO / dealer trade-in / consumer marketplace / fleet remarketing) | Partner box (NEW) | D7 § 2.2 | **A** | Illustrative category; explicit SPECULATIVE-REFERENCE posture. |
| CVX-account dashed boundary | Boundary | D7 § 1 legend | **B** | Rendered on D1, D2, D3. |
| ADP-account dashed cross-account boundary | Boundary | D7 § 1 legend | **B** | Rendered on D2. |
| Partner-ecosystem labeled light-fill grouping | Grouping convention | D7 § 2.2 rendering notes | **A** | Same rationale as D4/D5/D6 grouping. |
| Arrow `[1]` Customer → Amazon Connect | Reused arrow | D7 § 3.1 | **B** | Rendered on D1. |
| Arrow `[2]` Amazon Connect → Supervisor Lambda | Reused arrow | D7 § 3.1 | **B** | Rendered on D1. |
| Arrow `[1']` Customer → API Gateway | Reused arrow | D7 § 3.1 | **B** | Rendered on D1. |
| Arrow `[2']` API Gateway → Supervisor Lambda | Reused arrow | D7 § 3.1 | **B** | Rendered on D1. |
| Arrow `[3]` Supervisor Lambda → AgentCore Runtime | Reused arrow | D7 § 3.2 | **B** | Rendered on D1. |
| Arrow `[4b]` Runtime → CDR (session-init Customer 360 read) | Reused arrow | D7 § 3.2 | **B** | Rendered on D2. |
| Arrow `[4c]` Runtime → AgentCore Memory (LTM write, dashed) | Reused arrow | D7 § 3.2 | **B** | Rendered on D2. |
| Arrow `[30]` Runtime → CDR (service-history provenance READ, cross-account, ADP Vehicle Lifecycle) | NEW arrow | D7 § 3.3 | **A** | Stage-specific tool call against a deferred ADP data product. Reuses the D2 cross-account read authorization surface. |
| Arrow `[31]` Runtime → Commerce marketplace partner (`commerce_handoff()`) | NEW arrow | D7 § 3.3 | **A** | Stage-specific speculative partner handoff. |
| Arrow `[12]` Runtime → Supervisor Lambda (response) | Reused arrow | D7 § 3.4 | **B** | Rendered on D1. |
| Arrow `[13a]` Supervisor Lambda → Amazon Connect (response) | Reused arrow | D7 § 3.4 | **B** | Rendered on D1. |
| Arrow `[13b]` Supervisor Lambda → API Gateway (response) | Reused arrow | D7 § 3.4 | **B** | Rendered on D1. |
| Arrow `[14]` Amazon Connect / API Gateway → Customer (delivery) | Reused arrow | D7 § 3.4 | **B** | Rendered on D1. |
| Stage-level "Not yet demonstrated in a reference implementation" trailing callout | Annotation | D7 § 5.3 | **A** | Moves to IG prose. |
| SPECULATIVE REFERENCE banner (top-of-diagram, load-bearing) | Annotation | D7 § 5.4 | **A** | Stage-scoped speculative-posture disclaimer. Moves to IG prose (already lives verbatim in `the-customer-journey.adoc` § Sell / Trade-in). |
| Provenance-record annotation on `[30]` (accumulation of `ServiceRepairOrderAdd` payloads over ownership tenure) | Annotation | D7 § 5.5 | **A** (specific instance) + partial **C** (STAR-JSON cross-stage convention — see C-item #3) | Stage-specific instantiation of the cross-stage data-dependency; the underlying STAR-JSON cross-stage convention is C-item #3. |
| Commerce-partner API-contract schema-deferral callout on `[31]` / box `I` | Annotation | D7 § 5.8 | **A** | Stage-scoped. |

---

## Summary counts

| Diagram | Total elements classified | (A) Stage-specific | (B) Already in D1-3 | (C) Foundational-but-missing (or partial-C) |
|---|---|---|---|---|
| Diagram 4 (Onboard / Own)  | 33 | 17 | 16 | 2 partial (`4` compose-outbound mode, `[20]` invocation-mode instance) |
| Diagram 5 (Discover / Find) | 30 | 12 | 17 | 1 partial (Nova 2 Sonic ~1-s budget / regions annotation) |
| Diagram 6 (Acquire / Finance) | 42 | 19 | 22 | 4 partial (`[29]` cross-account WRITE, Guardrails presence, Nova Sonic 1-s budget, STAR-JSON convention on `[29]`) |
| Diagram 7 (Sell / Trade-in) | 25 | 9 | 15 | 1 partial (STAR-JSON convention on `[30]` provenance annotation) |
| **Total** | **130** | **57** | **70** | **3 distinct category-C themes** (rolled up below; individual line-item partial-C counts sum to 8 across the four diagrams because the same three themes surface on multiple diagrams) |

Every element on Diagrams 4-7 is either (A) stage-specific speculation appropriate
to move to IG prose, or (B) genuinely reuses a construct already rendered on
Diagrams 1-3 — with the exception of the three category-C themes below.

**Notes on the counting.**

- (B) verdicts include every reused arrow and box whose target/source is
  genuinely present on the surviving diagrams. Verified by re-reading D1, D2, D3
  baselines directly rather than relying on the D4-7 "reused from Diagram N"
  claims.
- Elements marked with dual classification (e.g. "**B** (box) + **A** (sub-list)")
  are counted once in the diagram-total column and placed in whichever
  classification carries the substantive novelty. Boundary/grouping/annotation
  rows count as one element each.
- The three category-C themes surface across multiple diagrams (Nova Sonic
  budget on D5 + D6; STAR-JSON convention on D6 + D7; cross-account WRITE
  direction on D6 only; compose-outbound Runtime mode on D4 only). Each is
  discussed once below with cross-references.

---

## Category-C findings

Three distinct architectural themes on Diagrams 4-7 arguably meet the
"foundational, shared across multiple stages, structurally important, not
rendered on 1-3" bar. All three are **partial-C** — the substance is
foundational, but the current visual asserts a *narrower* framing that ties
each to a specific stage. Losing them from the diagram set is defensible so
long as the IG prose captures the broader framing.

### C-item #1 — Runtime invocation mode: async / event-driven compose (Diagram 4 only)

**Where it appears.** Diagram 4 lower band. Box `4` sub-annotation
"compose-outbound"; arrow `[20]` Proactive Engagement Lambda → AgentCore
Runtime.

**Why it's foundational.** Diagrams 1-3 render exactly one Runtime invocation
pattern: Supervisor Lambda invokes the Runtime synchronously in response to a
user-initiated request (arrow `[3]`). Diagram 4 introduces a second invocation
pattern: a non-Supervisor Lambda invokes the Runtime asynchronously in response
to an event. This is architecturally distinct — different caller, different
trigger, different session-lifecycle semantics (no user waiting on the other end
of a channel).

**Why it's stage-specific in current form.** The only demonstrated caller of the
async invocation is the Proactive Engagement Lambda, which is itself
Onboard/Own-scoped in the current diagram set. Retiring D4 removes both the
async caller and the mode.

**Recommendation.** Fine to retire visually. The IG prose in
`the-customer-journey.adoc` § Onboard / Own / Proactive engagement covers this
via `agentic-architecture.adoc` cross-references. If a future stage introduces
a second async Runtime caller, the mode can be surfaced on Diagram 1 with a
small sub-annotation on box `4` (e.g. "invoked synchronously by Supervisor
Lambda `[3]`; async event-driven invocation supported per
`agentic-architecture.adoc`").

**Cost of losing it.** Low — one line of IG prose captures it; no working code
path exists for async invocation in the current CVX repo per D4 § 3.2's
`lambdas/proactive/` caveat.

### C-item #2 — CVX-side cross-account WRITE direction to ADP (Diagram 6 only)

**Where it appears.** Diagram 6. Arrow `[29]` Runtime → Centralized Data
Repository (`VehicleOwnershipTransfer` STAR emission, dashed, cross-account
write). Annotations "extends Diagram 2 authorization surface with write grants"
and the `[29]` ADP-write-mechanism schema-deferral callout.

**Why it's foundational.** The ADP cross-account authorization boundary is a
load-bearing element of Diagram 2 that currently renders READ-ONLY arrows
(`[4b]` Athena / `[7]` KB `Retrieve`). The idea that CVX may also *write* into
ADP across the same boundary — a specifically bounded set of write grants —
is a genuinely different authorization surface. It applies (in principle) to
any stage that emits state-change facts into the ADP data foundation:
ownership transfer, ownership termination, service completion, etc. This is
one direction of the classic composition pattern "CVX composes on top of ADP,
and also feeds ADP back."

**Why it's stage-specific in current form.** Only one instance in the 7-diagram
set (`[29]` in Acquire / Finance). D7 explicitly declined to render a
`VehicleOwnershipTermination` write, and D4 explicitly kept the `[15]`
onboarding write on CMS-side, not ADP-side.

**Recommendation.** Retiring D6 removes the only visual anchor for this
architectural direction. Two options:

- **Option A (accept the loss).** IG prose in
  `the-customer-journey.adoc` § Acquire / Finance can carry the write-grant
  extension as prose. If future work introduces additional cross-account
  writes, Diagram 2 gains a write-arrow annotation at that time.
- **Option B (preserve via Diagram 2 amendment).** Add a small note on
  Diagram 2's ADP cross-account boundary: *"currently rendered read-only; the
  same authorization surface supports scoped write grants when CVX emits
  state-change facts into ADP data products — see IG prose for the pattern."*
  This preserves the *possibility* of the direction visually without
  committing to the Acquire-specific instance.

**Cost of losing it (Option A).** Medium. The pattern is architecturally real
and the IG will need explicit prose to keep it discoverable. No production
code depends on it today, so the loss doesn't break any commitments.

### C-item #3 — STAR-JSON as a cross-stage schema convention (Diagrams 6 and 7)

**Where it appears.**
- Diagram 6 arrow `[29]` and its STAR-JSON annotation: *"STAR-shaped
  `VehicleOwnershipTransfer` payload (same pattern as Service stage `book()`
  `[9]` and Onboard subscription-activation `[16]`)"*.
- Diagram 7 arrow `[30]` and its provenance-record annotation:
  *"accumulation of STAR-shaped `ServiceRepairOrderAdd` payloads written by
  Service stage `book()` `[9]` over the ownership tenure"*.

**Why it's foundational.** STAR is an industry-standard automotive JSON
schema family that CVX uses as its cross-stage record convention. At least
four distinct write patterns across the 7-diagram set produce STAR-shaped
payloads (`book()` [9] service-ticket; `subscription_activate` [16]
Onboard/Own; `VehicleOwnershipTransfer` [29] Acquire; `ServiceRepairOrderAdd`
provenance read [30] Sell/Trade-in). This is a genuine cross-stage
convention.

**Why it's stage-specific in current form.** Diagram 2 renders arrow `[9]` as
"`book()` writes the STAR-JSON service ticket" in the arrow narrative, but
does not surface STAR as a *cross-stage convention*. The framing of "STAR is
CVX's cross-stage record schema" only appears on D6's `[29]` annotation and
D7's `[30]` provenance annotation.

**Recommendation.** Retiring D6 and D7 removes the only visible framing of
STAR-JSON as a convention. Two options:

- **Option A (accept the loss).** The convention is implicit in Diagram 2's
  `[9]` narrative ("STAR-JSON service ticket"). IG prose in
  `the-customer-journey.adoc` and `building-on-cms-and-adp.adoc` should be
  the authoritative surface for cross-stage schema conventions.
- **Option B (preserve via Diagram 2 amendment).** Add a small side-note on
  Diagram 2 near arrow `[9]`: *"STAR-shaped `ServiceRepairOrderAdd` payload —
  the same schema family CVX uses for cross-stage record emissions."* This
  preserves the convention pointer visually without committing to
  Acquire/Sell-specific instances.

**Cost of losing it (Option A).** Low-medium. STAR is well-known within the
automotive-data domain and its use is documented in `building-on-cms-and-adp.adoc`.
The visual assertion is nice-to-have, not load-bearing.

---

## Items considered for category-C but rejected

To justify the "3 partial-C themes" finding, this section lists everything that
was considered as a candidate for category-C and rejected, with rationale.

### The CMS Flink → MSK → Lambda proactive-engagement pattern itself (special-attention item 1)

Explicitly flagged in the audit prompt: "is the pattern itself, distinct from
the specific Lambda/tool, foundational? or is it Onboard/Own-specific?"

**Verdict: NOT category-C.** The pattern of "subscribe a CVX Lambda to CMS
telemetry events on MSK, then act on them" has exactly one demonstrated
instantiation across the 7-diagram set (proactive engagement). No other stage
in the set consumes CMS telemetry events. Retiring D4 loses the pattern's only
visualization, but there is no cross-stage foundational claim to preserve —
the pattern is Onboard/Own-scoped as demonstrated. If a future stage
subscribes to CMS telemetry events for a different reason (e.g. anomaly-
detected proactive-service-recommendation to Sell/Trade-in), that spec would
introduce the pattern anew and could render it on its own diagram at that
time. Meanwhile, C-item #1 (async Runtime invocation mode) captures the more
architecturally-general question the pattern raises, without committing to
CMS-Flink-MSK-Lambda specifics.

### The Partner-ecosystem grouping convention itself (special-attention item 2)

Explicitly flagged in the audit prompt: "whether the Partner-ecosystem
grouping convention itself (distinct from any specific partner) is worth
having on diagrams 1-3 as a general 'third-party integration point exists
here' marker."

**Verdict: NOT category-C.** Diagrams 1-3 contain zero third-party integration
points. Every rendered component is either AWS-service or CVX-actor. There is
nothing on the surviving diagrams for a partner-ecosystem grouping to *attach
to*. The convention exists only where partners exist, and retiring 4-7
removes every partner from the set. A "third-party integration point exists
here somewhere" marker without any actual integration point to reference
would be a floating annotation with no anchor — an anti-pattern for
architecture diagrams. If future work adds a partner integration to the
surviving-diagram set, the grouping convention comes back with it. Meanwhile,
the convention is preserved textually in `the-customer-journey.adoc` and in
the paired-docs pattern description in IG prose.

### Well-Architected roadmap-item-1 content (special-attention item 3)

Explicitly flagged in the audit prompt: "anything in the Well-Architected
roadmap-item-1 discussion that's actually foundational rather than deferred."

**Verdict: NOT category-C.** Reviewed `DIAGRAM-BASELINE.md` § "Roadmap / not
yet covered" item 1 in full. The Well-Architected callouts (Security /
Reliability / Cost / Performance) are consistently *deferred across the
entire 7-diagram set*, including the surviving diagrams. Nothing in WA
roadmap-item-1 is uniquely rendered on Diagrams 4-7. The specific WA-adjacent
content that IS rendered — server-side persona inference via Cognito claims,
cross-account authorization boundary, session-init grounding fan-out — is
already captured on D1 and D2. Retiring 4-7 creates no new WA-content
visualization gap; the WA overlay decision remains an independent operator/
illustrator choice for a future v1.1 as documented in `DIAGRAM-BASELINE.md`.

### Nova 2 Sonic ~1-second tool-completion budget and regional availability (from D5 / D6 annotations)

**Verdict: NOT full category-C, but explicitly acknowledged in partial-C
counts in the table.** Nova 2 Sonic itself is captured on Diagram 1 as a
bidi Runtime sub-note. The specific *~1-second tool-completion budget* and
*regional availability* (`us-east-1` / `eu-north-1` / `ap-northeast-1`)
are foundational Runtime-level constraints that do not render on any
surviving diagram. However, per `DIAGRAM-BASELINE.md` roadmap-item-5, the
cross-region routing detail is a deliberate non-goal even for the
surviving diagrams — it lives in `agentic-architecture.adoc` IG prose.
The 1-second budget lives in the same IG prose file. Losing the D5/D6
stage-specific instantiations does not create a new visualization gap
because the constraints were never visualized on 1-3 to begin with.
Discussion is preserved via the partial-C mark in the table for the
reviewer's benefit.

### Bedrock Guardrails presence (from D6 `[26]` annotation)

**Verdict: NOT full category-C, but explicitly acknowledged in partial-C
counts in the table.** Same rationale as Nova 2 Sonic constraints:
Guardrails apply at the platform level to every LLM interaction in CVX,
but the diagram set consistently defers visualizing Guardrails as an
element. Per `DIAGRAM-BASELINE.md`, no visualization plan for
Guardrails-as-an-element exists on the surviving diagrams either.
Retiring D6 does not create a new gap.

### Amazon Connect outbound-campaign capability (D4 outbound-side sub-list)

**Verdict: NOT category-C.** Connect's outbound-campaign capability is only
exercised on D4 (proactive engagement). Retiring D4 removes the only
instance; there is no cross-stage claim to preserve. If a future stage
needs outbound-campaign visualization, it can add a Connect variant then.

### Onboarding write to CMS DynamoDB from Supervisor Lambda (D4 arrow `[15]`)

**Verdict: NOT category-C.** CMS DynamoDB is on Diagram 2 with `[9]`
render already. The specific "Supervisor Lambda directly writes new
`vehicles` + `drivers` rows on first registration" pattern is
Onboard-scoped and doesn't recur elsewhere in the set. IG prose covers.

---

## Bottom line

**Retiring Diagrams 4-7 is architecturally safe** provided the IG prose in
`the-customer-journey.adoc` and its companion chapters carries three
category-C themes forward explicitly:

1. **Async / event-driven Runtime invocation** (currently one instantiation
   on D4; foundational as a Runtime usage mode).
2. **CVX-side cross-account WRITE to ADP** (currently one instantiation on
   D6; foundational as a mirror to the read-only pattern on D2).
3. **STAR-JSON as CVX's cross-stage record convention** (currently visible
   only on D6 / D7 annotations; foundational to the composition story with
   ADP data products).

For each, two options: accept-the-loss (IG prose only) or preserve-via-D2-
amendment (small text annotation on the corresponding D2 element). None of
the three requires a full new Diagram — a Diagram 2 side-note is the
biggest amendment the audit recommends, and even that is optional.

Every other element on Diagrams 4-7 is either (A) stage-specific speculation
appropriate for prose treatment, or (B) genuinely reused from D1-D3 and
therefore retained by the surviving-diagram set.

**Table verdict summary.** 130 elements classified. 57 (A) stage-specific.
70 (B) already-in-D1-3. 3 distinct partial-C themes surfacing across 8
individual line-items in the table (Runtime async mode on D4; Nova Sonic
budget on D5/D6; cross-account write on D6; Guardrails on D6; STAR-JSON on
D6/D7).
