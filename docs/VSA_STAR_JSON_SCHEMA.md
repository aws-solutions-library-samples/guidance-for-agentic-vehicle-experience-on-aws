# VSA STAR-JSON Schema

Every booking-shaped artifact VSA produces — service request tickets, appointment proposals, appointment confirmations, warranty pre-checks — is emitted as **STAR-shaped JSON**. The structure maps 1:1 to the Standards for Technology in Automotive Retail (STAR) XML message types, so tenants already consuming STAR XML can receive a JSON equivalent today and enable an optional XML converter if their downstream systems require XML.

This doc specifies every field for the three v1 message types, maps them to the source data inside VSA, and provides a complete worked example that matches the P1 scenario from `VSA_TRIAGE_CLASSIFIER.md`.

---

## 1. Scope

| Message type | Status | Triggered by | Sink |
|---|---|---|---|
| `ServiceRequestAdd` (STAR `ServiceRepairOrderAdd`-shaped) | v1 | Triage P1 or P0; also P2 if tenant policy requires | Tenant ticket sink |
| `AppointmentAdd` | v1 (draft only) / v2 (proposed + confirmed) | Booking Agent when warm-transfer or slot proposal is invoked | Tenant ticket sink, Connect attributes for warm transfer |
| `WarrantyClaimPreCheck` | v1 (advisory only) | Booking Agent when warranty coverage is relevant | Tenant ticket sink; ADP warranty system on confirm |

A single VSA session may emit one or more messages (typically one `ServiceRequestAdd` plus an optional `WarrantyClaimPreCheck`). Messages are correlated by `sessionId`.

---

## 2. Common Envelope

Every message shares a common envelope. The envelope is the outer object; the payload varies by message type.

```json
{
  "schemaVersion": "1.0.0",
  "messageType": "ServiceRequestAdd | AppointmentAdd | WarrantyClaimPreCheck",
  "messageId": "msg-uuid",
  "sessionId": "vsa-sess-a1b2c3",
  "tenantId": "michelin",
  "createdAt": "ISO 8601",
  "createdBy": "vsa-supervisor-v1",
  "correlation": {
    "triageDecisionId": "triage-uuid",
    "vin": "1FMCU0F70MUA12345",
    "customerId": "cust-1234"
  },
  "payload": { /* message-specific, see §3–§5 */ },
  "signature": {
    "algo": "HMAC-SHA256",
    "keyId": "vsa-msg-signing-key-2026-04",
    "value": "base64..."
  }
}
```

Fields:

| Field | Required | Notes |
|---|---|---|
| `schemaVersion` | yes | Semver. Consumers branch on version. |
| `messageType` | yes | One of the three listed. |
| `messageId` | yes | Globally unique (UUID v4). |
| `sessionId` | yes | Links to VSA session for cross-referencing. |
| `tenantId` | yes | Slug from tenant config. |
| `createdAt` | yes | UTC, ISO 8601 with ms. |
| `createdBy` | yes | Agent identifier + version. |
| `correlation.triageDecisionId` | yes for ServiceRequestAdd | Links to classifier output. |
| `correlation.vin` | yes | The vehicle the message is about. |
| `correlation.customerId` | conditional | Present if resolved; null for anonymous-demo. |
| `signature` | yes for external sinks | HMAC over the canonical JSON of the envelope minus the signature block; verifies integrity for downstream consumers. |

---

## 3. `ServiceRequestAdd` Payload

Maps to STAR `ServiceRepairOrderAdd`. Captures the service request created during a VSA session.

