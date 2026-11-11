-- booking_requests: mean request duration per location_id over the last 28 days.
-- Non-pending rows whose start_at is in [now-28d, now).
-- date_diff returns seconds; divide by 3600.0 for hours.
-- Spec: G4-D1. Template variables: ${stage}, ${fleet_id}, ${now} (YYYY-MM-DD HH:MM:SS).
SELECT
    location_id,
    AVG(date_diff('second', start_at, end_at) / 3600.0) AS mean_request_hours
FROM adp_${stage}_fleet_rebalance.booking_requests
WHERE fleet_id = '${fleet_id}'
  AND outcome <> 'pending'
  AND start_at >= CAST('${now}' AS TIMESTAMP) - INTERVAL '28' DAY
  AND start_at <  CAST('${now}' AS TIMESTAMP)
GROUP BY location_id
