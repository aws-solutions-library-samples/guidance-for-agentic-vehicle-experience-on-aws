-- move_outcomes_snapshot: newest materialised_on.
-- Spec: G4-D1. W7: materialised_on selected for source_ref. Template variables: ${stage}, ${fleet_id}.
SELECT
    to_location_id,
    effective_date,
    ratio_hours,
    predicted_on_rent_hours_gain,
    actual_on_rent_hours_gain,
    materialised_on
FROM adp_${stage}_fleet_rebalance.move_outcomes_snapshot
WHERE fleet_id = '${fleet_id}'
  AND materialised_on = (
      SELECT MAX(materialised_on)
      FROM adp_${stage}_fleet_rebalance.move_outcomes_snapshot
      WHERE fleet_id = '${fleet_id}'
  )
