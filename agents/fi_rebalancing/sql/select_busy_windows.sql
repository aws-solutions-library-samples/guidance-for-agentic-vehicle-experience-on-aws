-- booking_requests: fulfilled bookings active or starting within the next 24 h.
-- start_at >= now - 35 days to prune partitions (Athena partition pruning).
-- Spec: G4-D1. Template variables: ${stage}, ${fleet_id}, ${now} (YYYY-MM-DD HH:MM:SS).
SELECT
    vin,
    location_id,
    start_at,
    end_at
FROM adp_${stage}_fleet_rebalance.booking_requests
WHERE fleet_id = '${fleet_id}'
  AND outcome = 'fulfilled'
  AND vin IS NOT NULL
  AND start_at >= CAST('${now}' AS TIMESTAMP) - INTERVAL '35' DAY
  AND start_at <  CAST('${now}' AS TIMESTAMP) + INTERVAL '24' HOUR
  AND end_at   >  CAST('${now}' AS TIMESTAMP)
