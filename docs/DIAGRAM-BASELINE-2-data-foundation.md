# CVX Reference Architecture Diagram 2 — Data Foundation & Composition

**Story.** Where CVX's intelligence comes from. The composition layer over CMS's
operational state (Redis LKS, DynamoDB, Recalls Lambda) and ADP's analytical foundation
(cross-account Bedrock KB + Centralized Data Repository), plus AgentCore Memory as the
per-session cache that binds the two. This is CVX's actual differentiator — the two
foundations fused into one grounded agentic surface at runtime.

**Scope.** Session-init grounding load `[4a] [4b] [4c]` and per-turn grounding
reads/writes `[6] [7] [8] [9]`. The full ADP cross-account boundary (dashed rectangle)
and the CMS foundation labeled boundary render here in full — the collapsed
`Centralized Data Repository (Amazon Athena + AWS Glue + Amazon S3, governed by AWS
Lake Formation)` box lives on this diagram exactly as specified in the baseline index.

**What this diagram intentionally does NOT show.** The full ingress chain
(Customer → Connect / API Gateway → Supervisor Lambda) is represented as a single
labeled off-diagram edge `per-turn request → Diagram 1`. The AgentCore Runtime (box 4)
is on this diagram, but only as the entry point at the edge — the runtime's tool-loop
mechanics (bidi vs text variants, Sonnet model annotation) render in full on Diagram 1;
here box 4 is rendered as a compact edge box, no bidi/text sub-list. The escalation
path is not touched by this diagram at all.

**Numbered badges are stable across all three files.** Badge `[7]` means the same thing
in Diagram 2 as it does in Diagrams 1 and 3 (cross-account Bedrock KB `Retrieve`). Do
not renumber.

---

## 1. Legend

See index at `docs/DIAGRAM-BASELINE.md § 1` for the shared legend. This diagram uses:

- **Solid boxes** for AWS services
- **Dashed rectangle (governance boundary)** for the ADP account / region — this is the
  cross-account authorization surface (Lake Formation v4 grants + Bedrock KB resource
  policy), a load-bearing element of the architecture
- **Labeled light-fill grouping boundary** (stylistically distinct from the dashed
  cross-account rectangle) for the CMS foundation — in the reference/staging topology
  CMS + CVX share an account and region; in production customer topologies they may be
  separate. The grouping label reads `CMS foundation — real-time operational state`.
- **Solid arrow →** for reads / synchronous calls
- **Dashed arrow ⇢** for writes / asynchronous emissions
- **Numbered badges `[n]`** identical to the single-diagram baseline

One off-diagram reference edge appears on this diagram:

- **Off-diagram reference edge** — a solid arrow entering the diagram from outside the
  CVX-account boundary, labeled `per-turn request → Diagram 1` at the tail. It
  terminates at box 4 (AgentCore Runtime).

---

## 2. Components on this diagram

| #  | Name                                          | AWS service                                        | Role in this diagram                                                                     |
| -- | --------------------------------------------- | -------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| 4  | **Bedrock AgentCore Runtime** *(edge)*        | Amazon Bedrock AgentCore Runtime                   | Entry point at the edge of this diagram. Fans out to CMS + ADP + Memory for grounding. **Do not re-draw its bidi/text sub-list on this diagram — see Diagram 1 for the runtime's own composition.** Render as a compact box with just the service name. |
| 5  | **AgentCore Memory (LTM)**                    | Amazon Bedrock AgentCore Memory                    | Long-term session store inside the CVX account, sitting immediately adjacent to box 4. Receives session-init grounding writes (`[4c]`), serves per-turn metadata-filtered reads (`[6]`). |
| 8  | **CMS Redis LKS**                             | Amazon ElastiCache (Redis)                         | Sub-ms telemetry-snapshot store: active DTCs, coolant temp, brake wear, tire pressure. Read once per session on init (`[4a]`). |
| 9  | **CMS DynamoDB**                              | Amazon DynamoDB                                    | `vehicles`, `trips`, `service-history` tables. Target of the `book()` write (`[9]`) that persists a STAR-JSON service ticket. |
| 10 | **CMS Recalls Integration Lambda**            | AWS Lambda                                         | NHTSA open-recall status per VIN. Invoked per turn (`[8]`); its output factors into `triage()` and the supervisor response. |
| 11 | **Bedrock Knowledge Base**                    | Amazon Bedrock Knowledge Base (backed by Amazon OpenSearch Serverless) | Cross-account `Retrieve` target for `knowledge()` and `parts_lookup()` per-turn calls (`[7]`). Filtered by `source_category` (dtc_guide / tsb_recall / service_policy / parts_catalog / owner_manual / charging_narrative). |
| 12 | **Centralized Data Repository** *(collapsed)* | Amazon Athena + AWS Glue + Amazon S3, governed by AWS Lake Formation | The user's collapsed-simplification box. Rendered as ONE physical box with the four services enumerated as parenthetical text inside the label. Target of `persona_loader` session-init Athena queries (`[4b]`). |

