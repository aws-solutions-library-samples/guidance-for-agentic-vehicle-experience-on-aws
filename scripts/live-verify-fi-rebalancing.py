#!/usr/bin/env python3
"""Live verification for FI rebalancing Stage E tasks 6.1 / 6.3.

Spec: .kiro/specs/2026-10-06-fi-rebalancing-stage-e/tasks.md Group 6.

Usage (staging only):
    LIVE_TESTS=1 python3 scripts/live-verify-fi-rebalancing.py \\
        --part adp --stage staging \\
        --workgroup adp-staging-foundation-fleet-rebalance-jobs \\
        [--expect-after YYYY-MM-DD] [--lake-bucket BUCKET]

    FI_REBALANCING_LIVE_GO=staging LIVE_TESTS=1 python3 scripts/live-verify-fi-rebalancing.py \\
        --part agent --stage staging \\
        --workgroup adp-staging-foundation-fleet-rebalance-jobs \\
        --avx-api-endpoint URL --allow-writes [--use-run <started_at>]

Exit codes: 0 all PASS, 1 any FAIL, 2 any SKIP (guard also exits 2 for bad stage/env).

--part adp checks (read-only; staging only):
  A1  Glue tables/views/snapshots exist in adp_{stage}_fleet_rebalance; COUNT(*) each.
  A2  max(data_as_of) from bookings_runs; max(run_date) from fleet_bookings_forecast;
      with --expect-after each must be strictly later.
  A3  Answer key present in S3 (plants JSON + at least one parquet).
  A4  Consumer roles cannot s3:GetObject the key; ADP bookings role can s3:PutObject.
  A5  Every SNS topic in the fleet-rebalance CFN stack has a confirmed subscription.
  A6  Every rental VIN in cms_ingest.vehicles has a dimensions.vins row and one seed record.
  A7  fleet_bookings_parameters deficit_above/target_utilization/max_utilization match
      the AVX SSM policy /vsa/{stage}/avx/fi-rebalancing/policy.

--part agent checks (--allow-writes required for G1/G3/G4b/G5; staging only):
  G1  Demand-surge plants vs move findings: ≥80% get a move INTO the deficit location
      from the same metro; the no-spare plant's location has a gap item.
  G2  Every move finding's money fields equal D9 formulas recomputed from its own evidence.
  G3  Re-run: zero new finding ids, run record model_calls==0, duplicates_skipped>0.
  G4  Isolation: no owner_id; fleet-operator of delivery fleet can't see rental findings;
      broker logged a skip for a rebalancing finding.
  G5  Forced SSM failure: malformed policy → Lambda FunctionError + aborted run record;
      restore in finally with signals held; alarm published to operator-alerts topic.

    FI_REBALANCING_LIVE_GO=staging LIVE_TESTS=1 python3 scripts/live-verify-fi-rebalancing.py \\
        --part cms --stage staging --workgroup <wg> \\
        --cms-api-endpoint https://<cms-ui-api>.execute-api.us-west-2.amazonaws.com/prod \\
        [--allow-writes [--approve-move]] [--cleanup-only --run-id <id>]

    LIVE_TESTS=1 python3 scripts/live-verify-fi-rebalancing.py \\
        --part cms-next --stage staging --workgroup <wg> --run-id <id of the cms run>

--part cms checks (task 7.5; staging only). Reads run as the operator; every write goes through
the screens' routes with two temporary fleet-operators (rental and depot fleet), never straight
to DynamoDB. --allow-writes creates the temporary operators; --approve-move also approves one
real rental move and grounds one car for the refusal probe. No writes 03:45-07:15 UTC, checked at
start and before each write item; --cleanup-only is exempt (a car left grounded is worse for the
nightly jobs than its return to service). One run approves at most one move: any FAIL before a
write item skips it. Run with python3 or .venv/bin/python from the AVX repo root (the script puts
the root on sys.path).
  C0  A live rental move the screen could approve (every car at its origin, none grounded, on the
      live roster or held by an iOS test user, room at the destination), and a car in another
      live move for the probe (fresh recall view, in the cohort, no unremedied recall).
  C1  The rental fleet's page and tile reads show real figures (cache under 26 h).
  C2  A depot fleet's utilization reads (both days values the screen asks) must be 200 with
      basis "trips", fleetKind "depot", a numeric summary.utilizationPct, and a fresh cache
      (same 26 h rule as C1). Demand and gaps must give the exact sentence "Demand forecasts
      and moves apply to rental and car-share fleets." with every figure null, the same on
      both. The row names the figure and the sentence.
  C3  Each read's p95 over 20 requests is under 3 s.
  C4  The depot operator gets CMS's fleet refusal on every rental read, each after a 200 control.
  C5  The probe car, grounded through Stage C's route: the screen's approve of a move naming it
      is refused (409 in AVX's audit), CMS's move route says "Vehicle <vin> is grounded and
      can't be moved.", nothing moves; the depot operator is refused on the grounding and
      return-to-service routes (the latter sent only while this run's grounding is the car's
      only open one); then return to service through Stage C's route, retried,
      in ``finally`` with signals held. If it can't, every later write is skipped and stderr
      names the --cleanup-only command.
  C6  The move approved through the screen's route: AVX executed, CMS's move record, each car's
      locationId at the destination, the move listed by GET /moves; the manifest records it.
  C7  The depot operator is refused on the screen's approve and on CMS's /moves for that move,
      each after the rental operator's identical request succeeds (both repeat a decided move,
      so a route failing open still changes nothing; compared after).
  C8  Always, once: this run's grounding returned, the probe wrote no move and left its car,
      every temporary operator deleted.
  X1  --cleanup-only: return the run's test car to service.

--part cms-next (read-only), after ADP's bookings job settles the move's effective day:
  N1  bookings_runs has settled the effective day (else SKIP with when to re-run).
  N2  vehicle_days places every moved car at the destination that day.
  N3  bookings by the moved cars from that day are all at the destination, at least one served.

    FI_REBALANCING_LIVE_GO=staging LIVE_TESTS=1 python3 scripts/live-verify-fi-rebalancing.py \\
        --part review --stage staging --workgroup cvx-staging-analytics \\
        --cms-api-endpoint https://<cms-ui-api>.execute-api.us-west-2.amazonaws.com/prod \\
        --run-id <id of the cms run> [--allow-writes] [--agent-run <started_at>]

--part review (task 7.7; staging only), on or after the move's review date (effective date + 7
days) once CMS's 06:40 UTC refresh has run. Reads only: --workgroup is the AGENT's workgroup,
whose query history R1 reads (no query is started, no Lake Formation grant needed);
--allow-writes admits only one temporary car-share fleet-operator for the CMS route (deleted
in finally), without it R2 and R4 SKIP. No write window: nothing it does touches the jobs.
  R1  The first completed rebalance run from the review date's 06:00 UTC (or --agent-run) read
      the outcomes snapshot (its own select_outcomes execution, exact SQL, during the run) and
      got the move's row: predicted and actual gain, ratio = actual / predicted, materialised on
      or after the review date.
  R2  CMS's Rebalance history route (GET /moves) lists the move with a non-null outcome whose
      figures equal R1's (SKIP before 06:55 UTC on the review date).
  R3  That run's move items into the destination carry the move in rebalance.recent_outcomes
      with R1's figures and snapshot. A move the run kept alive with evidence from before the
      review date is a FAIL (its inputs_hash excludes recent_outcomes); SKIP only when the run
      wrote no move into the destination: re-run with --agent-run of a later run.
  R4  Every move under 7 days old has no outcome and reviewAvailableOn = effective + 7 days
      (the tab's "Review on <date>"); SKIP when there is none.

Run manifests live in ~/.local/state/live-verify-fi-rebalancing/ (FI_REBAL_LV_STATE_DIR).
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import io
import json
import math
import os
import re
import signal as _sig
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Iterator

# repo root on sys.path so agents.fi_rebalancing.policy is importable (A7)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Live-write guard (scripts/lib/live_write_guard.py)
_LIB_DIR = str(Path(__file__).resolve().parent / "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)
import live_write_guard as _lwg  # noqa: E402

#: Set to ``--stage`` by the live-run step, on the user's go, and by nothing else.
_LIVE_GO_ENV = "FI_REBALANCING_LIVE_GO"

# ---------------------------------------------------------------------------
# Validation patterns for values interpolated into Athena SQL
# ---------------------------------------------------------------------------
_STAGE_RE = re.compile(r"^[a-z]{1,16}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# ---------------------------------------------------------------------------
# Region constants
# ---------------------------------------------------------------------------
ADP_REGION = "us-east-1"
CVX_REGION = "us-west-2"

# ---------------------------------------------------------------------------
# Agent-part constants
# ---------------------------------------------------------------------------
#: Write window during which --part agent is refused (matches adp job windows).
_WRITE_WINDOW_START_MIN = 4 * 60 + 50   # 04:50 UTC
_WRITE_WINDOW_END_MIN   = 6 * 60 + 10   # 06:10 UTC
#: Timezone names accepted by the scheduler guard.
_UTC_ZONES = frozenset({"UTC", "Etc/UTC"})
#: rate() schedule components safe to ignore (non-cron, diagnostics-only, 5-min).
_SAFE_RATE_COMPONENTS = frozenset({"diagnostics"})
#: Lambda function names
_TIER2_LAMBDA_FMT = "vsa-{stage}-tier2-health-agent-{region}"
#: DynamoDB table names
_AVX_FINDINGS_TABLE_FMT   = "vsa-{stage}-avx-findings"
_AVX_AGENT_RUNS_TABLE_FMT = "vsa-{stage}-avx-agent-runs"
#: Run record partition key for rebalancing (agent_id#component).
_REBALANCE_AGENT_COMPONENT = "agent-fi-rebalancing#rebalance"
#: Rental fleet id used across G-checks.
_RENTAL_FLEET = "demo-rental-carshare"
#: Lambda invoke timeout (must exceed the 15-min function timeout).
_LAMBDA_READ_TIMEOUT_S = 960  # 16 min


# ---------------------------------------------------------------------------
# Time helpers (agent part)
# ---------------------------------------------------------------------------


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def _iso(t: datetime.datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")


def _in_write_window() -> bool:
    """True when UTC time is inside the scheduled-job write window (04:50–06:10)."""
    n = _now()
    mins = n.hour * 60 + n.minute
    return _WRITE_WINDOW_START_MIN <= mins <= _WRITE_WINDOW_END_MIN


# ---------------------------------------------------------------------------
# Signal helper (agent part — used during SSM restore in G5)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _signals_held() -> Iterator[None]:
    """Hold SIGINT/SIGTERM/SIGHUP during a cleanup block; re-deliver after."""
    held: list[int] = []

    def _hold(signum: int, frame: object) -> None:
        held.append(signum)

    sigs = (_sig.SIGINT, _sig.SIGTERM, _sig.SIGHUP)
    old = {s: _sig.getsignal(s) for s in sigs}
    for s in sigs:
        _sig.signal(s, _hold)
    try:
        yield
    finally:
        for s, handler in old.items():
            _sig.signal(s, handler)
        if held:
            if held[0] == _sig.SIGINT:
                raise KeyboardInterrupt
            raise SystemExit(143)


# ---------------------------------------------------------------------------
# EB Scheduler timing guard (agent part)
# ---------------------------------------------------------------------------


def _check_scheduler_timing(sched: Any, fn_arn: str) -> tuple[bool, str]:
    """List EB Scheduler schedules targeting fn_arn; check cron timing.

    Refuses (fail-closed) if any cron schedule fires within ±45 min of now.
    rate() schedules are accepted only when Target.Input.component is in
    _SAFE_RATE_COMPONENTS. Zero matching schedules, a parse error, or a list
    error also refuse. DISABLED schedules are skipped.  Timezone must be UTC.
    """
    try:
        summaries: list[dict] = []
        kw: dict = {}
        while True:
            resp = sched.list_schedules(**kw)
            for s in resp.get("Schedules", []):
                if (s.get("Target") or {}).get("Arn") == fn_arn:
                    summaries.append(s)
            tok = resp.get("NextToken")
            if not tok:
                break
            kw["NextToken"] = tok
    except Exception as exc:  # noqa: BLE001
        return False, f"list_schedules error: {str(exc)[:120]}"

    if not summaries:
        return False, "0 schedule(s) matched Lambda ARN — refusing (fail closed)"

    n = _now()
    now_min = n.hour * 60 + n.minute
    disabled: list[str] = []

    for s in summaries:
        name = s.get("Name", "?")
        group_name = s.get("GroupName") or "default"
        try:
            full = sched.get_schedule(Name=name, GroupName=group_name)
        except Exception as exc:  # noqa: BLE001
            return False, f"get_schedule({name!r}) error: {str(exc)[:120]}"

        if full.get("State") == "DISABLED":
            disabled.append(name)
            continue

        expr = full.get("ScheduleExpression", "")
        tz = full.get("ScheduleExpressionTimezone", "UTC") or "UTC"
        if tz not in _UTC_ZONES:
            return False, f"schedule {name!r}: timezone {tz!r} is not UTC — refusing"

        if expr.startswith("cron("):
            m = re.match(r"^cron\((\d+)\s+(\d+)\s+\*\s+\*\s+\?\s+\*\)$", expr)
            if not m:
                return False, f"unparseable cron {expr!r} on schedule {name!r}"
            minute, hour = int(m.group(1)), int(m.group(2))
            run_min = hour * 60 + minute
            # Normalise to [-720, 719] to handle midnight wrap (W11).
            delta = (now_min - run_min + 720) % 1440 - 720
            if -45 <= delta <= 45:
                return False, (
                    f"schedule {name!r} cron at {hour:02d}:{minute:02d}Z; "
                    f"now {n.hour:02d}:{n.minute:02d}Z is within ±45 min"
                )
        elif expr.startswith("rate("):
            try:
                inp = json.loads((full.get("Target") or {}).get("Input") or "{}")
            except (ValueError, TypeError):
                return False, f"rate schedule {name!r}: can't parse Target.Input"
            component = inp.get("component", "")
            if component not in _SAFE_RATE_COMPONENTS:
                return False, (
                    f"rate schedule {name!r}: component {component!r} "
                    f"not in safe set {_SAFE_RATE_COMPONENTS}"
                )
        else:
            return False, f"schedule {name!r}: unknown expression {expr!r}"

    enabled = len(summaries) - len(disabled)
    skip_msg = f"; {len(disabled)} disabled skipped" if disabled else ""
    return True, f"{enabled} enabled schedule(s) checked{skip_msg}"

# ---------------------------------------------------------------------------
# Glue objects in adp_{stage}_fleet_rebalance
# ---------------------------------------------------------------------------
_RAW_TABLES = (
    "booking_requests",
    "vehicle_days",
    "bookings_runs",
    "metro_events",
    "fleet_bookings_forecast",
    "fleet_bookings_parameters",
)
# Views from platform-foundation/source/data-products/fleet_bookings/apply_ddl.py
_VIEWS = (
    "supply_demand_gaps",
    "location_daily",
    "move_outcomes",
    "vehicle_utilization_daily",
    "utilization_by_region",
)
# Snapshot tables from tests/test_materialise_sql.py _SDG_LOC/_LD_LOC/_MO_LOC
_SNAPSHOT_TABLES = (
    "supply_demand_gaps_snapshot",
    "location_daily_snapshot",
    "move_outcomes_snapshot",
)

_RENTAL_FLEET = "demo-rental-carshare"
# ADP lake S3 bucket format: adp-{stage}-foundation-lake-{account}-{region}
_LAKE_BUCKET_FMT = "adp-{stage}-foundation-lake-{account}-{region}"
# Restricted S3 prefix for the answer key (handler_bookings._ANSWER_KEY_PREFIX)
_ANSWER_KEY_PREFIX = "restricted/fleet_bookings/answer_key"
# ADP fleet-rebalance CFN stack name (_stage_name(stage, "fleet-rebalance"))
_FR_STACK_FMT = "adp-{stage}-foundation-fleet-rebalance"
# Bookings job role name (_stage_name(stage, f"fr-bookings-{region}"))
_BOOKINGS_ROLE_FMT = "adp-{stage}-foundation-fr-bookings-{region}"

# ---------------------------------------------------------------------------
# Read-only guard (installed before the first client, via _make_guarded_session)
# ---------------------------------------------------------------------------


class GuardViolation(RuntimeError):
    """Raised by the botocore guard when a call is not on the allowlist."""


def _make_guarded_bcsession(
    allow_writes: bool,
    stage: str,
    workgroup: str,
    fn_name: str,
    cms_only: bool = False,
    auto_only: bool = False,
) -> Any:
    """Return a botocore Session with a before-parameter-build event guard.

    Always allowed (any service): ops starting Get/List/Describe/BatchGet;
    athena GetQueryExecution/GetQueryResults; iam SimulatePrincipalPolicy;
    sts GetCallerIdentity; dynamodb Scan/Query; s3 HeadObject;
    cloudwatch-logs FilterLogEvents.

    athena StartQueryExecution: allowed only when QueryString.lstrip() starts
    with 'SELECT ' or 'SHOW ', WorkGroup equals *workgroup*, and no
    ResultConfiguration is present.

    When *allow_writes* is True: additionally allows —
      lambda Invoke with FunctionName == fn_name, InvocationType
        'RequestResponse', and Payload == {"component": "rebalance"};
      ssm PutParameter with Name == /vsa/{stage}/avx/fi-rebalancing/policy,
        Type 'String', Overwrite True;
      cognito-identity-provider AdminCreateUser/AdminSetUserPassword/
        AdminAddUserToGroup/AdminDeleteUser with Username starting
        'livetest-fi-rebal-g4-'; InitiateAuth unconditionally.

    With *cms_only* (--part cms), --allow-writes admits only the Cognito temp-user calls:
    the Lambda invoke and the policy write belong to --part agent.

    With *auto_only* (--part auto), --allow-writes admits:
      lambda Invoke with FunctionName == fn_name (the auto-approver) and
        InvocationType 'RequestResponse' (any payload);
      cognito-identity-provider writes as for cms_only (temp operators).

    Anything else raises GuardViolation.
    """
    import botocore.session as _bcs

    bc = _bcs.Session()

    def _guard(params: dict, model: Any, event_name: str, **_kw: Any) -> None:
        parts = event_name.split(".")
        if len(parts) < 3:
            return
        # botocore emits hyphenated service ids, e.g. cognito-identity-provider
        service = parts[1]
        op = parts[2]

        # Prefix-based allowlist: read operations on any service
        for pfx in ("Get", "List", "Describe", "BatchGet"):
            if op.startswith(pfx):
                return

        # Athena
        if service == "athena":
            if op in ("GetQueryExecution", "GetQueryResults"):
                return
            if op == "StartQueryExecution":
                qs = (params.get("QueryString") or "").lstrip()
                wg = params.get("WorkGroup") or ""
                rc = params.get("ResultConfiguration")
                if (qs.startswith("SELECT ") or qs.startswith("SHOW ")):
                    if wg == workgroup and not rc:
                        return
                raise GuardViolation(
                    f"athena StartQueryExecution blocked: "
                    f"SQL must start with SELECT /SHOW , WorkGroup must be "
                    f"{workgroup!r} and no ResultConfiguration; "
                    f"got qs={qs[:60]!r} wg={wg!r} rc={bool(rc)}"
                )

        # IAM
        if service == "iam" and op == "SimulatePrincipalPolicy":
            return

        # STS
        if service == "sts" and op == "GetCallerIdentity":
            return

        # DynamoDB read
        if service == "dynamodb" and op in ("Scan", "Query"):
            return

        # S3
        if service == "s3" and op == "HeadObject":
            return

        # Cognito: confirming the temp user is gone (G4b cleanup) is a read
        if service == "cognito-identity-provider" and op == "AdminGetUser":
            return

        # CloudWatch Logs (botocore service id is cloudwatch-logs)
        if service == "cloudwatch-logs" and op == "FilterLogEvents":
            return

        # Write operations — only when allow_writes is True
        if allow_writes:
            # Lambda Invoke: strict check on all three params
            if service == "lambda" and op == "Invoke" and not cms_only:
                got_fn = params.get("FunctionName") or ""
                got_inv = params.get("InvocationType") or ""
                raw_pl = params.get("Payload") or b""
                try:
                    got_pl = json.loads(raw_pl)
                except Exception:  # noqa: BLE001
                    got_pl = None
                if auto_only:
                    # --part auto: invoke the auto-approver with any payload
                    if got_fn == fn_name and got_inv == "RequestResponse":
                        return
                    raise GuardViolation(
                        f"lambda Invoke blocked (auto_only): FunctionName={got_fn!r} "
                        f"(want {fn_name!r}), InvocationType={got_inv!r}"
                    )
                if (
                    got_fn == fn_name
                    and got_inv == "RequestResponse"
                    and got_pl == {"component": "rebalance"}
                ):
                    return
                raise GuardViolation(
                    f"lambda Invoke blocked: FunctionName={got_fn!r} "
                    f"(want {fn_name!r}), InvocationType={got_inv!r}, "
                    f"payload={got_pl!r}"
                )

            # SSM PutParameter: only the policy parameter for this stage
            if service == "ssm" and op == "PutParameter" and not cms_only:
                want_name = f"/vsa/{stage}/avx/fi-rebalancing/policy"
                got_name = params.get("Name") or ""
                got_type = params.get("Type") or ""
                got_ow = params.get("Overwrite")
                if got_name == want_name and got_type == "String" and got_ow is True:
                    return
                raise GuardViolation(
                    f"ssm PutParameter blocked: Name={got_name!r} "
                    f"(want {want_name!r}), Type={got_type!r}, Overwrite={got_ow!r}"
                )

            # Cognito write operations
            if service == "cognito-identity-provider":
                if op == "InitiateAuth":
                    return
                if op in (
                    "AdminCreateUser", "AdminSetUserPassword",
                    "AdminAddUserToGroup", "AdminDeleteUser",
                ):
                    got_user = params.get("Username") or ""
                    if not got_user.startswith("livetest-fi-rebal-g4-"):
                        raise GuardViolation(
                            f"cognito {op} blocked: Username={got_user!r} "
                            f"(must start with 'livetest-fi-rebal-g4-')"
                        )
                    # The temp users are fleet-operators only, never another group.
                    if op == "AdminAddUserToGroup" and params.get("GroupName") != "fleet-operator":
                        raise GuardViolation(
                            f"cognito AdminAddUserToGroup blocked: GroupName="
                            f"{params.get('GroupName')!r} (only 'fleet-operator')")
                    if op == "AdminSetUserPassword" and params.get("Permanent") is not True:
                        raise GuardViolation("cognito AdminSetUserPassword blocked: not Permanent")
                    return

        raise GuardViolation(
            f"blocked operation: {event_name} (allow_writes={allow_writes})"
        )

    bc.get_component("event_emitter").register("before-parameter-build.*.*", _guard)
    return bc


_SESSION: Any = None


def _init_session(
    *,
    allow_writes: bool,
    stage: str,
    workgroup: str,
    fn_name: str,
    cms_only: bool = False,
    auto_only: bool = False,
) -> None:
    """Initialize the shared guarded boto3 session (keyword-only; idempotent).

    Must be called before the first _client() call.  Subsequent calls are
    no-ops; the first call fixes allow_writes/stage/workgroup/fn_name for
    the lifetime of the process.
    """
    global _SESSION
    if _SESSION is not None:
        return
    import boto3
    _SESSION = boto3.Session(
        botocore_session=_make_guarded_bcsession(allow_writes, stage, workgroup, fn_name,
                                                 cms_only=cms_only, auto_only=auto_only)
    )


def _client(service: str, region: str, **kwargs: Any) -> Any:
    """Build a boto3 client through the shared guarded session.

    Raises RuntimeError if _init_session has not been called.
    """
    if _SESSION is None:
        raise RuntimeError(
            "_client called before _init_session; "
            "call _init_session(allow_writes=..., stage=..., workgroup=..., fn_name=...) first"
        )
    return _SESSION.client(service, region_name=region, **kwargs)


# ---------------------------------------------------------------------------
# Athena helper
# ---------------------------------------------------------------------------


def athena_rows(ath: Any, sql: str, wg: str) -> list[list[str | None]]:
    """Execute *sql* in workgroup *wg*; return data rows (header row stripped).

    Each row is a list of ``str | None`` (VarCharValue or None when absent).
    Raises RuntimeError on FAILED / CANCELLED.
    """
    qid: str = ath.start_query_execution(QueryString=sql, WorkGroup=wg)["QueryExecutionId"]
    status: dict = {}
    for _ in range(300):
        resp = ath.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
        status = resp["Status"]
        if status["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(2)
    if status["State"] != "SUCCEEDED":
        reason = status.get("StateChangeReason", "")
        raise RuntimeError(f"Athena {status['State']}: {reason}")
    rows: list[list[str | None]] = []
    kw: dict[str, Any] = {"QueryExecutionId": qid}
    while True:
        page = ath.get_query_results(**kw)
        rows += [
            [c.get("VarCharValue") for c in row["Data"]]
            for row in page["ResultSet"]["Rows"]
        ]
        token = page.get("NextToken")
        if not token:
            return rows[1:]  # strip header
        kw["NextToken"] = token


# ---------------------------------------------------------------------------
# Result recording
# ---------------------------------------------------------------------------


def record(results: list[tuple[str, str, str]], name: str, status: str, detail: str) -> None:
    """Append *(name, status, detail)* and print a formatted line."""
    results.append((name, status, detail))
    print(f"{status:8} {name}: {detail}", flush=True)


# ---------------------------------------------------------------------------
# Item checks A1–A7 (each independent; wrapped in try/except by _part_adp)
# ---------------------------------------------------------------------------


def check_a1_tables(ath: Any, fr_db: str, wg: str, results: list) -> None:
    """A1: all tables/views/snapshots exist; print COUNT(*) each.

    Validates *fr_db* shape before interpolation. Tries every object; records
    individual FAIL rows for missing ones, then a summary PASS/FAIL.
    """
    if not _ID_RE.match(fr_db):
        record(results, "A1 tables", "FAIL", f"fr_db fails validation: {fr_db!r}")
        return

    all_objects = list(_RAW_TABLES) + list(_VIEWS) + list(_SNAPSHOT_TABLES)
    counts: dict[str, str] = {}
    failed: list[str] = []

    for obj in all_objects:
        sql = f"SELECT count(*) FROM {fr_db}.{obj}"
        try:
            rows = athena_rows(ath, sql, wg)
            counts[obj] = rows[0][0] if rows else "0"
        except Exception as exc:  # noqa: BLE001
            counts[obj] = f"ERROR: {exc}"[:80]
            failed.append(obj)

    summary = ", ".join(f"{o}={counts[o]}" for o in all_objects)
    if failed:
        record(results, "A1 tables", "FAIL",
               f"missing/errored ({len(failed)}): {failed}; counts: {summary}")
    else:
        record(results, "A1 tables", "PASS",
               f"all {len(all_objects)} objects present; counts: {summary}")


def check_a2_dates(
    ath: Any, fr_db: str, wg: str, expect_after: str | None, results: list
) -> None:
    """A2: max(data_as_of) / max(run_date) must be strictly after --expect-after if given.

    Queries bookings_runs for max(data_as_of) and fleet_bookings_forecast for max(run_date).
    With --expect-after YYYY-MM-DD both values must be strictly later (the scheduled jobs
    ran since the seed window). Without --expect-after, records PASS with the dates only.
    """
    if not _ID_RE.match(fr_db):
        record(results, "A2 dates", "FAIL", f"fr_db fails validation: {fr_db!r}")
        return

    if expect_after is not None and not _DATE_RE.match(expect_after):
        record(results, "A2 dates", "FAIL",
               f"--expect-after value fails date validation: {expect_after!r}")
        return

    try:
        rows_br = athena_rows(
            ath, f"SELECT max(data_as_of) FROM {fr_db}.bookings_runs", wg
        )
        data_as_of: str | None = rows_br[0][0] if rows_br else None
    except Exception as exc:  # noqa: BLE001
        record(results, "A2 dates", "FAIL", f"bookings_runs query failed: {exc}"[:200])
        return

    try:
        rows_fc = athena_rows(
            ath, f"SELECT max(run_date) FROM {fr_db}.fleet_bookings_forecast", wg
        )
        run_date: str | None = rows_fc[0][0] if rows_fc else None
    except Exception as exc:  # noqa: BLE001
        record(results, "A2 dates", "FAIL",
               f"fleet_bookings_forecast query failed: {exc}"[:200])
        return

    detail = f"bookings_runs.max(data_as_of)={data_as_of!r}, fleet_bookings_forecast.max(run_date)={run_date!r}"

    if expect_after is not None:
        problems: list[str] = []
        if data_as_of is None or data_as_of <= expect_after:
            problems.append(
                f"data_as_of {data_as_of!r} is not strictly after {expect_after!r}"
            )
        if run_date is None or run_date <= expect_after:
            problems.append(
                f"run_date {run_date!r} is not strictly after {expect_after!r}"
            )
        if problems:
            record(results, "A2 dates", "FAIL",
                   "; ".join(problems) + f" ({detail})")
            return

    record(results, "A2 dates", "PASS", detail)


def check_a3_answer_key(s3_client: Any, lake_bucket: str, results: list) -> None:
    """A3: plants JSON + at least one parquet exist under the restricted prefix.

    Checks:
    - plants/fleet_bookings_answer_key.json exists
    - at least one <date>.parquet exists under the prefix
    """
    if not _ID_RE.match(lake_bucket.replace(".", "-").replace("_", "-")):
        record(results, "A3 answer key", "FAIL",
               f"lake_bucket fails basic validation: {lake_bucket!r}")
        return

    plants_key = f"{_ANSWER_KEY_PREFIX}/plants/fleet_bookings_answer_key.json"
    # Check plants file exists
    try:
        s3_client.head_object(Bucket=lake_bucket, Key=plants_key)
    except Exception as exc:  # noqa: BLE001
        record(results, "A3 answer key", "FAIL",
               f"plants JSON missing ({plants_key}): {exc}"[:200])
        return

    # Check at least one parquet under the prefix
    resp = s3_client.list_objects_v2(
        Bucket=lake_bucket, Prefix=f"{_ANSWER_KEY_PREFIX}/", MaxKeys=100,
    )
    parquets = [
        o["Key"] for o in resp.get("Contents", [])
        if o["Key"].endswith(".parquet")
    ]
    if not parquets:
        record(results, "A3 answer key", "FAIL",
               f"no parquet files found under s3://{lake_bucket}/{_ANSWER_KEY_PREFIX}/")
        return

    record(results, "A3 answer key", "PASS",
           f"plants JSON present; {len(parquets)} parquet(s) found: {parquets[:3]}")


def check_a4_iam(
    iam_client: Any,
    lam_cvx: Any,   # Lambda client in CVX_REGION (for AVX tier2 health-agent)
    lam_cms: Any,   # Lambda client in CVX_REGION (for CMS FI function)
    lake_bucket: str,
    stage: str,
    account_id: str,
    results: list,
) -> None:
    """A4: consumer roles cannot GetObject the key; bookings role can PutObject.

    Reads the actual Lambda execution roles (no guessing), then uses iam:SimulatePrincipalPolicy.
    PASS only when both consumer roles are NOT allowed GetObject, AND the bookings job role IS
    allowed PutObject (positive control proving the policy exists and would catch real access).
    """
    # Look up AVX tier2 Lambda execution role
    avx_fn = f"vsa-{stage}-tier2-health-agent-{CVX_REGION}"
    try:
        avx_role_arn: str = lam_cvx.get_function_configuration(FunctionName=avx_fn)["Role"]
    except Exception as exc:  # noqa: BLE001
        record(results, "A4 IAM", "FAIL",
               f"cannot get AVX tier2 Lambda config ({avx_fn}): {exc}"[:200])
        return

    # Look up CMS FleetIntelligence Lambda execution role
    cms_fi_prefix = f"cms-{stage}-ui-FleetIntelligenceFunction"
    try:
        cms_fns: list[str] = []
        paginator = lam_cms.get_paginator("list_functions")
        for page in paginator.paginate():
            cms_fns += [
                f["FunctionName"] for f in page["Functions"]
                if f["FunctionName"].startswith(cms_fi_prefix)
            ]
        if not cms_fns:
            record(results, "A4 IAM", "FAIL",
                   f"no Lambda starting with {cms_fi_prefix!r} found")
            return
        cms_role_arn: str = lam_cms.get_function_configuration(
            FunctionName=cms_fns[0]
        )["Role"]
    except Exception as exc:  # noqa: BLE001
        record(results, "A4 IAM", "FAIL",
               f"cannot get CMS FI Lambda config: {exc}"[:200])
        return

    # ADP bookings job role ARN (positive control)
    bookings_role_name = _BOOKINGS_ROLE_FMT.format(stage=stage, region=ADP_REGION)
    bookings_role_arn = f"arn:aws:iam::{account_id}:role/{bookings_role_name}"

    # Key object ARN for the simulation (use the plants JSON as the specific object)
    key_arn = (
        f"arn:aws:s3:::{lake_bucket}/"
        f"{_ANSWER_KEY_PREFIX}/plants/fleet_bookings_answer_key.json"
    )

    problems: list[str] = []
    checks: list[tuple[str, str, str, str]] = [
        ("AVX tier2",  avx_role_arn,  "s3:GetObject", "not allowed"),
        ("CMS FI",     cms_role_arn,  "s3:GetObject", "not allowed"),
        ("ADP bookings job (positive control)", bookings_role_arn, "s3:PutObject", "allowed"),
    ]
    detail_parts: list[str] = []

    for label, role_arn, action, expectation in checks:
        try:
            resp = iam_client.simulate_principal_policy(
                PolicySourceArn=role_arn,
                ActionNames=[action],
                ResourceArns=[key_arn],
            )
            decision: str = resp["EvaluationResults"][0]["EvalDecision"]
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{label}: simulate failed: {exc}"[:150])
            detail_parts.append(f"{label}=ERROR")
            continue

        detail_parts.append(f"{label}={decision}")
        if expectation == "not allowed" and decision == "allowed":
            problems.append(
                f"{label} ({role_arn.split('/')[-1]}): s3:GetObject is ALLOWED "
                "(must be denied to protect the answer key)"
            )
        elif expectation == "allowed" and decision != "allowed":
            problems.append(
                f"positive control FAILED: {label} ({role_arn.split('/')[-1]}): "
                f"s3:PutObject decision={decision!r} (expected 'allowed')"
            )

    detail = f"key_arn={key_arn!r}; " + "; ".join(detail_parts)
    if problems:
        record(results, "A4 IAM", "FAIL", "; ".join(problems) + f" [{detail}]")
    else:
        record(results, "A4 IAM", "PASS", detail)


def check_a5_sns(
    sns_client: Any, cfn_client: Any, stage: str, results: list
) -> None:
    """A5: every SNS topic in the fleet-rebalance stack has a confirmed subscription.

    Lists stack resources of adp-{stage}-foundation-fleet-rebalance to find SNS topics,
    then checks each has at least one subscription that is not PendingConfirmation.
    """
    stack_name = _FR_STACK_FMT.format(stage=stage)
    try:
        topics: list[str] = []
        paginator = cfn_client.get_paginator("list_stack_resources")
        for page in paginator.paginate(StackName=stack_name):
            topics += [
                r["PhysicalResourceId"]
                for r in page["StackResourceSummaries"]
                if r["ResourceType"] == "AWS::SNS::Topic"
            ]
    except Exception as exc:  # noqa: BLE001
        record(results, "A5 SNS", "FAIL",
               f"cannot list stack resources for {stack_name}: {exc}"[:200])
        return

    if not topics:
        record(results, "A5 SNS", "FAIL",
               f"no SNS topics found in stack {stack_name}")
        return

    problems: list[str] = []
    detail_parts: list[str] = []

    for topic_arn in topics:
        try:
            resp = sns_client.list_subscriptions_by_topic(TopicArn=topic_arn)
            subs = resp.get("Subscriptions", [])
        except Exception as exc:  # noqa: BLE001
            problems.append(f"list_subscriptions_by_topic({topic_arn}): {exc}"[:150])
            detail_parts.append(f"{topic_arn.split(':')[-1]}=ERROR")
            continue

        confirmed = [
            s for s in subs
            if s.get("SubscriptionArn") and s["SubscriptionArn"] != "PendingConfirmation"
        ]
        topic_short = topic_arn.split(":")[-1]
        detail_parts.append(
            f"{topic_short}: {len(confirmed)}/{len(subs)} confirmed"
        )
        if not confirmed:
            problems.append(
                f"topic {topic_short!r} has no confirmed subscription "
                f"({len(subs)} total, all PendingConfirmation or none)"
            )

    detail = f"stack={stack_name!r}; " + "; ".join(detail_parts)
    if problems:
        record(results, "A5 SNS", "FAIL", "; ".join(problems) + f" [{detail}]")
    else:
        record(results, "A5 SNS", "PASS", detail)


def check_a6_vins(ath: Any, stage: str, wg: str, results: list) -> None:
    """A6 (carried item 3): the bookings job's input refusals, checked per VIN.

    Mirrors ``fleet_bookings/handler_bookings.py`` + ``job_inputs.py``: the rental fleet's
    VINs (``cms_ingest.vehicles``, fleet ``demo-rental-carshare``); each needs exactly one
    ``dimensions.vins`` row (``in_service_dates``) and a seed record among the fleet's
    ``vehicle_moves`` (``m.fleet_id`` filtered, as the job's SQL does); two seed records
    naming one VIN with different locations are refused. Anti-joins and GROUP BY, so a
    duplicate row cannot offset a missing one (review cycle 1 W5). FAIL on any non-zero.
    """
    if not _STAGE_RE.match(stage):
        record(results, "A6 rental VINs", "FAIL", f"stage fails validation: {stage!r}")
        return
    ci, dim = f"adp_{stage}_cms_ingest", f"adp_{stage}_dimensions"
    fleet = "'" + _RENTAL_FLEET + "'"  # a constant, not input
    # Subqueries, not a WITH clause: the script's own Athena guard admits only statements that
    # start with SELECT or SHOW (security review g6-lv c1 W-F: the WITH form was blocked live).
    rv = f"(SELECT DISTINCT vin FROM {ci}.vehicles WHERE fleet_id = {fleet})"
    seeds = (f"(SELECT mv.vin AS vin, m.to_location_id AS to_location_id "
             f"FROM {ci}.vehicle_moves m CROSS JOIN UNNEST(m.vins) AS mv(vin) "
             f"WHERE m.fleet_id = {fleet} AND m.decided_by_kind = 'seed')")
    sql = (
        "SELECT "
        f"(SELECT count(*) FROM {rv} rv), "
        f"(SELECT count(*) FROM {rv} rv LEFT JOIN {dim}.vins d ON d.vin = rv.vin WHERE d.vin IS NULL), "
        f"(SELECT count(*) FROM (SELECT d.vin FROM {dim}.vins d JOIN {rv} rv ON rv.vin = d.vin "
        "GROUP BY d.vin HAVING count(*) > 1)), "
        f"(SELECT count(*) FROM {rv} rv LEFT JOIN (SELECT DISTINCT vin FROM {seeds} sd) s "
        "ON s.vin = rv.vin WHERE s.vin IS NULL), "
        f"(SELECT count(*) FROM (SELECT vin FROM {seeds} sd GROUP BY vin "
        "HAVING count(DISTINCT to_location_id) > 1))"
    )
    try:
        (row,) = athena_rows(ath, sql, wg)
        rental, no_dim, dup_dim, no_seed, conflicting = (int(v or "0") for v in row)
    except Exception as exc:  # noqa: BLE001
        record(results, "A6 rental VINs", "FAIL", f"query failed: {exc}"[:200])
        return
    detail = (f"rental_vins={rental}; without dimensions.vins row={no_dim}; with repeated "
              f"dimensions rows={dup_dim}; without seed record={no_seed}; with conflicting "
              f"seed records={conflicting}")
    problems = [p for p, n in (("no rental VINs", rental == 0),
                               ("VINs without a dimensions.vins row", no_dim),
                               ("VINs with repeated dimensions.vins rows", dup_dim),
                               ("VINs without a seed record", no_seed),
                               ("VINs whose seed records disagree", conflicting)) if n]
    record(results, "A6 rental VINs", "FAIL" if problems else "PASS",
           (f"bookings job would refuse: {problems} [{detail}]") if problems else detail)


def check_a7_policy(
    ath: Any, ssm_client: Any, fr_db: str, wg: str, stage: str, results: list
) -> None:
    """A7: fleet_bookings_parameters matches the AVX SSM policy for three shared keys.

    Reads deficit_above, target_utilization, max_utilization from fleet_bookings_parameters
    (a single-row Athena table), reads and parses the SSM parameter, and compares the three
    fields that must agree between the two systems (policy.py § "surplus_below is not here").
    """
    from agents.fi_rebalancing.policy import parse as _policy_parse, PolicyUnavailable

    if not _ID_RE.match(fr_db):
        record(results, "A7 policy", "FAIL", f"fr_db fails validation: {fr_db!r}")
        return

    # Read the three shared keys from fleet_bookings_parameters
    _SHARED_KEYS = ("deficit_above", "target_utilization", "max_utilization")
    cols = ", ".join(_SHARED_KEYS)
    try:
        rows = athena_rows(
            ath, f"SELECT {cols} FROM {fr_db}.fleet_bookings_parameters LIMIT 1", wg
        )
        if not rows:
            record(results, "A7 policy", "FAIL",
                   "fleet_bookings_parameters is empty")
            return
        adp_vals = dict(zip(_SHARED_KEYS, rows[0]))
    except Exception as exc:  # noqa: BLE001
        record(results, "A7 policy", "FAIL",
               f"fleet_bookings_parameters query failed: {exc}"[:200])
        return

    # Read the SSM policy parameter
    param_name = f"/vsa/{stage}/avx/fi-rebalancing/policy"
    try:
        raw = ssm_client.get_parameter(Name=param_name)["Parameter"]["Value"]
        ssm_policy = _policy_parse(raw)
    except PolicyUnavailable as exc:
        record(results, "A7 policy", "FAIL",
               f"SSM policy invalid ({param_name}): {exc}"[:200])
        return
    except Exception as exc:  # noqa: BLE001
        record(results, "A7 policy", "FAIL",
               f"cannot read SSM parameter {param_name!r}: {exc}"[:200])
        return

    # Compare: ADP parameter row vs SSM policy for the three shared keys
    problems: list[str] = []
    detail_parts: list[str] = []
    for key in _SHARED_KEYS:
        adp_raw = adp_vals.get(key)
        ssm_val = ssm_policy.get(key)
        try:
            adp_num = float(adp_raw) if adp_raw is not None else None
        except (TypeError, ValueError):
            adp_num = None
        detail_parts.append(f"{key}: adp={adp_raw!r} ssm={ssm_val!r}")
        if adp_num is None or ssm_val is None or abs(adp_num - float(ssm_val)) > 1e-9:
            problems.append(
                f"{key} mismatch: ADP={adp_raw!r}, SSM={ssm_val!r}"
            )

    detail = f"param={param_name!r}; " + "; ".join(detail_parts)
    if problems:
        record(results, "A7 policy", "FAIL",
               "; ".join(problems) + f" [{detail}]")
    else:
        record(results, "A7 policy", "PASS", detail)


# ---------------------------------------------------------------------------
# DynamoDB helper (agent part)
# ---------------------------------------------------------------------------

def _ddb_deser(item: dict) -> dict:
    """Deserialize a DynamoDB low-level item (TypeDeserializer)."""
    from boto3.dynamodb.types import TypeDeserializer  # noqa: PLC0415
    d = TypeDeserializer()
    return {k: d.deserialize(v) for k, v in item.items()}


def _ddb_scan_all(ddb: Any, table: str, **kw: Any) -> list[dict]:
    """Page-complete Scan returning deserialized items (pages NextToken)."""
    items: list[dict] = []
    kwargs: dict = {"TableName": table, **kw}
    while True:
        resp = ddb.scan(**kwargs)
        items.extend(_ddb_deser(r) for r in resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return items
        kwargs["ExclusiveStartKey"] = lek


def _ddb_query_all(ddb: Any, table: str, **kw: Any) -> list[dict]:
    """Page-complete Query returning deserialized items."""
    items: list[dict] = []
    kwargs: dict = {"TableName": table, **kw}
    while True:
        resp = ddb.query(**kwargs)
        items.extend(_ddb_deser(r) for r in resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return items
        kwargs["ExclusiveStartKey"] = lek


# ---------------------------------------------------------------------------
# Agent part (G1-G5): shared state, signals, invoke and run helpers
#
# Review cycle 1 (review-g6-lv-cycle1.md) rebuilt this part: every item scores
# the run it names (C2, C3, C4), the guard admits only the calls the items make
# (C1), G5 reads the run-failure alarm by name with Stage B's method (C5), the
# policy override is restored on SIGTERM/SIGHUP with retries (C6), a failed
# invoke stops every later invoking item (W1), and the schedule guard runs
# before each invoke (W2).
# ---------------------------------------------------------------------------

_REBALANCE_AGENT_ID = "agent-fi-rebalancing"
_REBALANCE_KINDS = ("fleet.rebalance.move", "fleet.rebalance.gap")
_DELIVERY_FLEET = "demo-lastmile-delivery"
_G4_USER_PREFIX = "livetest-fi-rebal-g4-"
_MALFORMED_POLICY = '{"bad": true}'
#: Restore attempts for the policy parameter (G5) and the errors worth retrying.
_RESTORE_ATTEMPTS = 4
_RETRYABLE_SSM = ("ThrottlingException", "TooManyUpdates", "InternalServerError")
#: G5 waits: the run-failure alarm's Errors period is 5 minutes.
_G5_ALARM_WAIT_S = 600
_G5_ACTION_WAIT_S = 90

# Shared with _install_signal_handlers: True once a SIGTERM/SIGHUP became SystemExit.
_SIGNAL_STATE: dict[str, bool] = {"raised": False}


def _install_signal_handlers() -> None:
    """SIGTERM/SIGHUP raise SystemExit(143) once, so every ``finally`` runs (G5's policy
    restore, G4's temp-user deletion); a second signal is ignored so it cannot cut that
    cleanup short. Same as ``live-verify-fi-cost.py``. Review cycle 1 C6."""
    _SIGNAL_STATE["raised"] = False

    def _exit_once(signum: int, frame: object) -> None:
        if not _SIGNAL_STATE["raised"]:
            _SIGNAL_STATE["raised"] = True
            raise SystemExit(143)

    _sig.signal(_sig.SIGTERM, _exit_once)
    _sig.signal(_sig.SIGHUP, _exit_once)


def _parse_ts(value: Any) -> datetime.datetime | None:
    """An ISO-8601 UTC string (``...Z`` or ``+00:00``) as an aware datetime; None if not one."""
    if not isinstance(value, str) or not value:
        return None
    try:
        t = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        return None
    return t.astimezone(datetime.timezone.utc)


def _cents(value: Any) -> int:
    """Money as integer cents (ValueError when absent or not a number)."""
    from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation  # noqa: PLC0415
    if value is None or isinstance(value, bool):
        raise ValueError(f"not a money value: {value!r}")
    try:
        d = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"not a money value: {value!r}") from exc
    if not d.is_finite():
        raise ValueError(f"not a money value: {value!r}")
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))


class InvokeFailed(RuntimeError):
    """The rebalance invoke returned a FunctionError or an unusable payload."""

    def __init__(self, message: str, payload: dict | None = None) -> None:
        super().__init__(message)
        self.payload = payload or {}


class _AgentState:
    """What the items share: the run under test, and whether invoking may go on."""

    def __init__(self, *, stage: str, fn_name: str, fn_arn: str, sched: Any,
                 allow_writes: bool) -> None:
        self.stage = stage
        self.fn_name = fn_name
        self.fn_arn = fn_arn
        self.sched = sched
        self.allow_writes = allow_writes
        self.invoke_failed = False
        self.run: dict | None = None        # G1's run: started_at, finished_at, record, payload
        self.run_items: list[dict] = []     # G1's run's live items (G2 and G3 read them)
        self.gaps: list[dict] | None = None # the gaps snapshot rows G1 scored against
        self.policy: dict | None = None     # the SSM policy the run read


def _may_invoke(state: _AgentState) -> str | None:
    """None when an invoke may start now; otherwise the reason it may not.

    Re-checked before every invoking item (W2): the time window, the Tier 2 schedules
    (+-45 min of any enabled cron) and any earlier failed invoke (W1).
    """
    if not state.allow_writes:
        return "--allow-writes not given"
    if state.invoke_failed:
        return "an earlier invoke failed, so later invoking items do not run"
    if _in_write_window():
        return "inside the 04:50-06:10 UTC window"
    ok, msg = _check_scheduler_timing(state.sched, state.fn_arn)
    if not ok:
        return f"scheduler guard: {msg}"
    return None


def _invoke_rebalance(lam: Any, fn_name: str) -> dict:
    """Sync-invoke the Tier 2 Lambda with ``{"component": "rebalance"}``; return its payload.

    The payload is the runner's run summary (``started_at``, ``finished_at``, ``status``,
    ``artifacts_written``, ``duplicates_skipped``, ``tokens_used`` and the run's stats).
    Raises :class:`InvokeFailed` on a FunctionError (``.payload`` holds the error body:
    ``errorType``, ``errorMessage``) or a payload without ``started_at``.
    """
    resp = lam.invoke(
        FunctionName=fn_name,
        InvocationType="RequestResponse",
        Payload=json.dumps({"component": "rebalance"}).encode(),
    )
    raw = resp["Payload"].read()
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    try:
        body = json.loads(text) if text else {}
    except ValueError:
        body = {"_raw": text[:300]}
    if not isinstance(body, dict):
        body = {"_raw": text[:300]}
    if resp.get("FunctionError"):
        raise InvokeFailed(f"FunctionError={resp['FunctionError']!r} "
                           f"errorType={body.get('errorType')!r}: {text[:300]}", body)
    if not isinstance(body.get("started_at"), str) or not body.get("started_at"):
        raise InvokeFailed(f"payload has no started_at: {text[:300]}", body)
    return body


def _run_record(ddb: Any, stage: str, started_at: str, *, wait_s: float = 30.0) -> dict | None:
    """The rebalance run record keyed exactly by *started_at*, or None."""
    table = _AVX_AGENT_RUNS_TABLE_FMT.format(stage=stage)
    deadline = time.monotonic() + wait_s
    while True:
        items = _ddb_query_all(
            ddb, table,
            KeyConditionExpression="agent_component = :k AND started_at = :s",
            ExpressionAttributeValues={":k": {"S": _REBALANCE_AGENT_COMPONENT},
                                       ":s": {"S": started_at}},
            ConsistentRead=True,
        )
        if items:
            return items[0]
        if time.monotonic() >= deadline:
            return None
        time.sleep(3)


def _runs_since(ddb: Any, stage: str, since: datetime.datetime) -> list[dict]:
    """Rebalance run records whose ``started_at`` is at or after *since* (one Query)."""
    table = _AVX_AGENT_RUNS_TABLE_FMT.format(stage=stage)
    return _ddb_query_all(
        ddb, table,
        KeyConditionExpression="agent_component = :k AND started_at >= :s",
        ExpressionAttributeValues={":k": {"S": _REBALANCE_AGENT_COMPONENT},
                                   ":s": {"S": _iso(since - datetime.timedelta(seconds=5))}},
        ConsistentRead=True,
    )


def _invoke_run(state: _AgentState, lam: Any, ddb: Any) -> dict:
    """One rebalance run: invoke, then its run record. Marks ``invoke_failed`` on any failure."""
    reason = _may_invoke(state)
    if reason:
        raise _NotRun(reason)
    try:
        payload = _invoke_rebalance(lam, state.fn_name)
        record_ = _run_record(ddb, state.stage, payload["started_at"])
        if record_ is None:
            raise InvokeFailed(f"no run record for started_at={payload['started_at']!r}", payload)
    except BaseException:
        state.invoke_failed = True
        raise
    return {"started_at": payload["started_at"],
            "finished_at": record_.get("finished_at") or payload.get("finished_at", ""),
            "record": record_, "payload": payload}


class _NotRun(Exception):
    """An item did not run (its precondition failed); recorded as SKIP."""


def _all_rebalancing_findings(ddb: Any, stage: str) -> list[dict]:
    """Every row the rebalancing agent wrote (page-complete Scan; a filtered page proves nothing)."""
    return _ddb_scan_all(
        ddb, _AVX_FINDINGS_TABLE_FMT.format(stage=stage),
        FilterExpression="agent_id = :aid",
        ExpressionAttributeValues={":aid": {"S": _REBALANCE_AGENT_ID}},
    )


def _items_of_run(findings: list[dict], run: dict, now: datetime.datetime) -> tuple[list, list]:
    """(the run's live rental move/gap items, ids of rows whose times don't parse).

    A run stamps ``computed_at`` on every item it puts, updates or refreshes (the writer's
    keep-alive sets it), so the run's items are the live ones whose ``computed_at`` falls
    inside the run's ``started_at``..``finished_at``. Older or expired rows are not this
    run's output (C2a).
    """
    lo, hi = _parse_ts(run["started_at"]), _parse_ts(run["finished_at"])
    if lo is None or hi is None:
        raise ValueError(f"run times don't parse: {run['started_at']!r}..{run['finished_at']!r}")
    # Run records carry millisecond started_at; computed_at is whole seconds
    # (runner._utc_now), so an item computed in the run's first second reads as
    # earlier than the start. Floor the start to its second (issue
    # 2026-10-27-fi-sweep-avx-ms-run-start-readers).
    lo = lo.replace(microsecond=0)
    out, bad = [], []
    for f in findings:
        if f.get("finding_kind") not in _REBALANCE_KINDS:
            continue
        if (f.get("scope") or {}).get("fleet_id") != _RENTAL_FLEET:
            continue
        exp, ca = _parse_ts(f.get("expires_at")), _parse_ts(f.get("computed_at"))
        if exp is None or ca is None:
            bad.append(f.get("finding_id", "?"))
            continue
        if exp > now and lo <= ca <= hi:
            out.append(f)
    return out, bad


def _read_gaps(ath: Any, stage: str, wg: str) -> list[dict]:
    """The gaps snapshot exactly as the agent reads it (``sql/select_gaps.sql``: newest
    ``materialised_on`` at ``horizon_days = 7``, in both the outer query and the subquery)."""
    if not _STAGE_RE.match(stage):
        raise ValueError(f"stage invalid: {stage!r}")
    db = f"adp_{stage}_fleet_rebalance"
    rows = athena_rows(ath, (
        "SELECT location_id, status, supply_vehicles, vehicles_short, vehicles_spare, "
        "data_as_of "
        f"FROM {db}.supply_demand_gaps_snapshot "
        "WHERE horizon_days = 7 AND materialised_on = ("
        f"SELECT MAX(materialised_on) FROM {db}.supply_demand_gaps_snapshot "
        "WHERE horizon_days = 7)"), wg)
    keys = ("location_id", "status", "supply_vehicles", "vehicles_short", "vehicles_spare",
            "data_as_of")
    return [dict(zip(keys, r)) for r in rows]


def _metro_by_location(ddb: Any, stage: str) -> dict[str, dict]:
    """The rental fleet's complete locations (the agent's own parser) by location id."""
    from agents.fi_rebalancing.sources import read_locations  # noqa: PLC0415
    complete, _incomplete, _metros = read_locations(
        _RENTAL_FLEET, ddb_client=ddb, table=f"cms-{stage}-storage-fleet-locations")
    return {loc["locationId"]: loc for loc in complete}


def _read_policy(ssm: Any, stage: str) -> dict:
    """The run's SSM policy, parsed strictly by the agent's own parser."""
    from agents.fi_rebalancing.policy import parse as _policy_parse  # noqa: PLC0415
    raw = ssm.get_parameter(Name=f"/vsa/{stage}/avx/fi-rebalancing/policy")["Parameter"]["Value"]
    return _policy_parse(raw)


# ---------------------------------------------------------------------------
# G1: planted demand surges vs this run's move and gap items
# ---------------------------------------------------------------------------


def check_g1_plants_vs_moves(state: _AgentState, *, ath: Any, s3: Any, ddb: Any, lam: Any,
                             lake_bucket: str, wg: str, use_run: str | None,
                             results: list) -> None:
    """G1 (6.3 Accept 1), scored against the answer key, never the agent's own output.

    Of the ``demand_surge`` plants active on the snapshot's data day whose location is
    ``deficit`` in the snapshot the run read (the plant whose note says
    "no-spare-vehicle" aside), at least 80% get a move item from this run INTO that
    location from a source in the same metro; the no-spare plant's location gets a gap
    item from this run. Every move item of the run is same-metro (W4). FAIL when no
    eligible plant is left or the key has no no-spare plant (C2d).
    """
    name = "G1 plants vs moves"
    # --- the run under test ---
    try:
        if use_run:
            rec = _run_record(ddb, state.stage, use_run, wait_s=0)
            if rec is None:
                record(results, name, "FAIL", f"--use-run {use_run!r}: no such run record")
                return
            if rec.get("status") != "completed" or not rec.get("finished_at"):
                record(results, name, "FAIL",
                       f"--use-run {use_run!r}: status={rec.get('status')!r} "
                       f"finished_at={rec.get('finished_at')!r} (need completed)")
                return
            run = {"started_at": use_run, "finished_at": rec["finished_at"], "record": rec,
                   "payload": {}}
        else:
            run = _invoke_run(state, lam, ddb)
    except _NotRun as exc:
        record(results, name, "SKIP", f"not run: {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        record(results, name, "FAIL", f"run failed: {type(exc).__name__}: {exc}"[:300])
        return
    if run["record"].get("status") != "completed":
        record(results, name, "FAIL",
               f"run {run['started_at']!r} status={run['record'].get('status')!r}")
        return
    state.run = run
    print(f"  G1: run started_at={run['started_at']} finished_at={run['finished_at']}", flush=True)

    # --- what the run read, and what it wrote ---
    gaps = _read_gaps(ath, state.stage, wg)
    state.gaps = gaps
    if not gaps:
        record(results, name, "FAIL", "gaps snapshot (horizon 7, newest) has no rows")
        return
    data_days = sorted({g["data_as_of"] for g in gaps if g.get("data_as_of")})
    if len(data_days) != 1:
        record(results, name, "FAIL", f"snapshot data_as_of not one day: {data_days}")
        return
    data_day = data_days[0][:10]
    status = {g["location_id"]: g.get("status") or "" for g in gaps}

    key = json.loads(s3.get_object(
        Bucket=lake_bucket,
        Key=f"{_ANSWER_KEY_PREFIX}/plants/fleet_bookings_answer_key.json")["Body"].read())
    plants = [p for p in key.get("plants", []) if p.get("kind") == "demand_surge"]
    no_spare = [p for p in plants if "no-spare-vehicle" in (p.get("note") or "")]
    if len(no_spare) != 1:
        record(results, name, "FAIL",
               f"answer key has {len(no_spare)} no-spare plant(s) (need exactly 1)")
        return
    no_spare_plant = no_spare[0]
    active = [p for p in plants if str(p.get("start_date", ""))[:10] <= data_day]
    eligible = [p for p in active if p is not no_spare_plant
                and status.get(p.get("location_id")) == "deficit"]

    locs = _metro_by_location(ddb, state.stage)
    findings = _all_rebalancing_findings(ddb, state.stage)
    items, bad = _items_of_run(findings, run, _now())
    state.run_items = items
    moves = [f for f in items if f.get("finding_kind") == "fleet.rebalance.move"]
    gaps_items = [f for f in items if f.get("finding_kind") == "fleet.rebalance.gap"]

    problems: list[str] = []
    if bad:
        problems.append(f"{len(bad)} rebalancing row(s) with unparseable times: {bad[:3]}")
    # W4: every move of this run is same-metro.
    for m in moves:
        loc = m.get("location") or {}
        fm = (locs.get(loc.get("from_location_id")) or {}).get("metro")
        tm = (locs.get(loc.get("to_location_id")) or {}).get("metro")
        if not fm or fm != tm:
            problems.append(f"move {m.get('finding_id')}: {loc.get('from_location_id')} "
                            f"({fm}) -> {loc.get('to_location_id')} ({tm}) is not same-metro")
    hits, verdicts = 0, []
    for p in eligible:
        lid = p["location_id"]
        metro = (locs.get(lid) or {}).get("metro")
        ok = bool(metro) and any(
            (m.get("location") or {}).get("to_location_id") == lid
            and (locs.get((m.get("location") or {}).get("from_location_id")) or {}).get("metro")
            == metro for m in moves)
        hits += ok
        verdicts.append(f"{lid}={'move' if ok else 'MISSING'}")
        print(f"  G1 plant {lid} start={p.get('start_date')} metro={metro}: "
              f"{'move found' if ok else 'NO MOVE'}", flush=True)
    nsp = no_spare_plant["location_id"]
    nsp_gap = any((g.get("location") or {}).get("location_id") == nsp for g in gaps_items)
    print(f"  G1 no-spare plant {nsp} status={status.get(nsp)!r}: "
          f"{'gap item found' if nsp_gap else 'NO GAP ITEM'}", flush=True)
    if not eligible:
        problems.append(f"no eligible deficit plant on data day {data_day} "
                        f"({len(active)} active demand_surge plants)")
    elif hits * 100 < 80 * len(eligible):
        problems.append(f"{hits}/{len(eligible)} eligible plants got a same-metro move (<80%)")
    if str(no_spare_plant.get("start_date", ""))[:10] > data_day:
        problems.append(f"no-spare plant {nsp} not active on {data_day}")
    elif not nsp_gap:
        problems.append(f"no-spare plant {nsp} (status {status.get(nsp)!r}) has no gap item")
    detail = (f"run={run['started_at']}; data_day={data_day}; items={len(items)} "
              f"(moves {len(moves)}, gaps {len(gaps_items)}); eligible={len(eligible)}; "
              f"hit={hits}; no-spare {nsp} gap={nsp_gap}; {verdicts}")
    record(results, name, "FAIL" if problems else "PASS",
           ("; ".join(problems) + f" [{detail}]") if problems else detail)


# ---------------------------------------------------------------------------
# G2: money on this run's move items, recomputed from evidence and the platform
# ---------------------------------------------------------------------------


def _evidence(item: dict, kind: str) -> list[dict]:
    return [e for e in (item.get("evidence") or []) if e.get("kind") == kind]


def _forecast_rows(ath: Any, stage: str, run_date: str, wg: str) -> dict[tuple[str, str], float]:
    """``{(location_id, forecast_date): forecast_hours}`` for one forecast run."""
    if not _STAGE_RE.match(stage) or not _DATE_RE.match(run_date):
        raise ValueError(f"bad stage/run_date: {stage!r} {run_date!r}")
    rows = athena_rows(ath, (
        "SELECT location_id, CAST(forecast_date AS VARCHAR), forecast_hours "
        f"FROM adp_{stage}_fleet_rebalance.fleet_bookings_forecast "
        f"WHERE run_date = DATE '{run_date}'"), wg)
    return {(r[0], (r[1] or "")[:10]): float(r[2]) for r in rows if r[2] is not None}


#: A move item's ``rebalance.supply`` evidence names the gaps snapshot it read (evidence.py).
_GAPS_REF_RE = re.compile(r"^adp\.supply_demand_gaps_snapshot data_as_of=(\d{4}-\d{2}-\d{2})$")


def _snapshot_supply(ath: Any, stage: str, data_as_of: str, wg: str) -> dict[str, float]:
    """``{location_id: supply_vehicles}`` from the gaps snapshot of one ``data_as_of`` (its
    newest materialisation at ``horizon_days = 7``): the snapshot a move item cites. A move the
    run kept rather than rewrote (same inputs_hash) keeps the evidence of the day it was first
    written, so it is checked against that day's snapshot, not today's (review g6-lv c2 W-A)."""
    if not _STAGE_RE.match(stage) or not _DATE_RE.match(data_as_of):
        raise ValueError(f"bad stage/data_as_of: {stage!r} {data_as_of!r}")
    db = f"adp_{stage}_fleet_rebalance"
    rows = athena_rows(ath, (
        "SELECT location_id, supply_vehicles "
        f"FROM {db}.supply_demand_gaps_snapshot "
        f"WHERE horizon_days = 7 AND data_as_of = DATE '{data_as_of}' AND materialised_on = ("
        f"SELECT MAX(materialised_on) FROM {db}.supply_demand_gaps_snapshot "
        f"WHERE horizon_days = 7 AND data_as_of = DATE '{data_as_of}')"), wg)
    return {r[0]: float(r[1]) for r in rows if r[1] is not None}


def _cited_sources(m: dict) -> tuple[str, str]:
    """The forecast ``run_date`` and gaps-snapshot ``data_as_of`` move item *m* cites. Exactly
    one of each, or ValueError."""
    run_dates = {str(e["value"].get("run_date"))[:10] for e in _evidence(m, "rebalance.forecast")}
    if len(run_dates) != 1 or not _DATE_RE.match(next(iter(run_dates))):
        raise ValueError(f"forecast evidence cites run_dates {sorted(run_dates)} (want one)")
    sup = _evidence(m, "rebalance.supply")
    refs = [_GAPS_REF_RE.match(str(e.get("source_ref") or "")) for e in sup]
    if len(sup) != 1 or not refs[0]:
        raise ValueError(f"rebalance.supply evidence cites {[e.get('source_ref') for e in sup]} "
                         "(want one gaps snapshot data_as_of)")
    return next(iter(run_dates)), refs[0].group(1)


def check_g2_money_fields(state: _AgentState, *, ath: Any, ssm: Any, ddb: Any, wg: str,
                          results: list) -> None:
    """G2 (6.3 Accept 2): every move item of G1's run carries money equal to D9's formulas.

    Recomputed independently, in integer cents (C3): destination per-day inputs from the
    item's ``rebalance.forecast`` evidence, each checked against ADP's forecast row and the
    snapshot's supply; source per-day inputs from ADP (forecast rows of the same run, the
    snapshot's source supply) and the source's CMS location. "The run" and "the snapshot" are
    the ones the item cites (``_cited_sources``), so a move kept from an earlier day is checked
    against that day's data (W-A); rates by CMS location type
    (``formulas.DEFAULTS['RATES']``); ``max_utilization`` from the run's SSM policy; the
    transfer cost from the two locations' coordinates. Any missing field is a FAIL.
    """
    from agents.fi_rebalancing import formulas as F  # noqa: PLC0415
    name = "G2 money fields"
    if state.run is None:
        record(results, name, "SKIP", "not run: G1 did not establish a run")
        return
    moves = [f for f in state.run_items if f.get("finding_kind") == "fleet.rebalance.move"]
    if not moves:
        record(results, name, "FAIL", f"run {state.run['started_at']} wrote no move items")
        return
    policy = _read_policy(ssm, state.stage)
    state.policy = policy
    max_util = float(policy["max_utilization"])
    locs = _metro_by_location(ddb, state.stage)
    fc_by_run: dict[str, dict] = {}
    supply_by_day: dict[str, dict] = {}
    cited: set[tuple[str, str]] = set()

    problems: list[str] = []
    for m in moves:
        fid = m.get("finding_id", "?")
        try:
            run_date, gaps_day = _cited_sources(m)
            cited.add((run_date, gaps_day))
            if run_date not in fc_by_run:
                fc_by_run[run_date] = _forecast_rows(ath, state.stage, run_date, wg)
            if gaps_day not in supply_by_day:
                supply_by_day[gaps_day] = _snapshot_supply(ath, state.stage, gaps_day, wg)
            fc, supply = fc_by_run[run_date], supply_by_day[gaps_day]
            loc = m.get("location") or {}
            src_id, dst_id = loc["from_location_id"], loc["to_location_id"]
            src, dst = locs[src_id], locs[dst_id]
            (tr,) = _evidence(m, "rebalance.transfer")
            (rv,) = _evidence(m, "rebalance.revenue")
            tv, rvv = tr["value"], rv["value"]
            vins = [e["value"]["vin"] for e in _evidence(m, "rebalance.vehicle")]
            count = int(tv["vehicle_count"])
            if count != len(vins) or count <= 0:
                raise ValueError(f"vehicle_count {count} != {len(vins)} vehicle entries")
            days = sorted(_evidence(m, "rebalance.forecast"), key=lambda e: e["value"]["date"])
            if not days:
                raise ValueError("no rebalance.forecast evidence")
            dist = F.great_circle_miles(dst["latitude"], dst["longitude"],
                                        src["latitude"], src["longitude"]) * 1.35
            if _cents(tv["distance_miles"]) != _cents(round(dist, 2)):
                raise ValueError(f"distance_miles {tv['distance_miles']} != {round(dist, 2)}")
            tc_pv = round(F.compute_transfer_cost(distance_miles=dist), 2)
            if _cents(tv["transfer_cost_per_vehicle"]) != _cents(tc_pv):
                raise ValueError(f"transfer_cost_per_vehicle {tv['transfer_cost_per_vehicle']} != {tc_pv}")
            rate = F.DEFAULTS["RATES"][dst["locationType"]]
            src_rate = F.DEFAULTS["RATES"][src["locationType"]]
            gain = loss = 0.0
            for e in days:
                v = e["value"]
                d = str(v["date"])[:10]
                f_h, s_v, op = float(v["forecast_hours"]), float(v["supply_vehicles"]), float(v["op_hours"])
                if _cents(f_h) != _cents(fc[(dst_id, d)]):
                    raise ValueError(f"{d}: forecast_hours {f_h} != ADP {fc[(dst_id, d)]}")
                if _cents(s_v) != _cents(supply[dst_id]) or _cents(op) != _cents(dst["op_hours"]):
                    raise ValueError(f"{d}: destination supply/op_hours {s_v}/{op} != "
                                     f"{supply[dst_id]}/{dst['op_hours']}")
                unserved = max(0.0, f_h - s_v * op * max_util)
                gain += F.compute_revenue_gain_per_day(
                    forecast_unserved_hours=unserved, moved_vehicles=count, vehicle_op_hours=op,
                    max_utilization=max_util, usd_per_on_rent_hour=rate)
                loss += F.compute_source_loss_per_day(
                    source_forecast_hours=float(fc[(src_id, d)]),
                    source_supply_vehicles=float(supply[src_id]), moved_vehicles=count,
                    vehicle_op_hours=float(src["op_hours"]), max_utilization=max_util,
                    usd_per_on_rent_hour=src_rate)
            impact = F.compute_revenue_impact(revenue_gain_per_day=gain,
                                              source_loss_per_day=loss, horizon_days=1)
            transfer = tc_pv * count
            net = F.compute_net_benefit(revenue_impact=impact, transfer_cost=transfer)
            want = {"transfer_cost_usd": transfer, "revenue_gain_usd": gain,
                    "source_loss_usd": loss, "revenue_impact_usd": impact,
                    "net_benefit_usd": net}
            got = {"transfer_cost_usd": tv["transfer_cost_usd"], **{
                k: rvv[k] for k in ("revenue_gain_usd", "source_loss_usd",
                                    "revenue_impact_usd", "net_benefit_usd")}}
            for k, w in want.items():
                if _cents(got[k]) != _cents(round(w, 2)):
                    problems.append(f"{fid}: {k} {got[k]} != recomputed {round(w, 2)}")
            est = (m.get("recommended_action") or {})["estimated_value_usd"]
            if _cents(est) != _cents(round(net, 2)):
                problems.append(f"{fid}: estimated_value_usd {est} != net {round(net, 2)}")
        except (KeyError, ValueError, TypeError) as exc:
            problems.append(f"{fid}: {type(exc).__name__}: {exc}"[:200])
    detail = (f"run={state.run['started_at']}; moves={len(moves)}; cited (forecast run_date, "
              f"gaps data_as_of)={sorted(cited)}; max_utilization={max_util}")
    record(results, name, "FAIL" if problems else "PASS",
           (f"{len(problems)} problem(s): {problems[:4]} [{detail}]") if problems else detail)


# ---------------------------------------------------------------------------
# G3: a re-run on unchanged inputs writes nothing and calls no model
# ---------------------------------------------------------------------------


def check_g3_idempotency(state: _AgentState, *, lam: Any, ddb: Any, results: list) -> None:
    """G3 (6.3 Accept 3). The re-run's record: ``status`` completed, ``artifacts_written``
    0 and ``model_calls`` 0 (C4: fields absent is a FAIL); ``duplicates_skipped`` equals
    the number of live items G1's run left; the payload's ``tokens_used`` is 0 (the run
    record has no ``tokens_used``). No new finding id, and no live item's content hash
    changed (the writer keeps ``finding_id`` on an in-place update, so ids alone can't
    show a rewrite)."""
    name = "G3 idempotency"
    if state.run is None:
        record(results, name, "SKIP", "not run: G1 did not establish a run")
        return
    expected = len(state.run_items)
    if expected == 0:
        record(results, name, "FAIL", "G1's run left no live items to re-check")
        return
    before = {f.get("finding_id"): f.get("inputs_hash")
              for f in _all_rebalancing_findings(ddb, state.stage)}
    try:
        rerun = _invoke_run(state, lam, ddb)
    except _NotRun as exc:
        record(results, name, "SKIP", f"not run: {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        record(results, name, "FAIL", f"re-run failed: {type(exc).__name__}: {exc}"[:300])
        return
    rec, pay = rerun["record"], rerun["payload"]
    after = {f.get("finding_id"): f.get("inputs_hash")
             for f in _all_rebalancing_findings(ddb, state.stage)}
    problems: list[str] = []
    if rec.get("status") != "completed":
        problems.append(f"status={rec.get('status')!r}")
    for field_, want in (("artifacts_written", 0), ("model_calls", 0),
                         ("duplicates_skipped", expected)):
        if field_ not in rec:
            problems.append(f"run record has no {field_}")
        elif int(rec[field_]) != want:
            problems.append(f"{field_}={rec[field_]} (want {want})")
    if "tokens_used" not in pay:
        problems.append("payload has no tokens_used")
    elif int(pay["tokens_used"]) != 0:
        problems.append(f"tokens_used={pay['tokens_used']} (want 0)")
    new_ids = sorted(set(after) - set(before))
    changed = sorted(i for i in before if i in after and after[i] != before[i])
    if new_ids:
        problems.append(f"{len(new_ids)} new finding id(s): {new_ids[:3]}")
    if changed:
        problems.append(f"{len(changed)} row(s) rewritten (inputs_hash changed): {changed[:3]}")
    detail = (f"re-run={rerun['started_at']}; artifacts_written={rec.get('artifacts_written')}; "
              f"model_calls={rec.get('model_calls')}; duplicates_skipped="
              f"{rec.get('duplicates_skipped')} (G1 left {expected}); tokens_used="
              f"{pay.get('tokens_used')}")
    record(results, name, "FAIL" if problems else "PASS",
           ("; ".join(problems) + f" [{detail}]") if problems else detail)



# ---------------------------------------------------------------------------
# G4: isolation (three separately recorded checks)
# ---------------------------------------------------------------------------


def _http_get(url: str, token: str, params: dict | None = None) -> tuple[int, Any]:
    """GET *url* with ``Authorization: Bearer <token>``; (status, parsed JSON body or {}).

    An HTTP error status returns its code with the body parsed when it is JSON; a network
    error returns ``(-1, {"_error": ...})``.
    """
    import urllib.error  # noqa: PLC0415
    import urllib.parse  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})

    def _body(raw: bytes) -> Any:
        text = raw.decode("utf-8", errors="replace")
        try:
            return json.loads(text) if text else {}
        except ValueError:
            return {"_raw": text[:200]}

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (https endpoint)
            return resp.status, _body(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _body(exc.read() or b"")
    except Exception as exc:  # noqa: BLE001
        return -1, {"_error": str(exc)[:120]}


def _fleet_route_pages(base: str, fleet_id: str, token: str,
                       max_pages: int = 50) -> tuple[int, list[dict]]:
    """Every page of ``GET /findings/fleet/{fleet_id}`` (``?next_token=``): (first status, items).

    Stops at the first non-200 page and returns its status."""
    items: list[dict] = []
    params: dict | None = None
    for _ in range(max_pages):
        code, body = _http_get(f"{base}/findings/fleet/{fleet_id}", token, params)
        if code != 200:
            return code, items
        items.extend(body.get("findings") or [])
        tok = body.get("next_token")
        if not tok:
            return 200, items
        params = {"next_token": tok}
    raise RuntimeError(f"/findings/fleet/{fleet_id}: more than {max_pages} pages")


def _find_cms_pool(idp: Any, stage: str) -> str:
    """The CMS user pool (``cms-{stage}-ui-users``), whose tokens AVX's API accepts."""
    kw: dict = {"MaxResults": 60}
    while True:
        resp = idp.list_user_pools(**kw)
        for p in resp.get("UserPools", []):
            if p.get("Name") == f"cms-{stage}-ui-users":
                return p["Id"]
        tok = resp.get("NextToken")
        if not tok:
            raise RuntimeError(f"Cognito pool cms-{stage}-ui-users not found")
        kw["NextToken"] = tok


def _password_client(idp: Any, pool_id: str) -> str:
    """The first app client on *pool_id* that allows USER_PASSWORD_AUTH."""
    kw: dict = {"UserPoolId": pool_id, "MaxResults": 60}
    while True:
        resp = idp.list_user_pool_clients(**kw)
        for c in resp.get("UserPoolClients", []):
            desc = idp.describe_user_pool_client(UserPoolId=pool_id, ClientId=c["ClientId"])
            if "ALLOW_USER_PASSWORD_AUTH" in (desc["UserPoolClient"].get("ExplicitAuthFlows") or []):
                return c["ClientId"]
        tok = resp.get("NextToken")
        if not tok:
            raise RuntimeError(f"no app client on {pool_id} allows USER_PASSWORD_AUTH")
        kw["NextToken"] = tok


@contextlib.contextmanager
def _temp_operator(idp: Any, pool_id: str, client_id: str, fleet_id: str,
                   cleanup: list) -> Iterator[str]:
    """A temporary ``fleet-operator`` of *fleet_id* in the CMS pool; yields its id token.

    Deleted in ``finally`` with signals held, then confirmed gone with ``admin_get_user``;
    a failed deletion is appended to *cleanup* with the exact delete command (S4)."""
    import secrets  # noqa: PLC0415
    username = f"{_G4_USER_PREFIX}{int(time.time())}-{secrets.token_hex(3)}@example.invalid"
    password = f"Lv{secrets.token_urlsafe(18)}!9Qz"
    created = False
    try:
        with _signals_held():
            idp.admin_create_user(
                UserPoolId=pool_id, Username=username, MessageAction="SUPPRESS",
                UserAttributes=[{"Name": "email", "Value": username},
                                {"Name": "email_verified", "Value": "true"},
                                {"Name": "custom:fleetIds", "Value": fleet_id}])
            created = True
        idp.admin_set_user_password(UserPoolId=pool_id, Username=username,
                                    Password=password, Permanent=True)
        idp.admin_add_user_to_group(UserPoolId=pool_id, Username=username,
                                    GroupName="fleet-operator")
        auth = idp.initiate_auth(ClientId=client_id, AuthFlow="USER_PASSWORD_AUTH",
                                 AuthParameters={"USERNAME": username, "PASSWORD": password})
        yield auth["AuthenticationResult"]["IdToken"]
    finally:
        with _signals_held():
            if created:
                gone = False
                try:
                    idp.admin_delete_user(UserPoolId=pool_id, Username=username)
                    try:
                        idp.admin_get_user(UserPoolId=pool_id, Username=username)
                    except idp.exceptions.UserNotFoundException:
                        gone = True
                except Exception as exc:  # noqa: BLE001
                    print(f"  G4b cleanup error: {type(exc).__name__}: {exc}", file=sys.stderr)
                if not gone:
                    cmd = (f"aws cognito-idp admin-delete-user --region {CVX_REGION} "
                           f"--user-pool-id {pool_id} --username '{username}'")
                    cleanup.append(cmd)
                    print(f"  G4b cleanup FAILED; run: {cmd}", file=sys.stderr)


def check_g4_isolation(state: _AgentState, *, ddb: Any, logs: Any, idp: Any,
                       avx_api_endpoint: str, results: list, cleanup: list) -> None:
    """G4 (6.3 Accept 4), recorded as three rows.

    G4a: at least one rebalancing Finding exists and none carries ``owner_id``.
    G4b: a temporary ``fleet-operator`` of ``demo-lastmile-delivery`` gets 403/404 for the
         rental fleet's route, and its own fleet's route answers 200 on every page with no
         rental or rebalancing item (W3).
    G4c: the broker logged ``broker: skipping — fi_item finding_id=<id> ...
         agent_id=agent-fi-rebalancing`` for an item of G1's run since that run started.
    """
    # --- G4a (read-only; runs whatever the flags) ---
    try:
        rows = [f for f in _all_rebalancing_findings(ddb, state.stage)
                if f.get("finding_kind") in _REBALANCE_KINDS]
        owned = [f.get("finding_id", "?") for f in rows if f.get("owner_id") not in (None, "")]
        if not rows:
            record(results, "G4a no owner_id", "FAIL", "no rebalancing Finding exists")
        elif owned:
            record(results, "G4a no owner_id", "FAIL",
                   f"{len(owned)} of {len(rows)} carry owner_id: {owned[:3]}")
        else:
            record(results, "G4a no owner_id", "PASS", f"{len(rows)} Findings, none with owner_id")
    except Exception as exc:  # noqa: BLE001
        record(results, "G4a no owner_id", "FAIL", f"{type(exc).__name__}: {exc}"[:200])

    # --- G4b (temp Cognito user: needs --allow-writes) ---
    if not state.allow_writes:
        record(results, "G4b fleet route isolation", "SKIP", "not run: --allow-writes not given")
    else:
        try:
            pool_id = _find_cms_pool(idp, state.stage)
            client_id = _password_client(idp, pool_id)
            base = avx_api_endpoint.rstrip("/")
            with _temp_operator(idp, pool_id, client_id, _DELIVERY_FLEET, cleanup) as token:
                deny, _ = _http_get(f"{base}/findings/fleet/{_RENTAL_FLEET}", token)
                own, own_items = _fleet_route_pages(base, _DELIVERY_FLEET, token)
            problems = []
            if deny not in (403, 404):
                problems.append(f"rental fleet route answered {deny} (want 403/404)")
            if own != 200:
                problems.append(f"own fleet route answered {own} (positive control; want 200)")
            leaked = [i.get("finding_id") for i in own_items
                      if i.get("agent_id") == _REBALANCE_AGENT_ID
                      or (i.get("scope") or {}).get("fleet_id") == _RENTAL_FLEET]
            if leaked:
                problems.append(f"own fleet route returned rental/rebalancing items: {leaked[:3]}")
            detail = f"rental route {deny}; own route {own} with {len(own_items)} item(s)"
            record(results, "G4b fleet route isolation", "FAIL" if problems else "PASS",
                   ("; ".join(problems) + f" [{detail}]") if problems else detail)
        except Exception as exc:  # noqa: BLE001
            record(results, "G4b fleet route isolation", "FAIL",
                   f"{type(exc).__name__}: {exc}"[:200])

    # --- G4c (read-only) ---
    if state.run is None or not state.run_items:
        record(results, "G4c broker skip", "SKIP", "not run: G1 left no items of its run")
        return
    try:
        ids = {f.get("finding_id") for f in state.run_items}
        start = _parse_ts(state.run["started_at"])
        kw: dict = {"logGroupName": f"/aws/lambda/vsa-{state.stage}-avx-broker-{CVX_REGION}",
                    "startTime": int(start.timestamp() * 1000) - 60_000,
                    "filterPattern": '"broker: skipping" "fi_item"'}
        hits: list[str] = []
        while True:
            page = logs.filter_log_events(**kw)
            for ev in page.get("events", []):
                msg = ev.get("message", "")
                m = re.search(r"fi_item finding_id=(\S+) kind=\S+ agent_id=(\S+)", msg)
                if m and m.group(2) == _REBALANCE_AGENT_ID and m.group(1) in ids:
                    hits.append(m.group(1))
            tok = page.get("nextToken")
            if not tok:
                break
            kw["nextToken"] = tok
        if hits:
            record(results, "G4c broker skip", "PASS",
                   f"{len(hits)} skip line(s) for items of run {state.run['started_at']}")
        else:
            record(results, "G4c broker skip", "FAIL",
                   f"no 'broker: skipping — fi_item' line for any of {len(ids)} items of "
                   f"run {state.run['started_at']}")
    except Exception as exc:  # noqa: BLE001
        record(results, "G4c broker skip", "FAIL", f"{type(exc).__name__}: {exc}"[:200])


# ---------------------------------------------------------------------------
# G5: a forced failure reaches the run-failure alarm's subscriber
# ---------------------------------------------------------------------------


def _restore_parameter(ssm: Any, name: str, value: str, backup_path: Path) -> tuple[bool, str]:
    """Put *value* back (retrying throttling/TooManyUpdates) and read it back byte for byte.

    Runs with signals held. Returns (restored, detail); on failure the detail names the
    backup file and the exact command that restores it (C6).
    """
    from botocore.exceptions import ClientError  # noqa: PLC0415
    last = ""
    with _signals_held():
        for attempt in range(1, _RESTORE_ATTEMPTS + 1):
            try:
                ssm.put_parameter(Name=name, Value=value, Type="String", Overwrite=True)
                back = ssm.get_parameter(Name=name)["Parameter"]["Value"]
                if back == value:
                    return True, f"restored and read back on attempt {attempt}"
                last = f"read-back differs ({len(back)} vs {len(value)} chars)"
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                last = f"{code}: {exc}"
                if code not in _RETRYABLE_SSM:
                    break
            except Exception as exc:  # noqa: BLE001
                last = f"{type(exc).__name__}: {exc}"
                break
            time.sleep(min(2 ** attempt, 10))
    value_file = backup_path.with_suffix(".value")
    return False, (f"RESTORE FAILED ({last}); the original value is in {value_file} "
                   f"(0600). Run: aws ssm put-parameter --region {CVX_REGION} --name {name} "
                   f"--type String --overwrite --value file://{value_file}")


def _write_backup(name: str, value: str) -> Path:
    """0600 backup of the policy parameter: a JSON record and the raw value (for file://)."""
    import tempfile  # noqa: PLC0415
    d = Path(tempfile.mkdtemp(prefix="fi-rebal-g5-"))
    path = d / "g5-ssm-backup.json"
    for p, text in ((path, json.dumps({"param_name": name, "original_value": value})),
                    (path.with_suffix(".value"), value)):
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
    print(f"  G5: policy backup written to {path} (0600)", flush=True)
    return path


def check_g5_forced_failure(state: _AgentState, *, lam: Any, ssm: Any, cw: Any, sns: Any,
                            ddb: Any, results: list) -> None:
    """G5 (6.3 Accept 5), Stage B's method (``live-verify-fi-cost.py`` item 6).

    1. The run-failure alarm, by name (``vsa-{stage}-tier2-run-failure-{region}``): metric
       ``Errors`` on this function, an SNS action to the operator topic, a confirmed
       subscriber on that topic (paged), and not already in ALARM (else SKIP: this run
       could not be shown to trigger it).
    2. Back up the policy (0600), put ``{"bad": true}``, invoke once: FunctionError with
       ``errorType`` ``PolicyUnavailable`` (S1) and a new ``aborted`` run record.
    3. Restore in ``finally`` (signals held, retries, read-back); a failed restore stops
       every later invoking item.
    4. The alarm reaches ALARM with ``StateUpdatedTimestamp`` at or after the invoke, and an
       Action history item at or after the invoke has ``HistoryData.actionState`` Succeeded.
    """
    name = "G5 forced failure"
    reason = _may_invoke(state)
    if reason:
        record(results, name, "SKIP", f"not run: {reason}")
        return
    alarm_name = f"vsa-{state.stage}-tier2-run-failure-{CVX_REGION}"
    topic = f"vsa-{state.stage}-tier2-operator-alerts"
    param = f"/vsa/{state.stage}/avx/fi-rebalancing/policy"
    alarms = cw.describe_alarms(AlarmNames=[alarm_name]).get("MetricAlarms", [])
    if len(alarms) != 1:
        record(results, name, "FAIL", f"alarm {alarm_name!r} not found")
        return
    alarm = alarms[0]
    dims = {d.get("Name"): d.get("Value") for d in alarm.get("Dimensions") or []}
    if alarm.get("MetricName") != "Errors" or dims.get("FunctionName") != state.fn_name:
        record(results, name, "FAIL", f"alarm {alarm_name!r} watches {alarm.get('MetricName')} "
                                      f"{dims} (want Errors on {state.fn_name})")
        return
    topic_arns = [a for a in alarm.get("AlarmActions") or [] if a.endswith(f":{topic}")]
    if not topic_arns:
        record(results, name, "FAIL", f"alarm actions {alarm.get('AlarmActions')} lack {topic}")
        return
    subs: list[dict] = []
    kw: dict = {"TopicArn": topic_arns[0]}
    while True:
        page = sns.list_subscriptions_by_topic(**kw)
        subs.extend(page.get("Subscriptions", []))
        if not page.get("NextToken"):
            break
        kw["NextToken"] = page["NextToken"]
    confirmed = [s for s in subs if (s.get("SubscriptionArn") or "").startswith("arn:")]
    if not confirmed:
        record(results, name, "FAIL", f"{topic} has no confirmed subscription")
        return
    if alarm.get("StateValue") == "ALARM":
        record(results, name, "SKIP", f"{alarm_name} is already in ALARM; this run could not "
                                      "be shown to trigger it")
        return

    from agents.fi_rebalancing.policy import PolicyUnavailable  # noqa: PLC0415
    from agents.fi_rebalancing.policy import parse as _policy_parse  # noqa: PLC0415
    original = ssm.get_parameter(Name=param)["Parameter"]["Value"]
    try:
        _policy_parse(original)
    except PolicyUnavailable as exc:
        record(results, name, "SKIP", f"current policy is already malformed; not overriding: {exc}")
        return
    backup = _write_backup(param, original)

    problems: list[str] = []
    invoke_at = _now()
    restored, restore_detail = False, ""
    try:
        ssm.put_parameter(Name=param, Value=_MALFORMED_POLICY, Type="String", Overwrite=True)
        try:
            _invoke_rebalance(lam, state.fn_name)
            problems.append("the invoke succeeded with a malformed policy (want FunctionError)")
        except InvokeFailed as exc:
            et = exc.payload.get("errorType")
            if et != "PolicyUnavailable":
                problems.append(f"FunctionError errorType={et!r} (want 'PolicyUnavailable')")
    finally:
        restored, restore_detail = _restore_parameter(ssm, param, original, backup)
        if not restored:
            state.invoke_failed = True
    if not restored:
        record(results, name, "FAIL", restore_detail)
        return
    runs = [r for r in _runs_since(ddb, state.stage, invoke_at)
            if (_parse_ts(r.get("started_at")) or invoke_at) >= invoke_at - datetime.timedelta(seconds=5)]
    if [r.get("status") for r in runs] != ["aborted"]:
        problems.append(f"run records since the invoke: {[(r.get('started_at'), r.get('status')) for r in runs]} "
                        "(want exactly one aborted)")

    reached = None
    deadline = time.monotonic() + _G5_ALARM_WAIT_S
    while time.monotonic() < deadline:
        cur = cw.describe_alarms(AlarmNames=[alarm_name]).get("MetricAlarms", [{}])[0]
        upd = cur.get("StateUpdatedTimestamp")
        if cur.get("StateValue") == "ALARM" and upd is not None:
            upd = upd if upd.tzinfo else upd.replace(tzinfo=datetime.timezone.utc)
            if upd >= invoke_at:
                reached = upd
                break
        time.sleep(15)
    action_state = None
    if reached is None:
        problems.append(f"{alarm_name} did not reach ALARM within {_G5_ALARM_WAIT_S}s of the invoke")
    else:
        deadline = time.monotonic() + _G5_ACTION_WAIT_S
        while action_state is None and time.monotonic() < deadline:
            hist = cw.describe_alarm_history(AlarmName=alarm_name, HistoryItemType="Action",
                                             MaxRecords=10, ScanBy="TimestampDescending")
            for h in hist.get("AlarmHistoryItems", []):
                ts = h.get("Timestamp")
                ts = ts if ts is None or ts.tzinfo else ts.replace(tzinfo=datetime.timezone.utc)
                if ts is not None and ts >= invoke_at:
                    action_state = str(json.loads(h.get("HistoryData") or "{}").get("actionState"))
                    break
            if action_state is None:
                time.sleep(5)
        if action_state != "Succeeded":
            problems.append(f"alarm action after the invoke: actionState={action_state!r} "
                            "(want 'Succeeded')")
    detail = (f"invoke at {_iso(invoke_at)}; policy {restore_detail}; alarm ALARM at "
              f"{reached}; action {action_state}; {len(confirmed)} confirmed subscriber(s)")
    record(results, name, "FAIL" if problems else "PASS",
           ("; ".join(problems) + f" [{detail}]") if problems else detail)


# ---------------------------------------------------------------------------
# Part agent orchestration
# ---------------------------------------------------------------------------


def _part_agent(args: argparse.Namespace) -> int:
    """Run --part agent checks G1-G5 in order; return exit code 0/1/2."""
    if _in_write_window():
        print("REFUSED: inside the 04:50-06:10 UTC window", file=sys.stderr)
        return 2
    if not args.avx_api_endpoint:
        print("REFUSED: --part agent needs --avx-api-endpoint (G4b)", file=sys.stderr)
        return 2
    fn_name = _TIER2_LAMBDA_FMT.format(stage=args.stage, region=CVX_REGION)
    _init_session(allow_writes=bool(args.allow_writes), stage=args.stage,
                  workgroup=args.workgroup, fn_name=fn_name)
    account_id: str = _client("sts", CVX_REGION).get_caller_identity()["Account"]
    fn_arn = f"arn:aws:lambda:{CVX_REGION}:{account_id}:function:{fn_name}"
    sched = _client("scheduler", CVX_REGION)
    ok, msg = _check_scheduler_timing(sched, fn_arn)
    if not ok:
        print(f"REFUSED: scheduler guard: {msg}", file=sys.stderr)
        return 2
    state = _AgentState(stage=args.stage, fn_name=fn_name, fn_arn=fn_arn, sched=sched,
                        allow_writes=bool(args.allow_writes))
    lake_bucket = args.lake_bucket or _LAKE_BUCKET_FMT.format(
        stage=args.stage, account=account_id, region=ADP_REGION)
    lam = _client("lambda", CVX_REGION, config=_lambda_config())
    ath = _client("athena", ADP_REGION)
    s3 = _client("s3", ADP_REGION)
    ddb = _client("dynamodb", CVX_REGION)
    ssm = _client("ssm", CVX_REGION)
    cw = _client("cloudwatch", CVX_REGION)
    sns = _client("sns", CVX_REGION)
    logs = _client("logs", CVX_REGION)
    idp = _client("cognito-idp", CVX_REGION)

    results: list[tuple[str, str, str]] = []
    cleanup: list[str] = []
    items = (
        ("G1", lambda: check_g1_plants_vs_moves(
            state, ath=ath, s3=s3, ddb=ddb, lam=lam, lake_bucket=lake_bucket,
            wg=args.workgroup, use_run=args.use_run, results=results)),
        ("G2", lambda: check_g2_money_fields(state, ath=ath, ssm=ssm, ddb=ddb,
                                             wg=args.workgroup, results=results)),
        ("G3", lambda: check_g3_idempotency(state, lam=lam, ddb=ddb, results=results)),
        ("G4", lambda: check_g4_isolation(state, ddb=ddb, logs=logs, idp=idp,
                                          avx_api_endpoint=args.avx_api_endpoint,
                                          results=results, cleanup=cleanup)),
        ("G5", lambda: check_g5_forced_failure(state, lam=lam, ssm=ssm, cw=cw, sns=sns,
                                               ddb=ddb, results=results)),
    )
    for label, fn in items:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            record(results, label, "FAIL", f"unexpected {type(exc).__name__}: {exc}"[:300])
    for cmd in cleanup:
        record(results, "cleanup", "FAIL", f"temp user not removed; run: {cmd}")
    fails = [r for r in results if r[1] == "FAIL"]
    skipped = [r for r in results if r[1] == "SKIP"]
    passed = [r for r in results if r[1] == "PASS"]
    print(f"\nSUMMARY: {len(passed)} PASS, {len(fails)} FAIL, {len(skipped)} SKIP", flush=True)
    return 1 if fails else (2 if skipped else 0)


def _lambda_config() -> Any:
    """botocore Config for Lambda invoke: read_timeout > 15-min timeout, 1 attempt."""
    from botocore.config import Config  # noqa: PLC0415
    return Config(read_timeout=_LAMBDA_READ_TIMEOUT_S, retries={"total_max_attempts": 1})


def _part_adp(args: argparse.Namespace) -> int:
    """Run all --part adp checks; return exit code 0/1/2."""
    _init_session(
        allow_writes=False,
        stage=args.stage,
        workgroup=args.workgroup,
        fn_name="",
    )
    sts = _client("sts", ADP_REGION)
    account_id: str = sts.get_caller_identity()["Account"]

    lake_bucket: str = args.lake_bucket or _LAKE_BUCKET_FMT.format(
        stage=args.stage, account=account_id, region=ADP_REGION,
    )
    fr_db = f"adp_{args.stage}_fleet_rebalance"
    wg: str = args.workgroup

    ath = _client("athena", ADP_REGION)
    s3 = _client("s3", ADP_REGION)
    iam = _client("iam", "us-east-1")      # IAM is global; us-east-1 is conventional
    lam_cvx = _client("lambda", CVX_REGION)
    lam_cms = _client("lambda", CVX_REGION)
    sns = _client("sns", ADP_REGION)
    cfn = _client("cloudformation", ADP_REGION)
    ssm = _client("ssm", CVX_REGION)

    results: list[tuple[str, str, str]] = []
    expect_after: str | None = getattr(args, "expect_after", None)

    for fn, fn_args in (
        (check_a1_tables,    (ath, fr_db, wg, results)),
        (check_a2_dates,     (ath, fr_db, wg, expect_after, results)),
        (check_a3_answer_key,(s3, lake_bucket, results)),
        (check_a4_iam,       (iam, lam_cvx, lam_cms, lake_bucket, args.stage, account_id, results)),
        (check_a5_sns,       (sns, cfn, args.stage, results)),
        (check_a6_vins,      (ath, args.stage, wg, results)),
        (check_a7_policy,    (ath, ssm, fr_db, wg, args.stage, results)),
    ):
        try:
            fn(*fn_args)
        except Exception as exc:  # noqa: BLE001
            record(results, fn.__name__, "FAIL",
                   f"unexpected exception: {type(exc).__name__}: {exc}"[:300])

    fails   = [r for r in results if r[1] == "FAIL"]
    skipped = [r for r in results if r[1] == "SKIP"]
    passed  = [r for r in results if r[1] == "PASS"]
    print(
        f"\nSUMMARY: {len(passed)} PASS, {len(fails)} FAIL, {len(skipped)} SKIP",
        flush=True,
    )
    return 1 if fails else (2 if skipped else 0)


# ---------------------------------------------------------------------------
# --part cms / --part cms-next (task 7.5): the rebalancing screens' routes, live
#
# Writes go through the screens' own routes only (CMS's approve proxy, CMS's groundings
# and return-to-service routes), with temporary fleet-operators; the script itself never
# writes to DynamoDB (the botocore guard admits reads only, plus the temp users).
# ---------------------------------------------------------------------------

#: No writes from 03:45 to 07:15 UTC: ADP's bookings jobs (04:15-05:15), the Tier 2 runs that
#: read groundings and locations (recall 05:00, warranty 05:20, service 05:40, rebalance 06:00)
#: and CMS's rebalance refresh (06:40) all run inside it.
_CMS_NO_WRITE_START_MIN = 3 * 60 + 45
_CMS_NO_WRITE_END_MIN = 7 * 60 + 15
_RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")
_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
_LOC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ACTION_ID_RE = re.compile(r"^AVX-ACT-[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
_FINDING_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
#: The test grounding: a campaign id no real campaign uses (CMS's pattern MRD-dddd-ddd).
_TEST_CAMPAIGN = "MRD-9999-975"
_PROBE_PREFIX = "LIVETEST-75-"
_FI = "/api/v1/fleet-intelligence"
# Exact sentence required on /demand and /gaps for a depot fleet (G9-D4, CMS 75007152b).
# C2 requires this sentence on every demand/gaps route, all figures null (fail closed on old
# sentences and any figures present), and both routes must agree.
_DEPOT_DEMAND_SENTENCE = "Demand forecasts and moves apply to rental and car-share fleets."
_GROUNDED_SENTENCE = "Vehicle {vin} is grounded and can't be moved."
_CMS_FLEET_403 = "Not authorized for the requested fleet scope"
_P95_LIMIT_S = 3.0
_P95_SAMPLES = 20
_CACHE_MAX_AGE_H = 26.0
#: The requests the Rebalancing page and the overview tile make (CMS rebalanceSource.ts,
#: fiApiSources.ts readUtilization at CMS 20e6ce01e).
_CMS_READS = (("locations", {}), ("utilization", {"days": "7"}), ("utilization", {"days": "1"}),
              ("demand", {"days": "7"}), ("gaps", {"horizon": "7"}), ("moves", {}))
_DEPOT_READS = (("utilization", {"days": "7"}), ("utilization", {"days": "1"}),
                ("demand", {"days": "7"}), ("gaps", {"horizon": "7"}))
#: CMS refuses return to service on a recall view older than 26 h; keep a 2 h margin.
_RECALL_MAX_AGE_S = 24 * 3600
_RTS_WAITS_S = (0.0, 2.0, 5.0)
_FINDING_MIN_LIFE = datetime.timedelta(hours=2)


def _wall() -> datetime.datetime:
    """The real UTC time, for data freshness (Finding expiry, recall view age). ``_now`` is
    the write-window clock, which tests move."""
    return datetime.datetime.now(datetime.timezone.utc)


def _cms_writes_allowed_now() -> bool:
    n = _now()
    mins = n.hour * 60 + n.minute
    return not (_CMS_NO_WRITE_START_MIN <= mins <= _CMS_NO_WRITE_END_MIN)


def _https_base(url: Any) -> str | None:
    """*url* without a trailing slash if it is https to a named host with no credentials,
    query or fragment (the operator's token goes to it), else None."""
    import urllib.parse  # noqa: PLC0415
    if not isinstance(url, str) or any(c.isspace() for c in url):
        return None
    p = urllib.parse.urlsplit(url)
    if (p.scheme != "https" or not p.hostname or p.username or p.password
            or p.query or p.fragment):
        return None
    return url.rstrip("/")


def _state_dir() -> Path:
    d = Path(os.environ.get("FI_REBAL_LV_STATE_DIR")
             or Path.home() / ".local" / "state" / "live-verify-fi-rebalancing")
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def _new_run_id() -> str:
    import secrets  # noqa: PLC0415
    return f"{datetime.datetime.now(datetime.timezone.utc):%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"


def _save_manifest(m: dict) -> None:
    path = _state_dir() / f"{m['run_id']}.json"
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(m, fh, indent=1, default=str)
    os.replace(tmp, path)


def _load_manifest(run_id: str) -> dict:
    return json.loads((_state_dir() / f"{run_id}.json").read_text())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: urllib would copy the operator's token onto the redirect."""

    def redirect_request(self, *a: Any, **k: Any) -> None:  # noqa: D401
        return None


def _request(method: str, url: str, token: str, body: Any = None,
             params: dict | None = None) -> tuple[int, Any, float]:
    """One HTTPS request with ``Authorization: Bearer <token>``: (status, JSON body, seconds).

    A TLS context per call (the environment's trust store), no redirects. An HTTP error
    returns its status and body; no answer returns ``(-1, {"_error": <class>}, seconds)``."""
    import ssl  # noqa: PLC0415
    import urllib.error  # noqa: PLC0415
    import urllib.parse  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    data = None if body is None else json.dumps(body).encode()
    headers = {"Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    opener = urllib.request.build_opener(
        _NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def _parse(raw: bytes) -> Any:
        text = raw.decode("utf-8", errors="replace")
        try:
            return json.loads(text) if text else {}
        except ValueError:
            return {"_raw": text[:200]}

    t0 = time.monotonic()
    try:
        with opener.open(req, timeout=30) as resp:
            raw = resp.read()
            return resp.status, _parse(raw), time.monotonic() - t0
    except urllib.error.HTTPError as exc:
        return exc.code, _parse(exc.read() or b""), time.monotonic() - t0
    except Exception as exc:  # noqa: BLE001
        return -1, {"_error": type(exc).__name__}, time.monotonic() - t0


def _get_json(url: str, token: str, params: dict | None = None) -> tuple[int, Any, float]:
    return _request("GET", url, token, params=params)


def _post_json(url: str, token: str, body: Any) -> tuple[int, Any, float]:
    return _request("POST", url, token, body=body)


def _put_json(url: str, token: str, body: Any) -> tuple[int, Any, float]:
    return _request("PUT", url, token, body=body)


def _cms_tables(stage: str) -> dict[str, str]:
    return {"vehicles": f"cms-{stage}-storage-vehicles",
            "groundings": f"cms-{stage}-storage-vehicle-groundings",
            "moves": f"cms-{stage}-storage-vehicle-moves",
            "locations": f"cms-{stage}-storage-fleet-locations",
            "drivers": f"cms-{stage}-storage-drivers",
            "live_plan": f"cms-{stage}-storage-live-plan",
            "findings": f"vsa-{stage}-avx-findings",
            "actions": f"vsa-{stage}-avx-actions"}


def _decision_id(finding_id: str) -> str:
    """AVX's per-Finding decision Action id (contract § 4), computed here so the script needs
    no AVX package import; a test pins it equal to AVX's ``_derive_decision_action_id``."""
    import hashlib  # noqa: PLC0415
    h = hashlib.sha256(f"{finding_id}|fi-decision".encode()).hexdigest()[:32]
    return f"AVX-ACT-{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


class _CmsRun:
    """State shared by the --part cms items."""

    def __init__(self, *, stage: str, run_id: str, base: str, allow_writes: bool,
                 approve_move: bool, manifest: dict) -> None:
        self.stage, self.run_id, self.base = stage, run_id, base
        self.allow_writes, self.approve_move = allow_writes, approve_move
        self.manifest = manifest
        self.t = _cms_tables(stage)
        self.move: dict | None = None      # {finding_id, action_id, vins, from, to}
        self.probe: dict | None = None     # {finding_id, action_id, vin, vehicle_id, origin}
        self.rental: str | None = None     # temp operator tokens
        self.delivery: str | None = None
        self.write_failed = False
        self.cleanup_failed = False
        self.probe_passed = False
        self.moved = False

    def url(self, path: str) -> str:
        return f"{self.base}{_FI}/{path}"

    def save(self) -> None:
        _save_manifest(self.manifest)

    def write_skip(self) -> str | None:
        """Why a write item may not run now, or None."""
        if not self.allow_writes:
            return "not run: --allow-writes not given"
        if not self.approve_move:
            return "not run: --approve-move not given (it approves a real move and grounds a car)"
        if self.cleanup_failed:
            return "not run: an earlier cleanup failed"
        if self.write_failed:
            return "not run: an earlier item failed (one run approves at most one move)"
        if not _cms_writes_allowed_now():
            return "not run: inside the 03:45-07:15 UTC write window"
        if self.rental is None or self.delivery is None:
            return "not run: the temporary operators were not created"
        return None


def _vehicle_rows(ddb: Any, run: _CmsRun) -> list[dict]:
    return _ddb_scan_all(ddb, run.t["vehicles"],
                         ProjectionExpression="vehicleId, vin, fleetId, locationId")


def _open_groundings(ddb: Any, run: _CmsRun, vehicle_id: str) -> list[dict]:
    """CMS's rule (groundings.open_groundings_for_vehicle): every row of the vehicle's
    partition without ``returnedAt``."""
    rows = _ddb_query_all(ddb, run.t["groundings"], KeyConditionExpression="vehicleId = :v",
                          ExpressionAttributeValues={":v": {"S": vehicle_id}},
                          ConsistentRead=True)
    return [r for r in rows if not r.get("returnedAt")]


#: Recall-view reads are capped: a shard is ~100 KB; a much larger object is not CMS's.
_RECALL_MAX_BYTES = 64 * 1024 * 1024


def _bounded_read(s3: Any, bucket: str, key: str) -> bytes:
    body = s3.get_object(Bucket=bucket, Key=key)["Body"]
    raw = body.read(_RECALL_MAX_BYTES + 1)
    if len(raw) > _RECALL_MAX_BYTES:
        raise ValueError(f"s3://{bucket}/{key} is over {_RECALL_MAX_BYTES} bytes")
    return raw


def _recall_clear(s3: Any, bucket: str, vin: str, now: datetime.datetime) -> str | None:
    """None when CMS's return to service would find *vin* in a fresh recall cohort with no
    unremedied row at all (stricter than CMS's blocking rule, so never looser); else why not."""
    import gzip  # noqa: PLC0415
    import hashlib  # noqa: PLC0415
    prefix = "fleet-intelligence/_cache/fi-recall-views-v1/"
    man = json.loads(_bounded_read(s3, bucket, prefix + "manifest.json"))
    at = _parse_ts(man.get("computedAt"))
    if at is None or (now - at).total_seconds() > _RECALL_MAX_AGE_S:
        return f"recall views computed {man.get('computedAt')!r} (older than 24 h)"
    gen = man.get("generation")
    if not isinstance(gen, str) or not re.fullmatch(r"\d{8}T\d{12}Z-[0-9a-f]{8}", gen):
        return "recall manifest has no valid generation"
    shard = int.from_bytes(hashlib.sha256(vin.encode()).digest()[:4], "big") % 16
    raw = _bounded_read(s3, bucket, f"{prefix}{gen}/recall/vin-shard-{shard:02d}.json.gz")
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
        text = gz.read(_RECALL_MAX_BYTES + 1)
    if len(text) > _RECALL_MAX_BYTES:
        raise ValueError(f"recall shard {shard:02d} is over {_RECALL_MAX_BYTES} bytes unpacked")
    view = json.loads(text)
    if vin not in {r.get("vin") for r in view.get("cohort") or []}:
        return "not in the recall cohort (return to service would be refused)"
    if any(r.get("vin") == vin and r.get("remedied") is not True
           for r in view.get("vehicle_status") or []):
        return "has an unremedied recall row"
    return None


def check_c0_candidates(run: _CmsRun, *, ddb: Any, s3: Any, results_bucket: str,
                        results: list) -> None:
    """C0 (read-only): a live rental move the screen could approve (every car at its origin,
    none grounded, on the live roster or held by an iOS test user, room at the destination),
    and a second live move with a car usable for the grounded probe."""
    now = _wall()
    since = _iso(now - datetime.timedelta(days=4))
    items = _ddb_query_all(ddb, run.t["findings"], IndexName="by_agent_computed_at",
                           KeyConditionExpression="agent_id = :a AND computed_at >= :s",
                           ExpressionAttributeValues={":a": {"S": _REBALANCE_AGENT_ID},
                                                      ":s": {"S": since}})
    live: list[dict] = []
    for f in items:
        loc = f.get("location") or {}
        vins = ((f.get("recommended_action") or {}).get("parameters") or {}).get("vehicle_vins")
        exp = _parse_ts(f.get("expires_at"))
        if (f.get("finding_kind") == "fleet.rebalance.move"
                and (f.get("scope") or {}).get("fleet_id") == _RENTAL_FLEET
                and exp is not None and exp > now + _FINDING_MIN_LIFE
                and _FINDING_ID_RE.match(str(f.get("finding_id")))
                and all(_LOC_RE.match(str(loc.get(k))) for k in ("from_location_id", "to_location_id"))
                and isinstance(vins, list) and vins and all(_VIN_RE.match(str(v)) for v in vins)):
            live.append(f)
    undecided = [f for f in live if "Item" not in ddb.get_item(
        TableName=run.t["actions"], Key={"action_id": {"S": _decision_id(f["finding_id"])}},
        ConsistentRead=True)]
    if not undecided:
        record(results, "C0 candidates", "SKIP",
               f"no rental move to approve: {len(live)} live move Finding(s), all decided")
        return

    by_vin: dict[str, list[dict]] = {}
    at_loc: dict[str, int] = {}
    for v in _vehicle_rows(ddb, run):
        by_vin.setdefault(v.get("vin"), []).append(v)
        if v.get("fleetId") == _RENTAL_FLEET:
            at_loc[v.get("locationId")] = at_loc.get(v.get("locationId"), 0) + 1
    roster = _ddb_query_all(ddb, run.t["live_plan"], KeyConditionExpression="planDate = :r",
                            ExpressionAttributeValues={":r": {"S": "roster"}})
    roster_ids = {r.get("vin") for r in roster} | {r.get("vehicleId") for r in roster}
    ios = {d.get("assignedVehicleId") for d in _ddb_scan_all(
        ddb, run.t["drivers"], FilterExpression="attribute_exists(cognitoEmail)",
        ProjectionExpression="assignedVehicleId")}

    def why_not(vin: str, origin: str) -> str | None:
        rows = by_vin.get(vin) or []
        if len(rows) != 1:
            return f"{len(rows)} registry rows"
        r = rows[0]
        if r.get("fleetId") != _RENTAL_FLEET or r.get("locationId") != origin:
            return f"at {r.get('locationId')} in {r.get('fleetId')}"
        if vin in roster_ids or r["vehicleId"] in roster_ids:
            return "on the live roster"
        if r["vehicleId"] in ios:
            return "held by an iOS test user"
        if _open_groundings(ddb, run, r["vehicleId"]):
            return "grounded"
        return None

    def room(to: str) -> int:
        item = ddb.get_item(TableName=run.t["locations"],
                            Key={"fleetId": {"S": _RENTAL_FLEET}, "locationId": {"S": to}}).get("Item")
        return (int(_ddb_deser(item).get("parkingSpaces", 0)) if item else 0) - at_loc.get(to, 0)

    def key(f: dict) -> tuple:
        return (len(f["recommended_action"]["parameters"]["vehicle_vins"]), f["expires_at"],
                f["finding_id"])

    reasons: list[str] = []
    for f in sorted(undecided, key=key):
        vins = list(f["recommended_action"]["parameters"]["vehicle_vins"])
        fr, to = f["location"]["from_location_id"], f["location"]["to_location_id"]
        bad = {v: why_not(v, fr) for v in vins}
        bad = {v: w for v, w in bad.items() if w}
        if bad:
            reasons.append(f"{f['finding_id']}: {next(iter(bad.values()))}")
            continue
        if room(to) < len(vins):
            reasons.append(f"{f['finding_id']}: no room at {to}")
            continue
        run.move = {"finding_id": f["finding_id"], "action_id": _decision_id(f["finding_id"]),
                    "vins": vins, "from": fr, "to": to}
        break
    if run.move is None:
        record(results, "C0 candidates", "SKIP",
               "no rental move candidate: " + "; ".join(reasons[:5]))
        return
    probe_why: list[str] = []
    for f in sorted((g for g in undecided if g["finding_id"] != run.move["finding_id"]),
                    key=lambda g: (g["expires_at"], g["finding_id"])):
        fr = f["location"]["from_location_id"]
        for v in sorted(f["recommended_action"]["parameters"]["vehicle_vins"]):
            if v in run.move["vins"]:
                continue
            w = why_not(v, fr) or _recall_clear(s3, results_bucket, v, now)
            if w:
                probe_why.append(f"{v}: {w}")
                continue
            run.probe = {"finding_id": f["finding_id"], "action_id": _decision_id(f["finding_id"]),
                         "vin": v, "vehicle_id": by_vin[v][0]["vehicleId"], "origin": fr}
            break
        if run.probe:
            break
    m = run.move
    detail = (f"move={m['finding_id']} ({m['from']} -> {m['to']}, {len(m['vins'])} car(s)); "
              + (f"probe={run.probe['finding_id']} vin={run.probe['vin']}" if run.probe
                 else "probe=none (" + ("; ".join(probe_why[:3]) or "no other live move") + ")"))
    run.manifest["candidates"] = {"move": run.move, "probe": run.probe}
    run.save()
    record(results, "C0 candidates", "PASS", detail)


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v


def _read_problems(path: str, code: int, body: Any) -> list[str]:
    """What is wrong with a rental fleet read of /<path> (real figures, fresh cache)."""
    if code != 200 or not isinstance(body, dict):
        return [f"{path} answered {code}"]
    if body.get("unavailable"):
        return [f"{path} unavailable: {body.get('reason') or body.get('depot_message')}"]
    p: list[str] = []
    if path == "locations":
        locs = body.get("locations") or []
        if body.get("rebalancing") is not True or not locs:
            p.append("locations: not a rebalancing fleet or no locations")
        elif sum(int(x.get("assignedVehicles") or 0) for x in locs) <= 0:
            p.append("locations: no assigned vehicles")
        return p
    if path == "moves":
        if not isinstance(body.get("rows"), list):
            p.append("moves: no rows list")
        return p
    age = body.get("cacheAgeHours")
    if not _num(age) or age > _CACHE_MAX_AGE_H:
        p.append(f"{path}: cache age {age!r} h")
    rows = body.get("rows")
    if not isinstance(rows, list) or not rows:
        p.append(f"{path}: no rows")
    if path == "utilization":
        s = body.get("summary") or {}
        if not (_num(s.get("utilizationPct")) and _num(s.get("onRentHours"))
                and _num(s.get("availableHours")) and s["availableHours"] > 0):
            p.append(f"utilization: summary {s!r}")
    elif path == "demand":
        if isinstance(rows, list) and not all(_num(r.get("forecastHours")) for r in rows):
            p.append("demand: a row without forecastHours")
    elif path == "gaps":
        s = body.get("summary") or {}
        if not isinstance(s.get("deficitLocations"), int):
            p.append(f"gaps: summary {s!r}")
    return p


def check_c1_rental_reads(run: _CmsRun, results: list) -> None:
    """C1: the rental fleet's Rebalancing page and overview tile reads show real figures."""
    problems, seen = [], []
    for path, params in _CMS_READS:
        code, body, _ = _get_json(run.url(path), run.rental, {**params, "fleetId": _RENTAL_FLEET})
        problems += _read_problems(path, code, body)
        if path == "utilization" and code == 200 and isinstance(body, dict):
            seen.append(f"utilization {params['days']}d {(body.get('summary') or {}).get('utilizationPct')}%")
        elif code == 200 and isinstance(body, dict):
            seen.append(f"{path} {len(body.get('rows') or body.get('locations') or [])} row(s)")
    record(results, "C1 rental reads", "FAIL" if problems else "PASS",
           "; ".join(problems) if problems else ", ".join(seen))


def check_c2_depot_reads(run: _CmsRun, results: list) -> None:
    """C2: utilization (both days) must be 200 with basis="trips", fleetKind="depot", a numeric
    summary.utilizationPct, and a fresh cache (≤26 h); demand and gaps must each give the exact
    sentence _DEPOT_DEMAND_SENTENCE with every figure null, and both must agree."""
    problems: list[str] = []
    util_figures: list[str] = []
    demand_sentences: list[str] = []
    for path, params in _DEPOT_READS:
        code, body, _ = _get_json(run.url(path), run.delivery, {**params, "fleetId": _DELIVERY_FLEET})
        b = body if isinstance(body, dict) else {}
        if path == "utilization":
            s = b.get("summary") or {}
            age = b.get("cacheAgeHours")
            if (code != 200 or b.get("basis") != "trips" or b.get("fleetKind") != "depot"
                    or not _num(s.get("utilizationPct"))
                    or not _num(age) or age > _CACHE_MAX_AGE_H):
                problems.append(f"{path} {params} answered {code} {json.dumps(b)[:120]}")
            else:
                util_figures.append(f"{params['days']}d {s['utilizationPct']}%")
        else:
            msg = b.get("depot_message")
            if (code != 200 or b.get("unavailable") is not True
                    or msg != _DEPOT_DEMAND_SENTENCE
                    or any(b.get(k) is not None for k in ("data", "rows", "summary"))):
                problems.append(f"{path} answered {code} {json.dumps(b)[:120]}")
            else:
                demand_sentences.append(msg)
    if not problems and len(set(demand_sentences)) != 1:
        problems.append(f"demand and gaps give different messages: {sorted(set(demand_sentences))}")
    record(results, "C2 depot reads", "FAIL" if problems else "PASS",
           "; ".join(problems) if problems else
           f"utilization trips {', '.join(util_figures)}; "
           f"demand/gaps: {demand_sentences[0]!r}")


def check_c3_latency(run: _CmsRun, results: list) -> None:
    """C3: each read's p95 over 20 requests is under 3 s."""
    import math  # noqa: PLC0415
    problems, seen = [], []
    for path, params in _CMS_READS:
        label = path + "".join(f" {k}={v}" for k, v in params.items())
        times, bad = [], []
        for _ in range(_P95_SAMPLES):
            code, _b, secs = _get_json(run.url(path), run.rental, {**params, "fleetId": _RENTAL_FLEET})
            (times if code == 200 else bad).append(secs if code == 200 else code)
        if bad:
            problems.append(f"{label}: {len(bad)} non-200 ({bad[:3]})")
            continue
        times.sort()
        p95 = times[math.ceil(0.95 * len(times)) - 1]
        seen.append(f"{label} {p95:.2f}s")
        if p95 >= _P95_LIMIT_S:
            problems.append(f"{label}: p95 {p95:.2f} s")
    record(results, "C3 latency", "FAIL" if problems else "PASS",
           ("; ".join(problems) + f" [{', '.join(seen)}]") if problems else "p95 " + ", ".join(seen))


def check_c4_isolation_reads(run: _CmsRun, results: list) -> None:
    """C4: the depot fleet's operator gets CMS's fleet refusal on every rental read, each
    after the same request as the rental operator succeeds (the control)."""
    problems = []
    for path, params in _CMS_READS:
        q = {**params, "fleetId": _RENTAL_FLEET}
        ok, _b, _ = _get_json(run.url(path), run.rental, q)
        if ok != 200:
            problems.append(f"control {path} answered {ok}")
            continue
        code, body, _ = _get_json(run.url(path), run.delivery, q)
        if code != 403 or not isinstance(body, dict) or body.get("error") != _CMS_FLEET_403:
            problems.append(f"{path} as {_DELIVERY_FLEET} answered {code}")
    record(results, "C4 isolation reads", "FAIL" if problems else "PASS",
           "; ".join(problems) if problems else
           f"{len(_CMS_READS)} rental reads refused for {_DELIVERY_FLEET}, each after a 200 control")


def _grounding_key(run: _CmsRun) -> dict:
    return {"vehicleId": {"S": run.probe["vehicle_id"]}, "actionId": {"S": _PROBE_PREFIX + run.run_id}}


def _read_grounding(ddb: Any, run: _CmsRun) -> dict | None:
    item = ddb.get_item(TableName=run.t["groundings"], Key=_grounding_key(run),
                        ConsistentRead=True).get("Item")
    return _ddb_deser(item) if item else None


def _vehicle(ddb: Any, run: _CmsRun, vehicle_id: str) -> dict:
    item = ddb.get_item(TableName=run.t["vehicles"], Key={"vehicleId": {"S": vehicle_id}},
                        ConsistentRead=True).get("Item")
    return _ddb_deser(item) if item else {}


def _move_record(ddb: Any, run: _CmsRun, action_id: str) -> dict | None:
    item = ddb.get_item(TableName=run.t["moves"],
                        Key={"fleetId": {"S": _RENTAL_FLEET}, "moveId": {"S": action_id}},
                        ConsistentRead=True).get("Item")
    return _ddb_deser(item) if item else None


def _return_to_service(ddb: Any, run: _CmsRun, token: str | None,
                       written: bool | None) -> tuple[bool, str]:
    """Return the probe car to service through CMS's route, with retries; confirmed by a
    consistent read of this run's grounding row. Signals are held by the caller.

    *written*: True when the grounding route answered 2xx, False when it refused (4xx, nothing
    written), None when its outcome is unknown (no answer or 5xx: the Lambda may still write).
    A missing row is final only when nothing was written; with an unknown outcome the waits run
    out and the run fails with the cleanup pointer. Another open grounding on the car stops it:
    CMS's return to service closes every open row, and the script must not close a real one.
    """
    vid = run.probe["vehicle_id"]
    last = "not tried"
    for wait in _RTS_WAITS_S:
        if wait:
            time.sleep(wait)
        try:
            row = _read_grounding(ddb, run)
            if row is not None and row.get("returnedAt"):
                return True, f"returned at {row['returnedAt']}"
            if row is None:
                if written is False:
                    return True, "the grounding route wrote nothing"
                last = "no grounding row yet (the grounding's outcome is unknown)"
                continue
            others = [g for g in _open_groundings(ddb, run, vid)
                      if g.get("actionId") != row.get("actionId")]
            if others:
                return False, (f"another open grounding on the car ({others[0].get('actionId')}); "
                               "not returning it, because CMS would close that one too")
            if token is None:
                return False, "no operator token to return it with"
            code, body, _ = _post_json(run.url(f"groundings/{vid}/return-to-service"), token, {})
            last = f"return-to-service answered {code} {json.dumps(body)[:160]}"
            row = _read_grounding(ddb, run)
            if row is not None and row.get("returnedAt") and not _open_groundings(ddb, run, vid):
                return True, f"returned at {row['returnedAt']}"
        except Exception as exc:  # noqa: BLE001 — one step, one try; the next wait retries
            last = f"{type(exc).__name__}: {exc}"[:200]
    return False, last


def _cleanup_pointer(run: _CmsRun, what: str) -> None:
    try:
        print(f"  CLEANUP NEEDED: {what}. Recover, from the AVX repo root, with: "
              f"FI_REBALANCING_LIVE_GO={run.stage} LIVE_TESTS=1 .venv/bin/python "
              f"scripts/live-verify-fi-rebalancing.py --part cms --stage {run.stage} "
              f"--workgroup <wg> --cms-api-endpoint {run.base} --allow-writes --approve-move "
              f"--cleanup-only --run-id {run.run_id}", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 — a lost line must not stop the cleanup after it
        pass


def _auto_cleanup_pointer(run_id: str, stage: str, cms_base: str, avx_base: str,
                          workgroup: str, what: str) -> None:
    """Print a --part auto cleanup-only command to stderr so the operator can recover.

    Mirrors 7.5's _cleanup_pointer: printed on any U6 return-to-service failure so the
    operator knows exactly which --run-id to pass to --cleanup-only.  Swallowed on any
    exception so a formatting error never stops the surrounding cleanup block.
    """
    try:
        print(
            f"  CLEANUP NEEDED: {what}. Recover, from the AVX repo root, with: "
            f"FI_REBALANCING_LIVE_GO={stage} LIVE_TESTS=1 .venv/bin/python "
            f"scripts/live-verify-fi-rebalancing.py --part auto --stage {stage} "
            f"--workgroup {workgroup} "
            f"--cms-api-endpoint {cms_base} --avx-api-endpoint {avx_base} "
            f"--allow-writes --cleanup-only --run-id {run_id}",
            file=sys.stderr, flush=True,
        )
    except Exception:  # noqa: BLE001 — a lost line must not stop the cleanup after it
        pass


def check_c5_grounded_refusal(run: _CmsRun, *, ddb: Any, results: list) -> None:
    """C5: a car grounded through Stage C's route can't be moved. The screen's approve of a
    move naming it is refused (AVX records CMS's 409), CMS's move route gives the grounding
    sentence, nothing moves; then the car goes back into service through Stage C's route."""
    skip = run.write_skip()
    if skip is None and run.probe is None:
        skip = "not run: no live move has a car usable for the probe (C0)"
    if skip:
        record(results, "C5 grounded move refused", "SKIP", skip)
        record(results, "C5 isolation groundings", "SKIP", skip)
        record(results, "C5 return to service", "SKIP", skip)
        return
    p = run.probe
    gid = _PROBE_PREFIX + run.run_id
    posted = finished = iso_done = False
    written: bool | None = None
    problems: list[str] = []
    try:
        if _open_groundings(ddb, run, p["vehicle_id"]):
            problems.append(f"{p['vin']} was already grounded")
            return
        run.manifest["grounding"] = {"vehicle_id": p["vehicle_id"], "vin": p["vin"],
                                     "action_id": gid, "returned": False}
        run.save()
        posted = True
        gbody = {"vin": p["vin"], "campaignId": _TEST_CAMPAIGN, "findingId": gid, "actionId": gid}
        code, body, _ = _post_json(run.url("groundings"), run.rental, gbody)
        written = True if code in (200, 201) else (False if 400 <= code < 500 else None)
        if code not in (200, 201):
            problems.append(f"grounding answered {code} {json.dumps(body)[:160]}")
            return
        row = _read_grounding(ddb, run)
        if not row or row.get("returnedAt") or row.get("openFleetId") != _RENTAL_FLEET:
            problems.append(f"the grounding of {p['vin']} did not read back open: {row!r}"[:240])
            return
        code, body, _ = _post_json(run.url(f"actions/{p['finding_id']}/approve"), run.rental,
                                   {"parameters": {"vehicle_vins": [p["vin"]]}})
        dec = ((body or {}).get("decision") or {}) if isinstance(body, dict) else {}
        trail = dec.get("auditTrail") or [{}]
        if code != 200 or dec.get("status") != "declined" or trail[-1].get("errorStatus") != 409:
            problems.append(f"the approve of {p['finding_id']} with grounded {p['vin']} answered "
                            f"{code}, status {dec.get('status')!r}, error {trail[-1].get('errorStatus')!r}")
        else:
            # Only once the screen's approve showed CMS's refusal: if AVX never reached CMS and
            # CMS's grounding check were broken, this request would be the one that moves the car.
            code, body, _ = _post_json(run.url("moves"), run.rental,
                                       {"actionId": p["action_id"], "vehicle_vins": [p["vin"]]})
            want = {"error": _GROUNDED_SENTENCE.format(vin=p["vin"])}
            if code != 409 or body != want:
                problems.append(f"CMS's move route answered {code} {json.dumps(body)[:160]}")
        # Isolation on the two routes CMS's fleet gate defers to their handlers, while the car
        # is grounded: the depot operator repeats the grounding (same key: even failing open it
        # writes nothing) and tries to return the car to service, each after the rental
        # operator's identical grounding. Failing open, the return to service closes every open
        # row on the car, so it is sent only while this run's grounding is the car's only one.
        ctl, _b, _ = _post_json(run.url("groundings"), run.rental, gbody)
        dg, dgb, _ = _post_json(run.url("groundings"), run.delivery, gbody)
        probes = [("grounding", dg, dgb)]
        others = [g for g in _open_groundings(ddb, run, p["vehicle_id"]) if g.get("actionId") != gid]
        if not others:
            dr, drb, _ = _post_json(run.url(f"groundings/{p['vehicle_id']}/return-to-service"),
                                    run.delivery, {})
            probes.append(("return to service", dr, drb))
        iso = []
        if ctl != 200:
            iso.append(f"control: the rental operator's repeat grounding answered {ctl}")
        for label, c, b in probes:
            if c != 403 or not isinstance(b, dict) or b.get("error") != _CMS_FLEET_403:
                iso.append(f"{label} as {_DELIVERY_FLEET} answered {c} {json.dumps(b)[:100]}")
        if iso:
            run.write_failed = True    # one run, one move: a fail-open here stops C6
        record(results, "C5 isolation groundings",
               "FAIL" if iso else ("SKIP" if others else "PASS"),
               "; ".join(iso) if iso else
               (f"{_DELIVERY_FLEET} refused on the grounding route; the return-to-service probe "
                f"was not sent: another open grounding on the car ({others[0].get('actionId')})"
                if others else
                f"{_DELIVERY_FLEET} refused on the grounding and return-to-service routes"))
        iso_done = True
        if _move_record(ddb, run, p["action_id"]) is not None:
            problems.append(f"a move record {p['action_id']} exists")
        loc = _vehicle(ddb, run, p["vehicle_id"]).get("locationId")
        if loc != p["origin"]:
            problems.append(f"{p['vin']} moved to {loc}")
        finished = True
    except Exception as exc:  # noqa: BLE001
        problems.append(f"{type(exc).__name__}: {exc}"[:200])
    finally:
        if not iso_done:
            record(results, "C5 isolation groundings", "SKIP",
                   "not run: C5 stopped before its isolation probes")
        if not finished and not problems:
            problems.append("interrupted before its checks finished")
        if problems:
            run.write_failed = True
        else:
            run.probe_passed = True
        record(results, "C5 grounded move refused", "FAIL" if problems else "PASS",
               "; ".join(problems) if problems else
               f"{p['vin']} grounded; the screen's approve was refused (409); CMS said "
               f"{_GROUNDED_SENTENCE.format(vin=p['vin'])!r}; nothing moved")
        if posted:
            with _signals_held():
                ok, detail = _return_to_service(ddb, run, run.rental, written)
                run.manifest["grounding"]["returned"] = ok
                try:
                    run.save()
                except OSError:
                    pass
            if not ok:
                run.cleanup_failed = True
                _cleanup_pointer(run, f"{p['vin']} ({p['vehicle_id']}) is still grounded")
            record(results, "C5 return to service", "PASS" if ok else "FAIL",
                   f"{p['vin']}: {detail}")
        else:
            record(results, "C5 return to service", "SKIP", "nothing was grounded")


def check_c6_move(run: _CmsRun, *, ddb: Any, results: list) -> None:
    """C6: one move approved through the screen's route (CMS's fleet check, then AVX's)
    changes the cars' locationId and writes CMS's move record."""
    skip = run.write_skip()
    if skip is None and run.move is None:
        skip = "not run: no rental move candidate (C0)"
    if skip is None and not run.probe_passed:
        skip = ("not run: C5 did not pass (the move waits for the probe, so one run shows both "
                "and approves one real move)")
    if skip:
        record(results, "C6 move approved", "SKIP", skip)
        return
    m = run.move
    problems: list[str] = []
    finished = False
    try:
        if "Item" in ddb.get_item(TableName=run.t["actions"],
                                  Key={"action_id": {"S": m["action_id"]}}, ConsistentRead=True):
            problems.append(f"{m['finding_id']} was decided before the approve")
            return
        vids = {}
        for v in m["vins"]:
            rows = _ddb_query_all(ddb, run.t["vehicles"], IndexName="vin-index",
                                  KeyConditionExpression="vin = :v",
                                  ExpressionAttributeValues={":v": {"S": v}})
            # vin-index is KEYS_ONLY (CMS storage_stack.py): read the base row, as CMS does.
            base_row = _vehicle(ddb, run, rows[0]["vehicleId"]) if len(rows) == 1 else {}
            if (len(rows) != 1 or base_row.get("fleetId") != _RENTAL_FLEET
                    or base_row.get("locationId") != m["from"]):
                problems.append(f"{v} is no longer a single car at {m['from']}")
                return
            vids[v] = rows[0]["vehicleId"]
        run.manifest["move"] = {**m, "approved": "pending"}
        run.save()
        code, body, _ = _post_json(run.url(f"actions/{m['finding_id']}/approve"), run.rental,
                                   {"parameters": {"vehicle_vins": m["vins"]}})
        dec = ((body or {}).get("decision") or {}) if isinstance(body, dict) else {}
        if (code != 200 or dec.get("status") != "executed" or dec.get("decision") != "approved"
                or dec.get("targetRef") != m["action_id"]):
            problems.append(f"the approve answered {code}, status {dec.get('status')!r}, "
                            f"decision {dec.get('decision')!r}, targetRef {dec.get('targetRef')!r}")
        rec = _move_record(ddb, run, m["action_id"])
        if rec is None:
            problems.append(f"no move record {m['action_id']}")
            return
        run.moved = True
        created = _parse_ts(rec.get("createdAt"))
        eff = rec.get("effectiveDate")
        want_eff = (created.date() + datetime.timedelta(days=1)).isoformat() if created else None
        if (rec.get("fromLocationId") != m["from"] or rec.get("toLocationId") != m["to"]
                or list(rec.get("vins") or []) != m["vins"] or rec.get("decidedByKind") != "operator"
                or eff != want_eff):
            problems.append(f"move record differs: {json.dumps(rec, default=str)[:240]}")
        for v, vid in vids.items():
            loc = _vehicle(ddb, run, vid).get("locationId")
            if loc != m["to"]:
                problems.append(f"{v} is at {loc}, not {m['to']}")
        listed, token = False, None
        for _ in range(10):
            code, body, _ = _get_json(run.url("moves"), run.rental,
                                      {"fleetId": _RENTAL_FLEET, "limit": "200",
                                       **({"nextToken": token} if token else {})})
            if code != 200:
                problems.append(f"GET /moves answered {code}")
                break
            listed = listed or any(r.get("moveId") == m["action_id"] for r in body.get("rows") or [])
            token = body.get("nextToken")
            if listed or not token:
                break
        if not listed:
            problems.append(f"GET /moves does not list {m['action_id']}")
        run.manifest["move"] = {**m, "approved": True, "effective_date": eff,
                                "created_at": rec.get("createdAt")}
        run.save()
        finished = True
    except Exception as exc:  # noqa: BLE001
        problems.append(f"{type(exc).__name__}: {exc}"[:200])
    finally:
        if not finished and not problems:
            problems.append("interrupted before its checks finished")
        if problems:
            run.write_failed = True
        record(results, "C6 move approved", "FAIL" if problems else "PASS",
               "; ".join(problems) if problems else
               f"{m['finding_id']}: {len(m['vins'])} car(s) {m['from']} -> {m['to']}, "
               f"move {m['action_id']} effective {run.manifest['move'].get('effective_date')}")


def check_c7_isolation_writes(run: _CmsRun, *, ddb: Any, results: list) -> None:
    """C7: the depot fleet's operator is refused on the screen's approve (CMS's check 1) and
    on CMS's move route for the move C6 made, each after the rental operator's identical
    request succeeds. Both probes repeat a decided, executed move, so even a route that
    failed open would change nothing; the Action and the move record are compared after."""
    skip = run.write_skip() or (None if run.moved else "not run: C6 made no move")
    if skip:
        record(results, "C7 isolation writes", "SKIP", skip)
        return
    m = run.move
    problems: list[str] = []
    try:
        def snap() -> tuple:
            a = ddb.get_item(TableName=run.t["actions"], Key={"action_id": {"S": m["action_id"]}},
                             ConsistentRead=True).get("Item") or {}
            r = _move_record(ddb, run, m["action_id"]) or {}
            return (json.dumps(_ddb_deser(a), sort_keys=True, default=str),
                    json.dumps(r, sort_keys=True, default=str))
        before = snap()
        approve = run.url(f"actions/{m['finding_id']}/approve")
        abody = {"parameters": {"vehicle_vins": m["vins"]}}
        code, body, _ = _post_json(approve, run.rental, abody)
        if code != 200 or ((body or {}).get("decision") or {}).get("actionId") != m["action_id"]:
            problems.append(f"control: the rental operator's repeat approve answered {code}")
        code, body, _ = _post_json(approve, run.delivery, abody)
        if code != 403 or not isinstance(body, dict) or body.get("error") != "fleet_not_in_scope":
            problems.append(f"approve as {_DELIVERY_FLEET} answered {code} {json.dumps(body)[:120]} "
                            "(want 403 fleet_not_in_scope)")
        mbody = {"actionId": m["action_id"], "vehicle_vins": m["vins"]}
        code, body, _ = _post_json(run.url("moves"), run.rental, mbody)
        if code != 200 or not isinstance(body, dict) or body.get("replayed") is not True:
            problems.append(f"control: the rental operator's /moves replay answered {code}")
        code, body, _ = _post_json(run.url("moves"), run.delivery, mbody)
        if code != 403 or not isinstance(body, dict) or body.get("error") != _CMS_FLEET_403:
            problems.append(f"/moves as {_DELIVERY_FLEET} answered {code} {json.dumps(body)[:120]}")
        if snap() != before:
            problems.append("the Action or the move record changed during the probes")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"{type(exc).__name__}: {exc}"[:200])
    record(results, "C7 isolation writes", "FAIL" if problems else "PASS",
           "; ".join(problems) if problems else
           f"{_DELIVERY_FLEET} refused on the approve and on /moves, each after a rental control; "
           "nothing changed")


def check_c8_residue(run: _CmsRun, *, ddb: Any, cleanup: list, results: list) -> None:
    """C8 (always, once, in ``finally``): this run's keys only. The test grounding is
    returned, the probe wrote no move and left its car where it was, every temp user is gone."""
    problems: list[str] = []
    checked: list[str] = []
    g = run.manifest.get("grounding")
    if g:
        try:
            row = _read_grounding(ddb, run)
            if row is not None and not row.get("returnedAt"):
                problems.append(f"{g['vin']} ({g['vehicle_id']}) is still grounded by {g['action_id']}")
                _cleanup_pointer(run, f"{g['vin']} ({g['vehicle_id']}) is still grounded")
                if g.get("returned") is not False:
                    g["returned"] = False
                    try:
                        run.save()
                    except OSError as exc:
                        problems.append(f"manifest save failed: {type(exc).__name__}")
            checked.append("grounding")
            if _move_record(ddb, run, run.probe["action_id"]) is not None:
                problems.append(f"the probe wrote move record {run.probe['action_id']}")
            loc = _vehicle(ddb, run, run.probe["vehicle_id"]).get("locationId")
            if loc != run.probe["origin"]:
                problems.append(f"probe car {g['vin']} is at {loc}")
            checked.append("probe")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"residue read failed: {type(exc).__name__}")
    for cmd in cleanup:
        problems.append(f"temp user not removed; run: {cmd}")
    left = ""
    if g and run.probe:
        # Only when the probe's approve really decided its Finding (a grounding that failed
        # returns before the approve, leaving the Finding undecided).
        try:
            decided = "Item" in ddb.get_item(TableName=run.t["actions"],
                                             Key={"action_id": {"S": run.probe["action_id"]}},
                                             ConsistentRead=True)
        except Exception as exc:  # noqa: BLE001
            decided = False
            problems.append(f"residue read failed: {type(exc).__name__}")
        if decided:
            left = (f"; left by design: the probe Finding {run.probe['finding_id']}'s decided "
                    f"Action {run.probe['action_id']} (overridden, declined); never re-send it to "
                    "CMS's /moves")
    record(results, "C8 residue", "FAIL" if problems else "PASS",
           "; ".join(problems) if problems else
           ("checked " + ", ".join(checked + ["temp users"]) + left if checked
            else "nothing written"))


def _read_items(run: _CmsRun, results: list) -> None:
    if not run.allow_writes:
        for name in ("C1 rental reads", "C2 depot reads", "C3 latency", "C4 isolation reads"):
            record(results, name, "SKIP",
                   "not run: --allow-writes not given (the reads need temporary operators)")
        return
    if run.rental is None or run.delivery is None:
        for name in ("C1 rental reads", "C2 depot reads", "C3 latency", "C4 isolation reads"):
            record(results, name, "SKIP", "not run: the temporary operators were not created")
        return
    for name, fn in (("C1 rental reads", check_c1_rental_reads),
                     ("C2 depot reads", check_c2_depot_reads),
                     ("C3 latency", check_c3_latency),
                     ("C4 isolation reads", check_c4_isolation_reads)):
        try:
            fn(run, results)
        except Exception as exc:  # noqa: BLE001
            record(results, name, "FAIL", f"unexpected {type(exc).__name__}: {exc}"[:300])


def _cms_refusal(args: argparse.Namespace) -> tuple[str | None, str | None]:
    """(endpoint base, refusal): every check that needs no AWS call, before any client."""
    base = _https_base(args.cms_api_endpoint)
    if base is None:
        return None, "--cms-api-endpoint must be an https URL to a named host (no credentials, query or fragment)"
    if args.run_id is not None and not _RUN_ID_RE.match(args.run_id):
        return None, "--run-id must look like 20261021T170000Z-abc123"
    if args.cleanup_only and args.run_id is None:
        return None, "--cleanup-only needs --run-id"
    if args.part == "cms" and args.run_id is not None and not args.cleanup_only:
        return None, "--part cms takes --run-id only with --cleanup-only (a new run mints its own)"
    if args.part == "cms" and not args.cleanup_only and args.allow_writes \
            and not _cms_writes_allowed_now():
        return None, "inside the 03:45-07:15 UTC write window"
    return base, None


def _summary(results: list) -> int:
    fails = [r for r in results if r[1] == "FAIL"]
    skipped = [r for r in results if r[1] == "SKIP"]
    passed = [r for r in results if r[1] == "PASS"]
    print(f"\nSUMMARY: {len(passed)} PASS, {len(fails)} FAIL, {len(skipped)} SKIP", flush=True)
    return 1 if fails else (2 if skipped else 0)


def _part_cms(args: argparse.Namespace) -> int:
    """--part cms: C0-C8 (task 7.5), or --cleanup-only for an earlier run."""
    base, refusal = _cms_refusal(args)
    if refusal:
        print(f"REFUSED: {refusal}", file=sys.stderr)
        return 2
    _init_session(allow_writes=bool(args.allow_writes), stage=args.stage,
                  workgroup=args.workgroup, fn_name="", cms_only=True)
    run_id = args.run_id or _new_run_id()
    if args.run_id:
        try:
            manifest = _load_manifest(run_id)
        except (OSError, ValueError) as exc:
            print(f"REFUSED: no readable manifest for {run_id} ({type(exc).__name__})",
                  file=sys.stderr)
            return 2
        if manifest.get("stage") != args.stage or manifest.get("part") != "cms":
            print("REFUSED: the run's manifest is for another stage or part", file=sys.stderr)
            return 2
    else:
        manifest = {"run_id": run_id, "stage": args.stage, "part": "cms",
                    "started_at": _iso(datetime.datetime.now(datetime.timezone.utc))}
    run = _CmsRun(stage=args.stage, run_id=run_id, base=base,
                  allow_writes=bool(args.allow_writes), approve_move=bool(args.approve_move),
                  manifest=manifest)
    ddb = _client("dynamodb", CVX_REGION)
    idp = _client("cognito-idp", CVX_REGION)
    results: list[tuple[str, str, str]] = []
    cleanup: list[str] = []
    print(f"run id {run_id}", flush=True)
    if args.cleanup_only:
        return _cms_cleanup_only(run, ddb=ddb, idp=idp, cleanup=cleanup, results=results)
    run.save()
    account = _client("sts", CVX_REGION).get_caller_identity()["Account"]
    bucket = args.cms_results_bucket or f"cms-{args.stage}-athena-results-{account}-{ADP_REGION}"
    s3 = _client("s3", ADP_REGION)
    try:
        try:
            check_c0_candidates(run, ddb=ddb, s3=s3, results_bucket=bucket, results=results)
        except Exception as exc:  # noqa: BLE001
            run.write_failed = True
            record(results, "C0 candidates", "FAIL", f"unexpected {type(exc).__name__}: {exc}"[:300])
        with contextlib.ExitStack() as users:
            if run.allow_writes:
                try:
                    pool_id = _find_cms_pool(idp, args.stage)
                    client_id = _password_client(idp, pool_id)
                    run.rental = users.enter_context(
                        _temp_operator(idp, pool_id, client_id, _RENTAL_FLEET, cleanup))
                    run.delivery = users.enter_context(
                        _temp_operator(idp, pool_id, client_id, _DELIVERY_FLEET, cleanup))
                except Exception as exc:  # noqa: BLE001
                    record(results, "temp operators", "FAIL", f"{type(exc).__name__}: {exc}"[:200])
                    run.rental = run.delivery = None
            _read_items(run, results)
            if any(r[1] == "FAIL" for r in results):
                run.write_failed = True    # one run, one move: fix what failed, then re-run
            for name, fn in (("C5 grounded move refused", check_c5_grounded_refusal),
                             ("C6 move approved", check_c6_move),
                             ("C7 isolation writes", check_c7_isolation_writes)):
                try:
                    fn(run, ddb=ddb, results=results)
                except Exception as exc:  # noqa: BLE001
                    record(results, name, "FAIL", f"unexpected {type(exc).__name__}: {exc}"[:300])
    finally:
        check_c8_residue(run, ddb=ddb, cleanup=cleanup, results=results)
    return _summary(results)


def _cms_cleanup_only(run: _CmsRun, *, ddb: Any, idp: Any, cleanup: list, results: list) -> int:
    """X1: return an earlier run's test car to service (its manifest names it)."""
    g = run.manifest.get("grounding")
    if not g:
        record(results, "X1 cleanup", "PASS", "nothing to clean: the run grounded nothing")
        return _summary(results)
    if not _VIN_RE.match(str(g.get("vin"))) or not _FINDING_ID_RE.match(str(g.get("vehicle_id"))):
        record(results, "X1 cleanup", "FAIL", "the manifest's grounding fails its shape check")
        return _summary(results)
    run.probe = {"vehicle_id": g["vehicle_id"], "vin": g["vin"], "action_id": "", "origin": ""}
    try:
        row = _read_grounding(ddb, run)    # the row, never the manifest's flag (it can be stale)
    except Exception as exc:  # noqa: BLE001
        record(results, "X1 cleanup", "FAIL",
               f"{g['vin']}: the grounding row is unreadable ({type(exc).__name__}); re-run "
               "--cleanup-only")
        return _summary(results)
    if row is None or row.get("returnedAt"):
        g["returned"] = True
        try:
            run.save()
        except OSError as exc:
            record(results, "X1 cleanup", "FAIL",
                   f"{g['vin']} has {'no' if row is None else 'a returned'} test grounding, but the "
                   f"manifest was not saved ({type(exc).__name__})")
            return _summary(results)
        record(results, "X1 cleanup", "PASS",
               f"nothing to clean: {g['vin']} has {'no' if row is None else 'a returned'} test grounding")
        return _summary(results)
    if not run.allow_writes:
        record(results, "X1 cleanup", "SKIP", "not run: --allow-writes not given")
        return _summary(results)
    try:
        pool_id = _find_cms_pool(idp, run.stage)
        client_id = _password_client(idp, pool_id)
        with _temp_operator(idp, pool_id, client_id, _RENTAL_FLEET, cleanup) as token:
            with _signals_held():
                ok, detail = _return_to_service(ddb, run, token, False)
                run.manifest["grounding"]["returned"] = ok
                try:
                    run.save()
                except OSError as exc:
                    detail += f"; the manifest was not saved ({type(exc).__name__})"
                    ok = False
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, f"{type(exc).__name__}: {exc}"[:200]
    for cmd in cleanup:
        record(results, "cleanup", "FAIL", f"temp user not removed; run: {cmd}")
    record(results, "X1 cleanup", "PASS" if ok else "FAIL", f"{g['vin']}: {detail}")
    return _summary(results)


def _part_cms_next(args: argparse.Namespace) -> int:
    """--part cms-next (read-only): once ADP's bookings job has settled the move's effective
    day, the moved cars serve bookings at the destination and none at the origin."""
    if args.run_id is None or not _RUN_ID_RE.match(args.run_id):
        print("REFUSED: --part cms-next needs the --run-id of the --part cms run", file=sys.stderr)
        return 2
    try:
        m = _load_manifest(args.run_id)
    except (OSError, ValueError) as exc:
        print(f"REFUSED: no readable manifest for {args.run_id} ({type(exc).__name__})",
              file=sys.stderr)
        return 2
    mv = m.get("move") or {}
    vins, fr, to, eff = mv.get("vins"), mv.get("from"), mv.get("to"), mv.get("effective_date")
    if (m.get("stage") != args.stage or mv.get("approved") is not True
            or not isinstance(vins, list) or not vins
            or not all(isinstance(v, str) and _VIN_RE.match(v) for v in vins)
            or not all(isinstance(x, str) and _LOC_RE.match(x) for x in (fr, to))
            or not isinstance(eff, str) or not _DATE_RE.match(eff)
            or not _ID_RE.match(args.workgroup)):
        print("REFUSED: the run made no move, or its manifest or --workgroup fails the shape "
              "checks", file=sys.stderr)
        return 2
    _init_session(allow_writes=False, stage=args.stage, workgroup=args.workgroup, fn_name="",
                  cms_only=True)
    ath = _client("athena", ADP_REGION)
    db = f"adp_{args.stage}_fleet_rebalance"
    in_vins = ", ".join(f"'{v}'" for v in vins)
    results: list[tuple[str, str, str]] = []
    try:
        rows = athena_rows(ath, f"SELECT CAST(max(data_as_of) AS VARCHAR) AS d FROM {db}.bookings_runs",
                           args.workgroup)
        settled = rows[0][0] if rows else None
    except Exception as exc:  # noqa: BLE001
        record(results, "N1 settled", "FAIL", f"bookings_runs read failed: {type(exc).__name__}")
        return _summary(results)
    if not isinstance(settled, str) or settled[:10] < eff:
        nxt = datetime.date.fromisoformat(eff) + datetime.timedelta(days=1)
        record(results, "N1 settled", "SKIP",
               f"latest settled day {settled!r}; the bookings job settles {eff} on {nxt} at "
               "04:15 UTC, run after that")
        return _summary(results)
    record(results, "N1 settled", "PASS", f"bookings settled through {settled[:10]}")
    nxt_day = (datetime.date.fromisoformat(eff) + datetime.timedelta(days=1)).isoformat()
    try:
        vd = athena_rows(ath, f"SELECT vin, location_id FROM {db}.vehicle_days "
                              f"WHERE snapshot_date = DATE '{eff}' AND vin IN ({in_vins})",
                         args.workgroup)
        where: dict[str, list[str]] = {}
        for v, loc in vd:
            where.setdefault(v, []).append(loc)
        bad = [f"{v} at {where.get(v)}" for v in vins if where.get(v) != [to]]
        record(results, "N2 vehicle days", "FAIL" if bad else "PASS",
               ("not at " + to + " on " + eff + ": " + "; ".join(bad)) if bad
               else f"{len(vins)} car(s) at {to} on {eff}")
    except Exception as exc:  # noqa: BLE001
        record(results, "N2 vehicle days", "FAIL", f"vehicle_days read failed: {type(exc).__name__}")
    try:
        br = athena_rows(ath, f"SELECT vin, location_id, outcome, "
                              f"CAST(date(start_at) AS VARCHAR) AS start_day "
                              f"FROM {db}.booking_requests WHERE vin IN ({in_vins}) "
                              f"AND start_at >= TIMESTAMP '{eff} 00:00:00' "
                              f"AND start_at < TIMESTAMP '{nxt_day} 00:00:00'", args.workgroup)
        elsewhere = [f"{v} at {loc} ({day})" for v, loc, _o, day in br if loc != to]
        served = [v for v, loc, o, day in br if loc == to and o == "fulfilled" and day == eff]
        if elsewhere:
            record(results, "N3 bookings", "FAIL",
                   f"booked away from {to} on {eff}: " + "; ".join(elsewhere[:5]))
        elif not served:
            record(results, "N3 bookings", "SKIP",
                   f"no booking was served by the moved cars on {eff}, so the property can't be "
                   f"shown ({len(br)} booking(s) that day, none at the origin)")
        else:
            record(results, "N3 bookings", "PASS",
                   f"{len(served)} booking(s) on {eff} served at {to} by {sorted(set(served))}; "
                   f"none at {fr}")
    except Exception as exc:  # noqa: BLE001
        record(results, "N3 bookings", "FAIL", f"booking_requests read failed: {type(exc).__name__}")
    return _summary(results)


# ---------------------------------------------------------------------------
# --part review (task 7.7): the 7-day review of the move a --part cms run approved.
# Reads only, except the one temporary fleet-operator the CMS route needs (--allow-writes).
# ---------------------------------------------------------------------------

#: ADP's ``move_outcomes`` admits a move once ``effective_date <= current_date - 7 days``.
_REVIEW_DAYS = 7
#: The scheduled rebalance run (06:00 UTC) is the first to read the snapshot that the 05:15
#: materialise job writes on the review date.
_REVIEW_RUN_AT = datetime.time(6, 0)
#: CMS's refresh-rebalance-cache runs at 06:40 UTC; its outcome reaches the route after it.
_REVIEW_ROUTE_AT = datetime.time(6, 55)
_STARTED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")
_HISTORY_PAGES = 40
_RESULT_PAGES = 50
_MOVES_PAGES = 20
_REVIEW_FIGURES = ("predicted_on_rent_hours_gain", "actual_on_rent_hours_gain", "ratio_hours")
_ROUTE_FIGURES = {"predicted_on_rent_hours_gain": "predictedOnRentHoursGain",
                  "actual_on_rent_hours_gain": "actualOnRentHoursGain",
                  "ratio_hours": "ratioHours"}


class _Review:
    """State shared by the --part review items."""

    def __init__(self, *, stage: str, workgroup: str, base: str, move: dict,
                 agent_run: str | None) -> None:
        self.stage, self.workgroup, self.base = stage, workgroup, base
        self.move = move                    # {action_id, vins, from, to, effective_date}
        self.review_on = (datetime.date.fromisoformat(move["effective_date"])
                          + datetime.timedelta(days=_REVIEW_DAYS))
        self.agent_run = agent_run
        self.run: dict | None = None        # the agent run R1 read (R3 scores it)
        self.snapshot: dict | None = None   # R1's row: figures and materialised_on


def _fig(v: Any) -> float | None:
    """A figure as a float (Athena and DynamoDB give strings); None if absent or not a number.
    Zero is a figure."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _same(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-6)


def _figure_problems(figs: dict[str, float | None], where: str) -> list[str]:
    """Missing figures, and a ratio that is not actual / predicted."""
    missing = [k for k in _REVIEW_FIGURES if figs.get(k) is None]
    if missing:
        return [f"{where}: no {', '.join(missing)}"]
    p, a, r = (figs[k] for k in _REVIEW_FIGURES)
    if p == 0:
        return [f"{where}: predicted gain is 0, so no ratio"]
    if not _same(r, a / p):
        return [f"{where}: ratio {r} is not actual {a} / predicted {p} = {a / p}"]
    return []


def _review_move(m: dict, stage: str) -> dict | None:
    """The manifest's move after its shape checks (everything later reaches SQL text, a URL
    or a comparison), else None."""
    mv = m.get("move") or {}
    vins, fr, to = mv.get("vins"), mv.get("from"), mv.get("to")
    eff, aid = mv.get("effective_date"), mv.get("action_id")
    if (m.get("stage") != stage or m.get("part") != "cms" or mv.get("approved") is not True
            or not isinstance(vins, list) or not vins
            or not all(isinstance(v, str) and _VIN_RE.match(v) for v in vins)
            or not all(isinstance(x, str) and _LOC_RE.match(x) for x in (fr, to))
            or not isinstance(aid, str) or not _ACTION_ID_RE.match(aid)
            or not isinstance(eff, str) or not _DATE_RE.match(eff)):
        return None
    try:
        datetime.date.fromisoformat(eff)
    except ValueError:
        return None
    return {"action_id": aid, "vins": list(vins), "from": fr, "to": to, "effective_date": eff}


def _review_refusal(args: argparse.Namespace) -> tuple[str | None, dict | None, str | None]:
    """(endpoint base, move, refusal): every check that needs no AWS call, before any client."""
    base = _https_base(args.cms_api_endpoint)
    if base is None:
        return None, None, ("--cms-api-endpoint must be an https URL to a named host (no "
                            "credentials, query or fragment)")
    if args.run_id is None or not _RUN_ID_RE.match(args.run_id):
        return None, None, "--part review needs the --run-id of the --part cms run that made the move"
    if args.agent_run is not None and (not _STARTED_AT_RE.match(args.agent_run)
                                       or _parse_ts(args.agent_run) is None):
        return None, None, "--agent-run must be a run's started_at, like 2026-10-29T06:00:45.550Z"
    if not _ID_RE.match(args.workgroup):
        return None, None, "--workgroup fails its shape check"
    try:
        m = _load_manifest(args.run_id)
    except (OSError, ValueError) as exc:
        return None, None, f"no readable manifest for {args.run_id} ({type(exc).__name__})"
    move = _review_move(m, args.stage)
    if move is None:
        return None, None, ("the run made no approved move, or its manifest fails the shape "
                            "checks")
    return base, move, None


def _review_run(rv: _Review, ddb: Any) -> tuple[dict | None, str, str]:
    """(run record, status, why): the named run, else the first completed rebalance run that
    started on or after the review date's 06:00 UTC run."""
    since = datetime.datetime.combine(rv.review_on, _REVIEW_RUN_AT, datetime.timezone.utc)
    if rv.agent_run:
        rec = _run_record(ddb, rv.stage, rv.agent_run, wait_s=0)
        if rec is None:
            return None, "FAIL", f"no rebalance run record started_at={rv.agent_run}"
        t = _parse_ts(rec.get("started_at"))
        if t is None or t < since:
            return None, "FAIL", (f"run {rv.agent_run} started before {_iso(since)}, so it can't "
                                  f"have read the move's outcome (review date {rv.review_on})")
        if rec.get("status") != "completed":
            return None, "FAIL", f"run {rv.agent_run} is {rec.get('status')!r}, not completed"
        return rec, "PASS", ""
    runs: list[tuple[datetime.datetime, dict]] = []
    for r in _runs_since(ddb, rv.stage, since):
        t = _parse_ts(r.get("started_at"))
        if r.get("status") == "completed" and t is not None and t >= since:
            runs.append((t, r))
    if not runs:
        return None, "SKIP", (f"no completed rebalance run since {_iso(since)} (the move's review "
                              f"date is {rv.review_on}); re-run after that run")
    return min(runs, key=lambda x: x[0])[1], "PASS", ""


def _run_window(rec: dict) -> tuple[datetime.datetime, datetime.datetime]:
    lo, hi = _parse_ts(rec.get("started_at")), _parse_ts(rec.get("finished_at"))
    if lo is None or hi is None:
        raise ValueError(f"run times don't parse: {rec.get('started_at')!r}..{rec.get('finished_at')!r}")
    # computed_at is whole seconds; started_at has milliseconds (see _items_of_run).
    return lo.replace(microsecond=0), hi


def _query_history(ath: Any, wg: str, lo: datetime.datetime, hi: datetime.datetime,
                   sql: str) -> tuple[list[dict], int]:
    """(executions in *wg* submitted in [lo, hi] whose text is *sql*, how many others in that
    window name ``move_outcomes_snapshot``). ``ListQueryExecutions`` gives ids newest first;
    stop once a whole page was submitted over an hour before *lo*."""
    kw: dict = {"WorkGroup": wg, "MaxResults": 50}
    found: list[dict] = []
    near = 0
    for _ in range(_HISTORY_PAGES):
        page = ath.list_query_executions(**kw)
        ids = page.get("QueryExecutionIds") or []
        execs: list[dict] = []
        if ids:
            got = ath.batch_get_query_execution(QueryExecutionIds=ids)
            if got.get("UnprocessedQueryExecutionIds"):
                raise RuntimeError(f"{len(got['UnprocessedQueryExecutionIds'])} execution(s) in "
                                   f"{wg} could not be read")
            execs = got.get("QueryExecutions") or []
        times = []
        for e in execs:
            t = (e.get("Status") or {}).get("SubmissionDateTime")
            if not isinstance(t, datetime.datetime) or t.tzinfo is None:
                raise RuntimeError(f"execution {e.get('QueryExecutionId')} has no submission time")
            t = t.astimezone(datetime.timezone.utc)
            times.append(t)
            if lo <= t <= hi:
                q = e.get("Query") or ""
                if q.strip() == sql.strip():
                    found.append(e)
                elif "move_outcomes_snapshot" in q:
                    near += 1
        tok = page.get("NextToken")
        if not tok or (times and max(times) < lo - datetime.timedelta(hours=1)):
            return found, near
        kw["NextToken"] = tok
    raise RuntimeError(f"more than {_HISTORY_PAGES * 50} executions in {wg} since the run")


def _query_rows(ath: Any, qid: str) -> list[dict]:
    """Every result row of execution *qid*, keyed by the header row."""
    rows: list[list[str | None]] = []
    kw: dict = {"QueryExecutionId": qid}
    for _ in range(_RESULT_PAGES):
        page = ath.get_query_results(**kw)
        rows += [[c.get("VarCharValue") for c in r["Data"]] for r in page["ResultSet"]["Rows"]]
        tok = page.get("NextToken")
        if not tok:
            break
        kw["NextToken"] = tok
    else:
        raise RuntimeError(f"execution {qid}: more than {_RESULT_PAGES} result pages")
    if not rows:
        raise RuntimeError(f"execution {qid} returned no header row")
    head = rows[0]
    return [dict(zip(head, r)) for r in rows[1:]]


def check_r1_snapshot(rv: _Review, *, ddb: Any, ath: Any, results: list) -> None:
    """R1: the outcomes snapshot holds the move's row with predicted and actual figures and a
    ratio, as the agent's first run on or after the review date read it (its own
    ``select_outcomes`` execution in the agent's workgroup; read-only, no grant needed)."""
    from agents.fi_rebalancing.sources import _load  # noqa: PLC0415
    run, status, why = _review_run(rv, ddb)
    if run is None:
        record(results, "R1 snapshot", status, why)
        return
    lo, hi = _run_window(run)
    sql = _load("select_outcomes.sql", stage=rv.stage, fleet_id=_RENTAL_FLEET)
    found, near = _query_history(ath, rv.workgroup, lo - datetime.timedelta(seconds=2),
                                 hi + datetime.timedelta(seconds=2), sql)
    if len(found) != 1:
        hint = (f"; {near} other quer(ies) in the run name move_outcomes_snapshot, so the agent's "
                "text differs from select_outcomes.sql" if near and not found else "")
        record(results, "R1 snapshot", "FAIL",
               f"run {run['started_at']}: {len(found)} select_outcomes read(s) in {rv.workgroup} "
               f"during the run, want 1{hint}")
        return
    ex = found[0]
    qid = ex.get("QueryExecutionId", "")
    state = (ex.get("Status") or {}).get("State")
    if state != "SUCCEEDED":
        record(results, "R1 snapshot", "FAIL", f"run {run['started_at']}: its read {qid} is {state}")
        return
    to, eff = rv.move["to"], rv.move["effective_date"]
    read = _query_rows(ath, qid)
    mine = [r for r in read if r.get("to_location_id") == to and r.get("effective_date") == eff]
    if len(mine) != 1:
        mats = sorted({str(r.get("materialised_on")) for r in read})
        record(results, "R1 snapshot", "FAIL",
               f"run {run['started_at']} (read {qid}, {len(read)} row(s), materialised_on "
               f"{', '.join(mats) or 'none'}): {len(mine)} row(s) for {to} effective {eff}, want 1"
               " (a materialised_on before the review date means the 05:15 job ran late: re-run "
               "with --agent-run of a later run)")
        return
    row = mine[0]
    figs = {k: _fig(row.get(k)) for k in _REVIEW_FIGURES}
    problems = _figure_problems(figs, f"read {qid}")
    mat = row.get("materialised_on")
    if not isinstance(mat, str) or not _DATE_RE.match(mat) or mat < rv.review_on.isoformat():
        problems.append(f"snapshot materialised_on {mat!r} is before the review date {rv.review_on}")
    if problems:
        record(results, "R1 snapshot", "FAIL", "; ".join(problems))
        return
    rv.run, rv.snapshot = run, {**figs, "materialised_on": mat}
    record(results, "R1 snapshot", "PASS",
           f"run {run['started_at']} read {to} effective {eff} (query {qid}, snapshot "
           f"materialised_on {mat}): predicted {figs['predicted_on_rent_hours_gain']} h, "
           f"actual {figs['actual_on_rent_hours_gain']} h, ratio {figs['ratio_hours']}")


def _moves_rows(rv: _Review, token: str) -> tuple[int, list[dict], bool]:
    """(status, every row, outcomesUnavailable) of ``GET /moves`` for the rental fleet."""
    rows: list[dict] = []
    unavailable, nxt = False, None
    for _ in range(_MOVES_PAGES):
        code, body, _ = _get_json(f"{rv.base}{_FI}/moves", token,
                                  {"fleetId": _RENTAL_FLEET, "limit": "200",
                                   **({"nextToken": nxt} if nxt else {})})
        if code != 200 or not isinstance(body, dict) or not isinstance(body.get("rows"), list):
            return code if code != 200 else -2, rows, unavailable
        rows += body["rows"]
        unavailable = unavailable or body.get("outcomesUnavailable") is True
        nxt = body.get("nextToken")
        if not nxt:
            return 200, rows, unavailable
    raise RuntimeError(f"GET /moves: more than {_MOVES_PAGES} pages")


def check_r2_route(rv: _Review, code: int, rows: list[dict], unavailable: bool,
                   results: list) -> None:
    """R2: CMS's Rebalance history route, as the car-share operator, returns the move with a
    non-null outcome (the ``move_outcomes`` view's row, as CMS's 06:40 refresh cached it).

    Its figures must equal R1's snapshot row. On the review date both come from the same
    settled bookings (04:15 bookings job, 05:15 materialise, 06:40 refresh); on a later day
    this assumes ``booking_requests`` for [effective, effective + 7) never change afterwards."""
    m = rv.move
    if code != 200:
        record(results, "R2 history route", "FAIL", f"GET /moves answered {code}")
        return
    row = next((r for r in rows if r.get("moveId") == m["action_id"]), None)
    if row is None:
        record(results, "R2 history route", "FAIL",
               f"GET /moves ({len(rows)} row(s)) does not list {m['action_id']}")
        return
    problems: list[str] = []
    if (row.get("fromLocationId") != m["from"] or row.get("toLocationId") != m["to"]
            or sorted(row.get("vins") or []) != sorted(m["vins"])
            or row.get("effectiveDate") != m["effective_date"]):
        problems.append(f"the route's move differs from the manifest's: "
                        f"{json.dumps({k: row.get(k) for k in ('fromLocationId', 'toLocationId', 'vins', 'effectiveDate')})}")
    if unavailable or row.get("outcomesUnavailable") is True:
        problems.append("outcomesUnavailable: the tab shows a dash, not the outcome")
    o = row.get("outcome")
    if not problems and o is None:
        ready = datetime.datetime.combine(rv.review_on, _REVIEW_ROUTE_AT, datetime.timezone.utc)
        if _wall() < ready:
            record(results, "R2 history route", "SKIP",
                   f"no outcome yet: CMS's cache refreshes at 06:40 UTC on {rv.review_on}; re-run "
                   f"after {_iso(ready)}")
            return
        problems.append(f"{m['action_id']} has no outcome (reviewAvailableOn "
                        f"{row.get('reviewAvailableOn')!r}) after the review date's refresh")
    if not problems:
        if not isinstance(o, dict):
            problems.append(f"outcome is {o!r}")
        else:
            figs = {k: (o.get(v) if _num(o.get(v)) else None) for k, v in _ROUTE_FIGURES.items()}
            problems += _figure_problems(figs, "route outcome")
            if not problems and rv.snapshot is not None:
                diff = [f"{k} {figs[k]} vs snapshot {rv.snapshot[k]}" for k in _REVIEW_FIGURES
                        if not _same(figs[k], rv.snapshot[k])]
                if diff:
                    problems.append("the route differs from R1's snapshot row: " + "; ".join(diff))
    if problems:
        record(results, "R2 history route", "FAIL", "; ".join(problems))
        return
    compared = "equal to R1's snapshot row" if rv.snapshot is not None else "not compared (R1 did not pass)"
    record(results, "R2 history route", "PASS",
           f"{m['action_id']} {m['from']} -> {m['to']}: actual {o['actualOnRentHoursGain']} h of "
           f"predicted {o['predictedOnRentHoursGain']} h, ratio {o['ratioHours']} ({compared})")


def check_r3_evidence(rv: _Review, *, ddb: Any, results: list) -> None:
    """R3: the R1 run's move items into the destination carry the outcome in
    ``rebalance.recent_outcomes``: an entry computed in that run citing R1's snapshot, or one an
    earlier run on or after the review date wrote (its own snapshot from that date on). An entry
    from before the review date on a move the run kept alive is a FAIL, not a wait."""
    if rv.run is None or rv.snapshot is None:
        record(results, "R3 agent evidence", "SKIP", "not run: needs R1's run and row")
        return
    m, snap = rv.move, rv.snapshot
    lo, hi = _run_window(rv.run)
    items, bad = _items_of_run(_all_rebalancing_findings(ddb, rv.stage), rv.run, _wall())
    if bad:
        record(results, "R3 agent evidence", "FAIL", f"rows whose times don't parse: {bad[:5]}")
        return
    into = [f for f in items if f.get("finding_kind") == "fleet.rebalance.move"
            and (f.get("location") or {}).get("to_location_id") == m["to"]]
    want_ref = f"adp.move_outcomes_snapshot materialised_on={snap['materialised_on']}"
    since = datetime.datetime.combine(rv.review_on, _REVIEW_RUN_AT, datetime.timezone.utc)
    ref_prefix = "adp.move_outcomes_snapshot materialised_on="
    counted, problems = [], []
    for f in into:
        fid = f.get("finding_id", "?")
        ents = [e for e in f.get("evidence") or [] if e.get("kind") == "rebalance.recent_outcomes"]
        if len(ents) != 1:
            problems.append(f"{fid}: {len(ents)} recent_outcomes entries, want 1")
            continue
        e = ents[0]
        ca = _parse_ts(e.get("computed_at"))
        ref = e.get("source_ref") or ""
        if ca is not None and lo <= ca <= hi:
            if ref != want_ref:
                problems.append(f"{fid}: source_ref {ref!r}, want {want_ref!r}")
                continue
        elif ca is None or ca < since:
            # The run kept this live move (its computed_at is in the run) without rewriting its
            # evidence: the move's inputs_hash excludes recent_outcomes, so an unchanged move
            # keeps the outcomes it was written with. Its evidence for the destination then
            # lacks the move: Accept (c) is unmet, and a later run behaves the same.
            problems.append(f"{fid}: the run refreshed it but kept recent_outcomes computed "
                            f"{e.get('computed_at')!r}, before the review date (an unchanged "
                            "move keeps its evidence: inputs_hash excludes recent_outcomes)")
            continue
        else:   # written by an earlier run on or after the review date: its own snapshot
            mo = ref[len(ref_prefix):] if ref.startswith(ref_prefix) else ""
            if not _DATE_RE.match(mo) or mo < rv.review_on.isoformat():
                problems.append(f"{fid}: kept recent_outcomes cite {ref!r}, not a snapshot from "
                                f"the review date on")
                continue
        els = [x for x in (e.get("value") or []) if isinstance(x, dict)
               and x.get("to_location_id") == m["to"] and x.get("effective_date") == m["effective_date"]]
        if len(els) != 1:
            problems.append(f"{fid}: {len(els)} outcome(s) for {m['to']} effective "
                            f"{m['effective_date']}, want 1")
            continue
        diff = [f"{k} {els[0].get(k)!r} vs snapshot {snap[k]}" for k in _REVIEW_FIGURES
                if _fig(els[0].get(k)) is None or not _same(_fig(els[0].get(k)), snap[k])]
        if diff:
            problems.append(f"{fid}: " + "; ".join(diff))
            continue
        counted.append(f)
    if problems:
        record(results, "R3 agent evidence", "FAIL", "; ".join(problems))
        return
    if not counted:
        record(results, "R3 agent evidence", "SKIP",
               f"run {rv.run['started_at']} wrote no move into {m['to']} (recent_outcomes rides "
               "only on move items); re-run with --agent-run <started_at of a later run that "
               "moves cars there>")
        return
    record(results, "R3 agent evidence", "PASS",
           f"run {rv.run['started_at']}: " + ", ".join(
               f"{f['finding_id']} (confidence {f.get('confidence')})" for f in counted)
           + f" carry {m['effective_date']} ratio {snap['ratio_hours']} in rebalance.recent_outcomes")


def check_r4_young(rv: _Review, code: int, rows: list[dict], unavailable: bool,
                   results: list) -> None:
    """R4: every move under 7 days old has no outcome and ``reviewAvailableOn`` = its effective
    date + 7 days, so the tab shows "Review on <date>"."""
    if code != 200:
        record(results, "R4 young moves", "FAIL", f"GET /moves answered {code}")
        return
    if unavailable:
        record(results, "R4 young moves", "FAIL",
               "outcomesUnavailable: the tab shows a dash, not \"Review on <date>\"")
        return
    today = _wall().date()
    young, problems = [], []
    for r in rows:
        mid = r.get("moveId", "?")
        try:
            eff = datetime.date.fromisoformat(str(r.get("effectiveDate")))
        except ValueError:
            problems.append(f"{mid}: effectiveDate {r.get('effectiveDate')!r}")
            continue
        due = eff + datetime.timedelta(days=_REVIEW_DAYS)
        if due <= today:
            continue
        young.append(f"{mid} \"Review on {due.isoformat()}\"")
        if r.get("outcome") is not None:
            problems.append(f"{mid} (effective {eff}) has an outcome before {due}")
        if r.get("reviewAvailableOn") != due.isoformat():
            problems.append(f"{mid}: reviewAvailableOn {r.get('reviewAvailableOn')!r}, want {due}")
    if problems:
        record(results, "R4 young moves", "FAIL", "; ".join(problems))
    elif not young:
        record(results, "R4 young moves", "SKIP", "no move under 7 days old to show the review date")
    else:
        record(results, "R4 young moves", "PASS", "; ".join(young))


def _part_review(args: argparse.Namespace) -> int:
    """--part review (task 7.7): R1-R4. Reads only; --allow-writes admits only the temporary
    car-share operator the CMS route needs (deleted in ``finally``), else R2 and R4 SKIP."""
    base, move, refusal = _review_refusal(args)
    if refusal:
        print(f"REFUSED: {refusal}", file=sys.stderr)
        return 2
    _init_session(allow_writes=bool(args.allow_writes), stage=args.stage,
                  workgroup=args.workgroup, fn_name="", cms_only=True)
    rv = _Review(stage=args.stage, workgroup=args.workgroup, base=base, move=move,
                 agent_run=args.agent_run)
    ddb = _client("dynamodb", CVX_REGION)
    ath = _client("athena", ADP_REGION)
    results: list[tuple[str, str, str]] = []
    cleanup: list[str] = []
    print(f"review of {move['action_id']} ({move['from']} -> {move['to']}, effective "
          f"{move['effective_date']}, review date {rv.review_on})", flush=True)
    try:
        check_r1_snapshot(rv, ddb=ddb, ath=ath, results=results)
    except Exception as exc:  # noqa: BLE001
        record(results, "R1 snapshot", "FAIL", f"unexpected {type(exc).__name__}: {exc}"[:300])
    route: tuple[int, list[dict], bool] | None = None
    route_error: str | None = None
    if not args.allow_writes:
        for name in ("R2 history route", "R4 young moves"):
            record(results, name, "SKIP",
                   "not run: --allow-writes not given (the route needs a temporary operator)")
    else:
        try:
            idp = _client("cognito-idp", CVX_REGION)
            pool_id = _find_cms_pool(idp, args.stage)
            client_id = _password_client(idp, pool_id)
            with _temp_operator(idp, pool_id, client_id, _RENTAL_FLEET, cleanup) as token:
                try:
                    route = _moves_rows(rv, token)
                except Exception as exc:  # noqa: BLE001
                    route_error = f"route read failed: {type(exc).__name__}: {exc}"[:300]
        except Exception as exc:  # noqa: BLE001
            record(results, "temp operator", "FAIL", f"{type(exc).__name__}: {exc}"[:200])
        if route_error:
            record(results, "R2 history route", "FAIL", route_error)
            record(results, "R4 young moves", "SKIP", "not run: the route was not read")
        elif route is None:
            for name in ("R2 history route", "R4 young moves"):
                record(results, name, "SKIP", "not run: the route was not read")
        else:
            try:
                check_r2_route(rv, *route, results)
            except Exception as exc:  # noqa: BLE001
                record(results, "R2 history route", "FAIL", f"unexpected {type(exc).__name__}: {exc}"[:300])
    try:
        check_r3_evidence(rv, ddb=ddb, results=results)
    except Exception as exc:  # noqa: BLE001
        record(results, "R3 agent evidence", "FAIL", f"unexpected {type(exc).__name__}: {exc}"[:300])
    if route is not None:
        try:
            check_r4_young(rv, *route, results)
        except Exception as exc:  # noqa: BLE001
            record(results, "R4 young moves", "FAIL", f"unexpected {type(exc).__name__}: {exc}"[:300])
    for cmd in cleanup:
        record(results, "cleanup", "FAIL", f"temp user not removed; run: {cmd}")
    return _summary(results)


# ---------------------------------------------------------------------------
# --part auto (task 8.4): invoke the live auto-approver by hand to verify the
# full pipeline: rule evaluation, SigV4 dispatch to CMS, grounding probe, happy path.
# ---------------------------------------------------------------------------

#: Prefix for the per-run rule_id created in U2.
#: The full id is ``f"{_AUTO_RULE_ID_PREFIX}-{_run_id}"``, unique per run so
#: the residue check needs no time filter — any Action by this rule was created
#: in THIS run.
_AUTO_RULE_ID_PREFIX = "livetest-fi-rebal-auto"
#: Auto-approver Lambda name.
_AUTO_APPROVER_FN_FMT = "vsa-{stage}-avx-auto-approver-{region}"
#: Bounds SSM parameter.
_AUTO_BOUNDS_PARAM_FMT = "/vsa/{stage}/avx/fi-rebalancing/auto-approval-bounds"
#: Expected bounds (must match the Lambda's config; group8-notes G8-D9).
_AUTO_EXPECTED_BOUNDS = {"max_limit_usd": 500, "max_vehicles_per_auto_move": 5}
#: Path for the internal IAM-only moves route (CMS stage E Group 8, U6).
_AUTO_INTERNAL_MOVES_PATH = "/api/v1/fleet-intelligence/internal/moves"
#: CMS CloudFormation stack that exports FiInternalApiEndpoint.
_AUTO_CMS_STACK_FMT = "cms-{stage}-ui"
#: CMS CFN stack output key for the internal IAM API.
_AUTO_CMS_INTERNAL_OUTPUT_KEY = "FiInternalApiEndpoint"
#: grounding actionId prefix for --part auto's probe.
_AUTO_PROBE_PREFIX = "LIVETEST-84-"
#: W16 — seconds to widen U3's BETWEEN window on each side to tolerate operator /
#: Lambda clock skew.  A live Lambda's clock can lag or lead the operator's by up
#: to a few seconds; a tight window ([u3_start, u3_end], both from the operator's
#: clock) drops the run record whenever the skew exceeds the invoke's duration.
_U3_WINDOW_MARGIN_S = 10


def _auto_tables(stage: str) -> dict[str, str]:
    """Table and resource names needed for --part auto checks."""
    return {
        "vehicles":   f"cms-{stage}-storage-vehicles",
        "groundings": f"cms-{stage}-storage-vehicle-groundings",
        "moves":      f"cms-{stage}-storage-vehicle-moves",
        "locations":  f"cms-{stage}-storage-fleet-locations",
        "drivers":    f"cms-{stage}-storage-drivers",
        "live_plan":  f"cms-{stage}-storage-live-plan",
        "findings":   f"vsa-{stage}-avx-findings",
        "actions":    f"vsa-{stage}-avx-actions",
        "rules":      f"vsa-{stage}-avx-approval-rules",
        "runs":       f"vsa-{stage}-avx-agent-runs",
    }


def _auto_open_groundings(ddb: Any, t: dict, vehicle_id: str) -> list[dict]:
    rows = _ddb_query_all(ddb, t["groundings"], KeyConditionExpression="vehicleId = :v",
                          ExpressionAttributeValues={":v": {"S": vehicle_id}}, ConsistentRead=True)
    return [r for r in rows if not r.get("returnedAt")]


def _auto_vehicle(ddb: Any, t: dict, vehicle_id: str) -> dict:
    item = ddb.get_item(TableName=t["vehicles"], Key={"vehicleId": {"S": vehicle_id}},
                        ConsistentRead=True).get("Item") or {}
    return _ddb_deser(item)


def _auto_move(ddb: Any, t: dict, action_id: str) -> dict | None:
    item = ddb.get_item(TableName=t["moves"],
                        Key={"fleetId": {"S": _RENTAL_FLEET}, "moveId": {"S": action_id}},
                        ConsistentRead=True).get("Item")
    return _ddb_deser(item) if item else None


def _auto_read_grounding(ddb: Any, t: dict, vid: str, gid: str) -> dict | None:
    item = ddb.get_item(TableName=t["groundings"],
                        Key={"vehicleId": {"S": vid}, "actionId": {"S": gid}},
                        ConsistentRead=True).get("Item")
    return _ddb_deser(item) if item else None


def _auto_url(base: str, path: str) -> str:
    return f"{base}{_FI}/{path}"


def _auto_vehicle_id_of(ddb: Any, t: dict, vin: str) -> str:
    rows = _ddb_query_all(ddb, t["vehicles"], IndexName="vin-index",
                          KeyConditionExpression="vin = :v",
                          ExpressionAttributeValues={":v": {"S": vin}})
    if len(rows) != 1:
        raise RuntimeError(f"expected 1 vehicle row for vin {vin}, got {len(rows)}")
    return rows[0]["vehicleId"]


def _auto_return_to_service(ddb: Any, t: dict, base: str, token: str | None,
                             vid: str, gid: str, written: bool | None) -> tuple[bool, str]:
    """Return the probe car to service; retries as in _return_to_service."""
    last = "not tried"
    for wait in _RTS_WAITS_S:
        if wait:
            time.sleep(wait)
        try:
            row = _auto_read_grounding(ddb, t, vid, gid)
            if row is not None and row.get("returnedAt"):
                return True, f"returned at {row['returnedAt']}"
            if row is None:
                if written is False:
                    return True, "the grounding route wrote nothing"
                last = "no grounding row yet"
                continue
            others = [g for g in _auto_open_groundings(ddb, t, vid)
                      if g.get("actionId") != gid]
            if others:
                return False, (f"another open grounding on the car "
                               f"({others[0].get('actionId')}); not returning")
            if token is None:
                return False, "no operator token to return it with"
            code, body, _ = _post_json(_auto_url(base, f"groundings/{vid}/return-to-service"),
                                       token, {})
            last = f"return-to-service answered {code} {json.dumps(body)[:80]}"
            row = _auto_read_grounding(ddb, t, vid, gid)
            if row is not None and row.get("returnedAt") and not _auto_open_groundings(ddb, t, vid):
                return True, f"returned at {row['returnedAt']}"
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"[:100]
    # C5: "written is None" means the grounding route's outcome is unknown (5xx or no
    # answer); the Lambda may still commit after the waits.  Whether or not a row
    # appeared, the run fails — consistent with _return_to_service's contract.
    # Callers print _auto_cleanup_pointer() so the operator knows the --run-id.
    return False, last


def _auto_sigv4_post(url: str, body: bytes) -> tuple[int, Any]:
    """POST *url* signed with the current AWS credentials (SigV4 execute-api).

    Used for U1's caller-signed probe: the current credentials are the script's
    own identity (not the auto-approver role), so the resource-policy Deny fires.
    """
    import re as _re  # noqa: PLC0415
    import ssl  # noqa: PLC0415
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415
    from botocore.auth import SigV4Auth  # noqa: PLC0415
    from botocore.awsrequest import AWSRequest  # noqa: PLC0415

    if _SESSION is None:
        raise RuntimeError("_init_session must be called first")
    creds = _SESSION.get_credentials()
    if creds is None:
        raise RuntimeError("no credentials available for SigV4 signing")
    # Extract region from URL when it looks like an execute-api endpoint; else fall back.
    m = _re.search(r"execute-api\.([a-z][a-z0-9-]+)\.amazonaws\.com", url)
    region = m.group(1) if m else CVX_REGION
    request = AWSRequest(method="POST", url=url, data=body,
                         headers={"Content-Type": "application/json"})
    SigV4Auth(creds.get_frozen_credentials(), "execute-api", region).add_auth(request)
    headers = dict(request.headers)
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    opener = urllib.request.build_opener(
        _NoRedirect(), urllib.request.HTTPSHandler(context=ctx))
    try:
        with opener.open(req, timeout=30) as resp:
            raw = resp.read()
            text = raw.decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(text) if text else {}
            except ValueError:
                return resp.status, {"_raw": text[:200]}
    except urllib.error.HTTPError as exc:
        raw = exc.read() or b""
        text = raw.decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(text) if text else {}
        except ValueError:
            return exc.code, {"_raw": text[:200]}
    except Exception as exc:  # noqa: BLE001
        return -1, {"_error": type(exc).__name__}


def _auto_unsigned_post(url: str, body: bytes) -> tuple[int, Any]:
    """POST *url* with no Authorization header (unsigned).  For U1's unsigned probe."""
    import ssl  # noqa: PLC0415
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(
        _NoRedirect(), urllib.request.HTTPSHandler(context=ctx))
    try:
        with opener.open(req, timeout=30) as resp:
            raw = resp.read()
            text = raw.decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(text) if text else {}
            except ValueError:
                return resp.status, {"_raw": text[:200]}
    except urllib.error.HTTPError as exc:
        raw = exc.read() or b""
        text = raw.decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(text) if text else {}
        except ValueError:
            return exc.code, {"_raw": text[:200]}
    except Exception as exc:  # noqa: BLE001
        return -1, {"_error": type(exc).__name__}


def check_p1_auto_config(
    lam: Any, ssm: Any, apigw: Any, cf: Any, ddb: Any,
    stage: str, t: dict, fn_name: str, results: list,
) -> bool:
    """P1 (read-only): verify the auto-approver's live configuration.

    Checks in order (all within one P1 row):
      1. GetFunctionConfiguration: FI_AUTO_MOVES_ENABLED=true, endpoint matches CMS CFN
         output, Timeout >= 300.
      2. ListEventSourceMappings: at least one mapping is Enabled.
      3. SSM bounds parameter == expected bounds.
      4. API Gateway resource policy has a Deny for non-approver callers.
      5. No enabled move_vehicles rule in the rules table.

    Returns True (all PASS) or False (at least one FAIL).
    """
    problems: list[str] = []

    # 1. Lambda function configuration
    try:
        config = lam.get_function_configuration(FunctionName=fn_name)
        env = (config.get("Environment") or {}).get("Variables") or {}
        if env.get("FI_AUTO_MOVES_ENABLED") != "true":
            problems.append(f"FI_AUTO_MOVES_ENABLED={env.get('FI_AUTO_MOVES_ENABLED')!r} (want 'true')")
        # Compare the Lambda's endpoint against CMS's CloudFormation output
        lam_endpoint = (env.get("VSA_CMS_FI_INTERNAL_API_ENDPOINT") or "").rstrip("/")
        try:
            stack_name = _AUTO_CMS_STACK_FMT.format(stage=stage)
            stacks = cf.describe_stacks(StackName=stack_name).get("Stacks") or []
            outputs = {o["OutputKey"]: o["OutputValue"]
                       for s in stacks for o in (s.get("Outputs") or [])}
            cf_endpoint = outputs.get(_AUTO_CMS_INTERNAL_OUTPUT_KEY, "").rstrip("/")
        except Exception as exc:  # noqa: BLE001
            cf_endpoint = None
            problems.append(f"could not read CMS stack {stack_name!r}: {type(exc).__name__}")
        if cf_endpoint is not None and lam_endpoint != cf_endpoint:
            problems.append(
                f"endpoint mismatch: Lambda has {lam_endpoint!r}, "
                f"CMS {_AUTO_CMS_INTERNAL_OUTPUT_KEY} is {cf_endpoint!r}"
            )
        # W7(d): endpoint must be non-empty (empty string matches empty Lambda env, hiding
        # both misconfigured).
        if cf_endpoint is not None and not cf_endpoint:
            problems.append(
                f"CMS {_AUTO_CMS_INTERNAL_OUTPUT_KEY} is empty "
                f"(Lambda env VSA_CMS_FI_INTERNAL_API_ENDPOINT not set or CFN output missing)")
        timeout = config.get("Timeout", 0)
        if int(timeout) < 300:
            problems.append(f"Timeout={timeout} (want >= 300)")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"GetFunctionConfiguration failed: {type(exc).__name__}: {exc}"[:200])

    # 2. Event source mapping
    try:
        mappings = lam.list_event_source_mappings(FunctionName=fn_name).get("EventSourceMappings") or []
        enabled = [m for m in mappings if m.get("State") == "Enabled"]
        if not enabled:
            problems.append(f"no Enabled event source mapping ({len(mappings)} mapping(s))")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"ListEventSourceMappings failed: {type(exc).__name__}"[:100])

    # 3. Bounds SSM parameter
    try:
        bounds_param = _AUTO_BOUNDS_PARAM_FMT.format(stage=stage)
        # W7: also verify the Lambda's AVX_AUTO_APPROVAL_BOUNDS_PARAM env matches.
        lam_bounds_param = env.get("AVX_AUTO_APPROVAL_BOUNDS_PARAM") if "env" in dir() else None
        if "env" in dir():
            if lam_bounds_param is None:
                problems.append("Lambda AVX_AUTO_APPROVAL_BOUNDS_PARAM not set")
            elif lam_bounds_param != bounds_param:
                problems.append(
                    f"Lambda AVX_AUTO_APPROVAL_BOUNDS_PARAM={lam_bounds_param!r} "
                    f"!= expected {bounds_param!r}")
        raw = ssm.get_parameter(Name=bounds_param)["Parameter"]["Value"]
        bounds = json.loads(raw)
        if bounds != _AUTO_EXPECTED_BOUNDS:
            problems.append(
                f"bounds param {bounds_param!r} = {bounds!r}, want {_AUTO_EXPECTED_BOUNDS!r}"
            )
    except Exception as exc:  # noqa: BLE001
        problems.append(f"bounds param check failed: {type(exc).__name__}: {exc}"[:200])

    # 4. API Gateway resource policy
    try:
        import re as _re  # noqa: PLC0415
        import urllib.parse as _up  # noqa: PLC0415
        # W7(e): derive restApiId from the CFN output endpoint first (not the env var), then
        # try the Lambda's own endpoint URL.  The env var is a last-resort so the world's tests
        # can run when neither URL is execute-api shaped.
        _ep_for_id = cf_endpoint or (lam_endpoint if "lam_endpoint" in dir() else "")
        _m = _re.search(r"https://([a-z0-9]{1,32})\.execute-api\.", _ep_for_id)
        if not _m and "lam_endpoint" in dir():
            _m = _re.search(r"https://([a-z0-9]{1,32})\.execute-api\.", lam_endpoint)
        api_id = _m.group(1) if _m else ""
        if not api_id:
            # W11: fall back to DescribeStackResource when endpoint is not execute-api shaped.
            try:
                _stack_for_api = _AUTO_CMS_STACK_FMT.format(stage=stage)
                api_id = cf.describe_stack_resource(
                    StackName=_stack_for_api,
                    LogicalResourceId="FiInternalApi",
                )["StackResourceDetail"]["PhysicalResourceId"]
            except Exception:  # noqa: BLE001
                api_id = ""
        if not api_id:
            problems.append(
                "cannot derive CMS API id from endpoint URLs "
                "(no execute-api pattern in cf_endpoint or lam_endpoint)")
        else:
            api = apigw.get_rest_api(restApiId=api_id)
            # W11: live API returns policy as backslash-escaped JSON, not URL-encoded.
            policy_raw = api.get("policy") or ""
            try:
                policy = json.loads(json.loads('"' + policy_raw + '"'))
            except (json.JSONDecodeError, ValueError):
                try:
                    policy = json.loads(_up.unquote(policy_raw))
                except json.JSONDecodeError:
                    problems.append("cannot parse internal API resource policy")
                    policy = {}
            stmts = policy.get("Statement") or []
            # W7(a): require exactly one Deny statement naming only the auto-approver role ARN.
            deny_stmts = [s for s in stmts if s.get("Effect") == "Deny"]
            allow_stmts = [s for s in stmts if s.get("Effect") == "Allow"]
            if allow_stmts:
                problems.append(
                    f"internal API policy has Allow statement(s) (want none): "
                    f"{json.dumps(allow_stmts[0])[:200]}")
            if not deny_stmts:
                problems.append("internal API resource policy has no Deny statement")
            elif len(deny_stmts) > 1:
                problems.append(
                    f"internal API policy has {len(deny_stmts)} Deny statements "
                    f"(want exactly 1)")
            else:
                deny = deny_stmts[0]
                # W11: derive account from FunctionArn for exact Resource/Condition ARN checks.
                _fn_arn = config.get("FunctionArn", "") if "config" in dir() else ""
                _m_acct = _re.search(r"arn:aws:lambda:[^:]+:(\d+):", _fn_arn)
                acct = _m_acct.group(1) if _m_acct else ""
                # W11: Principal must be {"AWS": "*"} (live format, not plain "*").
                if deny.get("Principal") != {"AWS": "*"}:
                    problems.append(
                        f"Deny Principal={deny.get('Principal')!r} (want {{\"AWS\": \"*\"}})")
                # Check Action == "execute-api:Invoke"
                if deny.get("Action") != "execute-api:Invoke":
                    problems.append(
                        f"Deny Action={deny.get('Action')!r} "
                        f"(want 'execute-api:Invoke')")
                # W11: Resource must exactly match the expected ARN for this API.
                res = deny.get("Resource") or ""
                expected_resource = (
                    f"arn:aws:execute-api:{CVX_REGION}:{acct}:{api_id}/*"
                    if acct and api_id else ""
                )
                if expected_resource and res != expected_resource:
                    problems.append(
                        f"Deny Resource {res!r}, expected {expected_resource!r}")
                elif not expected_resource and not (isinstance(res, str) and api_id in res):
                    problems.append(
                        f"Deny Resource {res!r} does not reference api_id {api_id!r}")
                # W11: Condition must be exactly StringNotEquals on aws:PrincipalArn == expected ARN.
                cond = deny.get("Condition") or {}
                expected_auto_approver_arn = (
                    f"arn:aws:iam::{acct}:role/vsa-{stage}-avx-auto-approver-{CVX_REGION}"
                )
                if not cond or len(cond) != 1:
                    problems.append(
                        f"Deny Condition must have exactly one operator: {json.dumps(cond)[:200]}")
                else:
                    op = next(iter(cond.keys()))
                    if op != "StringNotEquals":
                        problems.append(
                            f"Deny Condition operator must be StringNotEquals, got {op!r}")
                    else:
                        inner = cond[op]
                        if not isinstance(inner, dict) or list(inner.keys()) != ["aws:PrincipalArn"]:
                            problems.append(
                                f"Deny Condition key must be aws:PrincipalArn: "
                                f"{json.dumps(inner)[:200]}")
                        else:
                            actual_arn = inner.get("aws:PrincipalArn")
                            if actual_arn != expected_auto_approver_arn:
                                problems.append(
                                    f"Deny Condition role ARN {actual_arn!r} "
                                    f"!= expected {expected_auto_approver_arn!r}")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"API Gateway policy check failed: {type(exc).__name__}: {exc}"[:200])

    # 5. No enabled move_vehicles rules
    try:
        all_rules = _ddb_scan_all(ddb, t["rules"], ConsistentRead=True)
        enabled_mv = [r for r in all_rules
                      if r.get("enabled") is True and r.get("action_kind") == "move_vehicles"]
        if enabled_mv:
            ids = [f"{r.get('fleet_id')}/{r.get('rule_id')}" for r in enabled_mv[:3]]
            problems.append(f"enabled move_vehicles rule(s) already exist: {ids}")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"rules table scan failed: {type(exc).__name__}: {exc}"[:200])

    if problems:
        record(results, "P1 config", "FAIL", "; ".join(problems))
        return False
    record(results, "P1 config", "PASS", "config, bounds, policy and rules all look correct")
    return True


