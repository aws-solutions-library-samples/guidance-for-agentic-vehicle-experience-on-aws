# FI Recall and Warranty Contract — Stage C

**Version**: 1.0 (Stage C — spec `2026-10-05-fi-recall-warranty-stage-c`)
**Spec decisions**: D9, D10, D11, D12
**Relates to**: [`docs/fi-action-queue-contract.md`](fi-action-queue-contract.md) (§§ 3, 4, 10
updated to v2.6)
**OpenAPI**: [`docs/api/avx-findings-actions.openapi.yaml`](api/avx-findings-actions.openapi.yaml)

This document covers the two Finding kinds introduced in Stage C:
- `fleet.recall.vehicle` (§ 1) — one per vehicle, campaign and action
- `fleet.warranty.claim_candidate` (§ 2) — one per repair-order line

It is a companion to [`docs/fi-action-queue-contract.md`](fi-action-queue-contract.md), which
governs the queue mechanics, approve/decline routes, guardrails and dispatch. Everything in that
document applies here; this document records the fields and rules specific to these two kinds.

---

## 1. `fleet.recall.vehicle` — Recall Findings

### 1.1 Identity and scope

- `agent_id: agent-fi-recall-warranty`, `component: recall`
- `finding_kind: fleet.recall.vehicle`
- VIN-scoped. The writer projects the vehicle's `fleet_id`; a vehicle with no resolved fleet is in
  no queue (contract § 7, fail closed). Both ground and book items for the same vehicle share the
  same scope.

### 1.2 `subject_key` forms

```
<campaign_id>/<action>/<attempt>
```

| Segment | Values |
|---|---|
| `campaign_id` | `MRD-YYYY-NNN` (the ADP catalog's id for the campaign) |
| `action` | `book` or `ground` |
| `attempt` | Integer ≥ 1, starting at 1 on the first run for this vehicle/campaign/action |

A vehicle with a `do_not_drive` recall produces two items: `<id>/ground/1` and `<id>/book/1`.
The `subject_key` joins the `finding_key` hash (contract § 1.1). Each attempt is a new Finding
with a fresh `pending` item; Stage A's rule of one decision per Finding is unchanged.

### 1.3 Severity and items — band table

The band table is deterministic configuration (spec D9). The model cannot change severity or
grounding.

| Campaign | Match basis | Severity | Items produced |
|---|---|---|---|
| `safety`, `do_not_drive` | either | `p0` | ground item AND book item |
| `safety` | CONFIRMED | `p0` | ground item AND book item |
| `safety` | POPULATION | `attention` | book item only |
| `non_safety` | CONFIRMED | `soon` | book item only |
| `non_safety` | POPULATION | `routine` | book item only |

Additional rules:
- No grounding item when the vehicle is already grounded (`cms-{stage}-storage-vehicle-groundings`
  shows an open grounding for this vehicle).
- No booking item when `remedy_available_from` is in the future: an informational item (no
  `recommended_action`) says when parts are expected. The booking item appears on that date.
- When no roster rooftop holds every remedy part, the booking item names the nearest rooftop
  and notes the parts are not in stock.

### 1.4 Fixed headline templates

The model writes only `detail` (at most 600 characters). All recall-item and grounding
headlines use fixed templates; no other form is valid:

| Item | Headline |
|---|---|
| Grounding | `Safety recall <id>: ground until the remedy is done` |
| Booking — remedy parts available | `Recall <id>: book the remedy at <rooftop>` |
| Informational — remedy parts not yet available | `Recall <id>: remedy parts expected <date>` |

`<id>` is the `campaign_id`; `<rooftop>` is the candidate rooftop's `dealer_id`; `<date>` is the
catalog's `remedy_available_from` (ISO 8601 date).

The after-5-attempts informational item (§ 4) uses a separate headline not from this table:
"Bookings keep failing — manual follow-up needed." That item carries no `recommended_action`.

Grounding items receive fixed text and no model call.

### 1.5 `disclosure_txt`

The catalog's `summary`, `consequence` and `remedy` fields joined by `"\n\n"` (decisions.md call
12), byte for byte. No model output touches this field. CMS shows it in full on every
recall-related screen; it is never truncated or summarised by the agent or CMS. A test asserts
structural membership: `summary in txt`, `consequence in txt`, `txt.endswith(remedy)` — a trim
that drops the remedy or a paraphrase that rewrites a field both fail.

