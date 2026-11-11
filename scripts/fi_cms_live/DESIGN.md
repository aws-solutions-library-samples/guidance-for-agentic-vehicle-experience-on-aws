# FI stage D 7.7 and 8.4 live checks: design and build brief

Owner: the stage D 7.7/8.4 window (AVX spec `2026-10-05-fi-fleet-health-stage-d`, tasks 7.7 and
8.4). **Code only while building**: no AWS writes, no deploys, no landing, no pushes. Read-only
`describe`/`get`/`list` calls to capture real shapes are allowed (scrub to account
`123456789012`).

- Branch `wip/fi-health-d-g78-lv` from AVX main `861a366e`, worktree `~/cvx-wt-fi-health-d-g78-lv`.
  Commit by path only (`git commit -m ... -- <paths>`); `.venv` and `infrastructure/node_modules`
  are untracked symlinks.
- Pinned cross-repo sources, read with `git -C <repo> show <ref>:<path>` or `git archive <ref>`,
  never through a working tree: CMS main `1ce93a75b`, ADP main `895f7b61`, DMS main `de6c181`.
  The world loads cross-repo code from these commits only (no fallback to a working tree).
- Fenced (read-only references at most, never edited): AVX `wip/fi-health-d-g93*` (9.3 window),
  AVX `wip/fi-health-d-g6-lv` (6.3 window), ADP `fix/fi-health-d-thread-lake`.
- Scratch: `/tmp/2026-10-05-fi-fleet-health-stage-d-g78-<purpose>`.

## Layout

```
scripts/fi_cms_live/__init__.py
scripts/fi_cms_live/harness.py        shared: stage gate, botocore allowlist guard, clients,
                                      Results/exit codes, signals, run manifest, Cognito temp
                                      users (CMS pool), CMS/AVX/DMS HTTP, windows, residue
scripts/live-verify-fi-cms-fleet.py   task 7.7 (fleet track), parts below
scripts/live-verify-fi-cms-maker.py   task 8.4 (maker track), parts below
scripts/tests/fi_cms_world/           shared fake world, extends scripts/tests/fi_rw_world
scripts/tests/test_live_verify_fi_cms_*.py
```

Both entry scripts put the repo root on `sys.path` first (as `live-verify-fi-cost.py` does) and
import `scripts.fi_cms_live.harness`.

## What each script proves (tasks.md Accept)

### 7.7, `live-verify-fi-cms-fleet.py`

Preconditions read in `pre`: the CMS FI health cache is fresh (`computed_at` < 2 h; the
`cms-staging-ui-fi-health-views-stale` alarm OK); `FAULT_GROUNDING_ENABLED` on the health agent's
live env (the `ground` part requires `true`, every other part records it); the migration's 37
`plan-mig-*` plans exist in `cms-{stage}-storage-pm-plans` (record count; not a gate for `perf`).

| Part | Writes | Items |
|---|---|---|
| `pre` | none | stage/identity/windows; the preconditions above; residue snapshot of this script's own markers; pick the `ground` target (below); write the manifest |
| `perf` | temp CMS user | `GET /api/v1/fleet-intelligence/faults` p95 < 3 s, 20 sequential requests, by a fleet-operator whose `custom:fleetIds` holds all four demo fleets (the managed fleet's size, 4,250 vehicles); also `/faults/summary` recorded |
| `isolation` | temp CMS users | fleet A operator vs fleet B on every stage D fleet route (list below): each probe refused (exact status and body from the code) with a same-route own-fleet control that must succeed; write routes' controls are requests refused AFTER the scope check (validation 400), so no probe or control writes |
| `grounding-items` | none | after a service run with the switch on: every managed vehicle the run selected that has an open p0 fault has exactly one pending `ground/<n>` Finding (`ground_vehicle`, p0, its fleet) whose `disclosure_txt` equals the catalog directive byte for byte; no `ground/<n>` Finding exists for a vehicle without an open p0 fault |
| `ground` | `--allow-writes --allow-managed-writes` | manifest-recorded sub-steps, resumable with `--run-id`: (1) approve the target's `ground_vehicle` Finding through AVX as a temp fleet-operator of its fleet; CMS records a fault grounding (consistent read of `cms-{stage}-storage-vehicle-groundings`); (2) return to service refused 409 `fault_not_repaired` naming the code; (3) remote-clear record: put a `LIVETEST-<run>` row in `cms-{stage}-storage-dtc-history` for the code already in its remote-cleared state (`CLEARED_REMOTE`, `clearedBy`, `clearedDate`; never ACTIVE, so no reader sees an active code), then return to service still refused `fault_not_repaired`; the row is deleted in `finally` and confirmed gone; (4) a DMS repair order on the target VIN with the code's mapped labor op (`code_labor_op_map`), marked `LIVETEST-<run>`, not an FI booking (no `need_id`, no recall line), moved to `Ready` by a temp DMS service advisor; (5) wait, across runs of the part (SKIP while waiting), until the CMS health cache shows the code's `repairRef` = that RO; (6) return to service accepted 200. The RO is kept (it is the vehicle's repair of record) and recorded |
| `cleanup` | `--allow-writes` | delete every temp user and the dtc-history row; confirm each by a read; residue = manifest's planned set exactly (the grounding history and the RO are planned) |

