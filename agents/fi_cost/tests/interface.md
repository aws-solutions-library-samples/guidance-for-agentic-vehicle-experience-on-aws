# agents/fi_cost — interface contract for Group 3

**Written by task 1.4 (red-test phase). Group 3 implements against this.**  
Spec: `.kiro/specs/2026-10-02-fi-cost-stage-b/spec.md`

---

## Modules and their public symbols

### `agents/fi_cost/savings.py`

```python
def estimate_saving(
    action: str,                # one of D7's four action kinds, or "no_action"
    inputs: dict[str, float],   # action-specific evidence values (see D7 table)
) -> dict[str, Any] | None:
    """Compute estimated_value_usd and estimated_value_basis for a D7 action.

    Returns {"estimated_value_usd": float, "estimated_value_basis": str}
    or None when action == "no_action".

    Formulas (spec D7):
      shift_to_depot_off_peak  public_and_peak_kwh * (mean_public_peak_price - depot_off_peak_price)
      schedule_inspection      vehicle_maintenance_cost - cohort_baseline_maintenance_cost
      reassign_or_remarket     max(0, (lease + insurance) * (1 - miles / fleet_median_miles))
      driver_coaching          energy_cost * (1 - cohort_kwh_per_100mi / vehicle_kwh_per_100mi)
    """
```

**inputs keys by action** (see `fixtures/savings_golden.json`):

| Action | Required input keys |
|---|---|
| `shift_to_depot_off_peak` | `public_and_peak_kwh`, `mean_public_peak_price_per_kwh`, `depot_off_peak_price_per_kwh`, `month` |
| `schedule_inspection` | `vehicle_maintenance_cost`, `cohort_baseline_maintenance_cost`, `month` |
| `reassign_or_remarket` | `lease_cost`, `insurance_cost`, `vehicle_miles`, `fleet_median_miles`, `month` |
| `driver_coaching` | `vehicle_energy_cost`, `cohort_kwh_per_100mi`, `vehicle_kwh_per_100mi`, `month` |

---

### `agents/fi_cost/evidence.py`

```python
def compute_inputs_hash(evidence: dict[str, Any]) -> str:
    """sha256 of the canonical JSON of the evidence rows.

    Canonical = sorted keys, fixed number formatting.
    Must be called BEFORE the Bedrock call.
    Narration fields (headline, detail) must not affect the hash.

    Returns a 64-character hex string.
    """
```

```python
def gather_evidence(
    selected_vins: list[str],
    year_month: str,
    athena_client: Any,
    workgroup: str,
    results_s3_path: str,
) -> list[dict[str, Any]]:
    """One Athena query for ALL selected VINs — D6 step 2 (decisions item 5).

    D6 step 2 fetches evidence for all selected VINs in a SINGLE query, not one
    query per VIN.  With D13's 79–354 s Athena queue time, 50 sequential queries
    cannot fit the 15-minute Lambda budget.

    Delegates Athena mechanics to agents.tier2.adp_source.run_query.
    Returns a list of flat evidence dicts, one per VIN, each containing the
    evidence keys defined in spec D8 (see the Evidence keys table below).
    """
```

---

### `agents/fi_cost/selection.py`

```python
def select_anomalies(
    athena_client: Any,
    workgroup: str,
    results_s3_path: str,
    max_diagnoses_per_run: int,
    latest_complete_month: str,   # "YYYY-MM"
) -> list[dict[str, str]]:
    """Athena query on vehicle_cost_baseline: managed vehicles with is_outlier.

    Ordered by excess cost, capped at max_diagnoses_per_run.
    Paginates GetQueryResults via NextToken until exhausted (NEVER stops early).

    Each row: {"vin": str, "ratio_cohort": str, "fleet_id": str, ...}
    """
```

---

### `agents/fi_cost/diagnose.py`

