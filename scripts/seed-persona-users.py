"""Seed Cognito users + CMS driver/vehicle rows for OEM, Rental, and expansion personas.

Pairs with seed-personas.py (which adds tenant configs). After this
runs, the iOS sign-in flow can authenticate as any persona and the
backend's /drivers/me endpoint resolves them to a real assigned vehicle.

What this seeds (idempotent — safe to re-run):

  meridian.driver@example.com
    Cognito user (pool <tenant-pool-id>, password from $DEMO_PASSWORD, force-confirmed)
    DRV-MRDN-0015 — assignedVehicleId=VEH-MRDN-0015
    VEH-MRDN-0015 — Halcyon F-150 in Atlanta, GA

  priya.shah@example.com
    Cognito user (same pool, same password, force-confirmed)
    DRV-WPR-001 — assignedVehicleId=VEH-WPR-001
    VEH-WPR-001 — Meridian Azimuth in Chicago, IL

  vehicle.owner@example.com  (expansion — vehicle_owner persona)
    DRV-VO-001 — assignedVehicleId=VEH-VO-001
    VEH-VO-001 — Meridian Crestwind in Austin, TX (vin MRDCW01H8PC00V7RD)

  service.advisor@example.com  (expansion — service_advisor persona)
    DRV-SA-001 — no assigned vehicle (advisor, not a driver)

  technician@example.com  (expansion — technician persona)
    DRV-TECH-001 — no assigned vehicle (technician, not a driver)

Also re-asserts cognitoEmail on the existing samantha.carter@example.com
driver row so the existing fleet persona keeps working without manual
DDB editing.

NOTE (2026-07-28): this script no longer writes `custom:persona_id`.
Neither staging pool declares that schema attribute, and nothing in either
repo reads it — persona routing goes through
`lambdas/api-assistant-chat/persona_map.py::infer_persona`, which uses
`cognito:groups` and `custom:role`. See the comment in
`_ensure_cognito_user` for the full rationale and how to reinstate it
safely if a real consumer ever appears.

Defaults are STAGING-SAFE. `STAGE` defaults to `staging` and `AWS_REGION`
is derived from the stage when unset, so a bare invocation can never
touch production. Seeding prod additionally requires an explicit
confirmation env var (see below) — this script force-sets passwords on
real Cognito users and writes driver/vehicle rows, so an accidental prod
run is destructive.

Usage:
    # staging (default — nothing extra needed)
    python scripts/seed-persona-users.py

    # staging, explicit
    STAGE=staging AWS_REGION=us-west-2 python scripts/seed-persona-users.py

    # override the target pool (e.g. the consolidated CMS UI pool that the
    # iOS app authenticates against, rather than the CVX tenant pool)
    USER_POOL_ID=<user-pool-id> python scripts/seed-persona-users.py

    # production — requires the confirmation gate
    STAGE=prod CONFIRM_PROD_SEED=yes python scripts/seed-persona-users.py
"""
from __future__ import annotations

import os
import secrets
import string
import sys
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Environment resolution — staging-safe defaults
# ---------------------------------------------------------------------------
# Prior to 2026-07-28 these defaulted to REGION=us-east-1 / STAGE=prod, so a
# bare `python scripts/seed-persona-users.py` aimed at PRODUCTION tables and
# production Cognito. That only ever failed safe by accident (the us-east-1
# pool lookup missed). Since this script force-sets user passwords and writes
# driver/vehicle rows, the default is now staging and prod is explicitly gated.
STAGE = (os.environ.get("STAGE") or "").strip() or "staging"
CMS_STAGE = (os.environ.get("CMS_STAGE") or "").strip() or STAGE

# Region defaults per stage rather than to a single hardcoded value, so the
# region can never silently disagree with the stage.
_STAGE_DEFAULT_REGION = {"staging": "us-west-2", "prod": "us-east-1"}
REGION = (os.environ.get("AWS_REGION") or "").strip() or _STAGE_DEFAULT_REGION.get(STAGE, "us-west-2")

def _random_pw() -> str:
    """Generate a strong per-run password.

    Mirrors `scripts/seed-meridian-fleet-user.py::_random_pw` — same alphabet, same
    complexity check — so both seed paths behave consistently.
    """
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(20))
        if (
            any(c.islower() for c in pw)
            and any(c.isupper() for c in pw)
            and any(c.isdigit() for c in pw)
        ):
            return pw


