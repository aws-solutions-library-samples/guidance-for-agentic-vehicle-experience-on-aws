"""Seed Cognito users + CMS driver/vehicle rows for OEM, Rental, and expansion personas.

Pairs with seed-personas.py (which adds tenant configs). After this
runs, the iOS sign-in flow can authenticate as any persona and the
backend's /drivers/me endpoint resolves them to a real assigned vehicle.

What this seeds (idempotent — safe to re-run):

  ford.driver@example.com
    Cognito user (pool <tenant-pool-id>, password from $DEMO_PASSWORD, force-confirmed)
    DRV-FORD-001 — assignedVehicleId=VEH-FORD-001
    VEH-FORD-001 — Halcyon F-150 in Atlanta, GA

  enterprise.driver@example.com
    Cognito user (same pool, same password, force-confirmed)
    DRV-ENT-001 — assignedVehicleId=VEH-ENT-001
    VEH-ENT-001 — Toyota Camry in Chicago, IL

  vehicle.owner@example.com  (expansion — vehicle_owner persona)
    DRV-VO-001 — assignedVehicleId=VEH-VO-001
    VEH-VO-001 — Chevy Bolt EV in Austin, TX

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

    Mirrors `scripts/seed-michelin-user.py::_random_pw` — same alphabet, same
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
    """Resolve the michelin Cognito user pool ID from CFN exports."""
    if pool_id := os.environ.get("USER_POOL_ID"):
        return pool_id
    cfn = boto3.client("cloudformation", region_name=REGION)
    export_name = f"vsa-{STAGE}-michelin-pool-id"
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
# (VEH-FORD-001 / VEH-ENT-001 / VEH-VO-001). Because this script writes
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
        "email": "ford.driver@example.com",
        "firstName": "Marcus",
        "lastName": "Reyes",
        "fleetId": "FORD-OEM",
        "homeBase": "Atlanta",
        "tenantId": "ford",
        "driver": {
            "driverId": "DRV-FORD-001",
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
            "vehicleId": "VEH-FORD-001",
            "vin": "1FTFW1ED5MFB12345",
            "make": "Halcyon",
            "model": "F-150",
            "year": 2023,
            "color": "Oxford White",
            "licensePlate": "GA-FORD01",
            "vehicleType": "pickup",
            "fuelType": "gasoline",
            # Atlanta, GA — downtown-ish
            "lastLatitude": Decimal("33.7488"),
            "lastLongitude": Decimal("-84.3877"),
            # `lastSeenAt` and `connectionStatus` are intentionally absent
            # from seed writes — see the doctrine block above `PERSONAS =`.
            "fuelLevel": Decimal("78"),
            "batteryVoltage": Decimal("12.6"),
            "engineTemp": Decimal("190"),
            "engineRPM": Decimal("0"),
            "lastSpeed": Decimal("0"),
            "odometer": Decimal("32145"),
            "mileage": Decimal("32145"),
            "totalTrips": Decimal("387"),
            "purchaseDate": "2023-03-10",
            "purchasePrice": Decimal("48995"),
            "annualInsurancePremium": Decimal("1820"),
            "batteryCapacityKwh": Decimal("0"),
            "fleetId": "FORD-OEM",
            "name": "Marcus's F-150",
            "status": "ACTIVE",
            "enrollmentStatus": "ACTIVE",
            "attributes": {
                "primaryUse": "personal",
                "fuelType": "gasoline",
                "operationalCity": "atlanta",
                "fleetType": "oem-owner",
            },
        },
    },
    {
        "label": "Enterprise rental driver",
        "email": "enterprise.driver@example.com",
        "firstName": "Priya",
        "lastName": "Shah",
        "fleetId": "ENT-RENTAL",
        "homeBase": "Chicago",
        "tenantId": "enterprise",
        "driver": {
            "driverId": "DRV-ENT-001",
            "phone": "+13125550202",
            "licenseClass": "C",
            "licenseState": "IL",
            "licenseNumber": "IL-ENT-001",
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
            "vehicleId": "VEH-ENT-001",
            "vin": "4T1B11HK0LU98765",
            "make": "Toyota",
            "model": "Camry",
            "year": 2024,
            "color": "Midnight Black",
            "licensePlate": "IL-ENT-002",
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
            "fleetId": "ENT-RENTAL",
            "name": "Camry — Rental Unit 7842",
            "status": "ACTIVE",
            "enrollmentStatus": "ACTIVE",
            "attributes": {
                "primaryUse": "rental",
                "fuelType": "gasoline",
                "operationalCity": "chicago",
                "fleetType": "rental",
                "rentalReturnLocation": "Enterprise — Chicago O'Hare",
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
        "tenantId": "vehicle_owner",
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
            "vin": "1G1FY6S07N4100001",
            "make": "Chevrolet",
            "model": "Bolt EV",
            "year": 2022,
            "color": "Glacier Blue",
            "licensePlate": "TX-VO-001",
            "vehicleType": "hatchback",
            "fuelType": "electric",
            # Austin, TX — downtown
            "lastLatitude": Decimal("30.2672"),
            "lastLongitude": Decimal("-97.7431"),
            # `lastSeenAt` and `connectionStatus` are intentionally absent
            # from seed writes — see the doctrine block above `PERSONAS =`.
            "fuelLevel": Decimal("0"),
            "batteryVoltage": Decimal("400"),
            "engineTemp": Decimal("22"),
            "engineRPM": Decimal("0"),
            "lastSpeed": Decimal("0"),
            "odometer": Decimal("22800"),
            "mileage": Decimal("22800"),
            "totalTrips": Decimal("210"),
            "purchaseDate": "2022-04-05",
            "purchasePrice": Decimal("38495"),
            "annualInsurancePremium": Decimal("1620"),
            "batteryCapacityKwh": Decimal("65"),
            "fleetId": "VO-OEM",
            "name": "Jordan's Bolt EV",
            "status": "ACTIVE",
            "enrollmentStatus": "ACTIVE",
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
        "tenantId": "service_advisor",
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
                         persona_id: str | None = None) -> None:
    """Create the user if missing, then force-set the permanent password
    (so first-sign-in doesn't require the temp-password flow). Idempotent."""
    user_attributes = [
        {"Name": "email", "Value": email},
        {"Name": "email_verified", "Value": "true"},
        {"Name": "given_name", "Value": given_name},
        {"Name": "family_name", "Value": family_name},
    ]
    # NOTE (2026-07-28): `custom:persona_id` is deliberately NOT written.
    #
    # Neither `vsa-staging-michelin` nor `cms-staging-ui-users` declares a
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
                             persona_id=p.get("persona_id"))
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
