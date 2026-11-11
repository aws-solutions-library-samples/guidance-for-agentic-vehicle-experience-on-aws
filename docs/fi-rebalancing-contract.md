# FI Rebalancing Contract — Stage E

**Spec**: `.kiro/specs/2026-10-06-fi-rebalancing-stage-e/` decisions D8–D14  
**Additive to**: [`docs/fi-action-queue-contract.md`](fi-action-queue-contract.md) (§ 4 `move_vehicles` row, edited in task 4.4 on top of Stage D's v2.7; § 6's first limited kind and the CMS service identity, Group 8)  
**Stage**: E (FI Rebalancing). Older documents and Stage C D15 call this "Stage D".

This document covers only what is specific to the rebalancing agent. For the queue contract, the
approve route, server-set parameters, failure reasons, and fleet isolation, see
`docs/fi-action-queue-contract.md`.

---

## 1. Agent identity

| Field | Value |
|---|---|
| `agent_id` | `agent-fi-rebalancing` |
| Component | `rebalance` |
| Run record key | `agent-fi-rebalancing#rebalance` |
| Schedule | EventBridge Scheduler, daily 06:00 UTC (after ADP jobs at 04:15 and 04:45) |
| Lambda | Tier 2 Lambda (same function, different `component` input) |
| Staleness alarm | `vsa-{stage}-avx-run-stale-rebalance-<region>` (notifies `vsa-{stage}-tier2-operator-alerts`) |
| Broker skip | `agent-fi-*` Findings are skipped by the push broker (Stage C D13); move and gap Findings do not reach mobile push |

### 1a. What the agent reads (task 4.1; `group4-design.md` G4-D1)

AVX holds no `cms_ingest` grant (user rule, 2026-10-09). ADP: `supply_demand_gaps_snapshot`,
`move_outcomes_snapshot` (newest `materialised_on`), `fleet_bookings_forecast` (latest
`run_date`), `vehicle_days`, `vehicle_utilization_daily`, `booking_requests`, `metro_events`, and
`service_records`; DMS: `repair_orders` (latest image per RO). CMS DynamoDB: `fleet-locations`
(metro, coordinates, type, parking, operating hours), the vehicles `vin-index` (VIN →
`vehicleId`) and `vehicle-groundings`. AVX: the findings table's `by_vin` index. Every Athena
read pages `NextToken` to the end. A grounding that can't be read, a VIN without exactly one
`vehicleId`, or any other unknown makes the vehicle ineligible (U7's safe direction).

---

## 2. Finding kinds

Two kinds, both fleet-scoped (`scope.fleet_id`):

### 2.1 `fleet.rebalance.move`

A recommended move between two locations in the same metro.

| Field | Shape |
|---|---|
| `finding_kind` | `"fleet.rebalance.move"` |
| `scope.fleet_id` | fleet id (rental fleet only in this stage) |
| `location` | `{"from_location_id": "<id>", "to_location_id": "<id>"}` |
| `subject_key` | `"<from_location_id>/<to_location_id>/<ISO week of first deficit day>"` — a re-run updates the same item in place (contract §4 idempotency); next week's need is a new item |
| Fixed headline | The first of these within 120 characters (amended 2026-11-04, issue `2026-11-04-rebalancing-headline-location-ids`): (1) `"Move <n> vehicles from <from_name> to <to_name>: <to_name> short <k> vehicles from <day>"`; (2) `"Move <n> vehicles from <from_name> to <to_name>: short <k> vehicles from <day>"`; (3) `"Move <n> vehicles from <from_name> to <to_name>: short <k> vehicles"`; (4) form 1 with location ids. A count of 1 reads "1 vehicle". Every form keeps `from <from> to <to>: `, which CMS's Action Queue parses for its From and To lines. Set by the agent, not the model. The wording's version (`headline_format`) is part of `inputs_hash`, so a wording change rewrites standing items once |
| Severity | `attention` (turned-away share ≥ 30%), `soon` (≥ 15%), `routine` (< 15%); **never** `p0` |
| `confidence` | `high` (forecast MAPE ≤ 15%), `medium` (≤ 30%), `low` (> 30%); lowered one band when median actual-to-predicted ratio of last 3 executed moves into the destination is < 0.5 (spec D9, D12) |
| Expiry | 2 days, refreshed by each daily run while the gap persists |
| `inputs_hash` | Computed before any model call, over evidence rounded to suppress daily noise: forecast shortfalls rounded to whole vehicles, eligibility as yes/no |
| `recommended_action` | Present; see § 5 |

### 2.2 `fleet.rebalance.gap`

An informational item for a deficit with no candidate move, a model hold, or a surplus with
nowhere to send vehicles.

| Field | Shape |
|---|---|
| `finding_kind` | `"fleet.rebalance.gap"` |
| `scope.fleet_id` | fleet id |
| `location` | `{"location_id": "<id>"}` |
| `subject_key` | `"<location_id>/<ISO week>"` |
| Fixed headline | `"<location_name>: short <k> vehicles from <day>, no move recommended"`; surplus: `"<location_name>: <m> spare vehicles, no location in the metro is short"`. A count of 1 is singular ("1 vehicle", "1 spare vehicle"; amended 2026-11-04) |
| Severity | `attention` / `soon` / `routine` as above; **never** `p0` |
| `confidence` | Same formula as move items |
| Expiry | 2 days, refreshed per daily run |
| `recommended_action` | **Absent** (gap items are informational only) |

### 2.3 Refreshes and the weekly twin (Group 4 review cycle 2, accepted)

- **A refreshed item keeps what it was first written with.** While `inputs_hash` is unchanged a
  daily run only extends `expires_at` and sets the item's top-level `computed_at`; the headline,
  evidence (each entry carries its own `computed_at`, forecast days and `run_date`) and the
  recommendation's hours and turnaways stay as first written (net benefit is hashed to its $25
  bucket). Read evidence and figures as of the evidence's `computed_at`, not the item's. On a
  gap item the evidence VIN (the lowest VIN assigned at the location) is not hashed, so a refreshed
  gap can name a vehicle that has since left the location.