**Rendering notes for box 4 (edge role only on this diagram).** Render as a compact
box with just the service name and icon. Do NOT include:

- The `bidi (voice / Nova 2 Sonic)` and `text (chat / web / mobile)` sub-list (that
  detail is in Diagram 1)
- The `Claude Sonnet 4.6` model annotation (also in Diagram 1)
- The Strands supervisor mention

The compactness is intentional — this diagram's focus is downstream of box 4, not on
box 4 itself.

**Rendering notes for box 12 (collapsed Centralized Data Repository).** Per the user
directive from the baseline index — render ONE physical box, with the four underlying
services enumerated inside the box label as parenthetical text:

```
+---------------------------------------------------------------+
|  Centralized Data Repository                                  |
|  (Amazon Athena + AWS Glue + Amazon S3,                       |
|   governed by AWS Lake Formation)                             |
+---------------------------------------------------------------+
```

Use the Amazon Athena or AWS Lake Formation icon inside the box (illustrator's choice —
both are appropriate for a governed analytical data repository). A compact stack of
small service icons is also acceptable but MUST NOT be laid out as four separate boxes
with arrows between them.

**Rendering notes for the ADP dashed boundary.** Both box 11 (Bedrock Knowledge Base)
and box 12 (Centralized Data Repository) sit inside the SAME dashed rectangle labeled
`ADP account / region — cross-account authorization boundary`. This visually
communicates that the CVX agent's cross-account IAM (Lake Formation v4 grants +
Bedrock KB resource policy) applies to both grounding surfaces. Bedrock KB stays as its
own box inside the boundary because it is the actual API surface the agent calls
(`bedrock-agent-runtime:Retrieve`), not an ADP-internal.

**Rendering notes for the CMS foundation grouping.** A labeled light-fill grouping
(title `CMS foundation — real-time operational state`), visually distinct from the
dashed cross-account rectangle around ADP. In the reference/staging topology CMS + CVX
share an account and region; in production customer topologies they may be separated —
the labeled boundary works either way and does not imply cross-account authorization.

## 2.1 Off-diagram reference tag

| Tag                | Represents                                                                  | Points from                                     |
| ------------------ | --------------------------------------------------------------------------- | ----------------------------------------------- |
| **← Diagram 1**    | Per-turn request originating from Supervisor Lambda invocation `[3]`. The full ingress chain (Customer → Connect / API Gateway → Supervisor Lambda) is not drawn here — it renders in Diagram 1. | `DIAGRAM-BASELINE-1-orchestration.md`           |

Render as a small pill or chevron with the label `← Diagram 1` at the TAIL of the
arrow (the arrow enters this diagram from outside the CVX-account boundary and
terminates at box 4).

---

## 3. Arrows and numbered flow steps on this diagram

Numbered badges are the same as the single-diagram baseline. Steps `[1] [1'] [2] [2']
[3] [5] [12] [13a] [13b] [14]` (ingress + response) are NOT drawn on this diagram —
they render on Diagram 1. Steps `[10a] [10b] [11]` (escalation) are on Diagram 3.

### 3.0 Off-diagram entry (per-turn request from Diagram 1)

