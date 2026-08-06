# AWS Solutions Publishing Template — Landing Page (Draft)

**Status: DRAFT — many fields below are marked `<NEEDS: ...>` and must be
filled by the user or a named point of contact before submission. This is
not ready to submit. Content that IS filled below is sourced directly from
the completed CVX Implementation Guide; every filled field cites its source
so it can be verified against the source doc.**

**Two structural blockers before this can be submitted at all** — see
"Blockers" section at the bottom before reading the rest.

---

## REQUESTED PUBLISHING DATE

| Field | Value |
|---|---|
| Requested Publishing Date | `<NEEDS: user decision>` |
| Is Date Driven By Business-Need? | `<NEEDS: user decision — if yes, state the business driver>` |

## POINTS OF CONTACT

| Role | Name |
|---|---|
| Product Manager – Technical (PM-T) | `<NEEDS: user>` |
| Software Development Manager (SDM) | `<NEEDS: user>` |
| Technical Writer | `<NEEDS: user>` |
| Technical Program Manager (TPM) | `<NEEDS: user>` |
| Business Development Manager (BDM) | `<NEEDS: user>` |

## DIRECTORY CONTENT

| Field | Value | Source |
|---|---|---|
| Solution Name | Guidance for Connected Vehicle Experience on AWS | `build-info.xml` `<title>`; matches "Title" field below per the template's own cross-check instruction |
| Short Description | `<NEEDS: draft below, needs Marketing Lead review per template's own pre-submission checklist>` — draft: *"An AI-powered customer-engagement layer that composes live vehicle data and governed analytics into voice- and text-based agentic experiences across the full vehicle ownership lifecycle."* (176 chars — within the 150-300 range) | Derived from `guidance-overview.adoc` opening paragraph; NOT yet reviewed by an Industry/Product Marketing Lead as the template requires — flagging this explicitly, do not submit without that review |
| Response ID (from Valence) | `<NEEDS: user — Valence-specific, no visibility into this system>` | |

## PAGE CONTENT — HEADER

| Field | Value | Source |
|---|---|---|
| Title | Connected Vehicle Experience on AWS | Following the template's own naming rule ("[Use Case] on AWS"); matches the existing public-facing pattern used by the sibling CMS/ADP guidances (`Connected Mobility on AWS`, `Automotive Data Platform on AWS`) |
| Short Description (subhead, ≤70 chars) | `<NEEDS: Customer Experience team writes this per template's own instructions — explicitly not a field I should draft; it requires an SEO keyword search this agent has no tooling to perform>` | |
| Implementation Guide HTML URL | `<NEEDS: does not exist yet — no public-mirror publish has happened for CVX; see Blockers>` | |
| Overview Intro (50-150 words, no hyperlinks) | *"The Guidance for Connected Vehicle Experience on AWS (CVX) is an AI-powered customer-engagement layer built on Amazon Bedrock and Amazon Connect. It composes live vehicle telemetry from the Connected Mobility guidance with governed analytical data from the Automotive Data Platform guidance to deliver a voice- and text-capable assistant across the vehicle ownership lifecycle — from finding a vehicle through ownership, service, and end-of-life. The assistant can triage a check-engine warning, quote a repair, book a service appointment, and hand off to a human specialist, all within a single conversation, while multi-tenant configuration lets an automotive organization brand and isolate the experience per OEM, fleet, or rental customer."* (103 words) | Adapted from `guidance-overview.adoc` §"What CVX Is" and §"Amazon Connect as the Contact Center Channel" — hyperlinks removed per template instruction |
| Benefit 1 | **Header**: "Unified customer journey" (24 chars). **Body**: "One agentic assistant spans discovery, financing, ownership, and service — replacing siloed point tools with a single conversational thread grounded in real vehicle and customer data." (30 words) | Derived from `the-customer-journey.adoc` framing (5-stage lifecycle) |
| Benefit 2 | **Header**: "Voice and text, one supervisor" (31 chars). **Body**: "Amazon Bedrock AgentCore hosts a single supervisor agent reachable by voice via Amazon Connect and Nova Sonic, or by text via a web or mobile channel — same grounding, same tools, same safety rules." (33 words) | Derived from `architecture-overview.adoc` §"Two runtime modes" |
| Benefit 3 | **Header**: "Deterministic safety, not a guess" (34 chars). **Body**: "Triage classification (P0–P3) runs as deterministic Lambda logic, not an LLM judgment call — a safety-critical P0 finding always escalates to a human agent and cannot be suppressed by the model." (32 words) | Derived from `the-customer-journey.adoc` §"Triage: deterministic P0–P3 classification" and `security.adoc` safety-guardrail section |
| Benefit 4 | **Header**: "Composable, not a monolith" (26 chars). **Body**: "CVX takes no hard runtime dependency on the CMS or ADP foundation guidances — it subscribes to their data. Deploy any layer independently and add CVX when the organization is ready." (30 words) | Derived from `guidance-overview.adoc` §"What CVX Is" ("Each layer is independently deployable... CVX draws... but takes no hard code or runtime dependency") |

