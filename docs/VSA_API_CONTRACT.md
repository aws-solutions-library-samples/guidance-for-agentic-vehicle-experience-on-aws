# VSA API Contract

The contract the iOS companion app (and any future client) implements against. Source of truth for endpoint shapes, auth, and error handling.

**Base URL** (outputs from `vsa-prod-api` stack):
- REST: `https://{restApiId}.execute-api.us-east-1.amazonaws.com/prod`
- WebSocket: `wss://{wsApiId}.execute-api.us-east-1.amazonaws.com/prod?token={jwt}`

Both URLs are emitted as CloudFormation outputs; clients read them from a bundled config generated at deploy time.

---

## 1. Authentication

### Per-tenant Cognito user pools

One pool per tenant. Pool IDs are emitted as CFN exports: `vsa-prod-{tenantId}-pool-id` and `vsa-prod-{tenantId}-client-id`.

Users are admin-created. There is no self-signup.

### JWT claims the API enforces

| Claim | Source | Used for |
|---|---|---|
| `custom:tenantId` | Set by admin at user creation | Request authorization (must match path or body tenantId) |
| `custom:role` | Set by admin at user creation | Future role-based authz (not enforced in v1) |
| `sub` | Cognito-issued | Session correlation |
| `iss` | Cognito-issued | Validated by authorizer against the pool issuer URL |

### Sign-in flow (client side)

1. Client knows its tenant (e.g., configured at app install time)
2. Client reads pool ID + client ID from bundled config for that tenant
3. Client performs `InitiateAuth` with `USER_SRP_AUTH` (SRP) or `USER_PASSWORD_AUTH` (direct password)
4. Cognito returns `IdToken`, `AccessToken`, `RefreshToken`
5. Client uses `IdToken` in the `Authorization: Bearer <jwt>` header for REST, or as `?token=<jwt>` query parameter for WebSocket connect

### Token refresh

Client refreshes before expiry using the refresh token. v1 uses default Cognito lifetimes (ID: 60 min, Refresh: 30 days).

---

## 2. REST endpoints

### `POST /triage`

Invokes the deterministic P0–P3 classifier for a given vehicle.

**Request:**

```
POST /triage
Authorization: Bearer <jwt>
Content-Type: application/json

{
  "vin": "1FMCU0F70MUA12345",
  "sessionId": "vsa-sess-optional",
  "tenantId": "michelin",                // optional; must match JWT tenantId if present
  "extra": { "fleet_sla": "tier-1" }     // optional; passed to tenant rule expressions
}
```

**Response — 200:**

```json
{
  "statusCode": 200,
  "classification": "P1",
  "sessionId": "vsa-sess-a1b2c3",
  "decidedAt": "2026-04-27T14:22:19.312Z",
  "latencyMs": 642
}
```

**Response — 400:** invalid JSON or missing `vin`.
**Response — 401:** missing or invalid JWT, or missing `custom:tenantId` claim.
**Response — 403:** request body `tenantId` does not match JWT claim.
**Response — 500:** classifier Lambda failure.

### `GET /tenants/{tenantId}/config`

Returns the public subset of tenant config for the calling tenant. `{tenantId}` in the path must match the caller's JWT tenantId.

**Request:**

```
GET /tenants/michelin/config
Authorization: Bearer <jwt>
```

**Response — 200:**

```json
{
  "tenantId": "michelin",
  "version": "1.0.0",
  "status": "active",
  "displayName": "Michelin Fleet Services",
  "segment": "fleet",
  "branding": {
    "logoUrl": "https://assets.vsa.example.com/michelin/logo.svg",
    "primaryColor": "#1A3CA8",
    "secondaryColor": "#F4D35E",
    "fontFamily": "'Univers', 'Helvetica Neue', sans-serif",
    "mapStyle": "mapbox://styles/vsa/fleet-dark",
    "greeting": {
      "voice": "Michelin Fleet Support...",
      "chat":  "Hi — I'm Michelin Fleet Support...",
      "app":   "Live support for your fleet..."
    }
  },
  "voice": {
    "engine": "nova-2-sonic",
    "voiceId": "matthew",
    "locale": "en-US"
  },
  "features": {
    "imageInput": false,
    "agentAssistCoPilot": false,
    "calendarSlotProposal": false,
    "dmsBooking": false
  }
}
```

