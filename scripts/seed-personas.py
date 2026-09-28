"""Seed the Meridian (OEM), Waypoint Rentals, and expansion-persona tenant configs.

These are sister configs to seed-meridian-fleet.py, each adjusted for their
persona segment so the agent's find_service_center filter and the
voice prompt's narration block route correctly:

  ford           -> segment=oem    (route to authorised Meridian dealers)
  waypoint-rentals -> segment=rental (route to national chains / quick-service)
  vehicle-owner  -> segment=oem    (dealer-network steering for private owners)
  service-advisor -> segment=fleet  (broad service routing for advisors)
  technician     -> segment=fleet  (broad service routing for technicians)

The configs reuse the Meridian Fleet shape so existing readers (tenant-config
loader, escalate handler, voice agent) work identically. Branding
colors, display names, greetings, and the supervisor prompt id are the
fields that actually change. Connect/Cognito/Lex ARNs stay placeholder
because the demo deploys all three personas against the same backend
infrastructure for now.

Usage:
  STAGE=staging AWS_REGION=us-west-2 python scripts/seed-personas.py
  STAGE=prod    AWS_REGION=us-east-1 python scripts/seed-personas.py --confirm-prod

STAGE is REQUIRED and there is no default. It previously defaulted to `prod` with
`AWS_REGION` defaulting to `us-east-1`, and the script parsed no arguments — so a bare
invocation, or one carrying an unrecognised flag such as `--help`, silently upserted
five tenant configs into PRODUCTION. That happened on 2026-08-20 while someone was
merely trying to read the usage text. The documented invocation and the dangerous one
were byte-identical apart from an env prefix, which is not a safe design for a script
whose only action is a write.

Writing to prod now additionally requires `--confirm-prod`. Idempotency is not a
defence here: overwriting a row with source values is only harmless if source and prod
already agree, and nobody checks that before running.

Idempotent: re-running overwrites in place (DDB PutItem on the same
(tenantId, version) and "current" pointer).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import boto3
import jsonschema

HERE = Path(__file__).parent
ROOT = HERE.parent
SCHEMA = json.loads((ROOT / "schemas" / "vsa-tenant-config.schema.json").read_text())
SCHEMA_NAME = "schemas/vsa-tenant-config.schema.json"

_VALID_STAGES = ("staging", "prod")

STAGE = os.environ.get("STAGE", "").strip()
if STAGE not in _VALID_STAGES:
    sys.exit(
        f"STAGE is required and must be one of {_VALID_STAGES} (got {STAGE!r}).\n"
        "There is deliberately NO default: this script's only action is a write, and it\n"
        "used to default to prod — a bare run, or `--help`, seeded production.\n"
        "  STAGE=staging AWS_REGION=us-west-2 python scripts/seed-personas.py\n"
        "  STAGE=prod    AWS_REGION=us-east-1 python scripts/seed-personas.py --confirm-prod"
    )

REGION = os.environ.get("AWS_REGION", "").strip()
if not REGION:
    sys.exit(
        "AWS_REGION is required. It used to default to us-east-1, so a staging-intended\n"
        "run with STAGE unset wrote to prod in a region the operator never named."
    )

# Reject unknown flags rather than ignoring them. `--help` being silently swallowed and
# executing a prod write is precisely the failure this guards.
_ALLOWED_FLAGS = {"--confirm-prod"}
_unknown = [a for a in sys.argv[1:] if a not in _ALLOWED_FLAGS]
if _unknown:
    sys.exit(
        f"unrecognised argument(s): {' '.join(_unknown)}\n"
        "This script takes no options other than --confirm-prod. If you were looking for\n"
        "usage, read the module docstring — it does not execute."
    )

if STAGE == "prod" and "--confirm-prod" not in sys.argv[1:]:
    sys.exit(
        "refusing to seed PROD without --confirm-prod.\n"
        f"Would have written to vsa-prod-tenant-config in {REGION}."
    )

TABLE = f"vsa-{STAGE}-tenant-config"

now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _common_block(*, tenant_id: str, display_name: str, segment: str,
                  primary: str, secondary: str, greeting_app: str,
                  greeting_voice: str, prompt_id: str) -> dict:
    """Return a tenant-config record matching the Meridian Fleet schema, with
    the persona-specific bits dropped in. Channels/Connect/escalation
    point at the same demo infra placeholders Meridian Fleet uses; in a real
    deploy each tenant would have its own Connect instance + Lex bot."""
    return {
        "tenantId": tenant_id,
        "version": "1.0.0",
        "status": "active",
        "displayName": display_name,
        "segment": segment,
        "branding": {
            "logoUrl": f"https://assets.vsa.example.com/{tenant_id}/logo.svg",
            "primaryColor": primary,
            "secondaryColor": secondary,
            "fontFamily": "'Helvetica Neue', sans-serif",
            "mapStyle": "mapbox://styles/vsa/fleet-dark",
            "greeting": {
                "voice": greeting_voice,
                "chat": greeting_voice,
                "app": greeting_app,
            },
        },
        "channels": {
            "connect": {
                "instanceArn": "arn:aws:connect:us-east-1:YOUR_ACCOUNT_ID:instance/PLACEHOLDER",
                "inboundDid": "+18005551200",
                "contactFlowId": "PLACEHOLDER",
                "lexBotAliasArn": "arn:aws:lex:us-east-1:YOUR_ACCOUNT_ID:bot-alias/PLACEHOLDER/PROD",
                "recording": True,
            },
            "companionApp": {"enabled": True, "subdomain": tenant_id, "authMode": "sso"},
        },
        "voice": {"engine": "nova-2-sonic", "voiceId": "matthew", "locale": "en-US"},
        "identity": {
            "callerResolution": "phone-lookup",
            "phoneLookupSource": "adp-c360",
            "consentRequired": ["recording"],
            "tenantIdp": {
                "cognitoUserPoolArn": "arn:aws:cognito-idp:us-east-1:YOUR_ACCOUNT_ID:userpool/PLACEHOLDER"
            },
        },
        "agents": {
            "runtime": "agentcore",
            "supervisor": {
                "framework": "strands",
                "modelId": "anthropic.claude-sonnet-4-6",
                "systemPromptId": prompt_id,
                "maxTokens": 4096,
            },
            "specialistsEnabled": ["recall-warranty", "maintenance", "cost"],
            "tenantSpecialists": [],
            "knowledgeBaseIds": {
                "shared": "VFO_SHARED_KB_ID",
                "tenantOverlay": f"{tenant_id.upper()}_TENANT_KB_ID",
            },
        },
        "triage": {
            "policyOverrides": {
                "thresholds": {
                    "coolantTempP1C": 115,
                    "coolantTempP0C": 125,
                    "tirePressureP2Delta": 4,
                    "tirePressureP1Delta": 8,
                    "brakePadWearP2Mm": 3,
                    "brakePadWearP1Mm": 1.5,
                },
                "escalations": [],
                "downgradeForbidden": True,
            },
            "predictiveMaintenanceWeight": "escalator-one-tier",
        },
        "booking": {
            "ticketSink": {
                "type": "dynamodb",
                "target": f"vsa-{STAGE}-tickets-fallback",
                "credentialsSecretArn": None,
            },
            "starXmlConversion": False,
            "warmTransfer": {
                "enabled": True,
                "queueArn": "arn:aws:connect:us-east-1:YOUR_ACCOUNT_ID:instance/PLACEHOLDER/queue/tier2-ops",
                "businessHours": "Mon-Fri 06:00-22:00 America/New_York",
            },
            "slotProposal": {"enabled": False},
            "dmsIntegration": {"enabled": False},
        },
        "escalation": {
            "tier2QueueArn": "arn:aws:connect:us-east-1:YOUR_ACCOUNT_ID:instance/PLACEHOLDER/queue/tier2-ops",
            "businessHours": "Mon-Fri 06:00-22:00 America/New_York",
            "outsideHoursBehavior": "force-roadside-if-p0",
            "roadsideDispatch": {"enabled": False},
        },
        "policies": {
            "dataResidencyRegion": "us-east-1",
            "auditLogRetentionDays": 180,
            "recordingRetentionDays": 30,
            "piiRedaction": {"enabled": True, "fields": ["phoneNumber", "email", "licensePlate"]},
            "budgets": {"perSessionTokenCap": 50000},
        },
        "observability": {"logGroupPrefix": f"/aws/vsa/{tenant_id}"},
        "features": {
            "imageInput": False,
            "agentAssistCoPilot": False,
            "calendarSlotProposal": False,
            "dmsBooking": False,
        },
        "metadata": {
            "createdAt": now,
            "updatedAt": now,
            "updatedBy": "seed-personas.py",
            "notes": f"{display_name} demo persona — segment={segment}.",
        },
    }


# Meridian OEM persona — Meridian brand blue + accent silver, OEM segment so
# find_service_center filters to authorised Meridian dealers.
#
# 2026-08-20: Halcyon -> Meridian throughout this tenant. The customer on this
# tenant (Marcus Reyes) owns a Meridian Trailwind, and every dealer in the service
# network is Meridian-branded, but the greeting told him "Halcyon Owner Connect"
# and offered to route him to his "authorized Halcyon dealer" — so the one string
# the customer actually HEARS named a different manufacturer than the car he owns.
#
# Halcyon is not retired portfolio-wide: it remains a sanctioned fictional name
# from spec `2026-08-03-customer-rebrand` (Ford display -> Halcyon) and is applied
# to prod DynamoDB. It is simply the wrong brand for THIS tenant, whose vehicle is
# a Meridian.
MERIDIAN = _common_block(
    # `meridian`, not `ford` (2026-08-21). This is a lookup key into
    # vsa-<stage>-tenant-config, and `ford` was the last real-company identifier left in
    # the live path — displayName had already become "Meridian Motors" while the key
    # underneath still named a real manufacturer.
    #
    # The rename was blocked until today for a reason worth recording: `custom:tenantId`
    # is an IMMUTABLE Cognito attribute, so every account on the old key had to be deleted
    # and recreated rather than updated. That is why earlier passes changed display names
    # and left the key alone.
    tenant_id="meridian",
    # "Meridian Motors" (2026-08-21, user direction: everything is Meridian, no public
    # companies). Was "Meridian Owner Connect" — a product-line name rather than the
    # company. `Meridian Motors` is already the established company name in the iOS
    # client (SplashView, Info.plist), with "Meridian" as the short form, so this makes
    # the tenant config agree with what the app already shows on launch.
    display_name="Meridian Motors",
    segment="oem",
    primary="#003478",      # Meridian brand blue
    secondary="#9CA3AF",    # neutral silver — accents instead of yellow
    greeting_app="Meridian Owner Connect. Drive confidently — your dealer is one tap away.",
    greeting_voice="Meridian Owner Connect. I can see your vehicle and route you to your authorised Meridian dealer. How can I help?",
    prompt_id="vsa-supervisor-oem-v1",
)

# Waypoint Rentals persona — neutral navigation-theme branding, rental
# segment so find_service_center prefers national chains / quick-service
# (renters want fast turnaround over service depth). Waypoint = navigation
# checkpoint; sibling of Meridian (line of longitude) at portfolio level,
# fictional to keep the public mirror clear of real rental-brand names.
WAYPOINT_RENTALS = _common_block(
    tenant_id="waypoint-rentals",
    display_name="Waypoint Rentals",
    segment="rental",
    primary="#00703C",      # deep navigation green — neutral
    secondary="#F4F4F4",    # neutral light accent
    greeting_app="Waypoint Rentals. Quick help while you're on your trip.",
    greeting_voice="Waypoint Rentals. I'll get you back on the road fast. What's going on?",
    prompt_id="vsa-supervisor-rental-v1",
)

# Vehicle Owner persona — OEM segment; private owners routed to
# brand-authorized dealers. Neutral slate branding keeps it generic
# across OEM co-deployment scenarios.
VEHICLE_OWNER = _common_block(
    tenant_id="vehicle-owner",
    display_name="Vehicle Owner Connect",
    segment="oem",
    primary="#1E3A5F",      # deep navy — neutral OEM anchor
    secondary="#D1D5DB",    # light gray accent
    greeting_app="Vehicle Owner Connect. Your vehicle history and service records, at a tap.",
    greeting_voice="Vehicle Owner Connect. I have your vehicle on file. How can I help today?",
    prompt_id="vsa-supervisor-vehicle-owner-v1",
)

# Service Advisor persona — fleet segment; advisors need broad routing
# across dealer and independent centers to support diverse customer drop-offs.
SERVICE_ADVISOR = _common_block(
    tenant_id="service-advisor",
    display_name="Service Advisor Co-Pilot",
    segment="fleet",
    primary="#374151",      # charcoal — professional, tool-oriented
    secondary="#6B7280",    # medium gray accent
    greeting_app="Service Advisor Co-Pilot. TSBs, recall status, and service history in one place.",
    greeting_voice="Service Advisor Co-Pilot. I have the vehicle history and recall bulletins ready. What do you need?",
    prompt_id="vsa-supervisor-service-advisor-v1",
)

# Technician persona — fleet segment; technicians need access to DTC
# guides, repair procedures, and parts catalog in addition to service history.
TECHNICIAN = _common_block(
    tenant_id="technician",
    display_name="Technician Co-Pilot",
    segment="fleet",
    primary="#111827",      # near-black — industrial / shop environment
    secondary="#4B5563",    # dark gray accent
    greeting_app="Technician Co-Pilot. DTC guides, repair procedures, and parts lookup.",
    greeting_voice="Technician Co-Pilot. Vehicle is on the lift. What are we diagnosing?",
    prompt_id="vsa-supervisor-technician-v1",
)


#: A seeded value containing any of these is a placeholder, not configuration.
#: `seed-personas.py` ships placeholder Connect coordinates so a fresh account
#: seeds cleanly, but those placeholders are what made escalation fail with
#: `Resource not found of Type: INSTANCE` for as long as the feature existed —
#: the Lambda parsed instance id `PLACEHOLDER` and Connect correctly rejected it.
#: See issues/2026-09-22-escalation-connect-config-is-placeholder/.
_PLACEHOLDER_MARKERS = ("PLACEHOLDER", "YOUR_ACCOUNT_ID")

#: SSM parameters holding the real Connect coordinates, one set per stage.
#: SSM rather than literals in this file for two reasons: the values are
#: account- and stage-specific, and the underlying Connect resources are named
#: after the tenant — writing those names here would put a brand string back
#: into git, which is the class of thing the publish scanner exists to catch.
#: Same Tier-B pattern as scripts/seed-stage-parameters.sh.
_CONNECT_SSM_PARAMS = {
    "instanceArn": "connect-instance-arn",
    "contactFlowId": "connect-contact-flow-id",
    "tier2QueueArn": "connect-tier2-queue-arn",
}


def _is_placeholder(value) -> bool:
    """True when a value is absent or still carries a placeholder marker."""
    if not isinstance(value, str) or not value.strip():
        return True
    return any(marker in value for marker in _PLACEHOLDER_MARKERS)


def _resolve_connect_from_ssm(stage: str, region: str) -> dict[str, str]:
    """Read real Connect coordinates from SSM, or return {} if unavailable.

    Returns {} rather than raising on ANY failure — missing parameters, no
    credentials, no network. A seeder that cannot reach SSM must still be able
    to seed the other 40-odd fields of every tenant; the Connect block then
    falls through to preserve-existing (see `_apply_connect_config`).

    All-or-nothing on purpose: a partial set would write one real value beside
    two placeholders, which reads as configured and is not.
    """
    try:
        ssm = boto3.client("ssm", region_name=region)
        resolved: dict[str, str] = {}
        for field, suffix in _CONNECT_SSM_PARAMS.items():
            name = f"/cvx/{stage}/{suffix}"
            value = ssm.get_parameter(Name=name)["Parameter"]["Value"].strip()
            if _is_placeholder(value):
                print(f"  connect: {name} is itself a placeholder — ignoring SSM")
                return {}
            resolved[field] = value
        return resolved
    except Exception as e:  # noqa: BLE001 — any failure means "fall through"
        print(f"  connect: SSM lookup unavailable ({type(e).__name__}) — will preserve existing")
        return {}


def _apply_connect_config(tenant_config: dict, ddb, overrides: dict[str, str]) -> None:
    """Fill the Connect block from SSM, else PRESERVE what is already stored.

    The bug this exists to prevent: this script runs on every
    `make deploy-staging`, and `_put` replaces the whole row. So a row that an
    operator had configured correctly was silently reset to `PLACEHOLDER` by the
    next deploy — escalation would work, then stop, with nothing in the deploy
    output naming the cause.

    Precedence is SSM > existing stored value > placeholder. Placeholder is only
    ever written when there is nothing better, so a fresh account still seeds.

    Mutates `tenant_config` in place; the caller passes a deep copy.
    """
    channels = tenant_config.setdefault("channels", {}).setdefault("connect", {})
    escalation = tenant_config.setdefault("escalation", {})
    warm = tenant_config.setdefault("booking", {}).setdefault("warmTransfer", {})

    if overrides:
        channels["instanceArn"] = overrides["instanceArn"]
        channels["contactFlowId"] = overrides["contactFlowId"]
        escalation["tier2QueueArn"] = overrides["tier2QueueArn"]
        # The escalate Lambda falls back to warmTransfer.queueArn when
        # escalation.tier2QueueArn is absent (api-escalate/handler.py:149), so
        # leaving this a placeholder would leave a live fallback path broken.
        warm["queueArn"] = overrides["tier2QueueArn"]
        print(f"  connect: applied from SSM (/cvx/{STAGE}/connect-*)")
        return

    # No SSM. Carry forward any stored value that is not a placeholder.
    try:
        existing = ddb.get_item(
            Key={"tenantId": tenant_config["tenantId"], "version": tenant_config["version"]}
        ).get("Item")
    except Exception as e:  # noqa: BLE001
        print(f"  connect: cannot read existing row ({type(e).__name__}) — writing as-is")
        return
    if not existing:
        return

    preserved = []
    for container, key, stored in (
        (channels, "instanceArn", (existing.get("channels") or {}).get("connect", {})),
        (channels, "contactFlowId", (existing.get("channels") or {}).get("connect", {})),
        (escalation, "tier2QueueArn", existing.get("escalation") or {}),
        (warm, "queueArn", ((existing.get("booking") or {}).get("warmTransfer") or {})),
    ):
        stored_value = stored.get(key)
        # Only preserve over a placeholder — never let a stale stored value
        # shadow a real one this script is deliberately updating.
        if not _is_placeholder(stored_value) and _is_placeholder(container.get(key)):
            container[key] = stored_value
            preserved.append(key)
    if preserved:
        print(f"  connect: preserved existing {', '.join(sorted(set(preserved)))} (no SSM override)")


#: Top-level blocks this script does NOT define, but other seeders do write.
#: `_put` replaces the whole row, so without a carry-forward every deploy
#: destroys them.
#:
#: `acquire` holds the iOS Home tab's upgrade offers and the owner's existing
#: finance, written by CMS `deployment/scripts/seed_tenant_acquire_offers.py`.
#: `UpgradeOfferBanner` hides itself when the field is absent and has no empty
#: state, so the symptom is simply that the offer stops appearing, with nothing
#: in the deploy output naming the cause — the same shape as the Connect
#: PLACEHOLDER regression above.
_UNMANAGED_BLOCKS = ("acquire",)


def _preserve_unmanaged_blocks(item: dict, ddb) -> None:
    """Carry forward top-level blocks this script does not own.

    Precedence differs from `_apply_connect_config` on purpose. There, both
    sides hold a real value and the rule is placeholder-vs-real. Here this
    script has NO opinion at all, so the rule is simply "if the stored row has
    it and we do not, keep it". Should this script ever start defining one of
    these blocks, its value wins — same principle as Connect: a stored value
    never shadows a deliberate update.

    Operates on the DDB-ready `item`, AFTER `_put`'s `json.dumps` round-trip,
    which matters: a block read back from DynamoDB contains `Decimal` values
    and `json.dumps` raises `TypeError` on those. Injecting here also keeps the
    block out of `jsonschema.validate`, which is correct — the schema does not
    describe `acquire` and must not start rejecting it.

    Mutates `item` in place.
    """
    missing = [b for b in _UNMANAGED_BLOCKS if b not in item]
    if not missing:
        return
    try:
        existing = ddb.get_item(
            Key={"tenantId": item["tenantId"], "version": item["version"]}
        ).get("Item")
    except Exception as e:  # noqa: BLE001
        print(f"  unmanaged: cannot read existing row ({type(e).__name__}) — writing as-is")
        return
    if not existing:
        return

    carried = []
    for block in missing:
        stored = existing.get(block)
        # Falsy covers both absent and an empty map; neither is worth carrying.
        if stored:
            item[block] = stored
            carried.append(block)
    if carried:
        print(f"  preserved unmanaged block(s): {', '.join(sorted(carried))}")


def _put(tenant_config: dict, ddb, connect_overrides: dict[str, str] | None = None) -> None:
    # Deep-copy before mutating: the configs are module-level singletons and
    # `validate_all` already ran against them.
    tenant_config = json.loads(json.dumps(tenant_config))
    _apply_connect_config(tenant_config, ddb, connect_overrides or {})
    jsonschema.validate(tenant_config, SCHEMA)
    item = json.loads(json.dumps(tenant_config), parse_float=Decimal)
    # After the round-trip, before the write. See the docstring for why the
    # order is load-bearing rather than incidental.
    _preserve_unmanaged_blocks(item, ddb)
    ddb.put_item(Item=item)
    ddb.put_item(
        Item={
            "tenantId": tenant_config["tenantId"],
            "version": "current",
            "activeVersion": tenant_config["version"],
        }
    )
    print(
        f"Seeded {tenant_config['tenantId']} v{tenant_config['version']} "
        f"(segment={tenant_config['segment']}) -> {TABLE}"
    )


ALL_CONFIGS = (MERIDIAN, WAYPOINT_RENTALS, VEHICLE_OWNER, SERVICE_ADVISOR, TECHNICIAN)


def validate_all(configs=ALL_CONFIGS) -> list[str]:
    """Validate every config against the schema and return ALL error strings.

    Separated from `_put` and run before any write, because this loop used to be
    ordered, write-as-you-go, and not fault-tolerant: the first schema violation
    raised and aborted the process, so earlier tenants were already committed to
    DynamoDB and every later tenant was silently never seeded. That is exactly
    how `vehicle_owner`'s underscored tenantId (illegal under the schema's
    `^[a-z0-9-]{3,32}$` pattern) left staging with only `ford` and the pre-rename
    rental tenant seeded for roughly six weeks, with `service_advisor` and
    `technician` collateral damage purely because of their position in the
    tuple. See
    issues/2026-08-02-seed-personas-underscore-tenantid-truncates-seed/.

    The same ordered-and-not-fault-tolerant shape had already caused an identical
    partial-seed in `seed-persona-users.py` (see its 2026-07-28 note), so this is
    a repeat structural failure, not a one-off.

    Validating up front turns a half-applied seed into a no-op that names every
    offender at once.
    """
    errors: list[str] = []
    for cfg in configs:
        try:
            jsonschema.validate(cfg, SCHEMA)
        except jsonschema.ValidationError as exc:
            errors.append(
                f"  {cfg.get('tenantId', '<no tenantId>')}: "
                f"{exc.message} (at {'/'.join(str(p) for p in exc.absolute_path) or 'root'})"
            )
    return errors


def main() -> int:
    # Fail before the first write, and report every offender — not just the first.
    errors = validate_all()
    if errors:
        print(
            f"❌ {len(errors)} tenant config(s) fail {SCHEMA_NAME}; nothing was written:",
            file=sys.stderr,
        )
        for err in errors:
            print(err, file=sys.stderr)
        print(
            "\n   tenantId must match ^[a-z0-9-]{3,32}$ — kebab-case, no underscores.\n"
            "   Note that a persona_id (snake_case, e.g. 'vehicle_owner') is NOT a\n"
            "   tenantId; reusing one verbatim is what caused this class of failure.",
            file=sys.stderr,
        )
        return 1

    ddb = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    # Resolved ONCE, not per tenant: three SSM reads instead of 3*N, and every
    # tenant then gets the same Connect coordinates or the same fall-through.
    connect_overrides = _resolve_connect_from_ssm(STAGE, REGION)
    if not connect_overrides:
        print(
            f"ℹ️  No Connect coordinates in SSM (/cvx/{STAGE}/connect-*). Existing\n"
            "   non-placeholder values will be PRESERVED rather than reset to\n"
            "   PLACEHOLDER. To configure escalation, set those three parameters."
        )
    for config in ALL_CONFIGS:
        _put(config, ddb, connect_overrides)
    return 0


if __name__ == "__main__":
    sys.exit(main())
