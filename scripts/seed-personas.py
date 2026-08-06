"""Seed the Halcyon (OEM), Enterprise (Rental), and expansion-persona tenant configs.

These are sister configs to seed-michelin.py, each adjusted for their
persona segment so the agent's find_service_center filter and the
voice prompt's narration block route correctly:

  ford           -> segment=oem    (route to authorized Halcyon dealers)
  enterprise     -> segment=rental (route to national chains / quick-service)
  vehicle-owner  -> segment=oem    (dealer-network steering for private owners)
  service-advisor -> segment=fleet  (broad service routing for advisors)
  technician     -> segment=fleet  (broad service routing for technicians)

The configs reuse the Michelin shape so existing readers (tenant-config
loader, escalate handler, voice agent) work identically. Branding
colors, display names, greetings, and the supervisor prompt id are the
fields that actually change. Connect/Cognito/Lex ARNs stay placeholder
because the demo deploys all three personas against the same backend
infrastructure for now.

Usage:
  STAGE=prod AWS_REGION=us-east-1 python scripts/seed-personas.py

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

STAGE = os.environ.get("STAGE", "prod")
REGION = os.environ.get("AWS_REGION", "us-east-1")
TABLE = f"vsa-{STAGE}-tenant-config"

now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _common_block(*, tenant_id: str, display_name: str, segment: str,
                  primary: str, secondary: str, greeting_app: str,
                  greeting_voice: str, prompt_id: str) -> dict:
    """Return a tenant-config record matching the Michelin schema, with
    the persona-specific bits dropped in. Channels/Connect/escalation
    point at the same demo infra placeholders Michelin uses; in a real
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


# Halcyon OEM persona — Halcyon brand blue + accent silver, OEM segment so
# find_service_center filters to authorized Halcyon dealers.
FORD = _common_block(
    tenant_id="ford",
    display_name="Halcyon Owner Connect",
    segment="oem",
    primary="#003478",      # Halcyon brand blue
    secondary="#9CA3AF",    # neutral silver — accents instead of yellow
    greeting_app="Halcyon Owner Connect. Drive confidently — your dealer is one tap away.",
    greeting_voice="Halcyon Owner Connect. I can see your vehicle and route you to your authorized Halcyon dealer. How can I help?",
    prompt_id="vsa-supervisor-oem-v1",
)

# Enterprise Rental persona — Enterprise green + soft accent, rental
# segment so find_service_center prefers national chains / quick-service
# (renters want fast turnaround over service depth).
ENTERPRISE = _common_block(
    tenant_id="enterprise",
    display_name="Enterprise Connect",
    segment="rental",
    primary="#00703C",      # Enterprise green
    secondary="#F4F4F4",    # neutral light accent
    greeting_app="Enterprise Connect. Quick help while you're on your trip.",
    greeting_voice="Enterprise Connect. I'll get you back on the road fast. What's going on?",
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


def _put(tenant_config: dict, ddb) -> None:
    jsonschema.validate(tenant_config, SCHEMA)
    item = json.loads(json.dumps(tenant_config), parse_float=Decimal)
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


ALL_CONFIGS = (FORD, ENTERPRISE, VEHICLE_OWNER, SERVICE_ADVISOR, TECHNICIAN)


def validate_all(configs=ALL_CONFIGS) -> list[str]:
    """Validate every config against the schema and return ALL error strings.

    Separated from `_put` and run before any write, because this loop used to be
    ordered, write-as-you-go, and not fault-tolerant: the first schema violation
    raised and aborted the process, so earlier tenants were already committed to
    DynamoDB and every later tenant was silently never seeded. That is exactly
    how `vehicle_owner`'s underscored tenantId (illegal under the schema's
    `^[a-z0-9-]{3,32}$` pattern) left staging with only `ford` and `enterprise`
    for roughly six weeks, with `service_advisor` and `technician` collateral
    damage purely because of their position in the tuple. See
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
    for config in ALL_CONFIGS:
        _put(config, ddb)
    return 0


if __name__ == "__main__":
    sys.exit(main())
