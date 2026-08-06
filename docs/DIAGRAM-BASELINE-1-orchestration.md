# CVX Reference Architecture Diagram 1 — Agentic Orchestration

**Story.** How a customer request gets handled: which surfaces receive the traffic,
which Lambda routes it, which runtime executes the agent, and how the synthesized
response gets streamed back on the same channel the customer came in on.

**Scope.** The request/response spine of CVX. Ingress (voice / chat / web+mobile),
per-turn tool dispatch via the AgentCore Runtime, triage classification, and response
back to the customer. Steps `[1] [1'] [2] [2'] [3] [5] [12] [13a] [13b] [14]`.

**What this diagram intentionally does NOT show.** The grounding-data mechanics
(Memory, KB, LKS, DynamoDB, Recalls Lambda, Centralized Data Repository) are represented
here as a single labeled off-diagram edge `grounding queries → Diagram 2`. The
escalation / warm-transfer path (Connect P0/P1/P2 queues + Tier-2 Human Agent + CVX
operational DynamoDB writes) is a single labeled off-diagram edge
`P0/P1/P2 → Diagram 3`. The full detail lives in those two files — do not attempt to
render it here.

**Numbered badges are stable across all three files.** Badge `[5]` means the same thing
in Diagram 1 as it does in Diagram 3 (`triage()` classification). Do not renumber.

---

## 1. Legend

See index at `docs/DIAGRAM-BASELINE.md § 1` for the shared legend (solid boxes /
dashed boundaries / arrow kinds / numbered badges / person icons / collapsed boxes).
This diagram uses solid boxes, one dashed CVX-account governance boundary, solid
arrows for reads and synchronous calls, and one dashed arrow for the async escalation
write on the off-diagram edge.

Two off-diagram edges appear in this diagram and are stylistically distinct:

- **Off-diagram reference edge** — thick solid arrow with a filled block label
  `→ Diagram N` at the head. Renders like a normal flow arrow but the head does not
  terminate at a box; instead it points at a labeled tag `→ Diagram N`.

---

## 2. Components on this diagram

| # | Name                               | AWS service                          | Role in this diagram                                                        |
| - | ---------------------------------- | ------------------------------------ | --------------------------------------------------------------------------- |
| A | **Customer**                       | Person                               | Voice caller (PSTN), chat user, or web/mobile app user.                     |
| 1 | **Amazon Connect**                 | Amazon Connect                       | Voice + chat channel ingress; tenant-branded contact flows. **This diagram renders only Connect's channel + tenant-flow aspects.** The escalation-queue / Customer Profiles / Agent Desktop widget sub-detail lives in Diagram 3. |
| 2 | **Amazon API Gateway (+ Cognito)** | API Gateway + Cognito                | Web / mobile REST ingress to `{vsaApiEndpoint}/assistant/chat`; Cognito JWT authorizer; persona inference from `cognito:groups` + `custom:role`. |
| 3 | **Supervisor Lambda (proxy)**      | AWS Lambda                           | Single proxy Lambda invoked by both Connect and API Gateway. Channel is an attribute on the session, not a code branch. Also the destination for the response fan-out. |
| 4 | **Bedrock AgentCore Runtime**      | Amazon Bedrock AgentCore Runtime     | Hosts the Strands supervisor agent on Claude Sonnet 4.6. Two variants: `bidi (voice, Nova 2 Sonic)` and `text (chat / web / mobile)`. Executes the tool loop. |
| 6 | **Triage Classifier Lambda**       | AWS Lambda                           | Deterministic P0/P1/P2/P3 rules engine over DTCs + telemetry + recalls + tenant policy overrides. `triage()` output routes downstream (P0/P1/P2 escalate → Diagram 3). |

**Rendering notes for box 1 (Amazon Connect) in this diagram.** Render the Connect box
with the channel-and-flow sub-list only:

- Voice channel (Nova 2 Sonic speech-to-speech)
- Chat channel (Connect Chat SDK)
- Tenant-branded contact flows (DNIS-resolved tenant ID)

The full six-bullet Connect list (adding escalation queues / Customer Profiles / Agent
Desktop widget) lives in Diagram 3. Rendering the same box twice with different
sub-lists across diagrams is intentional — each diagram shows the aspects relevant to
its story.

