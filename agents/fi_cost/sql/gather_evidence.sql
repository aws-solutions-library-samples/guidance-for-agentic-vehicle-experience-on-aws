-- Evidence-gathering query for fi_cost Tier 2 component.
--
-- Spec: .kiro/specs/2026-10-02-fi-cost-stage-b/spec.md § D6 step 2, § D8
-- Decisions: decisions.md 2026-10-04 "Group 1 findings" item 5 (single query
--            for all selected VINs); decisions.md 2026-10-05 (energy rule keys)
--
-- One Athena query for ALL selected VINs.  D13 notes Athena queue times of
-- 79-354 s; 50 sequential calls cannot fit the 15-minute Lambda budget.
-- A single batched query is the correct pattern (also: the health agent's
-- lesson from live verification, agentic-tiers.md).
--
-- Template variables: ${stage}, ${year_month}, ${vin_list}
-- ${vin_list} is a comma-separated quoted list: 'VIN1','VIN2',...
--
-- Returns one row per VIN with all D8 evidence keys.
--
-- Column aliases use the evidence-key naming convention (interface.md
-- "Evidence keys referenced in tests").  Python postprocessing adds:
--   cost.flag        (total / energy / both from is_outlier, is_energy_outlier)
--   delta.*_usd      (per-mile delta × miles; decisions cycle 3 item 1)
--
-- Exact median algorithm (decisions.md "Group 2 start" item 3; same pattern as
-- ADP's vehicle_cost_baseline.sql CTEs cohort_all_ranked / cohort_stats):
--   1. Number each row in the partition by the metric ASC, vin ASC (tie-break).
--   2. Count non-null rows: cnt = COUNT(*) OVER partition.
--   3. Keep rows where rn IN (FLOOR((cnt+1)/2), CEIL((cnt+1)/2)).
--   4. Median = AVG(val) over those one or two rows.
-- Applied to: fleet_median_miles and cohort_kwh_per_100mi.
-- Shares and price stats remain AVG over the full cohort (no approximation needed).
--
-- Window bounds (svc_window): service months M-5 to M inclusive — six calendar
-- months ending at the scored month.  An upper bound is required so that records
-- for months after '${year_month}' cannot change month M's evidence or its hash
-- (D11 determinism).

WITH
-- Per-VIN energy aggregation for the scored month
vin_energy AS (
    SELECT
        vin,
        SUM(CASE WHEN category IN ('energy_public', 'energy_depot') THEN kwh ELSE 0.0 END)
            AS total_kwh,
        SUM(CASE WHEN category = 'energy_public' THEN kwh ELSE 0.0 END)
            AS public_kwh,
        SUM(CASE WHEN category = 'energy_depot' THEN kwh ELSE 0.0 END)
            AS depot_kwh,
        SUM(CASE WHEN category = 'energy_depot' AND tariff_band = 'peak' THEN kwh ELSE 0.0 END)
            AS depot_peak_kwh,
        SUM(CASE WHEN category = 'energy_public' AND tariff_band = 'dc_fast' THEN kwh ELSE 0.0 END)
            AS dc_fast_kwh,
        -- public-and-peak kWh for shift_to_depot_off_peak formula
        SUM(CASE WHEN category = 'energy_public'
                   OR (category = 'energy_depot' AND tariff_band = 'peak')
                 THEN kwh ELSE 0.0 END)
            AS public_and_peak_kwh,
        -- mean price for public+peak kWh
        CASE
            WHEN SUM(CASE WHEN category = 'energy_public'
                            OR (category = 'energy_depot' AND tariff_band = 'peak')
                          THEN kwh ELSE 0.0 END) > 0
            THEN SUM(CASE WHEN category = 'energy_public'
                            OR (category = 'energy_depot' AND tariff_band = 'peak')
                          THEN amount_usd ELSE 0.0 END)
                 / SUM(CASE WHEN category = 'energy_public'
                               OR (category = 'energy_depot' AND tariff_band = 'peak')
                             THEN kwh ELSE 0.0 END)
            ELSE NULL
        END AS mean_public_peak_price_per_kwh,
        -- depot off-peak price per kWh (C2: vehicle's own price; COALESCED with cohort below)
        CASE
            WHEN SUM(CASE WHEN category = 'energy_depot' AND tariff_band = 'off_peak'
                          THEN kwh ELSE 0.0 END) > 0
            THEN SUM(CASE WHEN category = 'energy_depot' AND tariff_band = 'off_peak'
                          THEN amount_usd ELSE 0.0 END)
                 / SUM(CASE WHEN category = 'energy_depot' AND tariff_band = 'off_peak'
                             THEN kwh ELSE 0.0 END)
            ELSE NULL
        END AS depot_off_peak_price_per_kwh,
        -- overall mean price per kWh
        CASE
            WHEN SUM(CASE WHEN category IN ('energy_public', 'energy_depot')
                          THEN kwh ELSE 0.0 END) > 0
            THEN SUM(CASE WHEN category IN ('energy_public', 'energy_depot')
                          THEN amount_usd ELSE 0.0 END)
                 / SUM(CASE WHEN category IN ('energy_public', 'energy_depot')
                             THEN kwh ELSE 0.0 END)
            ELSE NULL
        END AS mean_price_per_kwh
    FROM adp_${stage}_fleet_cost.fleet_cost_events
    WHERE year_month = '${year_month}'
      AND vin IN (${vin_list})
      AND category IN ('energy_public', 'energy_depot')
    GROUP BY vin
),
-- Fleet median miles: exact median via ROW_NUMBER/COUNT (no APPROX_PERCENTILE;
-- same pattern as ADP's cohort_all_ranked / cohort_stats).
-- Ties broken by vin ASC to ensure a fully deterministic ranking (D11).
fleet_median_ranked AS (
    SELECT
        fleet_id,
        vin,
        CAST(miles AS DOUBLE) AS miles_val,
        ROW_NUMBER() OVER (
            PARTITION BY fleet_id
            ORDER BY CAST(miles AS DOUBLE) ASC, vin ASC
        ) AS rn,
        COUNT(*) OVER (PARTITION BY fleet_id) AS cnt
    FROM adp_${stage}_fleet_cost.vehicle_cost_monthly
    WHERE year_month = '${year_month}'
      AND miles IS NOT NULL
),
fleet_median AS (
    SELECT
        fleet_id,
        AVG(miles_val) AS fleet_median_miles
    FROM fleet_median_ranked
    WHERE rn >= CAST(FLOOR(CAST(cnt + 1 AS DOUBLE) / 2.0) AS BIGINT)
      AND rn <= CAST(CEIL( CAST(cnt + 1 AS DOUBLE) / 2.0) AS BIGINT)
    GROUP BY fleet_id
),
-- Per-vehicle energy stats for the scored month (cohort-wide, not just selected VINs).
-- Used for cohort_kwh_per_100mi exact median and cohort AVG share/price stats.
cohort_energy_per_vehicle AS (
    SELECT
        vcm.fleet_id,
        vcm.model,
        vcm.vin,
        CAST(fce_agg.total_kwh AS DOUBLE)
            / NULLIF(CAST(vcm.miles AS DOUBLE), 0) * 100
            AS kwh_per_100mi,
        CAST(fce_agg.public_kwh AS DOUBLE)
            / NULLIF(CAST(fce_agg.total_kwh AS DOUBLE), 0)
            AS public_share,
        CAST(fce_agg.depot_peak_kwh AS DOUBLE)
            / NULLIF(CAST(fce_agg.depot_kwh AS DOUBLE), 0)
            AS peak_share,
        CAST(fce_agg.dc_fast_kwh AS DOUBLE)
            / NULLIF(CAST(fce_agg.public_kwh AS DOUBLE), 0)
            AS dc_fast_share,
        fce_agg.mean_price_per_kwh,
        -- depot off-peak price per kWh for this vehicle (C2: cohort median fallback)
        fce_agg.depot_off_peak_price_per_kwh
    FROM adp_${stage}_fleet_cost.vehicle_cost_monthly vcm
    JOIN (
        SELECT
            vin,
            SUM(CASE WHEN category IN ('energy_public', 'energy_depot')
                     THEN kwh ELSE 0.0 END) AS total_kwh,
            SUM(CASE WHEN category = 'energy_public' THEN kwh ELSE 0.0 END) AS public_kwh,
            SUM(CASE WHEN category = 'energy_depot' THEN kwh ELSE 0.0 END) AS depot_kwh,
            SUM(CASE WHEN category = 'energy_depot' AND tariff_band = 'peak'
                     THEN kwh ELSE 0.0 END) AS depot_peak_kwh,
            SUM(CASE WHEN category = 'energy_public' AND tariff_band = 'dc_fast'
                     THEN kwh ELSE 0.0 END) AS dc_fast_kwh,
            CASE
                WHEN SUM(CASE WHEN category IN ('energy_public', 'energy_depot')
                              THEN kwh ELSE 0.0 END) > 0
                THEN SUM(CASE WHEN category IN ('energy_public', 'energy_depot')
                              THEN amount_usd ELSE 0.0 END)
                     / SUM(CASE WHEN category IN ('energy_public', 'energy_depot')
                                 THEN kwh ELSE 0.0 END)
                ELSE NULL
            END AS mean_price_per_kwh,
            -- depot off-peak price per kWh (C2: used for cohort median fallback below)
            CASE
                WHEN SUM(CASE WHEN category = 'energy_depot' AND tariff_band = 'off_peak'
                              THEN kwh ELSE 0.0 END) > 0
                THEN SUM(CASE WHEN category = 'energy_depot' AND tariff_band = 'off_peak'
                              THEN amount_usd ELSE 0.0 END)
                     / SUM(CASE WHEN category = 'energy_depot' AND tariff_band = 'off_peak'
                                 THEN kwh ELSE 0.0 END)
                ELSE NULL
            END AS depot_off_peak_price_per_kwh
        FROM adp_${stage}_fleet_cost.fleet_cost_events
        WHERE year_month = '${year_month}'
          AND category IN ('energy_public', 'energy_depot')
        GROUP BY vin
    ) fce_agg ON vcm.vin = fce_agg.vin
    WHERE vcm.year_month = '${year_month}'
),
-- Rank by kwh_per_100mi for exact median (ties broken by vin ASC; D11 determinism).
-- Only non-null rows participate — mirrors cohort_rows in vehicle_cost_baseline.sql.
cohort_kwh_ranked AS (
    SELECT
        fleet_id,
        model,
        kwh_per_100mi,
        ROW_NUMBER() OVER (
            PARTITION BY fleet_id, model
            ORDER BY kwh_per_100mi ASC, vin ASC
        ) AS rn,
        COUNT(*) OVER (PARTITION BY fleet_id, model) AS cnt
    FROM cohort_energy_per_vehicle
    WHERE kwh_per_100mi IS NOT NULL
),
-- Exact median of kwh_per_100mi per (fleet_id, model)
cohort_kwh_median AS (
    SELECT
        fleet_id,
        model,
        AVG(kwh_per_100mi) AS cohort_kwh_per_100mi
    FROM cohort_kwh_ranked
    WHERE rn >= CAST(FLOOR(CAST(cnt + 1 AS DOUBLE) / 2.0) AS BIGINT)
      AND rn <= CAST(CEIL( CAST(cnt + 1 AS DOUBLE) / 2.0) AS BIGINT)
    GROUP BY fleet_id, model
),
-- Exact median of depot off-peak price per kWh per (fleet_id, model).
-- C2: fallback when the vehicle itself has no off-peak charging data (e.g. peak_share=1.0).
-- Only non-null rows participate (vehicles that did charge at depot off-peak).
cohort_depot_opk_ranked AS (
    SELECT
        fleet_id,
        model,
        depot_off_peak_price_per_kwh,
        ROW_NUMBER() OVER (
            PARTITION BY fleet_id, model
            ORDER BY depot_off_peak_price_per_kwh ASC, vin ASC
        ) AS rn,
        COUNT(*) OVER (PARTITION BY fleet_id, model) AS cnt
    FROM cohort_energy_per_vehicle
    WHERE depot_off_peak_price_per_kwh IS NOT NULL
),
cohort_depot_opk_median AS (
    SELECT
        fleet_id,
        model,
        AVG(depot_off_peak_price_per_kwh) AS cohort_depot_off_peak_price_per_kwh
    FROM cohort_depot_opk_ranked
    WHERE rn >= CAST(FLOOR(CAST(cnt + 1 AS DOUBLE) / 2.0) AS BIGINT)
      AND rn <= CAST(CEIL( CAST(cnt + 1 AS DOUBLE) / 2.0) AS BIGINT)
    GROUP BY fleet_id, model
),
-- Cohort energy statistics for the scored month (fleet_id + model).
-- cohort_kwh_per_100mi: exact median from cohort_kwh_median.
-- cohort_depot_off_peak_price_per_kwh: exact median from cohort_depot_opk_median (C2).
-- Shares and price stats: AVG over the full cohort (decisions cycle 3 item 1).
-- Decisions cycle 3 item 1: driver_coaching uses cost.energy (total monthly energy USD).
cohort_energy AS (
    SELECT
        cep.fleet_id,
        cep.model,
        ckm.cohort_kwh_per_100mi,
        cdm.cohort_depot_off_peak_price_per_kwh,
        AVG(cep.public_share) AS cohort_public_share,
        AVG(cep.peak_share) AS cohort_peak_share,
        AVG(cep.dc_fast_share) AS cohort_dc_fast_share,
        AVG(cep.mean_price_per_kwh) AS cohort_mean_price_per_kwh
    FROM cohort_energy_per_vehicle cep
    LEFT JOIN cohort_kwh_median ckm
        ON cep.fleet_id = ckm.fleet_id
       AND cep.model    = ckm.model
    LEFT JOIN cohort_depot_opk_median cdm
        ON cep.fleet_id = cdm.fleet_id
       AND cep.model    = cdm.model
    GROUP BY cep.fleet_id, cep.model, ckm.cohort_kwh_per_100mi, cdm.cohort_depot_off_peak_price_per_kwh
),
-- Maintenance repeat-count for the 6-month window (M-5 to M inclusive).
-- Upper bound ensures records for months after '${year_month}' cannot change
-- month M's evidence or its inputs_hash (D11).
-- decisions cycle 1 item 1: window is six calendar months ending at the scored month.
svc_window AS (
    SELECT
        vin,
        MAX(cnt) AS max_repeat_count
    FROM (
        SELECT vin, service_type, COUNT(*) AS cnt
        FROM adp_${stage}_service_records.service_records
        WHERE vin IN (${vin_list})
          AND service_month >= DATE_ADD('month', -5,
                DATE_PARSE('${year_month}', '%Y-%m'))
          AND service_month <= DATE_PARSE('${year_month}', '%Y-%m')
          AND (warranty_covered IS NULL OR warranty_covered = false)
        GROUP BY vin, service_type
    )
    GROUP BY vin
)
SELECT
    vcb.vin,
    vcb.fleet_id,
    vcb.year_month,
    -- Core baseline fields
    vcb.ratio_cohort,
    vcb.ratio_own,
    vcb.severity,
    vcb.method                          AS "cost.method",
    vcb.is_outlier,
    vcb.is_energy_outlier,
    -- Energy outlier rule fields (D3, decisions 2026-10-05)
    vcb.energy_ratio_own                AS "cost.energy_ratio_own",
    vcb.energy_ratio_cohort             AS "cost.energy_ratio_cohort",
    vcb.energy_excess_usd               AS "cost.energy_excess_usd",
    vcb.energy_method                   AS "cost.energy_method",
    -- Per-category costs from monthly view (joined via baseline)
    vcm.cost_per_mile                   AS "cost.cost_per_mile",
    vcb.own_mean                        AS "cost.own_baseline",
    vcb.cohort_median                   AS "cost.cohort_baseline",
    vcm.energy_cost                     AS "cost.energy",
    vcm.maintenance_cost                AS "cost.maintenance",
    vcm.lease_cost                      AS "cost.lease",
    vcm.insurance_cost                  AS "cost.insurance",
    vcm.miles                           AS "cost.miles",
    -- Per-mile deltas from cohort (decisions F2.2 notes item 1)
    vcb.delta_energy_cohort             AS "delta.energy",
    vcb.delta_maintenance_cohort        AS "delta.maintenance",
    vcb.delta_lease_cohort              AS "delta.lease",
    vcb.delta_insurance_cohort          AS "delta.insurance",
    -- Fleet median miles (exact median — no APPROX_PERCENTILE)
    fm.fleet_median_miles               AS "cost.fleet_median_miles",
    -- Energy mix for this vehicle
    ve.public_and_peak_kwh              AS "energy.public_and_peak_kwh",
    ve.mean_public_peak_price_per_kwh   AS "energy.mean_public_peak_price_per_kwh",
    -- C2: COALESCE vehicle's own depot off-peak price with the fleet-model cohort median.
    -- For vehicles with peak_share=1.0 (no off-peak charging), the cohort median provides
    -- the price needed for the shift_to_depot_off_peak saving formula.  When both are NULL
    -- (no vehicle in the cohort charged off-peak), the validator rejects the action.
    COALESCE(
        ve.depot_off_peak_price_per_kwh,
        ce.cohort_depot_off_peak_price_per_kwh
    )                                   AS "energy.depot_off_peak_price_per_kwh",
    ce.cohort_depot_off_peak_price_per_kwh AS "energy.cohort_depot_off_peak_price_per_kwh",
    CAST(ve.public_kwh AS DOUBLE)
        / NULLIF(CAST(ve.total_kwh AS DOUBLE), 0)
                                        AS "energy.public_share",
    CAST(ve.depot_peak_kwh AS DOUBLE)
        / NULLIF(CAST(ve.depot_kwh AS DOUBLE), 0)
                                        AS "energy.peak_share",
    CAST(ve.dc_fast_kwh AS DOUBLE)
        / NULLIF(CAST(ve.public_kwh AS DOUBLE), 0)
                                        AS "energy.dc_fast_share",
    ve.mean_price_per_kwh               AS "energy.mean_price_per_kwh",
    CAST(ve.total_kwh AS DOUBLE)
        / NULLIF(CAST(vcm.miles AS DOUBLE), 0) * 100
                                        AS "energy.vehicle_kwh_per_100mi",
    -- Cohort energy stats (cohort_kwh_per_100mi: exact median; rest: AVG)
    ce.cohort_kwh_per_100mi             AS "energy.cohort_kwh_per_100mi",
    ce.cohort_public_share              AS "energy.cohort_public_share",
    ce.cohort_peak_share                AS "energy.cohort_peak_share",
    ce.cohort_dc_fast_share             AS "energy.cohort_dc_fast_share",
    ce.cohort_mean_price_per_kwh        AS "energy.cohort_mean_price_per_kwh",
    -- Maintenance window (M-5 to M inclusive; six months)
    COALESCE(sw.max_repeat_count, 0)    AS "maintenance.max_repeat_count"
FROM adp_${stage}_fleet_cost.vehicle_cost_baseline vcb
JOIN adp_${stage}_fleet_cost.vehicle_cost_monthly vcm
    ON vcb.vin = vcm.vin
   AND vcb.year_month = vcm.year_month
LEFT JOIN vin_energy ve
    ON vcb.vin = ve.vin
LEFT JOIN fleet_median fm
    ON vcb.fleet_id = fm.fleet_id
LEFT JOIN cohort_energy ce
    ON vcm.fleet_id = ce.fleet_id
   AND vcm.model = ce.model
LEFT JOIN svc_window sw
    ON vcb.vin = sw.vin
WHERE vcb.year_month = '${year_month}'
  AND vcb.vin IN (${vin_list})
