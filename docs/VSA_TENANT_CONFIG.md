# VSA Tenant Configuration

Every tenant is defined by a single JSON config record. One VSA deployment serves many tenants; no code changes are required to onboard, modify, or disable a tenant.

This document specifies the schema, enumerates every field, and provides three worked examples:

- **Cascadia** — fully filled in (v1 primary tenant)
- **Halcyon** — OEM preview (v2)
- **Avis** — rental preview (v2)

---

## 1. Storage

- **Table.** DynamoDB `vsa-tenant-config`
- **Primary key.** `tenantId` (string)
- **Sort key.** `version` (string, semver) — many versions may exist; `currentVersion` field elsewhere (`vsa-tenant-current`) points to the active one
- **Cache.** ElastiCache `vsa-tenant-cache` with 60s TTL; reads go to cache first, fall back to DynamoDB
- **Audit.** Every version written is immutable; updates create a new version row

Tenant config **version** is stamped onto every triage decision and ticket so the exact policy applied at decision time is reproducible.

---

## 2. Schema

```jsonc
{
  "tenantId":              "string, slug, required, 3–32 chars, [a-z0-9-]",
  "version":               "string, semver, required",
  "status":                "enum: active | paused | archived, required",
  "displayName":           "string, required",
  "segment":               "enum: fleet | oem | rental, required",

  "branding": {
    "logoUrl":             "string, https URL",
    "primaryColor":        "string, hex",
    "secondaryColor":      "string, hex",
    "fontFamily":          "string, CSS font stack",
    "mapStyle":            "string, MapLibre style URL or preset name",
    "greeting": {
      "voice":             "string, TTS copy for Connect",
      "chat":              "string, opening chat message",
      "app":               "string, companion app hero copy"
    }
  },

  "channels": {
    "connect": {
      "instanceArn":       "string, Connect instance ARN",
      "inboundDid":        "string, E.164",
      "contactFlowId":     "string, Connect flow ID",
      "lexBotAliasArn":    "string, Lex bot alias ARN",
      "recording":         "boolean, default true"
    },
    "companionApp": {
      "enabled":           "boolean",
      "subdomain":         "string, e.g. 'michelin' for michelin.vsa.example.com",
      "authMode":          "enum: sso | signed-link | anonymous-demo"
    }
  },

  "voice": {
    "engine":              "enum: nova-sonic | polly-neural",
    "voiceId":             "string, e.g. 'matthew', 'amy', nova-sonic voice ID",
    "locale":              "string, BCP-47, e.g. en-US, fr-FR"
  },

  "identity": {
    "callerResolution":    "enum: phone-lookup | account-number | sso | vin-prompt",
    "tenantIdp": {
      "cognitoUserPoolArn": "string, optional",
      "oidcDiscoveryUrl":   "string, optional"
    },
    "phoneLookupSource":   "enum: adp-c360 | tenant-crm-webhook | none",
    "tenantCrmWebhook":    "string, optional HTTPS endpoint for tenant-owned lookups",
    "consentRequired":     ["recording", "smsOutreach"]
  },

  "agents": {
    "supervisor": {
      "modelId":           "string, Bedrock model ID, e.g. 'anthropic.claude-sonnet-4-...'",
      "systemPromptId":    "string, references a prompt in Bedrock prompt management",
      "maxTokens":         "number, default 4096"
    },
    "specialistsEnabled":  ["cost", "recall-warranty", "maintenance", "rebalancing"],
    "tenantSpecialists":   [
      {
        "id":              "string, e.g. 'fleet-sla'",
        "lambdaArn":       "string",
        "description":     "string"
      }
    ],
    "knowledgeBaseIds": {
      "shared":            "string, VFO shared KB ID",
      "tenantOverlay":     "string, per-tenant KB ID (OEM-specific manuals, rental policies, fleet SLAs)"
    }
  },

  "triage": {
    "policyOverrides": {
      "thresholds": {
        "coolantTempP1C":        "number, default 115",
        "coolantTempP0C":        "number, default 125",
        "tirePressureP2Delta":   "number, default 4 (PSI below spec)",
        "tirePressureP1Delta":   "number, default 8",
        "brakePadWearP2Mm":      "number, default 3",
        "brakePadWearP1Mm":      "number, default 1.5"
      },
      "escalations": [
        {
          "when":          "string, expression, e.g. 'classification == P1 && segment == rental'",
          "action":        "enum: force-tier2 | dispatch-roadside | force-warm-transfer",
          "reason":        "string, human-readable"
        }
      ],
      "downgradeForbidden": true
    },
    "predictiveMaintenanceWeight": "enum: none | escalator-one-tier | advisory"
  },

  "booking": {
    "ticketSink": {
      "type":              "enum: sqs | webhook | salesforce | servicenow | dynamodb",
      "target":            "string, queue ARN | HTTPS URL | Salesforce org + object | ServiceNow table",
      "credentialsSecretArn": "string, Secrets Manager ARN, required for salesforce/servicenow/webhook with auth"
    },
    "starXmlConversion":   "boolean, default false",
    "warmTransfer": {
      "enabled":           "boolean",
      "queueArn":          "string, Connect queue ARN, required if enabled",
      "businessHours":     "string, cron-style expression (tz-aware); outside hours warm transfer is suppressed"
    },
    "slotProposal": {
      "enabled":           "boolean (v2)",
      "availabilityFeed":  "string, iCal URL or API endpoint (v2)"
    },
    "dmsIntegration": {
      "enabled":           "boolean (v3)",
      "provider":          "enum: cdk | reynolds | tekion | dealertrack | fleetio | whip-around | custom",
      "credentialsSecretArn": "string"
    }
  },

  "escalation": {
    "tier2QueueArn":       "string, Connect queue ARN",
    "businessHours":       "string, cron-style",
    "outsideHoursBehavior": "enum: voicemail | callback-request | next-open-queue | force-roadside-if-p0",
    "roadsideDispatch": {
      "enabled":           "boolean",
      "provider":          "enum: tenant-managed | partner-api",
      "partnerWebhook":    "string, optional"
    }
  },

  "policies": {
    "dataResidencyRegion": "string, AWS region, e.g. 'us-east-1'",
    "auditLogRetentionDays": "number, default 90",
    "recordingRetentionDays": "number, default 30",
    "piiRedaction": {
      "enabled":           "boolean, default true",
      "fields":            ["phoneNumber", "email", "licensePlate", "driversLicense"]
    },
    "budgets": {
      "monthlyTokenBudget": "number, optional",
      "perSessionTokenCap": "number, default 50000"
    }
  },

  "observability": {
    "logGroupPrefix":      "string, default '/aws/vsa/{tenantId}'",
    "metricsNamespace":    "string, default 'VSA/Tenant'",
    "alarmSnsTopicArn":    "string, optional"
  },

  "features": {
    "imageInput":          "boolean, default false",
    "agentAssistCoPilot":  "boolean, default false (v2+)",
    "calendarSlotProposal": "boolean, default false (v2+)",
    "dmsBooking":          "boolean, default false (v3+)"
  },

  "metadata": {
    "createdAt":           "ISO 8601",
    "updatedAt":           "ISO 8601",
    "updatedBy":           "string, IAM principal or operator email",
    "notes":               "string, free-form"
  }
}
```

