# CVX Diagram Consolidation — Audit of Diagram Baselines vs. Post-Agentic-Depth-Rewrite IG

**Audit date:** 2026-07-15
**Audit scope:** 7 diagram baselines at `docs/DIAGRAM-BASELINE-{1..7}-*.md`
**Reference IG text:** `~/AWSGuidanceforConnectedVehicleExperienceDocs/latest/implementationguide/{the-customer-journey.adoc, agentic-architecture.adoc}` (post agentic-depth rewrite)
**Audit-only:** No diagram artwork, no `.drawio` file, no `DIAGRAM-BASELINE-*.md` file is edited by this audit.

---

## 0. Convention shift (context for every section below)

The IG text has moved to a **stage-level trailing-callout maturity convention** in `the-customer-journey.adoc`:

- `Demonstrated in CVX::` — the stage's tools, data-product wiring, and reference implementation exist in the CVX code base today. Applies to **Onboard / Own** and **Service**.
- `Not yet demonstrated in a reference implementation::` — the architectural pattern is described, but the tools and data-product integrations are tenant- and roadmap-dependent. Applies to **Discover / Find**, **Acquire / Finance**, and **Sell / Trade-in**.

This replaces the **OLD leading `[functional]` / `[roadmap]` inline-tag convention** used by the pre-rewrite IG and carried forward into the Diagram 4/5/6/7 baselines. The trailing callout is stage-level (one per stage), not per-arrow.

Note: `agentic-architecture.adoc` still uses `[functional]`/`[roadmap]` inline tags internally (in its "Maturity framing" section, tool catalog, and mesh topology). That is intentional and stage-agnostic — it describes the platform's current-state-vs-target-state split, not any specific customer-journey stage. Diagrams 1–3 correspond to `agentic-architecture.adoc`'s stage-agnostic surface, so their labeling posture is independent of the trailing-callout convention.

**Baseline usage of the OLD convention** (grep evidence):

| Baseline | `[functional]`/`[roadmap]` count |
|---|---|
| Diagram 1 (Orchestration) | 0 |
| Diagram 2 (Data Foundation) | 0 |
| Diagram 3 (Escalation) | 0 |
| Diagram 4 (Onboard/Own) | 30 |
| Diagram 5 (Discover/Find) | 31 |
| Diagram 6 (Acquire/Finance) | 44 |
| Diagram 7 (Sell/Trade-in) | 27 |

Diagrams 1–3 never adopted the inline-tag convention (stage-agnostic scope, all-`[functional]` plumbing). Diagrams 4–7 adopted it heavily and must migrate to the trailing-callout convention.

---

## 1. Diagram 1 — Agentic Orchestration

**(a) Maturity labeling.** No `[functional]`/`[roadmap]` inline tags used (0 occurrences). Diagram 1 is stage-agnostic: it renders the ingress→Supervisor→Runtime→Triage→response spine that composes for all journey stages. The new trailing-callout convention is a per-stage device; Diagram 1 is not a stage diagram, so it doesn't need a callout. Labeling posture is already correct.

**(b) AgentCore Runtime / AgentCore Memory / Nova 2 Sonic / Amazon Connect composition detail.**
- AgentCore Runtime (box 4) present with the two variants `bidi (voice, Nova 2 Sonic)` and `text (chat / web / mobile)`, plus the `Claude Sonnet 4.6` annotation ✓
- AgentCore Memory: correctly punted to Diagram 2 (represented via off-diagram edge `→ Diagram 2`) ✓
- Nova 2 Sonic: rendered inline as a Runtime sub-note (per convention, Nova 2 Sonic is not its own box) ✓
- Amazon Connect: rendered with the channel/tenant-flow sub-list (voice + chat + tenant-branded contact flows); escalation-side and outbound-side sub-lists correctly deferred to Diagrams 3 and 4 ✓

**(c) New named tools / data flows / components in the rewritten IG that don't appear on the diagram.** None. The IG's `agentic-architecture.adoc` chapter continues to be authoritative for Diagram 1's story — the composition described there (AgentCore Runtime with two runtime modes, the sub-agent-as-tool pattern, Nova 2 Sonic constraints, Triage as a deterministic-first tool call) matches what Diagram 1 renders.