### 1.6 Evidence keys

All entries carry `vin` and `provenance: live` (Stage B D8 convention). The `diagnosis_source`
entry indicates whether the model ran (`model`) or the rules fallback fired (`rules`).

| Key | Contents |
|---|---|
| `recall.campaign` | `campaign_id`, `recall_class`, `do_not_drive`, `park_outside`, `received_date`, `catalog_version` |
| `recall.rule` | `rule_version`, populated rule fields (`models`, `model_year_min`, `model_year_max`, `plants`, `build_from`, `build_to`). FI stage D 9.3: ADP's "no bound" year sentinels on an issued recall (`0` / `9999`) are written `null`, as unbounded `plants` / `build_*` already are; an issued lot recall adds `component_lots`, a list of `{supplier_id, supplier_lot}` read from `adp_{stage}_engineering.recall_campaigns_issued` (issued rows not reusing a Stage C id, the received view's rule). A campaign without lots (every Stage C one) has no `component_lots` key, so its `inputs_hash` is unchanged. |
| `recall.match_basis` | `match_basis` (`CONFIRMED` or `POPULATION`) |
| `recall.confirming_signal` | `confirming_code`, `confirming_seen_on`, `confirming_source` (absent when `match_basis = POPULATION`) |
| `recall.item` | `campaign_id`, `item` (one of `book`, `ground`, `parts_expected`, `attempts_exhausted`), `attempt` (integer). Present on every recall item, including informational ones. CMS groups on (`vin`, `campaign_id`, `item`) and shows the highest `attempt` first. |
| `recall.rooftops` | Top-three candidate rooftops: `dealer_id`, `distance_miles`, `in_stock` (boolean, computed over every remedy part), `stock_as_of` |
| `recall.position` | `latitude`, `longitude`, `position_date` (from ADP telemetry); or `source: fleet_home` when no telemetry position exists for this VIN |
| `vehicle.utilisation` | `daily_average_miles_30d` |
| `vehicle.open_items` | Finding ids of other open items for this vehicle: other recall items, health Findings, coverage ending within 30 days |
| `recall.diagnosis_source` | `model` when the Bedrock call ran; `rules` when the one-retry-then-fallback fired (`confidence: low` in that case) |

The rules-fallback plan is built by `runner.build_rules_plan(candidates, band_result)`: nearest
rooftop from `recall.rooftops` by `distance_miles` as `dealer_id`, `combine_with: []`,
`band_result.urgency` as `urgency`, a fixed `detail`, `confidence: low`, `diagnosis_source: rules`.
A test goes through this function with three candidates and a band result and asserts the nearest
`dealer_id`, empty `combine_with`, the band's urgency, `confidence: low` and `diagnosis_source: rules`.

### 1.7 `recommended_action` per item type

**Booking item** (`book_recall_service`):

```json
{
  "action_kind": "book_recall_service",
  "target_system": "dms",
  "parameters": {
    "vehicle_vin": "<server-set from Finding scope>",
    "recall_id":   "<server-set from subject_key campaign>",
    "dealer_id":   "<agent-chosen or operator-chosen roster rooftop>"
  }
}
```

**Grounding item** (`ground_vehicle`):

```json
{
  "action_kind": "ground_vehicle",
  "target_system": "cms",
  "parameters": {
    "vehicle_vin": "<server-set from Finding scope>",
    "recall_id":   "<server-set from subject_key campaign>"
  }
}
```

Server-set parameters (§ 3 of this document, contract § 4 v2.6): a body value that omits or
equals the server-set value is accepted; a different value returns 400.

### 1.8 Expiry

3 days from `computed_at`, refreshed by each daily run while the condition persists. A remedied
or superseded item expires within 3 days of the daily run that first finds the condition gone. CMS
shows the newest attempt per (vehicle, campaign, action) and lists earlier attempts as history
through the activity feed (contract § 9).