---

## 3. Field Groups Explained

### `branding`

Everything required to reskin the companion app and Connect voice experience. The companion app reads this block at load time and applies it via CSS custom properties. The Connect contact flow reads `greeting.voice` via a Lambda attribute fetch.

### `channels`

- `connect.instanceArn` — each tenant may have its own Connect instance, or share one where the contact flow branches on DNIS.
- `companionApp.subdomain` — VSA hosts `{subdomain}.vsa.example.com` and routes to a single React bundle; the subdomain selects the tenant.
- `companionApp.authMode = anonymous-demo` — used for exec briefings where the presenter bypasses auth. Gated by IAM.

### `voice`

Nova 2 Sonic is the v1 default for fleet driver scenarios (unified speech-to-speech, sub-700ms latency, native Amazon Connect integration). Polly + Transcribe is the fallback stack when Nova 2 Sonic is unavailable in a region. Nova Sonic v1 is not supported — it is being retired September 14, 2026.

### `identity`

Five callers resolve in five different ways. Fleet tenants (Cascadia) typically use SSO from the companion app and phone-lookup via ADP C360 on voice. OEM tenants (Halcyon) typically use account number + phone lookup. Rental tenants (Avis) resolve by rental agreement number.

### `agents`

- `specialistsEnabled` — whitelist of reused VFO agents. A tenant may disable, say, `rebalancing` if irrelevant to their use case.
- `tenantSpecialists` — additional Lambdas specific to this tenant (e.g., `fleet-sla` for Cascadia, `rental-policy` for Avis). Lambdas are developed separately and referenced here by ARN.
- `knowledgeBaseIds.shared` — the VFO cross-tenant KB (NHTSA, general service manuals).
- `knowledgeBaseIds.tenantOverlay` — the tenant-specific KB (OEM FSBs, rental policies, fleet SLAs, brand voice guidelines). Retrieval queries **both** KBs with overlay results preferred.

### `triage.policyOverrides`

