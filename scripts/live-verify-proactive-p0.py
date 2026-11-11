#!/usr/bin/env python3
"""Live check: a proactive P0 alert reaches the driver by voice (watcher path)
and by push (APNs path).

Spec `.kiro/specs/2026-11-05-avx-proactive-p0-alert/` (§ V voice, § P push).

Run only with LIVE_TESTS=1.  ``--vehicle-id`` is required; never exercises
VEH-MRDN-0015 or VIN MRDN0000000000015 (held for field-video demos).

Run order
---------
``voice`` must run **before** deploying ``vsa-staging-tier2``.  Once
``vsa-staging-tier2`` is deployed the watcher runs on a schedule and can
write ``agent-2-vehicle-health`` Findings during the ``voice`` window,
which would interfere with ``push``'s Finding-wait loop and the residual
Finding check.  Run ``push`` **after** ``vsa-staging-tier2`` is deployed,
and before the iOS Simulator app registers the APNs device token (or with
the app signed out) — otherwise register-device returns ``endpoint_arn=None``.

Preconditions on the test car (both subcommands refuse before any write):
no ``cms-staging-storage-drivers`` row names it as ``assignedVehicleId`` (a
second assigned driver leaves the p0 Finding with no ``owner_id``, so the
broker skips it silently); no ACTIVE CRITICAL/HIGH ``dtc-history`` row (it
changes the expected directive); neither its id nor its resolved VIN is the
field-video car's.

Subcommands
-----------
voice   Open a bidi session as a throwaway driver on the test car, wait 12 s
        for the watcher to establish its baseline, seed one ACTIVE C1234 row
        (unique source label per run), and within 30 s require:
          - ``transcript`` whose text == ``p0_directive(faults_for_codes(["C1234"]), name)``
          - ``classification`` value=P0
          - model transcript leading with the directive OR ``platform.speak``
            with the same text
        Prints the session's ``proactive-p0:`` runtime log lines and asserts
        ``proactive-p0: alerted codes=`` is present (not the no-table fallback)
        and ``severity table unavailable`` is absent.
        With ``--open-reason p0_push``: seeds C1234 *before* opening the
        session, opens with the ``X-Amzn-Bedrock-AgentCore-Runtime-Custom-OpenReason``
        header, and additionally asserts the ``bidi: openReason=p0_push`` log
        line.  The watcher skips its baseline on this path and alerts on the
        first poll.
        Cleanup in one try/finally with signals held: deletes only the
        Cognito user, CMS driver row, and DTC row named in the manifest.

push    Register an APNs device token through the register-device API, seed
        ACTIVE C1234 on the test car, wait ≤7 min for a p0 Finding from
        agent-2-vehicle-health, then wait ≤2 min for the broker ledger row to
        reach ``published``.  Requires ``--device-token`` (APNs hex token from
        the iOS Simulator).  Cleanup deletes the DTC row, SNS endpoint(s) (by
        device row and by ``CustomUserData`` on the platform application),
        notification-devices row, driver row, and Cognito user.

Usage::

    LIVE_TESTS=1 python3 scripts/live-verify-proactive-p0.py voice \\
        --vehicle-id VEH-TEST-001

    LIVE_TESTS=1 python3 scripts/live-verify-proactive-p0.py voice \\
        --vehicle-id VEH-TEST-001 --open-reason p0_push

    LIVE_TESTS=1 python3 scripts/live-verify-proactive-p0.py push \\
        --vehicle-id VEH-TEST-001 --device-token <hex-token>

Exit codes: 0 all checks passed · 1 a check failed or an AWS call errored ·
            2 skipped (no LIVE_TESTS) or push not exercised (no APNs secret,
            no register-device function, or no platform application ARN) ·
            143 interrupted (SIGTERM/SIGHUP).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import requests as _http
import secrets
import signal
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import boto3
from botocore.exceptions import ClientError as _BotoClientError
import websockets
import yaml
from websockets.asyncio.client import connect as ws_connect

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from agents.supervisor import dtc_response_catalog as cat  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STAGE = "staging"
REGION = "us-west-2"

#: Live-test identity guards: never touch the field-video demo vehicle.
BANNED_VEHICLE_IDS: frozenset[str] = frozenset({"VEH-MRDN-0015"})
BANNED_VINS: frozenset[str] = frozenset({"MRDN0000000000015"})

MANIFEST_DIR: Path = Path.home() / ".cache" / "avx-live-verify" / "proactive-p0"

#  lv-pp0-20261106T120000Z-a1b2c3
RUN_ID_RE = re.compile(r"^lv-pp0-\d{8}T\d{6}Z-[0-9a-f]{6}$")

#: Seconds after session open before seeding the DTC (lets the watcher
#: establish its baseline — one 10-second poll after establishment).
_BASELINE_WAIT_S: float = 12.0

#: Seconds to wait for the P0 alert events after seeding the DTC.
_ALERT_WAIT_S: float = 30.0


# ---------------------------------------------------------------------------
# Signal handling (SIGTERM/SIGHUP → SystemExit(143) so ``finally`` runs)
# ---------------------------------------------------------------------------

def _install_signal_handlers() -> None:
    def _term(signum: int, frame: object) -> None:  # noqa: ARG001
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGHUP, _term)


@contextmanager
def _signals_held() -> Iterator[None]:
    """Hold SIGINT/SIGTERM/SIGHUP so cleanup is never interrupted mid-flight."""
    held: list[int] = []

    def _hold(signum: int, frame: object) -> None:  # noqa: ARG001
        held.append(signum)

    sigs = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    old = {s: signal.getsignal(s) for s in sigs}
    for s in sigs:
        signal.signal(s, _hold)
    try:
        yield
    finally:
        for s, handler in old.items():
            signal.signal(s, handler)
        if held:
            if held[0] == signal.SIGINT:
                raise KeyboardInterrupt
            raise SystemExit(143)


# ---------------------------------------------------------------------------
# Manifest (0700 dir, 0600 file, atomic write)
# ---------------------------------------------------------------------------

def new_run_id(now: datetime | None = None) -> str:
    ts = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return f"lv-pp0-{ts}-{secrets.token_hex(3)}"


def validate_run_id(run_id: str) -> str:
    if not RUN_ID_RE.match(run_id or ""):
        raise SystemExit(f"refusing: run_id {run_id!r} does not match {RUN_ID_RE.pattern}")
    return run_id


def _manifest_path(run_id: str, base: Path | None = None) -> Path:
    return (base or MANIFEST_DIR) / f"{validate_run_id(run_id)}.json"


def save_manifest(m: dict[str, Any], base: Path | None = None) -> Path:
    """Atomic 0600 write in a 0700 directory."""
    path = _manifest_path(m["run_id"], base)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(m, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# Refusal helpers (called before any AWS client is built)
# ---------------------------------------------------------------------------

def _refuse(msg: str) -> None:
    print(f"REFUSED: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _guard_vehicle(vehicle_id: str | None, vin: str | None = None) -> None:
    if not vehicle_id:
        _refuse("--vehicle-id is required; pass a test car, not VEH-MRDN-0015")
    if vehicle_id in BANNED_VEHICLE_IDS:
        _refuse(f"{vehicle_id} is the field-video demo car — use a test car")
    if vin and vin in BANNED_VINS:
        _refuse(f"VIN {vin} belongs to the field-video demo car")


def _guard_stage(stage: str) -> None:
    if stage != STAGE:
        _refuse(f"only --stage staging is allowed (got {stage!r})")


# ---------------------------------------------------------------------------
# ISO-8601 timestamp comparison helper (W2)
# ---------------------------------------------------------------------------

def _computed_at_ge(computed_at: str, since: datetime) -> bool:
    """Return True iff the ISO-8601 *computed_at* string represents a time >= *since*.

    Compares client-side so the result is independent of ISO format variations
    (e.g. ``+00:00`` vs ``Z`` suffix) that would defeat a lexicographic
    server-side DynamoDB filter.
    Returns False for absent, empty, or unparseable values.
    """
    if not computed_at:
        return False
    try:
        dt = datetime.fromisoformat(computed_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt >= since
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# CloudWatch: collect proactive-p0: log lines for the session
# ---------------------------------------------------------------------------

def _runtime_log_lines(region: str, arn: str, session_id: str, since_ms: int) -> list[str]:
    runtime_id = arn.rsplit("/", 1)[1]
    logs = boto3.client("logs", region_name=region)
    group = f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"
    out: list[str] = []
    kwargs: dict[str, Any] = {
        "logGroupName": group,
        "startTime": since_ms,
        "filterPattern": f'"{session_id}"',
    }
    while True:
        try:
            resp = logs.filter_log_events(**kwargs)
        except logs.exceptions.ResourceNotFoundException:
            return out
        out.extend(e["message"].strip() for e in resp.get("events", []))
        if not resp.get("nextToken"):
            return out
        kwargs["nextToken"] = resp["nextToken"]


# ---------------------------------------------------------------------------
# OpenReason header (W4)
# ---------------------------------------------------------------------------

#: Header name that bidi_app._establish_session reads as openReason.
_OPEN_REASON_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-OpenReason"


def _apply_open_reason_header(headers: dict[str, str], open_reason: str | None) -> None:
    """Add the OpenReason header to *headers* when ``open_reason`` is ``'p0_push'``.

    Only ``'p0_push'`` is a known-open reason; other values are silently
    dropped by ``_establish_session`` and are not sent.
    """
    if open_reason == "p0_push":
        headers[_OPEN_REASON_HEADER] = "p0_push"


# ---------------------------------------------------------------------------
# WebSocket frame decoder (suggestion: skip non-JSON frames)
# ---------------------------------------------------------------------------

def _try_decode_frame(raw: str) -> "dict[str, Any] | None":
    """Decode a WebSocket frame as JSON; return ``None`` for non-JSON frames.

    Non-JSON frames (WebSocket PING/PONG, binary chunks, protocol frames)
    are skipped by the caller with ``continue`` so that event collection
    survives them instead of raising and ending the receive loop silently.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Log-line checks (W4 + suggestion: assert alerted / table-unavailable)