```python
RECORD_DIAGNOSIS_TOOL_SCHEMA: dict[str, Any]
# The Bedrock Converse toolSpec for record_diagnosis.
# Shape: {"toolSpec": {"name": "record_diagnosis", "description": ...,
#           "inputSchema": {"json": {...}}}}
# Tests must handle both the bare {"name": ..., "inputSchema": ...} form and the
# {"toolSpec": {...}} wrapper form; extract the inner schema before checking properties.
# Input schema properties (exactly six, per D6): cause (enum), action (enum),
#   evidence_refs (list[str]), headline (str <=120), detail (str <=600), confidence (enum).
# MUST have no numeric/integer field in any property, any list-type, anyOf/oneOf, or items
# (spec Tier item 4).

PLAYBOOK_VERSION: str   # "1.0.0" — read from playbook.md header "## version: X.Y.Z"
AGENT_VERSION: str      # "agent-fi-cost/playbook:<version>" — used in Finding.agent_version

def run_cost_diagnosis(
    evidence: dict[str, Any],
    model_reply: dict[str, Any],
) -> dict[str, Any]:
    """Given validated model reply + evidence, produce the Finding dict.

    Deterministic seams (decisions item 2):
      - finding["scope"]["vin"]  ← evidence["vin"]  (never model_reply)
      - finding["severity"]      ← evidence["severity"]  (from ADP row; decisions item 2;
                                    never model_reply — AVX keeps no band table)
      - finding["recommended_action"]["estimated_value_usd"]
                                 ← estimate_saving(action, evidence)  (never model_reply)
      - finding["recommended_action"]["parameters"]["vehicle_vin"]
                                 ← evidence["vin"]  (D9: AVX sets it from scope)

    Returns a Finding dict ready for write_finding().
    """
```

> **Decisions item 2 (severity source)**: `vehicle_cost_baseline` emits `severity` from the
> ADP parameters table (`severity_attention_ratio=2.0`, `severity_soon_ratio=1.5`).  The agent
> copies `evidence["severity"]` from the selected row.  **AVX keeps no band table.**
> Tests must include a case where `evidence["severity"]` disagrees with the D8 default bands
> (e.g. `ratio_cohort=1.6`, `severity="attention"` as if `severity_attention_ratio` was lowered
> to 1.5) to verify that AVX never recomputes severity from `ratio_cohort`.

---

### `agents/fi_cost/validate.py`

```python
def validate_tool_input(
    reply: dict[str, Any],
    evidence: dict[str, Any],
) -> tuple[bool, str]:
    """Validate record_diagnosis tool input against the schema and evidence.

    Checks (in order):
      1. cause is in VALID_CAUSES.
      2. Energy-only restriction (decisions 2026-10-05 item 6): if
         evidence["cost.flag"] == "energy", cause must be in
         _ENERGY_ONLY_CAUSES = {"charging_rate", "charging_mix", "efficiency", "unclear"}.
         Mutation target: allow repeat_repair for energy-only → test_energy_only_outlier_
         rejects_repeat_repair fails.
      3. action is in VALID_ACTIONS.
      4. (cause, action) pair is in the allowed table (decisions item 1).
      5. each evidence_refs key exists in evidence.
      6. cause is consistent with the numbers in evidence
         (e.g. charging_rate requires mean price above cohort AND peak or DC-fast share
         above cohort; charging_mix requires public share above cohort).

    Returns (True, "") on success or (False, error_message) on the first failure.
    """

VALID_CAUSES: frozenset[str]
# {"charging_rate", "charging_mix", "repeat_repair", "low_utilisation", "efficiency", "unclear"}

VALID_ACTIONS: frozenset[str]
# {"shift_to_depot_off_peak", "schedule_inspection", "reassign_or_remarket",
#  "driver_coaching", "no_action"}

# Energy-only causes (decisions 2026-10-05 item 6):
# _ENERGY_ONLY_CAUSES = {"charging_rate", "charging_mix", "efficiency", "unclear"}
```

---

### `agents/fi_cost/fallback.py`

```python
def rules_fallback(evidence: dict[str, Any]) -> dict[str, Any]:
    """Deterministic fallback when validation fails after one retry (D6 step 5).

    Rules (decisions item 1 and 2026-10-05 item 6):
      - Energy-only outlier (evidence["cost.flag"] == "energy"):
        Regardless of which dollar delta is largest, pick the energy sub-cause
        with the highest relative signal (charging_rate, charging_mix, efficiency)
        or unclear.  Mutation target: allow repeat_repair here →
        test_energy_only_fallback_uses_energy_cause_regardless_of_maintenance_delta
        fails.
      - General case: cause = category with the largest positive delta.*_usd value;
        energy ties broken by whichever of charging_rate/charging_mix/efficiency has
        the largest relative signal (v/cohort − 1); no positive delta → "unclear"
      - action = first allowed action for that cause (per the cause→action table)
      - confidence = "low"
      - narration = fixed text (no model)
      - evidence entry: {kind: "cost.diagnosis_source", value: "rules"}

    Returns a partial reply dict (same shape as a model reply, not a Finding).
    `run_cost_diagnosis` converts it to a Finding with `recommended_action.parameters.cause`
    set from this dict's `cause` field.
    """
```