Tenants can tighten thresholds but **cannot downgrade** classifications. `downgradeForbidden: true` is a hard guardrail; the classifier enforces it. Tenants can add escalation rules that force a higher response (e.g., rental tenants force tier-2 on any P1 because a stranded rental customer is business-critical).

### `booking.ticketSink`

Five sink options supported. `dynamodb` is the always-on fallback — even when an external sink is configured, VSA first writes to the fallback table, then attempts external delivery with retries.

### `escalation.outsideHoursBehavior`

- `voicemail` — record a message and email the tier-2 supervisor
- `callback-request` — create a callback record in the ticket sink
- `next-open-queue` — route to another geographically-available queue
- `force-roadside-if-p0` — safety override; for P0 during off-hours, always dispatch roadside

### `policies.budgets`

Per-tenant monthly token budget. When exceeded, sessions degrade to the deterministic classifier only (no LLM narration); an alarm fires.

---

## 4. Worked Example: Cascadia (Fleet, v1)

```json
{
  "tenantId": "michelin",
  "version": "1.0.0",
  "status": "active",
  "displayName": "Cascadia Fleet Services",
  "segment": "fleet",

  "branding": {
    "logoUrl": "https://assets.vsa.example.com/michelin/logo.svg",
    "primaryColor": "#1A3CA8",
    "secondaryColor": "#F4D35E",
    "fontFamily": "'Univers', 'Helvetica Neue', sans-serif",
    "mapStyle": "mapbox://styles/vsa/fleet-dark",
    "greeting": {
      "voice": "Cascadia Fleet Support. I can see your vehicle in real time. How can I help?",
      "chat": "Hi — I'm Cascadia Fleet Support. I have live access to VEH-0025. What's going on?",
      "app": "Live support for your fleet. Ask about any vehicle, any alert, any time."
    }
  },

  "channels": {
    "connect": {
      "instanceArn": "arn:aws:connect:us-east-1:111122223333:instance/michelin-prod",
      "inboundDid": "+18005551200",
      "contactFlowId": "abc-123-def-456",
      "lexBotAliasArn": "arn:aws:lex:us-east-1:111122223333:bot-alias/MICHELIN/PROD",
      "recording": true
    },
    "companionApp": {
      "enabled": true,
      "subdomain": "michelin",
      "authMode": "sso"
    }
  },

  "voice": {
    "engine": "nova-2-sonic",
    "voiceId": "matthew",
    "locale": "en-US"
  },

  "identity": {
    "callerResolution": "phone-lookup",
    "tenantIdp": {
      "cognitoUserPoolArn": "arn:aws:cognito-idp:us-east-1:111122223333:userpool/<your-tenant-sso-pool-id>"
    },
    "phoneLookupSource": "adp-c360",
    "consentRequired": ["recording"]
  },

  "agents": {
    "runtime": "agentcore",
    "supervisor": {
      "framework": "strands",
      "modelId": "anthropic.claude-sonnet-4-6",
      "systemPromptId": "vsa-supervisor-fleet-v1",
      "maxTokens": 4096
    },
    "specialistsEnabled": ["recall-warranty", "maintenance", "cost"],
    "tenantSpecialists": [
      {
        "id": "fleet-sla",
        "lambdaArn": "arn:aws:lambda:us-east-1:111122223333:function:vsa-tenant-michelin-fleet-sla",
        "description": "Checks the vehicle against Cascadia's SLA tiers and downtime budget"
      }
    ],
    "knowledgeBaseIds": {
      "shared": "VFO_SHARED_KB_ID",
      "tenantOverlay": "MICHELIN_TENANT_KB_ID"
    }
  },

  "triage": {
    "policyOverrides": {
      "thresholds": {
        "coolantTempP1C": 115,
        "coolantTempP0C": 125,
        "tirePressureP2Delta": 4,
        "tirePressureP1Delta": 8,
        "brakePadWearP2Mm": 3,
        "brakePadWearP1Mm": 1.5
      },
      "escalations": [
        {
          "when": "classification == 'P1' && fleetSla == 'tier-1'",
          "action": "force-warm-transfer",
          "reason": "Tier-1 SLA customer — always offer immediate human handoff"
        }
      ],
      "downgradeForbidden": true
    },
    "predictiveMaintenanceWeight": "escalator-one-tier"
  },

  "booking": {
    "ticketSink": {
      "type": "sqs",
      "target": "arn:aws:sqs:us-east-1:444455556666:michelin-service-requests",
      "credentialsSecretArn": null
    },
    "starXmlConversion": false,
    "warmTransfer": {
      "enabled": true,
      "queueArn": "arn:aws:connect:us-east-1:111122223333:instance/michelin-prod/queue/tier2-ops",
      "businessHours": "0 6 * * 1-5 America/New_York / 0 22 * * 1-5 America/New_York"
    },
    "slotProposal": { "enabled": false, "availabilityFeed": null },
    "dmsIntegration": { "enabled": false, "provider": null, "credentialsSecretArn": null }
  },

  "escalation": {
    "tier2QueueArn": "arn:aws:connect:us-east-1:111122223333:instance/michelin-prod/queue/tier2-ops",
    "businessHours": "0 6 * * 1-5 America/New_York / 0 22 * * 1-5 America/New_York",
    "outsideHoursBehavior": "force-roadside-if-p0",
    "roadsideDispatch": {
      "enabled": true,
      "provider": "partner-api",
      "partnerWebhook": "https://roadside.michelin-partner.example.com/dispatch"
    }
  },

  "policies": {
    "dataResidencyRegion": "us-east-1",
    "auditLogRetentionDays": 180,
    "recordingRetentionDays": 30,
    "piiRedaction": {
      "enabled": true,
      "fields": ["phoneNumber", "email", "licensePlate", "driversLicense"]
    },
    "budgets": {
      "monthlyTokenBudget": 50000000,
      "perSessionTokenCap": 50000
    }
  },

  "observability": {
    "logGroupPrefix": "/aws/vsa/michelin",
    "metricsNamespace": "VSA/Tenant",
    "alarmSnsTopicArn": "arn:aws:sns:us-east-1:111122223333:vsa-alarms-michelin"
  },

  "features": {
    "imageInput": false,
    "agentAssistCoPilot": false,
    "calendarSlotProposal": false,
    "dmsBooking": false
  },

  "metadata": {
    "createdAt": "2026-04-27T00:00:00Z",
    "updatedAt": "2026-04-27T00:00:00Z",
    "updatedBy": "andrewg@example.com",
    "notes": "v1 launch tenant. Tier-1 SLA customers force warm transfer on any P1."
  }
}
```

