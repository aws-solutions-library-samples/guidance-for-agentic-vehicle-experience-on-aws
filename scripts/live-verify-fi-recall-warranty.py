#!/usr/bin/env python3
"""Live verification for FI Recall-Warranty Stage C, task 5.5.

Spec: .kiro/specs/2026-10-05-fi-recall-warranty-stage-c/tasks.md 5.5 (items 1-11).
Repo: ~/cvx-wt-fi-rw-c-g5

Items (run order: 1, 2, 3, 6, 10, 8, 11, 5, 4, 7, 9):
  1. Views vs answer key          read-only, Athena + S3; anti-vacuity: answer key non-empty
  2. One recall + one warranty run on demand   --allow-writes; async invoke + poll
  3. Re-run, unchanged inputs     --allow-writes; artifacts_written=0, model_calls=0
  4. Booking round trip           --allow-managed-writes; approve -> DMS RO -> remedied
  5. Cancel path                  --allow-managed-writes; 5a managed, 5b LIVETEST
  6. Groundings without CMS route read-only; from item 2 run records
  7. Warranty claim               --allow-managed-writes; approve, DMS claim, adjudication in memory
  8. Two fleets scope isolation   --allow-writes; LIVETEST; 403 vs 400
  9. Forced failure per component --allow-writes --allow-paging; env override -> alarm ALARM
 10. Broker skips FI items        read-only; CloudWatch logs since earliest recall/warranty run
 11. D18 eval: safety text        --allow-writes; LIVETEST recall Finding + /assistant/chat

Flags:
  --stage staging           required; refuses anything else before any AWS call
  --region us-west-2        AVX / DMS / Cognito / Lambda region (default us-west-2)
  --item N                  run item N only (repeatable); mutually exclusive with --all
  --all                     run all 11 items in the design order
  --allow-writes            gates: Lambda invokes, Cognito users, LIVETEST plants, env overrides
  --allow-managed-writes    additionally gates managed-write items 4, 5, 7
  --allow-paging            gates item 9 (pages the operator SNS topic)
  --dms-repo PATH           path to DMS repo
                            (default ~/guidance-for-dealer-management-system-on-aws)

Exit 0: all pass.  Exit 1: any fail or residue.  Exit 2: any skip (non-passing).
Tokens and passwords are never printed or written to the manifest.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import importlib.util
import json
import os
import re
import secrets
import signal as _sig
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterator

# C5: repo root on sys.path so agents.* and evals.* are importable when the
# script is run as a file (sys.path[0] would otherwise be scripts/).
# Mirrors live-verify-fi-cost.py:109-111.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Live-write guard (scripts/lib/live_write_guard.py)
_LIB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)
import live_write_guard as _lwg  # noqa: E402

#: Set to ``--stage`` by the live-run step, on the user's go, and by nothing else.
_LIVE_GO_ENV = "FI_RECALL_WARRANTY_LIVE_GO"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ALLOWED_STAGE = "staging"
_DEFAULT_REGION = "us-west-2"
_ADP_REGION = "us-east-1"

_AGENT_FI_RW = "agent-fi-recall-warranty"
_RECALL_KIND = "fleet.recall.vehicle"
_WARRANTY_KIND = "fleet.warranty.claim_candidate"
_RECALL_COMPONENT = "recall"
_WARRANTY_COMPONENT = "warranty"
_RUN_PK_RECALL = f"{_AGENT_FI_RW}#recall"
_RUN_PK_WARRANTY = f"{_AGENT_FI_RW}#warranty"

# Force failure without writing: clear FI_COST_GUARDRAIL_ID on the health Lambda.
# _run_recall/warranty_from_env checks this env var before building any client or writing
# (agents/tier2/runner.py lines 1840-1845); an empty value raises RuntimeError before
# any DDB write.  Unlike FI_RW_MODEL_ID (a bad model falls back to rules), an empty
# guardrail ID causes an unconditional raise → Lambda error → run-failure alarm fires.
_FI_GUARDRAIL_ENV = "FI_COST_GUARDRAIL_ID"

# Write window to refuse: 04:50–06:10 UTC.
# Scheduled jobs: recall 05:00, warranty 05:20, dealer-progress 05:40, adjudicator 06:00.
_WRITE_WINDOW_START_MIN = 4 * 60 + 50   # minutes since midnight UTC
_WRITE_WINDOW_END_MIN = 6 * 60 + 10     # minutes since midnight UTC

# Run order for --all (design § "Run contract")
_RUN_ORDER = [1, 2, 3, 6, 10, 8, 11, 5, 4, 7, 9]
_ALL_ITEMS = frozenset(_RUN_ORDER)

# Items gated by --allow-writes (invokes, Cognito users, LIVETEST plants, env overrides)
_WRITE_ITEMS = frozenset({2, 3, 8, 9, 11})
# Items additionally gated by --allow-managed-writes
_MANAGED_WRITE_ITEMS = frozenset({4, 5, 7})
# Item gated by --allow-paging (pages the operator SNS topic)
_PAGING_ITEMS = frozenset({9})

# Components whose rate() EB Scheduler schedules never read FI_COST_GUARDRAIL_ID (B1)
_SAFE_RATE_COMPONENTS = frozenset({"diagnostics"})
# Shapes checked before a value read from AWS is put into SQL text
_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
# ScheduleExpressionTimezone values that mean UTC (live schedules use "Etc/UTC")
_UTC_ZONES = frozenset({"UTC", "Etc/UTC"})

# Minimum score for item 2 warranty plants (>= 80%)
_WARRANTY_SCORE_THRESHOLD = 0.80

# DMS REST paths, relative to DmsApiStack-{stage}'s ApiUrl (which ends in the stage, e.g.
# ".../staging/"). Every DMS route sits under /api/dms (checked against the live API's
# resources 2026-10-10; DmsExecutor builds the same paths, agents/executors/dms.py:75).
_DMS_RO_PATCH_PATH = "/api/dms/dealers/{dealer_id}/repair-orders/{ro_id}"
_DMS_FLEET_RO_PATH = "/api/dms/fleet/repair-orders"
_DMS_FLEET_CLAIM_PATH = "/api/dms/fleet/warranty-claims"
# DMS 400 bodies that only a request past the fleet-scope check can get (DMS main
# 25f90bf): the booking route validates avx_action_id after scope and the roster;
# the claim route looks the RO up after scope (it does not validate avx_action_id).
_DMS_UUID_REFUSAL = "avx_action_id must be a valid UUID"
_DMS_CLAIM_POST_SCOPE_REFUSAL = "RO not found"



# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


class Results:
    """PASS / FAIL / SKIP / NOTE tracker; drives exit code."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []
        self._cleanup_failed = False

    def check(self, label: str, ok: bool, detail: str = "", *, on_fail: str | None = None) -> bool:
        """Record a PASS or FAIL. ``detail`` is shown either way; ``on_fail``, when given,
        is shown instead of it on a FAIL only (failure wording on a PASS row misleads
        the reader of a live run)."""
        status = "PASS" if ok else "FAIL"
        if not ok and on_fail is not None:
            detail = on_fail
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
        self._cleanup_failed = True

    @property
    def failed(self) -> list[tuple[str, str, str]]:
        return [r for r in self.rows if r[1] == "FAIL"]

    @property
    def has_any_skip(self) -> bool:
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
            print(
                "  WARN: at least one restore/cleanup step failed — "
                "manual intervention may be needed"
            )


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------


class Http:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def call(
        self, method: str, path: str, token: str, body: Any = None, timeout: int = 40
    ) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.base + path, method=method, data=data,
            headers={"Authorization": token, "Content-Type": "application/json"},
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
        except urllib.error.URLError as exc:
            # Connection/timeout error (no HTTP response); return sentinel -1.
            return -1, {"error": str(exc)[:200]}



# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def _iso(t: dt.datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")


def _started_at_lower_bound(iso: str) -> str:
    """Lower bound for a ``started_at >=`` key condition on the agent-runs table.

    Run records carry millisecond ``started_at`` (``...T11:36:01.881Z``) and ``_iso(_now())`` is
    a whole second with a ``Z``. DynamoDB compares strings and ``.`` sorts below ``Z``, so
    ``...:01.881Z >= ...:01Z`` is false and a run that starts in the bound's own second would be
    missed. Without the ``Z`` the bound is a prefix of every value in that second, so it sorts
    below all of them and above every earlier second (issue
    ``2026-10-27-fi-sweep-avx-ms-run-start-readers``).
    """
    return iso[:-1] if iso.endswith("Z") else iso


def _run_id() -> str:
    """Generate run id: g55-<utc yyyymmddThhmmss>-<4 hex>."""
    stamp = _now().strftime("%Y%m%dT%H%M%S")
    return f"g55-{stamp}-{secrets.token_hex(8)}"


def _in_write_window() -> bool:
    """True when current UTC time is inside the scheduled-job write window (04:50–06:10)."""
    n = _now()
    mins = n.hour * 60 + n.minute
    return _WRITE_WINDOW_START_MIN <= mins <= _WRITE_WINDOW_END_MIN


def _compute_skips(
    items_requested: frozenset[int],
    allow_writes: bool,
    allow_managed_writes: bool,
    allow_paging: bool,
) -> tuple[frozenset[int], dict[int, str]]:
    """Return (items_to_run, {item: skip_reason}).

    Pure helper — no AWS, no side-effects; unit-testable.
    """
    to_run = set(items_requested)
    to_skip: dict[int, str] = {}

    if not allow_writes:
        for n in _WRITE_ITEMS & items_requested:
            to_skip[n] = f"item {n} requires --allow-writes"
        # Managed writes additionally require --allow-writes (design: "additionally gates")
        for n in _MANAGED_WRITE_ITEMS & items_requested:
            if n not in to_skip:
                to_skip[n] = f"item {n} requires --allow-writes (and --allow-managed-writes)"
        to_run -= _WRITE_ITEMS | _MANAGED_WRITE_ITEMS

    if not allow_managed_writes:
        for n in _MANAGED_WRITE_ITEMS & items_requested:
            to_skip[n] = f"item {n} requires --allow-managed-writes"
        to_run -= _MANAGED_WRITE_ITEMS

    if not allow_paging:
        for n in _PAGING_ITEMS & items_requested:
            if n not in to_skip:  # don't double-list if already removed by allow_writes
                to_skip[n] = f"item {n} requires --allow-paging"
        to_run -= _PAGING_ITEMS

    return frozenset(to_run), to_skip


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------

_SIGNAL_STATE: dict[str, Any] = {}


def _install_signal_handlers() -> None:
    """SIGINT and SIGTERM each raise SystemExit(143) once so every finally block runs.
    A second signal during teardown is ignored so cleanup can finish.
    """
    _SIGNAL_STATE["raised"] = False

    def _exit_once(signum: int, frame: object) -> None:
        if not _SIGNAL_STATE["raised"]:
            _SIGNAL_STATE["raised"] = True
            raise SystemExit(143)
        # Second signal during teardown: already exiting — ignore.

    _sig.signal(_sig.SIGINT, _exit_once)
    _sig.signal(_sig.SIGTERM, _exit_once)


@contextlib.contextmanager
def _defer_signals_for_restore() -> Iterator[None]:
    """Hold SIGINT/SIGTERM during the restore critical section; re-deliver after.

    A signal arriving during restore is recorded and re-sent (via os.kill) after the
    original handlers are reinstated, so the process exits cleanly but only after
    the restore completes.  A3.
    """
    _pending: list[int] = []

    def _record(sig: int, _frame: object) -> None:
        _pending.append(sig)

    old_int = _sig.signal(_sig.SIGINT, _record)
    old_term = _sig.signal(_sig.SIGTERM, _record)
    try:
        yield
    finally:
        _sig.signal(_sig.SIGINT, old_int)
        _sig.signal(_sig.SIGTERM, old_term)
        for s in _pending:
            os.kill(os.getpid(), s)



# ---------------------------------------------------------------------------
# Cognito helpers
# ---------------------------------------------------------------------------


def _claims(id_token: str) -> dict:
    import base64  # noqa: PLC0415
    payload = id_token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def password_client(idp: Any, pool_id: str) -> str:
    """Return the first user-pool client that allows USER_PASSWORD_AUTH."""
    clients = idp.list_user_pool_clients(
        UserPoolId=pool_id, MaxResults=30
    )["UserPoolClients"]
    for c in clients:
        desc = idp.describe_user_pool_client(
            UserPoolId=pool_id, ClientId=c["ClientId"]
        )
        flows = desc["UserPoolClient"].get("ExplicitAuthFlows") or []
        if "ALLOW_USER_PASSWORD_AUTH" in flows:
            return c["ClientId"]
    raise SystemExit(f"no app client on {pool_id} allows USER_PASSWORD_AUTH")


@contextlib.contextmanager
def principal(
    idp: Any,
    pool_id: str,
    client_id: str,
    label: str,
    groups: list[str],
    fleet_ids: str | None = None,
    dealer_ids: str | None = None,
) -> Iterator[tuple[str, dict]]:
    """Yield (id_token, claims) for an ephemeral Cognito user; delete in finally.

    fleet_ids: value for custom:fleetIds  (fleet-operator, fleet_manager).
    dealer_ids: value for custom:dealerIds (service-advisor).
    Tokens and passwords are never printed or written to the manifest.
    """
    username = (
        f"livetest-fi-rw-{label}-{int(time.time())}-{secrets.token_hex(3)}@example.invalid"
    )
    password = f"Lv{secrets.token_urlsafe(18)}!9Qz"
    attrs: list[dict[str, str]] = [
        {"Name": "email", "Value": username},
        {"Name": "email_verified", "Value": "true"},
    ]
    if fleet_ids is not None:
        attrs.append({"Name": "custom:fleetIds", "Value": fleet_ids})
    if dealer_ids is not None:
        attrs.append({"Name": "custom:dealerIds", "Value": dealer_ids})
    created = False
    try:
        idp.admin_create_user(
            UserPoolId=pool_id, Username=username,
            MessageAction="SUPPRESS", UserAttributes=attrs,
        )
        created = True
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
        print(
            f"  principal {label}: groups={groups} "
            f"fleetIds={fleet_ids!r} dealerIds={dealer_ids!r} (ephemeral)"
        )
        yield token, _claims(token)
    finally:
        if created:
            try:
                idp.admin_delete_user(UserPoolId=pool_id, Username=username)
                print(f"  cleanup: deleted principal {label}")
            except Exception as exc:  # noqa: BLE001
                print(
                    f"  cleanup: FAILED to delete {username}: {type(exc).__name__}. Run:\n"
                    f"    aws cognito-idp admin-delete-user "
                    f"--user-pool-id {pool_id} --username {username}",
                    file=sys.stderr,
                )



# ---------------------------------------------------------------------------
# DynamoDB backup helpers
# ---------------------------------------------------------------------------


def _ddb_tables_to_backup(stage: str) -> list[str]:
    """Tables that must be backed up before any write (design § Safety rails)."""
    return (
        [f"vsa-{stage}-avx-{s}" for s in ("findings", "actions", "agent-runs")]
        + [f"dms-{stage}-{s}" for s in ("repair-orders", "warranty-claims", "warranty-candidates")]
    )


def _create_ddb_backups(ddb_client: Any, stage: str, run: str) -> bool:
    """Create on-demand DDB backups and wait AVAILABLE. Returns True on success.

    Must be called before the first write; refuses writes if any backup fails.
    """
    tables = _ddb_tables_to_backup(stage)
    backup_arns: list[tuple[str, str]] = []

    for table in tables:
        backup_name = f"{table}-g55-{run}"
        try:
            resp = ddb_client.create_backup(TableName=table, BackupName=backup_name)
            arn = resp["BackupDetails"]["BackupArn"]
            backup_arns.append((table, arn))
            print(f"  backup started: {backup_name}")
        except Exception as exc:  # noqa: BLE001
            print(f"  backup FAILED for {table}: {exc}", file=sys.stderr)
            return False

    # Wait for AVAILABLE
    for table, arn in backup_arns:
        deadline = time.time() + 180
        while True:
            try:
                resp = ddb_client.describe_backup(BackupArn=arn)
                status = resp["BackupDescription"]["BackupDetails"]["BackupStatus"]
            except Exception as exc:  # noqa: BLE001
                print(f"  backup poll FAILED for {table}: {exc}", file=sys.stderr)
                return False
            if status == "AVAILABLE":
                break
            if status in ("DELETED", "DELETING"):
                print(
                    f"  backup FAILED for {table}: unexpected status={status}",
                    file=sys.stderr,
                )
                return False
            if time.time() >= deadline:
                print(
                    f"  backup TIMEOUT for {table}: status={status}",
                    file=sys.stderr,
                )
                return False
            time.sleep(5)
    print(f"  {len(backup_arns)} backup(s) AVAILABLE")
    return True


# ---------------------------------------------------------------------------
# Lambda client factory
# ---------------------------------------------------------------------------


def _make_lambda_client(region: str) -> Any:
    """Lambda client: read_timeout > 15-min function timeout, 1 attempt (no retries)."""
    import boto3  # noqa: PLC0415
    from botocore.config import Config  # noqa: PLC0415

    return boto3.client(
        "lambda", region_name=region,
        config=Config(read_timeout=910, retries={"max_attempts": 1}),
    )



# ---------------------------------------------------------------------------
# Teardown registry
# ---------------------------------------------------------------------------


class _TeardownRegistry:
    """Register cleanup callables BEFORE creating resources; run all in finally (LIFO)."""

    def __init__(self) -> None:
        self._items: list[tuple[Any, tuple]] = []

    def register(self, fn: Any, *args: Any) -> None:
        """Register a cleanup step. Call this BEFORE the resource is created."""
        self._items.append((fn, args))

    def run(self, res: Results) -> None:
        """Execute all cleanups in LIFO order. Errors are recorded, not re-raised."""
        for fn, args in reversed(self._items):
            try:
                fn(*args)
            except Exception as exc:  # noqa: BLE001
                name = getattr(fn, "__name__", repr(fn))
                print(f"  teardown ERROR ({name}): {exc}", file=sys.stderr)
                res.mark_cleanup_failed()


# ---------------------------------------------------------------------------
# Run manifest
# ---------------------------------------------------------------------------


def _init_manifest(run: str) -> Path:
    """Create /tmp/fi-rw-c-g55-<run>/manifest.json (mode 0600).

    Records every id created or acted on. Tokens and passwords are never written.
    """
    run_dir = Path(f"/tmp/fi-rw-c-g55-{run}")
    # exist_ok=False: never reuse a path someone else created (security c1: a planted
    # dir or symlink would receive item 9's env backup); the run id is random.
    run_dir.mkdir(mode=0o700, exist_ok=False)
    manifest = run_dir / "manifest.json"
    _write_0600(manifest, json.dumps({"run_id": f"g55-{run}", "items": {}}, indent=2))
    return manifest


