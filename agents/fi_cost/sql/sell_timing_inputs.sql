-- Sell-timing inputs for the fi_cost Tier 2 component.
--
-- Spec: .kiro/specs/2026-10-02-fi-cost-stage-b/spec.md § D10 ("Inputs come from the
-- same ADP views and vehicle_pricing").
--
-- One row per managed vehicle per month up to the data month: the month's maintenance
-- cost from vehicle_cost_monthly (the series CMS's Lifecycle screen fits) and the
-- vehicle's purchase price from vehicle_pricing.  A vehicle without a price has no
-- depreciation line, so it has no crossover; the inner join drops it.
--
-- Not ordered here: the caller groups by VIN and sorts each series by year_month.
--
-- Template variables: ${stage}, ${year_month}

SELECT
    m.vin,
    m.fleet_id,
    m.year_month,
    m.maintenance_cost,
    p.purchase_price_usd
FROM adp_${stage}_fleet_cost.vehicle_cost_monthly m
JOIN adp_${stage}_vehicle_pricing.vehicle_pricing p
  ON p.vin = m.vin
WHERE m.year_month <= '${year_month}'
