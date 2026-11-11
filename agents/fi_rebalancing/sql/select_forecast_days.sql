-- fleet_bookings_forecast: latest run_date; forecast_date run_date+1 .. run_date+9.
-- This product covers the entire rental fleet; fleet_id is not a column here.
-- Spec: G4-D1. Template variables: ${stage}.
WITH latest AS (
    SELECT MAX(run_date) AS run_date
    FROM adp_${stage}_fleet_rebalance.fleet_bookings_forecast
)
SELECT
    f.location_id,
    f.forecast_date,
    f.forecast_hours,
    f.method,
    f.mape_28d,
    f.run_date,
    f.data_as_of
FROM adp_${stage}_fleet_rebalance.fleet_bookings_forecast f
CROSS JOIN latest l
WHERE f.run_date = l.run_date
  AND f.forecast_date > l.run_date
  AND f.forecast_date <= l.run_date + INTERVAL '9' DAY