- **A standing deficit gets a second live item each Monday.** `forecast_week` in `subject_key` is
  the ISO week of the first deficit day, and for a standing deficit that day is `run_date + 1`, so
  the Monday run writes under the new week's key while last week's item stays live up to its
  2-day expiry. This is spec D10's key (one live item per from, to and week), kept as is. The twin
  can't move a vehicle twice: CMS refuses vehicles no longer at the origin (§ 8; a first-attempt
  409 is `refused`).
- **A location with no assigned vehicle writes no gap item** (the finding schema needs a `vin` on
  every fleet-scoped evidence entry). It is counted in the run record's
  `gap_items_without_vehicle` and logged with the location id.

---

## 3. `recommended_action` for move items (spec D11)

```json
{
  "action_kind": "move_vehicles",
  "target_system": "cms",
  "parameters": {
    "fleet_id": "<fleet_id>",
    "from_location_id": "<from_id>",
    "to_location_id": "<to_id>",
    "vehicle_vins": ["MRD-…", "…"],
    "forecast_week": "<ISO week string, e.g. 2026-W42>",
    "transfer_cost_usd": 420.50,
    "predicted_on_rent_hours_gain": 84.0,
    "predicted_turnaways_avoided": 3
  },
  "estimated_value_usd": 813.00,
  "estimated_value_basis": "forecast on-rent hours recovered at <to> less lost at <from>, at location-type rates, less transfer cost, 7 days from <date>"
}
```

---

## 4. `move_vehicles` approve parameters (spec D11; queue contract §4)

**Server-set fields** (the approve handler fills these from the Finding; the caller may not
override them):

| Parameter | Source |
|---|---|
| `fleet_id` | Finding top-level `fleet_id` |
| `from_location_id` | Finding `location.from_location_id` |
| `to_location_id` | Finding `location.to_location_id` |
| `forecast_week` | `rec.parameters.forecast_week` |
| `transfer_cost_usd` | `rec.parameters.transfer_cost_usd × scale` |
| `predicted_on_rent_hours_gain` | `rec.parameters.predicted_on_rent_hours_gain × scale` |
| `predicted_turnaways_avoided` | `rec.parameters.predicted_turnaways_avoided × scale` |

`scale = len(body_vins) / len(rec_vins)`.

**Comparison basis for the three scaled fields**: the server-set check compares the body value
against the **pre-scale recommendation value** (`rec.parameters.X`), not the post-scale result
(`rec.parameters.X × scale`). A body that echoes back the post-scale figure must be rejected.
The scaled output is what the handler computes and stores after the check passes; it is not the
comparison basis.

**`vehicle_vins` subset rule**: the operator may only drop vehicles from the recommendation's
list.

