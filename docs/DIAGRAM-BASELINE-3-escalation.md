# CVX Reference Architecture Diagram 3 — Escalation & Human Hand-off

**Story.** The safety-invariant path from an autonomous supervisor to a Tier-2 Human
Agent. What Amazon Connect actually DOES beyond channel ingress — its escalation
queues, Customer Profiles pre-population, and Agent Desktop widget that renders
transcript + triage + vehicle state BEFORE the human agent answers. This is CVX's
warm-transfer pattern: the P0 (Stop Driving) safety invariant that the LLM cannot
downgrade, plus the P1/P2 policy-exception and explicit-request paths.

**Scope.** The escalation triangle: AgentCore Runtime `escalate()` tool → CVX
operational DynamoDB (write) + Amazon Connect (transfer) → Tier-2 Human Agent. Steps
`[10a] [10b] [11]`. The full Amazon Connect box renders here with its escalation-side
sub-list (escalation queues + Customer Profiles + Agent Desktop widget); the
channel/tenant-flow sub-list rendered on Diagram 1 is not repeated here.

**What this diagram intentionally does NOT show.** The full ingress + response chain
(Customer, API Gateway, Supervisor Lambda, per-turn tool dispatch) lives on Diagram 1.
The grounding-data chain (Memory, KB, LKS, DynamoDB, Recalls Lambda, Centralized Data
Repository) lives on Diagram 2. On this diagram AgentCore Runtime is a compact edge
box, and the trigger is represented as a labeled entry from the Triage Classifier's
output on Diagram 1.

**Numbered badges are stable across all three files.** Badge `[11]` means the same
thing on Diagram 3 as it does on Diagrams 1 and 2 (Connect routing to Tier-2 Human
Agent with pre-populated context). Do not renumber.

**Escalation is triggered when.** `triage()` returns **P0 (Stop Driving)** — the safety
invariant is a hard escalation the LLM cannot downgrade — OR by a P1/P2 policy
exception OR by an explicit customer request. All three paths converge on the same
`escalate()` tool invocation shown on this diagram.

---

## 1. Legend

See index at `docs/DIAGRAM-BASELINE.md § 1` for the shared legend. This diagram uses:

- **Solid boxes** for AWS services
- **Person icon** for the Tier-2 Human Agent actor
- **Dashed rectangle** for the CVX-account governance boundary
- **Solid arrow →** for reads / synchronous calls (including the Connect
  `TransferContact` invocation and the routing to the tier-2 queue)
- **Dashed arrow ⇢** for the `escalate()` write to CVX operational DynamoDB
- **Numbered badges `[n]`** identical to the single-diagram baseline

One off-diagram reference edge appears on this diagram:

- **Off-diagram reference edge** — a solid arrow entering the diagram from outside the
  CVX-account boundary, labeled `triage() returns P0/P1/P2 → Diagram 1 [5]` at the
  tail. It terminates at box 4 (AgentCore Runtime — the tool caller for `escalate()`).

---

## 2. Components on this diagram

| # | Name                                   | AWS service                         | Role in this diagram                                                                                              |
| - | -------------------------------------- | ----------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| 4 | **Bedrock AgentCore Runtime** *(edge)* | Amazon Bedrock AgentCore Runtime    | Compact edge box. Represents the caller of the `escalate()` tool. **Do not re-draw the bidi/text sub-list or the Sonnet model annotation on this diagram — those live in Diagram 1.** |
| 1 | **Amazon Connect**                     | Amazon Connect                      | This diagram renders Connect's escalation-side aspects only: escalation queues, Customer Profiles, Agent Desktop widget. The channel + tenant-branded-flow sub-list is on Diagram 1. |
| 7 | **CVX operational DynamoDB**           | Amazon DynamoDB                     | The `vsa-escalation-records` + `vsa-tenant-config` tables. Target of the `escalate()` write (transcript + triage output + draft STAR ticket). `vsa-tenant-config` provides the P0/P1/P2 queue ARN per tenant. |
| B | **Tier-2 Human Agent**                 | Person                              | Receives the warm-transferred session in the Amazon Connect Agent Desktop with pre-populated Customer Profiles (VehicleProfile + ServiceRecord) and the Custom Views widget rendering transcript + triage + vehicle state. |

