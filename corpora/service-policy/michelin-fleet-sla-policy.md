---
documentId: MICHELIN-FLEET-SLA-2026
documentType: fleet-operations
title: Michelin Fleet Services — SLA Tiers and Escalation Policy
source: tenantOverlay
tenantId: michelin
---

# Michelin Fleet Services — SLA Tiers and Escalation Policy

## SLA Tiers

| Tier | Definition | Downtime Budget | Warm Transfer |
|---|---|---|---|
| tier-1 | Top-150 customers, enterprise logistics | ≤ 4 hr/month | Always offered on any P1+ |
| tier-2 | Mid-market fleets, regional carriers | ≤ 8 hr/month | Offered on P0 and P1 with open recall |
| tier-3 | Small fleet and owner-operator | ≤ 16 hr/month | Offered on P0 only |

## Escalation rules

- Any P0 classification: auto-escalate to Michelin Fleet Ops tier-2 queue
  regardless of customer tier. Dispatch roadside via partner webhook.
- Tier-1 customer, P1 classification: always offer warm transfer to Fleet Ops.
  Per tenant config escalation rule `classification == 'P1' && fleetSla == 'tier-1'`,
  action is `force-warm-transfer`.
- Tier-1 customer, P1 with open recall: warm transfer + ticket must reference
  the recall ID in the `symptoms.relatedRecalls` block.

## Service center network

Preferred network for coolant and powertrain work in the Dallas region:

- **Rush Truck Center — Dallas North** (SVC-DAL-014), 2101 Main St, Dallas TX
- **Authorized Diesel Service** (SVC-DAL-022), Dallas TX (alternative)

For recall remedy work, preferred center must be a Freightliner-authorized
dealer. Rush Truck Center — Dallas North meets this requirement.

## Booking narrative style

When narrating booking confirmations, use the caller's last name with
"Mr./Ms." unless a preferred salutation is in the customer record. Always
state the estimated cost ($0 for recall remedies), the service center name
and address, and the recommended service window.
