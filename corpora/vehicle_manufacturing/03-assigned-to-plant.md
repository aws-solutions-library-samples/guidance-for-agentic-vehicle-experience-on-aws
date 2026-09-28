---
source_category: vehicle_manufacturing
stage: assignedToPlant
title: Assigned to Plant — Production Slot Allocation and VIN Assignment
---

# Assigned to Plant — Production Slot Allocation and VIN Assignment

When a vehicle order is assigned to a manufacturing plant, it crosses from the commercial world into the production world. The order is given a physical production identity: a specific plant, a specific production week, a position in the build sequence, and — for the first time — a vehicle identification number (VIN) that will stay with that vehicle for its entire life.

## How Plant Assignment Works

**Scheduling cycle.** Manufacturer production planning teams run scheduling cycles — typically weekly — during which confirmed, payment-received orders are matched to available plant capacity. The algorithm considers multiple factors: model variant (the body style, powertrain, and trim level each correspond to specific plant line configurations), paint colour family (some plants handle metallic and special finishes on dedicated paint lines), the destination market's delivery lead time, and the port or rail ramp through which the finished vehicle will be exported.

**Plant selection.** Most volume passenger car programmes are built at a single dedicated plant per model family, so "plant assignment" is often a formality that confirms the expected plant. For manufacturers with multiple plants capable of building the same model, the production planning system allocates orders across plants to balance line loading, minimise logistics cost to the delivery market, and honour any market-specific regulatory build requirements.

**Production week and sequence number.** The order is placed into a specific production week — the calendar week in which the vehicle's assembly is scheduled to begin. Within that week, it is given a sequence number that defines its position in the build order on the main assembly line. The sequence number is what allows the plant to pre-position the correct colour-coded parts, interior kits, and option packs at each station before the vehicle arrives.

**VIN assignment.** At this point the vehicle identification number is generated and registered. The VIN is a 17-character code structured to ISO 3779 that encodes the world manufacturer identifier (WMI), vehicle descriptor section (VDS) capturing model, body, engine, and restraint systems, and vehicle indicator section (VIS) capturing model year, plant code, and sequential production number. The VIN is registered with the manufacturer's global database and, in many markets, with the national vehicle register before the physical vehicle is built.

**Build sheet generation.** A build sheet — the master document specifying every option, colour, material, and feature for this specific vehicle — is generated against the VIN. The build sheet is the controlling document that every assembly station downstream will consult to confirm the correct components are fitted.

## What the Customer Sees

When the order advances to "Assigned to Plant," the customer's progress tracker moves from the administrative queue to the production pipeline. The status update typically includes a refined estimated delivery window, narrower than the indicative lead time provided at order confirmation, because the production week allocation gives the scheduling team enough information to estimate build completion and logistics transit time more precisely.

At this stage, customers sometimes ask whether they can still modify their order. The answer is almost always no once a production week has been assigned. The build sheet has been generated and supplier components have been triggered for just-in-time delivery to the plant. Modifying the order would require pulling the vehicle from the sequence, amending the build sheet, and re-triggering the component supply chain — a process that most manufacturers will not accommodate once sequencing has begun.

## Typical Time Between Assignment and Build Start

The gap between plant assignment and the actual start of physical manufacturing varies by manufacturer and model. It commonly ranges from two to eight weeks, depending on the build programme's backlog depth and any component supply lead times that require the plant to hold orders before releasing them to the line.

## References

- [Source: ISO Standard 3779:2009, *Road vehicles — Vehicle identification number (VIN) — Content and structure* — authoritative specification for VIN encoding]
- [Source: Wikipedia, "Vehicle identification number", https://en.wikipedia.org/wiki/Vehicle_identification_number — accessible overview of WMI, VDS, and VIS structure]
- [Source: Generic automotive industry practice — production scheduling cycles, build sheet generation, and sequence number assignment are standard across passenger car manufacturing programmes]
