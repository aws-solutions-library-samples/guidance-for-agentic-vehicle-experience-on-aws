---
source_category: vehicle_manufacturing
stage: cross-cutting
title: Visibility Signals — What Status Updates Does a Customer Receive During Vehicle Production?
---

# Visibility Signals — What Status Updates Does a Customer Receive During Vehicle Production?

Customers who have placed a vehicle order want to know where their vehicle is and what is happening to it. The visibility signals that manufacturers provide during production and delivery vary considerably between programmes, but the underlying data points that drive those signals are consistent across the industry. This document explains what status updates a customer can expect, what each signal actually represents in the manufacturing process, and why some stages generate more visible progress than others.

## The Nature of Manufacturing Status Updates

A customer order does not travel like a parcel with continuous GPS tracking. A vehicle in production passes through a finite number of discrete scanning and logging events — points at which the VIN is read, the build sheet is confirmed, a test result is recorded, or a logistics handoff occurs. Each of these events can generate a status update visible to the customer. Between events, the vehicle's status does not change even if significant physical work is being done — paint shop dwell time, for example, may last twelve hours with no intermediate scan events.

Customers should understand that a status that "hasn't moved in two days" does not mean nothing is happening — it most likely means the vehicle is within a stage that has a long dwell time, such as paint curing, ocean shipping, or dealer PDI scheduling.

## Status Events and What They Signal

### Order Placed
Generated when the order management system creates the order record and assigns an order number. This is a database event, not a physical event. It confirms that the customer's configuration has been locked and a commercial agreement exists.

### Payment Received
Generated when the OMS receives confirmation from the finance company, dealer account team, or payment processor that the payment or funding commitment has cleared. This is a financial gateway event.

### Assigned to Plant
Generated when the production planning system allocates the order to a specific plant, production week, and sequence position, and assigns a VIN. This is the first event with physical implications — the vehicle now has an identity that links it to a specific build schedule.

### Sub-Assembly
Generated when the body-in-white fabrication or powertrain sub-assembly begins for this specific VIN. In some programmes this event is triggered when the body-in-white clears the geometry inspection gate; in others it is triggered when the weld cell begins processing the body. Regardless of trigger definition, this event signals that physical manufacturing has started.

### Paint
Generated when the body-in-white enters the paint shop's pre-treatment tunnel or, in some programmes, when it exits the paint shop's inspection station having cleared quality review. The paint stage can span twelve to twenty hours of process time, which is why customers who see this status sometimes feel the progress tracker has stalled.

### Final Assembly
Generated when the vehicle enters the trim zone of the final assembly line. This event is significant because it means the vehicle is on the moving line and being built to the customer's specific build sheet in real time. The marriage point — joining the body to the powertrain and chassis — happens during this stage.

### Quality Control
Generated when the vehicle enters the end-of-line testing station. This event signals that physical assembly is complete and the plant is now verifying that the vehicle performs to specification before releasing it for shipment.

### Shipped
Generated when the vehicle is loaded onto the outbound carrier — a road transporter, a rail wagon, or a RoRo vessel — and the VIN is scanned out of the finished vehicle compound. For export vehicles, a second shipped event may be generated when the vessel departs port.

### At Dealer
Generated when the dealer scans the vehicle's VIN into their dealer management system at receipt — confirming that the vehicle has arrived safely at the destination dealer and the PDI process can begin.

### Ready for Delivery
Generated when the dealer marks the pre-delivery inspection as complete in the manufacturer's dealer management system and all registration, finance, and documentation requirements have been confirmed. This is the trigger for the customer's collection appointment notification.

## How Status Signals Reach the Customer

In programmes with a connected vehicle platform, status signals are published through the manufacturer's order tracking API, which drives the customer's connected app or web portal. In programmes relying on traditional dealer communication, status updates are communicated by the dealer's sales team via email, SMS, or phone call.

The gap between a manufacturing event occurring and a customer-visible status update appearing is typically minutes to hours for electronic system integrations, and one to two business days for manual dealer-entered updates. This means the "quality control" status the customer sees may already be in the "shipped" state in the plant's system — the customer-visible tracker is always slightly behind the physical reality.

## Managing Customer Expectations

The stages with the longest average dwell time and therefore the most customer anxiety are paint (twelve to twenty hours of process time with few intermediate scan points), shipped (three days to three weeks of logistics transit with infrequent scan events), and at dealer (two to five days of PDI scheduling and workshop time). Proactive communication from the manufacturer or dealer at these stages — for example, a notification when the vehicle exits the paint shop or when it loads onto the ocean vessel — significantly reduces inbound enquiries and improves customer satisfaction.

## References

- [Source: Generic automotive industry practice — VIN scan event architecture and customer order-tracking integration are standard components of modern connected OEM programmes]
- [Source: Wikipedia, "Vehicle tracking system", https://en.wikipedia.org/wiki/Vehicle_tracking_system — overview of VIN-based tracking across logistics networks]
- [Source: Generic automotive customer experience practice — status signal design and proactive communication at high-anxiety stages are documented in J.D. Power Initial Quality and Sales Satisfaction studies, illustrative reference]
