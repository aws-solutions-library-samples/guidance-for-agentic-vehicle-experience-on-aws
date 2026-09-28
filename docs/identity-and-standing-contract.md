# Identity and Standing Contract — CVX / CMS / DMS / ADP

**Canonical definition of who a principal is, and when they may act on a vehicle.**
This document is the contract derived from the identity-model investigation
recorded in the project's issue tracker; every figure in that record is a live
read.

CVX owns this vocabulary because CVX is where the authorization decision is made. DMS and ADP
reference it from their own `docs/cvx-integration-contract.md`; CMS is the authoritative source
of the vehicle-to-party facts it depends on.

Contract version: **1.0.0**

---

## 1. Why this document exists

Four systems held four different ideas of "customer" and none of them said so. Two concrete
consequences, both measured:

* **CMS/ADP and DMS use the same prefix for different namespaces.** CMS `sold_to` and ADP
  `customer_360.customer_id` are `CUST-` + 8 hex (99/99 and verified against the 5M-row table).
  DMS `repair-orders.customer_id` and `deals.customer_id` are `CUST-` + 6 digits (25/25). A join
  between them returns **zero rows with no error** — it reads as "this customer has no repair
  orders".
* **The authorization layer had no representation of any of it.** Cognito `sub` is a UUID with no
  link to a vehicle, and no pool carries a customer attribute. So a check written as
  `claims["sub"] == finding["owner_id"]` could never be true.

## 2. Namespaces

Five, none interchangeable. **Never compare across rows of this table.**

| Concept | Format | Authoritative home | Mirrors |
|---|---|---|---|
| **Principal** | Cognito `sub`, UUID | the pool the caller authenticated against | — |
| **Party** (customer / owner) | `CUST-` + 8 hex, e.g. `CUST-004000DD` | CMS `storage-vehicles.sold_to` | ADP `customer_360.customer_id` (same population — verified) |
| **Driver** | `DRV-*`, e.g. `DRV-MRDN-0015` | Cognito `custom:driverId` | — |
| **Fleet** | `FLEET-*` or slug, e.g. `FLEET-MERIDIAN-ONBOARD`, `oem1-staging-fleet` | CMS `storage-vehicles.fleetId` | Cognito `custom:fleetIds` (CSV) |
| **Vehicle** | `VEH-*` + VIN | CMS `storage-vehicles.vehicleId` / `vin` | Cognito `custom:vehicleId` |

**A principal is not a party.** There is no field anywhere stating that driver `DRV-X` *is* party
`CUST-Y`. The only connection is *through a vehicle*, and it is not an identity claim — see § 4.

### 2.1 DMS is currently out of contract