# ---------------------------------------------------------------------------

def _evaluate_p0_log_lines(
    p0_log_lines: list[str],
    log_lines: list[str],
    open_reason: str | None,
) -> "dict[str, bool]":
    """Derive boolean pass/fail checks from the session's runtime log lines.

    *p0_log_lines*: lines containing ``proactive-p0:``.
    *log_lines*: all lines for the session (superset of *p0_log_lines*).
    *open_reason*: ``args.open_reason`` (``'p0_push'`` or ``None``).

    Returns a dict suitable for merging into the main ``checks`` dict.
    """
    result: dict[str, bool] = {}
    result["proactive-p0: alerted logged"] = any(
        "proactive-p0: alerted codes=" in ln for ln in p0_log_lines
    )
    result["severity table unavailable absent"] = not any(
        "severity table unavailable" in ln for ln in p0_log_lines
    )
    if open_reason == "p0_push":
        result["bidi: openReason=p0_push logged"] = any(
            "openReason=p0_push" in ln for ln in log_lines
        )
    return result


# ---------------------------------------------------------------------------
# Pre-write guard: refuse a car with standing CRITICAL/HIGH faults (W3)
# ---------------------------------------------------------------------------

def _guard_active_critical_dtc(vehicle_id: str, dtc_table: Any) -> None:
    """Refuse if the car has any ACTIVE CRITICAL or HIGH dtc-history rows.

    Such rows change the directive text the exact-text check expects and can
    cause the watcher to alert on a code other than the seeded C1234.
    Called *before* any write so the run leaves no trace on refusal.
    """
    resp = dtc_table.query(
        KeyConditionExpression="vehicleId = :v",
        FilterExpression=(
            "#st = :active AND (severity = :crit OR severity = :high)"
        ),
        ExpressionAttributeNames={"#st": "status"},
        ExpressionAttributeValues={
            ":v": vehicle_id,
            ":active": "ACTIVE",
            ":crit": "CRITICAL",
            ":high": "HIGH",
        },
    )
    rows = resp.get("Items", [])
    if rows:
        _refuse(
            f"car {vehicle_id} has {len(rows)} ACTIVE CRITICAL/HIGH DTC row(s); "
            f"use a car with no standing faults to avoid directive text mismatch"
        )


# ---------------------------------------------------------------------------
# Pre-write guard: refuse a car already assigned to a driver (W3)
# ---------------------------------------------------------------------------

def _guard_no_assigned_drivers(vehicle_id: str, drivers_table: Any) -> None:
    """Refuse if any existing drivers row already names this car as assignedVehicleId.

    The owner resolver skips vehicles with multiple assigned drivers (sets no
    owner_id on the Finding).  A pre-existing row therefore causes the p0
    Finding to be broker-skipped silently — the run waits 9 min and fails on
    the ledger check with no indication of why.  This guard fires before any
    write so the run leaves no trace on refusal.

    The scan is paged (ExclusiveStartKey) to match the resolver's full-table
    pass. Projection minimises read cost.
    """
    kwargs: dict[str, Any] = {
        "FilterExpression": "assignedVehicleId = :v",
        "ExpressionAttributeValues": {":v": vehicle_id},
        "ProjectionExpression": "driverId, assignedVehicleId, cognitoEmail",
    }
    conflicting: list[dict] = []
    while True:
        resp = drivers_table.scan(**kwargs)
        conflicting.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    if conflicting:
        emails = [r.get("cognitoEmail", "?") for r in conflicting]
        _refuse(
            f"car {vehicle_id} is already assigned to driver(s) "
            f"{', '.join(emails)}; "
            "run voice and push only on a car with no existing assigned drivers "
            "(a second assignedVehicleId causes the p0 Finding to have no owner_id "
            "and the broker to skip it silently)"
        )


# ---------------------------------------------------------------------------
# Post-cleanup residual-findings check (W3)
# ---------------------------------------------------------------------------

def _check_residual_findings(
    findings_table: Any,
    vin: str,
    started_ms: int,
    manifest: dict[str, Any],
) -> None:
    """Query for lingering agent-2-vehicle-health Findings written after *started_ms*.

    Filters client-side on ``computed_at`` (ISO-8601) so the comparison is
    correct regardless of the writer's ISO format variant.  DynamoDB
    lexicographic comparison of ISO strings is unreliable across format
    variants, so no server-side timestamp filter is used.

    Reports any found to stderr and records their IDs in
    ``manifest["residual_finding_ids"]``.  Does NOT delete them — the spec
    requires recording, not deleting, these rows because the push run may
    legitimately need them.
    """
    run_start = datetime.fromtimestamp(started_ms / 1000, tz=timezone.utc)
    try:
        resp = findings_table.query(
            IndexName=_PUSH_FINDINGS_GSI,
            KeyConditionExpression="vin = :v",
            FilterExpression="agent_id = :a",
            ExpressionAttributeValues={
                ":v": vin,
                ":a": _PUSH_AGENT_ID,
            },
        )
    except Exception as e:  # noqa: BLE001
        print(f"WARN: residual-findings query failed: {e}", file=sys.stderr)
        return
    residuals = [
        r for r in resp.get("Items", [])
        if _computed_at_ge(r.get("computed_at", ""), run_start)
    ]
    if not residuals:
        return
    print(
        f"WARN: {len(residuals)} residual agent-2-vehicle-health Finding(s) "
        f"for VIN {vin} remain after cleanup (computed_at after run start):",
        file=sys.stderr,
    )
    ids: list[str] = []
    for r in residuals:
        fid = str(r.get("finding_id", "?"))
        sev = r.get("severity", "?")
        print(f"  finding_id={fid} severity={sev}", file=sys.stderr)
        ids.append(fid)
    manifest["residual_finding_ids"] = ids


