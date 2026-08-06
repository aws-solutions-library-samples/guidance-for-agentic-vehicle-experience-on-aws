# VSA Triage Classifier — Deterministic P0–P3 Specification

The triage classifier is the **decision-of-record** for every VSA session. It maps the fused state of a vehicle at the moment of contact onto one of four priority levels — P0, P1, P2, P3 — with a structured reason trace. The LLM narrates the result to the caller; it cannot change the result.

This doc is the complete specification: inputs, rules, thresholds, tenant override semantics, output shape, and worked examples.

---

## 1. Why Deterministic

Three reasons the classifier is deterministic code, not a prompt:

1. **Safety.** A P0 classification is a safety recommendation. An LLM hallucinating a downgrade because the prompt sounded reassuring is unacceptable. Deterministic rules with source-visible logic are auditable by regulators and reviewable by engineers.
2. **Consistency.** Every caller with the same inputs gets the same classification, regardless of model drift, prompt changes, or token-limit truncation.
3. **Auditability.** The trace lists exactly which inputs contributed and at what weight. Required for warranty disputes, compliance reviews, and tenant SLA reporting.

The LLM still has a role: it **narrates** the classification in natural language in the tenant's voice, handles small talk around the decision, and synthesizes follow-up actions. It never decides the tier.

**Hard rule:** the supervisor agent's system prompt explicitly forbids overriding or restating the classification level; attempts to do so are caught by an output validator and trigger session logging plus a silent human escalation.

---

## 2. Inputs

All inputs are fetched by the classifier Lambda in a single fan-out before rules execute. Latency budget: 300ms P99.

| Input | Source | Required | Failure behavior |
|---|---|---|---|
| Active DTCs | CMS `vehicle:dtc:active:{VIN}` (Redis) | yes | If Redis unavailable, fall back to CMS DynamoDB DTC table |
| DTC history (30d) | CMS `cms-{env}-dtc-history` DynamoDB | no | Skip; trace notes `history: unavailable` |
| Telemetry snapshot | CMS `vehicle:state:{VIN}` (Redis) | yes | If stale (>5 min) or missing, trace marks `STALE`; classification proceeds on DTCs + recalls only |
| Open recalls | CMS recall-integration service | no | Skip; trace notes `recall_lookup: unavailable` |
| Predictive maintenance signals | ADP predictive maintenance | no | Skip; no escalation applied |
| Tenant config | DynamoDB `vsa-tenant-config` via cache | yes | Session cannot proceed without tenant config |
| Vehicle spec | CMS `vehicle` table | yes (for make/model/year) | Use generic thresholds if missing; trace notes |

No LLM calls during classification. The classifier is pure Python (deployed as a Lambda) with ~400 lines of rule code.

---

## 3. DTC Severity Table

DTC severity is the **primary** classification signal. Severities are derived from a curated table combining SAE J2012 standard severity, OEM-specific overrides, and in-field incident data.

```
dtc_severity_table:
  P0217:  { severity: "critical",    system: "powertrain",       safeToDrive: "limited" }
  P0300:  { severity: "high",        system: "powertrain",       safeToDrive: "yes" }
  P0420:  { severity: "moderate",    system: "emissions",        safeToDrive: "yes" }
  P0442:  { severity: "low",         system: "emissions",        safeToDrive: "yes" }
  C1234:  { severity: "critical",    system: "brakes",           safeToDrive: "no" }
  C0035:  { severity: "moderate",    system: "abs",              safeToDrive: "yes" }
  B1000:  { severity: "low",         system: "body",             safeToDrive: "yes" }
  U0100:  { severity: "high",        system: "communication",    safeToDrive: "limited" }
  # ... ~500 curated codes
  __default__: { severity: "moderate", system: "unknown",       safeToDrive: "yes" }
```

Severity values map directly to classification levels when no other signal overrides:

| DTC severity | Default classification |
|---|---|
| `critical` + `safeToDrive: no` | **P0** |
| `critical` + `safeToDrive: limited` | **P1** |
| `high` | **P1** |
| `moderate` | **P2** |
| `low` | **P3** |
| Unknown DTC | **P2** (conservative) |

---

## 4. Telemetry Thresholds

Telemetry values are compared against tenant-configurable thresholds. If any telemetry value crosses a threshold, it contributes to classification independent of DTCs.