```json
{
  "requestNumber": "VSA-2026-04-27-4821",
  "priority": "P1",
  "urgency": {
    "classification": "service-now",
    "recommendedWindowHours": 48,
    "safetyAssessment": {
      "safeToDrive": "limited",
      "restrictions": ["Avoid extended highway driving until resolved"]
    }
  },
  "customer": {
    "customerId": "cust-1234",
    "tenantId": "michelin",
    "name": "T. Hassan",
    "contact": {
      "phone": "+15551234567",
      "email": null,
      "preferredChannel": "voice",
      "locale": "en-US"
    }
  },
  "vehicle": {
    "vin": "1FMCU0F70MUA12345",
    "vehicleId": "VEH-0025",
    "make": "Freightliner",
    "model": "Cascadia",
    "modelYear": 2023,
    "odometerMiles": 187432,
    "fleetContext": {
      "fleetId": "michelin-north-america",
      "slaTier": "tier-1",
      "downtimeBudgetHoursRemainingMonth": 2.5
    }
  },
  "serviceCenter": {
    "preferred": {
      "id": "SVC-DAL-014",
      "name": "Rush Truck Center — Dallas North",
      "address": "2101 Main St, Dallas, TX 75208",
      "distanceMiles": 18,
      "contact": "+12145550100",
      "networkTier": "preferred"
    },
    "alternatives": [
      {
        "id": "SVC-DAL-022",
        "name": "Authorized Diesel Service",
        "distanceMiles": 31,
        "networkTier": "authorized"
      }
    ]
  },
  "symptoms": {
    "reported": "Driver reports temperature gauge spiked while on I-30 westbound.",
    "observedByVsa": [
      {
        "type": "dtc",
        "code": "P0217",
        "description": "Engine coolant over-temperature condition",
        "firstSeenAt": "2026-04-27T14:22:05Z",
        "persistent": true
      },
      {
        "type": "telemetry",
        "signal": "coolant_temp_c",
        "value": 118,
        "threshold": 115,
        "observedAt": "2026-04-27T14:22:18Z"
      }
    ],
    "relatedRecalls": [
      {
        "nhtsaId": "26V123456",
        "description": "Coolant temperature sensor may provide inaccurate readings",
        "remedyAvailable": true,
        "openedAt": "2026-02-14T00:00:00Z"
      }
    ]
  },
  "workScope": {
    "primaryComplaint": "Coolant over-temperature with active DTC",
    "recommendedInspections": [
      "Coolant temperature sensor diagnostic (linked recall 26V123456)",
      "Coolant level and hose integrity check",
      "Thermostat operation test",
      "Radiator fan operation test"
    ],
    "partsPlanningHint": [
      {
        "partNumber": "TBD-COOLANT-SENSOR-23",
        "partDescription": "Coolant temperature sensor (recall remedy)",
        "estimatedAvailability": "in-stock-network"
      }
    ],
    "estimatedLaborHours": 1.5
  },
  "warranty": {
    "preCheckStatus": "covered",
    "coverageType": "powertrain-5yr-60k",
    "expiresAt": "2028-03-15T00:00:00Z",
    "deductible": 0,
    "claimPreCheckMessageId": "msg-uuid-warranty-precheck",
    "notes": "Coolant sensor failure within recall scope — no warranty deductible expected."
  },
  "costEstimate": {
    "estimatedTotalUsd": 0,
    "basis": "Recall remedy covers parts and labor",
    "confidence": "high"
  },
  "actions": {
    "recommended": "schedule-at-preferred",
    "offeredToCustomer": [
      "create-ticket",
      "warm-transfer-to-fleet-ops",
      "schedule-roadside-if-vehicle-fails"
    ],
    "takenByCustomer": ["warm-transfer-to-fleet-ops"]
  },
  "narrative": "P1 service-now classification. Coolant over-temperature confirmed by telemetry and DTC, with an open recall for the coolant sensor that likely explains the failure mode. Recommend service within 48 hours at the preferred Dallas center. Customer is a Tier-1 SLA fleet — warm transfer offered and accepted."
}
```

### Field reference — `ServiceRequestAdd`