`ground` target: a managed VIN (vehicle row in `cms-{stage}-storage-vehicles`, one of the four
demo fleets, not in the live tier roster) with an open p0 fault in the health cache and no
`repairRef`, a pending `ground/<n>` Finding, no open CMS grounding, no open booking for it in
AVX Actions or DMS, and no open DMS RO. Pick deterministically (sorted VIN, first that passes),
record why each earlier candidate was skipped.

Stage D fleet routes for `isolation` (read every handler and its scope check from CMS
`1ce93a75b`, `services/fleet_intelligence/index.py`, `_auth.py`, `pm_plans.py`,
`groundings.py`, `health_source.py`): GET `/faults`, `/faults/summary`, `/vehicles/{vin}/faults`,
`/health-findings`, `/pm/compliance`, `/pm/schedules`, `/pm/plans`; POST `/pm/plans`; PUT
`/pm/plans/{planId}`; POST `/pm/plans/{planId}/complete`; POST `/groundings` (fault form);
POST `/groundings/{vehicleId}/return-to-service`.

### 8.4, `live-verify-fi-cms-maker.py`

Preconditions in `pre`: the population cache is fresh (`cms-staging-ui-fi-population-views-stale`
OK; it is in ALARM until ADP registers `adp_{stage}_engineering.digital_thread`); the
investigations queue and its DLQ are 0/0; the investigation event source is Enabled.

| Part | Writes | Items |
|---|---|---|
| `pre` | none | as above; residue snapshot; pick the pattern (below); manifest |
| `perf` | temp CMS user | p95 < 3 s of `GET population/patterns` and `GET population/signals`, 20 each, as a `product-engineer` |
| `scope` | temp CMS users | a fleet-operator gets 403 (`Not authorized for the requested population scope`) on every maker route, GET and POST, with the engineer's same request as control (POST controls must be refused after the scope check, never a write); a `connected-services` reader gets 200 on the GETs and 403 on start, decision, decline and issue (POST controls likewise); every maker GET response, every page, is scanned: keys outside the route's allowlist and the forbidden values (fleet id, driver, owner, customer, latitude, longitude, trip, cost, dealer) inside strings fail |
| `investigate` | `--allow-writes` | `POST population/patterns/{id}/investigations` as the engineer; poll `GET population/investigations/{id}` until `completed` (cap, then SKIP with the resume command); the result has a root cause and options |
| `decide` | `--allow-writes` | `POST population/investigations/{id}/decision` with the option whose kind is `software_update`; the AVX Action reaches its terminal state; a thread record exists (`GET population/threads/{threadId}`) with a draft; the `quality.issue` Finding appears |
| `issue` | `--allow-writes` | `POST population/threads/{threadId}/issue`; the campaign is issued (`GET population/campaigns/software` after the next refresh) and the thread has a new version |
| `cleanup` | `--allow-writes` | delete the temp users; residue = the manifest's planned set (the investigation, decision, thread record and issued campaign are planned and kept for the user's browser check; recorded by id) |

