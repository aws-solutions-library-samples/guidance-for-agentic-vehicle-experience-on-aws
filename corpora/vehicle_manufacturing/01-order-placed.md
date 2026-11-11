---
source_category: vehicle_manufacturing
stage: orderPlaced
title: Order Placed — Capturing and Confirming a Vehicle Order
---

# Order Placed — Capturing and Confirming a Vehicle Order

When a vehicle order is placed and confirmed, it marks the transition from a customer's configuration intent to a legally acknowledged production commitment. The order-placed stage spans the moment a buyer finalises their model, trim, colour, and option selections through to the instant the manufacturer's system assigns a unique order number and returns a confirmation.

## What Happens Immediately After an Order Is Placed

**Order number generation.** The manufacturer's order management system (OMS) creates a vehicle order record and assigns a unique identifier — typically a 10–14 character alphanumeric code — that travels with the vehicle through every subsequent stage. This number appears on the buyer's confirmation email, on the dealer's order book, and eventually on the vehicle's build sheet at the plant.

**Configuration lock.** At the point of order confirmation, the customer's selected trim level, exterior colour, interior material, and option packages are frozen into a build specification. Changes after this point may be possible within a narrow window but typically require the dealer to submit a change request to the OMS, and changes become impossible once the order is scheduled into a production slot.

**Credit and compliance check.** For orders involving dealer stock or direct purchase, the OMS may initiate an automated credit check or export-compliance screening in parallel. For fleet orders, a purchase order number from the buyer's procurement system is logged against the order record.

**Dealer allocation.** The order is associated with the originating dealer (or the manufacturer's direct-sales channel if the brand operates a direct model). The dealer record captures the expected delivery region, which feeds into plant assignment logic in a later stage.

**Customer notification.** An automated confirmation is dispatched to the customer — typically by email and, where a connected vehicle app is in use, as a push notification. The notification includes the order number, a summary of the configured specification, the originating dealer, and an indicative lead time. The lead time at this stage is an estimate based on current production scheduling; it is not a firm delivery commitment.

## What Is Not Locked at This Stage

Paint scheduling, production week, and plant assignment are not finalised at the order-placed stage. The order enters a queue managed by the production planning team, which reviews order volumes, plant capacity, and logistics requirements before slotting the order into a build programme. The order remains in a pending state until payment is received and a production slot is allocated.

## Why This Stage Matters to the Customer

The order-placed confirmation is the first durable record that the vehicle exists as a committed purchase, not just a saved configuration. It provides a reference number for all subsequent communication with the dealer, enables the buyer to track progress through the production pipeline, and in many markets establishes the legally binding order agreement between buyer and seller.

Customers sometimes ask whether they can still change their mind at this stage. Minor option changes — accessory packs, floor mat selections, minor trim variants — may be accommodated if the order has not yet been scheduled into a production slot, but exterior colour and major powertrain changes are typically not possible after order confirmation. The window for change is usually expressed in days rather than weeks.

## Typical Duration in This Stage

The order-placed stage itself is an administrative step that resolves in minutes to hours. The time a customer spends "waiting in" the order-placed state before advancing to the next stage depends on payment processing speed and the production scheduling cycle. Manufacturers typically run scheduling cycles weekly or biweekly, meaning an order placed on a Monday may not advance to the payment-received confirmation or plant assignment until the following planning cycle runs.

## References

- [Source: Ward, J., *Automotive Retail and Order Management*, Society of Automotive Engineers, illustrative reference for OMS workflow and order confirmation procedures]
- [Source: Wikipedia, "Order management system", https://en.wikipedia.org/wiki/Order_management_system — overview of order capture, configuration lock, and confirmation generation]
- [Source: Generic automotive industry practice — configuration lock and build-sheet generation at order confirmation are standard practice across volume passenger car programmes]
