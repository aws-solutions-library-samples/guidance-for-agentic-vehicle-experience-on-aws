-- supply_demand_gaps_snapshot: newest materialised_on, horizon_days = 7.
-- This product covers the entire rental fleet; fleet_id is not a column here.
-- Spec: G4-D1. Template variables: ${stage}.
SELECT
    location_id,
    forecast_hours,
    supply_vehicles,
    forecast_utilization_pct,
    status,
    vehicles_short,
    vehicles_spare,
    data_as_of
FROM adp_${stage}_fleet_rebalance.supply_demand_gaps_snapshot
WHERE horizon_days = 7
  AND materialised_on = (
      SELECT MAX(materialised_on)
      FROM adp_${stage}_fleet_rebalance.supply_demand_gaps_snapshot
      WHERE horizon_days = 7
  )
