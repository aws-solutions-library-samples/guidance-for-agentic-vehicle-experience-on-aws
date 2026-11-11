/**
 * Feature flags for the FI fleet-health Tier 2 components (task 4.3, spec
 * 2026-10-05-fi-fleet-health-stage-d).
 *
 * FI_FLEET_HEALTH_LIVE — when true:
 *   - the `investigation` SQS event source on the Tier 2 Lambda is ENABLED;
 *   - the `service` EventBridge Scheduler schedule is ENABLED (B2);
 *   - the `service` run-staleness alarm has ActionsEnabled=true (B2);
 *   - the analytics stack provisions the stage D Lake Formation grants for
 *     fleet_health/engineering/ota_campaigns (C; vehicle_telemetry_aggregated is not
 *     granted, its reader is a stub).
 *   A LF grant on a missing table fails the deploy, so this flag gates them (same
 *   lesson as decisions.md call 15 for fleet_recall). Checked read-only 2026-10-11:
 *   the databases existed and held no tables then.
 *
 * FI_FLEET_HEALTH_MODEL_LIVE — when true:
 *   - sets FI_SERVICE_MODEL_ENABLED and FI_INVESTIGATION_MODEL_ENABLED to
 *     'true' on the Tier 2 Lambda (B2/D).
 *   When false (default), no Bedrock client is built and bedrock_client=None
 *   reaches the loop and the job (the rules path only, as Group 3). The
 *   flip condition is that Fix A (3c7b8786) is merged and vsa-staging-tier2
 *   is deployed.
 *
 * Task 4.3 decisions (task43-notes.md §"Design decisions for 4.3" items 1–2):
 *   Both flags shipped false. bin/vsa.ts passes nothing, so every deploy from
 *   main uses these constants, flipped in reviewed commits.
 *
 * Group 6 rest-plan step 7 (2026-10-21): FI_FLEET_HEALTH_LIVE = true. Deploy it only
 * after every table the analytics grants name exists. They name 11 tables:
 * fleet_health.{vehicle_fault_status_daily, pm_vehicle_status_daily,
 * vehicle_utilisation_daily, snapshot_runs} (created by ADP's snapshots job, rest-plan
 * step 6), fleet_health.fault_codes, engineering.{fault_patterns,
 * vehicle_build_components, engineering_failure_modes, engineering_remedy_options} and
 * ota_campaigns.{ota_campaigns, ota_campaign_events}. A Lake Formation grant on a missing
 * table fails the analytics deploy. The model switch stays false (its own flip).
 *
 * FI_FAULT_GROUNDING_LIVE — when true, sets FAULT_GROUNDING_ENABLED to 'true' on the
 *   Tier 2 Lambda: the `service` component then raises a grounding item (ground_vehicle,
 *   p0, an operator approves it) beside the booking item for every vehicle with an open
 *   stop-driving fault (spec D9, U6). Turning it off again stops new grounding items; it
 *   deletes nothing (pending grounding items run to their own expires_at, recorded CMS
 *   groundings stay until return to service). Stage D task 7.7 turns it on after the
 *   pm-schedules migration, a user stop. bin/vsa.ts passes nothing, as for the flags above.
 *   Flipped to true for stage D 7.7 (deploy only on the user's go, after the migration).
 */
export const FI_FLEET_HEALTH_LIVE        = true;
export const FI_FLEET_HEALTH_MODEL_LIVE  = false;
export const FI_FAULT_GROUNDING_LIVE     = true;