def check_p2_auto_candidates(
    ddb: Any, t: dict, limit: float, results: list, *,
    s3: Any, results_bucket: str, now: datetime.datetime,
) -> tuple[dict | None, dict | None, dict | None]:
    """P2 (read-only): pick two undecided move Findings (A for execution, B for probe).

    Both must be in the rental fleet, have ≤5 VINs, cost ≤ limit, pass C0 car checks,
    and be live (expires_at > now + 2 h).  B must also have a probe VIN that passes
    _recall_clear.  Returns (finding_a, finding_b, probe_dict) on success, or
    (None, None, None) on SKIP.  probe_dict has keys {vin, vehicle_id, origin}.
    """
    # Read all move findings from the FINDINGS table.
    all_findings = _ddb_scan_all(ddb, t["findings"])
    move_findings = [
        f for f in all_findings
        if (f.get("finding_kind") == "fleet.rebalance.move"
            and f.get("fleet_id") == _RENTAL_FLEET          # S3: handler.py:327-328
            and (f.get("scope") or {}).get("fleet_id") == _RENTAL_FLEET
            and ((f.get("recommended_action") or {}).get("parameters") or {}).get("fleet_id")
                == _RENTAL_FLEET                             # S3: handler.py:125 (_move_within_bounds)
            and (f.get("recommended_action") or {}).get("target_system") == "cms"
            # S3: handler.py:356-358; target_system for move_vehicles is "cms" (policy.py:116)
            and _FINDING_ID_RE.match(str(f.get("finding_id", ""))))
    ]

    # Filter: undecided (no Action), live (expires_at > now + 2 h), ≤5 VINs, cost ≤ limit,
    # _LOC_RE / _VIN_RE format checks (as C0).
    undecided: list[dict] = []
    for f in sorted(move_findings, key=lambda x: x.get("finding_id", "")):
        fid = f["finding_id"]
        action_id = _decision_id(fid)
        if "Item" in ddb.get_item(TableName=t["actions"], Key={"action_id": {"S": action_id}},
                                  ConsistentRead=True):
            continue  # already decided
        params = (f.get("recommended_action") or {}).get("parameters") or {}
        vins = params.get("vehicle_vins") or []
        cost = params.get("transfer_cost_usd")
        if len(vins) == 0 or len(vins) > 5:
            continue
        try:
            cost_f = float(cost)
        except (TypeError, ValueError):
            continue
        if cost_f > limit:
            continue
        if cost_f <= 1.0:
            continue  # S3: limit_u2 = round(a_cost − 1, 2) ≤ 0 → rules API 400 invalid_limit
        exp = _parse_ts(f.get("expires_at"))
        if exp is None or exp <= now + _FINDING_MIN_LIFE:
            continue
        loc = f.get("location") or {}
        fr_loc = loc.get("from_location_id", "")
        to_loc = loc.get("to_location_id", "")
        if not (_LOC_RE.match(str(fr_loc)) and _LOC_RE.match(str(to_loc))):
            continue
        if not all(_VIN_RE.match(str(v)) for v in vins):
            continue
        undecided.append(f)

    if len(undecided) < 2:
        record(results, "P2 candidates", "SKIP",
               f"need 2 undecided move Findings with ≤5 VINs and cost ≤ ${limit:.2f}; "
               f"found {len(undecided)} ({len(move_findings)} total move Findings). "
               f"Retry after the next 06:00Z run.")
        return None, None, None

    # C0 car checks: at origin, not grounded, not on live roster, no iOS holder, room at dest.
    # C3: exactly one registry row per VIN (as C0's by_vin: dict[str, list[dict]]).
    at_loc: dict[str, int] = {}
    by_vin: dict[str, list[dict]] = {}
    for v in _ddb_scan_all(ddb, t["vehicles"],
                           ProjectionExpression="vehicleId, vin, fleetId, locationId"):
        if v.get("fleetId") == _RENTAL_FLEET:
            at_loc[v.get("locationId")] = at_loc.get(v.get("locationId"), 0) + 1
        by_vin.setdefault(v.get("vin"), []).append(v)

    roster = _ddb_query_all(ddb, t["live_plan"], KeyConditionExpression="planDate = :r",
                            ExpressionAttributeValues={":r": {"S": "roster"}})
    roster_ids = {r.get("vin") for r in roster} | {r.get("vehicleId") for r in roster}
    ios = {d.get("assignedVehicleId") for d in _ddb_scan_all(
        ddb, t["drivers"], FilterExpression="attribute_exists(cognitoEmail)",
        ProjectionExpression="assignedVehicleId")}

    def why_not(vin: str, origin: str) -> str | None:
        rows_v = by_vin.get(vin) or []
        if len(rows_v) != 1:
            return f"{len(rows_v)} registry rows"
        r = rows_v[0]
        if r.get("fleetId") != _RENTAL_FLEET or r.get("locationId") != origin:
            return f"at {r.get('locationId')} in {r.get('fleetId')}"
        vid = r["vehicleId"]
        if vin in roster_ids or vid in roster_ids:
            return "on the live roster"
        if vid in ios:
            return "held by an iOS test user"
        if _auto_open_groundings(ddb, t, vid):
            return "grounded"
        return None

    def room(to: str, n_vins: int) -> bool:
        item = ddb.get_item(TableName=t["locations"],
                            Key={"fleetId": {"S": _RENTAL_FLEET}, "locationId": {"S": to}},
                            ConsistentRead=True).get("Item")
        capacity = int(_ddb_deser(item).get("parkingSpaces", 0)) if item else 0
        return capacity - at_loc.get(to, 0) >= n_vins

    # Phase 1: find A — the first undecided finding where ALL VINs pass C0 and there's room.
    # Phase 2: find B — the first OTHER finding where ALL VINs pass C0, there's room, and
    #   at least one probe VIN (not in A's VINs) also passes _recall_clear.
    fa: dict | None = None
    fb: dict | None = None
    probe_dict: dict | None = None
    skip_reasons_a: list[str] = []
    skip_reasons_b: list[str] = []

    for f in undecided:
        fid = f["finding_id"]
        params = (f.get("recommended_action") or {}).get("parameters") or {}
        vins = params.get("vehicle_vins") or []
        loc = f.get("location") or {}
        fr = loc.get("from_location_id", "")
        to = loc.get("to_location_id", "")
        if fa is None:
            # Candidate for A: all VINs must pass C0 (U5 moves all of them).
            bad = {v: why_not(v, fr) for v in vins}
            bad = {v: w for v, w in bad.items() if w}
            if bad:
                skip_reasons_a.append(f"{fid}: {next(iter(bad.values()))}")
            elif not room(to, len(vins)):
                skip_reasons_a.append(f"{fid}: no room at {to}")
            else:
                fa = f
            continue  # Always move to the next finding after the A-check pass.
        # fa is found — B must have a probe VIN that passes both C0 (why_not) and _recall_clear.
        if f["finding_id"] == fa["finding_id"]:
            continue
        a_vins_set = set(
            (fa.get("recommended_action") or {}).get("parameters", {}).get("vehicle_vins") or []
        )
        # C1: every car of B must pass the full C0 check (why_not, same as A) so the probe
        # grounding is the ONLY CMS re-check that can fail in U4.  Using why_not for all VINs
        # also covers the live-roster and iOS-test-user hold checks on non-probe cars, so CMS
        # won't refuse B's move for any reason other than the grounding.
        b_ok = True
        for v_b in vins:
            reason = why_not(v_b, fr)
            if reason:
                skip_reasons_b.append(f"{fid}: {reason} for {v_b}")
                b_ok = False
                break
        if not b_ok:
            continue
        if not room(to, len(vins)):
            skip_reasons_b.append(f"{fid}: no room at {to} for B's cars")
            continue
        # Probe VIN: first sorted VIN not in A that passes why_not AND _recall_clear (C3/W5).
        probe_vin = None
        for v in sorted(vins):
            if v not in a_vins_set and not why_not(v, fr) and not _recall_clear(
                    s3, results_bucket, v, now):
                probe_vin = v
                break
        if probe_vin:
            fb = f
            probe_dict = {"vin": probe_vin,
                          "vehicle_id": by_vin[probe_vin][0]["vehicleId"],
                          "origin": fr}
            break
        else:
            skip_reasons_b.append(f"{fid}: no probe VIN passes C0 and recall check")

    if fa is None:
        record(results, "P2 candidates", "SKIP",
               f"no C0-eligible move finding (A): {'; '.join(skip_reasons_a[:3])} "
               f"Retry after the next 06:00Z run.")
        return None, None, None
    if fb is None:
        # Scan all remaining undecided for B (in case the loop ended without finding B)
        a_vins_set = set(
            (fa.get("recommended_action") or {}).get("parameters", {}).get("vehicle_vins") or []
        )
        for f in undecided:
            if f["finding_id"] == fa["finding_id"]:
                continue
            params = (f.get("recommended_action") or {}).get("parameters") or {}
            vins = params.get("vehicle_vins") or []
            loc = f.get("location") or {}
            fr = loc.get("from_location_id", "")
            to = loc.get("to_location_id", "")
            # Probe VIN in fallback: same as main loop — C1 full why_not for all B VINs,
            # room at dest, then why_not + recall for the probe (C3/W5).
            b_ok_fb = True
            for v_b in vins:
                if why_not(v_b, fr):
                    b_ok_fb = False
                    break
            if not b_ok_fb or not room(to, len(vins)):
                fid2 = f["finding_id"]
                if fid2 not in {r.split(":")[0] for r in skip_reasons_b}:
                    skip_reasons_b.append(f"{fid2}: fails C1 at-origin/room check")
                continue
            probe_vin = None
            for v in sorted(vins):
                if v not in a_vins_set and not why_not(v, fr) and not _recall_clear(
                        s3, results_bucket, v, now):
                    probe_vin = v
                    break
            if probe_vin:
                fb = f
                probe_dict = {"vin": probe_vin,
                              "vehicle_id": by_vin[probe_vin][0]["vehicleId"],
                              "origin": fr}
                break
            else:
                fid2 = f["finding_id"]
                if fid2 not in {r.split(":")[0] for r in skip_reasons_b}:
                    skip_reasons_b.append(f"{fid2}: no probe VIN passes C0 and recall check")
    if fb is None:
        record(results, "P2 candidates", "SKIP",
               f"found A={fa['finding_id']} but no probe finding (B) with a C0-eligible VIN. "
               f"{'; '.join(skip_reasons_b[:3])} Retry after the next 06:00Z run.")
        return None, None, None

    record(results, "P2 candidates", "PASS",
           f"A={fa['finding_id']} B={fb['finding_id']}")
    return fa, fb, probe_dict