DMS's `customer_id` is `CUST-` + 6 digits and resolves to **no customer table** (`dynamodb
list-tables` shows none in DMS). It is a dangling reference in a namespace that collides visually
with the party namespace.

DMS MUST do one of:

* **(a)** migrate to the party namespace (`CUST-` + 8 hex, sourced from CMS `sold_to`), or
* **(b)** rename its identifier so the collision is visible — `DMS-CUST-*` or `dms_customer_id`
  — and publish a mapping table to the party namespace.

Until one lands, **any code joining DMS `customer_id` to CMS or ADP is incorrect** even when it
runs clean. Applies today to `dms_copilot` and the AVX pre-brief path.

## 3. Standing — the thing authorization actually keys on

> **A principal has *standing* over a vehicle. Standing is a SET, not a scalar.**

This is the core of the contract. Asking "is this caller the customer?" is the wrong question and
produced two defects (the owner path and the fleet path, independently). The right question is
**"does this caller have standing over this vehicle, and does that standing carry the right being
exercised?"**

| Standing | Meaning | Principal-side claim | Vehicle-side fact | Status |
|---|---|---|---|---|
| `owner` | party of record for the vehicle | `custom:customerId` | `owner_id` | claim **not yet provisioned** — see § 5 |
| `fleet_manager` | manages the fleet the vehicle is in | `fleet-operator` group **and** `custom:fleetIds` ∋ | `fleet_id` | claim exists and is populated |
| `fleet_driver` | assigned to operate this vehicle | `custom:vehicleId` | `vehicle_id` / `vin` | claim exists and is populated |

A principal may hold **all three** over the same vehicle — that is the normal case for a retail
owner who drives their own car, and it is why standing is a set. Measured in staging:
`meridian.driver@example.com` is `DRV-MRDN-0015`, drives `VEH-MRDN-0015`, which is owned by
`CUST-004000DD` and sits in `FLEET-MERIDIAN-ONBOARD`.

`fleet_manager` is the one standing that also needs a group (spec
`2026-09-25-avx-own-vehicle-findings`, 2026-09-26): a driver's token can carry
`custom:fleetIds`, and a driver sees only the assigned vehicle. Every read route and the approve
route apply the same `fleet-operator` gate, so no caller can approve a Finding that no read route
returns to it.

### 3.1 Standings are not ranked, and they do not imply each other

Each is granted independently and carries different rights. In particular:

* **`fleet_driver` MUST NOT confer `owner`.** The chain `sub` → `custom:vehicleId` → vehicle →
  `sold_to` reaches a party id, so deriving owner standing from the driven vehicle is *possible*
  and is **forbidden**: it would hand a fleet employee the commercial rights of the company that
  owns the vehicle. Driving is not owning.
* **`owner` does not confer `fleet_driver`.** A party owning 17 vehicles (the measured staging
  case) is not thereby the assigned operator of any of them.

### 3.2 Rights by standing

Operational rights act on the vehicle's condition. Commercial rights act on money or title.

| Right | `owner` | `fleet_manager` | `fleet_driver` |
|---|---|---|---|
| read findings for the vehicle | yes | yes (own fleet) | yes (own vehicle) |
| approve an operational action (book service, order a part) | yes | yes | **yes** |
| approve a commercial action (accept an offer, initiate payment, transfer title) | yes | fleet policy | **no** |

`fleet_driver` holding the operational approve is deliberate and is the primary AVX use case: a
driver acting on their own vehicle's tire warning must not need the fleet owner in the loop.

### 3.3 `GET /findings/me` — the caller asks for its own, and the server decides what that means

Every other findings route names a scope in its path (`/findings/vehicle/{vin}`,
`/findings/owner/{owner_id}`, `/findings/fleet/{fleet_id}`), so the caller states the scope and the
server checks the caller is entitled to it. `GET /findings/me` inverts that: the caller names no
scope, and the server derives every scope the caller holds from its verified claims, reads each, and
merges the result.

This exists because a driver has no scope identifier to put in a path. Their own `sub` is not the
owner id of the vehicle they drive, and asking the client to discover a VIN first is what produced
the defect this route replaced — the iOS Alerts tab read `/findings/owner/{sub}`, which refuses any
id but the caller's own, so a driver saw nothing.

What it means for standing:

- **No standing is a successful, empty answer**, not a 403: `200 {"findings": [], "standings": []}`.
  A caller with no vehicle, no fleet and no dealership is not making an illegitimate request; they
  simply have nothing. A 403 would tell a probing caller that some scope exists and was refused.
- **`standings` names what the server used.** The client does not infer it. A driver sees
  `["vehicle"]`; an operator with both may see `["fleet", "vehicle"]`.
- **The scopes are derived, never supplied.** The route reads no scope from the query string or the
  body. A driver's vehicle comes from `custom:vehicleId` and, for the owner scope, from the CMS
  registry — both of which the principal cannot write (§ 5.1).
- **Expired Findings are hidden**, so a card for a fault that has since cleared does not reappear.
- **One driver, one vehicle, by decision.** A driver identity reads Findings for the vehicle
  assigned to it and for no other, including other vehicles sold to the same customer. The iOS app
  is 1:1 with a vehicle and has no multi-vehicle view.

`/findings/me` grants no standing that the named routes would not. It is a convenience over the same
checks, which is why it is documented here rather than as a new right in § 3.2.

## 4. Party of record: customer and owner are one field today, and will separate

CMS carries exactly one party field, `sold_to`, which literally means *sold to* — the
**purchaser**. There is no title field. So today:

```
customer (commercial party)  ==  owner (title holder)  ==  sold_to
```

They diverge in every commercially interesting case, and two already exist in staging data
(`FLEET-TESLA-OFFBOARD`, `WPR-RENTAL`):

| Case | Purchaser | Title holder | Operator |
|---|---|---|---|
| retail purchase | the individual | the individual | the individual |
| **lease** | the lessee | the finance arm | the lessee |
| **fleet** | the company | the company | an employee |
| **rental** | the rental firm | the rental firm | a transient customer |
| **resale** | stale — names the previous party | the new owner | the new owner |

**Decision (2026-09-23): the distinction is introduced in the vocabulary now, not when it
bites.** Consumers write against `owner_id` meaning "party of record", and the contract commits
to a `customer_id` attribute joining it when CMS can distinguish title from purchase. Because the
*concept* is named from the start, that later split is a data change, not a contract break.

Do NOT ship a second projected attribute duplicating `owner_id` today — a column whose value is
by construction identical to another teaches readers the two are interchangeable, which is the
belief this section exists to prevent.

### 4.1 `owner_id` is the party of record, and its provenance is `sold_to`

The projected attribute is named `owner_id` (a deployed GSI is keyed on it; renaming would break
the read surface). Its definition in this contract is **"the party of record for this vehicle"**,
currently sourced from `sold_to`. When CMS adds a title field, `owner_id` re-points to it and
`customer_id` takes over the purchaser role. Both names are reserved now.

## 5. Principal-side claims

The authorizer trusts two pools: **`cms-staging-ui-users`** (the CMS pool, managed by CMS) and
**`vsa-staging-meridian-fleet`** (the CVX fleet pool, managed by CVX). Both pools' clients
contribute to the self-writable risk analysis: a claim is safe to authorize on only if it is
excluded from EVERY trusted pool's app-client `WriteAttributes`.

### CMS pool (`cms-staging-ui-users`)

| Claim | Namespace | Populated | Self-writable risk |
|---|---|---|---|
| `sub` | UUID | always | no — issued by Cognito |
| `custom:customerId` | party | **provisioning-pending** (T4.1: CMS pool attribute added; T4.3: `assign_standing_claims.py --apply` seeds values) | no — `WriteAttributes` on the CMS pool app client is `["email","name"]` today, so every `custom:*` attribute is already excluded from client writes (live-verified 2026-09-24) |
| `custom:driverId` | driver | yes | no — excluded from client writes by the same `WriteAttributes` setting (live-verified 2026-09-24) |
| `custom:vehicleId` | vehicle | yes | no — excluded from client writes by the same `WriteAttributes` setting (live-verified 2026-09-24) |
| `custom:fleetIds` | fleet, CSV | yes | no — excluded from client writes by the same `WriteAttributes` setting (live-verified 2026-09-24) |

### CVX fleet pool (`vsa-staging-meridian-fleet`)

| Claim | Namespace | Populated | Self-writable risk |
|---|---|---|---|
| `sub` | UUID | always | no |
| `custom:vehicleId` | vehicle | yes | no — the attribute is `Mutable: true` (for backfill), but both app clients (`…-app`, `…-kiosk`) set `WriteAttributes = ["email","name"]` since T3.4 deployed (clients `UPDATE_COMPLETE` 2026-09-24 18:14Z; live-verified by `scripts/check-standing-claim-pools.sh`, § 8). Before T3.4 both clients left `WriteAttributes` unset, which made the claim self-writable; vehicle-id trust stayed `false` until that was closed. |

This pool has no `custom:fleetIds` or `custom:customerId` attribute, so those attestations are unaffected by this pool.

**`email` MUST NOT be a standing key.** Under CMS's default Cognito pool settings, a
signed-in user can rewrite their own `email` unverified; the rewritten value becomes
the token claim and sign-in identity. No AVX authorization
path may key on `email`.

### 5.1 Every `custom:*` claim used for authorization needs WriteAttributes exclusion across ALL trusted pools

A `custom:*` attribute is **self-writable under the app client's default (unset)
`WriteAttributes`**. Verified live on the CMS pool 2026-08-03 against `custom:role`:
`update-user-attributes` with the caller's *own* access token succeeded and the new value appeared
in the next ID token, escalating a consumer's persona. Recorded at
`lambdas/api-assistant-chat/tests/test_persona_map.py::test_unknown_role_does_not_escalate_to_fleet_driver`.

So for **every** claim in the table above that grants standing:

1. The attribute MUST be excluded from **every trusted pool's** app-client `WriteAttributes`. It must stay
   `mutable=True` for backfill, and mutable-plus-default-WriteAttributes **is** the vulnerable
   state — so immutability is not the control.
2. **The owning repo for each pool MUST hold a guard test** asserting the exclusion for that pool.
3. **An attestation (`AVX_*_CLAIM_TRUSTED=true`) is valid ONLY if ALL trusted pools satisfy (1) and (2).**
   A single pool leaving the claim self-writable makes the attestation unsafe regardless of the other pools.

CVX enforces (3) as a fail-closed opt-in rather than a comment: `AVX_CUSTOMER_ID_CLAIM_TRUSTED`
must be exactly `"true"`, and setting it is the operator's attestation that (1) and (2) hold
for **every pool the authorizer trusts**. Default-off means the dangerous state is unreachable by omission.

**This requirement applies to `custom:vehicleId` and `custom:fleetIds` too.** They are already
populated and already load-bearing for fleet standing, which means the exposure exists **now**,
not when `custom:customerId` arrives. Tracked obligations:

- **CMS pool** `custom:fleetIds`, `custom:vehicleId`: excluded by `WriteAttributes = ["email","name"]` (live-verified 2026-09-24).
- **CVX fleet pool** `custom:vehicleId`: excluded by `WriteAttributes = ["email","name"]` on both clients since T3.4 deployed (2026-09-24 18:14Z). Guard: `infrastructure/test/tenant-pool-client-write-attributes.test.ts`.
- **CMS pool** `custom:customerId`: declared since T4.1 deployed (2026-09-24 19:36-19:41Z), and excluded by the same `WriteAttributes` setting. Guards: CMS `deployment/stacks/test_standing_claims_pool.py` (declaration) and `deployment/stacks/test_client_write_attributes.py` (exclusion).

### 5.2 Tier A SSM attestation parameters

The three CVX trust flags are seeded by the operator after confirming the WriteAttributes
exclusion (§ 5.1) and the guard test hold **for every trusted pool**:

| Parameter | SSM path | Env-var override | Reaches | Seed condition |
|---|---|---|---|---|
| `avxFleetIdsClaimTrusted` | `/cvx/{stage}/avx-fleet-ids-claim-trusted` | `CVX_AVX_FLEET_IDS_CLAIM_TRUSTED` | findings Lambda AND actions Lambda | After verifying § 5.1 for all pools (fleet pool has no `fleetIds` attribute — no risk) |
| `avxCustomerIdClaimTrusted` | `/cvx/{stage}/avx-customer-id-claim-trusted` | `CVX_AVX_CUSTOMER_ID_CLAIM_TRUSTED` | findings Lambda ONLY | After T4.1 deploys (CMS pool attribute and guard). Seed `true` only after T4.1 deploys. Fleet pool has no `customerId` attribute — no risk from that pool. |
| `avxVehicleIdClaimTrusted` | `/cvx/{stage}/avx-vehicle-id-claim-trusted` | `CVX_AVX_VEHICLE_ID_CLAIM_TRUSTED` | findings Lambda ONLY | After T4.1 AND after T3.4 deploys (T3.4 restricts `vsa-staging-meridian-fleet` pool's `WriteAttributes` so `custom:vehicleId` is no longer self-writable there). Seed `true` only after both prerequisites are verified. |

See `scripts/seed-stage-parameters.sh` for the seeding command and `docs/DEPLOYMENT.md` for
the current parameter count.

## 6. Carrying standing on an artifact

A Finding or Action is scoped to a subject. Consumers MUST NOT re-resolve standing per request
against CMS — that puts a cross-module read on the authorization hot path. Instead the producer
**projects the vehicle's standing-relevant facts onto the persisted item**, and the authorizer
intersects them against the caller's claims.

Projected attributes on an AVX Finding item (storage layer only — never on the schema-validated
artifact, never in `inputs_hash`):

| Attribute | Source | Enables |
|---|---|---|
| `vin` | the Finding's scope | `fleet_driver` standing |
| `owner_id` | CMS `sold_to` | `owner` standing |
| `fleet_id` | CMS `fleetId` | `fleet_manager` standing |

All three come from the **same CMS vehicle row**, so resolving them is one lookup, not three.

Fail-closed rules, non-negotiable:

* An unresolvable fact is **omitted**, never written as `""`, `None`, or the VIN. Absent means
  "no standing provable by this route", which must deny.
* A projection that becomes stale (the registry row changed) MUST be **removed** on the next
  write, not left in place. A stale projection authorizes a party the registry no longer names.
* Absence of all three MUST deny. "We could not determine standing" is not "anyone may act".

## 7. Obligations by repo

| Repo | Obligation | Status (2026-09-24) |
|---|---|---|
| **CMS** | Authoritative for `sold_to`, `fleetId`, `vehicleId`, `vin`. Provision `custom:customerId`; exclude it **and** `custom:vehicleId` **and** `custom:fleetIds` from every app client's `WriteAttributes`; hold a guard test per § 5.1. Longer term: a title field distinct from `sold_to` (§ 4). | Done for staging. `WriteAttributes` is `["email","name"]`, so all three claims are excluded from client writes. T4.1 deployed 2026-09-24 19:36-19:41Z: `custom:customerId` declared (mutable), `vehicle-owner` and `fleet-driver` groups exist, and `AttributesRequireVerificationBeforeUpdate = [email]` (live-verified 2026-09-24). Guards: `test_standing_claims_pool.py`, `test_client_write_attributes.py`. |
| **CVX** | Owns this contract. Resolves standing facts from CMS at write time and projects them per § 6. Authorizes by standing intersection, fail-closed, per § 3. Trusts `custom:customerId` only behind the § 5.1 opt-in. | All three Tier A SSM parameters wired to the findings Lambda; `AVX_FLEET_IDS_CLAIM_TRUSTED` also wired to the actions Lambda (T3.1 + F1.4). **Live attestation state (staging, 2026-09-24): all three are `true`.** `avx-fleet-ids-claim-trusted` since 10:07 EDT. `avx-customer-id-claim-trusted` and `avx-vehicle-id-claim-trusted` since 15:43 EDT (T4.3), after `check-standing-claim-pools.sh` passed for every trusted pool. The findings Lambda has carried `true` for all three since the `vsa-staging-api` deploy at 23:54Z; the actions Lambda carries the fleet flag only. |
| **DMS** | Resolve the namespace collision per § 2.1 before any join to CMS/ADP party ids. Adopt the § 3 standing vocabulary for its own dealer-side authorization. | Open. |
| **ADP** | `customer_360.customer_id` is in the party namespace and is a **mirror, not a source** — CMS is authoritative. `primary_vin` is NOT an ownership bridge: it is per-customer and non-unique (43.7% of VINs map to 2+ customers), so it MUST NOT be used to resolve a vehicle's party. | No change required from this spec. |

## 8. Verification

```bash
# Party namespace is 8-hex in CMS (expect: 0 violations)
aws dynamodb scan --table-name cms-staging-storage-vehicles --region us-west-2 \
  --filter-expression "attribute_exists(sold_to)" --projection-expression "sold_to" \
  --query 'Items[].sold_to.S' --output text | tr '\t' '\n' \
  | grep -vE '^(None)?$' | grep -cvE '^CUST-[0-9A-F]{8}$'