# Password used for every seeded persona.
#
# There is deliberately NO hardcoded fallback. Until 2026-08-03 this line read
# a hardcoded literal fallback, which meant every run that did not set DEMO_PASSWORD
# force-set the same well-known password on every persona in whichever pool was
# targeted — including production, per
# issues/2026-07-28-unauthorized-prod-persona-seed/. That literal was also
# published in agents/supervisor/README.md and docs/DEMO-RUNBOOK.md, so it was a
# shared credential documented in the repo.
#
# Note that entropy-based scanners do NOT catch a value like that: verified
# 2026-08-03 that `gitleaks` 8.30.1 reports zero findings for it across both the
# working tree and all 127 commits of history. A short, low-entropy, memorable
# password is invisible to the tooling — which is exactly why it must not exist
# in source in the first place.
#
# Behaviour: honour DEMO_PASSWORD when the operator sets it (e.g. sourced from
# Secrets Manager); otherwise generate a fresh one per run and print it once at
# the end so the operator can use it. Either way, nothing shared is baked in.
# See issues/2026-08-03-cvx-demo-password-rotation/report.md.
_DEMO_PASSWORD_FROM_ENV = bool((os.environ.get("DEMO_PASSWORD") or "").strip())
DEMO_PASSWORD = (os.environ.get("DEMO_PASSWORD") or "").strip() or _random_pw()
DRIVERS_TABLE = f"cms-{CMS_STAGE}-storage-drivers"
VEHICLES_TABLE = f"cms-{CMS_STAGE}-storage-vehicles"

# ---------------------------------------------------------------------------
# CROSS-REPO WRITE — this script is a writer of record for CMS's tables.
#
# The two names above are CMS-owned tables in the CMS account. This script
# therefore mutates another repo's data plane, and CMS's guards cannot see it:
# `connected-mobility-guidance-on-aws/deployment/stacks/tests/
# test_no_retired_vehicle_ids.py` scans the CMS repo only, so a retired
# identifier left HERE is invisible to it while still being able to resurrect
# the row over there.
#
# That is not hypothetical. On 2026-09-22 CMS renamed `VEH-FORD-001` ->
# `VEH-MRDN-0015` across 13 tables and verified every surface clean; ~25 minutes
# later a full vehicle row reappeared under the old id, along with the driver
# row, both carrying the same microsecond timestamp. It took a cross-repo search
# to attribute: this file, whose vehicle dict still held the old id, the
# Ford-derived VIN and `model: Trailwind`. Nothing in CMS could have produced it.
#
# So: when an identifier or a vehicle attribute is retired on the CMS side, it
# must be retired HERE in the same change, or the next `make seed-staging`
# silently reverts it. The same trap is already documented for this persona's
# EV conversion at the `vehicle` dict below ("the live row was converted ... but
# this seed was never brought along").
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Vehicle model manifest constants
#
# Every persona vehicle written to `cms-{stage}-storage-vehicles` MUST carry
# `modelManifestName` and its derived fields.  This satisfies the CMS-side
# lint at `modules/cms_ui/source/handlers/main_api/
# test_seed_scripts_vehicle_model_required.py`, added by spec
# `2026-08-28-cms-cert-follows-model` § D5.  The values are configurable
# via env vars so an operator can target a different model manifest without
# a code change.
# ---------------------------------------------------------------------------
_MODEL_MANIFEST_NAME = (
    os.environ.get("CMS_DEFAULT_MODEL_MANIFEST_NAME") or "CMS-Fleet-Default"
)
_MODEL_MANIFEST_VERSION = (
    os.environ.get("CMS_DEFAULT_MODEL_MANIFEST_VERSION") or "1"
)
_DECODER_MANIFEST_REF = (
    os.environ.get("CMS_DEFAULT_DECODER_MANIFEST_REF") or "cms-fleet-v3"
)
# `dataSource` is camelCase on the vehicle row per `main_api` convention;
# the value is the new-enum string introduced by spec
# `2026-06-09-cms-data-source-model-refactor`.
_DATA_SOURCE = "vehicle-telemetry"


def _guard_production() -> None:
    """Refuse to seed production without an explicit opt-in.

    Non-interactive by design (per `~/.kiro/steering/non-interactive.md`):
    the gate is an env var, never a stdin prompt, so this stays usable from
    CI and from agent sessions. Fails loudly with a non-zero exit rather
    than prompting.
    """
    targets_prod = "prod" in (STAGE, CMS_STAGE)
    if not targets_prod:
        return
    if os.environ.get("CONFIRM_PROD_SEED", "").strip().lower() in {"yes", "true", "1"}:
        print(
            f"⚠️  PROD SEED CONFIRMED — stage={STAGE} cms_stage={CMS_STAGE} "
            f"region={REGION}"
        )
        return
    print(
        "❌ Refusing to seed PRODUCTION without confirmation.\n"
        f"   Resolved: STAGE={STAGE} CMS_STAGE={CMS_STAGE} REGION={REGION}\n"
        f"   Tables:   {DRIVERS_TABLE}, {VEHICLES_TABLE}\n"
        "\n"
        "   This script force-sets Cognito passwords and overwrites driver/\n"
        "   vehicle rows. If you really mean production, re-run with:\n"
        "       STAGE=prod CONFIRM_PROD_SEED=yes python scripts/seed-persona-users.py\n"
        "\n"
        "   Did you mean staging? That is now the default — just drop STAGE.",
        file=sys.stderr,
    )
    raise SystemExit(2)