---

## 2. `fleet.warranty.claim_candidate` — Warranty Findings

### 2.1 Identity and scope

- `agent_id: agent-fi-recall-warranty`, `component: warranty`
- `finding_kind: fleet.warranty.claim_candidate`
- VIN-scoped. Writer projects `fleet_id`; fail closed as in § 1.1.

### 2.2 `subject_key` form

```
<ro_id>/<line_no>
```

One Finding per DMS repair-order line. `<ro_id>` is the DMS repair order id; `<line_no>` is the
line number within that order (as it appears in the DMS candidates table). Only `approved` and
`requires_review` verdicts get a Finding with `recommended_action`; any selected line whose
model returns `proceed: insufficient_evidence` gets an informational Finding only (no
`recommended_action`). Denied lines receive no Finding.

### 2.3 Severity

| Condition | Severity |
|---|---|
| Default | `routine` |
| `recoverable_usd` ≥ $1,000, OR filing deadline within 7 days | `soon` |

Both thresholds are configuration. The model has no numeric field in its tool schema; severity is
always deterministic.

### 2.4 Fixed headline

```
Warranty claim candidate: <verdict_word> — <labor_op>
```

`<verdict_word>` is DMS's verdict word verbatim: `approved` or `requires_review`. The headline
uses DMS's exact word for both cases (`requires_review`, not "requires review"). Informational
items (no `recommended_action`) use the headline:

```
Warranty: insufficient evidence — <labor_op>
```

### 2.5 `disclosure_txt`

The coverage group's term statement from `warranty_terms.json` (e.g., `"Basic warranty: 4 years /
50,000 miles"`), followed by the fixed sentence: `"DMS decides coverage when the claim is filed."`

### 2.6 Evidence keys

| Key | Contents |
|---|---|
| `warranty.verdict` | `verdict` word, `reason`, `matched_rule_id`, `terms_version` from the DMS candidates table |
| `warranty.ro_lines` | RO lines considered: `line_no`, `labor_op`, `fault_code`, `pay_type`, `status` |
| `warranty.coverage` | `coverage_group`, `in_service_date`, `odometer_at_repair` (miles), `term_ends_on`, `term_miles_left` (miles) |
| `warranty.fault_history` | First-sighting date of the fault code in ADP `service_records` |
| `warranty.telemetry` | Dated telemetry entries around the repair date from ADP `fleet_ops`, where available |
| `warranty.recovery` | `recoverable_usd`, `flat_rate_hours`, `labor_rate`, `parts_allowance`, `terms_version` |

### 2.7 `recommended_action`

`approved` and `requires_review` verdicts that did not return `insufficient_evidence`:

```json
{
  "action_kind": "submit_warranty_claim",
  "target_system": "dms",
  "estimated_value_usd": "<DMS recoverable_usd — never from model>",
  "estimated_value_basis": "<flat-rate hours × labor rate + parts allowance, terms version>",
  "parameters": {
    "vehicle_vin": "<server-set>",
    "ro_id":       "<server-set>",
    "line_no":     "<server-set>",
    "dealer_id":   "<server-set — rooftop from the RO>",
    "labor_op":    "<server-set>",
    "fault_code":  "<server-set>",
    "notes":       "<model-authored narrative, at most 1,000 characters — body may differ>"
  }
}
```

`estimated_value_usd` equals DMS's `recoverable_usd` whatever the model returns. A test asserts
this; mutation: wire `estimated_value_usd` to model output.

`notes` is the model's `narrative` field (complaint, cause and correction) from the
`record_claim_draft` tool call. The operator may send a different value at approve; doing so makes
the decision `overridden` (contract § 4 v2.6). All other parameters are server-set (§ 3 below).

### 2.8 Expiry

The earlier of:
- 3 days after `computed_at`; and
- The filing deadline from the DMS candidates table (30 days after the RO closed, from
  `warranty_terms.json`).

---

## 3. Server-set parameters per kind (contract § 4 v2.6)

Stage A's "Vehicle" rule generalised to a per-kind list. For each kind, the listed parameters are
set by AVX from the Finding at approve time; the request body must omit them or supply an equal
value (400 otherwise). Changing an otherwise-allowed parameter makes the decision `overridden`
(contract § 4). AVX fills in any server-set parameter the body omits.