---

### `agents/fi_cost/sell_timing.py`

```python
SELL_TIMING_HORIZON_MONTHS: int  # = 3 (spec D10; must equal fixture's _horizon_months)

def compute_crossover_month(
    purchase_price: float,
    maintenance_series: list[dict[str, Any]],  # [{"yearMonth": "YYYY-MM", "maintenanceCost": float}]
    horizon_months: int,
) -> dict[str, Any]:
    """Port of CMS lifecycle.analyze_sell_timing, trimmed to the fields this stage needs.

    Returns:
      {
        "crossover_month": str | None,    # "YYYY-MM" or None
        "r_squared_meaningful": bool,
        "series_length": int,
        "maintenance_fit": {"slope": float, "intercept": float},
        "depreciation_params": {"purchase_price": float, "monthly_depreciation": float, "life_months": int},
      }

    Output must match fixtures/crossover_golden.json vector-for-vector.
    """

def should_write_sell_timing_finding(
    crossover_month: str | None,
    series_end_month: str,           # "YYYY-MM"
    horizon_months: int,
    *,
    already_crossed: bool,           # crossed_by_data_month(result); required
) -> bool:
    """True when the crossover is still ahead (already_crossed is False) and within
    horizon_months of series_end_month (amended 2026-10-07, option (b))."""

def crossed_by_data_month(result: dict[str, Any]) -> bool:
    """Fitted maintenance at the data month (index n - 1) >= monthly depreciation."""

def build_sell_timing_finding(
    vin: str,
    fleet_id: str,
    crossover_month: str,
    purchase_price: float,
    monthly_depreciation: float,
    year_month: str,
) -> dict[str, Any]:
    """Build a fleet.cost.sell_timing Finding dict (contract § 1.1, D10).

    recommended_action.parameters.cost_action = "reassign_or_remarket"
    No model call; narration is fixed text per U3.
    """
```

---

### `agents/fi_cost/agent.py`

```python
def process_vehicle(
    evidence: dict[str, Any],
    live_finding: dict[str, Any] | None,
    bedrock_client: Any,             # boto3 bedrock-runtime client or stub
    write_finding: Callable,         # write_finding(finding, **kw) from agents/tier2/writer.py
) -> dict[str, Any]:
    """D6 steps 3–6 for one vehicle.

    Steps:
      3. Compute inputs_hash. If live_finding has the same hash → return {"duplicates_skipped": 1}
      4. Call Bedrock Converse with record_diagnosis tool.
      5. Validate; run `bedrock_client.apply_guardrail(source='OUTPUT', content=[headline, detail])`
         on the validated reply's `headline` and `detail` (decisions security review cycle 1,
         item 1).  `GUARDRAIL_INTERVENED` on either field is treated like a failed validation:
         one retry, then `rules_fallback`.  Model text that the guardrail blocked never reaches
         `write_finding()`.
         Guardrail configuration read from environment variables at call time:
           FI_COST_GUARDRAIL_ID      — guardrail identifier (ID string or full ARN)
           FI_COST_GUARDRAIL_VERSION — version string ("DRAFT" or "1", "2", …)
      6. `evidence["severity"]` + `estimate_saving` + `write_finding()`.  **No band table in AVX**
         (decisions item 2): severity comes from the evidence row, not recomputed here.

    Returns run stats: {"duplicates_skipped": int, "written": int, "tokens_used": int}
    """
```

---

## Fixture files (Group 3 must not modify)

| File | Pinned by |
|---|---|
| `fixtures/savings_golden.json` | shared with CMS task 6.4 (both must agree on rounding) |
| `fixtures/crossover_golden.json` | sha256 `ef28de7aa759706ece8f83a9904abb95d747d0e501ca43e836f3c4f16e6b68f0` (test_sell_timing.py line `_PINNED_SHA256`) |

---

## Evidence keys referenced in tests

From spec D8 evidence entries:

| Key | Type | Description |
|---|---|---|
| `vin` | str | Vehicle VIN |
| `fleet_id` | str | Fleet identifier from CMS registry |
| `year_month` | str | "YYYY-MM" of the latest complete month |
| `ratio_cohort` | float | cost_per_mile / cohort_median (D3) |
| `ratio_own` | float | cost_per_mile / own_mean (D3) |
| `severity` | str | ADP-provided severity ("attention"/"soon"/"routine"); agent copies this — no band table in AVX (decisions item 2) |
| `cost.month` | str | Data month string |
| `cost.cost_per_mile` | float | |
| `cost.own_baseline` | float | |
| `cost.cohort_baseline` | float | |
| `cost.method` | str | Verbatim from the ADP view (D3) |
| `energy.public_and_peak_kwh` | float | |
| `energy.mean_public_peak_price_per_kwh` | float | |
| `energy.depot_off_peak_price_per_kwh` | float | |
| `energy.public_share` | float | Share of kWh charged at public stations (charging_mix consistency) |
| `energy.cohort_public_share` | float | Cohort median of the same |
| `energy.peak_share` | float | Share of depot kWh charged at the peak tariff (charging_rate consistency) |
| `energy.cohort_peak_share` | float | Cohort median of the same |
| `energy.dc_fast_share` | float | Share of public kWh charged at DC fast (charging_rate consistency) |
| `energy.cohort_dc_fast_share` | float | Cohort median of the same |
| `energy.mean_price_per_kwh` | float | Vehicle's mean price per kWh charged (for charging_rate consistency) |
| `energy.cohort_mean_price_per_kwh` | float | Cohort's mean price per kWh charged (for charging_rate consistency) |
| `energy.vehicle_kwh_per_100mi` | float | Vehicle efficiency kWh/100 mi (for efficiency consistency and driver_coaching formula) |
| `energy.cohort_kwh_per_100mi` | float | Cohort mean kWh/100 mi |
| `cost.maintenance` | float | Vehicle's maintenance cost in month |
| `cost.lease` | float | |
| `cost.insurance` | float | |
| `cost.miles` | float | |
| `cost.fleet_median_miles` | float | |
| `cost.energy` | float | Monthly energy cost in USD (ADP view's `energy_cost` column; decisions cycle 3 item 1) |
| `delta.maintenance` | float | Maintenance per-mile delta from ADP view: `delta_maintenance_cohort` ($/mi); used with `cost.miles` to compute `delta.maintenance_usd` |
| `delta.energy` | float | Energy per-mile delta from ADP view ($/mi) |
| `delta.lease` | float | Lease per-mile delta from ADP view ($/mi) |
| `delta.insurance` | float | Insurance per-mile delta from ADP view ($/mi) |
| `delta.maintenance_usd` | float | Dollar delta: `delta.maintenance × cost.miles`; D7 formula and fallback use this |
| `delta.energy_usd` | float | Dollar delta: `delta.energy × cost.miles`; fallback category-selection uses this |
| `delta.lease_usd` | float | Dollar delta: `delta.lease × cost.miles`; fallback category-selection uses this |
| `delta.insurance_usd` | float | Dollar delta: `delta.insurance × cost.miles`; fallback category-selection uses this |
| `maintenance.max_repeat_count` | int | Maximum repeat count for any service_type in the window (for repeat_repair cause consistency and fallback) |

### Evidence keys by D7 formula (decisions item 5)

Each D7 saving formula maps to these evidence keys via `_inputs_for(action, evidence)`:

| Action | Evidence key → `estimate_saving` input key |
|---|---|
| `shift_to_depot_off_peak` | `energy.public_and_peak_kwh` → `public_and_peak_kwh` |
| | `energy.mean_public_peak_price_per_kwh` → `mean_public_peak_price_per_kwh` |
| | `energy.depot_off_peak_price_per_kwh` → `depot_off_peak_price_per_kwh` |
| | `cost.month` (or `year_month`) → `month` |
| `schedule_inspection` | `cost.maintenance` → `vehicle_maintenance_cost` |
| | `cost.maintenance − delta.maintenance_usd` → `cohort_baseline_maintenance_cost` |
| | `cost.month` → `month` |
| `reassign_or_remarket` | `cost.lease` → `lease_cost` |
| | `cost.insurance` → `insurance_cost` |
| | `cost.miles` → `vehicle_miles` |
| | `cost.fleet_median_miles` → `fleet_median_miles` |
| | `cost.month` → `month` |
| `driver_coaching` | `cost.energy` → `vehicle_energy_cost` |
| | `energy.cohort_kwh_per_100mi` → `cohort_kwh_per_100mi` |
| | `energy.vehicle_kwh_per_100mi` → `vehicle_kwh_per_100mi` |
| | `cost.month` → `month` |

---

## Cause → action table (decisions item 1)

Allowed actions per cause (the first is the rules-fallback's choice):

| Cause | Allowed actions | Numbers that must support the cause |
|---|---|---|
| `charging_rate` | `shift_to_depot_off_peak`, `no_action` | `energy.mean_price_per_kwh > energy.cohort_mean_price_per_kwh` AND (`energy.peak_share > energy.cohort_peak_share` OR `energy.dc_fast_share > energy.cohort_dc_fast_share`) |
| `charging_mix` | `shift_to_depot_off_peak`, `no_action` | public share of kWh above cohort (`energy.public_share > energy.cohort_public_share`) |
| `repeat_repair` | `schedule_inspection`, `reassign_or_remarket`, `no_action` | 2+ events of one `service_type` in window; maintenance above cohort |
| `low_utilisation` | `reassign_or_remarket`, `no_action` | `cost.miles` below fleet median |
| `efficiency` | `driver_coaching`, `schedule_inspection`, `no_action` | kWh/100 mi above cohort |
| `unclear` | `no_action` | — |

**Fallback cause selection** (by largest positive dollar delta, `delta.*_usd`): energy → `charging_rate` / `charging_mix` / `efficiency` (whichever relative signal `v/cohort − 1` is largest, using `energy.mean_price_per_kwh`, `energy.public_share`, `energy.vehicle_kwh_per_100mi` respectively); maintenance → `repeat_repair` if `maintenance.max_repeat_count >= 2`, else `unclear`; lease/insurance → `low_utilisation` if miles below fleet median, else `unclear`; no positive dollar delta → `unclear`.

---

## Decisions (ambiguities resolved in this task)

1. **`run_cost_diagnosis` signature**: the spec describes the overall D6 flow but does
   not name the sub-function. Interface.md splits it into `run_cost_diagnosis(evidence, model_reply)`
   (receives already-validated reply) and `process_vehicle()` (orchestrates steps 3–6
   including the Bedrock call and validation). Group 3 may rename or merge if the spec's
   flow makes a different split cleaner, provided all six test items still pass.

2. **`compute_inputs_hash` narration exclusion**: the spec says narration fields must not
   affect the hash (D11 "The model's narration is not hashed"). The interface uses a
   convention: any evidence key starting with `_` is excluded. Group 3 may use a different
   mechanism (e.g. an explicit field allowlist) as long as the test passes.

3. **Severity source (decisions item 2)**: Severity has one source: ADP.
   `vehicle_cost_baseline` emits `severity` from the parameters table
   (`severity_attention_ratio=2.0`, `severity_soon_ratio=1.5`).  The agent copies
   `evidence["severity"]` from the ADP row.  **AVX keeps no band table; `band_table_severity`
   is not a module in this package.**  Tests must include at least one case where
   `evidence["severity"]` disagrees with the D8 default bands to verify the agent does not
   recompute severity from `ratio_cohort`.

4. **`fallback.rules_fallback` cause-to-action mapping (decisions item 1)**: spec D9 /
   decisions item 1 defines the cause→action table.  Tests assert
   `cause == "repeat_repair"` when maintenance is the largest delta AND
   `maintenance.max_repeat_count >= 2` (decisions item 1 says "maintenance →
   `repeat_repair` if a service type repeats, else `unclear`").
   (`"schedule_inspection"` is an action, not a cause; earlier tests
   incorrectly included it in the assertion.)  When count < 2, tests assert
   `cause == "unclear"` exactly.  Fix Group 3 W1 tightened the assertion from
   `cause in ("repeat_repair", "unclear")` to `cause == "repeat_repair"` for
   the count >= 2 case so that C3-6 (always "unclear") fails.

5. **`crossover_golden.json` provenance**: lifecycle.py requires provenance in
   `{"derived", "measured", "reference", "simulated"}`. The fixture was generated with
   `provenance: "simulated"`. The ported `compute_crossover_month` in sell_timing.py
   does not check provenance (it takes plain dicts without provenance keys), consistent
   with "one arithmetic" in D10.