| Step | From              | To                    | Kind  | Label / meaning                                                                                          |
| ---- | ----------------- | --------------------- | ----- | -------------------------------------------------------------------------------------------------------- |
| [3]  | **← Diagram 1**   | AgentCore Runtime (4) | Solid | Supervisor Lambda invokes the AgentCore runtime. Rendered on this diagram as the entry edge from the `← Diagram 1` tag. |

The `[3]` badge and description are identical to Diagram 1's step `[3]` — the same
flow-step just crosses the diagram boundary here. Do not renumber.

### 3.1 Session initialization (once per session — the cross-account grounding load — CVX's differentiator)

| Step | From                          | To                                  | Kind   | Label / meaning                                                                                                       |
| ---- | ----------------------------- | ----------------------------------- | ------ | --------------------------------------------------------------------------------------------------------------------- |
| [4a] | AgentCore Runtime (4)         | CMS Redis LKS (8)                   | Solid  | Sub-ms telemetry-snapshot read: active DTCs, coolant temp, brake wear, tire pressure. Snapshot held in session context. |
| [4b] | AgentCore Runtime (4)         | Centralized Data Repository (12)    | Solid  | `persona_loader` issues partition-scoped Athena queries against the ADP Glue catalog (Lake Formation v4 grant). Crosses the ADP cross-account boundary. |
| [4c] | AgentCore Runtime (4)         | AgentCore Memory (5)                | Dashed | Query result rows are serialized and written to Memory as metadata-tagged records (`persona_id`, `vehicle_id`, `data_source`, `tenant_id`). |

Steps `[4a]`, `[4b]`, `[4c]` render as one badge cluster labeled `[4] session init`. The
illustrator can draw them as three separate short arrows radiating from box 4 or as a
single fanned arrow with three heads — either is fine. **This is the primary visual
assertion of Diagram 2.** The three-way fan-out from AgentCore Runtime is what "data
foundation and composition" MEANS — CMS operational cache + ADP analytical repository
fused into Memory in a single session-init cycle.

### 3.2 Per-turn tool dispatch (many per session — grounding reads and one write)

| Step | From                          | To                                  | Kind  | Label / meaning                                                                                                  |
| ---- | ----------------------------- | ----------------------------------- | ----- | ---------------------------------------------------------------------------------------------------------------- |
| [6]  | AgentCore Runtime (4)         | AgentCore Memory (5)                | Solid | `vehicle_identity()`, `service_history()`, `charging_history()`, `customer_profile()`, `interaction_history()`, `ota_history()` — metadata-filtered LTM reads. |
| [7]  | AgentCore Runtime (4)         | Bedrock Knowledge Base (11)         | Solid | `knowledge()`, `parts_lookup()` — cross-account `bedrock-agent-runtime:Retrieve` with `source_category` filter (dtc_guide / tsb_recall / service_policy / parts_catalog / owner_manual / charging_narrative). |
| [8]  | AgentCore Runtime (4)         | CMS Recalls Integration Lambda (10) | Solid | NHTSA open-recall status per VIN — factored into `triage()` and surfaced in supervisor response.                 |
| [9]  | AgentCore Runtime (4)         | CMS DynamoDB (9)                    | Dashed | `book()` writes the STAR-JSON service ticket to the CMS `service-history` table.                                |

**Note on `[4b]` / `[7]` (ADP cross-account boundary).** Both arrows are rendered read-only here (Athena query + KB `Retrieve`); the same cross-account authorization surface supports scoped *write* grants when CVX emits state-change facts (e.g. `VehicleOwnershipTransfer`) back into ADP data products — see IG prose in `the-customer-journey.adoc` for the write-direction pattern. No dedicated write arrow is drawn on this diagram because no such emission exists in the current reference implementation.

**Note on `[9]` (STAR-JSON service ticket).** The STAR-shaped `ServiceRepairOrderAdd` payload written by `book()` is one instance of the STAR-JSON schema family CVX uses for cross-stage record emissions generally (subscription-activation, ownership-transfer, service completion) — see IG prose in `building-on-cms-and-adp.adoc` for the cross-stage schema convention.

### 3.3 Triage `[5]` and escalation `[10a] [10b] [11]` — NOT on this diagram

