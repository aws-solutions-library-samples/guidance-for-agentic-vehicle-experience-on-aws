# fh_world — Design Note

**Purpose**: shared in-process world for `scripts/tests/test_live_verify_fi_fleet_health_item*.py`
that runs the **real** fi_fleet_health producers (service runner, ADP views, DMS handlers,
AVX findings API, DMS event consumer) so each item test asserts against produced output,
not mocked data shapes.

**Base**: subclass or reuse `scripts/tests/fi_rw_world/` exactly as fi_rw_world subclasses
tier-2 infra.  Every module in fi_rw_world (`alarm`, `apigw`, `athena`, `awsfake`, `bus`,
`clock`, `cognito`, `lambdas`) is reused unchanged.  `fh_world/world.py` replaces `world.py`
only in the *scheduled run* section (runs the service health agent rather than recall/warranty)
and the *ADP lake* section (installs fleet_fault_events views instead of recall_vehicle_status).

---

<!-- sections appended below -->

## Item 1 — U7 nightly coverage: every managed VIN evaluated; fallback → 7-day cycle evidence

### AWS calls `_item_1` makes
| Service | Operation | Key params | Allowlist |
|---|---|---|---|
| dynamodb | Query | `runs_table`, `agent_component` (pk) | `dynamodb.Query` |
| dynamodb | GetItem | `cursor_table`, pk=`CURSOR#{component}` sk=`STATE` | `dynamodb.GetItem` |
| athena | StartQueryExecution + GetQueryExecution + GetQueryResults | workgroup, pool-count SQL for each of 4 components | `athena.{Start,GetQuery*}` |

Guard allowlist: `{"dynamodb.Query", "dynamodb.GetItem", "athena.StartQueryExecution",
"athena.GetQueryExecution", "athena.GetQueryResults"}`.

### Real producer path
`agents/fi_fleet_health/runner.py` (service component) →
`agents/tier2/selection.py:_resolve_batch_managed_first` writes the cursor item
(`vsa-{stage}-health-findings`, pk=`CURSOR#service`, sk=`STATE`).
`agents/tier2/run_records.py:write_run_record` writes the run record
(`vsa-{stage}-avx-agent-runs`, pk=`agent-fi-fleet-health#service`, sk=started_at ISO).
Athena pool-count SQL reads `adp_{stage}_cms_ingest.vin_cms_fleet` — installed in FakeAthena
from `seed.VIN_FLEET`.

### World must seed
- `cms-staging-storage-vehicles` + `dms-staging-fleet-membership` + `cms-staging-storage-fleets`
  (reuse fi_rw_world `seed.VIN_FLEET`).
- `adp_staging_cms_ingest.vin_cms_fleet` table in FakeAthena with managed VINs.
- `adp_{stage}_fleet_health.vehicle_fault_status` snapshot (empty OK for item 1 positive control;
  fault events needed for items 2-3).
- `vsa-staging-avx-agent-runs` table (DDB, moto).
- `vsa-staging-health-findings` table with cursor GSI (DDB, moto).

### fi_rw_world coverage vs gaps
**Covered**: FakeLambda, FakeAthena, DDB tables via moto, `seed.VIN_FLEET` fleet structure.
**Missing**: HEALTH_FN (`vsa-staging-tier2-health-agent-us-west-2`) Lambda env; calling
`agents/fi_fleet_health/runner.py` instead of recall/warranty runner; `vin_cms_fleet`
Athena table installation; cursor table GSI shape; `agent-fi-fleet-health` run-record pk.

## Item 2 — Fleet-health items in correct fleet queues; p0 items carry catalog directive byte for byte

### AWS calls `_item_2` makes
| Service | Operation | Key params | Allowlist |
|---|---|---|---|
| dynamodb | Query | `avx-findings` by_fleet GSI `fleet_id=:fid` | `dynamodb.Query` |
| dynamodb | GetItem | `storage-vehicles` vehicleId | `dynamodb.GetItem` |
| s3 | GetObject | `adp-{stage}-foundation-lake-{acct}-{region}/fleet_health/fault_codes.parquet` | `s3.GetObject` |

Guard allowlist: `{"dynamodb.Query", "dynamodb.GetItem", "s3.GetObject"}`.

