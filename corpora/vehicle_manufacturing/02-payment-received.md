---
source_category: vehicle_manufacturing
stage: paymentReceived
title: Payment Received — Releasing the Order into Production Scheduling
---

# Payment Received — Releasing the Order into Production Scheduling

Once payment is received for a vehicle order, the manufacturer's financial and production systems move in tandem to transition the order from a commercial commitment to an active production candidate. The payment-received stage is a gate: the order cannot be slotted into a build programme until this gate opens.

## What Triggers This Stage

Payment received means different things depending on the transaction structure:

- **Cash or financed purchase.** The buyer's deposit or full payment clears the manufacturer's or dealer's bank account. For financed purchases, this is typically the finance company's funding confirmation, not the buyer's individual bank transfer.
- **Lease.** The first rental payment and any associated administrative fees are confirmed as received by the leasing company.
- **Fleet purchase order.** The purchase order from the fleet operator's procurement system is accepted and logged, which in many fleet programmes is treated as a payment equivalent for scheduling purposes.
- **Direct-to-customer programmes.** Some manufacturers require a booking deposit at order and balance payment closer to production. In these programmes, the payment-received stage triggers when the deposit clears, with the balance due at or before delivery.

## What Happens After Payment Clears

**Production eligibility.** The OMS marks the order as "payment confirmed" and elevates it to production-eligible status. Orders that remain in payment-pending status beyond a defined window (typically 7–21 days) may be flagged for dealer follow-up or, in some markets, administratively cancelled.

**Finance-company data exchange.** For financed vehicles, the finance company receives an anticipated delivery date so it can schedule the lending agreement commencement date and insurance inception. The VIN (vehicle identification number) is not yet assigned at this stage — it will be allocated at plant assignment — but the order number links all downstream records.

**Scheduling queue entry.** The production planning team draws on payment-confirmed orders to populate the next scheduling cycle. Plant assignment, production week, and sequencing are all determined at this step based on model demand, plant capacity by region, and logistics lead times to the delivery market.

**Customer status update.** The customer app or dealer portal advances the status indicator from "Order Placed" to "Payment Received," which communicates that the order has cleared the financial gate and is now queued for production scheduling. This is often the update buyers watch for most eagerly in the early days after ordering.

## What the Customer Should Know About This Stage

The payment-received confirmation is not a delivery date. It confirms that the financial pre-condition for production has been met. The actual production week allocation happens in the scheduling cycle that follows, which may run on a weekly cadence.

If payment has been made but the status has not advanced within a few business days, the most common causes are a hold on dealer reconciliation, a discrepancy in the purchase order reference for fleet orders, or a pending credit confirmation from a finance provider. In these cases, the originating dealer or fleet account manager is the correct point of contact.

## Why Production Scheduling Cannot Begin Earlier

Manufacturers plan production in batches rather than against individual orders. Scheduling a vehicle into a weekly build programme commits plant resources — stamping press time, paint capacity, assembly line slots — and purchases supplier components on a just-in-time basis. Scheduling against an unconfirmed payment would create financial exposure if the order is subsequently cancelled or amended, and would consume production capacity that another confirmed order could use. The payment gate is therefore a genuine operational control, not merely a contractual one.

## Typical Duration in This Stage

An order in the payment-received stage typically advances to plant assignment within one to two production-scheduling cycles, which commonly means one to three weeks depending on the manufacturer's scheduling frequency and current order bank depth. High-demand models or limited-production variants may remain in the payment-confirmed queue longer if production slots are constrained.

## References

- [Source: Generic automotive industry practice — payment confirmation as a production-eligibility gate is standard across passenger car OEM programmes]
- [Source: Wikipedia, "Vehicle identification number", https://en.wikipedia.org/wiki/Vehicle_identification_number — note that VIN assignment happens at or after plant scheduling, not at order]
- [Source: Ward, J., *Automotive Retail and Order Management*, Society of Automotive Engineers, illustrative reference for payment processing in dealer and direct-to-customer channels]