# ---------------------------------------------------------------------------
# Cleanup (called from the finally block regardless of how we exit)
# ---------------------------------------------------------------------------

def _voice_cleanup(
    *,
    idp: Any,
    pool: str,
    user_email: str | None,
    drivers_table: Any,
    driver_id: str | None,
    dtc_table: Any,
    vehicle_id: str,
    dtc_ts_ms: int | None,
    dtc_source: str,
) -> None:
    """Delete only the rows / users this run created (manifest-keyed cleanup)."""
    if user_email:
        try:
            idp.admin_delete_user(UserPoolId=pool, Username=user_email)
            print(f"deleted cognito user {user_email}")
        except Exception as e:  # noqa: BLE001
            print(f"WARN: could not delete cognito user {user_email}: {e}",
                  file=sys.stderr)
            print(f"  manual cleanup: aws cognito-idp admin-delete-user "
                  f"--user-pool-id {pool} --username {user_email}", file=sys.stderr)

    if driver_id and drivers_table is not None:
        try:
            drivers_table.delete_item(
                Key={"driverId": driver_id},
                ConditionExpression="#src = :s",
                ExpressionAttributeNames={"#src": "source"},
                ExpressionAttributeValues={":s": dtc_source},
            )
            print(f"deleted driver row {driver_id}")
        except Exception as e:  # noqa: BLE001
            print(f"WARN: could not delete driver row {driver_id}: {e}",
                  file=sys.stderr)
            print(
                f"  manual: aws dynamodb delete-item "
                f"--table-name cms-{STAGE}-storage-drivers "
                f'--key \'{{"driverId":{{"S":"{driver_id}"}}}}\' '
                f"--condition-expression '#src = :s' "
                f"--expression-attribute-names '{{\"#src\":\"source\"}}' "
                f"--expression-attribute-values "
                f"'{{\":s\":{{\"S\":\"{dtc_source}\"}}}}'",
                file=sys.stderr,
            )

    if dtc_ts_ms is not None and dtc_table is not None:
        try:
            dtc_table.delete_item(
                Key={"vehicleId": vehicle_id, "timestamp": dtc_ts_ms},
                ConditionExpression="#src = :s",
                ExpressionAttributeNames={"#src": "source"},
                ExpressionAttributeValues={":s": dtc_source},
            )
            print(f"deleted DTC row vehicleId={vehicle_id} ts={dtc_ts_ms}")
        except Exception as e:  # noqa: BLE001
            print(f"WARN: could not delete DTC row ts={dtc_ts_ms}: {e}",
                  file=sys.stderr)
            print(f"  manual: aws dynamodb delete-item "
                  f"--table-name cms-{STAGE}-storage-dtc-history "
                  f'--key \'{{"vehicleId":{{"S":"{vehicle_id}"}},'
                  f'"timestamp":{{"N":"{dtc_ts_ms}"}}}}\' '
                  f"--condition-expression '#src = :s' "
                  f"--expression-attribute-names '{{\"#src\":\"source\"}}' "
                  f"--expression-attribute-values '{{\":s\":{{\"S\":\"{dtc_source}\"}}}}'",
                  file=sys.stderr)


# ---------------------------------------------------------------------------
# voice subcommand
# ---------------------------------------------------------------------------

