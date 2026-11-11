-- Latest complete data month for the fi_cost Tier 2 component.
--
-- Spec: .kiro/specs/2026-10-02-fi-cost-stage-b/spec.md § D6 step 1, § D13
-- (generator row: "the data month in evidence stops advancing").
--
-- fleet_cost_events holds complete months only (the ADP monthly job writes month M
-- after M ends), so the newest year_month partition is the latest complete month.
-- The upper bound is the calendar month before the run, so a partition written early
-- (a hand-run job, a clock skew) is never read as complete.
--
-- year_month is the table's partition column (projection, yyyy-MM), so this reads
-- partition locations, not cost rows.
--
-- Template variables: ${stage}, ${through_month}

SELECT max(year_month) AS year_month
FROM adp_${stage}_fleet_cost.fleet_cost_events
WHERE year_month <= '${through_month}'
