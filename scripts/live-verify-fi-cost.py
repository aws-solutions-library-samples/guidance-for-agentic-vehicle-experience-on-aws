#!/usr/bin/env python3
"""Live verification for FI Cost Stage B — spec 2026-10-02-fi-cost-stage-b, task 5.3.

Refuses without ``LIVE_TESTS=1``.

Exit codes
----------
0  Every requested item ran and passed.
1  At least one check failed.
2  At least one check was skipped — even when other checks passed.  A skip
   is a non-passing result: any item that cannot run is unverified.  Pass
   ``--allow-writes`` to enable write-gated items; pre-populate Findings via
   items 2-3 before running items 5, 7, 8.

Items (run order: 1, 7, 2, 3, 4, 5, 6, 8)
------------------------------------------
1  Athena views (adp_{stage}_fleet_cost) return rows for every managed fleet;
   CMS trips vs ADP miles comparison runs as NOTE-only (D2).  Read-only.

7  With FI_COST_MODEL_ID overridden to a nonexistent model (one-run Lambda
   env override, reverted in finally), the run writes rules-fallback Findings
   instead of aborting.  Checks cost.diagnosis_source == "rules" from the
   Finding's evidence list.  Runs before items 2/3 (D3); returns False if
   guard refuses or invoke raises, which skips items 2 and 3.
   Requires ``--allow-writes``.

2  One agent run (repeated until defers=0) finds >= 80% of planted anomalies
   with the planted cause.  Scored against the answer key, which this script
   reads under the operator's credentials — never the agent role (ADP decisions
   2026-10-04 item 7).  Requires ``--allow-writes`` (Lambda invocation).

3  A second run, after item 2 is fully converged, adds zero Findings and makes
   zero model calls (cost.deferred=0, tokens_used=0 in the run record).
   Requires ``--allow-writes``.

4  Athena results were paged to the end: the agent's anomaly-selected count
   (cost.anomalies_selected from the latest run record) matches a COUNT(*)
   of the baseline view's outlier predicate for the same data_month.  Also
   measures DataScannedInBytes for the agent selection query.  Read-only.

5  A fleet-A operator sees zero fleet-B cost Findings via the AVX
   /findings/fleet/{id} route (D1; CMS FI queue route is item 7.1's probe).
   Creates one ephemeral Cognito user (fleet-operator group).
   Requires ``--allow-writes``.

6  A forced Lambda error (malformed max_diagnoses_per_run override) triggers
   the cost run-failure alarm via real CloudWatch Errors metric; the alarm's
   SNS topic has at least one confirmed subscriber.
   Requires ``--allow-writes``.

8  The fleet_recommendation supervisor tool, asked from a fleet_manager session
   via the text runtime (/assistant/chat), returns the cost Finding's headline,
   evidence and decision status for a Finding in the caller's fleet, and refuses
   a Finding in another fleet.  Skips (exit 2) if:
     - the text runtime's AVX_FLEET_IDS_CLAIM_TRUSTED env var is not "true"
       (task 5.0 step 3 has not been done), or
     - no existing cost Finding is found to test with, or
     - no fleet-operator user with custom:fleetIds matching a known fleet exists.
   Requires ``--allow-writes``.

Read-only items: 1, 4.
Write items (require ``--allow-writes``): 2, 3, 5, 6, 7, 8.
Creates ephemeral Cognito users: 5, 8 (included in write items above).

Rule: exit 0 only when every requested item ran and passed.  Any SKIP makes
the run non-passing (exit 2) because a skipped item is unverified.

Usage
-----
    LIVE_TESTS=1 .venv/bin/python3 scripts/live-verify-fi-cost.py
    LIVE_TESTS=1 .venv/bin/python3 scripts/live-verify-fi-cost.py \\
        --stage staging --region us-west-2
    LIVE_TESTS=1 .venv/bin/python3 scripts/live-verify-fi-cost.py \\
        --stage staging --item 1
    FI_COST_LIVE_GO=staging LIVE_TESTS=1 .venv/bin/python3 scripts/live-verify-fi-cost.py \\
        --stage staging --all --allow-writes
    FI_COST_LIVE_GO=staging LIVE_TESTS=1 .venv/bin/python3 scripts/live-verify-fi-cost.py \\
        --stage staging --item 2 --allow-writes

Verify command (tasks.md 5.3, runs all 8 items):
    FI_COST_LIVE_GO=staging LIVE_TESTS=1 .venv/bin/python3 scripts/live-verify-fi-cost.py \\
        --stage staging --region us-west-2 --all --allow-writes

Anti-vacuity
------------
The script exits 1 (not 0) when the answer key is empty or when no cost
Findings exist at all — a zero-row answer key would let item 2 vacuously pass.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import re
import secrets
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator

# Run as a file (``python scripts/live-verify-fi-cost.py``), sys.path[0] is scripts/, so items 4
# and 8's imports of the agent's own modules (``agents.fi_cost``, ``agents.supervisor``) need the
# repo root on the path (5.3 run 1, 2026-10-08: item 4 crashed on ``import agents``).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Live-write guard (scripts/lib/live_write_guard.py)
_LIB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)
import live_write_guard as _lwg  # noqa: E402

#: Set to ``--stage`` by the live-run step, on the user's go, and by nothing else.
_LIVE_GO_ENV = "FI_COST_LIVE_GO"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ADP_REGION = "us-east-1"          # Athena and lake always us-east-1
_AGENT_ID = "agent-fi-cost"
_FINDING_KIND_ANOMALY = "fleet.cost.anomaly"
_FINDING_KIND_SELL = "fleet.cost.sell_timing"
_COST_ALARM_SUFFIX = "run-failure"  # vsa-{stage}-tier2-run-failure-{region}
_COST_RUN_COMPONENT = "cost"
_NON_EXISTENT_MODEL = "anthropic.claude-nonexistent-does-not-exist-v0"
_PLANTS_PREFIX = "restricted/fleet_cost/"
_ANSWER_KEY_RELKEY_GLOB = "plants/plant_anchor_month="
_ANSWER_KEY_FILENAME = "answer_key.parquet"
_SCORE_THRESHOLD_PCT = 0.80       # 80 % of planted anomalies

# B-S3: the only stage this script may run against (it writes there).
_ALLOWED_STAGE = "staging"
# B-S5: shapes checked before a value is put into Athena SQL text.
_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")       # as agents/fi_cost/evidence.py
_YEAR_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

# D5: phrases used to infer cause from no_action Finding headline+detail (lowercase).
# A no_action Finding has no recommended_action, so cause is not structured.
# Count only when EXACTLY ONE cause phrase matches and it equals the planted cause.
_CAUSE_PHRASES: dict[str, tuple[str, ...]] = {
    "charging_rate": ("charging rate", "charge rate"),
    "charging_mix": ("charging mix", "public charging", "public station"),
    "repeat_repair": ("repeat repair", "recurring repair"),
    "low_utilisation": ("low utilisation", "low utilization", "underutilised", "underutilized"),
    "efficiency": ("efficiency", "energy efficiency"),
    "unclear": ("unclear", "no clear cause"),
}

# Miles tolerance for CMS trips vs ADP monthly miles comparison.
# ADP vehicle_cost_monthly.miles is generated from duty-cycle totals
# (fleet_ops.daily_usage, provenance='generated').  CMS trips store
# totalDistance in km (Flink TripProcessor writes km); this script converts
# km to miles via KM_PER_MILE before comparing.  There is no pipeline that
# forces ADP generated miles to equal CMS trip miles — they use different
# sources (energy model vs GPS sensor).
#
# W1: tolerance must be set explicitly via --miles-tolerance.  Absent means
# SKIP ('tolerance not set; user decision pending') — the check should not
# FAIL at an uncalibrated 50 % threshold.  Once the operator has measured
# a representative sample and agreed on a tolerance, pass e.g.
# km per mile, matching trip_units.py
_KM_PER_MILE = 1.609344

# Fleet IDs held by iOS persona / test users seeded via seed-persona-users.py.
# W7: this set is kept as a last-resort fallback ONLY.  The primary check
# (_check_ios_fleet_intersection) reads custom:fleetIds from live Cognito
# users and FAILs if the read cannot run.  Any fleet discovered here that
# is not already in the live Cognito check is an additional safety net.
_IOS_PERSONA_FLEET_IDS: frozenset = frozenset({
    "FLEET-MERIDIAN-ONBOARD",   # Marcus Reyes vehicle fleet (seed-persona-users.py)
    "MERIDIAN-OEM",             # Marcus Reyes user-tenant fleet
    "WPR-RENTAL",               # Priya Shah rental fleet
    "VO-OEM",                   # Jordan Parker (vehicle_owner persona) fleet
    "SA-FLEET",                 # service_advisor persona fleet
    "TECH-FLEET",               # technician persona fleet
})

# CMS web test user (fleet.operator.verify) excluded from the iOS fleet check.
# spec decisions.md 2026-10-07 "User answers at the 5.3 cycle-limit stop" D4.
# Keyed by stage; value is the frozenset of Cognito 'sub' ATTRIBUTE values to skip.
_WEB_TEST_USER_SUBS: dict[str, frozenset] = {
    "staging": frozenset({"28f183d0-0011-70de-f2bb-ccf6ac1bfc4b"}),
}

# Maximum number of non-livetest users to page from Cognito for the iOS fleet check.
_IOS_FLEET_IDS_COGNITO_MAX_USERS = 500
_VIEW_MONTHLY = "vehicle_cost_monthly"
_VIEW_BASELINE = "vehicle_cost_baseline"
_RUNS_COMPONENT = f"{_AGENT_ID}#{_COST_RUN_COMPONENT}"

# Decision 2026-10-05 item 9, decisions.md: max cap raised to bypass
# 50-call limit so a single run can reach 80% without repeating four times.
# NW1: 04:00 UTC is the scheduled cost run.  Items 2/3/7 refuse to start a cost run
# within ±_COST_SCHEDULE_MARGIN_MINUTES of that hour (item 6's invoke fails before
# run_cost starts, so it does not check).
_COST_SCHEDULE_UTC_HOUR = 4    # 04:00 UTC
_COST_SCHEDULE_MARGIN_MINUTES = 60  # ±60 minutes

# Maximum pages to fetch in item 5's paging loop (N1 fix: was infinite).
_MAX_FINDINGS_PAGES = 20
# Item 2 repeats the cost run until nothing is deferred. 5.3 run 1 (2026-10-08) left 149 deferred
# after 6 runs, falling 12–33 a run; 13 runs clear 149 at the slowest rate, plus margin (user,
# 2026-10-08: one 5.3 run must clear the waiting list).
_MAX_REPEAT_RUNS = 16

# Lambda read_timeout must exceed the function's 15-minute timeout (900 s) to
# avoid botocore retrying a live run (C3).  We use 910 s + 1 attempt max.
_LAMBDA_READ_TIMEOUT_S = 910
_LAMBDA_MAX_ATTEMPTS = 1

# ---------------------------------------------------------------------------
# Database / view helpers
# ---------------------------------------------------------------------------


def _fleet_cost_db(stage: str) -> str:
    """Return the Athena database name for fleet_cost for the given stage.

    Correct name: adp_{stage}_fleet_cost  (not the bare 'fleet_cost').
    This matches the database prefix in agents/fi_cost/sql/select_anomalies.sql.
    C7: was using the wrong database name 'fleet_cost'.
    """
    return f"adp_{stage}_fleet_cost"


def _select_anomalies_count_sql(stage: str, data_month: str) -> str:
    """COUNT(*) equivalent of agents/fi_cost/sql/select_anomalies.sql.

    Used by item 4 to verify the agent paged all selected anomalies.
    Matches the WHERE predicate from the template file.
    C5: item 4 used to check the script's own pager, not the agent's.
    """
    db = _fleet_cost_db(stage)
    if not _YEAR_MONTH_RE.fullmatch(str(data_month)):
        raise ValueError(f"data_month {data_month!r} is not YYYY-MM")  # B-S5
    return (
        f"SELECT COUNT(*) FROM {db}.{_VIEW_BASELINE} "
        f"WHERE year_month = '{data_month}' "
        f"AND (is_outlier OR is_energy_outlier)"
    )


# ---------------------------------------------------------------------------
# Pure helpers — unit-testable without AWS
# ---------------------------------------------------------------------------


_NEGATION_AFTER = ("normal", "in line", "not", "typical")
_NEGATION_BEFORE = ("not", "no", "isn't", "rather than")


def _phrase_named(text: str, phrase: str) -> bool:
    """True when *phrase* occurs in *text* at least once without a nearby negation.

    R4-W5: "efficiency is in line with the cohort" names the cause only to rule it out.
    A mention counts unless one of _NEGATION_AFTER appears in the next 4 words or one of
    _NEGATION_BEFORE in the previous 3.
    """
    start = 0
    while True:
        i = text.find(phrase, start)
        if i < 0:
            return False
        after = [w.strip(".,;:!?()") for w in text[i + len(phrase):].split()[:4]]
        before = [w.strip(".,;:!?()") for w in text[:i].split()[-3:]]

        def _hit(neg: str, words: list[str]) -> bool:
            # single words match whole words only ("no" must not match "economy")
            return neg in words if " " not in neg else neg in " ".join(words)

        if not any(_hit(n, after) for n in _NEGATION_AFTER) and not any(
            _hit(n, before) for n in _NEGATION_BEFORE
        ):
            return True
        start = i + len(phrase)


def score_plants_detail(
    answer_key_rows: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    agent_id: str = _AGENT_ID,
    text_hits_out: list[tuple[str, str, str]] | None = None,
) -> tuple[int, int, int, int, int, int, float]:
    """Return (flagged, right_cause_action, no_action, no_action_right_cause, correct, total, pct).

    D5: extends the structured-cause check to no_action Findings whose headline+detail
    names exactly one _CAUSE_PHRASES key equal to the planted cause.
    A rules-fallback Finding (fixed headline/detail) names no cause phrase → no hit.
    flagged: planted VINs with any cost-anomaly Finding (right cause, wrong cause or
        no_action), i.e. flagged by either rule and diagnosed.
    right_cause_action: matched via recommended_action.parameters.cause.
    no_action: Finding exists for VIN but has no recommended_action.
    no_action_right_cause: no_action Finding whose text names exactly one matching cause.
    correct: right_cause_action + no_action_right_cause.
    """
    vin_causes: dict[str, set[str]] = {}
    vin_no_action: dict[str, list[dict[str, Any]]] = {}
    for f in findings:
        if f.get("finding_kind") != _FINDING_KIND_ANOMALY:
            continue
        if f.get("agent_id") != agent_id:
            continue
        vin = (f.get("scope") or {}).get("vin") or f.get("vin") or ""
        if not vin:
            continue
        ra = f.get("recommended_action") or {}
        cause = (ra.get("parameters") or {}).get("cause") or ""
        if cause:
            vin_causes.setdefault(vin, set()).add(cause)
        else:
            vin_no_action.setdefault(vin, []).append(f)

    flagged = right_cause_action = no_action = no_action_right_cause = 0
    for row in answer_key_rows:
        vin = row.get("vin") or ""
        planted = row.get("cause") or ""
        if vin in vin_causes or vin in vin_no_action:
            flagged += 1
        if vin in vin_causes:
            if planted in vin_causes[vin]:
                right_cause_action += 1
        elif vin in vin_no_action:
            no_action += 1
            for f in vin_no_action[vin]:
                text = ((f.get("headline") or "") + " " + (f.get("detail") or "")).lower()
                hits = [c for c, ps in _CAUSE_PHRASES.items()
                        if any(_phrase_named(text, p) for p in ps)]
                if len(hits) == 1 and hits[0] == planted:
                    no_action_right_cause += 1
                    if text_hits_out is not None:
                        phrase = next(p for p in _CAUSE_PHRASES[planted] if _phrase_named(text, p))
                        text_hits_out.append((vin, phrase, str(f.get("headline") or "")[:120]))
                    break

    correct = right_cause_action + no_action_right_cause
    total = len(answer_key_rows)
    pct = correct / total if total > 0 else 0.0
    return flagged, right_cause_action, no_action, no_action_right_cause, correct, total, pct


def score_plants(
    answer_key_rows: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    agent_id: str = _AGENT_ID,
) -> tuple[int, int, float]:
    """Return (correctly_diagnosed, total_plants, score_fraction). See score_plants_detail for D5."""
    _, _, _, _, correct, total, pct = score_plants_detail(answer_key_rows, findings, agent_id)
    return correct, total, pct


def check_paging_match(count_result: int, paged_count: int) -> tuple[bool, str]:
    """Return (ok, detail).

    The paged row count must equal the COUNT(*) result from the same query
    execution window (decisions 2026-10-05 Group 2 cycle 2 addition to 5.3).
    """
    if count_result == paged_count:
        return True, f"rows={paged_count} matches COUNT(*)"
    return False, f"paged={paged_count} != COUNT(*)={count_result}"


def build_env_with_override(
    original_env: dict[str, str],
    overrides: dict[str, str],
) -> dict[str, str]:
    """Return a new env dict with overrides applied, leaving original untouched."""
    merged = dict(original_env)
    merged.update(overrides)
    return merged


def _is_resource_conflict(exc: Exception) -> bool:
    """Return True when exc is a Lambda ResourceConflictException (update in progress)."""
    resp = getattr(exc, "response", None)
    return isinstance(resp, dict) and resp.get("Error", {}).get("Code") == "ResourceConflictException"


def _year_month_epoch_ms_range(year_month: str) -> tuple[int, int]:
    """Return (start_ms, end_ms_inclusive) for a 'YYYY-MM' string.

    startTime in CMS trips is a NUMBER (epoch milliseconds).  DynamoDB
    begins_with only works on strings; we use BETWEEN on NUMBER keys instead.
    C7: was using begins_with which raises ValidationException on a NUMBER sort key.
    """
    year, month = map(int, year_month.split("-"))
    start_dt = datetime.datetime(year, month, 1, tzinfo=datetime.timezone.utc)
    if month == 12:
        end_dt = datetime.datetime(year + 1, 1, 1, tzinfo=datetime.timezone.utc)
    else:
        end_dt = datetime.datetime(year, month + 1, 1, tzinfo=datetime.timezone.utc)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000) - 1  # inclusive end of last ms in month
    return start_ms, end_ms


def _extract_cost_stats(run_result: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return (cost_stats, ok) from a run_cost() summary dict.

    run_cost() returns summary["cost"] = stats (all cost-specific counters).
    Top-level has artifacts_written, tokens_used.  C2: the script was reading
    deferred/model_calls/rules_fallbacks from the wrong (top) level.

    Returns (cost_dict, True) if 'cost' key is present; ({}, False) otherwise.
    """
    if "cost" not in run_result:
        return {}, False
    return run_result["cost"], True