**Verdict:** **NO CHANGES NEEDED**

---

## 2. Diagram 2 — Data Foundation & Composition

**(a) Maturity labeling.** No `[functional]`/`[roadmap]` inline tags used (0 occurrences). Stage-agnostic — Diagram 2 renders the ADP-side cross-account grounding surface plus the CMS operational foundation that composes for every stage. No stage-level trailing callout applies. Labeling posture is already correct.

**(b) Composition detail.**
- AgentCore Runtime rendered compact (edge role) with explicit "no bidi/text/Sonnet sub-list here — see Diagram 1" note ✓
- AgentCore Memory (box 5) rendered adjacent to the Runtime with session-init write `[4c]` and per-turn read `[6]` ✓
- Nova 2 Sonic: correctly deferred to Diagram 1 ✓
- Amazon Connect: not on this diagram (correct — Diagram 2 is downstream of ingress) ✓
- Cross-account boundary (dashed) around ADP KB + Centralized Data Repository ✓
- CMS foundation grouping around LKS + DynamoDB + Recalls Lambda ✓

**(c) New named tools / data flows / components in the rewritten IG that don't appear on the diagram.** None. Diagram 2's coverage of the composition layer (ADP KB Retrieve, ADP Athena via Centralized Data Repository, CMS Redis LKS, CMS DynamoDB, CMS Recalls Lambda, AgentCore Memory LTM) matches the IG's Service-stage data-wiring text and the ADP-side data-foundation posture the IG carries forward across stages.

**Verdict:** **NO CHANGES NEEDED**

---

## 3. Diagram 3 — Escalation & Human Hand-off

**(a) Maturity labeling.** No `[functional]`/`[roadmap]` inline tags used (0 occurrences). Stage-agnostic — Diagram 3 renders the escalation triangle that composes for both Service (P0/P1/P2 safety queues) and Acquire (tier-2 finance-specialist queue). No stage-level trailing callout applies. Labeling posture is already correct.

**(b) Composition detail.**
- AgentCore Runtime rendered compact (edge role) ✓
- Amazon Connect rendered with escalation-side sub-list (escalation queues + Customer Profiles + Agent Desktop Custom Views widget) ✓
- CVX operational DynamoDB (box 7) rendered with `vsa-escalation-records` + `vsa-tenant-config` sub-label ✓
- Tier-2 Human Agent (box B) person icon ✓
- Escalation triggered narrative (P0 safety invariant + P1/P2 policy exception + explicit customer request) matches the IG's Service-stage escalation description ✓

**(c) New named tools / data flows / components in the rewritten IG that don't appear on the diagram.** None. The IG's Service-stage escalate() description matches Diagram 3 exactly; the Acquire-stage IG explicitly notes the escalation is "the exact same three-arrow triangle the Service stage uses", and Diagram 6's baseline confirms it's reused wholesale from Diagram 3.

**Verdict:** **NO CHANGES NEEDED**

---

## 4. Diagram 4 — Onboard / Own (identity + proactive engagement)

**(a) Maturity labeling.** Uses OLD `[functional]`/`[roadmap]` inline-tag convention heavily (30 occurrences). Every arrow on the diagram carries a `[functional]` or `[roadmap]` tag; every new box (13 Flink, 14 MSK, 15 Proactive Engagement Lambda, C Subscription-management, D CRM) is labeled `[roadmap]`.

The IG stage-level trailing callout for Onboard / Own reads:

> **Demonstrated in CVX::** Vehicle identity resolution, session-init AgentCore Memory LTM hydration, and **the proactive-engagement pipeline (CMS Flink telemetry processors → MSK → Proactive Engagement Lambda → Amazon Connect outbound campaign) are operational in the CVX reference implementation.** Relevant sources: `agents/supervisor/supervisor.py`, `lambdas/agent/tools/vehicle_identity.py`, `lambdas/proactive/` (for the outbound-engagement pattern), and `infrastructure/lib/` for the corresponding CDK. Subscription-management partner integration (illustrative: Zuora) and CRM partner integration (illustrative: Salesforce Service Cloud) are illustrative — the specific partner webhook contracts remain tenant-dependent.