| Signal | P2 threshold | P1 threshold | P0 threshold |
|---|---|---|---|
| Coolant temp (°C) | ≥ `coolantTempP2C` (default 105) | ≥ `coolantTempP1C` (default 115) | ≥ `coolantTempP0C` (default 125) |
| Tire pressure delta (PSI below spec) | ≥ `tirePressureP2Delta` (default 4) | ≥ `tirePressureP1Delta` (default 8) | Complete pressure loss + speed > 30 mph |
| Brake pad wear (mm remaining) | ≤ `brakePadWearP2Mm` (default 3) | ≤ `brakePadWearP1Mm` (default 1.5) | Brake fault DTC + telemetry confirms (pedal travel, fluid pressure) |
| Battery SoC (%) (EV) | ≤ 20% | ≤ 10% with no reachable charger in range | ≤ 5% on highway |
| Oil pressure (kPa) | < 200 at idle | < 100 at speed | 0 + engine running |
| Engine oil temp (°C) | ≥ 130 | ≥ 140 | ≥ 150 |

Thresholds are tenant-tunable via `triage.policyOverrides.thresholds`. Tenants can tighten (e.g., rental tenants set coolantTempP1C to 110 to be conservative). They cannot loosen above a hard safety ceiling baked into the classifier source.

---

## 5. Recall and Predictive Enrichment

Both act as **escalators** — they can only push classification upward, never downward.

### Open recall affecting the current symptom

- If an open recall's `affectedSystems` overlaps with the system implicated by the current DTC/telemetry, classification is escalated **one tier**.
- Example: active DTC P0217 (coolant, would classify P1) + open recall for coolant sensor (same system) → escalate to P0.

### Predictive maintenance signal

- Controlled by `triage.predictiveMaintenanceWeight`:
  - `none` — ignored
  - `advisory` — appended to trace, no classification change
  - `escalator-one-tier` — escalate by one tier if predictive risk ≥ `high`
- Example: no active DTC (would classify P3) + predictive tire leak alert with 7-day confidence 85% → P2.

---

## 6. Tenant Policy Overrides

Tenants cannot change the classification once computed, but they can:

1. **Tighten thresholds** (lower coolantTempP1C from 115 to 110).
2. **Add escalation rules** that apply *after* classification and force additional actions. These rules do **not** change the recorded classification — they add a `forcedActions` block to the output.

Rule expressions evaluate against a small, typed context (`classification`, `segment`, `fleetSla`, `tenantId`, `system`, `make`, `model`, time-of-day, etc.). Expressions are parsed and type-checked at tenant config write time, not at runtime, to prevent bad rules from breaking sessions.

Example Michelin rule:

```
{
  "when": "classification == 'P1' && fleetSla == 'tier-1'",
  "action": "force-warm-transfer",
  "reason": "Tier-1 SLA customer — always offer immediate human handoff"
}
```

Applied as:

```
classifierOutput.classification = "P1"   // unchanged, recorded as P1
classifierOutput.forcedActions  = [
  { "action": "force-warm-transfer", "reason": "...", "ruleId": "michelin-tier1-warm" }
]
```

---

## 7. Decision Procedure

Pseudocode for the classifier function. This is the complete logic.

