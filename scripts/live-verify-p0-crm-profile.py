#!/usr/bin/env python3
"""Live check: on a verified P0 escalation with the Meridian callback armed, the chat
carries the driver's CRM profile and the case has a customer link.

Issue: issues/2026-11-03-p0-callback-starves-crm-profile/report.md
Spec:  .kiro/specs/2026-11-02-avx-connect-profiles-cases/spec.md

Checks (live mode only):
  C1  Lambda returns 200 (contactId present, rsaDispatched=False)
  C2  The invoke log tail carries callback_initiated=True and has no 'crm: skipped' and
      no 'driver lookup failed' line
  C3  Chat attributes: customerProfileId = profileSearchValue = the profile found by
      _account; profileSearchKey=_profileId; p0CallbackPlaced=true
  C4a Case found by escalationKey has customer_id = crm.profile_arn(...)
  C4b Case has a Contact related item whose contactArn is the chat contact
  C5  START -> "callback: initiated" no later than the pre-fix baseline for a cold or
      warm start (_CALLBACK_BASELINE_MS; cold start read from the REPORT Init Duration)
  C6  The profile carries the driver row's first name, last name and phone (the
      request sends no displayName, so these come only from the driver row the
      handler read; issue 2026-11-04-escalate-crm-driver-row-unread)
  C7  The profile has a CvxVehicle object with the VIN
  C8  (only with --nameless-profile) The chat's profile is that pre-existing nameless
      profile, the only one carrying the driver's account: the handler named it rather
      than creating one (issue 2026-11-04-escalate-crm-nameless-profile). With C6, its
      names are the row's and its phone is unchanged (the preflight required the row's).

Run order: the FIRST invoke after a deploy is a cold start, the bug's own case; run
this check then, and once more warm (a second unused driver) if a warm reading is
wanted. The run record says which it was (coldStart, initDurationMs).

Safety:
  - Refuses without LIVE_TESTS=1 (exit 2) unless --dry-run.
  - --driver-id and --vehicle-id are required.
  - Refuses DRV-MRDN-0015, VEH-MRDN-0015, and any CMS→ADP live-roster vehicle.
  - Refuses a driver used by any earlier runs/crm-*.json run (vehicle-object reuse
    would fail C4a for a reason unrelated to the bug under test).
  - Refuses driver not assigned to the vehicle.
  - Refuses phone not a fictional +1NXX-555-01XX number (the callback dials it).
  - Refuses phone matching an unclaimed profile.
  - Refuses if the tenant callback is not armed (contactFlowId, queueArn, chatContactFlowId).
  - Refuses if CONNECT_CRM_CONFIG is absent or invalid in the Lambda env.
  - Refuses a driver a profile already carries (the run would overwrite, then delete,
    that profile's vehicle object), except with --nameless-profile <id>: then that
    profile must be the only one on the driver's account, with no FirstName, LastName
    or Attributes, no CvxVehicle object, and the driver row's phone, and no other,
    unclaimed profile may hold that phone. The run keeps it (it pre-existed) after
    deleting the vehicle object it wrote; the operator who created it removes it.
  - Refuses if the Lambda's CMS_DRIVERS_TABLE isn't cms-{stage}-storage-drivers (the
    phone guard must read the table the callback dials from).
  - Refuses a driver row with no firstName or name (C6 couldn't tell a read row from
    an unread one).
  - dispatchRoadside=False (no RSA write).
  - Does NOT connect the chat participant (keeps the chat out of agent queues).
  - No retry invoke, no after-callback chat.

Cleanup (finally, signals held):
  Stop the chat; find the outbound call by its escalationId attribute (search_contacts,
  up to ~5 min) and stop it if still live (only a VOICE contact whose escalationId
  matches), then read back that it ended; a call still live, or not found when the
  handler reported it placed, is a CLEANUP FAIL; delete related items, case, vehicle object,
  profile (only if absent before this run AND carries our AccountNumber and
  source=avx-escalate), the fixture row; read back that each is gone. The case is read
  back by GetCase (ResourceNotFoundException = gone): SearchCases lags a DeleteCase.

Run record:
  Writes runs/crm-p0-<ts>Z-<hex>.json under the spec's runs/ dir: ids only, VIN
  length only, each check's verdict (checks), coldStart (null if C5 never ran),
  initDurationMs, cleanupFail, plus callback and chat log-line offsets from the invocation's START
  (fetched via filter_log_events by request id from /aws/lambda/{fn_name}).

--dry-run:
  Read-only preflight + plan. No LIVE_TESTS=1 needed. Exits 0.

Usage:
  .venv/bin/python3 scripts/live-verify-p0-crm-profile.py --dry-run \\
      --driver-id DRV-MRDN-XXXX --vehicle-id VEH-MRDN-XXXX
  LIVE_TESTS=1 .venv/bin/python3 scripts/live-verify-p0-crm-profile.py \\
      --driver-id DRV-MRDN-XXXX --vehicle-id VEH-MRDN-XXXX
  # the found-by-account naming path (C8): pre-create a nameless profile on the account first
  LIVE_TESTS=1 .venv/bin/python3 scripts/live-verify-p0-crm-profile.py \\
      --driver-id DRV-MRDN-XXXX --vehicle-id VEH-MRDN-XXXX --nameless-profile <profile-id>

Exit codes: 0 all checks passed · 1 check/preflight/cleanup failed · 2 skipped / dry-run done
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import importlib.util
import json
import os
import signal
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

# crm helpers live in lambdas/api-escalate/crm.py
_REPO_ROOT = Path(__file__).resolve().parents[1]
_LAMBDA_DIR = _REPO_ROOT / "lambdas" / "api-escalate"
if str(_LAMBDA_DIR) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_DIR))

from crm import account_number, contact_arn, escalation_key, load_config, profile_arn  # noqa: E402

# ---------------------------------------------------------------------------
# Import helpers from live-verify-connect-crm.py via importlib (reuse, not copy)
# ---------------------------------------------------------------------------

def _load_connect_crm():
    _spec = importlib.util.spec_from_file_location(
        "live_verify_connect_crm",
        Path(__file__).resolve().parent / "live-verify-connect-crm.py",
    )
    mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(mod)
    return mod


_crm = _load_connect_crm()

_live_roster_vehicle_ids = _crm._live_roster_vehicle_ids
_find_candidate = _crm._find_candidate
_drivers_used_by_earlier_runs = _crm._drivers_used_by_earlier_runs
_phone_matches_unclaimed_profile = _crm._phone_matches_unclaimed_profile
_search_case_by_key = _crm._search_case_by_key
_get_case_field_values = _crm._get_case_field_values
_search_related_items = _crm._search_related_items
_related_items_until = _crm._related_items_until
_vehicle_object_keys = _crm._vehicle_object_keys
_write_run_record = _crm._write_run_record
_chat_search_attributes = _crm._chat_search_attributes

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEST_SOURCE = "live-verify-p0-crm-profile"

_SPEC_DIR = _REPO_ROOT / ".kiro" / "specs" / "2026-11-02-avx-connect-profiles-cases"
_RUNS_DIR = _SPEC_DIR / "runs"

_REFUSED_DRIVERS = frozenset({"DRV-MRDN-0015"})
# Both ids until the vehicleId = VIN migration has moved this car (CMS spec 2026-10-28-vehicleid-equals-vin, cohort 3).
_REFUSED_VEHICLES = frozenset({"VEH-MRDN-0015", "MRDN0000000000015"})

_FIXTURE_DTCS: list[str] = ["P0300", "C0040"]
_ROSTER_PAGE_CAP = 30
_CALL_POLL_ATTEMPTS = 60    # up to ~5 min at 5 s each (indexing lagged > 90 s, 2026-10-05)
_CALL_POLL_S = 5
_CALL_PAGE_CAP = 10
_LOG_POLL_ATTEMPTS = 10     # CloudWatch ingestion lag: up to ~30 s for this invoke's lines
_LOG_POLL_S = 3
# Pre-fix START -> "callback: initiated" (report § Owner's investigation): the fix must
# not place the call later. Warm 0.89 s and 1.02 s (10-05); cold 2.31 s (11-03) and
# 4.64 s (10-05). The bar is the slower warm run and the faster cold run.
_CALLBACK_BASELINE_MS = {"warm": 1020, "cold": 2310}
_RELATED_POLL_ATTEMPTS = 5
_RELATED_POLL_S = 2
_CASE_GONE_POLL_ATTEMPTS = 5
_CASE_GONE_POLL_S = 2


# ---------------------------------------------------------------------------
# Signal hold — from live-verify-p0-escalation.py pattern
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _signals_held() -> Iterator[None]:
    """Defer SIGINT/SIGTERM/SIGHUP until the cleanup block completes."""
    held: list[int] = []

    def _hold(signum: int, frame: object) -> None:
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
# Helpers
# ---------------------------------------------------------------------------

def _fictional_phone(phone: str) -> bool:
    """True if the phone is a fictional +1NXX-555-01XX number (safe to call back)."""
    digits = "".join(c for c in (phone or "") if c.isdigit())
    # E.164: 11 digits, country code 1, NXX area code, 555 exchange, 01xx station
    return (len(digits) == 11
            and digits[0] == "1"
            and digits[4:7] == "555"
            and digits[7:9] == "01")


def _row_names(row: dict[str, Any]) -> tuple[str | None, str | None]:
    """(first, last) the profile should carry, from the CMS drivers row alone:
    firstName/lastName, else `name` split once. (None, None) when the row has neither."""
    first, last = row.get("firstName"), row.get("lastName")
    if isinstance(first, str) and first.strip():
        return first.strip(), (last.strip() if isinstance(last, str) and last.strip() else None)
    full = row.get("name")
    if isinstance(full, str) and full.strip():
        parts = full.strip().split(None, 1)
        return parts[0], (parts[1] if len(parts) > 1 else None)
    return None, None


def _nameless_profile_problem(
    cp: Any, crm_cfg: dict[str, Any], existing: list[dict[str, Any]],
    profile_id: str, acct: str, phone: str | None,
) -> str | None:
    """Why `profile_id` can't stand in for the take's adopted profile, or None.

    It must be the only profile `_account` finds for the driver, carry the account, have
    no name, no Attributes (so it isn't one the handler made) and no CvxVehicle object
    (the run deletes the vehicle object it writes), and hold the driver row's phone (C6
    then shows the phone unchanged). No other profile may hold the phone unclaimed.
    Errors propagate: the caller refuses the live run. Never names a phone or a name.
    """
    ids = [p.get("ProfileId") for p in existing]
    if ids != [profile_id]:
        return f"the driver's account is on {len(ids)} profile(s), not exactly this one"
    p = existing[0]
    if p.get("AccountNumber") != acct:
        return "it does not carry the driver's account"
    if any(p.get(k) not in (None, "") for k in ("FirstName", "LastName")):
        return "it already has a name"
    if p.get("Attributes"):
        return "it has Attributes (made by the handler or someone else)"
    if (p.get("PhoneNumber") or "").strip() != (phone or "").strip():
        return "its phone is not the driver row's"
    objs = cp.list_profile_objects(DomainName=crm_cfg["profilesDomain"], ObjectTypeName=crm_cfg["objectType"],
                                   ProfileId=profile_id, MaxResults=25).get("Items") or []
    if objs:
        return f"it has {len(objs)} {crm_cfg['objectType']} object(s)"
    if _phone_matches_unclaimed_profile(cp, crm_cfg["profilesDomain"], phone):
        return "another, unclaimed profile holds the driver's phone"
    return None


def _case_gone(
    cc: Any,
    domain_id: str,
    case_id: str,
    attempts: int = _CASE_GONE_POLL_ATTEMPTS,
    sleep_s: float = _CASE_GONE_POLL_S,
) -> bool:
    """True once GetCase answers ResourceNotFoundException for the case.

    SearchCases lags a DeleteCase: run crm-p0-20261104T134715Z-b6c4cf read a deleted case
    back as present, and GetCase said it was gone. Any other error propagates, so the
    caller marks cleanup failed rather than guessing.
    """
    from botocore.exceptions import ClientError  # noqa: PLC0415

    for attempt in range(attempts):
        try:
            cc.get_case(domainId=domain_id, caseId=case_id, fields=[{"id": "customer_id"}])
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                return True
            raise
        if attempt + 1 < attempts:
            time.sleep(sleep_s)
    return False


def _find_call_by_escalation_id(
    connect: Any,
    instance_id: str,
    esc_id: str,
    start_time: datetime,
    attempts: int = _CALL_POLL_ATTEMPTS,
    sleep_s: float = _CALL_POLL_S,
) -> dict[str, Any] | None:
    """Poll search_contacts for a VOICE contact with escalationId = esc_id.

    Returns the full describe_contact body, or None if not found after all polls.
    Only a contact whose `escalationId` attribute equals `esc_id` is ever returned
    (another session's or an agent's call in the window is never touched). Follows
    NextToken. Search indexing can lag by > 90 s (live-verify-p0-call-first.py, 2026-10-05).
    """
    for _ in range(attempts):
        try:
            kw: dict[str, Any] = {
                "InstanceId": instance_id,
                "TimeRange": {
                    "Type": "INITIATION_TIMESTAMP",
                    "StartTime": start_time - timedelta(seconds=5),
                    "EndTime": datetime.now(timezone.utc) + timedelta(seconds=5),
                },
                "SearchCriteria": {"Channels": ["VOICE"]},
            }
            for _page in range(_CALL_PAGE_CAP):
                page = connect.search_contacts(**kw)
                for c in page.get("Contacts", []):
                    try:
                        attrs = connect.get_contact_attributes(
                            InstanceId=instance_id,
                            InitialContactId=c["Id"],
                        ).get("Attributes", {})
                        if attrs.get("escalationId") == esc_id:
                            return connect.describe_contact(
                                InstanceId=instance_id, ContactId=c["Id"])["Contact"]
                    except Exception:
                        pass
                if not page.get("NextToken"):
                    break
                kw["NextToken"] = page["NextToken"]
        except Exception:
            pass
        time.sleep(sleep_s)
    return None


def _report_init_ms(log_lines: list[str]) -> int | None:
    """Init Duration (ms) from the invoke's REPORT line; None on a warm invoke."""
    for ln in log_lines:
        if ln.startswith("REPORT") and "Init Duration:" in ln:
            try:
                return int(float(ln.split("Init Duration:")[1].split("ms")[0].strip()))
            except (ValueError, IndexError):
                return None
    return None