### Real producer path
`agents/fi_fleet_health/runner.py` → `agents/fi_fleet_health/writer.py:build_all_items` →
`agents/fi_fleet_health/write.py:build_finding` writes findings to `vsa-staging-avx-findings`
with `fleet_id` top-level attribute (T10.2 denormalization, `agents/tier2/writer.py`).
`agents/fi_fleet_health/bands.py:classify_needs` computes severity + p0 directive text from
`fault_codes.parquet` (same file `_load_fault_catalog_s3` reads).

### World must seed
- `adp-staging-foundation-lake-…/fleet_health/fault_codes.parquet` on moto S3 (parquet built
  from ADP `adp_code_labor_op_map.json` + `needs_config.yaml`; bands: p0/attention/soon/routine;
  one p0 row with `directive` matching `build_disclosure_txt` expectation).
- At least one managed VIN in each of two fleets: one with a p0 fault event (directive line), one
  with an attention fault, one with a PM-due task; all present in `vehicle_fault_status` snapshot.
- `cms-staging-storage-vehicles` with vehicleId→VIN mapping.
- `vsa-staging-avx-findings` with `by_fleet` GSI — moto creates GSI from table definition in
  `live_shapes.json`; the health runner's PutItem writes fleet_id as top-level attr.

### fi_rw_world coverage vs gaps
**Covered**: `avx-findings` table + `by_fleet` GSI (live_shapes.json), `storage-vehicles` table,
moto S3 bucket creation.
**Missing**: `fault_codes.parquet` content + S3 key; ADP lake bucket name constant; fleet_id
denormalization (writer.py T10.2 must be confirmed in fi_fleet_health's writer — verify
`agents/fi_fleet_health/write.py` calls `agents/tier2/artifact_writer.py` which sets `fleet_id`).

## Item 3 — `book_fleet_service` → DMS RO with lines; DMS status → Action status; closed-early → `cancelled` and re-raise

### AWS calls `_item_3` makes
| Service | Operation | Key params | Allowlist |
|---|---|---|---|
| dynamodb | Query, GetItem, Scan | `avx-findings`, `avx-actions`, `storage-vehicles`, `avx-agent-runs` | `dynamodb.{Query,GetItem,Scan}` |
| cognito-idp | AdminCreate/SetPassword/AddGroup/Delete, InitiateAuth, ListUserPools/Clients, DescribeUserPoolClient | pool\_id, username `LIVETEST-…` | cognito-idp ops |
| ssm | GetParameter | `/cvx/{stage}/voice-auth-user-pool-id` | `ssm.GetParameter` |
| cloudformation | DescribeStacks | CMS stack | `cloudformation.DescribeStacks` |
| lambda | Invoke | `vsa-{stage}-tier2-health-agent-{region}` | `lambda.Invoke` |
| s3 | GetObject | `fault_codes.parquet` | `s3.GetObject` |
| HTTP (AVX REST) | POST `/findings/{id}/approve` | finding_id, JWT | (handled by FakeRestApi) |

### Real producer path
`lambdas/api-avx-findings/handler.py:handler` (approve route) →
`agents/supervisor/lambdas/avx_broker.py` dispatches `book_fleet_service` →
`agents/fi_fleet_health/dms.py:_dispatch_fleet_service` calls
`source/handlers/repair_orders.py:create_fleet_repair_order` (DMS FakeRestApi) →
writes RO to `dms-staging-repair-orders`; EventBus emits DMS status-change event →
`lambdas/api-avx-dms-event-consumer/handler.py` updates Action status in `avx-actions`.
Closed-early path: `_apply_cancel` in consumer writes `cancelled` + audit_trail.
Re-raise: `agents/fi_fleet_health/attempts.py:should_retry_health` returns True for
`cancelled`; next run's `maybe_write` will mint a new finding.

### World must seed
- One fleet-health finding for a managed VIN with a booking recommended_action
  (written by the health runner in world.open()).
- `avx-actions` table (moto DDB) with GSI `by_finding_id`.
- `dms-staging-rooftops` with at least one dealer.
- `dms-staging-fleet-membership` synced.
- SSM parameter `/cvx/staging/voice-auth-user-pool-id` → FakeUserPool pool_id (via SSM mock or
  router override; fi_rw_world uses `_api_env` SSM mock pattern).