| Field | Source | Notes |
|---|---|---|
| `requestNumber` | Generated | Format: `VSA-{YYYY-MM-DD}-{seq}`. Stable across sink deliveries. |
| `priority` | Triage classifier output | Literal P0/P1/P2/P3. |
| `urgency.classification` | Derived from priority | `stop-driving` / `service-now` / `service-soon` / `monitor`. |
| `urgency.recommendedWindowHours` | Classifier policy | Per-tenant tunable. |
| `urgency.safetyAssessment` | Classifier trace + DTC severity table | |
| `customer.*` | ADP C360 lookup | `preferredChannel` inferred from current session. |
| `vehicle.*` | CMS `vehicle` table + Redis LKS | `fleetContext` optional. |
| `serviceCenter.*` | Tenant config + geospatial query against service center table | Supervisor picks preferred within range. |
| `symptoms.reported` | LLM summary of caller's initial complaint | Free-form, PII-redacted. |
| `symptoms.observedByVsa` | Triage classifier `inputsSnapshot` | DTCs + telemetry + recalls, structured. |
| `workScope.recommendedInspections` | Derived from DTC + affected systems | Enumerated lookup, not LLM-invented. |
| `workScope.partsPlanningHint` | ADP parts catalog lookup | Advisory only. |
| `warranty` | ADP warranty check via `RecallWarrantyAgent` | See §5 for the pre-check message. |
| `costEstimate` | VFO Cost Agent, optional | May be null if cost agent disabled for tenant. |
| `actions.offeredToCustomer` / `takenByCustomer` | Booking Agent session state | Auditable record of what options the caller had and chose. |
| `narrative` | LLM, post-classification | Natural-language summary for human reviewers. Cannot change any structured field. |

### Enumerations

- `priority`: `P0` | `P1` | `P2` | `P3`
- `urgency.classification`: `stop-driving` | `service-now` | `service-soon` | `monitor`
- `safetyAssessment.safeToDrive`: `yes` | `limited` | `no`
- `serviceCenter.networkTier`: `preferred` | `authorized` | `independent`
- `warranty.preCheckStatus`: `covered` | `partially-covered` | `not-covered` | `pending` | `unknown`
- `actions.recommended`: `schedule-at-preferred` | `schedule-at-alternative` | `dispatch-roadside` | `monitor` | `no-action`
- `actions.offeredToCustomer` / `takenByCustomer`: `create-ticket` | `warm-transfer-to-fleet-ops` | `warm-transfer-to-dealer` | `schedule-roadside-if-vehicle-fails` | `propose-slot` | `book-appointment`

---

## 4. `AppointmentAdd` Payload

Maps to STAR `AppointmentAdd`. In v1, VSA emits an `AppointmentAdd` in **draft** state to accompany a `ServiceRequestAdd` when warm-transfer is offered; the receiving tier-2 agent or dealer system confirms the slot and emits an updated `AppointmentAdd` with `status: "confirmed"`.

In v2+, VSA directly proposes slots via a calendar feed and emits `status: "proposed"` → `status: "confirmed"` transitions.

```json
{
  "appointmentNumber": "APT-2026-04-27-4821-D1",
  "status": "draft",
  "serviceRequestNumber": "VSA-2026-04-27-4821",
  "serviceCenter": {
    "id": "SVC-DAL-014",
    "name": "Rush Truck Center — Dallas North"
  },
  "proposedSlots": [],
  "confirmedSlot": null,
  "vehicle": { "vin": "1FMCU0F70MUA12345", "vehicleId": "VEH-0025" },
  "customer": { "customerId": "cust-1234", "name": "T. Hassan" },
  "requestedBy": "vsa-booking-agent-v1",
  "notes": "Draft appointment attached to service request. Tier-2 fleet-ops to confirm slot and update to 'confirmed' state."
}
```

### Field reference — `AppointmentAdd`

| Field | Source | Notes |
|---|---|---|
| `appointmentNumber` | Generated | Includes `-D{n}` for draft iterations, `-C{n}` for confirmed. |
| `status` | State machine | `draft` → `proposed` → `confirmed` / `cancelled`. |
| `serviceRequestNumber` | Linked to parent ServiceRequestAdd | Required. |
| `proposedSlots` | v2 — populated from tenant availability feed | Array of `{startAt, endAt, serviceCenterId}`. |
| `confirmedSlot` | v2 — populated when customer accepts | Single object. |
| `requestedBy` | Agent identifier | For audit. |