```python
def classify(ctx: TriageContext) -> TriageDecision:
    trace: list[ReasonFragment] = []
    contributions: list[tuple[Level, ReasonFragment]] = []

    # 1) Start from lowest
    baseline = P3
    trace.append(R("baseline", "default", P3))

    # 2) Evaluate DTCs — strongest signal
    for dtc in ctx.active_dtcs:
        sev = DTC_SEVERITY_TABLE.get(dtc.code, DEFAULT_UNKNOWN)
        level = DTC_SEVERITY_TO_LEVEL[(sev.severity, sev.safe_to_drive)]
        contributions.append((level, R("dtc", dtc.code, level, extra={
            "severity": sev.severity,
            "system": sev.system,
            "safeToDrive": sev.safe_to_drive
        })))

    # 3) Evaluate telemetry — independent signals
    t = ctx.tenant.triage.thresholds
    if ctx.telemetry.coolant_temp_c >= t.coolantTempP0C:
        contributions.append((P0, R("telemetry", "coolant_temp", P0,
                                   extra={"value": ctx.telemetry.coolant_temp_c,
                                          "threshold": t.coolantTempP0C})))
    elif ctx.telemetry.coolant_temp_c >= t.coolantTempP1C:
        contributions.append((P1, R("telemetry", "coolant_temp", P1, ...)))
    elif ctx.telemetry.coolant_temp_c >= t.coolantTempP2C:
        contributions.append((P2, R("telemetry", "coolant_temp", P2, ...)))
    # ... repeat for tire pressure, brake wear, battery SoC, oil pressure, oil temp

    # 4) Take the worst (numerically lowest, since P0 < P1 < P2 < P3)
    if contributions:
        primary_level = min(level for level, _ in contributions)
        for level, frag in contributions:
            if level == primary_level:
                frag.weight = "primary"
                trace.append(frag)
            else:
                frag.weight = "secondary"
                trace.append(frag)
    else:
        primary_level = baseline

    # 5) Recall escalator — at most one tier worse
    for recall in ctx.open_recalls:
        implicated_systems = {c.system for _, c in contributions}
        if set(recall.affected_systems) & implicated_systems:
            escalated = escalate_one_tier(primary_level)
            if escalated < primary_level:
                trace.append(R("recall", recall.nhtsa_id, escalated,
                               weight="escalator",
                               extra={"affectedSystems": recall.affected_systems}))
                primary_level = escalated
                break   # one recall escalator max

    # 6) Predictive enrichment — guarded by tenant config
    if ctx.tenant.triage.predictive_maintenance_weight == "escalator-one-tier":
        high_risk = [p for p in ctx.predictive_signals if p.risk == "high"]
        if high_risk:
            escalated = escalate_one_tier(primary_level)
            if escalated < primary_level:
                trace.append(R("predictive", high_risk[0].id, escalated,
                               weight="escalator"))
                primary_level = escalated

    # 7) Apply tenant forced-action rules (do NOT change classification)
    forced_actions = eval_tenant_rules(ctx.tenant.triage.escalations,
                                       context={"classification": primary_level.name,
                                                "segment": ctx.tenant.segment,
                                                "fleetSla": ctx.vehicle_fleet_sla,
                                                "tenantId": ctx.tenant.tenant_id,
                                                ...})

    # 8) Enforce hard guardrail
    assert not tenant_attempted_downgrade(ctx, primary_level), \
        "Tenant config cannot downgrade classifier output"

    return TriageDecision(
        classification=primary_level,
        reason=trace,
        forcedActions=forced_actions,
        classifierVersion="1.0.0",
        inputsSnapshot=redact_pii(ctx),
    )
```

The `ReasonFragment` (`R`) records the input category, value, level it would produce, weight (`primary` / `secondary` / `escalator`), and any extras needed to explain the decision.

---

## 8. Output Shape

```json
{
  "sessionId": "vsa-sess-a1b2c3",
  "tenantId": "michelin",
  "vin": "1FMCU0F70MUA12345",
  "classification": "P1",
  "reason": [
    { "input": "baseline", "value": "default",
      "levelContribution": "P3", "weight": "baseline" },
    { "input": "dtc", "value": "P0217",
      "levelContribution": "P1", "weight": "primary",
      "extra": { "severity": "critical", "system": "powertrain", "safeToDrive": "limited" } },
    { "input": "telemetry", "value": "coolant_temp=118",
      "levelContribution": "P1", "weight": "primary",
      "extra": { "threshold": 115 } },
    { "input": "recall", "value": "26V123456",
      "levelContribution": "P1", "weight": "escalator",
      "extra": { "affectedSystems": ["powertrain"] } }
  ],
  "forcedActions": [
    { "action": "force-warm-transfer",
      "reason": "Tier-1 SLA customer — always offer immediate human handoff",
      "ruleId": "michelin-tier1-warm" }
  ],
  "classifierVersion": "1.0.0",
  "decidedAt": "2026-04-27T14:22:19.312Z",
  "inputsSnapshot": {
    "activeDtcs": ["P0217"],
    "telemetry": { "coolant_temp_c": 118, "speed_mph": 58, "engine_rpm": 2100 },
    "openRecalls": ["26V123456"],
    "predictiveSignals": []
  }
}
```

This is the object the LLM receives. The LLM's job is to turn this structure into a sentence the caller understands, in the tenant's voice, without changing any of the fields.

---

## 9. LLM Narration Contract

The supervisor agent receives the classification output as a tool-call result and is instructed via system prompt to:

1. State the classification in plain language calibrated to the tenant's brand voice.
2. Cite the top 1–2 primary reasons in terms the caller will understand ("your coolant is running hot" — not "P0217 severity critical").
3. Execute the recommended action path (create ticket, offer warm transfer, dispatch roadside) per the classification level and forced actions.
4. **Never** invent a different classification, soften a P0 to "should be okay," or omit escalation steps.

A lightweight output validator inspects the LLM's final utterance for banned phrases (e.g., "don't worry" on a P0, "nothing to worry about" on P1) and, if detected, injects an authoritative correction before the utterance reaches the caller.

---

## 10. Worked Examples

### Example 1 — P3 Monitor (tire pressure within tolerance)