def _resolve_pool_id() -> str:
    """Resolve the meridian-fleet Cognito user pool ID from CFN exports."""
    if pool_id := os.environ.get("USER_POOL_ID"):
        return pool_id
    cfn = boto3.client("cloudformation", region_name=REGION)
    export_name = f"vsa-{STAGE}-meridian-fleet-pool-id"
    for page in cfn.get_paginator("list_exports").paginate():
        for exp in page["Exports"]:
            if exp["Name"] == export_name:
                return exp["Value"]
    raise RuntimeError(f"Export {export_name} not found — is the API stack deployed?")


# Gate BEFORE the pool lookup below — `_resolve_pool_id()` makes a live CFN
# call, so the prod check has to happen ahead of any AWS interaction.
_guard_production()

USER_POOL_ID = _resolve_pool_id()

now = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Persona definitions
# ---------------------------------------------------------------------------
#
# NOTE (2026-07-31): seed-time `connectionStatus` and `lastSeenAt` were
# previously written as `"connected"` / `now` for all three persona vehicles
# (VEH-MRDN-0015 / VEH-WPR-001 / VEH-VO-001, the second renamed 2026-08-10
# as part of the rental-tenant rebrand — see
# issues/2026-08-10-rental-tenant-rename-residual-sweep/). Because this script writes
# directly to CMS's `cms-{stage}-storage-vehicles` DDB table, the fabrication
# rendered in the CMS UI as a green "Connected" badge with a live-looking
# "Last Connected" timestamp — despite no real IoT lifecycle event ever
# having fired. This was the exact same defect class fixed in May 2026 on
# the CMS side (`cms/deployment/scripts/seed_engineering_fleets.py`), just
# recurring on the CVX side because the May guard was a source-comment in
# a different repo and could not reach here.
#
# See:
#   cms/issues/2026-05-28-cms-vehicle-fake-connected-status/{report,summary}.md
#   cms/issues/2026-07-31-fake-connected-status-regression/{report,summary}.md
#   issues/2026-07-31-persona-seed-fake-connected-regression/{report,summary}.md
#
# Rule: seed code MUST NOT write `connectionStatus="connected"` or a
# `lastSeenAt` timestamp. Leave both fields absent. Real connectivity is
# surfaced by the API's `_build_live_vehicle_state` overlay, which reads
# the Redis `vehicle:{id}:meta` hash that Flink populates on real MSK
# telemetry. So a vehicle with genuine inbound telemetry still shows as
# Connected in the UI without the seeder having to lie about it. A vehicle
# without telemetry correctly shows as Disconnected with "Last Connected: N/A".
#
# Enforced by `scripts/tests/test_seed_scripts_no_fake_connected.py` which
# also scans the sibling CMS repo. Do NOT re-introduce these fields here.

