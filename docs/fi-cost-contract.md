# FI Cost — Finding and Action Contract

**Version**: 1.0 (Stage B — spec `2026-10-02-fi-cost-stage-b`)  
**Spec decisions**: D2, D3, D7, D8, D9, D11  
**Relates to**: [`docs/fi-action-queue-contract.md`](fi-action-queue-contract.md) §§ 4, 10, 10a  
**Agent**: `agent-fi-cost` (AVX Tier 2 component, EventBridge Scheduler)

This document defines:
- Every cost Finding field and evidence key (derived from ADP `vehicle_cost_baseline` and `vehicle_cost_monthly` views)
- The `apply_cost_action` parameters schema for cost recommendations
- The CMS cost-action record structure that persists operator decisions

---

## 1. Cost Findings

All cost Findings are VIN-scoped (spec D8). They carry `agent_id: agent-fi-cost` and one of two kinds:

### 1.1 `fleet.cost.anomaly` — vehicle's monthly cost exceeds baseline

**When written**: the latest complete month shows `is_outlier` or `is_energy_outlier` in ADP's `vehicle_cost_baseline` view, OR a cost Finding was written in an earlier month for the same (VIN, month) and not yet expired.

**Scope**: one vehicle's cost in one calendar month.

**`subject_key`**: the data month, format `YYYY-MM` (spec D8, user 2026-10-04 decision, decisions.md). Joins the `finding_key` hash to make each month's recommendation its own queue item.

**`severity`** (from `vehicle_cost_baseline`, decisions 2026-10-04 item 1; no band table in AVX):
- `attention` when `ratio_cohort >= severity_attention_ratio` (default 2.0)
- `soon` when `ratio_cohort >= severity_soon_ratio` (default 1.5)
- `routine` otherwise

**`location`**: absent (cost is fleet-wide, decisions 2026-10-04 item 2).

**`recommended_action`** (present when a diagnosis succeeds; spec D6 step 5, D9):
```json
{
  "action_kind": "apply_cost_action",
  "target_system": "cms",
  "parameters": {
    "cost_action": "<one of the four below>",
    "vehicle_vin": "<from Finding scope>",
    "month": "<YYYY-MM>",
    "cause": "<determined by agent; one of spec D6 step 5>"
  },
  "estimated_value_usd": "<monthly saving, by the D7 formula for cost_action>",
  "estimated_value_basis": "<the D7 basis string, with the data month>"
}
```
`estimated_value_usd` and `estimated_value_basis` are computed from evidence values only (spec D7),
never by the model. A diagnosis of `no_action` writes no `recommended_action` at all, and a
`shift_to_depot_off_peak` with no depot off-peak price for the vehicle or its fleet-model cohort is
written as an informational Finding without one.

**Action kinds** (per spec D9, **operator approval required**; contract § 4 makes `apply_cost_action` executable in Stage B):
- `shift_to_depot_off_peak` — reduce public/peak charging, move to depot off-peak
- `schedule_inspection` — maintenance cost above baseline; inspect for repeat issues
- `reassign_or_remarket` — vehicle's fixed costs exceed its utilisation; reassign or remarket
- `driver_coaching` — vehicle's energy efficiency below cohort; coaching or driver reassignment
- `no_action` — no diagnosis supports acting; Finding written for visibility only; no `recommended_action` field

**Expiry**: 35 days (decisions 2026-10-04 item 3).

**Evidence entries** (per spec D8; also see § 2 keys table):