**This creates two mismatches:**
- Convention style: `[functional]`/`[roadmap]` inline tags are OLD. The trailing-callout convention is now stage-level, singular per stage.
- **Substantive maturity drift:** The Diagram 4 baseline labels the entire lower band (CMS Flink processors, MSK, Proactive Engagement Lambda) as `[roadmap]` on the premise "CVX consumption is not deployed today". The IG now explicitly states that the proactive-engagement pipeline (Flink → MSK → Proactive Engagement Lambda → Connect outbound) **is operational in CVX today** and cites `lambdas/proactive/` as the reference source. Only the illustrative partners (Zuora, Salesforce Service Cloud) remain tenant-dependent.

**(b) Composition detail.**
- AgentCore Runtime (box 4) rendered as shared spine ✓ with "session-init (upper band)" and "compose-outbound (lower band)" sub-annotation ✓
- AgentCore Memory (box 5) rendered compactly, with first-time-hydration role ✓
- Amazon Connect (box 1) rendered with outbound-side sub-list (StartOutboundVoiceContact + StartOutboundChatContact + Customer Profiles widget) ✓
- Nova 2 Sonic: not shown; correctly deferred to Diagram 1 per convention (Onboard/Own IG does not add stage-specific Nova Sonic detail) ✓
- CMS DynamoDB, CMS Flink, MSK, Proactive Engagement Lambda all present ✓
- Session-init cluster `[4a] [4b] [4c]` all present as reused from Diagram 2 ✓

**(c) New named tools / data flows / components in the rewritten IG that don't appear on the diagram.** None missing at the component level. All named components in the IG (Flink processors, MSK, Proactive Engagement Lambda, Connect outbound campaign, Salesforce Service Cloud CRM alternative, Zuora subscription activation) are on the diagram.

**Verdict:** **BOTH** (style + content, style-heavy)

Style: Replace the ~30 per-arrow `[functional]`/`[roadmap]` inline tags with a single stage-level trailing callout matching the IG's `Demonstrated in CVX::` framing. Illustrative partners (Zuora, Salesforce Service Cloud) can keep an illustrative-only marker; the operating AWS-side pipeline should drop its `[roadmap]` labels.

Content correction (substantive, not just cosmetic): The lower band's Flink→MSK→Proactive-Engagement-Lambda→Connect pipeline is NOT `[roadmap]` per the current IG — it's demonstrated in CVX (`lambdas/proactive/`). The current diagram labeling misrepresents the IG's claim about what's shipped.

---

## 5. Diagram 5 — Discover / Find (pre-purchase browse)

**(a) Maturity labeling.** Uses OLD `[functional]`/`[roadmap]` inline-tag convention (31 occurrences). Every new Discover-specific arrow (`[23] [24a] [24b] [25]`) is labeled `[roadmap]`; reused plumbing badges carry `[functional]`; `[4b]` carries a hybrid "component `[functional]` / query shape `[roadmap]`" tag.

The IG stage-level trailing callout for Discover / Find reads:

> **Not yet demonstrated in a reference implementation::** The Discover / Find stage is a forward-looking pattern. The AgentCore Runtime, AgentCore Memory, Nova 2 Sonic, and Amazon Connect components are operational in CVX today for the Service stage; the tools and data-product reads specific to Discover (`catalog_query()`, `offers_lookup()`, the persona-segment scoping config schema in `vsa-tenant-config`, and the ADP Vehicle Identity Graph and Campaigns/Offers data products) are tenant- and roadmap-dependent.

**(b) AgentCore Runtime / AgentCore Memory / Nova 2 Sonic / Amazon Connect composition detail.**

The rewritten IG now adds explicit stage-specific composition subsections that were NOT in the pre-rewrite text:

- **"Session start via Amazon Connect or Web/Mobile"** — Diagram 5 covers both `[1]` voice/chat and `[1']` web/mobile ingress ✓
- **"Persona-segment resolution"** — Diagram 5 renders as `[23]` ✓
- **"AgentCore Runtime dispatches per-turn tools"** (`catalog_query()`, `offers_lookup()`) — Diagram 5 renders as `[24a]/[24b]/[25]` ✓
- **"AgentCore Memory session-init hydration"** with the Discover-specific `segment_scope` metadata write — Diagram 5 renders as `[4c]` with the `segment_scope` sub-annotation ✓
- **"Nova 2 Sonic for voice-mediated Discover"** — the IG now explicitly states:
  > "When the browse conversation enters through the Amazon Connect voice channel, the bidi AgentCore Runtime carries the conversation over Nova 2 Sonic — the same speech-to-speech model the Service stage uses. An illustrative voice turn: _'I'm looking for a mid-size SUV with tow capacity above 5,000 lb — what fits, and what's the current lease offer?'_ The platform-level Nova 2 Sonic constraints documented in xref:agentic-architecture[] — approximately a 1-second tool-completion budget and regional availability limited to us-east-1, eu-north-1, and ap-northeast-1 — apply unchanged at the Discover stage."

  Diagram 5's Runtime is rendered compact with the explicit note "Do NOT re-draw the bidi/text/Sonnet sub-list here — that lives on Diagram 1." Connect is also rendered compact with no sub-list. **Under the compositional-reuse convention, Nova 2 Sonic is inherited from Diagram 1** — the reader is expected to bring that composition forward. However, the IG now explicitly names Nova 2 Sonic as a stage-specific composition element with an illustrative voice turn. Two reasonable positions:
  - Convention-strict: no change — Diagram 1 delegation is sufficient.
  - Content-explicit: add a small stage-specific annotation (either on the `[1]` voice-ingress path or as a Runtime-box sub-note) acknowledging the voice-mediated Discover pattern, without violating the "no bidi/Sonnet sub-list" convention.

- **"Amazon Connect for channel entry and escalation"** — the IG now adds:
  > "Escalation to a human specialist at the Discover stage is not a safety-gate-style fallback — browse conversations do not carry a P0-style mandatory-escalation trigger. The customer can always request a human via `escalate()`, which routes to a generic advisor queue if the tenant has one configured in `vsa-tenant-config`. This is a deliberate difference from the Acquire stage, where escalation is a first-class, compliance-load-bearing path."

  Diagram 5 baseline explicitly excludes the escalation triangle: *"Escalation `[10a] [10b] [11]` — no P0/P1/P2 on a browse surface."* But the IG now describes an optional customer-initiated `escalate()` path routing to a generic advisor queue. **This is a real content gap** — the diagram treats escalation as fully absent, but the IG says it's available (albeit not compliance-load-bearing).

**(c) New named tools / data flows / components in the rewritten IG that don't appear on the diagram.**

1. **Nova 2 Sonic voice-mediated Discover** — architecturally reused from Diagram 1; the IG's new explicit prose adds stage-specific framing (illustrative voice turn + 1-second tool-completion budget applies unchanged) but no new box or new arrow. Marginal content gap — reasonable to address with a small annotation, or defer under strict Diagram-1-delegation convention.
2. **`escalate()` to a generic advisor queue** — the IG now names this optional path explicitly. Diagram 5 does not render escalation at all. Reasonable content additions:
   - Render an off-diagram edge or compact escalation cluster (Runtime → CVX operational DynamoDB write for `vsa-escalation-records` + Runtime → Connect for `TransferContact` → generic advisor queue). Explicitly weaker than Diagram 3's safety triangle: no P0 invariant, no mandatory dispatch — only customer-initiated.
   - Or: add a narrative-only annotation "customer-initiated `escalate()` to generic advisor queue optional; see Diagram 3 for the escalation triangle mechanism" without rendering an arrow.
