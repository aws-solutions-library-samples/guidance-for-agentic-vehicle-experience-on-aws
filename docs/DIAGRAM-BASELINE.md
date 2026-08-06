# CVX Reference Architecture Diagrams — Baseline Index

> **Diagrams 4-7 retired 2026-07-16.** The four sequential customer-journey stage
> diagrams (Onboard / Own, Discover / Find, Acquire / Finance, Sell / Trade-in)
> added on 2026-07-15 as an extension to this baseline were retired one day
> later. Rendered technical diagrams for speculative/roadmap journey-stage
> content have high maintenance cost relative to their value: the stage-specific
> architecture in those diagrams is the same Ingress → Runtime → Session-init →
> Per-turn tools → Response chain that Diagrams 1-3 already carry, differing
> only in which tool names appear on the AgentCore Runtime edge and which
> partner-category boxes it points at — content that reads more clearly as IG
> prose than as four near-duplicate diagrams. The stage-specific concepts (per-
> stage tool catalogs, partner-integration points, entry-path variants,
> maturity-callout posture) live in
> `~/AWSGuidanceforConnectedVehicleExperienceDocs/latest/implementationguide/the-customer-journey.adoc`
> and are the authoritative source for that content going forward. A
> completeness audit at `docs/DIAGRAM-4-7-RETIREMENT-AUDIT.md` confirmed no
> foundational architecture was lost in the retirement, with two small
> insurance annotations added to Diagram 2's baseline (`DIAGRAM-BASELINE-2-
> data-foundation.md`) to cover the two cross-diagram concepts flagged by the
> audit as worth preserving explicitly: (a) the ADP cross-account authorization
> boundary supports scoped write grants for CVX state-change emissions (e.g.
> `VehicleOwnershipTransfer`) in addition to the read-only reference-data flow
> rendered today, and (b) the STAR-JSON `ServiceRepairOrderAdd` payload written
> by `book()` is one instance of a broader STAR-JSON schema family CVX uses for
> cross-stage record emissions generally. This index now describes only
> Diagrams 1-3, matching the state that existed before the 2026-07-15
> extension.

---

This is the **index** for the CVX Reference Architecture Diagram baseline. The prior
single-diagram spec at this path was split into **three separate diagram-input specs**
on 2026-07-14 along natural content seams. This file is the shared entry point,
shared legend / conventions, split rationale, and roadmap for content not yet covered.

**Original file provenance.** The prior single-file baseline (479 lines, 14 boxes, 21
numbered arrows across 5 phase groups) was derived from the completed 17-chapter CVX
guidance-doc Implementation Guide at
`~/AWSGuidanceforConnectedVehicleExperienceDocs/latest/implementationguide/`,
specifically `architecture-overview.adoc`, `contact-center-integration.adoc`, and
`building-on-cms-and-adp.adoc`. That content was redistributed into Diagrams
1-3 — nothing was lost, and the numbered badges `[1] … [14]` are stable across those
three files (badge `[7]` means the same thing on every diagram).

---

## The three diagrams

| # | File                                             | Story                                                                                                    | Steps                                         |
| - | ------------------------------------------------ | -------------------------------------------------------------------------------------------------------- | --------------------------------------------- |
| 1 | [`DIAGRAM-BASELINE-1-orchestration.md`](./DIAGRAM-BASELINE-1-orchestration.md)   | **Agentic Orchestration** — how a request gets handled: ingress (voice / chat / web) + per-turn tool dispatch + triage + response back.                            | `[1] [1'] [2] [2'] [3] [5] [12] [13a] [13b] [14]` |
| 2 | [`DIAGRAM-BASELINE-2-data-foundation.md`](./DIAGRAM-BASELINE-2-data-foundation.md) | **Data Foundation & Composition** — where the intelligence comes from: session-init grounding load across CMS + ADP + Memory, per-turn KB / LTM / Recalls / DDB reads and writes. CVX's actual differentiator. | `[4a] [4b] [4c] [6] [7] [8] [9]`            |
| 3 | [`DIAGRAM-BASELINE-3-escalation.md`](./DIAGRAM-BASELINE-3-escalation.md)         | **Escalation & Human Hand-off** — the P0 safety invariant + P1/P2 policy exception paths from triage output through Amazon Connect's escalation queues + Customer Profiles + Agent Desktop widget to a Tier-2 Human Agent. | `[10a] [10b] [11]`                          |

Each file is a self-contained diagram input: its own legend reference, components
table, arrows table, ASCII layout sketch, and illustrator notes.

---

## Split rationale

The original single-diagram baseline packed 14 boxes and 21 arrows into one artwork.
At normal print/landing-page rendering density this crossed the readability threshold —
three distinct stories (orchestration / composition / escalation) shared one visual
surface, and none of them got the space to be clean. The three seams are:

- **Orchestration** is the "request/response spine" story. It answers *how* the system
  handles a single turn. Its natural boundary is the Supervisor Lambda + AgentCore
  Runtime spine.
- **Data Foundation & Composition** is CVX's actual differentiator — the composition
  layer over CMS + ADP + AgentCore Memory. It's the *why-CVX-exists* story and needs
  the full cross-account boundary + collapsed Centralized Data Repository detail to
  land. Cramming it into the orchestration diagram flattens it.
- **Escalation & Human Hand-off** is a distinct sub-story with a distinct set of
  components (Connect's escalation-queue / Customer Profiles / Agent Desktop
  sub-detail + Tier-2 Human Agent actor + CVX operational DDB write). It's already its
  own Implementation Guide chapter (`contact-center-integration.adoc`) — the split
  matches the existing IG topology.

**Why not two.** The 2-way split (orchestration + escalation combined, data-foundation
separate) was considered and rejected. Escalation's component set is different from
orchestration's — Connect's escalation-side sub-list (P0/P1/P2 queues, Customer
Profiles, Agent Desktop widget) is distinct from the channel/tenant-flow sub-list used
in orchestration, and rendering both sub-lists on the same box is visually noisier
than rendering the box twice with different sub-lists across two diagrams. The Tier-2
Human Agent actor and the CVX operational DDB `vsa-escalation-records` write are also
escalation-only. Keeping them on their own diagram sharpens both.

**Numbered-badge stability.** Badges are consistent across all three files —
`[7]` always means cross-account Bedrock KB `Retrieve`, `[11]` always means Connect
routing to Tier-2 Human Agent, and so on. This lets downstream artifacts (README
callouts, Implementation Guide flow-step numbers, `docs/FLOW-STEPS.md`) reference
badges uniformly regardless of which diagram they render in.

**Shared components rendered multiple times.** Amazon Connect appears on Diagrams
1 and 3 with **two distinct sub-lists**: channel + tenant-flow (Diagram 1) and
escalation queues + Customer Profiles + Agent Desktop widget (Diagram 3). AgentCore
Runtime appears on every diagram — full sub-list on Diagram 1, compact edge box on
Diagrams 2 and 3. This is intentional — each diagram surfaces the aspects relevant
to its story, not the full component definition every time. Cross-diagram consistency
is preserved via the stable-badge convention and the sub-list allocation notes in
each file's "Rendering notes" section.

---

## Shared legend

The full legend lives here; each split file references this section rather than
duplicating.

| Element                        | Convention                                                                                 |
| ------------------------------ | ------------------------------------------------------------------------------------------ |
| **Solid AWS-service box**      | Concrete AWS service in the deployed topology                                              |
| **Dashed rectangle**           | Account / region governance boundary                                                       |
| **Labeled light-fill grouping** | Sub-system grouping that is NOT a governance boundary (e.g. CMS foundation grouping on Diagram 2) — visually distinct from dashed governance boundaries |
| **Solid arrow →**              | Request / read path (with a numbered flow-step badge `[n]`)                                |
| **Dashed arrow ⇢**             | Write path or asynchronous emission                                                        |
| **Numbered badge `[n]`**       | Flow-step sequence; groups of numbers travel together (e.g. `[7a]` and `[7b]` are one step) |
| **Person icon**                | Human actor (customer, tier-2 agent)                                                       |
| **Collapsed box (with `+`)**   | Simplified aggregation of multiple sub-services; label enumerates the contents in parens   |
| **Off-diagram reference tag**  | Pill or chevron shape (distinct from AWS-service box), light-gray border and fill, label `→ Diagram N` or `← Diagram N` — marks where a flow-step leaves or enters this diagram and continues in another |

The illustrator should use the AWS Architecture Icons v20 (or latest) from
<https://aws.amazon.com/architecture/icons/>. Text should render with a white
background for TCX publishing compliance.

---

## Component inventory (across all three diagrams)

The 14 components from the single-diagram baseline, redistributed into Diagrams 1-3.

| # | Name                                          | Diagram(s) it appears on                                                  |
| - | --------------------------------------------- | ------------------------------------------------------------------------- |
| A | Customer                                      | 1                                                                         |
| B | Tier-2 Human Agent                            | 3                                                                         |
| 1 | Amazon Connect                                | 1 (channel + tenant-flow sub-list); 3 (escalation + Profiles + widget sub-list) |
| 2 | Amazon API Gateway (+ Cognito)                | 1                                                                         |
| 3 | Supervisor Lambda (proxy)                     | 1                                                                         |
| 4 | Bedrock AgentCore Runtime                     | 1 (full: bidi/text/Sonnet); 2, 3 (compact edge)                           |
| 5 | AgentCore Memory (LTM)                        | 2                                                                         |
| 6 | Triage Classifier Lambda                      | 1                                                                         |
| 7 | CVX operational DynamoDB                      | 3                                                                         |
| 8 | CMS Redis LKS                                 | 2                                                                         |
| 9 | CMS DynamoDB                                  | 2                                                                         |
| 10 | CMS Recalls Integration Lambda               | 2                                                                         |
| 11 | Bedrock Knowledge Base                       | 2                                                                         |
| 12 | Centralized Data Repository (collapsed)      | 2                                                                         |

**Totals.** 12 AWS-side boxes (numbered `1`–`12`) plus 2 human actors (`A`, `B`) =
14 distinct components. Amazon Connect and AgentCore Runtime are rendered on
multiple diagrams with different sub-lists / roles; Centralized Data Repository
is rendered on Diagram 2 only.

---

## Numbered arrow inventory (across all three diagrams)

Master badge-continuity table `[1]` through `[14]` — every numbered flow-step
across the three-diagram set, with owning diagram and target. Badges are stable:
badge `[7]` means the same thing on every diagram it appears on.

| Step         | Kind   | Diagram(s)                | Source → Target                                                         |
| ------------ | ------ | ------------------------- | ----------------------------------------------------------------------- |
| `[1]`        | Solid  | 1                          | Customer (A) → Amazon Connect (1)                                       |
| `[1']`       | Solid  | 1                          | Customer (A) → API Gateway (2)                                          |
| `[2]`        | Solid  | 1                          | Amazon Connect (1) → Supervisor Lambda (3)                              |
| `[2']`       | Solid  | 1                          | API Gateway (2) → Supervisor Lambda (3)                                 |
| `[3]`        | Solid  | 1 (full) / 2 (entry edge)  | Supervisor Lambda (3) → AgentCore Runtime (4)                           |
| `[4a]`       | Solid  | 2                          | AgentCore Runtime (4) → CMS Redis LKS (8)                               |
| `[4b]`       | Solid  | 2                          | AgentCore Runtime (4) → Centralized Data Repository (12) (cross-account) |
| `[4c]`       | Dashed | 2                          | AgentCore Runtime (4) → AgentCore Memory (5)                            |
| `[5]`        | Solid  | 1                          | AgentCore Runtime (4) → Triage Classifier Lambda (6)                    |
| `[6]`        | Solid  | 2                          | AgentCore Runtime (4) → AgentCore Memory (5) (per-turn read)            |
| `[7]`        | Solid  | 2                          | AgentCore Runtime (4) → Bedrock Knowledge Base (11) (cross-account)     |
| `[8]`        | Solid  | 2                          | AgentCore Runtime (4) → CMS Recalls Integration Lambda (10)             |
| `[9]`        | Dashed | 2                          | AgentCore Runtime (4) → CMS DynamoDB (9)                                |
| `[10a]`      | Dashed | 3                          | AgentCore Runtime (4) → CVX operational DynamoDB (7)                    |
| `[10b]`      | Solid  | 3                          | AgentCore Runtime (4) → Amazon Connect (1)                              |
| `[11]`       | Solid  | 3                          | Amazon Connect (1) → Tier-2 Human Agent (B)                             |
| `[12]`       | Solid  | 1                          | AgentCore Runtime (4) → Supervisor Lambda (3)                           |
| `[13a]`      | Solid  | 1                          | Supervisor Lambda (3) → Amazon Connect (1)                              |
| `[13b]`      | Solid  | 1                          | Supervisor Lambda (3) → API Gateway (2)                                 |
| `[14]`       | Solid  | 1                          | Amazon Connect (1) / API Gateway (2) → Customer (A)                     |

**Cross-diagram edges** (off-diagram tags represent these on each diagram):

| Step  | Represented on   | As                                                    |
| ----- | ---------------- | ----------------------------------------------------- |
| `[3]` | Diagram 1        | Full arrow (Supervisor → AgentCore Runtime)           |
| `[3]` | Diagram 2        | Entry tag `← Diagram 1` terminating at box 4          |
| `[4] session init` | Diagram 1 | Exit tag `→ Diagram 2` from box 4                     |
| `[6/7/8/9] per-turn grounding` | Diagram 1 | Exit tag `→ Diagram 2` from box 4       |
| `[10] escalate` | Diagram 1 | Exit tag `→ Diagram 3` from box 4 (P0/P1/P2 trigger) |
| `[5] triage output` | Diagram 3 | Entry tag `← Diagram 1 [5]` terminating at box 4 |

---

## Design directives (inherited from original single-diagram baseline)

**User directive on ADP internals collapse (2026-07-14).** The ADP-side analytical
data-layer internals (Amazon Athena + AWS Glue + Amazon S3, governed by AWS Lake
Formation) are collapsed into a **single** labeled box — `Centralized Data Repository` —
for presentation simplicity. The account/region boundary around that box is
**preserved** (dashed rectangle per AWS convention) because the cross-account
authorization surface is a load-bearing element of the architecture, not a hidden
detail. The **Bedrock Knowledge Base remains its own box** because it is the specific
integration point the agent calls via cross-account `Retrieve` — not an ADP-internal
implementation detail. This collapse lives on Diagram 2.

**Amazon Connect is a first-class component.** Connect appears on Diagrams 1 and 3
with two distinct sub-lists. Not omitted from any diagram set.

---

## Sourcing traceability

Every architectural claim across the three diagrams traces back to a specific section
of the completed 17-chapter guidance-doc IG. See each diagram file for scope-specific
sourcing; the master mapping is:

| Diagram element                              | Source section (`.adoc` file)                        | Renders on |
| -------------------------------------------- | ---------------------------------------------------- | ---------- |
| CVX as composition layer (framing)           | `architecture-overview.adoc` § `arch-composition-layer` | 2 (primary), 1 |
| Bedrock AgentCore Runtime (bidi + text)      | `architecture-overview.adoc` § `arch-agentcore-runtime` | 1 (full)   |
| Supervisor agent + Claude Sonnet             | `architecture-overview.adoc` § `arch-supervisor-agent` | 1          |
| Tool catalog                                 | `architecture-overview.adoc` § `arch-tool-catalog`   | 1, 2       |
| Channel ingress (voice / chat / web)         | `architecture-overview.adoc` § `arch-flow-channels`  | 1          |
| Supervisor orchestration steps 1–6           | `architecture-overview.adoc` § `arch-flow-steps`     | 1          |
| Grounding data path (Memory / KB)            | `architecture-overview.adoc` § `arch-flow-grounding` | 2          |
| CDK stack topology                           | `architecture-overview.adoc` § `arch-topology-stacks` | 1          |
| Cross-account topology + Nova regions        | `architecture-overview.adoc` § `arch-topology-cross-account` | 2 |
| Well-Architected callouts                    | `architecture-overview.adoc` § `arch-well-architected` | (see Roadmap below) |
| Amazon Connect voice / chat channels         | `contact-center-integration.adoc` § `connect-channels` | 1          |
| Tenant-branded contact flows                 | `contact-center-integration.adoc` § `connect-tenant-branded-flows` | 1 |
| P0/P1/P2/P3 escalation queues + safety invariant | `contact-center-integration.adoc` § `connect-escalation-queues` | 3 |
| Tier-2 agent-assist + Custom Views widget    | `contact-center-integration.adoc` § `connect-tier2-agent-assist` | 3 |
| Customer Profiles (VehicleProfile / ServiceRecord) | `contact-center-integration.adoc` § `connect-customer-profiles` | 3 |
| Warm-transfer sequence + fallback            | `contact-center-integration.adoc` § `connect-warm-transfer` | 3 |
| CMS data surfaces                            | `building-on-cms-and-adp.adoc` § `bca-cms`           | 2          |
| ADP tabular data products                    | `building-on-cms-and-adp.adoc` § `bca-adp-tabular`   | 2          |
| ADP Bedrock KB source categories             | `building-on-cms-and-adp.adoc` § `bca-adp-kb`        | 2          |
| Session-initialization and data fusion       | `building-on-cms-and-adp.adoc` § `bca-data-fusion`   | 2          |
| Cross-account Lake Formation grants          | `building-on-cms-and-adp.adoc` § `bca-lakeformation` | 2          |
| Bedrock KB resource policy                   | `building-on-cms-and-adp.adoc` § `bca-bedrock-kb-policy` | 2       |
| CDK context parameters (`adpAccountId` etc.) | `building-on-cms-and-adp.adoc` § `bca-cdkcontext`    | (see Roadmap) |
| Same-account cross-region topology           | `building-on-cms-and-adp.adoc` § `bca-staging-topology` | (see Roadmap) |
| Split-account topology                       | `building-on-cms-and-adp.adoc` § `bca-splitaccount-topology` | (see Roadmap) |
| Graceful degradation behaviors               | `building-on-cms-and-adp.adoc` § `bca-graceful-degradation` | (see Roadmap) |

**Customer-journey stage-specific content.** The prescriptive customer-journey
patterns (Onboard / Own vehicle-identity + proactive-engagement, Discover / Find
`catalog_query()` / `offers_lookup()`, Acquire / Finance `finance_qualify()` /
`payment_initiate()` / `reservation_handoff()` / `VehicleOwnershipTransfer`,
Sell / Trade-in `commerce_handoff()`) live in
`~/AWSGuidanceforConnectedVehicleExperienceDocs/latest/implementationguide/the-customer-journey.adoc`
as IG prose, not as rendered diagrams. See the retirement note at the top of this
index for the rationale and the completeness audit at
`docs/DIAGRAM-4-7-RETIREMENT-AUDIT.md` for the analysis that confirmed no
foundational architecture was lost when Diagrams 4-7 were retired. Diagram 2
carries two insurance annotations that surface the cross-diagram concepts flagged
by that audit: an ADP cross-account write-grant note on `[4b] / [7]`, and a
STAR-JSON schema-family note on `[9]`.

---

## Roadmap / not yet covered

Content that exists in the source IG or the original single-diagram baseline but does
NOT render on any of the three split diagrams as authored — call-outs for the
illustrator and the user producing the final artwork.

### 1. Well-Architected callouts (§ `arch-well-architected`)

The original single-diagram baseline `§ 5.6` listed four optional callout tags
(Security / Reliability / Cost / Performance) as space-permitting overlays. **Not
allocated to any of the three split diagrams.** Options:

- **Overlay on Diagram 1** — request/response spine is the most natural surface for
  Security callouts (least-privilege at six boundaries; server-side persona inference).
- **Overlay on Diagram 2** — data-foundation is the most natural surface for Cost
  callouts (Memory 15-min TTL, per-session `persona_loader` Athena cost, persona-scoped
  `source_category` KB filters) and for Reliability (graceful degradation in every
  tool factory).
- **Separate right-side callout column across all three diagrams** — simplest visually,
  duplicates the content but keeps the diagrams uncluttered.
- **A separate "WA overlay" artwork** — highest fidelity, but adds another file to the
  set and is not a blocker for v1 publish.

Deferred. Recommend: Security → Diagram 1, Cost + Reliability → Diagram 2, Performance
Efficiency → Diagram 1 or 2. Operator/illustrator to decide during production.

### 2. Topology variants (§ `bca-staging-topology` + § `bca-splitaccount-topology`)

The original baseline noted that ONE diagram covers both deployment topologies
(same-account cross-region v1 staging vs split-account production) — only the
`adpAccountId` CDK context variable differs. This still holds — Diagram 2 uses the
same dashed ADP-account boundary regardless of topology, and the Lake Formation
cross-account APIs are exercised in both. **Not deferred** — the three-file set as
authored covers both.

**However** — if the illustrator or user wants a topology-comparison artifact
explicitly showing the two account layouts side by side, that would be an
additional diagram. Currently not planned for v1 (matches the original baseline's
judgment).

### 3. CDK context parameters (§ `bca-cdkcontext`)

The `adpAccountId` / `adpRegion` / other cross-account CDK context knobs are described
in the IG chapter but do NOT render on any of the three architecture diagrams — they
are deployment-time parameters, not runtime architecture. **Correctly out of scope.**
If a v1.1 wants a "CDK context knobs" reference card, it's a documentation artifact
(table or code sample), not a diagram.

### 4. Graceful degradation behaviors (§ `bca-graceful-degradation`)

Each tool factory has documented graceful-degradation behavior (LKS miss → "vehicle
state currently unavailable"; KB `Retrieve` throttle → "insight not available at this
time"; Athena permission denied → skip persona seed). **Not rendered on any diagram.**
These are per-tool behaviors, not architectural boundaries — they belong in the tool
catalog or the Well-Architected Reliability callout, not on an architecture diagram.

If the operator wants a "failure modes" overlay, it's a distinct artifact — table or
sequence diagram, not a component/flow diagram like the three in this set.

### 5. Nova 2 Sonic cross-region routing detail

Nova 2 Sonic runs inside the AgentCore bidi runtime container; on Diagram 1 it appears
as internal text of box 4. **The cross-region us-west-2 → us-east-1 fallback routing**
(mentioned in `arch-topology-cross-account`) is not visualized. Currently correctly
elided — the original baseline flagged this as a non-goal.

### 6. Polly / Transcribe fallback route

Mentioned in `arch-topology-cross-account` as a fallback route only, not the default
path. **Correctly out of scope of the diagram set** — flagged in the original baseline
non-goals.

### 7. WebSocket reasoning-feed API

Mentioned in `arch-topology-stacks`; low-signal for the reference-architecture surface.
**Correctly out of scope** — flagged in the original baseline non-goals.

### 8. IoT Core / MSK / Flink CMS-internal telemetry pipeline

CMS-internal producer path that feeds the LKS + DynamoDB. CVX reads outputs only.
**Correctly out of scope** — flagged in the original baseline non-goals.

### 9. Individual tool Lambdas

Not broken out (book / escalate / knowledge / vehicle_identity / service_network /
parts_lookup / etc.). All represented inside the Supervisor Lambda / AgentCore Runtime
boxes per rendering notes. **Correctly out of scope** — flagged in the original baseline
non-goals.

### 10. Response chain for chat-in-Connect step `[13a]`

The `[13a]` arrow (Supervisor Lambda → Amazon Connect for voice audio streaming or
chat text streaming) renders on Diagram 1. **The Connect Chat WebSocket detail** is
elided — same policy as the original baseline (WebSocket reasoning-feed non-goal).

### 11. Code-repo alignment status

All three split diagrams derive from the completed 17-chapter guidance-doc IG at
`~/AWSGuidanceforConnectedVehicleExperienceDocs/latest/implementationguide/`, which is
the authoritative topology source. Every box and arrow maps to an existing code
artifact in the CVX repo (Lambdas under `lambdas/`, agents under `agents/supervisor/`,
CDK stacks under `infrastructure/lib/`, AgentCore runtime configuration in
`.bedrock_agentcore.yaml`, IAM inline policies inside the CDK stacks).

**Well-Architected callouts** (item 1 above) are documented in the IG chapter but
not deployed as tagged CDK metadata or CloudWatch dashboards. They are architectural
intent statements, not deployed instrumentation. If a v1.1 wants Well-Architected-
tagged dashboards or Config rules, that's a separate build.

---

## Delta from prior single-diagram baseline (superseded 2026-07-14)

**Same 14 original boxes, same 21 original numbered arrows.** The 2026-07-14 split
into Diagrams 1-3 was presentation-only — no architectural content was added,
removed, or reinterpreted. The prior baseline's components table, arrows table,
rendering notes, and non-goals policies were all preserved and redistributed
across the three files. The ASCII layout sketch in the prior baseline was likewise
redistributed and scoped per diagram.

The relevant substance from earlier baseline iterations (Connect first-class, ADP
collapse, cross-account boundary preserved, KB kept as own box, Tier-2 Human Agent
added) is all preserved in the three-file split.

---

## What this baseline set is not

Not the diagrams. This is the input specification for the illustrator. Not a runbook,
ADR, or operator guide — for those, see `docs/DEPLOYMENT.md`, `docs/ARCHITECTURE.md`,
and the completed IG at
`~/AWSGuidanceforConnectedVehicleExperienceDocs/latest/implementationguide/`.

The user retains authority over the visual product. This baseline set exists so that
when the three diagrams are produced (via the design tool of the user's choice), every
box, arrow, and badge in the final artwork traces back to a documented section of the
completed CVX guidance-doc IG and applies the design directives (Amazon Connect
first-class + ADP internals collapsed + numbered-badge stability) exactly.