async def _voice_run(args: argparse.Namespace) -> int:
    """Voice watcher live-verify: seed C1234, expect directive within 30 s."""
    region, stage = REGION, STAGE
    run_id = args.run_id or new_run_id()
    validate_run_id(run_id)

    # ── State for cleanup — initialised to sentinel values so ``finally``
    #    has something to call even if we raise before any writes.
    vehicle_id: str = args.vehicle_id
    pool: str = ""
    user_email: str = ""
    driver_id: str = ""
    dtc_source: str = f"lv-pp0-init-{secrets.token_hex(3)}"
    dtc_ts_ms: int | None = None
    cognito_created = False
    driver_row_created = False
    idp: Any = None
    dtc_table: Any = None
    drivers_table: Any = None
    findings_table: Any = None      # for residual-findings check in finally
    vin: str | None = None          # resolved inside try; needed in finally
    started_ms: int = 0             # set just before session open
    manifest: dict[str, Any] = {}   # populated inside try; read in finally

    try:
        idp = boto3.client("cognito-idp", region_name=region)

        # ── pool / CloudFormation API URL ──────────────────────────────────
        pool = (boto3.client("ssm", region_name=region)
                .get_parameter(Name=f"/cvx/{stage}/voice-auth-user-pool-id")
                ["Parameter"]["Value"].strip())
        outs = (boto3.client("cloudformation", region_name=region)
                .describe_stacks(StackName=f"vsa-{stage}-api")
                ["Stacks"][0]["Outputs"])
        _base_url = next(o["OutputValue"].rstrip("/") for o in outs  # noqa: F841
                         if o["OutputKey"] == "RestApiUrl")

        # ── App client that supports USER_PASSWORD_AUTH ────────────────────
        client_id = next(
            c["ClientId"]
            for c in idp.list_user_pool_clients(
                UserPoolId=pool, MaxResults=60)["UserPoolClients"]
            if "ALLOW_USER_PASSWORD_AUTH" in (
                idp.describe_user_pool_client(
                    UserPoolId=pool, ClientId=c["ClientId"])
                ["UserPoolClient"].get("ExplicitAuthFlows") or []))

        # ── Clone meridian.driver's groups and non-vehicle claims ──────────
        md = idp.admin_get_user(UserPoolId=pool,
                                Username="meridian.driver@example.com")
        claims = {a["Name"]: a["Value"] for a in md["UserAttributes"]
                  if a["Name"].startswith("custom:")}
        tenant = claims.get("custom:tenantId") or "meridian-fleet"
        claims.pop("custom:vehicleId", None)
        claims.pop("custom:driverId", None)
        groups = [g["GroupName"] for g in idp.admin_list_groups_for_user(
            UserPoolId=pool,
            Username="meridian.driver@example.com")["Groups"]]

        # ── VIN (required by bidi WebSocket headers) ───────────────────────
        vin = getattr(args, "vin", None)
        if not vin:
            veh_row = (boto3.resource("dynamodb", region_name=region)
                       .Table(f"cms-{stage}-storage-vehicles")
                       .get_item(Key={"vehicleId": vehicle_id})
                       .get("Item") or {})
            vin = veh_row.get("vin")
        if not vin:
            _refuse(f"could not resolve VIN for {vehicle_id}; pass --vin explicitly")
        # Guard the *resolved* VIN (not just the value from --vin flag).
        _guard_vehicle(vehicle_id, vin)

        # ── Bidi runtime ARN ───────────────────────────────────────────────
        rt_arn: str = yaml.safe_load((_REPO / ".bedrock_agentcore.yaml").read_text())[
            "agents"]["vsa_supervisor_bidi_staging"]["bedrock_agentcore"]["agent_arn"]

        # ── Run-scoped identifiers ─────────────────────────────────────────
        prefix = f"lv-pp0-{secrets.token_hex(3)}"
        user_email = f"{prefix}@example.com"
        pw = f"Av{secrets.token_urlsafe(18)}!7Qx"
        driver_id = f"DRV-LV-PP0-{secrets.token_hex(4).upper()}"
        first_name = "LiveTester"
        dtc_source = f"{prefix}-c1234"

        manifest: dict[str, Any] = {
            "run_id": run_id, "stage": stage,
            "vehicle_id": vehicle_id, "vin": vin,
            "cognito_pool": pool, "cognito_user": user_email,
            "driver_id": driver_id, "dtc_source": dtc_source,
            "dtc_timestamp_ms": None, "session_id": None,
            "subcommand": "voice",
        }
        save_manifest(manifest)

        ddb = boto3.resource("dynamodb", region_name=region)
        dtc_table = ddb.Table(f"cms-{stage}-storage-dtc-history")
        drivers_table = ddb.Table(f"cms-{stage}-storage-drivers")
        findings_table = ddb.Table(f"vsa-{stage}-avx-findings")

        # Guard: refuse if the test car has standing CRITICAL or HIGH faults.
        # Such faults alter the expected directive text and can trigger the
        # watcher on a code other than the seeded C1234.
        _guard_active_critical_dtc(vehicle_id, dtc_table)
        # Guard: refuse if another driver already has this car assigned.
        # A second assignedVehicleId causes the p0 Finding to have no owner_id.
        _guard_no_assigned_drivers(vehicle_id, drivers_table)

        # ── 1. Create throwaway Cognito user in the voice-auth pool ───────
        idp.admin_create_user(
            UserPoolId=pool, Username=user_email, MessageAction="SUPPRESS",
            UserAttributes=(
                [{"Name": "email", "Value": user_email},
                 {"Name": "email_verified", "Value": "true"},
                 {"Name": "custom:vehicleId", "Value": vehicle_id}]
                + [{"Name": k, "Value": v} for k, v in claims.items()]))
        cognito_created = True
        idp.admin_set_user_password(UserPoolId=pool, Username=user_email,
                                    Password=pw, Permanent=True)
        for g in groups:
            idp.admin_add_user_to_group(UserPoolId=pool, Username=user_email,
                                        GroupName=g)
        print(f"cognito user {user_email} created")

        # ── 2. Create CMS driver row so bidi_app resolves the first name ──
        drivers_table.put_item(
            Item={
                "driverId": driver_id,
                "cognitoEmail": user_email,
                "firstName": first_name,
                "assignedVehicleId": vehicle_id,
                "tenantId": tenant,
                "phone": "+15555010199",   # fictional 555-01xx placeholder
                "source": dtc_source,      # scopes cleanup conditional delete
            },
            ConditionExpression="attribute_not_exists(driverId)",
        )
        driver_row_created = True
        print(f"driver row {driver_id} created (assignedVehicleId={vehicle_id})")
        manifest["driver_row_created"] = True
        save_manifest(manifest)

        # ── 3. Sign in with vehicle-scoped token ──────────────────────────
        token = idp.initiate_auth(
            ClientId=client_id, AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": user_email, "PASSWORD": pw},
        )["AuthenticationResult"]["IdToken"]

        # ── 4. Open bidi session ──────────────────────────────────────────
        from bedrock_agentcore.runtime import AgentCoreRuntimeClient  # noqa: PLC0415

        session_id = f"{prefix}-{secrets.token_hex(12)}"
        manifest["session_id"] = session_id
        save_manifest(manifest)

        ws_url, headers = AgentCoreRuntimeClient(region=region).generate_ws_connection(
            runtime_arn=rt_arn, session_id=session_id)
        headers["X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token"] = token
        headers["X-Amzn-Bedrock-AgentCore-Runtime-Custom-Tenant-Id"] = tenant
        headers["X-Amzn-Bedrock-AgentCore-Runtime-Custom-Vin"] = vin
        headers["X-Amzn-Bedrock-AgentCore-Runtime-Custom-VehicleId"] = vehicle_id
        started_ms = int(time.time() * 1000) - 5_000
        print(f"session {session_id} opened {time.strftime('%H:%M:%SZ', time.gmtime())}")

        open_reason = getattr(args, "open_reason", None)

        # ── W4: for p0_push, seed C1234 BEFORE opening the session so the
        #    watcher sees it immediately on the first poll (no baseline wait).
        if open_reason == "p0_push":
            dtc_ts_ms = int(time.time() * 1000)
            dtc_table.put_item(Item={
                "vehicleId": vehicle_id,
                "timestamp": dtc_ts_ms,
                "vin": vin,
                "code": "C1234",
                "status": "ACTIVE",
                "severity": "CRITICAL",
                "system": "brakes",
                "description": "Brake system fault (live-verify P0 / p0_push pre-session)",
                "source": dtc_source,
            })
            manifest["dtc_timestamp_ms"] = dtc_ts_ms
            save_manifest(manifest)
            print(f"seeded ACTIVE C1234 ts={dtc_ts_ms} source={dtc_source!r} "
                  f"(pre-session, p0_push mode)")

        # ── Add OpenReason header when open-reason is p0_push. ────────────
        _apply_open_reason_header(headers, open_reason)

        # Expected directive text for C1234
        expected_directive = cat.p0_directive(cat.faults_for_codes(["C1234"]), first_name)
        expected_no_name = cat.p0_directive(cat.faults_for_codes(["C1234"]), None)
        print(f"expected directive: {expected_directive!r}")

        events: list[dict] = []
        done_event = asyncio.Event()

        def _is_directive(text: str) -> bool:
            return text == expected_directive or text == expected_no_name

        async def _recv(ws: Any) -> None:
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=_ALERT_WAIT_S + 5)
                except (asyncio.TimeoutError, websockets.ConnectionClosed):
                    done_event.set()
                    return
                evt = _try_decode_frame(raw)
                if evt is None:
                    continue  # skip non-JSON frames (PING, binary, etc.)
                if evt.get("type") == "audio.chunk":
                    continue
                events.append(evt)
                t = evt.get("type", "")
                if t == "transcript" and evt.get("isFinal"):
                    flag = " [P0-DIRECTIVE]" if _is_directive(evt.get("text", "")) else ""
                    print(f"  transcript {evt.get('role')}{flag}: "
                          f"{evt.get('text', '')[:180]}")
                elif t == "classification":
                    print(f"  classification value={evt.get('value')} "
                          f"source={evt.get('source')}")
                elif t == "platform.speak":
                    print(f"  platform.speak: {evt.get('text', '')[:180]}")

        async with ws_connect(ws_url, additional_headers=headers,
                              max_size=2 ** 24, open_timeout=20) as ws:
            recv_task = asyncio.create_task(_recv(ws))

            # ── 5. Wait for watcher baseline to be established ──────────
            if open_reason != "p0_push":
                print(f"waiting {_BASELINE_WAIT_S:.0f}s for watcher baseline …")
                await asyncio.sleep(_BASELINE_WAIT_S)

            # ── 6. Seed ACTIVE C1234 row (normal mode only; p0_push pre-seeded)
            if open_reason != "p0_push":
                dtc_ts_ms = int(time.time() * 1000)
                dtc_table.put_item(Item={
                    "vehicleId": vehicle_id,
                    "timestamp": dtc_ts_ms,
                    "vin": vin,
                    "code": "C1234",
                    "status": "ACTIVE",
                    "severity": "CRITICAL",
                    "system": "brakes",
                    "description": "Brake system fault (live-verify proactive P0)",
                    "source": dtc_source,
                })
                manifest["dtc_timestamp_ms"] = dtc_ts_ms
                save_manifest(manifest)
                print(f"seeded ACTIVE C1234 ts={dtc_ts_ms} source={dtc_source!r}")

            # ── 7. Wait up to 30 s for the P0 alert events ──────────────
            try:
                await asyncio.wait_for(done_event.wait(), timeout=_ALERT_WAIT_S)
            except asyncio.TimeoutError:
                pass
            recv_task.cancel()

        # ── 8. Evaluate ──────────────────────────────────────────────────
        p0_cls = [e for e in events if e.get("type") == "classification"
                  and e.get("value") == "P0"]
        directive_transcripts = [
            e for e in events
            if e.get("type") == "transcript" and e.get("isFinal")
            and _is_directive(e.get("text", ""))]
        platform_speaks = [
            e for e in events if e.get("type") == "platform.speak"
            and _is_directive(e.get("text", ""))]
        model_leads = any(
            (e.get("text", "").startswith(expected_directive[:30])
             or e.get("text", "").startswith(expected_no_name[:30]))
            for e in events if e.get("type") == "transcript"
            and e.get("role") == "assistant"
            and not e.get("_platform_safety_directive")
            and e.get("isFinal"))

        checks: dict[str, bool] = {
            "classification P0 received": bool(p0_cls),
            "directive transcript received": bool(directive_transcripts),
            "platform.speak or model lead with directive": bool(
                platform_speaks or model_leads),
        }
        print()
        for label, result in checks.items():
            print(("PASS" if result else "FAIL") + f": {label}")

        if p0_cls:
            print(f"INFO: first P0 cls source={p0_cls[0].get('source')}")
        if directive_transcripts:
            print(f"INFO: directive: {directive_transcripts[0].get('text', '')[:200]}")
        if platform_speaks:
            print(f"INFO: platform.speak: {platform_speaks[0].get('text', '')[:200]}")
        print()

        # ── 9. Print proactive-p0: runtime log lines ──────────────────────
        await asyncio.sleep(10)
        log_lines = _runtime_log_lines(region, rt_arn, session_id, started_ms)
        p0_lines = [ln for ln in log_lines if "proactive-p0:" in ln]
        print(f"proactive-p0: runtime log lines ({len(p0_lines)}):")
        for ln in p0_lines:
            print("  " + ln[-300:])
        if not p0_lines:
            print("  (none — watcher may not have run or logs not yet delivered)")

        # ── 10. Evaluate log-derived checks ───────────────────────────────
        log_checks = _evaluate_p0_log_lines(p0_lines, log_lines, open_reason)
        checks.update(log_checks)
        print()
        for label, result in log_checks.items():
            print(("PASS" if result else "FAIL") + f": {label}")

        return 0 if all(checks.values()) else 1

    finally:
        with _signals_held():
            _voice_cleanup(
                idp=idp,
                pool=pool,
                user_email=user_email if cognito_created else None,
                drivers_table=drivers_table,
                driver_id=driver_id if driver_row_created else None,
                dtc_table=dtc_table,
                vehicle_id=vehicle_id,
                dtc_ts_ms=dtc_ts_ms,
                dtc_source=dtc_source,
            )
            if findings_table is not None and vin and started_ms:
                _check_residual_findings(
                    findings_table, vin, started_ms, manifest,
                )
                save_manifest(manifest)