# Expect: 0

# DMS is still out of contract until this returns 0.
aws dynamodb scan --table-name dms-staging-repair-orders --region us-west-2 \
  --projection-expression "customer_id" --max-items 25 \
  --query 'Items[].customer_id.S' --output text | tr '\t' '\n' \
  | grep -vE '^(None)?$' | grep -cvE '^CUST-[0-9A-F]{8}$'
# Currently: 25 of 25 non-null values are 6-digit. Target: 0.

# custom:customerId schema attribute declared in pool (after T4.1)
aws cognito-idp describe-user-pool --user-pool-id <cms-staging-user-pool-id> --region us-west-2 \
  --query 'UserPool.SchemaAttributes[?Name==`custom:customerId`].Name' --output text
# Before T4.1: empty. After T4.1: custom:customerId

# vehicle-owner and fleet-driver groups exist in pool (after T4.1)
aws cognito-idp list-groups --user-pool-id <cms-staging-user-pool-id> --region us-west-2 \
  --query 'Groups[?GroupName==`vehicle-owner`||GroupName==`fleet-driver`].GroupName' --output text
# Before T4.1: empty. After T4.1: fleet-driver  vehicle-owner

# AttributesRequireVerificationBeforeUpdate contains email (after T4.1)
aws cognito-idp describe-user-pool --user-pool-id <cms-staging-user-pool-id> --region us-west-2 \
  --query 'UserPool.UserAttributeUpdateSettings.AttributesRequireVerificationBeforeUpdate' --output text
