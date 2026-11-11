#!/usr/bin/env python3
"""Live verification for FI Fleet-Health Stage D, task 5.3 / 6.4 / 6.5.

Spec: .kiro/specs/2026-10-05-fi-fleet-health-stage-d/53-brief.md
Branch: wip/fi-health-d-65

Parts:
  --part now      items 1, 6, 7, 8a. Item 6 runs only while the investigation event source
                  is Disabled (it refuses rather than race the real consumer). It passed in
                  5.3 (2026-10-17); since FI_FLEET_HEALTH_LIVE = true is deployed (Group 6
                  rest-plan step 7) it SKIPs, so --part now exits 2. tasks.md 6.4 covers
                  the enabled path.
  --part after-6  items 2, 3, 4, 5, 8b: write-window gated items that require the
                  investigations event source to be Enabled (Group 6 rest-plan step 7).
                  Implemented in tasks.md 6.4 (stages 1-3).
                  Exit 0: all items ran and passed. Exit 1: any FAIL. Exit 2: any SKIP.
  --part item9    item 9 (task 6.5): test the DLQ→alarm→email chain for investigations.
                  One run takes ~20-45 min and sends one alarm email to the operator
                  address.  Requires --allow-writes.
                  Exit 0: all checks PASS. Exit 1: any FAIL. Exit 2: SKIP only.
  --part after-6-check  item 9 is --part item9 (task 6.5). This part exits 1 before
                  any AWS call, naming --part item9 as the correct entry point.
  --cleanup-only        undo whatever the manifest records for a given run-id.
  --items 1,6,...       explicit list

Flags:
  --stage staging          required; refuses anything else before any AWS call
  --part now|after-6|after-6-check|item9   run a named part
  --items N,...            run specific item numbers
  --allow-writes           gates items 6, 8a, all after-6 write items, and --part item9; it also needs
                           FI_QUALITY_LIVE_GO=<stage> unless the conftest AWS block is installed (docs/DEPLOYMENT.md)
  --answer-key PATH        local parquet (required for item 1)
  --run-id ID              run id for --cleanup-only or to resume --part item9

Exit 0: every selected item ran and passed.
Exit 1: any FAIL.
Exit 2: any SKIP.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import re
import secrets
import signal as _sig
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterator

# Repo root on sys.path (script runs as a file; scripts/ would be sys.path[0]).
# Mirrors live-verify-fi-recall-warranty.py:53-55.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ALLOWED_STAGE = "staging"

# AVX (us-west-2) — names from brief §Names
_AVX_REGION = "us-west-2"
_FINDINGS_TABLE = "vsa-{stage}-avx-findings"
_ACTIONS_TABLE = "vsa-{stage}-avx-actions"
_INVESTIGATIONS_TABLE = "vsa-{stage}-avx-investigations"
_INV_QUEUE = "vsa-{stage}-avx-investigations"
_TIER2_LAMBDA = "vsa-{stage}-tier2-health-agent-us-west-2"
_VSA_API_STACK = "vsa-{stage}-api"

# ADP (us-east-1) — names from brief §Names
_ADP_REGION = "us-east-1"
_PATTERNS_TABLE = "adp-{stage}-fault-patterns"
_FAULT_DAILY_SCHEDULE = "adp-{stage}-foundation-fault-daily-schedule"
_FAULT_PATTERNS_SCHEDULE = "adp-{stage}-foundation-fault-patterns-schedule"
# Each schedule lives in its own group, not the default one (ADP fleet_health_stack.py:700, 710
# `_stage_name(stage, "fault-daily")` / `"fault-patterns"`; live list_schedules 2026-10-17).
_FAULT_DAILY_SCHEDULE_GROUP = "adp-{stage}-foundation-fault-daily"
_FAULT_PATTERNS_SCHEDULE_GROUP = "adp-{stage}-foundation-fault-patterns"
_PATTERNS_LAMBDA = "adp-{stage}-foundation-fault-patterns-job"

# CMS Cognito pool name — brief §Cognito
_CMS_POOL_NAME = "cms-staging-ui-users"

# Livetest user label prefix — brief §Cognito
_USER_PREFIX = "livetest-fi-quality"

# Items in each part (task 5.3's Accept numbering; 8a = the patterns-job re-run, 8b = the
# unchanged-investigation re-request). The user's split, decisions.md 2026-10-16 20:53Z.
_NOW_ITEMS: tuple[str, ...] = ("1", "6", "7", "8a")
_AFTER6_ITEMS: tuple[str, ...] = ("2", "3", "4", "5", "8b")
_ALL_ITEMS: tuple[str, ...] = ("1", "2", "3", "4", "5", "6", "7", "8a", "8b")

# Items that write to staging and need --allow-writes
_WRITE_ITEMS = frozenset({"6", "8a", "2", "3", "4", "5", "8b"})

# SQS waits in item 6's cleanup (long polls; tests set them to 0).
_SQS_DRAIN_WAIT_S = 5
_SQS_FINAL_WAIT_S = 10
_QUEUE_EMPTY_POLL_S = 60

#: Item 6's cap snapshot before it is read (``None`` means "read, and absent").
_NOT_READ = object()

# ---------------------------------------------------------------------------
# After-6 stage 1 constants
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# After-6 cleanup (ordered, signal-held, one finally)
_MANIFEST_DIR_ENV = "FI_QUALITY_MANIFEST_DIR"

#: Default directory for run manifests (production): durable 0700 dir outside /tmp.
#: W11 fix: was "/tmp/2026-10-05-fi-fleet-health-stage-d-64" (ephemeral, test pollution).
_MANIFEST_DEFAULT_DIR = str(Path.home() / ".cache" / "avx-live-verify" / "fi-quality")

#: File permissions for the manifest (0600: owner read/write only).
_MANIFEST_PERMS = 0o600

#: Directory permissions for the per-run manifest directory (0700).
_MANIFEST_DIR_PERMS = 0o700

#: Shape that a valid run-id must match: fi-quality-{YYYYMMDD}T{HHMMSS}-{8hex}.
_RUN_ID_RE = re.compile(r"^fi-quality-\d{8}T\d{6}-[0-9a-f]{8}$")

# ---------------------------------------------------------------------------
# Stage 3 constants (item 8b)
# ---------------------------------------------------------------------------

#: Long-poll window for all SQS receives (W7: avoid short-poll false empties).
_SQS_LONG_POLL_WAIT_S = 20  # SQS maximum; world advances clock instead of hanging

#: Name fragment for the DLQ alarm (format: vsa-{stage}-avx-investigations-dlq-depth-{region}).
_INV_DLQ_ALARM_BASE = "vsa-{stage}-avx-investigations-dlq-depth-{region}"

#: ADP engineering corpus DynamoDB table (for options-number check in item 2, C3).
_CORPUS_TABLE = "adp-{stage}-engineering-corpus"

# ADP Digital Thread tables (us-east-1) — from brief §Names (64-facts §1)
_DIGITAL_THREAD_TABLE = "adp-{stage}-digital-thread"
_OTA_CAMPAIGNS_TABLE = "adp-{stage}-ota-campaigns-issued"
_RECALL_CAMPAIGNS_TABLE = "adp-{stage}-recall-campaigns-issued"
_THREAD_LAKE_PREFIX = "curated/engineering/digital_thread/"
_ISSUED_RECALLS_PREFIX = "curated/engineering/recall_campaigns_issued/"
_THREAD_WRITER_FN = "adp-{stage}-digital-thread-writer"

#: Schedule name prefixes that define the after-6 write window.
_AFTER6_SCHEDULE_PREFIXES_TMPL = ["vsa-{stage}-tier2-", "dms-{stage}-fi-"]

#: Max seconds to poll an investigation to terminal state before failing.
_INV_POLL_MAX_S = 600

#: Seconds between poll attempts.
_INV_POLL_INTERVAL_S = 5

#: New --part value for the after-6-check pass.
_PART_AFTER6_CHECK = "after-6-check"

#: New --part value for item 9 (task 6.5).
_PART_ITEM9 = "item9"

# ---------------------------------------------------------------------------
# Item 9 constants
# ---------------------------------------------------------------------------

#: DLQ name template for investigations.
_INV_DLQ = "vsa-{stage}-avx-investigations-dlq"

#: DLQ depth alarm name template.
_INV_DLQ_ALARM = "vsa-{stage}-avx-investigations-dlq-depth-{region}"

#: Seconds between alarm-history polls in link 3.
_ITEM9_ALARM_POLL_INTERVAL_S = 30

#: Maximum seconds to wait for alarm to fire (link 3).
_ITEM9_ALARM_POLL_MAX_S = 25 * 60  # 25 min

#: Maximum seconds to wait for alarm to return to OK (residue check).
_ITEM9_RESIDUE_ALARM_MAX_S = 15 * 60  # 15 min

#: Seconds between residue alarm polls.
_ITEM9_RESIDUE_POLL_INTERVAL_S = 30

#: Maximum seconds to poll for Action item after StateUpdate (link 3).
_ITEM9_ACTION_POLL_MAX_S = 5 * 60  # 5 min bounded after StateUpdate

#: Maximum seconds to poll DLQ empty after delete (cleanup).
_ITEM9_DLQ_CONFIRM_MAX_S = 3 * 60  # 3 min

#: Seconds between DLQ empty polls (cleanup).
_ITEM9_DLQ_CONFIRM_INTERVAL_S = 10


def _investigation_esms(lam: Any, fn: str, queue_name: str) -> list[dict]:
    """Event source mappings of ``fn`` whose source is exactly the queue ``queue_name``
    (ARN suffix match, so ``…-investigations-dlq`` doesn't count); follows NextMarker."""
    out: list[dict] = []
    kwargs: dict[str, Any] = {"FunctionName": fn}
    while True:
        page = lam.list_event_source_mappings(**kwargs)
        out += [m for m in page.get("EventSourceMappings", [])
                if (m.get("EventSourceArn") or "").endswith(f":{queue_name}")]
        if not page.get("NextMarker"):
            return out
        kwargs["Marker"] = page["NextMarker"]

def _queue_esms(lam: Any, queue_arn: str) -> list[dict]:
    """Every event source mapping on the queue, whatever its function; follows NextMarker."""
    out: list[dict] = []
    kwargs: dict[str, Any] = {"EventSourceArn": queue_arn}
    while True:
        page = lam.list_event_source_mappings(**kwargs)
        out += page.get("EventSourceMappings", [])
        if not page.get("NextMarker"):
            return out
        kwargs["Marker"] = page["NextMarker"]


# Brief: parts
_PART_NOW = "now"
_PART_AFTER6 = "after-6"

# ---------------------------------------------------------------------------
# Run-id validation
# ---------------------------------------------------------------------------

def _validate_run_id(s: str) -> bool:
    """Return True iff ``s`` matches the script's run-id shape (fi-quality-YYYYMMDDTHHMMSS-8hex).

    Validates before any AWS call so a free-text ``--run-id`` cannot reach table keys,
    URL path segments or Cognito usernames (security review Warning).
    """
    return bool(_RUN_ID_RE.fullmatch(s))


# ---------------------------------------------------------------------------
# Live-write go (shared helper: scripts/lib/live_write_guard.py)
# ---------------------------------------------------------------------------

if str(Path(__file__).resolve().parent / "lib") not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import live_write_guard as _lwg  # noqa: E402

#: Set to ``--stage`` by the live-run step, on the user's go, and by nothing else.
_LIVE_GO_ENV = "FI_QUALITY_LIVE_GO"


def _aws_block_active() -> bool:
    return _lwg.aws_block_active()


def _writes_sanctioned(stage: str) -> bool:
    return _lwg.writes_sanctioned(stage, _LIVE_GO_ENV)



def _validate_cleanup_manifest(
    manifest: dict, run_id: str, stage: str
) -> str | None:
    """S5: shape-check the manifest before --cleanup-only runs any AWS call.

    Returns None when the manifest is consistent with ``run_id`` and ``stage``;
    returns a mismatch description otherwise (caller prints the refusal and returns 1).

    Checks:
    1. manifest["run_id"] == run_id (argument and file agree on run identity).
    2. manifest["stage"] == stage (argument and file agree on target environment).
    3. Every DynamoDB table name and S3 bucket name recorded in the manifest
       contains the manifest's stage as a substring (guards against a staging
       manifest mistakenly applied to a production environment).
    """
    mfst_run_id = manifest.get("run_id", "")
    # S2: refuse an empty run_id — a manifest without an identity cannot be safely applied.
    if not mfst_run_id:
        return "manifest run_id is missing or empty; refusing --cleanup-only on unidentified run"
    if mfst_run_id != run_id:
        return (
            f"manifest run_id={mfst_run_id!r} != --run-id argument {run_id!r}"
        )
    mfst_stage = manifest.get("stage", "")
    # S2: refuse an empty stage — cannot validate environment without it.
    if not mfst_stage:
        return "manifest stage is missing or empty; refusing --cleanup-only"
    if mfst_stage != stage:
        return (
            f"manifest stage={mfst_stage!r} != --stage argument {stage!r}"
        )
    # Check all table names and bucket names recorded in the manifest.
    check_stage = mfst_stage or stage
    for key, items in manifest.items():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            for field in ("table", "bucket"):
                val = item.get(field, "")
                if val and check_stage and check_stage not in val:
                    return (
                        f"manifest[{key!r}] {field}={val!r} does not contain "
                        f"stage={check_stage!r}; possible wrong-environment manifest"
                    )
    # S2: also check top-level string bucket fields not covered by the list loop above.
    for bucket_key in (
        "item4_vin_bucket",
        "item4_thread_lake_bucket",
        "item4_issued_recalls_bucket",
    ):
        val = manifest.get(bucket_key)
        if val and isinstance(val, str) and check_stage and check_stage not in val:
            return (
                f"manifest[{bucket_key!r}] bucket={val!r} does not contain "
                f"stage={check_stage!r}; possible wrong-environment manifest"
            )
    return None


# ---------------------------------------------------------------------------
# Manifest helpers — 0600 file, 0700 per-run directory, atomic os.replace
# ---------------------------------------------------------------------------

def _manifest_base_dir() -> str:
    """Return the base directory for manifests (env override or default)."""
    return os.environ.get(_MANIFEST_DIR_ENV, _MANIFEST_DEFAULT_DIR)


def _manifest_path(run_id: str, manifest_dir: str | None = None) -> Path:
    """Return the manifest file path for ``run_id``.

    Directory layout: ``{manifest_dir}/{run_id}/manifest.json``.
    Tests override ``manifest_dir`` via ``FI_QUALITY_MANIFEST_DIR`` or the kwarg.
    """
    base = manifest_dir if manifest_dir is not None else _manifest_base_dir()
    return Path(base) / run_id / "manifest.json"


def _save_manifest(run_id: str, data: dict, manifest_dir: str | None = None) -> None:
    """Atomically write the manifest to a 0600 file in a 0700 directory.

    Uses ``os.replace`` for atomicity (rename is atomic on POSIX when src/dst are on
    the same filesystem).  Creates the directory at 0700 before writing.
    """
    p = _manifest_path(run_id, manifest_dir)
    d = p.parent
    if not d.exists():
        d.mkdir(parents=True, mode=_MANIFEST_DIR_PERMS, exist_ok=True)
        os.chmod(d, _MANIFEST_DIR_PERMS)  # ensure mode even if umask masked it
    raw = json.dumps(data, default=str).encode()
    tmp = p.with_suffix(".json.tmp")
    try:
        tmp.write_bytes(raw)
        os.chmod(tmp, _MANIFEST_PERMS)
        os.replace(tmp, p)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _load_manifest(run_id: str, manifest_dir: str | None = None) -> dict | None:
    """Return the manifest for ``run_id``, or None if it does not exist."""
    p = _manifest_path(run_id, manifest_dir)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_bytes())
    except (ValueError, OSError):
        return None


def _fresh_manifest(run_id: str, stage: str) -> dict:
    """Return an empty manifest dict for a new after-6 run."""
    return {
        "run_id": run_id,
        "stage": stage,
        "started_at": _utc_now().isoformat(timespec="seconds"),
        "investigations": [],   # list of {pattern_group_id, investigation_id, status}
        "item3_cap": None,      # {date, before_count, moved} set by item 3
        "event_source_uuid": None,        # ESM UUID, saved BEFORE disable (step 1)
        "users": [],            # ephemeral Cognito usernames
        "event_source_disabled": False,
        "items_passed": {},
        "user_sub": "",         # C2 fix: JWT sub of the product-engineer user created for this run
    }


# ---------------------------------------------------------------------------
# After-6 write-window guard — reuses _schedule_guard with tier2 schedule names
# ---------------------------------------------------------------------------

def _list_schedule_names(
    scheduler: Any, prefixes: list[str]
) -> list[tuple[str, str]]:
    """List all schedules whose Name starts with any prefix; follows NextToken.

    Returns a list of ``(name, group_name)`` tuples for use with ``_schedule_guard``.
    The summaries from ``list_schedules`` lack ``ScheduleExpression``, so only names
    and groups are returned here; ``_schedule_guard`` fetches the full expression per
    schedule via ``get_schedule``.
    """
    names: list[tuple[str, str]] = []
    kwargs: dict[str, Any] = {}
    while True:
        page = scheduler.list_schedules(**kwargs)
        for s in page.get("Schedules", []):
            name = s.get("Name", "")
            group = s.get("GroupName", "default")
            if any(name.startswith(p) for p in prefixes):
                names.append((name, group))
        token = page.get("NextToken")
        if not token:
            break
        kwargs["NextToken"] = token
    return names


class _WriteWindowPassNote(str):
    """Falsy pass note returned by ``_after6_write_window_guard`` when all schedules are rate().

    S1 fix: callers check ``if window_reason: res.skip(...)``; since this is falsy they
    proceed to write (unchanged behaviour).  The skipped schedule names are available
    via ``str(result)`` for a PASS/NOTE row.
    """

    def __bool__(self) -> bool:
        return False


def _after6_write_window_guard(
    scheduler: Any, stage: str, now: dt.datetime
) -> str | None:
    """Write-window guard for after-6 items.

    Same 45-min margin as the now-items' guard (``_schedule_guard``).
    Staging today: writes only 06:45Z–01:15Z (crons 02:00–06:00 UTC, DMS 05:40).
    Returns None when writes are safe; a refusal reason otherwise.
    Raises on schedule read errors (fail-closed).

    C4 fix: rate() schedules of components that cannot conflict (like
    vsa-staging-tier2-diagnostics-us-west-2 rate(5 minutes)) are skipped before
    calling _schedule_guard.  at() and non-UTC expressions still fail closed.

    S1 fix: when all schedules are rate(), the return value is a falsy
    ``_WriteWindowPassNote`` naming the skipped schedules rather than plain
    ``None``, so callers' ``if window_reason: skip()`` guards still pass through
    but the note is available as ``str(result)`` for a PASS row.
    """
    prefixes = [p.format(stage=stage) for p in _AFTER6_SCHEDULE_PREFIXES_TMPL]
    names = _list_schedule_names(scheduler, prefixes)
    if not names:
        # No schedules found — could mean the stage prefix is wrong; fail closed.
        return (
            f"no schedules found for prefixes {prefixes!r}; refusing writes "
            f"(cannot determine the safe window)"
        )

    # C4 fix: filter out rate() schedules before evaluating the write window.
    # Rate schedules run continuously at their interval; they don't define a
    # specific window that could conflict, so they cannot "close" the write window.
    # at() and other non-cron expressions are still evaluated (fail closed in _schedule_guard).
    filtered_names: list[tuple[str, str]] = []
    rate_names: list[str] = []  # S1: track names of skipped rate() schedules
    for name, group in names:
        try:
            sc = scheduler.get_schedule(Name=name, GroupName=group)
            expr = sc.get("ScheduleExpression", "").strip()
        except Exception:  # noqa: BLE001
            # Read error — keep the name so _schedule_guard raises (fail closed)
            filtered_names.append((name, group))
            continue
        if expr.lower().startswith("rate("):
            # Skip rate() schedules (memory live-check-cleanup-traps: diagnostics
            # rate(5 min) should not block writes; rate schedules have no fixed window)
            rate_names.append(name)  # S1: record the name for the pass note
            continue
        filtered_names.append((name, group))

    if not filtered_names:
        # S1: All schedules were rate() → writes safe; return a falsy pass-note naming them
        # so callers' ``if window_reason:`` guards still pass through (the note is falsy)
        # but the skipped schedules are visible via str(result).
        return _WriteWindowPassNote(
            f"all schedules are rate(); skipped: {', '.join(rate_names)}"
        )
    return _schedule_guard(scheduler, filtered_names, now)


# ---------------------------------------------------------------------------
# After-6 pre-check guards — ESM Enabled, queue/DLQ depths
# ---------------------------------------------------------------------------

#: Drain wait for the after-6 write items' pre-check (issue
#: 2026-10-28-fi-quality-after6-queue-precheck-race). The job marks an investigation
#: ``completed`` before its Lambda returns and the event source deletes the message, and
#: SQS's ApproximateNumberOf* counters can lag about a minute, so a write item that follows
#: item 2 can read the queue as in flight for a while after the work is done.
#: Pass only when the queue and DLQ read 0/0 on every read across ``_PRECHECK_SETTLE_S``;
#: give up after ``_PRECHECK_DRAIN_MAX_S`` (well inside the write window's 45-min margin).
_PRECHECK_SETTLE_S = 60
_PRECHECK_DRAIN_INTERVAL_S = 10
_PRECHECK_DRAIN_MAX_S = 240


