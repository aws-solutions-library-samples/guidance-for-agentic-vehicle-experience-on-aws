# re:Invent 2026 Industries Zone — Zone 2 "The Order"

**Technical design (§2.2 Requirements + §2.3 Architecture) — DRAFT for stakeholder review**

Status: DRAFT — 2026-07-31, authored by architect on the PO-owned initiative `2026-07-31-reinvent-zone2-the-order` (see `~/.kiro/portfolio/initiatives/2026-07-31-reinvent-zone2-the-order/prd.md`).

This file drafts the two sections of the scoping doc that are currently empty and overdue — §2.2 Requirements and §2.3 Architecture of "Auto+mfg demo scoping doc v2" for Zone 2 "The Order". It does **not** edit the scoping doc itself, which is stakeholder-signed at §1 (PMM Rachel Burke-Smalley 5/29, Demo Engineering Cameryn Steiger 6/5) and cannot be rewritten in place. Once these sections are reviewed and signed, they should be pasted into §2.2/§2.3 of the doc verbatim or with minor stylistic edits.

Three corrections are baked into the design below and should be reconciled into §1.3 of the scoping doc in the same review pass — see § Corrections to the scoping doc below.

**Revision 2, 2026-07-31 (same day).** The first draft asserted that Zone 2 needed no new API routes and no new compute, because the order path was already deployed. Direct verification against the codebase disproved that. The order-placement and build-progression path exists as unit-tested application code that is **deployed nowhere** — no API Gateway routes, no Lambda functions, no progression scheduler. This revision corrects §2.2.3, §2.3.1, §2.3.2, §2.3.6, §2.3.7, §2.3.10, §2.3.11, §2.3.12 and the sequencing note. The correction **raises** the build estimate materially and adds a fifth workstream. Evidence and the full nine-item delta list are in `issues/2026-07-31-acquire-order-path-never-deployed/report.md`. Nothing in §2.2's visitor-facing requirements changed; the journey we are promising is the same journey. What changed is the honest account of how much of it is already built.

---

## Corrections to the scoping doc

Before the technical content: three items from §1.3's Zone 2 row need reconciliation. Baked into §2.3 already; called out here so the review can address them explicitly.

