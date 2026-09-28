---
documentId: DTC-U0100
category: technical-reference
title: "U0100 — Lost Communication with ECM/PCM"
severity: P1
systems: [network, communication, powertrain]
---

# U0100 — Lost Communication with ECM/PCM

## Summary
A control module on the CAN bus has lost communication with the Engine Control Module (ECM/PCM). Multiple systems may be affected as the ECM is the primary powertrain controller.

## Severity: P1 — Service Now
Vehicle may enter limp mode or fail to start. Multiple warning lights likely illuminated. Do not ignore.

## Common Causes
1. **CAN bus wiring fault** (30%) — Open or short in CAN-H or CAN-L lines
2. **ECM power/ground issue** (25%) — Corroded ground strap, blown fuse
3. **Failed ECM** (20%) — Internal processor or communication IC failure
4. **Water intrusion** (15%) — Flooded connector or module
5. **Aftermarket device interference** (10%) — OBD-II plug-in devices, remote starters

## Diagnostic Steps
1. Check for multiple U-codes — pattern indicates where the break is
2. Verify ECM power (B+ at key-on) and grounds
3. Measure CAN bus resistance at OBD-II port (should be ~60 ohms between pins 6 and 14)
4. Check for aftermarket OBD-II devices — remove and retest
5. Inspect ECM connectors for water/corrosion

## Driver Communication
> "Your vehicle's main engine computer has lost communication with other systems. You may see multiple warning lights and the engine might run in a reduced-power mode. This needs professional diagnosis — I'd recommend getting it to a service center today rather than continuing to drive."

## Repair Estimate
- Wiring repair: $200-$500
- ECM replacement + programming: $1,000-$2,500
- Connector repair: $100-$300
