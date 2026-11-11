-- Rooftop coordinates and per-rooftop part stock from the DMS lake.
--
-- Spec: .kiro/specs/2026-10-05-fi-recall-warranty-stage-c/spec.md D8 recall step 2
-- DMS docs: docs/fleet-warranty.md § 6 (rooftops), § 7.1 (parts_inventory lake table)
--
-- Joins dms_${stage}.parts_inventory with dms_${stage}.rooftops so the caller gets
-- both the stock count (qty_on_hand) and the coordinates (latitude, longitude) for
-- each (dealer_id, part_number) combination in a single round trip.
--
-- Template variable: ${stage} — replaced by select_parts_stock() before execution.

SELECT
    i.dealer_id,
    i.part_number,
    i.qty_on_hand,
    r.latitude,
    r.longitude
FROM dms_${stage}.parts_inventory i
JOIN dms_${stage}.rooftops r
    ON i.dealer_id = r.dealer_id
ORDER BY i.dealer_id, i.part_number