**Rendering notes for box 1 (Amazon Connect) on this diagram.** Render the Connect box
with the escalation-side sub-list only:

- Escalation queues (P0 safety / P1 service-now / P2 service-soon)
- Customer Profiles (VehicleProfile + ServiceRecord objects)
- Agent Desktop Custom Views widget (tier-2 context panel)

The channel + tenant-branded-contact-flow sub-list (Voice, Chat, Tenant contact flows)
renders on Diagram 1's Connect box. Rendering the same box with different sub-lists
across diagrams is intentional — each diagram surfaces the aspects relevant to its
story.

**Rendering notes for box 4 (edge role only on this diagram).** Compact box, service
name and icon only. Do NOT include the bidi/text sub-list or the Sonnet model
annotation — those live on Diagram 1.

**Rendering notes for box 7 (CVX operational DynamoDB).** Same visual as the single-
diagram baseline, but on this diagram the box's role is specifically the
`vsa-escalation-records` write target and the `vsa-tenant-config` queue-ARN-lookup
source. The `triage-decisions` table (also in this DDB) is not exercised on this
diagram and can be omitted from the sub-label.

**Rendering notes for box B (Tier-2 Human Agent).** Standard AWS person icon. Position
to the right or below the Connect box, connected by the `[11]` warm-transfer arrow.

## 2.1 Off-diagram reference tag

| Tag                                             | Represents                                                                                              | Points from                                     |
| ----------------------------------------------- | ------------------------------------------------------------------------------------------------------- | ----------------------------------------------- |
| **← Diagram 1 [5] triage() returns P0/P1/P2**   | Triage Classifier output on Diagram 1's step `[5]` returned P0 / P1 / P2 — the trigger for the escalate() tool call. | `DIAGRAM-BASELINE-1-orchestration.md`           |

Render as a pill or chevron with light-gray border and fill. Label reads
`← Diagram 1 [5] triage() returns P0/P1/P2` and terminates at box 4 (the tool caller).

Alternatively, the illustrator may add a short one-line annotation on the arrow itself:
`or explicit customer request` — capturing the two non-triage escalation triggers
(customer-initiated + P1/P2 policy exception).

---

## 3. Arrows and numbered flow steps on this diagram

Numbered badges are the same as the single-diagram baseline. Only three arrows fire on
this diagram — `[10a] [10b] [11]`. Steps `[1] [1'] [2] [2'] [3] [5] [12] [13a] [13b]
[14]` (ingress + response) fire on Diagram 1. Steps `[4a] [4b] [4c] [6] [7] [8] [9]`
(grounding) fire on Diagram 2.

### 3.0 Off-diagram entry (P0/P1/P2 triage output from Diagram 1)

| Step  | From              | To                    | Kind  | Label / meaning                                                                                          |
| ----- | ----------------- | --------------------- | ----- | -------------------------------------------------------------------------------------------------------- |
| (via [5]) | **← Diagram 1**   | AgentCore Runtime (4) | Solid | Triage Classifier returned P0 / P1 / P2 (or explicit customer request). Runtime invokes `escalate()` tool. Rendered on this diagram as the entry edge from the `← Diagram 1 [5]` tag. |

No new badge is minted for this entry — the trigger *is* Diagram 1's `[5]` output. The
tag references it explicitly.

### 3.1 Escalation / warm-transfer

| Step  | From                          | To                                        | Kind   | Label / meaning                                                                                                            |
| ----- | ----------------------------- | ----------------------------------------- | ------ | -------------------------------------------------------------------------------------------------------------------------- |
| [10a] | AgentCore Runtime (4)         | CVX operational DynamoDB (7)              | Dashed | `escalate()` writes the escalation record to `vsa-escalation-records` with transcript + triage output + draft STAR ticket. |
| [10b] | AgentCore Runtime (4)         | Amazon Connect (1)                        | Solid  | `escalate()` invokes Connect `TransferContact` (live call) or `StartOutboundVoiceContact` / `StartChatContact` (mobile/web escalations). Tenant P0/P1/P2 queue ARN read from `vsa-tenant-config`. |
| [11]  | Amazon Connect (1)            | Tier-2 Human Agent (B)                    | Solid  | Connect routes to the tenant's tier-2 queue. Customer Profiles pre-populates the `VehicleProfile` + `ServiceRecord`; the Custom Views widget renders transcript + triage + vehicle state on the agent desktop **before** the agent answers. |