## TECHNICAL DETAILS

| Field | Value | Source |
|---|---|---|
| Intro Copy | *"CVX runs as a channel layer above two foundation guidances. Amazon Bedrock AgentCore hosts a supervisor agent that receives requests from Amazon Connect (voice/chat) or a web/mobile API, resolves the caller's context by reading live vehicle state from the Connected Mobility foundation and governed reference data from the Automotive Data Platform foundation via a cross-account Lake Formation grant, and responds through the same channel the request arrived on. Safety-critical findings escalate to a human agent through Amazon Connect's tier-2 queue."* | Derived from `architecture-overview.adoc` composition-layer framing + the shipped Diagram 1 (Agentic Orchestration) and Diagram 2 (Data Foundation & Composition) |
| Image | `<NEEDS: user to produce the actual PNG per this template's own instructions — "Save the diagram as a PNG file with white background... Provide a link to your PPTX and PNG">`. Closest existing assets: `~/guidance-for-connected-vehicle-experience-on-aws/documentation/diagrams/cvx-1-orchestration.drawio.png` (already rendered, white background, but is one of 7 diagrams — this template wants ONE hero diagram, not 7). **Recommend**: use Diagram 1 (Agentic Orchestration) as the landing-page hero since it's the request/response spine and closest to what a customer needs to understand the guidance at a glance; the other 6 diagrams stay in the Implementation Guide body. This is a recommendation, not a decision — confirm with whoever owns the landing-page visual before submitting. | |
| Step 1–11 | See "Flow Steps" subsection below — drafted from Diagram 1's already-numbered badges, condensed to the template's 10-20-words-per-step guidance. **Only 8 of the requested 11 step slots are filled** — Diagram 1 has 10 numbered flow-step badges (`[1][1'][2][2'][3][5][12][13a][13b][14]`), several of which are response-return legs that collapse into fewer prose steps; padding to 11 with invented content would misrepresent the architecture, so 8 real steps are provided and the remaining 3 slots are left `<NEEDS: delete unused step rows>` per the template's own "insert/delete rows as needed" instruction. | |

### Flow Steps (drafted from Diagram 1 badges)

1. A customer initiates a voice or chat session through **Amazon Connect**, or a text session through a web or mobile app via **Amazon API Gateway**.
2. Amazon Connect or API Gateway invokes a proxy **AWS Lambda** function with the session request and channel metadata.
3. The Lambda function invokes **Amazon Bedrock AgentCore Runtime**, which hosts the CVX supervisor agent.
4. On the first turn of a session, the supervisor agent reads live vehicle state from the Connected Mobility foundation and governed reference data from the Automotive Data Platform foundation.
5. The supervisor agent runs deterministic triage classification via an **AWS Lambda** rules engine before responding to any service-related inquiry.
6. The agent synthesizes a grounded response using retrieved data and, for safety-critical findings, invokes escalation to a human tier-2 agent.
7. The response streams back through the same **Amazon Bedrock AgentCore Runtime** session to the originating Lambda proxy.
8. The Lambda proxy returns the response to the customer through the same channel the request arrived on — voice/chat via Amazon Connect, or text via API Gateway.

*(3 slots intentionally left blank — see note above.)*

## USE CASES

| Field | Value |
|---|---|
| Use Case 1 (name and link) | `<NEEDS: user — this is an Alchemy use-case alignment; the earlier conversation referenced Alchemy work-items but I have no visibility into which named Use Case entry CVX is tagged against>` |
| Use Case 2 (name and link) | `<NEEDS: user>` |
| Use Case 3 (name and link) | `<NEEDS: user>` |

## ABOUT THIS DEPLOYMENT

