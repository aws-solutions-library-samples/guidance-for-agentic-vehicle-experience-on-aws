-- Unremedied recall rows for received campaigns, with catalog fields the writer needs.
--
-- Spec: .kiro/specs/2026-10-05-fi-recall-warranty-stage-c/spec.md D8 recall step 1
-- Interface: platform-foundation/source/data-products/recall_campaigns/interface.md
--
-- Joins recall_vehicle_status with recall_campaigns_received to add the catalog
-- fields that band.classify() and writer.build_finding() require.
-- Only Group 1 column names (no Group 2 additions: managed_as_of).
--
-- do_not_drive is a boolean in Iceberg; Athena returns it as the string "true" or
-- "false" via GetQueryResults. The Python caller must parse it strictly (G3-9).
-- remedy_part_numbers is array<string>; Athena returns it as "[a, b]".
--
-- Template variable: ${stage}

SELECT
    rvs.campaign_id,
    rvs.vin,
    rvs.fleet_id,
    rvs.match_basis,
    rvs.confirming_code,
    rvs.confirming_seen_on,
    rvs.confirming_source,
    rvs.rule_version,
    rc.recall_class,
    rc.do_not_drive,
    rc.park_outside,
    rc.received_date,
    rc.catalog_version,
    rc.models,
    rc.model_year_min,
    rc.model_year_max,
    rc.plants,
    rc.build_from,
    rc.build_to,
    rc.component,
    rc.summary,
    rc.consequence,
    rc.remedy,
    rc.remedy_available_from,
    rc.remedy_part_numbers,
    rc.remedy_labor_op,
    -- FI stage D 9.3: an issued recall's lot rule, for recall.rule's evidence. ADP's received
    -- view keeps Stage C's columns, so the lots come from the issued table itself, with the
    -- view's own rule: issued rows only, and never a row reusing a Stage C id (ADP
    -- recall_vehicle_status.sql issued_lots). NULL for every other campaign.
    ri.component_lots
FROM adp_${stage}_fleet_recall.recall_vehicle_status rvs
JOIN adp_${stage}_fleet_recall.recall_campaigns_received rc
    ON rvs.campaign_id = rc.campaign_id
LEFT JOIN (
    SELECT i.campaign_id, i.component_lots
    FROM adp_${stage}_engineering.recall_campaigns_issued i
    WHERE i.status = 'issued'
      AND i.campaign_id NOT IN (
          SELECT campaign_id
          FROM adp_${stage}_fleet_recall.recall_campaigns
          WHERE campaign_id IS NOT NULL
      )
) ri
    ON ri.campaign_id = rvs.campaign_id
WHERE rvs.remedied = FALSE
ORDER BY rvs.campaign_id, rvs.vin