def _log_offsets_from_cwl(
    logs_client: Any,
    fn_name: str,
    request_id: str,
    start_time: datetime,
) -> tuple[int | None, int | None]:
    """Return (callback_ms, chat_ms) — milliseconds after the START log event.

    Uses filter_log_events on /aws/lambda/{fn_name} for the given request_id, re-read
    until the START and the callback lines are both in (ingestion lags the invoke).
    Returns (None, None) on any error so failures don't abort the run record write.
    """
    try:
        log_group = f"/aws/lambda/{fn_name}"
        start_ms = int((start_time - timedelta(seconds=30)).timestamp() * 1000)
        events: list[dict[str, Any]] = []
        for attempt in range(_LOG_POLL_ATTEMPTS):
            end_ms = int((datetime.now(timezone.utc) + timedelta(seconds=5)).timestamp() * 1000)
            # All Lambda lines for this invocation contain the request_id (tab-separated format).
            events = logs_client.filter_log_events(
                logGroupName=log_group,
                startTime=start_ms,
                endTime=end_ms,
                filterPattern=f'"{request_id}"',
                limit=200,
            ).get("events", [])
            msgs = [ev.get("message", "") for ev in events]
            if (any(f"START RequestId: {request_id}" in m for m in msgs)
                    and any("callback: initiated" in m for m in msgs)):
                break
            if attempt + 1 < _LOG_POLL_ATTEMPTS:
                time.sleep(_LOG_POLL_S)

        # Baseline: the START event timestamp
        start_ts: int | None = None
        for ev in events:
            if f"START RequestId: {request_id}" in ev.get("message", ""):
                start_ts = ev["timestamp"]
                break
        if start_ts is None:
            return None, None

        callback_ms: int | None = None
        chat_ms: int | None = None
        for ev in sorted(events, key=lambda e: e["timestamp"]):
            msg = ev.get("message", "")
            offset = ev["timestamp"] - start_ts
            if "callback: initiated" in msg and callback_ms is None:
                callback_ms = offset
            if "escalate: contact=" in msg and chat_ms is None:
                chat_ms = offset
        return callback_ms, chat_ms
    except Exception as exc:
        print(f"[log-offsets] filter_log_events: {type(exc).__name__}: {exc}")
        return None, None


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:  # noqa: C901
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", default="staging")
    ap.add_argument("--region", default="us-west-2",
                    help="Lambda / DDB region (default: us-west-2)")
    ap.add_argument("--tenant", default="meridian")
    ap.add_argument("--driver-id", required=True, metavar="DRV")
    ap.add_argument("--vehicle-id", required=True, metavar="VEH")
    ap.add_argument("--dry-run", action="store_true",
                    help="Read-only preflight + plan; no LIVE_TESTS=1 needed")
    ap.add_argument("--nameless-profile", metavar="PROFILE_ID", default=None,
                    help="A profile the operator created on the driver's account with no name;"
                         " the run checks the handler names it (C8) and keeps it")
    args = ap.parse_args()

    dry_run = args.dry_run
    nameless = args.nameless_profile
    stage = args.stage
    tenant = args.tenant
    lam_region = args.region
    fn_name = f"vsa-{stage}-api-escalate"

    if not dry_run and os.environ.get("LIVE_TESTS") != "1":
        print("SKIP: set LIVE_TESTS=1 or use --dry-run.")
        print("  --dry-run   reads live AWS state and prints the plan (no writes).")
        print("  LIVE_TESTS=1 places one P0 escalation (call + chat) and deletes all artifacts.")
        return 2

    # ── 0. Hard-refused IDs ────────────────────────────────────────────────────
    if args.driver_id in _REFUSED_DRIVERS or args.vehicle_id in _REFUSED_VEHICLES:
        print(f"PREFLIGHT FAILED: --driver-id / --vehicle-id must not be"
              f" {_REFUSED_DRIVERS | _REFUSED_VEHICLES}")
        return 1

    import boto3

    ddb = boto3.resource("dynamodb", region_name=lam_region)
    lam = boto3.client("lambda", region_name=lam_region)

    print("=== Pre-checks (read-only) ===")
    crm_cfg: dict | None = None
    connect_region = "us-east-1"

    # ── 1. Lambda env: CONNECT_CRM_CONFIG (required for P0 path) ──────────────
    try:
        fn_meta = lam.get_function_configuration(FunctionName=fn_name)
        env_vars = fn_meta.get("Environment", {}).get("Variables", {})
        # The callback dials the phone from the table the Lambda names; the fictional-
        # phone guard below reads cms-{stage}-storage-drivers, so they must be one table.
        lam_drivers_table = env_vars.get("CMS_DRIVERS_TABLE") or f"cms-{stage}-storage-drivers"
        if lam_drivers_table != f"cms-{stage}-storage-drivers":
            print(f"PREFLIGHT FAILED: {fn_name} reads drivers from {lam_drivers_table!r},"
                  f" not cms-{stage}-storage-drivers; the phone guard would read the wrong table")
            return 1
        crm_json = env_vars.get("CONNECT_CRM_CONFIG")
        if crm_json:
            crm_cfg = load_config({
                "CONNECT_CRM_CONFIG": crm_json,
                "CONNECT_CRM_TENANTS": tenant,
            })
            if crm_cfg:
                connect_region = crm_cfg["region"]
                print(f"[OK] CONNECT_CRM_CONFIG valid; region={connect_region}"
                      f" domain={crm_cfg['profilesDomain']!r}"
                      f" casesDomain={crm_cfg['casesDomainId']!r}")
            else:
                if not dry_run:
                    print("PREFLIGHT FAILED: CONNECT_CRM_CONFIG present but fails crm.load_config"
                          " — deploy task 2.2 and verify the fix before running this check")
                    return 1
                print("[WARN] CONNECT_CRM_CONFIG fails validation — dry-run continues")
        else:
            if not dry_run:
                print(f"PREFLIGHT FAILED: CONNECT_CRM_CONFIG not in {fn_name} env;"
                      " this check requires the CRM feature to be deployed")
                return 1
            print(f"[WARN] CONNECT_CRM_CONFIG not in {fn_name} env — dry-run continues")
    except Exception as exc:
        if not dry_run:
            print(f"PREFLIGHT FAILED: Lambda env read failed ({type(exc).__name__})")
            return 1
        print(f"[WARN] Lambda env read failed ({type(exc).__name__}) — dry-run continues")

    # ── 2. Tenant config: callback must be armed ───────────────────────────────
    tc = ddb.Table(f"vsa-{stage}-tenant-config")
    instance_id = ""
    contact_flow_id = ""
    callback_armed = False
    try:
        ver = tc.get_item(Key={"tenantId": tenant, "version": "current"})["Item"]["activeVersion"]
        cfg = tc.get_item(Key={"tenantId": tenant, "version": ver})["Item"]
        conn = (cfg.get("channels") or {}).get("connect") or {}
        instance_arn = conn.get("instanceArn") or ""
        instance_id = instance_arn.split("/")[-1]
        tc_region = instance_arn.split(":")[3] if ":" in instance_arn else ""
        if tc_region:
            connect_region = tc_region
        contact_flow_id = conn.get("contactFlowId") or ""
        cb_cfg = (cfg.get("escalation") or {}).get("callback") or {}
        cb_flow_id = cb_cfg.get("contactFlowId")
        cb_queue_arn = cb_cfg.get("queueArn")
        cb_chat_flow_id = cb_cfg.get("chatContactFlowId")
        callback_armed = bool(cb_flow_id and cb_queue_arn and cb_chat_flow_id)
        if not callback_armed:
            if not dry_run:
                print(f"PREFLIGHT FAILED: tenant {tenant!r} {ver} callback not fully armed"
                      f" (contactFlowId={cb_flow_id!r}, queueArn={cb_queue_arn!r},"
                      f" chatContactFlowId={cb_chat_flow_id!r})")
                return 1
            print(f"[WARN] tenant {tenant!r} {ver} callback not armed — dry-run continues")
        else:
            print(f"[OK] Tenant {tenant!r} {ver}: callback armed,"
                  f" instanceId={instance_id!r} flowId={contact_flow_id!r}")
    except Exception as exc:
        print(f"PREFLIGHT FAILED: tenant config ({type(exc).__name__})")
        return 1

    # ── 3. Live roster ─────────────────────────────────────────────────────────
    try:
        roster = _live_roster_vehicle_ids(ddb, stage)
        print(f"[OK] Live roster: {len(roster)} vehicle(s) on cms-{stage}-storage-live-plan")
    except Exception as exc:
        if not dry_run:
            print(f"PREFLIGHT FAILED: live roster read failed ({type(exc).__name__})")
            return 1
        print(f"[WARN] Live roster read failed ({type(exc).__name__}) — dry-run continues")
        roster = set()

    # ── 4. Driver / vehicle selection ─────────────────────────────────────────
    candidate = _find_candidate(
        ddb, stage, tenant, roster,
        force_driver=args.driver_id,
        force_vehicle=args.vehicle_id,
    )
    if candidate is None:
        print(f"PREFLIGHT FAILED: driver {args.driver_id!r} or vehicle {args.vehicle_id!r}"
              " not found, not assigned together, or on the live roster")
        return 1

    driver_id, vehicle_id, vin, phone = candidate

    # Callback dials the driver's phone — it must be fictional.
    if not _fictional_phone(phone):
        # Security review: never echo the number this guard protects (handler masks it too).
        print(f"PREFLIGHT FAILED: driver {driver_id!r} phone is not a fictional"
              f" +1NXX-555-01XX number ({(phone or '')[:4]}****); the callback would dial a real person")
        return 1

    # Earlier runs used this driver: the vehicle object reuse would silently fail C4a.
    used_by = _drivers_used_by_earlier_runs(_RUNS_DIR).get(driver_id)
    if used_by:
        print(f"PREFLIGHT FAILED: driver {driver_id!r} was used by run {used_by!r};"
              " pick an unused assigned pair with --driver-id / --vehicle-id")
        return 1

    print(f"[OK] Candidate: driver={driver_id!r} vehicle={vehicle_id!r}"
          f" vin={'*' * len(vin)}(len={len(vin)})")

    # C6 compares the created profile's names with the driver row's. The request sends
    # no displayName, so a profile carrying them shows the handler read the row (issue
    # 2026-11-04-escalate-crm-driver-row-unread). A row without names can't show it.
    try:
        drv_row = ddb.Table(f"cms-{stage}-storage-drivers").get_item(
            Key={"driverId": driver_id}).get("Item") or {}
    except Exception as exc:
        print(f"PREFLIGHT FAILED: driver row read failed ({type(exc).__name__})")
        return 1
    want_first, want_last = _row_names(drv_row)
    if not want_first:
        print(f"PREFLIGHT FAILED: driver {driver_id!r} row has no firstName or name;"
              " C6 could not tell a row the handler read from one it didn't")
        return 1

    # ── 5. Profile / phone pre-check ──────────────────────────────────────────
    pre_profile_exists = False
    pre_profile_id: str | None = None
    if crm_cfg:
        cp_pre = boto3.client("customer-profiles", region_name=connect_region)
        acct = account_number(tenant, driver_id)
        try:
            existing = cp_pre.search_profiles(
                DomainName=crm_cfg["profilesDomain"],
                KeyName="_account", Values=[acct], MaxResults=5,
            ).get("Items", [])
            pre_profile_exists = bool(existing)
            pre_profile_id = existing[0]["ProfileId"] if existing else None
            if nameless:
                problem = _nameless_profile_problem(cp_pre, crm_cfg, existing, nameless, acct, phone)
                if problem:
                    if not dry_run:
                        print(f"PREFLIGHT FAILED: --nameless-profile {nameless!r}: {problem}")
                        return 1
                    print(f"[WARN] --nameless-profile {nameless!r}: {problem} — a live run would refuse")
                else:
                    print(f"[OK] Nameless profile {nameless!r}: the only one on the account; no name,"
                          " attributes or vehicle object; the driver row's phone")
            elif pre_profile_exists:
                if not dry_run:
                    # Review c1 suggestion: the run would overwrite that profile's vehicle
                    # object and cleanup would delete it, leaving neither.
                    print(f"PREFLIGHT FAILED: a profile already carries this driver's account"
                          f" ({pre_profile_id!r}); pick a driver no profile has yet")
                    return 1
                print(f"[INFO] Profile by _account already exists ({pre_profile_id!r})"
                      " — a live run would refuse this driver")
            else:
                unclaimed = _phone_matches_unclaimed_profile(
                    cp_pre, crm_cfg["profilesDomain"], phone)
                if unclaimed and not dry_run:
                    print("PREFLIGHT FAILED: the driver's phone matches an existing unclaimed"
                          " profile; the handler would adopt it and cleanup must not delete it."
                          " Pick a driver with a different phone.")
                    return 1
                print(f"[OK] No existing profile by account; unclaimed phone match: {unclaimed}")
        except Exception as exc:
            if not dry_run:
                print(f"PREFLIGHT FAILED: profile pre-check failed ({type(exc).__name__})")
                return 1
            print(f"[WARN] Profile pre-check failed ({type(exc).__name__}) — dry-run continues")

    if dry_run:
        _acct = account_number(tenant, driver_id) if crm_cfg else "<crm-unavailable>"
        print("\n=== DRY-RUN PLAN ===")
        print(f"  driverId   : {driver_id!r}")
        print(f"  vehicleId  : {vehicle_id!r}")
        print(f"  vin length : {len(vin)} chars (masked)")
        print(f"  account key: {_acct!r}")
        print(f"  callback   : armed={callback_armed}")
        print()
        print("  Steps the live run would execute:")
        steps = [
            f"WRITE  vsa-{stage}-triage-decisions: P0 fixture (activeDtcs={_FIXTURE_DTCS}, 1 h TTL)",
            f"INVOKE {fn_name} (severity=P0, dispatchRoadside=False, LogType=Tail)",
            "         → 200 + contactId + rsaDispatched=False  [C1]",
            "         → log tail: callback_initiated=True, no 'crm: skipped',"
            " no 'driver lookup failed'  [C2]",
            "READ   connect.get_contact_attributes(chat)",
            "READ   customer-profiles.search_profiles(_account) → profile_id",
            "         → customerProfileId=profileSearchValue=profile_id, profileSearchKey=_profileId,",
            "           p0CallbackPlaced=true  [C3]",
            "READ   customer-profiles.search_profiles(_profileId) → FirstName, LastName, PhoneNumber"
            " = the driver row's  [C6]",
            "READ   customer-profiles.list_profile_objects(CvxVehicle) → an object with the VIN  [C7]",
            *(["READ   the chat's profile is --nameless-profile, the only one on the account  [C8]"]
              if nameless else []),
            f"READ   connectcases.search_cases(escalationKey)",
            "READ   connectcases.get_case(customer_id) → must equal crm.profile_arn(...)  [C4a]",
            "READ   connectcases.search_related_items → Contact related item for the chat  [C4b]",
            "READ   logs.filter_log_events(request id) + REPORT Init Duration → START->callback"
            f" ≤ {_CALLBACK_BASELINE_MS} ms (cold/warm)  [C5]",
            "         (run this first after a deploy: the first invoke is the cold start)",
            "FINALLY stop chat",
            f"FINALLY search_contacts (VOICE, up to ~{_CALL_POLL_ATTEMPTS * _CALL_POLL_S}s) for call",
            "        with escalationId; stop if still live",
            "FINALLY delete related items, case, vehicle object, profile (ownership rule), fixture",
            "FINALLY read-back each deletion (FAIL row if not gone; the case by GetCase)",
            f"WRITE  {_RUNS_DIR}/crm-p0-<ts>Z-<hex>.json",
        ]
        for s in steps:
            print(f"    {s}")
        print()
        return 0

    # ═══════════════════════════════════════════════════════════════════════════
    # LIVE RUN
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n=== Live run ===")
    results: list[tuple[str, bool, str]] = []
    deleted: list[str] = []
    cleanup_fail = False

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(f"{'PASS' if ok else 'FAIL'}: {name}"
              + (f" — {detail}" if detail else ""))

    def _check_deleted(label: str, read_back: Any) -> None:
        """Call read_back(); if it raises or returns truthy, mark cleanup_fail."""
        nonlocal cleanup_fail
        try:
            if read_back():
                print(f"CLEANUP FAIL: {label} still present after delete")
                cleanup_fail = True
        except Exception as exc:
            print(f"CLEANUP FAIL: {label} read-back raised {type(exc).__name__}: {exc}")
            cleanup_fail = True

    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    run_id = f"crm-p0-{run_ts}Z-{uuid.uuid4().hex[:6]}"
    fixture_sid = f"{TEST_SOURCE}-{uuid.uuid4()}"
    reason = "LIVE-VERIFY P0 crm profile check; not a real incident"
    esc_type = "safety"
    sev = "P0"
    acct = account_number(tenant, driver_id)

    esc_key = escalation_key(
        tenant_id=tenant, vin=vin, escalation_type=esc_type, severity=sev,
        triage_session_id=fixture_sid, voice_session_id=None, reason=reason,
    )
    print(f"run_id={run_id!r}  fixture={fixture_sid!r}  esc_key={esc_key[:16]}…")

    connect = boto3.client("connect", region_name=connect_region)
    cp_client = boto3.client("customer-profiles", region_name=connect_region)
    cc_client = boto3.client("connectcases", region_name=connect_region)
    logs_client = boto3.client("logs", region_name=lam_region)

    chat_id: str | None = None
    body: dict[str, Any] = {}
    profile_id: str | None = pre_profile_id
    profile_was_new = not pre_profile_exists
    case_id: str | None = None
    fixture_written = False
    invoke_start_time = datetime.now(timezone.utc)
    init_ms: int | None = None
    callback_log_ms: int | None = None
    chat_log_ms: int | None = None
    call_contact_id: str | None = None

    try:
        # ── Fixture triage-decisions row ───────────────────────────────────────
        ddb.Table(f"vsa-{stage}-triage-decisions").put_item(Item={
            "sessionId": fixture_sid,
            "tenantId": tenant,
            "vin": vin,
            "classification": sev,
            "decidedAt": (datetime.now(timezone.utc)
                          .isoformat(timespec="milliseconds").replace("+00:00", "Z")),
            "source": TEST_SOURCE,
            "ttl": int(time.time()) + 3600,
            "inputsSnapshot": {"activeDtcs": _FIXTURE_DTCS},
        })
        fixture_written = True
        print(f"fixture: sessionId={fixture_sid!r} activeDtcs={_FIXTURE_DTCS}")

        # ── Invoke ─────────────────────────────────────────────────────────────
        invoke_start_time = datetime.now(timezone.utc)
        payload = {
            "httpMethod": "POST",
            "requestContext": {"authorizer": {"claims": {
                "custom:tenantId": tenant,
                "custom:driverId": driver_id,
                "custom:vehicleId": vehicle_id,
            }}},
            "body": json.dumps({
                "severity": sev,
                "escalationType": esc_type,
                "vin": vin,
                "vehicleId": vehicle_id,
                "driverId": driver_id,
                "dispatchRoadside": False,
                "triageSessionId": fixture_sid,
                "reason": reason,
            }),
        }
        resp = lam.invoke(
            FunctionName=fn_name,
            InvocationType="RequestResponse",
            LogType="Tail",
            Payload=json.dumps(payload).encode(),
        )
        env = json.loads(resp["Payload"].read())
        logs_b64 = resp.get("LogResult", "")
        logs = base64.b64decode(logs_b64).decode("utf-8", "replace")
        body = json.loads(env.get("body") or "{}")
        chat_id = body.get("contactId")
        # Chat participant NOT connected — the chat stays out of agent queues.

        # ── C1 ─────────────────────────────────────────────────────────────────
        check(
            "C1 Lambda returns 200 with contactId and rsaDispatched=False",
            env.get("statusCode") == 200 and bool(chat_id) and body.get("rsaDispatched") is False,
            f"status={env.get('statusCode')} contactId={'ok' if chat_id else 'MISSING'}"
            f" rsaDispatched={body.get('rsaDispatched')!r} err={body.get('error')!r}",
        )

        # ── C2 ─────────────────────────────────────────────────────────────────
        log_lines = logs.splitlines()
        # callback_initiated=True appears on the "escalate: contact=" line when the
        # handler placed a call before returning.
        chat_line = next((ln for ln in log_lines if "escalate: contact=" in ln), "")
        has_callback_initiated = "callback_initiated=True" in chat_line
        has_crm_skipped = any("crm: skipped" in ln for ln in log_lines)
        has_driver_lookup_failed = any("driver lookup failed" in ln for ln in log_lines)
        check(
            "C2 Log tail: callback_initiated=True, no crm: skipped, no driver lookup failure",
            has_callback_initiated and not has_crm_skipped and not has_driver_lookup_failed,
            f"callback_initiated={has_callback_initiated}"
            f" crm_skipped={has_crm_skipped}"
            f" driver_lookup_failed={has_driver_lookup_failed}"
            f" (chat_line snippet={chat_line[:80]!r})",
        )

        # Extract request ID for the run record log offsets.
        request_id = next(
            (ln.split("RequestId: ")[-1].split()[0]
             for ln in log_lines if "START RequestId:" in ln),
            "",
        )
        if request_id:
            callback_log_ms, chat_log_ms = _log_offsets_from_cwl(
                logs_client, fn_name, request_id, invoke_start_time)

        # ── C5: the call was placed no later than before the fix ───────────────
        init_ms = _report_init_ms(log_lines)
        cold_start = init_ms is not None
        bar_ms = _CALLBACK_BASELINE_MS["cold" if cold_start else "warm"]
        check(
            "C5 Call placed no later than the pre-fix baseline",
            callback_log_ms is not None and callback_log_ms <= bar_ms,
            f"{'cold' if cold_start else 'warm'} start (init={init_ms} ms);"
            f" START->callback={callback_log_ms} ms, baseline {bar_ms} ms;"
            f" START->chat line={chat_log_ms} ms",
        )

        if not chat_id:
            print("Cannot continue: invoke returned no contactId")
            # cleanup still runs via finally
        else:
            # ── C3 ──────────────────────────────────────────────────────────────
            # Read chat attributes and look up the profile for this driver.
            chat_attrs: dict[str, str] = {}
            extended_attrs: dict[str, str] = {}
            try:
                chat_attrs = _chat_search_attributes(connect, instance_id, chat_id)
                extended_attrs = (
                    connect.get_contact_attributes(
                        InstanceId=instance_id, InitialContactId=chat_id)
                    .get("Attributes") or {}
                )
            except Exception as exc:
                print(f"C3 attributes read: {type(exc).__name__}: {exc}")

            # Get the profile ID via _account search (needed for C3 and C4a).
            account_ids: list[str] = []
            try:
                prof_items = cp_client.search_profiles(
                    DomainName=crm_cfg["profilesDomain"],
                    KeyName="_account", Values=[acct], MaxResults=10,
                ).get("Items", [])
                # Same pick as the handler: the lowest ProfileId carrying our account.
                mine = sorted(p["ProfileId"] for p in prof_items
                              if p.get("ProfileId") and p.get("AccountNumber") == acct)
                account_ids = mine
                profile_id = mine[0] if mine else None
            except Exception as exc:
                print(f"C3 profile search: {type(exc).__name__}: {exc}")
                profile_id = None

            p0_cb_placed = extended_attrs.get("p0CallbackPlaced", "")
            esc_id_attr = extended_attrs.get("p0CallbackEscalationId", "")
            check(
                "C3 Chat attributes carry profile and p0CallbackPlaced=true",
                (bool(profile_id)
                 and chat_attrs.get("profileSearchKey") == "_profileId"
                 and chat_attrs.get("profileSearchValue") == profile_id
                 and chat_attrs.get("customerProfileId") == profile_id
                 and p0_cb_placed == "true"),
                f"profileId={'ok' if profile_id else 'MISSING'}"
                f" searchKey={chat_attrs.get('profileSearchKey')!r}"
                f" value_match={chat_attrs.get('profileSearchValue') == profile_id}"
                f" cpId_match={chat_attrs.get('customerProfileId') == profile_id}"
                f" p0CallbackPlaced={p0_cb_placed!r}",
            )

            # ── C6: the profile carries the driver row's names and phone ─────────
            prof: dict[str, Any] = {}
            if profile_id:
                try:
                    hits = cp_client.search_profiles(
                        DomainName=crm_cfg["profilesDomain"],
                        KeyName="_profileId", Values=[profile_id], MaxResults=5,
                    ).get("Items", [])
                    prof = next((p for p in hits if p.get("ProfileId") == profile_id), {})
                except Exception as exc:
                    print(f"C6 profile read: {type(exc).__name__}")
            want = (want_first, want_last, (phone or "").strip())
            got = (prof.get("FirstName"), prof.get("LastName"), prof.get("PhoneNumber"))
            # Values never printed: ok / MISMATCH / absent per field.
            c6 = ["ok" if g == w_ else ("absent" if g in (None, "") else "MISMATCH")
                  for g, w_ in zip(got, want)]
            check(
                "C6 Profile carries the driver row's first name, last name and phone",
                bool(prof) and got == want,
                f"profile={'read' if prof else 'MISSING'}"
                f" first={c6[0]} last={c6[1]} phone={c6[2]}",
            )

            # ── C7: the vehicle object is on the profile ────────────────────────
            veh_keys: list[str] = []
            if profile_id:
                try:
                    veh_keys = _vehicle_object_keys(
                        cp_client, crm_cfg["profilesDomain"], crm_cfg["objectType"],
                        profile_id, vin)
                except Exception as exc:
                    print(f"C7 vehicle objects read: {type(exc).__name__}")
            check(
                "C7 Profile has a CvxVehicle object with the VIN",
                bool(veh_keys),
                f"objects_with_vin={len(veh_keys)}",
            )

            # ── C8: the pre-existing nameless profile was named, not replaced ────
            if nameless:
                check(
                    "C8 Chat's profile is the pre-existing nameless one, the only one on the account",
                    (profile_id == nameless and account_ids == [nameless]
                     and chat_attrs.get("customerProfileId") == nameless),
                    f"chat_profile={'it' if chat_attrs.get('customerProfileId') == nameless else 'OTHER'}"
                    f" on_account={len(account_ids)}",
                )

            # ── C4a / C4b ───────────────────────────────────────────────────────
            case_id = _search_case_by_key(
                cc_client, crm_cfg["casesDomainId"],
                crm_cfg["fieldIds"]["escalationKey"], esc_key)

            if case_id:
                # C4a: case customer_id must equal crm.profile_arn(...)
                expected_cust_id = (profile_arn(crm_cfg, profile_id)
                                    if profile_id else None)
                actual_cust_id: str | None = None
                try:
                    case_resp = cc_client.get_case(
                        domainId=crm_cfg["casesDomainId"],
                        caseId=case_id,
                        fields=[{"id": "customer_id"}],
                    )
                    for field in case_resp.get("fields", []):
                        if field.get("id") == "customer_id":
                            actual_cust_id = (field.get("value") or {}).get("stringValue")
                            break
                except Exception as exc:
                    print(f"C4a get_case: {type(exc).__name__}: {exc}")

                check(
                    "C4a Case customer_id is the profile ARN",
                    bool(expected_cust_id) and actual_cust_id == expected_cust_id,
                    f"expected={'ok' if expected_cust_id else 'unknown'}"
                    f" actual={'ok' if actual_cust_id == expected_cust_id else actual_cust_id!r}",
                )

                # C4b: related item with the chat contact ARN
                expected_contact_arn = contact_arn(crm_cfg, chat_id)

                def _has_chat_ri(items: list[dict[str, Any]]) -> bool:
                    return any(
                        (r.get("content") or {}).get("contact", {}).get("contactArn")
                        == expected_contact_arn
                        for r in items
                    )

                ri_list = _related_items_until(
                    cc_client, crm_cfg["casesDomainId"], case_id, _has_chat_ri)
                check(
                    "C4b Case has Contact related item for the chat",
                    _has_chat_ri(ri_list),
                    f"related={len(ri_list)} expected_arn_suffix=…{chat_id[-8:]!r}",
                )
            else:
                check("C4a Case customer_id is the profile ARN", False,
                      f"case not found by escalationKey={esc_key[:16]}…")
                check("C4b Case has Contact related item for the chat", False, "case not found")

    finally:
        print("\n=== Cleanup ===")
        with _signals_held():
            # -- Stop the chat -------------------------------------------------
            if chat_id and instance_id:
                try:
                    connect.stop_contact(ContactId=chat_id, InstanceId=instance_id)
                    deleted.append(f"contact:{chat_id}")
                    print(f"stopped chat {chat_id}")
                except Exception as exc:
                    print(f"stop_contact {chat_id}: {type(exc).__name__}"
                          " (already ended is expected)")

                def _chat_still_open() -> bool:
                    for _ in range(3):
                        c = connect.describe_contact(
                            InstanceId=instance_id, ContactId=chat_id)["Contact"]
                        if c.get("DisconnectTimestamp"):
                            return False
                        time.sleep(2)
                    return True

                _check_deleted(f"chat {chat_id} (not ended)", _chat_still_open)

            # -- Find and stop the outbound call --------------------------------
            # The chat carries the call's escalation id; when the chat didn't start,
            # the 502 body does (handler: callbackInitiated + p0CallbackEscalationId).
            esc_id_for_call = str(body.get("p0CallbackEscalationId") or "")
            placed_attr: str | None = None  # None: the chat's attributes were not read
            if chat_id and instance_id:
                try:
                    ext = (connect.get_contact_attributes(
                        InstanceId=instance_id, InitialContactId=chat_id)
                        .get("Attributes", {}))
                    esc_id_for_call = ext.get("p0CallbackEscalationId", "") or esc_id_for_call
                    placed_attr = str(ext.get("p0CallbackPlaced", ""))
                except Exception as exc:
                    print(f"cleanup: get p0CallbackEscalationId: {type(exc).__name__}")

            # Review c3 W1: p0CallbackPlaced=false is not proof there is no call; the
            # handler also says false after a read timeout, when Connect may have placed
            # it. So any escalation id is searched; only "not found" is read differently.
            if esc_id_for_call and instance_id:
                print(f"cleanup: searching for VOICE contact"
                      f" escalationId={esc_id_for_call!r}"
                      f" (up to ~{_CALL_POLL_ATTEMPTS * _CALL_POLL_S}s)…")
                voice = _find_call_by_escalation_id(
                    connect, instance_id, esc_id_for_call, invoke_start_time)
                if voice:
                    vc_id = voice["Id"]
                    call_contact_id = vc_id
                    ended = bool(voice.get("DisconnectReason") or voice.get("DisconnectTimestamp"))
                    if not ended:
                        try:
                            connect.stop_contact(ContactId=vc_id, InstanceId=instance_id)
                            deleted.append(f"contact:{vc_id}")
                            print(f"stopped call {vc_id}")
                        except Exception as exc:
                            print(f"stop call {vc_id}: {type(exc).__name__}")

                        def _call_still_open() -> bool:
                            for _ in range(3):
                                c = connect.describe_contact(
                                    InstanceId=instance_id, ContactId=vc_id)["Contact"]
                                if c.get("DisconnectTimestamp"):
                                    return False
                                time.sleep(2)
                            return True

                        _check_deleted(f"call {vc_id} (not ended)", _call_still_open)
                    else:
                        print(f"call {vc_id} already ended")
                elif placed_attr == "false":
                    print("cleanup: no call found and the handler reported none placed"
                          " (p0CallbackPlaced=false)")
                else:
                    print("CLEANUP FAIL: outbound call not found in search_contacts"
                          f" within ~{_CALL_POLL_ATTEMPTS * _CALL_POLL_S}s; check by hand that"
                          f" the VOICE contact with escalationId={esc_id_for_call!r} has ended")
                    cleanup_fail = True
            elif placed_attr == "":
                # The chat has no p0CallbackPlaced at all: the handler never tried a call.
                print("cleanup: the handler attempted no call (no p0CallbackPlaced on the chat)")
            elif fixture_written:
                print("CLEANUP FAIL: no escalation id for the outbound call (chat attributes and"
                      " response body both lack p0CallbackEscalationId); check by hand")
                cleanup_fail = True

            # -- Ensure case_id for cleanup (index lag) -------------------------
            if not case_id:
                for _ in range(5):
                    try:
                        case_id = _search_case_by_key(
                            cc_client, crm_cfg["casesDomainId"],
                            crm_cfg["fieldIds"]["escalationKey"], esc_key)
                    except Exception as exc:
                        print(f"cleanup case lookup: {type(exc).__name__}")
                    if case_id:
                        break
                    time.sleep(2)

            # -- Ensure profile_id for cleanup ----------------------------------
            if not profile_id and profile_was_new:
                try:
                    _items = cp_client.search_profiles(
                        DomainName=crm_cfg["profilesDomain"],
                        KeyName="_account", Values=[acct], MaxResults=5,
                    ).get("Items", [])
                    profile_id = _items[0]["ProfileId"] if _items else None
                except Exception as exc:
                    print(f"cleanup profile lookup: {type(exc).__name__}")
            elif not profile_id and nameless:
                # The run's vehicle object goes on the kept nameless profile (review c1 S3).
                profile_id = nameless

            # -- Delete related items then case ---------------------------------
            if case_id:
                try:
                    ri_del = _search_related_items(
                        cc_client, crm_cfg["casesDomainId"], case_id)
                    for ri in ri_del:
                        ri_id = ri.get("relatedItemId")
                        if ri_id:
                            cc_client.delete_related_item(
                                domainId=crm_cfg["casesDomainId"],
                                caseId=case_id,
                                relatedItemId=ri_id,
                            )
                            deleted.append(f"related_item:{ri_id}")
                            print(f"deleted related item {ri_id!r}")
                except Exception as exc:
                    print(f"delete related items: {type(exc).__name__}: {exc}")

                try:
                    cc_client.delete_case(domainId=crm_cfg["casesDomainId"], caseId=case_id)
                    deleted.append(f"case:{case_id}")
                    print(f"deleted case {case_id}")
                except Exception as exc:
                    print(f"delete case {case_id}: {type(exc).__name__}: {exc}")

                # Read-back: case must be gone. By GetCase, not SearchCases (which lags a
                # delete; issue 2026-11-04-escalate-crm-driver-row-unread).
                _cid = case_id
                _check_deleted(
                    f"case {_cid}",
                    lambda: not _case_gone(cc_client, crm_cfg["casesDomainId"], _cid),
                )

            # -- Delete CvxVehicle objects --------------------------------------
            if profile_id:
                try:
                    all_veh_keys = _vehicle_object_keys(
                        cp_client, crm_cfg["profilesDomain"],
                        crm_cfg["objectType"], profile_id, vin)
                    for k in all_veh_keys:
                        cp_client.delete_profile_object(
                            DomainName=crm_cfg["profilesDomain"],
                            ProfileId=profile_id,
                            ProfileObjectUniqueKey=k,
                            ObjectTypeName=crm_cfg["objectType"],
                        )
                        deleted.append(f"vehicle_object:{k}")
                        print(f"deleted vehicle object {k!r}")
                except Exception as exc:
                    print(f"delete vehicle objects: {type(exc).__name__}: {exc}")

                # -- Delete profile only if this run created it -----------------
                ours = False
                if profile_was_new:
                    try:
                        _cur = cp_client.search_profiles(
                            DomainName=crm_cfg["profilesDomain"],
                            KeyName="_profileId", Values=[profile_id], MaxResults=5,
                        ).get("Items", [])
                        ours = any(
                            p.get("ProfileId") == profile_id
                            and p.get("AccountNumber") == acct
                            and (p.get("Attributes") or {}).get("source") == "avx-escalate"
                            for p in _cur
                        )
                    except Exception as exc:
                        print(f"cleanup ownership check: {type(exc).__name__}")

                if ours:
                    try:
                        cp_client.delete_profile(
                            DomainName=crm_cfg["profilesDomain"], ProfileId=profile_id)
                        deleted.append(f"profile:{profile_id}")
                        print(f"deleted profile {profile_id}")
                    except Exception as exc:
                        print(f"delete profile {profile_id}: {type(exc).__name__}: {exc}")

                    # Read-back: profile must be gone.
                    _pid = profile_id  # capture for lambda
                    _check_deleted(
                        f"profile {_pid}",
                        lambda: bool(cp_client.search_profiles(
                            DomainName=crm_cfg["profilesDomain"],
                            KeyName="_profileId", Values=[_pid], MaxResults=5,
                        ).get("Items")),
                    )
                else:
                    print(f"kept profile {profile_id}"
                          " (pre-existed, or not provably created by this run)")

            # -- Delete fixture row --------------------------------------------
            if fixture_written:
                try:
                    ddb.Table(f"vsa-{stage}-triage-decisions").delete_item(
                        Key={"sessionId": fixture_sid})
                    deleted.append(f"fixture:{fixture_sid}")
                    print(f"deleted fixture {fixture_sid!r}")
                except Exception as exc:
                    print(f"delete fixture: {type(exc).__name__}: {exc}")

                # Read-back: fixture must be gone.
                _sid = fixture_sid
                _check_deleted(
                    f"fixture {_sid}",
                    lambda: ddb.Table(f"vsa-{stage}-triage-decisions").get_item(
                        Key={"sessionId": _sid}).get("Item") is not None,
                )

            # -- Run record ----------------------------------------------------
            try:
                passed = sum(1 for _, ok, _ in results if ok)
                failed_count = sum(1 for _, ok, _ in results if not ok)
                record = {
                    "runId": run_id,
                    "timestamp": (datetime.now(timezone.utc)
                                  .isoformat(timespec="milliseconds").replace("+00:00", "Z")),
                    "stage": stage,
                    "tenant": tenant,
                    "driverId": driver_id,
                    "vehicleId": vehicle_id,
                    "vinLength": len(vin),          # VIN masked: length only
                    "profileId": profile_id,
                    "profileWasNew": profile_was_new,
                    "namelessProfile": nameless,
                    "caseId": case_id,
                    "contactId": chat_id,
                    "callContactId": call_contact_id,
                    "fixtureSessionId": fixture_sid,
                    "deleted": deleted,
                    "result": f"{passed}/{passed + failed_count} checks passed",
                    "checks": {name.split()[0]: ("PASS" if ok else "FAIL") for name, ok, _ in results},
                    # null when C5 never ran (an aborted run measured nothing).
                    "coldStart": ((init_ms is not None)
                                  if any(n.startswith("C5") for n, _, _ in results) else None),
                    "initDurationMs": init_ms,
                    "cleanupFail": cleanup_fail,
                    "callbackLogOffsetMs": callback_log_ms,
                    "chatLogOffsetMs": chat_log_ms,
                }
                rec_path = _RUNS_DIR / f"{run_id}.json"
                _write_run_record(rec_path, record)
                print(f"run record: {rec_path}")
            except Exception as exc:
                print(f"run record write failed: {type(exc).__name__}: {exc}")

    failed_names = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed_names)}/{len(results)} checks passed")
    if failed_names:
        print("Failed:", ", ".join(failed_names))
    return 1 if (failed_names or cleanup_fail) else 0


if __name__ == "__main__":
    sys.exit(main())