PERSONAS = [
    {
        "label": "Halcyon OEM driver",
        # Renamed from ford.driver 2026-08-21 — see the CMS-side note in
        # deployment/scripts/seed_driver_users.py. Both Cognito pools were renamed
        # in place; this seed must agree.
        "email": "meridian.driver@example.com",
        "firstName": "Marcus",
        "lastName": "Reyes",
        "fleetId": "MERIDIAN-OEM",
        "homeBase": "Atlanta",
        # "meridian", not "ford". This value becomes the Cognito user's
        # custom:tenantId, which MainTabView.loadTenantConfig() looks up by exact
        # match in vsa-{stage}-tenant-config. The row was renamed ford -> meridian on
        # 2026-08-21 (commit 389ab7c) along with seed-personas.py and the iOS
        # DemoPersona, but this seed was missed — so a reseed would have written a
        # tenantId with no matching row, and the lookup fails SILENTLY:
        # LayoutSegment.from(nil) falls through to .fleet, mislabelling tabs and
        # applying the wrong theming with no error surfaced.
        #
        # custom:tenantId is immutable in the pool, so fixing a wrongly-seeded user
        # means delete-and-recreate, not an attribute update.
        "tenantId": "meridian",
        "driver": {
            "driverId": "DRV-MRDN-0015",
            "phone": "+14045550101",
            "licenseClass": "C",
            "licenseState": "GA",
            "licenseNumber": "GA-FORD-001",
            "licenseExpiry": "2028-09-15",
            "hireDate": "2023-03-12",
            "yearsExperience": 6,
            "totalMiles": 48312,
            "totalTrips": 387,
            "incidentCount": 0,
            "safetyScore": 92,
            "lastTripDate": "2026-05-17",
            "status": "ACTIVE",
            "certifications": [],
        },
        "vehicle": {
            "vehicleId": "VEH-MRDN-0015",
            "vin": "MRDN0000000000015",
            # This persona's vehicle is an ELECTRIC SUV, not the petrol pickup
            # this block used to describe.
            #
            # The live row was converted to an EV directly in DynamoDB on
            # 2026-08-12 — make/model/vehicleType/fuelType/batteryCapacityKwh and
            # batteryVoltage were all updated there — but this seed was never
            # brought along. So `make seed-staging` would have silently reverted
            # the demo vehicle to a gasoline "Halcyon F-150 pickup" the next time
            # anyone ran it, on the morning of a demo. Aligned to the live row
            # 2026-08-19; keep the two in step.
            "make": "Meridian",
            "model": "Azimuth",
            # 2023 deliberately, NOT the purchase year. The upgrade recommender
            # applies a same-nameplate bonus, so the owned car must be an EARLIER
            # generation than the 2026 Trailwind the demo offers — a 2025 owned
            # against a 2026 offer reads as trading a one-year-old car. Set in
            # cms/deployment/scripts/seed_driver_users.py first (commit c96c7fa4);
            # this seed must agree or whichever runs last wins.
            "year": 2023,
            "color": "Slate Blue Metallic",
            "licensePlate": "GA-MERI01",
            "vehicleType": "suv",
            "fuelType": "electric",
            # Atlanta, GA — downtown-ish
            "lastLatitude": Decimal("33.7488"),
            "lastLongitude": Decimal("-84.3877"),
            # `lastSeenAt` and `connectionStatus` are intentionally absent
            # from seed writes — see the doctrine block above `PERSONAS =`.
            #
            # `fuelLevel` carries STATE OF CHARGE for a BEV. That is the
            # platform's existing convention, not a shortcut: the backend
            # aliases the Redis `ev_soc` signal onto `fuelLevel`, and both iOS
            # surfaces flip the row's label to "Battery" on `fuelType`. A second
            # `batterySoc` field would give SoC two sources that can disagree.
            #
            # The EV conversion left this at 0, which the app rendered as a
            # flat "Battery 0%" on an otherwise healthy vehicle.
            "fuelLevel": Decimal("72"),
            # State of health has no existing home, so it IS a new field.
            # Distinct from SoC: charge is how full the pack is now, health is
            # how much of its original capacity remains after 443 trips.
            "batterySoh": Decimal("94"),
            "batteryCapacityKwh": Decimal("94"),
            "batteryVoltage": Decimal("398.4"),
            # NO engineTemp / engineRPM. An EV has neither, and seeding them as
            # 0 is worse than omitting them: the UI cannot tell "no engine" from
            # "engine reading zero", so it rendered "Engine 0°F" on a BEV.
            "lastSpeed": Decimal("0"),
            "odometer": Decimal("39840"),
            "mileage": Decimal("39840"),
            "totalTrips": Decimal("443"),
            "purchaseDate": "2023-05-20",   # matches the 2023 model year
            "purchasePrice": Decimal("62500"),
            "annualInsurancePremium": Decimal("1980"),
            "fleetId": "FLEET-MERIDIAN-ONBOARD",
            "name": "Marcus's Trailwind",
            "status": "ACTIVE",
            "enrollmentStatus": "ACTIVE",
            # Model manifest fields — required by spec 2026-08-28-cms-cert-follows-model § D6.
            # Override via CMS_DEFAULT_MODEL_MANIFEST_NAME / _VERSION / _DECODER_MANIFEST_REF.
            "modelManifestName":    _MODEL_MANIFEST_NAME,
            "modelManifestVersion": _MODEL_MANIFEST_VERSION,
            "decoderManifestRef":   _DECODER_MANIFEST_REF,
            "dataSource":           _DATA_SOURCE,
            "attributes": {
                "primaryUse": "personal",
                "fuelType": "electric",
                "operationalCity": "atlanta",
                "fleetType": "oem-owner",
            },
        },
    },
    {
        "label": "Waypoint Rentals driver",
        "email": "priya.shah@example.com",
        "firstName": "Priya",
        "lastName": "Shah",
        "fleetId": "WPR-RENTAL",
        "homeBase": "Chicago",
        "tenantId": "waypoint-rentals",
        "driver": {
            "driverId": "DRV-WPR-001",
            "phone": "+13125550202",
            "licenseClass": "C",
            "licenseState": "IL",
            "licenseNumber": "IL-WPR-001",
            "licenseExpiry": "2027-06-30",
            "hireDate": "2025-11-04",
            "yearsExperience": 4,
            "totalMiles": 1247,
            "totalTrips": 8,
            "incidentCount": 0,
            "safetyScore": 88,
            "lastTripDate": "2026-05-18",
            "status": "ACTIVE",
            "certifications": [],
        },
        "vehicle": {
            # ID is VEH-ENT-001, not VEH-WPR-001.
            #
            # The rental-brand -> Waypoint Rentals rename changed this block's
            # vehicleId, but the new row was never created, so the ORIGINAL
            # VEH-ENT-001 stayed live carrying a real "Toyota Camry" — and both
            # assigned drivers (DRV-ENT-001 Priya Shah, DRV-0001 Christopher
            # Wright) still point at VEH-ENT-001. The audit reported the two halves
            # as separate findings ("VEH-WPR-001 declared in the seed but absent"
            # and a real manufacturer on VEH-ENT-001); they are one half-finished
            # rename. Reconciled onto the ID that is actually in use and joined
            # against, rather than renaming a partition key.
            "vehicleId": "VEH-ENT-001",
            # `DEMO0000000000013` — was `4T1B11HK0LU98765`, a `4T1` Toyota WMI on a
            # row whose make/model had already been rebranded to DemoMotors/Voyager.
            # The last real-automaker identifier in either repo's seed scripts.
            #
            # `DEMO` and not `MRDN`, because this vehicle is deliberately DemoMotors
            # (see the note below) — an MRDN VIN would contradict `make` the same
            # way the Toyota VIN contradicted it, just less visibly. `DEMO00…` is an
            # established synthetic prefix in this fleet; 013 is the next free
            # sequence value (001-005 and 011-012 are in use).
            #
            # Also fixes a LENGTH defect: the old value was **16 characters**, not
            # the 17 ISO 3779 requires. That is why
            # `scripts/tests/test_seed_scripts_no_real_automaker_wmi.py` did not
            # catch it on first write — its regex required exactly 17, so a
            # malformed VIN carrying a real WMI slipped past the guard built to find
            # exactly that. The guard now matches 11-21 characters and flags
            # non-17-length separately; this row was its first real catch.
            "vin": "DEMO0000000000013",
            # DemoMotors, not Meridian. This block previously said "Meridian
            # Azimuth", which contradicts the lineup twice: Azimuth is a PICKUP in
            # corpora/meridian_lineup_narrative/ and in seed_meridian_fleet.py,
            # and the Meridian lineup is electric — this is a petrol sedan.
            # DemoMotors is already an established fictional make in this demo
            # (VEH-DEMO-PUB-001/002 are DemoMotors Voyager sedans) and carries no
            # lineup narrative to contradict.
            #
            # Kept petrol deliberately: it is the only ICE persona vehicle, so it
            # is the only one that exercises the app's "Fuel" row and engine-temp
            # path rather than the EV battery card.
            "make": "DemoMotors",
            "model": "Voyager",
            "year": 2024,
            "color": "Midnight Black",
            "licensePlate": "IL-WPR-002",
            "vehicleType": "sedan",
            "fuelType": "gasoline",
            # Chicago, IL — Loop
            "lastLatitude": Decimal("41.8781"),
            "lastLongitude": Decimal("-87.6298"),
            # `lastSeenAt` and `connectionStatus` are intentionally absent
            # from seed writes — see the doctrine block above `PERSONAS =`.
            "fuelLevel": Decimal("64"),
            "batteryVoltage": Decimal("12.4"),
            "engineTemp": Decimal("195"),
            "engineRPM": Decimal("0"),
            "lastSpeed": Decimal("0"),
            "odometer": Decimal("1247"),
            "mileage": Decimal("1247"),
            "totalTrips": Decimal("8"),
            "purchaseDate": "2024-08-15",
            "purchasePrice": Decimal("28450"),
            "annualInsurancePremium": Decimal("1450"),
            "batteryCapacityKwh": Decimal("0"),
            "fleetId": "WPR-RENTAL",
            "name": "Azimuth — Rental Unit 7842",
            "status": "ACTIVE",
            "enrollmentStatus": "ACTIVE",
            # Model manifest fields — required by spec 2026-08-28-cms-cert-follows-model § D6.
            # Override via CMS_DEFAULT_MODEL_MANIFEST_NAME / _VERSION / _DECODER_MANIFEST_REF.
            "modelManifestName":    _MODEL_MANIFEST_NAME,
            "modelManifestVersion": _MODEL_MANIFEST_VERSION,
            "decoderManifestRef":   _DECODER_MANIFEST_REF,
            "dataSource":           _DATA_SOURCE,
            "attributes": {
                "primaryUse": "rental",
                "fuelType": "gasoline",
                "operationalCity": "chicago",
                "fleetType": "rental",
                "rentalReturnLocation": "Waypoint — Chicago O'Hare",
            },
        },
    },
    # --- Expansion personas (CVX-on-foundation v1) ---
    {
        "label": "Vehicle owner",
        "email": "vehicle.owner@example.com",
        "firstName": "Jordan",
        "lastName": "Parker",
        "fleetId": "VO-OEM",
        "homeBase": "Austin",
        "tenantId": "vehicle-owner",
        "persona_id": "vehicle_owner",
        "driver": {
            "driverId": "DRV-VO-001",
            "phone": "+15125550303",
            "licenseClass": "C",
            "licenseState": "TX",
            "licenseNumber": "TX-VO-001",
            "licenseExpiry": "2029-03-20",
            "hireDate": "2024-01-10",
            "yearsExperience": 5,
            "totalMiles": 22800,
            "totalTrips": 210,
            "incidentCount": 0,
            "safetyScore": 95,
            "lastTripDate": "2026-06-14",
            "status": "ACTIVE",
            "certifications": [],
        },
        "vehicle": {
            "vehicleId": "VEH-VO-001",
            # VIN: a real Meridian Crestwind from ADP's analysed fleet.
            #
            # Was `1G1FY6S07N4100001` — a GM WMI (`1G1`) on a Meridian-branded row.
            # CMS scrubbed that on 2026-09-20 to `MRDN0000000000016`
            # (`cms/issues/2026-09-20-unrebranded-vins-on-meridian-vehicles/`) and
            # marked it RESOLVED. **This seeder silently reverted it.** It writes
            # directly into `cms-{stage}-storage-vehicles`, still carried the GM
            # literal, and its 2026-09-22T13:25:33Z run put the GM VIN back — the
            # same cross-repo resurrection that
            # `cms/issues/2026-09-22-vsa-michelin-stale-persona-ids/` records for
            # VEH-FORD-001. The DDB write was never the durable fix; THIS file is.
            #
            # Target changed from the scrub's `MRDN0000000000016` deliberately.
            # That value is Meridian-consistent but exists nowhere else: 0 rows in
            # `adp_staging_dimensions.vins` and 0 in all three health sources, so a
            # vehicle carrying it can never produce an AVX Finding. This VIN is in
            # ADP's fleet as a Meridian Motors Crestwind 2023 and already carries a
            # live Finding, so it satisfies the scrub's goal (no real-automaker WMI)
            # AND lets the drivers-construct ownership chain resolve end to end:
            #   vehicle.owner@example.com -> DRV-VO-001 -> VEH-VO-001 -> this VIN
            #   -> Finding.scope.vin
            # See `cvx/issues/2026-09-23-avx-owner-id-not-comparable-to-cognito-sub/`
            # § "USER DECISION" for why ownership keys on the driver, not `sold_to`.
            #
            # Verified free before adoption: 0 other rows in
            # `cms-staging-storage-vehicles` carry it.
            "vin": "MRDCW01H8PC00V7RD",
            # Meridian Crestwind, was "Chevrolet Bolt EV" — a real manufacturer
            # AND a real nameplate, on a persona vehicle the agent narrates.
            #
            # Meridian specifically, for two reasons beyond the brand string. This
            # tenant (`vehicle-owner`) is segment=oem, so find_service_center
            # filters dealers by the vehicle's make — and no dealer services
            # Chevrolet, so Jordan's dealer and test-ride lookups returned ZERO
            # centres. Second, Crestwind is the lineup's saloon and has an imageset
            # (MeridianCrestwind), so the app can actually render the vehicle;
            # VehicleSweepView.imageName(forModelToken:) resolves only
            # crestwind/trailwind/windrose/azimuth.
            #
            # The VIN now agrees with both: `MRDCW` is the Crestwind prefix in ADP's
            # own VIN scheme, and ADP's dimension row for it says Crestwind too, so
            # make/model/VIN no longer disagree the way they did under the GM VIN.
            #
            # vehicleType follows the lineup: Crestwind is a sedan, not a hatchback.
            "make": "Meridian",
            "model": "Crestwind",
            # 2023, matching ADP's dimension row for this VIN. Was 2022 under the
            # GM VIN, which no external source could contradict; now it can, so it
            # agrees.
            "year": 2023,
            "color": "Glacier Blue",
            "licensePlate": "TX-VO-001",
            "vehicleType": "sedan",
            "fuelType": "electric",
            # Austin, TX — downtown
            "lastLatitude": Decimal("30.2672"),
            "lastLongitude": Decimal("-97.7431"),
            # `lastSeenAt` and `connectionStatus` are intentionally absent
            # from seed writes — see the doctrine block above `PERSONAS =`.
            #
            # `fuelLevel` carries STATE OF CHARGE for a BEV — see the note on
            # VEH-MRDN-0015 above. This was seeded as 0, which the app rendered as
            # "Battery 0%" on a healthy EV, and `engineTemp` was seeded as 22 on a
            # vehicle that has no engine — worse than a zero, because 22°F looks
            # like a genuine reading rather than missing data. Corrected
            # 2026-08-19; `scripts/audit-vehicle-data.py` now fails on both shapes.
            "fuelLevel": Decimal("58"),
            "batteryVoltage": Decimal("400"),
            # NO engineTemp / engineRPM — a BEV has neither.
            "lastSpeed": Decimal("0"),
            "odometer": Decimal("22800"),
            "mileage": Decimal("22800"),
            "totalTrips": Decimal("210"),
            "purchaseDate": "2022-04-05",
            "purchasePrice": Decimal("38495"),
            "annualInsurancePremium": Decimal("1620"),
            "batteryCapacityKwh": Decimal("65"),
            "batterySoh": Decimal("91"),
            "fleetId": "VO-OEM",
            "name": "Jordan's Bolt EV",
            "status": "ACTIVE",
            "enrollmentStatus": "ACTIVE",
            # Model manifest fields — required by spec 2026-08-28-cms-cert-follows-model § D6.
            # Override via CMS_DEFAULT_MODEL_MANIFEST_NAME / _VERSION / _DECODER_MANIFEST_REF.
            "modelManifestName":    _MODEL_MANIFEST_NAME,
            "modelManifestVersion": _MODEL_MANIFEST_VERSION,
            "decoderManifestRef":   _DECODER_MANIFEST_REF,
            "dataSource":           _DATA_SOURCE,
            "attributes": {
                "primaryUse": "personal",
                "fuelType": "electric",
                "operationalCity": "austin",
                "fleetType": "oem-owner",
            },
        },
    },
    {
        "label": "Service advisor",
        "email": "service.advisor@example.com",
        "firstName": "Alex",
        "lastName": "Rivera",
        "fleetId": "SA-FLEET",
        "homeBase": "Dallas",
        "tenantId": "service-advisor",
        "persona_id": "service_advisor",
        "driver": {
            "driverId": "DRV-SA-001",
            "phone": "+12145550404",
            "licenseClass": "C",
            "licenseState": "TX",
            "licenseNumber": "TX-SA-001",
            "licenseExpiry": "2028-11-30",
            "hireDate": "2021-07-19",
            "yearsExperience": 8,
            "totalMiles": 0,
            "totalTrips": 0,
            "incidentCount": 0,
            "safetyScore": 100,
            "lastTripDate": "2026-06-10",
            "status": "ACTIVE",
            "certifications": ["ASE-service-consultant"],
        },
        "vehicle": None,  # service advisor does not have an assigned vehicle
    },
    {
        "label": "Technician",
        "email": "technician@example.com",
        "firstName": "Sam",
        "lastName": "Chen",
        "fleetId": "TECH-FLEET",
        "homeBase": "Phoenix",
        "tenantId": "technician",
        "persona_id": "technician",
        "driver": {
            "driverId": "DRV-TECH-001",
            "phone": "+16025550505",
            "licenseClass": "C",
            "licenseState": "AZ",
            "licenseNumber": "AZ-TECH-001",
            "licenseExpiry": "2027-08-15",
            "hireDate": "2019-03-01",
            "yearsExperience": 12,
            "totalMiles": 0,
            "totalTrips": 0,
            "incidentCount": 0,
            "safetyScore": 100,
            "lastTripDate": "2026-06-13",
            "status": "ACTIVE",
            "certifications": ["ASE-A1", "ASE-A8", "ASE-L1"],
        },
        "vehicle": None,  # technician does not have an assigned vehicle
    },
]