# ---------------------------------------------------------------------------
# push subcommand — T1.5b
# ---------------------------------------------------------------------------

#: Seconds to wait for the diagnostics component to write a p0 Finding.
_PUSH_FINDING_WAIT_S: float = 420.0  # 7 min
_PUSH_FINDING_POLL_S: float = 15.0
#: Seconds to wait for the broker to mark the ledger row published.
_PUSH_LEDGER_WAIT_S: float = 120.0  # 2 min
_PUSH_LEDGER_POLL_S: float = 10.0

_PUSH_AGENT_ID = "agent-2-vehicle-health"
_PUSH_FINDINGS_GSI = "by_vin"
_PUSH_REGISTER_DEVICE_FN = "api-avx-register-device"  # suffix; full = vsa-{stage}-{this}
_PUSH_APNS_SECRET_ID_TMPL = "cvx/{stage}/apns-auth-key"
_PUSH_REGISTER_DEVICE_PATH = "/notifications/register-device"


def _jwt_sub(token: str) -> str:
    """Decode the JWT payload (no verification) and return the ``sub`` claim."""
    payload_b64 = token.split(".")[1]
    # Pad to a multiple of 4.
    payload_b64 += "=" * (4 - len(payload_b64) % 4)
    return json.loads(base64.urlsafe_b64decode(payload_b64))["sub"]


def _push_cleanup(
    *,
    idp: Any,
    pool: str,
    user_email: str | None,
    drivers_table: Any,
    driver_id: str | None,
    dtc_table: Any,
    vehicle_id: str,
    dtc_ts_ms: int | None,
    dtc_source: str,
    sns_client: Any,
    devices_table: Any,
    owner_sub: str | None,
    platform_arn: str | None = None,
) -> None:
    """Delete only the items this push run created (manifest-keyed cleanup).

    Order: DTC row → notification-devices rows → SNS endpoints left without a
    row → driver row → Cognito user.

    The run's SNS endpoint is found two ways, both keyed by ``owner_sub`` (the
    fresh Cognito user's ``sub``, so only this run's items can match):

    * **Device rows**, queried by ``owner_id`` with ``ConsistentRead``. Covers a
      register-device 2xx whose HTTP response was lost, and a failed readback.
      Each row's endpoint is deleted, then the row.
    * **The platform application's endpoints**, paged with
      ``ListEndpointsByPlatformApplication`` and matched on
      ``Attributes.CustomUserData == owner_sub`` (register-device sets it,
      ``handler.py`` ``_create_platform_endpoint``). Covers register-device
      returning 500 after ``CreatePlatformEndpoint`` succeeded and ``put_item``
      failed, which leaves an endpoint and no row (review cycle 4 W1). Needs
      ``platform_arn``; on failure prints the list and delete commands.

    Do NOT delete Findings, actions or rate-limits/ledger rows.
    Never short-circuits: each delete is wrapped independently.
    """
    # 1. DTC row
    if dtc_ts_ms is not None and dtc_table is not None:
        try:
            dtc_table.delete_item(
                Key={"vehicleId": vehicle_id, "timestamp": dtc_ts_ms},
                ConditionExpression="#src = :s",
                ExpressionAttributeNames={"#src": "source"},
                ExpressionAttributeValues={":s": dtc_source},
            )
            print(f"deleted DTC row vehicleId={vehicle_id} ts={dtc_ts_ms}")
        except Exception as e:  # noqa: BLE001
            print(f"WARN: could not delete DTC row ts={dtc_ts_ms}: {e}", file=sys.stderr)
            print(
                f"  manual: aws dynamodb delete-item "
                f"--table-name cms-{STAGE}-storage-dtc-history "
                f'--key \'{{"vehicleId":{{"S":"{vehicle_id}"}},'
                f'"timestamp":{{"N":"{dtc_ts_ms}"}}}}\' '
                f"--condition-expression '#src = :s' "
                f"--expression-attribute-names '{{\"#src\":\"source\"}}' "
                f"--expression-attribute-values '{{\":s\":{{\"S\":\"{dtc_source}\"}}}}'",
                file=sys.stderr,
            )

    # 2. Notification-devices rows — query ALL for this owner_id (strongly
    # consistent: cleanup can run milliseconds after register-device's write)
    # so a row is found even when the HTTP response was lost.
    deleted_endpoints: set[str] = set()
    if owner_sub and devices_table is not None:
        try:
            resp = devices_table.query(
                KeyConditionExpression="owner_id = :o",
                ExpressionAttributeValues={":o": owner_sub},
                ConsistentRead=True,
            )
            for row in resp.get("Items", []):
                arn = row.get("endpoint_arn")
                tok = row.get("device_token")
                if arn and sns_client is not None:
                    try:
                        sns_client.delete_endpoint(EndpointArn=arn)
                        deleted_endpoints.add(arn)
                        print(f"deleted SNS endpoint {arn}")
                    except Exception as e:  # noqa: BLE001
                        print(f"WARN: could not delete SNS endpoint {arn}: {e}",
                              file=sys.stderr)
                        print(f"  manual: aws sns delete-endpoint "
                              f"--endpoint-arn {arn}", file=sys.stderr)
                if tok:
                    try:
                        devices_table.delete_item(
                            Key={"owner_id": owner_sub, "device_token": tok},
                        )
                        print(f"deleted device row owner_id={owner_sub} "
                              f"token_prefix={tok[:8]}…")
                    except Exception as e:  # noqa: BLE001
                        print(f"WARN: could not delete device row "
                              f"owner_id={owner_sub}: {e}", file=sys.stderr)
                        print(
                            f"  manual: aws dynamodb delete-item "
                            f"--table-name vsa-{STAGE}-notification-devices "
                            f'--key \'{{"owner_id":{{"S":"{owner_sub}"}},'
                            f'"device_token":{{"S":"{tok}"}}}}\' ',
                            file=sys.stderr,
                        )
        except Exception as e:  # noqa: BLE001
            print(f"WARN: could not query device rows for {owner_sub}: {e}",
                  file=sys.stderr)

    # 3. SNS endpoints with no device row: register-device creates the endpoint
    # before its put_item, so a 500 there leaves an endpoint and no row. Match on
    # the CustomUserData register-device sets (this run's fresh owner_sub).
    if owner_sub and platform_arn and sns_client is not None:
        _delete_owner_endpoints(sns_client, platform_arn, owner_sub, deleted_endpoints)

    # 4. Driver row
    if driver_id and drivers_table is not None:
        try:
            drivers_table.delete_item(
                Key={"driverId": driver_id},
                ConditionExpression="#src = :s",
                ExpressionAttributeNames={"#src": "source"},
                ExpressionAttributeValues={":s": dtc_source},
            )
            print(f"deleted driver row {driver_id}")
        except Exception as e:  # noqa: BLE001
            print(f"WARN: could not delete driver row {driver_id}: {e}", file=sys.stderr)
            print(
                f"  manual: aws dynamodb delete-item "
                f"--table-name cms-{STAGE}-storage-drivers "
                f'--key \'{{"driverId":{{"S":"{driver_id}"}}}}\' '
                f"--condition-expression '#src = :s' "
                f"--expression-attribute-names '{{\"#src\":\"source\"}}' "
                f"--expression-attribute-values "
                f"'{{\":s\":{{\"S\":\"{dtc_source}\"}}}}'",
                file=sys.stderr,
            )

    # 5. Cognito user
    if user_email and idp is not None:
        try:
            idp.admin_delete_user(UserPoolId=pool, Username=user_email)
            print(f"deleted cognito user {user_email}")
        except Exception as e:  # noqa: BLE001
            print(f"WARN: could not delete cognito user {user_email}: {e}", file=sys.stderr)
            print(f"  manual cleanup: aws cognito-idp admin-delete-user "
                  f"--user-pool-id {pool} --username {user_email}", file=sys.stderr)


