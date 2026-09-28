# Persona Mapping: CMS Claims to AVX Personas

This document describes how CMS Cognito claims map to AVX agent personas in the web chat integration (`POST /assistant/chat`).

## Mapping Table

| CMS Signal | Condition | AVX Persona |
|---|---|---|
| `cognito:groups` or `custom:role` | `platform-admin` or `fleet-operator` | `fleet_driver` |
| `custom:role` | `service-advisor` | `service_advisor` |
| Anything else or missing | (default fallback) | `fleet_driver` |

**Default persona:** `fleet_driver`

## Source of Truth

The canonical persona mapping logic lives in **one location only**:

- **`lambdas/api-assistant-chat/persona_map.py`** — the `map_persona_from_claims()` function and its unit tests (`tests/test_persona_map.py`) define the authoritative mapping.

This single source ensures consistency across routes and prevents persona-bleed across tenants or role boundaries.

## Valid Personas

AVX supports six personas, defined in `agents/supervisor/persona_definitions.py`:

- `fleet_driver` — default; fleet operations, vehicle monitoring
- `consumer` — rental/consumer vehicle ownership
- `retail_advisor` — dealership sales and service
- `vehicle_owner` — private owner context
- `service_advisor` — fleet service and warranty (KB-grounded)
- `technician` — technical diagnostics (KB-grounded)

In v1, only personas reachable from CMS roles are mapped in the web chat (above table). The full set remains available to iOS callers.

## Persona Inference Flow

1. Browser sends CMS bearer token (IdToken) to `POST {vsaApiEndpoint}/assistant/chat`
2. API Gateway Cognito authorizer validates the token and injects claims into the route Lambda's context
3. Route Lambda reads `sub`, `custom:tenantId`, `cognito:groups`/`custom:role` from the authorizer context
4. Route Lambda calls `map_persona_from_claims()` to infer the persona
5. Route Lambda passes `personaId` (validated) to the AVX runtime
6. AVX entrypoint accepts the optional `personaId` in the payload, validates it, defaults to `fleet_driver`

## Security Notes

- The persona is inferred **server-side** on the route Lambda — not sent by the client, preventing privilege escalation.
- Invalid or absent `personaId` always defaults to the most-constrained persona (`fleet_driver`).
- CMS pool trust is already in place via `EXTRA_USER_POOL_IDS` in the `vsa-api` Cognito authorizer (for the health-score widget + now the chat route).

## Testing

Unit tests in `tests/test_persona_map.py` verify:
- `platform-admin` and `fleet-operator` both map to `fleet_driver`
- `service-advisor` maps to `service_advisor`
- Unknown/missing roles default to `fleet_driver`
- Never raise on invalid input