**Rendering notes for box 4 (AgentCore Runtime) in this diagram.** Same as the baseline
index: one main box with two labeled sub-notes `bidi (voice, Nova 2 Sonic)` and
`text (chat / web / mobile)`, and a small `Claude Sonnet 4.6` annotation inside the box.

## 2.1 Off-diagram reference boxes

These are not real components on Diagram 1 — they are labeled tags that mark where
flow steps leave this diagram and continue in another one.

| Tag              | Represents                                                                 | Points to                                       |
| ---------------- | -------------------------------------------------------------------------- | ----------------------------------------------- |
| **→ Diagram 2**  | Grounding queries: AgentCore Memory reads/writes, cross-account Bedrock KB `Retrieve`, cross-account Athena via Centralized Data Repository, CMS Redis LKS reads, CMS DynamoDB reads/writes, CMS Recalls Lambda invocations. | `DIAGRAM-BASELINE-2-data-foundation.md`         |
| **→ Diagram 3**  | Escalation / warm-transfer: `escalate()` writes to CVX operational DynamoDB, Connect `TransferContact` / `StartOutbound*` invocation, tier-2 queue routing, Customer Profiles pre-population, Agent Desktop widget rendering, Tier-2 Human Agent take-over. | `DIAGRAM-BASELINE-3-escalation.md`              |

Render each off-diagram tag as a small pill or chevron with the label `→ Diagram N` at
the head of the arrow. Do not draw a box for the destination diagram — the arrow just
points at the tag and stops.

---

## 3. Arrows and numbered flow steps on this diagram

Numbered badges are the same as the single-diagram baseline. Steps `[4a] [4b] [4c] [6]
[7] [8] [9]` (grounding) and `[10a] [10b] [11]` (escalation) are NOT drawn on this
diagram — they render in Diagrams 2 and 3 respectively.

### 3.1 Ingress (channel entry — how a session starts)

