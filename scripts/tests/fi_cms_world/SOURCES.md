# SOURCES — what is not real code in this world and why

## Real code (loaded from pinned commits, not copied)

| Module | Source | Method |
|---|---|---|
| `cms_fi.py` — CMS FI Lambda handler | CMS `1ce93a75b` `services/fleet_intelligence/` | `git archive` into temp dir, `importlib` |
| `maker.py` — ADP digital_thread_writer | ADP `895f7b61` `platform-foundation/source/lambda/digital_thread_writer/handler.py` | `git show` into temp file, `importlib.util.spec_from_file_location` |
| `maker.py` — AVX investigations handler | AVX worktree `lambdas/api-avx-investigations/handler.py` | `importlib.util.spec_from_file_location` |
| `maker.py` — tier2 runner | AVX worktree `agents/tier2/runner.py` | `import agents.tier2.runner` |
| `world.py` — DMS RO create/PATCH, auth.get_granted_dealer_ids, warranty-candidates job | DMS `de6c181` (checked at `CmsWorld.open`) | working-tree `sys.path` via `fi_rw_world.clock.install` — `CmsWorld.open` refuses any other checkout |

## Stubs / fakes (not real code)

| Item | Why not real |
|---|---|
| `FiQualityBedrock` (in `maker.py`) | Bedrock Converse is not available in-process; ported from `49ff3719:scripts/tests/fi_loop_world/fi_quality_bedrock.py`. Fills required schema fields from enum constraints so the real `InvestigationValidator` accepts the output. |
| `array_join` DuckDB UDF | Trino/Hive `array_join` is not a DuckDB built-in. Python lambda: `sep.join(str(x) for x in arr)`. |
| `ota_campaigns.status` column | The population-refresh SQL uses a 7-column `ota_campaigns` schema; fi_quality readers also query `status`. Combined DDL is a superset. Empty in the world. |
| Population Athena tables seeded by `seed_pattern` | Athena tables (`fault_patterns`, `engineering_failure_modes`, etc.) contain world-generated fake data shaped like ADP's Iceberg corpus. Only the columns the SQL reads are populated; missing columns are added via `ALTER TABLE`. |
| `fleet_ops.faults` TABLE (adp_faults.py) | A plain DuckDB table seeded directly via `add_fault()`. The real ADP coverage-union view (`fleet_ops_faults.sql`, which joins `cms_ingest.dtc_history` with `fleet_fault_events` inside a live-coverage interval) is NOT installed. CMS `dtc-history` rows therefore don't reach `vehicle_fault_status` in the world. The `remote_clear` propagation path is **checked nowhere**: fleet `ground` step 3 is a NOTE (no dtc-history write), because the user accepted dropping "a remote clear still refused" on 2026-10-23 (CMS return-to-service reads only the health cache; ADP ignores clears outside the live tier). |
| `vehicle_identity` stub (adp_faults.py) | Minimal `(vin, model, model_year)` stub for `_SQL_FAULTS` LEFT JOIN. Only columns consumed by the SQL are populated; all other `vehicle_identity` columns are absent. |
| `fleet_ops.daily_usage` TABLE (adp_faults.py) | Empty stub. The real table backs mileage-basis plan due-date calculations in `pm_vehicle_status.sql`. The world has no odometer reads, so all mileage-basis plans use calendar fallback. |
| `service_records.service_records` TABLE (adp_faults.py) | Empty stub. The real table backs completion-source-1 in `pm_vehicle_status.sql`. No completions come from service records in the world. |
| Trino→DuckDB rules in `adp_faults.py:_RULES` | `_RULES` is a local list of search-replace pairs applied to ADP SQL before execution in DuckDB. Beyond the subset copied from ADP `view_runner.py`, two extra rules handle `array(json)→JSON[]` and `json_parse(p.completions)→p.completions` (see module docstring). These rules are not tested against the live Trino engine. |
| `_patch_cms_requests` / `_proxy` transport patch (world.py) | `agents.executors.cms.requests.post` is monkeypatched to forward calls to the fake HTTPS host (`cms-fi.world.test`) to the in-process HTTP test server. Live code uses real HTTPS; the world substitutes `urllib` behind the scenes. |
| `_patch_avx_transport` (cms_fi.py) | CMS FI's `call_avx` transport is patched to redirect `avx-api.example.invalid` to the world's `FakeRestApi`. Live code calls the real AVX API Gateway. |
| `SOURCES.md` (`SOURCES.md` lists what is not real and why — the population Athena tables match the SQL column list from `population_refresh.py` at CMS `1ce93a75b`). |

## Views the world cannot run from real SQL (live-run only)

| View / query | Why not runnable in world |
|---|---|
| `read_health_rollup` (population_refresh) | Reads `adp_staging_fleet_health.vehicle_fault_status_daily` with a complex aggregation that requires the full fleet-health ADP pipeline output; the world seeds only minimal fault_codes. The `health_rollup` dataset is listed as `failed` in `refresh_population()` — this does not block the W3 flow (patterns, signals, threads, campaigns all succeed). The live run (task 8.4) checks the alarm `cms-staging-ui-fi-population-views-stale`. |
| `fleet_ops_faults.sql` coverage-union view | Not installed; see `fleet_ops.faults` stub above. The live run doesn't exercise the `cms_ingest.dtc_history` → `remote_clear` → `vehicle_fault_status` path either (task 7.7 step 3 is a NOTE). |

## World mechanics that aren't real code (added review c2 S-4)

| Item | Why |
|---|---|
| RO image log (`world.py` `_ro_image_log`, `_append_ro_image_from_route`) | The DMS lake keeps one image per RO status change; the world appends an image after every DMS create/PATCH it serves through the real route, and `add_ro_image` adds images for test-only ROs. The live lake's own pump timing is not modelled. |
| Guard order (`world.py` `_install_guard_order`) | botocore calls the most specific event's handlers first, so while a `CmsWorld` is open the world registers its router last, after the harness's `before-call.*.*` op-allowlist guard. Without it the guard never runs in a world test (the 2026-10-23 00:39Z live `pre` FAIL). |