def _after6_pre_checks(
    sqs: Any, lam: Any, stage: str, *, wait_for_drain: bool = False,
) -> str | None:
    """Pre-checks for all after-6 write items: ESM Enabled, queue + DLQ both empty.

    Returns None when all checks pass; a refusal reason otherwise.
    Must be called BEFORE any AWS write in an after-6 item.

    ``wait_for_drain=False`` (default): one read of each queue; any message refuses.
    ``wait_for_drain=True``: a non-Enabled event source or a non-empty DLQ refuses at once
    (nothing drains the DLQ). A non-empty queue is re-read every
    ``_PRECHECK_DRAIN_INTERVAL_S``; pass needs both queues 0/0 on every read for
    ``_PRECHECK_SETTLE_S`` (a non-zero read restarts that window) and the event source still
    Enabled when re-read afterwards. Gives up after ``_PRECHECK_DRAIN_MAX_S``, or after a
    fixed number of reads if the clock stalls.
    """
    tier2_fn = _TIER2_LAMBDA.format(stage=stage)
    inv_queue_nm = _INV_QUEUE.format(stage=stage)

    def _esm_reason() -> str | None:
        inv_esms = _investigation_esms(lam, tier2_fn, inv_queue_nm)
        if not inv_esms:
            return (
                f"no SQS event source mapping for {inv_queue_nm!r} on {tier2_fn!r}; "
                f"cannot confirm ESM Enabled"
            )
        disabled = [m for m in inv_esms if m.get("State") != "Enabled"]
        if disabled:
            return (
                f"investigation ESM is not Enabled "
                f"({[m.get('State') for m in disabled]}); "
                f"after-6 items require the ESM Enabled"
            )
        return None

    esm_reason = _esm_reason()
    if esm_reason:
        return esm_reason

    # Queue and DLQ must both be empty (visible=0, in-flight=0).
    q_url = sqs.get_queue_url(QueueName=inv_queue_nm)["QueueUrl"]
    dlq_url = sqs.get_queue_url(QueueName=f"{inv_queue_nm}-dlq")["QueueUrl"]

    def _depth(url: str) -> tuple[int, int]:
        attrs = sqs.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=["ApproximateNumberOfMessages",
                            "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        return (int(attrs.get("ApproximateNumberOfMessages", 0)),
                int(attrs.get("ApproximateNumberOfMessagesNotVisible", 0)))

    def _not_empty(label: str, url: str, depth: tuple[int, int]) -> str:
        return (f"{label} not empty before write: "
                f"visible={depth[0]} in-flight={depth[1]} ({url})")

    if not wait_for_drain:
        for label, url in (("queue", q_url), ("DLQ", dlq_url)):
            depth = _depth(url)
            if depth != (0, 0):
                return _not_empty(label, url, depth)
        return None

    start = time.monotonic()
    deadline = start + _PRECHECK_DRAIN_MAX_S
    # Read cap for a stalled clock only; set above the deadline's read count so the
    # deadline governs whenever the clock moves.
    max_reads = 2 * (_PRECHECK_DRAIN_MAX_S // _PRECHECK_DRAIN_INTERVAL_S) + 2
    zero_since: float | None = None
    reads = 0
    while True:
        reads += 1
        q_depth = _depth(q_url)
        d_depth = _depth(dlq_url)
        if d_depth != (0, 0):
            return _not_empty("DLQ", dlq_url, d_depth) + "; nothing drains the DLQ, not waiting"
        now = time.monotonic()
        if q_depth == (0, 0):
            if zero_since is None:
                zero_since = now
            if now - zero_since >= _PRECHECK_SETTLE_S:
                return _esm_reason()
        else:
            zero_since = None
        if now >= deadline or reads >= max_reads:
            waited = int(now - start)
            if zero_since is None:
                return (_not_empty("queue", q_url, q_depth)
                        + f"; waited {waited} s of {_PRECHECK_DRAIN_MAX_S} s for it to drain")
            return (f"queue read 0/0 for only {int(now - zero_since)} s of the "
                    f"{_PRECHECK_SETTLE_S} s settle window after waiting {waited} s "
                    f"({q_url})")
        time.sleep(_PRECHECK_DRAIN_INTERVAL_S)


# ---------------------------------------------------------------------------
# Investigation poll helper
# ---------------------------------------------------------------------------

def _poll_investigation(
    api_url: str, inv_id: str, token: str,
    *, max_s: int = _INV_POLL_MAX_S, interval_s: int = _INV_POLL_INTERVAL_S,
    max_iterations: int | None = None,
) -> dict | None:
    """Poll GET /investigations/{id} until status reaches a terminal state.

    Returns the full investigation body on ``completed`` or ``failed``; None on timeout.

    ``max_iterations`` caps the number of GET + sleep cycles independent of wall-clock
    time, so a frozen or ScriptTime-backed ``time.monotonic()`` cannot produce an
    infinite loop.  Default: ``max_s // max(1, interval_s) + 2``.
    """
    if max_iterations is None:
        max_iterations = max_s // max(1, interval_s) + 2
    deadline = time.monotonic() + max_s
    iters = 0
    while time.monotonic() < deadline and iters < max_iterations:
        iters += 1
        s, b = _item_6_http(api_url, "GET", f"investigations/{inv_id}", token)
        if s == 200 and isinstance(b, dict):
            status = b.get("status", "")
            if status in ("completed", "failed"):
                return b
        if time.monotonic() < deadline and iters < max_iterations:
            time.sleep(interval_s)
    return None


def _safe_print(*args: Any, **kwargs: Any) -> None:
    """Print, swallowing OSError/ValueError (closed terminal, broken pipe).

    W1 fix: every cleanup-path printer goes through this so a closed stdout
    does not stop cleanup mid-step.
    """
    try:
        print(*args, **kwargs)
    except (OSError, ValueError):
        pass


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

class Results:
    """PASS / FAIL / SKIP tracker; drives exit code."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def skip(self, label: str, reason: str) -> None:
        self.rows.append((label, "SKIP", reason))
        _safe_print(f"  [SKIP] {label}: {reason}", flush=True)

    @property
    def has_any_skip(self) -> bool:
        return any(r[1] == "SKIP" for r in self.rows)

    @property
    def has_any_fail(self) -> bool:
        return any(r[1] == "FAIL" for r in self.rows)

    def summary(self, run_id: str) -> None:
        _safe_print("\nSummary")
        for label, status, detail in self.rows:
            _safe_print(f"  {status:4}  {label}{(': ' + detail[:120]) if detail else ''}")
        n_p = sum(1 for _, s, _ in self.rows if s == "PASS")
        n_f = sum(1 for _, s, _ in self.rows if s == "FAIL")
        n_s = sum(1 for _, s, _ in self.rows if s == "SKIP")
        _safe_print(f"\n  pass={n_p}  fail={n_f}  skip={n_s}  run_id={run_id}")


# ---------------------------------------------------------------------------
# check() — top-level helper (brief §Shape)
# ---------------------------------------------------------------------------

def check(res: Results, label: str, ok: bool, detail: str = "", *, on_fail: str | None = None) -> bool:
    """Record PASS or FAIL; return ok."""
    status = "PASS" if ok else "FAIL"
    shown = detail if (ok or on_fail is None) else on_fail
    res.rows.append((label, status, shown))
    _safe_print(f"  [{status}] {label}{(': ' + shown) if shown else ''}", flush=True)
    return ok


# ---------------------------------------------------------------------------
# After-6 items (2, 3, 4, 5, 8b, 9): all items are now implemented.
# S8 fix: placeholder factories and dead constants removed.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------


# After-6 cleanup (ordered, signal-held, one finally)
# Steps 1-7; stages 2-3 fill in steps 4 (items 4/5) and 5's 8b investigations.
# ---------------------------------------------------------------------------

def _after6_cleanup(
    res: Results,
    *,
    manifest: dict,
    manifest_dir: str | None,
    stage: str,
    idp: Any | None = None,
    pool_id: str | None = None,
    mode: str = "after-6",
    **_: Any,
) -> None:
    """Ordered cleanup for all after-6 items; run once from a finally with signals held.

    Steps (design §Cleanup):
    1. Re-enable event source if the manifest says it was disabled (stage 3).
    2. Delete LIVETEST- pattern rows (stage 2).
    3. Restore cap counters this run moved (item 3).
    4. Undo items 4/5 writes (stage 2).
    5. Delete item 2/3 investigations, locks and job Findings.
    6. Delete temp users.
    7. Residue scan.

    Each step runs in its own try so an error is that step's FAIL and later steps run.
    Records cleanup rows after the with-block exits (live-check-cleanup-traps rule).
    """
    import boto3  # noqa: PLC0415
    from decimal import Decimal as _Dec  # noqa: PLC0415

    def _step(label: str, fn: Any) -> tuple[bool, Any]:
        try:
            return True, fn()
        except Exception as exc:  # noqa: BLE001
            check(res, label, False, on_fail=f"{type(exc).__name__}: {exc}")
            return False, None

    run_id = manifest.get("run_id", "")
    ddb = boto3.resource("dynamodb", region_name=_AVX_REGION)
    inv_tbl = ddb.Table(_INVESTIGATIONS_TABLE.format(stage=stage))

    # ── Step 1: Re-enable event source if the manifest says it was disabled ─
    esm_uuid = manifest.get("event_source_uuid", "")
    if manifest.get("event_source_disabled"):
        if not esm_uuid:
            check(res, "after6-cleanup-esm-reenable",
                  False, on_fail="manifest event_source_disabled=True but no UUID; cannot re-enable")
        else:
            import boto3 as _b3_esm  # noqa: PLC0415
            def _reenable_esm() -> None:
                lam_ctl = _b3_esm.client("lambda", region_name=_AVX_REGION)
                _update_esm(lam_ctl, esm_uuid, True)
                # C5 fix: poll to Enabled instead of immediate readback
                final = _wait_esm_state(lam_ctl, esm_uuid, "Enabled", max_s=120)
                if final != "Enabled":
                    raise RuntimeError(
                        f"ESM {esm_uuid!r} re-enable timed out; final State={final!r}"
                    )
            ok, _ = _step("after6-cleanup-esm-reenable", _reenable_esm)
            if ok:
                check(res, "after6-cleanup-esm-reenable", True,
                      f"ESM {esm_uuid!r} re-enabled and verified")
                manifest["event_source_disabled"] = False
                _save_manifest(run_id, manifest, manifest_dir)
    else:
        # event_source_disabled=False: nothing to re-enable
        check(res, "after6-cleanup-esm-nothing-to-clean", True,
              "ESM was not disabled by this run (event_source_disabled=False)")

    # ── Step 2: Delete LIVETEST- pattern rows (items 4/5) ──────────────────
    ddb_adp_cleanup = boto3.resource("dynamodb", region_name=_ADP_REGION)
    for pat_rec in manifest.get("item4_patterns", []):
        pid = pat_rec.get("pattern_id", "")
        if not pid:
            continue
        pat_tbl_c = ddb_adp_cleanup.Table(_PATTERNS_TABLE.format(stage=stage))

        def _del_pattern(pattern_id: str = pid) -> None:
            try:
                pat_tbl_c.delete_item(
                    Key={"pattern_id": pattern_id},
                    ConditionExpression="livetest_run_id = :rid",
                    ExpressionAttributeValues={":rid": run_id},
                )
            except pat_tbl_c.meta.client.exceptions.ConditionalCheckFailedException:
                # Re-read: absent = already deleted (PASS); present = not ours
                item = pat_tbl_c.get_item(
                    Key={"pattern_id": pattern_id}, ConsistentRead=True
                ).get("Item")
                if item is not None:
                    raise RuntimeError(
                        f"pattern {pattern_id!r} present but not ours "
                        f"(livetest_run_id={item.get('livetest_run_id')!r})"
                    )

        _step(f"after6-cleanup-del-pattern-{pid}", _del_pattern)

    # ── Step 3: Restore cap counters ────────────────────────────────────────
    cap_info = manifest.get("item3_cap")
    if cap_info and cap_info.get("moved"):
        date = cap_info.get("date", "")
        before_count = cap_info.get("before_count")
        cap_key = f"cap#{date}"

        def _restore_cap() -> None:
            if before_count is None:
                # Cap was absent before → delete with condition count = 20 (item 3 set it to cap)
                # W2 fix: item 3 sets count=20 (the cap value), not count=1, so condition must be 20
                inv_tbl.delete_item(
                    Key={"investigation_id": cap_key},
                    ConditionExpression="#cnt = :cap",
                    ExpressionAttributeNames={"#cnt": "count"},
                    ExpressionAttributeValues={":cap": _Dec("20")},
                )
            else:
                n = _Dec(str(before_count))
                inv_tbl.update_item(
                    Key={"investigation_id": cap_key},
                    UpdateExpression="SET #cnt = :n",
                    ConditionExpression="#cnt = :cap",
                    ExpressionAttributeNames={"#cnt": "count"},
                    ExpressionAttributeValues={":n": n, ":cap": _Dec("20")},
                )

        _step("after6-cleanup-cap-restore", _restore_cap)

    # ── Step 3b: Return cap slots taken by items 2 and 8b (W4 fix) ─────────
    # Each entry in cap_slots_taken: {"date", "investigation_id"}.
    # Flag cap_slot_returning before the DDB call; cap_slot_returned on success.
    # A failed condition (count<1) or any error → FAIL row; slot stays unreturned.
    for slot in manifest.get("cap_slots_taken", []):
        if slot.get("cap_slot_returned"):
            continue  # already returned; idempotent
        cap_date_s = slot.get("date", "")
        inv_id_s = slot.get("investigation_id", "")
        if not cap_date_s:
            check(res, f"after6-cleanup-cap-slot-{(inv_id_s or '?')[:8]}", False,
                  on_fail="cap slot recorded with no date; cannot return it")
            continue
        cap_key_s = f"cap#{cap_date_s}"
        if slot.get("cap_slot_returning"):
            # An earlier attempt sent the decrement and its outcome is unknown (it may
            # have landed): never send a second one.  The operator checks the counter.
            check(res, f"after6-cleanup-cap-slot-{cap_key_s}-{inv_id_s[:8]}", False,
                  on_fail=(f"an earlier return of this slot on {cap_key_s} has an unknown "
                           f"outcome; not retried (read the counter by hand)"))
            continue
        slot["cap_slot_returning"] = True
        _save_manifest(run_id, manifest, manifest_dir)

        def _return_slot(
            k: str = cap_key_s,
            _slot: dict = slot,
        ) -> None:
            inv_tbl.update_item(
                Key={"investigation_id": k},
                UpdateExpression="SET #cnt = #cnt - :one",
                ConditionExpression="#cnt >= :one",
                ExpressionAttributeNames={"#cnt": "count"},
                ExpressionAttributeValues={":one": _Dec("1")},
            )
            _slot["cap_slot_returned"] = True

        lbl = f"after6-cleanup-cap-slot-{cap_key_s}-{inv_id_s[:8]}"
        ok, _ = _step(lbl, _return_slot)
        if ok:
            _save_manifest(run_id, manifest, manifest_dir)

    # ── Step 4: Undo items 4/5 writes ───────────────────────────────────────
    ddb_adp4 = boto3.resource("dynamodb", region_name=_ADP_REGION)
    s3_adp4 = boto3.client("s3", region_name=_ADP_REGION)
    ddb_avx4 = boto3.resource("dynamodb", region_name=_AVX_REGION)
    actions_tbl4 = ddb_avx4.Table(_ACTIONS_TABLE.format(stage=stage))
    findings_tbl4 = ddb_avx4.Table(_FINDINGS_TABLE.format(stage=stage))

    # 4a. Delete Actions from item 4/5.
    for act_rec in (manifest.get("item4_actions", []) + manifest.get("item5_actions", [])):
        aid = act_rec.get("action_id", "")
        if not aid:
            continue

        def _del_action(action_id: str = aid, fid: str = act_rec.get("finding_id", "")) -> None:
            # Fix round 2 (e2e): owned by this run's Finding (the approve route writes
            # ``finding_id`` on the Action); a re-run after an earlier cleanup finds it gone.
            if not fid:
                raise RuntimeError(f"manifest Action {action_id!r} has no finding_id")
            try:
                actions_tbl4.delete_item(
                    Key={"action_id": action_id},
                    ConditionExpression="finding_id = :fid",
                    ExpressionAttributeValues={":fid": fid},
                )
            except actions_tbl4.meta.client.exceptions.ConditionalCheckFailedException:
                item = actions_tbl4.get_item(
                    Key={"action_id": action_id}, ConsistentRead=True).get("Item")
                if item is not None:
                    raise RuntimeError(
                        f"Action {action_id!r} present with finding_id "
                        f"{item.get('finding_id')!r}, not this run's {fid!r}; not ours")

        _step(f"after6-cleanup-del-action-{aid[:8]}", _del_action)

    # 4b. Delete thread versions from ADP DDB and their S3 lake copies.
    thread_tbl4 = ddb_adp4.Table(_DIGITAL_THREAD_TABLE.format(stage=stage))
    for tv_rec in manifest.get("item4_thread_versions", []):
        tid = tv_rec.get("thread_id", "")
        ver = tv_rec.get("version")
        if not tid or ver is None:
            continue
        action_id_ref = tv_rec.get("action_id", "")

        def _del_thread_ver(thread_id: str = tid, version: Any = ver,
                            act_ref: str = action_id_ref) -> None:
            cond = "attribute_exists(thread_id)"
            expr_vals: dict = {}
            if act_ref:
                cond += " AND action_id = :aid"
                expr_vals[":aid"] = act_ref
            kwargs: dict = {
                "Key": {"thread_id": thread_id, "version": version},
                "ConditionExpression": cond,
            }
            if expr_vals:
                kwargs["ExpressionAttributeValues"] = expr_vals
            try:
                thread_tbl4.delete_item(**kwargs)
            except thread_tbl4.meta.client.exceptions.ConditionalCheckFailedException:
                item = thread_tbl4.get_item(
                    Key={"thread_id": thread_id, "version": version},
                    ConsistentRead=True,
                ).get("Item")
                if item is not None:
                    raise RuntimeError(
                        f"thread {thread_id!r} v{version} present but action_id mismatch"
                    )
            # Delete S3 lake copy for this version.
            lake_key = f"{_THREAD_LAKE_PREFIX}{thread_id}.v{version}.json"
            thread_lake_bucket = manifest.get("item4_thread_lake_bucket", "")
            if thread_lake_bucket:
                try:
                    s3_adp4.delete_object(Bucket=thread_lake_bucket, Key=lake_key)
                except Exception:  # noqa: BLE001
                    pass  # lake copy is advisory; failure is not fatal

        _step(f"after6-cleanup-del-thread-v{ver}-{tid[:8]}", _del_thread_ver)

    # 4c. Delete S3 lake objects explicitly recorded in manifest.
    for obj in manifest.get("item4_s3_lake_objects", []):
        bucket, key = obj.get("bucket", ""), obj.get("key", "")
        if not bucket or not key:
            continue

        def _del_lake(b: str = bucket, k: str = key) -> None:
            s3_adp4.delete_object(Bucket=b, Key=k)

        _step(f"after6-cleanup-del-lake-{key[-32:]}", _del_lake)

    # 4d. Delete VIN objects (versioned bucket — report remaining versions).
    for obj in manifest.get("item4_vin_objects", []):
        bucket, key = obj.get("bucket", ""), obj.get("key", "")
        if not bucket or not key:
            continue

        def _del_vin_obj(b: str = bucket, k: str = key) -> None:
            s3_adp4.delete_object(Bucket=b, Key=k)
            # Confirm: on a versioned bucket a delete marker remains; report it.
            vresp = s3_adp4.list_object_versions(Bucket=b, Prefix=k)
            versions = vresp.get("Versions", [])
            markers = vresp.get("DeleteMarkers", [])
            live = [v for v in versions if not v.get("IsDeleteMarker")]
            if live:
                raise RuntimeError(
                    f"VIN object {k!r} in {b!r} still has {len(live)} live version(s) "
                    f"(versioned bucket; delete markers: {len(markers)})"
                )

        _step(f"after6-cleanup-del-vin-{key[-24:]}", _del_vin_obj)

    # 4e. Delete recall S3 objects.
    for obj in manifest.get("item4_recall_s3_objects", []):
        bucket, key = obj.get("bucket", ""), obj.get("key", "")
        if not bucket or not key:
            continue

        def _del_recall_s3(b: str = bucket, k: str = key) -> None:
            s3_adp4.delete_object(Bucket=b, Key=k)

        _step(f"after6-cleanup-del-recall-s3-{key[-24:]}", _del_recall_s3)

    # 4f. Delete draft rows (OTA and recall_campaigns_issued).
    for draft_rec in manifest.get("item4_draft_rows", []):
        cid = draft_rec.get("campaign_id", "")
        tbl_name = draft_rec.get("table", "")
        if not cid or not tbl_name:
            continue
        draft_tbl = ddb_adp4.Table(tbl_name)

        def _del_draft(campaign_id: str = cid, tbl: Any = draft_tbl) -> None:
            try:
                tbl.delete_item(
                    Key={"campaign_id": campaign_id},
                    ConditionExpression="attribute_exists(campaign_id)",
                )
            except tbl.meta.client.exceptions.ConditionalCheckFailedException:
                item = tbl.get_item(
                    Key={"campaign_id": campaign_id}, ConsistentRead=True
                ).get("Item")
                if item is not None:
                    raise RuntimeError(
                        f"draft {campaign_id!r} present but ConditionCheck failed"
                    )

        _step(f"after6-cleanup-del-draft-{cid[:20]}", _del_draft)

    # 4g. Delete item 4/5 Findings (investigation + quality.issue).
    for fnd_rec in (manifest.get("item4_findings", []) + manifest.get("item5_findings", [])):
        fid = fnd_rec.get("finding_id", "")
        if not fid:
            continue

        def _del_finding4(finding_id: str = fid) -> None:
            try:
                findings_tbl4.delete_item(
                    Key={"finding_id": finding_id},
                    ConditionExpression="attribute_exists(finding_id)",
                )
            except findings_tbl4.meta.client.exceptions.ConditionalCheckFailedException:
                item = findings_tbl4.get_item(
                    Key={"finding_id": finding_id}, ConsistentRead=True
                ).get("Item")
                if item is not None:
                    raise RuntimeError(
                        f"finding {finding_id!r} present but ConditionCheck failed"
                    )

        _step(f"after6-cleanup-del-finding4-{fid[:8]}", _del_finding4)

    # ── Step 5: Delete item 2/3 investigations, locks and job Findings ──────
    findings_tbl = ddb.Table(_FINDINGS_TABLE.format(stage=stage))
    user_sub = manifest.get("user_sub", "")
    for rec in manifest.get("investigations", []):
        inv_id = rec.get("investigation_id")
        pgid = rec.get("pattern_group_id", "")
        if not inv_id:
            continue
        # W4: use this entry's own sub; fall back to manifest["user_sub"] for old entries
        # (old entries created before this fix have no "requested_by" field).
        rec_sub = rec.get("requested_by") or user_sub
        # W4: read the live row (consistent) before deleting to pick up any finding_id
        # the job wrote after a 202 (item 3b: job runs the investigation without
        # the manifest ever recording finding_id for it).
        # Cycle 4 W2 (architect): the read goes through ``_step`` so one DynamoDB error is
        # that row's FAIL, not the end of the cleanup; on an error fall back to the
        # manifest's ``finding_id``.
        _ok_read, _live_row = _step(
            f"after6-cleanup-read-inv-{inv_id[:8]}",
            lambda iid=inv_id: inv_tbl.get_item(
                Key={"investigation_id": iid}, ConsistentRead=True
            ).get("Item") or {},
        )
        _live_finding_id = ((_live_row or {}).get("finding_id")
                            or rec.get("finding_id"))

        # Delete the investigation row (W4: conditional on this entry's principal's sub)
        def _del_inv(iid: str = inv_id, sub: str = rec_sub) -> None:
            try:
                inv_tbl.delete_item(
                    Key={"investigation_id": iid},
                    ConditionExpression="requested_by = :sub",
                    ExpressionAttributeValues={":sub": sub},
                )
            except Exception as exc:  # noqa: BLE001
                code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
                if code == "ConditionalCheckFailedException":
                    # Re-read: absent = done; present = not ours (leave it, FAIL)
                    item = inv_tbl.get_item(
                        Key={"investigation_id": iid}, ConsistentRead=True
                    ).get("Item")
                    if item is not None:
                        raise RuntimeError(
                            f"investigation {iid!r} present but requested_by mismatch "
                            f"(requested_by={item.get('requested_by')!r}; not ours)"
                        )
                else:
                    raise

        _step(f"after6-cleanup-del-inv-{inv_id[:8]}", _del_inv)

        # Delete the lock only if it still points to this run's investigation.
        lock_key = f"lock#{pgid}"

        def _del_lock(lk: str = lock_key, iid: str = inv_id) -> None:
            # Conditional: only if investigation_id_ref == this run's id.
            try:
                inv_tbl.delete_item(
                    Key={"investigation_id": lk},
                    ConditionExpression="investigation_id_ref = :iid",
                    ExpressionAttributeValues={":iid": iid},
                )
            except Exception as exc:  # noqa: BLE001
                code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
                if code != "ConditionalCheckFailedException":
                    raise
                # Lock already points to a different investigation: not ours.
                item = inv_tbl.get_item(
                    Key={"investigation_id": lk}, ConsistentRead=True).get("Item")
                if item and item.get("investigation_id_ref") != iid:
                    pass  # another run's lock; leave it
                # else absent → already gone; OK

        _step(f"after6-cleanup-del-lock-{pgid[:8]}", _del_lock)

        # Delete the job's Finding for this investigation. W4: use the live row's
        # finding_id (picked up by the consistent read above) so item 3b's cap-probe
        # investigation is covered even though the manifest entry never recorded it.
        finding_id = _live_finding_id
        if finding_id:
            def _del_finding(fid: str = finding_id, iid: str = inv_id) -> None:
                # Fix round 2 (e2e): the investigation Finding carries the investigation id
                # as ``subject_key`` (agents/fi_quality/write.py build_investigation_finding);
                # it has no top-level ``investigation_id``, so conditioning on that attribute
                # refused every delete and left each Finding behind.
                try:
                    findings_tbl.delete_item(
                        Key={"finding_id": fid},
                        ConditionExpression="subject_key = :iid",
                        ExpressionAttributeValues={":iid": iid},
                    )
                except Exception as exc:  # noqa: BLE001
                    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
                    if code == "ConditionalCheckFailedException":
                        # Re-read: absent = done; present but different iid = not ours
                        item = findings_tbl.get_item(
                            Key={"finding_id": fid}, ConsistentRead=True
                        ).get("Item")
                        if item is not None:
                            raise RuntimeError(
                                f"finding {fid!r} present but subject_key "
                                f"{item.get('subject_key')!r} is not this run's "
                                f"investigation {iid!r}; not ours"
                            )
                    else:
                        raise

            _step(f"after6-cleanup-del-finding-{finding_id[:8]}",
                  _del_finding)

    # ── Step 6: Delete temp users ────────────────────────────────────────────
    if idp and pool_id:
        for username in list(manifest.get("users", [])):
            def _del_user(un: str = username) -> None:
                try:
                    _delete_user(idp, pool_id, un)
                except Exception as exc:  # noqa: BLE001
                    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
                    if code == "UserNotFoundException":
                        return
                    raise

            ok, _ = _step(f"after6-cleanup-del-user-{username[:20]}", _del_user)
            if ok:
                # Confirm user absent by read.
                ok2, gone = _step(
                    f"after6-confirm-user-absent-{username[:20]}",
                    lambda un=username: idp.list_users(
                        UserPoolId=pool_id,
                        Filter=f'email = "{un}"',
                    ).get("Users", []),
                )
                if ok2:
                    check(res, f"after6-confirm-user-absent-{username[:20]}",
                          not gone,
                          f"user absent ({username})",
                          on_fail=f"user still present: {username}")

    # ── Step 7: Residue scan ─────────────────────────────────────────────────
    _after6_residue_scan(res, manifest=manifest, stage=stage, inv_tbl=inv_tbl)

    # Record cleanup completion in manifest.
    manifest["cleanup_done"] = True
    if run_id:
        try:
            _save_manifest(run_id, manifest, manifest_dir)
        except Exception:  # noqa: BLE001
            pass


def _after6_residue_scan(
    res: Results,
    *,
    manifest: dict,
    stage: str,
    inv_tbl: Any = None,
) -> None:
    """Step 7: residue scan for this run's ids — all tables/buckets touched.

    Checks that none of this run's rows remain (item 2/3 investigations and
    item 4/5 pattern rows, thread versions, draft rows, Findings, Actions, VIN/lake
    S3 objects). A remaining row/object is a FAIL.
    Stage 2 extension: also scans ADP and AVX tables for item 4/5 writes.
    """
    import boto3  # noqa: PLC0415
    run_id = manifest.get("run_id", "")
    remaining: list[str] = []

    # Resolve inv_tbl if not passed (caller may omit it when called from tests).
    if inv_tbl is None:
        inv_tbl = boto3.resource("dynamodb", region_name=_AVX_REGION).Table(
            _INVESTIGATIONS_TABLE.format(stage=stage)
        )

    # Item 2/3: investigation rows and locks.
    for rec in manifest.get("investigations", []):
        inv_id = rec.get("investigation_id")
        pgid = rec.get("pattern_group_id", "")
        if inv_id:
            try:
                item = inv_tbl.get_item(
                    Key={"investigation_id": inv_id}, ConsistentRead=True
                ).get("Item")
                if item is not None:
                    remaining.append(f"inv-row:{inv_id[:8]}")
            except Exception:  # noqa: BLE001
                pass
        if pgid:
            lk = f"lock#{pgid}"
            try:
                item = inv_tbl.get_item(
                    Key={"investigation_id": lk}, ConsistentRead=True
                ).get("Item")
                if item and item.get("investigation_id_ref") in (
                    rec.get("investigation_id"), run_id
                ):
                    remaining.append(f"lock:{pgid[:8]}")
            except Exception:  # noqa: BLE001
                pass

    # Item 4/5: LIVETEST- pattern rows in ADP.
    ddb_adp_r = boto3.resource("dynamodb", region_name=_ADP_REGION)
    pat_tbl_r = ddb_adp_r.Table(_PATTERNS_TABLE.format(stage=stage))
    for pat_rec in manifest.get("item4_patterns", []):
        pid = pat_rec.get("pattern_id", "")
        if not pid:
            continue
        try:
            item = pat_tbl_r.get_item(
                Key={"pattern_id": pid}, ConsistentRead=True
            ).get("Item")
            if item is not None:
                remaining.append(f"livetest-pattern:{pid}")
        except Exception:  # noqa: BLE001
            pass

    # Item 4/5: thread versions in ADP.
    thread_tbl_r = ddb_adp_r.Table(_DIGITAL_THREAD_TABLE.format(stage=stage))
    for tv_rec in manifest.get("item4_thread_versions", []):
        tid = tv_rec.get("thread_id", "")
        ver = tv_rec.get("version")
        if not tid or ver is None:
            continue
        try:
            item = thread_tbl_r.get_item(
                Key={"thread_id": tid, "version": ver}, ConsistentRead=True
            ).get("Item")
            if item is not None:
                remaining.append(f"thread:{tid[:8]}:v{ver}")
        except Exception:  # noqa: BLE001
            pass

    # Item 4 unk: verify no thread row for unknown-option decisions (S9).
    # The unk branch `continue`s in _item_4 before any thread write; the
    # derived thread_id is saved in item4_unk_thread_ids for this check.
    for _unk_tid in manifest.get("item4_unk_thread_ids", []):
        if not _unk_tid:
            continue
        try:
            _unk_rows = thread_tbl_r.query(
                KeyConditionExpression="thread_id = :t",
                ExpressionAttributeValues={":t": _unk_tid},
                ConsistentRead=True,
            ).get("Items", [])
            if _unk_rows:
                remaining.append(f"unk-thread:{_unk_tid[:8]}")
        except Exception:  # noqa: BLE001
            pass

    # Item 4/5: draft rows (OTA and recall_campaigns_issued).
    for draft_rec in manifest.get("item4_draft_rows", []):
        cid = draft_rec.get("campaign_id", "")
        tbl_name = draft_rec.get("table", "")
        if not cid or not tbl_name:
            continue
        try:
            item = ddb_adp_r.Table(tbl_name).get_item(
                Key={"campaign_id": cid}, ConsistentRead=True
            ).get("Item")
            if item is not None:
                remaining.append(f"draft:{cid[:20]}")
        except Exception:  # noqa: BLE001
            pass

    # Item 4/5: Findings in AVX (investigation + quality.issue).
    ddb_avx_r = boto3.resource("dynamodb", region_name=_AVX_REGION)
    findings_tbl_r = ddb_avx_r.Table(_FINDINGS_TABLE.format(stage=stage))
    all_item45_findings = (
        manifest.get("item4_findings", []) + manifest.get("item5_findings", [])
    )
    for fnd_rec in all_item45_findings:
        fid = fnd_rec.get("finding_id", "")
        if not fid:
            continue
        try:
            item = findings_tbl_r.get_item(
                Key={"finding_id": fid}, ConsistentRead=True
            ).get("Item")
            if item is not None:
                remaining.append(f"finding4:{fid[:8]}")
        except Exception:  # noqa: BLE001
            pass

    # Item 4/5: Actions in AVX.
    actions_tbl_r = ddb_avx_r.Table(_ACTIONS_TABLE.format(stage=stage))
    all_item45_actions = (
        manifest.get("item4_actions", []) + manifest.get("item5_actions", [])
    )
    for act_rec in all_item45_actions:
        aid = act_rec.get("action_id", "")
        if not aid:
            continue
        try:
            item = actions_tbl_r.get_item(
                Key={"action_id": aid}, ConsistentRead=True
            ).get("Item")
            if item is not None:
                remaining.append(f"action4:{aid[:8]}")
        except Exception:  # noqa: BLE001
            pass

    # Cap slots: any slot taken but not returned is residue
    for slot in manifest.get("cap_slots_taken", []):
        if not slot.get("cap_slot_returned"):
            cap_date_slot = slot.get("date", "?")
            inv_id_slot = (slot.get("investigation_id") or "?")[:8]
            remaining.append(f"cap-slot:{cap_date_slot}:{inv_id_slot}")

    check(
        res, "after6-residue-scan",
        not remaining,
        "no residue after cleanup",
        on_fail=f"{len(remaining)} item(s) remain after cleanup: {remaining}",
    )


# ---------------------------------------------------------------------------
# _derive_thread_id — byte-identical copy of ADP digital_thread_writer/handler.py
# _derive_thread_id (02b8716f lines 813-819).
# Pinned by test_pin_thread_id_against_adp_dtw in test_live_verify_fi_quality_after6_s2b.py.
# ---------------------------------------------------------------------------

def _derive_thread_id(pattern_id: str, investigation_finding_id: str) -> str:
    """Deterministic, stable hash of (pattern_id, investigation_finding_id).

    Byte-identical copy of ADP 02b8716f digital_thread_writer/handler.py
    _derive_thread_id.  Pinned against ADP by test_pin_thread_id_against_adp_dtw.
    """
    import hashlib  # noqa: PLC0415
    key = f"{pattern_id}|{investigation_finding_id}"
    return "thread-" + hashlib.sha256(key.encode()).hexdigest()[:16]


def _make_livetest_vins(seed: str, n: int) -> list[str]:
    """Return *n* sorted unique 17-char VINs using valid VIN chars [A-HJ-NPR-Z0-9].

    Deterministic for a given seed; used by item 4 to generate synthetic VINs that
    pass the writer's ``affected_vins_from_item`` check and match no real vehicle.
    Source: digital_thread_writer/handler.py _VIN_RE = r"[A-HJ-NPR-Z0-9]{17}".
    """
    _CH = "ABCDEFGHJKMNPRSTUVWXYZ0123456789"  # 32 valid VIN chars (no I, O, Q)
    import hashlib as _hlib  # noqa: PLC0415
    out: set[str] = set()
    i = 0
    while len(out) < n:
        h = _hlib.sha256(f"{seed}-{i}".encode()).digest()
        vin = "".join(_CH[b % len(_CH)] for b in h[:17])
        out.add(vin)
        i += 1
    return sorted(out)

def _item_2(
    res: Results,
    *,
    stage: str,
    run_id: str,
    manifest: dict,
    manifest_dir: str | None,
    allow_writes: bool,
    answer_key: str | None = None,
    **_: Any,
) -> None:
    """Item 2: each planted pattern leads to an investigation naming the planted cause and FM.

    Design §Item 2.  Fixes applied: C1 (pattern_group_id), C2 (root-cause candidate check),
    C3 (pattern numbers + corpus options), M6 (failure mode), W1 (active denominator),
    W2 (conditional cap restore), W3 (confirm user absent), S6 (date from 202 requested_at).
    """
    import boto3  # noqa: PLC0415
    if not allow_writes:
        res.skip("item-2", "writes to staging; needs --allow-writes")
        return
    if manifest is None:
        res.skip("item-2", "no manifest: run --part after-6 to create one")
        return
    if not answer_key:
        res.skip("item-2", "needs --answer-key <local parquet>")
        return

    # Write-window guard (before any AWS call)
    now = _utc_now()
    scheduler = boto3.client("scheduler", region_name=_AVX_REGION)
    window_reason = _after6_write_window_guard(scheduler, stage, now)
    if window_reason:
        res.skip("item-2", f"write window: {window_reason}")
        return

    # Pre-check guards
    sqs = boto3.client("sqs", region_name=_AVX_REGION)
    lam_c = boto3.client("lambda", region_name=_AVX_REGION)
    pre_reason = _after6_pre_checks(sqs, lam_c, stage, wait_for_drain=True)
    if pre_reason:
        res.skip("item-2", f"pre-check: {pre_reason}")
        return

    # Load answer key (planted patterns — parquet from ADP tests)
    ak_rows = _load_answer_key(answer_key)
    if not check(res, "item-2-answer-key",
                 bool(ak_rows) and all(_AK_COLUMNS <= set(r) for r in ak_rows),
                 f"{len(ak_rows)} planted patterns",
                 on_fail=f"answer key empty or missing columns {sorted(_AK_COLUMNS)}"):
        return

    # Scan ADP DDB for live pattern rows (us-east-1), ignoring LIVETEST rows.
    pat_tbl = boto3.resource("dynamodb", region_name=_ADP_REGION).Table(
        _PATTERNS_TABLE.format(stage=stage)
    )
    live_rows = [
        r for r in _scan_all(pat_tbl)
        if not str(r.get("pattern_id", "")).startswith("LIVETEST-")
    ]
    print("  [NOTE] item-2: FI_INVESTIGATION_MODEL_ENABLED=false; job runs rules fallback only")

    # API URL
    cfn = boto3.client("cloudformation", region_name=_AVX_REGION)
    outputs = {
        o["OutputKey"]: o["OutputValue"]
        for o in cfn.describe_stacks(StackName=_VSA_API_STACK.format(stage=stage))
                   ["Stacks"][0]["Outputs"]
    }
    api_url: str = outputs["RestApiUrl"]

    # Cognito setup
    idp = boto3.client("cognito-idp", region_name=_AVX_REGION)
    pool_id = _resolve_pool_id(idp)
    client_id = password_client(idp, pool_id)

    # ADP corpus — used for C3 options-numbers check (design §Item 2).
    # Read fm#{failure_mode_id} to verify each option's total = parts_cost × vehicles_affected.
    import boto3 as _b3_corpus  # noqa: PLC0415
    corpus_tbl = _b3_corpus.resource("dynamodb", region_name=_ADP_REGION).Table(
        _CORPUS_TABLE.format(stage=stage)
    )

    # Active planted patterns (those whose start_date ≤ today's date — W1)
    today_str = now.strftime("%Y-%m-%d")
    active_aks = [ak for ak in ak_rows if str(ak.get("start_date", "")) <= today_str]
    if not check(res, "item-2-active-plants", bool(active_aks),
                 f"{len(active_aks)} active planted patterns"):
        return

    user_emails: list[str] = manifest.setdefault("users", [])
    pass_count = 0
    total_count = len(active_aks)

    with principal(idp, pool_id, client_id, "eng", ["product-engineer"],
                   created_users=user_emails) as (eng_tok, eng_claims, _eng_email):

            # C2 fix: store the JWT sub so cleanup can condition on requested_by
            user_sub = eng_claims.get("sub", "")
            if user_sub and not manifest.get("user_sub"):
                manifest["user_sub"] = user_sub
                _save_manifest(run_id, manifest, manifest_dir)

            for ak in active_aks:
                pid = str(ak["pattern_id"])

                # W1: find the live pattern row that matches this planted pattern
                matched = [r for r in live_rows if _matches(r, ak)]
                if not matched:
                    check(res, f"item-2-plant-{pid}-found", False,
                          on_fail=f"no live pattern row for planted {pid} "
                                  f"({ak['code_family']} "
                                  f"{ak['cause_dimension']}={ak['cause_value']})")
                    continue

                # Use the row with the lowest candidate_rank
                pat_row = min(matched, key=lambda r: float(r.get("candidate_rank", 99)))
                pgid = str(pat_row["pattern_group_id"])

                # Record BEFORE the POST (cleanup can undo even if POST times out)
                existing = next(
                    (i for i in manifest.get("investigations", [])
                     if i.get("pattern_group_id") == pgid),
                    None,
                )
                if existing is None:
                    manifest.setdefault("investigations", []).append(
                        {"pattern_group_id": pgid, "investigation_id": None,
                         "status": None, "plant_id": pid,
                         "requested_by": user_sub}  # W4: record sub before POST
                    )
                    _save_manifest(run_id, manifest, manifest_dir)

                # POST investigation
                s, b = _item_6_http(
                    api_url, "POST", "investigations", eng_tok,
                    {"pattern_id": pgid},
                )
                inv_id = b.get("investigation_id") if isinstance(b, dict) else None
                if not check(res, f"item-2-plant-{pid}-post",
                             s in (200, 202) and bool(inv_id),
                             f"POST → {s} id={inv_id}",
                             on_fail=f"POST → {s} {b}"):
                    continue

                # C2 fix: only record 202 (this run created it) for cleanup.
                # A 200 means an existing investigation (another user's) — treat it
                # read-only and do NOT add to manifest["investigations"] for deletion.
                this_run_investigation = (s == 202)
                if this_run_investigation:
                    # Update manifest with the investigation_id
                    for rec in manifest.get("investigations", []):
                        if rec.get("pattern_group_id") == pgid:
                            rec["investigation_id"] = inv_id
                            break
                    # W4: record cap slot taken by this 202
                    cap_date_2 = (b.get("requested_at") or "")[:10] if isinstance(b, dict) else ""
                    if cap_date_2:
                        manifest.setdefault("cap_slots_taken", []).append(
                            {"date": cap_date_2, "investigation_id": inv_id}
                        )
                    _save_manifest(run_id, manifest, manifest_dir)
                else:
                    # 200: existing investigation — read-only, not ours to delete
                    check(res, f"item-2-plant-{pid}-reuse",
                          True,
                          f"POST → 200 (existing investigation {inv_id[:8]}; read-only)")

                # Poll to terminal state (bounded)
                inv_data = _poll_investigation(api_url, inv_id, eng_tok)
                if not check(res, f"item-2-plant-{pid}-terminal",
                             inv_data is not None,
                             "investigation reached terminal state",
                             on_fail=f"investigation {inv_id} timed out "
                                     f"(>{_INV_POLL_MAX_S}s)"):
                    continue

                # Update manifest with status and finding_id
                status = inv_data.get("status", "")
                finding_id_from_inv = inv_data.get("finding_id")
                for rec in manifest.get("investigations", []):
                    if rec.get("investigation_id") == inv_id:
                        rec["status"] = status
                        rec["finding_id"] = finding_id_from_inv
                        break
                _save_manifest(run_id, manifest, manifest_dir)

                if status == "failed":
                    check(res, f"item-2-plant-{pid}-completed", False,
                          on_fail=f"investigation {inv_id} failed")
                    continue

                # Read the Finding to get quality evidence (design §Item 2 / s1-notes.md).
                # quality.* fields are in the Finding's evidence list,
                # NOT in the investigation DDB row.
                if not finding_id_from_inv:
                    check(res, f"item-2-plant-{pid}-finding-id", False,
                          on_fail=f"investigation {inv_id} completed but has no finding_id")
                    continue

                import boto3 as _b3  # noqa: PLC0415
                findings_tbl = _b3.resource("dynamodb", region_name=_AVX_REGION).Table(
                    _FINDINGS_TABLE.format(stage=stage)
                )
                finding = findings_tbl.get_item(
                    Key={"finding_id": finding_id_from_inv}, ConsistentRead=True
                ).get("Item")
                if not check(res, f"item-2-plant-{pid}-finding-present",
                             finding is not None,
                             f"Finding {finding_id_from_inv} exists",
                             on_fail=f"Finding {finding_id_from_inv} not found"):
                    continue

                # Parse evidence list into a dict keyed by evidence kind suffix
                # quality.root_cause → ev["value"], quality.candidates → ev["value"], etc.
                evidence = finding.get("evidence") or []
                quality: dict[str, Any] = {}
                for ev in evidence:
                    kind = str(ev.get("kind", ""))
                    if kind.startswith("quality."):
                        quality[kind[len("quality."):]] = ev.get("value")

                # C3: quality.pattern must be present and cite the pattern's numbers
                pat_ev = quality.get("pattern") or {}
                if not check(res, f"item-2-plant-{pid}-pattern-present",
                             bool(pat_ev),
                             "quality.pattern present in Finding evidence",
                             on_fail="quality.pattern absent or empty in Finding evidence"):
                    continue

                veh_affected = pat_row.get("vehicles_affected")
                aff_count = pat_ev.get("affected_count")
                check(res, f"item-2-plant-{pid}-pattern-numbers",
                      aff_count is not None
                      and veh_affected is not None
                      and float(aff_count) == float(veh_affected),
                      f"affected_count={aff_count} == vehicles_affected={veh_affected}",
                      on_fail=f"affected_count {aff_count!r} != "
                              f"vehicles_affected {veh_affected!r}")

                # C3: quality.options must be present and non-empty
                opts = quality.get("options")
                if not check(res, f"item-2-plant-{pid}-options-present",
                             bool(opts),
                             f"{len(opts) if opts else 0} option(s)",
                             on_fail="quality.options absent or empty in Finding evidence"):
                    continue

                # C2: root-cause candidate must match the plant.
                # quality.root_cause.value has candidate_id and failure_mode_id.
                # quality.candidates.value is a list with dimension/value fields.
                rc_ev = quality.get("root_cause") or {}
                rc_cand_id = rc_ev.get("candidate_id")
                # (quality.candidates used only by other checks; rc_cand not needed here)
                # W4: ratio and p_value from quality.pattern must match the live pattern row.
                # The writer (fi_quality/write.py) copies pattern.ratio → quality.pattern.ratio
                # and pattern.p_value → quality.pattern.p_value (job.py maps rate_ratio→ratio).
                pat_ratio = pat_ev.get("ratio")
                pat_p_value = pat_ev.get("p_value")
                row_rate_ratio = float(pat_row.get("rate_ratio") or 0)
                row_p_value = float(pat_row.get("p_value") or 1)
                check(res, f"item-2-plant-{pid}-pattern-ratio",
                      pat_ratio is not None
                      and abs(float(pat_ratio) - row_rate_ratio) < 1e-6,
                      f"quality.pattern ratio={pat_ratio} == pattern_row rate_ratio={row_rate_ratio}",
                      on_fail=f"ratio {pat_ratio!r} != pattern_row rate_ratio {row_rate_ratio!r}")
                check(res, f"item-2-plant-{pid}-pattern-pvalue",
                      pat_p_value is not None
                      and abs(float(pat_p_value) - row_p_value) < 1e-9,
                      f"quality.pattern p_value={pat_p_value} == pattern_row p_value={row_p_value}",
                      on_fail=f"p_value {pat_p_value!r} != pattern_row p_value {row_p_value!r}")

                # W4: root-cause lookup matches candidate_id = pattern_id only.
                # No pattern_group_id arm: a group's OTHER row must not stand in for the
                # named candidate.  No code-family fallback: unknown candidate = FAIL.
                # (candidates.py D10: candidate_id == pattern_id for every live candidate.)
                if rc_cand_id:
                    rc_live = next(
                        (r for r in live_rows if r.get("pattern_id") == rc_cand_id),
                        None,
                    )
                    cause_ok = _matches(rc_live, ak) if rc_live is not None else False
                else:
                    cause_ok = False
                # W4: per-plant cause miss = NOTE row, not FAIL; only 80% aggregate decides.
                if not cause_ok:
                    print(f"  [NOTE] item-2-plant-{pid}: cause miss "
                          f"(candidate_id={rc_cand_id!r} not in live_rows or no match "
                          f"for {ak.get('code_family')} {ak.get('cause_dimension')}"
                          f"={ak.get('cause_value')})", flush=True)

                # M6: failure mode must match the plant's failure_mode_id.
                # W4: per-plant FM miss = NOTE row, not FAIL; only 80% aggregate decides.
                inv_fm = rc_ev.get("failure_mode_id")
                ak_fm = str(ak.get("failure_mode_id", ""))
                fm_ok = bool(inv_fm) and bool(ak_fm) and str(inv_fm) == ak_fm
                if not fm_ok:
                    print(f"  [NOTE] item-2-plant-{pid}: failure-mode miss "
                          f"(finding={inv_fm!r} plant={ak_fm!r})", flush=True)

                # C3 (continued): check each option's numbers against the corpus formula.
                # Read fm#{inv_fm} from ADP corpus DDB (deferred note in stage 1).
                # Formula (agents/fi_quality/options.py): total_cost = parts_cost × total_vehicles
                # Also: total_vehicles must equal the pattern row's vehicles_affected.
                if opts and inv_fm:
                    corpus_fm_item = None
                    try:
                        corpus_fm_item = corpus_tbl.get_item(
                            Key={"pk": f"fm#{inv_fm}"}, ConsistentRead=True
                        ).get("Item")
                    except Exception as _cex:  # noqa: BLE001
                        check(res, f"item-2-plant-{pid}-corpus-read", False,
                              on_fail=f"corpus read error: {type(_cex).__name__}: {_cex}")
                    # W4: missing corpus row is a FAIL row (not silent).
                    if corpus_fm_item is None and opts and inv_fm:
                        check(res, f"item-2-plant-{pid}-corpus-missing", False,
                              on_fail=f"corpus row fm#{inv_fm} not found in ADP corpus table "
                                      f"(table has no entry for this failure mode)")
                    if corpus_fm_item is not None:
                        corpus_opts_by_id = {
                            o.get("remedy_option_id"): o
                            for o in (corpus_fm_item.get("options") or [])
                            if o.get("remedy_option_id")
                        }
                        veh_aff = float(pat_row.get("vehicles_affected") or 0)
                        for opt in opts:
                            opt_id = str(opt.get("remedy_option_id") or "?")
                            corpus_opt = corpus_opts_by_id.get(opt_id)
                            if not check(res, f"item-2-plant-{pid}-opt-{opt_id[:12]}-corpus",
                                         corpus_opt is not None,
                                         f"opt {opt_id} in corpus fm#{inv_fm}",
                                         on_fail=f"opt {opt_id!r} not in corpus fm#{inv_fm}; "
                                                 f"known ids: {list(corpus_opts_by_id)}"):
                                continue
                            corpus_parts = float(
                                corpus_opt.get("parts_cost_per_vehicle")
                                or corpus_opt.get("parts_cost_per_vehicle_usd")
                                or 0.0
                            )
                            expected_total = corpus_parts * veh_aff
                            actual_total = float(opt.get("total_cost") or 0)
                            check(
                                res,
                                f"item-2-plant-{pid}-opt-{opt_id[:12]}-total",
                                abs(expected_total - actual_total) < 0.01,
                                f"total_cost={actual_total:.2f} == "
                                f"{corpus_parts:.2f}×{veh_aff:.0f}",
                                on_fail=f"total_cost {actual_total:.2f} != "
                                        f"corpus {corpus_parts:.2f} × "
                                        f"vehicles_affected {veh_aff:.0f} "
                                        f"= {expected_total:.2f}",
                            )
                            check(
                                res,
                                f"item-2-plant-{pid}-opt-{opt_id[:12]}-veh-count",
                                float(opt.get("total_vehicles") or 0) == veh_aff,
                                f"opt total_vehicles={opt.get('total_vehicles')} == "
                                f"vehicles_affected={veh_aff:.0f}",
                                on_fail=f"opt total_vehicles {opt.get('total_vehicles')!r} "
                                        f"!= vehicles_affected {veh_aff!r}",
                            )

                if cause_ok and fm_ok and pat_ev and opts:
                    pass_count += 1

    # Note: cleanup is NOT called here. C1 fix: cleanup is in main()'s try/finally
    # so it runs exactly once at the end of the whole after-6 part.
    # (The design says one ordered finally from main; the per-item cleanups were removed.)

    # Score: ≥80% of active planted patterns must have a passing investigation (W1)
    rate = pass_count / total_count if total_count else 0.0
    check(
        res, "item-2-cause-fm-rate",
        total_count > 0 and rate >= 0.8,
        f"{pass_count}/{total_count} active plants passed ({rate:.0%})",
        on_fail=f"{pass_count}/{total_count} active plants passed "
                f"({rate:.0%}); needs 80%",
    )

    manifest["items_passed"]["2"] = rate >= 0.8
    _save_manifest(run_id, manifest, manifest_dir)


# ---------------------------------------------------------------------------
# Item 3 — repeat returns same investigation; cap refuses when full (writes)
# ---------------------------------------------------------------------------

_CAP_SENTENCE = "The daily investigation limit has been reached. Try again tomorrow."
_PATTERN_ID_SHAPE = re.compile(r"[A-Za-z0-9-]{1,256}")
_CAP_PROBE_MAX_CANDIDATES = 50


def _pick_unlocked_real_pattern(
    pat_tbl: Any, inv_tbl: Any, exclude: set[str],
) -> str | None:
    """Item 3b's cap probe needs a real pattern with NO lock row (fix round 2, e2e).

    The investigations route checks the pattern's lock before the cap, so a POST for a
    pattern that already has a live investigation answers 200 and never reaches the cap.
    Returns the first real (non-``LIVETEST-``) pattern id, in sorted order, whose
    ``lock#<id>`` row is absent on a consistent read; None when none of the first
    ``_CAP_PROBE_MAX_CANDIDATES`` qualifies.
    """
    ids = sorted(
        str(r.get("pattern_id", "")) for r in _scan_all(pat_tbl)
        if str(r.get("pattern_id", ""))
        and not str(r.get("pattern_id", "")).startswith("LIVETEST-")
        and _PATTERN_ID_SHAPE.fullmatch(str(r.get("pattern_id", "")))
        and str(r.get("pattern_id", "")) not in exclude
    )
    for pid in ids[:_CAP_PROBE_MAX_CANDIDATES]:
        lock = inv_tbl.get_item(
            Key={"investigation_id": f"lock#{pid}"}, ConsistentRead=True).get("Item")
        if lock is None:
            return pid
    return None


def _item_3(
    res: Results,
    *,
    stage: str,
    run_id: str,
    manifest: dict,
    manifest_dir: str | None,
    allow_writes: bool,
    **_: Any,
) -> None:
    """Item 3: (a) repeat POST returns same id; (b) cap refuses past limit.

    Design §Item 3.  Fixes: W2 (conditional cap restore).
    Requires item 2 to have run and populated manifest["investigations"].
    """
    import boto3  # noqa: PLC0415
    from decimal import Decimal as _Dec  # noqa: PLC0415

    if not allow_writes:
        res.skip("item-3", "writes to staging; needs --allow-writes")
        return

    if manifest is None:
        res.skip("item-3", "no manifest: run --part after-6 to create one")
        return

    investigations = manifest.get("investigations", [])
    completed = [i for i in investigations if i.get("status") == "completed"
                 and i.get("pattern_group_id")]
    if not completed:
        res.skip("item-3", "no completed investigations from item 2; run item 2 first")
        return

    # Write-window guard (before any AWS call)
    now = _utc_now()
    scheduler = boto3.client("scheduler", region_name=_AVX_REGION)
    window_reason = _after6_write_window_guard(scheduler, stage, now)
    if window_reason:
        res.skip("item-3", f"write window: {window_reason}")
        return

    cfn = boto3.client("cloudformation", region_name=_AVX_REGION)
    outputs = {
        o["OutputKey"]: o["OutputValue"]
        for o in cfn.describe_stacks(StackName=_VSA_API_STACK.format(stage=stage))
                   ["Stacks"][0]["Outputs"]
    }
    api_url: str = outputs["RestApiUrl"]

    idp = boto3.client("cognito-idp", region_name=_AVX_REGION)
    pool_id = _resolve_pool_id(idp)
    client_id = password_client(idp, pool_id)
    user_emails: list[str] = manifest.setdefault("users", [])

    ddb = boto3.resource("dynamodb", region_name=_AVX_REGION)
    inv_tbl = ddb.Table(_INVESTIGATIONS_TABLE.format(stage=stage))

    # Use the first completed investigation for the repeat-returns-same test (a)
    target = completed[0]
    pgid = target["pattern_group_id"]
    expected_inv_id = target["investigation_id"]

    try:
        with principal(idp, pool_id, client_id, "eng", ["product-engineer"],
                       created_users=user_emails) as (eng_tok, eng_claims_3, _):

            # (a) Repeat POST: 200 with the same investigation_id
            s, b = _item_6_http(
                api_url, "POST", "investigations", eng_tok,
                {"pattern_id": pgid},
            )
            repeat_id = b.get("investigation_id") if isinstance(b, dict) else None
            check(res, "item-3-repeat-status", s == 200,
                  "repeat POST returned 200",
                  on_fail=f"repeat POST → {s} (expected 200 for live investigation)")
            check(res, "item-3-repeat-same-id",
                  bool(repeat_id) and repeat_id == expected_inv_id,
                  f"repeat returns same id {repeat_id}",
                  on_fail=f"repeat returned different id: "
                          f"got {repeat_id!r} expected {expected_inv_id!r}")

            # Verify no second row for this pattern_group_id (consistent scan)
            # W1 fix: investigation rows carry pattern_id (not pattern_group_id per handler.py:470)
            all_inv = [
                i for i in _scan_all(inv_tbl, consistent=True)
                if i.get("pattern_id") == pgid
                and not str(i.get("investigation_id", "")).startswith("lock#")
            ]
            check(res, "item-3-no-second-row",
                  len(all_inv) <= 1,
                  f"at most 1 investigation row for {pgid[:8]}",
                  on_fail=f"{len(all_inv)} investigation rows for {pgid[:8]}")

            # (b) Cap test: read cap#<date>, set to 20 (default cap), POST → 429, restore
            today_str = now.strftime("%Y-%m-%d")
            cap_key = f"cap#{today_str}"

            # Snapshot BEFORE the write (W2 fix: conditional write)
            cap_before = inv_tbl.get_item(
                Key={"investigation_id": cap_key}, ConsistentRead=True
            ).get("Item")
            before_count = int(cap_before["count"]) if cap_before else None

            # The route keys the counter on ITS UTC date; near midnight the script's date
            # and the route's can differ and the full cap would sit on the wrong day's row
            # (a real investigation would then be created). Don't try within 10 minutes.
            _mins = now.hour * 60 + now.minute
            if _mins >= 23 * 60 + 50 or _mins < 10:
                res.skip("item-3-cap", f"within 10 minutes of 00:00Z ({now.isoformat()}); "
                                       f"re-run item 3 later")
                manifest["items_passed"]["3"] = False
                _save_manifest(run_id, manifest, manifest_dir)
                return

            # The cap probe needs a real pattern with no lock (fix round 2, e2e).
            probe_pid = _pick_unlocked_real_pattern(
                boto3.resource("dynamodb", region_name=_ADP_REGION).Table(
                    _PATTERNS_TABLE.format(stage=stage)),
                inv_tbl,
                {str(r.get("pattern_group_id", ""))
                 for r in manifest.get("investigations", [])},
            )
            if not check(res, "item-3-cap-probe-pattern", probe_pid is not None,
                         f"cap probe pattern {probe_pid} has no lock",
                         on_fail="no real pattern without a lock; cap test not run"):
                manifest["items_passed"]["3"] = False
                _save_manifest(run_id, manifest, manifest_dir)
                return
            manifest["item3_cap_probe_pattern_id"] = probe_pid

            # Record the cap snapshot in manifest (BEFORE the write)
            manifest["item3_cap"] = {
                "date": today_str,
                "before_count": before_count,
                "moved": False,
            }
            _save_manifest(run_id, manifest, manifest_dir)

            # SET count = 20 conditional on the value just read (W2)
            _CAP_VAL = _Dec("20")
            try:
                if cap_before is None:
                    inv_tbl.put_item(
                        Item={"investigation_id": cap_key, "count": _CAP_VAL},
                        ConditionExpression="attribute_not_exists(investigation_id)",
                    )
                else:
                    inv_tbl.update_item(
                        Key={"investigation_id": cap_key},
                        UpdateExpression="SET #cnt = :cap",
                        ConditionExpression="#cnt = :before",
                        ExpressionAttributeNames={"#cnt": "count"},
                        ExpressionAttributeValues={
                            ":cap": _CAP_VAL,
                            ":before": _Dec(str(before_count)),
                        },
                    )
                manifest["item3_cap"]["moved"] = True
                _save_manifest(run_id, manifest, manifest_dir)
            except Exception as exc:  # noqa: BLE001
                check(res, "item-3-cap-set", False,
                      on_fail=f"failed to set cap to 20: {type(exc).__name__}: {exc}")
                # Bail out of the cap test — we didn't move the cap
                manifest["items_passed"]["3"] = False
                _save_manifest(run_id, manifest, manifest_dir)
                return

            # POST the unlocked real pattern → expect 429 and the route's exact body.
            # (The route checks the lock before the cap: item 2's group would answer 200.)
            rows_before = [
                i for i in _scan_all(inv_tbl, consistent=True)
                if i.get("pattern_id") == probe_pid
                and not str(i.get("investigation_id", "")).startswith(("lock#", "cap#"))
            ]
            s429, b429 = _item_6_http(
                api_url, "POST", "investigations", eng_tok,
                {"pattern_id": probe_pid},
            )
            cap429_ok = (s429 == 429 and isinstance(b429, dict)
                         and b429.get("error") == _CAP_SENTENCE)
            check(res, "item-3-cap-429",
                  cap429_ok,
                  "POST with full cap returned 429 with the cap sentence",
                  on_fail=f"POST with full cap → {s429} {b429}; expected 429 {_CAP_SENTENCE!r}")
            _iid3 = b429.get("investigation_id") if isinstance(b429, dict) else None
            if s429 == 202 and _iid3:
                # The cap did not hold and this run created a real investigation: record
                # it as this run's so the cleanup deletes it (a 200 would be someone else's).
                # W4: record item 3's principal sub so step 5 conditions on the right sub.
                _sub3 = (eng_claims_3 or {}).get("sub", "")
                manifest.setdefault("investigations", []).append(
                    {"pattern_group_id": probe_pid, "investigation_id": _iid3,
                     "status": None, "plant_id": "item3-cap-probe",
                     "requested_by": _sub3})
                # W4: record the cap slot taken by this 202 (same pattern as item 2).
                _cap_date_3 = (
                    (b429.get("requested_at") or "")[:10]
                    if isinstance(b429, dict) else ""
                )
                if _cap_date_3:
                    manifest.setdefault("cap_slots_taken", []).append(
                        {"date": _cap_date_3, "investigation_id": _iid3}
                    )
                _save_manifest(run_id, manifest, manifest_dir)

            # No row and no lock for the probe pattern
            rows_after = [
                i for i in _scan_all(inv_tbl, consistent=True)
                if i.get("pattern_id") == probe_pid
                and not str(i.get("investigation_id", "")).startswith(("lock#", "cap#"))
            ]
            lock_after = inv_tbl.get_item(
                Key={"investigation_id": f"lock#{probe_pid}"}, ConsistentRead=True
            ).get("Item")
            check(res, "item-3-cap-no-row",
                  len(rows_after) == len(rows_before) and lock_after is None,
                  "no investigation row or lock for the 429 POST",
                  on_fail=(f"{len(rows_after)} rows after cap-hit (was {len(rows_before)}); "
                           f"lock present={lock_after is not None}"))

            # Restore cap (W2 fix: conditional on count = 20)
            def _restore_cap() -> None:
                if before_count is None:
                    inv_tbl.delete_item(
                        Key={"investigation_id": cap_key},
                        ConditionExpression="#cnt = :cap",
                        ExpressionAttributeNames={"#cnt": "count"},
                        ExpressionAttributeValues={":cap": _CAP_VAL},
                    )
                else:
                    inv_tbl.update_item(
                        Key={"investigation_id": cap_key},
                        UpdateExpression="SET #cnt = :before",
                        ConditionExpression="#cnt = :cap",
                        ExpressionAttributeNames={"#cnt": "count"},
                        ExpressionAttributeValues={
                            ":before": _Dec(str(before_count)),
                            ":cap": _CAP_VAL,
                        },
                    )

            try:
                _restore_cap()
                manifest["item3_cap"]["moved"] = False  # restored
                _save_manifest(run_id, manifest, manifest_dir)
            except Exception as exc:  # noqa: BLE001
                check(res, "item-3-cap-restore", False,
                      on_fail=f"cap restore failed: {type(exc).__name__}: {exc}")

            manifest["items_passed"]["3"] = cap429_ok
            _save_manifest(run_id, manifest, manifest_dir)
    finally:  # noqa: B009 — cleanup is in main()'s try/finally; nothing to do here
        pass


# ---------------------------------------------------------------------------
# Items 4 and 5 — record_engineering_decision and issue_software_campaign
# ---------------------------------------------------------------------------


def _item_4(
    res: Results,
    *,
    stage: str,
    run_id: str,
    manifest: dict,
    manifest_dir: str | None,
    allow_writes: bool,
    **_: Any,
) -> None:
    """Item 4: approve record_engineering_decision for 4 LIVETEST- patterns.

    Seeds LIVETEST-4-<run>-{sw,hw,mon,unk} rows in adp-staging-fault-patterns and
    engineering-corpus, builds investigation Findings via build_investigation_finding,
    and approves each through POST /findings/{id}/approve.

    Checks: M9 (action executed), C8 (monitor: no campaign_id), W8 (sw: no recall_id),
    M8 (unk: declined), VIN sha256. Design §Item 4.
    """
    _n_fail_start_4 = sum(1 for _, s, _ in res.rows if s == "FAIL")
    if not allow_writes:
        res.skip("item-4", "writes to staging; needs --allow-writes")
        return
    if manifest is None:
        res.skip("item-4", "no manifest")
        return

    import boto3  # noqa: PLC0415
    import hashlib  # noqa: PLC0415
    import json as _json  # noqa: PLC0415
    import uuid as _uuid  # noqa: PLC0415
    from decimal import Decimal as _Dec  # noqa: PLC0415

    # Write-window guard (before any AWS call).
    scheduler = boto3.client("scheduler", region_name=_AVX_REGION)
    now = _utc_now()
    reason = _after6_write_window_guard(scheduler, stage, now)
    if reason:
        res.skip("item-4", f"write window: {reason}")
        return

    sqs_c = boto3.client("sqs", region_name=_AVX_REGION)
    lam_c = boto3.client("lambda", region_name=_AVX_REGION)
    reason = _after6_pre_checks(sqs_c, lam_c, stage, wait_for_drain=True)
    if reason:
        res.skip("item-4", f"pre-check: {reason}")
        return

    # API URL from CloudFormation (same as items 2/3).
    cfn = boto3.client("cloudformation", region_name=_AVX_REGION)
    outputs = {
        o["OutputKey"]: o["OutputValue"]
        for o in cfn.describe_stacks(StackName=_VSA_API_STACK.format(stage=stage))
                   ["Stacks"][0]["Outputs"]
    }
    api_url: str = outputs["RestApiUrl"]

    # Cognito — product-engineer user in the CMS pool.
    idp = boto3.client("cognito-idp", region_name=_AVX_REGION)
    pool_id = _resolve_pool_id(idp)
    client_id = password_client(idp, pool_id)

    # Thread writer env (C4 fix: read VIN_BUCKET from deployed Lambda, not assumed).
    lam_adp = boto3.client("lambda", region_name=_ADP_REGION)
    try:
        fc = lam_adp.get_function_configuration(
            FunctionName=_THREAD_WRITER_FN.format(stage=stage)
        )
        tw_env = fc.get("Environment", {}).get("Variables", {})
    except Exception as exc:  # noqa: BLE001
        check(res, "item-4-writer-env", False,
              on_fail=f"get_function_configuration failed: {exc}")
        return
    vin_bucket = tw_env.get("VIN_BUCKET", "")
    thread_lake_bucket = tw_env.get("THREAD_LAKE_BUCKET", "")
    issued_recalls_bucket = tw_env.get("ISSUED_RECALLS_BUCKET", "")
    if not check(res, "item-4-writer-env",
                 bool(vin_bucket) and bool(thread_lake_bucket) and bool(issued_recalls_bucket),
                 f"VIN_BUCKET={vin_bucket!r}",
                 on_fail=(
                     f"VIN_BUCKET={vin_bucket!r} THREAD_LAKE_BUCKET={thread_lake_bucket!r} "
                     f"ISSUED_RECALLS_BUCKET={issued_recalls_bucket!r} not set")):
        return
    manifest["item4_vin_bucket"] = vin_bucket
    manifest["item4_thread_lake_bucket"] = thread_lake_bucket
    manifest["item4_issued_recalls_bucket"] = issued_recalls_bucket
    _save_manifest(run_id, manifest, manifest_dir)

    # ADP DDB and S3 resources.
    ddb_adp = boto3.resource("dynamodb", region_name=_ADP_REGION)
    pat_tbl = ddb_adp.Table(_PATTERNS_TABLE.format(stage=stage))
    corpus_tbl = ddb_adp.Table(_CORPUS_TABLE.format(stage=stage))
    ddb_avx = boto3.resource("dynamodb", region_name=_AVX_REGION)
    findings_tbl = ddb_avx.Table(_FINDINGS_TABLE.format(stage=stage))
    s3_adp = boto3.client("s3", region_name=_ADP_REGION)
    scheduler_guard = boto3.client("scheduler", region_name=_AVX_REGION)

    _hex = run_id[-8:]
    # Supplier/lot values for the recall_population call (must match _LOT_RE).
    # _LOT_RE = r"[A-Z][A-Z0-9_-]*/[A-Z0-9_-]+"
    specs = [
        # (suffix, remedy_type, cohort_value)
        ("sw",  "software_update",
         f"LTSTSW{_hex[:4].upper()}/LOT001"),
        ("hw",  "hardware_recall",
         f"LTSTHW{_hex[:4].upper()}/LOT001"),
        ("mon", "monitor",
         f"LTST{_hex[:4].upper()}/MON001"),
        ("unk", None,              # unknown: corpus won't match option_id
         f"LTST{_hex[:4].upper()}/UNK001"),
    ]

    # C3 fix: corpus is READ-ONLY. Map each remedy type to a real corpus failure mode.
    # Real failure modes on staging (read 2026-10-27 07:21Z):
    # FM-BCP-01 has OPT-FM-BCP-01-A (hardware_recall), -B (software_update), -C (monitor)
    # FM-COMM-01, FM-MCTRL-01 also have multiple remedy types.
    # We pick one real fm_id per suffix with the right remedy type and read it to get option ids.
    _C3_REAL_FM_IDS = {
        "sw": "FM-BCP-01",   # has software_update option OPT-FM-BCP-01-B
        "hw": "FM-BCP-01",   # has hardware_recall option OPT-FM-BCP-01-A
        "mon": "FM-BCP-01",  # has monitor option OPT-FM-BCP-01-C
        "unk": "FM-COMM-01", # pick a different FM for unk; we'll use a non-existent option_id
    }
    _C3_REMEDY_TYPE_MAP = {
        "sw": "software_update",
        "hw": "hardware_recall",
        "mon": "monitor",
    }

    # Read the real corpus entries for each fm_id (read-once, cached per fm_id)
    corpus_cache: dict[str, dict] = {}
    for _suffix_key, _fm_key in _C3_REAL_FM_IDS.items():
        if _fm_key not in corpus_cache:
            try:
                _item_c = corpus_tbl.get_item(
                    Key={"pk": f"fm#{_fm_key}"}, ConsistentRead=True
                ).get("Item")
                if _item_c is not None:
                    corpus_cache[_fm_key] = _item_c
            except Exception as _cx:  # noqa: BLE001
                check(res, f"item-4-corpus-read-{_fm_key}", False,
                      on_fail=f"corpus read for {_fm_key} failed: {_cx}")

    if not check(res, "item-4-corpus-real-fm-found",
                 bool(corpus_cache),
                 f"real corpus entries found: {list(corpus_cache)}",
                 on_fail="could not read any real corpus failure mode; "
                         "check corpus table and FM IDs"):
        return

    user_emails: list[str] = manifest.setdefault("users", [])

    with principal(idp, pool_id, client_id, "eng", ["product-engineer"],
                   created_users=user_emails) as (eng_tok, _, _eng_email):

        for suffix, remedy_type, cohort_val in specs:
            pattern_id = f"LIVETEST-4-{run_id}-{suffix}"
            # C3 fix: use real corpus fm_id, not a fabricated one
            real_fm_id = _C3_REAL_FM_IDS.get(suffix, "FM-BCP-01")
            corpus_entry = corpus_cache.get(real_fm_id)

            if suffix != "unk":
                # Pick the option_id from the real corpus entry with the matching remedy type
                target_remedy = _C3_REMEDY_TYPE_MAP.get(suffix, "monitor")
                option_id = ""
                if corpus_entry:
                    for _opt in (corpus_entry.get("options") or []):
                        if _opt.get("remedy_type") == target_remedy:
                            option_id = str(_opt.get("remedy_option_id", ""))
                            break
                if not option_id:
                    # Fallback option_id if corpus entry not found (world tests)
                    option_id = f"OPT-{real_fm_id}-{suffix.upper()[:1]}"
            else:
                # unk: use a non-existent option_id (corpus has options, but not this one)
                option_id = f"opt-unk-{_hex}-NOTEXIST"

            n_vins = 4
            vins = _make_livetest_vins(f"{run_id}-{suffix}", n_vins)
            sha256 = hashlib.sha256(_json.dumps(vins).encode()).hexdigest()

            # Record in manifest BEFORE any write (cleanup can undo if a call hangs).
            manifest.setdefault("item4_patterns", []).append({
                "pattern_id": pattern_id,
                "fm_id": real_fm_id,  # C3: real corpus fm_id
                "vins": vins,
                "sha256": sha256,
                "livetest_run_id": run_id,
            })
            _save_manifest(run_id, manifest, manifest_dir)

            # Seed fault-patterns row in ADP (us-east-1) — uses REAL corpus fm_id.
            # C3 fix: code_family must be consistent with the real corpus failure mode's
            # symptom_codes. FM-BCP-01/COMM-01 have P0xxx codes → use "P0BC"/"P0CM".
            real_code_family = ""
            if corpus_entry:
                syms = corpus_entry.get("symptom_codes") or []
                if syms:
                    real_code_family = str(syms[0])[:4]  # e.g. "P0BC" from "P0BC0"
            if not real_code_family:
                real_code_family = "P0LT"
            pat_tbl.put_item(Item={
                "pattern_id": pattern_id,
                "pattern_group_id": pattern_id,
                "candidate_rank": _Dec("1"),
                "code_family": real_code_family,
                "cohort_dimension": "supplier_lot",
                "cohort_value": cohort_val,
                "rate_ratio": _Dec("3.0"),
                "p_value": _Dec("0.001"),
                "affected_vins": vins,
                "affected_vins_count": len(vins),
                "vehicles_affected": len(vins),
                "affected_vins_sha256": sha256,
                "livetest_run_id": run_id,
                "failure_mode_id": real_fm_id,  # C3: point at real corpus entry
            })

            # Build population-scoped quality.investigation Finding.
            # If manifest already has a finding for this pattern_id (pre-seeded by a FAIL-path
            # test), reuse that finding_id and skip the DDB write (finding already in table).
            existing_fnd = next(
                (f for f in manifest.get("item4_findings", [])
                 if f.get("pattern_id") == pattern_id),
                None,
            )
            if existing_fnd and existing_fnd.get("finding_id"):
                finding_id = existing_fnd["finding_id"]
            else:
                from agents.fi_quality.write import (  # noqa: PLC0415
                    build_investigation_finding,
                )
                from agents.tier2.writer import _coerce_floats as _cf  # noqa: PLC0415
                inv_id = str(_uuid.uuid4())
                candidate_id = f"cand-lt-{_hex[:8]}"
                bundle = {
                    "pattern": {
                        "pattern_id": pattern_id, "pattern_group_id": pattern_id,
                        "code_family": real_code_family, "cohort_dimension": "supplier_lot",
                        "cohort_value": cohort_val,
                        "dimension": "supplier_lot", "value": cohort_val,
                        "ratio": _Dec("3"), "p_value": _Dec("1") / _Dec("1000"),
                        "vehicles_affected": len(vins),
                    },
                    "candidates": [{
                        "candidate_id": candidate_id,
                        "dimension": "supplier_lot",
                        "value": cohort_val,
                        "ratio": _Dec("3"), "p_value": _Dec("0.001"), "rank": 1,
                    }],
                    "failure_modes": [],
                    "affected_vins": vins,
                    "controls": [],
                }
                root_cause = {
                    "candidate_id": candidate_id,
                    "failure_mode_id": real_fm_id,  # C3: use real fm_id
                    "diagnosis_source": "rules",
                    "confidence": "low",
                    "evidence_refs": [],
                }
                if remedy_type is not None:
                    rc_options: list[dict] = [{
                        "remedy_option_id": option_id,
                        "remedy_type": remedy_type,
                        "recommended": True,
                        "total_cost": _Dec("0"),
                        "total_vehicles": len(vins),
                        "parts_cost_per_vehicle": _Dec("0"),
                        "tradeoffs": [],
                    }]
                else:
                    rc_options = [{
                        "remedy_option_id": "opt-existing-only",
                        "remedy_type": "monitor",
                        "recommended": True,
                        "total_cost": _Dec("0"),
                        "total_vehicles": len(vins),
                        "parts_cost_per_vehicle": _Dec("0"),
                        "tradeoffs": [],
                    }]
                inv_finding = build_investigation_finding(
                    investigation_id=inv_id,
                    pattern_id=pattern_id,
                    bundle=bundle,
                    root_cause=root_cause,
                    options=rc_options,
                    computed_at=_utc_now().isoformat().replace("+00:00", "Z"),
                )
                finding_id = inv_finding.get("finding_id", "")

                # Record finding in manifest BEFORE writing to AVX DDB.
                manifest.setdefault("item4_findings", []).append({
                    "finding_id": finding_id,
                    "finding_kind": inv_finding.get("finding_kind"),
                    "pattern_id": pattern_id,
                    "investigation_id": inv_id,
                })
                _save_manifest(run_id, manifest, manifest_dir)

                # Write Finding to AVX DDB (coerce floats to Decimal for DDB serialization).
                findings_tbl.put_item(Item=_cf(inv_finding))

            # Re-check write window before approve.
            now = _utc_now()
            reason = _after6_write_window_guard(scheduler_guard, stage, now)
            if reason:
                check(res, f"item-4-{suffix}-window", False,
                      on_fail=f"write window closed before approve: {reason}")
                continue

            # POST approve.
            approve_body = {
                "action_kind": "record_engineering_decision",
                "target_system": "adp",
                "parameters": {"option_id": option_id},
            }
            s, b = _item_6_http(
                api_url, "POST", f"findings/{finding_id}/approve",
                eng_tok, approve_body
            )
            if not check(res, f"item-4-{suffix}-approve-status",
                         s == 200, f"POST approve → {s}",
                         on_fail=f"POST approve → {s} {str(b)[:100]}"):
                continue

            action = b if isinstance(b, dict) else {}
            action_id = action.get("action_id", "")

            # Record action in manifest.
            manifest.setdefault("item4_actions", []).append({
                "action_id": action_id,
                "finding_id": finding_id,
                "suffix": suffix,
            })
            _save_manifest(run_id, manifest, manifest_dir)

            # M9 check: action.status must be 'executed' (valid options) or 'declined' (unk).
            expected_status = "declined" if suffix == "unk" else "executed"
            check(res, f"item-4-{suffix}-action",
                  action.get("status") == expected_status,
                  f"action.status={action.get('status')!r}",
                  on_fail=(f"action.status={action.get('status')!r} "
                           f"expected {expected_status!r}: {str(action)[:200]}"))

            # M8 check: unk option → declined, no thread record.
            if suffix == "unk":
                # Derive the thread id the way ADP's writer does (same deterministic hash).
                unk_thread_id = _derive_thread_id(pattern_id, finding_id)
                # Query ADP DDB — expect no rows (writer must not have created one for unk).
                unk_thread_tbl = ddb_adp.Table(_DIGITAL_THREAD_TABLE.format(stage=stage))
                unk_rows = unk_thread_tbl.query(
                    KeyConditionExpression="thread_id = :t",
                    ExpressionAttributeValues={":t": unk_thread_id},
                    ConsistentRead=True,
                ).get("Items", [])
                check(res, f"item-4-{suffix}-no-thread-record",
                      not unk_rows,
                      f"no thread row for unknown option (422 → declined): "
                      f"thread_id={unk_thread_id!r}",
                      on_fail=f"unexpected thread row for unk: thread_id={unk_thread_id!r}")
                # List the thread lake key — expect no current object.
                unk_lake_key = f"{_THREAD_LAKE_PREFIX}{unk_thread_id}.v1.json"
                try:
                    s3_adp.head_object(Bucket=thread_lake_bucket, Key=unk_lake_key)
                    check(res, f"item-4-{suffix}-no-lake-object", False,
                          on_fail=f"unexpected lake object for unk: key={unk_lake_key!r}")
                except Exception as _e_head:  # noqa: BLE001
                    _code = getattr(_e_head, "response", {}).get("Error", {}).get("Code", "")
                    check(res, f"item-4-{suffix}-no-lake-object",
                          _code in ("404", "NoSuchKey"),
                          f"no current lake object for unk: key={unk_lake_key!r}",
                          on_fail=f"head_object error for unk lake key: {_e_head}")
                # Record derived thread id in manifest so residue scan can verify it.
                manifest.setdefault("item4_unk_thread_ids", []).append(unk_thread_id)
                _save_manifest(run_id, manifest, manifest_dir)
                continue

            thread_id = action.get("target_ref", "")
            if not thread_id:
                check(res, f"item-4-{suffix}-thread-id", False,
                      on_fail=f"action.target_ref not set: {str(action)[:100]}")
                continue

            # Record thread info in manifest BEFORE querying DDB.
            manifest.setdefault("item4_thread_versions", []).append({
                "thread_id": thread_id,
                "version": 1,
                "action_id": action_id,
                "finding_id": finding_id,
            })
            _save_manifest(run_id, manifest, manifest_dir)

            # Read thread v1 from ADP DDB.
            thread_tbl = ddb_adp.Table(_DIGITAL_THREAD_TABLE.format(stage=stage))
            thread_rows = thread_tbl.query(
                KeyConditionExpression="thread_id = :t",
                ExpressionAttributeValues={":t": thread_id},
                ConsistentRead=True,
            ).get("Items", [])
            thread_row = (
                min(thread_rows, key=lambda r: int(r.get("version", 9999)))
                if thread_rows else None
            )

            if not check(res, f"item-4-{suffix}-thread-present",
                         thread_row is not None,
                         f"thread_id={thread_id!r}",
                         on_fail=f"no thread row for thread_id={thread_id!r}"):
                continue

            check(res, f"item-4-{suffix}-thread-status",
                  str(thread_row.get("status")) == "decided",
                  f"status={thread_row.get('status')!r}",
                  on_fail=f"thread status {thread_row.get('status')!r} != 'decided'")

            # C8 check (monitor): no campaign_id.
            if suffix == "mon":
                has_sw = bool(thread_row.get("software_campaign_id"))
                has_hw = bool(thread_row.get("recall_campaign_id"))
                check(res, f"item-4-{suffix}-no-campaign-id",
                      not has_sw and not has_hw,
                      (f"sw_campaign_id={thread_row.get('software_campaign_id')!r} "
                       f"recall_campaign_id={thread_row.get('recall_campaign_id')!r}"),
                      on_fail="monitor decision must not set campaign ids (C8)")

                # W5 fix: record mon VIN object for cleanup too
                mon_vin_key = thread_row.get("affected_vins_s3_key", "")
                if mon_vin_key:
                    manifest.setdefault("item4_vin_objects", []).append({
                        "bucket": vin_bucket, "key": mon_vin_key
                    })
                    _save_manifest(run_id, manifest, manifest_dir)

            elif suffix == "sw":
                # W8 check: software_update must not set recall_campaign_id.
                check(res, f"item-4-{suffix}-no-recall-id",
                      thread_row.get("recall_campaign_id") is None,
                      f"recall_campaign_id={thread_row.get('recall_campaign_id')!r}",
                      on_fail="software_update must not set recall_campaign_id (W8)")

                # VIN sha256 check.
                vin_key = thread_row.get("affected_vins_s3_key", "")
                expected_sha = thread_row.get("affected_vins_sha256", "")
                if vin_key and expected_sha:
                    try:
                        vin_body = s3_adp.get_object(
                            Bucket=vin_bucket, Key=vin_key
                        )["Body"].read()
                        vins_in_s3 = _json.loads(vin_body)
                        actual_sha = hashlib.sha256(
                            _json.dumps(sorted(vins_in_s3)).encode()
                        ).hexdigest()
                        check(res, f"item-4-{suffix}-vin-sha256",
                              actual_sha == expected_sha,
                              "sha256 matches pattern row sha256",
                              on_fail=(f"VIN sha256 mismatch: "
                                       f"s3={actual_sha[:16]!r} "
                                       f"expected={expected_sha[:16]!r}"))
                    except Exception as exc:  # noqa: BLE001
                        check(res, f"item-4-{suffix}-vin-sha256", False,
                              on_fail=f"VIN object read failed: {exc}")
                    # W5 fix: record sw VIN object (was already here)
                    manifest.setdefault("item4_vin_objects", []).append({
                        "bucket": vin_bucket, "key": vin_key
                    })
                    _save_manifest(run_id, manifest, manifest_dir)

                # Draft row check.
                sw_campaign_id = thread_row.get("software_campaign_id")
                if sw_campaign_id:
                    manifest.setdefault("item4_draft_rows", []).append({
                        "campaign_id": sw_campaign_id,
                        "table": _OTA_CAMPAIGNS_TABLE.format(stage=stage),
                    })
                    _save_manifest(run_id, manifest, manifest_dir)
                    ota_tbl = ddb_adp.Table(_OTA_CAMPAIGNS_TABLE.format(stage=stage))
                    draft = ota_tbl.get_item(
                        Key={"campaign_id": sw_campaign_id}, ConsistentRead=True
                    ).get("Item")
                    check(res, f"item-4-{suffix}-draft-row",
                          draft is not None and str(draft.get("status")) == "draft",
                          f"campaign_id={sw_campaign_id!r} status={draft.get('status') if draft else None!r}",
                          on_fail=f"SW draft row missing or wrong status: {str(draft)[:100]}")

            elif suffix == "hw":
                recall_campaign_id = thread_row.get("recall_campaign_id")
                check(res, f"item-4-{suffix}-campaign-type",
                      thread_row.get("campaign_type") == "hardware_recall",
                      f"campaign_type={thread_row.get('campaign_type')!r}",
                      on_fail="hw thread campaign_type != hardware_recall")
                check(res, f"item-4-{suffix}-no-sw-id",
                      thread_row.get("software_campaign_id") is None,
                      f"software_campaign_id={thread_row.get('software_campaign_id')!r}",
                      on_fail="hardware_recall must not set software_campaign_id")
                if recall_campaign_id:
                    manifest.setdefault("item4_draft_rows", []).append({
                        "campaign_id": recall_campaign_id,
                        "table": _RECALL_CAMPAIGNS_TABLE.format(stage=stage),
                    })
                    _save_manifest(run_id, manifest, manifest_dir)
                    recall_tbl = ddb_adp.Table(_RECALL_CAMPAIGNS_TABLE.format(stage=stage))
                    hw_draft = recall_tbl.get_item(
                        Key={"campaign_id": recall_campaign_id}, ConsistentRead=True
                    ).get("Item")
                    check(res, f"item-4-{suffix}-draft-row",
                          hw_draft is not None and str(hw_draft.get("status")) == "draft",
                          on_fail=f"HW draft row missing or wrong status: {str(hw_draft)[:100]}")

                # W5 fix: record hw VIN object for cleanup too
                hw_vin_key = thread_row.get("affected_vins_s3_key", "")
                if hw_vin_key:
                    manifest.setdefault("item4_vin_objects", []).append({
                        "bucket": vin_bucket, "key": hw_vin_key
                    })
                    _save_manifest(run_id, manifest, manifest_dir)

            # Record thread lake object for cleanup.
            lake_key = f"{_THREAD_LAKE_PREFIX}{thread_id}.v1.json"
            manifest.setdefault("item4_s3_lake_objects", []).append({
                "bucket": thread_lake_bucket, "key": lake_key
            })
            _save_manifest(run_id, manifest, manifest_dir)

    # W7: record item 4 pass/fail status for after-6-check
    manifest["items_passed"]["4"] = (
        sum(1 for _, s, _ in res.rows if s == "FAIL") == _n_fail_start_4
    )
    _save_manifest(run_id, manifest, manifest_dir)


def _item_5(
    res: Results,
    *,
    stage: str,
    run_id: str,
    manifest: dict,
    manifest_dir: str | None,
    allow_writes: bool,
    **_: Any,
) -> None:
    """Item 5: issue_software_campaign on the quality.issue Finding from item 4's sw decision.

    Checks: action executed (M9), thread version status == 'issued' (M10),
    issued_row.software_campaign_id == draft_id (S4). Design §Item 5.
    """
    _n_fail_start_5 = sum(1 for _, s, _ in res.rows if s == "FAIL")
    if not allow_writes:
        res.skip("item-5", "writes to staging; needs --allow-writes")
        return
    if manifest is None:
        res.skip("item-5", "no manifest")
        return

    import boto3  # noqa: PLC0415

    # Find the sw thread_id from item 4's manifest records.
    sw_thread_ids: list[str] = [
        rec.get("thread_id", "")
        for rec in manifest.get("item4_thread_versions", [])
        if rec.get("thread_id")
    ]
    if not sw_thread_ids:
        res.skip("item-5", "no sw thread_id in manifest from item 4; run item 4 first")
        return

    # Write-window guard.
    scheduler = boto3.client("scheduler", region_name=_AVX_REGION)
    now = _utc_now()
    reason = _after6_write_window_guard(scheduler, stage, now)
    if reason:
        res.skip("item-5", f"write window: {reason}")
        return

    sqs_c = boto3.client("sqs", region_name=_AVX_REGION)
    lam_c = boto3.client("lambda", region_name=_AVX_REGION)
    reason = _after6_pre_checks(sqs_c, lam_c, stage, wait_for_drain=True)
    if reason:
        res.skip("item-5", f"pre-check: {reason}")
        return

    # API URL.
    cfn = boto3.client("cloudformation", region_name=_AVX_REGION)
    outputs = {
        o["OutputKey"]: o["OutputValue"]
        for o in cfn.describe_stacks(StackName=_VSA_API_STACK.format(stage=stage))
                   ["Stacks"][0]["Outputs"]
    }
    api_url: str = outputs["RestApiUrl"]

    idp = boto3.client("cognito-idp", region_name=_AVX_REGION)
    pool_id = _resolve_pool_id(idp)
    client_id = password_client(idp, pool_id)
    ddb_avx = boto3.resource("dynamodb", region_name=_AVX_REGION)
    findings_tbl = ddb_avx.Table(_FINDINGS_TABLE.format(stage=stage))
    ddb_adp = boto3.resource("dynamodb", region_name=_ADP_REGION)

    user_emails: list[str] = manifest.setdefault("users", [])

    with principal(idp, pool_id, client_id, "eng", ["product-engineer"],
                   created_users=user_emails) as (eng_tok, _, _eng_email):

        # W6: read the quality.issue Finding by its derived finding_key (GSI query),
        # NOT a scan.  The table is 214 MB; a scan returns only the first 1 MB page,
        # which misses most Findings.  The finding_key is deterministic from the
        # thread_id (issue.py: subject_key = "{thread_id}/issue",
        # scope = population_id = "meridian-motors", agent_id = "agent-fi-quality").
        # Query the by_finding_key GSI; no ConsistentRead (GSI does not support it).
        issue_finding: dict | None = None
        issue_finding_id_from_manifest = manifest.get("item4_issue_finding_id")

        if issue_finding_id_from_manifest:
            # Fast path: finding_id already recorded by item 4 (or a previous run).
            issue_finding = findings_tbl.get_item(
                Key={"finding_id": issue_finding_id_from_manifest}, ConsistentRead=True
            ).get("Item")
        else:
            # GSI path: derive finding_key for each sw thread_id and query.
            from agents.tier2.writer import compute_finding_key as _cfk  # noqa: PLC0415
            from agents.fi_quality.issue import (  # noqa: PLC0415
                AGENT_ID as _ISSUE_AGENT_ID,
                POPULATION_ID as _ISSUE_POPULATION_ID,
                ISSUE_FINDING_KIND as _ISSUE_KIND,
            )
            from boto3.dynamodb.conditions import Key as _DdbKey  # noqa: PLC0415
            max_wait_s = 60
            deadline = time.monotonic() + max_wait_s
            while time.monotonic() < deadline and issue_finding is None:
                for tid in sw_thread_ids:
                    fk = _cfk(
                        agent_id=_ISSUE_AGENT_ID,
                        scope_field="population_id",
                        scope_value=_ISSUE_POPULATION_ID,
                        finding_kind=_ISSUE_KIND,
                        subject_key=f"{tid}/issue",
                    )
                    resp = findings_tbl.query(
                        IndexName="by_finding_key",
                        KeyConditionExpression=_DdbKey("finding_key").eq(fk),
                    )
                    for it in resp.get("Items", []):
                        if it.get("finding_kind") == _ISSUE_KIND:
                            issue_finding = it
                            break
                    if issue_finding is not None:
                        break
                if issue_finding is None:
                    time.sleep(5)

        if not check(res, "item-5-issue-finding-present",
                     issue_finding is not None,
                     "quality.issue Finding present after item 4 sw decision",
                     on_fail="quality.issue Finding not found; item 4 sw hook may have failed"):
            return

        issue_finding_id = issue_finding.get("finding_id", "")

        # Record in manifest.
        manifest.setdefault("item5_findings", []).append({
            "finding_id": issue_finding_id,
            "finding_kind": "quality.issue",
        })
        _save_manifest(run_id, manifest, manifest_dir)

        # Read thread_id and draft_id from the quality.issue Finding's recommendation.
        issue_rec = issue_finding.get("recommended_action") or {}
        issue_params = issue_rec.get("parameters") or {}
        thread_id = issue_params.get("thread_id", "")
        draft_id = issue_params.get("draft_id", "")
        if not check(res, "item-5-issue-finding-params",
                     bool(thread_id) and bool(draft_id),
                     f"thread_id={thread_id!r} draft_id={draft_id!r}",
                     on_fail="quality.issue Finding missing thread_id or draft_id in parameters"):
            return

        # Re-check write window.
        now = _utc_now()
        reason = _after6_write_window_guard(scheduler, stage, now)
        if reason:
            check(res, "item-5-window", False,
                  on_fail=f"write window closed before issue: {reason}")
            return

        # POST approve with issue_software_campaign.
        approve_body = {
            "action_kind": "issue_software_campaign",
            "target_system": "adp",
            "parameters": {},  # thread_id and draft_id are server-set from the Finding
        }
        s, b = _item_6_http(
            api_url, "POST", f"findings/{issue_finding_id}/approve",
            eng_tok, approve_body
        )
        if not check(res, "item-5-issue-action",
                     s == 200 and isinstance(b, dict) and b.get("status") == "executed",
                     f"POST issue → {s} status={b.get('status') if isinstance(b, dict) else None!r}",
                     on_fail=f"issue_software_campaign → {s} {str(b)[:100]}"):
            return

        action_resp = b if isinstance(b, dict) else {}
        issue_action_id = action_resp.get("action_id", "")

        # Record action.
        manifest.setdefault("item5_actions", []).append({
            "action_id": issue_action_id,
            "finding_id": issue_finding_id,
        })
        _save_manifest(run_id, manifest, manifest_dir)

        # M10 check: thread v2 (or higher) has status == 'issued'.
        thread_tbl = ddb_adp.Table(_DIGITAL_THREAD_TABLE.format(stage=stage))
        thread_rows = thread_tbl.query(
            KeyConditionExpression="thread_id = :t",
            ExpressionAttributeValues={":t": thread_id},
            ConsistentRead=True,
        ).get("Items", [])
        issued_rows = [r for r in thread_rows if int(r.get("version", 0)) >= 2]
        has_issued = any(str(r.get("status")) == "issued" for r in issued_rows)
        check(res, "item-5-thread-issued",
              has_issued,
              f"thread_id={thread_id!r} versions={[r.get('version') for r in thread_rows]}",
              on_fail=(f"no thread version with status='issued' for thread_id={thread_id!r} "
                       f"(M10 check). rows={[dict(r) for r in thread_rows]!r:.200}"))

        # Record v2 thread version for cleanup.
        for r in issued_rows:
            manifest.setdefault("item4_thread_versions", []).append({
                "thread_id": thread_id,
                "version": int(r.get("version", 2)),
                "action_id": issue_action_id,
                "finding_id": issue_finding_id,
            })
        _save_manifest(run_id, manifest, manifest_dir)

        # S4 check: issued thread version's software_campaign_id == draft_id.
        # "the issued row" = the thread v2 (or higher) with status='issued' (design §Item 5).
        issued_rows = [r for r in thread_rows if str(r.get("status")) == "issued"]
        if check(res, "item-5-draft-issued-id",
                 bool(issued_rows) and str(issued_rows[0].get("software_campaign_id", "")) == draft_id,
                 (f"issued_row.software_campaign_id={issued_rows[0].get('software_campaign_id') if issued_rows else None!r} "
                  f"== draft_id={draft_id!r}"),
                 on_fail=(f"S4 mismatch: issued thread row.software_campaign_id="
                          f"{issued_rows[0].get('software_campaign_id') if issued_rows else None!r} "
                          f"draft_id={draft_id!r}")):
            pass

    # W7: record item 5 pass/fail status for after-6-check
    manifest["items_passed"]["5"] = (
        sum(1 for _, s, _ in res.rows if s == "FAIL") == _n_fail_start_5
    )
    _save_manifest(run_id, manifest, manifest_dir)


# ---------------------------------------------------------------------------
# Item 6 helpers
# ---------------------------------------------------------------------------

def _item_6_http(
    api_url: str, method: str, path: str, token: str, body: dict | None = None,
) -> tuple[int, dict]:
    """HTTP call to the AVX API; returns (status_code, parsed_body).

    Uses stdlib urllib so no requests dependency is needed. Brief §Item 6.
    """
    import json as _json  # noqa: PLC0415
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    url = api_url.rstrip("/") + "/" + path.lstrip("/")
    data = _json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    def _parse(raw: bytes) -> dict:
        try:
            out = _json.loads(raw or b"{}")
        except ValueError:
            return {"_raw": raw[:200].decode("utf-8", "replace")}
        return out if isinstance(out, dict) else {"_raw": str(out)[:200]}

    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, _parse(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _parse(e.read())
    except (urllib.error.URLError, OSError) as e:  # includes timeouts: status 0, no body
        return 0, {"_error": f"{type(e).__name__}: {e}"}


#: How long the cleanup's own receives hide a message: long enough to cover the rest of the
#: cleanup, so this run receives a foreign message (a real request) at most once. The queue's
#: maxReceiveCount is 2 (``vsa-tier2-stack.ts``), so a second receive here would send that
#: request to the DLQ on its next delivery. Review cycle 2 W1.
_SQS_FOREIGN_HOLD_S = 120


def _item_6_cleanup(
    res: Results,
    *,
    sqs: Any,
    queue_url: str,
    inv_tbl: Any,
    findings_tbl: Any,
    investigation_id: str | None,
    lock_key: str,
    plant_id: str,
    cap_key: str,
    cap_before: Any,
    idp: Any,
    pool_id: str,
    user_emails: list[str],
) -> None:
    """Cleanup for item 6; call from finally with _defer_signals_for_restore held.

    Order: read the lock → drain the queue → delete inv row + lock → restore cap → delete
    plant → confirm by reads (queue empty, rows absent, cap restored, users absent).
    Brief §Item 6 cleanup. Each step runs in its own try, so an error is that step's FAIL
    and every later step still runs (review cycle 2 W2). Reads after this run's writes are
    consistent reads (W3).
    """
    from decimal import Decimal as _Dec  # noqa: PLC0415

    def _step(label: str, fn: Callable[[], Any]) -> tuple[bool, Any]:
        try:
            return True, fn()
        except Exception as exc:  # noqa: BLE001
            check(res, label, False, on_fail=f"{type(exc).__name__}: {exc}")
            return False, None

    # 0. The route writes the lock, the row and the cap increment in one transaction
    #    (api-avx-investigations/handler.py _post_investigation). So the lock tells whether
    #    this run's control was counted, and names its row even when the 202 was lost
    #    (a client timeout after the route succeeded). Unreadable lock: counted is unknown.
    ok, lock_now = _step("item-6-cleanup-lock-read", lambda: inv_tbl.get_item(
        Key={"investigation_id": lock_key}, ConsistentRead=True).get("Item"))
    counted: bool | None = (lock_now is not None) if ok else None
    if counted and not investigation_id:
        investigation_id = lock_now.get("investigation_id_ref") or None

    # 1. Receive from the queue; delete only the control's own message (body equals
    #    investigation_id). Any other body is left in place, received once (hidden for the
    #    rest of the cleanup), and FAILs the item. Brief §Item 6 cleanup.
    foreign: dict[str, str] = {}  # MessageId -> ApproximateReceiveCount when we saw it

    def _drain() -> bool:
        own_deleted = False
        for _attempt in range(6):
            msgs = sqs.receive_message(
                QueueUrl=queue_url, MaxNumberOfMessages=10,
                VisibilityTimeout=_SQS_FOREIGN_HOLD_S, WaitTimeSeconds=_SQS_DRAIN_WAIT_S,
                AttributeNames=["ApproximateReceiveCount"],
            ).get("Messages") or []
            for msg in msgs:
                if (msg.get("Body") or "") == investigation_id:
                    sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"])
                    own_deleted = True
                else:
                    foreign[str(msg.get("MessageId", "?"))] = str(
                        (msg.get("Attributes") or {}).get("ApproximateReceiveCount", "?"))
            if own_deleted:
                break
        return own_deleted

    if investigation_id:
        ok, own_deleted = _step("item-6-cleanup-drain", _drain)
        if ok:
            check(res, "item-6-cleanup-own-message", own_deleted,
                  "the control's queue message received and deleted",
                  on_fail="the control's queue message was never received")
    if foreign:
        check(res, "item-6-cleanup-no-foreign-msg", False,
              on_fail=f"{len(foreign)} foreign message(s) left in the queue, not deleted "
                      f"(MessageId: receive count {foreign}); find each in the queue or its DLQ")

    # 2. Delete investigation row. Brief §Item 6 cleanup.
    if investigation_id:
        _step("item-6-cleanup-del-inv-row",
              lambda: inv_tbl.delete_item(Key={"investigation_id": investigation_id}))

    # 3. Delete lock#LIVETEST-<run_id>. Brief §Item 6 cleanup.
    _step("item-6-cleanup-del-lock", lambda: inv_tbl.delete_item(Key={"investigation_id": lock_key}))

    # 4. Restore cap item. Brief §Item 6 cleanup.
    #    absent before → delete with condition count = 1
    #    count=n before → set back to n with condition count = n+1
    def _restore() -> None:
        if cap_before is _NOT_READ:
            return  # never snapshotted (an earlier step raised): touch nothing
        if counted is None:
            raise RuntimeError("the lock couldn't be read, so whether this run counted is "
                               "unknown; the cap was left alone")
        if not counted:
            return  # the control's transaction never ran: the cap wasn't moved by this run
        if cap_before is None:
            inv_tbl.delete_item(
                Key={"investigation_id": cap_key},
                ConditionExpression="#cnt = :one",
                ExpressionAttributeNames={"#cnt": "count"},
                ExpressionAttributeValues={":one": _Dec("1")},
            )
        else:
            n = cap_before.get("count", _Dec("0"))
            inv_tbl.update_item(
                Key={"investigation_id": cap_key},
                UpdateExpression="SET #cnt = :n",
                ConditionExpression="#cnt = :np1",
                ExpressionAttributeNames={"#cnt": "count"},
                ExpressionAttributeValues={":n": n, ":np1": n + _Dec("1")},
            )
    _step("item-6-cleanup-cap-restore", _restore)

    # 5. Delete plant finding. Brief §Item 6 cleanup.
    _step("item-6-cleanup-del-plant", lambda: findings_tbl.delete_item(Key={"finding_id": plant_id}))

    # ── Confirm by reads (each in its own try; consistent reads) ────────────

    # Queue attributes both 0 (poll up to ~60 s; approximate). Brief §Item 6 cleanup.
    def _attrs() -> tuple[int, int]:
        n_vis = n_inflight = -1
        deadline = time.monotonic() + _QUEUE_EMPTY_POLL_S
        _max_iters = int(_QUEUE_EMPTY_POLL_S) + 2  # cap: stuck clock can't loop forever
        _iters = 0
        while time.monotonic() < deadline and _iters < _max_iters:
            _iters += 1
            qa = sqs.get_queue_attributes(
                QueueUrl=queue_url,
                AttributeNames=["ApproximateNumberOfMessages",
                                "ApproximateNumberOfMessagesNotVisible"],
            )["Attributes"]
            n_vis = int(qa.get("ApproximateNumberOfMessages", -1))
            n_inflight = int(qa.get("ApproximateNumberOfMessagesNotVisible", -1))
            if n_vis == 0 and n_inflight == 0:
                break
            time.sleep(1)
        return n_vis, n_inflight
    ok, counts = _step("item-6-confirm-queue-attrs", _attrs)
    if ok:
        n_vis, n_inflight = counts
        check(res, "item-6-confirm-queue-attrs", n_vis == 0 and n_inflight == 0,
              "queue attributes both 0",
              on_fail=f"queue not empty: visible={n_vis} in-flight={n_inflight}")

    # One ReceiveMessage returns nothing. Brief §Item 6 cleanup. Not run once a foreign
    # message was seen: receiving it again would spend its receive budget (W1).
    if foreign:
        check(res, "item-6-confirm-queue-receive", False,
              on_fail="not run: foreign message(s) in the queue (see item-6-cleanup-no-foreign-msg)")
    else:
        ok, leftover = _step("item-6-confirm-queue-receive", lambda: sqs.receive_message(
            QueueUrl=queue_url, MaxNumberOfMessages=10,
            VisibilityTimeout=5, WaitTimeSeconds=_SQS_FINAL_WAIT_S,
        ).get("Messages"))
        if ok:
            check(res, "item-6-confirm-queue-receive", not leftover,
                  "ReceiveMessage returns nothing",
                  on_fail=f"queue has messages: {[m.get('Body', '')[:40] for m in (leftover or [])]}")

    # Investigation row, lock row and plant absent. Brief §Item 6 cleanup.
    absent_checks = [("item-6-confirm-lock-absent", inv_tbl, {"investigation_id": lock_key}, "lock row"),
                     ("item-6-confirm-plant-absent", findings_tbl, {"finding_id": plant_id}, "plant")]
    if investigation_id:
        absent_checks.insert(0, ("item-6-confirm-inv-absent", inv_tbl,
                                 {"investigation_id": investigation_id}, "investigation row"))
    for label, tbl, key, what in absent_checks:
        ok, item = _step(label, lambda tbl=tbl, key=key: tbl.get_item(
            Key=key, ConsistentRead=True).get("Item"))
        if ok:
            check(res, label, item is None, f"{what} absent",
                  on_fail=f"{what} still present ({key})")

    # Cap restored. Brief §Item 6 cleanup.
    ok, cap_after = _step("item-6-confirm-cap-restored", lambda: inv_tbl.get_item(
        Key={"investigation_id": cap_key}, ConsistentRead=True).get("Item"))
    if ok:
        if cap_before is _NOT_READ:
            check(res, "item-6-confirm-cap-restored", counted is False,
                  "cap never snapshotted and the control never counted",
                  on_fail=f"cap never snapshotted; the control counted or may have ({cap_key}: {cap_after})")
        elif counted is None:
            check(res, "item-6-confirm-cap-restored", False,
                  on_fail=f"unknown whether the control counted (lock unreadable); {cap_key}: {cap_after}")
        elif cap_before is None:
            check(res, "item-6-confirm-cap-restored", cap_after is None,
                  "cap absent (was absent before)",
                  on_fail=f"cap not deleted ({cap_key}: {cap_after})")
        else:
            n = cap_before.get("count")
            n_after = cap_after.get("count") if cap_after else None
            check(res, "item-6-confirm-cap-restored", n_after == n,
                  f"cap count restored to {n}",
                  on_fail=f"cap count is {n_after!r}, expected {n}")

    # Users absent (brief §Cognito confirm-by-read). Brief §Item 6 cleanup.
    for email in user_emails:
        ok, gone = _step("item-6-confirm-user-absent", lambda email=email: idp.list_users(
            UserPoolId=pool_id, Filter=f'email = "{email}"').get("Users", []))
        if ok:
            check(res, "item-6-confirm-user-absent", not gone,
                  f"user absent ({email})",
                  on_fail=f"user still present: {email}")


# ---------------------------------------------------------------------------
# Item 6 placeholder (writes; --allow-writes required)
# ---------------------------------------------------------------------------

def _item_6(res: Results, *, allow_writes: bool, stage: str, run_id: str = "", **_: Any) -> None:
    """Item 6: who may start and decide investigations (writes; --allow-writes required).

    Principals: eng (product-engineer), cs (connected-services), fop (fleet-operator).
    Plant a population-scoped Finding, send POST /investigations, then POST
    /findings/<plant>/approve and /findings/<plant>/decline. Cleanup in finally.
    Brief §Item 6.
    """
    import boto3  # noqa: PLC0415

    if not allow_writes:
        res.skip("item-6", "writes to staging; needs --allow-writes")
        return

    # API base URL from CloudFormation output (brief §Names).
    cfn = boto3.client("cloudformation", region_name=_AVX_REGION)
    outputs = {
        o["OutputKey"]: o["OutputValue"]
        for o in cfn.describe_stacks(StackName=_VSA_API_STACK.format(stage=stage))
                       ["Stacks"][0]["Outputs"]
    }
    api_url: str = outputs["RestApiUrl"]

    # SQS queue URL (brief §Names).
    sqs = boto3.client("sqs", region_name=_AVX_REGION)
    queue_url = sqs.get_queue_url(QueueName=_INV_QUEUE.format(stage=stage))["QueueUrl"]

    # Refuse (SKIP) unless the investigation event source mapping is Disabled.
    # Brief §Item 6.
    lam_c = boto3.client("lambda", region_name=_AVX_REGION)
    tier2_fn = _TIER2_LAMBDA.format(stage=stage)
    inv_queue_nm = _INV_QUEUE.format(stage=stage)
    inv_esms = _investigation_esms(lam_c, tier2_fn, inv_queue_nm)
    if not inv_esms:
        res.skip("item-6", f"no SQS event source mapping for {inv_queue_nm} on {tier2_fn}")
        return
    if any(m.get("State") != "Disabled" for m in inv_esms):
        res.skip(
            "item-6",
            f"investigation event source is not Disabled "
            f"({[m['State'] for m in inv_esms]})",
        )
        return
    # Every consumer of the queue, not only the Tier 2 function's mapping (security review
    # cycle 1 S1): any enabled mapping would process the control's message.
    queue_arn = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])[
        "Attributes"]["QueueArn"]
    other = [m for m in _queue_esms(lam_c, queue_arn) if m.get("State") != "Disabled"]
    if other:
        res.skip("item-6", f"another event source on {inv_queue_nm} is not Disabled: "
                           f"{[(m.get('FunctionArn'), m.get('State')) for m in other]}")
        return

    # The route counts the control under cap#<UTC date at the POST>; refuse near midnight
    # so the day this run snapshots is the day the route counts.
    _now = _utc_now()
    if (_now.hour == 23 and _now.minute >= 50) or (_now.hour == 0 and _now.minute < 10):
        res.skip("item-6", "within 10 min of 00:00 UTC (the cap counter's day changes)")
        return

    # Refuse (SKIP) unless queue attributes are both 0. Brief §Item 6.
    qa = sqs.get_queue_attributes(
        QueueUrl=queue_url,
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
    )["Attributes"]
    n_vis = int(qa.get("ApproximateNumberOfMessages", 0))
    n_inflight = int(qa.get("ApproximateNumberOfMessagesNotVisible", 0))
    if n_vis != 0 or n_inflight != 0:
        res.skip("item-6", f"queue not empty before write: visible={n_vis} in-flight={n_inflight}")
        return

    # Cognito (brief §Cognito).
    idp = boto3.client("cognito-idp", region_name=_AVX_REGION)
    pool_id = _resolve_pool_id(idp)
    client_id = password_client(idp, pool_id)
    # This script's users left by an earlier run whose delete failed (security cycle 1 W1).
    _reap_stale_users(res, idp, pool_id)

    # Tables.
    ddb = boto3.resource("dynamodb", region_name=_AVX_REGION)
    findings_tbl = ddb.Table(_FINDINGS_TABLE.format(stage=stage))
    inv_tbl = ddb.Table(_INVESTIGATIONS_TABLE.format(stage=stage))
    actions_tbl = ddb.Table(_ACTIONS_TABLE.format(stage=stage))

    # Plant-related ids. Brief §Item 6.
    plant_id = f"LIVETEST-FI-QUALITY-{run_id}"
    pattern_id = f"LIVETEST-{run_id}"
    lock_key = f"lock#{pattern_id}"

    # One clock for the midnight guard and the cap key (review cycle 2 S4).
    cap_key = f"cap#{_now.strftime('%Y-%m-%d')}"
    cap_before: Any = _NOT_READ
    investigation_id: str | None = None
    user_emails: list[str] = []  # filled by principal() as each user is created

    try:
        # Plant (conditional put; brief §Item 6). expires_at is an ISO string, as the
        # writer stores it (agents/tier2/writer.py).
        now = dt.datetime.now(dt.timezone.utc)
        findings_tbl.put_item(
            Item={
                "finding_id": plant_id,
                "agent_id": "agent-fi-quality",
                "finding_kind": "quality.investigation",
                "scope": {"population_id": "meridian-motors"},
                "livetest": True,
                "computed_at": now.isoformat(timespec="seconds"),
                "expires_at": (now + dt.timedelta(hours=1)).isoformat(timespec="seconds"),
            },
            ConditionExpression="attribute_not_exists(finding_id)",
        )
        # Snapshot cap#<today UTC> BEFORE the eng POST (brief §Item 6 cleanup).
        cap_before = inv_tbl.get_item(Key={"investigation_id": cap_key},
                                      ConsistentRead=True).get("Item")
        with principal(idp, pool_id, client_id, "eng", ["product-engineer"],
                       created_users=user_emails) \
                as (eng_tok, _, _eng_email), \
             principal(idp, pool_id, client_id, "cs", ["connected-services"],
                       created_users=user_emails) \
                as (cs_tok, _, _cs_email), \
             principal(idp, pool_id, client_id, "fop", ["fleet-operator"],
                       fleet_ids="demo-lastmile-delivery", created_users=user_emails) \
                as (fop_tok, _, _fop_email):

            inv_body = {"pattern_id": pattern_id}

            # POST /investigations: cs → 403, fop → 403, eng → 202 (brief §Item 6).
            # Group check at handler.py ~756; SQS body is the bare id, ~603.
            # The route's own 403 is {"error": "Forbidden"}; API Gateway's is {"message": ...}.
            for who, tok in (("cs", cs_tok), ("fop", fop_tok)):
                s, b = _item_6_http(api_url, "POST", "investigations", tok, inv_body)
                check(res, f"item-6-inv-{who}-403", s == 403 and b.get("error") == "Forbidden",
                      f"{who} POST /investigations → 403 from the route",
                      on_fail=f"{who} POST /investigations → {s} {b}")
            s, b = _item_6_http(api_url, "POST", "investigations", eng_tok, inv_body)
            investigation_id = b.get("investigation_id") if isinstance(b, dict) else None
            check(res, "item-6-inv-eng-202",
                  s == 202 and isinstance(investigation_id, str) and bool(investigation_id),
                  f"eng POST /investigations → {s} id={investigation_id}")

            # POST /findings/<plant>/approve: cs → 403, fop → 403, eng → 400.
            # eng 400: "is not valid for a population-scoped Finding" (~1295). Brief §Item 6.
            app_body = {"action_kind": "acknowledge_only", "target_system": "none",
                        "parameters": {}}
            for who, tok in (("cs", cs_tok), ("fop", fop_tok)):
                s, b = _item_6_http(api_url, "POST", f"findings/{plant_id}/approve", tok, app_body)
                check(res, f"item-6-approve-{who}-403",
                      s == 403 and b.get("error") == "insufficient scope to approve this finding",
                      f"{who} POST /approve → 403 from the route",
                      on_fail=f"{who} POST /approve → {s} {b}")
            s, b = _item_6_http(
                api_url, "POST", f"findings/{plant_id}/approve", eng_tok, app_body
            )
            err = b.get("error", "") if isinstance(b, dict) else ""
            check(res, "item-6-approve-eng-400",
                  s == 400 and "is not valid for a population-scoped Finding" in err,
                  f"eng POST /approve → {s}: {err[:80]}")

            # POST /findings/<plant>/decline: cs → 403, fop → 403, eng → 400.
            # eng 400: "decline is only for FI queue items" (~1623). Brief §Item 6.
            dec_body = {"reason": "livetest"}
            for who, tok in (("cs", cs_tok), ("fop", fop_tok)):
                s, b = _item_6_http(api_url, "POST", f"findings/{plant_id}/decline", tok, dec_body)
                check(res, f"item-6-decline-{who}-403",
                      s == 403 and b.get("error") == "insufficient scope to decline this finding",
                      f"{who} POST /decline → 403 from the route",
                      on_fail=f"{who} POST /decline → {s} {b}")
            s, b = _item_6_http(
                api_url, "POST", f"findings/{plant_id}/decline", eng_tok, dec_body
            )
            err = b.get("error", "") if isinstance(b, dict) else ""
            check(res, "item-6-decline-eng-400",
                  s == 400 and "decline is only for FI queue items" in err,
                  f"eng POST /decline → {s}: {err[:80]}")

        # Principals deleted by context-manager finally blocks above.

        # No Action row references the plant (full paged scan; brief §Item 6).
        plant_actions = [a for a in _scan_all(actions_tbl, consistent=True)
                         if a.get("finding_id") == plant_id]
        check(res, "item-6-no-action-row", not plant_actions,
              "no Action row for the plant",
              on_fail=f"{len(plant_actions)} Action row(s) reference the plant")

    finally:
        with _defer_signals_for_restore():
            _item_6_cleanup(
                res,
                sqs=sqs, queue_url=queue_url,
                inv_tbl=inv_tbl, findings_tbl=findings_tbl,
                investigation_id=investigation_id,
                lock_key=lock_key,
                plant_id=plant_id,
                cap_key=cap_key,
                cap_before=cap_before,
                idp=idp,
                pool_id=pool_id,
                user_emails=user_emails,
            )


# ---------------------------------------------------------------------------
# Item 8a placeholder (writes; --allow-writes required)
# ---------------------------------------------------------------------------

_SCHEDULE_GUARD_MIN = 45


def _utc_now() -> dt.datetime:
    """The clock item 8a's schedule guard reads (tests replace it)."""
    return dt.datetime.now(dt.timezone.utc)