#: Page cap for the endpoint sweep (a staging test application holds a handful).
_PUSH_ENDPOINT_PAGE_CAP = 200


def _delete_owner_endpoints(
    sns_client: Any,
    platform_arn: str,
    owner_sub: str,
    already_deleted: set[str],
) -> None:
    """Delete every endpoint of *platform_arn* whose CustomUserData is *owner_sub*.

    Pages ``ListEndpointsByPlatformApplication`` (``NextToken``). Skips ARNs in
    *already_deleted*. If listing fails, prints the list and delete commands; if
    one delete fails, prints that delete command and carries on.
    """
    manual_list = (
        f"  manual: aws sns list-endpoints-by-platform-application --region {REGION} "
        f"--platform-application-arn {platform_arn} "
        f"--query \"Endpoints[?Attributes.CustomUserData=='{owner_sub}'].EndpointArn\" "
        f"--output text\n"
        f"  then for each ARN: aws sns delete-endpoint --region {REGION} --endpoint-arn <arn>"
    )
    matches: list[str] = []
    kwargs: dict[str, Any] = {"PlatformApplicationArn": platform_arn}
    try:
        for _ in range(_PUSH_ENDPOINT_PAGE_CAP):
            resp = sns_client.list_endpoints_by_platform_application(**kwargs)
            for ep in resp.get("Endpoints", []):
                arn = ep.get("EndpointArn")
                attrs = ep.get("Attributes") or {}
                if arn and attrs.get("CustomUserData") == owner_sub:
                    matches.append(arn)
            token = resp.get("NextToken")
            if not token:
                break
            kwargs["NextToken"] = token
        else:
            print(f"WARN: endpoint sweep stopped after {_PUSH_ENDPOINT_PAGE_CAP} pages; "
                  "check the rest by hand", file=sys.stderr)
            print(manual_list, file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"WARN: could not list endpoints of {platform_arn} for "
              f"CustomUserData={owner_sub}: {e}", file=sys.stderr)
        print(manual_list, file=sys.stderr)
        return
    # Always say what the sweep saw, so a run whose list missed a just-created
    # endpoint (no read-after-write guarantee is documented) stays auditable.
    print(f"endpoint sweep: {len(matches)} endpoint(s) with CustomUserData={owner_sub} "
          f"on {platform_arn}")
    if not matches:
        print(manual_list.replace("  manual:", "  to re-check by hand:", 1))
    for arn in matches:
        if arn in already_deleted:
            continue
        try:
            sns_client.delete_endpoint(EndpointArn=arn)
            already_deleted.add(arn)
            print(f"deleted SNS endpoint {arn} (CustomUserData={owner_sub}, no device row)")
        except Exception as e:  # noqa: BLE001
            print(f"WARN: could not delete SNS endpoint {arn}: {e}", file=sys.stderr)
            print(f"  manual: aws sns delete-endpoint --region {REGION} "
                  f"--endpoint-arn {arn}", file=sys.stderr)


def _detail_is_exact_directive(finding: dict[str, Any], expected: str) -> bool:
    """Return True iff finding['detail'] equals ``expected`` exactly.

    Uses strict equality, never substring containment, so a superset or
    near-match detail is rejected (spec T1.5b exact directive comparison).
    """
    return finding.get("detail") == expected


