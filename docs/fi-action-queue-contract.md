# FI Action Queue Contract — Stage A

**Version**: 2.9 (the simulated fleet operator and the rebook rule — spec `2026-11-02-fi-ground-book-return` D7, D10, §§ 1.2, 6, 8, 10, 12)
**Previous version**: 2.8 (the target's own refusal words — issue `2026-10-23-executor-drops-cms-refusal-reason`, §§ 1.2, 3, 7, 10b, 12)
**Earlier**: 2.7 (Stage D — spec `2026-10-05-fi-fleet-health-stage-d`, §§ 1, 4, 6, 10, 10a, 12)
**Spec decisions**: D2–D14, D16
**Relates to**: [`docs/identity-and-standing-contract.md`](identity-and-standing-contract.md)
**OpenAPI**: [`docs/api/avx-findings-actions.openapi.yaml`](api/avx-findings-actions.openapi.yaml)

This is the document CMS builds the Fleet Intelligence queue against. It covers:
- the fields v2.5 adds;
- how a queue status is derived;
- the approve, decline and approval-rules routes;
- the storage shape CMS reads directly, and what may reach a browser;
- run records and the activity feed;
- the four guardrails (v2.5) plus G5 (v2.7);
- what other consumers (iOS, DMS) must not see.

---

## What changed in v2.9

One additive enum value. Every Action written before v2.9 still validates unchanged.

| Section | Change |
|---|---|
| § 1.2, § 10 | `decided_by.kind` += `simulated_fleet_operator` (`id` `simulated-fleet-operator`): the scheduled stand-in that approves FI items for the four staging demo fleets (spec D10). G2 and G3 admit it, and the booking kinds take it, only under two conditions; otherwise the registry refuses it before G1-G5 run, with no guardrail id |
| § 6 | `book_fleet_service` is rule-approvable, for one case only: re-booking the same work after a dealer cancelled the earlier booking of a grounded car (spec D7, "Group 8"), and never for a car on the simulated fleet operator's hold list (added 2026-11-08). The auto-approver acts on such a rule only when `FI_AUTO_REBOOK_ENABLED` is `"true"`, which the stack sets when `FI_AUTO_REBOOK_LIVE` is `true` (since 2026-11-08) |
| § 8 | The stand-in's run records: `agent-fi-sim-operator#<mode>` |
| § 12 | The Action JSON Schema and OpenAPI pins rotated together (the OpenAPI enum was added 2026-11-08). The CMS iOS and DMS `action.schema.json` copies are behind until refreshed (`python3 scripts/check-contract-copies.py` reports them STALE); each repo's own pin test still passes, and no runtime path validates Actions against either copy |

The stand-in's own behaviour (what it approves, the cap, the hold list, the switch) is in
`docs/DEPLOYMENT.md` § "Simulated fleet operator (FI, demo fleets)".

---

## What changed in v2.8

One additive Action field. Every Action written before v2.8 still validates unchanged.

| Section | Change |
|---|---|
| § 1.2 | New audit-entry field `target_reason`: the target's own refusal words (CMS's sentence, DMS's reason code) |
| § 3 | A refused item shows `target_reason` when present, else `failure_reason` |
| § 7 | `target_reason` added to what CMS may return to a browser |
| § 10b | New: where `target_reason` comes from, when it is kept, and how it is cleaned |
| § 12 | v2.8 rotates the Action JSON Schema pin and the OpenAPI pin; CMS iOS and DMS copies refreshed to v2.8 on 2026-10-23 (CMS `d3aa1aeb7`, DMS `28c77e8`) |

---

## What changed in v2.7

All JSON Schema changes are **additive**: every Finding and Action written before v2.7 still validates unchanged.

| Section | Change |
|---|---|
| § 1 | New subsections 1.1 Finding additions and 1.2 Action additions extended for v2.7 (Stage D D14) |
| § 4 | Table extended: four new action kinds, executability column updated; `book_fleet_service` and the three engineering kinds added |
| § 6 | `auto_approvable` note updated: Stage D's four new kinds are not auto-approvable |
| § 10 | G5 row added to the guardrails table |
| § 10a | G5 sentence added to the failure-reason table; `adp` → ADP added to the target-names table; "Which outcomes are reachable today" updated with ADP/AdpExecutor outcomes |
| § 12 | Fixture pin state updated: v2.7 rotates both JSON Schema pins and the OpenAPI pin; CMS iOS and DMS copies must be refreshed to v2.7 |

---

## What changed in v2.6

All changes are prose-only. No Finding or Action JSON Schema field changed; every pre-existing
fixture and consumer still validates unchanged.

| Section | Change |
|---|---|
| § 3 | `cancelled` queue status row added; fixed sentence for `cancelled` added; "Known gap" paragraph removed (Stage C D12) |
| § 4 | Server-set parameters per kind table added (Stage C D11); `submit_warranty_claim` and `ground_vehicle` marked executable; Override rule rewritten to one rule (server-set params never cause `overridden`; decisions.md Stage C call 3) |
| § 10a | "Which outcomes are reachable today" updated: DMS claim route 201/202/422/400 and CMS grounding 2xx/4xx/5xx; G4 recall-reference check remains off (U8) |
| § 12 | Fixture pin state clarified: both DMS and CMS iOS pins are at v2.5 and unchanged for v2.6; broker-skip (task 1.8) noted |

---

## 1. What changed in v2.5

All changes are **additive for data**: every Finding and Action written before v2.5 still
validates. They are **not** additive for every client. See § 12 before writing FI Findings
anywhere an iOS user can read.

### 1.1 Finding additions

| Addition | Field | Notes |
|---|---|---|
| Agent ids | `agent_id` enum | `agent-fi-cost`, `agent-fi-recall-warranty`, `agent-fi-rebalancing` (v2.5); `agent-fi-fleet-health`, `agent-fi-quality` (v2.7) |
| Kinds | `finding_kind` enum | `fleet.cost.anomaly`, `fleet.cost.sell_timing`, `fleet.recall.vehicle`, `fleet.warranty.claim_candidate`, `fleet.rebalance.move`, `fleet.rebalance.gap` (v2.5); `fleet.health.service`, `quality.investigation`, `quality.issue` (v2.7) |
| Subject discriminator | `subject_key` (optional, non-empty) | Joins the `finding_key` hash. Absent → the hash is byte-identical to the four-field form. Lets one fleet hold several Findings of one kind (one per move). |
| Proposed action | `recommended_action` (optional) | `{action_kind, target_system, parameters, estimated_value_usd?, estimated_value_basis?}`. A Finding with this field is an **FI queue item** and follows § 4's rules. |
| Location | `location` (optional) | Exactly one of `{location_id}` or `{from_location_id, to_location_id}`, all non-empty. CMS `locationId` values. See § 2. |
| Population scope | `scope.population_id` (optional, v2.7) | The deployment's vehicle-maker id (e.g. `meridian-motors`). A population-scoped Finding carries no `fleet_id`; it is in no fleet queue (§ 7 fail-closed state). Stage D kinds `quality.investigation` and `quality.issue` are population-scoped (spec D11, D14). |
| Grounding disclosure guard | `allOf` if/then (v2.7) | A `fleet.health.service` Finding at `p0` severity must carry non-empty `disclosure_txt` — the fault catalog's stop-driving directive. Enforced by the JSON Schema. |

### 1.2 Action additions

| Addition | Field | Notes |
|---|---|---|
| Target | `target_system` += `cms` | CMS-side actions (grounding, moves, recall exemption and completion). Stage A's `CmsExecutor` refuses all of them. |
| Target | `target_system` += `adp` (v2.7) | The three engineering action kinds. `AdpExecutor` invokes `adp-{stage}-digital-thread-writer` cross-region (us-east-1) with a deadline inside the dispatch budget. |
| Kinds | `action_kind` enum | Seven FI kinds plus the two retail kinds; table in § 4. |
| Kinds (v2.7) | `action_kind` enum | `book_fleet_service` (dms; operator only until v2.9, which adds the rebook rule, § 6, and the simulated fleet operator, § 10), `record_engineering_decision` (adp, G5), `issue_recall` (adp, G5), `issue_software_campaign` (adp, G5); table in § 4. |
| Who decided | `decided_by` (optional) | `{kind, id}`. `kind`: `operator` (fleet-operator standing on the Finding's fleet, or `platform-admin`), `user` (any other human the approve check admits: owner, fleet driver, service advisor), `rule`. `id`: the verified token `sub`, or `<fleet_id>/<rule_id>`. |
| Simulated kind (v2.9) | `decided_by.kind` += `simulated_fleet_operator` | The scheduled stand-in for the four staging demo fleets, `id` `simulated-fleet-operator`, written by its own Lambda. It is admitted (G2, G3, the booking kinds) only in that Lambda's process and on a demo fleet; otherwise the registry refuses it before G1-G5 (§ 10). |
| Engineer kind (v2.7) | `decided_by.kind` += `engineer` | A human on product-engineer standing. Written by the investigations route and the population approve handler from the verified token. G5 requires it for the three engineering kinds; G1–G4 require `operator`, which an `engineer` is not (unless also `platform-admin`). |
| How decided | `decision` (optional) | `approved`, `overridden`, `auto_approved` or `declined`. Written **only** on an FI queue item's one decision Action; every other Action, including non-FI approves, omits it. Only the decline route writes `declined`. |
| Guardrail block | audit entry `guardrail` (optional) | `G1`–`G4` (v2.5), `G5` (v2.7). Valid only on a transition to `declined` with `failure_kind: target_refused`, and always with a `failure_reason`; the schema enforces both. |
| Why it did not go through | audit entry `failure_reason` (optional) | One sentence from § 10a's fixed vocabulary; the schema's `pattern` admits nothing else. Valid only on a transition to `declined` or `failed`. |
| Target's HTTP status | audit entry `error_status` (optional, 100–599) | The status the target returned to the dispatch. Valid only on `declined` or `failed`, never on a guardrail block; omitted, never `null`, when there was none. |
| Target's own words (v2.8) | audit entry `target_reason` (optional, 1–200 characters, plain text) | The refusal text the target sent, word for word (§ 10b): CMS's sentence, or DMS's warranty reason code. Valid only on a transition to `declined` with `failure_kind: target_refused` and a 4xx `error_status`, never with `guardrail`; `failure_reason` is always written beside it. Untrusted text: show it as text, never as markup. |

---

## 2. Scope and location

- **VIN-scoped Finding**: about one vehicle. The writer projects the vehicle's `fleet_id` when it
  resolves, so the Finding is in that fleet's queue.
- **Fleet-scoped Finding**: spans vehicles or locations. Evidence entries carry per-vehicle `vin`;
  `recommended_action.parameters` lists the vehicles a move covers.
- **Location is a field, not a scope.** Access is fleet-wide. There is no per-location role (user,
  2026-10-02).
- **No location index.** CMS filters a fleet's Findings by location in code. A Finding matches a
  location when any of its `location` IDs equals it.

```json
{ "location": { "location_id": "loc-airport-02" } }
{ "location": { "from_location_id": "loc-downtown-01", "to_location_id": "loc-airport-02" } }
```

---

## 3. Queue status

Every FI queue item (a Finding with `recommended_action`) has **at most one decision Action**
(§ 4, "One decision per Finding"). Its queue status is a function of the Finding and that one
Action, evaluated top to bottom:

| # | Condition | Queue status |
|---|---|---|
| 1 | No decision Action, Finding `expires_at` in the future | `pending` |
| 2 | No decision Action, Finding expired | `expired` |
| 3 | Action `declined` and `decision: declined` | `rejected` |
| 4 | Action `declined`, last audit entry carries `guardrail` | `blocked` |
| 5 | Action `declined`, last audit entry `failure_kind: target_refused`, no `guardrail` | `refused` (the target system said no) |
| 6 | Action `failed` | `failed` |
| 7 | Action `cancelled` | `cancelled` |
| 8 | Action `completed` or `executed` | `executed` |
| 9 | Action `pending_execution` and `updated_at` more than 15 minutes ago | `stalled` (recorded, but dispatch never reported back) |
| 10 | Action `pending_execution` or `in_progress` | the Action's `decision`: `approved`, `overridden` or `auto_approved` |

`dismissed` cannot occur on a decision Action — the route refuses it. `cancelled` cannot occur on
a decision Action through `PATCH /actions/{id}` or `POST /actions/{id}/dismiss` — only DMS's
status events write it (spec D12).

**Failed and blocked items show why** (user, 2026-10-02). For `blocked`, `refused` and `failed`,
the screen shows the last audit entry's `failure_reason` (§ 10a), and `error_status` when present,
next to the status. When that entry also has a `target_reason` (§ 10b, v2.8), CMS shows the
target's own words instead of `failure_reason`, after re-checking them with § 10b's cleaning
rules and its target/status table; text that fails the check is not shown, and
`failure_reason` is. If that entry has no `failure_reason`, CMS shows the fixed sentence "AVX did
not record a reason." CMS never shows `note`. For `stalled`, CMS shows the fixed sentence "AVX recorded this approval, but
the system it was sent to never reported back." For `cancelled`, CMS shows the fixed sentence
"DMS closed the repair order before the work was done." The approve response already carries the
Action with that audit entry, so the screen can show the reason as soon as the approve returns.

Rows 3–5 are distinct by construction. The decline route is the only writer of `decision:
declined`. A guardrail block or a target refusal keeps the decision that was made, and the audit
trail says what happened. Read `guardrail` and `failure_kind`; never parse `note`.

Findings without `recommended_action` (Agent 2's health Findings today) are **informational**
queue items, with no status beyond `pending` and `expired`.

**A decision Action changes only through the executor and DMS's status events.**
`PATCH /actions/{action_id}` and `POST /actions/{action_id}/dismiss` return 409
`fi_decision_immutable` on any Action that carries `decision`. Without this, the deciding
operator, a service advisor or a platform admin could move it into a state § 3 does not cover.
The scope check runs first: a caller without standing gets the same 403 for a decision Action
as for any other, so the 409 cannot tell an outsider which ids are FI decisions.

---

## 4. Approve route

```
POST /findings/{finding_id}/approve
Authorization: Bearer <the operator's own Cognito ID token>
```

CMS checks the operator's fleet first, then forwards the operator's token. AVX checks again
(`_check_approve_scope`) and records the approver from the verified token. Neither check trusts
a body field for identity (spec D6).

```json
{ "target_system": "none", "action_kind": "acknowledge_only", "parameters": {} }
```

### Rules for every approve (v2.5)

- `decided_by` is written from the token. Any approver-like body field is ignored.
- A kind that is not executable in this deployment returns 400 before anything is written.

| Kind | Target | Executable in Stage C |
|---|---|---|
| `book_combined_visit`, `schedule_inspection`, `update_charging_schedule`, `file_warranty_pre`, `surface_recall`, `acknowledge_only` | as today | as today (unchanged) |
| `lead_capture`, `configurator_recommend` | `retail` | as today: executor exists, and the `_RETAIL_KINDS` runtime gate still applies |
| `book_recall_service` | `dms` | yes (`DmsExecutor`, operator token) |
| `submit_warranty_claim` | `dms` | **yes** (new in Stage C, `DmsExecutor`, `POST /api/dms/fleet/warranty-claims`) |
| `ground_vehicle` | `cms` | **yes** (new in Stage C, `CmsExecutor`, operator token) |
| `move_vehicles` | `cms` | **yes** (new in Stage E, `CmsExecutor`, operator token, `POST /api/v1/fleet-intelligence/moves`; auto-approvable from Stage E Group 8 under a limited rule, sent SigV4-signed to CMS's internal route (§ 6); server-set fleet, locations, week and predicted figures; the operator may only drop vehicles, which makes the decision `overridden` and scales the figures; `docs/fi-rebalancing-contract.md` § 4) |
| `exempt_recall`, `complete_recall` | `cms` | no (Stage C) |
| `apply_cost_action` | set by the cost stage | no (Stage B) |
| `book_fleet_service` | `dms` | **yes** (new in Stage D, `DmsExecutor`, operator token; G5 does not apply) |
| `record_engineering_decision` | `adp` | **yes** (new in Stage D, `AdpExecutor`, G5 required, `product-engineer` only) |
| `issue_recall` | `adp` | **yes** (new in Stage D, `AdpExecutor`, G5 required, `product-engineer` only) |
| `issue_software_campaign` | `adp` | **yes** (new in Stage D, `AdpExecutor`, G5 required, `product-engineer` only) |

### Extra rules for FI queue items (Findings with `recommended_action`)

- **Order of checks.** Every 403 (the approve check, then operator standing) is decided before
  the decision is read. A caller without standing gets the same 403 whether or not the item was
  decided, and never sees `already_decided` or the decision's `action_id`.
- **Who.** Only `decided_by.kind: operator` may decide them; anyone else gets 403. Operator
  standing on a fleet comes from `custom:fleetIds` only when `AVX_FLEET_IDS_CLAIM_TRUSTED` is
  `true`; with the flag off, only `platform-admin` is an operator.
- **What.** The body's `action_kind` and `target_system` must equal the recommendation's (400).
- **Override.** Server-set parameters (listed per kind below) must be omitted or equal
  (400 otherwise). They are filled in by AVX after the comparison and never make a decision
  `overridden`. `decision` is `overridden` when the body's `parameters` differ from
  `recommended_action.parameters` on an otherwise-allowed (non-server-set) field, and
  `approved` otherwise. The overrideable fields are `dealer_id` (for `book_recall_service`)
  and `notes` (for `submit_warranty_claim`). Omitted overridable fields take the
  recommendation's value and are not an override; only a body value that differs from the
  recommendation makes the decision `overridden` (decisions.md call 10).
- **Location.** Every location ID in `parameters` (`location_id`, `from_location_id`,
  `to_location_id`) must equal the same key in the Finding's `location` (400 otherwise). A body
  may omit them, so a recommendation approved exactly as recommended passes. A Finding with no
  `location` takes none, and a `null` value counts as a mismatch.
- **Server-set parameters per kind (Stage C v2.6).** For each kind, AVX sets the listed
  parameters from the Finding at approve time; the body must omit these or supply an equal value
  (400 otherwise). AVX fills in any server-set parameter the body omits. A body value that differs
  from `recommended_action.parameters` on an otherwise-allowed field makes the decision
  `overridden`. See `docs/fi-recall-warranty-contract.md` § 3 for the complete table; in summary:

  | Kind | Server-set (body must omit or match) | Body may differ |
  |---|---|---|
  | `book_recall_service` | `vehicle_vin`, `recall_id` | `dealer_id` |
  | `ground_vehicle` | `vehicle_vin`, `recall_id` | (nothing) |
  | `submit_warranty_claim` | `vehicle_vin`, `ro_id`, `line_no`, `dealer_id`, `labor_op`, `fault_code` | `notes` |
  | `book_fleet_service` | `vehicle_vin`, `needs` | `dealer_id`, `urgency` |
  | `record_engineering_decision` | `investigation_id`, `pattern_id` | `option_id`, `notes` |
  | `issue_recall` | all parameters server-set | (nothing) |
  | `issue_software_campaign` | all parameters server-set | (nothing) |
  | All other VIN-scoped FI items | `vehicle_vin` | depends on kind |

  G4 compares the repair order against the server-set `vehicle_vin`.
- **One decision per Finding.** Approve, decline and auto-approval all write the same
  `action_id`, derived from the `finding_id` alone, with a conditional put. The first decision
  wins. The derivation, pinned by literal in both Lambdas' tests:

  ```
  h = sha256("<finding_id>|fi-decision").hexdigest()[:32]
  action_id = "AVX-ACT-" + h[0:8] + "-" + h[8:12] + "-" + h[12:16] + "-" + h[16:20] + "-" + h[20:32]
  ```
  - Repeating the same decision returns it (200).
  - Any other decision gets 409 `already_decided` with the existing `action_id`.
  - **A decision is final for as long as the Finding exists, including a `failed` or `refused`
    one.** The writer updates a persisting condition in place, extending `expires_at` and keeping
    the `finding_id`, so the item does not come back fresh while the condition lasts. Retrying a
    decision is a follow-on.

Findings without `recommended_action` keep today's per-caller idempotency, keyed on
`(finding_id, action_kind, caller)`.

---

## 5. Decline route

```
POST /findings/{finding_id}/decline
Authorization: Bearer <the operator's own Cognito ID token>
```

```json
{ "reason": "Handled offline; depot manager confirmed" }
```

- Only for FI queue items (Findings with `recommended_action`); 400 otherwise. iOS cards use
  `POST /actions/{action_id}/dismiss`.
- Requires `decided_by.kind: operator` (403 otherwise), with the same order of checks as § 4:
  every 403 comes before the decision is read.
- `reason` is optional, at most 500 characters (400 otherwise). The body takes no other field.
- Writes the Finding's one decision Action:

  | Field | Value |
  |---|---|
  | `status` | `declined` |
  | `decision` | `declined` |
  | `decided_by` | from the token |
  | `action_kind`, `target_system` | copied from `recommended_action` |
  | `parameters` | `{"decline_reason": "<reason>"}`, or `{}` |
  | `audit_trail` | `[]` (creation is not a transition) |

- Nothing is dispatched. A kind that is not executable yet can still be declined.
- A repeated decline returns the existing Action (200). A Finding that is already approved
  returns 409 `already_decided`.

---

## 6. Approval-rules routes

```
GET    /approval-rules/fleet/{fleet_id}
PUT    /approval-rules/fleet/{fleet_id}/{rule_id}
DELETE /approval-rules/fleet/{fleet_id}/{rule_id}
Authorization: Bearer <the operator's own Cognito ID token>
```

- **Scope.** The caller needs the `fleet-operator` or `platform-admin` group AND a trusted
  `custom:fleetIds` naming the fleet (trusted only when `AVX_FLEET_IDS_CLAIM_TRUSTED` is
  `true`; read through `_lib/avx_scope.py`, never raw). All three methods carry the same
  `x-scope-predicate`. **There is no `platform-admin` bypass of the membership
  check**, matching `/findings/fleet/{fleet_id}`. A rule is standing approval for one fleet.
- **Concurrent writes.** A create and an update are conditional writes. If another request
  created, changed or deleted the rule between this request's read and its write, the PUT
  returns 409 `rule_changed_concurrently` and writes nothing.
- **Create is always disabled.** A PUT that creates a rule stores `enabled: false`. A create
  whose body sets `enabled: true` gets 400 `rules_are_created_disabled`, so enabling is always a
  second, deliberate PUT on an existing rule.
- **What can be ruled.** Only kinds that are `auto_approvable` in this deployment: in Stage A
  that is just `acknowledge_only`; Stage E Group 8 adds `move_vehicles`; v2.9 adds
  `book_fleet_service` for the rebook rule (below). Guarded kinds (`exempt_recall`, `ground_vehicle`,
  `submit_warranty_claim`, `complete_recall`) always get 400. Stage D's three engineering kinds
  (`record_engineering_decision`, `issue_recall`, `issue_software_campaign`) are not
  auto-approvable and always return 400 on this route.
- **Limits.** A limited kind's rule requires a numeric `limit`. `move_vehicles` is the first
  (Stage E Group 8, U6): its `limit` is the most `transfer_cost_usd` a move the rule approves may
  cost, from 0 to the deployment's `max_limit_usd`. The bounds are one SSM parameter,
  `/vsa/{stage}/avx/fi-rebalancing/auto-approval-bounds`, a JSON object with exactly
  `max_limit_usd` ($500 on staging) and `max_vehicles_per_auto_move` (5), seeded from
  `deployment/config/avx-policy-defaults.json`. It is read per request by this route, by the
  auto-approval evaluator and by CMS, and each fails closed: here an unreadable or malformed
  value is 503 `auto_approval_bounds_unavailable` with nothing written (a PUT that only disables
  a rule doesn't read it). Refusals: no `limit` on a limited kind, 400 `limit_required`; a
  `limit` out of range or not a number, 400 `invalid_limit`; a `limit` on a kind without one
  (such as `acknowledge_only`), 400 `limit_not_supported`. Changing a rule's `action_kind` stores
  it disabled, as a create does, and a change to a kind without a limit drops the stored one.
  <!-- verify: python3 -c "import json;print([p['staging_default'] for p in json.load(open('deployment/config/avx-policy-defaults.json'))['parameters'] if p.get('_key_template','').endswith('auto-approval-bounds')])" -->

```json
{ "action_kind": "acknowledge_only" }
{ "action_kind": "move_vehicles", "limit": 250 }
```

- **The rebook rule (`book_fleet_service`, v2.9, spec D7).** A rule approves a fleet-health
  booking only when all of these hold, checked by the auto-approval evaluator: the Finding is
  `agent-fi-fleet-health` with `target_system` `dms` and `subject_key` `service/<n>`, n ≥ 2 (a
  first booking never qualifies); the same VIN and fleet's `service/<n-1>` Action exists, is
  `book_fleet_service`, is `cancelled` and was decided by an operator or by a rule (not by the
  simulated fleet operator, whose own cap covers its rebookings); the new
  booking's `need_id`s equal that Action's (the same work, never new work); the VIN has an open
  FI fault grounding; the fleet has an enabled `book_fleet_service` rule; and the car isn't on the
  simulated fleet operator's hold list (`hold_vins`, and `hold_vehicle_ids`, which must be VIN-shaped,
  in `/cvx/{stage}/fi/sim-operator`; added 2026-11-08 before the rule went live). Anything unreadable
  writes no Action, including an absent or malformed hold-list parameter or a held id that isn't a VIN. The evaluator acts on it only when its process has `FI_AUTO_REBOOK_ENABLED ==
  "true"`, and the stack sets that variable (and the evaluator's grant on DMS's internal
  repair-orders route) only when `FI_AUTO_REBOOK_LIVE` in `infrastructure/lib/fi-auto-rebook-live.ts`
  is `true`, which it is since 2026-11-08: a fleet with an enabled rule for the kind gets its
  rebookings. <!-- verify: grep -n "export const FI_AUTO_REBOOK_LIVE" infrastructure/lib/fi-auto-rebook-live.ts -->
- **Who made it.** `created_by` on the rule is the verified token `sub`. A rule-created Action has
  `created_by: rule-<fleet_id>/<rule_id>`, `decided_by: {kind: rule, id: <fleet_id>/<rule_id>}`
  and `decision: auto_approved`.
- **Stage A limitation, and the CMS service identity.** Executors forward the caller's token, and
  a rule-created Action has no caller, so only token-free kinds could be auto-approved in Stage A.
  Stage E Group 8 adds a service identity for CMS moves (v2.9 adds one new caller and gives the existing one a
  new route: the simulated fleet operator, signing to CMS's internal groundings route and both DMS internal
  routes; and the evaluator's rule rebookings, to DMS's internal repair-orders route once rule
  rebooking is on): the evaluator's role
  (`vsa-{stage}-avx-auto-approver-{region}`) signs a rule-decided `move_vehicles` Action with
  SigV4 to CMS's `POST /api/v1/fleet-intelligence/internal/moves`, on CMS's IAM-only internal API
  (`/cvx/{stage}/cms-fi-internal-api-endpoint`), and sends no token. CMS refuses every other
  principal and re-checks the Action, the rule, the limit and the bounds before moving anything
  (CMS `fi-rebalancing-api.md` § 7). Operator-decided moves keep the token path.
- **When the evaluator approves a move.** Only when `FI_REBALANCING_AUTO_APPROVAL_LIVE` is on in
  the deployment (it shipped off until task 8.4; `fi-rebalancing-auto-live.ts` sets it): the Finding's recommended move names that fleet, a non-empty list
  of unique, well-formed 17-character VINs no longer than `max_vehicles_per_auto_move`, both
  locations, the forecast week and both predicted figures, and a `transfer_cost_usd` from 0 to
  `max_limit_usd`; and an enabled `move_vehicles` rule for the fleet, read strongly consistent,
  has an id CMS accepts (no `/` or whitespace, at most 128 characters) and a `limit` that is
  itself within `max_limit_usd` and at or above that cost (the first such rule in `rule_id`
  order). Otherwise it writes nothing and the Finding waits for an operator. So lowering
  `max_limit_usd` below an enabled rule's `limit` stops that rule approving anything until its
  limit is lowered (a PUT re-validates it). A grounded vehicle is never moved on either path: CMS
  refuses it.

---

## 7. Storage CMS reads directly

CMS reads `vsa-{stage}-avx-findings`, `vsa-{stage}-avx-actions`, `vsa-{stage}-avx-agent-runs` and
`vsa-{stage}-avx-approval-rules` directly, applying its own fleet scope. CMS's check is the only
gate on these reads, and it has a removal test in CMS (spec D7).

### Keys and indexes

All GSIs project `ALL`.

| Table | Key | GSI used by CMS | GSI keys | Use |
|---|---|---|---|---|
| findings | `finding_id` | `by_fleet` | pk `fleet_id`, no sort key | The fleet's queue (CMS sorts by `computed_at`) |
| findings | | `by_agent_computed_at` | pk `agent_id`, sk `computed_at` | Per-agent activity |
| actions | `action_id` | `by_fleet` (new) | pk `fleet_id`, sk `updated_at` | The fleet's decisions |
| actions | | `by_finding` | pk `finding_id`, no sort key | The decision for one Finding |
| agent-runs | pk `agent_component`, sk `started_at` | none | | Last run per agent (§ 8) |
| approval-rules | pk `fleet_id`, sk `rule_id` | none | | A fleet's rules |

### Storage-only attributes

These exist on the stored item for the GSIs and are **not** part of the schemas. AVX's API strips
them, and CMS must not return them either.

| Attribute | On | Present when |
|---|---|---|
| `fleet_id` | Finding | fleet-scoped (from `scope.fleet_id`), or VIN-scoped with a resolved fleet |
| `owner_id`, `vehicle_id` | Finding | VIN-scoped, resolved from the CMS vehicle registry |
| `vin`, `dealership_id`, `district_id` | Finding | that scope field is set |
| `fleet_id` | Action | every Action written by approve, decline or a rule (v2.5) |
| `ttl` | Finding | informational rows only: no `recommended_action`, no `ttl_exempt`, and `agent_id` not `agent-fi-recall-warranty` or `agent-fi-quality`. Value: epoch seconds of `expires_at` + 604,800 (7 days). The table's TTL attribute, so DynamoDB deletes the row in the background after that time. A row without it is kept (`agents/tier2/writer.py` `_compute_ttl`). |
| `ttl_exempt` | Finding | `true` once any write saw `recommended_action` on the stored row or the incoming artifact. Never removed, so a row that was ever an FI queue item never gets `ttl` and the purge script skips it. |

A VIN-scoped Finding whose fleet did not resolve has no top-level `fleet_id`, so it is in no
fleet's queue. That is the fail-closed state the approve check relies on.

### What CMS may return to a browser

An allowlist; anything not listed stays server-side.

- **Finding**: `finding_id`, `agent_id`, `finding_kind`, `severity`, `confidence`, `headline`,
  `detail`, `computed_at`, `expires_at`, `recommended_action`, `location`, `disclosure_txt`,
  `agent_version`, the value of `scope.vin` (as `vin`), and `evidence[]` entries' `kind`, `source`,
  `value`, `computed_at`, `provenance` and `vin`. The VIN is the vehicle's own identifier and is
  already shown on CMS vehicle pages; evidence is capped as on the vehicle page, with a total count.
  `agent_version` and `scope.vin` were added on 2026-10-04 to match what CMS Group 5 returns (as
  `agentVersion` and `vin`).
- **Action**: `action_id`, `finding_id`, `action_kind`, `target_system`, `status`, `decision`,
  `decided_by.kind`, `created_at`, `updated_at`, `target_ref`, and `audit_trail[]` entries' `at`,
  `from_status`, `to_status`, `failure_kind`, `failure_reason`, `error_status`, `guardrail` and
  (v2.8) `target_reason`, re-checked with § 10b's rules first. **On decision Actions only** (those
  carrying `decision`), also `parameters`: what was approved, any override, `decline_reason`. A
  non-FI Action's `parameters` stay server-side; they can carry a driver's free text, such as a
  `symptom_note`.
- **Approval rule**: `fleet_id`, `rule_id`, `action_kind`, `enabled`, `limit` and `updated_at`.
  AVX's rules API returns `created_by` (the author's Cognito `sub`) to CMS; like `decided_by.id`,
  it does not go to the browser.
- **Not returned**: `decided_by.id` (a Cognito `sub`; CMS shows only whether the caller made the
  decision), `created_by`, `scope` (apart from `scope.vin` above), `finding_key`, `inputs_hash`, `subject_key`, `tokens_used`, `cost_usd`,
  the Finding `audit_trail`, audit `note` and `by`, and every storage-only attribute above (the
  storage-only top-level `vin` is not returned, and the queue does not use it; the VIN shown comes
  from `scope.vin`).

---

## 8. Run records

`vsa-{stage}-avx-agent-runs`: pk `agent_component` (`<agent_id>#<component>`), sk `started_at`.

```json
{
  "agent_component": "agent-2-vehicle-health#battery",
  "started_at": "2026-10-02T02:20:00Z",
  "finished_at": "2026-10-02T02:21:43Z",
  "status": "completed",
  "artifacts_written": 14,
  "duplicates_skipped": 2,
  "agent_version": "1.2.3"
}
```

- `status` is `completed` or `aborted`, the runner's own values.
- CMS reads the newest record per `agent_component` and shows "last ran at". With no record, it
  shows "has not run yet", never an empty queue presented as "nothing to do".
- Scheduled agents (the Tier 2 health components) write one record per run, so an old "last
  ran" means a missed run, and an operator alarm fires (spec D10). The auto-approval evaluator
  is event-driven: it writes one record per batch of FI Findings it receives and none while no
  FI Finding arrives. An old evaluator "last ran" is therefore not a failure on its own; its
  errors and DLQ alarms are the failure signal.
- The simulated fleet operator (v2.9) writes one record per run under
  `agent-fi-sim-operator#<mode>`: `#approve` for the 07:10 UTC schedule, `#dry_run` and
  `#observe` for runs started by hand (`scripts/set-fi-sim-operator.py --invoke`). Its detail is
  in a `sim_operator` map (`mode`, `errors`, per-fleet counts). Five observe runs of 2026-11-08
  00:22-00:26 UTC were stored under `#approve` before that was fixed; filter on
  `sim_operator.mode`. CMS doesn't list the stand-in among its agents.
- `managed_coverage` (health components only; stage D U7): present when the component's managed
  pool is larger than its nightly cap, so the managed cursor rotates and every managed vehicle is
  covered over several nights. The run counts its managed pool (one Athena query over the
  source's own managed-pool SQL) and records the fallback:

  ```json
  "managed_coverage": {
    "mode": "7_day_fallback", "cycle_nights": 7, "cap": 600, "managed_pool": 4042,
    "nights_to_cover": 7, "cycle_covers": 4042, "slice_start": 1801, "slice_size": 600
  }
  ```

  `cycle_covers` is `min(managed_pool, 7 x cap)`: below `managed_pool`, 7 nights don't cover the
  pool, and the record says so rather than claiming coverage. `slice_start` and `slice_size` are
  this run's slice by 1-based VIN-order position (a slice can wrap past the end of the pool). A
  count that fails or answers inconsistently, or a failed wrap-around query that leaves the
  slice short (also counted in `managed_select_errors`), writes `{"mode": "unmeasured",
  "cycle_nights", "cap", "slice_size"}` and never fails the run. Absent when the managed page
  wasn't full and no managed query failed: the pool fits the cap. The count waits at most 30 s
  queued and 30 polls running. Readers check the arithmetic themselves; stage D 6.3 item 1
  passes a rotating component only on a sound record.

---

## 9. Activity feed

Two queries, merged and ordered by time in CMS:
1. **Findings**: findings `by_fleet` → one "recommended" event per Finding at `computed_at`.
2. **Actions**: actions `by_fleet` → one "decided" event at `created_at` (with `decision` and
   `decided_by.kind`), plus one event per `audit_trail` entry.

`GET /actions/fleet/{fleet_id}` is not used: it returns 501 `actions_scope_index_absent`. Actions
written before v2.5 have no top-level `fleet_id` and do not appear.

---

## 10. The five guardrails

All five run in `registry.dispatch()`, before any executor, for every Action however it was
created. Fail closed: a missing, malformed or empty `decided_by` is not an operator, and is not
an engineer.

| # | Guardrail | Kind | Refused when |
|---|---|---|---|
| G1 | A safety recall is never auto-exempted | `exempt_recall` | `decided_by.kind != "operator"` |
| G2 | Grounding needs the operator | `ground_vehicle` | `decided_by.kind != "operator"`, except the simulated fleet operator (v2.9, below) |
| G3 | Sending a warranty claim needs the operator | `submit_warranty_claim` | `decided_by.kind != "operator"`, except the simulated fleet operator (v2.9, below) |
| G4 | A recall is never complete without a service record | `complete_recall` | always, unless `decided_by.kind` is `operator` and `parameters.service_record_ref` names a DMS repair order whose `vehicle_vin` equals `parameters.vehicle_vin` (server-set from the Finding, § 4) and whose status is `Ready`, `Delivered` or `Invoiced` |
| G5 | Engineering decisions need a product engineer | `record_engineering_decision`, `issue_recall`, `issue_software_campaign` | `decided_by.kind != "engineer"`. A missing, malformed or empty `decided_by` is not an engineer. Stage D D11, D14. |

- **The simulated fleet operator (v2.9).** G2 and G3 admit a `decided_by` whose `kind` is
  `simulated_fleet_operator` and `id` is `simulated-fleet-operator` (AVX compares those two fields,
  `is_simulated_decider`; CMS's and DMS's internal routes require exactly those two keys) only when
  the running process has `FI_SIM_OPERATOR_ENABLED == "true"` (set only on the stand-in's Lambda)
  and the Action's `fleet_id` is in `DEMO_FLEET_IDS`, a literal set of the four demo fleets in
  `agents/executors/simulated_operator.py`. `book_fleet_service` and `book_recall_service` get the
  same two-condition re-check. Every other kind is refused before G1-G5 run, by the registry's
  own simulated-decider check, so such a refusal carries no `guardrail` id. A rule decider is
  still refused by G1-G3. Its executors call DMS's and CMS's IAM-only
  internal routes with SigV4 and no token; each route re-reads the Action before acting.
- **G4's status list is explicit.** `Closed` is excluded because DMS defines it as administrative
  closure, not completed work. With no resolver configured, G4 refuses.
- **Recall reference (deferred).** Checking that the repair order was for *this* recall (a line
  whose `recall_campaign_id` equals `parameters.recall_id`, or whose labor op is the campaign's
  `remedy_labor_op`) is built with the stage that makes `complete_recall` executable. Stage C
  deferred it (its `decisions.md`, 2026-10-05): the refusal needs a new § 10a sentence, which the
  Action schema's `failure_reason` pattern does not admit, and `complete_recall` has no path in
  Stage C.
- **What a refusal looks like.** A refused Action is `declined`, with an audit entry carrying
  `failure_kind: target_refused`, `guardrail` and `failure_reason` (§ 10a, which also gives G4's
  check order). It never reaches an executor, never escalates, and shows as `blocked`.
- **Live verification.** G4 is unit- and mutation-tested in Stage A. It is live-verified, with
  the recall-reference check, when `complete_recall` becomes executable.

---

## 10a. Failure reasons

**Who writes them.** Every transition to `declined` or `failed` that `registry.dispatch()` or an
executor records carries `failure_reason`: guardrail blocks, executor results, and the rule
evaluator's dispatches, because all of them go through `registry.dispatch()`. Other writers of
`declined` (for example `PATCH /actions/{id}`, which FI decision Actions refuse, § 3) are outside
this section.

**Where the text comes from.** One function, `agents/executors/failure_reason.py` (spec D14),
builds the sentence. `registry.dispatch()` is the one place that calls it, for guardrail blocks
and for executor results alike. Its inputs are the target, the `failure_kind`, the HTTP status,
the attempt count, the guardrail, the G4 case and DMS's RO status word. Nothing else.

- **It never takes text from an exception, a response body or the caller.** A reason therefore
  cannot carry a token, an internal hostname or a driver's free text.
- Writers never add their own text to `failure_reason`, and never put an exception message in
  `note`. `note` may carry an error class name such as `ConnectionError`.
- **It never raises.** Any input the table below does not name exactly gets the fallback
  sentence. That includes an unknown target or `failure_kind`, a guardrail with a `failure_kind`
  other than `target_refused`, and a G4 case with no guardrail. The input is never echoed.
- `<RO status>` is used only when it is one of DMS's seven status words: `Draft`, `InProgress`,
  `AwaitingParts`, `Ready`, `Delivered`, `Closed`, `Invoiced` (source: DMS
  `source/handlers/repair_orders.py`, the status vocabulary above `RO_ACTIVE_STATUSES`). The match
  is exact and case-sensitive.
  Anything else is written as `unknown`.

**`error_status`** is the HTTP status the target returned to the dispatch, when there was one.
- It is omitted when there was none. It is never `null`.
- It is never written on a guardrail block, because a guardrail never sends the Action. G4's
  repair-order lookup is a read, and its status stays in the logs.
- The sentence and `error_status` always agree: a sentence that names a status names the
  `error_status` on the same entry.

**Target names.** `dms` → DMS, `cms` → CMS, `adp` → ADP, `retail` → the retail system, `vehicle-command` → the
vehicle, `ios-owner` → the owner's app. `chat` and `none` use the target-free sentence. At the
start of a sentence the first letter is capitalised ("The retail system refused…"). `ADP` is
always all-caps whether at sentence start or mid-sentence.

**`<n>`** is the audit entry's `retry_count`: the number of attempts made. One attempt reads
"after 1 attempt"; more read "after 3 attempts".

The first row that matches wins:

| # | When | `failure_reason` |
|---|---|---|
| 1 | G1 | `Blocked: exempting a safety recall needs a fleet operator.` |
| 2 | G2 | `Blocked: grounding a vehicle needs a fleet operator.` |
| 3 | G3 | `Blocked: sending a warranty claim needs a fleet operator.` |
| 4 | G4, decided by a non-operator | `Blocked: completing a recall needs a fleet operator.` |
| 5 | G4, no `service_record_ref` | `Blocked: completing a recall needs a service record.` |
| 6 | G4, no resolver configured, or the DMS lookup failed (non-404 error, timeout) | `Blocked: the service record could not be checked.` |
| 7 | G4, DMS has no such repair order (404) | `Blocked: the service record could not be found.` |
| 8 | G4, repair order for another vehicle | `Blocked: the service record is for a different vehicle.` |
| 9 | G4, repair order not completed | `Blocked: the service record is not complete (status <RO status>).` |
| 9a | G5 | `Blocked: engineering decisions need a product engineer.` |
| 10 | Target `chat` or `none`, any `failure_kind` | `AVX could not complete this action.` |
| 11 | Target `cms`, `target_refused`, no status (Stage A's `CmsExecutor`) | `CMS cannot carry out this action yet.` |
| 12 | `target_refused`, with a status | `<Target> refused the request (HTTP <status>).` |
| 13 | `target_refused`, no status | `<Target> refused the request.` |
| 14 | `executor_exhausted_retries`, with a status, `<n>` ≥ 1 | `<Target> was unavailable (HTTP <status>) after <n> attempts.` |
| 15 | `executor_exhausted_retries`, no status, `<n>` ≥ 1 | `<Target> did not respond after <n> attempts.` |
| 16 | `executor_exhausted_retries`, no status, `<n>` = 0 (the time limit ran out first) | `AVX ran out of time before it could contact <target>.` |
| 17 | `executor_terminal_error`, with a status | `<Target> did not accept the request (HTTP <status>).` |
| 18 | `executor_terminal_error`, no status | `AVX could not complete the request to <target>.` |
| — | Anything else | `AVX could not complete this action.` |

Rows 1–9 need `failure_kind: target_refused` and no status. Rows 1–3 take no G4 case. Rows 4–9
take the case shown, and only row 9 takes an RO status.

Which outcomes are reachable today:
- **DMS bookings** (`book_recall_service`, `book_combined_visit`, `schedule_inspection`):
  DMS answers a booking it rejects (an unknown `dealer_id`, a malformed body) with 400. AVX
  records it as `failed` / `executor_terminal_error` (decision D-G10-8), so the record reads
  "DMS did not accept the request (HTTP 400)." and escalates. AVX records a DMS 422 as
  `refused`, but DMS's booking routes send no 422 today.
- **DMS fleet bookings** (`book_fleet_service`, Stage D new): same DMS path; operator token
  forwarded.
- **DMS warranty claims** (`submit_warranty_claim`, Stage C new): DMS answers 201 (Submitted →
  `executed`), 202 (Validated → `executed`), 422 (coverage denied → `refused`), 400 (bad
  request → `failed`). Row 12 applies to the 422 case, row 17 to the 400 case.
- **CMS grounding** (`ground_vehicle`, Stage C new): CMS answers 2xx (→ `executed`), 4xx (→
  `refused`, row 12), 5xx exhausted retries (→ `failed`, rows 14–16).
- **ADP engineering actions** (`record_engineering_decision`, `issue_recall`,
  `issue_software_campaign`, Stage D new): `AdpExecutor` invokes the digital-thread-writer
  Lambda in us-east-1. It answers 200 (→ `executed`), 422 (→ `refused`, row 12), 400 (→
  `failed`, row 17); timeout → row 16.
- **The vehicle** answers 400, 409 and 422 with a refusal (row 12).

**G4 check order.** Matching rows 4–9, G4 checks in this order and stops at the first failure:
1. the decider is an operator;
2. `service_record_ref` is present;
3. a resolver is configured and the lookup succeeds;
4. the repair order exists;
5. its `vehicle_vin` equals `parameters.vehicle_vin`;
6. its status is `Ready`, `Delivered` or `Invoiced`.

The operator check comes first, so AVX never reads DMS on behalf of a decider who could not
complete the recall anyway.

The schema enforces the vocabulary: `failure_reason` has a `pattern` that admits exactly these
sentences, so CMS and DMS can check what they read.

---

## 10b. The target's own refusal words (v2.8)

When a deterministic target refuses an Action, the operator reads the target's own words, not
only AVX's § 10a sentence: CMS's "Vehicle <vin> is grounded and can't be moved." rather than
"CMS refused the request (HTTP 409)." A deterministic seam's refusal is narrated verbatim
(`~/.kiro/steering/agentic-tiers.md`). The text goes in the audit entry's `target_reason`;
`failure_reason` keeps the fixed § 10a vocabulary and is still written beside it, and is what the
screen shows when there is no `target_reason`. AVX never rewords the target's text: DMS's
warranty verdict is a reason code (`miles_term_expired`, `date_term_expired`, or a rule's
`denial_reason_code` such as `labor_op_mismatch`; DMS `source/_lib/warranty_rules.py`), and it is
recorded and shown as that code.

**Where it comes from.** An executor reads one named field from the response to a refusal, and
only from a JSON object body of at most 4096 bytes. It does not clean or judge the text.

| Target | Field | When |
|---|---|---|
| `cms` | `error` | Any refusal `classify_status` calls `refused` (a 4xx other than 401, 403, 404, 405, 415, 408, 425, 429): the FI move routes (operator and SigV4 internal), the grounding route and the cost-action route all answer `{"error": "<sentence>"}`. Not a move 409 after an attempt whose outcome is unknown, which is `failed`. |
| `dms` | `verdict_reason` | A 422. The executor reads it only on `submit_warranty_claim`'s 422 (coverage denied): DMS's deterministic warranty verdict, a reason code. |

**When it is kept.** `registry.dispatch()` is the one place that records it
(`agents/executors/target_reason.py` `kept_target_reason`), as it is for `failure_reason`. It keeps
the text only when the result is `target_refused`, the status is a 4xx the table above allows for
that target, and the text passes cleaning. Anything else an executor carried is dropped, so a 5xx
body, a terminal error, a guardrail block or a success never records one.

**Cleaning** (`clean_target_reason`). The text comes from another system, so it is untrusted:
- not a string → dropped;
- every whitespace character (newline, tab, no-break space, line separator) becomes one space;
  control, format (bidi overrides, zero-width characters, BOM), surrogate, private-use and
  unassigned characters are removed; runs of spaces collapse; the ends are trimmed;
- empty, longer than 200 characters, or containing `<` or `>` → dropped whole. It is never cut,
  so the screen never shows a fragment that reads differently from what the target said.

An ordinary sentence comes back unchanged. Input longer than 4096 characters (more than any body
the executor parses) is dropped before cleaning. The Action schema's `target_reason` `pattern`
refuses the control, bidi and zero-width characters and `<` `>`, as a backstop.

**What CMS shows.** CMS shows a stored `target_reason` only on a `declined` /
`target_refused` entry with no `guardrail`, only for the targets and statuses in the table above
(`cms`: a refusal-class 4xx; `dms`: 422), and only when the stored text is exactly what the
cleaning produces. It renders it as text. Both sides' rules are pinned to each other by CMS's
`test_fi_action_queue_storage_pin.py`.

**Unicode versions.** Removing unassigned characters depends on the interpreter's Unicode
database: AVX's approve route runs Python 3.12, CMS's `main_api` runs 3.9. A character newer
than CMS's Unicode version (for example U+1F6DE) is kept by AVX and treated as unassigned by CMS,
so CMS shows `failure_reason` for that entry. The mismatch only ever falls back to the fixed
sentence.

**Who else sees it.** It is FI-only today (CMS and DMS warranty refusals are FI items), so the
iOS routes never return it (§ 12). AVX's own `GET /actions/vehicle/{vin}` returns full audit
trails to service-advisor and platform-admin callers, so they receive it too: render it as text.
The `fleet_manager` chat tool's audit-entry allowlist deliberately omits it: target text does
not go into a model's context.

---

## 11. Two fleet checks on approval

```
CMS screen → CMS FI API (check 1: CMS fleet scope)
  → AVX POST /findings/{id}/approve or /decline, with the operator's token
      check 2: _check_approve_scope + operator standing (authoritative)
      decided_by from the token → registry.dispatch() → guardrails → executor
```

- **With check 1 removed**, a CMS operator who tries to decide another fleet's item still gets
  AVX's 403.
- **With check 2 removed**, CMS's screens still refuse, but anyone holding a valid CMS-pool token
  could decide any fleet's item by calling AVX directly.
- The approver is always the verified token's `sub`, so neither check can be made to record
  someone else.

Both checks have removal tests.

---

## 11a. How CMS serves the queue

CMS's routes are `/api/v1/fleet-intelligence/actions` and `/api/v1/fleet-intelligence/approval-rules`,
served by CMS main_api and documented in CMS
`modules/cms_ui/source/handlers/main_api/docs/fi-action-queue-api.md`.

- **Reads** go straight to the four tables in § 7. Findings and Actions are read within the
  caller's CMS fleet scope: `platform-admin` and `fleet-viewer` read every CMS fleet;
  `fleet-operator` and `fleet-guest` read the fleets in `custom:fleetIds`. Each Finding and Action
  row is checked against the fleet partition it came from. Run records are read per agent.
  CMS's main_api role already allows DynamoDB on `table/*` in the account; CMS writes nothing to
  these tables.
- **Writes** (approve, decline, rule `PUT` and `DELETE`) go to this API with the caller's own token,
  after CMS's own fleet check; this API then checks the fleet again (§ 11 for approve and decline,
  § 6 for rules). CMS refuses redirects
  and answers 503 `approvals_unavailable` when its `AVX_API_ENDPOINT` is unset.
- **To the browser** CMS returns Findings, Actions and rules only through § 7's allowlist, and
  run records as each agent's last-run time and status. `decided_by.id` and `created_by` stay
  server-side; CMS shows whether the caller made the decision, not who did.

---

## 12. Other consumers

- **iOS.** The shipped CMS iOS client decodes Findings and Actions strictly. Its agent-id,
  action-kind and target-system enums have no unknown fallback, so one FI Finding or FI Action in
  a response fails the whole response.
  - AVX therefore keeps FI items out of every route iOS reads. `GET /findings/me`, and any other
    route an iOS screen calls, excludes:
    - Findings whose `agent_id` starts with `agent-fi-` or that carry `recommended_action`;
    - Actions that carry `decision` (every FI decision, whatever its kind, including an FI
      `acknowledge_only`) or whose `action_kind` is one of the v2.5 FI kinds.
  - The AVX broker (D13, task 1.8) applies the same predicate to skip dispatching FI items
    to the push service: any item matching either clause above is not forwarded. Both consumers
    of the predicate are mutation-tested. The broker-skip also covers `agent-fi-cost` Findings
    from Stage B; no Stage B change is needed.
  - **No escalation card either.** When a decision Action fails, `registry.dispatch()` fires the
    operator half of the D5 escalation (the `AVXActionTerminalFailure` metric and its alarm) and
    skips the owner-visible card. The operator made the decision and sees the reason in the queue
    (§ 3); a card would tell the vehicle's driver about a booking they never asked for. A
    decision Action carries no `owner_id` today, so the card is already skipped; this rule
    keeps it skipped if a later writer adds one.
  - Live tests write FI Findings only to fleets no iOS test user holds.
  - **Fixture pin state**: the CMS iOS fixture pins (`FixtureHashesTests.swift`) and DMS
    vendored contract pins (`tests/unit/test_avx_fixtures_byte_identical.py`) were refreshed to
    v2.5 on 2026-10-05 by spec `2026-10-05-fi-recall-warranty-stage-c` task 1.9 (D14; commit
    `a8ef844` AVX, `6b68583c` CMS, `cb68ebf` DMS). v2.6's Finding and Action JSON schemas are
    unchanged (prose-only OpenAPI update). v2.7 changed both JSON Schema files and v2.8 changed
    the Action JSON Schema (one optional audit-entry field, § 10b); both changes are additive
    (backward-compatibility statement in `docs/fi-schema-v2.7-proposal.md`). **Both copies and
    their pins were refreshed to v2.8 on 2026-10-23 (CMS `d3aa1aeb7`, DMS `28c77e8`; issue
    `2026-10-23-contract-copies-behind-v2-8`).** The next schema change makes them stale again
    until refreshed: `CONTRACT_COPIES=1 test_contract_copies.py` and
    `scripts/check-contract-copies.py` report stale copies; each repo's own pin test cannot,
    because its pin follows its copy. v2.9 (2026-11-08, `decided_by.kind` += `simulated_fleet_operator`)
    left both copies behind: `check-contract-copies.py` reports them STALE until they are refreshed.
    No runtime path validates Actions against either copy: DMS's internal routes read stand-in
    Actions with no schema, and the routes iOS reads exclude FI decision Actions (above). iOS decodes unknown enum values without crashing since
    CMS `91e40864`; DMS's pre-brief agent ignores Findings with no VIN; the v2.8 field is FI-only,
    so neither consumer receives it.
- **DMS.** DMS's pre-brief agent reads the findings table by storage shape. Its vendored contract
  pins (`tests/unit/test_avx_fixtures_byte_identical.py`) are at v2.8 (refreshed 2026-10-23).

---

## 13. Cross-references

- [`docs/identity-and-standing-contract.md`](identity-and-standing-contract.md): standing model,
  `fleet_manager` standing (§ 3).
- [`docs/api/avx-findings-actions.openapi.yaml`](api/avx-findings-actions.openapi.yaml): routes
  and schemas.
- `agents/tier2/schemas/finding.schema.json`, `agents/tier2/schemas/action.schema.json`.
- `deployment/config/avx-policy-defaults.json`: auto-approval limit defaults.
- Spec: `.kiro/specs/2026-10-02-fi-action-queue-stage-a/spec.md`.
