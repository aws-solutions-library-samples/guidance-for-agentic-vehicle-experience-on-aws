-- service_records: upcoming service dates in [today, today+3].
-- Adds service_month partition filter to avoid full-history scans (spec G4-D1 W2).
-- Template variables: ${stage}, ${today} (YYYY-MM-DD).
SELECT
    vin,
    service_date
FROM adp_${stage}_service_records.service_records
WHERE service_date >= CAST('${today}' AS DATE)
  AND service_date <= CAST('${today}' AS DATE) + INTERVAL '3' DAY
  AND service_month IN (
    DATE_TRUNC('month', CAST('${today}' AS DATE)),
    DATE_TRUNC('month', CAST('${today}' AS DATE) + INTERVAL '3' DAY)
  )
