# FI Fleet Health Contract — Stage D

**Version**: 1.0 (Stage D — spec `2026-10-05-fi-fleet-health-stage-d`)
**Spec decisions**: D8, D9, D10, D11, D12, D13, D14, D16
**Relates to**: [`docs/fi-action-queue-contract.md`](fi-action-queue-contract.md) (§§ 1, 4, 6, 10,
10a, 12 updated to v2.7)
**OpenAPI**: [`docs/api/avx-findings-actions.openapi.yaml`](api/avx-findings-actions.openapi.yaml)

This document covers the three Finding kinds introduced in Stage D:
- `fleet.health.service` (§ 1) — booking item and grounding item, one per (vehicle, attempt)
- `quality.investigation` (§ 2) — one per investigation; population-scoped
- `quality.issue` (§ 3) — one per thread record; population-scoped; appears after a decision

It is a companion to [`docs/fi-action-queue-contract.md`](fi-action-queue-contract.md), which
governs the queue mechanics, approve/decline routes, guardrails and dispatch. Everything in that
document applies here; this document records the fields and rules specific to these three kinds.

Guardrail G5, which governs the three engineering action kinds, is described in § 5 and in the
v2.7 update to [`docs/fi-action-queue-contract.md`](fi-action-queue-contract.md) §§ 6 and 10.

The fault form of `ground_vehicle` — its `fault_codes` server-set parameter, its clearance rule,
and the `fault_grounding_enabled` rollout flag — is described in § 4. Stage C's recall form
(`recall_id`) is unchanged and documented in
[`docs/fi-recall-warranty-contract.md`](fi-recall-warranty-contract.md) § 3.

---

## 1. `fleet.health.service` — Fleet-Health Findings

### 1.1 Identity and scope

- `agent_id: agent-fi-fleet-health`, `component: service`
- `finding_kind: fleet.health.service`
- VIN-scoped. The writer projects the vehicle's `fleet_id`; a vehicle with no resolved fleet is
  in no queue (contract § 7, fail closed). Both a booking item and a grounding item for the same
  vehicle share the same VIN scope.

### 1.2 `subject_key` forms

```
service/<attempt>           — the booking item
ground/<attempt>            — the grounding item
service_exhausted/<n>        — the "manual follow-up" informational item (after 5 booking attempts in an episode; <n> is the attempt that triggered exhaustion)
ground_exhausted/<n>        — the "manual follow-up" informational item (after 5 grounding attempts in an episode; <n> is the attempt that triggered exhaustion)
```

| Segment | Values |
|---|---|
| First segment | `service`, `ground`, `service_exhausted`, or `ground_exhausted` |
| `<attempt>` | Integer ≥ 1, starting at 1 on the first run for this vehicle/subject |

A vehicle with an open `p0` fault and `fault_grounding_enabled: true` produces two items:
`service/1` and `ground/1`. The `subject_key` joins the `finding_key` hash (contract § 1.1).
Each attempt is a new Finding with a fresh `pending` item; Stage A's rule of one decision per
Finding is unchanged.

The `service_exhausted/1` and `ground_exhausted/1` items are written after 5 failed booking or
grounding attempts (see § 6). CMS groups both exhausted forms under the same vehicle card as
the base `service` and `ground` segments respectively, using the `subject_key` prefix.
They carry no `recommended_action` and are informational only.

### 1.3 Severity and items — band table

The band table is deterministic configuration (spec D9). The model cannot change severity or
grounding. The most severe need decides the booking item's severity.

| Most severe need present | Severity | Items produced |
|---|---|---|
| Open fault whose code's band is `p0` (stop-driving) | `p0` | booking item AND grounding item (when `fault_grounding_enabled` is `true` and vehicle is not already grounded) |
| Open fault in band `attention`, or an Agent 2 finding at `attention` | `attention` | booking item only |
| Open fault in band `soon`, an Agent 2 finding at `soon`, or a PM task `overdue` | `soon` | booking item only |
| PM task `due` | `routine` | booking item only |

