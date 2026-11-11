-- vehicle_utilization_daily: 7 days ending at the latest vehicle_days snapshot_date.
-- idle day = available_hours > 0 AND on_rent_hours = 0.
-- vehicle_utilization_daily holds the entire rental fleet; fleet_id is not a column here.
-- Spec: G4-D1. Template variables: ${stage}, ${fleet_id}.
WITH latest_vd AS (
    SELECT MAX(snapshot_date) AS snapshot_date
    FROM adp_${stage}_fleet_rebalance.vehicle_days
    WHERE fleet_id = '${fleet_id}'
)
SELECT
    u.vin,
    u.snapshot_date,
    u.available_hours,
    u.on_rent_hours
FROM adp_${stage}_fleet_rebalance.vehicle_utilization_daily u
CROSS JOIN latest_vd l
WHERE u.snapshot_date > l.snapshot_date - INTERVAL '7' DAY
  AND u.snapshot_date <= l.snapshot_date