def check_u1_403_probes(
    ddb: Any, t: dict, endpoint_url: str, results: list,
    stage: str = "",
) -> bool:
    """U1: unsigned and caller-signed POSTs to the internal route both get 403.

    The body names a made-up Action id so even a fail-open writes nothing.
    Compares the moves table before and after to verify no write slipped through.

    Requires the specific AWS 403 bodies (W8):
    - Unsigned probe: ``{"message": "Missing Authentication Token"}``.
    - Caller-signed probe: explicit-deny body whose resource ARN ends with
      ``/POST<_AUTO_INTERNAL_MOVES_PATH>``.

    Records the caller ARN from sts:GetCallerIdentity in the PASS detail and
    FAILs if the caller is the auto-approver role (probe must come from a caller
    the Deny actually fires on).

    Returns True (PASS) or False (FAIL).
    """
    probe_url = endpoint_url.rstrip("/") + _AUTO_INTERNAL_MOVES_PATH
    fake_body = json.dumps({
        "actionId": "AVX-ACT-00000000-0000-0000-0000-000000000000",
        "findingId": "livetest-auto-probe-findingid",
    }, separators=(",", ":")).encode()

    # Caller identity — for PASS detail and auto-approver guard.
    caller_arn = ""
    try:
        caller_arn = _client("sts", CVX_REGION).get_caller_identity()["Arn"]
    except Exception as exc:  # noqa: BLE001
        caller_arn = f"(sts error: {type(exc).__name__})"

    moves_before = len(_ddb_scan_all(ddb, t["moves"]))
    problems: list[str] = []

    # C8: the deny ARN's stage comes from the endpoint URL, not args.stage.
    # CMS's internal API is always deployed on stage "prod" regardless of the VSA stage.
    import urllib.parse as _up_u1  # noqa: PLC0415
    _ep_path = _up_u1.urlsplit(endpoint_url).path.strip("/")
    _api_stage = _ep_path.split("/")[0] if _ep_path else "prod"
    _m_ep = re.search(r"https://([a-z0-9]{1,32})\.execute-api\.", endpoint_url)
    _api_id_ep = _m_ep.group(1) if _m_ep else ""

    # Guard: if we ARE the auto-approver, the Deny won't fire on the signed probe.
    approver_fragment = f"avx-auto-approver-" if not stage else f"vsa-{stage}-avx-auto-approver-"
    if approver_fragment in caller_arn:
        problems.append(
            f"caller ARN is the auto-approver role ({caller_arn!r}); "
            "the Deny would not fire — use non-approver credentials"
        )

    # Unsigned POST — must get "Missing Authentication Token"
    try:
        code, body_u = _auto_unsigned_post(probe_url, fake_body)
        if code != 403:
            problems.append(f"unsigned POST answered {code} (want 403)")
        else:
            msg_u = (body_u.get("message") or body_u.get("Message") or "") if isinstance(body_u, dict) else ""
            if "missing authentication token" not in msg_u.lower():
                problems.append(
                    ("unsigned POST 403 body lacks Missing Authentication Token: "
                     + repr(body_u))[:200]
                )
    except Exception as exc:  # noqa: BLE001
        problems.append(f"unsigned POST raised {type(exc).__name__}"[:100])

    # Caller-signed POST — must get explicit deny naming the correct resource ARN.
    try:
        code, body_s = _auto_sigv4_post(probe_url, fake_body)
        if code != 403:
            problems.append(f"caller-signed POST answered {code} (want 403)")
        else:
            import re as _re  # noqa: PLC0415
            deny_msg = (body_s.get("Message") or body_s.get("message") or "") if isinstance(body_s, dict) else ""
            if "is not authorized to perform: execute-api:Invoke" not in deny_msg:
                problems.append(
                    ("caller-signed POST 403 body lacks explicit deny: "
                     + repr(body_s))[:200]
                )
            else:
                if "with an explicit deny" not in deny_msg:
                    problems.append(
                        "caller-signed POST deny body lacks 'with an explicit deny': "
                        + repr(deny_msg)[:150]
                    )
                _m_arn = _re.search(r"on resource: (\S+)", deny_msg)
                if not _m_arn:
                    problems.append(
                        "caller-signed POST deny body has no 'on resource: ...' field: "
                        + repr(deny_msg)[:150]
                    )
                else:
                    # C8: check api_id (from execute-api endpoint) + api_stage (from endpoint
                    # path) to pin the deny ARN to the exact API and stage CMS deploys.
                    _arn_tail = (
                        f":{_api_id_ep}/{_api_stage}/POST{_AUTO_INTERNAL_MOVES_PATH}"
                        if _api_id_ep
                        else f"/{_api_stage}/POST{_AUTO_INTERNAL_MOVES_PATH}"
                    )
                    if not _m_arn.group(1).endswith(_arn_tail):
                        problems.append(
                            f"explicit-deny resource ARN wrong path: got {_m_arn.group(1)!r}, "
                            f"expected suffix {_arn_tail!r}"
                        )
    except Exception as exc:  # noqa: BLE001
        problems.append(f"caller-signed POST raised {type(exc).__name__}"[:100])

    moves_after = len(_ddb_scan_all(ddb, t["moves"]))
    if moves_before != moves_after:
        problems.append(f"moves table changed: {moves_before} -> {moves_after} rows")

    if problems:
        record(results, "U1 403 probes", "FAIL", "; ".join(problems))
        return False
    record(results, "U1 403 probes", "PASS",
           f"unsigned and caller-signed POSTs both got 403; moves table unchanged; caller={caller_arn}")
    return True