### Enumerations

- `status`: `draft` | `proposed` | `confirmed` | `cancelled`

---

## 5. `WarrantyClaimPreCheck` Payload

Maps to STAR `WarrantyClaimAdd` in a pre-check / advisory state. Produced when the triage surfaces a component likely to be under warranty; gives the receiving dealer or warranty system a structured pre-flight so the actual claim submission downstream is faster and more accurate.

```json
{
  "preCheckNumber": "WPC-2026-04-27-4821",
  "status": "advisory",
  "serviceRequestNumber": "VSA-2026-04-27-4821",
  "vehicle": {
    "vin": "1FMCU0F70MUA12345",
    "modelYear": 2023,
    "odometerMiles": 187432
  },
  "coverage": {
    "type": "powertrain-5yr-60k",
    "effectiveFrom": "2023-03-15T00:00:00Z",
    "expiresAt": "2028-03-15T00:00:00Z",
    "odometerLimit": 60000,
    "odometerRemaining": null,
    "deductible": 0,
    "source": "adp-warranty-lookup"
  },
  "claimScope": {
    "complaint": "Coolant over-temperature",
    "suspectedComponent": "coolant_temperature_sensor",
    "suspectedFailureMode": "sensor-drift-per-recall-26V123456",
    "relatedRecall": "26V123456",
    "partsSuspectedUnderWarranty": [
      { "partNumber": "TBD-COOLANT-SENSOR-23", "description": "Coolant temperature sensor" }
    ]
  },
  "determination": {
    "result": "covered",
    "rationale": "Failure mode falls within open manufacturer recall; recall remedy covers parts and labor. No deductible expected.",
    "requiresDealerConfirmation": true,
    "confidence": "high"
  },
  "createdBy": "vsa-recall-warranty-agent-v1",
  "notes": "Pre-check is advisory. Dealer must submit the actual WarrantyClaimAdd after diagnosis confirmation."
}
```

### Field reference — `WarrantyClaimPreCheck`

| Field | Source | Notes |
|---|---|---|
| `preCheckNumber` | Generated | Correlates with parent service request. |
| `status` | Always `advisory` at v1 | A later phase will support `submitted` / `approved` / `denied` when integrated with dealer warranty systems. |
| `coverage.*` | ADP warranty lookup | `source` records which system provided the data. |
| `claimScope.suspectedComponent` | Derived from DTC + recall mapping | Enumerated component taxonomy. |
| `determination.result` | Classifier + warranty lookup | See enumeration below. |
| `determination.requiresDealerConfirmation` | Always true at v1 | Becomes false when direct DMS integration exists. |

### Enumerations

- `status`: `advisory` | `submitted` | `approved` | `denied`
- `determination.result`: `covered` | `partially-covered` | `not-covered` | `indeterminate`
- `determination.confidence`: `high` | `medium` | `low`

---

## 6. Worked Example — Full Envelope for the P1 Coolant Scenario

This is the `ServiceRequestAdd` message corresponding to the P1 worked example in `VSA_TRIAGE_CLASSIFIER.md §10`. Truncated for readability — full structure is as §3 above.

```json
{
  "schemaVersion": "1.0.0",
  "messageType": "ServiceRequestAdd",
  "messageId": "msg-9fa8c0b1-...",
  "sessionId": "vsa-sess-a1b2c3",
  "tenantId": "michelin",
  "createdAt": "2026-04-27T14:22:30.145Z",
  "createdBy": "vsa-supervisor-v1",
  "correlation": {
    "triageDecisionId": "triage-2026-04-27-0001",
    "vin": "1FMCU0F70MUA12345",
    "customerId": "cust-1234"
  },
  "payload": { /* see §3 */ },
  "signature": {
    "algo": "HMAC-SHA256",
    "keyId": "vsa-msg-signing-key-2026-04",
    "value": "R1Jz...base64..."
  }
}
```

