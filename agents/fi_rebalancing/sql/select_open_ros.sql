-- repair_orders: latest image per ro_id (highest _seq_no), open status only.
-- open = status NOT IN ('Ready', 'Delivered', 'Invoiced', 'Closed'). Closed is DMS's administrative
-- close: the car is out of the shop, as ADP's supply_demand_gaps and fleet_bookings count it
-- (ADP FI sweep 2026-10-26, row "Rebalance Closed RO open").
-- Spec: G4-D1. Template variables: ${stage}.
WITH ranked AS (
    SELECT
        ro_id,
        vehicle_vin,
        status,
        _seq_no,
        ROW_NUMBER() OVER (PARTITION BY ro_id ORDER BY _seq_no DESC) AS rn
    FROM dms_${stage}.repair_orders
)
SELECT
    ro_id,
    vehicle_vin,
    status
FROM ranked
WHERE rn = 1
  AND status NOT IN ('Ready', 'Delivered', 'Invoiced', 'Closed')