| Kind | Target | Server-set (body must omit or match) | Body may differ |
|---|---|---|---|
| `book_recall_service` | `dms` | `vehicle_vin`, `recall_id` | `dealer_id` (any roster rooftop) |
| `ground_vehicle` | `cms` | `vehicle_vin`, `recall_id` | (nothing — no body fields are changeable) |
| `submit_warranty_claim` | `dms` | `vehicle_vin`, `ro_id`, `line_no`, `dealer_id`, `labor_op`, `fault_code` | `notes` |
| All other VIN-scoped FI items | — | `vehicle_vin` | depends on kind |

---

## 4. Attempt rules for recall items (D12)

Applies to `fleet.recall.vehicle` only. At the next daily run, the agent writes attempt n+1 when:

| Previous attempt's queue status | Condition | Attempt n+1? |
|---|---|---|
| `cancelled` (DMS closed RO before work done) | Vehicle still unremedied | Yes, immediately |
| `failed` (executor terminal error) | Vehicle still unremedied | Yes, immediately |
| `refused` (target system said no) | Vehicle still unremedied | Yes, immediately |
| `rejected` (operator declined) | `recall_class = safety` AND ≥ 14 days since decision | Yes |
| `rejected` (operator declined) | `recall_class ≠ safety` AND ≥ 30 days since decision | Yes |
| `blocked` (guardrail) | any | No — inform the operator; no automatic retry |
| `stalled` (dispatch timed out) | any | No — Stage A stalled escalation fires; no attempt item |
| `executed` / `completed` | any | No — vehicle remedied or grounded |

Queue statuses above map to Action statuses: `cancelled` ← Action `cancelled`; `failed` ←
Action `failed`; `refused` ← Action `declined` + `failure_kind: target_refused` (no guardrail);
`rejected` ← Action `declined` + `decision: declined`; `blocked` ← Action `declined` + audit
`guardrail`; `stalled` ← Action `pending_execution` > 15 min.

Maximum 5 attempts per (vehicle, campaign, action). After 5 attempts, an informational item (no
`recommended_action`) is written: "Bookings keep failing — manual follow-up needed."

Stage A's rule of one decision per Finding is unchanged: each attempt is a new `subject_key` and
a new Finding. The next attempt's `subject_key` is `<campaign_id>/<action>/<n+1>`; a new Finding
id is generated.

---

## 5. Browser fields — additions to contract § 7

The § 7 allowlist applies to every recall and warranty Finding without exception. Additional
notes specific to these kinds:

- **`disclosure_txt`** for `fleet.recall.vehicle` is returned in full. CMS shows it without
  truncation or summarisation on the Recalls tab and the vehicle service page. The evidence cap
  does not apply to `disclosure_txt`.
- **CONFIRMED and POPULATION** are two separate numbers sourced from ADP
  `recall_fleet_summary`. No screen sums them into a single "affected" count (PRD Must Have 9).
  The vehicle route returns `match_basis` per VIN; the fleet/campaign route returns `confirmed`
  and `population` as separate integers.
- **`estimated_value_usd` and `estimated_value_basis`** are in `recommended_action` on
  `fleet.warranty.claim_candidate` Findings and are returned as part of `recommended_action`
  per § 7. They are not top-level fields.
- **`subject_key`** is not returned to the browser (§ 7 "Not returned").
- **Attempt history**: CMS shows the newest attempt per (vehicle, campaign, action). Earlier
  attempts appear in the activity feed (contract § 9). CMS identifies the campaign and action
  from the `recall.item` evidence entry (`campaign_id` and `item` fields; decisions.md call 11),
  present on every recall item — including informational ones that carry no `recommended_action`.
  `recall.campaign` carries its own catalog fields and is not used for grouping.
- **`recall_id` in `parameters`**: on recall Findings the approve response carries the server-set
  `recall_id` inside `parameters`. This is returned to the browser as part of `parameters` on
  decision Actions (§ 7: "On decision Actions only, also `parameters`").
