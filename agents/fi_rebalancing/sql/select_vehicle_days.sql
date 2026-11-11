-- vehicle_days: latest snapshot_date for the fleet.
-- Spec: G4-D1. W7: snapshot_date selected for source_ref. Template variables: ${stage}, ${fleet_id}.
SELECT
    vin,
    location_id,
    available_hours,
    snapshot_date
FROM adp_${stage}_fleet_rebalance.vehicle_days
WHERE fleet_id = '${fleet_id}'
  AND snapshot_date = (
      SELECT MAX(snapshot_date)
      FROM adp_${stage}_fleet_rebalance.vehicle_days
      WHERE fleet_id = '${fleet_id}'
  )
