-- metro_events: events whose window overlaps [today, today+9] and announced_on <= today.
-- This product covers the entire rental fleet; no fleet_id column.
-- Spec: G4-D1. Template variables: ${stage}, ${today} (YYYY-MM-DD).
SELECT
    event_id,
    metro,
    affected_location_ids,
    category,
    uplift_band,
    starts_on,
    ends_on,
    announced_on
FROM adp_${stage}_fleet_rebalance.metro_events
WHERE ends_on   >= CAST('${today}' AS DATE)
  AND starts_on <= CAST('${today}' AS DATE) + INTERVAL '9' DAY
  AND announced_on <= CAST('${today}' AS DATE)