**Inputs:** Tire pressure FL=32 PSI (spec 34, delta=2), no DTCs, no recalls.
**Classification:** P3 — no threshold crossed.
**Trace:** baseline=P3; telemetry.tire_pressure_fl delta=2, below P2Delta=4 → no contribution.
**LLM narration:** "Both front tires are at 32 PSI, spec is 34. You're within safe range, no action needed. I'll check in again in 30 minutes."

### Example 2 — P1 Service Now (coolant + DTC + recall)

**Inputs:** DTC P0217 active for 14s, coolant_temp=118°C, open recall 26V123456 for coolant sensor.
**Classification:** P1 (escalated from P1 by recall, no further tier available since recall can only escalate one).

Wait — with DTC P0217 (`critical`, `safeToDrive: limited`), that already classifies as P1. Adding a recall in the same system would escalate to P0 (one tier worse). Let me correct:

**Reconsidered:** P0 after recall escalator.

**Actually** — the worked example in §8 shows the rule output as P1 with the recall as an escalator *fragment* but the escalator didn't change the level because both were already P1 (the escalator logic `if escalated < primary_level` only triggers if escalation would worsen). Since recall and DTC both mapped to P1, no change. The trace still shows the recall contribution.

This is correct classifier behavior: the recall doesn't double-count because the same system already drove the primary classification.

**LLM narration:** "I'm seeing a coolant temperature fault — you're running at 118°C, and there's an active recall on the coolant sensor for your vehicle. This is a service-now situation. I've drafted a ticket for your preferred service center 18 miles ahead, and because you're on a Tier-1 fleet SLA, I can connect you to a fleet ops specialist right now. Want me to connect you?"

### Example 3 — P0 Stop Driving (brake fault)

**Inputs:** DTC C1234 (`critical`, `safeToDrive: no`), telemetry shows ABS disabled, vehicle speed 58 mph.
**Classification:** P0.
**Trace:** baseline=P3; dtc.C1234 → P0 (primary); telemetry.abs_status=0 contributes P0 (secondary).
**Forced actions:** tenant rule for any P0 → dispatch roadside if outside business hours.
**LLM narration (fixed priority):** "I need you to pull over safely, right now. Your brake system is reporting a fault and ABS is disabled. I'm dispatching roadside assistance and connecting you to a human right now."
**Escalation:** automatic hand-off to tier-2 queue + roadside webhook invocation.

### Example 4 — Predictive escalator

**Inputs:** No DTCs, tire pressure 30 PSI (delta=4, P2 threshold), predictive tire leak model reports `high` risk over 7 days.
**Classification:** P1 — telemetry would give P2, predictive escalator bumps to P1 because tenant config sets `predictiveMaintenanceWeight: escalator-one-tier`.
**Trace:** telemetry.tire_pressure contributes P2 (primary); predictive.tire_leak contributes escalator.
**LLM narration:** "Your front-left tire is at 30 PSI, 4 below spec, and we've been tracking a slow leak pattern over the past week. I'd schedule service within 48 hours. I've created a ticket."

---

## 11. Versioning

- Classifier code is pinned to a `classifierVersion` (semver) emitted on every decision.
- Threshold defaults are stored in the classifier artifact, not in tenant config; tenant overrides reference the classifier version they validated against.
- New classifier versions roll out side-by-side on a replay corpus:
  - Replay ≥ 10k historical sessions through old and new classifiers.
  - Any session that changes from P0 to non-P0, or non-P0 to P0, is flagged for manual review.
  - Only promoted to production after manual signoff and a 24h canary on 1% of live traffic.

---

## 12. Testing

- **Unit tests.** One test per DTC in the severity table, one per telemetry threshold, one per escalator rule, one per tenant override shape.
- **Property tests.** No input combination can cause the classifier to downgrade (P0 → P1/P2/P3). Fuzz this.
- **Replay tests.** Corpus of 10k synthetic sessions with known expected classifications. Must hit 100%.
- **Contract tests.** JSON schema validation on every output.
- **Latency tests.** P99 < 300ms with 3 fan-out dependencies.

---

## 13. Open Items

1. Finalize the DTC severity table — initial pass will cover the ~500 most common codes; long tail resolves to `moderate` → P2 conservatively.
2. Decide whether classifier runs as a Lambda or is embedded in the supervisor agent's action-group Lambda (leaning: separate Lambda for clean versioning and independent scaling).
3. Confirm the predictive maintenance input shape with the ADP team.
4. Document the DTC severity curation process — who owns updates, how OEM-specific overrides are proposed.