Step `[5]` (`triage()` call) fires on Diagram 1 (Triage Classifier Lambda is on that
diagram). Steps `[10a] [10b] [11]` (escalation) fire on Diagram 3. Neither is drawn on
Diagram 2 — no dangling triage or escalation arrows should appear on the final artwork.

---

## 4. ASCII layout sketch

Layout hint only — the illustrator owns the final visual. Adjust proportions for balance.

```
                                          ,---------------- CVX account ----------------------------.
                                          |                                                         |
                                          |    ,--- ← Diagram 1 ---.                                |
                                          |    | per-turn request  |                                |
                                          |    | (from Supervisor  |                                |
                                          |    |  Lambda [3])      |                                |
                                          |    '---------+---------'                                |
                                          |              |                                          |
                                          |              v  [3]                                     |
                                          |    +--------------------+       +----------------+      |
                                          |    |         [4]        |       |      [5]       |     |
                                          |    | Bedrock AgentCore  |------>| AgentCore      |      |
                                          |    | Runtime  (edge)    | [4c]  | Memory (LTM)   |      |
                                          |    +-+--+--+--+--+------+ [6]   +----------------+      |
                                          |      |  |  |  |  |                                      |
                                          |     [4a][4b][6-per-turn][7][8][9]                       |
                                          |      |  |  |  |  |                                      |
                                          '------|--|--|--|--|--------------------------------------'
                                                 |  |  |  |  |
                                                 v  |  v  |  v
              ,-- CMS foundation — real-time operational state --. ,-- ADP account / region -----------.
              | (labeled light-fill grouping)                    | | (dashed cross-account boundary)   |
              |                                                  | |                                   |
              |  +-----------+  +-------------+  +--------+      | |  +----------------------+  <----- [7]
              |  |    [8]    |  |     [9]     |  |  [10]  |      | |  |        [11]          |  Retrieve
              |  | CMS Redis |  | CMS         |  | CMS    |      | |  |  Bedrock Knowledge   |  x-account
              |  |    LKS    |  | DynamoDB    |  | Recalls|      | |  |         Base         |
              |  |           |  | (vehicles / |  | Integ. |      | |  +----------------------+
              |  |           |  | trips /     |  | Lambda |      | |
              |  +-----+-----+  | service-hist)|  +---+---+      | |  +---------------------------------+
              |        ^        +------+------+     ^            | |  |             [12]                |
              |        | [4a]          ^ [9] book   | [8] recall | |  |  Centralized Data Repository    |
              |        |               | (write)    |            | |  |  (Amazon Athena + AWS Glue      |<---- [4b]
              |        |               |            |            | |  |   + Amazon S3, governed by      |    x-account
              |        |               |            |            | |  |   AWS Lake Formation)           |
              |        |               |            |            | |  +---------------------------------+
              '--------+---------------+------------+------------' '-----------------------------------'
                                                                        (dashed = ADP cross-account
                                                                         authorization boundary)
```

**Layout intent.**

- **Top**: the `← Diagram 1` off-diagram tag flows into box 4 (AgentCore Runtime, edge
  role). Adjacent to box 4 is box 5 (AgentCore Memory) inside the same CVX-account
  boundary. The `[4c]` write and `[6]` read arrows connect box 4 to box 5 with short
  edges — visually reinforces "Memory sits right next to the Runtime."
- **Middle**: box 4 fans out downward — three arrows in the session-init cluster
  (`[4a] [4b] [4c]`) plus four per-turn arrows (`[6] [7] [8] [9]`). Some of these
  targets overlap (`[6]` and `[4c]` both hit box 5) — the illustrator should draw them
  as separate arrows in different visual weights, not merged.
- **Bottom-left**: CMS foundation labeled grouping. Three boxes (LKS, DynamoDB, Recalls
  Lambda). The `[4a]` arrow enters LKS from box 4; the `[9]` dashed write arrow enters
  DynamoDB; the `[8]` arrow enters Recalls Lambda.
- **Bottom-right**: ADP account / region dashed cross-account rectangle. Two boxes
  (Bedrock KB, Centralized Data Repository). The `[7]` arrow enters KB; the `[4b]`
  arrow enters Centralized Data Repository — both cross the dashed boundary from left
  (CVX-account) to right (ADP-account) to visually reinforce cross-account access.

