-- Selection query for fi_cost Tier 2 component.
--
-- Spec: .kiro/specs/2026-10-02-fi-cost-stage-b/spec.md § D6 step 1
-- Decisions: decisions.md 2026-10-05 (energy-cost outlier rule, item 6)
--
-- Returns managed vehicles with is_outlier OR is_energy_outlier in the given
-- year_month, ordered by the larger of total excess and energy_excess_usd.
--
-- NOT capped here: max_diagnoses_per_run caps model calls after the
-- unchanged-skip (D6 step 3), not this query.  The query returns the full
-- anomaly set so daily runs reach all vehicles in excess-cost order.
-- (decisions.md 2026-10-05, item 6; spec D6 step 1 amended)
--
-- Template variables: ${stage}, ${year_month}
--
-- Column name cross-reference (decisions.md 2026-10-04 "Group 2 start", item 5):
--   ratio_cohort, ratio_own  — cost_per_mile / cohort_median, own_mean
--   severity                 — from ADP parameters table (decisions item 2)
--   is_outlier               — both ratios >= threshold and gates hold
--   is_energy_outlier        — energy ratios >= energy_threshold, excess >= min (D3)
--   energy_excess_usd        — miles × LEAST(energy cpm - own_mean, energy cpm - cohort) (D3)
--   cost_per_mile, cohort_median, miles — needed for excess ordering
--   energy_ratio_own, energy_ratio_cohort — for evidence gathering
--   energy_method            — pre-rendered literal from cost_parameters (D3)

SELECT
    vin,
    fleet_id,
    year_month,
    ratio_cohort,
    ratio_own,
    severity,
    is_outlier,
    is_energy_outlier,
    energy_excess_usd,
    energy_ratio_own,
    energy_ratio_cohort,
    energy_method,
    cost_per_mile,
    cohort_median,
    miles,
    method
FROM adp_${stage}_fleet_cost.vehicle_cost_baseline
WHERE year_month = '${year_month}'
  AND (is_outlier OR is_energy_outlier)
ORDER BY GREATEST(
    COALESCE((cost_per_mile - cohort_median) * miles, 0.0),
    COALESCE(energy_excess_usd, 0.0)
) DESC,
-- Tie-breaker: when excesses tie, the vehicles the cap defers are the same on every run.
vin