| Field | Type | Description |
|---|---|---|
| `cost.month` | str | Data month, format `YYYY-MM`. Same as `subject_key`. |
| `cost.cost_per_mile` | float | Vehicle's cost per mile (CPM) for the month. |
| `cost.own_baseline` | float | Vehicle's own mean CPM over the previous 6 months (`own_mean`). |
| `cost.cohort_baseline` | float | Fleet-model cohort's median CPM for the same month (`cohort_median`). |
| `cost.method` | str | Verbatim baseline description from `vehicle_cost_baseline.method` (e.g. "own 6-month mean and same-month fleet-model median; outlier when both ratios >= 1.30"). |
| `cost.flag` | str | Outlier classification: `"total"` (total cost), `"energy"` (energy only), or `"both"`. Derived from `is_outlier` and `is_energy_outlier` flags in the baseline view. |
| `cost.miles` | float | Miles driven in the month. |
| `cost.fleet_median_miles` | float | Cohort median miles for the month. |
| `cost.energy` | float | Monthly energy cost (public + depot charging) in USD. From `vehicle_cost_monthly.energy_cost` column. |
| `cost.maintenance` | float | Monthly maintenance cost in USD (non-warranty). |
| `cost.lease` | float | Monthly lease cost in USD. |
| `cost.insurance` | float | Monthly insurance cost in USD. |
| `delta.maintenance` | float | Maintenance per-mile delta vs. cohort ($/mi). |
| `delta.maintenance_usd` | float | Dollar delta: `delta.maintenance × cost.miles`. Used by D7 formula. |
| `delta.energy` | float | Energy per-mile delta vs. cohort ($/mi). |
| `delta.energy_usd` | float | Dollar delta: `delta.energy × cost.miles`. Used by fallback and D7 formula. |
| `delta.lease` | float | Lease per-mile delta vs. cohort ($/mi). |
| `delta.lease_usd` | float | Dollar delta: `delta.lease × cost.miles`. Used by fallback. |
| `delta.insurance` | float | Insurance per-mile delta vs. cohort ($/mi). |
| `delta.insurance_usd` | float | Dollar delta: `delta.insurance × cost.miles`. Used by fallback. |
| `energy.public_and_peak_kwh` | float | kWh charged at public stations (any tariff) or at depot peak rate. |
| `energy.mean_public_peak_price_per_kwh` | float | Vehicle's mean price per kWh for public and peak charging. |
| `energy.depot_off_peak_price_per_kwh` | float | Depot off-peak tariff price per kWh. |
| `energy.public_share` | float | Share of energy kWh from public stations (0–1). For `charging_mix` consistency. |
| `energy.cohort_public_share` | float | Cohort median public share. |
| `energy.peak_share` | float | Share of depot kWh at peak tariff (0–1). For `charging_rate` consistency. |
| `energy.cohort_peak_share` | float | Cohort median peak share. |
| `energy.dc_fast_share` | float | Share of public kWh at DC fast charging (0–1). For `charging_rate` consistency. |
| `energy.cohort_dc_fast_share` | float | Cohort median DC fast share. |
| `energy.vehicle_kwh_per_100mi` | float | Vehicle efficiency (kWh/100 miles). For `efficiency` and `driver_coaching` formula. |
| `energy.cohort_kwh_per_100mi` | float | Cohort mean efficiency (kWh/100 miles). |
| `energy.mean_price_per_kwh` | float | Vehicle's mean price per kWh charged (for charging_rate consistency). |
| `energy.cohort_mean_price_per_kwh` | float | Cohort's mean price per kWh charged (for charging_rate consistency). |
| `energy.cohort_depot_off_peak_price_per_kwh` | float | Cohort median depot off-peak tariff price per kWh (fallback when vehicle's own off-peak price is unavailable). |
| `maintenance.max_repeat_count` | int | Highest count of any one `service_type` in the month. For `repeat_repair` cause consistency. |
| `cost.diagnosis_source` | str | Source of the diagnosis: `"model"` (Bedrock returned a valid record_diagnosis) or `"rules"` (fallback after validation failed or model error). |
| **Energy outlier keys (present only when `cost.flag` is `"energy"` or `"both"`; decisions 2026-10-05):** | | |
| `cost.energy_ratio_own` | float | Energy cost per mile / vehicle's own energy CPM baseline (6-month mean). |
| `cost.energy_ratio_cohort` | float | Energy cost per mile / cohort's energy CPM baseline (same-month median). |
| `cost.energy_excess_usd` | float | Energy cost above both baselines at the vehicle's miles. Formula: `vehicle.energy_cost − GREATEST(own_energy_baseline, cohort_energy_baseline) × vehicle.miles`. Equivalently `miles × LEAST(cpm_energy − own, cpm_energy − cohort)`: the energy spend above the higher of the two baselines, so above both. |
| `cost.energy_method` | str | Baseline description for energy (e.g. "own 6-month mean and same-month fleet-model median; energy outlier when both ratios >= 1.10 and excess >= $20.00"). Includes thresholds. |

**Ratio fields** (per ADP view and decisions 2026-10-04):
- `ratio_cohort` = `cost_per_mile / cohort_median_cpm` (used for severity bands and D11 skip logic)
- `ratio_own` = `cost_per_mile / own_mean_cpm` (included in evidence for transparency)

---

### 1.2 `fleet.cost.sell_timing` — vehicle crossover month approaching

**When written**: vehicle enrolled in a managed fleet AND its crossover is still ahead: the fitted maintenance in the data month is below monthly depreciation, and `compute_crossover_month()` puts the crossover in the coming 1–3 months (spec D10 as amended 2026-10-07, `SELL_TIMING_HORIZON_MONTHS=3`). A vehicle already at or past the crossover in the data month gets no Finding (`crossed_by_data_month()`).

**Scope**: one vehicle.

**`subject_key`**: the data month, format `YYYY-MM`, same as `fleet.cost.anomaly` (decisions 2026-10-04 item 6; so each month produces its own decision item).

**`severity`**: always `routine`.

**`location`**: absent.

**`recommended_action`** (always present):
```json
{
  "action_kind": "apply_cost_action",
  "target_system": "cms",
  "parameters": {
    "cost_action": "reassign_or_remarket",
    "vehicle_vin": "<from Finding scope>",
    "month": "<YYYY-MM>",
    "cause": "low_utilisation"
  }
}
```

**Expiry**: 35 days.

**Evidence entries** (written by `agents/fi_cost/sell_timing.py`):

| Field | Type | Description |
|---|---|---|
| `cost.month` | str | Data month, format `YYYY-MM`. |
| `lifecycle.crossover_month` | str | Crossover month, format `YYYY-MM` (when rising maintenance exceeds declining residual value). |
| `lifecycle.purchase_price` | float | Vehicle purchase price from `vehicle_pricing`. |
| `lifecycle.monthly_depreciation` | float | Monthly depreciation rate used in the calculation. |

---

## 2. Evidence keys and usage

**Evidence entries are listed per Finding kind in § 1.** Their allowlist for the browser is defined in Stage A contract [`docs/fi-action-queue-contract.md`](fi-action-queue-contract.md) § 7: evidence `kind`, `source`, `value`, `computed_at`, `provenance`, and `vin`. Those six fields are returned to the browser; no other evidence fields, and no pattern mechanism. Cost evidence values (the numeric deltas and ratios) are shown on the vehicle page, capped per that section.

**Per-category deltas** (decisions 2026-10-04 "Group 1 findings" item 1):
- `delta_<cat>_own = cost_per_mile_<cat> − own_mean_cpm_<cat>` (vehicle's own 6-month mean)
- `delta_<cat>_cohort = cost_per_mile_<cat> − cohort_median_cpm_<cat>` (same-fleet-model-month exact median)

**Dollar equivalents** (decisions cycle 3, item 1):
- `delta.<cat>_usd = delta_<cat>_cohort × cost.miles`
- Computed by `evidence.py` postprocessor before model call and Bedrock usage

**Provenance** (spec D8): all cost evidence entries carry `provenance: live` (data read live from ADP views, not generated or simulated). CMS screens map this to the existing provenance value used for ADP sources (identity managed by `adp_source.py`).

---

## 3. `apply_cost_action` parameters

**Schema** (spec D9, decisions 2026-10-04 "Architect calls from review cycle 1" item 6):

```json
{
  "cost_action": "<required; one of: shift_to_depot_off_peak, schedule_inspection, reassign_or_remarket, driver_coaching>",
  "vehicle_vin": "<required; string; set by AVX from Finding scope; body may omit or match>",
  "month": "<required; YYYY-MM; the data month from Finding evidence; set by AVX; body may omit or match>",
  "cause": "<required; string; one of: charging_rate, charging_mix, repeat_repair, low_utilisation, efficiency, unclear; may be overridden by approver to a different value in the same closed list>",
  "finding_id": "<required; string; the AVX Finding ID; set by AVX; body must omit or match>"
}
```

**Execution route** (Stage A contract § 4, target = `cms`):
```
POST /api/v1/fleet-intelligence/cost-actions
Authorization: Bearer <operator's Cognito ID token>
Content-Type: application/json

{
  "cost_action": "shift_to_depot_off_peak",
  "vehicle_vin": "VIN...",
  "month": "2026-10",
  "cause": "charging_mix"
}
```

**Response on success** (200):
- Cost-action record written (see § 4 below)
- Body: `{"record_id": "<str>"}`

**Failure responses**:
- `400` — `cost_action` not in the allowed enum, `month` does not match `^\d{4}-(0[1-9]|1[0-2])$`, `vehicle_vin` empty, `cause` empty/missing or not in the allowed enum, or `finding_id` empty/missing
- `403` — caller lacks standing (not fleet-operator on this fleet or not platform-admin), OR vehicle VIN does not resolve to a fleet (fail-closed, not a VIN-existence oracle)
- `409` — same `finding_id` already decided with different parameters; response body: `{"error": "<message>"}`

**Idempotency**: `finding_id` is the idempotency key. A repeat POST with the same `finding_id` returns the stored record (200, `record_id` unchanged); a repeat POST with different `cost_action`, `cause`, `vehicle_vin`, or `month` gets 409.

---

## 4. CMS cost-action record

**Table**: `cms-{stage}-storage-cost-actions` (DynamoDB, created by Group 6 task 6.2)

**Partition key**: `vin` (string)  
**Sort key**: `record_id` (string; derived from `finding_id` by CMS task 6.2)

**Required fields** (defined by CMS task 1.6's tests):

| Field | Type | Description |
|---|---|---|
| `vin` | str | Vehicle VIN. Partition key. |
| `record_id` | str | Derived from `finding_id` by CMS; returned in all responses. Sort key. |
| `finding_id` | str | AVX Finding ID this action was approved for. Used for idempotency comparison. |
| `fleet_id` | str | Fleet the vehicle belongs to at approval time. Used for audit trail. |
| `action` | str | The approved action: one of `shift_to_depot_off_peak`, `schedule_inspection`, `reassign_or_remarket`, `driver_coaching`. |
| `cause` | str | The agent's stated cause for the recommendation (one of the six from the D9 table), or an override to a different value in that list. |
| `month` | str | Data month, format `YYYY-MM`. |
| `approver_sub` | str | The verified Cognito token `sub` claim of the approving operator (never `email`; see decisions 2026-10-04 "Group 1 findings" item 5). |
| `created_at` | str | ISO 8601 timestamp when the record was first created (UTC). |

**Immutability** (contract § 4): once written with a decision (`finding_id` present), the record cannot be updated via `PATCH /actions/{id}`. Only external systems (CMS vehicle operations, depot scheduling) may update execution state through separate routes (not this API).

**Audit trail**: Every write (initial approval or idempotent repeat) logs the approver, timestamp, and action kind to CloudWatch Logs via `PUT_ITEM` logging (no separate audit table).

---

## 5. Deterministic seams and how they're preserved

**Spec Tier item 4** lists four deterministic seams in the cost stage. All four are in AVX and data-driven:

1. **Estimated saving** (D7 formula, verified by test):
   - Computed in `agents/fi_cost/savings.py` from evidence values only
   - `Finding.recommended_action.estimated_value_usd` must equal the formula result
   - Test mutant: wire model output to the value; mutant must fail

2. **Severity** (decisions 2026-10-04 "Architect calls" item 2):
   - Source: ADP `vehicle_cost_baseline.severity`, derived from `ratio_cohort` and parameters table
   - AVX copies `evidence["severity"]` from the row
   - AVX has no band table; no recomputation
   - Test mutant: override evidence severity to disagree with ADP defaults; assert AVX severity matches evidence, not recomputed

3. **Outlier and energy-outlier flags** (decisions 2026-10-04 "Architect calls" item 4):
   - Source: ADP view columns `is_outlier`, `is_energy_outlier` (deterministic rules on baseline ratios)
   - Agent selects vehicles where either flag is true
   - Evidence `cost.flag` derived from flags: `"total"` / `"energy"` / `"both"`
   - Test mutant: feed model a row with false flags; assert the Finding does not name an outlier

4. **Execution** (contract § 4):
   - All cost actions require `operator_required: true` and `auto_approvable: false`
   - CMS records the decision in `cms-{stage}-storage-cost-actions`
   - Changing/approving/overriding via operator token only (no automation)

---

## 6. Related documents

- **[fi-action-queue-contract.md](fi-action-queue-contract.md)** — defines the queue item status transitions, approve/decline routes, and CMS's view of all FI Findings
  - § 4: `apply_cost_action` as an executable action kind (Stage B)
  - § 10a: failure vocabulary for declined/failed cost actions

- **[identity-and-standing-contract.md](identity-and-standing-contract.md)** — operator standing and Cognito token claims

- **[VSA_API_CONTRACT.md](VSA_API_CONTRACT.md)** — the v2 Finding and Action endpoint shape (cost Findings are v2.5 additions)

---

## 7. Queue display and evidence visibility

**On the Cost Recommendations screen**:
- Each cost Finding (if `recommended_action` is present) appears as one queue item
- The item shows the data month (evidence `cost.month`; `subject_key` itself is not returned), severity, action, the estimated saving and its basis, and an evidence preview
- Status comes from the decision Action (contract § 3)
- On `executed`: show `created_at` timestamp
- On `declined` / `refused` / `failed`: show `failure_reason` from the audit entry (contract § 10a)

**Evidence visibility** (per Stage A contract [`fi-action-queue-contract.md`](fi-action-queue-contract.md) § 7):
- Operator sees only evidence `kind`, `source`, `value`, `computed_at`, `provenance`, and `vin`
- No pattern mechanism; no `subject_key`, `inputs_hash`, `tokens_used`, or `cost_usd`
- The `cost.energy_method` and `cost.method` strings are **displayed as-is** to the operator; keep them concise and clear

---

## 8. Notes for Group 6 (CMS implementation)

- The cost-action route is behind the existing Cognito authorizer; operator standing comes from `custom:fleetIds` only (see `_lib/avx_scope.py`)
- `approver_sub` comes from the verified token's `claims["sub"]` — never a body field (decisions 2026-10-04 item 5)
- The idempotency key is `finding_id` alone; the record derivation is CMS-internal (defined by task 1.6's tests)
- Test suite must verify idempotency: two POSTs with the same `finding_id` return the same `record_id`
- **No cost action carries a safety text.** All cost recommendations are informational + financial
- CMS must reject any approver-like field in the request body; the decision-maker comes from the verified token only (decisions 2026-10-04 item 5)