3. Illustrative upstream: DMS inventory feeds — the IG mentions "Digital commerce (Salesforce, Adobe); DMS inventory feeds" in the stage-summary table and Diagram 5 baseline covers this narratively in § 5.7 (not drawn as an arrow). No gap.

**Verdict:** **BOTH** (style-heavy + content)

Style: Replace the ~31 per-arrow `[functional]`/`[roadmap]` tags with a single stage-level `Not yet demonstrated in a reference implementation::` trailing callout matching the IG.

Content: (a) Optionally surface Nova 2 Sonic explicitly for stage-specific voice-mediated Discover, and (b) add a small representation of the customer-initiated `escalate()` path to a generic advisor queue (either a compact escalation cluster or a narrative annotation with a pointer to Diagram 3).

---

## 6. Diagram 6 — Acquire / Finance (regulated-transaction surface)

**(a) Maturity labeling.** Uses OLD `[functional]`/`[roadmap]` inline-tag convention very heavily (44 occurrences — highest in the set). Every new Acquire-specific arrow (`[26] [27] [28] [29]`) is labeled `[roadmap]`; the reused escalation triangle carries `[functional]` for the mechanism and a mechanism-vs-readiness split annotation for `[11]` (tenant-side finance-specialist queue standup is `[roadmap]`); box H (Contract-management) carries the additional `deferrable-to-v2` marker.

The IG stage-level trailing callout for Acquire / Finance reads:

> **Not yet demonstrated in a reference implementation::** The Acquire / Finance stage is a forward-looking pattern. The AgentCore Runtime, AgentCore Memory, Nova 2 Sonic, and Amazon Connect components — including the full `escalate()` cluster that routes to Amazon Connect tier-2 queues — are operational in CVX today for the Service stage. The Acquire-specific tools (`finance_qualify()`, `payment_initiate()`, `contract_generate()`, `VehicleOwnershipTransfer` STAR emission), the corresponding partner integrations, and the finance-specialist tier-2 queue standup are tenant- and roadmap-dependent. The agent-tool patterns described in this stage are architecturally sound today; the finance-domain data products and partner integrations require tenant-by-tenant setup.

**(b) AgentCore Runtime / AgentCore Memory / Nova 2 Sonic / Amazon Connect composition detail.**

The rewritten IG adds explicit stage-specific composition subsections:

- **"Session start via Amazon Connect or Web/Mobile"** — Diagram 6 covers both `[1]` voice/chat and `[1']` web/mobile ingress ✓
- **"Persona and finance-segment resolution"** — Diagram 6 mentions this in the callout on box 2 (API Gateway + Cognito) but does not render a `[23]`-analogous config-read arrow; the baseline states "the Acquire-stage config reads happen implicitly through the Runtime, not the Supervisor". Consistent with IG; no gap.
- **"AgentCore Runtime dispatches four per-turn tools plus escalation"** — Diagram 6 renders `[26] [27] [28] [29]` plus the reused escalation triangle `[10a] [10b] [11]` ✓
- **"AgentCore Memory session-init hydration"** with Acquire-specific `finance_segment` metadata — Diagram 6 renders as `[4c]` with the finance-segment sub-annotation ✓
- **"Nova 2 Sonic for voice-mediated Acquire"** — the IG now explicitly states:
  > "When the acquire conversation enters through the Amazon Connect voice channel, the bidi AgentCore Runtime carries the conversation over Nova 2 Sonic — the same speech-to-speech model the Service stage uses. An illustrative voice turn: _'Given my income and this trade-in, what's my qualified rate range and monthly payment for the configuration I saved yesterday?'_ The approximately 1-second tool-completion budget documented in xref:agentic-architecture[] matters especially at this stage — long-running partner API calls (in particular `finance_qualify()`, if the decisioning partner is slow) risk exceeding it. See xref:agentic-architecture[] for the mitigation pattern (async tool wrapping with a keep-alive signal during long waits)."

  Diagram 6's Runtime is rendered compact with the explicit note "Do NOT re-draw the bidi/text/Sonnet sub-list here — that lives on Diagram 1." Under the strict Diagram-1-delegation convention, no change is required. Under a content-explicit posture, the diagram should surface at least the `finance_qualify()`-specific 1-second-tool-completion-budget concern, because it is Acquire-specific (the IG says it "matters especially at this stage") and it has a mitigation pattern that architects need to know about. This is a stage-specific implication of Nova 2 Sonic that Diagram 6 does not surface today.