Pattern pick: a pattern present in the population cache and in `adp-{stage}-fault-patterns`;
NOT one of the answer key's planted patterns matched by ADP's own `_matches`
(`engineering/tests/test_patterns_on_simulation.py`), so stage D 6.4 keeps every planted pattern;
NOT a supplier-lot cause (9.3's recall leg); no lock row `lock#<pattern_group_id>` in
`vsa-{stage}-avx-investigations`; and whose corpus remedy options for the top candidate's failure
mode include `software_update`. C5: never POST a pattern that doesn't exist while the event source
is Enabled.

## Facts to establish from code

Read the real code at the pinned commits; cite `repo:path:line` in a `FACTS` section of each
script's module docstring. Don't copy names from older scripts or this brief without checking.
Reference implementations to PORT from (read with `git show`, never import): AVX
`83553340:scripts/live-verify-fi-fleet-health.py` (6.3; through general and security review) for
the guard, `principal()` temp users, manifest, signals and residue helpers; AVX
`49ff3719:scripts/live-verify-fi-loop.py` (9.3, in review) for the investigation, decision and
issue calls and the DMS stand-in rules. Repo memories `cvx-liveverify-contracts`,
`fi-fleet-health-lv-facts` and `live-verify-script-safety` list the traps those scripts hit.

## Rules every part follows

- `--stage staging` required; refuse any other stage before creating a client. Validate
  `--run-id` against `^[A-Za-z0-9-]{1,64}$` before any AWS call.
- Every client through one guarded factory: a `before-call.*.*` allowlist per part keyed
  `service_name.Operation`, writes only in write parts, Cognito writes only for the run's own
  temp usernames, DynamoDB writes only to the keys the manifest recorded first. A test per part
  builds that part's real clients and sends each operation it makes through the guard at a dead
  endpoint (allowed → `EndpointConnectionError`, refused → `GuardViolation`).
- Write windows: refuse writes 04:00-06:15Z and within 45 min of any cron schedule in
  `vsa-{stage}-tier2-*` and `dms-{stage}-fi-*`; re-check before each write.
- Exit 0 only if every selected item ran and passed; any FAIL 1; any SKIP 2. PASS rows carry a
  positive detail; failure text only on FAIL.
- One `try/finally` from the first write: cleanup and the residue check run exactly once on every
  exit, signals held through cleanup (SIGINT, SIGTERM, SIGHUP).
- Temp users: deterministic `livetest-<run>-<role>@example.invalid`, recorded in the manifest
  before `AdminCreateUser`; delete confirmed by `AdminGetUser` → `UserNotFoundException`.
- Isolation probes never approve or change a real item: refusals only, with a positive control.
- Paging: follow every `LastEvaluatedKey` / `NextToken` / `next_token`; URL-quote cursors; cap pages
  and fail closed.

## The fake world, `scripts/tests/fi_cms_world/`

Extends `scripts/tests/fi_rw_world` (moto for DynamoDB/S3/SQS/Lambda config/Scheduler/CloudWatch/
SNS/Logs/SSM, `FakeRestApi` running real handlers, `FakeUserPool` JWTs, `FakeAthena` on DuckDB,
`WorldClock`). Adds, each in its own module:

1. `cms_fi.py`: the real CMS FI handler (`services/fleet_intelligence/index.py`, CMS `1ce93a75b`,
   extracted with `git archive` into a temp dir) behind a `FakeRestApi` with the CMS pool's
   authorizer claims (`cognito:groups`, `custom:fleetIds`, `sub`, `email`), its live-shaped env,
   and its tables. Every stage D fleet route and every maker route is served.
2. `adp_faults.py`: ADP's real `vehicle_fault_status.sql` and `pm_vehicle_status.sql` (ADP
   `895f7b61`) run on DuckDB through ADP's own `fleet_fault_events/view_runner.py` translation,
   over tables seeded from ADP's generator shapes (`fleet_ops.faults`, `fault_codes`,
   `vin_cms_fleet`, `dms_{stage}.repair_orders` images, `parameters`); a DMS RO status change in
   the world appends a lake image, so a qualifying repair reaches the view.
3. Health refresh: the real CMS `refresh-health-cache` task run in-process on clock advance (hourly
   at :47) against FakeAthena serving those views.
4. `maker.py`: the population caches from the real CMS `refresh-population-cache` over FakeAthena
   tables shaped like the ADP views it reads (columns from its own SQL); the AVX investigations
   route (real `lambdas/api-avx-investigations/handler.py`) with its table and moto SQS; the real
   Tier 2 investigation component (`agents/tier2/runner.py`, rules path) on queue pump; the real
   ADP thread writer (`platform-foundation/source/lambda/digital_thread_writer/handler.py`, ADP
   `895f7b61`) behind `AdpExecutor`'s Lambda invoke.
5. Live shapes captured read-only into `data/live_shapes.json` (scrubbed).

`SOURCES.md` lists what is not real code and why. Views the world can't run from the real SQL are
named there, and only the live run checks them.

## Build stages (one module or one part per stage; commit after each step)

W1 world: `cms_fi.py` + `world.py` subclass + smoke test (GET `/faults` from a seeded cache).
W2 world: `adp_faults.py` + health refresh + RO-to-repair flow test.
W3 world: `maker.py` pieces + an investigation-to-issue flow test.
H harness + its tests. F1 fleet `pre`/`perf`/`isolation`. F2 fleet `grounding-items`/`ground`/
`cleanup`. M1 maker `pre`/`perf`/`scope`. M2 maker `investigate`/`decide`/`issue`/`cleanup`.
Each part's tests drive the real `main()` against the world end to end, plus one mutant per
property it guards.