# ---------------------------------------------------------------------------
# Cognito user provisioning
# ---------------------------------------------------------------------------

def _ensure_cognito_user(cidp, email: str, given_name: str, family_name: str,
                         persona_id: str | None = None,
                         tenant_id: str | None = None) -> None:
    """Create the user if missing, then force-set the permanent password
    (so first-sign-in doesn't require the temp-password flow). Idempotent.

    tenant_id (kebab-case) is written to ``custom:tenantId`` at create time
    so persona chat requests resolve to their proper tenant instead of
    silently defaulting to ``meridian-fleet`` in the handler's fallback path.
    Added 2026-08-10 to close backlog row 'Tenant claim missing' (P2). The
    pool's ``custom:tenantId`` schema is ``Mutable: False`` (write-once at
    the pool level), so existing users with an EMPTY custom:tenantId slot
    still fall through to the update path below on subsequent runs — the
    initial write is allowed even for Mutable=False as long as the attribute
    has no prior value.
    """
    user_attributes = [
        {"Name": "email", "Value": email},
        {"Name": "email_verified", "Value": "true"},
        {"Name": "given_name", "Value": given_name},
        {"Name": "family_name", "Value": family_name},
    ]
    if tenant_id is not None:
        user_attributes.append({"Name": "custom:tenantId", "Value": tenant_id})
    # NOTE (2026-07-28): `custom:persona_id` is deliberately NOT written.
    #
    # Neither `vsa-staging-meridian-fleet` nor `cms-staging-ui-users` declares a
    # `custom:persona_id` schema attribute, so `admin_create_user` failed with
    # `InvalidParameterException: Attributes did not conform to the schema:
    # Type for attribute {custom:persona_id} could not be determined`. Because
    # this loop is ordered and not fault-tolerant, that aborted the whole
    # `seed-staging` target — the two personas WITHOUT a persona_id were
    # created and the three WITH one (vehicle_owner, service_advisor,
    # technician) were silently never provisioned. Symptom: a
    # `UserNotFoundException` at sign-in for those three.
    #
    # The attribute is also unused: a repo-wide grep found NO reader of
    # `custom:persona_id` in either repo. Persona routing goes through
    # `lambdas/api-assistant-chat/persona_map.py::infer_persona`, which reads
    # `cognito:groups` and `custom:role` only. Writing an attribute nobody
    # consumes was buying a hard failure for nothing.
    #
    # Adding the attribute to the pools was the alternative and was rejected:
    # Cognito custom attributes can never be deleted or renamed once created,
    # so it would be a one-way schema change on shared staging pools to
    # satisfy a phantom consumer. If a real consumer ever appears, add the
    # attribute to the pool definitions in CDK FIRST, then restore the write
    # here guarded by a schema probe.
    _ = persona_id  # retained in the signature for call-site compatibility
    try:
        cidp.admin_create_user(
            UserPoolId=USER_POOL_ID,
            Username=email,
            UserAttributes=user_attributes,
            MessageAction="SUPPRESS",  # don't email the user
        )
        print(f"  cognito: created {email}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "UsernameExistsException":
            print(f"  cognito: {email} already exists, updating password")
            # If the user pre-dates the custom:tenantId write (2026-08-10
            # fix, backlog row 'Tenant claim missing'), the slot is empty
            # and can be set once — Cognito's Mutable=False constraint
            # governs re-writes, not the initial write. If a value already
            # exists we log the mismatch and keep going (do NOT fail-closed,
            # per user decision 2026-08-03: meridian-fleet fallback in the handler
            # is intentional demo behaviour).
            if tenant_id is not None:
                try:
                    cidp.admin_update_user_attributes(
                        UserPoolId=USER_POOL_ID,
                        Username=email,
                        UserAttributes=[
                            {"Name": "custom:tenantId", "Value": tenant_id}
                        ],
                    )
                    print(f"  cognito: set custom:tenantId={tenant_id} on {email}")
                except ClientError as inner:
                    code = inner.response["Error"]["Code"]
                    if code == "InvalidParameterException":
                        # Slot already populated with a different value.
                        # Cognito Mutable=False refuses the change.
                        print(f"  cognito: WARNING — could not set "
                              f"custom:tenantId on {email} (slot "
                              f"immutable and populated); handler will "
                              f"fall through to meridian-fleet default.")
                    else:
                        raise
        else:
            raise
    cidp.admin_set_user_password(
        UserPoolId=USER_POOL_ID,
        Username=email,
        Password=DEMO_PASSWORD,
        Permanent=True,
    )
    print(f"  cognito: password set for {email}")


# ---------------------------------------------------------------------------
# CMS row provisioning
# ---------------------------------------------------------------------------

def _put_driver(table, persona: dict) -> None:
    item = {
        "driverId": persona["driver"]["driverId"],
        "email": persona["email"],
        "cognitoEmail": persona["email"],   # exact field api-drivers-me scans on
        "firstName": persona["firstName"],
        "lastName": persona["lastName"],
        "fleetId": persona["fleetId"],
        "homeBase": persona["homeBase"],
        "createdAt": now,
        "updatedAt": now,
        **persona["driver"],
    }
    if persona.get("vehicle"):
        item["assignedVehicleId"] = persona["vehicle"]["vehicleId"]
    table.put_item(Item=item)
    assigned = item.get("assignedVehicleId", "none")
    print(f"  driver: {item['driverId']} ({item['email']}) -> {assigned}")


def _put_vehicle(table, persona: dict) -> None:
    item = {
        "createdAt": now,
        "updatedAt": now,
        **persona["vehicle"],
    }
    table.put_item(Item=item)
    print(
        f"  vehicle: {item['vehicleId']} ({item['year']} {item['make']} "
        f"{item['model']}) at {persona['homeBase']}"
    )


def _fix_samantha_cognito_email(table) -> None:
    """The fleet-persona driver row had cognitoEmail nulled at some point.
    Re-set it so the existing demo path keeps working alongside the new
    personas. Safe to re-run."""
    try:
        table.update_item(
            Key={"driverId": "DRV-0054"},
            UpdateExpression="SET cognitoEmail = :e, updatedAt = :u",
            ExpressionAttributeValues={
                ":e": "samantha.carter@example.com",
                ":u": now,
            },
        )
        print("  driver: re-asserted cognitoEmail on DRV-0054 (samantha.carter)")
    except ClientError as e:
        print(f"  driver: skip samantha update — {e.response['Error']['Code']}")


def main() -> int:
    cidp = boto3.client("cognito-idp", region_name=REGION)
    drivers = boto3.resource("dynamodb", region_name=REGION).Table(DRIVERS_TABLE)
    vehicles = boto3.resource("dynamodb", region_name=REGION).Table(VEHICLES_TABLE)

    print(f"Pool: {USER_POOL_ID}")
    print()
    for p in PERSONAS:
        print(f"{p['label']} — {p['email']}")
        _ensure_cognito_user(cidp, p["email"], p["firstName"], p["lastName"],
                             persona_id=p.get("persona_id"),
                             tenant_id=p.get("tenantId"))
        _put_driver(drivers, p)
        if p.get("vehicle"):
            _put_vehicle(vehicles, p)
        print()

    print("Fixing existing fleet persona driver row")
    _fix_samantha_cognito_email(drivers)

    # Surface the credential exactly once, at the end, and only when we
    # generated it — if the operator supplied DEMO_PASSWORD they already have it,
    # and echoing it back would put it in CI logs for no benefit.
    if not _DEMO_PASSWORD_FROM_ENV:
        print()
        print("=" * 68)
        print("Generated a one-off password for the seeded personas:")
        print(f"    {DEMO_PASSWORD}")
        print()
        print("It is NOT stored anywhere. Capture it now if you need it, or set")
        print("DEMO_PASSWORD (e.g. from Secrets Manager) to control it yourself.")
        print("Re-running this script issues a new password.")
        print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