- Body `vehicle_vins` must be a **non-empty subset** of the recommendation's list → 400
  otherwise.
- If `body_vins == rec_vins`: `decision: approved`. An omitted `vehicle_vins` approves the
  recommendation's list; an explicit `[]`, a duplicate, a non-string or a VIN outside the list
  is 400 naming `vehicle_vins`.
- If `body_vins ⊊ rec_vins` (strict subset): `decision: overridden`; predicted figures and
  transfer cost scaled by `len(body_vins) / len(rec_vins)` (money and hours rounded to cents,
  turnaways floored); the stored list keeps the recommendation's order.
- The Finding's `scope.fleet_id` and `location` are the server values, and the
  recommendation must repeat them (400 otherwise), so a drifted recommendation can't move
  vehicles elsewhere.

**Policy row** (queue contract § 4 table):

| Kind | Target | Executable | `operator_required` | Auto-approvable |
|---|---|---|---|---|
| `move_vehicles` | `cms` | Yes (task 4.2) | Yes (`operator_required`; a rule decides only within its limit, § 10) | Yes, limited (Group 8, U6; the evaluator acts only when `FI_REBALANCING_AUTO_APPROVAL_LIVE` is on) |

---

## 5. Model tool schema (spec D8 step 6)

The agent sends one `record_rebalance_plan` tool call per metro with a candidate deficit.
The tool schema allows **only**:

```json
{
  "moves": [
    {
      "candidate_id": "<string — from the candidate bundle>",
      "vehicle_vins": ["<string>", "…"],
      "timing": "today | within_3_days",
      "detail": "<string, at most 600 characters>",
      "evidence_refs": ["<string>", "…"]
    }
  ],
  "holds": [
    {
      "gap_id": "<string>",
      "reason": "temporary_event | returns_expected | not_worth_cost | unclear",
      "detail": "<string, at most 300 characters>"
    }
  ]
}
```

**No numeric, headline, severity, confidence, or money field exists in the tool schema.**
All financial figures and confidence bands come from D9's formulas (§ 7 below). Bedrock
Guardrails apply to the `detail` fields.

**Validation** (before writing; deterministic):
- Every `candidate_id` must be in the bundle for that metro.
- Every `vehicle_vins` entry must be in that candidate's eligible list.
- A VIN may appear in at most one `moves` entry.
- After all moves in the metro, no source location's 7-day forecast utilization exceeds 85%.
- Net benefit recomputed for the chosen vehicles must be ≥ `min_net_benefit_usd`.
- A `temporary_event` hold must reference an announced event in that location's evidence for
  that week.
- One retry on validation failure; then the rules fallback (`diagnosis_source: rules`).

---

## 6. Evidence keys (spec D10; queue contract § 7 browser allowlist)

Every evidence entry carries `provenance: live`; CMS shows no "simulated" labels. Each
carries a `vin` (the schema requires one on every fleet-scoped entry): per-vehicle entries
their own VIN; location-level entries on a move the move's first VIN; on a gap item the first
VIN assigned at the location (a location with none gets no gap item). `rebalance.reservations`
is not written by the rules path (pending requests carry no VIN).

| Key | Contents |
|---|---|
| `rebalance.forecast` | Per day: forecast hours, turn-away rate, method string, 28-day backtest MAPE, `data_as_of` |
| `rebalance.supply` | Assigned vehicles, available count, exclusions (grounded, in-service, booked) |
| `rebalance.reservations` | Advance bookings already on the books for the forecast window |
| `rebalance.events` | Announced metro events in the forecast window, category and uplift band |
| `rebalance.transfer` | Great-circle distance (miles), road factor (1.35), transfer cost per vehicle |
| `rebalance.revenue` | Gain at destination, loss at source, rates per on-rent hour by location type, net benefit |
| `rebalance.vehicle` | One entry per VIN in `vehicle_vins`: days idle in last 7, next service in days, open lower-severity items; battery SoH band is `null` until a source is granted |
| `rebalance.recent_outcomes` | Last 3 executed moves into the destination: predicted vs actual `ratio_hours` (spec D12) |
| `rebalance.hold` | Present when the model chose to hold; reason and detail |
| `rebalance.diagnosis_source` | `rules` when the rules fallback ran; `model` otherwise |

---

## 7. Money and confidence formulas (spec D9)