def push_run(args: argparse.Namespace) -> int:
    """Push (APNs) live check: register device, seed C1234, wait for p0 push.

    Steps:
      0. Preconditions (read-only): secret exists + register-device Lambda has a
         non-empty AVX_APNS_PLATFORM_APP_ARN.  Exit 2 with no writes when the
         secret or function is not found or the ARN is empty; any other error
         propagates (exit 1).
      1. Build throwaway Cognito user + driver row (same pattern as voice).
      2. Register --device-token through the register-device API route.
      3. Read back the notification-devices row; check endpoint ARN is under
         the platform application.
      4. Seed ACTIVE C1234; wait ≤7 min for a p0 Finding from
         agent-2-vehicle-health whose detail == p0_directive(…, None).
      5. Wait ≤2 min for the broker's ledger row (sk p0:<finding_id>) to read
         ``published``.
      6. Print PASS/FAIL per check.
    Cleanup (try/finally, signals held): DTC, SNS endpoint(s) (by device row and
    by CustomUserData == owner_sub on the platform application), device row,
    driver row, Cognito user.  Findings, actions and ledger rows are left intact;
    their keys are recorded in the manifest.

    Preconditions: only ``ResourceNotFoundException`` (secret or register-device
    function) or an empty ``AVX_APNS_PLATFORM_APP_ARN`` exits 2; any other error
    propagates (exit 1).
    """
    region, stage = REGION, STAGE
    run_id = getattr(args, "run_id", None) or new_run_id()
    validate_run_id(run_id)

    # ── 0. Preconditions (read-only, zero writes) ─────────────────────────────
    # Only ResourceNotFoundException means "not exercised" (exit 2). Every other
    # error (expired SSO token, NoCredentialsError, throttling, an endpoint that
    # can't be reached) propagates, so the run exits 1 rather than reporting a
    # missing key it never confirmed (review cycle 4 S1).
    sm = boto3.client("secretsmanager", region_name=region)
    try:
        sm.describe_secret(SecretId=_PUSH_APNS_SECRET_ID_TMPL.format(stage=stage))
    except _BotoClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            print("not exercised: no APNs key (secret does not exist)")
            return 2
        raise

    lam = boto3.client("lambda", region_name=region)
    fn_name = f"vsa-{stage}-{_PUSH_REGISTER_DEVICE_FN}"
    try:
        fn_cfg = lam.get_function_configuration(FunctionName=fn_name)
    except _BotoClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            print(f"not exercised: {fn_name} does not exist")
            return 2
        raise
    platform_arn: str = (
        (fn_cfg.get("Environment") or {})
        .get("Variables", {})
        .get("AVX_APNS_PLATFORM_APP_ARN") or ""
    )

    if not platform_arn:
        print("not exercised: no APNs key (register-device has no "
              "AVX_APNS_PLATFORM_APP_ARN; redeploy vsa-staging-api after loading the key)")
        return 2

    # ── State for cleanup — set to sentinel values so ``finally`` never fails ─
    vehicle_id: str = args.vehicle_id
    device_token: str = args.device_token
    pool: str = ""
    user_email: str = ""
    driver_id: str = ""
    dtc_source: str = f"lv-pp0-push-{secrets.token_hex(3)}"
    dtc_ts_ms: int | None = None
    cognito_created = False
    driver_row_created = False
    endpoint_arn: str | None = None
    owner_sub: str | None = None
    idp: Any = None
    dtc_table: Any = None
    drivers_table: Any = None
    devices_table: Any = None
    sns_client: Any = boto3.client("sns", region_name=region)

    # Expected directive for C1234 (no driver name — push payload omits name)
    expected_detail = cat.p0_directive(cat.faults_for_codes(["C1234"]), None)

    try:
        idp = boto3.client("cognito-idp", region_name=region)

        # ── pool / CloudFormation API base URL ────────────────────────────
        pool = (boto3.client("ssm", region_name=region)
                .get_parameter(Name=f"/cvx/{stage}/voice-auth-user-pool-id")
                ["Parameter"]["Value"].strip())
        outs = (boto3.client("cloudformation", region_name=region)
                .describe_stacks(StackName=f"vsa-{stage}-api")
                ["Stacks"][0]["Outputs"])
        base_url = next(o["OutputValue"].rstrip("/") for o in outs
                        if o["OutputKey"] == "RestApiUrl")

        # ── App client that supports USER_PASSWORD_AUTH ───────────────────
        client_id = next(
            c["ClientId"]
            for c in idp.list_user_pool_clients(
                UserPoolId=pool, MaxResults=60)["UserPoolClients"]
            if "ALLOW_USER_PASSWORD_AUTH" in (
                idp.describe_user_pool_client(
                    UserPoolId=pool, ClientId=c["ClientId"])
                ["UserPoolClient"].get("ExplicitAuthFlows") or []))

        # ── Clone meridian.driver's non-vehicle claims ────────────────────
        md = idp.admin_get_user(UserPoolId=pool,
                                Username="meridian.driver@example.com")
        claims = {a["Name"]: a["Value"] for a in md["UserAttributes"]
                  if a["Name"].startswith("custom:")}
        tenant = claims.get("custom:tenantId") or "meridian-fleet"
        claims.pop("custom:vehicleId", None)
        claims.pop("custom:driverId", None)
        groups = [g["GroupName"] for g in idp.admin_list_groups_for_user(
            UserPoolId=pool,
            Username="meridian.driver@example.com")["Groups"]]

        # ── VIN ───────────────────────────────────────────────────────────
        vin: str | None = getattr(args, "vin", None)
        if not vin:
            veh_row = (boto3.resource("dynamodb", region_name=region)
                       .Table(f"cms-{stage}-storage-vehicles")
                       .get_item(Key={"vehicleId": vehicle_id})
                       .get("Item") or {})
            vin = veh_row.get("vin")
        if not vin:
            _refuse(f"could not resolve VIN for {vehicle_id}; pass --vin explicitly")
        # Guard the *resolved* VIN (not just the value from --vin flag).
        _guard_vehicle(vehicle_id, vin)

        # ── Run-scoped identifiers ────────────────────────────────────────
        prefix = f"lv-pp0-push-{secrets.token_hex(3)}"
        user_email = f"{prefix}@example.com"
        pw = f"Av{secrets.token_urlsafe(18)}!7Qx"
        driver_id = f"DRV-LV-PUSH-{secrets.token_hex(4).upper()}"
        dtc_source = f"{prefix}-c1234"

        manifest: dict[str, Any] = {
            "run_id": run_id, "stage": stage, "subcommand": "push",
            "vehicle_id": vehicle_id, "vin": vin,
            "cognito_pool": pool, "cognito_user": user_email,
            "driver_id": driver_id, "dtc_source": dtc_source,
            "dtc_timestamp_ms": None,
            "endpoint_arn": None, "owner_sub": None, "device_token": device_token,
            "finding_id": None, "ledger_key": None,
        }
        save_manifest(manifest)

        ddb = boto3.resource("dynamodb", region_name=region)
        dtc_table = ddb.Table(f"cms-{stage}-storage-dtc-history")
        drivers_table = ddb.Table(f"cms-{stage}-storage-drivers")
        devices_table = ddb.Table(f"vsa-{stage}-notification-devices")
        rate_limits_table = ddb.Table(f"vsa-{stage}-avx-rate-limits")
        findings_table = ddb.Table(f"vsa-{stage}-avx-findings")

        # Guard: refuse if the test car has standing CRITICAL or HIGH faults.
        _guard_active_critical_dtc(vehicle_id, dtc_table)
        # Guard: refuse if another driver already has this car assigned.
        _guard_no_assigned_drivers(vehicle_id, drivers_table)

        # ── 1. Create throwaway Cognito user ──────────────────────────────
        idp.admin_create_user(
            UserPoolId=pool, Username=user_email, MessageAction="SUPPRESS",
            UserAttributes=(
                [{"Name": "email", "Value": user_email},
                 {"Name": "email_verified", "Value": "true"},
                 {"Name": "custom:vehicleId", "Value": vehicle_id}]
                + [{"Name": k, "Value": v} for k, v in claims.items()]))
        cognito_created = True
        idp.admin_set_user_password(UserPoolId=pool, Username=user_email,
                                    Password=pw, Permanent=True)
        for g in groups:
            idp.admin_add_user_to_group(UserPoolId=pool, Username=user_email,
                                        GroupName=g)
        print(f"cognito user {user_email} created")

        # ── 2. Create CMS driver row ──────────────────────────────────────
        drivers_table.put_item(
            Item={
                "driverId": driver_id,
                "cognitoEmail": user_email,
                "firstName": "PushTester",
                "assignedVehicleId": vehicle_id,
                "tenantId": tenant,
                "phone": "+15555010198",
                "source": dtc_source,
            },
            ConditionExpression="attribute_not_exists(driverId)",
        )
        driver_row_created = True
        manifest["driver_row_created"] = True
        save_manifest(manifest)
        print(f"driver row {driver_id} created")

        # ── 3. Sign in to get auth token ──────────────────────────────────
        token = idp.initiate_auth(
            ClientId=client_id, AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": user_email, "PASSWORD": pw},
        )["AuthenticationResult"]["IdToken"]
        owner_sub = _jwt_sub(token)
        manifest["owner_sub"] = owner_sub
        save_manifest(manifest)

        # ── 4. Register device through the register-device API route ──────
        reg_url = f"{base_url}{_PUSH_REGISTER_DEVICE_PATH}"
        reg_resp = _http.post(
            reg_url,
            json={
                "device_token": device_token,
                "per_category_consent": {"safety": True, "coverage": True,
                                          "service": True},
                "platform": "apns-staging",
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if reg_resp.status_code not in (200, 201):
            print(f"register-device failed: {reg_resp.status_code} {reg_resp.text[:200]}",
                  file=sys.stderr)
            return 1
        print(f"device registered token_prefix={device_token[:8]}… "
              f"owner_sub={owner_sub}")

        # ── 5. Read back device row; verify endpoint ARN ──────────────────
        dev_item = (devices_table.get_item(
            Key={"owner_id": owner_sub, "device_token": device_token},
            ConsistentRead=True)
                    .get("Item") or {})
        endpoint_arn = dev_item.get("endpoint_arn") or ""
        manifest["endpoint_arn"] = endpoint_arn
        save_manifest(manifest)

        if not endpoint_arn:
            print(
                "FAIL: register-device returned 2xx but the notification-devices row "
                "has no endpoint_arn (or no row was read back).\n"
                "  Likely cause: SNS already holds an endpoint for this device token "
                "with another user's CustomUserData (the iOS app registered it).\n"
                "  Other causes: CreatePlatformEndpoint failed (throttling, IAM) — "
                "register-device stores the row with no endpoint in that case too.\n"
                f"  Check log group /aws/lambda/{fn_name} for "
                "'SNS create_platform_endpoint failed'.\n"
                "  Fix for the likely cause: run push before the iOS app registers "
                "this device token, or sign the app out first.",
                file=sys.stderr,
            )
            return 1

        check_endpoint_under_app = bool(
            endpoint_arn and platform_arn and
            # endpoint ARN shape: arn:aws:sns:<region>:<acct>:endpoint/<platform>/<app>/<id>
            # platform_arn shape: arn:aws:sns:<region>:<acct>:app/<platform>/<app>
            # The endpoint ARN contains the app name segment from the platform ARN.
            platform_arn.split("app/", 1)[-1] in endpoint_arn
        )

        print(f"  endpoint_arn: {endpoint_arn or '(none)'}")
        print(f"  platform_arn: {platform_arn}")
        print(("PASS" if check_endpoint_under_app else "FAIL")
              + ": endpoint ARN is under the platform application")

        # Record run start before seeding; used to filter stale Findings.
        started_ms = int(time.time() * 1000) - 5_000
        run_start_dt = datetime.fromtimestamp(started_ms / 1000, tz=timezone.utc)

        # ── 6. Seed ACTIVE C1234 row on the test car ──────────────────────
        dtc_ts_ms = int(time.time() * 1000)
        dtc_table.put_item(Item={
            "vehicleId": vehicle_id,
            "timestamp": dtc_ts_ms,
            "vin": vin,
            "code": "C1234",
            "status": "ACTIVE",
            "severity": "CRITICAL",
            "system": "brakes",
            "description": "Brake system fault (live-verify push P0)",
            "source": dtc_source,
        })
        manifest["dtc_timestamp_ms"] = dtc_ts_ms
        save_manifest(manifest)
        print(f"seeded ACTIVE C1234 ts={dtc_ts_ms} source={dtc_source!r}")

        # ── 7. Wait ≤7 min for p0 Finding from agent-2-vehicle-health ─────
        print(f"waiting up to {_PUSH_FINDING_WAIT_S:.0f}s for p0 Finding …")
        deadline = time.monotonic() + _PUSH_FINDING_WAIT_S
        p0_finding: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            resp = findings_table.query(
                IndexName=_PUSH_FINDINGS_GSI,
                KeyConditionExpression="vin = :v",
                FilterExpression=(
                    "agent_id = :a AND severity = :s AND #d = :txt"
                ),
                ExpressionAttributeNames={"#d": "detail"},
                ExpressionAttributeValues={
                    ":v": vin,
                    ":a": _PUSH_AGENT_ID,
                    ":s": "p0",
                    ":txt": expected_detail,
                },
            )
            # Filter client-side on computed_at so stale Findings from prior runs
            # (same VIN, same directive, expires_at still in the future) are skipped.
            items = [
                i for i in resp.get("Items", [])
                if _computed_at_ge(i.get("computed_at", ""), run_start_dt)
            ]
            if items:
                p0_finding = items[0]
                break
            time.sleep(_PUSH_FINDING_POLL_S)

        finding_id = (p0_finding or {}).get("finding_id", "")
        ledger_key = f"p0:{finding_id}" if finding_id else ""
        manifest["finding_id"] = finding_id
        manifest["ledger_key"] = ledger_key
        save_manifest(manifest)

        check_finding = bool(p0_finding)
        check_detail = (
            p0_finding is not None
            and _detail_is_exact_directive(p0_finding, expected_detail)
        )
        print(("PASS" if check_finding else "FAIL")
              + f": p0 Finding from {_PUSH_AGENT_ID} found for VIN {vin}")
        print(("PASS" if check_detail else "FAIL")
              + ": Finding detail equals p0_directive(faults_for_codes([\"C1234\"]), None)")

        # ── 8. Wait ≤2 min for broker ledger row to read ``published`` ─────
        check_published = False
        if finding_id and owner_sub:
            print(f"waiting up to {_PUSH_LEDGER_WAIT_S:.0f}s for ledger "
                  f"row p0:{finding_id} to reach published …")
            ledger_deadline = time.monotonic() + _PUSH_LEDGER_WAIT_S
            while time.monotonic() < ledger_deadline:
                row = (rate_limits_table.get_item(
                    Key={"owner_id": owner_sub,
                         "window_key": ledger_key})
                       .get("Item") or {})
                if row.get("state") == "published":
                    check_published = True
                    break
                time.sleep(_PUSH_LEDGER_POLL_S)
        else:
            print("SKIP: ledger check (no finding_id or owner_sub)")

        print(("PASS" if check_published else "FAIL")
              + f": broker ledger row {ledger_key!r} state=published")

        checks = {
            "endpoint ARN under platform application": check_endpoint_under_app,
            f"p0 Finding from {_PUSH_AGENT_ID}": check_finding,
            "Finding detail == directive": check_detail,
            "broker ledger row published": check_published,
        }
        print()
        for label, result in checks.items():
            print(("PASS" if result else "FAIL") + f": {label}")

        return 0 if all(checks.values()) else 1

    finally:
        with _signals_held():
            _push_cleanup(
                idp=idp,
                pool=pool,
                user_email=user_email if cognito_created else None,
                drivers_table=drivers_table,
                driver_id=driver_id if driver_row_created else None,
                dtc_table=dtc_table,
                vehicle_id=vehicle_id,
                dtc_ts_ms=dtc_ts_ms,
                dtc_source=dtc_source,
                sns_client=sns_client,
                devices_table=devices_table,
                # owner_sub as soon as it is known (before register-device is
                # called): the device-row query and the CustomUserData sweep
                # both key on it, so a non-2xx still cleans up (cycle 4 W1).
                owner_sub=owner_sub,
                platform_arn=platform_arn,
            )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    if os.environ.get("LIVE_TESTS") != "1":
        print("skipped: set LIVE_TESTS=1 to run against real AWS")
        return 2

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="subcommand", required=True)

    # voice
    vp = sub.add_parser("voice", help="voice watcher live-verify")
    vp.add_argument("--vehicle-id", required=True,
                    help="test car vehicleId; never VEH-MRDN-0015")
    vp.add_argument("--vin", default=None,
                    help="VIN (looked up from vehicles table if omitted)")
    vp.add_argument("--run-id", default=None,
                    help="idempotent run id (generated if omitted)")
    vp.add_argument("--stage", default=STAGE, choices=[STAGE],
                    help=f"deployment stage; only {STAGE!r} is supported")
    vp.add_argument("--open-reason", default=None, dest="open_reason",
                    choices=["p0_push"],
                    help="open-reason for the bidi session; 'p0_push' seeds C1234 "
                         "before the session and adds the OpenReason header")

    # push
    pp = sub.add_parser("push", help="push APNs live-verify (T1.5b)")
    pp.add_argument("--vehicle-id", required=True,
                    help="test car vehicleId; never VEH-MRDN-0015")
    pp.add_argument("--vin", default=None,
                    help="VIN (looked up from vehicles table if omitted)")
    pp.add_argument("--device-token", required=True, dest="device_token",
                    help="APNs device token from the iOS Simulator")
    pp.add_argument("--run-id", default=None, dest="run_id",
                    help="idempotent run id (generated if omitted)")
    pp.add_argument("--stage", default=STAGE, choices=[STAGE],
                    help=f"deployment stage; only {STAGE!r} is supported")

    args = ap.parse_args(argv)

    _guard_stage(args.stage)
    _guard_vehicle(args.vehicle_id, getattr(args, "vin", None))

    _install_signal_handlers()

    if args.subcommand == "voice":
        return asyncio.run(_voice_run(args))
    if args.subcommand == "push":
        return push_run(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