Steps `[10a]` and `[10b]` render as one badge cluster labeled `[10] escalate` — they
fire together from the same tool invocation. The illustrator can draw them as two
separate arrows from box 4 (one dashed to DDB, one solid to Connect) with badges `[10a]`
and `[10b]` respectively, or as a single fanned arrow with two heads sharing badge `[10]`
— either is fine, matching the single-diagram baseline's convention.

Step `[11]` is a distinct arrow inside Connect's routing surface (Connect → tier-2
queue → human agent). The tier-2 queue itself is not a separate box — it's rendered as
part of Connect's sub-list (`P0/P1/P2 escalation queues`).

### 3.2 What's NOT on this diagram

- The full ingress `[1] [1'] [2] [2']` and Supervisor invocation `[3]` — Diagram 1
- The response chain `[12] [13a] [13b] [14]` back to Customer — Diagram 1
- The triage call `[5]` itself (only its OUTPUT is referenced via the off-diagram tag) — Diagram 1
- All grounding steps `[4a] [4b] [4c] [6] [7] [8] [9]` — Diagram 2

---

## 4. ASCII layout sketch

Layout hint only — the illustrator owns the final visual. This is the smallest of the
three diagrams and should read as a tight triangle.

```
                                          ,------------------ CVX account -------------------------.
                                          |                                                        |
                                          |   ,--- ← Diagram 1 [5] ---.                            |
                                          |   | triage() returns      |                            |
                                          |   | P0 / P1 / P2          |                            |
                                          |   | (or explicit customer |                            |
                                          |   |  request)             |                            |
                                          |   '----------+------------'                            |
                                          |              |                                          |
                                          |              v                                          |
                                          |    +---------------------+                              |
                                          |    |         [4]         |                              |
                                          |    | Bedrock AgentCore   |                              |
                                          |    | Runtime  (edge)     |                              |
                                          |    +----+-------------+--+                              |
                                          |         |             |                                 |
                                          |         | [10a]       | [10b] escalate                  |
                                          |         | escalate    | (Connect                        |
                                          |         | (write)     |  TransferContact /              |
                                          |         v             |  StartOutbound*)                |
                                          |    +---------+        v                                 |
                                          |    |   [7]   |    +----------------------------+       |
                                          |    | CVX ops |    |          [1]               |       |
                                          |    | DynamoDB|    | Amazon Connect             |       |
                                          |    | (vsa-   |    |  · P0/P1/P2 escalation qs  |       |
                                          |    |  escala-|    |  · Customer Profiles       |       |
                                          |    |  tion-  |    |    (VehicleProfile /       |       |
                                          |    |  records|    |     ServiceRecord)         |       |
                                          |    |  +      |    |  · Agent Desktop widget    |       |
                                          |    |  vsa-   |    |    (transcript + triage +  |       |
                                          |    |  tenant-|    |     vehicle state)         |       |
                                          |    |  config)|    +--------------+-------------+       |
                                          |    +---------+                   |                     |
                                          |                                  | [11] warm transfer   |
                                          |                                  |                     |
                                          |                                  v                     |
                                          |                             +----------+                |
                                          |                             |    (B)   |                |
                                          |                             | Tier-2   |                |
                                          |                             | Human    |                |
                                          |                             | Agent    |                |
                                          |                             +----------+                |
                                          |                                                         |
                                          '---------------------------------------------------------'
                                                          (dashed = CVX account boundary)
```

**Layout intent.**

- **Top**: `← Diagram 1 [5]` off-diagram tag → box 4 (compact AgentCore Runtime).
- **Middle**: box 4 fans out into a two-arrow escalation cluster — `[10a]` dashed left
  to CVX operational DDB, `[10b]` solid right to Connect. These are the `escalate()`
  tool's two side effects.
- **Bottom-left**: CVX operational DynamoDB, with an internal sub-label enumerating the
  two tables involved (`vsa-escalation-records` + `vsa-tenant-config`). The `[10a]`
  arrow head terminates here.