- **"Amazon Connect for channel entry and compliance-load-bearing escalation"** — full escalation triangle present ✓. The IG explicitly says "The `escalate()` surface reuses the exact same three-arrow triangle the Service stage uses" and lists the four escalation conditions; Diagram 6 baseline confirms verbatim reuse of Diagram 3's `[10a] [10b] [11]` ✓. Only the queue-ARN entry differs (finance-specialist queue vs P0/P1/P2 safety queues) — Diagram 6 correctly annotates this.

**(c) New named tools / data flows / components in the rewritten IG that don't appear on the diagram.**

1. **Nova 2 Sonic voice-mediated Acquire with stage-specific 1-second-budget concern on `finance_qualify()`** — architecturally reused from Diagram 1; the IG's new prose specifically names `finance_qualify()` as the tool most at risk of exceeding the budget and points to the async-wrapping mitigation pattern in `agentic-architecture.adoc`. This is Acquire-specific and not covered by Diagram-1 delegation. Reasonable content addition: a small annotation on the `[26]` arrow ("1s tool-completion budget applies; async wrapping mitigation if decisioning partner is slow — see Diagram 1 / agentic-architecture chapter").
2. All four new Acquire tool arrows (`finance_qualify()`, `payment_initiate()`, `contract_generate()`, `VehicleOwnershipTransfer`) are present ✓.
3. All three partner categories (Credit decisioning F, Payments G, Contract management H) present ✓.
4. Bedrock Guardrails annotation on `[26]` ("LLM narrates, does not decide") — the IG's Compliance-and-safety section explicitly calls out "P0-style safety logic applies here"; the annotation is present ✓.
5. STAR-JSON `VehicleOwnershipTransfer` cross-stage-consistency annotation on `[29]` — present ✓.
6. Cross-account write-grants annotation on `[29]` (extends Diagram 2 authorization surface with write grants) — present ✓.

**Verdict:** **BOTH** (style-heavy + minor content)

Style: Replace the ~44 per-arrow `[functional]`/`[roadmap]` tags with a single stage-level `Not yet demonstrated in a reference implementation::` trailing callout matching the IG. Preserve the mechanism-vs-readiness split annotation on `[11]` (tenant-side finance-specialist-queue standup) and the deferrable-to-v2 marker on `[28]`/`H` — those are operationally meaningful and not covered by the stage-level callout.