# ---------------------------------------------------------------------------
# Results tracker
# ---------------------------------------------------------------------------


class Results:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []
        self._cleanup_failed: bool = False

    def check(self, label: str, ok: bool, detail: str = "") -> bool:
        status = "PASS" if ok else "FAIL"
        self.rows.append((label, status, detail))
        print(f"  [{status}] {label}{(': ' + detail) if detail else ''}", flush=True)
        return ok

    def note(self, label: str, detail: str) -> None:
        self.rows.append((label, "NOTE", detail))
        print(f"  [NOTE] {label}: {detail}", flush=True)

    def skip(self, label: str, reason: str) -> None:
        self.rows.append((label, "SKIP", reason))
        print(f"  [SKIP] {label}: {reason}", flush=True)

    def mark_cleanup_failed(self) -> None:
        """Record that a restore or cleanup step failed (contributes to exit 1)."""
        self._cleanup_failed = True

    @property
    def failed(self) -> list[tuple[str, str, str]]:
        return [r for r in self.rows if r[1] == "FAIL"]

    @property
    def has_any_skip(self) -> bool:
        """True when at least one check was skipped.

        C1 fix: a SKIP alongside PASSes previously returned 0 (pass) because
        all_skipped only returned True when *no* PASS or FAIL existed.  Now
        any SKIP returns exit 2 regardless of other checks.
        """
        return any(r[1] == "SKIP" for r in self.rows)

    def summary(self) -> None:
        print("\nSummary")
        for label, status, detail in self.rows:
            print(f"  {status:4}  {label}{(': ' + detail[:120]) if detail else ''}")
        n_pass = sum(1 for _, s, _ in self.rows if s == "PASS")
        n_fail = sum(1 for _, s, _ in self.rows if s == "FAIL")
        n_skip = sum(1 for _, s, _ in self.rows if s == "SKIP")
        print(f"\n  pass={n_pass}  fail={n_fail}  skip={n_skip}")
        if self._cleanup_failed:
            print("  WARN: at least one restore/cleanup step failed — manual intervention may be needed")
        skipped_labels = [label for label, s, _ in self.rows if s == "SKIP"]
        if skipped_labels:
            print(f"\n  Skipped items (not verified): {', '.join(skipped_labels)}")


# ---------------------------------------------------------------------------
# AWS helpers
# ---------------------------------------------------------------------------


def _athena_poll(
    athena: Any,
    execution_id: str,
    poll_interval_s: float = 2.0,
    timeout_s: float = 120.0,
) -> dict[str, Any]:
    """Poll until query completes; return the QueryExecution dict or raise."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        resp = athena.get_query_execution(QueryExecutionId=execution_id)
        qe = resp["QueryExecution"]
        state = qe["Status"]["State"]
        if state == "SUCCEEDED":
            return qe
        if state in ("FAILED", "CANCELLED"):
            reason = qe["Status"].get("StateChangeReason", "")
            raise RuntimeError(f"Athena query {execution_id} {state}: {reason}")
        if state not in ("RUNNING", "QUEUED"):
            # Unknown state — fail fast rather than spinning to the timeout.
            # This also catches non-string states (e.g. MagicMock in tests).
            raise RuntimeError(
                f"Athena query {execution_id} returned unexpected state: {state!r}. "
                "Expected RUNNING, QUEUED, SUCCEEDED, FAILED, or CANCELLED."
            )
        time.sleep(poll_interval_s)
    raise TimeoutError(f"Athena query {execution_id} did not finish in {timeout_s}s")


def _athena_run(
    athena: Any,
    sql: str,
    workgroup: str,
    output_location: str,
) -> tuple[dict[str, Any], list[list[str]]]:
    """Execute SQL, wait for completion, return (execution, all_rows_as_strings).

    Rows are returned as list-of-list[str], header row excluded.
    DataScannedInBytes is in execution["Statistics"].
    """
    resp = athena.start_query_execution(
        QueryString=sql,
        WorkGroup=workgroup,
        ResultConfiguration={"OutputLocation": output_location},
    )
    eid = resp["QueryExecutionId"]
    execution = _athena_poll(athena, eid)

    # Page all results
    rows: list[list[str]] = []
    kwargs: dict[str, Any] = {"QueryExecutionId": eid}
    first_page = True
    while True:
        page = athena.get_query_results(**kwargs)
        result_rows = page.get("ResultSet", {}).get("Rows", [])
        start = 1 if first_page else 0  # skip header on first page
        for row in result_rows[start:]:
            rows.append([d.get("VarCharValue", "") for d in row.get("Data", [])])
        first_page = False
        token = page.get("NextToken") if isinstance(page, dict) else None
        if not token or not isinstance(token, str):
            break
        kwargs["NextToken"] = token
    return execution, rows


_MIN_BILLING_BYTES: int = 10 * 1024 * 1024  # Athena bills per query ≥ 10 MB


class _CapturingAthena:
    """Wraps an Athena client; records each start_query_execution QueryExecutionId.

    R3-W5/W6 fix: lets item 4 measure DataScannedInBytes only on the agent's
    real reader executions, not on the COUNT(*) wrappers the script adds.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self.execution_ids: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def start_query_execution(self, **kwargs: Any) -> dict[str, Any]:
        resp = self._client.start_query_execution(**kwargs)
        eid = resp.get("QueryExecutionId", "")
        if eid:
            self.execution_ids.append(eid)
        return resp


def _athena_bytes(athena: Any, execution_ids: list[str]) -> int:
    """Sum DataScannedInBytes for each execution_id; each billed at min 10 MB."""
    total = 0
    for eid in execution_ids:
        resp = athena.get_query_execution(QueryExecutionId=eid)
        b = resp.get("QueryExecution", {}).get("Statistics", {}).get("DataScannedInBytes", 0)
        total += max(int(b), _MIN_BILLING_BYTES)
    return total


def _athena_bytes_only(
    athena: Any, sql: str, workgroup: str, output_location: str,
) -> int:
    """Start+poll a query; return DataScannedInBytes (min 10 MB). Does not fetch rows."""
    resp = athena.start_query_execution(
        QueryString=sql,
        WorkGroup=workgroup,
        ResultConfiguration={"OutputLocation": output_location},
    )
    eid = resp["QueryExecutionId"]
    qe = _athena_poll(athena, eid)
    b = qe.get("Statistics", {}).get("DataScannedInBytes", 0)
    return max(int(b), _MIN_BILLING_BYTES)


def _make_lambda_client(region: str) -> Any:
    """Create a Lambda client with a read_timeout that exceeds the function timeout.

    C3 fix: the default botocore read_timeout is 60 s and retries are enabled,
    so a cost run that takes > 60 s (Athena + Bedrock work on any real run) is
    silently retried up to 4 more times.  Each retry starts another cost run.
    We set read_timeout=910 s (> 15-min function timeout) and disable retries.
    """
    import boto3  # noqa: PLC0415
    from botocore.config import Config  # noqa: PLC0415

    return boto3.client(
        "lambda",
        region_name=region,
        config=Config(
            read_timeout=_LAMBDA_READ_TIMEOUT_S,
            retries={"total_max_attempts": _LAMBDA_MAX_ATTEMPTS},
            tcp_keepalive=True,
        ),
    )