---

## 5. Worked Example: Halcyon (OEM, v2 preview stub)

```jsonc
{
  "tenantId": "ford",
  "version": "0.1.0",
  "status": "paused",
  "segment": "oem",
  "displayName": "Halcyon Customer Service",
  "branding": { "primaryColor": "#003478", "secondaryColor": "#FFFFFF", "...": "..." },
  "identity": { "callerResolution": "account-number", "phoneLookupSource": "tenant-crm-webhook", "...": "..." },
  "agents": {
    "tenantSpecialists": [
      { "id": "ford-warranty-extended", "lambdaArn": "...", "description": "Halcyon ESP coverage lookup" }
    ],
    "knowledgeBaseIds": { "tenantOverlay": "FORD_TENANT_KB_ID" }
  },
  "booking": {
    "ticketSink": { "type": "salesforce", "target": "ford.my.salesforce.com/Case", "credentialsSecretArn": "..." }
  }
}
```

Placeholder only. Filled out fully during v2.

---

## 6. Worked Example: Avis (Rental, v2 preview stub)

```jsonc
{
  "tenantId": "avis",
  "version": "0.1.0",
  "status": "paused",
  "segment": "rental",
  "displayName": "Avis Roadside Assistance",
  "branding": { "primaryColor": "#D4002A", "...": "..." },
  "identity": { "callerResolution": "account-number", "...": "..." },
  "triage": {
    "policyOverrides": {
      "escalations": [
        { "when": "classification == 'P1'", "action": "force-tier2",
          "reason": "Stranded rental customer — always human" }
      ]
    }
  },
  "agents": {
    "tenantSpecialists": [
      { "id": "rental-policy", "lambdaArn": "...", "description": "Swap-vehicle eligibility, mileage caps" }
    ]
  },
  "booking": {
    "ticketSink": { "type": "webhook", "target": "https://avis.internal/vsa-tickets", "credentialsSecretArn": "..." }
  }
}
```

Placeholder only. Filled out fully during v2.

---

## 7. Validation

Every tenant config write is validated server-side against this schema before being persisted:

- JSON Schema (Draft 2020-12) artifact lives at `schemas/vsa-tenant-config.schema.json` (to be generated from this doc).
- CI rejects any config that fails schema validation or references non-existent AWS resources (Connect instance, KB ID, Lambda ARN, Queue ARN).
- Dry-run: a staged config can be validated against a replay session corpus before being promoted to `active`.

---

## 8. Change Management

- Updates write a new `version` row; the active version is pointed to via `vsa-tenant-current`.
- Rollback is a pointer flip, not a data restore.
- Breaking schema evolutions bump a top-level `schemaVersion` attribute (not shown in config body itself) and require a coordinated migration.
