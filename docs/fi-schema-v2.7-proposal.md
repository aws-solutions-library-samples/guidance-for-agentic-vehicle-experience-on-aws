# AVX Schema v2.7 Proposal — Stage D

**Version**: proposal (Stage D — spec `2026-10-05-fi-fleet-health-stage-d`)
**Status**: proposal only. Schemas change in task 3.5 after Stage B Group 3 and Stage C Group 3
merge. **Do not edit `agents/tier2/schemas/` or `docs/fi-action-queue-contract.md` from this
document.** This file records what task 3.5 will do.
**Current schema version**: v2.6 (contract `docs/fi-action-queue-contract.md`, on AVX `main`
since `9cda6c4`; Stage C's Group 3 adds its own prose-only edits on top of v2.5).
**Spec decision**: D14.

All changes are **additive**. Every Finding and Action written before v2.7 still validates
unchanged.

---

## What changed in v2.6 (baseline — already merged)

The v2.5 → v2.6 changes were prose-only: no JSON Schema field changed. The Finding and Action
schemas on `main` at `9cda6c4` are v2.5 JSON. The `docs/fi-action-queue-contract.md` prose
sections §§ 3, 4, 10a and 12 were updated to v2.6 (Stage C D11, D12; `check-contract-copies.py`
added in task 1.9).

v2.7 is the first additive JSON Schema change since v2.5.

---

## Summary of v2.7 additions

| Schema | Location | Change |
|---|---|---|
| `finding.schema.json` | `properties.agent_id.enum` | Add `agent-fi-fleet-health`, `agent-fi-quality` |
| `finding.schema.json` | `properties.finding_kind.enum` | Add `fleet.health.service`, `quality.investigation`, `quality.issue` |
| `finding.schema.json` | `$defs.scope.properties` | Add `population_id` field |
| `finding.schema.json` | `$defs.scope.oneOf` | Add `population_id` branch (exactly-one rule) |
| `finding.schema.json` | `allOf` | Add `if/then` guard: `quality.*` kinds require `disclosure_txt: absent` (informational, no monetary guard needed), plus a separate `if/then` that `fleet.health.service` at `p0` requires `disclosure_txt` present and non-empty |
| `action.schema.json` | `properties.target_system.enum` | Add `adp` |
| `action.schema.json` | `properties.action_kind.enum` | Add `book_fleet_service`, `record_engineering_decision`, `issue_recall`, `issue_software_campaign` |
| `action.schema.json` | `properties.decided_by.properties.kind.enum` | Add `engineer` |
| `action.schema.json` | `$defs.audit_entry.properties.guardrail.enum` | Add `G5` |
| `action.schema.json` | `$defs.audit_entry.properties.failure_reason.pattern` | Add G5 sentence; add `adp` → ADP to target-name alternations (rows 12–18, § 10a) |

---

## `finding.schema.json` — detailed diff

### 1. `agent_id` enum — add two values

**Before (v2.5/v2.6):**
```json
"enum": [
  "agent-2-vehicle-health",
  "agent-1-lite-energy",
  "agent-3-ownership-cost",
  "agent-4-lite-concierge",
  "agent-fi-cost",
  "agent-fi-recall-warranty",
  "agent-fi-rebalancing"
]
```

**After (v2.7):**
```json
"enum": [
  "agent-2-vehicle-health",
  "agent-1-lite-energy",
  "agent-3-ownership-cost",
  "agent-4-lite-concierge",
  "agent-fi-cost",
  "agent-fi-recall-warranty",
  "agent-fi-rebalancing",
  "agent-fi-fleet-health",
  "agent-fi-quality"
]
```

Description addition: `"Stage D (spec 2026-10-05-fi-fleet-health-stage-d) adds agent-fi-fleet-health (component: service, daily fleet-health run) and agent-fi-quality (component: investigation, event-driven on SQS)."`

---

### 2. `finding_kind` enum — add three values

**Before (v2.5/v2.6):**
```json
"enum": [
  "health.tires", "health.brakes", "health.battery", "health.diagnostics",
  "energy.charging", "energy.cost",
  "coverage.warranty", "coverage.recall",
  "loyalty.points", "loyalty.expiry",
  "concierge.appointment", "concierge.service",
  "fleet.cost.anomaly", "fleet.cost.sell_timing",
  "fleet.recall.vehicle", "fleet.warranty.claim_candidate",
  "fleet.rebalance.move", "fleet.rebalance.gap"
]
```

**After (v2.7):**
```json
"enum": [
  "health.tires", "health.brakes", "health.battery", "health.diagnostics",
  "energy.charging", "energy.cost",
  "coverage.warranty", "coverage.recall",
  "loyalty.points", "loyalty.expiry",
  "concierge.appointment", "concierge.service",
  "fleet.cost.anomaly", "fleet.cost.sell_timing",
  "fleet.recall.vehicle", "fleet.warranty.claim_candidate",
  "fleet.rebalance.move", "fleet.rebalance.gap",
  "fleet.health.service",
  "quality.investigation",
  "quality.issue"
]
```

Description addition: `"Stage D adds fleet.health.service (nightly health-booking and fault-grounding items), quality.investigation (one per ADP fault pattern, population-scoped), and quality.issue (campaign-issue follow-up after a decision, population-scoped)."`

---

### 3. `$defs.scope` — add `population_id` field and `oneOf` branch

#### 3a. `$defs.scope.properties` — add one field

**Before (v2.5/v2.6):**
```json
"properties": {
  "vin":           { "type": "string", "description": "Vehicle-scoped finding." },
  "owner_id":      { "type": "string", "description": "Owner-scoped finding." },
  "fleet_id":      { "type": "string", "description": "Fleet-scoped finding. ..." },
  "dealership_id": { "type": "string", "description": "Dealership-scoped finding. ..." },
  "district_id":   { "type": "string", "description": "District-scoped finding. ..." }
},
"additionalProperties": false
```

**After (v2.7) — add `population_id`:**
```json
"properties": {
  "vin":           { "type": "string", "description": "Vehicle-scoped finding." },
  "owner_id":      { "type": "string", "description": "Owner-scoped finding." },
  "fleet_id":      { "type": "string", "description": "Fleet-scoped finding. ..." },
  "dealership_id": { "type": "string", "description": "Dealership-scoped finding. ..." },
  "district_id":   { "type": "string", "description": "District-scoped finding. ..." },
  "population_id": {
    "type": "string",
    "description": "Population-scoped finding. The deployment's vehicle-maker id (e.g. 'meridian-motors'). Stage D (spec 2026-10-05-fi-fleet-health-stage-d D11, D14): quality.investigation and quality.issue are population-scoped. A population-scoped Finding carries no fleet_id; it is in no fleet queue (contract § 7 fail-closed state). Its agent-fi- prefix puts it under the iOS exclusion and push-broker skip (contract § 12) without new code."
  }
},
"additionalProperties": false
```

#### 3b. `$defs.scope.oneOf` — add the sixth branch

**Add after the `district_id` branch:**
```json
{
  "required": ["population_id"],
  "properties": {
    "vin":           { "not": {} },
    "owner_id":      { "not": {} },
    "fleet_id":      { "not": {} },
    "dealership_id": { "not": {} },
    "district_id":   { "not": {} },
    "population_id": { "type": "string" }
  }
}
```

---

### 4. `allOf` — add `if/then` guards for new kinds

#### 4a. `p0` fleet-health service items must carry `disclosure_txt`

Add after the existing monetary-kind guard:

```json
{
  "$comment": "Stage D D9: a fleet.health.service Finding at p0 severity must carry disclosure_txt — the fault catalog's stop-driving directive, byte for byte. The existing monetary guard covers coverage.* and energy.cost but not fleet.* items. A p0 health item that silently lacks disclosure_txt would validate without this guard.",
  "if": {
    "properties": {
      "finding_kind": { "const": "fleet.health.service" },
      "severity":     { "const": "p0" }
    },
    "required": ["finding_kind", "severity"]
  },
  "then": {
    "required": ["disclosure_txt"],
    "properties": {
      "disclosure_txt": { "type": "string", "minLength": 1 }
    }
  }
}
```

#### 4b. Fleet-scoped Findings must still have per-VIN evidence (existing guard is unchanged)

No change. The existing `fleet_id`-scoped-Finding guard (allOf item 1 in v2.5) is already
correct. Population-scoped Findings are not fleet-scoped, so they are not subject to the
`minItems: 1` evidence requirement.

---

## `action.schema.json` — detailed diff

### 5. `target_system` enum — add `adp`

**Before (v2.5/v2.6):**
```json
"enum": ["dms", "vehicle-command", "retail", "none", "chat", "ios-owner", "cms"]
```

**After (v2.7):**
```json
"enum": ["dms", "vehicle-command", "retail", "none", "chat", "ios-owner", "cms", "adp"]
```

Description addition: `"Stage D adds 'adp' for the three engineering action kinds: record_engineering_decision, issue_recall, issue_software_campaign. The AdpExecutor invokes adp-{stage}-digital-thread-writer cross-region (us-east-1) with a deadline inside the dispatch budget."`

---

### 6. `action_kind` enum — add four values

**Before (v2.5/v2.6):**
```json
"enum": [
  "book_combined_visit", "schedule_inspection", "update_charging_schedule",
  "file_warranty_pre", "surface_recall", "acknowledge_only",
  "book_recall_service", "ground_vehicle",
  "move_vehicles", "exempt_recall", "complete_recall",
  "submit_warranty_claim", "apply_cost_action",
  "lead_capture", "configurator_recommend"
]
```

**After (v2.7):**
```json
"enum": [
  "book_combined_visit", "schedule_inspection", "update_charging_schedule",
  "file_warranty_pre", "surface_recall", "acknowledge_only",
  "book_recall_service", "ground_vehicle",
  "move_vehicles", "exempt_recall", "complete_recall",
  "submit_warranty_claim", "apply_cost_action",
  "lead_capture", "configurator_recommend",
  "book_fleet_service",
  "record_engineering_decision",
  "issue_recall",
  "issue_software_campaign"
]
```

Description addition (each new kind):
- `book_fleet_service` — `dms`. Executable by operator (G2 does not apply; G5 does not apply). Server-set: `vehicle_vin`, `needs`. Body may differ: `dealer_id`, `urgency`. Not auto-approvable.
- `record_engineering_decision` — `adp`. Executable by `product-engineer` only. G5 refuses all others. Server-set: `investigation_id`, `pattern_id`. Body may differ: `option_id`, `notes`. Not auto-approvable.
- `issue_recall` — `adp`. Executable by `product-engineer` only under G5. All parameters server-set. Not auto-approvable.
- `issue_software_campaign` — `adp`. Executable by `product-engineer` only under G5. All parameters server-set. Not auto-approvable.

---

### 7. `decided_by.kind` enum — add `engineer`

**Before (v2.5/v2.6):**
```json
"enum": ["operator", "user", "rule"]
```

**After (v2.7):**
```json
"enum": ["operator", "user", "rule", "engineer"]
```

Description addition: `"'engineer' — a human who decided on product-engineer standing (membership in the 'product-engineer' Cognito group). Written by the investigations route and the population approve handler from the verified token; G5 requires it for the three engineering action kinds. G1–G4 compare decided_by.kind with 'operator'; an 'engineer' therefore cannot satisfy G1–G4 unless they are also in the operator groups (platform-admin). Stage D D11, D14, D16."`

---

### 8. `$defs.audit_entry.guardrail` enum — add `G5`

**Before (v2.5/v2.6):**
```json
"enum": ["G1", "G2", "G3", "G4"]
```

**After (v2.7):**
```json
"enum": ["G1", "G2", "G3", "G4", "G5"]
```

Description addition: `"G5 — engineering decisions need a product engineer (record_engineering_decision, issue_recall, issue_software_campaign refused when decided_by.kind != 'engineer'). Stage D D11, D14."`

---

### 9. `$defs.audit_entry.failure_reason.pattern` — add G5 sentence

The existing `pattern` in `action.schema.json` admits exactly the sentences in
`docs/fi-action-queue-contract.md` § 10a. v2.7 adds one sentence to that vocabulary.

**New sentence (added to the pattern alternation):**

```
Blocked: engineering decisions need a product engineer.
```

**Pattern change.** In the regex alternation after the existing four `Blocked:` sentences, insert:

```
Blocked: engineering decisions need a product engineer\.
```

The full updated alternation for guardrail sentences:

```
(?:Blocked: exempting a safety recall needs a fleet operator\.|
   Blocked: grounding a vehicle needs a fleet operator\.|
   Blocked: sending a warranty claim needs a fleet operator\.|
   Blocked: completing a recall needs a (?:fleet operator|service record)\.|
   Blocked: the service record could not be (?:checked|found)\.|
   Blocked: the service record is for a different vehicle\.|
   Blocked: the service record is not complete \(status (?:Draft|InProgress|AwaitingParts|Ready|Delivered|Closed|Invoiced|unknown)\)\.|
   Blocked: engineering decisions need a product engineer\.)
```

All other alternations in the pattern are unchanged.

---

### 10. `$defs.audit_entry.failure_reason.pattern` — add `adp` → ADP to target-name alternations (§ 10a)

Stage D adds `AdpExecutor` with `target_system: "adp"`.  The `failure_reason.py` function maps
`"adp"` to the display name `"ADP"`.  Every § 10a sentence that uses `<Target>` (rows 12–18)
would produce sentences like "ADP refused the request (HTTP 422)." — but `ADP` does not appear
in the current pattern's target-name groups, so those sentences fail schema validation.

**Task 3.5 also updates `docs/fi-action-queue-contract.md` § 10a** to add:
> `adp` → ADP

to the target-names table (alongside `dms` → DMS, `cms` → CMS, etc.).

**Pattern change** (applied alongside the § 9 G5 addition above).  In each alternation group
that lists target names, insert `ADP` (or `adp` for the one lowercase occurrence):

| Group | Before | After |
|---|---|---|
| Rows 12–13, 17: sentence-start `<Target>` | `(?:DMS\|CMS\|The retail system\|The vehicle\|The owner's app)` | `(?:DMS\|CMS\|ADP\|The retail system\|The vehicle\|The owner's app)` |
| Rows 14–15: sentence-start `<Target> was unavailable` | `(?:DMS\|CMS\|The retail system\|The vehicle\|The owner's app)` | `(?:DMS\|CMS\|ADP\|The retail system\|The vehicle\|The owner's app)` |
| Row 16: mid-sentence `<target>` (lowercase) in "AVX ran out of time" | `(?:DMS\|CMS\|the retail system\|the vehicle\|the owner's app)` | `(?:DMS\|CMS\|ADP\|the retail system\|the vehicle\|the owner's app)` |
| Row 18: mid-sentence `<target>` in "AVX could not complete the request to" | `(?:DMS\|CMS\|the retail system\|the vehicle\|the owner's app)` | `(?:DMS\|CMS\|ADP\|the retail system\|the vehicle\|the owner's app)` |

`ADP` is always all-caps whether at sentence start or mid-sentence; there is no lowercase
`adp` form in the display name.

Source: `agents/tier2/schemas/action.schema.json:305` (current pattern); `docs/fi-action-queue-contract.md:451-453`
(target-names table); spec D19 ("§ 10a's sentence on the item").

---

## Backward-compatibility statement

Every Finding and Action written at v2.5 or v2.6 validates unchanged under v2.7:
- `agent_id` values in v2.5/v2.6 are still in the v2.7 enum.
- `finding_kind` values in v2.5/v2.6 are still in the v2.7 enum.
- `scope` still requires exactly one field; existing Findings with `vin`, `owner_id`, `fleet_id`,
  `dealership_id`, or `district_id` are still valid.
- The new `disclosure_txt` guard fires only on `fleet.health.service` at `p0` severity. Existing
  kinds are unaffected.
- `target_system` values in v2.5/v2.6 are still in the v2.7 enum.
- `action_kind` values in v2.5/v2.6 are still in the v2.7 enum.
- `decided_by.kind` values `operator`, `user`, `rule` are still in the v2.7 enum.
- `guardrail` values `G1`–`G4` are still in the v2.7 enum.
- `failure_reason` sentences admitted by v2.5/v2.6's pattern are still admitted by v2.7's pattern
  (the G5 sentence is added to the alternation, not replacing any existing sentence).

---

## Consumer pin rotation (task 3.5)

When task 3.5 applies these changes, the following pins must be refreshed (spec D14, § 12):

| Consumer | File | Action |
|---|---|---|
| AVX tests | `tests/schemas/test_contract_fixtures_byte_identical.py` | Re-cut pins with dated comments |
| CMS iOS | `clients/ios/MeridianMotorsCompanionTests/Fixtures/avx/` and `FixtureHashesTests.swift` | Refresh fixture files and Swift hash constants |
| DMS vendored | `tests/schemas/avx/` and `tests/unit/test_avx_fixtures_byte_identical.py` | Refresh schema copies and Python hash constants |

`scripts/check-contract-copies.py` (Stage C task 1.9) must exit 0 after rotation.

iOS decodes unknown enum values without crashing since CMS `91e40864`; DMS's pre-brief agent
reads Findings by storage shape and ignores Findings with no VIN (Group 1 data fitness item 7).

---

## Cross-references

- [`docs/fi-fleet-health-contract.md`](fi-fleet-health-contract.md): the three new kinds.
- [`docs/fi-action-queue-contract.md`](fi-action-queue-contract.md): §§ 1, 4, 6, 10, 10a, 12
  updated to v2.7 in task 3.5.
- `agents/tier2/schemas/finding.schema.json` — changed in task 3.5.
- `agents/tier2/schemas/action.schema.json` — changed in task 3.5.
- Spec: `.kiro/specs/2026-10-05-fi-fleet-health-stage-d/spec.md` D14.