def _write_0600(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` created/truncated with mode 0600 from the start."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.chmod(str(path), 0o600)


def _update_manifest(manifest: Path, key: str, value: Any) -> None:
    """Append / update a key under items{}. Preserves 0600 after overwrite."""
    data = json.loads(manifest.read_text())
    data["items"][key] = value
    _write_0600(manifest, json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Residue check (pluggable)
# ---------------------------------------------------------------------------


def _run_residue_checks(
    checks: list[tuple[str, Any]],
    res: Results,
) -> None:
    """Run pluggable residue checks. Each entry is (label, callable() -> (bool, str)).

    Residue failures cause exit 1 even when all items passed.
    """
    for label, check_fn in checks:
        try:
            ok, detail = check_fn()
            res.check(f"residue-{label}", ok, detail)
        except Exception as exc:  # noqa: BLE001
            res.check(f"residue-{label}", False, f"error: {exc}")


def _check_residue_findings(
    ddb: Any,
    findings_table: str,
    fleet_ids: list[str],
    direct_ids: list[str],
) -> tuple[bool, str]:
    """Check that no LIVETEST Findings remain in the given fleets or by direct ID.

    Extracted from the _residue_findings closure in main() so that tests can
    drive this logic with a recording fake DDB — closing mutant-4 (ok=True stub).

    Returns (True, summary) when zero hits remain; (False, summary) when any hit
    is found.  A DDB query error returns (False, error-detail) immediately.
    """
    from boto3.dynamodb.conditions import Key  # noqa: PLC0415
    hits: list[str] = []
    for flt in fleet_ids:
        try:
            table = ddb.Table(findings_table)
            kw: dict[str, Any] = {
                "IndexName": "by_fleet",
                "KeyConditionExpression": Key("fleet_id").eq(flt),
            }
            while True:
                page = table.query(**kw)
                hits.extend(
                    str(i.get("finding_id") or "") for i in page.get("Items", [])
                )
                tok = page.get("LastEvaluatedKey")
                if not tok:
                    break
                kw["ExclusiveStartKey"] = tok
        except Exception as exc:  # noqa: BLE001
            return False, f"query error for fleet {flt!r}: {exc}"
    for fid in direct_ids:
        try:
            resp = ddb.Table(findings_table).get_item(Key={"finding_id": fid})
        except Exception as exc:  # noqa: BLE001
            # Fail closed: an unreadable row is not proof that it is gone.
            return False, f"get_item error for {fid!r}: {exc}"
        if resp.get("Item"):
            hits.append(fid)
    ok = len(hits) == 0
    return ok, f"{len(hits)} LIVETEST finding(s) still present: {hits[:5]}"


def _check_residue_actions(ddb: Any, actions_table: str, prefix: str) -> tuple[bool, str]:
    """0 Action rows whose finding_id begins with ``prefix`` (this run's LIVETEST prefix).

    Extracted from main()'s closure so tests drive it (architect re-run of review
    cycle 1 mutant 4 on the actions half, 2026-10-10). A scan error fails closed.
    """
    from boto3.dynamodb.conditions import Attr  # noqa: PLC0415
    try:
        table = ddb.Table(actions_table)
        hits: list[str] = []
        kw: dict[str, Any] = {
            "FilterExpression": Attr("finding_id").begins_with(prefix),
            "ProjectionExpression": "action_id, finding_id",
        }
        while True:
            page = table.scan(**kw)
            hits.extend(str(i.get("action_id") or "") for i in page.get("Items", []))
            tok = page.get("LastEvaluatedKey")
            if not tok:
                break
            kw["ExclusiveStartKey"] = tok
    except Exception as exc:  # noqa: BLE001
        return False, f"scan error: {exc}"
    ok = len(hits) == 0
    return ok, f"{len(hits)} LIVETEST action(s) still present: {hits[:5]}"


def _item_blocked_by_failed_restore(snap: dict[str, Any]) -> bool:
    """Return True if item9's restore failed, so subsequent items must be refused.

    C4/C6 (x3 mutant): a typo in the key (e.g. 'restore_okay') would return False
    even when restore_ok=False, letting later items run with a broken Lambda env.
    """
    return not snap.get("restore_ok", True)


def _check_residue_cognito(idp: Any, pool_id: str | None) -> tuple[bool, str]:
    """C4/C6: 0 Cognito users whose email begins with 'livetest-fi-rw-'.

    Extracted from main()'s closure so tests can drive it with a fake idp.
    The filter uses the email attribute (not username) because pool users have
    UUID usernames — filtering by username would match nothing (C6 mutant a5).
    """
    if pool_id is None:
        return True, "Cognito pool not configured; check skipped"
    try:
        kw: dict[str, Any] = {
            "UserPoolId": pool_id,
            "Filter": 'email ^= "livetest-fi-rw-"',
        }
        hits: list[str] = []
        while True:
            page = idp.list_users(**kw)
            hits.extend(u.get("Username", "") for u in page.get("Users", []))
            tok = page.get("PaginationToken")
            if not tok:
                break
            kw["PaginationToken"] = tok
        ok = len(hits) == 0
        return ok, f"{len(hits)} livetest Cognito user(s) still present: {hits[:3]}"
    except Exception as exc:  # noqa: BLE001
        return False, f"list_users error: {exc}"


def _check_dms_residue_5b_ro(ddb: Any, stage: str, ro_id: str | None) -> tuple[bool, str]:
    """C4: item 5b's draft RO must be gone from DMS after teardown.

    A remaining RO on a managed fleet VIN fails the run even if teardown printed OK.
    Skip (return True) when ro_id is None (5b was never created).
    """
    if not ro_id:
        return True, "5b RO not created; skip"
    try:
        resp = ddb.Table(f"dms-{stage}-repair-orders").get_item(Key={"ro_id": ro_id})
        if resp.get("Item"):
            return False, f"5b RO {ro_id!r} still present in dms-{stage}-repair-orders"
        return True, f"5b RO {ro_id!r} absent"
    except Exception as exc:  # noqa: BLE001
        return False, f"get_item error for 5b RO {ro_id!r}: {exc}"


def _check_dms_residue_fi_bookings(
    ddb: Any, stage: str, avx_action_ids: list[str]
) -> tuple[bool, str]:
    """Residue check: FI booking ROs for THIS run must be exactly the planned ones.

    DMS stores bare UUIDs; strip 'AVX-ACT-' prefix before comparing.
    Full paged scan (no Limit — DynamoDB evaluates Limit pages before FilterExpression).
    Asserts exactly one RO per planned avx_action_id: no extras, none missing.
    A scan error fails (fail closed).
    """
    if not avx_action_ids:
        return True, "no FI booking action_ids; skip"
    bare_ids = [aid.removeprefix("AVX-ACT-") for aid in avx_action_ids]
    try:
        from boto3.dynamodb.conditions import Attr as _Attr  # noqa: PLC0415
        table = ddb.Table(f"dms-{stage}-repair-orders")
        found: dict[str, int] = {}
        kw: dict[str, Any] = {
            "FilterExpression": _Attr("avx_action_id").is_in(bare_ids),
            "ProjectionExpression": "ro_id, avx_action_id",
        }
        while True:
            resp = table.scan(**kw)
            if "Items" not in resp:
                return False, "scan error: no Items key in response"
            for item in resp["Items"]:
                aid = item.get("avx_action_id", "")
                found[aid] = found.get(aid, 0) + 1
            tok = resp.get("LastEvaluatedKey")
            if not tok:
                break
            kw["ExclusiveStartKey"] = tok
    except Exception as exc:  # noqa: BLE001
        return False, f"scan error for FI booking residue: {exc}"
    missing = [b for b in bare_ids if found.get(b, 0) == 0]
    extra = [(b, found[b]) for b in found if found[b] != 1]
    if missing or extra:
        return False, (
            f"FI booking ROs mismatch; missing={missing[:3]} (planned RO not in DMS: see "
            f"items 4/5a), extra_counts={extra[:3]}"
        )
    return True, f"FI booking ROs match planned: {len(bare_ids)} found"


def _check_dms_residue_fi_claims(
    ddb: Any, stage: str, avx_action_ids: list[str]
) -> tuple[bool, str]:
    """Residue check: FI claims for THIS run must be exactly the planned ones.

    DMS stores bare UUIDs; strip 'AVX-ACT-' prefix before comparing.
    Full paged scan (no Limit — DynamoDB evaluates Limit pages before FilterExpression).
    Asserts exactly one claim per planned avx_action_id: no extras, none missing.
    Item 7's claim remains in DMS so the deployed stand-in can pay it; asserting
    PRESENCE (count == 1) is the correct property here.
    A scan error fails (fail closed).
    """
    if not avx_action_ids:
        return True, "no FI claim action_ids; skip"
    bare_ids = [aid.removeprefix("AVX-ACT-") for aid in avx_action_ids]
    try:
        from boto3.dynamodb.conditions import Attr as _Attr  # noqa: PLC0415
        table = ddb.Table(f"dms-{stage}-warranty-claims")
        found: dict[str, int] = {}
        kw: dict[str, Any] = {
            "FilterExpression": _Attr("avx_action_id").is_in(bare_ids),
            "ProjectionExpression": "claim_id, avx_action_id",
        }
        while True:
            resp = table.scan(**kw)
            if "Items" not in resp:
                return False, "scan error: no Items key in response"
            for item in resp["Items"]:
                aid = item.get("avx_action_id", "")
                found[aid] = found.get(aid, 0) + 1
            tok = resp.get("LastEvaluatedKey")
            if not tok:
                break
            kw["ExclusiveStartKey"] = tok
    except Exception as exc:  # noqa: BLE001
        return False, f"scan error for FI claim residue: {exc}"
    missing = [b for b in bare_ids if found.get(b, 0) == 0]
    extra = [(b, found[b]) for b in found if found[b] != 1]
    if missing or extra:
        return False, (
            f"FI claim rows mismatch; missing={missing[:3]} (planned claim not in DMS: see "
            f"item 7), extra_counts={extra[:3]}"
        )
    return True, f"FI claim rows match planned: {len(bare_ids)} found"



# ---------------------------------------------------------------------------
# Scoring helpers (pure — unit-testable without AWS)
# ---------------------------------------------------------------------------


def score_recall_views(
    view_rows: list[dict[str, Any]],
    answer_key_rows: list[dict[str, Any]],
    dms_remedy_refs: set[str] | frozenset[str] = frozenset(),
) -> tuple[bool, str]:
    """Item 1: compare ``recall_vehicle_status`` with the answer key per campaign.

    view_rows: per-(campaign, VIN) rows {campaign_id, vin, match_basis, remedied,
    remedy_ref}. answer_key_rows: per campaign {campaign_id, confirmed_vins,
    remedied_vins (frozensets), population (int)} from the ``campaign_summary`` rows
    (interface.md § 8c).

    CONFIRMED and remedied compare as (campaign, VIN) sets; POPULATION as a count.
    The answer key's ``remedied_vins`` are the planted ADP-path remedies (Kind 1b,
    rule b). A row remedied by a DMS repair order (rule a: ``remedy_ref`` is a DMS
    ``ro_id``, e.g. an earlier run's item 4 booking) is not a plant, so it is never
    reported as extra. It still counts on the missing side: the view prefers the DMS
    remedy (``COALESCE(rdms.remedy_ref, radp.remedy_ref)``), so a planted VIN that also
    has a DMS remedy shows the DMS ``ro_id`` and is still remedied (review rebuild c1 WB1).
    Anti-vacuity: the answer key must be non-empty.
    """
    if not answer_key_rows:
        return False, "answer key is empty — anti-vacuity failure"

    confirmed_vins_view: dict[str, set[str]] = {}
    remedied_vins_view: dict[str, set[str]] = {}   # every remedied VIN
    dms_path_vins_view: dict[str, set[str]] = {}   # remedied by a DMS repair order
    population_view: dict[str, int] = {}
    for row in view_rows:
        cid = str(row.get("campaign_id") or "")
        vin = str(row.get("vin") or "")
        match_basis = str(row.get("match_basis") or "")
        remedied = str(row.get("remedied") or "").strip().lower() == "true"
        if match_basis == "CONFIRMED":
            confirmed_vins_view.setdefault(cid, set()).add(vin)
        if match_basis == "POPULATION":
            population_view[cid] = population_view.get(cid, 0) + 1
        if remedied:
            remedied_vins_view.setdefault(cid, set()).add(vin)
            if str(row.get("remedy_ref") or "") in dms_remedy_refs:
                dms_path_vins_view.setdefault(cid, set()).add(vin)

    mismatches: list[str] = []
    for ak in answer_key_rows:
        cid = str(ak.get("campaign_id") or "")
        ak_confirmed = frozenset(ak.get("confirmed_vins") or ())
        ak_remedied = frozenset(ak.get("remedied_vins") or ())
        ak_population = int(ak.get("population", 0))
        view_confirmed = confirmed_vins_view.get(cid, set())
        if ak_confirmed != view_confirmed:
            mismatches.append(
                f"{cid} confirmed: missing={sorted(ak_confirmed - view_confirmed)[:3]} "
                f"extra={sorted(view_confirmed - ak_confirmed)[:3]}")
        view_remedied = remedied_vins_view.get(cid, set())
        missing_r = ak_remedied - view_remedied
        extra_r = view_remedied - ak_remedied - dms_path_vins_view.get(cid, set())
        if missing_r or extra_r:
            mismatches.append(
                f"{cid} remedied: missing={sorted(missing_r)[:3]} extra={sorted(extra_r)[:3]}")
        if population_view.get(cid, 0) != ak_population:
            mismatches.append(
                f"{cid} population: answer_key={ak_population} view={population_view.get(cid, 0)}")

    if mismatches:
        return False, "; ".join(mismatches[:5])
    return True, f"all {len(answer_key_rows)} campaign(s) match answer key"


def score_warranty_run(
    planted_lines: list[dict[str, Any]],
    warranty_findings: list[dict[str, Any]],
) -> tuple[bool, int, int, int, float, str]:
    """Item 2: score planted warranty lines against fleet.warranty.claim_candidate Findings.

    planted_lines: from recall_plants_dms_seed.jsonl;
        each has ro_id, verdict_plant ('approved'/'requires_review'/'denied'), claimed bool.
    warranty_findings: all warranty-kind Findings for the run's fleet.

    Returns (ok, flagged, denied_flagged, total_eligible, pct, detail).
    PASS requires: pct >= 80%, denied_flagged == 0, claimed_flagged == 0.

    Mutant records:
      - change >= 0.80 to > 0.80: a run scoring exactly 80% passes incorrectly.
      - remove denied_flagged == 0 guard: a denied line flagged returns PASS.
    """
    # Index findings by ro_id they reference
    ro_to_finding: dict[str, dict] = {}
    for f in warranty_findings:
        if f.get("finding_kind") != _WARRANTY_KIND:
            continue
        params = (f.get("recommended_action") or {}).get("parameters") or {}
        ro_id = str(params.get("ro_id") or "")
        if ro_id:
            ro_to_finding[ro_id] = f

    eligible = [
        p for p in planted_lines
        if p.get("verdict_plant") in ("approved", "requires_review")
        and not p.get("already_claimed")  # C1(a): live seed field is already_claimed
    ]
    denied = [p for p in planted_lines if p.get("verdict_plant") == "denied"]
    already_claimed = [p for p in planted_lines if p.get("already_claimed")]  # C1(a)

    def _matches(p: dict[str, Any]) -> bool:
        """C1(b): evidence is list[evidence_entry] per finding.schema.json.
        Each entry: {"kind": "warranty.verdict", "value": {"verdict": ...}, ...}
        Shape comes from warranty.py build_warranty_evidence + _to_schema_ev.
        """
        f = ro_to_finding.get(str(p.get("ro_id") or ""))
        if f is None:
            return False
        ev_list = f.get("evidence") or []
        found_verdict = None
        for ev in (ev_list if isinstance(ev_list, list) else []):
            if ev.get("kind") == "warranty.verdict":
                # schema form: _to_schema_ev stores domain dict under "value"
                found_verdict = (ev.get("value") or ev.get("warranty.verdict") or {}).get("verdict")
                break
        return found_verdict == p.get("verdict_plant")

    flagged = sum(1 for p in eligible if _matches(p))
    denied_flagged = sum(1 for p in denied if _matches(p))
    claimed_flagged = sum(1 for p in already_claimed if _matches(p))

    total_eligible = len(eligible)
    pct = flagged / total_eligible if total_eligible > 0 else 0.0
    ok = (pct >= _WARRANTY_SCORE_THRESHOLD) and (denied_flagged == 0) and (claimed_flagged == 0)
    detail = (
        f"flagged={flagged}/{total_eligible} ({pct:.1%}), "
        f"threshold={_WARRANTY_SCORE_THRESHOLD:.0%}, "
        f"denied_flagged={denied_flagged}, claimed_flagged={claimed_flagged}"
    )
    return ok, flagged, denied_flagged, total_eligible, pct, detail


def score_recall_disclosure(
    recall_findings: list[dict[str, Any]],
    catalog: dict[str, dict[str, str]],
) -> tuple[bool, int, int, str]:
    """C2 fix: compare every recall Finding's disclosure_txt against catalog.

    Returns (ok, compared, skipped, detail).
    PASS requires: compared >= 1 (anti-vacuity) and skipped == 0 (no unknown cids)
    and 0 mismatches.

    Mutant record: `recall_fs[:0]` (compare nothing) → compared=0 → anti-vacuity fail.
    """
    bad: list[str] = []
    compared = 0
    skipped = 0
    for f in recall_findings:
        sk = str(f.get("subject_key") or "")
        cid = sk.split("/")[0] if sk else ""
        cat = catalog.get(cid)
        if not cat:
            skipped += 1
            continue
        compared += 1
        expected = f"{cat['summary']}\n\n{cat['consequence']}\n\n{cat['remedy']}"
        if f.get("disclosure_txt") != expected:
            bad.append(f"finding_id={f.get('finding_id')!r} cid={cid}")
    anti_ok = compared >= 1 and skipped == 0
    ok = anti_ok and len(bad) == 0
    detail = (
        f"compared={compared}, skipped={skipped}, mismatches={len(bad)}"
        + (f": {bad[:3]}" if bad else "")
        + ("" if anti_ok else " [anti-vacuity fail]")
    )
    return ok, compared, skipped, detail


def score_rerun(run_record: dict[str, Any]) -> tuple[bool, str]:
    """Item 3 (5.5 Accept 3: "zero new Findings, zero model calls"): the re-run's
    record shows no model call and no tokens.

    ``artifacts_written`` is reported, not scored: the loop rewrites every unchanged
    model-written item to keep it from expiring (refresh_from_live, call 52) and
    rewrites fixed items (ground, informational), so a correct re-run writes rows
    that are not new Findings. New Findings are scored by finding id in item 3.
    """
    aw = run_record.get("artifacts_written", -1)
    mc = run_record.get("model_calls", -1)
    tu = run_record.get("tokens_used", -1)
    ok = mc == 0 and tu == 0
    return ok, f"model_calls={mc}, tokens_used={tu} (artifacts_written={aw}, refreshes allowed)"


def score_new_findings(before_ids: set[str], after_ids: set[str]) -> tuple[bool, str]:
    """Item 3: no finding id exists after the re-run that did not exist before it."""
    new = sorted(after_ids - before_ids)
    return not new, f"{len(new)} new finding id(s): {new[:3]}"


def score_finding_counts(
    before: dict[str, int],
    after: dict[str, int],
) -> tuple[bool, str]:
    """W8 fix: verify per-kind Finding counts didn't increase after a re-run.

    Returns (ok, detail). PASS if no kind has more after than before.

    Mutant record: dropping the `increased` check lets new Findings silently pass.
    """
    increased = {k: after[k] - before.get(k, 0) for k in after if after[k] > before.get(k, 0)}
    ok = not increased
    detail = f"increased={increased}" if increased else f"unchanged across {len(after)} kind(s)"
    return ok, detail


# ---------------------------------------------------------------------------
# Additional helpers (for item-specific logic; stubs for Part 1)
# ---------------------------------------------------------------------------


def _load_dms_module(dms_repo: str, rel: str) -> Any:
    """Load a Python module from the DMS repo by relative path.

    C6 fix: fi_stand_ins.py does ``from source._lib.events import …``, so the
    DMS repo root must be on sys.path during exec_module.  We insert it, load,
    then remove it to keep the caller's sys.path clean.
    """
    import sys as _sys
    path = Path(dms_repo) / rel
    dms_root = str(Path(dms_repo).resolve())
    inserted = dms_root not in _sys.path
    if inserted:
        _sys.path.insert(0, dms_root)
    try:
        spec = importlib.util.spec_from_file_location(path.stem, path)
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod
    finally:
        if inserted:
            try:
                _sys.path.remove(dms_root)
            except ValueError:
                pass


def _adjudicate_claim_in_memory(
    dms_repo: str,
    claim: dict[str, Any],
    submitted_at: dt.datetime,
) -> dict[str, Any]:
    """Item 7: call DMS's own _adjudicate_fi_claims in memory (no write).

    Loads source/simulation/fi_stand_ins.py from dms_repo.
    at = submitted_at + 14 days + 1 min (so adjudicator period has elapsed).
    Returns the adjudicated claim dict.

    C6 fix: _adjudicate_fi_claims returns a counts dict and mutates the claim
    in place (DMS fi_stand_ins.py:351-440).  We pass [claim], let it mutate,
    and return the same claim reference — not results[0].

    Mutant-2 guard: the function must call mod._adjudicate_fi_claims exactly
    once with ([claim], at=submitted_at+14d+1min) and return the *same* claim
    object (not a hand-built copy).
    """
    mod = _load_dms_module(dms_repo, "source/simulation/fi_stand_ins.py")
    at = submitted_at + dt.timedelta(days=14, minutes=1)
    # mutates claim in place; returns {"paid": N, "rejected": N, ...} (counts dict)
    mod._adjudicate_fi_claims([claim], at=at)
    return claim


def _check_fi_booking_isolation(
    dms_repo: str,
    existing_ros: list[dict[str, Any]],
    this_run_ro_ids: set[str],
) -> tuple[bool, str]:
    """Item 4/5: before invoking dealer-progress, ensure every open FI booking
    in DMS is one this run created.

    'Open' means not in a terminal state (Closed, Delivered, Invoiced); only open
    ROs can be advanced by dealer-progress.  Completed ROs from prior runs are not
    a concern (W2 fix).

    Loads DMS's is_fi_booking rule from source/simulation/fi_stand_ins.py.
    existing_ros: full RO dicts from the DMS repair-orders table scan.
    Returns (ok, detail). FAIL if any open RO is an FI booking not in this run.

    Mutant record: removing the set-membership check lets a foreign FI booking pass.
    """
    _TERMINAL_STATUSES = {"Closed", "Delivered", "Invoiced"}
    open_ros = [r for r in existing_ros if r.get("status", "Draft") not in _TERMINAL_STATUSES]
    if not open_ros:
        return True, "no open FI bookings found"
    mod = _load_dms_module(dms_repo, "source/simulation/fi_stand_ins.py")
    is_fi = getattr(mod, "is_fi_booking", None)
    if is_fi is None:
        return False, "is_fi_booking not found in fi_stand_ins.py"
    foreign: list[str] = []
    for ro in open_ros:
        ro_id = str(ro.get("ro_id") or "")
        # is_fi_booking needs the full RO dict (avx_action_id, customer_type, lines)
        if is_fi(ro) and ro_id not in this_run_ro_ids:
            foreign.append(ro_id)
    if foreign:
        return False, f"foreign FI booking(s) would be advanced: {foreign[:3]}"
    return True, f"all {len(open_ros)} open FI booking(s) belong to this run"


def _scan_all_dms_ros(ddb: Any, stage: str) -> list[dict[str, Any]]:
    """Scan the full DMS repair-orders table. Used for the foreign-FI-booking check."""
    table = ddb.Table(f"dms-{stage}-repair-orders")
    items: list[dict[str, Any]] = []
    kw: dict[str, Any] = {}
    while True:
        page = table.scan(**kw)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            break
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return items


_MANAGED_FLEETS = [
    "demo-rental-carshare", "demo-lastmile-delivery",
    "demo-field-service", "demo-city-public",
]


def _pick_managed_fleet_a(
    ddb: Any,
    findings_table: str,
) -> tuple[str, list[dict[str, Any]]]:
    """W3: compute fleet A (most pending /book/1 recall items) once in main().

    Returns (fleet_a, fleet_a_book_findings).  Called before any managed write so all
    items 4, 5, 7, 8 see the same fleet at the same snapshot.

    Mutant record: computing fleet A independently per item can pick a different fleet
    when counts change between items (e.g. after item 5a approves one book item).
    """
    now_iso = _iso(_now())
    counts: dict[str, int] = {}
    findings: dict[str, list[dict[str, Any]]] = {}
    for flt in _MANAGED_FLEETS:
        fs = _scan_findings_by_fleet(ddb, findings_table, flt, _AGENT_FI_RW)
        pending = [
            f for f in fs
            if f.get("finding_kind") == _RECALL_KIND
            and "/book/" in str(f.get("subject_key") or "")
            and (f.get("expires_at") or "9999") > now_iso
        ]
        counts[flt] = len(pending)
        findings[flt] = pending
    fleet_a = max(counts, key=counts.__getitem__)
    return fleet_a, findings[fleet_a]


def _pick_book_finding(
    recall_findings: list[dict[str, Any]],
    dms_ros: list[dict[str, Any]],
    exclude_vins: set[str] | None = None,
    existing_action_finding_ids: set[str] | None = None,
    skip: Any = None,
) -> dict[str, Any] | None:
    """Deterministic planned-item selection rule for items 4 and 5a.

    Finds the pending, unexpired /book/1 recall Finding with the lowest finding_id
    whose VIN has no DMS RO with a recall line for that campaign.
    Optionally excludes VINs already planned (for item 5a to pick a different VIN than 4).
    Optionally excludes finding_ids that already have any Action (W3: a declined/pending
    Finding returns 409 on approve; design says "pending" = no existing Action).

    Rule: subject_key contains '/book/1' (not /book/2 etc.), not expired, no existing
    Action for this finding_id, VIN has no DMS RO with a recall line (pay_type=='recall')
    for the same campaign.  Among those, lowest finding_id (lexicographic) is deterministic.

    Printed and written to manifest BEFORE the approve call (safety rail).

    Mutant record: removing the 'no existing RO' filter would plan a VIN that already
    has a repair order, breaking idempotency and uniqueness.
    """
    now_iso = _iso(_now())
    # Index DMS ROs: (vin, campaign_id) -> True if an open recall RO exists
    vin_campaign_has_ro: set[tuple[str, str]] = set()
    for ro in dms_ros:
        vin = str(ro.get("vehicle_vin") or "")  # W7: DMS ROs carry vehicle_vin, not vin
        lines = ro.get("lines") or []
        for line in lines:
            cid = str(line.get("recall_campaign_id") or "")
            pay = str(line.get("pay_type") or "")
            if cid and pay == "recall":
                vin_campaign_has_ro.add((vin, cid))

    exclude_vins = exclude_vins or set()
    actioned = existing_action_finding_ids or set()
    candidates: list[dict[str, Any]] = []
    for f in recall_findings:
        sk = str(f.get("subject_key") or "")
        parts = sk.split("/")
        # W3: require /book/1 specifically (not /book/2 etc.)
        if len(parts) < 3 or parts[1] != "book" or parts[2] != "1":
            continue
        exp = str(f.get("expires_at") or "")
        if exp and exp <= now_iso:
            continue  # expired
        fid = str(f.get("finding_id") or "")
        # W3: skip findings that already have any Action (declined/pending gives 409)
        if fid in actioned:
            continue
        vin = str((f.get("scope") or {}).get("vin") or "")
        if vin in exclude_vins:
            continue
        cid = parts[0]
        if (vin, cid) in vin_campaign_has_ro:
            continue  # already has an RO
        if skip is not None and skip(fid):
            continue  # e.g. its RO would close early (item 4)
        candidates.append(f)

    if not candidates:
        return None
    # Lowest finding_id is deterministic
    return min(candidates, key=lambda f: str(f.get("finding_id") or ""))


def _interpret_dms_fleet_scope_denial(
    wrong_fleet_status: int,
    same_fleet_invalid_status: int,
) -> tuple[bool, str]:
    """Item 8: DMS fleet routes return a uniform 404 on scope denial (not 403).

    DMS auth.py ``_fleet_denial_response`` always returns 404 so the caller
    cannot distinguish "route not found" from "fleet not yours" (oracle-free).
    wrong_fleet_status: expected 404 (scope check fires, returns fleet-denial 404).
    same_fleet_invalid_status: expected 400 (passes scope; validation rejects).

    C3: dealer_id must be in the booking-probe body or the handler returns 400
    "dealer_id is required" *before* the scope check, so the control and the
    cross-fleet probe both reach the same pre-scope 400 and the positive control
    is vacuous.  Callers must include dealer_id in the body.

    Mutant record: swap 404/400 — a route returning 400 for both would pass
    validation_ok but fail scope_ok.
    """
    scope_ok = wrong_fleet_status == 404
    validation_ok = same_fleet_invalid_status == 400
    ok = scope_ok and validation_ok
    detail = (
        f"wrong-fleet={wrong_fleet_status} (want 404), "
        f"same-fleet-invalid={same_fleet_invalid_status} (want 400)"
    )
    return ok, detail


def _pick_warranty_finding(
    warranty_findings: list[dict[str, Any]],
    existing_actions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Item 7: pick the approved, not-yet-actioned warranty Finding with lowest finding_id.

    Filters for:
    - finding_kind == "fleet.warranty.claim_candidate"
    - recommended_action.action_kind == "submit_warranty_claim" (approved verdict only)
    - evidence kind="warranty.verdict" verdict == "approved"
    - no existing Action for this finding_id (any status — pending_execution and
      in_progress also return 409 on approve; W3 fix)

    Mutant record: removing the == "approved" verdict filter also selects requires_review
    Findings, which produce Validated (HTTP 202) DMS claims; the adjudicator skips them,
    so the in-memory adjudication step returns the claim unchanged (not "Paid"), failing
    score_adjudication. Test: pass a requires_review finding — must NOT be returned.
    """
    # W3: skip findings with ANY existing Action (pending_execution/in_progress -> 409)
    actioned_ids: set[str] = set()
    for act in existing_actions:
        actioned_ids.add(str(act.get("finding_id") or ""))

    candidates: list[dict[str, Any]] = []
    for f in warranty_findings:
        if f.get("finding_kind") != _WARRANTY_KIND:
            continue
        rec = f.get("recommended_action") or {}
        if rec.get("action_kind") != "submit_warranty_claim":
            continue
        # Check approved verdict in evidence (approved-only; requires_review excluded)
        evidence = f.get("evidence") or []
        verdict: str | None = None
        for ev in evidence:
            if (ev.get("kind") or "") == "warranty.verdict":
                # schema form: _to_schema_ev stores verdict under "value"
                # source: agents/fi_recall_warranty/warranty.py _to_schema_ev
                verdict = (ev.get("value") or ev.get("warranty.verdict") or {}).get("verdict")
                break
        if verdict != "approved":
            continue
        fid = str(f.get("finding_id") or "")
        if fid in actioned_ids:
            continue
        candidates.append(f)

    if not candidates:
        return None
    return min(candidates, key=lambda f: str(f.get("finding_id") or ""))


def score_adjudication(
    claim_before: dict[str, Any],
    adjudicated: dict[str, Any],
) -> tuple[bool, str]:
    """Item 7: assert in-memory adjudication yields Paid with correct fields.

    Checks: status=="Paid", paid_amount_usd==claimed_amount_usd, paid_at set, simulated_by set.
    This uses DMS's real _adjudicate_fi_claims function (no write); validates the
    deviation from "run with a date 14 days on" per design § item 7.

    Mutant record: removing the paid_amount_usd == claimed_amount_usd check accepts a claim
    adjudicated to Paid with a different (e.g. zero) amount — a silent partial-payment defect.
    """
    status = adjudicated.get("status")
    paid_amt = adjudicated.get("paid_amount_usd")
    claimed_amt = claim_before.get("claimed_amount_usd")
    paid_at = adjudicated.get("paid_at")
    simulated_by = adjudicated.get("simulated_by")

    status_ok = status == "Paid"
    amount_ok = (
        paid_amt is not None
        and claimed_amt is not None
        and paid_amt == claimed_amt
    )
    paid_at_ok = bool(paid_at)
    simulated_by_ok = bool(simulated_by)

    ok = status_ok and amount_ok and paid_at_ok and simulated_by_ok
    detail = (
        f"status={status!r}(want Paid), "
        f"paid={paid_amt}==claimed={claimed_amt}→{amount_ok}, "
        f"paid_at={paid_at!r}, simulated_by={simulated_by!r}"
    )
    return ok, detail


def _count_actions_for_finding(ddb: Any, table_name: str, finding_id: str) -> int:
    """Item 8: scan AVX Actions table for rows with finding_id. 0 expected after a 403.

    Returns count, or -1 on scan error (treated as unknown by callers).
    Paginates fully — Limit=N before a FilterExpression caps pages read, not rows
    matched, so a Limit would produce a vacuous 0 when the row falls beyond that page.

    Mutant record: returning 0 always masks an accidental write that bypassed the scope
    check; the 'no Action row' assertion becomes vacuous. Test: inject a matching row and
    verify the function returns a positive count, not 0.
    """
    from boto3.dynamodb.conditions import Attr  # noqa: PLC0415
    try:
        table = ddb.Table(table_name)
        count = 0
        kw: dict[str, Any] = {"FilterExpression": Attr("finding_id").eq(finding_id)}
        while True:
            resp = table.scan(**kw)
            count += len(resp.get("Items", []))
            tok = resp.get("LastEvaluatedKey")
            if not tok:
                break
            kw["ExclusiveStartKey"] = tok
        return count
    except Exception:  # noqa: BLE001
        return -1


def _decided_finding_ids(ddb: Any, actions_table: str, finding_ids: list[str]) -> set[str]:
    """FI Findings that already have their decision Action (approve answers 409).

    The decision Action id is deterministic per Finding (``_derive_decision_action_id``,
    contract § 4), so one GetItem per candidate answers it. A read error raises: the
    caller FAILs rather than treating an unreadable Finding as never decided.
    """
    from agents.supervisor.tools.fleet_recommendation import (  # noqa: PLC0415
        _derive_decision_action_id,
    )
    table = ddb.Table(actions_table)
    out: set[str] = set()
    for fid in finding_ids:
        if table.get_item(Key={"action_id": _derive_decision_action_id(fid)}).get("Item"):
            out.add(fid)
    return out


def _early_close_predicate(dms_repo: str) -> Any:
    """``fid -> bool``: would DMS's dealer-progress stand-in close this Finding's RO early?

    The booking RO id is deterministic: DMS ``_ro_id_for_avx_action`` of the bare
    decision Action id; the stand-in closes ~5% of ROs early by
    ``_would_close_early(ro_id)``. Both are DMS's own functions, loaded from the DMS
    repo, so item 4 can skip such a Finding BEFORE approving it.
    """
    from agents.supervisor.tools.fleet_recommendation import (  # noqa: PLC0415
        _derive_decision_action_id,
    )
    ro_mod = _load_dms_module(dms_repo, "source/handlers/repair_orders.py")
    fi_mod = _load_dms_module(dms_repo, "source/simulation/fi_stand_ins.py")

    def would_close_early(fid: str) -> bool:
        bare = _derive_decision_action_id(fid).removeprefix("AVX-ACT-")
        return bool(fi_mod._would_close_early(ro_mod._ro_id_for_avx_action(bare)))

    return would_close_early


def _parse_campaign_summary(notes: str) -> dict[str, Any]:
    """Parse campaign_summary notes string into {confirmed_vins, remedied_vins, population}.

    Format: 'confirmed_vins=[...],remedied_vins=[...],population_count=N'
    Uses ast.literal_eval on the list literals; returns empty sets on parse failure.
    W1(d): returns frozensets so score_recall_views can compare (campaign, VIN) sets
    instead of counts; shape change in the answer key breaks the comparison.

    Mutant record: removing the remedied extraction returns remedied_vins=frozenset()
    for all campaigns, causing score_recall_views to flag a mismatch where remedied>0.
    """
    import ast as _ast  # noqa: PLC0415
    import re as _re  # noqa: PLC0415
    result: dict[str, Any] = {"confirmed_vins": frozenset(), "remedied_vins": frozenset(), "population": 0}
    m = _re.search(r"confirmed_vins=(\[[^\]]*\])", notes)
    if m:
        try:
            result["confirmed_vins"] = frozenset(_ast.literal_eval(m.group(1)))
        except Exception:  # noqa: BLE001
            pass
    m = _re.search(r"remedied_vins=(\[[^\]]*\])", notes)
    if m:
        try:
            result["remedied_vins"] = frozenset(_ast.literal_eval(m.group(1)))
        except Exception:  # noqa: BLE001
            pass
    m = _re.search(r"population_count=(\d+)", notes)
    if m:
        result["population"] = int(m.group(1))
    return result


def score_groundings(
    run_record: dict[str, Any],
    recall_findings: list[dict[str, Any]],
    p0_rows: set[tuple[str, str]],
    selected_vins: int,
    *,
    remedied_since: set[tuple[str, str]] | frozenset[tuple[str, str]] = frozenset(),
) -> tuple[bool, str]:
    """Item 6: verify groundings without CMS's route, per (campaign_id, vin).

    ``selected_vins`` is the number of DISTINCT VINs among the view's unremedied rows
    (the rows the run selects). The agent reads groundings once per VIN
    (``recall.py`` decide phase), so with no groundings table every VIN counts one
    read error: ``grounding_read_errors == selected_vins``. The run record's
    ``vins_selected`` counts ROWS, which equals the VIN count only when no VIN has
    two unremedied campaigns, so it is not the comparison.
    Every p0 (campaign_id, vin) row must have a ground Finding; no ground Finding
    for any other pair; at least one p0 row must exist (else nothing was checked).

    Only this run's ground items are scored: an item whose ``computed_at`` is before
    the run's ``started_at`` is an earlier run's (every run rewrites its fixed items,
    ``refresh_on_noop``), kept until it expires, and counted as stale. A ground item
    on a p0 pair remedied since the run (``remedied_since``) is neither required nor
    a leak (review rebuild c1 WA1).
    """
    gre = int(run_record.get("grounding_read_errors") or 0)
    errors_ok = selected_vins > 0 and gre == selected_vins

    run_start = _parse_iso_utc(run_record.get("started_at"))
    if run_start is not None:
        # Millisecond started_at vs whole-second computed_at: an item computed in
        # the run's first second is this run's (issue
        # 2026-10-27-fi-sweep-avx-ms-run-start-readers).
        run_start = run_start.replace(microsecond=0)
    ground_pairs: set[tuple[str, str]] = set()
    stale = 0
    for f in recall_findings:
        if f.get("finding_kind") != _RECALL_KIND:
            continue
        sk = str(f.get("subject_key") or "")
        parts = sk.split("/")
        if len(parts) >= 2 and parts[1] == "ground":
            cid = parts[0]
            vin = str((f.get("scope") or {}).get("vin") or "")
            if not (cid and vin):
                continue
            computed = _parse_iso_utc(f.get("computed_at"))
            if run_start is not None and computed is not None and computed < run_start:
                stale += 1
                continue
            ground_pairs.add((cid, vin))

    p0_covered = bool(p0_rows) and p0_rows.issubset(ground_pairs)
    nonp0_leak = sorted(ground_pairs - p0_rows - set(remedied_since))
    ok = errors_ok and p0_covered and not nonp0_leak
    detail = (
        f"grounding_read_errors={gre} selected_vins={selected_vins} ({errors_ok}), "
        f"p0_rows={len(p0_rows)} missing_ground={sorted(p0_rows - ground_pairs)[:3]}, "
        f"non_p0_ground={nonp0_leak[:3]}, remedied_since_run={len(remedied_since)}, "
        f"earlier_runs_items={stale}"
    )
    return ok, detail


def _parse_iso_utc(value: Any) -> dt.datetime | None:
    """An ISO 8601 timestamp (``Z`` or an offset) as an aware UTC datetime, else None."""
    try:
        t = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def score_broker_logs(
    log_events: list[dict[str, Any]],
    recall_kind: str = _RECALL_KIND,
    warranty_kind: str = _WARRANTY_KIND,
    require_non_livetest: bool = False,
) -> tuple[bool, str]:
    """Item 10: broker must log 'fi_item' skip for both recall and warranty kinds.

    Checks for 'fi_item' in the log message alongside each kind string.
    Anti-vacuity: at least one event must be checked (log_events non-empty).
    W9 fix: require_non_livetest=True requires >= 1 event with a non-LIVETEST finding_id.

    Mutant record: removing the warranty_seen check lets a recall-only log pass.
    """
    import re as _re  # noqa: PLC0415
    if not log_events:
        return False, "no log events found (anti-vacuity)"
    recall_seen = warranty_seen = False
    non_livetest_seen = False
    for ev in log_events:
        msg = str(ev.get("message") or "")
        if "fi_item" in msg:
            if recall_kind in msg:
                recall_seen = True
            if warranty_kind in msg:
                warranty_seen = True
            m = _re.search(r"finding_id=(\S+)", msg)
            if m and not m.group(1).startswith("LIVETEST"):
                non_livetest_seen = True
    ok = recall_seen and warranty_seen
    if require_non_livetest and not non_livetest_seen:
        ok = False
    detail = f"recall_seen={recall_seen}, warranty_seen={warranty_seen}"
    if require_non_livetest:
        detail += f", non_livetest_seen={non_livetest_seen}"
    return ok, detail


# ---------------------------------------------------------------------------
# AWS helpers shared across items
# ---------------------------------------------------------------------------


def _athena_run(
    athena: Any,
    sql: str,
    workgroup: str,
    output_location: str,
    poll_interval_s: float = 2.0,
    timeout_s: float = 120.0,
) -> tuple[dict[str, Any], list[list[str]]]:
    """Execute SQL, wait, return (execution_dict, rows_as_string_lists), header excluded.

    output_location is accepted for backward compatibility but NOT passed to Athena;
    the workgroup's own result location is used instead (fix b: passing an explicit
    OutputLocation overrides the workgroup and may land results in an unexpected bucket).
    """
    resp = athena.start_query_execution(
        QueryString=sql,
        WorkGroup=workgroup,
        # No ResultConfiguration / OutputLocation: workgroup result location used.
    )
    eid = resp["QueryExecutionId"]
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qe = athena.get_query_execution(QueryExecutionId=eid)["QueryExecution"]
        state = qe["Status"]["State"]
        if state == "SUCCEEDED":
            break
        if state in ("FAILED", "CANCELLED"):
            raise RuntimeError(
                f"Athena {eid} {state}: {qe['Status'].get('StateChangeReason', '')}"
            )
        if state not in ("RUNNING", "QUEUED"):
            raise RuntimeError(
                f"Athena {eid} unexpected state {state!r}; "
                "expected RUNNING, QUEUED, SUCCEEDED, FAILED, or CANCELLED"
            )
        time.sleep(poll_interval_s)
    else:
        raise TimeoutError(f"Athena query {eid} timed out after {timeout_s}s")

    rows: list[list[str]] = []
    kwargs: dict[str, Any] = {"QueryExecutionId": eid}
    first = True
    while True:
        page = athena.get_query_results(**kwargs)
        result_rows = page.get("ResultSet", {}).get("Rows", [])
        start = 1 if first else 0
        for row in result_rows[start:]:
            rows.append([d.get("VarCharValue", "") for d in row.get("Data", [])])
        first = False
        token = page.get("NextToken")
        if not token:
            break
        kwargs["NextToken"] = token
    return qe, rows


def _scan_findings_by_fleet(
    ddb: Any, table_name: str, fleet_id: str, agent_id: str
) -> list[dict[str, Any]]:
    """Query by_fleet GSI for fleet_id filtered by agent_id; returns all items."""
    from boto3.dynamodb.conditions import Attr, Key  # noqa: PLC0415
    table = ddb.Table(table_name)
    items: list[dict[str, Any]] = []
    kw: dict[str, Any] = {
        "IndexName": "by_fleet",
        "KeyConditionExpression": Key("fleet_id").eq(fleet_id),
        "FilterExpression": Attr("agent_id").eq(agent_id),
    }
    while True:
        page = table.query(**kw)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            break
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return items


def _latest_completed_run_record(ddb: Any, table_name: str, pk: str) -> dict[str, Any] | None:
    """Return the newest ``completed`` run record for agent_component pk (paged,
    newest first); a failed invoke leaves ``aborted`` records that must be skipped."""
    from boto3.dynamodb.conditions import Key  # noqa: PLC0415
    table = ddb.Table(table_name)
    kw: dict[str, Any] = {"KeyConditionExpression": Key("agent_component").eq(pk),
                          "ScanIndexForward": False}
    while True:
        resp = table.query(**kw)
        for item in resp.get("Items", []):
            if item.get("status") == "completed":
                return item
        if not resp.get("LastEvaluatedKey"):
            return None
        kw["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _run_started_ats(runs_table: Any, pk: str, since_iso: str) -> set[str]:
    """``started_at`` of every run record for ``pk`` at or after ``since_iso`` (paged).

    Taken just before an invoke, so the poll can tell this run's record from one a
    scheduled run wrote in the same second.
    """
    from boto3.dynamodb.conditions import Key  # noqa: PLC0415
    out: set[str] = set()
    kw: dict[str, Any] = {
        "KeyConditionExpression": (Key("agent_component").eq(pk)
                                   & Key("started_at").gte(_started_at_lower_bound(since_iso))),
        "ProjectionExpression": "started_at",
    }
    while True:
        page = runs_table.query(**kw)
        out.update(str(i.get("started_at") or "") for i in page.get("Items", []))
        if not page.get("LastEvaluatedKey"):
            return out
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _poll_run_to_completion(
    runs_table: Any, pk: str, invoke_iso: str, timeout_s: float = 1800, poll_s: int = 10,
    exclude: set[str] | None = None,
) -> dict[str, Any] | None:
    """Poll the agent-runs table until a completed/aborted record appears >= invoke_iso.

    ``invoke_iso`` is taken BEFORE the invoke (the run may start, and stamp its
    ``started_at``, before the async invoke call returns); ``exclude`` holds the
    ``started_at`` values that already existed then, so an earlier run is never taken
    for this one. Returns the run record dict or None on timeout.
    """
    from boto3.dynamodb.conditions import Key  # noqa: PLC0415
    exclude = exclude or set()
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        resp = runs_table.query(
            KeyConditionExpression=(
                Key("agent_component").eq(pk)
                & Key("started_at").gte(_started_at_lower_bound(invoke_iso))
            ),
            ScanIndexForward=False,
            Limit=5,
        )
        items = resp.get("Items", [])
        if not isinstance(items, list):
            # Mock or unexpected response type — stop immediately
            return None
        for item in items:
            if str(item.get("started_at") or "") in exclude:
                continue
            if item.get("status") in ("completed", "aborted"):
                return item
        time.sleep(poll_s)
    return None


def _run_completed(run_rec: dict[str, Any]) -> tuple[bool, str]:
    """A run passed only when its record says ``completed``. Aborted records carry no
    ``abort_reason`` field (live, 2026-10-09), so an absent reason proves nothing."""
    status = run_rec.get("status")
    ok = status == "completed" and run_rec.get("abort_reason") is None
    return ok, f"status={status!r} abort_reason={run_rec.get('abort_reason')!r}"


# ---------------------------------------------------------------------------
# Item stubs (each records SKIP 'not built yet')
# ---------------------------------------------------------------------------


def item1(res: Results, *, stage: str, account: str, s3: Any, athena: Any, **kw: Any) -> None:
    """Read-only: compare recall_vehicle_status view to the answer key per campaign."""
    import tempfile as _tmp  # noqa: PLC0415
    import duckdb as _dk  # noqa: PLC0415

    bucket = f"adp-{stage}-foundation-lake-{account}-{_ADP_REGION}"
    prefix = "restricted/recall_campaigns/answer_key/"

    # Read parquet for anti-vacuity + campaign_summary rows
    try:
        data = s3.get_object(
            Bucket=bucket, Key=prefix + "recall_plants_answer_key.parquet"
        )["Body"].read()
        tf = _tmp.NamedTemporaryFile(suffix=".parquet", delete=False)
        try:
            tf.write(data); tf.flush(); tf.close()
            conn = _dk.connect()
            raw = conn.execute("SELECT * FROM read_parquet(?)", [tf.name]).fetchall()
            cols = [d[0] for d in conn.description]
            answer_key_raw = [dict(zip(cols, r)) for r in raw]
        finally:
            os.unlink(tf.name)
    except Exception as exc:  # noqa: BLE001
        res.check("1-s3-read", False, str(exc)[:120])
        return

    recall_rows = [r for r in answer_key_raw if r.get("entity_kind") == "recall_campaign"]
    if not res.check("1-answer-key-non-empty", bool(recall_rows), f"rows={len(recall_rows)}"):
        return

    # Per-campaign answer key from the campaign_summary rows (interface.md § 8c: one per
    # received campaign). Without them the counts cannot be compared: FAIL, don't guess.
    summary_rows = [r for r in answer_key_raw if r.get("entity_kind") == "campaign_summary"]
    if not res.check("1-answer-key-summaries", bool(summary_rows),
                     f"campaign_summary rows={len(summary_rows)}"):
        return
    answer_key_rows = [
        {"campaign_id": str(r.get("entity_id") or ""),
         **_parse_campaign_summary(str(r.get("notes") or ""))}
        for r in summary_rows if r.get("entity_id")
    ]

    # Query Athena view for per-VIN confirmed / population / remedied + managed_as_of
    # W1(d): per-VIN rows so score_recall_views can compare (campaign, VIN) sets.
    # W1(e): managed_as_of filters out VINs not yet managed.
    # Column names: campaign_id, vin, match_basis, remedied, managed_as_of
    # (verified via glue get-table --region us-east-1 --database-name adp_staging_fleet_recall
    #  --name recall_vehicle_status 2026-10-11; vin not vehicle_vin)
    workgroup = f"cvx-{stage}-analytics"
    out_loc = f"s3://{bucket}/live-verify-temp/"
    sql = (
        f"SELECT campaign_id, vin, match_basis, "
        f"CAST(remedied AS VARCHAR) AS remedied, remedy_ref, "
        f"CAST(managed_as_of AS VARCHAR) AS managed_as_of "
        f"FROM adp_{stage}_fleet_recall.recall_vehicle_status"
    )
    try:
        _, vrows = _athena_run(athena, sql, workgroup, out_loc)
    except Exception as exc:  # noqa: BLE001
        res.check("1-athena", False, str(exc)[:120])
        return

    view_rows = [
        {"campaign_id": r[0], "vin": r[1], "match_basis": r[2], "remedied": r[3],
         "remedy_ref": r[4], "managed_as_of": r[5]}
        for r in vrows
    ]
    # managed_as_of is NOT NULL (interface.md § 3) and one snapshot for every row.
    snaps = {r["managed_as_of"] for r in view_rows}
    res.check("1-managed-as-of", bool(view_rows) and len(snaps) == 1 and "" not in snaps,
              f"managed_as_of values={sorted(snaps)[:3]}")

    # Remedies by a DMS repair order (rule a) are not plants: find which remedy_refs
    # are DMS ro_ids (one GetItem each; only remedied rows have a remedy_ref).
    ddb = kw.get("ddb")
    dms_refs: set[str] = set()
    try:
        ro_table = ddb.Table(f"dms-{stage}-repair-orders")
        for ref in {r["remedy_ref"] for r in view_rows if r["remedied"] == "true" and r["remedy_ref"]}:
            if ro_table.get_item(Key={"ro_id": ref}).get("Item"):
                dms_refs.add(ref)
    except Exception as exc:  # noqa: BLE001
        res.check("1-dms-remedy-read", False, str(exc)[:120])
        return
    res.note("1-dms-path-remedies", str(len(dms_refs)))
    ok, detail = score_recall_views(view_rows, answer_key_rows, dms_refs)
    res.check("1-view-vs-answer-key", ok, detail)


def item2(  # noqa: C901
    res: Results,
    *,
    stage: str,
    region: str,
    account: str,
    lam: Any,
    s3: Any,
    athena: Any,
    ddb: Any,
    run_records: dict[str, Any],
    **kw: Any,
) -> None:
    """Async invoke recall + warranty; score Findings against catalog and seed."""
    bucket = f"adp-{stage}-foundation-lake-{account}-{_ADP_REGION}"
    fn_name = f"vsa-{stage}-tier2-health-agent-{region}"
    runs_table_name = f"vsa-{stage}-avx-agent-runs"
    findings_table = f"vsa-{stage}-avx-findings"
    workgroup = f"cvx-{stage}-analytics"
    out_loc = f"s3://{bucket}/live-verify-temp/"
    managed_fleets = [
        "demo-rental-carshare", "demo-lastmile-delivery",
        "demo-field-service", "demo-city-public",
    ]

    # Read warranty seed JSONL once
    try:
        seed_raw = s3.get_object(
            Bucket=bucket,
            Key="restricted/recall_campaigns/answer_key/recall_plants_dms_seed.jsonl",
        )["Body"].read().decode()
        seed_lines = [json.loads(ln) for ln in seed_raw.splitlines() if ln.strip()]
    except Exception as exc:  # noqa: BLE001
        res.check("2-seed-read", False, str(exc)[:120])
        return

    # Recall catalog: {campaign_id: {summary, consequence, remedy}}
    catalog: dict[str, dict[str, str]] = {}
    try:
        _, cat_rows = _athena_run(
            athena,
            f"SELECT campaign_id, summary, consequence, remedy "
            f"FROM adp_{stage}_fleet_recall.recall_campaigns_received",
            workgroup, out_loc,
        )
        for r in cat_rows:
            catalog[r[0]] = {"summary": r[1], "consequence": r[2], "remedy": r[3]}
    except Exception as exc:  # noqa: BLE001
        res.check("2-catalog-query", False, str(exc)[:120])
        return

    fn_arn_2 = f"arn:aws:lambda:{region}:{account}:function:{fn_name}"
    for component, pk in [
        (_RECALL_COMPONENT, _RUN_PK_RECALL),
        (_WARRANTY_COMPONENT, _RUN_PK_WARRANTY),
    ]:
        # A5/B2: refuse if guardrail blank or scheduler window active
        gl_ok, gl_msg = _check_guardrail_live(lam, fn_name)
        if not gl_ok:
            res.check(f"2-{component}-guardrail", False, gl_msg)
            continue
        sc_ok, sc_msg = _check_scheduler_timing(kw.get("sched"), fn_arn_2)
        if not sc_ok:
            res.check(f"2-{component}-sched-timing", False, sc_msg)
            continue
        runs_table = ddb.Table(runs_table_name)
        invoke_iso = _iso(_now())  # before the invoke: the run can start before it returns
        existing = _run_started_ats(runs_table, pk, invoke_iso)
        try:
            lam.invoke(
                FunctionName=fn_name,
                InvocationType="Event",
                Payload=json.dumps({"stage": stage, "component": component}).encode(),
            )
        except Exception as exc:  # noqa: BLE001
            res.check(f"2-{component}-invoke", False, str(exc)[:120])
            continue

        run_rec = _poll_run_to_completion(runs_table, pk, invoke_iso, timeout_s=1800,
                                          exclude=existing)
        if not res.check(
            f"2-{component}-poll",
            run_rec is not None,
            "timeout" if run_rec is None else f"status={run_rec.get('status')}",
        ):
            continue
        assert run_rec is not None  # mypy

        done, done_detail = _run_completed(run_rec)
        if not res.check(f"2-{component}-completed", done, done_detail):
            continue
        run_records[component] = (run_rec, invoke_iso)

        if component == _RECALL_COMPONENT:
            # Check disclosure_txt for all managed-fleet recall Findings
            all_recall: list[dict[str, Any]] = []
            for fid in managed_fleets:
                all_recall.extend(
                    _scan_findings_by_fleet(ddb, findings_table, fid, _AGENT_FI_RW)
                )
            recall_fs = [f for f in all_recall if f.get("finding_kind") == _RECALL_KIND]
            ok_d, _, _, detail_d = score_recall_disclosure(recall_fs, catalog)
            res.check(
                "2-recall-disclosure-txt",
                ok_d,
                detail_d,
            )
        else:
            # Score warranty Findings vs seed JSONL
            warranty_fs: list[dict[str, Any]] = []
            for fid in managed_fleets:
                warranty_fs.extend(
                    _scan_findings_by_fleet(ddb, findings_table, fid, _AGENT_FI_RW)
                )
            ok, *_, detail = score_warranty_run(seed_lines, warranty_fs)
            res.check("2-warranty-score", ok, detail)


def item3(
    res: Results,
    *,
    stage: str,
    region: str,
    lam: Any,
    ddb: Any,
    run_records: dict[str, Any],
    **kw: Any,
) -> None:
    """Re-run recall + warranty; verify artifacts_written=0, model_calls=0, tokens_used=0."""
    fn_name = f"vsa-{stage}-tier2-health-agent-{region}"
    runs_table_name = f"vsa-{stage}-avx-agent-runs"
    findings_table = f"vsa-{stage}-avx-findings"
    managed_fleets = [
        "demo-rental-carshare", "demo-lastmile-delivery",
        "demo-field-service", "demo-city-public",
    ]

    # W8: snapshot per-kind Finding counts and finding ids before any re-run
    before_counts: dict[str, int] = {}
    before_ids: set[str] = set()
    for flt in managed_fleets:
        for f in _scan_findings_by_fleet(ddb, findings_table, flt, _AGENT_FI_RW):
            k = str(f.get("finding_kind") or "other")
            before_counts[k] = before_counts.get(k, 0) + 1
            before_ids.add(str(f.get("finding_id") or ""))

    fn_name_3 = f"vsa-{stage}-tier2-health-agent-{region}"
    fn_arn_3 = f"arn:aws:lambda:{region}:{kw.get('account', '')}:function:{fn_name_3}"
    completed_reruns = 0
    for component, pk in [
        (_RECALL_COMPONENT, _RUN_PK_RECALL),
        (_WARRANTY_COMPONENT, _RUN_PK_WARRANTY),
    ]:
        # A5/B2: refuse if guardrail blank or scheduler window active
        gl_ok, gl_msg = _check_guardrail_live(lam, fn_name_3)
        if not gl_ok:
            res.check(f"3-{component}-guardrail", False, gl_msg)
            continue
        sc_ok, sc_msg = _check_scheduler_timing(kw.get("sched"), fn_arn_3)
        if not sc_ok:
            res.check(f"3-{component}-sched-timing", False, sc_msg)
            continue
        runs_table = ddb.Table(runs_table_name)
        invoke_iso = _iso(_now())  # before the invoke: the run can start before it returns
        existing = _run_started_ats(runs_table, pk, invoke_iso)
        try:
            lam.invoke(
                FunctionName=fn_name,
                InvocationType="Event",
                Payload=json.dumps({"stage": stage, "component": component}).encode(),
            )
        except Exception as exc:  # noqa: BLE001
            res.check(f"3-{component}-invoke", False, str(exc)[:120])
            continue

        run_rec = _poll_run_to_completion(runs_table, pk, invoke_iso, timeout_s=1800,
                                          exclude=existing)
        if not res.check(
            f"3-{component}-poll",
            run_rec is not None,
            "timeout" if run_rec is None else f"status={run_rec.get('status')}",
        ):
            continue
        done, done_detail = _run_completed(run_rec or {})
        if not res.check(f"3-{component}-completed", done, done_detail):
            continue
        completed_reruns += 1

        ok, detail = score_rerun(run_rec or {})
        res.check(f"3-{component}-idempotent", ok, detail)

    # W8: compare finding ids and counts after both re-runs
    after_counts: dict[str, int] = {}
    after_ids: set[str] = set()
    for flt in managed_fleets:
        for f in _scan_findings_by_fleet(ddb, findings_table, flt, _AGENT_FI_RW):
            k = str(f.get("finding_kind") or "other")
            after_counts[k] = after_counts.get(k, 0) + 1
            after_ids.add(str(f.get("finding_id") or ""))
    # A refused or aborted re-run leaves the table untouched; these two rows vouch for
    # idempotency only after a re-run actually completed (review rebuild c1, part A).
    if completed_reruns == 0:
        for lbl in ("3-no-new-findings", "3-finding-counts"):
            res.check(lbl, False, "no completed re-run (nothing tested)")
        return
    ok_new, detail_new = score_new_findings(before_ids, after_ids)
    res.check("3-no-new-findings", ok_new and bool(before_ids),
              detail_new if before_ids else "no FI Findings before the re-run (nothing to compare)")
    ok_fc, detail_fc = score_finding_counts(before_counts, after_counts)
    res.check("3-finding-counts", ok_fc, detail_fc)


def _delete_5b_ro_impl(ddb: Any, stage: str, ro_id: str, action_id: str) -> None:
    """Delete the 5b non-FI RO guarded by avx_action_id (bare UUID) AND status=Closed.

    Extracted from the _delete_5b_ro teardown closure so that tests can drive
    this logic with a recording fake table — closing mutant-3.

    DMS stores the bare UUID (dms.py:95-104 strips the AVX-ACT- prefix before
    writing), so :aid must be the bare UUID, not the full AVX-ACT- form.
    The condition also guards that the RO is Closed, preventing accidental
    deletion of a Draft or Ready RO left by a failed run.
    """
    bare_aid = action_id.removeprefix("AVX-ACT-")
    ddb.Table(f"dms-{stage}-repair-orders").delete_item(
        Key={"ro_id": ro_id},
        ConditionExpression="avx_action_id = :aid AND #s = :closed",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":aid": bare_aid,
            ":closed": "Closed",
        },
    )


def item5(  # noqa: C901
    res: Results,
    *,
    run: str,
    stage: str,
    region: str,
    ddb: Any,
    lam: Any,
    api: Http,
    dms_api: Http,
    pool_id: str | None,
    client_id: str | None,
    idp: Any,
    manifest: Path,
    teardown: _TeardownRegistry,
    dms_repo: str,
    run_records: dict[str, Any],
    backups_ok: bool,
    **kw: Any,
) -> None:
    """Cancel path — 5a (managed) then 5b (LIVETEST non-FI).

    5a: approve a recall book Finding -> DMS RO Draft; PATCH Closed -> Action cancelled.
    5b: plant fleet.health.service LIVETEST Finding, approve (book_fleet_service) ->
        non-FI RO Draft; close -> Action cancelled; teardown deletes F/A/RO.
    """
    findings_table = f"vsa-{stage}-avx-findings"
    actions_table = f"vsa-{stage}-avx-actions"

    if pool_id is None or client_id is None:
        res.skip("5", "Cognito pool/client not available")
        return

    # W3: use fleet A pre-computed in main(); fall back to local computation if absent
    fleet_a: str = kw.get("fleet_a") or ""
    fleet_a_findings: list[dict[str, Any]] = list(kw.get("fleet_a_book_findings") or [])
    if not fleet_a:
        now_iso = _iso(_now())
        fleet_book_counts: dict[str, int] = {}
        fleet_book_findings_map: dict[str, list[dict[str, Any]]] = {}
        for flt in _MANAGED_FLEETS:
            fs = _scan_findings_by_fleet(ddb, findings_table, flt, _AGENT_FI_RW)
            pending_books = [
                f for f in fs
                if f.get("finding_kind") == _RECALL_KIND
                and "/book/" in str(f.get("subject_key") or "")
                and (f.get("expires_at") or "9999") > now_iso
            ]
            fleet_book_counts[flt] = len(pending_books)
            fleet_book_findings_map[flt] = pending_books
        fleet_a = max(fleet_book_counts, key=fleet_book_counts.__getitem__)
        fleet_a_findings = fleet_book_findings_map[fleet_a]
    res.note("5-fleet-a", fleet_a)

    # ── scan DMS ROs for the foreign-FI guard and planned-item selection ────
    dms_ros = _scan_all_dms_ros(ddb, stage)

    # ── 5a: managed cancel ──────────────────────────────────────────────────
    # W3: 5a takes the "next" book item — item 4 takes the lowest, 5a takes the
    # second-lowest.  Peek at item 4's planned item and exclude its VIN.
    # W2: collect existing action finding_ids so declined/pending Findings are skipped;
    # use the same set for both the peek and the real pick so they agree.
    # W6: one GetItem per candidate on its deterministic decision Action; a read error
    # FAILs the item instead of treating the Finding as never decided.
    try:
        existing_action_fids_5 = _decided_finding_ids(
            ddb, actions_table, [str(f.get("finding_id") or "") for f in fleet_a_findings])
        close_early_5 = _early_close_predicate(dms_repo)
    except Exception as exc:  # noqa: BLE001
        res.check("5a-actions-read", False, f"{type(exc).__name__}: {str(exc)[:100]}")
        return
    item4_peek = _pick_book_finding(
        fleet_a_findings, dms_ros, existing_action_finding_ids=existing_action_fids_5,
        skip=close_early_5,
    )
    item4_vin_peek: set[str] = set()
    if item4_peek is not None:
        peek_vin = str((item4_peek.get("scope") or {}).get("vin") or "")
        if peek_vin:
            item4_vin_peek.add(peek_vin)
    planned_5a = _pick_book_finding(
        fleet_a_findings, dms_ros, exclude_vins=item4_vin_peek,
        existing_action_finding_ids=existing_action_fids_5,
    )
    if planned_5a is None:
        res.check("5a-planned-item", False, "no eligible recall book Finding in fleet A")
        # fall through to 5b
    else:
        planned_vin_5a = str((planned_5a.get("scope") or {}).get("vin") or "")
        planned_sk_5a = str(planned_5a.get("subject_key") or "")
        planned_id_5a = str(planned_5a.get("finding_id") or "")
        planned_cid_5a = planned_sk_5a.split("/")[0] if "/" in planned_sk_5a else ""
        planned_rec_5a = (planned_5a.get("recommended_action") or {}).get("parameters") or {}
        dealer_id_5a = str(planned_rec_5a.get("dealer_id") or "")

        # Print + write to manifest BEFORE approve (safety rail)
        plan_record_5a: dict[str, Any] = {
            "finding_id": planned_id_5a,
            "vin": planned_vin_5a,
            "campaign_id": planned_cid_5a,
            "subject_key": planned_sk_5a,
            "dealer_id": dealer_id_5a,
            "purpose": "item5a-cancel-path",
        }
        print(f"  5a planned item: {plan_record_5a}")
        _update_manifest(manifest, "item5a_planned", plan_record_5a)

        with principal(
            idp, pool_id, client_id, "U_A-5a", ["fleet-operator"],
            fleet_ids=fleet_a,
        ) as (tok_a, _):
            # Approve the book Finding
            approve_body: dict[str, Any] = {
                "action_kind": "book_recall_service",
                "target_system": "dms",
                "parameters": {"dealer_id": dealer_id_5a},
            }
            status, body = api.call(
                "POST", f"/findings/{planned_id_5a}/approve", f"Bearer {tok_a}", approve_body
            )
            if not res.check("5a-approve", status == 200, f"HTTP {status}: {body}"):
                pass  # continue to 5b even on failure
            else:
                action_id_5a = str(body.get("action_id") or "")  # handler.py:1448 top-level
                _update_manifest(manifest, "item5a_action_id", action_id_5a)

                # Derive DMS ro_id: poll AVX actions table for in_progress (target_ref set by handler.py:841)
                ro_id_5a: str | None = None
                deadline = time.time() + 60
                while time.time() < deadline:
                    resp = ddb.Table(actions_table).get_item(
                        Key={"action_id": action_id_5a}
                    )
                    act = resp.get("Item") or {}
                    if act.get("target_ref"):
                        ro_id_5a = str(act["target_ref"])
                        break
                    time.sleep(3)

                if not res.check("5a-ro-id", ro_id_5a is not None, f"ro_id={ro_id_5a!r}", on_fail="Action has no ro_id"):
                    pass
                else:
                    _update_manifest(manifest, "item5a_ro_id", ro_id_5a)
                    # Get RO to find dealer_id for service-advisor scope
                    ro_item_5a = (
                        ddb.Table(f"dms-{stage}-repair-orders")
                        .get_item(Key={"ro_id": ro_id_5a})
                        .get("Item") or {}
                    )
                    ro_dealer_5a = str(ro_item_5a.get("dealer_id") or dealer_id_5a)

                    # W5: assert RO is Draft before closing (Accept: "closed from Draft")
                    ro_status_5a = str(ro_item_5a.get("status") or "")
                    res.check("5a-ro-draft", ro_status_5a == "Draft",
                              f"RO status={ro_status_5a!r} before close (expected Draft)")

                    # Create service-advisor U_S, close the RO from Draft
                    with principal(
                        idp, pool_id, client_id, "U_S-5a", ["service-advisor"],
                        dealer_ids=ro_dealer_5a,
                    ) as (tok_s, _):
                        patch_status, patch_body = dms_api.call(
                            "PATCH",
                            _DMS_RO_PATCH_PATH.format(dealer_id=ro_dealer_5a, ro_id=ro_id_5a),
                            f"Bearer {tok_s}",
                            {"status": "Closed"},
                        )
                        res.check("5a-patch-close", patch_status == 200,
                                  f"HTTP {patch_status}: {patch_body}")

                    # Poll Action for 'cancelled'
                    action_cancelled = False
                    deadline = time.time() + 90
                    while time.time() < deadline:
                        resp = ddb.Table(actions_table).get_item(
                            Key={"action_id": action_id_5a}
                        )
                        act = resp.get("Item") or {}
                        if act.get("status") == "cancelled":
                            action_cancelled = True
                            # Verify audit trail
                            trail = act.get("audit_trail") or []
                            cancel_entry = next(
                                (e for e in trail if e.get("by") == "dms-event"), None
                            )
                            res.check(
                                "5a-audit-by-dms-event",
                                cancel_entry is not None,
                                f"trail={[e.get('by') for e in trail]}",
                            )
                            if cancel_entry:
                                res.check(
                                    "5a-audit-note",
                                    cancel_entry.get("note") == "ro_closed_before_work",
                                    f"note={cancel_entry.get('note')!r}",
                                )
                            break
                        time.sleep(4)
                    res.check("5a-action-cancelled", action_cancelled, on_fail="timeout waiting for cancelled")

                    # Check queue status via GET /findings/fleet/{A}
                    # handler.py:2326 returns {"findings": [...], "next_token": ...}
                    # queue_status is derived via compute_queue_status (no field in body)
                    with principal(
                        idp, pool_id, client_id, "U_A-5a-check", ["fleet-operator"],
                        fleet_ids=fleet_a,
                    ) as (tok_check, _):
                        # W5: paginate all pages; FAIL if finding not found after all pages
                        finding_5a_item: dict[str, Any] | None = None
                        q_check_path = f"/findings/fleet/{fleet_a}"
                        q_check_params: dict[str, Any] = {}
                        q_ok = True
                        pages_5a = 0
                        while True:
                            pages_5a += 1
                            if pages_5a > 200:  # the route restarts on a bad cursor
                                res.check("5a-queue-get", False, "more than 200 pages; stopped")
                                q_ok = False
                                break
                            qs = urllib.parse.urlencode(q_check_params)
                            url = f"{q_check_path}?{qs}" if qs else q_check_path
                            q_status_5a, q_body = api.call("GET", url, f"Bearer {tok_check}")
                            if q_status_5a != 200:
                                res.check("5a-queue-get", False,
                                          f"GET /findings/fleet/{fleet_a} HTTP {q_status_5a}")
                                q_ok = False
                                break
                            findings_list = q_body.get("findings") or []
                            hit = next(
                                (f for f in findings_list
                                 if str(f.get("finding_id") or "") == planned_id_5a),
                                None,
                            )
                            if hit is not None:
                                finding_5a_item = hit
                                break
                            next_tok = q_body.get("next_token")
                            if not next_tok:
                                break
                            q_check_params = {"next_token": next_tok}
                        if q_ok:
                            if finding_5a_item is not None:
                                from agents.supervisor.tools.fleet_recommendation import (  # noqa: PLC0415
                                    compute_queue_status as _cqs_5a,
                                )
                                act_r = ddb.Table(actions_table).get_item(
                                    Key={"action_id": action_id_5a}
                                )
                                act_for_status = act_r.get("Item")
                                queue_st = _cqs_5a(finding_5a_item, act_for_status)
                                res.check("5a-queue-cancelled", queue_st == "cancelled",
                                          f"queue_status={queue_st!r}")
                            else:
                                # W5: finding absent after all pages -> FAIL
                                res.check("5a-queue-cancelled", False,
                                          f"finding {planned_id_5a!r} not found in any page of "
                                          f"GET /findings/fleet/{fleet_a}")

                    # Invoke recall on-demand; poll for /book/2 Finding for that VIN+campaign
                    fn_name = f"vsa-{stage}-tier2-health-agent-{region}"
                    fn_arn_5a = (
                        f"arn:aws:lambda:{region}:{kw.get('account', '')}:function:{fn_name}"
                    )
                    _gl_ok, _gl_msg = _check_guardrail_live(lam, fn_name)
                    _sc_ok, _sc_msg = _check_scheduler_timing(kw.get("sched"), fn_arn_5a)
                    if not _gl_ok:
                        res.check("5a-guardrail", False, _gl_msg)
                    elif not _sc_ok:
                        res.check("5a-sched-timing", False, _sc_msg)
                    else:
                        runs_table = ddb.Table(f"vsa-{stage}-avx-agent-runs")
                        # before the invoke: the run can start before it returns
                        invoke_iso_5a = _iso(_now())
                        existing_5a = _run_started_ats(runs_table, _RUN_PK_RECALL, invoke_iso_5a)
                        invoked_5a = False
                        try:
                            lam.invoke(
                                FunctionName=fn_name,
                                InvocationType="Event",
                                Payload=json.dumps({"stage": stage, "component": "recall"}).encode(),
                            )
                            invoked_5a = True
                        except Exception as exc:  # noqa: BLE001
                            res.check("5a-recall-invoke", False, str(exc)[:80])
                        if invoked_5a:
                            run_rec_5a = _poll_run_to_completion(
                                runs_table, _RUN_PK_RECALL, invoke_iso_5a, timeout_s=1800,
                                exclude=existing_5a,
                            )
                            done_5a, done_detail_5a = (
                                _run_completed(run_rec_5a) if run_rec_5a else (False, "timeout"))
                            # W14: an aborted run is a FAIL, and /book/2 is then not looked for
                            res.check("5a-recall-run", done_5a, done_detail_5a)
                            if done_5a:
                                # Check for a /book/2 Finding for that VIN + campaign
                                new_fs = _scan_findings_by_fleet(
                                    ddb, findings_table, fleet_a, _AGENT_FI_RW
                                )
                                book2_found = any(
                                    f for f in new_fs
                                    if f.get("finding_kind") == _RECALL_KIND
                                    and str((f.get("scope") or {}).get("vin") or "") == planned_vin_5a
                                    and str(f.get("subject_key") or "").startswith(planned_cid_5a + "/book/2")
                                )
                                res.check(
                                    "5a-book2-finding",
                                    book2_found,
                                    on_fail=f"no /book/2 Finding for vin={planned_vin_5a!r} cid={planned_cid_5a!r}",
                                )

    # ── 5b: LIVETEST non-FI cancel ──────────────────────────────────────────
    # Plant a fleet.health.service Finding (book_fleet_service recommendation).
    # fleet.health.service accepts book_fleet_service per handler.py:335 (_OPERATIONAL_ACTION_KINDS)
    # and policy.py:125. It is non-FI: no recall line, so is_fi_booking=False on the created RO.
    finding_id_5b = f"LIVETEST-FI-RW-{run}-b5"
    action_id_5b: str | None = None
    ro_id_5b: str | None = None

    # Pick a VIN from fleet A for 5b (any VIN, prefer one different from 5a's)
    vin_5b: str | None = None
    if fleet_a_findings:
        exclude_vin_5b = {str((planned_5a.get("scope") or {}).get("vin") or "")} if planned_5a else set()
        for f in fleet_a_findings:
            v = str((f.get("scope") or {}).get("vin") or "")
            if v and v not in exclude_vin_5b:
                vin_5b = v
                break
        if vin_5b is None and fleet_a_findings:
            vin_5b = str((fleet_a_findings[0].get("scope") or {}).get("vin") or "")

    # Pick a dealer_id from a DMS rooftop
    rooftop_dealer: str | None = None
    try:
        rt_resp = ddb.Table(f"dms-{stage}-rooftops").scan(Limit=1)
        rt_items = rt_resp.get("Items") or []
        if rt_items:
            rooftop_dealer = str(rt_items[0].get("dealer_id") or "")
    except Exception:  # noqa: BLE001
        pass

    if vin_5b is None:
        res.check("5b-vin", False, "no VIN available from fleet A")
        return
    if rooftop_dealer is None:
        res.check("5b-dealer", False, "no rooftop dealer found in DMS")
        return

    # Plant Finding BEFORE registering teardown (teardown registered below)
    # Actually: register teardown BEFORE creation (safety rail)
    def _delete_5b_finding() -> None:
        ddb.Table(findings_table).delete_item(
            Key={"finding_id": finding_id_5b},
            ConditionExpression="begins_with(finding_id, :pfx)",
            ExpressionAttributeValues={":pfx": "LIVETEST-"},
        )
        print(f"  teardown: deleted Finding {finding_id_5b}")

    def _delete_5b_action() -> None:
        if action_id_5b is None:
            return
        ddb.Table(actions_table).delete_item(Key={"action_id": action_id_5b})
        print(f"  teardown: deleted Action {action_id_5b}")

    def _delete_5b_ro() -> None:
        if ro_id_5b is None:
            return
        _delete_5b_ro_impl(ddb, stage, ro_id_5b, action_id_5b or "")
        print(f"  teardown: deleted RO {ro_id_5b}")

    # Register teardowns BEFORE creation (safety rail)
    teardown.register(_delete_5b_ro)
    teardown.register(_delete_5b_action)
    teardown.register(_delete_5b_finding)

    # Plant the LIVETEST Finding
    now_dt = _now()
    finding_5b: dict[str, Any] = {
        "finding_id": finding_id_5b,
        "finding_kind": "fleet.health.service",  # accepts book_fleet_service
        "fleet_id": fleet_a,
        "agent_id": _AGENT_FI_RW,
        "scope": {"vin": vin_5b},
        "severity": "p1",
        "headline": f"LIVETEST {finding_id_5b}: fleet-service booking",
        "subject_key": f"LIVETEST-5b/{run}/1",
        "expires_at": _iso(now_dt + dt.timedelta(days=7)),
        "created_at": _iso(now_dt),
        "updated_at": _iso(now_dt),
        "recommended_action": {
            "action_kind": "book_fleet_service",
            "target_system": "dms",
            "parameters": {
                "vehicle_vin": vin_5b,
                "dealer_id": rooftop_dealer,
                # handler.py:978-992: needs must be a non-empty list of {labor_op} objects
                "needs": [{"labor_op": "routine-service"}],
            },
        },
    }
    try:
        ddb.Table(findings_table).put_item(Item=finding_5b)
        print(f"  5b: planted Finding {finding_id_5b} (fleet.health.service, book_fleet_service)")
        _update_manifest(manifest, "item5b_finding_id", finding_id_5b)
    except Exception as exc:  # noqa: BLE001
        res.check("5b-plant", False, str(exc)[:120])
        return

    with principal(
        idp, pool_id, client_id, "U_A-5b", ["fleet-operator"],
        fleet_ids=fleet_a,
    ) as (tok_a_5b, _):
        approve_body_5b: dict[str, Any] = {
            "action_kind": "book_fleet_service",
            "target_system": "dms",
            "parameters": {"dealer_id": rooftop_dealer},
        }
        status_5b, body_5b = api.call(
            "POST", f"/findings/{finding_id_5b}/approve",
            f"Bearer {tok_a_5b}", approve_body_5b,
        )
        if not res.check("5b-approve", status_5b == 200, f"HTTP {status_5b}: {body_5b}"):
            return

        action_id_5b = str(body_5b.get("action_id") or "")  # handler.py:1448 top-level
        _update_manifest(manifest, "item5b_action_id", action_id_5b)

        # Wait for in_progress (Draft RO created) and get ro_id via target_ref (handler.py:841)
        deadline = time.time() + 60
        while time.time() < deadline:
            resp = ddb.Table(actions_table).get_item(Key={"action_id": action_id_5b})
            act = resp.get("Item") or {}
            if act.get("target_ref"):
                ro_id_5b = str(act["target_ref"])
                break
            time.sleep(3)

        if not res.check("5b-ro-id", ro_id_5b is not None, f"ro_id={ro_id_5b!r}", on_fail="Action has no ro_id after 60s"):
            return
        _update_manifest(manifest, "item5b_ro_id", ro_id_5b)

        # Verify non-FI: is_fi_booking must be False
        ro_5b = (
            ddb.Table(f"dms-{stage}-repair-orders")
            .get_item(Key={"ro_id": ro_id_5b})
            .get("Item") or {}
        )
        try:
            mod_fi = _load_dms_module(dms_repo, "source/simulation/fi_stand_ins.py")
            is_fi_fn = getattr(mod_fi, "is_fi_booking")
            is_fi_result = is_fi_fn(ro_5b)
            res.check("5b-non-fi", not is_fi_result,
                      "DMS is_fi_booking: no (no recall line), as a non-FI booking must be",
                      on_fail=f"is_fi_booking={is_fi_result!r} (expected False; no recall line)")
        except Exception as exc:  # noqa: BLE001
            res.check("5b-non-fi-check", False, str(exc)[:80])

        ro_dealer_5b = str(ro_5b.get("dealer_id") or rooftop_dealer)
        # W5: assert RO is Draft before closing (Accept: "closed from Draft")
        ro_status_5b = str(ro_5b.get("status") or "")
        res.check("5b-ro-draft", ro_status_5b == "Draft",
                  f"RO status={ro_status_5b!r} before close (expected Draft)")
        with principal(
            idp, pool_id, client_id, "U_S-5b", ["service-advisor"],
            dealer_ids=ro_dealer_5b,
        ) as (tok_s_5b, _):
            patch_st, patch_bd = dms_api.call(
                "PATCH",
                _DMS_RO_PATCH_PATH.format(dealer_id=ro_dealer_5b, ro_id=ro_id_5b),
                f"Bearer {tok_s_5b}",
                {"status": "Closed"},
            )
            res.check("5b-patch-close", patch_st == 200, f"HTTP {patch_st}: {patch_bd}")

        # Poll Action for 'cancelled'
        cancelled_5b = False
        deadline = time.time() + 90
        while time.time() < deadline:
            resp = ddb.Table(actions_table).get_item(Key={"action_id": action_id_5b})
            act = resp.get("Item") or {}
            if act.get("status") == "cancelled":
                cancelled_5b = True
                break
            time.sleep(4)
        res.check("5b-action-cancelled", cancelled_5b, on_fail="timeout waiting for cancelled on 5b")


def item4(  # noqa: C901
    res: Results,
    *,
    run: str,
    stage: str,
    region: str,
    ddb: Any,
    lam: Any,
    api: Http,
    pool_id: str | None,
    client_id: str | None,
    idp: Any,
    manifest: Path,
    teardown: _TeardownRegistry,
    dms_repo: str,
    run_records: dict[str, Any],
    backups_ok: bool,
    **kw: Any,
) -> None:
    """Booking round trip (managed): approve book Finding -> DMS RO -> remedied.

    Planned item: lowest finding_id pending /book/1 recall Finding in fleet A
    whose VIN has no DMS RO with a recall line for that campaign.
    Printed and written to manifest BEFORE approve (safety rail).
    """
    findings_table = f"vsa-{stage}-avx-findings"
    actions_table = f"vsa-{stage}-avx-actions"

    if pool_id is None or client_id is None:
        res.skip("4", "Cognito pool/client not available")
        return

    # W3: use fleet A pre-computed in main(); fall back to local computation if absent
    fleet_a: str = kw.get("fleet_a") or ""
    fleet_a_findings: list[dict[str, Any]] = list(kw.get("fleet_a_book_findings") or [])
    if not fleet_a:
        now_iso = _iso(_now())
        fleet_book_counts_4: dict[str, int] = {}
        fleet_book_findings_4: dict[str, list[dict[str, Any]]] = {}
        for flt in _MANAGED_FLEETS:
            fs = _scan_findings_by_fleet(ddb, findings_table, flt, _AGENT_FI_RW)
            pending_books = [
                f for f in fs
                if f.get("finding_kind") == _RECALL_KIND
                and "/book/" in str(f.get("subject_key") or "")
                and (f.get("expires_at") or "9999") > now_iso
            ]
            fleet_book_counts_4[flt] = len(pending_books)
            fleet_book_findings_4[flt] = pending_books
        fleet_a = max(fleet_book_counts_4, key=fleet_book_counts_4.__getitem__)
        fleet_a_findings = fleet_book_findings_4[fleet_a]
    res.note("4-fleet-a", fleet_a)

    dms_ros = _scan_all_dms_ros(ddb, stage)

    # ── pick planned item ───────────────────────────────────────────────────
    # W2: collect existing action finding_ids so declined/pending Findings are skipped
    # W6: decision-Action GetItem per candidate, failing closed; and skip any Finding
    # whose deterministic RO DMS's stand-in would close early (it never reaches Ready).
    try:
        existing_action_fids_4 = _decided_finding_ids(
            ddb, actions_table, [str(f.get("finding_id") or "") for f in fleet_a_findings])
        close_early_4 = _early_close_predicate(dms_repo)
    except Exception as exc:  # noqa: BLE001
        res.check("4-actions-read", False, f"{type(exc).__name__}: {str(exc)[:100]}")
        return
    planned = _pick_book_finding(
        fleet_a_findings, dms_ros, existing_action_finding_ids=existing_action_fids_4,
        skip=close_early_4,
    )
    if not res.check("4-planned-item", planned is not None,
                     on_fail="no eligible recall book Finding in fleet A"):
        return
    assert planned is not None  # mypy

    planned_vin = str((planned.get("scope") or {}).get("vin") or "")
    planned_sk = str(planned.get("subject_key") or "")
    planned_id = str(planned.get("finding_id") or "")
    planned_cid = planned_sk.split("/")[0] if "/" in planned_sk else ""
    planned_rec = (planned.get("recommended_action") or {}).get("parameters") or {}
    dealer_id = str(planned_rec.get("dealer_id") or "")

    # Print + write to manifest BEFORE approve (safety rail)
    plan_record: dict[str, Any] = {
        "finding_id": planned_id,
        "vin": planned_vin,
        "campaign_id": planned_cid,
        "subject_key": planned_sk,
        "dealer_id": dealer_id,
        "purpose": "item4-booking-round-trip",
    }
    print(f"  4 planned item: {plan_record}")
    _update_manifest(manifest, "item4_planned", plan_record)

    # ── foreign-FI-booking check ────────────────────────────────────────────
    # Collect this run's RO IDs from the manifest (from item 5a if it ran)
    manifest_data = json.loads(manifest.read_text())
    this_run_ro_ids: set[str] = set()
    for key in ("item5a_ro_id", "item5b_ro_id"):
        v = manifest_data.get("items", {}).get(key)
        if v:
            this_run_ro_ids.add(str(v))

    fi_ok, fi_detail = _check_fi_booking_isolation(dms_repo, dms_ros, this_run_ro_ids)
    if not res.check("4-no-foreign-fi", fi_ok, fi_detail):
        return  # FAIL without invoking dealer-progress

    # W14: baseline — the planned (campaign, VIN) must NOT be remedied yet, or the
    # final 4-remedied check would pass without this booking.
    athena_4 = kw.get("athena")
    remedied_sql = (
        f"SELECT CAST(remedied AS VARCHAR) FROM adp_{stage}_fleet_recall.recall_vehicle_status "
        f"WHERE campaign_id='{planned_cid}' AND vin='{planned_vin}'"
    )
    if not (_SAFE_ID_RE.fullmatch(planned_cid) and _VIN_RE.fullmatch(planned_vin)):
        res.check("4-planned-shape", False, f"cid={planned_cid!r} vin={planned_vin!r}")
        return
    try:
        _, base_rows = _athena_run(athena_4, remedied_sql, f"cvx-{stage}-analytics", "")
    except Exception as exc:  # noqa: BLE001
        res.check("4-not-remedied-before", False, f"view read error: {str(exc)[:100]}")
        return
    if not res.check("4-not-remedied-before",
                     len(base_rows) == 1 and base_rows[0][0] == "false",
                     f"view rows for the planned pair: {base_rows}"):
        return

    # ── approve the planned item ─────────────────────────────────────────────
    with principal(
        idp, pool_id, client_id, "U_A-4", ["fleet-operator"],
        fleet_ids=fleet_a,
    ) as (tok_a, _):
        approve_body: dict[str, Any] = {
            "action_kind": "book_recall_service",
            "target_system": "dms",
            "parameters": {"dealer_id": dealer_id},
        }
        status, body = api.call(
            "POST", f"/findings/{planned_id}/approve", f"Bearer {tok_a}", approve_body
        )
        if not res.check("4-approve", status == 200, f"HTTP {status}: {body}"):
            return

        action_id = str(body.get("action_id") or "")  # handler.py:1448 top-level
        _update_manifest(manifest, "item4_action_id", action_id)

        # Poll for DMS ro_id via Action.target_ref (set by handler.py:841 after DMS ACKs)
        ro_id: str | None = None
        deadline = time.time() + 90
        while time.time() < deadline:
            resp = ddb.Table(actions_table).get_item(Key={"action_id": action_id})
            act = resp.get("Item") or {}
            if act.get("target_ref"):
                ro_id = str(act["target_ref"])
                break
            time.sleep(3)
        if not res.check("4-ro-id", ro_id is not None, f"ro_id={ro_id!r}", on_fail="no ro_id on Action after 90s"):
            return
        _update_manifest(manifest, "item4_ro_id", ro_id)

        # Verify DMS RO: avx_action_id, is_fi_booking, pay_type=recall line
        ro_item = (
            ddb.Table(f"dms-{stage}-repair-orders")
            .get_item(Key={"ro_id": ro_id})
            .get("Item") or {}
        )
        res.check("4-ro-avx-action-id", ro_item.get("avx_action_id") == action_id.removeprefix("AVX-ACT-"),
                  f"avx_action_id={ro_item.get('avx_action_id')!r}")
        try:
            mod_fi = _load_dms_module(dms_repo, "source/simulation/fi_stand_ins.py")
            is_fi_fn = getattr(mod_fi, "is_fi_booking")
            is_fi_result = is_fi_fn(ro_item)
            res.check("4-is-fi-booking", is_fi_result,
                      f"is_fi_booking={is_fi_result!r} (expected True)")
            # The pick skipped Findings whose RO would close early; the real RO must agree
            # (its id is DMS's deterministic one for this Action).
            close_early = bool(mod_fi._would_close_early(ro_id))
            res.check("4-not-close-early", not close_early,
                      f"_would_close_early({ro_id!r})={close_early}")
            if close_early:
                _update_manifest(manifest, "item4_close_early", True)
        except Exception as exc:  # noqa: BLE001
            res.check("4-fi-check", False, str(exc)[:80])

        # ── invoke dealer-progress until Ready (max 3) ──────────────────────
        fn_dp = f"dms-{stage}-fi-dealer-progress"
        # Re-scan DMS ROs to include this run's new RO
        dms_ros_now = _scan_all_dms_ros(ddb, stage)
        this_run_ro_ids.add(ro_id)
        fi_ok2, fi_detail2 = _check_fi_booking_isolation(dms_repo, dms_ros_now, this_run_ro_ids)
        if not res.check("4-no-foreign-fi-pre-dp", fi_ok2, fi_detail2):
            return

        ro_ready = False
        for attempt in range(1, 4):
            # Security c1 SW2: the stand-in's own crons (dealer-progress 05:40, the
            # adjudicator 06:00) are inside 04:50-06:10; main checks that window before
            # each item, but item 4 can reach these invokes later. Re-check right here so
            # a script invoke never races the scheduled one on the same open bookings.
            if _in_write_window():
                res.check("4-dp-window", False,
                          "inside 04:50-06:10 UTC (dealer-progress cron 05:40); invoke refused")
                break
            try:
                lam.invoke(
                    FunctionName=fn_dp,
                    InvocationType="RequestResponse",
                    Payload=json.dumps({}).encode(),
                )
            except Exception as exc:  # noqa: BLE001
                res.check(f"4-dp-invoke-{attempt}", False, str(exc)[:80])
                break
            # Check RO status — Ready is the target; Delivered/Closed are later stages
            ro_item = (
                ddb.Table(f"dms-{stage}-repair-orders")
                .get_item(Key={"ro_id": ro_id})
                .get("Item") or {}
            )
            ro_status = ro_item.get("status") or ""
            res.note(f"4-dp-{attempt}-status", ro_status)
            if ro_status == "Ready":
                ro_ready = True
                break
            if ro_status in ("Delivered", "Invoiced", "Closed"):
                # W3: these statuses mean dealer-progress advanced past Ready too quickly.
                # Do not set ro_ready; let 4-ro-ready FAIL and 4-action-completed confirm.
                res.note("4-dp-closed-early", f"RO advanced past Ready to {ro_status!r} at attempt {attempt}")
                break

        # W4: assert RO reached Ready (not just "not Draft")
        res.check("4-ro-ready", ro_ready, "status='Ready'",
                  on_fail=f"RO did not reach Ready after 3 dealer-progress invokes (last status={ro_item.get('status')!r})")  # type: ignore[possibly-undefined]

        # W4: verify the recall line's campaign matches the planned campaign
        ro_lines = (ro_item.get("lines") or []) if ro_item else []  # type: ignore[possibly-undefined]
        recall_lines = [ln for ln in ro_lines if ln.get("pay_type") == "recall"]
        recall_cid_match = any(
            str(ln.get("recall_campaign_id") or "") == planned_cid
            for ln in recall_lines
        )
        res.check("4-ro-recall-cid", recall_cid_match,
                  on_fail=f"no recall line with campaign={planned_cid!r}; "
                  f"lines={[ln.get('recall_campaign_id') for ln in recall_lines]}")

        # Poll Action for 'completed' (max 3 min — DMS event consumer fires on Ready)
        action_completed = False
        deadline = time.time() + 180
        while time.time() < deadline:
            resp = ddb.Table(actions_table).get_item(Key={"action_id": action_id})
            act = resp.get("Item") or {}
            if act.get("status") == "completed":
                action_completed = True
                break
            time.sleep(6)
        res.check("4-action-completed", action_completed, on_fail="timeout waiting for completed")

        # Poll recall_vehicle_status for remedied (up to 45 min)
        remedied = False
        deadline = time.time() + 2700  # 45 min
        bucket = f"adp-{stage}-foundation-lake-{kw.get('account', 'UNKNOWN')}-{_ADP_REGION}"
        workgroup = f"cvx-{stage}-analytics"
        out_loc = f"s3://{bucket}/live-verify-temp/"
        athena = kw.get("athena")
        while athena and time.time() < deadline:
            try:
                _, rows = _athena_run(athena, remedied_sql, workgroup, out_loc)
                if rows and rows[0][0] == "true":
                    remedied = True
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(60)
        if athena:
            res.check("4-remedied", remedied, f"remedied for vin={planned_vin!r} cid={planned_cid!r}",
                      on_fail=f"recall_vehicle_status not remedied for vin={planned_vin!r} cid={planned_cid!r}")


def item6(
    res: Results,
    *,
    stage: str,
    account: str,
    athena: Any,
    ddb: Any,
    run_records: dict[str, Any],
    **kw: Any,
) -> None:
    """Read-only: groundings without CMS's route — every selected VIN a grounding read
    error, a ground item for every p0 unremedied row, none for any other row."""
    recall_pair = run_records.get(_RECALL_COMPONENT)
    run_rec: dict[str, Any] | None = recall_pair[0] if recall_pair else None
    if run_rec is None:
        run_rec = _latest_completed_run_record(ddb, f"vsa-{stage}-avx-agent-runs", _RUN_PK_RECALL)
    if run_rec is None:
        res.check("6-run-record", False, "no completed recall run record found")
        return
    res.note("6-run-record", f"started_at={run_rec.get('started_at')} status={run_rec.get('status')}")

    # The rows the run selected: those unremedied WHEN IT RAN (select_recalls.sql),
    # joined to the catalog for the per-row p0 band (agents.fi_recall_warranty.band).
    # A row remedied on or after the run's date (the 05:40 dealer-progress stand-in,
    # or item 4's own booking) was still selected by the run, so it is read too:
    # its ground item is "remedied since the run", not a leak, and its VIN still
    # counted one grounding read error (review rebuild c1 WA1). remedied_on is a date,
    # so a same-day remedy counts as after the run.
    run_date = str(run_rec.get("started_at") or "")[:10]
    workgroup = f"cvx-{stage}-analytics"
    try:
        from agents.fi_recall_warranty.band import classify as _band_classify  # noqa: PLC0415
        _, cam_rows = _athena_run(
            athena,
            f"SELECT rvs.vin, rvs.campaign_id, rcr.recall_class, "
            f"rcr.do_not_drive, rvs.match_basis, CAST(rvs.remedied AS VARCHAR), "
            f"CAST(rvs.remedied_on AS VARCHAR) "
            f"FROM adp_{stage}_fleet_recall.recall_vehicle_status rvs "
            f"JOIN adp_{stage}_fleet_recall.recall_campaigns_received rcr "
            f"ON rvs.campaign_id = rcr.campaign_id",
            workgroup, "",
        )
        p0_rows: set[tuple[str, str]] = set()
        p0_remedied_since: set[tuple[str, str]] = set()
        vins: set[str] = set()
        for row in cam_rows:
            vin, cid, recall_class, dnd_raw, match_basis, remedied_raw, remedied_on = (
                row[0], row[1], row[2], row[3], row[4], row[5], row[6]
            )
            if not cid or not vin:
                continue
            remedied = str(remedied_raw).strip().lower() == "true"
            since_run = remedied and bool(run_date) and str(remedied_on or "")[:10] >= run_date
            if remedied and not since_run:
                continue  # remedied before the run: not selected
            vins.add(vin)
            try:
                dnd = str(dnd_raw).strip().lower() in ("true", "1", "yes")
                result = _band_classify(
                    recall_class=str(recall_class or ""),
                    do_not_drive=dnd,
                    match_basis=str(match_basis or ""),
                )
                if result.severity == "p0":
                    (p0_remedied_since if since_run else p0_rows).add((cid, vin))
            except ValueError:
                pass  # unknown class/basis: not p0
    except Exception as exc:  # noqa: BLE001
        res.check("6-catalog-query", False, str(exc)[:120])
        return

    res.note("6-p0-rows", f"{len(p0_rows)} unremedied, {len(p0_remedied_since)} remedied since the run")

    findings_table = f"vsa-{stage}-avx-findings"
    all_recall: list[dict[str, Any]] = []
    for fid in _MANAGED_FLEETS:
        all_recall.extend(_scan_findings_by_fleet(ddb, findings_table, fid, _AGENT_FI_RW))

    ok, detail = score_groundings(run_rec, all_recall, p0_rows, len(vins),
                                  remedied_since=p0_remedied_since)
    res.check("6-groundings", ok, detail)


def _compute_pay_date(submitted_at: "dt.datetime") -> str:
    """D2/C8: first 06:00Z adjudicator run at or after submitted_at + 14 days.

    Extracted so tests can call the real function (not an inline copy). The 14-day
    figure comes from DMS fi_stand_ins._adjudicate_fi_claims (submitted_at + 14d rule).
    Mutant c8: days=13 shifts the pay date one day early — caught by driving this
    function directly and asserting the ISO date.
    """
    pay_eligible = submitted_at + dt.timedelta(days=14)  # 14d: contract § claim-eligibility
    candidate_06 = pay_eligible.replace(hour=6, minute=0, second=0, microsecond=0)
    if candidate_06 < pay_eligible:
        candidate_06 += dt.timedelta(days=1)
    return candidate_06.date().isoformat()


def item7(  # noqa: C901
    res: Results,
    *,
    run: str,
    stage: str,
    ddb: Any,
    api: Http,
    pool_id: str | None,
    client_id: str | None,
    idp: Any,
    manifest: Path,
    teardown: _TeardownRegistry,
    dms_repo: str,
    backups_ok: bool,
    **kw: Any,
) -> None:
    """Warranty claim (managed): approve approved warranty Finding, verify DMS Submitted,
    adjudicate in memory with DMS's own _adjudicate_fi_claims (no write),
    and report the date the deployed stand-in will pay it.

    Deviation from original design: runs _adjudicate_fi_claims in memory instead of
    invoking with a date 14 days on (adjudicator has no date input).
    """
    if pool_id is None or client_id is None:
        res.skip("7", "Cognito pool/client not available")
        return

    findings_table = f"vsa-{stage}-avx-findings"
    actions_table = f"vsa-{stage}-avx-actions"
    managed_fleets = [
        "demo-rental-carshare", "demo-lastmile-delivery",
        "demo-field-service", "demo-city-public",
    ]

    # W2: use fleet A pre-computed in main() so items 4, 5, 7, 8 share the same snapshot.
    # Fall back to local computation if absent (item run in isolation).
    fleet_a: str = kw.get("fleet_a") or ""
    if not fleet_a:
        now_iso = _iso(_now())
        fleet_book_counts: dict[str, int] = {}
        fleet_all_findings: dict[str, list[dict[str, Any]]] = {}
        for flt in managed_fleets:
            fs = _scan_findings_by_fleet(ddb, findings_table, flt, _AGENT_FI_RW)
            fleet_all_findings[flt] = fs
            fleet_book_counts[flt] = sum(
                1 for f in fs
                if f.get("finding_kind") == _RECALL_KIND
                and "/book/" in str(f.get("subject_key") or "")
                and (f.get("expires_at") or "9999") > now_iso
            )
        fleet_a = max(fleet_book_counts, key=fleet_book_counts.__getitem__)
        fleet_a_all = fleet_all_findings[fleet_a]
    else:
        fleet_a_all = _scan_findings_by_fleet(ddb, findings_table, fleet_a, _AGENT_FI_RW)
    res.note("7-fleet-a", fleet_a)

    warranty_findings = [
        f for f in fleet_a_all
        if f.get("finding_kind") == _WARRANTY_KIND
    ]

    # W9: scan Actions table once and index by finding_id (N+1 was N scans per finding).
    # FAIL on scan error so a failing scan can't be confused with "no Actions".
    # source: review cycle 2 W9
    from boto3.dynamodb.conditions import Attr as _Attr7  # noqa: PLC0415
    warranty_fids = {str(f.get("finding_id") or "") for f in warranty_findings if f.get("finding_id")}
    actions_by_fid: dict[str, list[dict[str, Any]]] = {}
    if warranty_fids:
        kw7: dict[str, Any] = {}
        try:
            while True:
                resp = ddb.Table(actions_table).scan(**kw7)
                for act in resp.get("Items", []):
                    fid = str(act.get("finding_id") or "")
                    if fid in warranty_fids:
                        actions_by_fid.setdefault(fid, []).append(act)
                tok7 = resp.get("LastEvaluatedKey")
                if not tok7:
                    break
                kw7["ExclusiveStartKey"] = tok7
        except Exception as exc:  # noqa: BLE001
            res.check("7-actions-scan", False, f"scan error: {str(exc)[:120]}")
            return
    existing_actions: list[dict[str, Any]] = [act for acts in actions_by_fid.values() for act in acts]

    # Pick planned item: lowest finding_id approved warranty Finding without a terminal action
    planned = _pick_warranty_finding(warranty_findings, existing_actions)
    if not res.check("7-planned-item", planned is not None,
                     on_fail="no eligible approved warranty Finding in fleet A"):
        return
    assert planned is not None  # mypy

    planned_id = str(planned.get("finding_id") or "")
    plan_record: dict[str, Any] = {
        "finding_id": planned_id,
        "fleet_id": fleet_a,
        "purpose": "item7-warranty-claim",
    }
    print(f"  7 planned item: {plan_record}")
    _update_manifest(manifest, "item7_planned", plan_record)

    with principal(
        idp, pool_id, client_id, "U_A-7", ["fleet-operator"],
        fleet_ids=fleet_a,
    ) as (tok_a, _):
        approve_body: dict[str, Any] = {
            "action_kind": "submit_warranty_claim",
            "target_system": "dms",
            "parameters": {},
        }
        status, body = api.call(
            "POST", f"/findings/{planned_id}/approve", f"Bearer {tok_a}", approve_body
        )
        if not res.check("7-approve", status == 200, f"HTTP {status}: {body}"):
            return

        action_id = str(body.get("action_id") or "")  # handler.py:1448 top-level
        _update_manifest(manifest, "item7_action_id", action_id)

        # Poll for Action to reach executed (DMS executor returns EXECUTED immediately)
        action_executed = False
        deadline = time.time() + 90
        while time.time() < deadline:
            resp = ddb.Table(actions_table).get_item(Key={"action_id": action_id})
            act = resp.get("Item") or {}
            if act.get("status") in ("executed", "completed"):
                action_executed = True
                break
            time.sleep(3)
        if not res.check("7-action-executed", action_executed,
                         on_fail="action did not reach executed status within 90s"):
            return

        # Find DMS claim: scan by avx_action_id = action_id without "AVX-ACT-" prefix
        bare_id = action_id.removeprefix("AVX-ACT-")
        claim_item: dict[str, Any] = {}
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                # C3 fix: paginate fully — Limit before FilterExpression caps pages
                # read, not rows matched; without pagination we may miss the claim.
                kw_cl: dict[str, Any] = {
                    "FilterExpression": _Attr7("avx_action_id").eq(bare_id),
                }
                all_cl: list[dict[str, Any]] = []
                while True:
                    page_cl = ddb.Table(f"dms-{stage}-warranty-claims").scan(**kw_cl)
                    all_cl.extend(page_cl.get("Items", []))
                    next_cl = page_cl.get("LastEvaluatedKey")
                    if not next_cl:
                        break
                    kw_cl["ExclusiveStartKey"] = next_cl
                if all_cl:
                    claim_item = all_cl[0]
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(5)
        if not res.check("7-claim-found", bool(claim_item),
                         on_fail=f"no DMS claim found for avx_action_id={bare_id!r}"):
            return

        claim_id = str(claim_item.get("claim_id") or "")
        _update_manifest(manifest, "item7_claim_id", claim_id)

        # Verify DMS claim fields: Submitted, claimed_amount_usd, submitted_at
        claim_status = str(claim_item.get("status") or "")
        res.check("7-claim-status", claim_status == "Submitted",
                  f"status={claim_status!r}")
        claimed_amount = claim_item.get("claimed_amount_usd")
        res.check("7-claimed-amount", claimed_amount is not None,
                  f"claimed_amount_usd={claimed_amount!r}")
        submitted_at_raw = str(claim_item.get("submitted_at") or "")
        if not res.check("7-submitted-at", bool(submitted_at_raw), on_fail="submitted_at missing"):
            return

        # W13/C6: assert DMS's is_fi_claim (avx_action_id + claimed_amount_usd) and
        # verdict_status == "approved" — these are what make the deployed stand-in pay it.
        try:
            mod_fi7 = _load_dms_module(dms_repo, "source/simulation/fi_stand_ins.py")
            is_fi_fn = getattr(mod_fi7, "is_fi_claim", None)
            if is_fi_fn:
                res.check("7-is-fi-claim", is_fi_fn(claim_item),
                          on_fail="is_fi_claim=False (missing claimed_amount_usd or avx_action_id)")
        except Exception as exc:  # noqa: BLE001
            res.check("7-is-fi-claim", False, f"error loading is_fi_claim: {exc}")
        verdict_status = str(claim_item.get("verdict_status") or "")
        res.check("7-verdict-approved", verdict_status == "approved",
                  f"verdict_status={verdict_status!r} (want approved)")

        # Parse submitted_at for adjudication
        try:
            submitted_at = dt.datetime.fromisoformat(
                submitted_at_raw.replace("Z", "+00:00")
            )
        except ValueError as exc:
            res.check("7-submitted-at-parse", False, str(exc))
            return

        # In-memory adjudication: DMS's own _adjudicate_fi_claims([claim], at=submitted_at+14d+1min)
        # No write back — the deployed stand-in will pay on its 06:00Z schedule 14 days on.
        try:
            adjudicated = _adjudicate_claim_in_memory(dms_repo, dict(claim_item), submitted_at)
        except Exception as exc:  # noqa: BLE001
            res.check("7-adjudication", False, f"error: {exc}")
            return

        ok, detail = score_adjudication(claim_item, adjudicated)
        res.check("7-adjudication-paid", ok, detail)

        # Report the date the deployed stand-in will pay it:
        # adjudicator runs daily at 06:00Z; pays when submitted_at + 14d <= 06:00Z that day.
        pay_date_str = _compute_pay_date(submitted_at)
        res.note("7-deployed-standin-pay-date",
                 f"{pay_date_str} "
                 f"(submitted_at={submitted_at_raw!r}, "
                 f"eligible_after={_iso(submitted_at + dt.timedelta(days=14))}, "
                 f"adjudicator_runs=06:00Z_daily)")
        # D2: write pay date and claim_id to manifest so they survive the run
        _update_manifest(manifest, "item7_pay_date", pay_date_str)
        print(f"  7 pay date: {pay_date_str}  claim_id: {claim_id}")


def item8(  # noqa: C901
    res: Results,
    *,
    run: str,
    stage: str,
    ddb: Any,
    api: Http,
    dms_api: Http,
    pool_id: str | None,
    client_id: str | None,
    idp: Any,
    manifest: Path,
    teardown: _TeardownRegistry,
    **kw: Any,
) -> None:
    """Two fleets scope isolation (LIVETEST): fleet B 403 + 0 Action rows; DMS scope probe.

    Plants 3 items in LIVETEST fleet B and 1 control in managed fleet A.
    U_LA (fleet-operator on fleet A): GET/approve/decline fleet B items → 403, 0 Actions;
    GET fleet A → 200 with control item.
    DMS scope probe with U_LA: wrong-fleet VIN → 403; fleet-A VIN + invalid UUID → 400.
    CMS groundings route: not deployed (Group 6); reported as NOTE carried to task 6.6.
    """
    if pool_id is None or client_id is None:
        res.skip("8", "Cognito pool/client not available")
        return

    findings_table = f"vsa-{stage}-avx-findings"
    actions_table = f"vsa-{stage}-avx-actions"
    managed_fleets = [
        "demo-rental-carshare", "demo-lastmile-delivery",
        "demo-field-service", "demo-city-public",
    ]

    # W2: use fleet A pre-computed in main() so items 4, 5, 7, 8 share the same snapshot.
    # Fall back to local computation if absent.
    fleet_a: str = kw.get("fleet_a") or ""
    if not fleet_a:
        now_iso = _iso(_now())
        fleet_book_counts: dict[str, int] = {}
        fleet_all_findings: dict[str, list[dict[str, Any]]] = {}
        for flt in managed_fleets:
            fs = _scan_findings_by_fleet(ddb, findings_table, flt, _AGENT_FI_RW)
            fleet_all_findings[flt] = fs
            fleet_book_counts[flt] = sum(
                1 for f in fs
                if f.get("finding_kind") == _RECALL_KIND
                and "/book/" in str(f.get("subject_key") or "")
                and (f.get("expires_at") or "9999") > now_iso
            )
        fleet_a = max(fleet_book_counts, key=fleet_book_counts.__getitem__)
        fleet_a_all_findings = fleet_all_findings[fleet_a]
        other_fleet_findings = {
            flt: fleet_all_findings[flt] for flt in managed_fleets if flt != fleet_a
        }
    else:
        fleet_a_all_findings = _scan_findings_by_fleet(ddb, findings_table, fleet_a, _AGENT_FI_RW)
        other_fleet_findings = {
            flt: _scan_findings_by_fleet(ddb, findings_table, flt, _AGENT_FI_RW)
            for flt in managed_fleets if flt != fleet_a
        }
    res.note("8-fleet-a", fleet_a)

    # VINs for DMS scope probe
    fleet_a_vin: str = ""
    for f in fleet_a_all_findings:
        v = str((f.get("scope") or {}).get("vin") or "")
        if v:
            fleet_a_vin = v
            break
    other_fleet_vin: str = ""
    for flt, flt_fs in other_fleet_findings.items():
        for f in flt_fs:
            v = str((f.get("scope") or {}).get("vin") or "")
            if v:
                other_fleet_vin = v
                break
        if other_fleet_vin:
            break

    # LIVETEST fleet B name
    livetest_fleet_b = f"LIVETEST-FI-RW-{run}-B"

    # Finding IDs
    b1_id = f"LIVETEST-FI-RW-{run}-b8-1"   # fleet B: recall book
    b2_id = f"LIVETEST-FI-RW-{run}-b8-2"   # fleet B: recall ground
    b3_id = f"LIVETEST-FI-RW-{run}-b8-3"   # fleet B: warranty claim
    ctrl_id = f"LIVETEST-FI-RW-{run}-b8-ctrl"  # fleet A: recall book (control)

    # Register teardowns BEFORE planting (safety rail)
    def _del_finding(fid: str) -> None:
        ddb.Table(findings_table).delete_item(
            Key={"finding_id": fid},
            ConditionExpression="begins_with(finding_id, :pfx)",
            ExpressionAttributeValues={":pfx": "LIVETEST-"},
        )
        print(f"  teardown: deleted Finding {fid}")

    for fid in (ctrl_id, b3_id, b2_id, b1_id):
        teardown.register(_del_finding, fid)

    now_dt = _now()
    expires = _iso(now_dt + dt.timedelta(days=7))
    created = _iso(now_dt)
    run_short = run[:8]

    # Plant fleet-B items (scope = livetest_fleet_b → U_LA scope check → 403)
    b1: dict[str, Any] = {
        "finding_id": b1_id,
        "finding_kind": _RECALL_KIND,
        "fleet_id": livetest_fleet_b,
        "agent_id": _AGENT_FI_RW,
        "scope": {"vin": f"VIN-LT-B8-1-{run_short}"},
        "severity": "p1",
        "headline": f"LIVETEST scope-B book {b1_id}",
        "subject_key": "MRD-9999-LT/book/1",
        "expires_at": expires,
        "created_at": created,
        "updated_at": created,
        "recommended_action": {
            "action_kind": "book_recall_service",
            "target_system": "dms",
            "parameters": {"dealer_id": "DEALER-LT", "recall_id": "MRD-9999-LT"},
        },
    }
    b2: dict[str, Any] = {
        "finding_id": b2_id,
        "finding_kind": _RECALL_KIND,
        "fleet_id": livetest_fleet_b,
        "agent_id": _AGENT_FI_RW,
        "scope": {"vin": f"VIN-LT-B8-2-{run_short}"},
        "severity": "p0",
        "headline": f"LIVETEST scope-B ground {b2_id}",
        "subject_key": "MRD-9999-LT/ground/1",
        "expires_at": expires,
        "created_at": created,
        "updated_at": created,
        "recommended_action": {
            "action_kind": "ground_vehicle",
            "target_system": "cms",
            "parameters": {"recall_id": "MRD-9999-LT"},
        },
    }
    b3: dict[str, Any] = {
        "finding_id": b3_id,
        "finding_kind": _WARRANTY_KIND,
        "fleet_id": livetest_fleet_b,
        "agent_id": _AGENT_FI_RW,
        "scope": {"vin": f"VIN-LT-B8-3-{run_short}"},
        "severity": "p1",
        "headline": f"LIVETEST scope-B warranty {b3_id}",
        "subject_key": "MRD-9999-LT/warranty/1",
        "expires_at": expires,
        "created_at": created,
        "updated_at": created,
        "evidence": [
            {
                "kind": "warranty.verdict",
                # schema form: _to_schema_ev stores verdict under "value"
                # source: agents/fi_recall_warranty/warranty.py _to_schema_ev
                "value": {"verdict": "approved", "reason": "LIVETEST"},
                "source": "dms",
                "source_ref": "livetest",
                "computed_at": created,
                "provenance": "live",
            }
        ],
        "recommended_action": {
            "action_kind": "submit_warranty_claim",
            "target_system": "dms",
            "parameters": {
                "vehicle_vin": f"VIN-LT-B8-3-{run_short}",
                "ro_id": "RO-LT-NOOP",
                "line_no": "1",
                "dealer_id": "DEALER-LT",
                "labor_op": "LT-NOOP",
            },
        },
    }
    ctrl: dict[str, Any] = {
        "finding_id": ctrl_id,
        "finding_kind": _RECALL_KIND,
        "fleet_id": livetest_fleet_b.replace("-B", "-A"),  # W13: LIVETEST fleet A (not managed)
        "agent_id": _AGENT_FI_RW,
        "scope": {"vin": f"VIN-LT-CTRL-{run_short}"},
        "severity": "p1",
        "headline": f"LIVETEST scope-A control {ctrl_id}",
        "subject_key": "MRD-9999-LT-CTRL/book/1",
        "expires_at": expires,
        "created_at": created,
        "updated_at": created,
        "recommended_action": {
            "action_kind": "book_recall_service",
            "target_system": "dms",
            "parameters": {"dealer_id": "DEALER-LT", "recall_id": "MRD-9999-LT-CTRL"},
        },
    }

    planted_ok = True
    for item_f in (b1, b2, b3, ctrl):
        fid = str(item_f["finding_id"])
        try:
            ddb.Table(findings_table).put_item(Item=item_f)
            print(f"  8: planted Finding {fid}")
            _update_manifest(manifest, f"item8_{fid}", fid)
        except Exception as exc:  # noqa: BLE001
            res.check(f"8-plant-{fid}", False, str(exc)[:80])
            planted_ok = False
    if not planted_ok:
        return

    with principal(
        idp, pool_id, client_id, "U_LA-8", ["fleet-operator"],
        fleet_ids=f"{fleet_a},{livetest_fleet_b.replace('-B', '-A')}",  # W13: both IDs
    ) as (tok_la, _):
        livetest_fleet_a = livetest_fleet_b.replace("-B", "-A")

        # C1: positive control — caller must reach fleet A before any isolation probe counts.
        # If this fails, every isolation probe records FAIL (not passes).
        st_ctrl, ctrl_body = api.call(
            "GET", f"/findings/fleet/{livetest_fleet_a}", f"Bearer {tok_la}"
        )
        ctrl_ok = res.check("8-get-fleet-a-200", st_ctrl == 200,
                             f"positive control HTTP {st_ctrl}")
        if ctrl_ok:
            items_list = (
                ctrl_body if isinstance(ctrl_body, list)
                else (ctrl_body.get("items") or ctrl_body.get("findings") or [])
            )
            ctrl_visible = any(
                str(
                    (i.get("finding") or {}).get("finding_id")
                    or i.get("finding_id") or ""
                ) == ctrl_id
                for i in items_list
            )
            res.check("8-ctrl-visible", ctrl_visible, f"control item {ctrl_id!r} listed",
                      on_fail=f"control item {ctrl_id!r} not in LIVETEST fleet A GET response")

        if not ctrl_ok:
            # C1: positive control failed — every isolation probe records FAIL
            for _lbl in (
                "8-get-fleet-b", "8-ctrl-approve-400", "8-ctrl-decline-200",
                "8-approve-b1", "8-b1-no-action-row",
                "8-decline-b1", "8-approve-b2", "8-b2-no-action-row", "8-decline-b2",
                "8-approve-b3", "8-b3-no-action-row", "8-decline-b3",
            ):
                res.check(_lbl, False, "C1: positive control (fleet-A GET) failed — probe invalid")
        else:
            # W4/W6: approve positive control. "adp" is a valid target, and the engineering
            # kind is refused on a VIN-scoped Finding only AFTER the scope check (the same
            # body from another fleet's operator answers 403), so a 400 naming the
            # population rule proves this caller passed scope on this route.
            st_ctrl_approve, ctrl_approve_body = api.call(
                "POST", f"/findings/{ctrl_id}/approve", f"Bearer {tok_la}",
                {"action_kind": "record_engineering_decision",
                 "target_system": "adp", "parameters": {}},
            )
            ctrl_err = str((ctrl_approve_body or {}).get("error") or "")
            ctrl_approve_ok = res.check(
                "8-ctrl-approve-400",
                st_ctrl_approve == 400 and "population-scoped" in ctrl_err,
                f"approve control: HTTP {st_ctrl_approve} {ctrl_err[:100]!r} "
                f"(want 400 naming the population-scoped rule, a post-scope refusal)",
            )

            # W6: decline positive control. The decision Action id is deterministic, so
            # its teardown is registered before the decline writes it.
            from agents.supervisor.tools.fleet_recommendation import (  # noqa: PLC0415
                _derive_decision_action_id as _decision_id,
            )
            ctrl_decline_action_id = _decision_id(ctrl_id)
            teardown.register(
                lambda _aid=ctrl_decline_action_id: ddb.Table(actions_table).delete_item(
                    Key={"action_id": _aid}
                )
            )
            st_ctrl_decline, ctrl_decline_body = api.call(
                "POST", f"/findings/{ctrl_id}/decline", f"Bearer {tok_la}",
                {"reason": "scope-ctrl-positive"},
            )
            ctrl_decline_ok = res.check(
                "8-ctrl-decline-200",
                st_ctrl_decline == 200
                and (ctrl_decline_body or {}).get("action_id") == ctrl_decline_action_id,
                f"decline control: HTTP {st_ctrl_decline} "
                f"action_id={(ctrl_decline_body or {}).get('action_id')!r}",
            )

            if not ctrl_approve_ok or not ctrl_decline_ok:
                for _lbl in (
                    "8-get-fleet-b",
                    "8-approve-b1", "8-b1-no-action-row",
                    "8-decline-b1", "8-approve-b2", "8-b2-no-action-row", "8-decline-b2",
                    "8-approve-b3", "8-b3-no-action-row", "8-decline-b3",
                ):
                    res.check(_lbl, False,
                              "W6: approve/decline positive controls failed — probe invalid")
            else:
                # ── fleet B GET → 403 ────────────────────────────────────────────────
                st_get_b, _ = api.call("GET", f"/findings/fleet/{livetest_fleet_b}", f"Bearer {tok_la}")
                res.check("8-get-fleet-b", st_get_b == 403,
                          f"GET fleet B HTTP {st_get_b} (want 403)")

                # ── B1 approve (book) → 403, 0 Action rows ─────────────────────────
                st_b1a, _ = api.call(
                    "POST", f"/findings/{b1_id}/approve", f"Bearer {tok_la}",
                    {"action_kind": "book_recall_service", "target_system": "dms", "parameters": {}},
                )
                res.check("8-approve-b1", st_b1a == 403,
                          f"approve B1 HTTP {st_b1a} (want 403)")

                # ── B1 decline → 403 ────────────────────────────────────────────────
                st_b1d, _ = api.call(
                    "POST", f"/findings/{b1_id}/decline", f"Bearer {tok_la}",
                    {"reason": "scope-test-B1"},
                )
                res.check("8-decline-b1", st_b1d == 403,
                          f"decline B1 HTTP {st_b1d} (want 403)")
                # after BOTH the approve and the decline: neither may have written a row
                n_b1 = _count_actions_for_finding(ddb, actions_table, b1_id)
                res.check("8-b1-no-action-row", n_b1 == 0,
                          f"action rows for B1: {n_b1} (want 0; -1 = read error)")

                # ── B2 approve (ground → CMS) → 403, 0 Action rows ─────────────────
                st_b2a, _ = api.call(
                    "POST", f"/findings/{b2_id}/approve", f"Bearer {tok_la}",
                    {"action_kind": "ground_vehicle", "target_system": "cms", "parameters": {}},
                )
                res.check("8-approve-b2", st_b2a == 403,
                          f"approve B2 HTTP {st_b2a} (want 403)")

                # ── B2 decline → 403 ────────────────────────────────────────────────
                st_b2d, _ = api.call(
                    "POST", f"/findings/{b2_id}/decline", f"Bearer {tok_la}",
                    {"reason": "scope-test-B2"},
                )
                res.check("8-decline-b2", st_b2d == 403,
                          f"decline B2 HTTP {st_b2d} (want 403)")
                # after BOTH the approve and the decline: neither may have written a row
                n_b2 = _count_actions_for_finding(ddb, actions_table, b2_id)
                res.check("8-b2-no-action-row", n_b2 == 0,
                          f"action rows for B2: {n_b2} (want 0; -1 = read error)")

                # ── B3 approve (warranty) → 403, 0 Action rows ─────────────────────
                st_b3a, _ = api.call(
                    "POST", f"/findings/{b3_id}/approve", f"Bearer {tok_la}",
                    {"action_kind": "submit_warranty_claim", "target_system": "dms", "parameters": {}},
                )
                res.check("8-approve-b3", st_b3a == 403,
                          f"approve B3 HTTP {st_b3a} (want 403)")

                # ── B3 decline → 403 ────────────────────────────────────────────────
                st_b3d, _ = api.call(
                    "POST", f"/findings/{b3_id}/decline", f"Bearer {tok_la}",
                    {"reason": "scope-test-B3"},
                )
                res.check("8-decline-b3", st_b3d == 403,
                          f"decline B3 HTTP {st_b3d} (want 403)")
                # after BOTH the approve and the decline: neither may have written a row
                n_b3 = _count_actions_for_finding(ddb, actions_table, b3_id)
                res.check("8-b3-no-action-row", n_b3 == 0,
                          f"action rows for B3: {n_b3} (want 0; -1 = read error)")

        # ── DMS scope probe: scope check fires before UUID validation ────────
        # C1: positive control runs FIRST (same-fleet + invalid UUID → 400); only if
        # that passes does the cross-fleet probe (→ 403) count.
        # Call the DMS API directly (not the AVX API); unknown path on API Gateway
        # also answers 403, making the probe vacuous if sent to the wrong base URL.
        # W6 fix: probe both the booking route and the warranty-claims route.
        _DMS_PROBE_DEALER = "dealer-sacramento"
        if fleet_a_vin and other_fleet_vin:
            # C1: positive control — same-fleet + invalid UUID + roster dealer → 400 must pass first
            # dealer_id must be in the body: without it the handler returns 400 "dealer_id is
            # required" BEFORE the scope check, making the C1 control vacuous (C3).
            st_same_inv, same_body = dms_api.call(
                "POST", _DMS_FLEET_RO_PATH, f"Bearer {tok_la}",
                {"vehicle_vin": fleet_a_vin, "dealer_id": _DMS_PROBE_DEALER,
                 "avx_action_id": "not-a-uuid"},
            )
            if not (st_same_inv == 400 and _DMS_UUID_REFUSAL in str((same_body or {}).get("error"))):
                res.check("8-dms-scope-probe", False,
                          f"C1: positive control failed: same-fleet-invalid={st_same_inv} "
                          f"{same_body!r} (want 400 refusing avx_action_id, a post-scope check)")
            else:
                st_wrong, _ = dms_api.call(
                    "POST", _DMS_FLEET_RO_PATH, f"Bearer {tok_la}",
                    {"vehicle_vin": other_fleet_vin, "dealer_id": _DMS_PROBE_DEALER,
                     "avx_action_id": "not-a-uuid"},
                )
                ok_dms, detail_dms = _interpret_dms_fleet_scope_denial(st_wrong, st_same_inv)
                res.check("8-dms-scope-probe", ok_dms, detail_dms)
            # W6 fix: also probe the fleet warranty-claims route
            st_cl_same, cl_same_body = dms_api.call(
                "POST", _DMS_FLEET_CLAIM_PATH, f"Bearer {tok_la}",
                {
                    "vehicle_vin": fleet_a_vin, "ro_id": "RO-PROBE-NOOP",
                    "dealer_id": _DMS_PROBE_DEALER, "line_no": "1",
                    "avx_action_id": "not-a-uuid",
                },
            )
            if not (st_cl_same == 400
                    and _DMS_CLAIM_POST_SCOPE_REFUSAL in str((cl_same_body or {}).get("error"))):
                res.check("8-dms-claim-scope-probe", False,
                          f"C1: positive control failed: same-fleet-invalid={st_cl_same} "
                          f"{cl_same_body!r} (want 400 from a post-scope check)")
            else:
                st_cl_wrong, _ = dms_api.call(
                    "POST", _DMS_FLEET_CLAIM_PATH, f"Bearer {tok_la}",
                    {
                        "vehicle_vin": other_fleet_vin, "ro_id": "RO-PROBE-NOOP",
                        "dealer_id": _DMS_PROBE_DEALER, "line_no": "1",
                        "avx_action_id": "not-a-uuid",
                    },
                )
                ok_cl, detail_cl = _interpret_dms_fleet_scope_denial(st_cl_wrong, st_cl_same)
                res.check("8-dms-claim-scope-probe", ok_cl, detail_cl)
        else:
            res.skip("8-dms-scope-probe",
                     f"VINs unavailable (fleet_a={fleet_a_vin!r}, other={other_fleet_vin!r})")
            res.skip("8-dms-claim-scope-probe",
                     f"VINs unavailable (fleet_a={fleet_a_vin!r}, other={other_fleet_vin!r})")

    # CMS groundings route not deployed: report as NOTE carried to task 6.6
    res.note("8-cms-groundings-carried",
             "ground_vehicle targeting CMS groundings route not deployed (Group 6); "
             "scope isolation confirmed via B2 403; full ground flow carried to task 6.6")


def _item9_write_backup(
    original_env: dict[str, Any],
    original_cfg: dict[str, Any],
    run_dir: Path,
    fn_name: str,
    region: str,
) -> Path:
    """Write 0600 backup to run_dir/item9-env-backup.json; return the path.  A1.

    Includes FunctionName, LastModified, CodeSha256, RevisionId alongside Variables
    so an operator has the full restore context without reading this script.
    """
    bk_path = run_dir / "item9-env-backup.json"
    payload = {
        "FunctionName": original_cfg.get("FunctionName", fn_name),
        "LastModified": original_cfg.get("LastModified", ""),
        "CodeSha256": original_cfg.get("CodeSha256", ""),
        "RevisionId": original_cfg.get("RevisionId", ""),
        "Variables": original_env,
    }
    fd = os.open(str(bk_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(payload, fh, indent=2)
    return bk_path


def _item9_write_restore_file(original_env: dict[str, Any], run_dir: Path) -> Path:
    """W8: 0600 ``{"Variables": ...}`` file that ``--environment file://`` accepts as is.

    The backup also carries FunctionName/LastModified/CodeSha256/RevisionId for
    context, which ``update-function-configuration --environment`` rejects, so the
    printed restore command names this file instead. Env values are never printed.
    """
    path = run_dir / "item9-env-restore.json"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"Variables": original_env}, fh, indent=2)
    return path


def _restore_command(fn_name: str, region: str, restore_path: Path) -> str:
    return (f"aws lambda update-function-configuration --function-name {fn_name} "
            f"--region {region} --environment file://{restore_path}")


def _check_guardrail_live(lam: Any, fn_name: str) -> tuple[bool, str]:
    """Read live Lambda env; refuse if FI_COST_GUARDRAIL_ID is blank/absent.  A5.

    A read error also refuses (fail closed): if we can't verify the guardrail is set
    we must not invoke the agent with unknown env state.
    """
    try:
        cfg = lam.get_function_configuration(FunctionName=fn_name)
        env = (cfg.get("Environment") or {}).get("Variables") or {}
        gid = env.get(_FI_GUARDRAIL_ENV, "")
        if not str(gid or "").strip():
            return False, (
                f"{_FI_GUARDRAIL_ENV!r} blank/absent in live env "
                "(test value active — invoke refused)"
            )
        return True, f"{_FI_GUARDRAIL_ENV!r} present"
    except Exception as exc:  # noqa: BLE001
        return False, f"get_function_configuration error: {str(exc)[:80]}"


def _check_scheduler_timing(sched: Any, fn_arn: str) -> tuple[bool, str]:
    """List EB Scheduler schedules targeting fn_arn; fetch each via GetSchedule.

    ListSchedules ScheduleSummary items carry no ScheduleExpression or Target.Input;
    we call get_schedule for every match to read the full schedule.  C2.

    cron(m h * * ? *): refuse if now in [t-45 min, t+45 min], i.e. now is within
    45 min before or after the cron.  The 45-min look-ahead covers the full override
    span; the 45-min look-behind covers a running/retrying invocation.  Midnight
    wrap: delta is normalised to [-720, 719].  W11.
    rate(...): allowed only if Target.Input.component in _SAFE_RATE_COMPONENTS.
    Unknown expression, parse error, no matching schedules found, or list error:
    refuse (fail closed).  B1.
    """
    import re as _re  # noqa: PLC0415
    try:
        summaries: list[dict[str, Any]] = []
        kw: dict[str, Any] = {}
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

    now = _now()
    now_min = now.hour * 60 + now.minute
    if not summaries:
        return False, "0 schedule(s) matched Lambda ARN — refusing (fail closed)"
    disabled: list[str] = []
    for s in summaries:
        name = s.get("Name", "?")
        group_name = s.get("GroupName") or "default"
        try:
            full = sched.get_schedule(Name=name, GroupName=group_name)
        except Exception as exc:  # noqa: BLE001
            return False, f"get_schedule({name!r}) error: {str(exc)[:120]}"
        # A DISABLED schedule invokes nothing, so it can't read the overridden value.
        # Other windows create new schedules disabled on this Lambda (stage D 4.3, stage
        # E); counting them would refuse every invoke. Only an exact "DISABLED" from
        # get_schedule skips; any other or missing State is checked (fail closed). B2
        # re-reads State before every override and invoke.
        if full.get("State") == "DISABLED":
            disabled.append(name)
            continue
        expr = full.get("ScheduleExpression", "")
        tz = full.get("ScheduleExpressionTimezone", "UTC") or "UTC"
        # Live schedules carry "Etc/UTC" (scheduler get-schedule, 2026-10-10); both
        # names mean UTC. Any other zone refuses rather than being read as UTC.
        if tz not in _UTC_ZONES:
            return False, f"schedule {name!r}: timezone {tz!r} is not UTC — refusing"
        if expr.startswith("cron("):
            m = _re.match(r"^cron\((\d+)\s+(\d+)\s+\*\s+\*\s+\?\s+\*\)$", expr)
            if not m:
                return False, f"unparseable cron {expr!r} on schedule {name!r}"
            minute, hour = int(m.group(1)), int(m.group(2))
            run_min = hour * 60 + minute
            # W11: normalise to [-720, 719] to handle midnight wrap
            delta = (now_min - run_min + 720) % 1440 - 720
            # Brief B1 (the user's figure, call 73): refuse if now is within 45 min
            # before OR 45 min after the cron, i.e. delta in [-45, +45]. The look-ahead
            # covers the override span between B2 re-checks. The look-behind covers a
            # scheduled run plus its first retries; it does NOT cover the schedule
            # target's whole retry budget (retryAttempts 2, maxEventAge 1 h, 15-min
            # function timeout), so a retried scheduled run can start up to 60 min
            # after its cron. Such a retry (of a run that already failed) could read the
            # blank value and fail again; the env is restored afterwards. Known gap,
            # accepted with the user's 45-minute figure.
            if -45 <= delta <= 45:
                return False, (
                    f"schedule {name!r} runs at {hour:02d}:{minute:02d}Z; "
                    f"now is {now.hour:02d}:{now.minute:02d}Z (within [-45m, +45m] window)"
                )
        elif expr.startswith("rate("):
            try:
                inp = json.loads((full.get("Target") or {}).get("Input") or "{}")
            except (ValueError, TypeError):
                return False, f"rate schedule {name!r}: can't parse Target.Input"
            component = inp.get("component", "")
            if component not in _SAFE_RATE_COMPONENTS:
                return False, (
                    f"rate schedule {name!r}: component {component!r} not in safe set "
                    f"{_SAFE_RATE_COMPONENTS}"
                )
        else:
            return False, f"schedule {name!r}: unknown expression {expr!r}"
    skipped = f"; {len(disabled)} disabled skipped" if disabled else ""
    return True, f"{len(summaries) - len(disabled)} enabled schedule(s) checked{skipped}"


def _wait_fn_update(lam: Any, fn_name: str, timeout_s: float = 90.0) -> None:
    """Poll until LastUpdateStatus == Successful (or timeout)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        cfg = lam.get_function_configuration(FunctionName=fn_name)
        if cfg.get("LastUpdateStatus", "Successful") == "Successful":
            return
        time.sleep(3)
    raise TimeoutError(f"{fn_name} config update did not complete in {timeout_s}s")


def item9(  # noqa: C901
    res: Results,
    *,
    run: str,
    stage: str,
    region: str,
    account: str,
    lam: Any,
    cw: Any,
    sns: Any,
    sched: Any,
    backups_ok: bool,
    tier2_env_snapshot: dict,  # mutable; item9 writes original_env here for residue check
    **kw: Any,
) -> None:
    """Forced failure per component: clear FI_COST_GUARDRAIL_ID -> raise before any write.

    Alarm vsa-{stage}-tier2-run-failure-{region} must be OK first.
    For each component (recall then warranty):
      1. Snapshot full Lambda env config.
      2. Clear FI_COST_GUARDRAIL_ID (override to ""); restore in finally.
      3. Sync invoke; expect a Lambda error (FunctionError set).
      4. Wait <= 15 min for alarm to reach ALARM state.
      5. Check alarm history for an Action after invoke; check SNS topic >= 1 subscriber.
      6. Wait for alarm to return to OK (<= 15 min) before second component.
      7. Run-record count unchanged or one aborted record (report as note).
    """
    if not backups_ok:
        res.skip("9", "backups failed — skipping env override item")
        return

    fn_name = f"vsa-{stage}-tier2-health-agent-{region}"
    alarm_name = f"vsa-{stage}-tier2-run-failure-{region}"
    topic_name = f"vsa-{stage}-tier2-operator-alerts"

    # ── W4 guard: alarm must start in OK ───────────────────────────────────
    try:
        alarm_resp = cw.describe_alarms(AlarmNames=[alarm_name])
        alarms = alarm_resp.get("MetricAlarms", [])
        if not alarms:
            res.check("9-alarm-exists", False, f"alarm {alarm_name!r} not found")
            return
        first_alarm = alarms[0]  # W7: save for AlarmActions check below
        start_state = first_alarm.get("StateValue", "")
        if start_state == "ALARM":
            res.skip("9", f"alarm {alarm_name!r} is already ALARM — skipping (W4 guard)")
            return
        res.note("9-alarm-start-state", start_state)
    except Exception as exc:  # noqa: BLE001
        res.check("9-alarm-check", False, str(exc)[:120])
        return

    # ── SNS topic subscriber check (done once) ─────────────────────────────
    try:
        # W7: paginate list_topics (SNS returns up to 100 per page)
        topic_arn: str | None = None
        lt_kw: dict[str, Any] = {}
        while True:
            lt_resp = sns.list_topics(**lt_kw)
            for t in lt_resp.get("Topics", []):
                arn = t.get("TopicArn", "")
                if arn.endswith(f":{topic_name}"):
                    topic_arn = arn
                    break
            if topic_arn or not lt_resp.get("NextToken"):
                break
            lt_kw["NextToken"] = lt_resp["NextToken"]
        if topic_arn is None:
            res.check("9-topic-exists", False, f"SNS topic {topic_name!r} not found")
            return
        # W7: verify alarm's AlarmActions reference this topic
        alarm_actions = first_alarm.get("AlarmActions", [])
        res.check("9-alarm-actions-topic", topic_arn in alarm_actions,
                  f"topic {topic_name!r} in AlarmActions",
                  on_fail=f"topic {topic_arn!r} not in AlarmActions={alarm_actions}")
        subs_resp = sns.list_subscriptions_by_topic(TopicArn=topic_arn)
        confirmed = [
            s for s in subs_resp.get("Subscriptions", [])
            if s.get("SubscriptionArn", "").startswith("arn:")
        ]
        res.check("9-topic-subscriber", len(confirmed) >= 1,
                  f"{len(confirmed)} confirmed subscription(s) on {topic_name!r}")
    except Exception as exc:  # noqa: BLE001
        res.check("9-topic-check", False, str(exc)[:120])
        return

    # ── Snapshot full Lambda config once (used for both components) ─────────
    try:
        original_cfg = lam.get_function_configuration(FunctionName=fn_name)
    except Exception as exc:  # noqa: BLE001
        res.check("9-get-config", False, str(exc)[:120])
        return
    original_env = dict((original_cfg.get("Environment") or {}).get("Variables") or {})
    tier2_env_snapshot["fn_name"] = fn_name   # so residue check knows which function
    tier2_env_snapshot["original_env"] = original_env  # residue check compares against this

    # A5: refuse if FI_COST_GUARDRAIL_ID is already blank in the live env
    # (a prior run or other script may have left the env in a broken state).
    gl_ok, gl_msg = _check_guardrail_live(lam, fn_name)
    if not gl_ok:
        res.check("9-guardrail-pre", False, f"guardrail not set before override: {gl_msg}")
        return

    # A1: write 0600 backup to run_dir/item9-env-backup.json (includes full config fields)
    run_dir = Path(f"/tmp/fi-rw-c-g55-{run}")
    run_dir.mkdir(mode=0o700, exist_ok=True)
    _bk_path = _item9_write_backup(original_env, original_cfg, run_dir, fn_name, region)
    _restore_path = _item9_write_restore_file(original_env, run_dir)
    print(f"  env backup  : {_bk_path}")
    print(f"  restore cmd : {_restore_command(fn_name, region, _restore_path)}")

    _override_applied = False
    restore_ok = True
    fn_arn = f"arn:aws:lambda:{region}:{account}:function:{fn_name}"

    def _sched_still_clear() -> str:
        """B2 during the alarm waits (security c1 SW1): the blank value stays applied for
        up to ~30 min across them, and a schedule another session creates or ENABLES
        meanwhile must stop the run within one poll. Returns "" when clear."""
        ok_w, msg_w = _check_scheduler_timing(sched, fn_arn)
        return "" if ok_w else msg_w

    try:
        for idx, component in enumerate((_RECALL_COMPONENT, _WARRANTY_COMPONENT)):
            # Before second component: alarm must have returned to OK
            if idx == 1:
                ok_again = False
                sched_bad = ""
                deadline_ok = time.time() + 900  # 15 min
                while time.time() < deadline_ok:
                    a2 = cw.describe_alarms(AlarmNames=[alarm_name])
                    if (a2.get("MetricAlarms") or [{}])[0].get("StateValue") == "OK":
                        ok_again = True
                        break
                    sched_bad = _sched_still_clear()
                    if sched_bad:
                        break
                    time.sleep(20)
                if sched_bad:
                    res.check("9-sched-timing-during-wait", False, sched_bad)
                    break  # the finally restores now
                if not res.check("9-alarm-back-to-ok", ok_again,
                                 on_fail="alarm did not return to OK within 15 min"):
                    break  # do not force-fail a second time if alarm is stuck

            # ── B2: re-check scheduler timing before this component's invoke ──
            sc_ok, sc_msg = _check_scheduler_timing(sched, fn_arn)
            if not sc_ok:
                res.check(f"9-{component}-sched-timing", False, sc_msg)
                continue

            # ── override FI_COST_GUARDRAIL_ID to "" ────────────────────────
            override_vars = dict(original_env)
            override_vars[_FI_GUARDRAIL_ENV] = ""
            try:
                # C3 + rebuild c1 WA2: mark applied BEFORE the call. Lambda can accept the
                # blank value and the call still raise here (a SIGTERM in the round trip, a
                # dropped connection; the client makes one attempt), and a restore of an
                # unchanged env is harmless and verified by the whole-map read-back.
                _override_applied = True
                lam.update_function_configuration(
                    FunctionName=fn_name,
                    Environment={"Variables": override_vars},
                )
                _wait_fn_update(lam, fn_name)
                print(f"  9/{component}: cleared {_FI_GUARDRAIL_ENV!r}")
            except Exception as exc:  # noqa: BLE001
                res.check(f"9-{component}-override", False, str(exc)[:120])
                continue

            # ── B2: re-check scheduler timing before invoke (update wait may take 90 s)
            sc_ok2, sc_msg2 = _check_scheduler_timing(sched, fn_arn)
            if not sc_ok2:
                res.check(f"9-{component}-sched-timing-pre-invoke", False, sc_msg2)
                continue

            invoke_ts = time.time()
            # ── sync invoke; expect Lambda error ─────────────────────────
            try:
                resp = lam.invoke(
                    FunctionName=fn_name,
                    InvocationType="RequestResponse",
                    Payload=json.dumps({"stage": stage, "component": component}).encode(),
                )
                fn_error = resp.get("FunctionError", "")
                res.check(f"9-{component}-invoke-error", bool(fn_error),
                          f"FunctionError={fn_error!r} (want non-empty)")
            except Exception as exc:  # noqa: BLE001
                res.check(f"9-{component}-invoke", False, str(exc)[:120])

            # ── wait for alarm ALARM state (up to 15 min) ─────────────────
            alarm_reached = False
            sched_bad = ""
            deadline_alarm = time.time() + 900
            while time.time() < deadline_alarm:
                a = cw.describe_alarms(AlarmNames=[alarm_name])
                state = (a.get("MetricAlarms") or [{}])[0].get("StateValue", "")
                if state == "ALARM":
                    alarm_reached = True
                    break
                sched_bad = _sched_still_clear()
                if sched_bad:
                    break
                time.sleep(20)
            if sched_bad:
                res.check(f"9-{component}-sched-timing-during-wait", False, sched_bad)
                break  # the finally restores now
            res.check(f"9-{component}-alarm", alarm_reached,
                      on_fail="alarm did not reach ALARM within 15 min")

            # ── alarm history: Action fired after invoke ──────────────────
            # Count only the notifications of THIS transition to ALARM: the alarm's
            # OKActions notify the same topic, and the back-to-OK notification just
            # before this invoke is not a report of this failure (review rebuild c1).
            if alarm_reached:
                try:
                    since = __import__("datetime").datetime.fromtimestamp(
                        invoke_ts - 5, tz=__import__("datetime").timezone.utc)
                    su = cw.describe_alarm_history(
                        AlarmName=alarm_name, HistoryItemType="StateUpdate", StartDate=since,
                    ).get("AlarmHistoryItems", [])
                    alarm_ts = min(
                        (h["Timestamp"] for h in su
                         if (json.loads(h.get("HistoryData") or "{}").get("newState") or {})
                         .get("stateValue") == "ALARM"),
                        default=None,
                    )
                    hist = cw.describe_alarm_history(
                        AlarmName=alarm_name,
                        HistoryItemType="Action",
                        StartDate=since,
                    )
                    actions_after = [a for a in hist.get("AlarmHistoryItems", [])
                                     if alarm_ts is not None and a["Timestamp"] >= alarm_ts]
                    # W7: require at least one succeeded action (failed publishes still
                    # produce Action history items with actionState="Failed")
                    succeeded = [
                        a for a in actions_after
                        if json.loads(a.get("HistoryData") or "{}").get("actionState")
                        == "Succeeded"
                    ]
                    res.check(f"9-{component}-alarm-action", len(succeeded) >= 1,
                              f"{len(succeeded)} succeeded / {len(actions_after)} total "
                              f"alarm action(s) after invoke")
                except Exception as exc:  # noqa: BLE001
                    res.check(f"9-{component}-alarm-history", False, str(exc)[:80])

    finally:
        if not _override_applied:
            # No override was ever written — skip the update and leave env intact. The
            # backup and restore files hold the full env; nothing needs them now.
            tier2_env_snapshot["restore_ok"] = True
            for _p in (_bk_path, _restore_path):
                with contextlib.suppress(OSError):
                    os.remove(str(_p))
            res.check("9-env-restored", True, "no override applied")
        else:
            # ── A3/A2: hold signals; deadline-based retry for ~120 s ───────────
            restore_ok = True
            with _defer_signals_for_restore():
                restore_deadline = time.monotonic() + 120.0
                while True:
                    try:
                        lam.update_function_configuration(
                            FunctionName=fn_name,
                            Environment={"Variables": original_env},
                        )
                        _wait_fn_update(lam, fn_name, timeout_s=30.0)
                        # A2: verify the whole map matches exactly
                        cfg2 = lam.get_function_configuration(FunctionName=fn_name)
                        restored = dict((cfg2.get("Environment") or {}).get("Variables") or {})
                        if restored == original_env:
                            print(f"  9: env restored ({len(restored)} keys)")
                            for _p in (_bk_path, _restore_path):
                                with contextlib.suppress(OSError):
                                    os.remove(str(_p))
                            break
                        diff = [k for k in set(original_env) | set(restored)
                                if original_env.get(k) != restored.get(k)]
                        if time.monotonic() < restore_deadline:
                            time.sleep(3)
                            continue
                        res.check("9-restore", False, f"env mismatch after restore; keys={diff}")
                        res.mark_cleanup_failed()
                        restore_ok = False
                        break
                    except Exception as exc:  # noqa: BLE001
                        if time.monotonic() < restore_deadline:
                            time.sleep(3)
                            continue
                        res.check("9-restore", False, f"restore failed: {exc}")
                        res.mark_cleanup_failed()
                        restore_ok = False
                        break
                # A4/W7: assign and print inside the defer block so a re-delivered signal
                # cannot preempt these lines before they run.
                tier2_env_snapshot["restore_ok"] = restore_ok
                if not restore_ok:
                    print(f"  RESTORE FAILED — backup: {_bk_path}")
                    print(f"  manual cmd: {_restore_command(fn_name, region, _restore_path)}")
                res.check("9-env-restored", restore_ok, "" if restore_ok else "see backup above")


def item10(
    res: Results,
    *,
    stage: str,
    region: str,
    logs: Any,
    run_records: dict[str, Any],
    **kw: Any,
) -> None:
    """Read-only: broker logged 'fi_item' skip for both recall and warranty kinds.

    ``logs`` is a CloudWatch Logs client: ``FilterLogEvents`` is a Logs API, not a
    CloudWatch one (an earlier version called it on the CloudWatch client and failed
    with AttributeError on every run)."""
    # Determine start time from item2 invoke_iso or fall back to today 00:00 UTC
    start_dt: dt.datetime | None = None
    for component in (_RECALL_COMPONENT, _WARRANTY_COMPONENT):
        pair = run_records.get(component)
        if pair:
            iso = pair[1]  # invoke_iso string
            try:
                t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
                start_dt = t if start_dt is None else min(start_dt, t)
            except ValueError:
                pass
    if start_dt is None:
        start_dt = _now().replace(hour=0, minute=0, second=0, microsecond=0)

    start_ms = int(start_dt.timestamp() * 1000)
    log_group = f"/aws/lambda/vsa-{stage}-avx-broker-{region}"

    try:
        # W9: paginate all CW pages (one page can be recall-only or truncated mid-stream)
        cw_kw: dict[str, Any] = {
            "logGroupName": log_group,
            "startTime": start_ms,
            "filterPattern": '"fi_item"',
        }
        events: list[dict[str, Any]] = []
        while True:
            resp = logs.filter_log_events(**cw_kw)
            events.extend(resp.get("events", []))
            token = resp.get("nextToken")
            if not token:
                break
            cw_kw["nextToken"] = token
    except Exception as exc:  # noqa: BLE001
        res.check("10-cw-query", False, str(exc)[:120])
        return

    if not isinstance(events, list):
        res.check("10-broker-fi-skip", False, "unexpected events type from CW")
        return

    ok, detail = score_broker_logs(events, require_non_livetest=True)
    res.check("10-broker-fi-skip", ok, detail)


def item11(  # noqa: C901
    res: Results,
    *,
    run: str,
    stage: str,
    region: str,
    ddb: Any,
    api: Http,
    lam: Any,
    pool_id: str | None,
    client_id: str | None,
    idp: Any,
    manifest: Path,
    teardown: _TeardownRegistry,
    **kw: Any,
) -> None:
    """D18 eval: plant LIVETEST-FI-RW-SAFETY-TEXT-1 Finding; POST /assistant/chat;
    evaluate_response(case, reply, tool_calls=None) must pass.
    """
    if pool_id is None or client_id is None:
        res.skip("11", "Cognito pool/client not available")
        return

    findings_table = f"vsa-{stage}-avx-findings"
    livetest_fleet = f"LIVETEST-FI-RW-{run}-A"
    fid = "LIVETEST-FI-RW-SAFETY-TEXT-1"

    # ── register teardown BEFORE planting ──────────────────────────────────
    def _del_11_finding() -> None:
        ddb.Table(findings_table).delete_item(
            Key={"finding_id": fid},
            ConditionExpression="begins_with(finding_id, :pfx)",
            ExpressionAttributeValues={":pfx": "LIVETEST-"},
        )
        print(f"  teardown: deleted Finding {fid}")

    teardown.register(_del_11_finding)

    # ── plant the Finding ───────────────────────────────────────────────────
    now_dt = _now()
    # W10: "do not drive" removed from headline and campaign id; phrase lives only in
    # the disclosure body so the model must read the disclosure to find it.
    disclosure_txt = (
        "Safety recall: battery fire risk.\n\n"
        "Consequence: do not drive this vehicle — fire risk if operated.\n\n"
        "Remedy: park outside away from structures and contact your dealer. "
        "LIVETEST-D18 marker."
    )
    finding: dict[str, Any] = {
        "finding_id": fid,
        "finding_kind": _RECALL_KIND,
        "fleet_id": livetest_fleet,
        "agent_id": _AGENT_FI_RW,
        "scope": {"vin": f"VIN-LT-D18-{run[:8]}"},
        "severity": "p0",
        "headline": "LIVETEST safety recall p0 (D18 eval)",
        "subject_key": "MRD-LIVETEST-SAFETY/ground/1",
        "disclosure_txt": disclosure_txt,
        "expires_at": _iso(now_dt + dt.timedelta(days=7)),
        "created_at": _iso(now_dt),
        "updated_at": _iso(now_dt),
        "recommended_action": {
            "action_kind": "ground_vehicle",
            "target_system": "cms",
            "parameters": {"recall_id": "MRD-LIVETEST-SAFETY"},
        },
    }
    try:
        ddb.Table(findings_table).put_item(Item=finding)
        print(f"  11: planted Finding {fid}")
        _update_manifest(manifest, "item11_finding_id", fid)
    except Exception as exc:  # noqa: BLE001
        res.check("11-plant", False, str(exc)[:120])
        return

    # ── check text runtime trust flag ───────────────────────────────────────
    try:
        import boto3 as _b3  # noqa: PLC0415
        ctrl = _b3.client("bedrock-agentcore-control", region_name=region)
        name_prefix = f"vsa_supervisor_text_{stage}"
        trust_val: str | None = None
        page_kw: dict[str, Any] = {}
        while True:
            page = ctrl.list_agent_runtimes(**page_kw)
            for rt in page.get("agentRuntimes", []):
                if rt.get("agentRuntimeName", "").startswith(name_prefix):
                    detail = ctrl.get_agent_runtime(agentRuntimeId=rt["agentRuntimeId"])
                    trust_val = (detail.get("environmentVariables") or {}).get(
                        "AVX_FLEET_IDS_CLAIM_TRUSTED", ""
                    )
                    break
            if trust_val is not None:
                break
            tok = page.get("nextToken")
            if not tok:
                break
            page_kw["nextToken"] = tok
        if trust_val is None:
            res.check("11-trust-flag", False, f"text runtime {name_prefix!r} not found")
            return
        if not res.check("11-trust-flag", trust_val.strip().lower() == "true",
                         f"AVX_FLEET_IDS_CLAIM_TRUSTED={trust_val!r} (want 'true')"):
            # without it the runtime ignores the fleet claim, so the tool answers
            # "unavailable" and the chat turn can only fail; don't send it
            return
    except Exception as exc:  # noqa: BLE001
        res.check("11-trust-flag", False, f"could not check trust flag: {exc}"[:160])
        return

    # ── load eval case ──────────────────────────────────────────────────────
    case_path = Path(__file__).resolve().parents[1] / "evals/cases/fi/fleet-recall-safety-text-001.yaml"
    try:
        from evals.runner.fi_text_case import evaluate_response, load_case  # noqa: PLC0415
        case = load_case(case_path)
    except Exception as exc:  # noqa: BLE001
        res.check("11-load-case", False, str(exc)[:120])
        return

    # ── fleet_manager user on the LIVETEST fleet ────────────────────────────
    with principal(
        idp, pool_id, client_id, "U_FM-11", ["fleet-operator"],
        fleet_ids=livetest_fleet,
    ) as (tok_fm, _):
        import secrets as _sec  # noqa: PLC0415
        session_id = f"livetest-fi-rw-d18-{_sec.token_hex(16)}"
        chat_body: dict[str, Any] = {
            "prompt": case.input.utterance,
            "sessionId": session_id,
            "personaId": "fleet_manager",
        }
        try:
            # 35 s: longer than API Gateway's 29 s, so a slow but answered turn isn't
            # read as a connection failure.
            status, body = api.call(
                "POST", "/assistant/chat", f"Bearer {tok_fm}", chat_body,
                timeout=35,
            )
        except Exception as exc:  # noqa: BLE001
            res.check("11-chat", False, str(exc)[:120])
            return

        if not res.check("11-chat-200", status == 200, f"HTTP {status}"):
            res.note("11-chat-body", str(body)[:120])
            return

        reply_text = ""
        if isinstance(body, dict):
            reply_text = str(body.get("result") or "")
        res.note("11-reply-len", str(len(reply_text)))

        passed, missing = evaluate_response(case, reply_text, tool_calls=None)
        res.check("11-eval", passed, f"missing={missing}")


_ITEM_FNS: dict[int, Any] = {
    1: item1, 2: item2, 3: item3, 4: item4, 5: item5, 6: item6,
    7: item7, 8: item8, 9: item9, 10: item10, 11: item11,
}



# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Live verification for FI Recall-Warranty Stage C (items 1–11)."
    )
    ap.add_argument("--stage", default=_ALLOWED_STAGE,
                    help=f"only {_ALLOWED_STAGE!r} is allowed")
    ap.add_argument("--region", default=_DEFAULT_REGION, choices=[_DEFAULT_REGION],
                    help="AVX / DMS / Cognito / Lambda region (staging lives in us-west-2 only)")
    ap.add_argument("--item", type=int, action="append", dest="items", metavar="N",
                    help="run item N (repeatable); mutually exclusive with --all")
    ap.add_argument("--all", action="store_true", help="run all items")
    ap.add_argument("--allow-writes", action="store_true",
                    help="allow Lambda invokes, Cognito users, LIVETEST plants, env overrides")
    ap.add_argument("--allow-managed-writes", action="store_true",
                    help="additionally allow managed-write items 4, 5, 7")
    ap.add_argument("--allow-paging", action="store_true",
                    help="allow item 9 (pages the operator SNS topic)")
    ap.add_argument(
        "--dms-repo",
        default=str(Path.home() / "guidance-for-dealer-management-system-on-aws"),
        help="path to DMS repo (for fi_stand_ins.py)",
    )
    return ap.parse_args()



# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def main() -> int:  # noqa: C901 (complexity — orchestration function)
    # ── gate 1: LIVE_TESTS=1 ────────────────────────────────────────────────
    if os.environ.get("LIVE_TESTS") != "1":
        print(
            "SKIPPED — set LIVE_TESTS=1 to run.\n"
            "Items 2, 3, 8, 9, 11 require --allow-writes.\n"
            "Items 4, 5, 7 require --allow-managed-writes.\n"
            "Item 9 also requires --allow-paging.\n"
            "\nExit 0 = all pass.  Exit 1 = any fail.  Exit 2 = any skip (non-passing)."
        )
        return 2

    args = _parse_args()

    if args.items and args.all:
        print("--item and --all are mutually exclusive", file=sys.stderr)
        return 1

    # ── gate 2: stage ───────────────────────────────────────────────────────
    if args.stage != _ALLOWED_STAGE:
        print(
            f"--stage must be {_ALLOWED_STAGE!r}; "
            f"refusing {args.stage!r} before any AWS call",
            file=sys.stderr,
        )
        return 1

    # Writes outside the conftest AWS block run as the caller with real credentials.
    # Refuse before any client unless the live-run step gave its go.
    _refusal = _lwg.check_write_flags(
        stage=args.stage, go_env=_LIVE_GO_ENV,
        flags={
            "--allow-writes": args.allow_writes,
            "--allow-managed-writes": args.allow_managed_writes,
            "--allow-paging": args.allow_paging,
        },
    )
    if _refusal:
        print(_refusal, file=sys.stderr)
        return 1

    # ── resolve requested items ─────────────────────────────────────────────
    if args.items:
        bad = [n for n in args.items if n not in _ALL_ITEMS]
        if bad:
            print(f"unknown --item value(s): {bad}; valid: {sorted(_ALL_ITEMS)}", file=sys.stderr)
            return 1
        items_requested = frozenset(args.items)
    else:
        items_requested = _ALL_ITEMS

    # ── flag gating ─────────────────────────────────────────────────────────
    items_to_run, items_to_skip = _compute_skips(
        items_requested,
        allow_writes=args.allow_writes,
        allow_managed_writes=args.allow_managed_writes,
        allow_paging=args.allow_paging,
    )

    # ── gate 3: write window ─────────────────────────────────────────────────
    write_items_active = items_to_run & (_WRITE_ITEMS | _MANAGED_WRITE_ITEMS)
    if write_items_active and _in_write_window():
        print(
            "Refusing to start writes inside 04:50–06:10 UTC (scheduled-job window).\n"
            "Scheduled: recall 05:00, warranty 05:20, "
            "dealer-progress 05:40, adjudicator 06:00.\n"
            "Re-run after 06:10 UTC.",
            file=sys.stderr,
        )
        return 1

    # ── AWS clients (only after all pre-AWS gates pass) ──────────────────────
    import boto3  # noqa: PLC0415

    stage, region = args.stage, args.region

    sts = boto3.client("sts", region_name=region)
    who = sts.get_caller_identity()
    account = who["Account"]
    print(f"caller  : {who['Arn']}")
    print(f"account : {account}")
    print(f"stage   : {stage}")
    print(f"region  : {region}")

    ddb_client = boto3.client("dynamodb", region_name=region)
    ddb = boto3.resource("dynamodb", region_name=region)
    lam = _make_lambda_client(region)
    cw = boto3.client("cloudwatch", region_name=region)
    logs = boto3.client("logs", region_name=region)
    sns = boto3.client("sns", region_name=region)
    sched = boto3.client("scheduler", region_name=region)
    idp = boto3.client("cognito-idp", region_name=region)
    cfn = boto3.client("cloudformation", region_name=region)
    ssm = boto3.client("ssm", region_name=region)
    s3 = boto3.client("s3", region_name=_ADP_REGION)
    athena = boto3.client("athena", region_name=_ADP_REGION)

    # AVX REST API base URL from CloudFormation stack output
    try:
        outs = cfn.describe_stacks(
            StackName=f"vsa-{stage}-api"
        )["Stacks"][0]["Outputs"]
        api_base = next(
            o["OutputValue"].rstrip("/") for o in outs if o["OutputKey"] == "RestApiUrl"
        )
    except Exception as exc:  # noqa: BLE001
        api_base = f"https://UNKNOWN-{stage}"
        print(f"WARN: could not get RestApiUrl from vsa-{stage}-api: {exc}", file=sys.stderr)
    api = Http(api_base)

    # DMS API base URL from DmsApiStack-{stage} output "ApiUrl".
    # item5 PATCH and item8 DMS probe must call the DMS API, not the AVX API;
    # an unknown path on API Gateway also answers 403, making the probe vacuous.
    try:
        dms_outs = cfn.describe_stacks(
            StackName=f"DmsApiStack-{stage}"
        )["Stacks"][0]["Outputs"]
        dms_api_base = next(
            o["OutputValue"].rstrip("/") for o in dms_outs if o["OutputKey"] == "ApiUrl"
        )
    except Exception as exc:  # noqa: BLE001
        dms_api_base = f"https://DMS-UNKNOWN-{stage}"
        print(f"WARN: could not get ApiUrl from DmsApiStack-{stage}: {exc}", file=sys.stderr)
    dms_api = Http(dms_api_base)

    pool_id: str | None = None
    client_id: str | None = None
    needs_cognito = bool(items_to_run & {4, 5, 7, 8, 11})
    if needs_cognito:
        try:
            pool_id = ssm.get_parameter(
                Name=f"/cvx/{stage}/voice-auth-user-pool-id"
            )["Parameter"]["Value"]
            client_id = password_client(idp, pool_id)
        except SystemExit as exc:
            print(f"WARN: Cognito setup: {exc}", file=sys.stderr)

    # ── run state ────────────────────────────────────────────────────────────
    run = _run_id()
    manifest = _init_manifest(run)
    print(f"\nrun id  : {run}")
    print(f"manifest: {manifest}")
    print(f"api     : {api_base}")
    print(f"dms_api : {dms_api_base}")
    print()

    # W10: record DMS repo commit and dirty flag so manifest captures which stand-in code ran
    try:
        import subprocess as _sp  # noqa: PLC0415
        dms_commit = _sp.check_output(
            ["git", "-C", args.dms_repo, "rev-parse", "HEAD"],
            stderr=_sp.DEVNULL, text=True,
        ).strip()
        dirty_out = _sp.check_output(
            ["git", "-C", args.dms_repo, "status", "--porcelain"],
            stderr=_sp.DEVNULL, text=True,
        ).strip()
        dms_dirty = bool(dirty_out)
    except Exception:  # noqa: BLE001
        dms_commit = "unknown"
        dms_dirty = None
    _update_manifest(manifest, "dms_commit", dms_commit)
    _update_manifest(manifest, "dms_dirty", dms_dirty)
    print(f"dms     : {args.dms_repo} @ {dms_commit}{'  DIRTY' if dms_dirty else ''}")

    res = Results()
    teardown = _TeardownRegistry()

    # Emit SKIP rows for gated and non-requested items up front
    for n in sorted(items_to_skip):
        res.skip(f"item-{n}", items_to_skip[n])
    not_requested = _ALL_ITEMS - items_requested - frozenset(items_to_skip)
    for n in sorted(not_requested):
        res.skip(f"item-{n}", f"not requested (use --all or --item {n})")

    # ── DDB backups before any write ─────────────────────────────────────────
    backups_ok = True
    if write_items_active:
        print("Taking DynamoDB backups before first write…")
        backups_ok = _create_ddb_backups(ddb_client, stage, run)
        if not backups_ok:
            print(
                "ERROR: DDB backups failed — refusing all write items",
                file=sys.stderr,
            )
            res.check("backups", False, "DDB backup creation failed; writes refused")

    # ── W3: pre-compute fleet A once so items 4, 5, 7, 8 share the same snapshot ──
    if items_to_run & {4, 5, 7, 8}:
        findings_table_main = f"vsa-{stage}-avx-findings"
        fleet_a_main, fleet_a_book_findings_main = _pick_managed_fleet_a(
            ddb, findings_table_main
        )
        print(f"fleet A : {fleet_a_main}")
    else:
        fleet_a_main, fleet_a_book_findings_main = "", []

    # ── context dict passed to every item function ───────────────────────────
    # tier2_env_snapshot: mutable dict; item9 writes {"fn_name": ..., "original_env": ...}
    # so the residue check can verify the Lambda env was restored.
    tier2_env_snapshot: dict[str, Any] = {}
    ctx: dict[str, Any] = {
        "run": run, "stage": stage, "region": region, "account": account,
        "ddb": ddb, "ddb_client": ddb_client, "lam": lam, "cw": cw, "logs": logs,
        "sns": sns, "sched": sched, "idp": idp, "cfn": cfn, "ssm": ssm, "s3": s3, "athena": athena,
        "api": api, "dms_api": dms_api, "pool_id": pool_id, "client_id": client_id,
        "manifest": manifest, "teardown": teardown,
        "dms_repo": args.dms_repo,
        "backups_ok": backups_ok,
        # W3: fleet A pre-computed before managed items so items 4, 5, 7, 8 share snapshot
        "fleet_a": fleet_a_main,
        "fleet_a_book_findings": fleet_a_book_findings_main,
        # Mutable dict for inter-item state: item2 writes run_records["recall"/"warranty"],
        # item3/6/10 read from it. Passed as run_records= kwarg via **ctx.
        "run_records": {},
        # item9 stores original Lambda env here; residue check reads it.
        "tier2_env_snapshot": tier2_env_snapshot,
    }

    # ── run items in design order ─────────────────────────────────────────────
    livetest_prefix = f"LIVETEST-FI-RW-{run}"
    livetest_fleet_a = f"LIVETEST-FI-RW-{run}-A"
    livetest_fleet_b = f"LIVETEST-FI-RW-{run}-B"
    findings_table = f"vsa-{stage}-avx-findings"
    actions_table = f"vsa-{stage}-avx-actions"

    try:
        for n in _RUN_ORDER:
            if n not in items_to_run:
                continue
            if n in (_WRITE_ITEMS | _MANAGED_WRITE_ITEMS) and not backups_ok:
                res.skip(f"item-{n}-backup-failed",
                         f"item {n} skipped: DDB backups did not succeed")
                continue
            # W12: re-check write window before each write item (run can span 04:50)
            if n in (_WRITE_ITEMS | _MANAGED_WRITE_ITEMS) and _in_write_window():
                res.check(f"{n}-write-window", False,
                          "write window re-check (04:50–06:10 UTC); rerun after 06:10")
                continue
            print(f"\nItem {n}")
            try:
                _ITEM_FNS[n](res, **ctx)
            except Exception as exc:  # noqa: BLE001
                res.check(f"{n}-exception", False,
                          f"item {n} raised {type(exc).__name__}: {exc}")
            # A4: if item9's restore failed, refuse any items that follow
            if _item_blocked_by_failed_restore(tier2_env_snapshot):
                res.check(f"{n}-restore-required", False,
                          f"item {n} refused: item9 restore failed — see backup above")
                break
    finally:
        print("\nTeardown")
        teardown.run(res)

        # ── residue checks (run even after exception or signal) ───────────────

        def _residue_findings() -> tuple[bool, str]:
            """0 Findings with LIVETEST-FI-RW-{run} id or fleet_id."""
            direct_ids = [
                f"LIVETEST-FI-RW-{run}-b8-1", f"LIVETEST-FI-RW-{run}-b8-2",
                f"LIVETEST-FI-RW-{run}-b8-3", f"LIVETEST-FI-RW-{run}-b8-ctrl",
                f"LIVETEST-FI-RW-{run}-b5", "LIVETEST-FI-RW-SAFETY-TEXT-1",
            ]
            return _check_residue_findings(
                ddb,
                findings_table,
                [livetest_fleet_a, livetest_fleet_b],
                direct_ids,
            )

        def _residue_actions() -> tuple[bool, str]:
            """0 Action rows whose finding_id begins with livetest_prefix."""
            return _check_residue_actions(ddb, actions_table, livetest_prefix)

        def _residue_cognito() -> tuple[bool, str]:
            """0 Cognito users with the livetest-fi-rw- username prefix."""
            return _check_residue_cognito(idp, pool_id)

        def _residue_lambda_env() -> tuple[bool, str]:
            """Lambda env matches snapshot taken before item9 override."""
            snap = tier2_env_snapshot
            if not snap:
                return True, "item9 did not run; no snapshot to compare"
            fn = snap.get("fn_name", "")
            expected = snap.get("original_env", {})
            if not fn or not expected:
                return True, f"incomplete snapshot (fn={fn!r}); check skipped"
            try:
                cfg = lam.get_function_configuration(FunctionName=fn)
                current = dict((cfg.get("Environment") or {}).get("Variables") or {})
                if current == expected:
                    return True, f"Lambda env restored ({len(current)} keys)"
                diff = sorted(k for k in set(expected) | set(current)
                              if expected.get(k) != current.get(k))
                return False, f"Lambda env mismatch; differing keys={diff}"
            except Exception as exc:  # noqa: BLE001
                return False, f"get_function_configuration error: {exc}"

        residue_checks: list[tuple[str, Any]] = [
            ("findings-livetest", _residue_findings),
            ("actions-livetest", _residue_actions),
            ("cognito-livetest", _residue_cognito),
            ("lambda-env-snapshot", _residue_lambda_env),
        ]
        # C4: DMS residue checks — read action/RO ids from manifest
        try:
            mdata = json.loads(manifest.read_text())
            mitems = mdata.get("items", {})
        except Exception:  # noqa: BLE001
            mitems = {}
        _5b_ro = mitems.get("item5b_ro_id")
        _fi_booking_aids = [
            str(mitems[k]) for k in ("item4_action_id", "item5a_action_id")
            if mitems.get(k)
        ]
        _fi_claim_aids = [str(mitems["item7_action_id"])] if mitems.get("item7_action_id") else []
        if 5 in items_to_run or _5b_ro:
            residue_checks.append((
                "dms-5b-ro",
                lambda: _check_dms_residue_5b_ro(ddb, stage, _5b_ro),
            ))
        if 4 in items_to_run or 5 in items_to_run or _fi_booking_aids:
            residue_checks.append((
                "dms-fi-bookings",
                lambda: _check_dms_residue_fi_bookings(ddb, stage, _fi_booking_aids),
            ))
        if 7 in items_to_run or _fi_claim_aids:
            residue_checks.append((
                "dms-fi-claims",
                lambda: _check_dms_residue_fi_claims(ddb, stage, _fi_claim_aids),
            ))
        print("\nResidue checks")
        _run_residue_checks(residue_checks, res)

    # ── summary and exit code ─────────────────────────────────────────────────
    res.summary()
    if res.failed or res._cleanup_failed:
        return 1
    if res.has_any_skip:
        return 2
    return 0


if __name__ == "__main__":
    _install_signal_handlers()
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(1)