_RUN_RECORD_PREFIX = "curated/engineering/pattern_runs/"
#: Fields the patterns job sets per run whatever the result (ADP fault_patterns.py).
_VOLATILE_PATTERN_FIELDS = frozenset({"computed_at"})


def _cron_minute_of_day(expr: str) -> int:
    """Minute of day of ``cron(M H * * ? *)`` with plain numbers; anything else raises.

    Raises ValueError for rate(), at(), or any unsupported expression (fail-closed).
    The _after6_write_window_guard pre-filters rate() schedules before calling this.
    """
    import re  # noqa: PLC0415
    expr = expr.strip()
    if expr.lower().startswith("at("):
        raise ValueError(f"at() expression not supported (fail closed): {expr!r}")
    m = re.fullmatch(r"cron\((\d{1,2}) (\d{1,2}) \* \* \? \*\)", expr)
    if not m or int(m.group(1)) > 59 or int(m.group(2)) > 23:
        raise ValueError(f"unsupported schedule expression {expr!r}")
    return int(m.group(2)) * 60 + int(m.group(1))


def _schedule_guard(scheduler: Any, names: list[tuple[str, str]], now: dt.datetime) -> str | None:
    """A reason to refuse when ``now`` is within 45 min before or after any of the
    ``(name, group)`` schedules (wrapping midnight), else None. Read errors raise (fail closed)."""
    n = now.hour * 60 + now.minute
    for name, group in names:
        sc = scheduler.get_schedule(Name=name, GroupName=group)
        tz = sc.get("ScheduleExpressionTimezone") or "UTC"
        if tz not in ("UTC", "Etc/UTC"):
            return f"{name} uses timezone {tz!r}; only UTC is supported"
        try:
            m = _cron_minute_of_day(sc["ScheduleExpression"])
        except ValueError as exc:
            # Fail closed: at(), unsupported cron, or rate() not pre-filtered
            return f"{name} unsupported expression: {exc}"
        gap = min((n - m) % 1440, (m - n) % 1440)
        if gap < _SCHEDULE_GUARD_MIN:
            return f"{name} ({sc['ScheduleExpression']}) is {gap} min away; needs {_SCHEDULE_GUARD_MIN}"
    return None


