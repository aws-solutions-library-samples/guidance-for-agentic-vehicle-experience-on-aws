---
documentId: SLA-ESCALATION-POLICY
category: service-policy
title: "Escalation Policy and SLA Definitions"
---

# Escalation Policy and Service Level Agreements

## Severity Classifications

| Severity | Definition | Response SLA | Resolution SLA |
|----------|-----------|-------------|----------------|
| **P0 — Critical** | Safety risk, vehicle immobilized, or driver in danger | Immediate (< 5 min) | Roadside dispatch within 30 min |
| **P1 — Urgent** | Safety system degraded, vehicle drivable but limited | < 15 min | Service appointment within 48 hours |
| **P2 — Service Soon** | Non-safety issue requiring attention | < 1 hour | Service appointment within 2 weeks |
| **P3 — Monitor** | Informational, no action required now | Next business day | Addressed at next scheduled service |

## P0 Triggers (automatic escalation, no driver confirmation needed)
- Airbag system fault (B0001-B0099)
- Complete brake failure
- Engine fire indicators
- Steering loss
- Vehicle rollover detection
- Driver distress signal
- Rapid tire pressure loss (> 2 PSI/hour)

## P1 Triggers (escalation offered to driver)
- ABS/ESC disabled
- Engine overheating (approaching redline)
- Transmission fault with limp mode
- Multiple simultaneous DTCs
- Flashing check engine light (active misfire)

## Escalation Channels

| Channel | P0 | P1 | P2 | P3 |
|---------|----|----|----|----|
| AI Agent auto-escalate | ✅ | Offered | ❌ | ❌ |
| Connect voice callback | ✅ | On request | ❌ | ❌ |
| Connect chat | ✅ | ✅ | ✅ | ❌ |
| Roadside dispatch | ✅ | On request | ❌ | ❌ |
| Service scheduling | ❌ | ✅ | ✅ | At next visit |

## Warranty Coverage Quick Reference

| Coverage Type | Duration | Mileage | Covers |
|--------------|----------|---------|--------|
| Bumper-to-bumper | 3 years | 36,000 mi | Everything except wear items |
| Powertrain | 5 years | 60,000 mi | Engine, transmission, drivetrain |
| Emissions | 8 years | 80,000 mi | Catalytic converter, ECM, O2 sensors |
| Corrosion | 6 years | Unlimited | Body rust-through |
| EV Battery | 8 years | 100,000 mi | Battery capacity below 70% |

## Parts Coverage Tiers

| Tier | Parts | Labor Rate |
|------|-------|-----------|
| OEM Genuine | Manufacturer original | $150/hr |
| OEM Equivalent | Certified aftermarket (same spec) | $120/hr |
| Fleet Standard | Quality aftermarket | $100/hr |