- **Center-right**: Amazon Connect (escalation-side sub-list). The `[10b]` arrow head
  terminates here.
- **Bottom-right**: Tier-2 Human Agent person icon. `[11]` solid arrow from Connect to
  the human agent.
- **Visual assertion**: the triangle box4 → DDB → Connect → Human is the entire story.
  The diagram should read as small, tight, and complete — this is the "P0 safety
  invariant terminates in a human" story, and it should be readable in five seconds.

---

## 5. Notes for the illustrator

### 5.1 Icons

Same v20 AWS Architecture Icons as the index. Icons required for this diagram:

- Amazon Bedrock AgentCore Runtime (compact — no bidi/text/Sonnet sub-detail here)
- Amazon Connect (with escalation-side sub-list rendered inside the box)
- Amazon DynamoDB (for CVX operational DDB)
- Person icon for Tier-2 Human Agent

### 5.2 Boundary styling

Single dashed rectangle for `CVX account / region`. Encloses box 4 (edge) + box 7
(operational DDB) + box 1 (Connect) + box B (Tier-2 Human Agent) + the
`← Diagram 1 [5]` tag. This is the same CVX-account boundary that appears on Diagrams
1 and 2 — stylistically identical.

### 5.3 Off-diagram tag styling

Same convention as Diagrams 1 and 2:

- Pill or chevron shape (distinct from AWS-service box)
- Light-gray border and fill
- Two-line label:
  ```
  ← Diagram 1 [5]
  triage() returns P0/P1/P2 (or explicit customer request)
  ```
- Terminates at box 4 with an incoming solid arrow

### 5.4 Sub-list rendering inside the Connect box

The Connect box on this diagram gets a 3-bullet sub-list:

- Escalation queues (P0 safety / P1 service-now / P2 service-soon)
- Customer Profiles (VehicleProfile + ServiceRecord objects)
- Agent Desktop Custom Views widget (tier-2 context panel)

Keep bullets short — one line each. The Customer Profiles bullet is the "pre-populate
context before the agent answers" story; the Agent Desktop widget bullet is the "render
transcript + triage + vehicle state" story. Together they are the reason warm-transfer
matters: the Tier-2 agent takes over a hot session with all context loaded, not a cold
handoff.

### 5.5 Sub-list rendering inside the CVX operational DDB box

Two tables surfaced on this diagram:

- `vsa-escalation-records` (target of the `[10a]` write)
- `vsa-tenant-config` (source of the P0/P1/P2 queue ARN read by `escalate()`)

The `triage-decisions` table lives in the same DDB but is not exercised on this
diagram; omit it from the sub-label.

### 5.6 What NOT to show on this diagram

Per the split, this diagram MUST NOT show:

- Customer (box A) — belongs on Diagram 1
- API Gateway (box 2) — belongs on Diagram 1
- Supervisor Lambda (box 3) — belongs on Diagram 1
- Triage Classifier Lambda (box 6) — belongs on Diagram 1 (its OUTPUT is referenced
  via the off-diagram tag only)
- AgentCore Memory (box 5) — belongs on Diagram 2
- CMS Redis LKS, CMS DynamoDB, CMS Recalls Lambda (boxes 8, 9, 10) — belong on Diagram 2
- Bedrock Knowledge Base (box 11) — belongs on Diagram 2
- Centralized Data Repository (box 12) — belongs on Diagram 2
- ADP account / region dashed cross-account boundary — belongs on Diagram 2
- CMS foundation labeled grouping — belongs on Diagram 2
- Amazon Connect's channel + tenant-branded-contact-flow sub-list — belongs on Diagram 1
- Nova 2 Sonic, Claude Sonnet 4.6 model annotations — belong on Diagram 1's box 4

### 5.7 Non-goals (inherited from index)

See index `DIAGRAM-BASELINE.md § "Non-goals"` — none of Nova 2 Sonic as own box,
Polly/Transcribe fallback, WebSocket reasoning-feed API, individual tool Lambdas broken
out, KMS keys / IAM roles / log groups, IoT / MSK / Flink pipeline should appear on
this diagram.

The tier-2 queue itself is NOT a separate box — it is a bullet inside the Connect box.
Same policy as the baseline index: sub-capabilities inside a single AWS service render
as internal bullets, not separate boxes.
