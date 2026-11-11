---
documentId: DTC-P0171
category: technical-reference
title: "P0171 — System Too Lean (Bank 1)"
severity: P2
systems: [fuel, intake, powertrain]
---

# P0171 — System Too Lean (Bank 1)

## Summary
The PCM has detected a lean air/fuel mixture on Bank 1. The long-term fuel trim has exceeded +25%, meaning the engine is adding significantly more fuel than normal to compensate for excess air or insufficient fuel delivery.

## Severity: P2 — Service Soon
Vehicle is drivable but may experience reduced power, rough idle, or hesitation. Prolonged lean operation can damage catalytic converter.

## Common Causes
1. **Vacuum leak** (40%) — Cracked intake hose, loose clamp, failed PCV valve
2. **Dirty/failed MAF sensor** (25%) — Underreporting airflow
3. **Weak fuel pump** (15%) — Insufficient fuel pressure at high demand
4. **Clogged fuel injector** (10%) — Restricted spray pattern
5. **Exhaust leak before O2 sensor** (10%) — False lean reading

## Diagnostic Steps
1. Check long-term fuel trim (LTFT) — values above +20% confirm lean condition
2. Smoke test intake system for vacuum leaks
3. Clean MAF sensor with MAF-specific cleaner; retest
4. Check fuel pressure at rail (spec: 55-62 PSI for most GDI systems)
5. Inspect intake boot and PCV connections

## Driver Communication
> "Your engine is running with too much air and not enough fuel. You might notice it idling rough or hesitating when you accelerate. It's safe to drive short distances, but I'd recommend getting it looked at this week to prevent any further issues."

## Repair Estimate
- Vacuum leak repair: $100-$300
- MAF sensor replacement: $150-$350
- Fuel pump replacement: $500-$900
- Injector cleaning/replacement: $200-$600