Additional rules:
- No grounding item when `fault_grounding_enabled` is `false` (rollout flag
  `FI_FAULT_GROUNDING_LIVE` in `infrastructure/lib/fi-fleet-health-live.ts`; `true` since
  2026-10-23, when CMS's groundings route accepted the fault form). A `p0` vehicle still gets its
  booking item and the stop-driving directive.
- No grounding item when the vehicle is already grounded (`cms-{stage}-storage-vehicle-groundings`
  shows an open grounding for this vehicle). A read error counts as "not grounded" (safe direction).
- A booking item is always present when any need is present.

### 1.4 Fixed headline templates

The model writes only `detail` (at most 600 characters) for booking items. Grounding items
receive fixed text and no model call. All headlines use fixed templates; no other form is valid.

| Item | Headline |
|---|---|
| Booking — any severity | `<severity label> fault <code>: book service at <rooftop>` (when `p0`); or `<need description>: book service at <rooftop>` (other severities) |
| Grounding | `Stop-driving fault <code>: ground until the repair is recorded` |

`<code>` is the `p0` fault code; `<rooftop>` is the chosen candidate rooftop's `dealer_id`.

More precisely, the fixed headline templates are:

| Condition | Headline |
|---|---|
| Booking, `p0` fault, rooftop available | `Stop-driving fault <code>: book service at <rooftop>` |
| Booking, `p0` fault, **no rooftop available** | `Stop-driving fault <code>: choose a rooftop to book service` |
| Booking, `attention` fault or Agent 2 finding | `Attention needed: book service at <rooftop>` |
| Booking, `soon` fault, Agent 2 finding or overdue PM | `Service needed soon: book service at <rooftop>` |
| Booking, `routine` (PM due) | `PM due: book service at <rooftop>` |
| Grounding | `Stop-driving fault <code>: ground until the repair is recorded` |

The after-5-attempts informational item (§ 4.3) uses a separate headline not from this table.

### 1.5 `disclosure_txt`

For a `p0` item (booking and grounding alike), `disclosure_txt` is the fault catalog's directive
for the code, byte for byte. No model output touches this field. CMS shows it in full on every
related screen; it is never truncated or summarised by the agent or CMS. A test asserts byte
equality with the catalog entry; mutation: trim or paraphrase the text.

When a p0 code has no catalog directive (code absent from `catalog_entries` or `directive` key missing), the loop uses the first sentence of AVX's `pullover_stop_engine` spoken template (first letter upper-cased) as the fallback `disclosure_txt` for both the booking and grounding items, and increments `p0_directive_fallbacks` in the run stats.

Items whose severity is below `p0` carry no `disclosure_txt`.

### 1.6 Evidence keys

All entries carry `vin` and `provenance: live`. The `diagnosis_source` entry indicates whether the
model ran (`model`) or the rules fallback fired (`rules`).

| Key | Contents |
|---|---|
| `health.need` | One entry per need. `kind` (`fault`, `agent2_finding`, `pm_task`); `source` (`vehicle_fault_status` for faults, `pm_vehicle_status` for PM tasks, `avx_findings` for Agent 2 needs); `code` (for fault needs), `finding_id` (for Agent 2 needs), `task_code` (for PM needs); `severity_band`; `need_id` (deterministic: `fault:<code>`, `pm:<task_code>`, `a2:<finding_kind>`; Agent 2's kind, not its finding id, because diagnostics Findings re-mint about every 30 minutes). Fault needs also carry `fault_source` (`"generated"` \| `"cms"` \| `null`): the `source` column of ADP's `vehicle_fault_status_daily`, the source of the (vin, code)'s latest open occurrence, else of its latest occurrence (same-day tie: `cms`) — ADP spec `2026-10-31-demo-fault-volume` D6. `null` only on snapshot rows written before ADP's snapshot job began writing it. **Deploy order**: ADP's `ALTER TABLE ... ADD COLUMNS (source string)` must run before this AVX build is deployed; against a table without the column the fault query fails (`COLUMN_NOT_FOUND`) and the whole nightly fleet-health run aborts. The old AVX build against the new ADP schema is safe (it doesn't select the column). PM `next_due_date`/`last_done_date` are a follow-on requiring the PM SQL to expose those columns. |
| `health.rooftops` | Top-three candidate rooftops: `dealer_id`, `distance_miles`, `parts_in_stock` (boolean, computed over all needed parts), `stock_as_of` |
| `health.position` | `latitude`, `longitude`, `position_date`; or `source: fleet_home` when no telemetry position exists for this VIN |
| `vehicle.utilisation` | `daily_average_miles_30d` |
| `health.recall_visit` | Present when a live Stage C booking item exists for this vehicle: `recall_item_finding_id`, `recall_id`, `rooftop_dealer_id`. The booking item defaults to that rooftop and notes "Book with recall <id> at <rooftop>". |
| `health.grounding_reason` | Present on grounding items only: `fault_codes[]` (the open `p0` codes) and `last_sighting[]` (one entry per code: `code`, `last_seen_at` — the anchor date used by the return-to-service check). **W4 note**: `last_seen_at` is currently `MAX(raised_at)` from the ADP `vehicle_fault_status` view, which equals the true anchor (`max(last_seen_at)` across per-occurrence sightings) for single-occurrence events. Once the ADP view exposes its computed `anchor` column (a stage-2 ADP change), this field will be selected from there. |
| `health.diagnosis_source` | `model` when the Bedrock call ran; `rules` when the one-retry-then-fallback fired (`confidence: low` in that case) |
| `health.episode` | `started_at_attempt`: the attempt number that opened the current episode (§ 6); the per-episode cap counts from it |

### 1.7 `recommended_action` per item type

**Booking item** (`book_fleet_service`):

```json
{
  "action_kind": "book_fleet_service",
  "target_system": "dms",
  "parameters": {
    "vehicle_vin":  "<server-set from Finding scope>",
    "needs":        "<server-set: list of need ids with their labor_op and description>",
    "dealer_id":    "<agent-chosen or operator-chosen roster rooftop; null when no candidate rooftop is available for a p0 item — see § 6>",
    "urgency":      "<this_week | this_month | next_visit — agent-chosen or operator-chosen>"
  }
}
```

**Grounding item** (`ground_vehicle`):

```json
{
  "action_kind": "ground_vehicle",
  "target_system": "cms",
  "parameters": {
    "vehicle_vin":  "<server-set from Finding scope>",
    "fault_codes":  "<server-set: the open p0 codes>"
  }
}
```

The CMS groundings route receives the fault form as `{vin, faultCodes, findingId, actionId}`
(camelCase `faultCodes`). `fault_codes` is the AVX Action parameter name (snake_case); the
CMS wire field is `faultCodes`. Group 7 updates `fleet-health-api.md` § 4.1 with this shape.

Server-set parameters (§ 4 of this document, contract § 4 v2.7): a body value that omits or
equals the server-set value is accepted; a different value returns 400.

The DMS executor sends one `customer_pay` line per need, and each line carries that need's
`need_id`. DMS's dealer stand-in recognises a fleet-health booking only by such a line
(DMS `docs/fleet-warranty.md` § 5.1), so dropping `need_id` would leave every fleet-health
booking at `Draft` (`agents/executors/tests/test_fleet_service_need_id.py`).

`ground_vehicle` in the recall form (Stage C) carries `recall_id` server-set. `ground_vehicle`
in the fault form (this stage) carries `fault_codes` server-set. Exactly one of the two is
present; having both is a schema error.

### 1.8 Expiry

3 days from `computed_at`, refreshed by each daily run while a need lasts. A need that clears
(service recorded, fault resolved) causes the item to expire within 3 days of the daily run that
first finds it gone. CMS shows the newest attempt per (vehicle, subject segment) and lists earlier
attempts as history through the activity feed (contract § 9).

---

## 2. `quality.investigation` — Investigation Findings

### 2.1 Identity and scope

- `agent_id: agent-fi-quality`, `component: investigation`
- `finding_kind: quality.investigation`
- Population-scoped: `scope.population_id = "meridian-motors"` (the deployment's maker id).
  Population Findings carry no `fleet_id`, so no fleet queue shows them (contract § 7's
  fail-closed state). Their `agent-fi-` prefix puts them under the iOS route exclusion and the
  push-broker skip (Stage C D13) with no new code.
- One live investigation per pattern (`subject_key = <investigation_id>`); a repeat request
  returns the existing one.

### 2.2 `subject_key` form

```
<investigation_id>
```

`<investigation_id>` is the UUID from `vsa-{stage}-avx-investigations`. It is stable for the
life of the investigation and joins the `finding_key` hash.

### 2.3 Severity

`quality.investigation` Findings are written only when the investigation job completes; no
Finding exists while the job is in flight (in-flight status lives in the investigations table,
not in a Finding). Severity is derived from the result at write time:

| Chosen failure mode | Severity |
|---|---|
| Root cause found, a remedy option is recommended | `soon` |
| Root cause found, only `monitor` option | `routine` |
| Root cause `unknown`, fallback ran | `routine` |

The model has no numeric field in its tool schema and no severity field; severity is always
deterministic.

### 2.4 Fixed headline

```
Investigation of <code family>: traces to <cause>
```

`<code family>` is the pattern's code family. `<cause>` is the chosen candidate's dimension and
value (e.g. `lot L of supplier B` or `plant CGA, build 2024-03 to 2024-05`). When the fallback
ran, the cause is the top-ranked candidate's dimension and value (spec D10: "the top-ranked
candidate … `confidence: low`, `diagnosis_source: rules`"). This headline is written by the
agent, not the model.

### 2.5 `disclosure_txt`

Not present on `quality.investigation` Findings.

### 2.6 Evidence keys

| Key | Contents |
|---|---|
| `quality.pattern` | `pattern_id`, `code_family`, `dimension`, `value`, `ratio`, `p_value`, `method`, `first_detected`, `trend`, `affected_count` |
| `quality.candidates` | Ranked candidates: `candidate_id`, `dimension`, `value`, `ratio`, `p_value`, `rank` |
| `quality.root_cause` | `candidate_id`, `failure_mode_id`, `reasoning` (at most 1,200 chars), `confidence`, `diagnosis_source` |
| `quality.failure_mode` | `failure_mode_id`, `component`, `mechanism`, `symptom_codes[]` |
| `quality.options` | Each option: `remedy_option_id`, `remedy_type`, `parts_cost_per_vehicle`, `labor_hours_per_vehicle`, `total_vehicles`, `total_cost`, `lead_time_weeks`, `over_the_air`, `recommended` (boolean), `tradeoffs` (at most 600 chars). All numeric fields come from ADP corpus tables; the model has no numeric tool field. |
| `quality.affected_vins` | `count`, `sample[]` (up to the configured cap), each entry: `vin`, `model`, `model_year`, `plant`, `build_month`, `component_lot` |
| `quality.controls` | Matched controls: same shape as `quality.affected_vins`, unaffected vehicles |
| `quality.diagnosis_source` | `model` when both Bedrock calls ran; `rules` when the one-retry-then-fallback fired |

### 2.7 `recommended_action`

```json
{
  "action_kind":  "record_engineering_decision",
  "target_system": "adp",
  "parameters": {
    "investigation_id": "<server-set>",
    "pattern_id":       "<server-set>",
    "option_id":        "<agent-chosen remedy_option_id, or operator-chosen>",
    "notes":            "<at most 1,000 chars — body may differ>"
  }
}
```

`investigation_id` and `pattern_id` are server-set (§ 4 below). `option_id` is the agent's
recommended option; a body that picks a different option makes the decision `overridden`. `notes`
are free text; changing notes does not make the decision `overridden`.

**Approval.** Only `product-engineer` group members may approve or decline; AVX records
`decided_by.kind: engineer`. G5 applies (§ 5). The kind is not auto-approvable.

**A CS engineer selects Accept or Decline on the Diagnosis Workbench.**

### 2.8 Expiry

7 days from `computed_at`. Not refreshed (there is no daily re-run for an investigation; a new
investigation request creates a new Finding).

---

## 3. `quality.issue` — Campaign-Issue Findings

### 3.1 Identity and scope

- `agent_id: agent-fi-quality`, `component: investigation`
- `finding_kind: quality.issue`
- Population-scoped: `scope.population_id = "meridian-motors"`.
- Written by the investigation agent after the `record_engineering_decision` executor writes a
  new thread record version with a `draft` campaign link.
- One `quality.issue` Finding per thread record: `subject_key = "<thread_id>/issue"`.

### 3.2 `subject_key` form

```
<thread_id>/issue
```

### 3.3 Severity

`soon` when the chosen failure mode's remedy type is `software_update` or `hardware_recall`;
`routine` otherwise.

### 3.4 Fixed headline

| Remedy type | Headline |
|---|---|
| `hardware_recall` | `Draft recall for <code family>: approve to issue` |
| `software_update` | `Draft software campaign for <code family>: approve to issue` |

### 3.5 `disclosure_txt`

Not present on `quality.issue` Findings.

### 3.6 Evidence keys

| Key | Contents |
|---|---|
| `quality.thread` | `thread_id`, `thread_version`, `decision_option_id`, `decided_by.kind`, `decided_at` |
| `quality.draft` | `draft_type` (`hardware_recall` or `software_update`), `draft_id`, `campaign_id` (once issued), `affected_count` |

### 3.7 `recommended_action`

**Recall draft** (`issue_recall`):

```json
{
  "action_kind":  "issue_recall",
  "target_system": "adp",
  "parameters": {
    "thread_id":  "<server-set>",
    "draft_id":   "<server-set — the recall_campaigns_issued draft id>"
  }
}
```

**Software-campaign draft** (`issue_software_campaign`):

```json
{
  "action_kind":  "issue_software_campaign",
  "target_system": "adp",
  "parameters": {
    "thread_id":  "<server-set>",
    "draft_id":   "<server-set — the ota_campaigns_issued draft id>"
  }
}
```

All parameters are server-set; no body field is changeable. A body that equals them is accepted;
any difference returns 400. Approval always has `decision: approved`.

**A second product-engineer approval** is required (G5 applies, § 5). The kind is not
auto-approvable.

### 3.8 Expiry

30 days from `computed_at`. Not refreshed.

---

## 4. Server-set parameters per kind (contract § 4 v2.7)

The complete table for Stage D kinds, extending
[`docs/fi-recall-warranty-contract.md`](fi-recall-warranty-contract.md) § 3.

| Kind | Target | Server-set (body must omit or match) | Body may differ |
|---|---|---|---|
| `book_fleet_service` | `dms` | `vehicle_vin`, `needs` | `dealer_id` (any roster rooftop), `urgency` |
| `ground_vehicle` — recall form (Stage C) | `cms` | `vehicle_vin`, `recall_id` | (nothing) |
| `ground_vehicle` — fault form (this stage) | `cms` | `vehicle_vin`, `fault_codes` | (nothing) |
| `record_engineering_decision` | `adp` | `investigation_id`, `pattern_id` | `option_id`, `notes` |
| `issue_recall` | `adp` | `thread_id`, `draft_id` | (nothing) |
| `issue_software_campaign` | `adp` | `thread_id`, `draft_id` | (nothing) |

For `book_fleet_service`, changing `dealer_id` or `urgency` from the agent's recommendation
makes the decision `overridden`; omitting them does not. **Exception (design answer 9):** when the
recommendation's `dealer_id` is null (no candidate rooftop was available for a `p0` fault), the
approve body must supply a roster `dealer_id` (400 otherwise), and supplying a value the
recommendation lacks is `approved`, not `overridden`.

For `record_engineering_decision`, changing `option_id` makes the decision `overridden`; changing
`notes` alone does not.

**Urgency validation.** The approve route validates `urgency` against the band: for a `p0` item,
only `this_week` is accepted; any other value returns 400. For `attention` and lower severities,
all three values (`this_week`, `this_month`, `next_visit`) are accepted. Changing `urgency` from
the agent's recommendation makes the decision `overridden`; omitting it does not.

**Fault grounding clearance.** A fault grounding clears only through an operator pressing
Return to service on CMS, which is gated by Stage C's return-to-service route. That route refuses
(409, reason shown) while any stop-driving code on the vehicle's open fault groundings lacks a
non-null `repair_ref` in `vehicle_fault_status`, read from a fresh cache. The repair rule
(decisions.md "Group 1 review cycle 2"):

- For a (vin, code), its **anchor** is the latest sighting across all its events:
  `max(last_seen_at)`, where each event's `last_seen_at` is its last occurrence (equal to
  `raised_at` for a single occurrence). `fleet_ops.faults` gains `last_seen_at` so stage 2's CMS
  side can carry `dtc-history`'s `lastSeenAt`.
- A **qualifying repair** is a DMS repair order for the VIN with status exactly `Ready`,
  `Delivered` or `Invoiced` and a line whose labor op maps to the code (configuration).
  Ordinary `service_records.dtc_codes` don't qualify (their background is 24–36% a quarter).
- `repair_ref`/`repaired_on` = the earliest qualifying repair dated **on or after the anchor**
  (same day counts). No lookback limit. If none, both are null.
- So a sighting after a repair moves the anchor past it and clears `repair_ref`; a vehicle that
  keeps reporting the code after a repair stays unrepaired.
- Return to service (D9) needs a non-null `repair_ref` for every stop-driving code on the
  vehicle's fault groundings, read from a fresh cache.

A self-clear and a remote clear never count. An unreadable or stale health cache also refuses
(fail closed, as Stage C does for recall status). Nothing lifts a grounding automatically.

**`fault_grounding_enabled` flag** (env `FAULT_GROUNDING_ENABLED`, set from
`FI_FAULT_GROUNDING_LIVE`; `true` since 2026-10-23). When `false`, the agent writes no grounding item
for a `p0` fault. The vehicle still gets its booking item and `disclosure_txt`. Turning it off stops
new grounding items and leaves existing groundings in place.

---

## 5. Guardrail G5 (new in v2.7)

G5 is the fifth guardrail in `registry.dispatch()`. It applies to all three engineering action
kinds in this stage.

| # | Guardrail | Kind(s) | Refused when |
|---|---|---|---|
| G5 | Engineering decisions need a product engineer | `record_engineering_decision`, `issue_recall`, `issue_software_campaign` | `decided_by.kind != "engineer"` |

**Failure reason** (contract § 10a v2.7 row):

```
Blocked: engineering decisions need a product engineer.
```

This sentence is the only `failure_reason` written for a G5 block. It is admitted by the schema
pattern in `action.schema.json` (v2.7 update).

**G5 and the other guardrails.** G1–G4 require `decided_by.kind: operator`; an
`engineer` is refused by them (spec D14; `fi-schema-v2.7-proposal.md` § 7
description: "an 'engineer' therefore cannot satisfy G1–G4 unless they are also in
the operator groups (platform-admin)"). G5 requires `decided_by.kind: engineer`;
an `operator` or a `rule` is refused by G5 even if the other checks were met.
Mutation: drop G5; a `decided_by.kind: operator` or `decided_by.kind: rule` passes
unblocked on a G5 kind.

**No auto-approval.** None of the three engineering kinds is `auto_approvable`; approval rules
return 400 for them (contract § 6's guarded list).

---

## 6. Attempt rules for fleet-health items (spec D9)

Applies to `fleet.health.service` only. At the next daily run, the agent writes attempt n+1 when:

| Previous attempt's queue status | Condition | Attempt n+1? |
|---|---|---|
| `cancelled` | Vehicle still has a need | Yes, immediately |
| `failed` (executor terminal error) | Vehicle still has a need | Yes, immediately |
| `refused` (target system said no) | Vehicle still has a need | Yes, immediately |
| `rejected` (operator declined) | Severity `p0` or `attention` AND ≥ 14 days since decision | Yes |
| `rejected` (operator declined) | Severity below `attention` AND ≥ 30 days since decision | Yes |
| `blocked` (guardrail) | any | No — inform the operator; no automatic retry |
| `stalled` (dispatch timed out) | any | No — Stage A stalled escalation fires; no attempt item |
| `executed` / `completed` | Episode closed (see below) | See episode rules below |
| `dismissed` | Episode closed (see below) | See episode rules below |

**Episode management (design answer 1).** The statuses `completed`, `executed`, `dismissed`, and
`exhausted` close the current episode for that vehicle+segment. A new episode starts on the NEXT
daily run when at least one current need_id was NOT present in the closing item's needs, OR when the
decline window (14 days for `p0`/`attention`, 30 days for others) has passed since the closing
decision. The 5-attempt cap counts attempts within the current episode.

Maximum 5 attempts per episode per (vehicle, subject segment). After 5 attempts an informational
item (no `recommended_action`) is written to `<segment>_exhausted/<n>`, where `<n>` is the attempt
number that triggered exhaustion:
"Bookings keep failing — manual follow-up needed." The exhausted item is a closing record: a new
episode starts once a new need or elapsed window triggers it. `in_progress` never re-raises.
CMS groups the exhausted item under the `service` or `ground` segment respectively.
Each attempt stores its `episode_start` (the attempt number of the first attempt in that episode)
in a `health.episode` evidence entry; the cap is `attempt_n - episode_start + 1 >= 5`.

Stage A's rule of one decision per Finding is unchanged: each attempt is a new `subject_key` and
a new Finding. The next attempt's `subject_key` is `<segment>/<n+1>`.

---

## 7. Browser fields — additions to contract § 7

The § 7 allowlist applies to every fleet-health and quality Finding without exception. Additional
notes specific to these kinds:

- **`disclosure_txt`** on `fleet.health.service` `p0` items is returned in full. CMS shows it
  without truncation on the vehicle page and the Service page. The evidence cap does not apply to
  `disclosure_txt`.
- **Booking and grounding items** for the same vehicle are linked in the browser through the
  common VIN scope. CMS identifies the subject segment (`service` or `ground`) from `parameters`
  on decision Actions (and from `subject_key` prefix before any decision), to group and display
  them side by side.
- **`subject_key`** is not returned to the browser (§ 7 "Not returned").
- **`population_id`** in scope is not returned to the browser; only `scope.vin` is returned as
  `vin` for VIN-scoped Findings. Population-scoped Findings have no `vin` at the browser.
- **Attempt history** for fleet-health items: CMS shows the newest attempt per (vehicle, subject
  segment). Earlier attempts appear in the activity feed (contract § 9).
- **Investigation status**: CS shows "Investigating since <time>" (from the run record's
  `started_at`), then the investigation result, or "The investigation could not finish. Start it
  again." (G1 failure sentence on the item).

---

## 8. Maker-scope projection (spec D1, D16)

Population-scoped Findings and the routes that serve them follow a strict allowlist. The maker
never sees fleet-private fields.

Routes under `/api/v1/fleet-intelligence/population/` apply this projection to every response.
The allowlist, checked by a test and a live probe (spec D20):

| Allowed | Never returned |
|---|---|
| `vin`, `model`, `model_year`, `plant`, `build_month` | `fleet_id`, fleet name |
| Tracked component: `component_id`, `supplier_id`, `supplier_lot` | Driver identity or id |
| Software versions | Owner or customer id |
| Fault codes and dates | Latitude, longitude, trip |
| Health finding kind, severity, evidence values | Cost, dealer id, rooftop |
| Pattern: code family, cause, ratio, p value, counts | RO id or lines |
| Fleet type as a cohort label (`rental`, `delivery`, `field_service`, `city`) | Any other fleet attribute |

A maker response containing any forbidden key, or a string value containing a forbidden value
(fleet id, driver, owner, latitude, longitude, trip id, cost figure, dealer id), fails the
maker allowlist test. The live probe repeats this check against staging.

---

## 9. Model tool input schemas (spec D8 step 6, D10)

The three tools are injected into the model's context before each Bedrock call. The model's
output is limited to these schemas; all other fields are server-set or come from corpus tables.
No tool schema contains a numeric field (cost, hours, lead time, or rate).

### 9.1 `record_service_plan` — fleet-health agent (spec D8 step 6)

Called by the model after evaluating the vehicle's needs and the candidate rooftops. The model
picks a rooftop from the candidate list, selects an urgency, optionally defers lower-priority
needs, and writes the operator-visible booking detail. No numeric field; all numeric evidence
(distance, parts-in-stock) is server-set.

```json
{
  "dealer_id":       "<string — must be one of the candidate dealer_ids in health.rooftops>",
  "urgency":         "<this_week | this_month | next_visit>",
  "defer_need_ids":  ["<need_id — needs to defer to a later visit; never p0 or attention>"],
  "detail":          "<at most 600 characters — booking detail shown to the operator>",
  "evidence_refs":   ["<evidence key references the model cites>"],
  "confidence":      "<high | medium | low>"
}
```

Server-level validation: `dealer_id` must appear in the `health.rooftops` evidence; `urgency`
must be one of the three valid values; for a `p0` item `urgency` must be `this_week` (§ 4);
`defer_need_ids` must not contain a `p0` or `attention` need; `detail` is required and capped
at 600 characters. A model output that fails any check triggers the one-retry-then-rules
fallback (`diagnosis_source: rules`).

### 9.2 `record_root_cause` — investigation agent, first model call (spec D10)

Called by the investigation agent after evaluating the ranked candidates and failure-mode
descriptions. No numeric field; all numeric evidence (rate_ratio, p_value) is server-set.

```json
{
  "candidate_id":    "<candidate_id from the investigation bundle>",
  "failure_mode_id": "<failure_mode_id from the bundle, or the string \"unknown\">",
  "evidence_refs":   ["<evidence key references the model cites>"],
  "reasoning":       "<at most 1,200 characters>",
  "confidence":      "<high | medium | low>"
}
```

Server-level validation: `candidate_id` must exist in the bundle's candidate list;
`failure_mode_id` must exist in the bundle or be the string `"unknown"`; `confidence` must be
one of three values; `reasoning` is required and capped at 1,200 characters. A model output
that fails any check triggers the rules fallback.

### 9.3 `record_design_options` — investigation agent, second model call (spec D10)

Called by the investigation agent after evaluating the remedy options list. The model provides
two to four options from the bundle's remedy options, marks exactly one `recommended`, and
explains each choice. No numeric field; cost, labor hours, lead time and over-the-air flag come
from corpus tables via the investigation bundle, never from this call.

```json
{
  "options": [
    {
      "remedy_option_id": "<remedy_option_id from the investigation bundle>",
      "tradeoffs":        "<at most 600 characters — model's analysis of this option>",
      "recommended":      true
    },
    {
      "remedy_option_id": "<remedy_option_id>",
      "tradeoffs":        "<at most 600 characters>",
      "recommended":      false
    }
  ]
}
```

The `options` array has 2–4 entries (matching the bundle's options). Exactly one entry has
`"recommended": true`; the rest have `"recommended": false`. Server-level validation: each
`remedy_option_id` must appear in the bundle; exactly one `recommended: true` required;
`tradeoffs` is required per option and capped at 600 characters. A model output that fails any
check triggers the rules fallback (`diagnosis_source: rules`).

---

## 10. Cross-references

- [`docs/fi-action-queue-contract.md`](fi-action-queue-contract.md): queue mechanics, §§ 1, 4, 6,
  10, 10a, 12 updated to v2.7.
- [`docs/fi-recall-warranty-contract.md`](fi-recall-warranty-contract.md): Stage C kinds and the
  recall form of `ground_vehicle` (§ 3 there).
- [`docs/fi-schema-v2.7-proposal.md`](fi-schema-v2.7-proposal.md): full diff of schema additions.
- [`docs/api/avx-findings-actions.openapi.yaml`](api/avx-findings-actions.openapi.yaml): routes and
  schemas.
- `agents/tier2/schemas/finding.schema.json`, `agents/tier2/schemas/action.schema.json`.
- Spec: `.kiro/specs/2026-10-05-fi-fleet-health-stage-d/spec.md`.