def _patterns_snapshot(table: Any, s3: Any, bucket: str, run_date: str | None) -> dict[str, Any]:
    """Every pattern item, the parquet object and the run record for ``run_date``.

    The scan is a consistent read: the "after" snapshot is taken straight after the re-run
    wrote the items, and a stale scan could pass "adds nothing" (review cycle 2 W3)."""
    items = _scan_all(table, consistent=True)
    pq = s3.get_object(Bucket=bucket, Key=_PATTERNS_PARQUET_KEY)
    snap: dict[str, Any] = {"items": items, "parquet": pq["Body"].read(),
                            "parquet_mtime": pq["LastModified"]}
    if run_date:
        rr = s3.get_object(Bucket=bucket, Key=f"{_RUN_RECORD_PREFIX}{run_date}.json")
        snap["record"] = rr["Body"].read()
        snap["record_mtime"] = rr["LastModified"]
    return snap


def _parquet_rows(raw: bytes) -> list[tuple]:
    """The parquet's rows, every column but the volatile ones, sorted by pattern_id."""
    import tempfile  # noqa: PLC0415
    import duckdb  # noqa: PLC0415
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "p.parquet")
        with open(path, "wb") as fh:
            fh.write(raw)
        con = duckdb.connect()
        try:
            cols = [c[0] for c in con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [path]).fetchall()]
            keep = [c for c in cols if c not in _VOLATILE_PATTERN_FIELDS]
            sel = ", ".join(f'"{c}"' for c in keep)
            return con.execute(f"SELECT {sel} FROM read_parquet(?) ORDER BY pattern_id", [path]).fetchall()
        finally:
            con.close()