Content: Add a small stage-specific annotation surfacing the Nova 2 Sonic 1-second-tool-completion-budget concern for `finance_qualify()` and pointing to the async-wrapping mitigation pattern in `agentic-architecture.adoc`. Optionally also surface Nova 2 Sonic voice-mediated Acquire framing more explicitly (parallel treatment to Diagram 5's Nova Sonic content decision).

---

## 7. Diagram 7 — Sell / Trade-in (speculative commerce handoff)

**(a) Maturity labeling.** Uses OLD `[functional]`/`[roadmap]` inline-tag convention (27 occurrences). Every new Sell-specific arrow (`[30] [31]`) is labeled `[roadmap]`; reused plumbing badges carry `[functional]`; `[4b]` carries the hybrid tag.

The IG stage-level trailing callout for Sell / Trade-in reads:

> **Not yet demonstrated in a reference implementation::** The Sell / Trade-in stage is a future / forward-looking concept. No CVX v1 code path exists for a commerce-handoff pattern into a marketplace platform (illustrative: Amazon Autos). The data foundation the pattern would depend on — STAR-shaped service history in the ADP Vehicle Lifecycle data product accumulated across the ownership tenure — is a downstream consequence of Service being operational. Implementers exploring this direction should start there.

**(b) AgentCore Runtime / AgentCore Memory / Nova 2 Sonic / Amazon Connect composition detail.**

The rewritten IG's Sell / Trade-in stage is deliberately brief (five paragraphs total in `the-customer-journey.adoc`) and does NOT add explicit stage-specific composition subsections analogous to those added to Discover / Find and Acquire / Finance. In particular:

- No stage-specific Nova 2 Sonic prose. (The agentic-depth rewrite added voice-conversation detail to Discover and Acquire only, not to Sell / Trade-in.)
- No stage-specific escalation prose.
- No new tools beyond `commerce_handoff()` (which Diagram 7 renders as `[31]` with the label `trade_in_handoff()`).
- Service history STAR-JSON provenance-record framing — Diagram 7 renders as `[30]` with a load-bearing provenance annotation ✓.
- Speculative-reference framing — Diagram 7 already includes a prominent load-bearing "SPECULATIVE REFERENCE" annotation ✓.

**(c) New named tools / data flows / components in the rewritten IG that don't appear on the diagram.** None. The IG stage is thin at this depth and Diagram 7 already reflects that thinness (two new badges, one new partner box, no escalation, no CVX operational DDB, no Nova Sonic detail). The IG stage's data-foundation guidance ("start there") aligns with Diagram 7's provenance-record annotation on `[30]`.

Nomenclature note: Diagram 7 baseline names the tool `trade_in_handoff()`; the IG summary table names it `commerce_handoff()`. Both are illustrative — Diagram 7 § 3.3 explicitly notes "the exact name depends on the selected partner" — and this is a nomenclature harmonization decision for the edit stage, not a substantive content gap.

**Verdict:** **STYLE-ONLY UPDATE** (maturity labeling)

Replace the ~27 per-arrow `[functional]`/`[roadmap]` tags with a single stage-level `Not yet demonstrated in a reference implementation::` trailing callout matching the IG. Preserve the load-bearing SPECULATIVE REFERENCE annotation and the provenance-record annotation on `[30]` — those are load-bearing on this diagram and are not covered by the stage-level callout. Consider harmonizing tool name to `commerce_handoff()` to match the IG summary table (minor, at edit stage).

---

## Summary — one-line verdict per diagram

| # | Diagram | Verdict |
|---|---|---|
| 1 | Orchestration | **NO CHANGES NEEDED** — no inline maturity tags used; composition matches IG. |
| 2 | Data Foundation | **NO CHANGES NEEDED** — no inline maturity tags used; composition matches IG. |
| 3 | Escalation | **NO CHANGES NEEDED** — no inline maturity tags used; composition matches IG. |
| 4 | Onboard / Own | **BOTH** — migrate 30 inline `[functional]`/`[roadmap]` tags to stage-level `Demonstrated in CVX::` callout, AND correct the substantive drift where the lower-band proactive-engagement pipeline (Flink → MSK → Proactive Engagement Lambda → Connect outbound) is now `Demonstrated in CVX::` per IG but labeled `[roadmap]` in the diagram. |
| 5 | Discover / Find | **BOTH** — migrate 31 inline tags to stage-level `Not yet demonstrated…::` callout; add representation of customer-initiated `escalate()` to a generic advisor queue (new in IG); optionally surface Nova 2 Sonic voice-mediated Discover framing beyond Diagram-1 delegation. |
| 6 | Acquire / Finance | **BOTH** — migrate 44 inline tags to stage-level `Not yet demonstrated…::` callout (preserve `[11]` mechanism-vs-readiness split + `[28]/H` deferrable-to-v2 markers); add stage-specific Nova 2 Sonic 1-second-tool-completion-budget annotation on `[26]` (`finance_qualify()`) with pointer to the async-wrapping mitigation in `agentic-architecture.adoc`. |
| 7 | Sell / Trade-in | **STYLE-ONLY UPDATE** — migrate 27 inline tags to stage-level `Not yet demonstrated…::` callout; preserve the load-bearing SPECULATIVE REFERENCE and `[30]` provenance-record annotations. Optional nomenclature harmonization from `trade_in_handoff()` to `commerce_handoff()`. |
