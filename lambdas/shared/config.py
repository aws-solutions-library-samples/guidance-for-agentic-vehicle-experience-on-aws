"""
Centralized configuration for the Virtual Fleet Operator (VFO).

All CMS table names, endpoints, and resource identifiers are derived from
two environment variables:

    CMS_STAGE       — the CMS deployment stage (default: "dev")
    AWS_REGION      — the AWS region (default: "us-east-1")

Lambda handlers, the agent supervisor, and scripts all import from here
rather than hardcoding table names.
"""

import os

# ─── Core settings ───────────────────────────────────────────────────────────
CMS_STAGE = os.environ.get("CMS_STAGE", "dev")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
VSA_STAGE = os.environ.get("VSA_STAGE", "prod")

# ─── CMS DynamoDB tables (deployed by the Connected Mobility repo) ───────────
CMS_VEHICLES_TABLE = os.environ.get(
    "CMS_VEHICLES_TABLE", f"cms-{CMS_STAGE}-storage-vehicles"
)
CMS_DRIVERS_TABLE = os.environ.get(
    "CMS_DRIVERS_TABLE", f"cms-{CMS_STAGE}-storage-drivers"
)
CMS_TRIPS_TABLE = os.environ.get(
    "CMS_TRIPS_TABLE", f"cms-{CMS_STAGE}-storage-trips"
)
CMS_SERVICE_HISTORY_TABLE = os.environ.get(
    "CMS_SERVICE_HISTORY_TABLE", f"cms-{CMS_STAGE}-storage-service-history"
)
CMS_SAFETY_EVENTS_TABLE = os.environ.get(
    "CMS_SAFETY_EVENTS_TABLE", f"cms-{CMS_STAGE}-storage-safety-events"
)
CMS_DTC_HISTORY_TABLE = os.environ.get(
    "CMS_DTC_HISTORY_TABLE", f"cms-{CMS_STAGE}-storage-dtc-history"
)
CMS_VFO_ACTION_QUEUE_TABLE = os.environ.get(
    "RSA_QUEUE_TABLE", f"cms-{CMS_STAGE}-vfo-action-queue"
)

# ─── CMS Redis (vehicle live state) ─────────────────────────────────────────
REDIS_ENDPOINT = os.environ.get("REDIS_ENDPOINT", "")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))

# ─── GSI names ───────────────────────────────────────────────────────────────
CMS_SAFETY_EVENTS_GSI = os.environ.get(
    "CMS_SAFETY_EVENTS_GSI", "vehicleId-timestamp-index"
)
CMS_TRIPS_GSI = os.environ.get(
    "CMS_TRIPS_GSI", "vehicleId-startTime-index"
)