Internal fields (ARNs, queue IDs, secrets, triage policy thresholds) are **not** returned.

**Response — 403:** path `tenantId` does not match JWT claim.
**Response — 404:** tenant not found or no active version.

---

## 3. WebSocket

### Connect

```
wss://{wsApiId}.execute-api.us-east-1.amazonaws.com/prod?token=<jwt>
```

- The Lambda authorizer validates the JWT against all configured tenant pools.
- On success, the connection is recorded in `vsa-prod-ws-connections` with the resolved `tenantId`.
- On failure, the connection is rejected with a 401.

### Server → client messages (v1)

**None yet.** The WebSocket is scaffolded but no producer is live. Future producers include:

- Mock telemetry publisher (short-term, before CMS us-east-1)
- CMS telemetry bridge (once Redis is reachable)
- Reasoning drawer stream (classifier + tool call events during a triage session)

### Client → server messages (v1)

**None required.** The WebSocket is pure server-push in the design. Any client message is acknowledged with `{"ack": true}` but otherwise ignored.

### Disconnect

The $disconnect handler removes the row from the connection table. TTL also cleans up stale rows.

---

## 4. CORS

- All REST endpoints respond to `OPTIONS` preflight with `*` origin.
- 4XX and 5XX responses carry CORS headers (critical — without these, browsers mask API errors as opaque CORS failures).
- WebSocket has no CORS concept (upgrade handshake).

---

## 5. Rate limits

Default throttling on REST API:
- 100 req/s steady, 50 burst per API (across all methods)

Per-tenant budgets are enforced in the application layer (tenant config `policies.budgets`). Exceeding the per-session token cap returns 200 with a degraded response, not an error.

---

## 6. Error model

Every error response shape:

```json
{ "error": "human-readable message", "detail": "optional context" }
```

Status codes:

| Code | Meaning |
|---|---|
| 400 | Malformed request body |
| 401 | Missing or invalid authentication |
| 403 | Authenticated but not authorized for this tenant |
| 404 | Resource not found |
| 429 | Rate-limited (from API Gateway default throttle) |
| 500 | Unexpected server error (check CloudWatch logs) |

---

## 7. Versioning

REST API paths are unversioned in v1 (`/triage`, not `/v1/triage`) to keep the contract minimal. Breaking changes will introduce `/v2/*` paths and retain `/v1/*` for a deprecation window. Non-breaking additions (new optional fields, new optional endpoints) do not bump the prefix.

---

## 8. SDK generation

No SDK generated in v1. iOS, web, and any other clients write thin type-safe wrappers against this doc. If we end up with three independent clients, we revisit OpenAPI → SDK generation.

---

## 9. What's *not* in v1 (documented here so no one expects it)

- No refresh-token endpoint (clients refresh against Cognito directly)
- No self-signup
- No password reset flow (admin resets via `admin-set-user-password`)
- No cross-tenant endpoints
- No `/bookings`, `/warranty`, `/recalls` — those come in Phase 4
- No `/agent/invoke` — that comes in Phase 3
- No device registration for push notifications

---

## 10. Quick-start for a new client

```bash
# 1. Read config for your tenant
curl -H "Authorization: Bearer $JWT" \
  https://.../prod/tenants/michelin/config

# 2. Call triage
curl -X POST -H "Authorization: Bearer $JWT" -H "Content-Type: application/json" \
  -d '{"vin":"1FMCU0F70MUA12345"}' \
  https://.../prod/triage

# 3. Open a WebSocket
wscat -c "wss://.../prod?token=$JWT"
```