**Visual assertion.** Box 4's fan-out is the visual centerpiece — three arrows in the
session-init cluster (one to CMS, one to ADP, one to Memory) plus four per-turn arrows
should read as "AgentCore Runtime IS the composition layer; CVX composes CMS + ADP + a
per-session cache."

---

## 5. Notes for the illustrator

### 5.1 Icons

Same v20 AWS Architecture Icons as the index. Icons required for this diagram:

- Amazon Bedrock AgentCore Runtime (compact — no bidi/text/Sonnet sub-detail here)
- Amazon Bedrock AgentCore Memory
- Amazon ElastiCache (Redis)
- Amazon DynamoDB (for CMS DDB)
- AWS Lambda (for CMS Recalls Integration Lambda)
- Amazon Bedrock Knowledge Base
- Amazon Athena, AWS Glue, Amazon S3, AWS Lake Formation — all four rendered INSIDE the
  single Centralized Data Repository box (either as parenthetical text or as a compact
  icon stack), NOT as separate boxes

### 5.2 Boundary styling

Two visually distinct boundary treatments:

- **CVX account** — outer dashed rectangle, light-gray stroke, label
  `CVX account / region` in top-left. Encloses box 4 (edge) + box 5 (Memory) + the
  `← Diagram 1` tag.
- **CMS foundation** — labeled light-fill grouping (title `CMS foundation — real-time
  operational state`) around boxes 8, 9, 10. This is a grouping, NOT a governance
  boundary — style differently from the dashed boundaries (e.g. subtle background fill,
  rounded corners, no dashed stroke).
- **ADP account** — dashed rectangle, light-gray stroke, label `ADP account / region —
  cross-account authorization boundary` in top-left. Encloses boxes 11 and 12. Same
  stylistic treatment as the CVX-account boundary. The dashed rectangle is
  load-bearing — it visually asserts the cross-account IAM surface (Lake Formation v4
  grants + Bedrock KB resource policy).

### 5.3 Off-diagram tag styling

Same as Diagram 1's convention:

- Pill or chevron shape (distinct from AWS-service box)
- Light-gray border and fill
- Label `← Diagram 1` with subtitle `per-turn request (from Supervisor Lambda [3])`

Note the arrow direction: on Diagram 1 the off-diagram tag points AWAY (`→ Diagram 2`),
on Diagram 2 the equivalent tag points TOWARD box 4 (`← Diagram 1`). Same flow-step,
same badge `[3]`, opposite visual direction on the two diagrams — this is by design.

### 5.4 What NOT to show on this diagram

Per the split, this diagram MUST NOT show:

- Customer (box A) — belongs on Diagram 1
- Amazon Connect (box 1) — belongs on Diagrams 1 and 3
- API Gateway (box 2) — belongs on Diagram 1
- Supervisor Lambda (box 3) — belongs on Diagram 1
- Triage Classifier Lambda (box 6 as a numeric ID — NB: same numeric ID also refers to
  step `[6]` per-turn Memory read; do not conflate) — belongs on Diagram 1
- CVX operational DynamoDB (box 7 — `vsa-escalation-records` write) — belongs on
  Diagram 3
- Tier-2 Human Agent (box B) — belongs on Diagram 3
- Nova 2 Sonic, Claude Sonnet 4.6 model annotations — belong on Diagram 1's box 4
- The full ingress arrow chain `[1] [1'] [2] [2'] [3]` — represented as the single
  `← Diagram 1` tag only
- The response chain `[12] [13a] [13b] [14]` — belongs on Diagram 1
- The escalation chain `[10a] [10b] [11]` — belongs on Diagram 3

### 5.5 Non-goals (inherited from index)

See index `DIAGRAM-BASELINE.md § "Non-goals"` — none of Nova 2 Sonic as own box,
Polly/Transcribe fallback, WebSocket reasoning-feed API, individual tool Lambdas
broken out, KMS keys / IAM roles / log groups, IoT / MSK / Flink telemetry pipeline, or
individual Athena / Glue / S3 / Lake Formation icons as separate boxes should appear on
this diagram. The Athena/Glue/S3/Lake Formation collapse into a single box (box 12) is
the load-bearing user directive for this diagram.