def _wait_lambda_response(
    lam: Any,
    function_name: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Invoke Lambda synchronously and return the parsed response payload.

    Uses a single attempt (no retries) with a read_timeout above the function
    timeout to prevent parallel cost runs (C3).  The timeout_s parameter from
    the old signature is removed; the client config sets the timeout.
    """
    resp = lam.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode(),
    )
    if resp.get("FunctionError"):
        raw = resp["Payload"].read() if hasattr(resp["Payload"], "read") else resp.get("Payload", b"")
        raise RuntimeError(
            f"Lambda {function_name} returned FunctionError={resp['FunctionError']!r}: "
            f"{raw[:400]}"
        )
    raw = resp["Payload"].read() if hasattr(resp["Payload"], "read") else resp.get("Payload", b"")
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"_raw": raw[:400]}


def _scan_findings_by_fleet(
    ddb: Any,
    table_name: str,
    fleet_id: str,
    agent_id: str = _AGENT_ID,
) -> list[dict[str, Any]]:
    """Query the by_fleet GSI for a given fleet_id, return all items."""
    from boto3.dynamodb.conditions import Attr, Key  # noqa: PLC0415

    table = ddb.Table(table_name)
    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {
        "IndexName": "by_fleet",
        "KeyConditionExpression": Key("fleet_id").eq(fleet_id),
        "FilterExpression": Attr("agent_id").eq(agent_id),
    }
    while True:
        page = table.query(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return items


def _latest_run_record(
    ddb: Any,
    table_name: str,
    component: str,
) -> dict[str, Any] | None:
    """Return the most recent run record for agent_component=component."""
    from boto3.dynamodb.conditions import Key  # noqa: PLC0415

    table = ddb.Table(table_name)
    resp = table.query(
        KeyConditionExpression=Key("agent_component").eq(component),
        ScanIndexForward=False,
        Limit=1,
    )
    items = resp.get("Items", [])
    return items[0] if items else None


def _read_answer_key(
    s3: Any,
    bucket: str,
    stage: str,
) -> tuple[list[dict[str, Any]], str]:
    """Return (rows, anchor_month) read from the answer key parquet in S3.

    Lists ``restricted/fleet_cost/plants/`` to find the anchor month, then
    reads the parquet with duckdb (not pyarrow).
    """
    prefix = f"{_PLANTS_PREFIX}plants/plant_anchor_month="
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, Delimiter="/")
    prefixes = [
        cp["Prefix"] for cp in resp.get("CommonPrefixes", [])
    ]
    if not prefixes:
        raise RuntimeError(
            f"No plant_anchor_month= prefixes found in s3://{bucket}/{prefix}. "
            "Has 5.1 run the generator for at least one month?"
        )
    # Use the lexicographically last anchor month (most recent run).
    anchor_prefix = sorted(prefixes)[-1]
    anchor_month = anchor_prefix.rstrip("/").rsplit("=", 1)[-1]
    key = anchor_prefix + _ANSWER_KEY_FILENAME
    obj = s3.get_object(Bucket=bucket, Key=key)
    data = obj["Body"].read()
    # Read with duckdb (always available in the shared .venv); no pyarrow needed.
    tmp = tempfile.NamedTemporaryFile(suffix=".parquet", delete=False)
    try:
        tmp.write(data)
        tmp.flush()
        tmp.close()
        import duckdb  # noqa: PLC0415
        conn = duckdb.connect()
        rows_raw = conn.execute("SELECT * FROM read_parquet(?)", [tmp.name]).fetchall()
        cols = [desc[0] for desc in conn.description]
        result = [dict(zip(cols, row)) for row in rows_raw]
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    return result, anchor_month


def _all_fleet_ids_for_agent(
    ddb: Any,
    table_name: str,
    agent_id: str = _AGENT_ID,
) -> set[str]:
    """Scan the findings table for unique fleet_ids from the cost agent."""
    from boto3.dynamodb.conditions import Attr  # noqa: PLC0415

    table = ddb.Table(table_name)
    fleet_ids: set[str] = set()
    kwargs: dict[str, Any] = {
        "FilterExpression": Attr("agent_id").eq(agent_id),
        "ProjectionExpression": "fleet_id",
    }
    while True:
        page = table.scan(**kwargs)
        for item in page.get("Items", []):
            fid = item.get("fleet_id")
            if fid:
                fleet_ids.add(str(fid))
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return fleet_ids


def _all_finding_ids_for_fleet(
    ddb: Any,
    table_name: str,
    fleet_id: str,
    agent_id: str = _AGENT_ID,
) -> set[str]:
    """Return all finding_id values from the by_fleet GSI for a fleet.

    C4 fix: item 5 was matching on the stripped top-level fleet_id field
    (which the API removes), passing vacuously.  We now collect finding_ids
    directly from DDB and check their absence in fleet A's response.
    """
    items = _scan_findings_by_fleet(ddb, table_name, fleet_id, agent_id)
    return {str(item.get("finding_id", "")) for item in items if item.get("finding_id")}

# ---------------------------------------------------------------------------
# W7: pre-invoke fleet safety check
# ---------------------------------------------------------------------------

def _collect_ios_fleet_ids_from_cognito(
    region: str,
    stage: str,
) -> tuple[frozenset, list]:
    """Return (fleet_ids, ignored) for non-livetest Cognito users in the CMS pool.

    fleet_ids: frozenset of all custom:fleetIds held by non-exempt users.
    ignored: list of (sub, frozenset(fleets)) for D4-exempt web-test-user subs.

    D4: a user whose 'sub' ATTRIBUTE is in _WEB_TEST_USER_SUBS[stage] is excluded
    from fleet_ids and recorded in ignored instead.

    Raises RuntimeError if the pool is not found, paging fails, or >500 users
    are returned with a PaginationToken still remaining (fail closed).
    """
    import boto3  # noqa: PLC0415
    from botocore.config import Config as _BotoConfig  # noqa: PLC0415
    idp = boto3.client(
        "cognito-idp",
        region_name=region,
        config=_BotoConfig(connect_timeout=10, retries={"total_max_attempts": 1}),
    )
    # Find the CMS pool for this stage
    pool_id: str | None = None
    page_args: dict[str, Any] = {"MaxResults": 60}
    while True:
        resp = idp.list_user_pools(**page_args)
        for p in resp.get("UserPools", []):
            if p["Name"] == f"cms-{stage}-ui-users":
                pool_id = p["Id"]
                break
        if pool_id:
            break
        tok = resp.get("NextToken")
        if not tok:
            break
        page_args["NextToken"] = tok
    if not pool_id:
        raise RuntimeError(f"Cognito pool cms-{stage}-ui-users not found in {region}")

    fleet_ids: set[str] = set()
    ignored: list[tuple[str, frozenset]] = []
    exempt_subs = _WEB_TEST_USER_SUBS.get(stage, frozenset())
    list_kwargs: dict[str, Any] = {"UserPoolId": pool_id, "Limit": 60}
    seen = 0
    while True:
        uresp = idp.list_users(**list_kwargs)
        for u in uresp.get("Users", []):
            uname = u.get("Username", "")
            if uname.startswith("livetest-"):
                continue
            attrs = {a["Name"]: a["Value"] for a in u.get("Attributes", [])}
            user_sub = attrs.get("sub", "")
            user_fleets: set[str] = set()
            for fid in attrs.get("custom:fleetIds", "").split(","):
                fid = fid.strip()
                if fid:
                    user_fleets.add(fid)
            if user_sub and user_sub in exempt_subs:
                # D4: exempt web-test-user; record but exclude from fleet_ids
                if user_fleets:
                    ignored.append((user_sub, frozenset(user_fleets)))
                continue
            fleet_ids.update(user_fleets)
        seen += len(uresp.get("Users", []))
        tok = uresp.get("PaginationToken")
        if seen >= _IOS_FLEET_IDS_COGNITO_MAX_USERS:
            if tok:
                raise RuntimeError(
                    f"Cognito list_users paged past {_IOS_FLEET_IDS_COGNITO_MAX_USERS} "
                    "users with PaginationToken remaining; cannot safely determine iOS fleets."
                )
            break
        if not tok:
            break
        list_kwargs["PaginationToken"] = tok
    # Also include the hardcoded set as a safety net
    return frozenset(fleet_ids) | _IOS_PERSONA_FLEET_IDS, ignored


def _near_cost_schedule() -> tuple[bool, str]:
    """Return (too_close, reason) if within ±_COST_SCHEDULE_MARGIN_MINUTES of 04:00 UTC.

    NW1: the 04:00 UTC scheduled cost run would be a second concurrent run.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    schedule = now.replace(hour=_COST_SCHEDULE_UTC_HOUR, minute=0, second=0, microsecond=0)
    # Check both today's 04:00 and yesterday's
    for delta_days in (0, -1, 1):
        candidate = schedule + datetime.timedelta(days=delta_days)
        diff_s = abs((now - candidate).total_seconds())
        if diff_s <= _COST_SCHEDULE_MARGIN_MINUTES * 60:
            return True, (
                f"within {_COST_SCHEDULE_MARGIN_MINUTES} min of scheduled "
                f"04:00 UTC cost run (now={now.strftime('%H:%M UTC')})"
            )
    return False, ""


def _check_ios_fleet_intersection(
    athena: Any,
    stage: str,
    region: str,
    workgroup: str,
    output_location: str,
    res: Results,
) -> bool:
    """Check that the managed fleet IDs in the views don't overlap iOS user fleets.

    W7 fix: reads custom:fleetIds of non-livetest Cognito users; FAIL (not NOTE)
    if the check cannot run.  Also checks NW1 schedule proximity.

    Returns True if safe to proceed; records FAIL and returns False otherwise.
    """
    # NW1: refuse to invoke if the scheduled 04:00 UTC run is near
    too_close, reason = _near_cost_schedule()
    if too_close:
        res.check("ios-fleet-schedule", False, reason)
        return False

    # Collect iOS fleet IDs from Cognito
    try:
        ios_fleet_ids, ignored_users = _collect_ios_fleet_ids_from_cognito(region, stage)
    except Exception as exc:  # noqa: BLE001
        res.check(
            "ios-fleet-cognito",
            False,
            f"W7: could not read iOS fleet IDs from Cognito: {exc}. "
            "Cannot safely invoke the agent without knowing which fleets iOS users hold.",
        )
        return False
    if ignored_users:
        res.note(
            "ios-fleet-ignored-users",
            "D4: exempt web-test-user subs skipped from iOS fleet check: "
            f"{[s for s, _ in ignored_users]}",
        )

    # Collect managed fleet IDs from Athena
    try:
        db = _fleet_cost_db(stage)
        sql = f"SELECT DISTINCT fleet_id FROM {db}.{_VIEW_MONTHLY}"
        _, rows = _athena_run(athena, sql, workgroup, output_location)
        managed_fleet_ids = {r[0] for r in rows if r and r[0]}
    except Exception as exc:  # noqa: BLE001
        res.check(
            "ios-fleet-athena",
            False,
            f"W7: could not enumerate managed fleet IDs from Athena: {exc}. "
            "Cannot safely invoke the agent.",
        )
        return False

    unsafe = managed_fleet_ids & ios_fleet_ids
    if unsafe:
        res.check(
            "ios-fleet-intersection",
            False,
            f"managed fleets overlap iOS test user fleets: {sorted(unsafe)}. "
            "Do not invoke the agent against these fleets.",
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Cognito helpers (shared with items 5 and 8)
# ---------------------------------------------------------------------------


def _find_pool(idp: Any, stage: str) -> str:
    pools = idp.list_user_pools(MaxResults=60)["UserPools"]
    for p in pools:
        if p["Name"] == f"cms-{stage}-ui-users":
            return p["Id"]
    raise SystemExit(f"Cognito pool cms-{stage}-ui-users not found")


def _password_client(idp: Any, pool_id: str) -> str:
    clients = idp.list_user_pool_clients(UserPoolId=pool_id, MaxResults=60)["UserPoolClients"]
    for c in clients:
        desc = idp.describe_user_pool_client(UserPoolId=pool_id, ClientId=c["ClientId"])
        if "ALLOW_USER_PASSWORD_AUTH" in (desc["UserPoolClient"].get("ExplicitAuthFlows") or []):
            return c["ClientId"]
    raise SystemExit(f"No app client on {pool_id} allows USER_PASSWORD_AUTH")


# Shared with _install_signal_handlers: True once a SIGTERM/SIGHUP has become SystemExit.
_SIGNAL_STATE: dict[str, bool] = {"raised": False}


@contextlib.contextmanager
def _signals_held() -> Iterator[None]:
    """Hold SIGINT, SIGTERM and SIGHUP while a cleanup block runs, then act on them.

    A signal that arrives inside the block is recorded, not acted on, so the cleanup
    (item 7's env restore and read-back, an ephemeral Cognito user's deletion) always
    finishes. When the block ends, the first held signal takes effect: SIGINT raises
    KeyboardInterrupt and SIGTERM/SIGHUP raise SystemExit(143), so the run stops
    rather than going on to later items. Security review cycle 1, A-W1.
    """
    import signal as _sig  # noqa: PLC0415

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
            _SIGNAL_STATE["raised"] = True
            raise SystemExit(143)


@contextlib.contextmanager
def _ephemeral_user(
    idp: Any,
    pool_id: str,
    client_id: str,
    label: str,
    groups: list[str],
    extra_attrs: dict[str, str],
    res: Results,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Context manager: create, yield (id_token, claims), then delete.

    W3: cleanup failures now mark res.mark_cleanup_failed() rather than just
    printing a WARN, so they contribute to a non-zero exit code.
    """
    username = (
        f"livetest-fi-cost-{label}-{int(time.time())}-{secrets.token_hex(3)}@example.invalid"
    )
    password = f"Lv{secrets.token_urlsafe(18)}!9Qz"
    attrs = [
        {"Name": "email", "Value": username},
        {"Name": "email_verified", "Value": "true"},
    ] + [{"Name": k, "Value": v} for k, v in extra_attrs.items()]
    created = False
    try:
        with _signals_held():  # no window between the create and created = True
            idp.admin_create_user(
                UserPoolId=pool_id, Username=username,
                MessageAction="SUPPRESS", UserAttributes=attrs,
            )
            created = True
        idp.admin_set_user_password(
            UserPoolId=pool_id, Username=username, Password=password, Permanent=True,
        )
        for g in groups:
            idp.admin_add_user_to_group(UserPoolId=pool_id, Username=username, GroupName=g)
        auth = idp.initiate_auth(
            ClientId=client_id, AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": username, "PASSWORD": password},
        )
        token = auth["AuthenticationResult"]["IdToken"]
        import base64  # noqa: PLC0415
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        print(f"  principal {label}: groups={groups} (ephemeral)")
        yield token, claims
    finally:
        with _signals_held():  # A-W1/B-S2: a signal can't cut the deletion short
            if created:
                _delete_ephemeral_user(idp, pool_id, username, label, res)


def _delete_ephemeral_user(idp: Any, pool_id: str, username: str, label: str, res: Results) -> None:
    """Delete one ephemeral user and confirm it is gone; any failure marks cleanup failed."""
    try:
        idp.admin_delete_user(UserPoolId=pool_id, Username=username)
        try:
            idp.admin_get_user(UserPoolId=pool_id, Username=username)
            print(
                f"  cleanup WARN: {username} still exists after delete!",
                file=sys.stderr,
            )
            res.mark_cleanup_failed()
        except idp.exceptions.UserNotFoundException:
            print(f"  cleanup: deleted + confirmed absent ({label})")
        except Exception:  # noqa: BLE001
            print(f"  cleanup: deleted {label} (confirmation check failed — verify manually)")
    except Exception as exc:  # noqa: BLE001
        print(
            f"  cleanup FAILED for {username}: {type(exc).__name__}. "
            f"Run: aws cognito-idp admin-delete-user "
            f"--user-pool-id {pool_id} --username {username!r}",
            file=sys.stderr,
        )
        res.mark_cleanup_failed()  # W3: failed cleanup fails the run


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------


class _Http:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def call(
        self,
        method: str,
        path: str,
        token: str,
        body: Any = None,
        timeout: int = 45,
    ) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.base + path, method=method, data=data,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw) if raw else {}
            except ValueError:
                return exc.code, {"raw": raw[:200].decode(errors="replace")}


# ---------------------------------------------------------------------------
# Item 1 — views return rows for managed fleets
# ---------------------------------------------------------------------------


def item1_views_rows(
    athena: Any,
    ddb: Any,
    workgroup: str,
    output_location: str,
    stage: str,
    res: Results,
) -> None:
    """Item 1: both views return rows for every managed fleet; miles check vs CMS.

    C7 fixes:
    - Database is adp_{stage}_fleet_cost, not the bare fleet_cost constant
    - Column is 'miles' (the view has 'miles', not 'total_miles_driven')
    - CMS trips query uses epoch-ms BETWEEN (startTime is NUMBER, not STRING)
    - Trip field is totalDistance (km), converted to miles via _KM_PER_MILE
    - Sample is per-fleet, not first 3 rows overall
    - Both views are checked per fleet_id
    """
    db = _fleet_cost_db(stage)
    print(f"\nItem 1: Athena views return rows for managed fleets (db={db})")

    # 1a. Basic count check for both views
    for view in (_VIEW_MONTHLY, _VIEW_BASELINE):
        sql = f"SELECT COUNT(*) FROM {db}.{view}"
        try:
            execution, rows = _athena_run(athena, sql, workgroup, output_location)
            count = int(rows[0][0]) if rows else 0
            scanned = execution.get("Statistics", {}).get("DataScannedInBytes", 0)
            res.check(
                f"1-{view}-count",
                count > 0,
                f"COUNT(*)={count}, DataScannedInBytes={scanned:,}",
            )
        except Exception as exc:  # noqa: BLE001
            res.check(f"1-{view}-count", False, f"query failed: {type(exc).__name__}: {exc}")

    # 1b. Miles column (correct name: 'miles', not 'total_miles_driven')
    sql_miles = (
        f"SELECT COUNT(*) FROM {db}.{_VIEW_MONTHLY} "
        f"WHERE miles > 0"
    )
    try:
        _, rows = _athena_run(athena, sql_miles, workgroup, output_location)
        count = int(rows[0][0]) if rows else 0
        res.check("1-miles-nonzero", count > 0, f"rows with miles > 0: {count}")
    except Exception as exc:  # noqa: BLE001
        res.check("1-miles-nonzero", False, str(exc))

    # 1c. Per-fleet coverage: compare against managed fleet list from fleet_enrollment
    sql_monthly_fleets = (
        f"SELECT DISTINCT fleet_id FROM {db}.{_VIEW_MONTHLY} ORDER BY fleet_id"
    )
    sql_baseline_fleets = (
        f"SELECT DISTINCT fleet_id FROM {db}.{_VIEW_BASELINE} ORDER BY fleet_id"
    )
    # Also get the enrolled fleet list from ADP cms_ingest (C7)
    sql_enrolled_fleets = (
        f"SELECT DISTINCT fleet_id FROM adp_{stage}_cms_ingest.fleet_enrollment"
    )
    try:
        _, mrows = _athena_run(athena, sql_monthly_fleets, workgroup, output_location)
        _, brows = _athena_run(athena, sql_baseline_fleets, workgroup, output_location)
        monthly_fleets = {r[0] for r in mrows if r and r[0]}
        baseline_fleets = {r[0] for r in brows if r and r[0]}
        missing_from_baseline = monthly_fleets - baseline_fleets
        res.check(
            "1-per-fleet-both-views",
            len(monthly_fleets) >= 1 and not missing_from_baseline,
            f"monthly_fleets={len(monthly_fleets)} baseline_fleets={len(baseline_fleets)} "
            + (f"missing_from_baseline={sorted(missing_from_baseline)}" if missing_from_baseline else "all match"),
        )
        res.note(
            "1-fleet-list",
            f"managed fleets in monthly view: {sorted(monthly_fleets)}",
        )
        # C7: compare with enrolled fleet list
        try:
            _, enrolled_rows = _athena_run(athena, sql_enrolled_fleets, workgroup, output_location)
            enrolled_fleets = {r[0] for r in enrolled_rows if r and r[0]}
            missing_from_views = enrolled_fleets - monthly_fleets
            extra_in_views = monthly_fleets - enrolled_fleets
            res.check(
                "1-enrolled-fleet-coverage",
                not missing_from_views,
                f"enrolled={len(enrolled_fleets)} monthly={len(monthly_fleets)} "
                + (f"missing_from_views={sorted(missing_from_views)}" if missing_from_views else "all enrolled fleets present")
                + (f" extra_in_views={sorted(extra_in_views)}" if extra_in_views else ""),
            )
        except Exception as exc:  # noqa: BLE001
            res.skip("1-enrolled-fleet-coverage", f"could not query fleet_enrollment: {exc}")
    except Exception as exc:  # noqa: BLE001
        res.check("1-per-fleet-both-views", False, str(exc))
        monthly_fleets = set()

    # 1d. CMS trips comparison — NOTE-only (D2).
    # Scans cms-{stage}-storage-trips; groups by (vehicleId, YYYY-MM) summing km for
    # 0 < km <= 1000; resolves vehicleId → vin; compares (vin, month) pairs that are
    # also present in vehicle_cost_monthly.  No FAIL or SKIP from this path.
    trip_totals: dict[tuple[str, str], float] = {}  # (vehicleId, YYYY-MM) → km
    try:
        trips_tbl = ddb.Table(f"cms-{stage}-storage-trips")
        scan_kw: dict[str, Any] = {}
        while True:
            page = trips_tbl.scan(**scan_kw)
            for it in page.get("Items", []):
                vid = it.get("vehicleId")
                st_ms = it.get("startTime")
                td = it.get("totalDistance") or it.get("distance")
                if not vid or st_ms is None or td is None:
                    continue
                try:
                    km = float(td)
                except (TypeError, ValueError):
                    continue
                if not (0 < km <= 1000):
                    continue
                try:
                    st_dt = datetime.datetime.fromtimestamp(
                        float(st_ms) / 1000, tz=datetime.timezone.utc
                    )
                    ym = st_dt.strftime("%Y-%m")
                except Exception:  # noqa: BLE001
                    continue
                key = (str(vid), ym)
                trip_totals[key] = trip_totals.get(key, 0.0) + km
            if "LastEvaluatedKey" not in page:
                break
            scan_kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    except Exception as exc:  # noqa: BLE001
        res.note("1-trips-scan-note", f"CMS trips scan: {exc}")

    vid_vin: dict[str, str] = {}
    if trip_totals:
        vehicles_tbl = ddb.Table(f"cms-{stage}-storage-vehicles")
        for vid, _ in trip_totals:
            if vid in vid_vin:
                continue
            try:
                item = vehicles_tbl.get_item(Key={"vehicleId": vid}).get("Item") or {}
                vin_val = item.get("vin")
                if vin_val:
                    vid_vin[vid] = str(vin_val)
            except Exception:  # noqa: BLE001
                pass

    vin_ym_cms: dict[tuple[str, str], float] = {}
    for (vid, ym), km in trip_totals.items():
        vin = vid_vin.get(vid)
        if vin:
            key2 = (vin, ym)
            vin_ym_cms[key2] = vin_ym_cms.get(key2, 0.0) + km / _KM_PER_MILE

    pairs_compared = 0
    # B-S5: only VIN-shaped values go into the SQL text; others get a NOTE.
    bad_vins = sorted({v for v, _ in vin_ym_cms if not _VIN_RE.fullmatch(v)})
    if bad_vins:
        res.note("1-miles-bad-vin", f"{len(bad_vins)} CMS VIN(s) not VIN-shaped, left out: {bad_vins[:5]}")
        vin_ym_cms = {k: v for k, v in vin_ym_cms.items() if _VIN_RE.fullmatch(k[0])}
    if vin_ym_cms:
        vins_str = "', '".join(sorted({v for v, _ in vin_ym_cms})[:50])
        sql_adp = (
            f"SELECT vin, year_month, miles FROM {db}.{_VIEW_MONTHLY} "
            f"WHERE vin IN ('{vins_str}')"
        )
        adp_map: dict[tuple[str, str], float] = {}
        try:
            _, adp_rows = _athena_run(athena, sql_adp, workgroup, output_location)
            for row in adp_rows:
                if row and len(row) >= 3 and row[0] and row[1]:
                    try:
                        adp_map[(row[0], row[1])] = float(row[2] or 0)
                    except (TypeError, ValueError):
                        pass
        except Exception:  # noqa: BLE001
            pass
        for (vin, ym), cms_mi in vin_ym_cms.items():
            adp_mi = adp_map.get((vin, ym))
            if adp_mi is None:
                continue
            ratio = cms_mi / adp_mi if adp_mi > 0 else None
            pairs_compared += 1
            res.note(
                f"1-trips-{vin}-{ym}",
                f"ADP miles={adp_mi:.1f} CMS miles={cms_mi:.1f} "
                + (f"ratio={ratio:.2f}" if ratio is not None else "adp_miles=0"),
            )

    res.note(
        "1-trips-summary",
        f"CMS trips vs ADP: {pairs_compared} pairs compared. "
        "not pass/fail; meaningful after CMS to ADP stage 2 backfill",
    )


# ---------------------------------------------------------------------------
# Item 2 — agent run scores >= 80% of planted anomalies
# ---------------------------------------------------------------------------


def item2_agent_scores_plants(
    lam: Any,
    ddb: Any,
    s3: Any,
    athena: Any,
    findings_table: str,
    runs_table: str,
    function_name: str,
    bucket: str,
    stage: str,
    region: str,
    workgroup: str,
    output_location: str,
    res: Results,
    one_run_cap: int | None = None,
) -> tuple[str | None, bool]:
    """Item 2: invoke agent (--allow-writes required), score against answer key.

    Returns (data_month, invoke_ok).  data_month is from the run stats (for item 6
    score filter, W6); invoke_ok is False when any Lambda invoke raised an exception
    (R3-W1: main() uses this to stop item 3 and item 7 without relying on label text).

    C2 fix: reads deferred/model_calls from summary["cost"], not top level.
    W6 fix: score filtered to data_month and non-expired Findings.
    W7 fix: pre-invoke fleet safety check.
    R3-W1 fix: returns (data_month, invoke_ok); main() uses invoke_ok directly.
    """
    print("\nItem 2: agent run scores planted anomalies")
    # W7: check no managed fleet overlaps iOS persona fleets before invoking
    if not _check_ios_fleet_intersection(athena, stage, region, workgroup, output_location, res):
        return None, False

    try:
        answer_key_rows, anchor_month = _read_answer_key(s3, bucket, stage)
    except Exception as exc:  # noqa: BLE001
        res.check("2-answer-key", False, f"could not read answer key: {exc}")
        return None, True

    res.check(
        "2-answer-key",
        len(answer_key_rows) >= 1,
        f"answer key: {len(answer_key_rows)} planted VINs, anchor_month={anchor_month}",
    )
    if not answer_key_rows:
        res.note("2-score", "skipped — answer key is empty")
        return None, True

    if _model_override_in_place(lam, function_name, res, "2-model-id"):
        return None, False
    payload_base: dict[str, Any] = {"stage": stage, "component": _COST_RUN_COMPONENT}
    if one_run_cap is not None:
        payload_base["max_diagnoses_per_run"] = one_run_cap
        max_runs = 1
    else:
        max_runs = _MAX_REPEAT_RUNS

    run_count = 0
    last_deferred = None
    data_month: str | None = None
    for attempt in range(max_runs):
        run_count += 1
        print(f"  invoking run {run_count}…")
        # R3-W1: check schedule proximity before every invoke in the loop,
        # not just once before the loop.  A start at 02:58 UTC can still be
        # running at 04:00 after a previous _near_cost_schedule pass.
        too_close, schedule_reason = _near_cost_schedule()
        if too_close:
            res.check(f"2-run-{run_count}-schedule", False, schedule_reason)
            return data_month, False
        
        try:
            result = _wait_lambda_response(lam, function_name, payload_base)
        except Exception as exc:  # noqa: BLE001
            res.check(f"2-run-{run_count}", False, f"Lambda invoke failed: {exc}")
            return data_month, False

        # C2: read from summary["cost"] subdict
        cost, cost_ok = _extract_cost_stats(result)
        if not cost_ok:
            res.check(f"2-run-{run_count}-cost-key", False,
                      f"run_cost returned no 'cost' subdict; top-level keys={list(result.keys())[:10]}")
            return data_month, True

        deferred = int(cost.get("deferred", 0))
        model_calls = int(cost.get("model_calls", 0))
        tokens = int(result.get("tokens_used", 0))  # top-level
        data_month = cost.get("data_month") or data_month
        last_deferred = deferred
        res.note(
            f"2-run-{run_count}",
            f"deferred={deferred} model_calls={model_calls} tokens_used={tokens} "
            f"data_month={data_month}",
        )
        if deferred == 0:
            break
        if attempt < max_runs - 1:
            print(f"  deferred={deferred}, repeating…")

    res.check(
        "2-convergence",
        last_deferred == 0,
        f"after {run_count} run(s), deferred={last_deferred}",
    )

    # W6: filter Findings to this data_month and non-expired, not all Findings ever.
    # AVX Findings carry expires_at (ISO string); the table's TTL is DISABLED.
    all_findings = _scan_findings_by_fleet_all_fleets(ddb, findings_table)
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if data_month:
        scored_findings = [
            f for f in all_findings
            if (f.get("recommended_action") or {}).get("parameters", {}).get("month") == data_month
            or not (f.get("recommended_action") or {}).get("parameters", {}).get("month")
        ]
        # Exclude expired Findings using expires_at (ISO string), not ttl
        scored_findings = [
            f for f in scored_findings
            if not f.get("expires_at") or str(f.get("expires_at", "")) > now_iso
        ]
    else:
        scored_findings = all_findings

    text_hits: list[tuple[str, str, str]] = []
    flagged, right_cause_action, no_action, no_action_right_cause, correct, total, pct = \
        score_plants_detail(answer_key_rows, scored_findings, text_hits_out=text_hits)
    for _vin, _phrase, _headline in text_hits:  # R4-W5: auditable in decisions.md
        res.note(f"2-text-inferred-hit-{_vin}", f"phrase={_phrase!r} headline={_headline!r}")
    res.check(
        "2-score",
        pct >= _SCORE_THRESHOLD_PCT,
        f"{correct}/{total} planted anomalies diagnosed correctly "
        f"({pct:.0%} >= {_SCORE_THRESHOLD_PCT:.0%})"
        + (f" [data_month={data_month}]" if data_month else ""),
    )
    res.note("2-score-detail", f"anchor_month={anchor_month} data_month={data_month}")
    res.note(
        "2-score-counts",
        f"flagged={flagged} right_cause_action={right_cause_action} "
        f"no_action={no_action} no_action_right_cause={no_action_right_cause} "
        f"correct={correct} total={total} pct={pct:.0%}",
    )
    return data_month, True


def _scan_findings_by_fleet_all_fleets(
    ddb: Any,
    table_name: str,
) -> list[dict[str, Any]]:
    """Scan the findings table for all cost-agent items (full scan)."""
    from boto3.dynamodb.conditions import Attr  # noqa: PLC0415

    table = ddb.Table(table_name)
    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {
        "FilterExpression": Attr("agent_id").eq(_AGENT_ID),
    }
    while True:
        page = table.scan(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return items


# ---------------------------------------------------------------------------
# Item 3 — second run adds zero Findings, makes zero model calls
# ---------------------------------------------------------------------------


def item3_second_run_noop(
    lam: Any,
    ddb: Any,
    athena: Any,
    findings_table: str,
    runs_table: str,
    function_name: str,
    stage: str,
    region: str,
    workgroup: str,
    output_location: str,
    res: Results,
) -> bool:
    """Item 3: second run after convergence writes nothing, spends no tokens.

    C2 fix: reads wrote/model_calls/deferred from summary["cost"], not top level.
    W7 fix: pre-invoke fleet safety check.
    NW2 fix: FAIL (not NOTE) when run record is missing or older than invoke.
    Returns False if the invoke itself failed (so main() can stop later invoking items).
    """
    print("\nItem 3: second run is a no-op (defers=0, tokens_used=0)")
    # W7: check no managed fleet overlaps iOS persona fleets before invoking
    if not _check_ios_fleet_intersection(athena, stage, region, workgroup, output_location, res):
        return False

    if _model_override_in_place(lam, function_name, res, "3-model-id"):
        return False
    payload = {"stage": stage, "component": _COST_RUN_COMPONENT}
    # NW2: take second-precision timestamp BEFORE invoke
    invoke_time = time.time()
    try:
        result = _wait_lambda_response(lam, function_name, payload)
    except Exception as exc:  # noqa: BLE001
        res.check("3-invoke", False, f"Lambda invoke failed: {exc}")
        return False

    # C2: read from summary["cost"] subdict
    cost, cost_ok = _extract_cost_stats(result)
    if not cost_ok:
        res.check("3-cost-key", False,
                  f"run_cost returned no 'cost' subdict; keys={list(result.keys())[:10]}")
        return True

    deferred = int(cost.get("deferred", 0))
    tokens = int(result.get("tokens_used", 0))  # top-level
    model_calls = int(cost.get("model_calls", 0))
    # "wrote" = anomalies written + sell timing written (from cost subdict)
    wrote = int(cost.get("anomalies_written", 0)) + int(cost.get("sell_timing_written", 0))

    res.check(
        "3-no-new-findings",
        wrote == 0,
        f"anomalies_written={cost.get('anomalies_written', 0)} "
        f"sell_timing_written={cost.get('sell_timing_written', 0)}",
    )
    res.check(
        "3-no-model-calls",
        tokens == 0 and model_calls == 0,
        f"tokens_used={tokens} model_calls={model_calls}",
    )
    res.check(
        "3-deferred-zero",
        deferred == 0,
        f"deferred={deferred}",
    )

    # NW2: FAIL when the run record is missing or older than invoke_iso.
    # The record is the only authoritative source for what the run actually did.
    rec = _latest_run_record(ddb, runs_table, _RUNS_COMPONENT)
    if rec:
        rec_started = str(rec.get("started_at", ""))
        # started_at is ISO 8601 with milliseconds since 2026-10-27 (...:01.881Z); the bound below
        # has no "Z", so it compares as a prefix and a run in the bound's own second still counts.
        # NW2+skew: accept a run record started up to 5 s before local invoke time
        # to tolerate clock drift between this host and Lambda.
        invoke_minus_5s = datetime.datetime.fromtimestamp(
            invoke_time - 5, tz=datetime.timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%S")
        is_this_run = rec_started >= invoke_minus_5s
        if is_this_run:
            rr_tokens = int(rec.get("tokens_used", 0))
            rr_wrote = int(rec.get("artifacts_written", 0))
            res.check(
                "3-run-record-tokens",
                rr_tokens == 0,
                f"run record tokens_used={rr_tokens} started_at={rec_started}",
            )
            res.check(
                "3-run-record-artifacts",
                rr_wrote == 0,
                f"run record artifacts_written={rr_wrote}",
            )
        else:
            # NW2: a record that predates the invoke cannot confirm this run
            res.check(
                "3-run-record-present",
                False,
                f"latest run record predates this invoke "
                f"(started_at={rec_started} < invoke-5s={invoke_minus_5s}). "
                "The run record is the Accept criterion; an older record cannot confirm it.",
            )
    else:
        # NW2: missing run record → FAIL
        res.check(
            "3-run-record-present",
            False,
            "no run record found; _emit_run_record may have failed. "
            "The run record is the Accept criterion for item 3.",
        )
    return True


# ---------------------------------------------------------------------------
# Item 4 — paging completeness and DataScannedInBytes
# ---------------------------------------------------------------------------


def item4_athena_paging(
    athena: Any,
    ddb: Any,
    workgroup: str,
    output_location: str,
    stage: str,
    runs_table: str,
    res: Results,
) -> None:
    """Item 4: agent readers page to completion; Athena cost < $5/mo.

    R3-W5/W6 fix: invoke the agent's own readers through _CapturingAthena so
    DataScannedInBytes is measured on real agent queries, not COUNT wrappers.
    """
    import datetime
    import math
    from agents.fi_cost.evidence import _VIN_RE, gather_evidence
    from agents.fi_cost.selection import (
        _render_sql as _render_anomalies,
        find_latest_complete_month,
        select_anomalies,
    )
    from agents.fi_cost.sell_timing import _INPUTS_SQL_TEMPLATE, fetch_sell_timing_inputs

    db = _fleet_cost_db(stage)
    print(f"\nItem 4: agent-reader paging and Athena cost (db={db})")
    rec = _latest_run_record(ddb, runs_table, _RUNS_COMPONENT)
    if not rec:
        res.skip("4-run-record", "no run record; run item 2 first")
        return
    data_month = rec.get("data_month")
    vins_selected_rec = rec.get("vins_selected")
    if not data_month:
        res.skip("4-data-month", "no data_month in run record; run item 2 first")
        return
    if not _YEAR_MONTH_RE.fullmatch(str(data_month)):  # B-S5: it goes into SQL text below
        res.check("4-data-month", False, f"run record data_month {data_month!r} is not YYYY-MM")
        return
    res.note("4-run-record", f"data_month={data_month} vins_selected={vins_selected_rec}")

    cap = _CapturingAthena(athena)
    try:
        find_latest_complete_month(
            cap, workgroup, output_location,
            now=datetime.datetime.now(datetime.timezone.utc), stage=stage,
        )
        anomaly_rows = select_anomalies(
            athena_client=cap, workgroup=workgroup, results_s3_path=output_location,
            max_diagnoses_per_run=99999, latest_complete_month=data_month, stage=stage,
        )
    except Exception as exc:  # noqa: BLE001
        res.check("4-reader-anomalies", False, f"anomaly reader failed: {exc}")
        return

    # Distinct valid VINs, first-seen order: the agent's own count (agent.py `seen` set).
    valid_vins = list(dict.fromkeys(
        r.get("vin") for r in anomaly_rows
        if isinstance(r.get("vin"), str) and _VIN_RE.match(r["vin"])
    ))
    if valid_vins:
        try:
            gather_evidence(
                valid_vins[:50], data_month, cap, workgroup, output_location, stage=stage,
            )
        except Exception as exc:  # noqa: BLE001
            res.note("4-gather-evidence-warn", f"gather_evidence: {exc}")

    try:
        sell_rows = fetch_sell_timing_inputs(
            cap, workgroup, output_location, year_month=data_month, stage=stage,
        )
    except Exception as exc:  # noqa: BLE001
        res.check("4-reader-sell-timing", False, f"sell-timing reader failed: {exc}")
        return

    # COUNT wrappers — plain athena (not cap) so their IDs don't enter the bytes tally
    a_sql = _render_anomalies(stage=stage, year_month=data_month)
    try:
        _, a_cnt = _athena_run(
            athena, f"SELECT COUNT(*) FROM ({a_sql.rstrip(';')})", workgroup, output_location,
        )
        count_anomalies = int(a_cnt[0][0]) if a_cnt else 0
    except Exception as exc:  # noqa: BLE001
        res.check("4-reader-anomalies", False, f"anomalies COUNT(*) failed: {exc}")
        return
    res.check(
        "4-reader-anomalies",
        len(anomaly_rows) == count_anomalies and count_anomalies > 0,
        f"reader_rows={len(anomaly_rows)} COUNT(*)={count_anomalies} (must be equal and > 0)",
    )

    s_sql = _INPUTS_SQL_TEMPLATE.replace("${stage}", stage).replace("${year_month}", data_month)
    try:
        _, s_cnt = _athena_run(
            athena, f"SELECT COUNT(*) FROM ({s_sql.rstrip(';')})", workgroup, output_location,
        )
        count_sell = int(s_cnt[0][0]) if s_cnt else 0
    except Exception as exc:  # noqa: BLE001
        res.check("4-reader-sell-timing", False, f"sell-timing COUNT(*) failed: {exc}")
        return
    pages_est = math.ceil(count_sell / 1000) if count_sell > 0 else 0
    res.check(
        "4-reader-sell-timing",
        len(sell_rows) == count_sell and count_sell > 0,
        f"reader_rows={len(sell_rows)} COUNT(*)={count_sell} ~{pages_est} page(s) of 1000",
    )

    if vins_selected_rec is not None:
        res.check(
            "4-agent-paging",
            len(valid_vins) == int(vins_selected_rec),
            f"reader valid_vins={len(valid_vins)} run_record vins_selected={vins_selected_rec}",
        )
    else:
        res.skip("4-agent-paging", "run record missing vins_selected")

    # Bytes: per-run = captured agent executions only (COUNT wrappers excluded)
    run_bytes = _athena_bytes(athena, cap.execution_ids)
    try:
        v_monthly = _athena_bytes_only(
            athena, f"SELECT * FROM {db}.{_VIEW_MONTHLY}", workgroup, output_location,
        )
        v_baseline = _athena_bytes_only(
            athena, f"SELECT * FROM {db}.{_VIEW_BASELINE}", workgroup, output_location,
        )
    except Exception as exc:  # noqa: BLE001
        # Without the views' bytes the estimate would understate: fail, don't guess.
        res.check("4-athena-cost-under-5", False, f"view bytes unavailable: {exc}")
        return

    TB = 10 ** 12  # Athena bills $5 per TB scanned; decimal TB is the conservative reading
    estimate_usd = (run_bytes + v_monthly + v_baseline) / TB * 5.0 * 30
    res.note(
        "4-bytes-breakdown",
        f"per-run={run_bytes:,}B monthly-view={v_monthly:,}B baseline-view={v_baseline:,}B"
        f" → ~${estimate_usd:.2f}/mo (view figure is upper bound for 6.1's read;"
        f" measured exactly in 7.1)",
    )
    res.check(
        "4-athena-cost-under-5",
        estimate_usd < 5.0,
        f"estimate=${estimate_usd:.2f}/mo at $5/TB × 30 runs/mo",
    )


# ---------------------------------------------------------------------------
# Item 5 — fleet A operator sees zero fleet B cost Findings
# ---------------------------------------------------------------------------


def item5_fleet_scope(
    ddb: Any,
    idp: Any,
    findings_table: str,
    pool_id: str,
    client_id: str,
    api: _Http,
    stage: str,
    res: Results,
    allow_writes: bool = False,
) -> None:
    """Item 5: fleet-scoped read isolation for cost Findings.

    C4 fix: collect fleet B's finding_ids from DDB by_fleet GSI and check
    none appear in fleet A's paged response.  Require ≥1 fleet A cost Finding.

    Creates one ephemeral Cognito user (fleet-operator group).
    Requires --allow-writes.
    """
    print("\nItem 5: fleet A operator sees zero fleet B cost Findings")

    if not allow_writes:
        res.skip(
            "5-scope",
            "item 5 creates ephemeral Cognito users; pass --allow-writes to enable.",
        )
        return

    # Discover two managed demo fleets excluding iOS persona fleets
    fleet_ids_raw = _all_fleet_ids_for_agent(ddb, findings_table)
    fleet_ids = sorted(fid for fid in fleet_ids_raw if fid not in _IOS_PERSONA_FLEET_IDS)
    if len(fleet_ids) < 2:
        excluded = fleet_ids_raw - set(fleet_ids)
        res.skip(
            "5-isolation",
            f"only {len(fleet_ids)} managed demo fleet(s) with cost Findings "
            f"(excluded {len(excluded)} iOS persona fleet(s)); need >= 2. "
            "Run items 2-3 first.",
        )
        return
    fleet_a, fleet_b = fleet_ids[0], fleet_ids[1]
    res.note("5-fleets", f"fleet_a={fleet_a!r}  fleet_b={fleet_b!r}")

    # C4: collect fleet B's finding_ids from DDB by_fleet GSI
    fleet_b_finding_ids = _all_finding_ids_for_fleet(ddb, findings_table, fleet_b)
    res.note("5-fleet-b-ddb", f"fleet B has {len(fleet_b_finding_ids)} finding_id(s) in by_fleet GSI")

    with _ephemeral_user(
        idp, pool_id, client_id, "op-a",
        groups=["fleet-operator"],
        extra_attrs={"custom:fleetIds": fleet_a},
        res=res,
    ) as (tok_a, _):
        # Fleet B must return 403 for fleet A operator
        s, body = api.call("GET", f"/findings/fleet/{fleet_b}", tok_a)
        res.check(
            "5-fleet-b-403",
            s == 403,
            f"GET /findings/fleet/{fleet_b!r} as fleet A → {s}",
        )

        # Fleet A must return 200 with ≥1 cost Finding
        # Page all results to catch any fleet B finding_id hiding on page 2+
        # N1 fix: handler reads 'next_token' (not 'nextToken'); URL-encode the token;
        # cap at _MAX_FINDINGS_PAGES to prevent infinite loop.
        all_fleet_a_findings: list[dict[str, Any]] = []
        page_url: str | None = f"/findings/fleet/{fleet_a}"
        page_count = 0
        fleet_a_200 = False
        while page_url and page_count < _MAX_FINDINGS_PAGES:
            s, body = api.call("GET", page_url, tok_a)
            page_count += 1
            if s != 200:
                res.check("5-fleet-a-200", False, f"GET {page_url} → {s} (page {page_count})")
                fleet_a_200 = False
                break
            fleet_a_200 = True
            findings_page = (body.get("findings") or []) if isinstance(body, dict) else []
            all_fleet_a_findings.extend(findings_page)
            # N1: handler reads 'next_token'; URL-encode so base64 '+' and '/' survive
            next_token = body.get("next_token") if isinstance(body, dict) else None
            if next_token:
                page_url = (
                    f"/findings/fleet/{fleet_a}"
                    f"?next_token={urllib.parse.quote(str(next_token), safe='')}"
                )
            else:
                page_url = None
        if fleet_a_200:
            res.check("5-fleet-a-200", True, f"→ 200, {page_count} page(s)")
        if page_count >= _MAX_FINDINGS_PAGES and page_url is not None:
            res.check("5-fleet-a-paging-cap",
                      False,
                      f"reached {_MAX_FINDINGS_PAGES}-page cap — fleet A paging did not complete. "
                      "Check the next_token loop.")

        cost_findings_a = [f for f in all_fleet_a_findings if f.get("agent_id") == _AGENT_ID]
        res.check(
            "5-fleet-a-has-findings",
            len(cost_findings_a) >= 1,
            f"fleet A cost Findings visible: {len(cost_findings_a)}",
        )

        # C4: check no fleet B finding_id appears in fleet A's response
        response_finding_ids = {str(f.get("finding_id", "")) for f in all_fleet_a_findings if f.get("finding_id")}
        leaked = fleet_b_finding_ids & response_finding_ids
        res.check(
            "5-fleet-b-not-visible",
            not leaked,
            f"fleet B finding_ids in fleet A response: {len(leaked)} "
            + (f"({sorted(leaked)[:3]})" if leaked else "(none — correct)"),
        )


# ---------------------------------------------------------------------------
# Item 6 — forced alarm failure reaches subscriber
# ---------------------------------------------------------------------------


def item6_forced_alarm_failure(
    lam: Any,
    cw: Any,
    sns: Any,
    function_name: str,
    stage: str,
    region: str,
    res: Results,
    wait_s: int = 360,
) -> None:
    """Item 6: a real Lambda error triggers the cost run-failure alarm via Errors metric.

    W4 fix: SKIP if alarm starts in ALARM (can't verify the trigger path).
    FAIL when action history is missing after invoke.
    Require StateUpdatedTimestamp >= invoke time.
    Default wait_s raised to 360 (alarm period is 300s; 360s gives one evaluation).
    """
    print("\nItem 6: forced Lambda error triggers run-failure alarm via real Errors metric")
    # A-W2: with --item 6 alone, item 7's own checks never run; a killed earlier item 7
    # can have left the nonexistent model id in place. Don't invoke the Lambda then.
    if _model_override_in_place(lam, function_name, res, "6-model-id"):
        return
    alarm_name = f"vsa-{stage}-tier2-{_COST_ALARM_SUFFIX}-{region}"

    # Read alarm config
    alarms = cw.describe_alarms(AlarmNames=[alarm_name]).get("MetricAlarms", [])
    if not alarms:
        res.check("6-alarm-exists", False, f"alarm {alarm_name!r} not found")
        return
    alarm = alarms[0]
    original_state = alarm["StateValue"]
    actions = alarm.get("AlarmActions") or []
    res.check(
        "6-alarm-exists",
        True,
        f"alarm found, state={original_state}, AlarmActions={len(actions)}",
    )

    # W4: SKIP if alarm is already in ALARM — cannot verify the trigger path
    if original_state == "ALARM":
        res.skip(
            "6-alarm-triggered",
            "alarm is already in ALARM state before invoke; "
            "cannot verify this run triggered it. Reset the alarm and re-run.",
        )
        return

    if not actions:
        res.check("6-alarm-actions", False, "alarm has no AlarmActions — cannot deliver")
        return

    if not any(":sns:" in a for a in actions):
        # Only an SNS action has a subscriber this item can check.
        res.check("6-alarm-actions", False, f"no SNS topic among AlarmActions {actions}")
        return

    # W4: check each topic has ≥1 confirmed subscriber
    for topic_arn in actions:
        if ":sns:" not in topic_arn:
            continue
        try:
            subs = sns.list_subscriptions_by_topic(TopicArn=topic_arn).get("Subscriptions", [])
            confirmed = [s for s in subs if s.get("SubscriptionArn") not in ("PendingConfirmation", "Deleted")]
            res.check(
                f"6-sns-subscriber-{topic_arn.rsplit(':', 1)[-1]}",
                len(confirmed) >= 1,
                f"topic {topic_arn.rsplit(':', 1)[-1]!r}: {len(confirmed)} confirmed subscriber(s)",
            )
        except Exception as exc:  # noqa: BLE001
            res.check(
                "6-sns-check",
                False,
                f"could not check topic {topic_arn.rsplit(':', 1)[-1]!r}: {exc}",
            )

    # W4: invoke with malformed max_diagnoses_per_run to trigger a real Lambda error
    invoke_time_utc = datetime.datetime.now(datetime.timezone.utc)
    payload = {"stage": stage, "component": _COST_RUN_COMPONENT, "max_diagnoses_per_run": "not-a-number"}
    print("  invoking with malformed max_diagnoses_per_run to trigger real Lambda error…")
    try:
        _wait_lambda_response(lam, function_name, payload)
        res.check("6-lambda-error", False, "Lambda did not raise on malformed max_diagnoses_per_run — unexpected")
    except RuntimeError as exc:
        error_str = str(exc)
        res.check(
            "6-lambda-error",
            "max_diagnoses_per_run must be an integer" in error_str,
            f"Lambda raised with expected message: {error_str[:200]}",
        )
    except Exception as exc:  # noqa: BLE001
        res.check("6-lambda-error", False, f"unexpected exception: {type(exc).__name__}: {exc}")

    # Wait for the alarm to transition to ALARM via real CloudWatch Errors metric
    print(f"  waiting up to {wait_s}s for alarm {alarm_name!r} to go to ALARM…")
    deadline = time.monotonic() + wait_s
    alarm_reached = False
    while time.monotonic() < deadline:
        cur = cw.describe_alarms(AlarmNames=[alarm_name]).get("MetricAlarms", [])
        if cur and cur[0]["StateValue"] == "ALARM":
            alarm_reached = True
            break
        time.sleep(5)

    res.check(
        "6-alarm-triggered",
        alarm_reached,
        f"alarm reached ALARM state within {wait_s}s via real Errors metric"
        if alarm_reached
        else f"alarm did not reach ALARM within {wait_s}s (was {original_state})",
    )

    # W4: require StateUpdatedTimestamp >= invoke time
    if alarm_reached:
        cur_alarm = cw.describe_alarms(AlarmNames=[alarm_name]).get("MetricAlarms", [])
        if cur_alarm:
            updated = cur_alarm[0].get("StateUpdatedTimestamp")
            if updated:
                updated_utc = updated if updated.tzinfo else updated.replace(tzinfo=datetime.timezone.utc)
                res.check(
                    "6-alarm-timestamp",
                    updated_utc >= invoke_time_utc,
                    f"StateUpdatedTimestamp={updated_utc.isoformat()} >= invoke={invoke_time_utc.isoformat()}",
                )

    # Read back the alarm history to confirm action fired — poll up to 30s.
    try:
        action_found = False
        _action_deadline = time.monotonic() + 30
        while not action_found and time.monotonic() < _action_deadline:
            history = cw.describe_alarm_history(
                AlarmName=alarm_name,
                HistoryItemType="Action",
                MaxRecords=5,
                ScanBy="TimestampDescending",
            )
            for item in history.get("AlarmHistoryItems", []):
                ts = item.get("Timestamp")
                if ts and ts >= invoke_time_utc:
                    hd = json.loads(item.get("HistoryData", "{}"))
                    action_state = str(hd.get("actionState", "Missing"))
                    res.check(
                        "6-action-delivered",
                        action_state == "Succeeded",
                        f"actionState={action_state}",
                    )
                    action_found = True
                    break
            if not action_found:
                time.sleep(3)
        if not action_found:
            res.check(
                "6-action-delivered",
                False,
                "no Action history entry found after 30s — "
                "action may not have fired or history is delayed",
            )
    except Exception as exc:  # noqa: BLE001
        res.check("6-action-delivered", False, f"could not read action history: {exc}")

    # NOTE the current alarm state — it will return to OK on its own as the
    # Errors metric falls below the threshold; forcing OK via set_alarm_state
    # could notify the subscriber unnecessarily.
    cur_end = cw.describe_alarms(AlarmNames=[alarm_name]).get("MetricAlarms", [])
    end_state = cur_end[0]["StateValue"] if cur_end else "UNKNOWN"
    res.note("6-alarm-end-state", f"alarm {alarm_name!r} end state={end_state} (returns to OK on its own)")


# ---------------------------------------------------------------------------
# Item 7 — model-id override → rules-fallback Findings
# ---------------------------------------------------------------------------


def item7_rules_fallback(
    lam: Any,
    ddb: Any,
    athena: Any,
    findings_table: str,
    function_name: str,
    stage: str,
    region: str,
    workgroup: str,
    output_location: str,
    res: Results,
) -> bool:
    """Item 7: with a nonexistent model ID, the run writes rules-fallback Findings.

    Returns False if its guard (_check_ios_fleet_intersection) refuses or if
    the Lambda invoke raises; True otherwise (D3: caller uses this to gate items 2/3).
    W2 fix: evidence is a list of {"kind": ..., "value": ...} entries;
    match {"kind": "cost.diagnosis_source", "value": "rules"} from diagnose.py:282.
    Limit to Findings written by this run (started_at >= invoke).
    W7 fix: pre-invoke fleet safety check.
    W3 fix: restore failure marks cleanup failed.
    """
    print("\nItem 7: nonexistent model ID → rules-fallback Findings")
    # W7: check no managed fleet overlaps iOS persona fleets before invoking
    if not _check_ios_fleet_intersection(athena, stage, region, workgroup, output_location, res):
        return False

    # Read current Lambda config
    try:
        config = lam.get_function_configuration(FunctionName=function_name)
    except Exception as exc:  # noqa: BLE001
        res.check("7-get-config", False, f"get_function_configuration failed: {exc}")
        return False

    original_env = dict(config.get("Environment", {}).get("Variables") or {})
    original_model = original_env.get("FI_COST_MODEL_ID", "<not set>")
    res.note("7-original-model", f"FI_COST_MODEL_ID={original_model!r}")
    if original_model == _NON_EXISTENT_MODEL:
        # A killed earlier run left the override in place; "restoring" to it would keep it.
        res.check("7-original-model-sane", False,
                  "FI_COST_MODEL_ID is already the nonexistent test model; restore the "
                  "Lambda's env from the deployed stack before running item 7")
        return False

    # Write a 0600 backup so the operator can restore manually if the run is interrupted.
    _restore_dir = tempfile.mkdtemp(prefix="live-verify-fi-cost-")
    _restore_path = os.path.join(_restore_dir, "restore-env.json")
    _fd = os.open(_restore_path, os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(_fd, "w") as _fh:
        json.dump({"Variables": original_env}, _fh, indent=2)
    print(f"  env backup : {_restore_path}")
    print(f"  restore cmd: aws lambda update-function-configuration"
          f" --function-name {function_name} --region {region}"
          f" --environment file://{_restore_path}")

    override_env = build_env_with_override(original_env, {"FI_COST_MODEL_ID": _NON_EXISTENT_MODEL})

    _run_ok = True
    try:
        lam.update_function_configuration(
            FunctionName=function_name,
            Environment={"Variables": override_env},
        )
        print(f"  overrode FI_COST_MODEL_ID → {_NON_EXISTENT_MODEL!r}")
        _wait_for_lambda_active(lam, function_name)

        # Invoke with max_diagnoses_per_run=1 so the run is cheap
        payload = {"stage": stage, "component": _COST_RUN_COMPONENT, "max_diagnoses_per_run": 1}
        invoke_time_monotonic = time.time()
        try:
            result = _wait_lambda_response(lam, function_name, payload)
        except Exception as exc:  # noqa: BLE001
            res.check("7-invoke", False, f"Lambda invoke failed: {exc}")
            return False

        # C2: read from summary["cost"] subdict
        cost, cost_ok = _extract_cost_stats(result)
        if not cost_ok:
            res.check("7-cost-key", False,
                      f"run_cost returned no 'cost' subdict; keys={list(result.keys())[:10]}")
            return False  # an unexpected reply: stop later invoking items

        rules_fallbacks = int(cost.get("rules_fallbacks", 0))
        aborted = result.get("abort_reason") is not None
        res.check(
            "7-rules-fallback",
            rules_fallbacks >= 1 and not aborted,
            f"rules_fallbacks={rules_fallbacks} aborted={aborted}",
        )

        # W2/R3-W2: filter to Findings written by THIS run only.
        # To tolerate a few seconds of clock skew between this host and Lambda,
        # use (invoke_time - 5 s) as the lower bound.
        # diagnosis_source is stored as: {"kind": "cost.diagnosis_source", "value": "rules"}
        # (agents/fi_cost/diagnose.py:282).  evidence is a list, not a dict.
        all_f = _scan_findings_by_fleet_all_fleets(ddb, findings_table)

        def _has_rules_diagnosis(finding: dict[str, Any]) -> bool:
            evidence = finding.get("evidence")
            if not isinstance(evidence, list):
                return False
            return any(
                isinstance(e, dict)
                and e.get("kind") == "cost.diagnosis_source"
                and str(e.get("value", "")).strip().lower() == "rules"
                for e in evidence
            )

        # R3-W2: subtract 5 s from invoke_time to tolerate clock skew, then FAIL
        # (not fall back to all findings) when no this-run Finding is found.
        invoke_minus_5s = datetime.datetime.fromtimestamp(
            invoke_time_monotonic - 5, tz=datetime.timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%S")
        this_run_findings = [
            f for f in all_f
            if str(f.get("computed_at", "")) >= invoke_minus_5s
        ]
        if not this_run_findings:
            res.check(
                "7-run-filter",
                False,
                f"no Findings with computed_at >= {invoke_minus_5s} (invoke time - 5s). "
                "The rules-fallback run wrote no Findings; cannot check diagnosis_source.",
            )
        else:
            rules_findings = [f for f in this_run_findings if _has_rules_diagnosis(f)]
            res.check(
                "7-diagnosis-source-rules",
                len(rules_findings) >= 1,
                f"Findings with cost.diagnosis_source='rules' in evidence list: {len(rules_findings)} "
                f"(from {len(this_run_findings)} this-run Findings)",
            )
    except Exception as exc:  # noqa: BLE001  — Q5: record, restore below, stop later invokes
        res.check("7-run-error", False, f"{type(exc).__name__}: {exc}")
        _run_ok = False
    finally:
        # R4-C1: track restore success so the return value reflects cleanup state.
        # A-W1: SIGINT, SIGTERM and SIGHUP are held for the whole restore and read-back,
        # then acted on (the run stops) once it has finished.
        with _signals_held():
            # R4-W4: retry for at least 120 s (~40 × 3 s) on ResourceConflict.
            # W3+: retry on ResourceConflictException; check FULL Variables map.
            _restore_ok = True
            _update_ok = False
            for _attempt in range(40):
                try:
                    lam.update_function_configuration(
                        FunctionName=function_name,
                        Environment={"Variables": original_env},
                    )
                    _update_ok = True
                    break
                except Exception as _exc:  # noqa: BLE001
                    if _is_resource_conflict(_exc) and _attempt < 39:
                        print(f"  restore attempt {_attempt + 1}: ResourceConflictException, retrying…")
                        time.sleep(3)
                    else:
                        res.check("7-restore", False,
                                  f"env restore failed after {_attempt + 1} attempt(s): {_exc}")
                        res.mark_cleanup_failed()
                        _restore_ok = False
                        break
            if _update_ok:
                try:
                    _wait_for_lambda_active(lam, function_name)
                    cfg2 = lam.get_function_configuration(FunctionName=function_name)
                    restored_vars = dict(cfg2.get("Environment", {}).get("Variables") or {})
                    if restored_vars == original_env:
                        print(f"  restored env ({len(restored_vars)} keys) matches original")
                    else:
                        diff = sorted(
                            k for k in (set(original_env) | set(restored_vars))
                            if original_env.get(k) != restored_vars.get(k)
                        )
                        res.check("7-restore", False,
                                  f"env after restore differs from original; mismatched keys={diff}")
                        res.mark_cleanup_failed()
                        _restore_ok = False
                except Exception as _exc:  # noqa: BLE001
                    res.check("7-restore", False, f"env restore read-back failed: {_exc}")
                    res.mark_cleanup_failed()
                    _restore_ok = False
            if _restore_ok:
                # B-S4: the backup is only needed when the restore didn't verify.
                with contextlib.suppress(OSError):
                    os.remove(_restore_path)
                    os.rmdir(_restore_dir)
            else:
                print(f"  env backup kept for a manual restore: {_restore_path}", file=sys.stderr)
    return _run_ok and _restore_ok  # R4-C1/Q5: False when the run errored or any restore step failed


def _model_override_in_place(lam: Any, function_name: str, res: Results, label: str) -> bool:
    """True (and a FAIL row) when FI_COST_MODEL_ID is the test model or can't be read.

    Items 2 and 3 run this before invoking: with ``--item 2``/``--item 3`` alone, item 7's
    own check never runs, and a killed earlier item 7 can leave the override in place.
    """
    try:
        cfg = lam.get_function_configuration(FunctionName=function_name)
        model = (cfg.get("Environment", {}).get("Variables") or {}).get("FI_COST_MODEL_ID")
    except Exception as exc:  # noqa: BLE001
        res.check(label, False, f"could not read the Lambda's FI_COST_MODEL_ID: {exc}")
        return True
    if model == _NON_EXISTENT_MODEL:
        res.check(label, False, "FI_COST_MODEL_ID is the nonexistent test model; restore it first")
        return True
    return False


def _wait_for_lambda_active(lam: Any, function_name: str, timeout_s: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        cfg = lam.get_function_configuration(FunctionName=function_name)
        status = cfg.get("LastUpdateStatus", "Successful")
        if status == "Successful":
            return
        time.sleep(2)
    raise TimeoutError(f"{function_name} config update did not finish in {timeout_s}s")


# ---------------------------------------------------------------------------
# Item 8 — fleet_recommendation tool via text runtime
# ---------------------------------------------------------------------------


def item8_fleet_recommendation(
    lam: Any,
    ddb: Any,
    idp: Any,
    findings_table: str,
    actions_table: str,
    pool_id: str,
    client_id: str,
    api: _Http,
    function_name: str,
    stage: str,
    region: str,
    res: Results,
    allow_writes: bool = False,
) -> None:
    """Item 8: fleet_recommendation via /assistant/chat as fleet_manager.

    C6 fixes:
    - Request body uses 'prompt' key (not 'message'); handler.py:245 requires prompt
    - personaId: "fleet_manager" is sent; this persona is group-gated and requires
      caller_has_fleet_group (fleet-operator group satisfies it)
    - Group is 'fleet-operator' (not 'fleet-manager'; CMS ui_stack.py has no fleet-manager group)
    - _extract_chat_text reads 'result' (handler returns result key)
    - Makes ≥1 assertion on Finding content (headline or evidence) from DDB
    - < 2 fleets → SKIP (not NOTE)

    Creates one ephemeral Cognito user with fleet-operator group.
    Requires --allow-writes.
    """
    print("\nItem 8: fleet_recommendation tool via text runtime")

    if not allow_writes:
        res.skip(
            "8-fleet-rec",
            "item 8 creates an ephemeral Cognito fleet_manager user; pass --allow-writes to enable.",
        )
        return

    # Check AVX_FLEET_IDS_CLAIM_TRUSTED on the AgentCore TEXT runtime
    trust_ok = _check_text_runtime_trust_flag(stage, region)
    if trust_ok is None:
        res.skip(
            "8-trust-flag",
            "could not determine AVX_FLEET_IDS_CLAIM_TRUSTED on the text runtime "
            "(task 5.0 step 3 not yet done or runtime not findable). "
            "Set AVX_FLEET_IDS_CLAIM_TRUSTED=true on the text supervisor runtime and re-run.",
        )
        return
    if not trust_ok:
        res.skip(
            "8-trust-flag",
            "AVX_FLEET_IDS_CLAIM_TRUSTED is not 'true' on the text runtime. "
            "Task 5.0 step 3 has not been done.",
        )
        return
    res.check("8-trust-flag", True, "AVX_FLEET_IDS_CLAIM_TRUSTED=true on text runtime")

    # Find cost Findings to test with
    all_f = _scan_findings_by_fleet_all_fleets(ddb, findings_table)
    cost_anomaly_f = [
        f for f in all_f
        if f.get("finding_kind") == _FINDING_KIND_ANOMALY
        and f.get("fleet_id")
        and str(f.get("fleet_id", "")) not in _IOS_PERSONA_FLEET_IDS
    ]
    if not cost_anomaly_f:
        res.skip("8-finding", "no fleet.cost.anomaly Findings in the table; run items 2-3 first.")
        return

    by_fleet: dict[str, list[dict[str, Any]]] = {}
    for f in cost_anomaly_f:
        fid = str(f.get("fleet_id") or "")
        by_fleet.setdefault(fid, []).append(f)

    if not by_fleet:
        res.skip("8-finding", "cost Findings have no fleet_id")
        return

    fleet_a_id = sorted(by_fleet.keys())[0]
    finding_a = by_fleet[fleet_a_id][0]
    finding_id_a = finding_a.get("finding_id", "")
    # Extract a distinctive substring of the headline for the assertion
    headline_a = (finding_a.get("headline") or finding_a.get("title") or "")[:60]

    fleet_b_id: str | None = None
    finding_id_b: str | None = None
    if len(by_fleet) >= 2:
        fleet_b_id = sorted(by_fleet.keys())[1]
        finding_id_b = by_fleet[fleet_b_id][0].get("finding_id", "")
    else:
        # C6: < 2 fleets is SKIP, not NOTE
        res.skip(
            "8-refusal-check",
            "fewer than 2 fleets with cost Findings — cannot test cross-fleet refusal. "
            "Populate more fleets or run items 2-3 after enrolling fleet B.",
        )

    res.note("8-finding", f"fleet_a={fleet_a_id!r} finding_id={finding_id_a!r} headline={headline_a!r}")
    if fleet_b_id:
        res.note("8-finding-b", f"fleet_b={fleet_b_id!r} finding_id={finding_id_b!r}")

    with _ephemeral_user(
        idp, pool_id, client_id, "mgr-a",
        # C6: use fleet-operator group (which satisfies caller_has_fleet_group check).
        # fleet_manager persona is then explicitly requested via personaId.
        # There is no "fleet-manager" Cognito group in CMS ui_stack.py.
        groups=["fleet-operator"],
        extra_attrs={"custom:fleetIds": fleet_a_id},
        res=res,
    ) as (tok_a, _):
        # C6: send 'prompt' key and personaId: "fleet_manager"
        prompt_a = (
            f"Use the fleet_recommendation tool with finding_id={finding_id_a!r} "
            f"and return the headline, one evidence value, and decision status."
        )
        status_a, body_a, lat_a = _post_chat(api, tok_a, prompt_a, persona_id="fleet_manager")
        text_a = _extract_chat_text(body_a)  # reads 'result' key
        res.check(
            "8-own-fleet-200",
            status_a == 200 and bool(text_a),
            f"→ {status_a} in {lat_a:.1f}s text_len={len(text_a)}",
        )

        # C6: assert the response contains a distinctive substring of the stored headline
        # (not just the finding_id which appears in the prompt).
        # The headline is a short phrase like "Energy cost 1.4x fleet-model median".
        headline_hint = (headline_a[:30].lower() if len(headline_a) >= 10 else "")
        finding_mentioned = bool(headline_hint and headline_hint in text_a.lower())
        refused_a = any(
            kw in text_a.lower()
            for kw in ("cannot access", "not authorized", "no access", "refused", "permission")
        )
        res.check(
            "8-own-fleet-content",
            finding_mentioned and not refused_a,
            f"headline_hint_found={finding_mentioned} refused={refused_a} "
            f"headline_hint={headline_hint!r} text={text_a[:200]!r}",
        )

        # R3-W3: assert a numeric cost entry (cost.cost_per_mile or similar) appears
        # in the reply, not evidence[0] which is always cost.diagnosis_source ("model"
        # or "rules") — a word that headlines also contain.
        finding_evidence = finding_a.get("evidence") or []
        cost_ev_value: str | None = None
        if isinstance(finding_evidence, list):
            for ev in finding_evidence:
                if isinstance(ev, dict) and str(ev.get("kind", "")).startswith("cost.cost_"):
                    raw_val = ev.get("value")
                    if raw_val is not None:
                        try:
                            rounded = f"{float(str(raw_val)):.2f}"
                            cost_ev_value = rounded
                        except (ValueError, TypeError):
                            pass
                    if cost_ev_value:
                        break
        if cost_ev_value:
            ev_in_text = cost_ev_value in text_a
            res.check(
                "8-evidence-in-reply",
                ev_in_text,
                f"cost evidence value {cost_ev_value!r} {'found' if ev_in_text else 'NOT found'} in reply",
            )
        else:
            res.skip("8-evidence-in-reply", "Finding has no cost.cost_* evidence entry to check")

        # N3/R3-C2: derive action_id and compute queue_status using the tool's own
        # functions instead of an inline copy.  Imported offline (checked in shared venv).
        # Pass the Action only when it carries 'decision'; otherwise None so that
        # compute_queue_status returns "pending" or "expired" for an undecided Finding.
        from agents.supervisor.tools.fleet_recommendation import (  # noqa: PLC0415
            compute_queue_status as _cqs,
            _derive_decision_action_id as _derive_action_id,
        )
        decision_action_id = _derive_action_id(finding_id_a)
        try:
            atab = ddb.Table(actions_table)
            aresp = atab.get_item(Key={"action_id": decision_action_id})
            action_item = aresp.get("Item")
            # Pass action only when it carries 'decision' (the tool's contract)
            action_for_status = action_item if (action_item and "decision" in action_item) else None
            expected_status = _cqs(finding_a, action_for_status)
            status_in_text = expected_status.lower() in text_a.lower()
            res.check(
                "8-action-status",
                status_in_text,
                f"expected queue_status={expected_status!r} "
                f"{'found' if status_in_text else 'NOT found'} in reply text",
            )
            res.note("8-action-status-detail",
                     f"action_id={decision_action_id!r} action={'present' if action_item else 'absent'} "
                     f"decision={action_item.get('decision') if action_item else 'n/a'}")
        except Exception as exc:  # noqa: BLE001
            res.check("8-action-status", False, f"could not read actions table: {exc}")

        if fleet_b_id and finding_id_b and "8-refusal-check" not in {r[0] for r in res.rows}:
            prompt_b = (
                f"Use the fleet_recommendation tool with finding_id={finding_id_b!r}."
            )
            status_b, body_b, lat_b = _post_chat(api, tok_a, prompt_b, persona_id="fleet_manager")
            text_b = _extract_chat_text(body_b)
            refused_b = any(
                kw in text_b.lower()
                for kw in (
                    "cannot access", "not authorized", "no access",
                    "no fleet access",
                    "refused", "permission", "not in your fleet",
                    "does not belong", "another fleet",
                )
            )
            # C6: also assert fleet B finding headline is absent
            fleet_b_headline = (by_fleet[fleet_b_id][0].get("headline") or "")[:20]
            headline_b_absent = not fleet_b_headline or fleet_b_headline.lower() not in text_b.lower()
            res.check(
                "8-refusal-check",
                refused_b and headline_b_absent,
                f"→ {status_b} in {lat_b:.1f}s refused={refused_b} "
                f"fleet_b_headline_absent={headline_b_absent}: {text_b[:200]!r}",
            )


def _post_chat(
    api: _Http,
    token: str,
    prompt: str,
    persona_id: str | None = None,
    timeout: int = 90,
) -> tuple[int, Any, float]:
    """POST to /assistant/chat.

    C6 fix: sends 'prompt' key (not 'message') and optionally personaId.
    handler.py:245 requires the 'prompt' field.
    """
    t0 = time.monotonic()
    # 5.3 run 2 (2026-10-08): /assistant/chat answers 400 "sessionId is required" without one
    # (lambdas/api-assistant-chat/handler.py). One fresh session per call; the handler pads it to
    # AgentCore's 33-character minimum, and this is already longer.
    body: dict[str, Any] = {"prompt": prompt,  # C6: 'prompt', not 'message'
                            "sessionId": f"livetest-fi-cost-{secrets.token_hex(16)}"}
    if persona_id:
        body["personaId"] = persona_id
    status, resp = api.call("POST", "/assistant/chat", token, body, timeout=timeout)
    return status, resp, time.monotonic() - t0


def _extract_chat_text(body: Any) -> str:
    """Extract the assistant's text from a chat response.

    C6 fix: handler.py returns 'result' key, not 'response'/'text'/'content'/'message'.
    (lambdas/api-assistant-chat/handler.py:566: response_body["result"] = result_text)
    """
    if isinstance(body, dict):
        return str(body.get("result") or "")
    return ""


def _check_text_runtime_trust_flag(
    stage: str,
    region: str,
) -> bool | None:
    """Return True if AVX_FLEET_IDS_CLAIM_TRUSTED=true on the AgentCore text runtime.

    The flag lives in the ``environmentVariables`` map returned at the top level
    of ``bedrock-agentcore-control get-agent-runtime``, NOT in any Lambda function
    environment.  The runtime is discovered by listing all runtimes in the region
    and matching the name prefix ``vsa_supervisor_text_{stage}``.

    Returns None if no text runtime matching the prefix can be found, or if the
    control-plane call fails.
    """
    import boto3  # noqa: PLC0415

    try:
        ctrl = boto3.client("bedrock-agentcore-control", region_name=region)
        name_prefix = f"vsa_supervisor_text_{stage}"
        page_kwargs: dict[str, Any] = {}
        while True:
            resp = ctrl.list_agent_runtimes(**page_kwargs)
            for rt in resp.get("agentRuntimes", []):
                if rt.get("agentRuntimeName", "").startswith(name_prefix):
                    runtime_id = rt["agentRuntimeId"]
                    detail = ctrl.get_agent_runtime(agentRuntimeId=runtime_id)
                    env_vars = detail.get("environmentVariables") or {}
                    trust = env_vars.get("AVX_FLEET_IDS_CLAIM_TRUSTED", "")
                    return trust.strip().lower() == "true"
            token = resp.get("nextToken")
            if not token:
                break
            page_kwargs["nextToken"] = token
    except Exception:  # noqa: BLE001
        pass
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--stage", default="staging",
                    help="Stage name (default: staging).")
    ap.add_argument("--region", default="us-west-2",
                    help="AVX region (default: us-west-2).")
    ap.add_argument("--item", type=int, metavar="N",
                    help="Run only item N (1–8). Mutually exclusive with --all.")
    ap.add_argument("--all", action="store_true",
                    help="Run all items (1–8).")
    ap.add_argument("--allow-writes", action="store_true",
                    help=(
                        "Allow write operations. Required for items 2, 3, 5, 6, 7, 8. "
                        "Items 2, 3, 7 invoke the cost agent Lambda. "
                        "Item 5 tests fleet-scoped AVX read isolation. "
                        "Item 6 triggers a Lambda error and alarm. "
                        "Items 5, 8 create ephemeral Cognito users. "
                        "Without this flag all write-gated items are SKIP (exit 2)."
                    ))
    ap.add_argument("--alarm-wait-s", type=int, default=360,
                    help="Seconds to poll for alarm transition after Lambda error (default: 360; alarm period is 300s).")
    ap.add_argument("--one-run-cap", type=int, default=None,
                    help="For item 2: run once with this max_diagnoses_per_run override.")
    return ap.parse_args()


def main() -> int:
    args = _parse_args()

    if os.environ.get("LIVE_TESTS") != "1":
        print(
            "SKIPPED — set LIVE_TESTS=1 to run.\n"
            "Items 2, 3, 5, 6, 7, 8 also require --allow-writes.\n"
            "\nExit 0 = all pass. Exit 1 = any fail. Exit 2 = any skip (non-passing)."
        )
        return 2

    if args.item is not None and args.all:
        print("--item and --all are mutually exclusive", file=sys.stderr)
        return 1

    # B-S3: the plan is staging only (fleet guards, the exempt web test user and the iOS
    # persona fleets are staging data). Refuse any other stage before any AWS call.
    if args.stage != _ALLOWED_STAGE:
        print(f"--stage must be {_ALLOWED_STAGE!r}; this script writes to staging only "
              f"(got {args.stage!r})", file=sys.stderr)
        return 1

    # Writes outside the conftest AWS block run as the caller with real credentials.
    # Refuse before any client unless the live-run step gave its go.
    _refusal = _lwg.check_write_flags(
        stage=args.stage, go_env=_LIVE_GO_ENV, flags={"--allow-writes": args.allow_writes}
    )
    if _refusal:
        print(_refusal, file=sys.stderr)
        return 1

    items_to_run: set[int]
    if args.item is not None:
        if args.item not in range(1, 9):
            print(f"--item must be 1–8, got {args.item}", file=sys.stderr)
            return 1
        items_to_run = {args.item}
    else:
        items_to_run = set(range(1, 9))

    WRITE_ITEMS = {2, 3, 5, 6, 7, 8}
    write_requested = items_to_run & WRITE_ITEMS
    # C1 fix: items NOT in items_to_run must still produce SKIP rows so that
    # --stage staging --region us-west-2 (no --all, no --allow-writes) cannot exit 0.
    # Track which items were explicitly dropped so we add their SKIP rows after res is created.
    items_skipped_for_writes: set[int] = set()
    if write_requested and not args.allow_writes:
        print(
            f"Items {sorted(write_requested)} require --allow-writes. "
            "Items 2, 3, 7 invoke the cost agent Lambda. "
            "Item 5 tests fleet scope isolation. "
            "Item 6 triggers a real Lambda error and alarm. "
            "Items 5, 8 create ephemeral Cognito users. "
            "Pass --allow-writes to enable.",
            file=sys.stderr,
        )
        items_skipped_for_writes = write_requested & items_to_run
        items_to_run -= write_requested

    import boto3  # noqa: PLC0415

    stage, region = args.stage, args.region
    adp_region = _ADP_REGION

    sts = boto3.client("sts", region_name=region)
    who = sts.get_caller_identity()
    account = who["Account"]
    print(f"caller  : {who['Arn']}")
    print(f"account : {account}")
    print(f"stage   : {stage}")
    print(f"region  : {region}")

    findings_table = f"vsa-{stage}-avx-findings"
    runs_table = f"vsa-{stage}-avx-agent-runs"
    actions_table = f"vsa-{stage}-avx-actions"
    function_name = f"vsa-{stage}-tier2-health-agent-{region}"
    adp_bucket = f"adp-{stage}-foundation-lake-{account}-{adp_region}"
    workgroup = f"cvx-{stage}-analytics"
    athena_results_bucket = f"cvx-{stage}-athena-results-{account}-{adp_region}"
    # W: use workgroup default output location (not a custom prefix with no lifecycle)
    output_location = f"s3://{athena_results_bucket}/athena-results/"

    print(f"findings: {findings_table}")
    print(f"runs    : {runs_table}")
    print(f"lambda  : {function_name}")
    print(f"bucket  : {adp_bucket}")
    print(f"workgrp : {workgroup}")
    print(f"fleet-db: {_fleet_cost_db(stage)}")
    print()

    # Shared clients
    ddb = boto3.resource("dynamodb", region_name=region)
    # C3: Lambda client with read_timeout > function timeout, no retries
    lam = _make_lambda_client(region)
    cw = boto3.client("cloudwatch", region_name=region)
    sns = boto3.client("sns", region_name=region)
    s3 = boto3.client("s3", region_name=adp_region)
    athena = boto3.client("athena", region_name=adp_region)
    idp = boto3.client("cognito-idp", region_name=region)
    cfn = boto3.client("cloudformation", region_name=region)

    # AVX REST API base URL
    try:
        outs = cfn.describe_stacks(StackName=f"vsa-{stage}-api")["Stacks"][0]["Outputs"]
        api_base = next(
            o["OutputValue"].rstrip("/")
            for o in outs
            if o["OutputKey"] == "RestApiUrl"
        )
    except Exception as exc:  # noqa: BLE001
        api_base = f"https://UNKNOWN-{stage}"
        print(f"WARN: could not get RestApiUrl from vsa-{stage}-api: {exc}", file=sys.stderr)
    api = _Http(api_base)
    print(f"api     : {api_base}")
    print()

    pool_id: str | None = None
    client_id: str | None = None
    if items_to_run & {5, 8}:
        try:
            pool_id = _find_pool(idp, stage)
            client_id = _password_client(idp, pool_id)
        except SystemExit as exc:
            print(f"WARN: {exc}", file=sys.stderr)

    res = Results()

    # C1: add SKIP rows for items not run due to missing --allow-writes,
    # and for any item not in items_to_run when --item N was used.
    all_items = set(range(1, 9))
    items_not_requested = all_items - items_to_run - items_skipped_for_writes
    for n in sorted(items_skipped_for_writes):
        res.skip(
            f"item-{n}",
            f"item {n} requires --allow-writes; pass --allow-writes to enable",
        )
    for n in sorted(items_not_requested):
        res.skip(
            f"item-{n}",
            f"item {n} not requested (--item {args.item} was given); "
            "running a subset is non-passing (exit 2)",
        )

    # NW1: track invoke failures to stop later invoking items
    invoke_ok = True
    # D3: item 7 runs before items 2/3; returns False if guard refuses or invoke raises.
    item7_ok = True

    if 1 in items_to_run:
        item1_views_rows(athena, ddb, workgroup, output_location, stage, res)

    if 7 in items_to_run:
        if not invoke_ok:
            res.skip("7-invoke", "NW1: skipped — earlier invoke failed; cannot start another run safely")
        else:
            item7_ok = item7_rules_fallback(
                lam, ddb, athena,
                findings_table=findings_table,
                function_name=function_name,
                stage=stage,
                region=region,
                workgroup=workgroup,
                output_location=output_location,
                res=res,
            )

    if 2 in items_to_run:
        if not item7_ok:
            res.skip("2-item7-blocked", "D3: item 7 guard refused or invoke raised; item 2 skipped")
        elif res._cleanup_failed:
            res.skip("2-cleanup-failed", "R4-C1: item 7 restore failed; item 2 skipped to avoid writes with bad model id")
        else:
            _data_month_2, item2_invoke_ok = item2_agent_scores_plants(
                lam, ddb, s3, athena,
                findings_table=findings_table,
                runs_table=runs_table,
                function_name=function_name,
                bucket=adp_bucket,
                stage=stage,
                region=region,
                workgroup=workgroup,
                output_location=output_location,
                res=res,
                one_run_cap=args.one_run_cap,
            )
            # R3-W1: item2 returns invoke_ok=False when any Lambda invoke raised.
            # Keyed on the return value, not on label text, so "2-run-{n}" labels
            # are correctly detected without containing the word "invoke".
            if not item2_invoke_ok:
                invoke_ok = False

    if 3 in items_to_run:
        if not item7_ok:
            res.skip("3-item7-blocked", "D3: item 7 guard refused or invoke raised; item 3 skipped")
        elif res._cleanup_failed:
            res.skip("3-cleanup-failed", "R4-C1: item 7 restore failed; item 3 skipped to avoid writes with bad model id")
        elif not invoke_ok:
            res.skip("3-invoke", "NW1: skipped — item 2 invoke failed; cannot start a second run safely")
        elif 2 in items_to_run and ("2-convergence", "PASS") not in [(r[0], r[1]) for r in res.rows]:
            # Item 3 asserts a no-op AFTER convergence; without it, it would only add a write run.
            res.skip("3-not-converged", "item 2 did not converge (or did not run); item 3 skipped")
        else:
            invoke_ok_3 = item3_second_run_noop(
                lam, ddb, athena,
                findings_table=findings_table,
                runs_table=runs_table,
                function_name=function_name,
                stage=stage,
                region=region,
                workgroup=workgroup,
                output_location=output_location,
                res=res,
            )
            if not invoke_ok_3:
                invoke_ok = False

    if 4 in items_to_run:
        item4_athena_paging(
            athena, ddb, workgroup, output_location, stage, runs_table, res,
        )

    if 5 in items_to_run:
        if pool_id and client_id:
            item5_fleet_scope(
                ddb, idp,
                findings_table=findings_table,
                pool_id=pool_id,
                client_id=client_id,
                api=api,
                stage=stage,
                res=res,
                allow_writes=args.allow_writes,
            )
        else:
            res.skip("5-isolation", "Cognito pool not found; cannot create ephemeral users")

    if 6 in items_to_run:
        if res._cleanup_failed:
            res.skip("6-cleanup-failed", "R4-C1: item 7 restore failed; item 6 skipped")
        else:
            item6_forced_alarm_failure(
                lam, cw, sns,
                function_name=function_name,
                stage=stage,
                region=region,
                res=res,
                wait_s=args.alarm_wait_s,
            )

    if 8 in items_to_run:
        if pool_id and client_id:
            item8_fleet_recommendation(
                lam, ddb, idp,
                findings_table=findings_table,
                actions_table=actions_table,
                pool_id=pool_id,
                client_id=client_id,
                api=api,
                function_name=function_name,
                stage=stage,
                region=region,
                res=res,
                allow_writes=args.allow_writes,
            )
        else:
            res.skip("8-fleet-rec", "Cognito pool not found; cannot create fleet_manager user")

    res.summary()

    # C1 fix: exit 1 on any fail; exit 2 on any skip (even alongside passes);
    # exit 0 only when every requested item ran and passed.
    if res.failed or res._cleanup_failed:
        n_failed = len(res.failed)
        print(f"\nFAIL: {n_failed} check(s) failed" + (" + cleanup failure" if res._cleanup_failed else ""))
        return 1
    if res.has_any_skip:
        n_skip = sum(1 for _, s, _ in res.rows if s == "SKIP")
        print(f"\nSKIPPED: {n_skip} check(s) were skipped — run is non-passing (exit 2)")
        return 2
    print("\nPASS")
    return 0


def _install_signal_handlers() -> None:
    """SIGTERM/SIGHUP raise SystemExit(143) once, so every ``finally`` runs (item 7's
    env restore and read-back, item 5/8's Cognito user deletion); later signals are
    ignored so a second SIGTERM cannot interrupt that cleanup."""
    import signal as _sig  # noqa: PLC0415

    _SIGNAL_STATE["raised"] = False

    def _exit_once(signum: int, frame: object) -> None:
        if not _SIGNAL_STATE["raised"]:
            _SIGNAL_STATE["raised"] = True
            raise SystemExit(143)
        # Second signal: already exiting; ignore so finally blocks can finish.

    _sig.signal(_sig.SIGTERM, _exit_once)
    _sig.signal(_sig.SIGHUP, _exit_once)


if __name__ == "__main__":
    _install_signal_handlers()
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(1)  # interrupted after writes may have happened