def _auto_cleanup_only(args: argparse.Namespace, manifest: dict) -> int:
    """X1-auto: disable the rule and return the probe car from an earlier --part auto run.

    Exempt from the write window (a grounded car is worse than violating the window).
    Reuses the manifest saved by the normal run before U2.
    """
    _init_session(allow_writes=bool(args.allow_writes), stage=args.stage, workgroup=args.workgroup,
                  fn_name="", cms_only=True)
    cms_base = _https_base(args.cms_api_endpoint) or ""
    avx_base = _https_base(args.avx_api_endpoint) or ""
    t = _auto_tables(args.stage)
    ddb = _client("dynamodb", CVX_REGION)
    idp = _client("cognito-idp", CVX_REGION)

    results: list[tuple[str, str, str]] = []
    cleanup: list[str] = []
    problems: list[str] = []

    rule_id = manifest.get("rule_id") or ""
    probe_gid = manifest.get("probe_gid")
    probe_vid = manifest.get("probe_vid")
    rule_url = f"{avx_base}/approval-rules/fleet/{_RENTAL_FLEET}/{rule_id}"

    try:
        pool_id = _find_cms_pool(idp, args.stage)
        client_id = _password_client(idp, pool_id)
        with _temp_operator(idp, pool_id, client_id, _RENTAL_FLEET, cleanup) as op_token:
            # C7: one _signals_held() around both disable and return-to-service so a
            # signal during either step is deferred until both finish.
            with _signals_held():
                # W10: read the rule first; if absent there is nothing to disable.
                _rule_exists = False
                try:
                    _rule_ddb = _ddb_deser(
                        ddb.get_item(TableName=t["rules"],
                                     Key={"fleet_id": {"S": _RENTAL_FLEET},
                                          "rule_id": {"S": rule_id}},
                                     ConsistentRead=True).get("Item") or {})
                    _rule_exists = bool(_rule_ddb)
                except Exception:  # noqa: BLE001
                    _rule_exists = True   # assume it exists; disable will clarify

                # Disable the rule (no limit, signals held, retry with read-back).
                _disabled = not _rule_exists  # skip if already absent
                if _rule_exists:
                    for _wait in (0.0, 2.0, 5.0):
                        if _wait:
                            time.sleep(_wait)
                        try:
                            code_d, _resp_d, _ = _put_json(
                                rule_url, op_token,
                                {"action_kind": "move_vehicles", "enabled": False})
                            if code_d == 200:
                                rule_now = _ddb_deser(
                                    ddb.get_item(TableName=t["rules"],
                                                 Key={"fleet_id": {"S": _RENTAL_FLEET},
                                                      "rule_id": {"S": rule_id}},
                                                 ConsistentRead=True).get("Item") or {})
                                if not rule_now.get("enabled"):
                                    _disabled = True
                                    # S2: delete the disabled livetest rule via the
                                    # approval-rules API so it does not accumulate.
                                    try:
                                        _request("DELETE", rule_url, op_token)
                                    except Exception:  # noqa: BLE001
                                        pass
                                    break
                        except Exception:  # noqa: BLE001
                            pass
                if not _disabled:
                    problems.append(f"rule {rule_id!r} still ENABLED after disable PUT")
                    # W17: pointer so the operator knows the rule is still active.
                    _auto_cleanup_pointer(
                        args.run_id, args.stage, cms_base, avx_base, args.workgroup,
                        f"rule {rule_id!r} is still ENABLED; "
                        f"the next 06:00Z run would be auto-approved")

                # Return the probe car to service (inside the same _signals_held so a
                # signal during the disable cannot skip the return).
                # W10: use probe_posted from manifest; if no POST was made, written=False.
                if probe_gid and probe_vid:
                    _probe_written = None if manifest.get("probe_posted") else False
                    ok_rts, detail_rts = _auto_return_to_service(
                        ddb, t, cms_base, op_token, probe_vid, probe_gid, _probe_written)
                    if not ok_rts:
                        problems.append(f"return to service: {detail_rts}")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"cleanup error: {type(exc).__name__}: {exc}"[:200])

    # Record cleanup rows AFTER ExitStack (and _temp_operator's finally) have run.
    for cmd in cleanup:
        problems.append(f"temp user not removed; run: {cmd}")
        record(results, "cleanup", "FAIL", f"temp user not removed; run: {cmd}")

    record(results, "X1-auto",
           "FAIL" if problems else "PASS",
           "; ".join(problems) if problems else
           # S5 bullet 3: say only what happened.
           (("rule disabled" if _rule_exists else "no rule created")
            + ", "
            + ("probe car returned"
               if (probe_gid and probe_vid and manifest.get("probe_posted"))
               else "nothing grounded")
            + ", temp operator deleted"))
    return _summary(results)