All configuration lives in `agents/fi_rebalancing/formulas.py`, unit-tested with fixed inputs and
shared as fixtures with CMS display tests (Stage B D7 pattern).

### Transfer cost

```
distance_miles = great_circle_miles(from_lat, from_lon, to_lat, to_lon) × road_factor (1.35)
transfer_cost_per_vehicle = (2 × distance_miles ÷ urban_mph + handling_hours)
                            × driver_usd_per_hour
                            + distance_miles × vehicle_usd_per_mile
```

Default config: `urban_mph=22`, `handling_hours=0.25`, `driver_usd_per_hour=30`,
`vehicle_usd_per_mile=0.15`. Example: 12-mile move ≈ $42 per vehicle.

### Revenue impact (7 days from effective date)

```
destination_gain_per_day = min(forecast_unserved_hours, moved_vehicles × op_hours × 0.95)
                           × usd_per_on_rent_hour[to_location_type]
source_loss_per_day      = [unserved(F_src, S − m) − unserved(F_src, S)]
                           × usd_per_on_rent_hour[from_location_type]
                           where unserved(F, supply) = max(0, F − supply × op_hours × 0.95)
                                 F_src = source forecast hours per day
                                 S     = source supply (vehicles) before the move
                                 m     = moved_vehicles
                           (spec D9. A well-staged source is at or under 85 % utilisation
                            after the move — spare vehicles check — so unserved(F, S) = 0
                            and unserved(F, S − m) = 0, yielding $0 source loss on the
                            typical valid candidate. A hand-computed example: 10-vehicle,
                            72 h/day source losing 2 → D9 loss = $0.00/day; the superseded
                            "most-pessimistic" formula priced the same move at $43.20/day.)
revenue_impact = sum(gain − loss, 7 days)
net_benefit    = revenue_impact − transfer_cost
```

Rates: airport `$3.50/h`, downtown `$3.00/h`, carshare hub `$9.00/h`.  
`estimated_value_usd = net_benefit`.

### Confidence bands (D9, D12)

| Forecast MAPE (28-day, destination) | Initial band |
|---|---|
| ≤ 15% | `high` |
| ≤ 30% | `medium` |
| > 30% | `low` |

Then: if median `ratio_hours` of the last 3 executed moves into the destination is < 0.5,
lower one band (`high → medium`, `medium → low`, `low` stays `low`).

### Severity thresholds (U9 — configuration, not hard-coded)

Based on the destination's forecast turned-away share over 7 days:

| Turned-away share | Severity |
|---|---|
| ≥ 30% | `attention` |
| ≥ 15% | `soon` |
| < 15% | `routine` |

---

## 8. CMS route checks, in order (spec D11)