| Field | Value | Source |
|---|---|---|
| Version | `latest` per the ZonBook package convention (see `build-info.xml`) — but the template's own instruction says "check under versioning for how versioning is numbered," implying a semver-style value (like CMS's `v0.2.6` or ADP's `v0.1.1`) may be expected here instead. `<NEEDS: user decision — "latest" (ZonBook build-track) vs a semver tag (would require first cutting a CVX release tag, which doesn't exist yet — CVX has no public-mirror releases, see Blockers)>` | |
| Released (MM/YYYY) | `<NEEDS: same blocker as Requested Publishing Date — CVX hasn't been released to the public mirror>` | |
| Author | AWS | Standard for AWS-authored guidances; matches CMS/ADP convention, no partner co-authorship evident in any CVX doc |
| Est. deployment time | `<NEEDS: no full-stack deployment-time figure exists anywhere in the IG>`. The only timing figure found is `deploy-the-guidance.adoc`'s "`agentcore deploy`... takes approximately three to five minutes on first deploy" — but that covers only the AgentCore Runtime deploy step, not the full prerequisite chain (CMS foundation + ADP foundation + CVX stacks). Do not use the 3-5 minute figure as the answer to this field; it would materially understate real deployment time. | `deploy-the-guidance.adoc` line 282 |
| Estimated cost (link to details) | `plan-your-deployment.adoc` §"Cost Model" has a real usage-based cost table (staging reference workload: ~30,000 text turns/month, ~3,000 voice sessions/month at ~8 min avg, ~21,000 KB retrieves/month) but `<NEEDS: a public URL — this content lives in the not-yet-published IG; also per the Solutions safety guardrail, actual $-figure estimates should point users to the AWS Pricing Calculator per this portfolio's own steering convention, not a hardcoded number>` | `plan-your-deployment.adoc` §"Cost Model", lines 125-162 |
| Implementation Guide PDF link | `<NEEDS: does not exist — build-info.xml has PDF commented out (`<!-- <pdf/> -->`), matching CMS/ADP's HTML-only convention; see "Build Output" note below>` | |
| Source code link | `<NEEDS: does not exist — CVX has no public GitHub mirror; see Blockers>` | |
| CloudFormation Template link | `<NEEDS: does not exist — CVX deploys via CDK + `agentcore deploy`, not a standalone CFN template a customer launches directly; even once public, this field may not apply the way it does for a pure-CFN solution>` | |
| Subscribe to RSS Feed link | `<NEEDS: generated automatically by the AWS Solutions publishing pipeline once the guidance is live — not something to fill manually>` | |

## DEPLOYMENT OPTIONS

| Field | Value |
|---|---|
| Launch in the AWS Console link | `<NEEDS: does not exist — same blocker as CloudFormation Template link>` |
| Deploy with an AWS IQ expert link | `<NEEDS: user — this is a business/partnership decision on whether AWS IQ expert deployment support is offered for this guidance>` |

## RELATED CONTENT (OPTIONAL)

| Field | Value |
|---|---|
| Product Video | `<NEEDS: user — optional field, no video exists>` |
| External Overview (One-Pager) | `<NEEDS: user — I noticed `~/Downloads/McLaren-Connected-Vehicle-One-Pager.docx` and `~/Downloads/McLaren-Connected-Vehicle-One-Pager.md` in your Downloads folder from an earlier context; if either is the intended one-pager for this guidance, confirm — I have not read their contents and won't assume they're the right document>` |
| Solution Spark for Public Sector | `<NEEDS: user — optional field>` |

---

## Blockers (read before attempting to submit)

Two structural gaps make several fields above un-fillable regardless of
research effort — these aren't things I failed to find, they're things
that don't exist yet:

1. **No public GitHub mirror.** Per the portfolio backlog's `CVX v0.1.0`
   row (status: Hold, owner: User), CVX has never been published to
   `aws-solutions-library-samples`. This blocks: Implementation Guide HTML
   URL, Source code link, CloudFormation Template link, Launch in AWS
   Console link, and any "Released" date. Per `CVX public repo migration`
   (P1, External) and `Publish-flow extract` (P1, PO) backlog rows, the
   publish mechanism itself doesn't exist for CVX yet either.
2. **Marketing Lead review not yet done.** The template's own pre-submission
   checklist requires the Overview and Benefits sections to be reviewed
   with an assigned Industry/Product Marketing Lead before submission. I
   drafted this content directly from the completed IG so there's something
   concrete to bring to that review, but the draft itself does not
   substitute for that review — do not submit without it.

**Recommendation**: this template is realistically not submittable until
(1) resolves — likely sequenced after `CVX v0.1.0`'s product-readiness call
and the publish-mechanism work. Treat this draft as prep material for that
future submission, not a document that's ready to go today.