Correlated messages in the same session:

1. `ServiceRequestAdd` (above) — primary ticket.
2. `AppointmentAdd` in `draft` state — linked via `serviceRequestNumber`, tier-2 or dealer confirms.
3. `WarrantyClaimPreCheck` — linked via `serviceRequestNumber`, advisory.

All three land in the tenant's ticket sink. Tenants process them together.

---

## 7. STAR XML Converter

Tenants with `booking.starXmlConversion: true` in tenant config get an additional XML artifact per message, generated from the same JSON source.

### Mapping principles

- Envelope `messageType` → STAR XML root element name (`ServiceRepairOrderAdd`, `AppointmentAdd`, `WarrantyClaimAdd`).
- STAR's `ApplicationArea` block is populated from envelope fields (`messageId` → `BODId`, `createdAt` → `CreationDateTime`, `createdBy` → `Sender/Component`).
- Each JSON field maps to its STAR XML element per the STAR 5.x schema. Unknown or VSA-specific fields are emitted inside a STAR `UserArea` extension block to preserve round-trip integrity.
- Arrays → repeated elements (`<Symptom>`, `<RecommendedInspection>`, `<AlternativeServiceCenter>`).
- Enumerations map to STAR-approved code values where they exist; otherwise to the STAR `OtherCode` mechanism with the VSA enum value as an attribute.
- Date/time → STAR `xsd:dateTime` with timezone. IDs → STAR `ID` elements with `agencyCode="VSA"`.

### Converter contract

Converter is a pure function:

```
convert_to_star_xml(envelope_json: dict, star_version: str = "5.6.4") -> str
```

- Idempotent — same JSON input produces byte-identical XML output.
- Emits a `<Verb>` attribute (`Add` / `Change` / `Cancel`) derived from `messageType` and payload `status`.
- Validates output against the STAR XSD for `star_version`; failure is a hard error, not a best-effort.
- Ships as a thin Lambda (`vsa-star-xml-converter`) invoked only for tenants that opt in. Default v1 behavior: no XML emitted.

### Delivery

When XML conversion is enabled:

- JSON is always delivered to the sink (source of truth).
- XML is delivered alongside as a second artifact — either:
  - A separate message for sinks that accept heterogeneous payloads (S3, SQS with MIME type).
  - A base64-encoded attachment field on the JSON envelope (`payload._xmlAttachment`) for sinks that are single-message-oriented.

Mapping-level field-by-field documentation is tracked in a companion appendix (to be generated alongside the JSON Schema artifact at `schemas/vsa-star.schema.json`).

---

## 8. Non-goals at v1

- Submission of actual warranty claims to OEM warranty systems (advisory pre-check only).
- Full DMS booking (`AppointmentAdd` in `confirmed` state from a real DMS API) — deferred to v3.
- STAR `ServiceEstimateAdd`, `PartsInventoryQuery`, `RepairOrderChange` — deferred to later phases.
- OEM-specific STAR extensions (Ford's `F_*` extensions, GM's `GM_*` extensions) — handled in tenant-specific agents, emitted in `UserArea`.

---

## 9. Versioning

- Breaking changes bump `schemaVersion` major.
- Non-breaking additions (new optional fields, new enumeration values) bump minor.
- Consumers must ignore unknown fields rather than fail.
- STAR converter version is reported alongside in an envelope attribute (`starConverterVersion`) when XML is emitted.

---

## 10. Open Items

1. Finalize which STAR 5.x version is the default (5.6.4 is a common production anchor).
2. Generate machine-readable JSON Schema at `schemas/vsa-star.schema.json` from this document.
3. Confirm HMAC signing key rotation policy with tenants — proposed: 90-day rotation, key ID in envelope.
4. Confirm naming: STAR calls the service-request concept `ServiceRepairOrderAdd` in some versions and `RepairOrderAdd` in others; we normalize to `ServiceRequestAdd` inside VSA and map outward.
