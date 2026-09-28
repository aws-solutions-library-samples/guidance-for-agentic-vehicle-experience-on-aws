---
documentId: NHTSA-BRAKE-FAULT-ADVISORY
documentType: recall-bulletin
title: Brake System Fault — Driver Advisory and P0 Response Protocol
source: shared
---

# Brake System Fault — Driver Advisory and P0 Response

**Scope.** Any active DTC in the C-range (C0xxx chassis brake system codes)
or any active brake fluid pressure fault reported by the ABS module. This
advisory applies to all makes and models.

**Classification.** Any active brake system fault is a P0 Stop Driving
condition. There is no tenant policy override that can downgrade a brake
fault below P0. Per VSA triage guardrails, `downgradeForbidden: true` applies
universally to brake system faults regardless of tenant segment.

**Required driver response (in order).**

1. Pull over safely to the shoulder or a parking area as soon as safely
   possible. Do not continue to destination.
2. Turn on hazard lights.
3. Do not attempt to continue driving, even at reduced speed. Brake system
   faults can degrade further without warning.
4. Remain in the vehicle if on a roadway with fast-moving traffic. Exit and
   move to a safe distance from the vehicle if on a shoulder with high
   traffic density.

**Required VSA response (in order).**

1. Deliver the pull-over instruction verbally before any other narration.
2. Auto-escalate to tenant tier-2 queue.
3. Dispatch roadside assistance per tenant `escalation.roadsideDispatch` config.
4. Create a STAR ServiceRequestAdd ticket with priority P0 and action
   `dispatch-roadside`.

**Do NOT.** Do not offer a regular service appointment as the primary
response. Do not suggest the driver continue at reduced speed. Do not
attempt to diagnose the specific fault over the phone.