- `FLEET_SYNC_MAX_AGE` check in `fleet_membership` must pass (world's `_refresh_marker`).

### fi_rw_world coverage vs gaps
**Covered**: FakeRestApi for AVX + DMS, `repair_orders` handler loaded, `consumer` handler loaded,
EventBus, FakeUserPool, `avx_broker` loaded, avx_api routes including `/findings/{id}/approve`.
**Missing**: `avx-actions` table DDB shape + GSI; SSM parameter mock for pool-id; `avx-actions`
pk/sk (need to check `avx-actions` schema in live_shapes.json vs fi_fleet_health broker output).

## Item 4 — PM: recompute due/overdue/current from raw sources; match view on every task

### AWS calls `_item_4` makes
| Service | Operation | Key params | Allowlist |
|---|---|---|---|
| athena | StartQueryExecution + GetQueryExecution + GetQueryResults | multiple queries against `adp_staging_cms_ingest.vin_cms_fleet`, `adp_staging_fleet_health.pm_vehicle_status_daily`, `adp_staging_fleet_health.parameters`, `adp_staging_cms_ingest.pm_operator_completions`, `adp_staging_service_records.service_records`, `dms_staging.repair_orders`, `adp_staging_fleet_health.code_labor_op_map`, `adp_staging_fleet_ops.daily_usage` | `athena.{Start,GetQuery*}` |

Guard allowlist: `{"athena.StartQueryExecution", "athena.GetQueryExecution", "athena.GetQueryResults"}`.

### Real producer path
`adp-wt-fi-health-d-g6` (ADP repo): `platform-foundation/source/data-products/fleet_fault_events/views/pm_vehicle_status.sql`
run by `fleet_fault_events/view_runner.py:run_pm_vehicle_status(...)` (DuckDB, no AWS).
The daily snapshot `pm_vehicle_status_daily` is populated by ADP's daily job;
in the world, FakeAthena installs the DuckDB-rendered result of `run_pm_vehicle_status()`
as an Athena table so the script's queries return the same rows the view would produce.
ADP's `view_runner._apply_trino_to_duckdb` handles the two Trino→DuckDB translation rules.
`fleet_fault_events/tests/fixtures.py:build_pm_plans_df`, `build_service_records_df`,
`build_daily_usage_df`, `build_repair_orders_df` provide the raw input DataFrames.

### World must seed
- FakeAthena tables: `adp_staging_cms_ingest.vin_cms_fleet` (VIN × fleet_id × tier=managed),
  `adp_staging_fleet_health.pm_vehicle_status_daily` (DuckDB view output, snapshotted to today),
  `adp_staging_fleet_health.parameters` (due_horizon_days, due_horizon_miles, data_as_of),
  `adp_staging_cms_ingest.pm_operator_completions`, `adp_staging_service_records.service_records`,
  `dms_staging.repair_orders`, `adp_staging_fleet_health.code_labor_op_map`,
  `adp_staging_fleet_ops.daily_usage`.
- At least: one calendar-PM-overdue VIN, one mileage-PM-due VIN, one PM-no-history VIN,
  one PM completed via DMS RO — matching `fixtures.py` named roles.

### fi_rw_world coverage vs gaps
**Covered**: FakeAthena DuckDB engine, `_refresh_lake` pattern (tables rebuilt before queries),
moto S3 / DDB infrastructure, `dms_staging.repair_orders` table.
**Missing**: ADP `view_runner.run_pm_vehicle_status` call during world setup; 7 new Athena table
registrations; `parameters` table with `data_as_of` = world clock date; ADP DMS root path
constant for `view_runner` SQL lookup (ADP repo is at a separate path in `adp-wt-fi-health-d-g6`).

## Item 5 (AVX half only) — Two-fleet isolation: fleet A operator sees no fleet B item

*CMS half (CloudFront + CMS API isolation) moved to Group 7 per user decision.*

### AWS calls `_item_5` makes (AVX half)
| Service | Operation | Key params | Allowlist |
|---|---|---|---|
| dynamodb | Query | `avx-findings` by_fleet GSI `fleet_id=:fid` | `dynamodb.Query` |
| dynamodb | PutItem, DeleteItem | `avx-findings` (plant + cleanup) | `dynamodb.PutItem`, `dynamodb.DeleteItem` |
| cognito-idp | AdminCreate/SetPassword/AddGroup/Delete, InitiateAuth, ListUserPools/Clients, DescribeUserPoolClient | fleet A and B operator users | cognito-idp ops |
| ssm | GetParameter | `/cvx/{stage}/voice-auth-user-pool-id` | `ssm.GetParameter` |
| cloudformation | DescribeStacks | | `cloudformation.DescribeStacks` |

### Real producer path
`lambdas/api-avx-findings/handler.py` route `GET /findings/fleet/{fleet_id}` →
`_lib/avx_scope.py:check_fleet_scope` gates on caller's `custom:fleetIds` claim.
Claim trusted only when `AVX_FLEET_IDS_CLAIM_TRUSTED=true` in Lambda env.
The world plants one Finding with `fleet_id="flt-a"` and verifies fleet B's JWT cannot
read it via the same endpoint.  Isolation is structural: `by_fleet` GSI key = `fleet_id`
and scope guard returns 403 before the GSI query runs.

### World must seed
- Two fleets (`flt-a`, `flt-b`) in `cms-staging-storage-fleets` and `dms-staging-fleet-membership`.
- Two operator Cognito users, each with `custom:fleetIds` set to their own fleet id.
- `AVX_FLEET_IDS_CLAIM_TRUSTED=true` in the `avx_findings` Lambda env (set in `env_of`).
- A LIVETEST finding with `fleet_id="flt-a"` planted via DDB PutItem (manifest-tracked for cleanup).

### fi_rw_world coverage vs gaps
**Covered**: FakeUserPool (groups, claims), `avx-findings` by_fleet GSI, `avx_findings` handler
loaded, FakeRestApi JWT verification, `_refresh_marker` for fleet membership.
**Missing**: `AVX_FLEET_IDS_CLAIM_TRUSTED=true` in env_of (must be added to `live_shapes.json`
functions.avx_findings.Env or patched in world); SSM mock for pool-id; `run_main` method on
HealthWorld to call `_item_5` directly with world's URLs.

## Item 6 — Re-run on unchanged inputs: zero new finding ids, zero model calls, zero tokens

### AWS calls `_item_6` makes
| Service | Operation | Key params | Allowlist |
|---|---|---|---|
| dynamodb | Scan, Query | `avx-findings`, `avx-agent-runs` | `dynamodb.Scan`, `dynamodb.Query` |
| lambda | GetFunctionConfiguration, Invoke | `vsa-{stage}-tier2-health-agent-{region}` | `lambda.GetFunctionConfiguration`, `lambda.Invoke` |
| scheduler | ListSchedules, GetSchedule | EventBridge Scheduler group | `scheduler.ListSchedules`, `scheduler.GetSchedule` |

Guard allowlist: `{"dynamodb.Scan", "dynamodb.Query", "lambda.GetFunctionConfiguration",
"lambda.Invoke", "scheduler.ListSchedules", "scheduler.GetSchedule"}`.

### Real producer path
First run (during `world.open()`): `agents/fi_fleet_health/runner.py` runs the service component,
writes findings with `inputs_hash` = SHA-256 of ADP inputs.  Second run (triggered by item 6 via
`lambda.Invoke`): same inputs → `runner.py:maybe_write` calls `same_hash_model_written`
(imported from `agents/fi_recall_warranty.runner`) → returns True → skip entirely.
Run record: `artifacts_written=0`, `duplicates_skipped=N`, `llm_calls=0`.
EventBridge Scheduler mock: FakeScheduler (new module, or moto's scheduler support) must return
the service schedule so the window check passes.

### World must seed
- HEALTH_FN registered in FakeLambda with its env (same as first run env).
- EventBridge Scheduler mock returning a schedule entry for `service` component at 05:40 UTC
  (outside test window); moto supports EventBridge Scheduler via `mock_aws()`.
- After first run: `avx-findings` already has finding rows with `inputs_hash`.
- Second Lambda invoke must re-use the same ADP Athena table data (no refresh between runs).

### fi_rw_world coverage vs gaps
**Covered**: FakeLambda with `Invoke`, moto EventBridge Scheduler (via `mock_aws`), DDB scan.
**Missing**: `lambda.GetFunctionConfiguration` router entry (must add to `awsfake.Router` or moto
GetFunctionConfiguration — moto supports this natively); world must expose the FakeLambda's
function config (env vars) via GetFunctionConfiguration; scheduler entries for the service
component cron; `same_hash_model_written` import path must be resolvable from fi_fleet_health.

## Item 7 — Forced failure reaches the staleness alarm's subscriber

### AWS calls `_item_7` makes
| Service | Operation | Key params | Allowlist |
|---|---|---|---|
| cloudwatch | DescribeAlarms | AlarmNames `[vsa-{stage}-avx-run-stale-service-{region}]` | `cloudwatch.DescribeAlarms` |
| cloudwatch | DescribeAlarmHistory | AlarmName, HistoryItemType StateUpdate + Action | `cloudwatch.DescribeAlarmHistory` |
| sns | ListTopics | (paginated) | `sns.ListTopics` |
| sns | GetTopicAttributes | TopicArn | `sns.GetTopicAttributes` |
| sns | ListSubscriptionsByTopic | TopicArn | `sns.ListSubscriptionsByTopic` |
| lambda | GetFunctionConfiguration, Invoke | HEALTH_FN (backup + forced invoke) | `lambda.GetFunctionConfiguration`, `lambda.Invoke` |
| dynamodb | Query | `avx-agent-runs` pk=`agent-fi-fleet-health#service` | `dynamodb.Query` |

Note: this is the **staleness** alarm (`CVX/Tier2 RunRecordsWritten`, 27 evaluation periods),
**not** the `fi_rw_world` RunFailureAlarm (`Errors` metric, 3 periods).

### Real producer path
`agents/tier2/observability.py:emit_run_record_heartbeat` emits metric `RunRecordsWritten`
in namespace `CVX/Tier2` only when `write_run_record` succeeds.  A forced failure path:
`FI_SERVICE_MODEL_ENABLED=true` + `FI_COST_GUARDRAIL_ID=""` → runner raises at
`runner.py:2619` before any write → no heartbeat emitted → CloudWatch FILL(m1,0)=0
for 27 consecutive periods → alarm transitions OK→ALARM → SNS notifies subscriber.

In the world: moto CloudWatch supports `PutMetricAlarm`, `SetAlarmState` (for seeding OK state),
and `DescribeAlarmHistory`.  The world (a) creates the alarm with moto; (b) seeds it in OK state
by calling `SetAlarmState` internally (legitimate world-setup, not item 7 itself);
(c) on the forced-failure invoke, advances the world clock by 27 hours and calls
`cloudwatch.SetAlarmState` internally from `alarm.py` when heartbeat not received — analogous
to `RunFailureAlarm.pump()` in `fi_rw_world/alarm.py`.  Item 7 then asserts `DescribeAlarmHistory`
shows OK→ALARM + Action item with actionState=Succeeded naming the topic.

### World must seed
- CloudWatch alarm `vsa-staging-avx-run-stale-service-us-west-2` (moto, PutMetricAlarm):
  metric CVX/Tier2::RunRecordsWritten, dimensions `stage=staging component=service`,
  EvaluationPeriods=27, DatapointsToAlarm=27, Threshold=1, ComparisonOperator=LessThanThreshold,
  TreatMissingData=breaching, ActionsEnabled=True.
- SNS topic `vsa-staging-tier2-operator-alerts` with KMS key and ≥1 confirmed subscription.
- A new `fh_world/alarm.py:StatenessAlarm` class (mirrors fi_rw_world/alarm.py
  `RunFailureAlarm` but watches `RunRecordsWritten`, 27-period window, CMK-encrypted topic).

### fi_rw_world coverage vs gaps
**Covered**: moto CloudWatch + SNS via `mock_aws()`, FakeLambda Invoke, clock advance pattern.
**Missing**: `StatenessAlarm` class (new — distinct from RunFailureAlarm's Errors metric);
SNS KMS key (moto supports `kms:Create/Encrypt/Decrypt` via mock_aws; the CMK ARN in facts.md
must be replaced with a world-generated key); `DescribeAlarmHistory` entries in the Action class
(moto records these when alarm transitions; verify moto version supports action history items).

---

## Build Order (≤8 stages, each ≤300 lines written)

### Stage 1 — `fh_world/seed.py`
Seed constants: `FLEETS` (2), `VIN_FLEET` (20 VINs × fleet), `vin()`, `vehicle_id()`,
`fault_events()` (planted fault rows per VIN with codes, raised/cleared dates),
`pm_plans()` (one calendar plan, one mileage plan), `service_records()`, `daily_usage()`,
`pm_operator_completions()`.  No AWS calls; no imports outside stdlib + fixtures.py constants.
**Acceptance**: `pytest scripts/tests/fh_world/test_seed.py` — round-trip: seed rows round-trip
through `fixtures.py:build_fleet_fault_events_df()` with zero assertion errors.

### Stage 2 — `fh_world/adp_lake.py`
`install_fleet_health_tables(athena, adp_root, today)`: calls
`fleet_fault_events.view_runner.run_vehicle_fault_status(...)` and
`run_pm_vehicle_status(...)` with seed DataFrames; registers results in FakeAthena as
`adp_staging_fleet_health.vehicle_fault_status_daily` and `pm_vehicle_status_daily` snapshots,
plus the 7 raw input tables item 4 queries.  Handles Trino→DuckDB dialect via `_apply_trino_to_duckdb`.
**Acceptance**: `pytest scripts/tests/fh_world/test_adp_lake.py` — `run_pm_vehicle_status`
returns ≥1 row with `status in {overdue, due}` for the seeded PM VINs.

### Stage 3 — `fh_world/world.py` (core — items 1, 2, 6)
`HealthWorld` subclasses or mirrors `fi_rw_world.World`.  `open()`: (a) sets up moto, router,
pool, athena, lam, bus, stream, clock as in fi_rw_world; (b) calls `adp_lake.install_fleet_health_tables`;
(c) creates `fault_codes.parquet` on moto S3; (d) seeds DDB tables; (e) runs HEALTH_FN
(`agents/fi_fleet_health/runner.py` service component) via `lam.run(HEALTH_FN, ...)`.
Adds `env_of("fh_health_fn")` and `env_of("avx_findings")` with `AVX_FLEET_IDS_CLAIM_TRUSTED=true`.
**Acceptance**: `pytest scripts/tests/test_live_verify_fi_fleet_health_item1.py` with `FH_WORLD=1`
— item 1 `_item_1` reads real run record + cursor; `res.failed` == 0.

### Stage 4 — `fh_world/alarm.py:StatenessAlarm`
Creates `vsa-staging-avx-run-stale-service-us-west-2` alarm and SNS topic in moto.
`pump()` advances the alarm's metric: if the last heartbeat was >27 periods ago,
calls `cloudwatch.SetAlarmState` → ALARM and records an Action history item.
Mirrors `fi_rw_world/alarm.py:RunFailureAlarm` but uses RunRecordsWritten / 27-period window.
**Acceptance**: `pytest scripts/tests/test_live_verify_fi_fleet_health_item7.py` with `FH_WORLD=1`
— static checks (alarm exists, ActionsEnabled, metric dimensions) pass.

### Stage 5 — Item-2 test using world (`test_fh_world_item2.py`)
`with HealthWorld() as w: rc = w.run_main(["--stage","staging","--item","2"])` —
verifies findings land in correct fleet queues with catalog severities; p0 directive byte-equal.
Injects one p0 fault VIN in fleet A, one attention VIN in fleet B.
**Acceptance**: `pytest scripts/tests/fh_world/test_fh_world_item2.py` passes; `rc == 0`.

### Stage 6 — Item-3 test using world (`test_fh_world_item3.py`)
World plants a booking finding.  Test calls approve → DMS RO created → consumer sets `in_progress`
→ world patches RO to `Closed/Draft` → consumer sets `cancelled` → health runner re-runs (item 3
re-raise check).  Each step asserted via DDB scan.
**Acceptance**: `pytest scripts/tests/fh_world/test_fh_world_item3.py` passes; `rc == 0` on item 3.

### Stage 7 — Item-5 (AVX half) + Item-6 test using world
Item-5: world creates two fleet operators; asserts fleet B JWT gets 403 from `/findings/fleet/flt-a`.
Item-6: world invokes HEALTH_FN a second time; asserts `artifacts_written=0 duplicates_skipped≥1`.
Both in `test_fh_world_item56.py`.
**Acceptance**: `pytest scripts/tests/fh_world/test_fh_world_item56.py` passes; both items `rc == 0`.

### Stage 8 — Item-7 test using world (`test_fh_world_item7.py`)
Forces failure (FI_COST_GUARDRAIL_ID cleared), advances clock 27 h via `w.clock.set(+27h)`,
calls `w.pump()` to fire StatenessAlarm.  Asserts DescribeAlarmHistory has OK→ALARM + Action.
**Acceptance**: `pytest scripts/tests/fh_world/test_fh_world_item7.py` passes; static alarm checks
pass; alarm history shows OK→ALARM transition + Action record for the operator topic.


---

## Architect corrections (2026-10-19, read these first; they override the sections above)

1. **Item 1 is the Agent 2 nightly runs, not the service run.** It reads run records
   `agent-2-vehicle-health#{battery,tires,brakes}` (script `_HEALTH_COMPONENTS`, line ~707), each
   component's cursor `CURSOR#<component>`/`STATE` in `vsa-{stage}-health-findings`, the Lambda's
   caps (`GetFunctionConfiguration`, `TIER2_MAX_VINS_*`, battery >= 600), and per-component pool
   counts in Athena (`_athena_count_component_pool`: battery = energy_usage JOIN managed_vehicles,
   tires = tire_health, brakes = service_records; the real SQL is each source's
   `_build_select_managed_vins_sql` in `agents/health/adp_*_source.py`). The producers are the same
   Lambda (`agents.tier2.runner.lambda_handler`) run with `component` battery/tires/brakes at
   02:00/02:20/02:40Z.
2. **The service run** (`agent-fi-fleet-health#service`, 05:40Z) reads, through Athena,
   `agents/fi_fleet_health/selection.py`'s `_FAULT_SQL` (vehicle_fault_status_daily JOIN
   fault_codes), `_PM_SQL` (pm_vehicle_status_daily), `_MANAGED_VINS_SQL`
   (fleet_recall.managed_vehicles) and `_DAILY_USAGE_SQL` (vehicle_utilisation_daily). The world
   builds those tables from ADP's real code: run ADP's `vehicle_fault_status.sql` /
   `pm_vehicle_status.sql` (the ADP repo's `fleet_fault_events/view_runner.py`, from
   `AVX_ADP_REPO` or `~/adp-wt-fi-health-d-g6`, as `agents/fi_fleet_health/tests/test_labor_op_vocabulary.py`
   resolves it) over seeded raw rows, then snapshot them with `snapshot_date` as ADP's snapshots
   job does.
3. **S3 config objects** (items 2, 3): `config/fleet_health/fault_codes/fault_codes.parquet` and
   `config/fleet_health/code_labor_op_map/code_labor_op_map.parquet` in the ADP lake bucket, written
   with ADP's own writers (`fleet_fault_events/fault_codes.py`, `daily.py`), not hand-built.
4. **Item 5**: AVX half only (CMS queue route half moves to Group 7, user 2026-10-17 G6-D4); the
   script's CMS half must report it as moved, not fail.
5. Rules for every stage: fakes refuse calls a real service would refuse (missing GroupName, wrong
   table key); real producers write what the script reads; behavioural tests only (no source-text
   checks); AWS env stripped, loopback only; one fresh world per test.

### Build stages (replaces "Build Order" above)

- **S1 `fh_world/adp_lake.py`** (+ `fh_world/seed.py` constants it needs): DuckDB lake from ADP's
  real views and writers, the config parquet bytes, registered in fi_rw_world's `FakeAthena`.
  Accept: AVX's `_FAULT_SQL`, `_PM_SQL`, `_MANAGED_VINS_SQL`, `_DAILY_USAGE_SQL` and the three
  Agent 2 sources' managed-VIN SQL run on it and return the planted rows (`fh_world/test_adp_lake.py`).
- **S2 `fh_world/world.py`**: `FhWorld(World)` with the script path, lake, Lambda env (live caps,
  battery 600, service flags), schedules (battery/tires/brakes/service), real nightly runs before
  the test starts. Accept: a world opens; run records exist for all four components.
- **S3** e2e tests items 1, 2, 6 (`test_live_verify_fi_fleet_health_e2e.py`).
- **S4** item 3 (approve, DMS RO, consumer, closed-early, re-raise).
- **S5** item 4 (PM recompute vs view on the world's lake).
- **S6** item 5 AVX half (two fleets' operators).
- **S7** `fh_world/alarm.py` staleness alarm + item 7 (force, then verify >= 27 h later).