# ---------------------------------------------------------------------------
# Item 8b — disable ESM, verify repeat POST, check queue empty (stage 3)
# ---------------------------------------------------------------------------


def _item_8b(
    res: Results,
    *,
    stage: str,
    run_id: str,
    manifest: dict,
    manifest_dir: str | None,
    allow_writes: bool,
    **_: Any,
) -> None:
    """Item 8b: disable the ESM, re-POST a completed group (same id), confirm queue empty.

    Design §Item 8b. Leaves the ESM disabled so --part item9 can run inside the same
    write window.  The manifest flag ``event_source_disabled`` is set to True BEFORE
    the disable call; cleanup step 1 uses it if the run crashes.
    """
    import boto3  # noqa: PLC0415

    _n_fail_start_8b = sum(1 for _, s, _ in res.rows if s == "FAIL")
    if not allow_writes:
        res.skip("item-8b", "writes to staging; needs --allow-writes")
        return
    if manifest is None:
        res.skip("item-8b", "no manifest: run --part after-6 to create one")
        return

    now = _utc_now()
    scheduler = boto3.client("scheduler", region_name=_AVX_REGION)
    window_reason = _after6_write_window_guard(scheduler, stage, now)
    if window_reason:
        res.skip("item-8b", f"write window: {window_reason}")
        return

    lam = boto3.client("lambda", region_name=_AVX_REGION)
    sqs = boto3.client("sqs", region_name=_AVX_REGION)

    # Find ESM UUID by listing for the tier2 lambda + queue ARN suffix
    tier2_fn = _TIER2_LAMBDA.format(stage=stage)
    inv_queue_nm = _INV_QUEUE.format(stage=stage)
    esms = _investigation_esms(lam, tier2_fn, inv_queue_nm)
    if not check(res, "item-8b-esm-found", bool(esms),
                 on_fail=f"no SQS ESM for {inv_queue_nm!r} on {tier2_fn!r}"):
        return
    esm_uuid = esms[0]["UUID"]
    esm_state = esms[0].get("State", "")

    # Require Enabled BEFORE we disable
    if not check(res, "item-8b-esm-enabled", esm_state == "Enabled",
                 f"ESM {esm_uuid} State={esm_state}",
                 on_fail=f"ESM is {esm_state!r}; must be Enabled before item 8b disables it"):
        return

    # Save UUID to manifest BEFORE the disable call (cleanup step 1 needs it)
    manifest["event_source_uuid"] = esm_uuid
    # Set per-write flag BEFORE the request
    manifest["event_source_disabled"] = True
    _save_manifest(run_id, manifest, manifest_dir)

    # Disable the ESM — poll to "Disabled" with a bound (C5 fix)
    _update_esm(lam, esm_uuid, False)
    final_state = _wait_esm_state(lam, esm_uuid, "Disabled", max_s=120)
    if not check(res, "item-8b-esm-disabled", final_state == "Disabled",
                 f"ESM {esm_uuid} Disabled (state={final_state})",
                 on_fail=f"ESM did not reach Disabled after 120s; final state={final_state!r}"):
        # Disable request was already sent; flag stays True so cleanup re-enables.
        return

    # W7: precondition — queue AND DLQ must be empty before the repeat POST.
    # (Same invariant the design requires; the ESM is now Disabled so no new messages
    # should appear.  A foreign message here means the queue was not clean going in.)
    q_url_pre = sqs.get_queue_url(QueueName=inv_queue_nm)["QueueUrl"]
    dlq_url_pre = sqs.get_queue_url(QueueName=f"{inv_queue_nm}-dlq")["QueueUrl"]
    for _lbl, _url in (("queue", q_url_pre), ("DLQ", dlq_url_pre)):
        _pre_attrs = sqs.get_queue_attributes(
            QueueUrl=_url,
            AttributeNames=["ApproximateNumberOfMessages",
                            "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        _pre_vis = int(_pre_attrs.get("ApproximateNumberOfMessages", 0))
        _pre_inf = int(_pre_attrs.get("ApproximateNumberOfMessagesNotVisible", 0))
        if _pre_vis != 0 or _pre_inf != 0:
            check(res, f"item-8b-{_lbl}-empty-pre",
                  False,
                  on_fail=f"{_lbl} not empty before repeat POST "
                          f"(visible={_pre_vis} in-flight={_pre_inf}); "
                          f"foreign message present")
            return

    # Re-POST one of item 2's completed pattern groups → must return 200 (same id)
    invs = manifest.get("investigations", [])
    completed = [i for i in invs if i.get("status") == "completed"]
    if not completed:
        check(res, "item-8b-repeat-target", False,
              on_fail="no completed investigations in manifest; run item 2 first")
        return

    repeat_pgid: str = completed[0]["pattern_group_id"]
    expected_inv_id: str = completed[0]["investigation_id"]

    cfn = boto3.client("cloudformation", region_name=_AVX_REGION)
    outputs = {
        o["OutputKey"]: o["OutputValue"]
        for o in cfn.describe_stacks(StackName=_VSA_API_STACK.format(stage=stage))
                   ["Stacks"][0]["Outputs"]
    }
    api_url: str = outputs["RestApiUrl"]

    idp_c = boto3.client("cognito-idp", region_name=_AVX_REGION)
    pool_id = _resolve_pool_id(idp_c)
    client_id = password_client(idp_c, pool_id)

    # Request the window guard AGAIN immediately before this write (design rule)
    window_reason2 = _after6_write_window_guard(scheduler, stage, _utc_now())
    if window_reason2:
        check(res, "item-8b-repeat-window", False, on_fail=f"window closed: {window_reason2}")
        return

    user_emails_8b: list[str] = manifest.setdefault("users", [])
    with principal(idp_c, pool_id, client_id, "eng", ["product-engineer"],
                   created_users=user_emails_8b) as (eng_tok, _, _eng_email_8b):
        status_r, body_r = _item_6_http(
            api_url, "POST", "investigations",
            eng_tok,
            {"pattern_id": repeat_pgid},
        )

    got_id = body_r.get("investigation_id") if isinstance(body_r, dict) else None
    if not check(res, "item-8b-repeat-same-id",
                 status_r == 200 and got_id == expected_inv_id,
                 f"200 same id={expected_inv_id[:8]}",
                 on_fail=f"expected 200 id={expected_inv_id[:8]}, got {status_r} id={got_id}"):
        return

    # Check investigation row and Findings unchanged (consistent read)
    ddb = boto3.resource("dynamodb", region_name=_AVX_REGION)
    inv_tbl = ddb.Table(_INVESTIGATIONS_TABLE.format(stage=stage))
    row = inv_tbl.get_item(
        Key={"investigation_id": expected_inv_id}, ConsistentRead=True
    ).get("Item")
    check(res, "item-8b-inv-unchanged",
          row is not None and row.get("status") == "completed",
          f"investigation row status={row.get('status') if row else 'absent'}",
          on_fail=f"investigation row changed: status={row.get('status') if row else 'absent'}")

    # W7: long-poll receive to confirm queue holds 0 messages after repeat POST.
    # Use large VisibilityTimeout so any unexpected message stays in-flight during cleanup.
    # Check body of any returned message to distinguish own from foreign.
    q_url = sqs.get_queue_url(QueueName=inv_queue_nm)["QueueUrl"]
    resp = sqs.receive_message(
        QueueUrl=q_url, MaxNumberOfMessages=1,
        VisibilityTimeout=600,  # 10 minutes — longer than sum of subsequent waits
        WaitTimeSeconds=_SQS_LONG_POLL_WAIT_S,
    )
    msgs = resp.get("Messages", [])
    check(res, "item-8b-queue-empty", not msgs,
          "queue holds 0 messages after repeat POST",
          on_fail=f"queue has {len(msgs)} message(s) after repeat POST (phantom duplicate?)")

    # W7: record item 8b pass/fail status for after-6-check
    manifest["items_passed"]["8b"] = (
        sum(1 for _, s, _ in res.rows if s == "FAIL") == _n_fail_start_8b
    )
    _save_manifest(run_id, manifest, manifest_dir)

    # ESM is still disabled; --part item9 uses this window.



# ---------------------------------------------------------------------------
# Item 8a (patterns half) — unchanged; declared after item 8b/9 for ordering
# ---------------------------------------------------------------------------


def _item_8a(res: Results, *, stage: str, **_: Any) -> None:
    """Item 8, patterns half: re-running the patterns job on its current run_date adds nothing.

    The handler takes ``{"run_date": ...}`` to re-run a day (ADP
    ``engineering/jobs/fault_patterns.py`` ``lambda_handler``). Re-running the run_date the
    live items already carry rewrites the same day, so a correct job leaves every item,
    every parquet row and the item count as they were (``computed_at`` aside).
    """
    import boto3  # noqa: PLC0415
    from botocore.config import Config  # noqa: PLC0415

    now = _utc_now()
    scheduler = boto3.client("scheduler", region_name=_ADP_REGION)
    names = [(_FAULT_DAILY_SCHEDULE.format(stage=stage), _FAULT_DAILY_SCHEDULE_GROUP.format(stage=stage)),
             (_FAULT_PATTERNS_SCHEDULE.format(stage=stage), _FAULT_PATTERNS_SCHEDULE_GROUP.format(stage=stage))]
    reason = _schedule_guard(scheduler, names, now)
    if reason:
        res.skip("item-8a", f"schedule window: {reason}")
        return

    account = boto3.client("sts").get_caller_identity()["Account"]
    bucket = f"adp-{stage}-foundation-lake-{account}-{_ADP_REGION}"
    table = boto3.resource("dynamodb", region_name=_ADP_REGION).Table(_PATTERNS_TABLE.format(stage=stage))
    s3 = boto3.client("s3", region_name=_ADP_REGION)

    first = _scan_all(table)
    # Items 1/8a rule: ignore LIVETEST- rows and print a NOTE with their count.
    livetest_first = sum(1 for it in first if str(it.get("pattern_id", "")).startswith("LIVETEST-"))
    if livetest_first:
        print(f"  [NOTE] item-8a: ignored {livetest_first} LIVETEST- pattern row(s)")
    first = [it for it in first if not str(it.get("pattern_id", "")).startswith("LIVETEST-")]
    run_dates = sorted({str(it.get("run_date", "")) for it in first})
    if not check(res, "item-8a-one-run-date", len(run_dates) == 1 and bool(run_dates[0]),
                 f"items carry run_date {run_dates[0] if run_dates else ''}",
                 on_fail=f"items carry run_dates {run_dates[:4]} ({len(first)} items)"):
        return
    run_date = run_dates[0]
    before = _patterns_snapshot(table, s3, bucket, run_date)

    lam = boto3.client("lambda", region_name=_ADP_REGION, config=Config(
        read_timeout=960, connect_timeout=10, retries={"total_max_attempts": 1}))
    resp = lam.invoke(FunctionName=_PATTERNS_LAMBDA.format(stage=stage),
                      InvocationType="RequestResponse",
                      Payload=json.dumps({"run_date": run_date}).encode())
    raw = resp["Payload"].read()
    try:
        payload = json.loads(raw or b"null")
    except ValueError:
        payload = None
    ok = (not resp.get("FunctionError") and isinstance(payload, dict)
          and payload.get("status") == "ok" and payload.get("run_date") == run_date)
    if not check(res, "item-8a-rerun-ok", ok,
                 f"re-ran {run_date}: {payload.get('pattern_count') if isinstance(payload, dict) else ''} patterns",
                 on_fail=f"invoke error {resp.get('FunctionError')!r}: {raw[:300]!r}"):
        return

    after = _patterns_snapshot(table, s3, bucket, run_date)
    b_items = {str(i["pattern_id"]): i for i in before["items"]}
    a_items = {str(i["pattern_id"]): i for i in after["items"]}
    check(res, "item-8a-same-ids", set(a_items) == set(b_items) and len(after["items"]) == len(before["items"]),
          f"{len(a_items)} items, same ids",
          on_fail=f"before {len(before['items'])} after {len(after['items'])}; new "
                  f"{sorted(set(a_items) - set(b_items))[:5]} gone {sorted(set(b_items) - set(a_items))[:5]}")

    def _stable(it: dict) -> dict:
        return {k: v for k, v in it.items() if k not in _VOLATILE_PATTERN_FIELDS}
    changed = sorted(k for k in set(a_items) & set(b_items) if _stable(a_items[k]) != _stable(b_items[k]))
    check(res, "item-8a-items-unchanged", not changed, "every item equal apart from computed_at",
          on_fail=f"{len(changed)} items changed, e.g. {changed[:3]}")
    check(res, "item-8a-parquet-unchanged", _parquet_rows(after["parquet"]) == _parquet_rows(before["parquet"]),
          "parquet rows equal apart from computed_at", on_fail="parquet rows differ after the re-run")
    check(res, "item-8a-job-wrote", after["parquet_mtime"] > before["parquet_mtime"]
          and after["record_mtime"] > before["record_mtime"],
          "parquet and run record rewritten by the re-run",
          on_fail="parquet or run record not rewritten: the re-run didn't write (vacuous)")


# ---------------------------------------------------------------------------
# Signal handling — copied from live-verify-fi-recall-warranty.py:280-322
# ---------------------------------------------------------------------------

_SIGNAL_STATE: dict[str, Any] = {}


def _install_signal_handlers() -> None:
    """SIGINT/SIGTERM/SIGHUP each raise SystemExit(143) once so every finally runs.

    W10 fix: SIGHUP added so a closed terminal does not skip cleanup.
    """
    _SIGNAL_STATE["raised"] = False

    def _exit_once(signum: int, frame: object) -> None:
        if not _SIGNAL_STATE["raised"]:
            _SIGNAL_STATE["raised"] = True
            raise SystemExit(143)

    _sig.signal(_sig.SIGINT, _exit_once)
    _sig.signal(_sig.SIGTERM, _exit_once)
    _sig.signal(_sig.SIGHUP, _exit_once)


@contextlib.contextmanager
def _defer_signals_for_restore() -> Iterator[None]:
    """Hold SIGINT/SIGTERM/SIGHUP during cleanup; re-deliver after.

    W1 fix: SIGHUP added — a closed terminal during cleanup no longer interrupts it
    mid-step. All three signals are deferred and re-delivered once the hold exits.
    """
    _pending: list[int] = []

    def _record(sig: int, _frame: object) -> None:
        _pending.append(sig)

    old_int = _sig.signal(_sig.SIGINT, _record)
    old_term = _sig.signal(_sig.SIGTERM, _record)
    old_hup = _sig.signal(_sig.SIGHUP, _record)
    try:
        yield
    finally:
        _sig.signal(_sig.SIGINT, old_int)
        _sig.signal(_sig.SIGTERM, old_term)
        _sig.signal(_sig.SIGHUP, old_hup)
        for s in _pending:
            os.kill(os.getpid(), s)


# ---------------------------------------------------------------------------
# ESM state polling (C5 fix)
# ---------------------------------------------------------------------------

#: Terminal ESM states (not transitional).
_ESM_TERMINAL_STATES = frozenset({"Enabled", "Disabled"})

#: Transitional ESM states that require a retry wait.
_ESM_TRANSITIONAL_STATES = frozenset({"Enabling", "Disabling", "Creating", "Updating", "Deleting"})


def _update_esm(lam: Any, uuid: str, enabled: bool,
                *, max_tries: int = 8, interval_s: float = 5.0) -> None:
    """Call ``UpdateEventSourceMapping``, retrying ``ResourceInUseException`` with a bound.

    C2(b): used at all three update sites (8b disable, re-enable, cleanup step 1 re-enable).
    A ``ResourceInUseException`` means the ESM is still transitioning; retry until it settles.
    Any other exception is re-raised immediately.
    """
    for attempt in range(max_tries):
        try:
            lam.update_event_source_mapping(UUID=uuid, Enabled=enabled)
            return
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code != "ResourceInUseException":
                raise
            if attempt >= max_tries - 1:
                raise
            time.sleep(interval_s)


def _wait_esm_state(
    lam: Any,
    uuid: str,
    target_state: str,
    *,
    max_s: int = 120,
    interval_s: float = 2.0,
) -> str:
    """Poll ``GetEventSourceMapping`` until ``State`` reaches ``target_state`` or the deadline.

    Returns the final ``State`` string.  Retries when ``ResourceInUseException`` is raised
    (ESM is in a transitional state and the API refuses an update).

    C5 fix: ESM state transitions pass through Enabling/Disabling; an immediate readback
    after UpdateEventSourceMapping returns the transitional state, not the terminal one.
    This helper polls until the state is terminal before returning.
    """
    deadline = time.monotonic() + max_s
    state = ""
    while time.monotonic() < deadline:
        try:
            rb = lam.get_event_source_mapping(UUID=uuid)
            state = rb.get("State", "")
            if state == target_state:
                return state
            if state not in _ESM_TRANSITIONAL_STATES:
                # Terminal state other than target — stop polling
                return state
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code != "ResourceInUseException":
                raise
            # ResourceInUseException: ESM is in a transitional state; wait and retry
        time.sleep(interval_s)
    return state  # return last known state even if timeout reached


# ---------------------------------------------------------------------------
# Cognito helpers — copied/adapted from live-verify-fi-recall-warranty.py:325-412
# Brief §Cognito: pool cms-staging-ui-users, prefix livetest-fi-quality-<label>-<epoch>-<hex>
# ---------------------------------------------------------------------------

def _claims(id_token: str) -> dict:
    import base64  # noqa: PLC0415
    payload = id_token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def password_client(idp: Any, pool_id: str) -> str:
    """Return the first pool client allowing USER_PASSWORD_AUTH (every page read)."""
    kwargs: dict[str, Any] = {"UserPoolId": pool_id, "MaxResults": 60}
    while True:
        page = idp.list_user_pool_clients(**kwargs)
        for c in page.get("UserPoolClients", []):
            desc = idp.describe_user_pool_client(
                UserPoolId=pool_id, ClientId=c["ClientId"]
            )
            flows = desc["UserPoolClient"].get("ExplicitAuthFlows") or []
            if "ALLOW_USER_PASSWORD_AUTH" in flows:
                return c["ClientId"]
        if not page.get("NextToken"):
            break
        kwargs["NextToken"] = page["NextToken"]
    raise RuntimeError(f"no app client on {pool_id} allows USER_PASSWORD_AUTH")


def _resolve_pool_id(idp: Any) -> str:
    """Return pool_id for cms-staging-ui-users; fail if not exactly one. Brief §Cognito."""
    token: str | None = None
    matching: list[str] = []
    while True:
        kwargs: dict[str, Any] = {"MaxResults": 60}
        if token:
            kwargs["NextToken"] = token
        page = idp.list_user_pools(**kwargs)
        for p in page.get("UserPools", []):
            if p["Name"] == _CMS_POOL_NAME:
                matching.append(p["Id"])
        token = page.get("NextToken")
        if not token:
            break
    if len(matching) != 1:
        raise RuntimeError(
            f"expected exactly one pool named {_CMS_POOL_NAME!r}, found {len(matching)}"
        )
    return matching[0]


#: Ephemeral-user deletes: attempts and the backoff step between them (security cycle 1 W1).
_USER_DELETE_ATTEMPTS = 3
_USER_DELETE_BACKOFF_S = 1.0
#: A user this script created that is older than this is a left-over from an earlier run.
_STALE_USER_AGE = dt.timedelta(hours=1)
#: Exactly the usernames principal() creates (labels eng, cs, fop).
_LIVETEST_USER_RE = re.compile(
    rf"{re.escape(_USER_PREFIX)}-(?:eng|cs|fop)-\d+-[0-9a-f]{{6}}@example\.invalid")


def _delete_user(idp: Any, pool_id: str, username: str) -> None:
    """``admin_delete_user`` with retries; an already-gone user counts as deleted. Raises the
    last error after ``_USER_DELETE_ATTEMPTS`` tries."""
    for attempt in range(_USER_DELETE_ATTEMPTS):
        try:
            idp.admin_delete_user(UserPoolId=pool_id, Username=username)
            return
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code == "UserNotFoundException":
                return
            if attempt == _USER_DELETE_ATTEMPTS - 1:
                raise
            time.sleep(_USER_DELETE_BACKOFF_S * (attempt + 1))


def _reap_stale_users(res: Results, idp: Any, pool_id: str) -> None:
    """Delete this script's own ephemeral users that an earlier run failed to delete: only
    usernames of principal()'s exact shape, and only ones older than ``_STALE_USER_AGE`` (a
    newer one may belong to a run in progress). Security review cycle 1 W1."""
    now = dt.datetime.now(dt.timezone.utc)
    stale: list[tuple[str, str]] = []
    recent = 0
    kwargs: dict[str, Any] = {"UserPoolId": pool_id, "Filter": f'email ^= "{_USER_PREFIX}-"',
                              "Limit": 60}
    while True:
        page = idp.list_users(**kwargs)
        for u in page.get("Users", []):
            email = next((a["Value"] for a in u.get("Attributes", []) if a["Name"] == "email"), "")
            if not _LIVETEST_USER_RE.fullmatch(email):
                continue
            created = u.get("UserCreateDate")
            if created is None or now - created < _STALE_USER_AGE:
                recent += 1
                continue
            stale.append((u["Username"], email))
        if not page.get("PaginationToken"):
            break
        kwargs["PaginationToken"] = page["PaginationToken"]
    failed: list[str] = []
    with _defer_signals_for_restore():
        for username, email in stale:
            try:
                _delete_user(idp, pool_id, username)
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{email}: {type(exc).__name__}")
    check(res, "item-6-reap-stale-users", not failed,
          f"{len(stale)} left-over user(s) from earlier runs deleted; {recent} recent left alone",
          on_fail=f"couldn't delete left-over users {failed}")


def _reap_stale_users_step(res: Results, idp: Any = None, pool_id: str | None = None) -> None:
    """Security review 6.4 cycle 1 W1: after-6 and ``--cleanup-only`` reap this script's own
    left-over temp users too (a SIGKILL between ``admin_create_user`` and the manifest save
    leaves one the manifest never records). One try: a failure is a FAIL row, never a raise."""
    try:
        if idp is None:
            import boto3  # noqa: PLC0415
            idp = boto3.client("cognito-idp", region_name=_AVX_REGION)
        if not pool_id:
            pool_id = _resolve_pool_id(idp)
        _reap_stale_users(res, idp, pool_id)
    except Exception as exc:  # noqa: BLE001
        check(res, "after6-reap-stale-users", False, on_fail=f"{type(exc).__name__}: {exc}")


@contextlib.contextmanager
def principal(
    idp: Any,
    pool_id: str,
    client_id: str,
    label: str,
    groups: list[str],
    fleet_ids: str | None = None,
    created_users: list[str] | None = None,
) -> Iterator[tuple[str, dict, str]]:
    """Yield (id_token, claims, username) for an ephemeral user; delete in finally.

    The username (email) is yielded as the third element, and also appended to
    ``created_users`` as soon as the user exists, so callers can confirm every created user
    absent even when a later setup step raises (review cycle 2 S3). The delete runs with
    signals held (S8). Brief §Cognito.
    """
    username = (
        f"{_USER_PREFIX}-{label}-{int(time.time())}-{secrets.token_hex(3)}@example.invalid"
    )
    password = f"Lv{secrets.token_urlsafe(18)}!9Qz"
    attrs: list[dict[str, str]] = [
        {"Name": "email", "Value": username},
        {"Name": "email_verified", "Value": "true"},
    ]
    if fleet_ids is not None:
        attrs.append({"Name": "custom:fleetIds", "Value": fleet_ids})
    created = False
    try:
        idp.admin_create_user(
            UserPoolId=pool_id, Username=username,
            MessageAction="SUPPRESS", UserAttributes=attrs,
        )
        created = True
        if created_users is not None:
            created_users.append(username)
        idp.admin_set_user_password(
            UserPoolId=pool_id, Username=username, Password=password, Permanent=True
        )
        for g in groups:
            idp.admin_add_user_to_group(
                UserPoolId=pool_id, Username=username, GroupName=g
            )
        auth = idp.initiate_auth(
            ClientId=client_id, AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": username, "PASSWORD": password},
        )
        token = auth["AuthenticationResult"]["IdToken"]
        print(f"  principal {label}: groups={groups} fleetIds={fleet_ids!r} (ephemeral)")
        yield token, _claims(token), username
    finally:
        if created:
            with _defer_signals_for_restore():
                try:
                    _delete_user(idp, pool_id, username)
                    print(f"  cleanup: deleted principal {label}")
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"  cleanup: FAILED to delete {username}: {type(exc).__name__}",
                        file=sys.stderr,
                    )


# ---------------------------------------------------------------------------
# Item 1 — detection (read-only)
# Brief §Item 1
# _matches copied from ADP test_patterns_on_simulation.py:36-52
# ---------------------------------------------------------------------------

# build_window dimension names, from test_patterns_on_simulation.py:34
_BW_DIMS = frozenset(("build_window_2m", "build_window_3m", "build_window_4m"))


def _matches(row: dict, ak: dict) -> bool:
    """Return True if a patterns DynamoDB row matches an answer-key row.
    Copied from ADP platform-foundation/source/data-products/engineering/tests/
    test_patterns_on_simulation.py lines 36-52, adapted for dict access.
    """
    if row.get("code_family") != ak.get("code_family"):
        return False
    dim = str(row.get("cohort_dimension", ""))
    val = str(row.get("cohort_value", ""))
    cd = ak.get("cause_dimension", "")
    if cd == "plant":
        return dim == "assembly_plant" and val == ak.get("cause_value")
    if cd == "supplier_lot":
        return dim == "supplier_lot" and val == ak.get("cause_value")
    if cd == "build_window":
        if dim not in _BW_DIMS:
            return False
        start, end = val.split(":")
        return any(start <= m <= end for m in str(ak.get("cause_value", "")).split(":"))
    return False


def _load_answer_key(path: str) -> list[dict]:
    """Read the local answer-key parquet with duckdb (pyarrow isn't in the venv).

    Columns (ADP fleet_fault_events plants.py answer key): pattern_id, code_family,
    cause_dimension, cause_value, planted_count, start_date, failure_mode_id.
    Dates come back as ISO strings.
    """
    import duckdb  # noqa: PLC0415
    con = duckdb.connect()
    try:
        cur = con.execute("SELECT * FROM read_parquet(?)", [path])
        cols = [c[0] for c in cur.description]
        out = []
        for row in cur.fetchall():
            out.append({c: (v.isoformat() if hasattr(v, "isoformat") else v)
                        for c, v in zip(cols, row)})
        return out
    finally:
        con.close()


_AK_COLUMNS = frozenset({"pattern_id", "code_family", "cause_dimension", "cause_value",
                         "planted_count", "start_date", "failure_mode_id"})
_PATTERNS_PARQUET_KEY = "curated/engineering/fault_patterns/fault_patterns.parquet"


def _scan_all(table: Any, *, consistent: bool = False) -> list[dict]:
    """Full paged scan of a boto3 resource Table (follows every LastEvaluatedKey).

    ``consistent=True`` for any read that must see a write this run just made."""
    items: list[dict] = []
    kwargs: dict[str, Any] = {"ConsistentRead": True} if consistent else {}
    while True:
        page = table.scan(**kwargs)
        items.extend(page.get("Items", []))
        lek = page.get("LastEvaluatedKey")
        if not lek:
            return items
        kwargs["ExclusiveStartKey"] = lek


def _parquet_pattern_ids(s3: Any, bucket: str) -> set[str]:
    """pattern_id set of the published fault_patterns parquet (one object per run)."""
    import tempfile  # noqa: PLC0415
    import duckdb  # noqa: PLC0415
    with tempfile.TemporaryDirectory() as td:
        local = os.path.join(td, "fault_patterns.parquet")
        s3.download_file(bucket, _PATTERNS_PARQUET_KEY, local)
        con = duckdb.connect()
        try:
            return {r[0] for r in con.execute(
                "SELECT pattern_id FROM read_parquet(?)", [local]).fetchall()}
        finally:
            con.close()


def _score_detection(items: list[dict], ak_rows: list[dict]) -> dict[str, Any]:
    """Score the patterns job's rows against the answer key (pure; task 5.3 item 1).

    ``found`` = at least one pattern row matches the plant (``_matches``, ADP's rule).
    Denominators: ``active`` plants (start_date <= run_date) and all plants; both ratios
    must reach 0.8. ``noise`` = pattern rows in a code family whose every plant is
    future-dated (ADP test assert 2). ``run_dates`` lists every run_date seen.
    """
    run_dates = sorted({str(it.get("run_date", "")) for it in items})
    run_date = run_dates[0] if len(run_dates) == 1 else ""
    active = [ak for ak in ak_rows if run_date and str(ak["start_date"]) <= run_date]
    active_families = {ak["code_family"] for ak in active}
    future_only = {ak["code_family"] for ak in ak_rows} - active_families
    found: dict[str, dict] = {}
    for ak in ak_rows:
        hits = [it for it in items if _matches(it, ak)]
        if hits:
            found[str(ak["pattern_id"])] = min(hits, key=lambda it: float(it["p_value"]))
    found_active = [ak for ak in active if str(ak["pattern_id"]) in found]
    noise = sorted({str(it["pattern_id"]) for it in items if it.get("code_family") in future_only})
    return {
        "run_dates": run_dates, "run_date": run_date, "active": active,
        "found": found, "found_active": found_active, "future_only": sorted(future_only),
        "noise": noise,
        "r_active": (len(found_active) / len(active)) if active else 0.0,
        "r_all": (len(found) / len(ak_rows)) if ak_rows else 0.0,
    }


def _fmt_ratio(v: Any) -> str:
    """A pattern's rate_ratio for the report: ADP stores None for an infinite ratio
    (``fault_patterns.py``), which must not turn a passed item 1 into an exception
    (review cycle 1 S7 / cycle 2 S5)."""
    return "inf" if v is None else f"{float(v):.2f}"


def _item_1(res: Results, *, stage: str, answer_key: str | None, **_: Any) -> None:
    """Item 1: the patterns job finds >= 80% of planted patterns on their cause (read-only).

    Reads ADP's DynamoDB copy of fault_patterns (the operator has no Lake Formation
    SELECT on the Athena table) and checks its pattern_id set equals the published
    parquet's. The answer key is a local file, read by this script only.
    """
    import boto3  # noqa: PLC0415

    if not answer_key:
        res.skip("item-1", "needs --answer-key <local parquet>")
        return
    ak_rows = _load_answer_key(answer_key)
    cols_ok = bool(ak_rows) and all(_AK_COLUMNS <= set(r) for r in ak_rows)
    if not check(res, "item-1-answer-key", cols_ok, f"{len(ak_rows)} planted patterns",
                 on_fail=f"answer key empty or missing columns {sorted(_AK_COLUMNS)}"):
        return

    account = boto3.client("sts").get_caller_identity()["Account"]
    bucket = f"adp-{stage}-foundation-lake-{account}-{_ADP_REGION}"
    items = _scan_all(boto3.resource("dynamodb", region_name=_ADP_REGION)
                      .Table(_PATTERNS_TABLE.format(stage=stage)))
    # Items 1/8a rule: ignore LIVETEST- rows and print a NOTE with their count.
    livetest_count = sum(1 for it in items if str(it.get("pattern_id", "")).startswith("LIVETEST-"))
    if livetest_count:
        print(f"  [NOTE] item-1: ignored {livetest_count} LIVETEST- pattern row(s)")
    items = [it for it in items if not str(it.get("pattern_id", "")).startswith("LIVETEST-")]
    if not check(res, "item-1-patterns-present", bool(items), f"{len(items)} pattern rows",
                 on_fail="no pattern rows in the patterns table"):
        return
    ddb_ids = {str(it["pattern_id"]) for it in items}
    pq_ids = _parquet_pattern_ids(boto3.client("s3", region_name=_ADP_REGION), bucket)
    check(res, "item-1-ddb-equals-parquet", ddb_ids == pq_ids,
          f"{len(ddb_ids)} ids in both",
          on_fail=f"DynamoDB-only {sorted(ddb_ids - pq_ids)[:5]} parquet-only "
                  f"{sorted(pq_ids - ddb_ids)[:5]} (sizes {len(ddb_ids)}/{len(pq_ids)})")

    sc = _score_detection(items, ak_rows)
    if not check(res, "item-1-one-run-date", bool(sc["run_date"]), f"run_date {sc['run_date']}",
                 on_fail=f"rows carry {len(sc['run_dates'])} run_dates {sc['run_dates'][:4]}"):
        return
    n_act, n_all = len(sc["active"]), len(ak_rows)
    check(res, "item-1-found-active", bool(sc["active"]) and sc["r_active"] >= 0.8,
          f"{len(sc['found_active'])}/{n_act} active plants found ({sc['r_active']:.0%})",
          on_fail=f"{len(sc['found_active'])}/{n_act} active plants found "
                  f"({sc['r_active']:.0%}; needs 80%)")
    check(res, "item-1-found-all", sc["r_all"] >= 0.8,
          f"{len(sc['found'])}/{n_all} planted found ({sc['r_all']:.0%})",
          on_fail=f"{len(sc['found'])}/{n_all} planted found ({sc['r_all']:.0%}; needs 80%)")
    check(res, "item-1-no-noise", not sc["noise"],
          f"no pattern in future-only families {sc['future_only']}",
          on_fail=f"pattern rows {sc['noise'][:5]} in future-only families {sc['future_only']}")
    for ak in ak_rows:
        pid = str(ak["pattern_id"])
        hit = sc["found"].get(pid)
        state = "future" if ak not in sc["active"] else ("found" if hit else "NOT FOUND")
        line = (f"    {pid} {ak['code_family']} {ak['cause_dimension']}={ak['cause_value']}: {state}")
        if hit:
            line += (f" as {hit['cohort_dimension']}={hit['cohort_value']} group "
                     f"{hit['pattern_group_id']} rank {hit['candidate_rank']} "
                     f"ratio {_fmt_ratio(hit.get('rate_ratio'))} p {float(hit['p_value']):.2e}")
        print(line)


# ---------------------------------------------------------------------------
# Item 7 — answer keys unreadable (read-only, IAM simulation)
# Brief §Item 7
# ---------------------------------------------------------------------------

# Restricted prefixes: ADP fleet_health_stack.py _ANSWER_KEY_PREFIXES (brief §Item 7)
_ANSWER_KEY_PREFIXES = (
    "restricted/fleet_fault_events/answer_key/answer_key.parquet",
    "restricted/engineering/answer_key/answer_key.parquet",
)

# Positive control paths (brief §Item 7, granted today, IAM read 2026-10-16)
_TIER2_POSITIVE = "curated/service_records/x"
_FI_POSITIVE = "curated/fleet_recall/x"

# FI role name prefix (brief §Item 7)
_CMS_FI_ROLE_PREFIX = "cms-staging-ui-FleetIntelligenceRole"


def _resolve_fi_role(iam: Any) -> str:
    """Resolve the unique CMS FI role. Brief §Item 7: paged list_roles, fail unless exactly one."""
    token: str | None = None
    matching: list[str] = []
    while True:
        kwargs: dict[str, Any] = {"PathPrefix": "/"}
        if token:
            kwargs["Marker"] = token
        page = iam.list_roles(**kwargs)
        for r in page.get("Roles", []):
            if r["RoleName"].startswith(_CMS_FI_ROLE_PREFIX):
                matching.append(r["Arn"])
        token = page.get("Marker") if page.get("IsTruncated") else None
        if not token:
            break
    if len(matching) != 1:
        raise ValueError(
            f"expected exactly one role matching {_CMS_FI_ROLE_PREFIX!r}, found {len(matching)}: {matching}"
        )
    return matching[0]


def _simulate(iam: Any, role_arn: str, actions: list[str], resource: str, *,
              resource_policy: str | None = None, owner: str | None = None) -> dict[str, str]:
    """``simulate_principal_policy`` for one resource; return {action: EvalDecision}.

    Follows ``Marker`` pages, and raises unless exactly one decision per action came
    back (an empty result would make every "denied" check pass vacuously).
    """
    kwargs: dict[str, Any] = {"PolicySourceArn": role_arn, "ActionNames": actions,
                              "ResourceArns": [resource]}
    if resource_policy is not None:
        kwargs["ResourcePolicy"] = resource_policy
        kwargs["ResourceOwner"] = owner
    out: dict[str, str] = {}
    while True:
        resp = iam.simulate_principal_policy(**kwargs)
        for ev in resp.get("EvaluationResults", []):
            out[ev["EvalActionName"]] = ev["EvalDecision"]
        if not resp.get("IsTruncated"):
            break
        kwargs["Marker"] = resp["Marker"]
    if sorted(out) != sorted(actions):
        raise RuntimeError(f"simulation returned decisions for {sorted(out)}, asked {sorted(actions)}")
    return out


def _restricted_glue_locations(glue: Any, db_name: str) -> list[str]:
    """Tables in ``db_name`` whose location is under ``restricted/`` (paged; errors raise)."""
    bad: list[str] = []
    kwargs: dict[str, Any] = {"DatabaseName": db_name}
    while True:
        page = glue.get_tables(**kwargs)
        for t in page.get("TableList", []):
            loc = (t.get("StorageDescriptor") or {}).get("Location", "") or ""
            if "/restricted/" in loc or loc.endswith("/restricted"):
                bad.append(f"{t['Name']}={loc}")
        if not page.get("NextToken"):
            return bad
        kwargs["NextToken"] = page["NextToken"]


def _item_7(res: Results, *, stage: str, **_: Any) -> None:
    """Item 7: the answer keys are unreadable by the Tier 2 and CMS FI roles (read-only).

    IAM policy simulation, identity only and again with the lake bucket's policy. Each
    deny is meaningful only next to a positive control the same role is allowed, so a
    control that isn't ``allowed`` fails the item.
    """
    import boto3  # noqa: PLC0415

    account = boto3.client("sts").get_caller_identity()["Account"]
    iam = boto3.client("iam")
    owner = f"arn:aws:iam::{account}:root"
    bucket = f"adp-{stage}-foundation-lake-{account}-{_ADP_REGION}"
    tier2_role_arn = f"arn:aws:iam::{account}:role/vsa-{stage}-tier2-scheduler-{_AVX_REGION}"
    fi_role_arn = _resolve_fi_role(iam)
    check(res, "item-7-roles", True, f"tier2={tier2_role_arn} fi={fi_role_arn}")

    bucket_policy: str | None = None
    try:
        bucket_policy = boto3.client("s3", region_name=_ADP_REGION).get_bucket_policy(
            Bucket=bucket)["Policy"]
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if code != "NoSuchBucketPolicy":
            raise
        print("  [NOTE] item-7: the lake bucket has no bucket policy; identity-only simulation")
    modes = [("identity", None)] + ([("bucket-policy", bucket_policy)] if bucket_policy else [])

    roles = [("tier2", tier2_role_arn, _TIER2_POSITIVE), ("fi", fi_role_arn, _FI_POSITIVE)]
    for role_label, role_arn, positive in roles:
        for mode, rp in modes:
            ctrl = _simulate(iam, role_arn, ["s3:GetObject"], f"arn:aws:s3:::{bucket}/{positive}",
                             resource_policy=rp, owner=owner)["s3:GetObject"]
            check(res, f"item-7-{role_label}-{mode}-control", ctrl == "allowed",
                  f"s3:GetObject on {positive}: allowed",
                  on_fail=f"control s3:GetObject on {positive} is {ctrl}; a deny below proves nothing")
            for key in _ANSWER_KEY_PREFIXES:
                dec = _simulate(iam, role_arn, ["s3:GetObject", "s3:GetObjectVersion"],
                                f"arn:aws:s3:::{bucket}/{key}", resource_policy=rp, owner=owner)
                ok = all(d in ("implicitDeny", "explicitDeny") for d in dec.values())
                check(res, f"item-7-{role_label}-{mode}-{key.split('/')[1]}", ok,
                      f"{key}: {dec}", on_fail=f"{key} readable: {dec}")

    glue = boto3.client("glue", region_name=_ADP_REGION)
    for db_name in (f"adp_{stage}_fleet_health", f"adp_{stage}_engineering"):
        bad = _restricted_glue_locations(glue, db_name)
        check(res, f"item-7-glue-{db_name}", not bad, "no table located under restricted/",
              on_fail=f"tables located under restricted/: {bad}")


# ---------------------------------------------------------------------------
# Item 9 helpers
# ---------------------------------------------------------------------------


def _item9_check_window(
    scheduler: Any, stage: str, label: str, res: Results
) -> bool:
    """Call the write-window guard; record a SKIP on ``res`` and return False if closed.

    Each call consumes one ``_utc_now()`` invocation, which is used by
    ``test_g7_window_closes_before_putitem`` to sequence the guard firings.
    """
    now = _utc_now()
    reason = _after6_write_window_guard(scheduler, stage, now)
    if reason:
        res.skip(label, f"write window: {reason}")
        return False
    return True


def _item9_cleanup(
    rows: "list[tuple[str, str, str]]",
    *,
    manifest: dict,
    manifest_dir: "str | None",
    stage: str,
) -> None:
    """Run item 9 cleanup steps; append ``(label, status, detail)`` tuples to ``rows``.

    Must be called inside a ``_defer_signals_for_restore()`` context.  The caller
    records the tuples to ``res`` AFTER the signal hold exits (contract rule).

    Steps (each has its own try/except so a failure does not skip later steps):
    1. item9-cleanup-dlq  — receive + delete the LIVETEST-9 message; confirm DLQ 0/0.
    2. item9-cleanup-dlq-foreign — FAIL for every other message; restore visibility=0.
    3. item9-cleanup-row  — DeleteItem the investigation row (conditional on pattern_id).
    4. item9-residue      — row absent, DLQ 0/0/0, alarm back to OK within 15 min.
    """
    import boto3  # noqa: PLC0415

    inv_id = manifest.get("item9_investigation_id")
    pattern_id = manifest.get("item9_pattern_id")
    dlq_body = manifest.get("item9_dlq_body")
    dlq_sent = manifest.get("item9_dlq_sent", False)
    row_put_sent = manifest.get("item9_row_put_sent", False)
    # W1 keys (written by Lane X's _run_item9; absent in cleanup-only tests that
    # don't seed them; both default to False so the W1 loop only fires when seeded)
    invoke_sent = manifest.get("item9_invoke_sent", False)
    invoke_answered = manifest.get("item9_invoke_answered", False)
    invoke_started_at_s = manifest.get("item9_invoke_started_at")
    # W7 keys
    alarm_seen_at_s = manifest.get("item9_alarm_seen_at")
    dlq_send_at_s = manifest.get("item9_dlq_send_at")

    sqs_c = boto3.client("sqs", region_name=_AVX_REGION)
    ddb_r = boto3.resource("dynamodb", region_name=_AVX_REGION)
    inv_tbl_c = ddb_r.Table(_INVESTIGATIONS_TABLE.format(stage=stage))
    cw_c = boto3.client("cloudwatch", region_name=_AVX_REGION)

    dlq_name_c = _INV_DLQ.format(stage=stage)
    alarm_name_c = _INV_DLQ_ALARM.format(stage=stage, region=_AVX_REGION)

    # ── Step 1+2: DLQ cleanup ───────────────────────────────────────────────
    if not dlq_sent:
        rows.append(("item9-cleanup-dlq", "PASS", "nothing sent"))
        rows.append(("item9-cleanup-dlq-foreign", "PASS", "nothing sent"))
    else:
        try:
            dlq_url_c = sqs_c.get_queue_url(QueueName=dlq_name_c)["QueueUrl"]

            # Receive loop: up to 3 batches of 10 to find our message.
            our_handles: "list[str]" = []
            foreign_msgs: "list[dict]" = []
            _seen_ids: "set[str]" = set()
            for _ in range(3):
                recv_resp = sqs_c.receive_message(
                    QueueUrl=dlq_url_c,
                    MaxNumberOfMessages=10,
                    VisibilityTimeout=900,
                    WaitTimeSeconds=_SQS_LONG_POLL_WAIT_S,
                    AttributeNames=["All"],
                )
                msgs = recv_resp.get("Messages", [])
                if not msgs:
                    break
                for m in msgs:
                    mid = m.get("MessageId", "")
                    if mid in _seen_ids:
                        continue
                    _seen_ids.add(mid)
                    if m.get("Body") == dlq_body:
                        our_handles.append(m["ReceiptHandle"])
                    else:
                        foreign_msgs.append(m)
                if our_handles:
                    break  # found ours; no need to pull more

            # Restore visibility of foreign messages immediately (VisibilityTimeout=0);
            # record any ChangeMessageVisibility errors.
            foreign_ids: "list[str]" = []
            foreign_cmv_errs: "list[str]" = []
            for fm in foreign_msgs:
                fm_id = fm.get("MessageId", "unknown")
                try:
                    sqs_c.change_message_visibility(
                        QueueUrl=dlq_url_c,
                        ReceiptHandle=fm["ReceiptHandle"],
                        VisibilityTimeout=0,
                    )
                except Exception as _cvm_exc:  # noqa: BLE001
                    foreign_cmv_errs.append(
                        f"{fm_id}: {type(_cvm_exc).__name__}: {_cvm_exc}"
                    )
                foreign_ids.append(fm_id)

            # Delete our message(s); on failure record the error and restore
            # visibility to 0 (so the alarm can recover instead of re-firing).
            delete_errs: "list[str]" = []
            for handle in our_handles:
                try:
                    sqs_c.delete_message(QueueUrl=dlq_url_c, ReceiptHandle=handle)
                except Exception as _del_exc:  # noqa: BLE001
                    delete_errs.append(
                        f"{type(_del_exc).__name__}: {_del_exc}"
                    )
                    try:
                        sqs_c.change_message_visibility(
                            QueueUrl=dlq_url_c,
                            ReceiptHandle=handle,
                            VisibilityTimeout=0,
                        )
                    except Exception:  # noqa: BLE001
                        pass

            # Confirm DLQ 0 visible + 0 in-flight (poll up to 3 min)
            _safe_print(
                f"  [INFO] confirming DLQ {dlq_name_c!r} reaches 0/0...",
                flush=True,
            )
            dlq_empty_c = False
            conf_dl = time.monotonic() + _ITEM9_DLQ_CONFIRM_MAX_S
            conf_reads = 0
            conf_max = _ITEM9_DLQ_CONFIRM_MAX_S // _ITEM9_DLQ_CONFIRM_INTERVAL_S + 2
            c_vis = c_inf = 0
            while True:
                conf_reads += 1
                c_attrs = sqs_c.get_queue_attributes(
                    QueueUrl=dlq_url_c,
                    AttributeNames=["ApproximateNumberOfMessages",
                                   "ApproximateNumberOfMessagesNotVisible"],
                )["Attributes"]
                c_vis = int(c_attrs.get("ApproximateNumberOfMessages", 0))
                c_inf = int(c_attrs.get("ApproximateNumberOfMessagesNotVisible", 0))
                if c_vis == 0 and c_inf == 0:
                    dlq_empty_c = True
                    break
                if time.monotonic() >= conf_dl or conf_reads >= conf_max:
                    break
                time.sleep(_ITEM9_DLQ_CONFIRM_INTERVAL_S)

            dlq_detail = f"deleted {len(our_handles) - len(delete_errs)} msg(s); DLQ 0/0"
            if delete_errs:
                dlq_detail = "; ".join(delete_errs)
            if dlq_empty_c and not delete_errs:
                rows.append(("item9-cleanup-dlq", "PASS", dlq_detail))
            else:
                rows.append(("item9-cleanup-dlq", "FAIL",
                             dlq_detail if delete_errs
                             else f"DLQ not empty after delete: vis={c_vis} inf={c_inf}"))

            f_parts: "list[str]" = []
            if foreign_ids:
                f_parts.append("foreign MessageId(s): " + ", ".join(foreign_ids))
            if foreign_cmv_errs:
                f_parts.append("ChangeMessageVisibility err(s): "
                               + "; ".join(foreign_cmv_errs))
            if f_parts:
                rows.append(("item9-cleanup-dlq-foreign", "FAIL",
                             "; ".join(f_parts)))
            else:
                rows.append(("item9-cleanup-dlq-foreign", "PASS", "no foreign messages"))

        except Exception as exc:  # noqa: BLE001
            rows.append(("item9-cleanup-dlq", "FAIL", f"{type(exc).__name__}: {exc}"))
            rows.append(("item9-cleanup-dlq-foreign", "FAIL", "not reached"))

    # ── Step 3: Row cleanup ─────────────────────────────────────────────────
    if not row_put_sent or not inv_id or not pattern_id:
        rows.append(("item9-cleanup-row", "PASS", "nothing sent"))
    else:
        try:
            _row_deleted = False
            try:
                inv_tbl_c.delete_item(
                    Key={"investigation_id": inv_id},
                    ConditionExpression="pattern_id = :p",
                    ExpressionAttributeValues={":p": pattern_id},
                )
                _row_deleted = True
            except Exception as _del_exc:  # noqa: BLE001
                _del_code = (
                    getattr(_del_exc, "response", {}).get("Error", {}).get("Code", "")
                )
                if _del_code == "ConditionalCheckFailedException":
                    _re_row0 = inv_tbl_c.get_item(
                        Key={"investigation_id": inv_id}, ConsistentRead=True
                    ).get("Item", {})
                    _row_deleted = not bool(_re_row0)
                else:
                    rows.append(("item9-cleanup-row", "FAIL",
                                f"{type(_del_exc).__name__}: {_del_exc}"))
                    # Skip the rest of step 3 so an error isn't double-reported.
                    raise

            # W1: when invoke was STARTED but the answer was not received, the
            # Lambda may still be running; its _set_failed UpdateItem can recreate
            # the row (without pattern_id) after the client's delete.  Keep reading
            # until invoke_started_at + 960 s regardless of whether the initial
            # delete succeeded, re-deleting on reappearance (pattern_id = :p OR
            # attribute_not_exists(pattern_id)).  After the deadline, delete once
            # if a row without pattern_id is present (no loop — Lambda is gone).
            if invoke_started_at_s and not invoke_answered:
                _w1_deadline: "dt.datetime | None" = None
                try:
                    _w1_deadline = (
                        dt.datetime.fromisoformat(
                            invoke_started_at_s.replace("Z", "+00:00")
                        ) + dt.timedelta(seconds=960)
                    )
                except (ValueError, TypeError):
                    pass
                _w1_reads = 0
                _w1_max = 960 // 30 + 2  # ~34 iterations at 30-s intervals
                _safe_print(
                    f"  [INFO] W1: invoke no-answer loop (deadline "
                    f"{_w1_deadline.isoformat() if _w1_deadline else 'cap-only'}; "
                    f"max {_w1_max} reads)...",
                    flush=True,
                )
                while True:
                    _w1_reads += 1
                    _now_utc = dt.datetime.now(dt.timezone.utc)
                    _w1_deadline_reached = (
                        (_w1_deadline and _now_utc >= _w1_deadline)
                        or _w1_reads > _w1_max
                    )
                    _w1_row = inv_tbl_c.get_item(
                        Key={"investigation_id": inv_id}, ConsistentRead=True
                    ).get("Item", {})
                    if not _w1_row:
                        _row_deleted = True
                        if _w1_deadline_reached:
                            break
                        time.sleep(30)
                        continue
                    if _w1_deadline_reached:
                        # After deadline: one delete conditioned on no pattern_id
                        # (Lambda can no longer write; what the read just saw).
                        # _row_deleted is set from a final consistent read
                        # regardless of whether the delete succeeded.
                        if not _w1_row.get("pattern_id"):
                            try:
                                inv_tbl_c.delete_item(
                                    Key={"investigation_id": inv_id},
                                    ConditionExpression="attribute_not_exists(pattern_id)",
                                )
                            except Exception:  # noqa: BLE001
                                pass
                            _row_deleted = not bool(inv_tbl_c.get_item(
                                Key={"investigation_id": inv_id},
                                ConsistentRead=True,
                            ).get("Item"))
                        break
                    _safe_print(
                        f"  [INFO] row {inv_id[:8]!r} reappeared (invoke no-answer, "
                        f"read {_w1_reads}); re-deleting...",
                        flush=True,
                    )
                    try:
                        inv_tbl_c.delete_item(
                            Key={"investigation_id": inv_id},
                            ConditionExpression=(
                                "pattern_id = :p OR attribute_not_exists(pattern_id)"
                            ),
                            ExpressionAttributeValues={":p": pattern_id},
                        )
                        _row_deleted = True
                    except Exception:  # noqa: BLE001
                        pass
                    time.sleep(30)

            if _row_deleted:
                rows.append(("item9-cleanup-row", "PASS", f"deleted {inv_id[:8]}"))
            else:
                _final_row = inv_tbl_c.get_item(
                    Key={"investigation_id": inv_id}, ConsistentRead=True
                ).get("Item", {})
                if not _final_row:
                    rows.append(("item9-cleanup-row", "PASS", "row absent"))
                else:
                    rows.append(("item9-cleanup-row", "FAIL",
                                f"row present: pattern_id={_final_row.get('pattern_id')!r}"))

        except Exception as exc:  # noqa: BLE001
            # Only record if not already recorded by the inner delete-error path.
            if not any(lbl == "item9-cleanup-row" for lbl, _, _ in rows):
                rows.append(("item9-cleanup-row", "FAIL", f"{type(exc).__name__}: {exc}"))

    # ── Step 4: Residue check ───────────────────────────────────────────────
    try:
        # S15: row and alarm checks first; DLQ URL resolved just before the DLQ
        # attribute read so a GetQueueUrl error only skips that read, not the rest.
        row_absent_c = True
        if inv_id:
            res_row = inv_tbl_c.get_item(
                Key={"investigation_id": inv_id}, ConsistentRead=True
            ).get("Item", {})
            row_absent_c = not bool(res_row)

        # S15: DLQ URL + attribute read wrapped so a GetQueueUrl failure yields
        # dlq_r_ok=False rather than skipping the alarm waits entirely.  The
        # exception text is preserved so the FAIL detail explains why, not "0/0/0".
        dlq_r_vis = dlq_r_inf = dlq_r_del = 0
        dlq_r_ok = False
        _dlq_r_err: "str | None" = None
        try:
            _dlq_url_r = sqs_c.get_queue_url(QueueName=dlq_name_c)["QueueUrl"]
            dlq_r_attrs = sqs_c.get_queue_attributes(
                QueueUrl=_dlq_url_r,
                AttributeNames=["ApproximateNumberOfMessages",
                               "ApproximateNumberOfMessagesNotVisible",
                               "ApproximateNumberOfMessagesDelayed"],
            )["Attributes"]
            dlq_r_vis = int(dlq_r_attrs.get("ApproximateNumberOfMessages", 0))
            dlq_r_inf = int(dlq_r_attrs.get("ApproximateNumberOfMessagesNotVisible", 0))
            dlq_r_del = int(dlq_r_attrs.get("ApproximateNumberOfMessagesDelayed", 0))
            dlq_r_ok = (dlq_r_vis == 0 and dlq_r_inf == 0 and dlq_r_del == 0)
        except Exception as _dlq_exc:  # noqa: BLE001
            _dlq_r_err = f"DLQ read error: {type(_dlq_exc).__name__}: {_dlq_exc}"

        alarm_r_ok = False
        alarm_r_detail = ""

        # W7: when DLQ was sent but no ALARM StateUpdate was seen during the run
        # (alarm_seen_at_s not set), poll history until send_at + 25 min before
        # checking whether the alarm has returned to OK.
        # S12 fail-closed: if dlq_send_at_s is missing or unparseable, wait 25 min
        # from now (the DLQ message could have been sent moments ago).
        _w7_dlq_send_at: "dt.datetime | None" = None
        if dlq_sent and not alarm_seen_at_s and dlq_send_at_s:
            try:
                _w7_dlq_send_at = dt.datetime.fromisoformat(
                    dlq_send_at_s.replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                pass
        if dlq_sent and not alarm_seen_at_s and _w7_dlq_send_at is None:
            # dlq_send_at_s missing or unparseable: conservatively wait 25 min from now
            _w7_dlq_send_at = dt.datetime.now(dt.timezone.utc)

        if _w7_dlq_send_at is not None:
            _w7_poll_deadline = _w7_dlq_send_at + dt.timedelta(
                seconds=_ITEM9_ALARM_POLL_MAX_S
            )
            _w7_start_date = _w7_dlq_send_at - dt.timedelta(seconds=60)
            _safe_print(
                f"  [INFO] DLQ sent but ALARM not recorded; waiting up to "
                f"{_ITEM9_ALARM_POLL_MAX_S // 60} min for alarm history...",
                flush=True,
            )
            _w7_alarm_found = False
            _w7_reads = 0
            _w7_max = _ITEM9_ALARM_POLL_MAX_S // _ITEM9_ALARM_POLL_INTERVAL_S + 2
            while not _w7_alarm_found:
                _w7_reads += 1
                _w7_hist = cw_c.describe_alarm_history(
                    AlarmName=alarm_name_c,
                    HistoryItemType="StateUpdate",
                    StartDate=_w7_start_date,
                ).get("AlarmHistoryItems", [])
                for _w7_item in _w7_hist:
                    try:
                        _w7_data = json.loads(_w7_item.get("HistoryData", "{}"))
                    except ValueError:
                        _w7_data = {}
                    if _w7_data.get("newState", {}).get("stateValue") == "ALARM":
                        _w7_alarm_found = True
                        break
                if _w7_alarm_found:
                    break
                _now_utc = dt.datetime.now(dt.timezone.utc)
                if _now_utc >= _w7_poll_deadline or _w7_reads >= _w7_max:
                    break
                time.sleep(_ITEM9_ALARM_POLL_INTERVAL_S)

            if not _w7_alarm_found:
                # No ALARM in 25 min: verify alarm is still OK (S14 fail-closed).
                _s14_alarms = cw_c.describe_alarms(
                    AlarmNames=[alarm_name_c]
                ).get("MetricAlarms", [])
                if _s14_alarms and _s14_alarms[0].get("StateValue") == "OK":
                    alarm_r_ok = True
                    alarm_r_detail = "alarm never fired within 25 min of DLQ send; alarm OK"
                # If alarm is not OK, fall through to the OK poll below.

        # Poll for alarm to return to OK within 15 min (skip when W7 already
        # determined no ALARM was fired; otherwise always run).
        if not alarm_r_ok:
            _safe_print(
                f"  [INFO] waiting for alarm {alarm_name_c!r} to return to OK...",
                flush=True,
            )
            r_dl = time.monotonic() + _ITEM9_RESIDUE_ALARM_MAX_S
            r_reads = 0
            r_max = _ITEM9_RESIDUE_ALARM_MAX_S // _ITEM9_RESIDUE_POLL_INTERVAL_S + 2
            while True:
                r_reads += 1
                cw_c.describe_alarm_history(AlarmName=alarm_name_c, MaxRecords=1)
                r_alarms = cw_c.describe_alarms(
                    AlarmNames=[alarm_name_c]
                ).get("MetricAlarms", [])
                if r_alarms and r_alarms[0].get("StateValue") == "OK":
                    alarm_r_ok = True
                    alarm_r_detail = "alarm OK"
                    break
                if time.monotonic() >= r_dl or r_reads >= r_max:
                    break
                time.sleep(_ITEM9_RESIDUE_POLL_INTERVAL_S)

        # W9 / S9: pattern_id scan in its own try AFTER alarm checks (paginated).
        # Runs even when dynamodb:Scan is denied on the first try; a failed scan
        # is fail-closed: row_absent_c is set to False so the residue FAILs rather
        # than silently passing without having checked the fourth contract condition.
        # Must come after the alarm waits so W7 / OK polls always run.
        _scan_err: "str | None" = None
        if pattern_id and row_absent_c:
            try:
                _scan_kw: dict[str, Any] = {
                    "FilterExpression": "pattern_id = :p",
                    "ExpressionAttributeValues": {":p": pattern_id},
                }
                while True:
                    _scan_resp = inv_tbl_c.scan(**_scan_kw)
                    if _scan_resp.get("Items"):
                        row_absent_c = False
                        break
                    _last_key = _scan_resp.get("LastEvaluatedKey")
                    if not _last_key:
                        break
                    _scan_kw["ExclusiveStartKey"] = _last_key
            except Exception as _scan_exc:  # noqa: BLE001
                row_absent_c = False
                _scan_err = f"pattern scan failed: {type(_scan_exc).__name__}"

        if row_absent_c and dlq_r_ok and alarm_r_ok:
            detail_str = alarm_r_detail or "alarm OK"
            rows.append(("item9-residue", "PASS",
                         f"row absent, DLQ 0/0/0, {detail_str}"))
        else:
            res_parts: "list[str]" = []
            if not row_absent_c:
                if _scan_err:
                    res_parts.append(_scan_err)
                else:
                    res_parts.append(f"row present (inv_id={inv_id})")
            if not dlq_r_ok:
                if _dlq_r_err:
                    res_parts.append(_dlq_r_err)
                else:
                    res_parts.append(
                        f"DLQ {dlq_r_vis}/{dlq_r_inf}/{dlq_r_del}"
                    )
            if not alarm_r_ok:
                res_parts.append(
                    f"alarm {alarm_name_c!r} did not return to OK "
                    f"within {_ITEM9_RESIDUE_ALARM_MAX_S}s"
                )
            rows.append(("item9-residue", "FAIL", "; ".join(res_parts)))
    except Exception as exc:  # noqa: BLE001
        rows.append(("item9-residue", "FAIL", f"{type(exc).__name__}: {exc}"))


def _run_item9(
    res: Results,
    *,
    stage: str,
    run_id: str,
    manifest: dict,
    manifest_dir: "str | None",
) -> None:
    """Run ``--part item9``: test the DLQ→alarm→email chain for investigations.

    One run takes ~20-45 min and sends one alarm email to the operator address.

    Write-window guard structure (three guards before PutItem so that
    ``test_g7_window_closes_before_putitem`` fires at ``_utc_now()`` call 4):

      Guard 1 (_utc_now() call 1): before any pre-checks.
      Pre-check 1 (DLQ) + Pre-check 2 (alarm) run.
      Guard 2 (_utc_now() call 2): before subscription pre-check.
      Pre-check 3 (subscription) runs.
      Guard 3 (_utc_now() call 3): before link 2.
      Link 2 runs.
      Guard 4 / PutItem guard (_utc_now() call 4): before link 1 writes.
    """
    import boto3  # noqa: PLC0415
    from botocore.config import Config  # noqa: PLC0415
    import uuid as _uuid  # noqa: PLC0415

    run_suffix = run_id.split("-")[-1]

    scheduler = boto3.client("scheduler", region_name=_AVX_REGION)
    sqs = boto3.client("sqs", region_name=_AVX_REGION)
    cw = boto3.client("cloudwatch", region_name=_AVX_REGION)
    sns_c = boto3.client("sns", region_name=_AVX_REGION)
    ddb = boto3.resource("dynamodb", region_name=_AVX_REGION)

    tier2_fn = _TIER2_LAMBDA.format(stage=stage)
    inv_queue_nm = _INV_QUEUE.format(stage=stage)
    dlq_name = _INV_DLQ.format(stage=stage)
    alarm_name = _INV_DLQ_ALARM.format(stage=stage, region=_AVX_REGION)

    inv_queue_url = sqs.get_queue_url(QueueName=inv_queue_nm)["QueueUrl"]
    dlq_url = sqs.get_queue_url(QueueName=dlq_name)["QueueUrl"]

    inv_tbl = ddb.Table(_INVESTIGATIONS_TABLE.format(stage=stage))
    runs_tbl = ddb.Table(f"vsa-{stage}-avx-agent-runs")

    # ── Guard 1: initial window check (before any pre-check AWS calls) ──────
    # _utc_now() call 1; if window closed here, no pre-checks run and writes == [].
    if not _item9_check_window(scheduler, stage, "item9-pre-window", res):
        return

    # ── Pre-check 1: DLQ 0/0/0 (GetQueueAttributes also activates DLQ metrics) ─
    dlq_pre = sqs.get_queue_attributes(
        QueueUrl=dlq_url,
        AttributeNames=["ApproximateNumberOfMessages",
                       "ApproximateNumberOfMessagesNotVisible",
                       "ApproximateNumberOfMessagesDelayed"],
    )["Attributes"]
    pc1_vis = int(dlq_pre.get("ApproximateNumberOfMessages", 0))
    pc1_inf = int(dlq_pre.get("ApproximateNumberOfMessagesNotVisible", 0))
    pc1_del = int(dlq_pre.get("ApproximateNumberOfMessagesDelayed", 0))
    if pc1_vis != 0 or pc1_inf != 0 or pc1_del != 0:
        res.skip("item9-pre-dlq-empty",
                 f"DLQ not empty: vis={pc1_vis} inf={pc1_inf} delayed={pc1_del}")
        return
    check(res, "item9-pre-dlq-empty", True,
          f"DLQ 0/0/0 (metric collection activated by read)")

    # ── Pre-check 1b: investigations queue idle (0 visible, 0 in-flight) ──────
    # A concurrent investigation processed from the queue in the same window would
    # produce a second run record and make item9-link1-run-record ambiguous (W4).
    inv_q_idle = sqs.get_queue_attributes(
        QueueUrl=inv_queue_url,
        AttributeNames=["ApproximateNumberOfMessages",
                       "ApproximateNumberOfMessagesNotVisible"],
    )["Attributes"]
    pc_iq_vis = int(inv_q_idle.get("ApproximateNumberOfMessages", 0))
    pc_iq_inf = int(inv_q_idle.get("ApproximateNumberOfMessagesNotVisible", 0))
    if pc_iq_vis != 0 or pc_iq_inf != 0:
        res.skip("item9-pre-inv-queue-idle",
                 f"investigations queue not idle: vis={pc_iq_vis} inf={pc_iq_inf}")
        return
    check(res, "item9-pre-inv-queue-idle", True,
          f"investigations queue 0 vis / 0 in-flight")

    # ── Pre-check 2: alarm OK, ActionsEnabled, AlarmActions contains operator topic ──
    # Build the expected operator topic ARN from account identity (W9: no CreateTopic
    # write in pre-checks; existence confirmed later via ListSubscriptionsByTopic).
    operator_topic_nm = f"vsa-{stage}-tier2-operator-alerts"
    _sts_c = boto3.client("sts", region_name=_AVX_REGION)
    _account_id = _sts_c.get_caller_identity()["Account"]
    operator_topic_arn = (
        f"arn:aws:sns:{_AVX_REGION}:{_account_id}:{operator_topic_nm}"
    )

    alarm_pre_list = cw.describe_alarms(AlarmNames=[alarm_name]).get("MetricAlarms", [])
    if not alarm_pre_list:
        res.skip("item9-pre-alarm-ok", f"alarm {alarm_name!r} not found")
        return
    alarm_pre = alarm_pre_list[0]
    pc2_state = alarm_pre.get("StateValue", "")
    pc2_enabled = alarm_pre.get("ActionsEnabled", False)
    pc2_actions = alarm_pre.get("AlarmActions", [])
    # topic_arn is the resolved operator topic; must be present in AlarmActions.
    topic_arn = operator_topic_arn

    if pc2_state != "OK":
        res.skip("item9-pre-alarm-ok",
                 f"alarm state={pc2_state!r}; must be OK to proceed")
        return
    if not pc2_enabled:
        res.skip("item9-pre-alarm-ok",
                 f"ActionsEnabled={pc2_enabled!r}")
        return
    if operator_topic_arn not in pc2_actions:
        res.skip("item9-pre-alarm-ok",
                 f"AlarmActions does not contain operator topic "
                 f"{operator_topic_nm!r}; got {pc2_actions!r}")
        return
    check(res, "item9-pre-alarm-ok", True,
          f"state=OK ActionsEnabled topic={topic_arn[-30:]!r}")

    # ── Guard 2: window check before subscription pre-check ──────────────────
    # _utc_now() call 2; for test_g7_write_window_shut this guard fires as SKIP
    # before ListSubscriptionsByTopic, keeping writes==[] for that test.
    if not _item9_check_window(scheduler, stage, "item9-pre-window-2", res):
        return

    # ── Pre-check 3: topic has >= 1 confirmed subscription ──────────────────
    # ListSubscriptionsByTopic also confirms the topic exists (NotFound → SKIP; W9).
    try:
        subs_list = sns_c.list_subscriptions_by_topic(
            TopicArn=topic_arn
        ).get("Subscriptions", [])
    except Exception as _sub_exc:  # noqa: BLE001
        from botocore.exceptions import ClientError as _ClientError  # noqa: PLC0415
        if isinstance(_sub_exc, _ClientError):
            _ec = _sub_exc.response.get("Error", {}).get("Code", "")
            if _ec in ("NotFound", "AWS.SimpleNotificationService.NoSuchTopic"):
                res.skip("item9-pre-subscription",
                         f"operator topic {operator_topic_nm!r} not found ({_ec})")
                return
        raise
    confirmed_subs = [
        s for s in subs_list
        if not s.get("SubscriptionArn", "").startswith("PendingConfirmation")
    ]
    if not confirmed_subs:
        res.skip("item9-pre-subscription",
                 f"no confirmed subscriptions on topic {topic_arn!r} "
                 f"(total={len(subs_list)}, pending={len(subs_list) - len(confirmed_subs)})")
        return
    check(res, "item9-pre-subscription", True,
          f"{len(confirmed_subs)} confirmed subscription(s) on operator topic")

    # ── Guard 3: window check before link 2 ─────────────────────────────────
    # _utc_now() call 3.
    if not _item9_check_window(scheduler, stage, "item9-link2-window", res):
        return

    # ── Link 2a: queue redrive policy ────────────────────────────────────────
    lam_r = boto3.client("lambda", region_name=_AVX_REGION)
    fn_cfg = lam_r.get_function_configuration(FunctionName=tier2_fn)
    sqs_max_env = fn_cfg.get("Environment", {}).get("Variables", {}).get(
        "SQS_MAX_RECEIVE_COUNT", ""
    )

    inv_q_attrs = sqs.get_queue_attributes(
        QueueUrl=inv_queue_url,
        AttributeNames=["RedrivePolicy"],
    )["Attributes"]
    redrive_raw = inv_q_attrs.get("RedrivePolicy", "{}")
    try:
        redrive = json.loads(redrive_raw) if redrive_raw else {}
    except ValueError:
        redrive = {}

    dlq_arn_str = sqs.get_queue_attributes(
        QueueUrl=dlq_url, AttributeNames=["QueueArn"]
    )["Attributes"].get("QueueArn", "")

    rd_dlq_ok = redrive.get("deadLetterTargetArn") == dlq_arn_str
    # S5: require a positive integer equal to env value (str comparison passes when both
    # are missing — "None" == "" is False but "" == "" is True); int comparison is clean.
    mrc_val = redrive.get("maxReceiveCount")
    try:
        sqs_max_int = int(sqs_max_env)
        rd_max_ok = isinstance(mrc_val, int) and mrc_val > 0 and mrc_val == sqs_max_int
    except (ValueError, TypeError):
        rd_max_ok = False
    if not check(res, "item9-link2-redrive",
                 rd_dlq_ok and rd_max_ok,
                 f"deadLetterTargetArn={dlq_arn_str[-24:]!r} "
                 f"maxReceiveCount={sqs_max_env}",
                 on_fail=f"dlq_match={rd_dlq_ok} max_match={rd_max_ok} "
                         f"(env={sqs_max_env!r} "
                         f"policy.maxReceiveCount={mrc_val!r})"):
        return  # FAIL before any write → rc=1

    # ── Link 2b: ESM — enabled, tier2 function, ReportBatchItemFailures ──────
    # Derive queue ARN from the DLQ ARN (avoids extra GetQueueAttributes call)
    inv_queue_arn = (
        dlq_arn_str.replace(f":{dlq_name}", f":{inv_queue_nm}")
        if dlq_arn_str else ""
    )
    all_esms = _queue_esms(lam_r, inv_queue_arn) if inv_queue_arn else []

    non_enabled_esms = [m for m in all_esms if m.get("State") != "Enabled"]

    def _has_rbif(m: dict) -> bool:
        return "ReportBatchItemFailures" in m.get("FunctionResponseTypes", [])

    def _fn_matches(m: dict) -> bool:
        """Exact match: FunctionArn ends with :function:<tier2_fn> or equals tier2_fn."""
        fn_arn = m.get("FunctionArn", "")
        return fn_arn == tier2_fn or fn_arn.endswith(f":function:{tier2_fn}")

    valid_esm = any(
        m.get("State") == "Enabled"
        and _has_rbif(m)
        and _fn_matches(m)
        for m in all_esms
    )

    if not check(res, "item9-link2-esm",
                 valid_esm and not non_enabled_esms,
                 f"Enabled ESM with ReportBatchItemFailures → {tier2_fn[-32:]!r}",
                 on_fail=(
                     f"non-Enabled mapping(s): "
                     f"{[m.get('State') for m in non_enabled_esms]}"
                     if non_enabled_esms
                     else f"no Enabled ESM with RBIF to {tier2_fn!r}"
                 )):
        return  # FAIL before any write → rc=1

    # ── Guard 4 / PutItem guard ───────────────────────────────────────────────
    # _utc_now() call 4; test_g7_window_closes_before_putitem fires here.
    now_l1 = _utc_now()
    l1_window = _after6_write_window_guard(scheduler, stage, now_l1)
    if l1_window:
        res.skip("item9-link1-window", f"write window: {l1_window}")
        return

    # ── Link 1: PutItem ───────────────────────────────────────────────────────
    investigation_id = str(_uuid.uuid4())
    pattern_id = f"LIVETEST-9-{run_suffix}"
    message_id = f"livetest-9-{run_suffix}"
    requested_at = _utc_now().isoformat().replace("+00:00", "Z")

    manifest["item9_investigation_id"] = investigation_id
    manifest["item9_pattern_id"] = pattern_id
    manifest["item9_row_put_sent"] = True
    _save_manifest(run_id, manifest, manifest_dir)

    try:
        inv_tbl.put_item(
            Item={
                "investigation_id": investigation_id,
                "pattern_id": pattern_id,
                "status": "queued",
                "requested_at": requested_at,
                "requested_by": f"LIVETEST-{run_id}",
                "livetest_run_id": run_id,
            },
            ConditionExpression="attribute_not_exists(investigation_id)",
        )
        check(res, "item9-link1-row-written", True,
              f"investigation_id={investigation_id[:8]}")
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        check(res, "item9-link1-row-written", False,
              on_fail=f"PutItem failed: {type(exc).__name__}: {exc}")
        return

    # Manifest: save before Invoke
    manifest["item9_message_id"] = message_id
    manifest["item9_invoke_sent"] = True
    _save_manifest(run_id, manifest, manifest_dir)

    # Guard before Invoke (write 2)
    now_invoke = _utc_now()
    invoke_window = _after6_write_window_guard(scheduler, stage, now_invoke)
    if invoke_window:
        res.skip("item9-invoke-window", f"write window: {invoke_window}")
        return

    # Synthetic SQS event: body=investigation_id, ApproximateReceiveCount="2"
    sqs_event = {
        "Records": [{
            "messageId": message_id,
            "receiptHandle": f"item9-handle-{run_id}",
            "body": investigation_id,
            "attributes": {
                "ApproximateReceiveCount": "2",
                "SentTimestamp": str(int(time.time() * 1000)),
                "SenderId": "",
                "ApproximateFirstReceiveTimestamp": str(int(time.time() * 1000)),
            },
            "messageAttributes": {},
            "md5OfBody": "",
            "eventSource": "aws:sqs",
            "eventSourceARN": inv_queue_arn,
            "awsRegion": _AVX_REGION,
        }]
    }

    # Lambda client: read_timeout > 900 s, 1 attempt, tcp_keepalive=True
    lam_invoke = boto3.client(
        "lambda", region_name=_AVX_REGION,
        config=Config(
            read_timeout=901,
            connect_timeout=10,
            retries={"total_max_attempts": 1},
            tcp_keepalive=True,
        ),
    )

    invoke_start = _utc_now()
    invoke_end = invoke_start
    manifest["item9_invoke_started_at"] = invoke_start.isoformat()
    _save_manifest(run_id, manifest, manifest_dir)
    try:
        invoke_resp = lam_invoke.invoke(
            FunctionName=tier2_fn,
            InvocationType="RequestResponse",
            Payload=json.dumps(sqs_event).encode(),
        )
        invoke_end = _utc_now()
        manifest["item9_invoke_answered"] = True
        _save_manifest(run_id, manifest, manifest_dir)
        raw_payload = invoke_resp["Payload"].read()

        if invoke_resp.get("FunctionError"):
            check(res, "item9-link1-invoke", False,
                  on_fail=f"FunctionError={invoke_resp['FunctionError']!r} "
                          f"payload={raw_payload[:200]!r}")
        else:
            try:
                payload = json.loads(raw_payload or b"null")
            except ValueError:
                payload = None
            expected_payload = {"batchItemFailures": [{"itemIdentifier": message_id}]}
            payload_ok = payload == expected_payload
            status_ok = invoke_resp.get("StatusCode") == 200
            check(res, "item9-link1-invoke", status_ok and payload_ok,
                  f"StatusCode={invoke_resp.get('StatusCode')} "
                  f"batchItemFailures=[{message_id}]",
                  on_fail=(
                      f"StatusCode={invoke_resp.get('StatusCode')!r} (expected 200)"
                      if not status_ok
                      else f"expected {expected_payload!r}; got {payload!r}"
                  ))
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        invoke_end = _utc_now()
        check(res, "item9-link1-invoke", False,
              on_fail=f"invoke failed: {type(exc).__name__}: {exc}")

    # Check investigation row status (ConsistentRead)
    try:
        row_after = inv_tbl.get_item(
            Key={"investigation_id": investigation_id}, ConsistentRead=True
        ).get("Item", {})
        row_status = (row_after.get("status", "absent") if row_after else "absent")
        check(res, "item9-link1-row-failed", row_status == "failed",
              f"status={row_status}",
              on_fail=f"expected status=failed; got {row_status!r}")
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        check(res, "item9-link1-row-failed", False,
              on_fail=f"GetItem failed: {type(exc).__name__}: {exc}")

    # Check run record keyed agent-fi-quality#investigation
    # Bounds: [invoke_start − 10 s, invoke_end + 10 s] — no Z suffix (contract)
    i_start = (
        invoke_start - dt.timedelta(seconds=10)
    ).replace(tzinfo=None).isoformat(timespec="milliseconds")
    i_end = (
        invoke_end + dt.timedelta(seconds=10)
    ).replace(tzinfo=None).isoformat(timespec="milliseconds")
    try:
        rr_resp = runs_tbl.query(
            KeyConditionExpression=(
                "agent_component = :pk AND started_at BETWEEN :s AND :e"
            ),
            ExpressionAttributeValues={
                ":pk": "agent-fi-quality#investigation",
                ":s": i_start,
                ":e": i_end,
            },
        )
        rr_items = rr_resp.get("Items", [])
        # No fallback: require exactly one record in the window (W4).
        if len(rr_items) == 0:
            check(res, "item9-link1-run-record", False,
                  on_fail=f"no run record for agent-fi-quality#investigation "
                          f"between {i_start} and {i_end}")
        elif len(rr_items) > 1:
            started_ats = [r.get("started_at") for r in rr_items]
            check(res, "item9-link1-run-record", False,
                  on_fail=f"ambiguous: {len(rr_items)} run records in window "
                          f"({started_ats!r}); ensure investigations queue is idle")
        else:
            rr = rr_items[0]
            rr_status = rr.get("status", "")
            check(res, "item9-link1-run-record",
                  rr_status != "completed",
                  f"agent-fi-quality#investigation "
                  f"started_at={rr.get('started_at')} status={rr_status}",
                  on_fail=f"status=completed (expected aborted/failed/running)")
            manifest["item9_run_record_started_at"] = rr.get("started_at", "")
            _save_manifest(run_id, manifest, manifest_dir)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        check(res, "item9-link1-run-record", False,
              on_fail=f"{type(exc).__name__}: {exc}")

    # S13: skip link 3 entirely when any link 1 check FAILed — the operator
    # gets one alarm email per run; a failed link 1 means the run must be
    # repeated, and sending link 3's email would generate a second alarm on
    # the next repeat.  Cleanup still runs (in main()'s finally block).
    if res.has_any_fail:
        return

    # ── Link 3: send to DLQ, poll alarm ─────────────────────────────────────

    # Re-read DLQ 0/0/0 and alarm OK before sending (FAIL if either is not clean)
    dlq_l3 = sqs.get_queue_attributes(
        QueueUrl=dlq_url,
        AttributeNames=["ApproximateNumberOfMessages",
                       "ApproximateNumberOfMessagesNotVisible",
                       "ApproximateNumberOfMessagesDelayed"],
    )["Attributes"]
    l3_vis = int(dlq_l3.get("ApproximateNumberOfMessages", 0))
    l3_inf = int(dlq_l3.get("ApproximateNumberOfMessagesNotVisible", 0))
    l3_del = int(dlq_l3.get("ApproximateNumberOfMessagesDelayed", 0))
    if l3_vis != 0 or l3_inf != 0 or l3_del != 0:
        check(res, "item9-link3-sent", False,
              on_fail=f"DLQ not 0/0/0 before send: "
                      f"vis={l3_vis} inf={l3_inf} delayed={l3_del}")
        return

    l3_alarms = cw.describe_alarms(AlarmNames=[alarm_name]).get("MetricAlarms", [])
    l3_alarm_state = l3_alarms[0].get("StateValue", "") if l3_alarms else ""
    if l3_alarm_state != "OK":
        check(res, "item9-link3-sent", False,
              on_fail=f"alarm not OK before send: state={l3_alarm_state!r}")
        return

    # Guard before SendMessage (write 3)
    now_send = _utc_now()
    send_window = _after6_write_window_guard(scheduler, stage, now_send)
    if send_window:
        res.skip("item9-send-window", f"write window: {send_window}")
        return

    # Manifest: save before SendMessage
    dlq_body = f"LIVETEST-9-{run_id}"
    send_at = _utc_now()
    manifest["item9_dlq_body"] = dlq_body
    manifest["item9_dlq_send_at"] = send_at.isoformat()
    manifest["item9_dlq_sent"] = True
    _save_manifest(run_id, manifest, manifest_dir)

    # SendMessage to DLQ (write 3)
    try:
        send_resp = sqs.send_message(QueueUrl=dlq_url, MessageBody=dlq_body)
        dlq_msg_id = send_resp.get("MessageId", "")
        manifest["item9_dlq_message_id"] = dlq_msg_id
        _save_manifest(run_id, manifest, manifest_dir)
        check(res, "item9-link3-sent", True, f"MessageId={dlq_msg_id}")
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        check(res, "item9-link3-sent", False,
              on_fail=f"SendMessage failed: {type(exc).__name__}: {exc}")
        return

    _safe_print(
        f"  [INFO] waiting up to {_ITEM9_ALARM_POLL_MAX_S // 60} min for "
        f"alarm {alarm_name!r} to fire...",
        flush=True,
    )

    # Poll alarm history for a StateUpdate to ALARM at or after send_at - 60 s.
    # The 60-second back-margin catches a rare CloudWatch quirk (contract §link3).
    # The fake's add_before_run_state_update() plants its fixture item more than
    # 60 s before send_at so the test still confirms filtering works.
    start_date = send_at - dt.timedelta(seconds=60)
    poll_dl = time.monotonic() + _ITEM9_ALARM_POLL_MAX_S
    poll_reads = 0
    poll_max = _ITEM9_ALARM_POLL_MAX_S // _ITEM9_ALARM_POLL_INTERVAL_S + 2
    state_update_ts: "dt.datetime | None" = None

    while True:
        poll_reads += 1
        hist = cw.describe_alarm_history(
            AlarmName=alarm_name,
            HistoryItemType="StateUpdate",
            StartDate=start_date,
        ).get("AlarmHistoryItems", [])

        for item in hist:
            ts = item.get("Timestamp")
            if ts is None:
                continue
            try:
                hist_data = json.loads(item.get("HistoryData", "{}"))
            except ValueError:
                hist_data = {}
            if hist_data.get("newState", {}).get("stateValue") == "ALARM":
                state_update_ts = ts
                break

        if state_update_ts is not None:
            break
        if time.monotonic() >= poll_dl or poll_reads >= poll_max:
            break
        time.sleep(_ITEM9_ALARM_POLL_INTERVAL_S)

    # Save the ALARM StateUpdate timestamp to the manifest (W1 / item9_alarm_seen_at).
    if state_update_ts is not None:
        manifest["item9_alarm_seen_at"] = (
            state_update_ts.isoformat()
            if isinstance(state_update_ts, dt.datetime)
            else str(state_update_ts)
        )
        _save_manifest(run_id, manifest, manifest_dir)

    check(res, "item9-link3-alarm-state",
          state_update_ts is not None,
          f"StateUpdate to ALARM at {state_update_ts!r}",
          on_fail=f"no StateUpdate to ALARM within {_ITEM9_ALARM_POLL_MAX_S}s "
                  f"after send_at={send_at.isoformat()}")

    # Poll for Action item with actionState=Succeeded naming the operator topic.
    # CloudWatch can trail the StateUpdate by a moment; poll for up to 5 min
    # after the StateUpdate is first seen rather than reading history once (W2).
    if state_update_ts is not None:
        _safe_print(
            "  [INFO] polling for Action item (up to 5 min after StateUpdate)...",
            flush=True,
        )
        act_poll_dl = time.monotonic() + _ITEM9_ACTION_POLL_MAX_S
        act_poll_max = _ITEM9_ACTION_POLL_MAX_S // _ITEM9_ALARM_POLL_INTERVAL_S + 2
        act_poll_reads = 0
        action_found = False

        while True:
            act_poll_reads += 1
            action_hist = cw.describe_alarm_history(
                AlarmName=alarm_name,
                HistoryItemType="Action",
                StartDate=start_date,
            ).get("AlarmHistoryItems", [])

            for a_item in action_hist:
                a_ts = a_item.get("Timestamp")
                if a_ts is None:
                    continue
                if (isinstance(a_ts, dt.datetime)
                        and isinstance(state_update_ts, dt.datetime)
                        and a_ts < state_update_ts):
                    continue
                try:
                    a_data = json.loads(a_item.get("HistoryData", "{}"))
                except ValueError:
                    a_data = {}
                if (a_data.get("actionState") == "Succeeded"
                        and topic_arn == a_data.get("notificationResource", "")):
                    action_found = True
                    break

            if action_found:
                break
            if time.monotonic() >= act_poll_dl or act_poll_reads >= act_poll_max:
                break
            time.sleep(_ITEM9_ALARM_POLL_INTERVAL_S)

        check(res, "item9-link3-action",
              action_found,
              f"Action Succeeded for topic {topic_arn[-30:]!r}",
              on_fail=f"no Action with actionState=Succeeded naming {topic_arn!r} "
                      f"within 5 min of StateUpdate")
    else:
        check(res, "item9-link3-action", False,
              on_fail="not reached: alarm-state check failed")


# ---------------------------------------------------------------------------
# Item runner
# ---------------------------------------------------------------------------

_ITEM_FNS: dict[str, Any] = {
    "1": _item_1,
    "6": _item_6,
    "7": _item_7,
    "8a": _item_8a,
    "2": _item_2,
    "3": _item_3,
    "4": _item_4,
    "5": _item_5,
    "8b": _item_8b,
}


def _run_items(
    items: list[str],
    res: Results,
    *,
    allow_writes: bool,
    stage: str,
    answer_key: str | None,
    run_id: str = "",
    manifest: dict | None = None,
    manifest_dir: str | None = None,
) -> None:
    """Run each item in its own try/except; write items SKIP without --allow-writes."""
    kwargs = dict(
        allow_writes=allow_writes,
        stage=stage,
        answer_key=answer_key,
        run_id=run_id,
        manifest=manifest,
        manifest_dir=manifest_dir,
    )
    for n in items:
        fn = _ITEM_FNS[n]
        if n in _WRITE_ITEMS and not allow_writes:
            res.skip(f"item-{n}", "writes to staging; needs --allow-writes")
            continue
        try:
            fn(res, **kwargs)
        except Exception as exc:  # noqa: BLE001
            check(res, f"item-{n}-exception", False, on_fail=f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# CLI / main
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Live verify FI quality (task 5.3 / 6.4 / 6.5)")
    p.add_argument("--stage", required=True)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--part", choices=[_PART_NOW, _PART_AFTER6, _PART_AFTER6_CHECK, _PART_ITEM9],
                   help="run a named part (now, after-6, after-6-check, or item9)")
    g.add_argument("--items", metavar="N,...")
    g.add_argument("--cleanup-only", action="store_true",
                   help="Undo writes recorded in the manifest for --run-id.")
    p.add_argument("--allow-writes", action="store_true")
    p.add_argument("--answer-key")
    p.add_argument("--run-id", dest="run_id", default=None,
                   help="Run id from a prior --part after-6 or --part item9 run "
                        "(required for --cleanup-only).")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # Guard: LIVE_TESTS=1 required (brief §Shape; conftest guard on the repo's test suite)
    if os.environ.get("LIVE_TESTS") != "1":
        print("LIVE_TESTS=1 is required", file=sys.stderr)
        return 2

    args = _parse_args(argv)

    # Stage refusal BEFORE any boto3 client (brief §Shape)
    if args.stage != _ALLOWED_STAGE:
        print(f"refused: --stage must be {_ALLOWED_STAGE!r} (got {args.stage!r})", file=sys.stderr)
        return 1

    # Writes outside the conftest AWS block run as the caller with real credentials (a helper's debug
    # call did that on 2026-10-29). Refuse before any client unless the live-run step gave its go.
    _refusal = _lwg.check_write_flags(
        stage=args.stage, go_env=_LIVE_GO_ENV, flags={"--allow-writes": args.allow_writes}
    )
    if _refusal:
        print(_refusal, file=sys.stderr)
        return 1

    # Answer key validations (brief §Shape)
    answer_key = args.answer_key
    if answer_key:
        if answer_key.startswith("s3://"):
            print("refused: --answer-key must be a local path, not s3://", file=sys.stderr)
            return 1
        if not Path(answer_key).exists():
            print(f"refused: --answer-key file not found: {answer_key}", file=sys.stderr)
            return 1

    # Run-id shape check (required for after-6-check and cleanup-only)
    run_id: str | None = args.run_id
    cleanup_only: bool = getattr(args, "cleanup_only", False)
    manifest_dir: str | None = os.environ.get(_MANIFEST_DIR_ENV)  # tests override via env

    if cleanup_only and not run_id:
        print(
            "refused: --run-id is required for --cleanup-only",
            file=sys.stderr,
        )
        return 1

    if run_id and not _validate_run_id(run_id):
        # item9 and cleanup-only accept an alphanumeric suffix (test IDs like "i9test001")
        _item9_relaxed = (
            getattr(args, "part", None) == _PART_ITEM9
            or cleanup_only
        )
        if not _item9_relaxed or not re.fullmatch(
            r"^fi-quality-\d{8}T\d{6}-[A-Za-z0-9]{1,16}$", run_id
        ):
            print(
                f"refused: --run-id {run_id!r} does not match the expected shape "
                f"(fi-quality-YYYYMMDDTHHMMSS-8hex)",
                file=sys.stderr,
            )
            return 1

    # --cleanup-only: load manifest and run cleanup, then exit
    if cleanup_only:
        if not args.allow_writes:
            print("refused: --cleanup-only requires --allow-writes", file=sys.stderr)
            return 1
        manifest = _load_manifest(run_id, manifest_dir)  # type: ignore[arg-type]
        if manifest is None:
            print(
                f"refused: no manifest found for run_id={run_id!r} "
                f"(manifest_dir={manifest_dir or _manifest_base_dir()!r})",
                file=sys.stderr,
            )
            return 1
        # S5: shape-check the manifest's run_id, stage, and table/bucket names.
        # Refuse with no AWS call if anything mismatches (prevents wrong-run cleanup).
        _mismatch = _validate_cleanup_manifest(manifest, run_id, args.stage)  # type: ignore[arg-type]
        if _mismatch:
            print(f"refused: --cleanup-only manifest mismatch: {_mismatch}", file=sys.stderr)
            return 1
        _install_signal_handlers()
        res = Results()
        run_id_for_cleanup = manifest.get("run_id", run_id)
        stage = manifest.get("stage", args.stage)

        # Dispatch to item9 cleanup if the manifest was written by --part item9
        if manifest.get("part") == _PART_ITEM9:
            _co_rows: list[tuple[str, str, str]] = []
            try:
                with _defer_signals_for_restore():
                    _item9_cleanup(
                        _co_rows,
                        manifest=manifest,
                        manifest_dir=manifest_dir,
                        stage=stage,
                    )
            finally:
                for _lbl, _st, _dt in _co_rows:
                    _safe_print(
                        f"  [{_st}] {_lbl}{(': ' + _dt) if _dt else ''}",
                        flush=True,
                    )
                    res.rows.append((_lbl, _st, _dt))
                res.summary(run_id_for_cleanup or "cleanup-only")
            return 1 if res.has_any_fail else 0

        try:
            import boto3  # noqa: PLC0415
            idp = boto3.client("cognito-idp", region_name=_AVX_REGION)
            try:
                pool_id = _resolve_pool_id(idp)
            except Exception:  # noqa: BLE001
                pool_id = None
            with _defer_signals_for_restore():
                _after6_cleanup(
                    res,
                    manifest=manifest,
                    manifest_dir=manifest_dir,
                    stage=stage,
                    idp=idp,
                    pool_id=pool_id,
                    mode="cleanup-only",
                )
                _reap_stale_users_step(res, idp, pool_id)
        finally:
            res.summary(run_id_for_cleanup or "cleanup-only")
        return 1 if res.has_any_fail else 0

    # --part after-6-check: item 9 uses --part item9 (task 6.5); refuse with guidance.
    if args.part == _PART_AFTER6_CHECK:
        print(
            "item 9 requires --part item9 (task 6.5, ~20-45 min, one alarm email); "
            "--part after-6-check is not the entry point for item 9 (no AWS call here)",
            flush=True,
        )
        return 1

    # --part item9: run the DLQ→alarm→email verification (task 6.5)
    if args.part == _PART_ITEM9:
        if not args.allow_writes:
            print("refused: --part item9 requires --allow-writes", file=sys.stderr)
            return 1
        if run_id is None:
            run_id = (
                f"fi-quality-"
                f"{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S')}"
                f"-{secrets.token_hex(4)}"
            )
        # S7: refuse if a manifest already exists for this run_id to prevent
        # overwriting a prior run's state (a later --cleanup-only would then read
        # "nothing sent" while the earlier row or message still exists).
        if _load_manifest(run_id, manifest_dir) is not None:
            print(
                f"refused: --part item9 --run-id {run_id!r}: "
                f"manifest already exists for this run id "
                f"(use --cleanup-only to resume cleanup)",
                file=sys.stderr,
            )
            return 1
        print(f"run_id: {run_id}  stage: {args.stage}  part: item9")
        _manifest_i9: dict = {
            "run_id": run_id,
            "stage": args.stage,
            "part": "item9",
            "item9_investigation_id": None,
            "item9_pattern_id": None,
            "item9_row_put_sent": False,
            "item9_message_id": None,
            "item9_invoke_sent": False,
            "item9_dlq_body": None,
            "item9_dlq_send_at": None,
            "item9_dlq_sent": False,
            "item9_dlq_message_id": None,
        }
        _save_manifest(run_id, _manifest_i9, manifest_dir)
        print(
            f"  manifest: {_manifest_path(run_id, manifest_dir)}\n"
            f"  to resume cleanup:\n"
            f"    {_LIVE_GO_ENV}={args.stage} LIVE_TESTS=1 python scripts/live-verify-fi-quality.py"
            f" --stage {args.stage}"
            f" --cleanup-only --run-id {run_id} --allow-writes",
            flush=True,
        )
        _install_signal_handlers()
        res = Results()
        _i9_interrupted = False
        try:
            _run_item9(
                res,
                stage=args.stage,
                run_id=run_id,
                manifest=_manifest_i9,
                manifest_dir=manifest_dir,
            )
        except SystemExit as exc:
            # Absorb signal exits (e.g. SIGTERM → SystemExit(143)) so cleanup
            # still runs in the finally block; the interrupted flag forces rc=1.
            if isinstance(exc.code, int) and exc.code > 2:
                _i9_interrupted = True
            else:
                raise
        except Exception as exc:  # noqa: BLE001 — S8
            # A non-signal exception (e.g. a fail-closed raise) is not recorded
            # automatically; capture it as an item9-exception FAIL row so the
            # summary prints it and rc=1 is forced even if cleanup runs cleanly.
            check(res, "item9-exception", False,
                  on_fail=f"{type(exc).__name__}: {exc}")
            _i9_interrupted = True
        finally:
            _i9_rows: list[tuple[str, str, str]] = []
            try:
                if _manifest_i9.get("item9_row_put_sent"):
                    try:
                        with _defer_signals_for_restore():
                            _item9_cleanup(
                                _i9_rows,
                                manifest=_manifest_i9,
                                manifest_dir=manifest_dir,
                                stage=args.stage,
                            )
                    finally:
                        # W5: ensure rows are recorded even when the signal hold
                        # re-delivers a deferred signal (SystemExit raised on exit).
                        for _lbl, _st, _dt in _i9_rows:
                            _safe_print(
                                f"  [{_st}] {_lbl}{(': ' + _dt) if _dt else ''}",
                                flush=True,
                            )
                            res.rows.append((_lbl, _st, _dt))
            finally:
                res.summary(run_id)
        if _i9_interrupted or res.has_any_fail:
            return 1
        if res.has_any_skip:
            return 2
        return 0

    # Determine items to run: exactly one of --part / --items (no default).
    if args.part == _PART_NOW:
        items = list(_NOW_ITEMS)
    elif args.part == _PART_AFTER6:
        items = list(_AFTER6_ITEMS)
    elif args.items:
        items = [x.strip() for x in args.items.split(",") if x.strip()]
        unknown = [x for x in items if x not in _ITEM_FNS]
        if unknown or not items:
            print(f"refused: --items must be from {list(_ALL_ITEMS)} (got {args.items!r})",
                  file=sys.stderr)
            return 1
    else:
        print("refused: give --part now|after-6|after-6-check|item9 or --items", file=sys.stderr)
        return 1

    if run_id is None:
        run_id = (
            f"fi-quality-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S')}"
            f"-{secrets.token_hex(4)}"
        )
    print(f"run_id: {run_id}  stage: {args.stage}  items: {items}")

    # For after-6, create and save the manifest before the first item
    manifest: dict | None = None
    if args.part == _PART_AFTER6:
        manifest = _fresh_manifest(run_id, args.stage)
        _save_manifest(run_id, manifest, manifest_dir)
        print(
            f"  manifest: {_manifest_path(run_id, manifest_dir)}"
        )

    _install_signal_handlers()
    res = Results()

    try:
        _run_items(
            items, res,
            allow_writes=args.allow_writes,
            stage=args.stage,
            answer_key=answer_key,
            run_id=run_id,
            manifest=manifest,
            manifest_dir=manifest_dir,
        )
    finally:
        # C1 fix: for after-6, always run ordered cleanup in the finally regardless
        # of exceptions or signals — cleanup was removed from _item_2's finally.
        if args.part == _PART_AFTER6 and manifest is not None and args.allow_writes:
            import boto3 as _b3_main  # noqa: PLC0415
            _idp_main: Any = None
            _pool_id_main: str | None = None
            try:
                with _defer_signals_for_restore():
                    # W1 fix: hold all three signals BEFORE resolving Cognito so a signal
                    # arriving during the network call is deferred, not delivered mid-cleanup.
                    if manifest.get("users"):
                        _idp_main = _b3_main.client("cognito-idp", region_name=_AVX_REGION)
                        try:
                            _pool_id_main = _resolve_pool_id(_idp_main)
                        except Exception:  # noqa: BLE001
                            _pool_id_main = None
                    _after6_cleanup(
                        res,
                        manifest=manifest,
                        manifest_dir=manifest_dir,
                        stage=args.stage,
                        idp=_idp_main,
                        pool_id=_pool_id_main,
                        mode="after-6",
                    )
                    # Security review cycle 1 W1: a run that used temp users also reaps this
                    # script's left-over ones from earlier runs (older than an hour), after its
                    # own users are gone. A run that made none never touches Cognito.
                    if manifest.get("users"):
                        _reap_stale_users_step(res, _idp_main, _pool_id_main)
            finally:
                res.summary(run_id)  # prints even when a held signal is re-raised at hold exit
        else:
            res.summary(run_id)  # also when a signal stops the run (review cycle 2 S8)

    if res.has_any_fail:
        return 1
    if res.has_any_skip:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