# Before T4.1: empty output (the API returns [] which the CLI renders as nothing). After T4.1: email

# No trusted pool lets a client write a standing claim. The script derives the pool set from
# the deployed API authorizer(s), so it cannot be scoped too narrowly. It prints
# OK / VIOLATION / ERROR per app client and EXITS 1 on any VIOLATION or ERROR, including
# "no pools found" and "a lookup failed". This is the pre-flight for every attestation flip.
# (The CLI prints JSON null when WriteAttributes is absent, which means all attributes are
# writable. The script reports that as VIOLATION, not a bare `None`.)
bash scripts/check-standing-claim-pools.sh --stage staging
# Run 2026-09-24, after T3.4's client change was live (IDs elided):
#   === pool <cms-staging-user-pool-id> (cms-staging-ui-users) ===
#     client <client-id>: OK (WriteAttributes=["email","name"])
#   === pool <cvx-staging-user-pool-id> (vsa-staging-meridian-fleet) ===
#     client <client-id>: OK (WriteAttributes=["email","name"])
#     client <client-id>: OK (WriteAttributes=["email","name"])
#   RESULT: PASS                                                  (exit 0)

# Live attestation state — iterate over the three Tier A parameters:
for P in avx-fleet-ids-claim-trusted avx-customer-id-claim-trusted avx-vehicle-id-claim-trusted; do
  printf "  /cvx/staging/$P = "
  aws ssm get-parameter --name "/cvx/staging/$P" --region us-west-2 \
    --query 'Parameter.Value' --output text 2>/dev/null || echo "(not found)"
done
# Expect (staging, since T4.3 on 2026-09-24): all three = true
```

The first three checks were executed 2026-09-23 and returned `0`, `25`, and empty respectively.
The T4.1 checks (customerId attribute, groups, UpdateSettings) were re-run 2026-09-24 after T4.1
deployed and returned `custom:customerId`, `vehicle-owner  fleet-driver`, and `email`.
`check-standing-claim-pools.sh` was re-run 2026-09-24 after review Cycle 5 W1 (`--max-results`
removed, so every page of app clients is checked) and printed the output above: 3 of 3 clients
`OK`, `RESULT: PASS`, exit 0.
The live attestation check returns `true` for all three (verified 2026-09-24).

## 9. Cross-references

* `lambdas/api-avx-findings/handler.py` — `_caller_customer_id`, the § 5.1 opt-in
* `agents/tier2/owner_resolver.py` — the CMS standing-fact lookup
* `lambdas/api-assistant-chat/tests/test_persona_map.py` — precedent for a
  self-writable `custom:*` claim on a shared pool
* `docs/cvx-integration-contract.md` in DMS and ADP — should reference this document