def _part_auto(args: argparse.Namespace) -> int:
    """--part auto: P1-P2 (read-only) + U1-U6 (writes with --allow-writes); task 8.4.

    Invokes the live auto-approver Lambda with the same input its DynamoDB Streams
    trigger sends (one MODIFY record, NewImage = the chosen Finding's current item).
    Everything after the trigger is the live path: the real rule, the evaluator, the
    Action write, the SigV4 dispatch to CMS, and CMS's re-checks.
    """
    # C4(d): --cleanup-only is exempt from the write window (leaving a car grounded
    # is worse than running outside the normal window guard).
    if getattr(args, "cleanup_only", False):
        if not getattr(args, "run_id", None) or not _RUN_ID_RE.match(args.run_id):
            print("REFUSED: --part auto --cleanup-only needs a valid --run-id", file=sys.stderr)
            return 2
        if not args.allow_writes:
            print("REFUSED: --part auto --cleanup-only requires --allow-writes", file=sys.stderr)
            return 2
        if not args.cms_api_endpoint:
            print("REFUSED: --part auto needs --cms-api-endpoint", file=sys.stderr)
            return 2
        if not args.avx_api_endpoint:
            print("REFUSED: --part auto needs --avx-api-endpoint", file=sys.stderr)
            return 2
        try:
            manifest = _load_manifest(args.run_id)
        except (OSError, ValueError) as exc:
            print(f"REFUSED: no readable manifest for {args.run_id} ({type(exc).__name__})",
                  file=sys.stderr)
            return 2
        if manifest.get("stage") != args.stage or manifest.get("part") != "auto":
            print("REFUSED: the run's manifest is for another stage or part", file=sys.stderr)
            return 2
        # W10: validate manifest shapes before any AWS call to catch hand-crafted manifests
        # or manifests from a different run that used a different auto-rule or probe prefix.
        m_rule_id = manifest.get("rule_id", "")
        m_probe_gid = manifest.get("probe_gid", "")
        # W12: the expected per-run rule_id is built from the manifest's own run_id.
        _m_run_id = manifest.get("run_id", "")
        _expected_rule_id = f"{_AUTO_RULE_ID_PREFIX}-{_m_run_id}"
        if m_rule_id != _expected_rule_id:
            print(f"REFUSED: manifest rule_id {m_rule_id!r} != {_expected_rule_id!r}",
                  file=sys.stderr)
            return 2
        if m_probe_gid and not m_probe_gid.startswith(_AUTO_PROBE_PREFIX):
            print(f"REFUSED: manifest probe_gid {m_probe_gid!r} does not start with "
                  f"{_AUTO_PROBE_PREFIX!r}", file=sys.stderr)
            return 2
        return _auto_cleanup_only(args, manifest)

    # Write window is checked at startup; refusing early avoids AWS calls on a hot path.
    if not _cms_writes_allowed_now():
        print("REFUSED: inside the 03:45-07:15 UTC write window — "
              "the auto-approver check does not run inside the write window",
              file=sys.stderr)
        return 2
    if not args.cms_api_endpoint:
        print("REFUSED: --part auto needs --cms-api-endpoint", file=sys.stderr)
        return 2
    if not args.avx_api_endpoint:
        print("REFUSED: --part auto needs --avx-api-endpoint", file=sys.stderr)
        return 2

    fn_name = _AUTO_APPROVER_FN_FMT.format(stage=args.stage, region=CVX_REGION)
    _init_session(allow_writes=bool(args.allow_writes), stage=args.stage,
                  workgroup=args.workgroup, fn_name=fn_name, auto_only=True)

    cms_base = _https_base(args.cms_api_endpoint) or ""
    avx_base = _https_base(args.avx_api_endpoint) or ""
    # W3: validate --limit before any AWS call (non-cleanup path only).
    # --cleanup-only is exempt; it uses no limit.
    # P1 reads max_limit_usd from SSM; pre-check against the locally-known constant.
    _max_limit = float(_AUTO_EXPECTED_BOUNDS["max_limit_usd"])
    _raw_limit = args.limit
    if _raw_limit is None or not math.isfinite(_raw_limit) or _raw_limit <= 0 or _raw_limit > _max_limit:
        _disp = f"{_raw_limit:g}" if _raw_limit is not None and math.isfinite(_raw_limit) else str(_raw_limit)
        print(
            f"REFUSED: --limit {_disp} — must be a positive number "
            f"in (0, {_max_limit:g}]; pass --limit <value>",
            file=sys.stderr,
        )
        return 2
    limit = float(_raw_limit)
    t = _auto_tables(args.stage)

    lam = _client("lambda", CVX_REGION, config=_lambda_config())
    ssm = _client("ssm", CVX_REGION)
    apigw = _client("apigateway", CVX_REGION)
    cf = _client("cloudformation", CVX_REGION)
    ddb = _client("dynamodb", CVX_REGION)
    idp = _client("cognito-idp", CVX_REGION)
    # S3 + results bucket for _recall_clear in P2 (same derivation as --part cms).
    account = _client("sts", CVX_REGION).get_caller_identity()["Account"]
    results_bucket = (getattr(args, "cms_results_bucket", None)
                      or f"cms-{args.stage}-athena-results-{account}-{ADP_REGION}")
    s3 = _client("s3", ADP_REGION)

    results: list[tuple[str, str, str]] = []
    cleanup: list[str] = []

    # --- P1 (read-only) ---
    p1_ok = check_p1_auto_config(lam, ssm, apigw, cf, ddb, args.stage, t, fn_name, results)
    if not p1_ok:
        return _summary(results)

    # --- P2 (read-only): pick A and B ---
    p2_now = _wall()
    fa, fb, probe_auto = check_p2_auto_candidates(
        ddb, t, limit, results, s3=s3, results_bucket=results_bucket, now=p2_now)
    if fa is None:
        for name in ("U1 403 probes", "U2 rule", "U3 over limit", "U4 grounded", "U5 within limit"):
            record(results, name, "SKIP", "not run: P2 found no eligible candidate pair")
        return _summary(results)

    # Endpoint URL for U1 (from Lambda config, already fetched in P1)
    try:
        fn_env = lam.get_function_configuration(FunctionName=fn_name).get(
            "Environment", {}).get("Variables", {})
        endpoint_url = fn_env.get("VSA_CMS_FI_INTERNAL_API_ENDPOINT", "").rstrip("/")
    except Exception as exc:  # noqa: BLE001
        record(results, "U1 403 probes", "FAIL",
               f"could not read function config for endpoint: {type(exc).__name__}")
        return _summary(results)

    # --- U1 (403 probes, no DDB writes needed) ---
    u1_ok = check_u1_403_probes(ddb, t, endpoint_url, results, stage=args.stage)
    if not u1_ok:
        for name in ("U2 rule", "U3 over limit", "U4 grounded", "U5 within limit"):
            record(results, name, "SKIP", "not run: U1 failed")
        return _summary(results)

    # --- Write items U2-U6 ---
    if not args.allow_writes:
        for name in ("U2 rule", "U3 over limit", "U4 grounded", "U5 within limit"):
            record(results, name, "SKIP", "not run: --allow-writes not given")
        return _summary(results)

    # Use P2's validated probe (C3/W5: avoids recomputing without the _recall_clear check).
    a_params = (fa.get("recommended_action") or {}).get("parameters") or {}
    a_cost = float(a_params.get("transfer_cost_usd", 0))
    a_vins = list(a_params.get("vehicle_vins") or [])
    a_to = (fa.get("location") or {}).get("to_location_id", "")
    a_fr = (fa.get("location") or {}).get("from_location_id", "")
    probe_vin: str | None = probe_auto["vin"] if probe_auto else None
    probe_vid: str | None = probe_auto["vehicle_id"] if probe_auto else None
    # B's VINs and origin are still needed for U4's "B cars still at origin" check.
    b_params = (fb.get("recommended_action") or {}).get("parameters") or {} if fb else {}
    b_all_vins = list(b_params.get("vehicle_vins") or [])
    b_fr = (fb.get("location") or {}).get("from_location_id", "") if fb else ""
    b_cost = float(b_params.get("transfer_cost_usd", 0))

    probe_gid = _AUTO_PROBE_PREFIX + _new_run_id()
    state = {
        "rule_created": False,
        "probe_grounded": False,
        "probe_written": None,
        "write_failed": False,
    }

    # C4(d): save a manifest before U2 so --cleanup-only can recover a crashed run.
    _run_id = _new_run_id()
    # W12: per-run rule_id so the residue check needs no time filter.
    _auto_rule_id = f"{_AUTO_RULE_ID_PREFIX}-{_run_id}"
    rule_url = f"{avx_base}/approval-rules/fleet/{_RENTAL_FLEET}/{_auto_rule_id}"
    _manifest: dict = {
        "run_id": _run_id, "stage": args.stage, "part": "auto",
        "started_at": _iso(datetime.datetime.now(datetime.timezone.utc)),
        "rule_id": _auto_rule_id,
        "probe_gid": probe_gid,
        "probe_vid": probe_vid,
    }
    try:
        _save_manifest(_manifest)
    except Exception as exc:  # noqa: BLE001  — a lost manifest is non-fatal
        record(results, "U2 rule", "FAIL",
               f"could not save run manifest: {type(exc).__name__}: {exc}"[:200])
        return _summary(results)

    # C7: print the run id and recovery command before U2 so the operator can identify
    # this run from logs even when the process is killed during writes.
    print(
        f"run id {_run_id}; recover with: "
        f"FI_REBALANCING_LIVE_GO={args.stage} LIVE_TESTS=1 .venv/bin/python scripts/live-verify-fi-rebalancing.py "
        f"--part auto --stage {args.stage} --workgroup {args.workgroup} "
        f"--cms-api-endpoint {cms_base} --avx-api-endpoint {avx_base} "
        f"--allow-writes --cleanup-only --run-id {_run_id}",
        flush=True,
    )

    # W1 fix: u6_problems defined outside ExitStack so it's available after ExitStack exits.
    u6_problems: list[str] = []

    try:
        pool_id = _find_cms_pool(idp, args.stage)
        client_id = _password_client(idp, pool_id)
    except Exception as exc:  # noqa: BLE001
        record(results, "U2 rule", "FAIL",
               f"could not find Cognito pool/client: {type(exc).__name__}: {exc}"[:200])
        return _summary(results)

    with contextlib.ExitStack() as users:
        try:
            op_token = users.enter_context(
                _temp_operator(idp, pool_id, client_id, _RENTAL_FLEET, cleanup))
        except Exception as exc:  # noqa: BLE001
            record(results, "U2 rule", "FAIL",
                   f"temp operator creation failed: {type(exc).__name__}: {exc}"[:200])
            for cmd in cleanup:
                record(results, "cleanup", "FAIL", f"temp user not removed; run: {cmd}")
            record(results, "U6 always", "SKIP", "not run: temp operator creation failed")
            return _summary(results)

        # U2-U5 followed by U6 (inside the ExitStack so op_token is still valid)
        try:
            # W6: capture run start and fleet's existing move-record keys before any
            # write.  The U6 residue check uses these to detect extra Actions and moves
            # that the rule approved beyond A and B during the enabled window.
            run_start_iso = _iso(_wall())
            existing_move_ids: frozenset[str] = frozenset()
            try:
                existing_move_ids = frozenset(
                    r["moveId"] for r in _ddb_query_all(
                        ddb, t["moves"],
                        KeyConditionExpression="fleetId = :f",
                        ExpressionAttributeValues={":f": {"S": _RENTAL_FLEET}},
                        ConsistentRead=True,
                    )
                    if "moveId" in r
                )
            except Exception:  # noqa: BLE001
                pass  # conservative: residue move check will see all current moves as new
            # --- U2: create disabled rule, then enable ---
            if not state["write_failed"] and _cms_writes_allowed_now():
                try:
                    limit_u2 = round(a_cost - 1.0, 2)
                    # W10: save before the create PUT so cleanup-only knows a rule
                    # may have been created even if the response is lost.
                    _manifest["rule_created"] = True
                    _save_manifest(_manifest)
                    code, resp, _ = _put_json(rule_url, op_token,
                                             {"action_kind": "move_vehicles", "limit": limit_u2})
                    if code not in (200, 201):
                        record(results, "U2 rule", "FAIL",
                               f"create rule answered {code} {json.dumps(resp)[:160]}")
                        state["write_failed"] = True
                    else:
                        state["rule_created"] = True
                        # W10: save again before the enable PUT.
                        _manifest["rule_enabled"] = True
                        _save_manifest(_manifest)
                        code2, resp2, _ = _put_json(rule_url, op_token,
                                                    {"action_kind": "move_vehicles",
                                                     "limit": limit_u2, "enabled": True})
                        if code2 != 200:
                            record(results, "U2 rule", "FAIL",
                                   f"enable PUT answered {code2} {json.dumps(resp2)[:160]}")
                            state["write_failed"] = True
                        else:
                            rule = _ddb_deser(
                                ddb.get_item(TableName=t["rules"],
                                             Key={"fleet_id": {"S": _RENTAL_FLEET},
                                                  "rule_id": {"S": _auto_rule_id}},
                                             ConsistentRead=True).get("Item") or {})
                            if not rule.get("enabled"):
                                record(results, "U2 rule", "FAIL",
                                       f"rule not enabled after PUT: {rule!r}"[:200])
                                state["write_failed"] = True
                            else:
                                record(results, "U2 rule", "PASS",
                                       f"rule {_auto_rule_id} created (limit=${limit_u2:.2f}) "
                                       f"and enabled via {_RENTAL_FLEET} operator")
                except Exception as exc:  # noqa: BLE001
                    record(results, "U2 rule", "FAIL",
                           f"unexpected {type(exc).__name__}: {exc}"[:200])
                    state["write_failed"] = True
            else:
                record(results, "U2 rule", "SKIP",
                       "not run: write window or earlier failure")

            # --- U3: invoke with A (limit < a_cost → no Action), then raise limit ---
            if not state["write_failed"] and _cms_writes_allowed_now():
                try:
                    action_id_a = _decision_id(fa["finding_id"])
                    if "Item" in ddb.get_item(TableName=t["actions"],
                                              Key={"action_id": {"S": action_id_a}},
                                              ConsistentRead=True):
                        record(results, "U3 over limit", "FAIL",
                               f"Action for A already exists before U3: {action_id_a}")
                        state["write_failed"] = True
                    else:
                        u3_start = _iso(_wall().replace(microsecond=0))
                        resp_u3 = lam.invoke(
                            FunctionName=fn_name, InvocationType="RequestResponse",
                            Payload=json.dumps(
                                _auto_build_stream_event(ddb, t, fa)).encode())
                        u3_end = _iso(_wall().replace(microsecond=0))   # S4: upper bound of invoke window
                        # W2: check the invoke response before reading the Action.
                        fn_err_u3 = resp_u3.get("FunctionError")
                        try:
                            _pl_u3 = resp_u3.get("Payload")
                            payload_u3 = json.loads(
                                _pl_u3.read() if hasattr(_pl_u3, "read") else
                                (_pl_u3 or b"{}"))
                        except Exception:  # noqa: BLE001
                            payload_u3 = {}
                        bad_items_u3 = payload_u3.get("batchItemFailures") or []
                        if fn_err_u3:
                            record(results, "U3 over limit", "FAIL",
                                   f"invoke FunctionError={fn_err_u3!r}")
                            state["write_failed"] = True
                        elif bad_items_u3:
                            record(results, "U3 over limit", "FAIL",
                                   f"invoke batchItemFailures={json.dumps(bad_items_u3)[:120]}")
                            state["write_failed"] = True
                        else:
                            # W2: evaluator-ran evidence — the run record proves the handler
                            # actually ran (not just that the invoke returned success).
                            # S4: BETWEEN :s AND :e so exactly one record is required —
                            # a concurrent stream-triggered run in the same window fails.
                            # W16: expand the window by _U3_WINDOW_MARGIN_S on each side
                            # to tolerate operator / Lambda clock skew.
                            _u3_margin = datetime.timedelta(seconds=_U3_WINDOW_MARGIN_S)
                            _u3_q_start = _iso(
                                datetime.datetime.fromisoformat(
                                    u3_start.replace("Z", "+00:00")) - _u3_margin)
                            _u3_q_end = _iso(
                                datetime.datetime.fromisoformat(
                                    u3_end.replace("Z", "+00:00")) + _u3_margin)
                            ev_runs = _ddb_query_all(
                                ddb, t["runs"],
                                KeyConditionExpression=(
                                    "agent_component = :k AND started_at BETWEEN :s AND :e"),
                                ExpressionAttributeValues={
                                    ":k": {"S": "agent-fi-evaluator#evaluator"},
                                    ":s": {"S": _u3_q_start},
                                    ":e": {"S": _u3_q_end}},
                                ConsistentRead=True,
                            )
                            if len(ev_runs) == 0:
                                record(results, "U3 over limit", "FAIL",
                                       "no evaluator run record in invoke window — "
                                       "handler may not have run")
                                state["write_failed"] = True
                            elif len(ev_runs) > 1:
                                record(results, "U3 over limit", "FAIL",
                                       f"expected exactly 1 evaluator run record "
                                       f"in invoke window, got {len(ev_runs)} — "
                                       "a concurrent run (e.g. stream-triggered) "
                                       "landed in the same window")
                                state["write_failed"] = True
                            elif (ev_runs[0].get("artifacts_written") != 0
                                    or ev_runs[0].get("duplicates_skipped") != 0):
                                rr = ev_runs[0]
                                record(results, "U3 over limit", "FAIL",
                                       f"evaluator run record shows "
                                       f"artifacts_written={rr.get('artifacts_written')!r} "
                                       f"duplicates_skipped={rr.get('duplicates_skipped')!r} "
                                       "(want both 0)")
                                state["write_failed"] = True
                            elif "Item" in ddb.get_item(
                                    TableName=t["actions"],
                                    Key={"action_id": {"S": action_id_a}},
                                    ConsistentRead=True):
                                record(results, "U3 over limit", "FAIL",
                                       f"evaluator approved A despite limit < cost "
                                       f"(Action {action_id_a} was written)")
                                state["write_failed"] = True
                            else:
                                # W3: raise rule to max(a_cost, b_cost), not to --limit.
                                # Both are ≤ limit (P2 filters by cost ≤ limit), and
                                # limit ≤ max_limit_usd.  The window of enablement
                                # covers exactly the two tested findings.
                                limit_raise = round(max(a_cost, b_cost), 2)
                                code3, resp3, _ = _put_json(
                                    rule_url, op_token,
                                    {"action_kind": "move_vehicles",
                                     "limit": limit_raise, "enabled": True})
                                if code3 != 200:
                                    record(results, "U3 over limit", "FAIL",
                                           f"limit raise PUT answered {code3} "
                                           f"{json.dumps(resp3)[:100]}")
                                    state["write_failed"] = True
                                else:
                                    record(results, "U3 over limit", "PASS",
                                           f"evaluator wrote no Action for A (rule limit "
                                           f"${limit_u2:.2f} < cost "
                                           f"${a_cost:.2f}); "
                                           f"limit raised to ${limit_raise:.2f}")
                except Exception as exc:  # noqa: BLE001
                    record(results, "U3 over limit", "FAIL",
                           f"unexpected {type(exc).__name__}: {exc}"[:200])
                    state["write_failed"] = True
            else:
                record(results, "U3 over limit", "SKIP",
                       "not run: write window or earlier failure")

            # --- U4: ground probe car, invoke with B → CMS refuses (409) ---
            if not state["write_failed"] and _cms_writes_allowed_now() and probe_vid:
                probe_problems: list[str] = []
                probe_written: bool | None = None
                try:
                    if _auto_open_groundings(ddb, t, probe_vid):
                        probe_problems.append(f"{probe_vin} was already grounded")
                    else:
                        gbody = {"vin": probe_vin, "campaignId": _TEST_CAMPAIGN,
                                 "findingId": probe_gid, "actionId": probe_gid}
                        # W10: save before the POST so cleanup-only knows the POST was sent.
                        # S5: set probe_grounded AFTER the save so a failing save doesn't
                        # make U6 wait on a grounding that was never sent.
                        _manifest["probe_posted"] = True
                        _save_manifest(_manifest)
                        state["probe_grounded"] = True   # C2: set before the POST so U6
                        # always calls _auto_return_to_service, even after a lost 5xx response.
                        g_code, g_resp, _ = _post_json(
                            _auto_url(cms_base, "groundings"), op_token, gbody)
                        probe_written = (True if g_code in (200, 201)
                                         else (False if 400 <= g_code < 500 else None))
                        if g_code not in (200, 201):
                            probe_problems.append(
                                f"grounding answered {g_code} {json.dumps(g_resp)[:120]}")
                        else:
                            # S4: read the grounding row back (ConsistentRead) before
                            # invoking B.  A route that answers 2xx but writes nothing
                            # would otherwise dispatch B with its car free.
                            _probe_row = _auto_read_grounding(ddb, t, probe_vid, probe_gid)
                            if _probe_row is None:
                                probe_written = False  # no row in DDB; U6 cleans up correctly
                                probe_problems.append(
                                    f"grounding POST answered {g_code} but no row "
                                    f"in DDB (vehicleId={probe_vid!r} "
                                    f"actionId={probe_gid!r})")
                            else:
                                resp_u4 = lam.invoke(
                                    FunctionName=fn_name, InvocationType="RequestResponse",
                                    Payload=json.dumps(
                                        _auto_build_stream_event(ddb, t, fb)).encode())
                                # W2: check U4 invoke response.
                                fn_err_u4 = resp_u4.get("FunctionError")
                                try:
                                    _pl_u4 = resp_u4.get("Payload")
                                    payload_u4 = json.loads(
                                        _pl_u4.read() if hasattr(_pl_u4, "read") else
                                        (_pl_u4 or b"{}"))
                                except Exception:  # noqa: BLE001
                                    payload_u4 = {}
                                bad_items_u4 = payload_u4.get("batchItemFailures") or []
                                if fn_err_u4:
                                    probe_problems.append(
                                        f"invoke FunctionError={fn_err_u4!r}")
                                elif bad_items_u4:
                                    probe_problems.append(
                                        f"invoke batchItemFailures="
                                        f"{json.dumps(bad_items_u4)[:120]}")
                                else:
                                    action_id_b = _decision_id(fb["finding_id"])
                                    # C1: Read B's Action (ConsistentRead) and verify the evaluator
                                    # dispatched and CMS declined with a 409 grounding refusal.
                                    act_b = _ddb_deser(
                                        ddb.get_item(TableName=t["actions"],
                                                     Key={"action_id": {"S": action_id_b}},
                                                     ConsistentRead=True).get("Item") or {})
                                    if not act_b:
                                        probe_problems.append(
                                            f"no Action for B ({action_id_b}) after invoke — "
                                            "approver may have failed to dispatch")
                                    else:
                                        exp_decided = {"kind": "rule",
                                                       "id": f"{_RENTAL_FLEET}/{_auto_rule_id}"}
                                        # W12: don't reassign run_start_iso here; keep the
                                        # value captured at the start of the run.
                                        if act_b.get("decision") != "auto_approved":
                                            probe_problems.append(
                                                f"B Action decision={act_b.get('decision')!r} "
                                                f"(want 'auto_approved')")
                                        if act_b.get("decided_by") != exp_decided:
                                            probe_problems.append(
                                                f"B Action decided_by={act_b.get('decided_by')!r} "
                                                f"(want {exp_decided!r})")
                                        if act_b.get("status") != "declined":
                                            probe_problems.append(
                                                f"B Action status={act_b.get('status')!r} "
                                                f"(want 'declined')")
                                        if (act_b.get("created_at") or "") < _iso(
                                                datetime.datetime.fromisoformat(
                                                    run_start_iso.replace("Z", "+00:00"))
                                                - datetime.timedelta(seconds=_U3_WINDOW_MARGIN_S)):
                                            probe_problems.append(
                                                f"B Action created_at="
                                                f"{act_b.get('created_at')!r} "
                                                f"predates run start {run_start_iso!r}")
                                        trail = act_b.get("audit_trail") or []
                                        if not trail:
                                            probe_problems.append(
                                                "B Action has empty audit_trail")
                                        else:
                                            last = trail[-1]
                                            if last.get("failure_kind") != "target_refused":
                                                probe_problems.append(
                                                    f"last audit entry failure_kind="
                                                    f"{last.get('failure_kind')!r} "
                                                    f"(want 'target_refused')")
                                            if last.get("error_status") != 409:
                                                probe_problems.append(
                                                    f"last audit entry error_status="
                                                    f"{last.get('error_status')!r} (want 409)")
                                    mv_b = _auto_move(ddb, t, action_id_b)
                                    if mv_b is not None:
                                        probe_problems.append(
                                            f"CMS did not refuse the move despite "
                                            f"{probe_vin} being grounded "
                                            f"(move record {action_id_b} exists)")
                                    for v in b_all_vins:
                                        try:
                                            v_vid = _auto_vehicle_id_of(ddb, t, v)
                                        except Exception as exc:  # noqa: BLE001
                                            probe_problems.append(
                                                f"vehicle_id lookup for {v} failed: {exc}"[:120])
                                            continue
                                        try:
                                            v_row = _auto_vehicle(ddb, t, v_vid)
                                            if v_row.get("locationId") != b_fr:
                                                probe_problems.append(
                                                    f"{v} moved to "
                                                    f"{v_row.get('locationId')!r} "
                                                    f"despite being grounded")
                                        except Exception as exc:  # noqa: BLE001
                                            probe_problems.append(
                                                f"vehicle row read for {v} ({v_vid}) "
                                                f"failed: {exc}"[:120])
                except Exception as exc:  # noqa: BLE001
                    probe_problems.append(f"unexpected {type(exc).__name__}: {exc}"[:150])
                state["probe_written"] = probe_written
                if probe_problems:
                    state["write_failed"] = True
                    record(results, "U4 grounded", "FAIL", "; ".join(probe_problems))
                else:
                    record(results, "U4 grounded", "PASS",
                           f"{probe_vin} grounded; auto-approver dispatched to CMS; "
                           f"CMS refused (409); no move record; B cars still at origin")
            elif not probe_vid:
                record(results, "U4 grounded", "SKIP",
                       "not run: no probe VIN available from P2")
            else:
                record(results, "U4 grounded", "SKIP",
                       "not run: write window or earlier failure")

            # --- U5: invoke with A (limit now == --limit > a_cost → should execute) ---
            if not state["write_failed"] and _cms_writes_allowed_now():
                try:
                    action_id_a = _decision_id(fa["finding_id"])
                    resp_u5 = lam.invoke(
                        FunctionName=fn_name, InvocationType="RequestResponse",
                        Payload=json.dumps(
                            _auto_build_stream_event(ddb, t, fa)).encode())
                    # W2: check U5 invoke response.
                    fn_err_u5 = resp_u5.get("FunctionError")
                    try:
                        _pl_u5 = resp_u5.get("Payload")
                        payload_u5 = json.loads(
                            _pl_u5.read() if hasattr(_pl_u5, "read") else
                            (_pl_u5 or b"{}"))
                    except Exception:  # noqa: BLE001
                        payload_u5 = {}
                    bad_items_u5 = payload_u5.get("batchItemFailures") or []
                    if fn_err_u5:
                        record(results, "U5 within limit", "FAIL",
                               f"invoke FunctionError={fn_err_u5!r}")
                        state["write_failed"] = True
                    elif bad_items_u5:
                        record(results, "U5 within limit", "FAIL",
                               f"invoke batchItemFailures="
                               f"{json.dumps(bad_items_u5)[:120]}")
                        state["write_failed"] = True
                    else:
                        act = _ddb_deser(
                            ddb.get_item(TableName=t["actions"],
                                         Key={"action_id": {"S": action_id_a}},
                                         ConsistentRead=True).get("Item") or {})
                        if not act:
                            record(results, "U5 within limit", "FAIL",
                                   f"no Action {action_id_a} after invoke with A")
                            state["write_failed"] = True
                        elif act.get("status") != "executed":
                            record(results, "U5 within limit", "FAIL",
                                   f"Action status={act.get('status')!r} (want 'executed'): "
                                   f"{json.dumps(act, default=str)[:200]}")
                            state["write_failed"] = True
                        else:
                            problems5: list[str] = []
                            # W4: compare decided_by exactly (kind AND id must match).
                            exp_decided_a = {"kind": "rule",
                                             "id": f"{_RENTAL_FLEET}/{_auto_rule_id}"}
                            if act.get("decided_by") != exp_decided_a:
                                problems5.append(
                                    f"decided_by={act.get('decided_by')!r} "
                                    f"(want {exp_decided_a!r})")
                            if act.get("decision") != "auto_approved":
                                problems5.append(
                                    f"decision={act.get('decision')!r} (want 'auto_approved')")
                            # W4: GET /moves must list this move with the right fields.
                            try:
                                code_mv, mv_list_body, _ = _get_json(
                                    _auto_url(cms_base, "moves"), op_token,
                                    {"fleetId": _RENTAL_FLEET, "limit": "200"})
                                if code_mv != 200:
                                    problems5.append(
                                        f"GET /moves answered {code_mv} (want 200)")
                                else:
                                    listed_mv = next(
                                        (r for r in (mv_list_body.get("rows") or [])
                                         if r.get("moveId") == action_id_a),
                                        None,
                                    )
                                    if listed_mv is None:
                                        problems5.append(
                                            f"GET /moves does not list {action_id_a}")
                                    else:
                                        got_vins = sorted(listed_mv.get("vins") or [])
                                        exp_vins = sorted(a_vins)
                                        if got_vins != exp_vins:
                                            problems5.append(
                                                f"move.vins {got_vins!r} != {exp_vins!r}")
                                        if listed_mv.get("fromLocationId") != a_fr:
                                            problems5.append(
                                                f"move.fromLocationId="
                                                f"{listed_mv.get('fromLocationId')!r} "
                                                f"(want {a_fr!r})")
                                        if listed_mv.get("toLocationId") != a_to:
                                            problems5.append(
                                                f"move.toLocationId="
                                                f"{listed_mv.get('toLocationId')!r} "
                                                f"(want {a_to!r})")
                                        if listed_mv.get("findingId") != fa["finding_id"]:
                                            problems5.append(
                                                f"move.findingId="
                                                f"{listed_mv.get('findingId')!r} "
                                                f"(want {fa['finding_id']!r})")
                                        # C6: check CMS's rule-decision fields in the GET /moves row.
                                        listed_rule_id = (listed_mv.get("ruleId")
                                                          or listed_mv.get("rule_id"))
                                        if listed_mv.get("decidedByKind") != "rule":
                                            problems5.append(
                                                f"move.decidedByKind="
                                                f"{listed_mv.get('decidedByKind')!r} "
                                                f"(want 'rule')")
                                        if listed_rule_id != _auto_rule_id:
                                            problems5.append(
                                                f"move.ruleId={listed_rule_id!r} "
                                                f"(want {_auto_rule_id!r})")
                                        if listed_mv.get("decision") != "auto_approved":
                                            problems5.append(
                                                f"move.decision="
                                                f"{listed_mv.get('decision')!r} "
                                                f"(want 'auto_approved')")
                                        if "approverSub" in listed_mv:
                                            problems5.append(
                                                f"move.approverSub present in CMS "
                                                f"move record: "
                                                f"{listed_mv.get('approverSub')!r}")
                            except Exception as exc:  # noqa: BLE001
                                problems5.append(
                                    f"GET /moves raised {type(exc).__name__}: {exc}"[:200])
                            # C6: ConsistentRead of CMS moves table for A's record.
                            try:
                                ddb_mv = _auto_move(ddb, t, action_id_a)
                                if ddb_mv is not None:
                                    ddb_rule_id = (ddb_mv.get("ruleId")
                                                   or ddb_mv.get("rule_id"))
                                    if ddb_mv.get("decidedByKind") != "rule":
                                        problems5.append(
                                            f"move (DDB) decidedByKind="
                                            f"{ddb_mv.get('decidedByKind')!r} "
                                            f"(want 'rule')")
                                    if ddb_rule_id != _auto_rule_id:
                                        problems5.append(
                                            f"move (DDB) ruleId={ddb_rule_id!r} "
                                            f"(want {_auto_rule_id!r})")
                                    if ddb_mv.get("decision") != "auto_approved":
                                        problems5.append(
                                            f"move (DDB) decision="
                                            f"{ddb_mv.get('decision')!r} "
                                            f"(want 'auto_approved')")
                                    if "approverSub" in ddb_mv:
                                        problems5.append(
                                            f"move (DDB) approverSub present: "
                                            f"{ddb_mv.get('approverSub')!r}")
                            except Exception as exc:  # noqa: BLE001
                                problems5.append(
                                    f"_auto_move ConsistentRead raised "
                                    f"{type(exc).__name__}: {exc}"[:200])
                            # Per-car checks: lookup failure is a FAIL (W4), not swallowed.
                            for v in a_vins:
                                try:
                                    v_vid = _auto_vehicle_id_of(ddb, t, v)
                                except Exception as exc:  # noqa: BLE001
                                    problems5.append(
                                        f"vehicle_id lookup for {v} failed: {exc}"[:120])
                                    continue
                                try:
                                    v_row = _auto_vehicle(ddb, t, v_vid)
                                    if v_row.get("locationId") != a_to:
                                        problems5.append(
                                            f"{v} at {v_row.get('locationId')!r} "
                                            f"not {a_to!r}")
                                except Exception as exc:  # noqa: BLE001
                                    problems5.append(
                                        f"vehicle row read for {v} ({v_vid}) "
                                        f"failed: {exc}"[:120])
                            if problems5:
                                record(results, "U5 within limit", "FAIL",
                                       "; ".join(problems5))
                                state["write_failed"] = True
                            else:
                                record(results, "U5 within limit", "PASS",
                                       f"Action auto_approved, "
                                       f"decided_by={exp_decided_a!r}, "
                                       f"executed; GET /moves lists it; "
                                       f"{len(a_vins)} car(s) at {a_to}")
                except Exception as exc:  # noqa: BLE001
                    record(results, "U5 within limit", "FAIL",
                           f"unexpected {type(exc).__name__}: {exc}"[:200])
                    state["write_failed"] = True
            else:
                record(results, "U5 within limit", "SKIP",
                       "not run: write window or earlier failure")

        finally:
            # --- U6 (always, inside the ExitStack so op_token is still valid): ---
            # Disable the rule and return probe car.
            # W1: u6_problems is defined before the ExitStack; cleanup rows and the U6
            # always row are recorded AFTER the ExitStack exits (after _temp_operator's
            # finally runs).
            # C7: one _signals_held() around all of U6 (disable → return-to-service →
            # residue) so a signal during any step defers until the full cleanup
            # completes; a SIGTERM during the disable can't skip the return-to-service.
            with _signals_held():
                if state["rule_created"]:
                    # C4: no 'limit' in body (pure-disable path skips the bounds SSM
                    # read), retry with ConsistentRead read-back.
                    _disabled = False
                    for _wait in (0.0, 2.0, 5.0):
                        if _wait:
                            time.sleep(_wait)
                        try:
                            code_d, _resp_d, _ = _put_json(
                                rule_url, op_token,
                                {"action_kind": "move_vehicles", "enabled": False})
                            if code_d == 200:
                                rule_now = _ddb_deser(
                                    ddb.get_item(TableName=t["rules"],
                                                 Key={"fleet_id": {"S": _RENTAL_FLEET},
                                                      "rule_id": {"S": _auto_rule_id}},
                                                 ConsistentRead=True).get("Item") or {})
                                if not rule_now.get("enabled"):
                                    _disabled = True
                                    # S2: delete the disabled livetest rule via the
                                    # approval-rules API so it does not accumulate.
                                    try:
                                        _request("DELETE", rule_url, op_token)
                                    except Exception:  # noqa: BLE001
                                        pass
                                    break
                        except Exception:  # noqa: BLE001
                            pass
                    if not _disabled:
                        u6_problems.append(
                            f"rule {_auto_rule_id!r} still ENABLED after disable PUT")
                        # W17: print the cleanup pointer so the operator knows which
                        # rule is still enabled (an enabled rule auto-approves the
                        # next 06:00Z run's moves up to its cost limit).
                        _auto_cleanup_pointer(
                            _run_id, args.stage, cms_base, avx_base, args.workgroup,
                            f"rule {_auto_rule_id!r} is still ENABLED; "
                            f"the next 06:00Z run would be auto-approved")

                # Return probe car before residue so the car is freed even if the
                # residue scan raises.
                if state["probe_grounded"] and probe_vid:
                    ok_rts, detail_rts = _auto_return_to_service(
                        ddb, t, cms_base, op_token, probe_vid, probe_gid,
                        state["probe_written"])
                    if not ok_rts:
                        u6_problems.append(f"return to service: {detail_rts}")
                        # C5: print the pointer so the operator can recover.
                        _auto_cleanup_pointer(
                            _run_id, args.stage, cms_base, avx_base,
                            args.workgroup, f"probe car {probe_vid} is still grounded")

                # W6/W12: residue check — no extra Actions by this run's rule, no extra
                # move records.  W12: the per-run rule_id is unique to this run, so no
                # time filter is needed; any Action with decided_by.id matching this run's
                # rule was approved by THIS run's rule.
                # Runs once in the finally path so a raise after U2 still reaches it.
                action_id_a = _decision_id(fa["finding_id"])
                action_id_b = _decision_id(fb["finding_id"])
                try:
                    actions_by_rule = [
                        a for a in _ddb_scan_all(
                            ddb, t["actions"],
                            FilterExpression="fleet_id = :f",
                            ExpressionAttributeValues={":f": {"S": _RENTAL_FLEET}},
                            ConsistentRead=True,
                        )
                        if (a.get("decided_by") or {}).get("id")
                        == f"{_RENTAL_FLEET}/{_auto_rule_id}"
                    ]
                    extra_actions = {a["action_id"] for a in actions_by_rule} - {
                        action_id_a, action_id_b}
                    current_move_ids = frozenset(
                        r["moveId"] for r in _ddb_query_all(
                            ddb, t["moves"],
                            KeyConditionExpression="fleetId = :f",
                            ExpressionAttributeValues={":f": {"S": _RENTAL_FLEET}},
                            ConsistentRead=True,
                        )
                        if "moveId" in r
                    )
                    extra_moves = (current_move_ids - existing_move_ids) - {action_id_a}
                    if extra_actions or extra_moves:
                        detail = (
                            f"extra Actions by rule: {sorted(extra_actions) or 'none'}; "
                            f"extra move records: {sorted(extra_moves) or 'none'}"
                        )
                        u6_problems.append(detail)
                        record(results, "U6 residue", "FAIL", detail)
                    else:
                        # W12: PASS text names the rule and the actual new move records.
                        new_move_ids = sorted(current_move_ids - existing_move_ids)
                        record(results, "U6 residue", "PASS",
                               f"no Action by rule {_auto_rule_id!r} besides A and B; "
                               f"new move records: {new_move_ids}")
                except Exception as exc_res:  # noqa: BLE001
                    _res_msg = f"residue check error: {type(exc_res).__name__}: {exc_res}"[:150]
                    u6_problems.append(_res_msg)
                    record(results, "U6 residue", "FAIL", _res_msg)

    # W1: record cleanup rows and U6 always AFTER the ExitStack exits, so
    # _temp_operator's finally (which may append to cleanup) has already run.
    for cmd in cleanup:
        u6_problems.append(f"temp user not removed; run: {cmd}")
        record(results, "cleanup", "FAIL", f"temp user not removed; run: {cmd}")

    record(results, "U6 always",
           "FAIL" if u6_problems else "PASS",
           "; ".join(u6_problems) if u6_problems else
           # S5 bullet 3: say only what happened.
           (("rule disabled" if state.get("rule_created") else "no rule created")
            + ", "
            + ("probe car returned" if state.get("probe_grounded") else "nothing grounded")
            + ", temp operator deleted"))

    return _summary(results)