`POST /api/v1/fleet-intelligence/moves` (operator path; token from the operator's Cognito JWT):

1. **Fleet and VIN scope**: caller has `fleet-operator` standing on `fleet_id`; every VIN is in
   that fleet by the vehicle registry (never by the request body).
2. **Grounding check**: no VIN has an open grounding in `cms-{stage}-storage-vehicle-groundings`
   → 409 `"Vehicle <vin> is grounded and can't be moved."` (U7 is a hard stop regardless of
   approval path).
3. **Current location**: every VIN is still assigned to `from_location_id` → 409.
4. **Parking capacity**: destination's assigned count + move count ≤ `parkingSpaces` → 409.
5. **Atomic transaction**: conditional Put of the move record (keyed on Action id; replay →
   no-op via `attribute_not_exists(moveId)`) + conditional Update of each vehicle's `locationId`
   from `from` to `to`.

**Each check has a removal test** (spec D16, testing.md).

**AVX's request** (`CmsExecutor`, task 4.3): `Authorization: Bearer <operator token>`, redirects
never followed, body `{actionId, findingId, fleetId, fromLocationId, toLocationId,
vehicle_vins, forecastWeek, transferCostUsd, predictedOnRentHoursGain,
predictedTurnawaysAvoided}`. CMS reads the server-set values from the Action, not the body.
2xx → `executed`; 409 and other refusal 4xx → `refused`; 5xx after retries → `failed`; 3xx,
401, 403 and 404 → terminal error.

---

## 9. Move record shape (spec D11)

Written to `cms-{stage}-storage-vehicle-moves`:

```
moveId                     (pk sk — Action id, so a replay is idempotent)
fleetId                    (pk)
fromLocationId
toLocationId
vins[]
vehiclesRecommended        (count in the Finding's vehicle_vins)
vehiclesApproved           (count after any operator drop)
effectiveDate              (next UTC day: first day bookings count vehicles at destination)
predictedOnRentHoursGain   (scaled)
predictedTurnaways Avoided (scaled)
transferCostUsd            (scaled)
actionId
findingId
decidedByKind              (operator | rule | seed)
approverSub                (verified token sub; absent for rule-approved and seed records)
decision                   (approved | overridden for operators; auto_approved for rules)
ruleId                     (the approving rule's id; rule records only, Group 8)
createdAt
```

---

## 10. Auto-approval path (U6, Group 8)

Design record: `.kiro/specs/2026-10-06-fi-rebalancing-stage-e/group8-notes.md` G8-D1..D9.

**The rule.** A `move_vehicles` rule (queue contract § 6) carries a `limit`: the most
`transfer_cost_usd` a move it approves may cost, at most `max_limit_usd`. Rules are created
disabled; enabling is a second PUT.

**The evaluator** (`lambdas/avx-auto-approver/`) considers `move_vehicles` only when its
`FI_AUTO_MOVES_ENABLED` is exactly `true`, which the stack sets only when
`FI_REBALANCING_AUTO_APPROVAL_LIVE` (`infrastructure/lib/fi-rebalancing-auto-live.ts`) is on.
It shipped `false` until task 8.4 and has been `true` since then (2026-10-24). It approves a recommended move only when the move's `fleet_id` is the
Finding's fleet, its VIN list is non-empty, unique, of well-formed 17-character VINs and at most
`max_vehicles_per_auto_move`, both location ids, the forecast week and both predicted figures are
present, and its cost is a finite number from 0 to `max_limit_usd`; and an enabled rule for the
fleet (read strongly consistent) has an id CMS accepts and a `limit` that is itself within
`max_limit_usd` and at or above the cost (the first such rule in `rule_id` order). Otherwise it
writes no Action and the Finding waits for an operator. Unreadable bounds approve nothing; the
bounds are read only when the fleet has an enabled move rule.

**Dispatch.** An Action with `decided_by.kind: rule` goes to
`<VSA_CMS_FI_INTERNAL_API_ENDPOINT>/api/v1/fleet-intelligence/internal/moves`, signed with SigV4
(`execute-api`) by the evaluator's role, with no token. The endpoint must be CMS's
`FiInternalApiEndpoint` output, `https://<id>.execute-api.<region>.amazonaws.com/<stage>/`, stage
included: the evaluator's `execute-api:Invoke` grant names the API id and stage taken from that
value at deploy. The seeder and the executor refuse a value with no stage, a second path segment,
anything after the stage or surrounding whitespace; synth refuses such a literal override (after
trimming it). A value written to SSM some other way isn't checked before deploy, and the executor
refuses it at dispatch. The signing region is taken
from the host, and the exact body bytes sent are the ones signed, re-signed on each retry. Every other Action
(operator decisions) keeps the token path to `POST /moves` (§ 8).

**CMS** (`fi-rebalancing-api.md` § 7) serves the route on its own REST API with `AWS_IAM`
authorization and a resource policy that denies every principal but the evaluator's role. The
FI Lambda checks the caller again, then re-reads the Action (a move already committed for it
answers its replay at that point, so AVX's retry after a lost response learns the move happened
even if the rule has since changed), the rule and the bounds (Action is
`auto_approved` by a rule, same fleet, rule enabled and of kind `move_vehicles`, cost within its
limit, limit within `max_limit_usd`, vehicle count within `max_vehicles_per_auto_move`, every VIN
in the fleet), then runs the operator route's commit: replay, grounding refusal, origin,
capacity, one transaction. A rule never drops vehicles; a body `vehicle_vins` must equal the
Action's list.

**Move record**: `decidedByKind: rule`, `ruleId`, `decision: auto_approved`; no `approverSub`.

**Fallback not taken**: tech.md § 1b's Cognito client-credentials app client. IAM on a separate
API needed no Cognito change and keeps the public CMSAPI free of a resource policy (G8-D1).

---

## 10a. Failure reasons for `move_vehicles` (queue contract § 10a)

Reachable refusals from the operator and internal routes:

| Row | Sentence |
|---|---|
| 12 | `"CMS refused the request (HTTP 409)."` — grounding, location mismatch, or parking cap |
| (5xx) | `"CMS did not respond after retries."` — maps to `failed` |

---

## 11. Parameters table (U9 — configuration, shipped in `deployment/config/avx-policy-defaults.json`)

**How the agent reads them** (2026-10-14, user's answer before the Group 6 deploy): the AVX
keys sit in one SSM parameter, `/vsa/{stage}/avx/fi-rebalancing/policy` (a JSON object), seeded
from `deployment/config/avx-policy-defaults.json` by `deployment/scripts/seed_avx_policy_params.sh`.
The Tier 2 runner reads it at the start of each `rebalance` run
(`agents/fi_rebalancing/policy.py`); exactly the five keys below marked "AVX policy", strictly
validated. No default exists in code: an absent or malformed parameter aborts the run before any
read or write. `agents/fi_rebalancing/tests/test_policy_no_literals.py` fails if any of these
numbers is written in agent code.

**Authority for shared thresholds**: `deficit_above` and `target_utilization` are applied by
both ADP's `supply_demand_gaps` view (from ADP's `fleet_bookings_parameters` table,
`fleet_bookings/interface.md` § 4) and the agent (first deficit day; the source cap after
moves), so ADP's parameters row and the AVX policy must hold the same values. ADP is the source
of truth: change ADP first, then the AVX policy. The live check (`scripts/live-verify-fi-rebalancing.py`)
compares the two. `surplus_below` is applied only by ADP's view; the agent reads each location's
status from the view's snapshot.

| Key | Default | Owner / source of truth | Description |
|---|---|---|---|
| `max_vehicles_per_move` | 10 | AVX policy | Maximum vehicles in one move item |
| `max_model_calls_per_run` | 12 | AVX stack env `FI_REBAL_MAX_MODEL_CALLS` | One per metro with a candidate; bounded by the number of metros |
| `min_net_benefit_usd` | 100 (U9; the agent used $50 from task 4.1c until the user approved $100 on 2026-10-16) <!-- verify: python3 -c "import json;print([p['staging_default']['min_net_benefit_usd'] for p in json.load(open('deployment/config/avx-policy-defaults.json'))['parameters'] if p.get('_key_template')=='/vsa/{stage}/avx/fi-rebalancing/policy'])" --> | AVX policy | Candidates below this are dropped (spec D8 step 2) |
| `deficit_above` | 90% | **ADP parameters table**; AVX policy holds the same value | Forecast utilization threshold for `deficit` status |
| `surplus_below` | 70% | **ADP parameters table** | Spec D5 default, kept after data-fitness item 3 (`decisions.md` 2026-10-06) |
| `target_utilization` | 85% | **ADP parameters table**; AVX policy holds the same value | Vehicles short/spare are counted to bring a location to this level |
| `max_utilization` | 95% | AVX policy (ADP's parameters row carries the same value for its view) | Used in revenue-impact formula to cap available hours |
| `max_limit_usd` | $500 (deployment bound) <!-- verify: python3 -c "import json;print([p['staging_default'] for p in json.load(open('deployment/config/avx-policy-defaults.json'))['parameters'] if p.get('_key_template','').endswith('auto-approval-bounds')])" --> | AVX auto-approval bounds, a separate parameter `/vsa/{stage}/avx/fi-rebalancing/auto-approval-bounds` (Group 8, task 8.3); not one of the five | U6: the most any `move_vehicles` rule's `limit` may be. Read by the approval-rules route, the evaluator and CMS's internal route; each fails closed |
| `max_vehicles_per_auto_move` | 5 (deployment bound) | Same parameter | U6: the most vehicles one rule-approved move may name |

---

## 12. Cross-references

- [`docs/fi-action-queue-contract.md`](fi-action-queue-contract.md) — queue, approve, § 4 table,
  § 6 auto-approval rules, § 7 browser allowlist, § 10a failure reasons, § 11 fleet checks.
- [`docs/identity-and-standing-contract.md`](identity-and-standing-contract.md) — operator
  standing definition.
- Spec `2026-10-06-fi-rebalancing-stage-e/spec.md` decisions D8–D14.
- `data-fitness.md` — items 6–8 confirm schema, `fleet_recommendation` return shape, and
  server-set parameter design.
- ADP interface: `automotive-data-platform-on-aws/platform-foundation/source/data-products/fleet_bookings/interface.md`.
- CMS API: `cms/services/fleet_intelligence/docs/fi-rebalancing-api.md`.