1. **AWS Supply Chain vs. Amazon Connect Decisions — pick one.** The §1.3 Key Services column for Zone 2 lists both `AWS Supply Chain` and `Amazon Connect Decisions`. Amazon Connect Decisions is the next-generation product that supersedes AWS Supply Chain — the two are not co-equal peers to show in the same demo. This design references only **Amazon Connect Decisions (ACD)**, and only at the handoff seam (see item 3 below). §1.3 should be updated to drop `AWS Supply Chain`.
2. **ACD is available only in `us-east-1` and `eu-west-1`.** Confirmed against AWS documentation ([AWS-side confirmation](https://docs.aws.amazon.com/connect-decisions/latest/adminguide/supported-regions.html)). Our production CVX deployment target is `us-east-1`, which aligns; our current staging deployment is `us-west-2`, which does **not** align. This is a real constraint, not a footnote — see §2.3 § Region and account topology for how we handle it.
3. **Supply chain is out of scope for our surface.** The scoping doc's Zone 2 §1.3 solution areas list Supply Chain Planning / Enablement / Execution alongside Customer Experience — those three sub-areas are the ACD-owned experience and are externally owned. Our surface owns the configure → order → build-journey path. The handoff seam (a one-way `order.placed` event publish) is designed in below; the downstream ACD screen and its experience are not our deliverable and are not architected in this document.

---

## 2.2 Requirements

Zone 2 is the commerce moment in the four-zone tunnel. A visitor arrives fresh from Zone 1 having watched an engineering workspace design a configurator; they now use it. They configure a **Meridian** vehicle on a tablet, place an order, and watch it enter production. The experience runs unattended on the show floor for five consecutive days at 60–80 visitors per hour, and completes its journey even if the adjacent zones or the externally owned supply-chain screen are unavailable.

### 2.2.1 What the visitor sees and does

The environment shifts from Zone 1's engineering-lab feel to a warm, showroom-inspired treatment — softer lighting, polished floor, a display pedestal holding physical color swatches and material samples. Three visitor-facing tablets are the interactive surfaces. Each tablet mirrors to a wall-mounted display so the visitor's selections are legible to non-participants as they move through the space. This is the cinematic "wow factor" moment the scoping doc §1.3 already promises — this design realizes it without bespoke touchscreen hardware.

A journey completes in roughly two to three minutes and unfolds in five beats.

1. **Discovery.** The visitor lands on the Meridian lineup screen. Four vehicles are surfaced — **Windrose** (compact SUV, entry / urban), **Trailwind** (mid SUV, adventure), **Crestwind** (large three-row SUV, family), **Azimuth** (sedan, executive). Model cards render a still image and a one-line personality descriptor. The wall shows the currently-inspected vehicle at scale. Tapping a card commits to configuration on that vehicle.
2. **Configuration.** The visitor progresses through the full CVX configurator surface — exterior color, interior material, in-vehicle experience (IVE) package (family / executive / adventure / entry-urban), and available accessories. Every selection commits to the visitor's session immediately, mirrors to the wall, and updates a live price and delivery-window estimate. Because the four vehicles have different lineage stories, the color and interior swatches are lineup-specific rather than a shared palette. The IVE-package selection is the one the scoping doc treats as consequential downstream — it is what personalizes the visitor's Zone 4 experience and is what gets loaded onto the NFC key card the visitor picks up in Zone 3.
3. **Order placement.** The visitor confirms the configuration and places the order. A CVX-owned order id (format `ORD-YYYYMMDD-<6hex>`) is minted server-side, the reservation is persisted, and the tablet transitions to the build-journey screen. Order placement is a deliberate, one-tap commitment moment — not a form. Because this is a show-floor kiosk with no captive-finance step, we skip finance qualification and payment capture entirely; the visitor has an order the moment they confirm.
4. **Supply-chain activation animation.** For roughly 30 seconds, the tablet plays the same demand-signals-firing / logistics-routes-lighting-up animation the scoping doc §1.3 describes. This is a client-side animated transition rendered locally on the tablet — it does not depend on any live ACD state and is not gated on any external system. It is the visual bridge between "I placed an order" and "here's my order moving through production."
5. **Accelerated build journey.** The animation resolves into an order tracker. The tracker shows the ten-stage delivery pipeline CVX already models (`orderPlaced → paymentReceived → assignedToPlant → subAssembly → paint → finalAssembly → qualityControl → shipped → atDealer → readyForDelivery`), and the order progresses through all ten stages on wall-clock-accelerated time — 12 seconds per stage, roughly two minutes end-to-end. As each stage advances, the tracker surfaces a short factual sentence (grounded in a four-wheeler manufacturing knowledge base entry) explaining what happens in that stage — for example, "paint booth cure cycle at 140°C" for `paint`. This is the moment where the LLM's grounding matters: the sentence is retrieved and cited rather than hallucinated.

Once the order reaches `readyForDelivery` the tablet displays a completion state and instructs the visitor to claim their NFC key card at Zone 3 — the physical handoff seam that carries the IVE-package selection into the visitor's Zone 4 personalization. A paper ticket referencing the order id is also printed at the tablet (or shown as a QR code for scan-to-Zone-3 workflows, depending on how the fabricator resolves the ticket-print method — see § Open questions).

The tablet resets to the lineup screen after either a short idle timeout (60 seconds without input) or an explicit "Start again" tap by a booth operator. No visitor session persists across resets — this is a kiosk, not a customer account.

### 2.2.2 What the visitor can interact with

Zone 2 exposes the full CVX configurator surface, not a scoped-down variant of it. This is a deliberate change from the scoping doc's §1.3 sketch, which named three selection axes (color, interior, IVE package). We surface all of the following, subject to lock-down only if a stakeholder objects during review:

- **Lineup pick** (Windrose / Trailwind / Crestwind / Azimuth)
- **Exterior color** — six-to-eight swatches per lineup entry
- **Interior material** — three-to-five swatches per lineup entry
- **IVE package** (family / executive / adventure / entry-urban) — surfaced with a suggested default per lineup entry so the visitor can commit in one tap, or change the default deliberately (Crestwind→family, Azimuth→executive, Trailwind→adventure, Windrose→entry-urban)
- **Accessories** — three-to-six per lineup entry
- **Ask a question** — a chat affordance that opens a persona-scoped assistant grounded in Meridian's four-wheeler knowledge base ("What's the range of the Windrose?", "How long does the paint cure step take?", "Can I get Trailwind in the deep-forest green?"). The affordance is small and secondary — the primary surface is the visual configurator, not the chat.

### 2.2.3 Non-visitor-visible requirements

The following are requirements on the experience that are not visible to the visitor but constrain the build.

- **Delivery target: iPad and iPhone.** Not bespoke touchscreen hardware. The scoping doc's §1.3 phrases "three standing touchscreens"; this design realizes them as three mounted iPads. Fabrication implication (see § Open questions): the enclosure design changes from custom touchscreen units to tablet enclosures.
- **Anonymous, unauthenticated-to-the-visitor public surface.** No sign-in, no captured PII beyond what the visitor optionally leaves via a `lead_capture()` follow-up (opt-in only, feature-flagged off for the show unless stakeholders opt in). The visitor never enters credentials; the *tablet* does authenticate, as its own pre-provisioned Cognito principal in a `kiosk` group (see § 2.3.7 — "anonymous" means unattributed to a person, not unauthenticated). A kiosk-session guard runs in `build_supervisor` to clamp the persona and the tool set at the runtime boundary. This is a hardening item, not a UX one.
- **Five-day run, 60–80 visitors per hour.** ~9,600 unique sessions across the show, well within CVX's staging profile of ~15,000 text-runtime sessions per month per `docs/COST-ESTIMATE.md`. Cost cap per session is enforced server-side to prevent a stuck session (e.g., an accidentally-long agent conversation) from running up unbounded Bedrock cost.
- **Graceful degradation.** Zone 2 is functionally independent of Zone 1, Zone 3, and the ACD-owned supply-chain screen. The build-journey stages advance on a wall-clock timer local to CVX's `order_progressor` regardless of whether the ACD experience is reachable. If the CVX Knowledge Base is unavailable, the per-stage sentence falls back to a static string; the animation and progression still complete. If the tablet loses Wi-Fi mid-session, the client renders the last state received and reconnects when the network returns.
- **Kiosk hardening.** Guided Access / Single App Mode configured on every iPad so the visitor cannot exit to the home screen. **Bedrock Guardrails enforced on the text path — this is new work, not an inherited posture.** The existing `GuardrailChecker` on the voice path is a deliberate dry-run observer that logs `ApplyGuardrail` verdicts without enforcing them, the text path has no guardrail wire at all, and the guardrail identifier currently threaded through tool responses (`discover-offers-guardrails-v1`) is a placeholder with no authored policy behind it. Zone 2 requires the policy to be authored and enforcement to be wired, because an anonymous public agent is the first CVX surface where an unauthenticated stranger can address the model directly. Anonymous-session guard wired into `build_supervisor` so an unsigned session cannot invoke tools that require a Cognito claim — note that today the persona inference layer *fails open*, returning the owner-scoped `fleet_driver` persona for claimless input, so this guard is a correction and not only an addition. Per-session cost cap enforced in the AgentCore text runtime.
- **Clean reset between visitors.** No previous-visitor state visible after reset. Session data (configuration in progress, chat transcript) is cleared; order records already committed persist server-side for the build-journey timer to complete but are not surfaced to any subsequent visitor.
- **Four-wheeler knowledge grounding.** The Meridian corpus (owner-manual excerpts, four-wheeler manufacturing narratives per stage, IVE-package descriptions) is ingested into the CVX Bedrock KB and filtered at retrieve-time by `source_category`. Retrieval must return a top hit above the 0.55 relevance threshold for stock four-wheeler questions — verified by a smoke test before the show opens.
- **No real customer-brand approval gate.** Meridian is a fictional OEM. Nothing on the visitor-facing surface (or in any knowledge-base document, or in any code path) requires a real OEM to review or approve UX. Deliberate — the doc's §1.3 lists Audi and Toyota as Zone 2 customer stories, and those stay on the customer-story placards, not embedded in the interactive experience.

### 2.2.4 Interaction model summary

| Beat | Duration | Visitor input | System response | Visible on |
| --- | --- | --- | --- | --- |
| 1 — Discovery | ~15 s | Taps a lineup card | Vehicle image renders at scale | Tablet + wall |
| 2 — Configuration | 60–120 s | Sequenced taps across color / interior / IVE / accessories | Live price + delivery-window update on each commit; each selection mirrored | Tablet + wall |
| 3 — Order placement | ~5 s | Taps "Place order" | Server mints `ORD-YYYYMMDD-<6hex>`; screen transitions | Tablet |
| 4 — Supply-chain animation | ~30 s | (Passive) | Client-side animated transition | Tablet + wall |
| 5 — Accelerated build journey | ~120 s | (Passive; optional "Ask a question" chat) | Ten stages advance every ~12 s; per-stage KB-grounded sentence surfaces | Tablet + wall |
| 6 — Handoff | (transient) | Visitor takes ticket / notes QR | Tablet displays completion + NFC-card instruction | Tablet |
| — Idle reset | 60 s post-completion | (None) | Session cleared; lineup screen restored | Tablet |

---

## 2.3 Architecture

Zone 2 is a new **tenant** and a new **surface** on the existing CVX platform, not a fork of it. The scoping doc §1.3's Zone 2 lineage cleanly maps: the "customer experience" surface is the same configure/order/build-journey surface CVX already ships in staging today, applied to a fictional OEM tenant (Meridian) with a four-wheeler corpus, running on iPads instead of a browser or bespoke touchscreen. This section restates the CVX platform aspects Zone 2 reuses, and delta-describes only what is new for Zone 2.

The design leans on three sources that already exist in this repo and are not re-authored here:

- **Request/response spine** — `docs/ARCHITECTURE.md` and `docs/FLOW-STEPS.md` describe how a customer request enters the platform (API Gateway → supervisor Lambda → AgentCore Runtime → Bedrock model → grounding → response). Zone 2 rides on the identical spine.
- **Data foundation** — `docs/DIAGRAM-BASELINE-2-data-foundation.md` describes the CVX ↔ ADP composition (AgentCore Memory + cross-account Bedrock KB + cross-region Athena). Zone 2 uses the Memory + KB surfaces; the Athena/ADP path is a follow-on and not exercised in Zone 2's v1.
- **Escalation and cost postures** — `docs/DIAGRAM-BASELINE-3-escalation.md` and `WELL_ARCHITECTED.md` describe how P0/P1/P2 escalations warm-transfer and how the platform is cost-shaped. Zone 2 has no escalation surface (no PSTN, no Amazon Connect ingress), but inherits the cost model.

### 2.3.1 AWS services

Zone 2's service list is a strict subset of CVX's platform services plus one new external seam.

| Service | Role in Zone 2 |
| --- | --- |
| **Amazon Bedrock AgentCore Runtime** — text runtime (`vsa_supervisor_text_prod` / `vsa_supervisor_text_staging`) | Hosts the Strands supervisor agent that fronts the configurator's "Ask a question" chat and the per-stage build-journey sentence grounding. HTTP unary invocation from the tablet's REST client through API Gateway. |
| **Amazon Bedrock — Claude Sonnet 4.6** (via `us.anthropic.claude-sonnet-4-6` cross-region inference profile) | Supervisor synthesis. Existing wire; no Zone-2-specific change. |
| **Amazon Bedrock Knowledge Base** (ADP-owned, same account; Meridian-tenant-filtered) | Grounds the "Ask a question" chat and the per-stage build-journey sentence. `source_category` filter selects `vehicle_manufacturing`, `meridian_owner_manual`, `meridian_ive_package`, `meridian_lineup_narrative`. |
| **Amazon API Gateway** (existing `vsa-api` REST) | Ingress for tablet requests. `POST /assistant/chat` is deployed today and carries the chat path. The `/acquire/*` surface is **not** deployed and is provisioned by Zone 2 — see §2.3.7. |
| **AWS Lambda** — the acquire quartet (`payment_initiate`, `finance_qualify`, `order_progressor`, `reservation_handoff`) plus `api-assistant-chat` and `api-tenant-config` | The order-placement path (Beat 3), the ten-stage progression (Beat 5), and the chat path. **The quartet is application code that Zone 2 must deploy** — no CDK stack currently creates a function for any of them, and no scheduler invokes `order_progressor`. `api-assistant-chat` and `api-tenant-config` are deployed today. |
| **Amazon API Gateway** — six new `/acquire/*` routes | Zone 2 provisions the six routes `AcquireCatalogClient` is already written against. See §2.3.7. |
| **AWS Step Functions** (Standard workflow, one execution per order) | Per-order stage progression at demo speed. A cron cannot do it — see §2.3.11. |
| **Amazon DynamoDB** — existing tables `vsa-<stage>-acquire-catalog`, `vsa-<stage>-acquire-orders`, `vsa-<stage>-acquire-leads`, `vsa-<stage>-tenant-config` | Catalog (Meridian tenant seeded alongside the six existing tenants), reservation records, optional lead capture, tenant config. No new tables. |
| **Amazon Cognito** (existing `vsa-api` authorizer, extended with anonymous mode) | Each Zone-2 tablet authenticates as its own provisioned principal in a `kiosk` group; no visitor ever signs in. The existing Cognito authorizer is extended to accept the kiosk claim for the Zone-2 tenant, which grants only the anonymous-safe subset of routes and tools. |
| **AWS KMS** (per-stage CMK) | Encrypt AgentCore Memory records and DDB items at rest. Existing per-stage CMK; no Zone-2-specific key. |
| **Amazon CloudWatch** (Logs + Transaction Search) + **AWS X-Ray** | Observability during the show. Session id correlated across API Gateway → Lambda → AgentCore Runtime → Bedrock → KB. Same wire as staging. |
| **Amazon EventBridge** | Optional one-way `order.placed` event bus publish (see § Handoff seam below) — creates zero dependency on the downstream ACD experience and is the prerequisite for a return-path integration later without rework. |
| **Amazon Connect Decisions** (external, out of Zone 2's scope) | Referenced only at the handoff seam. Owned and operated outside this design; runs on a separate back-end display driven by ACD's own screen. See § 2.3.4 handoff seam. |

Two services from the scoping doc §1.3 Zone 2 row are intentionally omitted from this list:

- **AWS Supply Chain** — superseded by Amazon Connect Decisions; see § Corrections above.
- **Amazon SageMaker, Amazon Personalize** — Zone 2's configurator does not run a bespoke recommender in v1; the supervisor agent handles configuration reasoning via `configurator_recommend` (an existing CVX tool). If a heavier personalization loop becomes a stakeholder ask during review, both are v2 candidates; not required to hit Oct 31 show-ready.

### 2.3.2 Reuse map — what Zone 2 harvests

Zone 2 is largely built by seeding an existing platform, not by writing new services. This table names each existing CVX artifact Zone 2 reuses and what (if anything) changes about it.

| Existing CVX artifact | Zone 2 use | Change required |
| --- | --- | --- |
| `vsa-<stage>-acquire-catalog` DDB table (schema `acquire-catalog-schema.md` § 2) | Meridian lineup + colors + interiors + IVE packages + accessories | Seed a Meridian tenant row set via existing `scripts/seed-acquire-catalog.py` |
| `vsa-<stage>-acquire-orders` DDB table (schema § 3) | Zone 2 order records + ten-stage state machine | None — schema is generic |
| `vsa-<stage>-acquire-leads` DDB table | Optional lead capture (feature-flagged OFF for the show unless opted in) | None |
| `vsa-<stage>-tenant-config` DDB table | Meridian tenant config: catalog namespace, KB filter, IVE mapping, accelerated cadence | Add a `meridian` row |
| `lambdas/acquire/reservation_handoff/handler.py` | Order-placement entrypoint (Beat 3) | **Deploy it** (no function exists today) + provision `POST /acquire/reservations`. Code changes: accept an anonymous caller (`ownerSub` / `dealerRef` are currently hard-required), accept `interiorId` + `ivePackage` in `configuration`, and resolve the outstanding `TODO` at the cadence seam — `nextTransitionAt` is presently a random 0-1 hour dwell that ignores tenant `deliveryCadence` entirely |
| `lambdas/acquire/order_progressor/handler.py` | Ten-stage advancer (Beat 5) | **Deploy it** (no function exists today) + add a per-order scheduler. Reads `deliveryCadence` correctly already; the gap is that nothing invokes it |
| `lambdas/acquire/finance_qualify/handler.py` | Not used in kiosk mode | Skipped — but note the pipeline's stage 2 is `paymentReceived`, so skipping payment requires a deliberate auto-advance or a tenant-shaped stage list (see §2.3.11) |
| `lambdas/acquire/payment_initiate/handler.py` | Not used in kiosk mode (no captive-finance step) | Skipped — same caveat as above |
| `clients/ios/.../Api/AcquireCatalogClient.swift` | The tablet's REST client for catalog + order reads | None — it is already written against the six `/acquire/*` routes and documents their absence in its own header. Zone 2 makes the server match the client, not the reverse |
| `lambdas/api-assistant-chat/handler.py` | "Ask a question" chat entrypoint | Persona inference extended to anonymous-kiosk claim; server-side clamp to Meridian tenant |
| AgentCore text runtime `vsa_supervisor_text_*` + Strands supervisor + tool loop | Chat + per-stage sentence grounding | Register a `meridian` persona; no code change beyond persona wiring |
| Bedrock KB (ADP-owned, same account, existing) | Meridian four-wheeler corpus | Ingest the corpus (see § 2.3.3 KB corpus below); no infra change |
| Existing acquire tools: `catalog_query`, `catalog_compare`, `variant_compare`, `configurator_recommend`, `offers_lookup`, `delivery_status`, `manufacturing_explain` | Configurator reasoning + build-journey narration | None — tools are already tenant-parameterized |
| `scripts/drawio_to_pptx.py` + `scripts/pptx_icon_extractor.py` | Render the Zone-2 architecture diagrams to PPTX with real AWS icons for the design deck | None — used as-is |

> **Status as of 2026-08-02:** every "what changes" cell in this table has been
> built and deployed to staging (spec `2026-07-31-zone2-order-backend-and-kiosk`).
> The two seams called out as open are closed: `reservation_handoff` now computes
> the first `nextTransitionAt` from the tenant's `deliveryCadence` rather than a
> random 0-1 hour dwell, and `order_progressor` is invoked per order by a Step
> Functions state machine. The columns are left in their original
> "baseline → change" form because that is what makes the reuse argument legible;
> read the third column as shipped, not pending.

**What is genuinely new for Zone 2:**

- **The order backend.** Deployment of the acquire quartet, the six `/acquire/*` API Gateway routes with their integrations and IAM, and a per-order progression scheduler. This is the largest single item and it was mis-stated as pre-existing in revision 1
- Order-payload and catalog extensions for `interiorId` and `ivePackage` — the IVE package is the field that personalizes Zone 4 through the NFC key card, so it is load-bearing across zones, and neither the order schema nor the catalog schema carries it today
- Meridian tenant seed data (catalog + `META#tenantConfig` + KB corpus documents)
- iOS/iPadOS tablet client (SwiftUI) — the surface layer for the configurator, order tracker, and chat affordance, plus new configurator steps for interior and IVE package
- Anonymous kiosk surface: a per-device authenticated Cognito identity in a `kiosk` group with a scoped role, an `anonymous-kiosk` rule in `persona_map.py` (which fails open to `fleet_driver` today), anonymous tool-gating in `build_supervisor`, first-time guardrail policy authoring and enforcement on the text path, per-session cost cap, Guided Access provisioning on the iPads
- Optional one-way `order.placed` EventBridge publish for the handoff seam

### 2.3.3 Meridian four-wheeler knowledge-base corpus

Zone 2 requires factually-correct grounding for a Meridian SUV. The existing CVX KB is majority-two-wheeler in staging (bike shopping and bike manufacturing corpora). Zone 2 adds a Meridian-tenant-scoped four-wheeler corpus consisting of:

- **Lineup narrative** — one page per lineup entry (Windrose / Trailwind / Crestwind / Azimuth) covering positioning, target driver, powertrain range, key differentiators (e.g., third-row seating on Crestwind). Grounds the "Ask a question" chat for lineup-comparison prompts. **4 documents**, `source_category=meridian_lineup_narrative`.
- **Owner-manual excerpts** — safety, warranty, first-service, connected-features overview. One page per topic across the lineup, not per topic per entry. Grounds ownership-adjacent chat (Zone 2 doesn't dwell here — this is thin coverage, not full-manual-depth). **4 documents**, `source_category=meridian_owner_manual`.
- **Manufacturing narrative** — one page per ten-stage pipeline stage plus two cross-cutting pages (typical cadence, visibility signals), four-wheeler specific (paint booths, robotic sub-assembly, in-line quality gates). The structure was modeled after the pre-Meridian two-wheeler manufacturing corpus but the content was written fresh for automotive scale. Grounds the per-stage sentence in the build-journey tracker. **12 documents**, `source_category=vehicle_manufacturing`.
  - The category is deliberately **`vehicle_manufacturing`, not `meridian_manufacturing`** (revision 1 of this document said the latter). Nothing in these pages is Meridian-specific — they describe four-wheeler manufacturing generically — so a brand-scoped name would have forced a duplicate corpus for the second four-wheeler tenant. The two brand-specific categories keep the `meridian_` prefix because their content genuinely is brand-specific.
- **IVE package descriptions** — family / executive / adventure / entry-urban, one page each, describing what each package personalizes downstream. Grounds Zone-4-question chat prompts. **4 documents**, `source_category=meridian_ive_package`. The NFC key-card handover mechanism is documented canonically in the family page and cross-referenced from the other three, rather than repeated in each — four near-identical mentions left no document as the authoritative answer and made retrieval for handover questions arbitrary.

**24 documents total**, each with a `.md.metadata.json` sidecar (48 files). The sidecar carries `source_category` plus, where relevant, a `stage` (`vehicle_manufacturing`) or `ive_package` (`meridian_ive_package`) attribute. It does **not** carry a `tenant` attribute — tenant scoping is achieved by mapping a tenant to its categories at the runtime via `VSA_ACQUIRE_KB_SOURCE_CATEGORY` and `VSA_DISCOVER_KB_SOURCE_CATEGORY`, consumed through `agents/supervisor/tools/kb_filter.py`. Ingestion is an operator action against the existing **ADP-owned** KB (`YULHRAUMNU`, `us-east-1`, same AWS account) — no infra change. Procedure, including the deliberate hyphenated-S3-prefix / underscored-`source_category` convention, is in `docs/DEPLOYMENT.md` § Knowledge base.

Success criterion for the corpus: an audit set of Meridian-representative prompts (lineup, powertrain, IVE-package, per-stage manufacturing) returns a top hit above the 0.55 relevance threshold via `scripts/smoke-test-kb-retrieve.py --audit-set`. **Result as built: 31 prompts, 31/31 pass with the per-tenant `source_category` filter applied.** Unfiltered, 30/31 pass — the exception being a manufacturing prompt that returns the two-wheeler page ahead of the four-wheeler one, because the two corpora share all ten stage names and filenames. That is the condition the filter exists to resolve, and it is the reason the per-tenant category configuration is a deployment requirement rather than an optimisation. Baseline and near-miss margins: `evals/kb_audit/baselines/2026-08-03-meridian-audit.md`.

### 2.3.4 Handoff seam to supply chain (Amazon Connect Decisions)

The ACD-owned supply-chain screen is out of Zone 2's scope. Zone 2 provides one loosely-coupled handoff option, and one only, in v1.

**Option B — one-way `order.placed` event publish (RECOMMENDED for v1)**

On Beat 3 (order placement), the `reservation_handoff` Lambda emits an `order.placed` event to an EventBridge event bus. Event schema (minimal):

```
{
  "detailType": "order.placed",
  "source": "cvx.meridian.reinvent2026",
  "detail": {
    "orderId": "ORD-20261201-a3b7c9",
    "tenantId": "meridian",
    "configuration": {
      "modelId": "trailwind",
      "colorId": "deep-forest-green",
      "interiorId": "adventure-brown",
      "ivePackage": "adventure",
      "accessoryIds": ["roof-basket", "hitch-3500"]
    },
    "placedAt": "2026-12-01T14:22:07Z"
  }
}
```

The event is fire-and-forget — the ACD-owned side subscribes if and only if it wants to; no ACK is expected; Zone 2 completes its journey whether or not any subscriber exists. This creates zero dependency on ACD availability and is a strict prerequisite for any richer return-path integration later without rework (see § Open questions for the deferred richer options).

Two things this seam intentionally does **not** do:

- **No shared order store.** Considered and deferred in the PRD (option D). Architecturally attractive, but a show floor punishes coupling and cross-account failure modes; not right for this venue. Documented as a natural future home in the CVX IG reference-pattern section, not built for the show.
- **No live ACD invocation from Zone 2.** ACD is region-restricted to us-east-1 and eu-west-1 (see § Region and account topology). Zone 2 does not itself call ACD; the event bus publish is the entire seam.

**Fallback option A — no external publish.** If the operator decides even the EventBridge publish is too much coupling for the show (e.g., no downstream subscriber is committed and the noise-to-signal ratio is not worth it), the publish can be feature-flagged off via `VSA_ACQUIRE_PUBLISH_ORDER_PLACED=false` on the `reservation_handoff` Lambda. Zone 2's journey is unaffected.

### 2.3.5 Region and account topology

CVX today runs staging in us-west-2 and prod in us-east-1 (per `docs/DEPLOYMENT.md`). Zone 2 targets **prod (us-east-1)** for the show, for three independent reasons:

1. **ACD alignment.** ACD is available only in us-east-1 and eu-west-1. Prod's us-east-1 aligns with the ACD region constraint from correction #2 above; staging's us-west-2 does not. If the ACD-side team ever wires a subscriber to Zone 2's `order.placed` event bus, us-east-1 keeps the cross-service integration in-region.
2. **Nova Sonic co-location.** Not exercised by Zone 2's text-only surface, but consistent with prod's overall service placement — no cross-region hop for Bedrock invocation.
3. **Kiosk-hardening posture already applied.** The prod stack has the guardrail on the text path today (staging does not per an explicit staging-skip decision in `.kiro/specs/2026-05-28-agentcore-staging-infra/decisions.md`). Zone 2's kiosk-hardening requirements extend from the guardrail posture prod already ships.

Staging (us-west-2) remains the dev+UAT environment for the Zone-2 tablet client and the Meridian corpus. The show tablet runs against prod (us-east-1) endpoints.

Cross-region caveat: none for Zone 2. This is a text-only, single-region deployment. All the cross-region complexity in `docs/ARCHITECTURE.md` (Nova Sonic cross-region, Athena cross-region, cross-account KB) applies to CVX generally, not to Zone 2's v1 surface.

### 2.3.6 Data model

Zone 2 requires no new DDB tables. Two existing tables carry the Meridian tenant.

**`vsa-prod-acquire-catalog`** — Meridian catalog rows. Schema (existing, per `acquire-catalog-schema.md` § 2) is a `{tenantId, itemKey}` single-table design. Item-key prefixes:

```
tenantId = "meridian"
itemKey  =
  CAT#<category>              (e.g. CAT#compact-suv, CAT#mid-suv, CAT#large-suv, CAT#sedan)
  MODEL#<modelId>             (e.g. MODEL#windrose, MODEL#trailwind, MODEL#crestwind, MODEL#azimuth)
  VAR#<modelId>#<variantId>   (variant refinements per model — trim levels)
  COLOR#<modelId>#<colorId>
  INTERIOR#<modelId>#<intId>
  ACC#<accessoryId>
  ACC-COMPAT#<accessoryId>#<modelId>
  IVE#<packageId>             (family, executive, adventure, entry-urban)
  OFFER#<offerId>             (empty in v1 — no promotional overlays for the show)
```

Approximate row count: ~4 categories + 4 models + ~10 variants + ~28 colors (7 avg × 4 models) + ~16 interiors (4 avg × 4 models) + ~16 accessories + ~16 compatibility entries + 4 IVE packages = **~98 rows**. Well within DDB pay-per-request; well within GSI query bounds.

**`vsa-prod-acquire-orders`** — Zone 2 orders. Schema unchanged. Order records include:

```
orderId          "ORD-YYYYMMDD-<6hex>"        (PK)
tenantId         "meridian"
tenantOwnerKey   "meridian#<device-sub>"      (GSI: by-tenant-and-owner)
orderPlacedAt    ISO-8601
stage            one of the 10 stages
nextTransitionAt ISO-8601 (accelerated) — omitted on terminal
stageHistory     [ {stage, enteredAt}, ... ]
configuration    { categoryId, modelId, variantId, colorId, interiorId, ivePackage, accessoryIds }
                 — `interiorId` and `ivePackage` are NEW; the handler accepts
                   categoryId/modelId/variantId/colorId/accessoryIds today and
                   400s on missing categoryId or variantId
correlation      { starEnvelope: { appointment_type: "handover", ... } }
discoverLeadId   NULL (no Discover phase in Zone 2)
```

Anonymous kiosk sessions carry the **tablet's own** Cognito subject as `ownerSub` — one distinct principal per device — so the by-tenant-and-owner GSI groups show-floor orders by the station that took them. Auditable ("here are all Zone-2 orders, and which tablet each came from"), and no visitor PII is stored.

A single shared sentinel owner was the original design and was rejected on security review. The order id is derived as `ORD-<date>-<sha256(tenantId|ownerSub|second)[:6]>`, so fixing both `tenantId` and `ownerSub` to constants leaves a one-second timestamp as the only varying input and makes every order id for the entire show computable offline. Per-device identity restores entropy an outsider does not hold. Order reads are additionally scoped to `ownerSub == claim.sub`.

**`vsa-prod-tenant-config`** — Meridian tenant row. New row:

```
tenantId              "meridian"
displayName           "Meridian"
vehicleCategoryLabel  "vehicle"                (four-wheeler default)
kbFilter              { source_category: ["meridian_lineup_narrative", ...] }
guardrailProfile      "vsa-vehicle-scope"       (policy to be authored — see § 2.2.3)
anonymousKiosk        true                       (Zone-2-specific flag)
handoffPublish        { enabled: true, eventBus: "cvx-reinvent2026-handoff" }
```

**`vsa-prod-acquire-catalog`, item `META#tenantConfig`** — the progression cadence. This lives on the **catalog** table, not on `vsa-prod-tenant-config`: `order_progressor` reads `(tenantId="meridian", itemKey="META#tenantConfig")` and takes `deliveryCadence` from that item. Bands are `{minHours, maxHours}` floats with a 24-hour default when absent.

```
tenantId          "meridian"
itemKey           "META#tenantConfig"
deliveryCadence   { orderPlacedToPayment: {minHours, maxHours}, ... }   (9 transition keys)
```

Two config surfaces rather than one is an existing platform wart, not a Zone 2 decision. Worth consolidating later; not worth a schema migration before a fixed conference date.

The `vehicleCategoryLabel` field is already tenant-driven through the `acquire` config block per commit `b5aff4f`, per the PRD. Meridian is effectively a seventh tenant alongside the six existing.

### 2.3.7 API routes

Zone 2 provisions six `/acquire/*` routes on the existing `vsa-api` REST API. **Revision 1 of this document stated that no new routes were needed. That was wrong** — the deployed API surface today is `/triage`, `/vehicles/{id}/{service-history,context,trips,live-state,safety-events}`, `/drivers/me`, `/tenants/{id}/config`, `/escalate`, `/find-service-center`, `/book`, and `/assistant/chat`. There is no `/acquire/*` and no `/discover/*`.

The route names below are **not invented for Zone 2** — they are the six routes `AcquireCatalogClient.swift` is already written against, and whose absence that file documents in its own header comment. Zone 2 makes the server match the client that exists rather than defining a third contract.

| Route | Purpose in Zone 2 | Auth |
| --- | --- | --- |
| `GET  /acquire/catalog/{tenantId}` | Beats 1-2 — the full Meridian catalog in one call: lineup, colors, interiors, IVE packages, accessories, compatibility | Anonymous kiosk claim |
| `POST /acquire/reservations` | Beat 3 — mints the order id, persists to `vsa-acquire-orders`, seeds the progression schedule, optionally publishes `order.placed` | Anonymous kiosk claim |
| `GET  /acquire/orders/{orderId}` | Beat 5 — polled by the tracker to render current stage, `stageHistory`, and `nextTransitionAt` | Anonymous kiosk claim |
| `GET  /acquire/offers/{modelId}` | Beat 2 — offer overlays if any are seeded (empty in v1) | Anonymous kiosk claim |
| `POST /acquire/leads` | Optional opt-in lead capture; feature-flagged OFF for the show unless stakeholders opt in | Anonymous kiosk claim |
| `GET  /acquire/leads/{leadId}` | Lead read-back; unused on the show floor, provisioned for contract completeness | Anonymous kiosk claim |
| `POST /assistant/chat` | Beats 2 & 5 — the "Ask a question" affordance; server-side clamped to the Meridian persona | Anonymous kiosk claim |

`POST /assistant/chat` is the only one of the seven deployed today.

The anonymous-kiosk claim is issued by the tablet at boot from a pre-provisioned Cognito identity, scoped to only the routes and tools that operate on the anonymous surface — no `escalate`, no `book`, no owner-scoped tools. Note what this claim is **not**: it is not an unauthenticated identity. The existing per-tenant identity pools are created with `allowUnauthenticatedIdentities: false` by deliberate design, on the rationale that every voice session must be authenticated, and Zone 2 does not relax that. Instead each tablet authenticates as its own provisioned Cognito principal in a `kiosk` group. "Anonymous" means unattributed to a *person*, not unauthenticated — and a named principal is revocable mid-show, which an unauthenticated pool is not.

### 2.3.8 Architecture diagrams

The design references the existing three-diagram baseline in `docs/DIAGRAM-BASELINE-1-orchestration.md`, `-2-data-foundation.md`, `-3-escalation.md`, and applies **Zone-2 overlays** rather than authoring net-new diagrams. Zone 2 is a subset of the platform — showing all three canonical diagrams with Zone-2 emphasis is the correct treatment.

**Diagram Z2-1 — Zone 2 request/response spine (overlay on Diagram 1)**

Same components as Diagram 1 (Customer, API Gateway + Cognito, Supervisor Lambda, AgentCore Runtime **text** variant only, Triage Classifier), with the following Zone-2 changes:

- **Amazon Connect box omitted.** Zone 2 has no PSTN / chat-Connect ingress. Ingress is API Gateway only. Draw the `[1] [2]` Connect-ingress arrows as omitted-with-annotation, or drop the Connect box entirely.
- **Runtime bidi variant omitted.** Zone 2 is text-only. Draw the AgentCore Runtime box with only the `text (chat / web / mobile)` sub-note.
- **Triage output does not fan out to escalation.** Zone 2 has no escalation surface; the `[5] triage()` arrow terminates at the Runtime and does not proceed to `→ Diagram 3`. Annotate: `Zone 2: no escalation surface`.
- **New Zone-2 client actor.** Replace or supplement box A (Customer) with `Zone-2 Tablet (iPad, Guided Access)`. The tablet is anonymous — no signed-in identity.

**Diagram Z2-2 — Zone 2 grounding surface (overlay on Diagram 2)**

Same components as Diagram 2, with the following Zone-2 simplifications:

- **ADP cross-account dashed boundary omitted.** Zone 2 v1 does not exercise the ADP grounding path. Draw the `[4b]` cross-region Athena arrow and the `[7]` cross-account KB arrow as either omitted or annotated `Zone 2 v1: not exercised`.
- **CMS foundation labeled grouping omitted.** Zone 2 has no CMS-side reads (no LKS, no Recalls, no CMS DDB write). Draw the CMS boundary as omitted-with-annotation or drop entirely.
- **KB (box 11) surfaces the Meridian corpus only.** Annotate: `filter: tenant=meridian AND source_category in {lineup_narrative, owner_manual, manufacturing, ive_package}`.
- **AgentCore Memory box remains** — the session-init grounding step still writes selected catalog rows to Memory for tool-loop reuse, even in the simplified Zone-2 flow.

**Diagram Z2-3 — Zone 2 handoff seam (net-new small diagram)**

A small diagram, not a full "escalation" analogue. Boxes:

- AgentCore Runtime (edge)
- `reservation_handoff` Lambda
- `vsa-acquire-orders` DDB (target of the reservation write)
- EventBridge event bus (labelled `cvx-reinvent2026-handoff`)
- ACD (rendered as a light-fill, out-of-scope grouping — dashed border, subtitle "externally owned; not built by this design")

Arrows:

- Order placement solid arrow from Runtime → `reservation_handoff` Lambda → DDB
- Dashed one-way publish arrow from `reservation_handoff` → EventBridge
- Dashed one-way subscribe arrow from EventBridge → ACD (annotated "if ACD subscribes; Zone 2 does not require it")

The intent of Z2-3 is to visually reinforce that our surface completes independently of ACD.

**Rendering pipeline.** The existing `scripts/drawio_to_pptx.py` + `scripts/pptx_icon_extractor.py` pair renders drawio sources to PPTX with real embedded AWS icons. Zone 2 authors the three drawio overlays (Z2-1, Z2-2, Z2-3) under `documentation/diagrams/reinvent-2026/`, then runs the existing script to produce a three-slide PPTX for the design deck. No new tooling required.

Draft drawio sources are not authored in this design pass — they are a follow-on task after these sections are signed off, per § Sequencing below.

### 2.3.9 Cost posture

Zone 2 fits inside the existing CVX cost envelope described in `docs/COST-ESTIMATE.md`. Estimated additional Zone-2 monthly cost during the show week (5 days at 60–80 visitors per hour × ~8 open hours = ~2,400–3,200 daily = ~12,000–16,000 unique sessions across the show):

- Session count is well below the staging profile of ~15,000 text-runtime sessions per month, so the cost model applies with no scaling factor
- Additional Bedrock token spend: 12,000–16,000 sessions × ~2 chat turns average × Sonnet 4.6 token cost per turn ≈ **~$60–$120 for the week**
- Additional AgentCore Runtime spend: **negligible** (scales to zero between requests; per-invocation billing)
- Additional API Gateway + Lambda: **negligible** (~$0.20 for 50k requests per COST-ESTIMATE)
- Additional KB Retrieve: **negligible** (well under 0.5 refs/turn on average)
- Additional EventBridge (if handoff publish enabled): **negligible** at 12–16k events/week
- **All-in Zone-2 incremental cost for the show week: order of $100.**

No cost lever needs pulling for the show scale. The per-session cost cap (kiosk hardening item) is a safety valve, not a cost driver.

### 2.3.10 Well-Architected alignment

Zone 2 inherits the three pillars articulated in `WELL_ARCHITECTED.md`:

- **Security** — Cognito authorizer + server-side persona clamp (extended to the Meridian tenant + a new anonymous-kiosk claim, correcting a persona-inference default that currently fails open to an owner-scoped persona); Bedrock Guardrails authored and enforced on the text path for the first time (today's wire is a voice-path dry-run observer with a placeholder policy identifier); server-side tenant clamp so a client cannot request a non-Meridian persona or escalate its own claim
- **Cost optimization** — serverless scale-to-zero at every request-path tier; usage-based Bedrock billing; per-session cost cap on the runtime; free-tier eval regression detection (Tier 1/2)
- **Operational excellence** — the same tiered eval pipeline + smoke test suite; the show-day CloudWatch Transaction Search + X-Ray correlation trace; CDK-managed infrastructure throughout (the Meridian *tenant* is seed data, but the order backend it rides on is a CDK change — see §2.3.12)

Reliability and Performance Efficiency inherit from AWS service defaults per `WELL_ARCHITECTED.md`. Zone 2's graceful-degradation requirements (§ 2.2.3) are the CVX pattern: tools return structured "not yet available" messages if grounding is unavailable, and client-side rendering falls back to a static string.

### 2.3.11 Accelerated build-journey progression

The `order_progressor` Lambda advances orders by querying the `by-progression-status` GSI for orders whose `nextTransitionAt` has passed, then advancing `stage` one step along the fixed ten-element `STAGES` sequence and recomputing `nextTransitionAt` from the tenant's `deliveryCadence` bands (`{minHours, maxHours}` floats, 24-hour default). That logic was correct and needed no change. Two things around it did, and both are now built:

**The Lambda is deployed and driven per order.** Revision 1 described an existing EventBridge five-minute cron; there was none. Zone 2 deploys the function, and invokes it once per stage transition from a Step Functions state machine rather than a cron.

**A cron cannot render a two-minute journey.** EventBridge cron granularity is one minute, so ten stages take at least ten minutes even with 12-second cadence bands configured. Revision 1 recommended a "config-only" approach on that basis; it does not work. Per-order scheduling was therefore built for v1, not deferred to v1.1. Two mechanisms were considered:

- **Step Functions state machine per order** *(built)*. `reservation_handoff` starts one execution at order placement; the machine alternates `Wait` (12 s) and a `Lambda:Invoke` of `order_progressor` scoped to that single `orderId`, nine times — nine transitions, not ten, because `orderPlaced` is the entry state rather than a transition. Precise cadence, per-order observability during the show, and a natural place to hang the completion event. Adds one service to the stack.
- **EventBridge Scheduler one-shot schedules** *(rejected)*. `reservation_handoff` creates ten one-time schedules at `orderPlacedAt + N × 12 s`. No new state machine, but ten schedule objects per order (≈96,000 across the show week) and a cleanup obligation.

**Step Functions, Standard workflow type, is what shipped.** Standard rather than Express, deliberately: Express caps execution duration at five minutes and uses an at-least-once model, while Standard runs to a year with exactly-once semantics and a 90-day queryable execution history. At this volume Express's throughput advantage is worthless — 60-80 visitors per hour is roughly 0.02 executions per second against Express's 100,000-per-second design point — and Standard's cost at ~21 state transitions per order across ~16,000 orders is single-digit dollars for the week. What Standard buys that matters on a show floor is the ability to open the console mid-show, find one visitor's stuck execution, and see exactly which stage it halted on. Express would require reconstructing that from CloudWatch Logs.

Either way the progression step is safe to retry: `order_progressor`'s `UpdateItem` carries a `ConditionExpression` on the observed stage and `nextTransitionAt`, so a duplicate advance is a no-op.

Two implementation details are worth recording, because both were mis-built first and only surfaced on a live staging run (see `issues/2026-08-02-order-progression-sfn-payload-and-resultpath/`). Each invoke passes the execution input straight through — no payload override — and discards its own result, so `{"orderId": ...}` survives all nine invokes. Overriding the payload with a bare `$` JSON-path renders as a literal string and crashes the first invoke; letting the Lambda's return value replace the state loses `orderId` and makes the order advance exactly one stage and then stall silently. Both shapes are now asserted against the synthesized state machine by `scripts/tests/test_order_progression_asl.py`.

Two smaller items in the same area, both closed:

- **`reservation_handoff` ignored cadence entirely.** The first transition was `_sample_next_transition(0.0, 1.0, now)` — a random 0-to-1-hour dwell — behind a `TODO` noting that the real implementation should read `deliveryCadence`. That seam is closed: the first `nextTransitionAt` is now computed from the tenant's cadence, so Beat 5's first stage advance lands while the visitor is still at the booth.
- **Kiosk mode skips payment, but `paymentReceived` is stage 2 of 10.** Resolved the cheap way, as recommended: the Meridian tenant auto-advances through it with a near-zero dwell (`orderPlacedToPayment` = 0.00001 h), which keeps `STAGES` a shared constant rather than making the stage list tenant-shaped.

Beat 5 of § 2.2.1 assumes precise per-order scheduling. The visitor's attention is on the wall and the tablet, so a stage advancing every ~12 seconds is the target; a stage advancing every ~60 seconds would not fit inside the two-to-three-minute dwell time the zone is designed around. Measured end-to-end on staging: **all ten stages in order in 125.7 s**, against a 180 s budget.

### 2.3.12 Deployment posture

Zone 2 deploys through the existing CVX prod deploy path. Steps 1-2 are the order-backend build that revision 1 of this document omitted:

1. **Order backend provisioned** — acquire quartet deployed as Lambda functions with IAM and env wiring; the six `/acquire/*` routes added to `vsa-api-stack.ts` with the anonymous-kiosk authorizer; per-order progression scheduler stood up. This is a CDK change, not a config change
2. **Anonymous-kiosk surface provisioned** — a **provisioned, authenticated** per-device Cognito principal in a `kiosk` group plus a scoped role (*not* an unauthenticated identity pool — see § 2.3.7), `persona_map` rule, `build_supervisor` tool-gating, guardrail policy authored and enforced on the text path, per-session cost cap. The guardrail id must be threaded onto the AgentCore text runtime as `VSA_KIOSK_GUARDRAIL_ID` together with `AGENTCORE_STAGE`; the deploy targets fail fast if either is unresolvable, because a runtime missing them enforces nothing and still returns 200
3. Meridian corpus documents authored, tenant seed data authored — commit to repo
4. `python3 scripts/seed-acquire-catalog.py --catalog scripts/seed_data/acquire_catalog/meridian-catalog.json --table vsa-<stage>-acquire-catalog --region <region>` — seeds catalog rows including the `META#tenantConfig` cadence item. Note there is **no `--tenant` flag**: `tenantId` is baked into the catalog JSON envelope and onto every item
5. Kiosk device users are provisioned per tablet (see `docs/DEPLOYMENT.md` § Kiosk device setup), not by `seed-personas.py`. No new persona is registered — the kiosk clamps to the existing `consumer` persona server-side
6. KB corpus ingestion — one-time operator action against the existing prod KB
7. Existing CVX prod deploy (`make deploy-prod`)
8. iPad app deployment — TestFlight or Enterprise MDM; the tablet client is a SwiftUI app talking to `/acquire/*` and `/assistant/chat`
9. Post-deploy smoke tests — the existing `smoke-test-kb-retrieve.py` extended with a Meridian audit set; a new `smoke-test-zone2-journey.py` that walks Beats 1-5 end-to-end against the prod API as an anonymous kiosk client

The show-day operational posture (Guided Access on the iPads, per-session cost cap enforced at the runtime, guardrail active) is verified against a checklist at show-open on Day 1 of re:Invent.

---

## Sequencing note

Revision 1 described four parallel follow-on workstreams. With the order backend correctly accounted for there are five, and they are **not** all parallel — three of them depend on the backend contract existing.

1. **Zone 2 order backend + anonymous kiosk** (new; the critical path) — deploy the acquire quartet, provision the six `/acquire/*` routes, stand up per-order progression, and build the anonymous-kiosk surface. The order backend and the kiosk surface are one spec rather than two because they share the claim contract: the authorizer, the `persona_map` rule, and the tool-gating in `build_supervisor` are all inputs to the same routes
2. `Zone 2 KB corpus` — author the Meridian four-wheeler corpus, ingest, verify against the audit set. **Genuinely parallel; depends on nothing above.** Start immediately
3. `Zone 2 build journey` — cadence configuration and the per-stage grounded sentence. Needs (1) deployed
4. `Zone 2 iPad target` — the tablet client. Needs (1)'s contract; also needs an XCTest target priced, since none exists and iOS acceptance criteria are otherwise unrunnable
5. `Atlanta OEM` (Meridian seed) — additive tenant seed underneath (2) and (3), not a critical-path rework

`Zone 2 vehicle 3D` (Wildlife-owned) and `Zone 2 SC handoff` (external subscriber, if any) remain long-lead externals that do not block our surface.

**Schedule consequence worth stating plainly to stakeholders:** Oct 31 show-ready is thirteen weeks out, and item (1) is a real API and compute build rather than the config change revision 1 implied. It is still achievable, but it is now the item that determines whether the date holds, and it should be resourced first rather than treated as already done.

---

## Open questions

Questions that surfaced during design and need product / stakeholder input to close. None block draft review.

1. **Screen count.** The scoping doc §1.3 sets three touchscreens for Zone 2. With ACD's supply-chain screen moved to a back-end display driven externally, the visitor-facing tablet count reduces — likely two, possibly three if a "public inspect" mode is desired. Impacts fabrication enclosure design.
2. **Ticket-print vs. QR-code handoff.** Beat 6 assumes either a printed paper ticket or a scan-to-Zone-3 QR code carries the visitor's order id to Zone 3's NFC-card dispenser. Which mechanism does Wildlife's fabrication support? A printer is a physical dependency; a QR code is not. Recommend QR unless printing is already scoped.
3. **Meridian trademark adjacencies.** No clearance search performed. Meridian Audio (Land Rover / Jaguar / Kia audio supplier) and Trailwind (one letter from Subaru's Tailwind mark) are foreseeable review questions. Not blocking; recommend a clearance pass before public asset commit.
4. **3D asset for the wall.** The Midjourney generations the user supplied (still + 5.2s turntable video) cover app-layer imagery. They do not cover the wall's interactive rotate/recolor/interior-swap unless Wildlife renders a real 3D asset. If the wall is a still-plus-turntable-video pair, the "interactive wall" language in §1.3 needs softening; if the wall is a real interactive 3D asset, Wildlife owns the deliverable and its timeline.
5. **Which of the configurator axes to lock down.** § 2.2.2 exposes the full CVX configurator surface. If any stakeholder wants to constrain the axes (e.g., "no accessories in Zone 2 — save that for Zone 4"), name them now; the surface is trivially reducible via tenant config.
6. **The scoping doc §1.3 Zone 2 solution areas.** Supply Chain Planning / Enablement / Execution are still listed. If we drop them because supply chain is out-of-scope for our surface (correction #3), does §1.3 need Supply Chain removed entirely, or does it stay to preserve the tunnel's overall solution-area coverage as owned by an external ACD-side team? PMM call.
7. **AI-disclosure position on the Midjourney assets.** The Midjourney PNG's IPTC metadata declares `trainedAlgorithmicMedia` and carries the prompt text + a Midjourney account id. Before any public commit, PMM / legal view is needed plus a metadata-stripping pass and a likely `scan_exclude` entry (ADP precedent: C2PA-signed JSONs tripped base64 false positives).

---

## Architectural gap the PRD did not anticipate

Surfacing one item that emerged during the design pass and is not captured in the PRD's Must / Should / Won't buckets, for the review to disposition.

**Anonymous kiosk mode is a first-class Cognito + persona-map surface, not just a flag.** The PRD lists kiosk hardening as a Must-Have — anonymous-session guard, per-session cost cap, guardrail extension — but treats them as cross-cutting hardening items. In implementation, the "anonymous kiosk" is actually a new **Cognito identity + persona-clamp path** that needs to be modeled explicitly:

- The tablet needs a pre-provisioned Cognito identity — as built, an **authenticated** per-device user in a `kiosk` group plus a scoped role, not an unauthenticated identity pool — that authorizes only the anonymous-safe routes and tools
- `lambdas/api-assistant-chat/persona_map.py` needs a mapping rule for `anonymous-kiosk` that clamps to the Meridian persona and rejects any client-supplied persona override
- `agents/supervisor/build_supervisor` (referenced in the PRD's Must-Have list) needs a code path that, when it detects an anonymous claim, disables tools that require a signed-in owner (`book`, `escalate`, owner-scoped tools) rather than letting them fail at tool-invocation time
- The `vsa-api` Cognito authorizer needs to accept and validate the anonymous-kiosk claim shape

This is a coherent slice, not a scatter of guardrails. Treating it as its own architectural surface (rather than three loose hardening items) makes the design testable end-to-end via a single "anonymous kiosk session" smoke test rather than three independent guards. It also makes the same surface reusable — any future kiosk-style deployment (partner events, dealer show floors) inherits it. Recommend elevating this as its own § in the PRD's Must-Have list, with the three hardening items as sub-bullets underneath. Fits within the existing `Zone 2 kiosk hardening` backlog row without a scope change.

---

## What this draft is NOT

- Not a spec. This is a design doc for stakeholder review, following the scoping doc's §2.2 / §2.3 shape. A `spec.md` under `.kiro/specs/2026-XX-XX-zone2-<slug>/` is the follow-on artifact once the design is signed and Phase 2 build begins.
- Not production code. No code is authored by this pass; the reuse map (§ 2.3.2) is the entire "what changes" list.
- Not a supply-chain design. That is externally owned.
- Not a rewrite of `docs/ARCHITECTURE.md`, `docs/FLOW-STEPS.md`, `WELL_ARCHITECTED.md`, or the three diagram baselines. Zone 2 is a subset of the platform they describe; this doc references them and delta-describes only Zone-2 specifics.