def _auto_build_stream_event(ddb: Any, t: dict, finding: dict) -> dict:
    """Build the DynamoDB Streams event the auto-approver receives for *finding*.

    Reads the Finding's current item with ConsistentRead and serialises it as a
    single-record MODIFY event (the same shape the stream trigger delivers).
    """
    fid = finding["finding_id"]
    item = ddb.get_item(TableName=t["findings"], Key={"finding_id": {"S": fid}},
                        ConsistentRead=True).get("Item") or {}
    # item is already in DynamoDB JSON (low-level client); use it as NewImage directly.
    return {
        "Records": [{
            "eventName": "MODIFY",
            "dynamodb": {"SequenceNumber": "0000000000001", "NewImage": item},
        }]
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Live verification for FI rebalancing Stage E (spec 2026-10-06-fi-rebalancing-stage-e)",
    )
    ap.add_argument(
        "--part", required=True, choices=["adp", "agent", "cms", "cms-next", "auto", "review"],
        help="verification part to run",
    )
    ap.add_argument("--stage", required=True, help="deployment stage; only 'staging' is accepted")
    ap.add_argument(
        "--workgroup", required=True,
        help="Athena workgroup for ADP queries",
    )
    ap.add_argument(
        "--expect-after", dest="expect_after", metavar="YYYY-MM-DD",
        help="A2: data_as_of / run_date must be strictly after this date",
    )
    ap.add_argument(
        "--lake-bucket", dest="lake_bucket",
        help="ADP lake S3 bucket (derived from stage+account+region when omitted)",
    )
    # --part agent options
    ap.add_argument(
        "--allow-writes", dest="allow_writes", action="store_true", default=False,
        help="agent part: allow Lambda invoke, SSM write, Cognito user create/delete",
    )
    ap.add_argument(
        "--use-run", dest="use_run", metavar="STARTED_AT",
        help="agent G1: use a pre-existing completed run by started_at instead of invoking",
    )
    ap.add_argument(
        "--avx-api-endpoint", dest="avx_api_endpoint",
        help="agent G4b: AVX API base URL (e.g. https://<id>.execute-api.us-west-2.amazonaws.com/staging)",
    )
    ap.add_argument(
        "--cms-pool-id", dest="cms_pool_id",
        help="agent G4b: CMS Cognito user pool id (read from SSM when omitted)",
    )
    # --part cms / cms-next options (task 7.5)
    ap.add_argument(
        "--cms-api-endpoint", dest="cms_api_endpoint",
        help="cms: the CMS UI API base URL (https://<id>.execute-api.us-west-2.amazonaws.com/prod)",
    )
    ap.add_argument(
        "--approve-move", dest="approve_move", action="store_true", default=False,
        help="cms: approve one real rental move and ground one car for the refusal probe",
    )
    ap.add_argument("--run-id", dest="run_id",
                    help="cms / cms-next / review: an earlier cms run's id")
    ap.add_argument(
        "--agent-run", dest="agent_run", metavar="STARTED_AT",
        help="review: the rebalance run R1/R3 score (default: the first completed run from the "
             "review date's 06:00 UTC)",
    )
    ap.add_argument(
        "--cleanup-only", dest="cleanup_only", action="store_true", default=False,
        help="cms: return an earlier run's test car to service, nothing else",
    )
    ap.add_argument(
        "--cms-results-bucket", dest="cms_results_bucket",
        help="cms: CMS's Athena results bucket holding the recall views (derived when omitted)",
    )
    ap.add_argument(
        "--limit", dest="limit", type=float, default=None,
        help="auto: maximum move cost ($USD) for candidate selection; "
             "required for --part auto (not cleanup-only); "
             "must be in (0, max_limit_usd]",
    )
    args = ap.parse_args(argv)

    _install_signal_handlers()
    if args.stage != "staging":
        print("REFUSED: only --stage staging is supported", file=sys.stderr)
        return 2
    if os.environ.get("LIVE_TESTS") != "1":
        print("REFUSED: set LIVE_TESTS=1 to run live checks", file=sys.stderr)
        return 2

    # Writes outside the conftest AWS block run as the caller with real credentials.
    # Refuse before any client unless the live-run step gave its go.
    _refusal = _lwg.check_write_flags(
        stage=args.stage, go_env=_LIVE_GO_ENV,
        flags={
            "--allow-writes": args.allow_writes,
            "--approve-move": args.approve_move,
            "--cleanup-only": args.cleanup_only,
        },
    )
    if _refusal:
        print(_refusal, file=sys.stderr)
        return 2

    if args.part == "adp":
        return _part_adp(args)
    if args.part == "agent":
        return _part_agent(args)
    if args.part == "cms":
        return _part_cms(args)
    if args.part == "cms-next":
        return _part_cms_next(args)
    if args.part == "auto":
        return _part_auto(args)
    if args.part == "review":
        return _part_review(args)

    print(f"REFUSED: unknown --part {args.part!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