| Step | From                | To                                        | Kind  | Label / meaning                                                                                                 |
| ---- | ------------------- | ----------------------------------------- | ----- | --------------------------------------------------------------------------------------------------------------- |
| [1]  | Customer (A)        | Amazon Connect (1)                        | Solid | Voice call (PSTN) or Connect Chat message enters tenant's Connect instance. DNIS/contact attribute resolves tenant. |
| [2]  | Amazon Connect (1)  | Supervisor Lambda (3)                     | Solid | Tenant-branded contact flow invokes the `api-escalate` / chat Lambda integration with session metadata.         |
| [1'] | Customer (A)        | API Gateway (2)                           | Solid | Web / mobile app POSTs to `{vsaApiEndpoint}/assistant/chat`. Cognito authorizer validates JWT; persona inferred from `cognito:groups` + `custom:role`. |
| [2'] | API Gateway (2)     | Supervisor Lambda (3)                     | Solid | REST proxy forwards to the same supervisor Lambda that Connect uses. Channel is an attribute on the session, not a branch. |
| [3]  | Supervisor Lambda (3) | AgentCore Runtime (4)                   | Solid | Lambda invokes the AgentCore runtime — `vsa_supervisor_bidi_*` for voice, `vsa_supervisor_text_*` for chat / web / mobile. |

### 3.2 Session-init grounding (represented as ONE off-diagram edge on this diagram)

| Step        | From                  | To                       | Kind  | Label / meaning                                                                                                                    |
| ----------- | --------------------- | ------------------------ | ----- | ---------------------------------------------------------------------------------------------------------------------------------- |
| [4a/4b/4c]  | AgentCore Runtime (4) | **→ Diagram 2**          | Solid | `session init: grounding load` — fans out to CMS Redis LKS + Centralized Data Repository (Athena) + AgentCore Memory. Full detail in Diagram 2. |

Render as a single labeled arrow from box 4 pointing at the `→ Diagram 2` tag with the
badge cluster `[4]` on the arrow midpoint. Do not draw the three sub-arrows here — that
detail lives in Diagram 2.

### 3.3 Per-turn tool dispatch (partial on this diagram: `[5]` renders in full; `[6] [7] [8] [9]` are folded into the off-diagram edge to Diagram 2)

| Step | From                          | To                                  | Kind  | Label / meaning                                                                                                  |
| ---- | ----------------------------- | ----------------------------------- | ----- | ---------------------------------------------------------------------------------------------------------------- |
| [5]  | AgentCore Runtime (4)         | Triage Classifier Lambda (6)        | Solid | `triage()` — deterministic P0/P1/P2/P3 rules engine over DTCs + telemetry + recalls + tenant policy overrides.   |
| [6/7/8/9] | AgentCore Runtime (4)    | **→ Diagram 2**                     | Solid | `per-turn tool dispatch: grounding reads/writes` — AgentCore Memory reads `[6]`, Bedrock KB `Retrieve` `[7]`, CMS Recalls Lambda `[8]`, CMS DynamoDB write on `book()` `[9]`. Full detail in Diagram 2. |

The triage arrow `[5]` renders in full because Triage Classifier Lambda (box 6) lives on
this diagram. The Triage Classifier's OUTPUT (P0/P1/P2 → escalate) is what leaves for
Diagram 3.

### 3.4 Escalation / warm-transfer (represented as ONE off-diagram edge on this diagram)

| Step                  | From                          | To                       | Kind          | Label / meaning                                                                                                                                     |
| --------------------- | ----------------------------- | ------------------------ | ------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| [10a/10b/11]          | AgentCore Runtime (4) via Triage output | **→ Diagram 3**    | Solid + dashed | `escalate: P0/P1/P2 → tier-2 human` — `escalate()` writes to CVX operational DynamoDB (dashed) then Connect `TransferContact` / `StartOutbound*` (solid), Connect routes to tenant tier-2 queue with pre-populated Customer Profiles and Agent Desktop widget. Full detail in Diagram 3. |

Render as one labeled arrow from box 4 pointing at the `→ Diagram 3` tag with the badge
cluster `[10]` at the arrow midpoint. Optionally add a small annotation on the arrow:
`triggered when triage() returns P0/P1/P2 or on explicit customer request`.

### 3.5 Response back to the customer

| Step  | From                                 | To                          | Kind  | Label / meaning                                                                                                    |
| ----- | ------------------------------------ | --------------------------- | ----- | ------------------------------------------------------------------------------------------------------------------ |
| [12]  | AgentCore Runtime (4)                | Supervisor Lambda (3)       | Solid | Supervisor synthesizes grounded response from tool results (voice: streams tokens to Nova 2 Sonic; text: JSON).    |
| [13a] | Supervisor Lambda (3)                | Amazon Connect (1)          | Solid | Voice: Nova 2 Sonic streams synthesized audio back through Connect to the caller. Chat: text streamed via Connect Chat WebSocket. |
| [13b] | Supervisor Lambda (3)                | API Gateway (2)             | Solid | Web / mobile: JSON response with `text` + `triage` + `vin` + `citations` fields returned to the calling app.       |
| [14]  | Amazon Connect (1) / API Gateway (2) | Customer (A)                | Solid | End-to-end response delivered on the same channel the customer entered on.                                         |

---

## 4. ASCII layout sketch

Layout hint only — the illustrator owns the final visual. Adjust proportions for balance.

```
                                          ,---------------- CVX account -------------------------.
                                          |                                                      |
  +---------+  [1] voice/chat             |    +----------------------------+                    |
  |         |------------------------>    |    | [1] Amazon Connect         |                    |
  |Customer |                             |    |  · Voice (Nova 2 Sonic)    |                    |
  |  (A)    |  [1'] web/mobile            |    |  · Chat (Connect Chat SDK) |                    |
  |         |----------.                  |    |  · Tenant contact flows    |                    |
  +----+----+          |                  |    |  (escalation detail →      |                    |
       ^               |                  |    |   Diagram 3)               |                    |
       | [14] response |                  |    +-------------+--------------+                    |
       | (voice/chat   |                  |            [2] |    ^ [13a] response                 |
       |  or JSON)     |                  |                v    |                                |
       |               |                  |    +----------------+--------+                       |
       |               v                  |    |         [3]              |                      |
       |     +--------------+             |    |  Supervisor Lambda       |                      |
       |     |    [2]       |             |    |  (proxy + tool Lambdas)  |                      |
       |     |  API Gateway |------------>|    |                          |                      |
       |     |  (+ Cognito) |  [2']       |    +---+----------------------+                      |
       |     +------+-------+             |     [3]|      ^ [12] response                        |
       |         [13b]                    |        v      |                                      |
       +----------------------------------|    +----------+----------------+                     |
                                          |    |          [4]              |                    |
                                          |    | Bedrock AgentCore Runtime |                    |
                                          |    |  · bidi (voice / Sonic)   |                    |
                                          |    |  · text (chat / web)      |                    |
                                          |    |  · Strands supervisor     |                    |
                                          |    |    on Claude Sonnet 4.6   |                    |
                                          |    +-+---------+-----------+---+                    |
                                          |      |         |           |                        |
                                          |     [5]  [4] session      [6/7/8/9] per-turn        |
                                          |      v    init grounding   grounding                |
                                          |    +---------+                                      |
                                          |    |   [6]   |----------.  ,---------------.        |
                                          |    | Triage  |          |  |               |        |
                                          |    | Classi- |          '->|→ Diagram 2    |        |
                                          |    | fier    |             | (grounding    |        |
                                          |    +----+----+             |  data path)   |        |
                                          |         |                  '---------------'        |
                                          |         |  P0/P1/P2                                 |
                                          |         v  [10] escalate                            |
                                          |    ,---------------.                                |
                                          |    |→ Diagram 3    |                                |
                                          |    | (escalation / |                                |
                                          |    |  warm-transfer)|                               |
                                          |    '---------------'                                |
                                          '------------------------------------------------------'

                                                          (dashed = CVX account boundary)
```

**Layout intent.**

- **Left column**: external actor (Customer). Voice/chat enters through Connect; web/mobile enters through API Gateway.
- **Center-top**: Amazon Connect + API Gateway feeding a single Supervisor Lambda — the two-ingress-to-one-Lambda pattern that makes channel an attribute rather than a code branch is the key visual assertion of this diagram.
- **Center**: Supervisor Lambda feeds AgentCore Runtime (`[3]`); Runtime returns synthesized response back through Supervisor (`[12]`).
- **Center-bottom / right**: Triage Classifier as its own leaf off the Runtime. Its P0/P1/P2 output is what leaves for Diagram 3.
- **Two labeled off-diagram tags**: `→ Diagram 2` (grounding data path) and `→ Diagram 3` (escalation). Both are rendered as pills or chevrons at the arrow head, not as boxes.
- **Response fan-out on the left**: `[13a]` back through Connect, `[13b]` back through API Gateway, `[14]` end-to-end to Customer.

---

## 5. Notes for the illustrator

### 5.1 Icons

Same v20 AWS Architecture Icons as the index. Icons required for this diagram:

- Amazon Connect
- Amazon API Gateway (with Amazon Cognito rendered inline inside the API Gateway box)
- AWS Lambda (used twice on this diagram: Supervisor Lambda proxy + Triage Classifier)
- Amazon Bedrock AgentCore Runtime

### 5.2 Off-diagram tag styling

The two `→ Diagram N` tags are the load-bearing element of this diagram — they are the
reason the split works. Render them as:

- A pill or chevron shape (distinct from the AWS-service box shape) with light-gray
  border and light-gray fill
- Label text `→ Diagram N` in a subordinate font (e.g. italic or slightly smaller than
  box labels)
- A short one-line subtitle underneath (e.g. `grounding data path` or
  `escalation / warm-transfer`)

Do NOT render them as full boxes with AWS icons — they are references, not components.

### 5.3 What NOT to show on this diagram

Per the split, this diagram MUST NOT show:

- AgentCore Memory (box 5) — belongs on Diagram 2
- CMS Redis LKS, CMS DynamoDB, CMS Recalls Lambda (boxes 8, 9, 10) — belong on Diagram 2
- Bedrock Knowledge Base (box 11) — belongs on Diagram 2
- Centralized Data Repository (box 12) — belongs on Diagram 2
- CVX operational DynamoDB (box 7) — belongs on Diagram 3 (the `vsa-escalation-records` write is the escalation story)
- Tier-2 Human Agent (box B) — belongs on Diagram 3
- Amazon Connect's escalation-queue / Customer Profiles / Agent Desktop widget sub-list — belongs on Diagram 3

If any of the above appears on the final Diagram 1 artwork, it's a scope violation.

### 5.4 Non-goals (inherited from index)

See index `DIAGRAM-BASELINE.md § "Non-goals"` — none of Nova 2 Sonic as own box,
Polly/Transcribe fallback, WebSocket reasoning-feed API, individual tool Lambdas, KMS
keys / IAM roles / log groups, or IoT / MSK / Flink pipeline should appear on this
diagram.
