-- Unclaimed DMS warranty candidate rows for the warranty component.
--
-- Spec: .kiro/specs/2026-10-05-fi-recall-warranty-stage-c/spec.md D8 warranty step 1
-- DMS docs: docs/fleet-warranty.md § 2.2 (warranty_candidates columns)
--
-- Selects unclaimed rows from the previous nightly candidates job run.
-- Only Group 1 columns (no skipped_reason, which appears only on dry-run rows).
-- Only columns DMS's candidates job writes on every row: the lake table is created by the
-- Glue merge's first run from those rows, so a column only the claim route writes
-- (claimed_at) never reaches it and selecting it fails COLUMN_NOT_FOUND. claimed_at is
-- always null under claimed = FALSE anyway (DMS issue 2026-10-09-fi-lake-tables-have-no-merge-job,
-- review g5-dms W2). Pinned by tests/test_warranty_candidates_sql_columns.py.
--
-- D8 step 1 filters (all applied in SQL):
--   claimed = FALSE
--   verdict_status IN ('approved', 'requires_review')
--   CAST(filing_deadline AS DATE) > current_date  (filing_deadline is VARCHAR in the lake)
--
-- line_no is a Number in DynamoDB / bigint in Iceberg; cast to VARCHAR here so the
-- Python side always sees a string (architect call G3-6).
--
-- Template variable: ${stage} — replaced by select_warranty_candidates() before execution.

SELECT
    ro_id,
    CAST(line_no AS VARCHAR) AS line_no,
    vehicle_vin,
    dealer_id,
    labor_op,
    fault_code,
    coverage_group,
    in_service_date,
    odometer_km,
    repair_date,
    verdict_status,
    verdict_reason,
    matched_rule_id,
    recoverable_usd,
    terms_version,
    filing_deadline,
    computed_at
FROM dms_${stage}.warranty_candidates
WHERE claimed = FALSE
  AND verdict_status IN ('approved', 'requires_review')
  AND CAST(filing_deadline AS DATE) > current_date
ORDER BY vehicle_vin, ro_id, line_no
