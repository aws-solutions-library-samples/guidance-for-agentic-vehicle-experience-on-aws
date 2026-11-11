-- Current DMS warranty terms, one row per (terms_version, coverage_group).
--
-- Spec: .kiro/specs/2026-10-05-fi-recall-warranty-stage-c/spec.md D10 (term statement,
-- value basis); group3-notes.md F4 / review-g3-cycle1 W5.
-- DMS docs: docs/fleet-warranty.md § 7.1 (warranty_terms lake table), § 7.2 (shape).
--
-- Only rows already in effect are returned. The newest terms_version is picked by
-- SEMVER in Python (selection.pick_current_terms), because § 7.2 S5 forbids string order.
--
-- Template variable: ${stage} — replaced by select_warranty_terms() before execution.

SELECT
    terms_version,
    coverage_group,
    effective_from,
    term_years,
    term_miles,
    labor_rate_usd
FROM dms_${stage}.warranty_terms
WHERE CAST(effective_from AS DATE) <= current_date
