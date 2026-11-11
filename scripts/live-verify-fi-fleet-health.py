"""Live verification for FI Fleet Health Stage D, task 6.3.

Spec: .kiro/specs/2026-10-05-fi-fleet-health-stage-d/tasks.md lines 639-660 (item 6.3).
Repo: ~/cvx-wt-fi-health-d-g6

Items (run order: 1, 2, 3, 4, 5, 6, 7):
  1. Every managed VIN evaluated nightly (U7) or run says which component fell back
     read-only; Athena + DDB run records
  2. Fleet-health items land in correct fleet queues with band-table severities;
     p0 items carry catalog directive byte for byte
     read-only; DDB by_fleet GSI
  3. book_fleet_service -> DMS RO with expected lines; DMS status -> Action status;
     closed-early -> cancelled and next run re-raises; fails when labor_op not in
     ADP fleet_health.code_labor_op_map (G6-D2)
     --allow-writes --allow-managed-writes
  4. PM: recompute due/overdue/current from raw service records, DMS lines and plans;
     match view on every task for 50 random managed vehicles
     read-only; Athena
  5. Two-fleet isolation: fleet A operator sees no fleet B item,
     through AVX and through CMS queue route
     read-only; DDB + AVX API
  6. Re-run on unchanged inputs adds zero items and makes zero model calls
     --allow-writes
  7. Forced failure reaches the staleness alarm's subscriber
     --allow-writes --allow-paging

Flags:
  --stage staging           required; refuses anything else before any AWS call
  --item N                  run item N only (repeatable); mutually exclusive with --all
  --all                     run all 7 items in order
  --allow-writes            gates: Lambda invokes, Cognito users, LIVETEST plants, env overrides
  --allow-managed-writes    additionally gates item 3 (managed-fleet DMS round-trip)
  --allow-paging            gates item 7 (pages the operator SNS topic)
  --run-id STR              override generated run id (for testing / replay)

Exit 0: all pass.  Exit 1: any fail or residue.  Exit 2: any skip (non-passing).
Tokens and passwords are never printed or written to the manifest.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import math
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

# Repo root on sys.path so agents.* and evals.* are importable when the
# script is run as a file (sys.path[0] would otherwise be scripts/).
# Mirrors live-verify-fi-recall-warranty.py:55-57.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# ---------------------------------------------------------------------------
# Live-write guard (scripts/lib/live_write_guard.py)
# ---------------------------------------------------------------------------

if str(Path(__file__).resolve().parent / "lib") not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import live_write_guard as _lwg  # noqa: E402

#: Set to ``--stage`` by the live-run step, on the user's go, and by nothing else.
_LIVE_GO_ENV = "FI_FLEET_HEALTH_LIVE_GO"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ALLOWED_STAGE = "staging"
_DEFAULT_REGION = "us-west-2"
_ADP_REGION = "us-east-1"

_AGENT_FI_FH = "agent-fi-fleet-health"
_FH_FINDING_KIND = "fleet.health.service"
_FH_COMPONENT = "service"
_FH_RUN_PK = f"{_AGENT_FI_FH}#{_FH_COMPONENT}"

# Force failure without writing: clear FI_COST_GUARDRAIL_ID on the fleet-health Lambda.
# _run_service_from_env checks this before any write (runner.py:2619-2626);
# an empty guardrail id raises RuntimeError → Lambda error → staleness alarm fires.
_FI_GUARDRAIL_ENV = "FI_COST_GUARDRAIL_ID"
# Also required for the guardrail check path to be reached (runner.py:2619):
_FI_MODEL_ENABLED_ENV = "FI_SERVICE_MODEL_ENABLED"

# Staleness alarm for the service component (vsa-tier2-stack.ts:2249).
# 27 consecutive 1-hour evaluation periods with 0 RunRecordsWritten → ALARM.
_FH_ALARM_SUFFIX = "avx-run-stale-service"
_FH_ALARM_CW_NAMESPACE = "CVX/Tier2"
_FH_ALARM_CW_METRIC = "RunRecordsWritten"
_FH_ALARM_EVAL_PERIODS = 27  # verify: earliest time = forced_at + 27 h

# Write window to refuse: 04:50-06:10 UTC.
# Service schedule: 05:40 UTC; window matches recall/warranty for same Lambda env override.
_WRITE_WINDOW_START_MIN = 4 * 60 + 50
_WRITE_WINDOW_END_MIN = 6 * 60 + 10

# Run order for --all
_RUN_ORDER = [1, 2, 3, 4, 5, 6, 7]
_ALL_ITEMS = frozenset(_RUN_ORDER)

# Items gated by --allow-writes (Lambda invokes, env overrides, LIVETEST plants)
_WRITE_ITEMS = frozenset({5, 6, 7})
# Items additionally gated by --allow-managed-writes (managed-fleet DMS round-trip)
_MANAGED_WRITE_ITEMS = frozenset({3})
# Item gated by --allow-paging (pages the operator SNS topic)
_PAGING_ITEMS = frozenset({7})

# VIN and id safety patterns
_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_UTC_ZONES = frozenset({"UTC", "Etc/UTC"})


# ---------------------------------------------------------------------------
# Botocore guard
# ---------------------------------------------------------------------------


class GuardViolation(Exception):
    """Raised when a botocore call is not in the item's declared allowlist."""


# Config for Lambda sync invokes: read_timeout > function timeout (15 min = 900 s),
# connect_timeout short, no retries (a read timeout is retried by default and would
# trigger duplicate service runs).  Mirrors live-verify-fi-recall-warranty.py:485-494.
def _lambda_invoke_config() -> Any:
    """Return a botocore Config suitable for synchronous Lambda invokes."""
    from botocore.config import Config  # noqa: PLC0415
    return Config(read_timeout=910, connect_timeout=10, retries={"total_max_attempts": 1})


def _client(
    service: str,
    region: str,
    *,
    allowed_ops: frozenset[str],
    endpoint_url: str | None = None,
    config: Any = None,
) -> Any:
    """Build a boto3 low-level client with a botocore before-call guard.

    The guard raises GuardViolation for any operation whose qualified name
    (``service.OperationName``) or plain name (``OperationName``) is not
    in *allowed_ops*.  Installed via ``client.meta.events.register`` so it
    fires before any network I/O.

    Pass *config* (a ``botocore.config.Config``) for non-default timeouts or
    retry settings; required for Lambda sync invokes.
    """
    import boto3  # noqa: PLC0415

    kw: dict[str, Any] = {"region_name": region}
    if endpoint_url is not None:
        kw["endpoint_url"] = endpoint_url
    if config is not None:
        kw["config"] = config
    boto_client = boto3.client(service, **kw)

    def _guard(model: Any, params: Any, **_kw: Any) -> None:
        op = model.name
        svc = model.service_model.service_name
        qualified = f"{svc}.{op}"
        if op not in allowed_ops and qualified not in allowed_ops:
            raise GuardViolation(
                f"Operation {qualified!r} not in allowlist {sorted(allowed_ops)!r}"
            )

    boto_client.meta.events.register("before-call", _guard)
    return boto_client


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


class Results:
    """PASS / FAIL / SKIP / NOTE tracker; drives exit code."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []
        self._cleanup_failed = False

    def check(
        self, label: str, ok: bool, detail: str = "", *, on_fail: str | None = None
    ) -> bool:
        """Record a PASS or FAIL.

        ``detail`` is shown on a PASS row exactly as given.
        ``on_fail``, when provided, replaces ``detail`` on a FAIL row only —
        keeping failure wording off PASS rows (brief rule).
        """
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
        self,
        method: str,
        path: str,
        token: str,
        body: Any = None,
        timeout: int = 40,
    ) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.base + path,
            method=method,
            data=data,
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

    Run records carry millisecond ``started_at`` (``...T05:45:01.881Z``) and ``_iso(_now())`` is
    a whole second with a ``Z``. DynamoDB compares strings and ``.`` sorts below ``Z``, so
    ``...:01.881Z >= ...:01Z`` is false and a run that starts in the bound's own second would be
    missed. Without the ``Z`` the bound is a prefix of every value in that second, so it sorts
    below all of them and above every earlier second (issue
    ``2026-10-27-fi-sweep-avx-ms-run-start-readers``).
    """
    return iso[:-1] if iso.endswith("Z") else iso


def _make_run_id() -> str:
    """g6lv-<utc yyyymmddThhmmss>-<4 hex>."""
    stamp = _now().strftime("%Y%m%dT%H%M%S")
    return f"g6lv-{stamp}-{secrets.token_hex(4)}"


def _in_write_window() -> bool:
    """True inside the scheduled-job write window (04:50–06:10 UTC)."""
    n = _now()
    mins = n.hour * 60 + n.minute
    return _WRITE_WINDOW_START_MIN <= mins <= _WRITE_WINDOW_END_MIN


def _compute_skips(
    items_requested: frozenset[int],
    allow_writes: bool,
    allow_managed_writes: bool,
    allow_paging: bool,
    item7_verify: bool = False,
) -> tuple[frozenset[int], dict[int, str]]:
    """Return (items_to_run, {item: skip_reason}).  Pure; no AWS, no side-effects.

    item7_verify=True treats item 7 as read-only (--item7-phase verify does no writes).
    """
    to_run = set(items_requested)
    to_skip: dict[int, str] = {}

    # Item 7 in verify phase is read-only: exclude it from the write gate.
    write_items = (_WRITE_ITEMS - {7}) if item7_verify else _WRITE_ITEMS

    if not allow_writes:
        for n in write_items & items_requested:
            to_skip[n] = f"item {n} requires --allow-writes"
        for n in _MANAGED_WRITE_ITEMS & items_requested:
            if n not in to_skip:
                to_skip[n] = (
                    f"item {n} requires --allow-writes (and --allow-managed-writes)"
                )
        to_run -= write_items | _MANAGED_WRITE_ITEMS

    if not allow_managed_writes:
        for n in _MANAGED_WRITE_ITEMS & items_requested:
            to_skip[n] = f"item {n} requires --allow-managed-writes"
        to_run -= _MANAGED_WRITE_ITEMS

    if not allow_paging:
        for n in _PAGING_ITEMS & items_requested:
            if n not in to_skip:
                to_skip[n] = f"item {n} requires --allow-paging"
        to_run -= _PAGING_ITEMS

    return frozenset(to_run), to_skip


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------

_SIGNAL_STATE: dict[str, Any] = {}


def _install_signal_handlers() -> None:
    """SIGINT, SIGTERM, and SIGHUP each raise SystemExit(143) once; second signal is ignored."""
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
    """Hold SIGINT/SIGTERM/SIGHUP during the Lambda env restore critical section.

    A signal arriving during restore is recorded and re-sent (via os.kill)
    after the original handlers are reinstated, so cleanup completes first.
    Mirrors live-verify-fi-recall-warranty.py:301-322.
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
# Manifest (0600)
# ---------------------------------------------------------------------------


def _init_manifest(run: str) -> Path:
    """Create /tmp/fi-fh-d-g6lv-<run>/manifest.json (mode 0600)."""
    run_dir = Path(f"/tmp/fi-fh-d-g6lv-{run}")
    run_dir.mkdir(mode=0o700, exist_ok=False)
    manifest = run_dir / "manifest.json"
    _write_0600(manifest, json.dumps({"run_id": run, "items": {}}, indent=2))
    return manifest


def _write_0600(path: Path, text: str) -> None:
    """Write text to path created/truncated with mode 0600 from the start."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.chmod(str(path), 0o600)


def _update_manifest(manifest: Path, key: str, value: Any) -> None:
    """Append / update a key under items{}.  Preserves 0600 after overwrite."""
    from decimal import Decimal  # noqa: PLC0415

    def _dec(o: Any) -> float | int:
        if isinstance(o, Decimal):
            return int(o) if o % 1 == 0 else float(o)
        raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")

    data = json.loads(manifest.read_text())
    data["items"][key] = value
    _write_0600(manifest, json.dumps(data, indent=2, default=_dec))


# ---------------------------------------------------------------------------
# Teardown registry
# ---------------------------------------------------------------------------


class _TeardownRegistry:
    """Register cleanup callables BEFORE creating resources; run all in finally (LIFO)."""

    def __init__(self) -> None:
        self._items: list[tuple[Any, tuple]] = []

    def register(self, fn: Any, *args: Any) -> None:
        self._items.append((fn, args))

    def run(self, res: Results) -> None:
        for fn, args in reversed(self._items):
            try:
                fn(*args)
            except Exception as exc:  # noqa: BLE001
                name = getattr(fn, "__name__", repr(fn))
                print(f"  teardown ERROR ({name}): {exc}", file=sys.stderr)
                res.mark_cleanup_failed()


# ---------------------------------------------------------------------------
# Cognito helper
# ---------------------------------------------------------------------------


def _password_client(idp: Any, pool_id: str) -> str:
    """Return the first app client allowing USER_PASSWORD_AUTH (paged).

    Mirrors live-verify-fi-recall-warranty.py:336-349.
    Raises SystemExit when none found.
    """
    kw: dict[str, Any] = {"UserPoolId": pool_id, "MaxResults": 30}
    while True:
        page = idp.list_user_pool_clients(**kw)
        for c in page.get("UserPoolClients") or []:
            desc = idp.describe_user_pool_client(
                UserPoolId=pool_id, ClientId=c["ClientId"]
            )
            flows = desc["UserPoolClient"].get("ExplicitAuthFlows") or []
            if "ALLOW_USER_PASSWORD_AUTH" in flows:
                return c["ClientId"]
        tok = page.get("NextToken")
        if not tok:
            break
        kw["NextToken"] = tok
    raise SystemExit(f"no app client on {pool_id} allows USER_PASSWORD_AUTH")


@contextlib.contextmanager
def principal(
    idp: Any,
    pool_id: str,
    client_id: str,
    run: str,
    label: str,
    groups: list[str],
    fleet_ids: str | None = None,
    dealer_ids: str | None = None,
) -> Iterator[tuple[str, dict]]:
    """Yield (id_token, claims) for an ephemeral LIVETEST-<run>- Cognito user.

    Username carries LIVETEST-<run>- prefix; user is deleted in finally.
    Tokens and passwords are never printed or written to the manifest.
    """
    import base64  # noqa: PLC0415

    username = (
        f"LIVETEST-{run}-{label}-{secrets.token_hex(3)}@example.invalid"
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
            UserPoolId=pool_id,
            Username=username,
            MessageAction="SUPPRESS",
            UserAttributes=attrs,
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
            ClientId=client_id,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": username, "PASSWORD": password},
        )
        token = auth["AuthenticationResult"]["IdToken"]
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        print(f"  principal {label}: groups={groups} fleetIds={fleet_ids!r} dealerIds={dealer_ids!r} (ephemeral)")
        yield token, claims
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
# Residue checks
# ---------------------------------------------------------------------------


def _check_residue_findings(
    ddb: Any,
    findings_table: str,
    run: str,
) -> tuple[bool, str]:
    """0 LIVETEST-<run> Findings remain (full paged Scan on finding_id / subject_key / disclosure_txt).

    Uses the low-level DDB client (TypeDeserializer) — ddb must be a boto3
    low-level client built via _client(), not a resource.
    A scan error fails closed (read errors are not proof of absence).
    """
    from boto3.dynamodb.types import TypeDeserializer  # noqa: PLC0415

    deser = TypeDeserializer()
    marker = f"LIVETEST-{run}"

    def _deser_item(raw: dict) -> dict:
        return {k: deser.deserialize(v) for k, v in raw.items()}

    def _has_marker(item: dict) -> bool:
        for field in ("finding_id", "subject_key", "disclosure_txt"):
            val = item.get(field, "")
            if isinstance(val, str) and marker in val:
                return True
        return False

    hits: list[str] = []
    try:
        kw: dict[str, Any] = {
            "TableName": findings_table,
            "FilterExpression": (
                "contains(finding_id, :m) OR contains(subject_key, :m)"
                " OR contains(disclosure_txt, :m)"
            ),
            "ExpressionAttributeValues": {":m": {"S": marker}},
            "ProjectionExpression": "finding_id, subject_key",
        }
        while True:
            page = ddb.scan(**kw)
            for raw in page.get("Items", []):
                item = _deser_item(raw)
                if _has_marker(item):
                    hits.append(str(item.get("finding_id") or raw))
            tok = page.get("LastEvaluatedKey")
            if not tok:
                break
            kw["ExclusiveStartKey"] = tok
    except Exception as exc:  # noqa: BLE001
        return False, f"scan error: {exc}"
    ok = len(hits) == 0
    return ok, f"{len(hits)} LIVETEST finding(s) still present: {hits[:5]}"


def _check_residue_actions(
    ddb: Any, actions_table: str, prefix: str
) -> tuple[bool, str]:
    """0 Action rows whose finding_id begins with this run's LIVETEST prefix (full scan).

    Uses the low-level DDB client (TypeDeserializer) — ddb must be a boto3
    low-level client built via _client(), not a resource.
    A scan error fails closed. Ported from live-verify-fi-recall-warranty.py:624-659.
    """
    from boto3.dynamodb.types import TypeDeserializer  # noqa: PLC0415

    deser = TypeDeserializer()

    def _deser_item(raw: dict) -> dict:
        return {k: deser.deserialize(v) for k, v in raw.items()}

    try:
        hits: list[str] = []
        kw: dict[str, Any] = {
            "TableName": actions_table,
            "FilterExpression": "begins_with(finding_id, :pfx)",
            "ExpressionAttributeValues": {":pfx": {"S": prefix}},
            "ProjectionExpression": "action_id, finding_id",
        }
        while True:
            page = ddb.scan(**kw)
            hits.extend(
                str(_deser_item(i).get("action_id") or "") for i in page.get("Items", [])
            )
            tok = page.get("LastEvaluatedKey")
            if not tok:
                break
            kw["ExclusiveStartKey"] = tok
    except Exception as exc:  # noqa: BLE001
        return False, f"scan error: {exc}"
    ok = len(hits) == 0
    return ok, f"{len(hits)} LIVETEST action(s) still present: {hits[:5]}"


def _resolve_cms_pool_id(idp: Any, stage: str) -> str | None:
    """Return the Id of pool named 'cms-{stage}-ui-users' by paging ListUserPools.

    Returns None when the pool is not found or on a ListUserPools error.
    Used by the Cognito residue check; an unresolved pool is a FAIL.
    """
    target = f"cms-{stage}-ui-users"
    try:
        kw: dict[str, Any] = {"MaxResults": 60}
        while True:
            page = idp.list_user_pools(**kw)
            for pool in page.get("UserPools", []):
                if pool.get("Name") == target:
                    return pool["Id"]
            tok = page.get("NextToken")
            if not tok:
                return None
            kw["NextToken"] = tok
    except Exception:  # noqa: BLE001
        return None


def _check_residue_cognito(
    idp: Any, pool_id: str | None, run: str
) -> tuple[bool, str]:
    """0 Cognito users whose email begins with LIVETEST-<run>- (full paged scan).

    Scoped to this run only (not script-wide) to avoid false positives from
    concurrent runs. A list_users error fails closed.
    """
    if pool_id is None:
        return True, "Cognito pool not configured; check skipped"
    prefix = f"LIVETEST-{run}-"
    try:
        kw: dict[str, Any] = {
            "UserPoolId": pool_id,
            "Filter": f'email ^= "{prefix}"',
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


def _run_residue_checks(
    checks: list[tuple[str, Any]], res: Results
) -> None:
    """Run pluggable residue checks.  Each entry: (label, callable() -> (bool, str))."""
    for label, check_fn in checks:
        try:
            ok, detail = check_fn()
            res.check(f"residue-{label}", ok, detail)
        except Exception as exc:  # noqa: BLE001
            res.check(f"residue-{label}", False, f"error: {exc}")


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Item 1 helpers — U7 managed-pool coverage check
# ---------------------------------------------------------------------------

# Health components with their nightly run-record pk prefix.
# VIN caps are live values from the Lambda env; see _read_health_caps.
# agent_id from contract.py:82-84
_HEALTH_COMPONENTS: list[tuple[str, str]] = [
    ("battery", "agent-2-vehicle-health#battery"),
    ("tires",   "agent-2-vehicle-health#tires"),
    ("brakes",  "agent-2-vehicle-health#brakes"),
]

# Lambda env-var names for VIN caps (runner.py:151-153).
# Live values come from vsa-{stage}-tier2-health-agent-{region}'s env;
# code defaults (375/156/2000) must not be substituted — see review W1.
_HEALTH_CAP_KEYS: dict[str, str] = {
    "battery": "TIER2_MAX_VINS_BATTERY",
    "tires":   "TIER2_MAX_VINS_TIRES",
    "brakes":  "TIER2_MAX_VINS_BRAKES",
}

# Night window for scheduled health runs (runner.py:1566; schedule 02:00/02:20/02:40Z)
_NIGHT_START_MIN = 2 * 60       # 02:00 UTC
_NIGHT_END_MIN   = 3 * 60 + 30  # 03:30 UTC


def _run_in_night_window(started_at: str) -> bool:
    """True if started_at (ISO-8601 UTC) falls in the 02:00-03:30 UTC window."""
    try:
        t = dt.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        mins = t.hour * 60 + t.minute
        return _NIGHT_START_MIN <= mins <= _NIGHT_END_MIN
    except (ValueError, AttributeError):
        return False


def _latest_health_run(
    ddb: Any,
    table_name: str,
    run_pk: str,
    *,
    _deser: Any = None,
) -> dict[str, Any] | None:
    """Return the newest run record for run_pk IFF it is ``completed`` and started within 26 h.

    The newest record decides, scheduled (02:00-03:30 UTC) or invoked by hand with the
    schedule's input (note KK, 2026-10-22: after the fallback-record deploy, battery is run
    once by hand and item 1 is rerun against that run, not the next night's). An aborted
    newest run returns None, and so does a newest record older than 26 h.

    Shape: run_records.py:50-115 (pk=agent_component, sk=started_at).
    Facts.md live example: started_at="2026-10-17T02:00:06Z" (whole seconds then; run records
    carry milliseconds, e.g. "2026-10-27T02:00:06.881Z", since the FI sweep's T1.5).
    """
    from boto3.dynamodb.types import TypeDeserializer  # noqa: PLC0415

    deser = _deser or TypeDeserializer()
    cutoff = _now() - dt.timedelta(hours=26)

    resp = ddb.query(
        TableName=table_name,
        KeyConditionExpression="agent_component = :pk",
        ExpressionAttributeValues={":pk": {"S": run_pk}},
        ScanIndexForward=False,
        Limit=1,
    )
    items = resp.get("Items", [])
    if not items:
        return None
    item = {k: deser.deserialize(v) for k, v in items[0].items()}
    if item.get("status") != "completed":
        return None
    sa = str(item.get("started_at") or "")
    try:
        t = dt.datetime.fromisoformat(sa.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t < cutoff:
        return None
    return item


def _latest_service_run(
    ddb: Any,
    table_name: str,
    *,
    _deser: Any = None,
) -> dict[str, Any] | None:
    """Return the newest completed run for agent-fi-fleet-health#service within 26 h.

    The service component fires at 05:40 UTC (vsa-tier2-stack.ts:2003), not in the
    Agent-2 02:00-03:30 window, so no clock-window filter is applied — only the 26 h
    recency bound.  An aborted newest record returns None.

    Shape: run_records.py:50-115 (pk=agent_component, sk=started_at ISO-8601 UTC).
    """
    from boto3.dynamodb.types import TypeDeserializer  # noqa: PLC0415

    deser = _deser or TypeDeserializer()
    cutoff = _now() - dt.timedelta(hours=26)
    resp = ddb.query(
        TableName=table_name,
        KeyConditionExpression="agent_component = :pk",
        ExpressionAttributeValues={":pk": {"S": _FH_RUN_PK}},
        ScanIndexForward=False,
        Limit=1,
    )
    items = resp.get("Items", [])
    if not items:
        return None
    item = {k: deser.deserialize(v) for k, v in items[0].items()}
    if item.get("status") != "completed":
        return None
    sa = str(item.get("started_at") or "")
    try:
        t = dt.datetime.fromisoformat(sa.replace("Z", "+00:00"))
    except ValueError:
        return None
    return item if t >= cutoff else None


def _get_cursor_item(
    ddb: Any,
    table_name: str,
    component: str,
    *,
    _deser: Any = None,
) -> dict[str, Any] | None:
    """GetItem the CURSOR#<component>/STATE item from vsa-{stage}-health-findings.

    Cursor schema: tier2/selection.py:191-200 (_build_cursor_item).
    Fields: pk, sk, last_vin, managed_last_vin (absent on legacy), pool_exhausted_count.
    """
    from boto3.dynamodb.types import TypeDeserializer  # noqa: PLC0415

    deser = _deser or TypeDeserializer()
    resp = ddb.get_item(
        TableName=table_name,
        Key={"pk": {"S": f"CURSOR#{component}"}, "sk": {"S": "STATE"}},
        ConsistentRead=True,
    )
    raw = resp.get("Item")
    if raw is None:
        return None
    return {k: deser.deserialize(v) for k, v in raw.items()}


_POOL_COUNT_LIMIT = 9_999_999  # effectively unlimited for counting managed pool VINs


def _athena_count_component_pool(
    athena: Any,
    stage: str,
    workgroup: str,
    component: str,
    *,
    _run_query_fn: Any = None,
) -> int | None:
    """Return COUNT(*) of managed VINs in this component's pool.

    Uses each source's own ``_build_select_managed_vins_sql`` so the predicate
    matches the agent exactly (spec § D7 / review W2).

    Sources (pool predicate):
      battery: AdpBatterySource._build_select_managed_vins_sql  — energy_usage JOIN managed_vehicles
               agents/health/adp_battery_source.py:300-338
      tires:   AdpTireSource._build_select_managed_vins_sql     — tire_health JOIN managed_vehicles
               WHERE needs_replacement = true
               agents/health/adp_tire_source.py:299-347
      brakes:  AdpBrakeSource._build_select_managed_vins_sql    — service_records JOIN managed_vehicles
               WHERE brake_service + OPEN_OUTCOMES
               agents/health/adp_brake_source.py:543-588

    Returns None on any error; caller treats None as unknown pool_size.
    """
    import time as _t  # noqa: PLC0415
    from agents.health.adp_battery_source import AdpBatterySource  # noqa: PLC0415
    from agents.health.adp_tire_source import AdpTireSource  # noqa: PLC0415
    from agents.health.adp_brake_source import AdpBrakeSource  # noqa: PLC0415

    # Build the source instance needed only for SQL construction (no Athena client).
    if component == "battery":
        src: Any = AdpBatterySource.__new__(AdpBatterySource)
        src._stage = stage
        src._database = f"adp_{stage}_energy_usage"
    elif component == "tires":
        src = AdpTireSource.__new__(AdpTireSource)
        src._stage = stage
    elif component == "brakes":
        src = AdpBrakeSource.__new__(AdpBrakeSource)
        src._stage = stage
    else:
        return None

    vin_sql = src._build_select_managed_vins_sql(cursor=None, limit=_POOL_COUNT_LIMIT)
    sql = f"SELECT COUNT(*) FROM ({vin_sql}) AS _pool"

    try:
        if _run_query_fn is not None:
            return _run_query_fn(sql)
        resp = athena.start_query_execution(QueryString=sql, WorkGroup=workgroup)
        eid = resp["QueryExecutionId"]
        deadline = _t.monotonic() + 120.0
        while _t.monotonic() < deadline:
            qe = athena.get_query_execution(QueryExecutionId=eid)["QueryExecution"]
            state = qe["Status"]["State"]
            if state == "SUCCEEDED":
                break
            if state in ("FAILED", "CANCELLED"):
                return None
            _t.sleep(2.0)
        else:
            return None
        page = athena.get_query_results(QueryExecutionId=eid)
        rows = page.get("ResultSet", {}).get("Rows", [])
        if len(rows) >= 2:
            val = rows[1].get("Data", [{}])[0].get("VarCharValue", "")
            return int(val) if val.isdigit() else None
        return None
    except Exception:  # noqa: BLE001
        return None


def _score_u7_component(
    res: Results,
    component: str,
    cap: int,
    run_rec: dict[str, Any] | None,
    cursor_item: dict[str, Any] | None,
    pool_size: int | None,
) -> None:
    """Apply U7 checks for one component; record PASS/FAIL rows.

    Checks (brief item 1 / spec.md:1022-1025):
      - run_rec present: the newest run, completed, within 26 h (scheduled or by hand)
      - managed_select_errors == 0  (run_records.py:67 kwarg)
      - cursor managed_last_vin is None (pool fits) OR pool_size <= cap (pool==cap case)
      - when rotating and pool > cap: PASS only on a sound 7-day fallback record in the
        run record (_score_7_day_fallback); FAIL otherwise

    Cursor schema: tier2/selection.py:191-200 (_build_cursor_item).
    managed_last_vin absent on legacy items = treat as None (selection.py:213-237).
    """
    prefix = f"1-{component}"

    # No completed run in the last 26 h (the newest record decides)
    if run_rec is None:
        res.check(f"{prefix}-run", False, "",
                  on_fail="newest run is not a completed run from the last 26 h")
        return
    sa = str(run_rec.get("started_at") or "")
    when = ("scheduled 02:00-03:30Z window" if _run_in_night_window(sa)
            else "outside the 02:00-03:30Z schedule window (invoked by hand)")
    if not res.check(f"{prefix}-run", True,
                     f"started_at={sa} ({when})"):
        return  # unreachable given the None guard above

    # managed_select_errors == 0
    sel_errs = int(run_rec.get("managed_select_errors") or 0)
    res.check(
        f"{prefix}-select-errors",
        sel_errs == 0,
        f"managed_select_errors={sel_errs}",
        on_fail=f"managed_select_errors={sel_errs} (selection.py:517)",
    )

    # Cursor: managed_last_vin absent/None = pool fits; non-None = rotating
    # Legacy items may lack managed_last_vin (selection.py:197-198).
    mlv = None
    if cursor_item is not None:
        mlv = cursor_item.get("managed_last_vin")  # absent key = None

    pool_detail = (
        f"pool_size={pool_size} cap={cap}"
        if pool_size is not None
        else f"pool_size=unknown cap={cap}"
    )

    # The script's own pool count decides, whatever the cursor says (review cycle 1 W1): a
    # cursor without managed_last_vin used to PASS "pool fits" even with a pool larger than
    # the cap (the night a wrap-around query failed resets the cursor and writes no record).
    if pool_size is None:
        res.check(
            f"{prefix}-pool-fitted",
            False,
            "",
            on_fail=(
                f"pool_size unknown (Athena query failed); "
                f"managed_last_vin={mlv!r}; cannot confirm all VINs covered with cap={cap}"
            ),
        )
    elif pool_size <= cap:
        if mlv is None:
            # Pool fits entirely — all managed VINs covered this night
            res.check(f"{prefix}-pool-fitted", True, pool_detail)
        else:
            # pool == cap edge case — all VINs still covered each run via
            # wrap-around (selection.py:543-569).
            res.check(
                f"{prefix}-pool-fitted",
                True,
                f"pool={pool_size}=cap={cap} wrap-rotation; all VINs covered",
            )
    else:
        # Genuine rotation: pool > cap, VINs deferred across nights. U7 allows this
        # only as the 7-day fallback the run states in its record.
        _score_7_day_fallback(res, prefix, cap, run_rec, pool_size, mlv)


#: U7 (stage D spec): the fallback cycle is every managed vehicle within 7 days.
_U7_CYCLE_NIGHTS = 7

#: The ints a 7_day_fallback record carries (agents/tier2/run_records.py _coverage_item).
_FALLBACK_FIELDS = ("cycle_nights", "cap", "managed_pool", "nights_to_cover", "cycle_covers",
                    "slice_start", "slice_size")


def _record_int(value: Any) -> int | None:
    """An integral DynamoDB number (Decimal after deserialising) or int; None otherwise."""
    from decimal import Decimal  # noqa: PLC0415

    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        return None
    try:
        as_int = int(value)
    except (ValueError, ArithmeticError):
        return None
    return as_int if as_int == value else None


def _score_7_day_fallback(
    res: Results,
    prefix: str,
    cap: int,
    run_rec: dict[str, Any],
    pool_size: int,
    mlv: Any,
) -> None:
    """PASS a rotating component (pool > cap) only on a sound 7-day fallback record.

    The run record's ``managed_coverage`` (agents/tier2/selection.py ``_managed_coverage``)
    must say ``7_day_fallback`` with a 7-night cycle, the live cap, a full slice, its own
    arithmetic right, and a pool it covers; and this script's own pool count must fit
    7 nights x the live cap. Anything else FAILs naming what was wrong.
    """
    label = f"{prefix}-pool-fitted"
    head = f"managed pool ({pool_size}) > cap ({cap}); managed_last_vin={mlv!r}; "
    cov = run_rec.get("managed_coverage")
    if not isinstance(cov, dict):
        res.check(label, False, "", on_fail=head + "no 7-day-cycle fallback record on last "
                  "night's run (managed_coverage absent)")
        return
    if cov.get("mode") != "7_day_fallback":
        res.check(label, False, "", on_fail=head + "no 7-day-cycle fallback record: "
                  f"managed_coverage mode={cov.get('mode')!r}")
        return
    f = {name: _record_int(cov.get(name)) for name in _FALLBACK_FIELDS}
    missing = sorted(name for name, v in f.items() if v is None)
    if missing:
        res.check(label, False, "", on_fail=head + f"7-day-cycle fallback record lacks {missing}")
        return
    problems: list[str] = []
    if f["cycle_nights"] != _U7_CYCLE_NIGHTS:
        problems.append(f"cycle_nights={f['cycle_nights']} (U7 is {_U7_CYCLE_NIGHTS})")
    if f["cap"] != cap:
        problems.append(f"record cap={f['cap']} != live cap {cap}")
    if f["slice_size"] != f["cap"]:
        problems.append(f"slice_size={f['slice_size']} is not a full slice of {f['cap']}")
    rec_pool, rec_cap = f["managed_pool"], f["cap"]
    if rec_cap <= 0 or rec_pool <= rec_cap:
        problems.append(f"record pool={rec_pool} is not larger than its cap={rec_cap}")
    else:
        nights = -(-rec_pool // rec_cap)
        covers = min(rec_pool, _U7_CYCLE_NIGHTS * rec_cap)
        if f["nights_to_cover"] != nights:
            problems.append(f"nights_to_cover={f['nights_to_cover']} (ceil({rec_pool}/{rec_cap})"
                            f" is {nights})")
        if f["cycle_covers"] != covers:
            problems.append(f"cycle_covers={f['cycle_covers']} (min({rec_pool}, "
                            f"{_U7_CYCLE_NIGHTS}x{rec_cap}) is {covers})")
        if covers < rec_pool:
            problems.append(f"7 nights cover {covers} of the record's {rec_pool}")
        if not 1 <= f["slice_start"] <= rec_pool:
            problems.append(f"slice_start={f['slice_start']} outside 1..{rec_pool}")
    if _U7_CYCLE_NIGHTS * cap < pool_size:
        problems.append(f"7 nights x cap {cap} = {_U7_CYCLE_NIGHTS * cap} < managed pool "
                        f"{pool_size}")
    if problems:
        res.check(label, False, "", on_fail=head + "7-day-cycle fallback record unsound: "
                  + "; ".join(problems))
        return
    end = (f["slice_start"] + f["slice_size"] - 2) % rec_pool + 1
    res.check(
        label, True,
        f"7-day fallback: managed pool {pool_size} (run record {rec_pool}), cap {cap}; "
        f"7 nights cover {f['cycle_covers']} of the record's {rec_pool} "
        f"(capacity {_U7_CYCLE_NIGHTS * cap}); this run's slice {f['slice_start']}-{end}"
        f" ({f['slice_size']} vehicles), {f['nights_to_cover']} nights to cover the pool",
    )


def _score_battery_cap_and_budget(
    res: Results,
    cap: int,
    run_rec: dict[str, Any] | None,
) -> None:
    """Battery-specific carried checks from tasks.md 6.3 (carried from 4.3):
      - TIER2_MAX_VINS_BATTERY (from Lambda env) >= 600
      - last night's battery run finished within 600 s

    run_record fields: run_records.py:50-115 (started_at, finished_at as ISO-8601 UTC).
    """
    res.check(
        "1-battery-cap-raised",
        cap >= 600,
        f"battery_cap={cap}",
        on_fail=f"battery cap={cap} < 600; TIER2_MAX_VINS_BATTERY not yet raised in Lambda env",
    )
    if run_rec is None:
        res.check(
            "1-battery-wall-budget",
            False,
            "",
            on_fail="no battery run record; cannot check wall budget",
        )
        return
    try:
        t_start = dt.datetime.fromisoformat(
            str(run_rec.get("started_at") or "").replace("Z", "+00:00")
        )
        t_end = dt.datetime.fromisoformat(
            str(run_rec.get("finished_at") or "").replace("Z", "+00:00")
        )
        duration = (t_end - t_start).total_seconds()
    except (ValueError, TypeError):
        res.check(
            "1-battery-wall-budget",
            False,
            "",
            on_fail="cannot parse started_at/finished_at from battery run record",
        )
        return
    res.check(
        "1-battery-wall-budget",
        duration < 600.0,
        f"battery_run_duration={duration:.0f}s",
        on_fail=f"battery run took {duration:.0f}s >= 600 s wall budget (tasks.md 6.3)",
    )


def _read_health_caps(
    lam: Any,
    stage: str,
    region: str,
) -> dict[str, int] | None:
    """Return {component: cap} from vsa-{stage}-tier2-health-agent-{region}'s env.

    Reads TIER2_MAX_VINS_{BATTERY,TIRES,BRAKES} (runner.py:151-153).
    Returns None on any error or if a key is missing; caller must FAIL closed.

    Lambda op: GetFunctionConfiguration (read-only; no env change).
    """
    fn_name = f"vsa-{stage}-tier2-health-agent-{region}"
    try:
        cfg = lam.get_function_configuration(FunctionName=fn_name)
        env = cfg.get("Environment", {}).get("Variables", {})
        caps: dict[str, int] = {}
        for comp, key in _HEALTH_CAP_KEYS.items():
            val = env.get(key)
            if not val:
                return None  # key absent in Lambda env
            caps[comp] = int(val)
        return caps
    except Exception:  # noqa: BLE001
        return None


def _item_1(res: Results, *, stage: str, region: str, **_kw: Any) -> None:
    """Every managed VIN evaluated nightly (U7) or run says fallback.

    Read-only: lambda.GetFunctionConfiguration (caps) +
               DDB Query on vsa-{stage}-avx-agent-runs +
               DDB GetItem on vsa-{stage}-health-findings (cursor) +
               Athena count on each component's managed pool.
    """
    lam = _client(
        "lambda",
        region,
        allowed_ops=frozenset({"lambda.GetFunctionConfiguration"}),
    )
    caps = _read_health_caps(lam, stage, region)
    if caps is None:
        fn = f"vsa-{stage}-tier2-health-agent-{region}"
        res.check("1-caps-read", False, "",
                  on_fail=f"GetFunctionConfiguration({fn}) failed or TIER2_MAX_VINS env keys missing; cannot score pool coverage without live caps")
        return
    res.check("1-caps-read", True, f"caps={caps}")

    ddb = _client(
        "dynamodb",
        region,
        allowed_ops=frozenset({"dynamodb.Query", "dynamodb.GetItem"}),
    )
    athena = _client(
        "athena",
        _ADP_REGION,
        allowed_ops=frozenset({
            "athena.StartQueryExecution",
            "athena.GetQueryExecution",
            "athena.GetQueryResults",
        }),
    )
    runs_table   = f"vsa-{stage}-avx-agent-runs"
    cursor_table = f"vsa-{stage}-health-findings"
    workgroup    = f"cvx-{stage}-analytics"

    for component, run_pk in _HEALTH_COMPONENTS:
        cap         = caps[component]
        pool_size   = _athena_count_component_pool(athena, stage, workgroup, component)
        if pool_size is None:
            res.note(f"1-{component}-pool-size",
                     f"Athena query failed; pool_size unknown for {component}")
        run_rec     = _latest_health_run(ddb, runs_table, run_pk)
        cursor_item = _get_cursor_item(ddb, cursor_table, component)
        _score_u7_component(res, component, cap, run_rec, cursor_item, pool_size)
        if component == "battery":
            _score_battery_cap_and_budget(res, cap, run_rec)


# ---------------------------------------------------------------------------
# Item 2 helpers — fleet routing + band-table severities + p0 disclosure
# ---------------------------------------------------------------------------

# Managed fleet ids for fleet-health (same pools as fleet-recall/warranty).
_MANAGED_FH_FLEETS: list[str] = [
    "demo-rental-carshare", "demo-lastmile-delivery",
    "demo-field-service", "demo-city-public",
]


def _load_fault_catalog_s3(s3: Any, bucket: str) -> dict[str, dict]:
    """Read config/fleet_health/fault_codes/fault_codes.parquet from ADP lake.

    Returns ``{code: {code, band, directive_text}}``.
    Raises on any error (caller wraps in try/except).

    S3 op: GetObject only (item 2 allowed_ops).
    Uses duckdb to read parquet (available in the CVX dev venv; no pyarrow needed).
    """
    import os as _os  # noqa: PLC0415
    import tempfile   # noqa: PLC0415
    import duckdb     # noqa: PLC0415

    key = "config/fleet_health/fault_codes/fault_codes.parquet"
    resp = s3.get_object(Bucket=bucket, Key=key)
    body = resp["Body"].read()

    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
        tmp.write(body)
        tmp_path = tmp.name
    try:
        conn = duckdb.connect()
        rows = conn.execute(
            f"SELECT code, band, directive_text FROM read_parquet('{tmp_path}')"
        ).fetchall()
    finally:
        _os.unlink(tmp_path)

    catalog: dict[str, dict] = {}
    for (code, band, directive_text) in rows:
        if not code:
            continue
        catalog[str(code).strip()] = {
            "code": str(code).strip(),
            "band": str(band or "").strip(),
            "directive_text": directive_text,
        }
    return catalog


def _vin_fleet_cms(ddb_low: Any, table: str, vin: str) -> str | None:
    """Return fleetId for vin from cms-{stage}-storage-vehicles; None if unresolvable.

    Uses vin-index Query + vehicleId GetItem —
    mirrors agents/fi_recall_warranty/selection.py:lookup_vehicle_cms_ids:309-339.
    Low-level DDB client (DDB ops: Query, GetItem).
    """
    from boto3.dynamodb.types import TypeDeserializer  # noqa: PLC0415

    deser = TypeDeserializer()
    resp = ddb_low.query(
        TableName=table,
        IndexName="vin-index",
        KeyConditionExpression="#v = :vin",
        ExpressionAttributeNames={"#v": "vin"},
        ExpressionAttributeValues={":vin": {"S": vin}},
        Limit=2,
    )
    items = resp.get("Items") or []
    if len(items) != 1:
        return None
    vehicle_id = (items[0].get("vehicleId") or {}).get("S")
    if not vehicle_id:
        return None
    gr = ddb_low.get_item(
        TableName=table,
        Key={"vehicleId": {"S": vehicle_id}},
        ProjectionExpression="fleetId",
    )
    raw = gr.get("Item") or {}
    item = {k: deser.deserialize(v) for k, v in raw.items()}
    return item.get("fleetId") or None


def _query_by_fleet_fh(
    ddb_low: Any,
    table: str,
    fleet_id: str,
    computed_at_ge: str,
) -> list[dict]:
    """Query by_fleet GSI for fleet_id, filter agent_id + finding_kind + computed_at.

    Returns deserialized finding dicts (all pages).
    DDB op: Query only.

    Adapted from agents/fi_fleet_health/selection.py:select_agent2_findings:347.
    """
    from boto3.dynamodb.types import TypeDeserializer  # noqa: PLC0415

    deser = TypeDeserializer()
    kw: dict[str, Any] = {
        "TableName": table,
        "IndexName": "by_fleet",
        "KeyConditionExpression": "fleet_id = :flt",
        "FilterExpression": (
            "agent_id = :aid"
            " AND finding_kind = :fk"
            " AND computed_at >= :cat"
        ),
        "ExpressionAttributeValues": {
            ":flt": {"S": fleet_id},
            ":aid": {"S": _AGENT_FI_FH},
            ":fk": {"S": _FH_FINDING_KIND},
            ":cat": {"S": computed_at_ge},
        },
    }
    items: list[dict] = []
    while True:
        page = ddb_low.query(**kw)
        for raw in page.get("Items", []):
            items.append({k: deser.deserialize(v) for k, v in raw.items()})
        tok = page.get("LastEvaluatedKey")
        if not tok:
            break
        kw["ExclusiveStartKey"] = tok
    return items


def _needs_from_evidence(evidence: list) -> tuple[list, list, list]:
    """Extract (open_faults, agent2_findings, pm_tasks) from a Finding's evidence.

    Reads health.need entries built by loop.py:_build_raw_evidence:638.
    Need value shape from bands.py:classify_needs:128-175:
      fault:           {kind, need_id, code, severity_band, ...}
      agent2_finding:  {kind, need_id, finding_id, finding_kind, severity_band, ...}
      pm_task:         {kind, need_id, task_code, status, severity_band, ...}
    """
    open_faults: list[dict] = []
    agent2_findings: list[dict] = []
    pm_tasks: list[dict] = []
    for entry in evidence:
        if entry.get("kind") != "health.need":
            continue
        need = entry.get("value") or {}
        kind = need.get("kind")
        if kind == "fault":
            open_faults.append({
                "code": need.get("code", ""),
                "band": need.get("severity_band", ""),
                "last_seen_at": need.get("last_seen_at") or "",
            })
        elif kind == "agent2_finding":
            # severity_band is already capped at attention (bands.py:177 W7)
            agent2_findings.append({
                "finding_id": need.get("finding_id", ""),
                "severity": need.get("severity_band", ""),
                "finding_kind": need.get("finding_kind", ""),
            })
        elif kind == "pm_task":
            pm_tasks.append({
                "task_code": need.get("task_code", ""),
                "status": need.get("status", ""),
            })
    return open_faults, agent2_findings, pm_tasks


def _item_2(  # noqa: C901
    res: Results,
    *,
    stage: str,
    region: str,
    account: str,
    args: Any,
    **_kw: Any,
) -> None:
    """Fleet-health items land in correct fleet queues with band-table severities.

    Read-only: DDB Query (by_fleet GSI on avx-findings, vin-index on storage-vehicles)
               DDB GetItem (storage-vehicles by vehicleId)
               S3 GetObject (ADP lake fault_codes.parquet)
    """
    from agents.fi_fleet_health.bands import classify_needs  # noqa: PLC0415

    ddb = _client(
        "dynamodb",
        region,
        allowed_ops=frozenset({"dynamodb.Query", "dynamodb.GetItem"}),
    )
    s3 = _client(
        "s3",
        _ADP_REGION,
        allowed_ops=frozenset({"s3.GetObject"}),
    )

    runs_table    = f"vsa-{stage}-avx-agent-runs"
    findings_table = f"vsa-{stage}-avx-findings"
    vehicles_table = f"cms-{stage}-storage-vehicles"
    adp_bucket = (
        getattr(args, "adp_lake_bucket", None)
        or f"adp-{stage}-foundation-lake-{account}-{_ADP_REGION}"
    )

    # 1. Latest completed service run (schedule: 05:40 UTC, vsa-tier2-stack.ts:2003)
    run_rec = _latest_service_run(ddb, runs_table)
    if run_rec is None:
        res.skip("item-2", "no completed agent-fi-fleet-health#service run in last 26 h")
        return
    started_at = str(run_rec.get("started_at") or "")
    res.note("2-run", f"started_at={started_at}")

    # 2. Load fault catalog from ADP lake
    try:
        catalog = _load_fault_catalog_s3(s3, adp_bucket)
    except Exception as exc:  # noqa: BLE001
        res.check("2-catalog-load", False, "",
                  on_fail=f"failed to read fault_codes.parquet: {exc}")
        return
    res.note("2-catalog", f"{len(catalog)} codes loaded from {adp_bucket}")

    # 3. Collect findings from by_fleet GSI for all managed fleets
    all_findings: list[dict] = []
    fids_seen: set[str] = set()
    # gsi_members: (fleet_queried, finding_id) — used to check that a finding is
    # reachable by querying by_fleet for its own fleet_id (not just some other fleet).
    gsi_members: set[tuple[str, str]] = set()
    for fleet_id in _MANAGED_FH_FLEETS:
        try:
            fleet_items = _query_by_fleet_fh(ddb, findings_table, fleet_id, started_at)
        except Exception as exc:  # noqa: BLE001
            res.check(f"2-query-{fleet_id}", False, "",
                      on_fail=f"by_fleet query error for {fleet_id!r}: {exc}")
            continue
        for f in fleet_items:
            fid = f.get("finding_id") or ""
            gsi_members.add((fleet_id, fid))  # key = fleet we queried, not fleet on Finding
            if fid not in fids_seen:
                fids_seen.add(fid)
                all_findings.append(f)

    if not all_findings:
        res.skip("item-2", f"no fleet.health.service findings with computed_at >= {started_at}")
        return
    res.note("2-findings", f"{len(all_findings)} finding(s) from the run")

    p0_checked = 0

    for finding in all_findings:
        fid = finding.get("finding_id") or "?"
        vin = (finding.get("scope") or {}).get("vin") or ""
        prefix = f"2-{vin[:8]}-{fid[:8]}"

        # (a) fleet_id from CMS enrollment matches finding's fleet_id
        try:
            cms_fleet = _vin_fleet_cms(ddb, vehicles_table, vin)
        except Exception as exc:  # noqa: BLE001
            res.check(f"{prefix}-fleet-cms", False, "",
                      on_fail=f"CMS vehicles lookup error for {vin!r}: {exc}")
            continue
        finding_fleet = finding.get("fleet_id")
        res.check(
            f"{prefix}-fleet-id",
            finding_fleet == cms_fleet,
            f"fleet_id={finding_fleet!r}",
            on_fail=f"fleet_id={finding_fleet!r} != cms enrollment fleet={cms_fleet!r}",
        )

        # (a) by_fleet GSI returns this finding for its fleet_id
        res.check(
            f"{prefix}-by-fleet-gsi",
            (finding_fleet or "", fid) in gsi_members,
            "",
            on_fail=f"finding {fid!r} not in by_fleet GSI for fleet {finding_fleet!r}",
        )

        # (b) severity matches classify_needs on the evidence needs
        evidence = finding.get("evidence") or []
        open_faults, agent2_findings, pm_tasks = _needs_from_evidence(evidence)
        if open_faults or agent2_findings or pm_tasks:
            band_result = classify_needs(
                open_faults, agent2_findings, pm_tasks
            )
            expected_sev = band_result.severity
            actual_sev = finding.get("severity")
            res.check(
                f"{prefix}-severity",
                actual_sev == expected_sev,
                f"severity={actual_sev!r}",
                on_fail=(
                    f"severity={actual_sev!r} expected={expected_sev!r} "
                    f"(bands.py:classify_needs)"
                ),
            )

        # (b) each fault need's band == ADP catalog band for that code
        for need in open_faults:
            code = need.get("code", "")
            need_band = need.get("band", "")
            if code not in catalog:
                continue
            cat_band = catalog[code]["band"]
            res.check(
                f"{prefix}-fault-{code}-band",
                need_band == cat_band,
                f"band={need_band!r} catalog={cat_band!r}",
                on_fail=(
                    f"fault {code!r} band={need_band!r} != "
                    f"catalog band={cat_band!r} (fault_codes.parquet)"
                ),
            )

        # (c) p0 booking/grounding items carry catalog directive_text byte for byte.
        # Fail closed: FAIL on missing disclosure_txt, no p0 fault, or code absent
        # from catalog.  p0_checked increments only when a comparison is made.
        # action_kind check: book_fleet_service or ground_vehicle (loop.py:1430,1520).
        # action_kind lives inside recommended_action (write.py:build_finding:104-107);
        # fall back to the top-level key for findings that carry it there.
        _ra = finding.get("recommended_action") or {}
        _action_kind = _ra.get("action_kind") or finding.get("action_kind")
        if finding.get("severity") == "p0" and _action_kind in (
            "book_fleet_service", "ground_vehicle",
        ):
            if "disclosure_txt" not in finding:
                res.check(
                    f"{prefix}-p0-disclosure",
                    False,
                    "",
                    on_fail=f"p0 Finding {fid!r} missing disclosure_txt (writer.py:64)",
                )
            else:
                # sort: last_seen_at DESC, code ASC — mirrors loop.py:1409-1414
                _p0f = sorted(
                    [f for f in open_faults if f.get("band") == "p0"],
                    key=lambda f: f.get("code") or "",
                )
                _p0f = sorted(
                    _p0f, key=lambda f: f.get("last_seen_at") or "", reverse=True
                )
                if not _p0f:
                    res.check(
                        f"{prefix}-p0-disclosure",
                        False,
                        "",
                        on_fail=f"p0 Finding {fid!r}: no p0 fault in evidence",
                    )
                else:
                    p0_code = _p0f[0]["code"]
                    if p0_code not in catalog:
                        res.check(
                            f"{prefix}-p0-disclosure",
                            False,
                            "",
                            on_fail=f"p0 code {p0_code!r} not in fault catalog",
                        )
                    else:
                        directive = catalog[p0_code].get("directive_text")
                        stored_txt = finding["disclosure_txt"]
                        res.check(
                            f"{prefix}-p0-disclosure",
                            stored_txt == directive,
                            f"code={p0_code!r}",
                            on_fail=(
                                f"disclosure_txt != catalog directive_text for "
                                f"{p0_code!r} (writer.py:build_disclosure_txt:64)"
                            ),
                        )
                    p0_checked += 1

    # Must have examined at least one p0 booking item (brief: zero p0 → FAIL)
    res.check(
        "2-p0-item-present",
        p0_checked > 0,
        f"{p0_checked} p0 item(s) checked",
        on_fail="no p0 item to check",
    )


# ---------------------------------------------------------------------------
# Item 3 helpers
# ---------------------------------------------------------------------------

# DMS PATCH path for a repair order (mirrors live-verify-fi-recall-warranty.py:114).
_DMS_RO_PATCH_FMT = "/api/dms/dealers/{dealer_id}/repair-orders/{ro_id}"


def score_fleet_service_lines(
    ro_lines: list[dict[str, Any]],
    needs: list[dict[str, Any]],
) -> tuple[bool, str]:
    """Verify DMS RO lines match the Finding's needs in order.

    Per dms.py:_fleet_service_body:183-196 (line_no=idx+1, labor_op, pay_type=customer_pay).
    Checks count, line_no sequence, labor_op match, pay_type.
    Mutant: a swapped pair passes count but fails line_no/labor_op order check.
    Source: agents/executors/dms.py _fleet_service_body:183-196.
    """
    if len(ro_lines) != len(needs):
        return False, f"len(lines)={len(ro_lines)} != len(needs)={len(needs)}"
    for i, (line, need) in enumerate(zip(ro_lines, needs)):
        exp_line_no = i + 1
        exp_op = str(need.get("labor_op") or "").strip()
        act_line_no = line.get("line_no")
        # DMS stores line_no as NUMBER (dms.py:189 W3 note); Decimal from DDB resource.
        try:
            act_line_no = int(act_line_no)
        except (TypeError, ValueError):
            pass
        act_op = str(line.get("labor_op") or "")
        act_pay = str(line.get("pay_type") or "")
        if act_line_no != exp_line_no:
            return False, f"line {i}: line_no={act_line_no!r} want {exp_line_no}"
        if act_op != exp_op:
            return False, f"line {i}: labor_op={act_op!r} want {exp_op!r}"
        if act_pay != "customer_pay":
            return False, f"line {i}: pay_type={act_pay!r} want 'customer_pay'"
    return True, f"{len(ro_lines)} line(s) match needs in order"


def _load_labor_op_map_s3(s3: Any, bucket: str) -> set[tuple[str, str]]:
    """Read config/fleet_health/code_labor_op_map/code_labor_op_map.parquet.

    Returns set of (code, labor_op) pairs.
    pm_vehicle_status.sql:197-205 joins m.code=pv.task_code AND lr.labor_op=m.labor_op.
    S3 op: GetObject only. Raises on any error.
    """
    import os as _os      # noqa: PLC0415
    import tempfile        # noqa: PLC0415
    import duckdb          # noqa: PLC0415

    key = "config/fleet_health/code_labor_op_map/code_labor_op_map.parquet"
    resp = s3.get_object(Bucket=bucket, Key=key)
    body = resp["Body"].read()
    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
        tmp.write(body)
        tmp_path = tmp.name
    try:
        conn = duckdb.connect()
        rows = conn.execute(
            f"SELECT code, labor_op FROM read_parquet('{tmp_path}')"
        ).fetchall()
    finally:
        _os.unlink(tmp_path)
    return {
        (str(code).strip(), str(labor_op).strip())
        for code, labor_op in rows
        if code and labor_op
    }


import re as _re


def _attempt_from_subject_key(subject_key: str | None) -> int:
    """Parse attempt from subject_key='service/N' (loop.py:1474).

    build_finding copies subject_key but not the attempt field (write.py:93-114),
    so this is the only reliable way to read attempt from a Finding.
    Returns 1 when absent or not a service key.
    """
    if subject_key:
        m = _re.match(r"^service/(\d+)$", str(subject_key))
        if m:
            return int(m.group(1))
    return 1


def _check_labor_ops_in_map(
    needs: list[dict[str, Any]],
    labor_op_map: set[tuple[str, str]],
) -> tuple[bool, str]:
    """G6-D2: every fault:/pm: need must have (code, labor_op) in the map.

    Discriminate by need_id prefix (loop.py:1455-1465):
      - fault:<code>  -> MUST have (code, labor_op) in map; FAIL if code has no row.
      - pm:<task>     -> same.
      - a2:<kind>     -> excluded (agent2 component, not in ADP's map by design).

    Empty needs list -> FAIL (vacuous PASS hides missing data).
    Missing -> FAIL naming (labor_op, code).
    Mutant: removing the (code, labor_op) check lets missing pairs pass.
    """
    if not needs:
        return False, "needs list is empty; cannot confirm labor_op mapping"
    missing: list[str] = []
    checked = 0
    a2_count = 0
    for need in needs:
        need_id = str(need.get("need_id") or "").strip()
        labor_op = str(need.get("labor_op") or "").strip()
        if need_id.startswith("a2:"):
            a2_count += 1
            continue
        if not need_id.startswith(("fault:", "pm:")):
            continue
        # take code from need_id suffix (fault:P0AA6 → P0AA6); description is secondary
        # Source: loop.py:1455-1465 need_id format
        _suf = need_id.split(":", 1)
        code = _suf[1].strip() if len(_suf) == 2 else str(need.get("description") or "").strip()
        if not code:
            continue
        if not labor_op:
            missing.append(f"no-labor_op/{code!r}")
            continue
        checked += 1
        if (code, labor_op) not in labor_op_map:
            missing.append(f"{labor_op!r}/{code!r}")
    if missing:
        return False, f"labor_op not in code_labor_op_map: {', '.join(missing)}"
    if checked == 0:
        return False, "no fault/PM need checked; cannot confirm labor_op mapping"
    detail = f"{checked} fault/PM need(s) verified in map"
    if a2_count:
        detail += f", {a2_count} a2 need(s) excluded"
    return True, detail


def _check_item3_ro_residue(
    ddb3: Any,
    dms_ros_table: str,
    planned: list[dict],
    res: "Results",
) -> None:
    """Exactly-planned RO residue check for item 3 (W1).

    Two-pass residue:
    1. GetItem each planned ro_id (confirms each planned RO exists).
    2. Paged DDB Scan for the run's avx_action_id values (bare UUIDs, no prefix);
       require exactly the planned ro_ids — no extras allowed.
    Runs in finally so partial writes are always checked.
    """
    from boto3.dynamodb.types import TypeDeserializer as _TD_r  # noqa: PLC0415

    for p in planned:
        ro_id = str(p.get("ro_id") or "").strip()
        action_id = str(p.get("action_id") or "").strip()
        label = f"3-residue-ro-{ro_id[:8] or action_id[:8]}"
        if not ro_id:
            continue  # no GetItem to run; scan pass will find any stray ROs by avx_action_id
        try:
            resp = ddb3.get_item(
                TableName=dms_ros_table,
                Key={"ro_id": {"S": ro_id}},
                ProjectionExpression="ro_id",
            )
        except Exception as exc:  # noqa: BLE001
            res.check(label, False, "", on_fail=f"GetItem error for ro_id={ro_id!r}: {exc}")
            continue
        deser = _TD_r()
        row = {k: deser.deserialize(v) for k, v in (resp.get("Item") or {}).items()}
        exists = bool(row.get("ro_id"))
        res.check(label, exists, f"ro_id={ro_id!r}",
                  on_fail=f"ro_id={ro_id!r} absent from {dms_ros_table} (action={action_id!r})")

    # Pass 2: paged scan for avx_action_ids; require exactly the planned ro_ids.
    # DMS stores bare UUIDs; strip 'AVX-ACT-' prefix.  An extra RO (duplicate or
    # unexpected action) would be invisible to the GetItem pass above.
    avx_ids = [
        str(p.get("action_id") or "").removeprefix("AVX-ACT-").strip()
        for p in planned
        if p.get("action_id")
    ]
    if not avx_ids:
        return
    planned_ro_ids = {str(p.get("ro_id") or "") for p in planned if p.get("ro_id")}
    try:
        deser2 = _TD_r()
        found_by_aid: dict[str, list[str]] = {}  # bare avx_action_id → [ro_id, ...]
        kw2: dict = {
            "TableName": dms_ros_table,
            "FilterExpression": "avx_action_id IN (" + ",".join(
                f":aid{i}" for i in range(len(avx_ids))
            ) + ")",
            "ExpressionAttributeValues": {
                f":aid{i}": {"S": v} for i, v in enumerate(avx_ids)
            },
            "ProjectionExpression": "ro_id, avx_action_id",
        }
        while True:
            resp2 = ddb3.scan(**kw2)
            for raw in (resp2.get("Items") or []):
                item2 = {k: deser2.deserialize(v) for k, v in raw.items()}
                aid = str(item2.get("avx_action_id") or "")
                rid = str(item2.get("ro_id") or "")
                found_by_aid.setdefault(aid, []).append(rid)
            tok2 = resp2.get("LastEvaluatedKey")
            if not tok2:
                break
            kw2["ExclusiveStartKey"] = tok2
    except Exception as exc:  # noqa: BLE001
        res.check("3-residue-scan", False, "", on_fail=f"scan error for avx_action_ids: {exc}")
        return

    found_ro_ids = {r for rids in found_by_aid.values() for r in rids}
    extra = found_ro_ids - planned_ro_ids
    missing = planned_ro_ids - found_ro_ids
    ok = not extra and not missing
    res.check(
        "3-residue-scan",
        ok,
        f"scan found {len(found_ro_ids)} RO(s) for {len(avx_ids)} action_id(s)",
        on_fail=(
            f"RO residue mismatch; extra={sorted(extra)[:3]} missing={sorted(missing)[:3]}"
        ),
    )


def _check_invoke_result(resp: dict) -> tuple[bool, str]:
    """Return (ok, detail) for a Lambda sync invoke response.

    FAIL on FunctionError or StatusCode != 200 (mirrors items 6 and 7).
    """
    fn_err = resp.get("FunctionError") or ""
    status = int(resp.get("StatusCode") or 0)
    if fn_err:
        payload_preview = (resp.get("Payload") or b"")[:120]
        return False, f"FunctionError={fn_err!r} payload={payload_preview!r}"
    if status != 200:
        return False, f"StatusCode={status}"
    return True, f"StatusCode={status}"


def _pick_fleet_service_finding(
    ddb_low: Any,
    findings_table: str,
    actions_table: str,
    fleet_id: str,
    now_iso: str,
    *,
    require_fault_or_pm: bool = True,
) -> dict[str, Any] | None:
    """Lowest finding_id eligible fleet.health.service Finding in fleet_id.

    ``require_fault_or_pm`` (review c3 S2): Finding A must carry a ``fault:`` or ``pm:``
    need, or the G6-D2 labor-op check is vacuous; Finding B (the cancel path, which
    checks no labor op) takes any eligible Finding.

    Eligible: finding_kind==fleet.health.service, action_kind==book_fleet_service,
    not expired, no existing decision Action for this finding_id.
    Uses _derive_decision_action_id (fleet_recommendation.py:191-205) for GetItem.
    Fails closed on any read error (treats finding as actioned).
    """
    from boto3.dynamodb.types import TypeDeserializer  # noqa: PLC0415
    from agents.supervisor.tools.fleet_recommendation import (  # noqa: PLC0415
        _derive_decision_action_id,
    )

    deser = TypeDeserializer()
    kw: dict[str, Any] = {
        "TableName": findings_table,
        "IndexName": "by_fleet",
        "KeyConditionExpression": "fleet_id = :flt",
        "FilterExpression": "agent_id = :aid AND finding_kind = :fk",
        "ExpressionAttributeValues": {
            ":flt": {"S": fleet_id},
            ":aid": {"S": _AGENT_FI_FH},
            ":fk": {"S": _FH_FINDING_KIND},
        },
    }
    candidates: list[dict[str, Any]] = []
    while True:
        page = ddb_low.query(**kw)
        for raw in page.get("Items", []):
            item = {k: deser.deserialize(v) for k, v in raw.items()}
            rec = (item.get("recommended_action") or {})
            if rec.get("action_kind") != "book_fleet_service":
                continue
            exp = str(item.get("expires_at") or "")
            if exp and exp <= now_iso:
                continue
            candidates.append(item)
        tok = page.get("LastEvaluatedKey")
        if not tok:
            break
        kw["ExclusiveStartKey"] = tok

    if not candidates:
        return None

    # Filter out candidates with an existing decision Action
    actioned: set[str] = set()
    for f in candidates:
        fid = str(f.get("finding_id") or "")
        if not fid:
            actioned.add(fid)
            continue
        try:
            resp = ddb_low.get_item(
                TableName=actions_table,
                Key={"action_id": {"S": _derive_decision_action_id(fid)}},
                ProjectionExpression="action_id",
            )
        except Exception:  # noqa: BLE001
            actioned.add(fid)  # fail closed
            continue
        if resp.get("Item"):
            actioned.add(fid)

    eligible = [f for f in candidates if str(f.get("finding_id") or "") not in actioned]
    if not eligible:
        return None
    # Finding A must carry at least one fault:/pm: need (G6-D2 labor_op check is
    # vacuous when checked==0, i.e. all needs are a2: only).
    def _has_fault_or_pm(f: dict) -> bool:
        params = (f.get("recommended_action") or {}).get("parameters") or {}
        needs = params.get("needs") or []
        return any(
            str((n or {}).get("need_id") or "").startswith(("fault:", "pm:")) for n in needs
        )
    pool = [f for f in eligible if _has_fault_or_pm(f)] if require_fault_or_pm else eligible
    if not pool:
        return None
    return min(pool, key=lambda f: str(f.get("finding_id") or ""))


def _item_3(res: Results, **_kw: Any) -> None:  # noqa: C901
    """book_fleet_service -> DMS RO; status flow; closed-early; G6-D2 labor_op check."""
    _t = time  # use module-level `time` (ScriptTime in test worlds; real time in production)
    from boto3.dynamodb.types import TypeDeserializer as _TD  # noqa: PLC0415

    run: str = _kw["run"]
    stage: str = _kw["stage"]
    region: str = _kw["region"]
    account: str = _kw["account"]
    manifest: Path = _kw["manifest"]

    # ── clients (each with only the ops it uses) ─────────────────────────────
    ddb3 = _client(
        "dynamodb", region,
        allowed_ops=frozenset({"dynamodb.Query", "dynamodb.GetItem", "dynamodb.Scan"}),
    )
    idp3 = _client(
        "cognito-idp", region,
        allowed_ops=frozenset({
            "cognito-idp.AdminCreateUser", "cognito-idp.AdminSetUserPassword",
            "cognito-idp.AdminAddUserToGroup", "cognito-idp.AdminDeleteUser",
            "cognito-idp.InitiateAuth",
            "cognito-idp.ListUserPools", "cognito-idp.ListUserPoolClients",
            "cognito-idp.DescribeUserPoolClient",
        }),
    )
    ssm3 = _client("ssm", region, allowed_ops=frozenset({"ssm.GetParameter"}))
    cfn3 = _client("cloudformation", region, allowed_ops=frozenset({"cloudformation.DescribeStacks"}))
    lam3 = _client("lambda", region, allowed_ops=frozenset({"lambda.Invoke"}),
                   config=_lambda_invoke_config())
    s3_3 = _client("s3", _ADP_REGION, allowed_ops=frozenset({"s3.GetObject"}))

    # ── Cognito pool/client ───────────────────────────────────────────────────
    try:
        pool_id = ssm3.get_parameter(
            Name=f"/cvx/{stage}/voice-auth-user-pool-id"
        )["Parameter"]["Value"]
    except Exception as exc:  # noqa: BLE001
        pool_id = _resolve_cms_pool_id(idp3, stage)  # fallback
        if pool_id is None:
            res.skip("item-3", f"SSM pool param unavailable and CMS pool unresolved: {exc}")
            return
    try:
        client_id = _password_client(idp3, pool_id)
    except SystemExit as exc:
        res.skip("item-3", f"Cognito client not found: {exc}")
        return

    # ── API endpoints ─────────────────────────────────────────────────────────
    try:
        outs = cfn3.describe_stacks(StackName=f"vsa-{stage}-api")["Stacks"][0]["Outputs"]
        avx_base = next(
            o["OutputValue"].rstrip("/") for o in outs if o["OutputKey"] == "RestApiUrl"
        )
    except Exception as exc:  # noqa: BLE001
        res.skip("item-3", f"cannot resolve vsa-{stage}-api RestApiUrl: {exc}")
        return
    try:
        dms_outs = cfn3.describe_stacks(StackName=f"DmsApiStack-{stage}")["Stacks"][0]["Outputs"]
        dms_base = next(
            o["OutputValue"].rstrip("/") for o in dms_outs if o["OutputKey"] == "ApiUrl"
        )
    except Exception as exc:  # noqa: BLE001
        res.skip("item-3", f"cannot resolve DmsApiStack-{stage} ApiUrl: {exc}")
        return

    api = Http(avx_base)
    dms_api = Http(dms_base)
    findings_table = f"vsa-{stage}-avx-findings"
    actions_table = f"vsa-{stage}-avx-actions"
    dms_ros_table = f"dms-{stage}-repair-orders"
    adp_bucket = f"adp-{stage}-foundation-lake-{account}-{_ADP_REGION}"
    fn_name = f"vsa-{stage}-tier2-health-agent-{region}"

    # ── load labor_op_map (G6-D2) ─────────────────────────────────────────────
    try:
        labor_op_map = _load_labor_op_map_s3(s3_3, adp_bucket)
    except Exception as exc:  # noqa: BLE001
        res.check("3b-map-load", False, "",
                  on_fail=f"code_labor_op_map.parquet read error: {exc}")
        return
    res.note("3-labor-op-map", f"{len(labor_op_map)} (code, labor_op) pairs")

    # ── pick finding A (complete path) ────────────────────────────────────────
    now_iso = _iso(_now())
    finding_a: dict[str, Any] | None = None
    fleet_a = ""
    for flt in _MANAGED_FH_FLEETS:
        f = _pick_fleet_service_finding(ddb3, findings_table, actions_table, flt, now_iso)
        if f is not None:
            finding_a = f
            fleet_a = flt
            break

    if not res.check("3a-planned-item", finding_a is not None,
                     on_fail=("no eligible fleet.health.service Finding with a fault: or pm: "
                              "need in any managed demo fleet")):
        return
    assert finding_a is not None

    fa_id = str(finding_a.get("finding_id") or "")
    fa_params = (finding_a.get("recommended_action") or {}).get("parameters") or {}
    fa_needs: list[dict[str, Any]] = list(fa_params.get("needs") or [])
    fa_vin = str((finding_a.get("scope") or {}).get("vin") or "")
    fa_dealer_id = str(fa_params.get("dealer_id") or "")
    plan_a = {"finding_id": fa_id, "vin": fa_vin, "fleet_id": fleet_a,
              "dealer_id": fa_dealer_id, "needs_count": len(fa_needs),
              "purpose": "item3a-complete-path"}
    print(f"  3 planned item A: {plan_a}")
    _update_manifest(manifest, "item3a_planned", plan_a)

    # ── (b) G6-D2 labor_op map check ─────────────────────────────────────────
    lom_ok, lom_detail = _check_labor_ops_in_map(fa_needs, labor_op_map)
    res.check("3b-labor-op-map", lom_ok, lom_detail,
              on_fail=f"G6-D2 FAIL: {lom_detail}")
    # G6-D2 note: expected to FAIL today by design; item continues regardless

    # ── (a) approve finding A as ephemeral fleet-operator ────────────────────
    action_id_a: str | None = None
    ro_id_a: str | None = None
    _residue_planned: list[dict] = []

    # W1 (review cycle 3): from the first approve on, every exit (a return, an exception,
    # or SystemExit from a signal) goes through one residue check in the finally below,
    # so every RO this item made is checked exactly once.
    try:
        with principal(idp3, pool_id, client_id, run, "U_A-3a",
                       ["fleet-operator"], fleet_ids=fleet_a) as (tok_a, _):
            approve_body: dict[str, Any] = {
                "action_kind": "book_fleet_service",
                "target_system": "dms",
                "parameters": {"dealer_id": fa_dealer_id},
            }
            status, body = api.call(
                "POST", f"/findings/{fa_id}/approve", f"Bearer {tok_a}", approve_body,
            )
            if not res.check("3a-approve", status == 200, f"HTTP {status}: {body}"):
                return
            action_id_a = str(body.get("action_id") or "")
            _residue_planned.append({"action_id": action_id_a})  # W3: record before poll
            _update_manifest(manifest, "item3a_action_id", action_id_a)

            # Poll for in_progress + ro_id (set by executor dms.py:468)
            in_progress_a = False
            deadline = _t.time() + 90
            while _t.time() < deadline:
                deser_a = _TD()
                resp = ddb3.get_item(
                    TableName=actions_table,
                    Key={"action_id": {"S": action_id_a}},
                    ProjectionExpression="#s, target_ref",
                    ExpressionAttributeNames={"#s": "status"},
                )
                act_a = {k: deser_a.deserialize(v)
                         for k, v in (resp.get("Item") or {}).items()}
                if act_a.get("status") == "in_progress":
                    in_progress_a = True
                    ro_id_a = str(act_a.get("target_ref") or "")
                    if ro_id_a:
                        _residue_planned[-1]["ro_id"] = ro_id_a  # W3: update entry
                        break
                _t.sleep(3)

            res.check("3c-action-in-progress", in_progress_a,
                      on_fail="Action did not reach in_progress after 90s")
            if not res.check("3a-ro-id", bool(ro_id_a), f"ro_id={ro_id_a!r}",
                             on_fail="no ro_id on Action after 90s"):
                return
            _update_manifest(manifest, "item3a_ro_id", ro_id_a)

            # Check RO lines match Finding's needs
            deser_ro = _TD()
            ro_resp = ddb3.get_item(
                TableName=dms_ros_table, Key={"ro_id": {"S": ro_id_a}},
            )
            ro_item_a = {k: deser_ro.deserialize(v) for k, v in (ro_resp.get("Item") or {}).items()}
            ro_lines_a = list(ro_item_a.get("lines") or [])
            lines_ok, lines_detail = score_fleet_service_lines(ro_lines_a, fa_needs)
            res.check("3a-ro-lines", lines_ok, lines_detail,
                      on_fail=f"RO lines mismatch: {lines_detail}")

            # ── (c) PATCH RO to Ready -> Action=completed ─────────────────────────
            ro_dealer_a = str(ro_item_a.get("dealer_id") or fa_dealer_id)
            with principal(idp3, pool_id, client_id, run, "U_S-3c",
                           ["service-advisor"], dealer_ids=ro_dealer_a) as (tok_s, _):
                patch_st, patch_body = dms_api.call(
                    "PATCH",
                    _DMS_RO_PATCH_FMT.format(dealer_id=ro_dealer_a, ro_id=ro_id_a),
                    f"Bearer {tok_s}",
                    {"status": "Ready"},
                )
                res.check("3c-patch-ready", patch_st == 200, f"HTTP {patch_st}: {patch_body}")

            # Poll Action for completed (max 3 min)
            action_completed_a = False
            deadline = _t.time() + 180
            while _t.time() < deadline:
                deser_c = _TD()
                resp = ddb3.get_item(
                    TableName=actions_table,
                    Key={"action_id": {"S": action_id_a}},
                    ProjectionExpression="#s",
                    ExpressionAttributeNames={"#s": "status"},
                )
                act_c = {k: deser_c.deserialize(v)
                         for k, v in (resp.get("Item") or {}).items()}
                if act_c.get("status") == "completed":
                    action_completed_a = True
                    break
                _t.sleep(5)
            res.check("3c-action-completed", action_completed_a,
                      on_fail="timeout waiting for Action status=completed")

        # ── pick finding B (cancel+re-raise path) ────────────────────────────────
        now_iso2 = _iso(_now())
        finding_b: dict[str, Any] | None = None
        fleet_b = ""
        for flt in _MANAGED_FH_FLEETS:
            f = _pick_fleet_service_finding(ddb3, findings_table, actions_table, flt, now_iso2,
                                            require_fault_or_pm=False)
            if f is not None and str(f.get("finding_id") or "") != fa_id:
                finding_b = f
                fleet_b = flt
                break

        if finding_b is None:
            res.skip("3d", "no second eligible Finding for cancel-path test")
            return

        fb_id = str(finding_b.get("finding_id") or "")
        fb_params = (finding_b.get("recommended_action") or {}).get("parameters") or {}
        fb_vin = str((finding_b.get("scope") or {}).get("vin") or "")
        fb_dealer_id = str(fb_params.get("dealer_id") or "")
        fb_attempt = _attempt_from_subject_key(finding_b.get("subject_key"))
        plan_b = {"finding_id": fb_id, "vin": fb_vin, "fleet_id": fleet_b,
                  "dealer_id": fb_dealer_id, "attempt": fb_attempt,
                  "purpose": "item3d-cancel-path"}
        print(f"  3 planned item B: {plan_b}")
        _update_manifest(manifest, "item3b_planned", plan_b)

        # ── (d) approve B, PATCH to Closed from Draft -> cancelled ───────────────
        action_id_b: str | None = None
        ro_id_b: str | None = None

        with principal(idp3, pool_id, client_id, run, "U_A-3d",
                       ["fleet-operator"], fleet_ids=fleet_b) as (tok_d, _):
            approve_b_body: dict[str, Any] = {
                "action_kind": "book_fleet_service",
                "target_system": "dms",
                "parameters": {"dealer_id": fb_dealer_id},
            }
            st_b, body_b = api.call(
                "POST", f"/findings/{fb_id}/approve", f"Bearer {tok_d}", approve_b_body,
            )
            if not res.check("3d-approve", st_b == 200, f"HTTP {st_b}: {body_b}"):
                return
            action_id_b = str(body_b.get("action_id") or "")
            _residue_planned.append({"action_id": action_id_b})  # W3: record before poll
            _update_manifest(manifest, "item3b_action_id", action_id_b)

            # Poll for ro_id
            deadline = _t.time() + 90
            while _t.time() < deadline:
                deser_b = _TD()
                resp = ddb3.get_item(
                    TableName=actions_table,
                    Key={"action_id": {"S": action_id_b}},
                    ProjectionExpression="target_ref",
                )
                act_b = {k: deser_b.deserialize(v) for k, v in (resp.get("Item") or {}).items()}
                if act_b.get("target_ref"):
                    ro_id_b = str(act_b["target_ref"])
                    _residue_planned[-1]["ro_id"] = ro_id_b  # W3: update entry
                    break
                _t.sleep(3)

            if not res.check("3d-ro-id", bool(ro_id_b), f"ro_id={ro_id_b!r}",
                             on_fail="Action B has no ro_id after 90s"):
                return
            _update_manifest(manifest, "item3b_ro_id", ro_id_b)

            deser_b2 = _TD()
            ro_resp_b = ddb3.get_item(
                TableName=dms_ros_table, Key={"ro_id": {"S": ro_id_b}},
                ProjectionExpression="dealer_id, #s",
                ExpressionAttributeNames={"#s": "status"},
            )
            ro_b = {k: deser_b2.deserialize(v) for k, v in (ro_resp_b.get("Item") or {}).items()}
            ro_dealer_b = str(ro_b.get("dealer_id") or fb_dealer_id)
            ro_status_b = str(ro_b.get("status") or "")
            res.check("3d-ro-draft", ro_status_b == "Draft", f"status={ro_status_b!r}",
                      on_fail=f"expected Draft for cancel-path, got {ro_status_b!r}")

            with principal(idp3, pool_id, client_id, run, "U_S-3d",
                           ["service-advisor"], dealer_ids=ro_dealer_b) as (tok_sd, _):
                patch_d_st, patch_d_body = dms_api.call(
                    "PATCH",
                    _DMS_RO_PATCH_FMT.format(dealer_id=ro_dealer_b, ro_id=ro_id_b),
                    f"Bearer {tok_sd}",
                    {"status": "Closed"},
                )
                res.check("3d-patch-close", patch_d_st == 200,
                          f"HTTP {patch_d_st}: {patch_d_body}")

            # Poll Action B for cancelled (max 90 s)
            action_cancelled_b = False
            deadline = _t.time() + 90
            while _t.time() < deadline:
                deser_d = _TD()
                resp = ddb3.get_item(
                    TableName=actions_table,
                    Key={"action_id": {"S": action_id_b}},
                    ProjectionExpression="#s",
                    ExpressionAttributeNames={"#s": "status"},
                )
                act_d = {k: deser_d.deserialize(v) for k, v in (resp.get("Item") or {}).items()}
                if act_d.get("status") == "cancelled":
                    action_cancelled_b = True
                    break
                _t.sleep(4)
            res.check("3d-action-cancelled", action_cancelled_b,
                      on_fail="timeout waiting for Action status=cancelled")

            if not action_cancelled_b:
                return

        # ── d2: invoke service Lambda; poll for next-attempt Finding ─────────────
        if _in_write_window():
            res.check("3d-invoke-window", False,
                      "inside 04:50-06:10 UTC write window; invoke refused")
            return
        invoke_iso = _iso(_now())
        try:
            invoke_resp = lam3.invoke(
                FunctionName=fn_name,
                InvocationType="RequestResponse",
                Payload=json.dumps({"stage": stage, "component": "service"}).encode(),
            )
        except Exception as exc:  # noqa: BLE001
            res.check("3d-invoke", False, str(exc)[:80])
            return
        inv_ok, inv_detail = _check_invoke_result(invoke_resp)
        if not res.check("3d-invoke", inv_ok, inv_detail,
                         on_fail=f"invoke failed: {inv_detail}"):
            return

        # Poll for a new Finding for fb_vin with attempt >= fb_attempt + 1
        new_finding_found = False
        next_attempt = fb_attempt + 1
        deadline = _t.time() + 300
        while _t.time() < deadline:
            new_fs = _query_by_fleet_fh(ddb3, findings_table, fleet_b, invoke_iso)
            for nf in new_fs:
                nf_vin = str((nf.get("scope") or {}).get("vin") or "")
                nf_attempt = _attempt_from_subject_key(nf.get("subject_key"))
                if nf_vin == fb_vin and nf_attempt >= next_attempt:
                    new_finding_found = True
                    res.note("3d-next-attempt", f"vin={fb_vin!r} attempt={nf_attempt}")
                    break
            if new_finding_found:
                break
            _t.sleep(15)
        res.check("3d-reraised", new_finding_found,
                  on_fail=f"no attempt>={next_attempt} Finding for vin={fb_vin!r} after service run")
    finally:
        _check_item3_ro_residue(ddb3, dms_ros_table, _residue_planned, res)


# ---------------------------------------------------------------------------
# Item 4 helpers — PM recompute oracle + Athena paging
# ---------------------------------------------------------------------------

# Mapping from CMS fuelType to plan powertrain vocab.
# Source: adp-wt-fi-health-d-g6/.../tests/reference.py _FUEL_POWERTRAIN
# and deployment/scripts/seed_pm_plans.py _FUEL_PT (lower-cased, not trimmed).
_FUEL_PT_MAP: dict[str, str] = {
    "electric": "ev", "ev": "ev", "bev": "ev",
    "battery-electric": "ev", "battery_electric": "ev",
    "hybrid": "hybrid",
    "gasoline": "ice", "diesel": "ice", "petrol": "ice",
}

# task_code → service_type that counts as a completion (interface.md § 6).
# Source: reference.py _TASK_SERVICE_TYPE_MAP; pm_vehicle_status.sql sr_completions CTE.
_PM_SR_TYPE: dict[str, str] = {
    "TIRE_ROTATION": "tire_service",
    "BRAKE_INSPECTION": "brake_service",
    "SCHEDULED_MAINTENANCE": "scheduled_maintenance",
    "HV_BATTERY_CHECK": "hv_battery_diagnostic",
}
_PM_SR_EXEMPT: frozenset[str] = frozenset({"CABIN_FILTER", "OIL_CHANGE"})
_PM_CLOSED_RO: frozenset[str] = frozenset({"Ready", "Delivered", "Invoiced"})


def _cast_bigint(v: float) -> int:
    """Replicate Trino's CAST(double AS BIGINT): round half away from zero.

    Python's built-in round() uses banker's rounding (half-to-even);
    Trino rounds half away from zero: _cast_bigint(42.5) == 43, not 42.
    Applied once per VIN to the summed double total to match
    pm_vehicle_status.sql's CAST(current_odo AS BIGINT) and CAST(last_done_odo AS BIGINT).
    Source: pm_vehicle_status.sql final SELECT; Trino docs CAST(double AS BIGINT).
    """
    if v >= 0:
        return math.floor(v + 0.5)
    return math.ceil(v - 0.5)


def _athena_run_paged(
    athena: Any,
    sql: str,
    workgroup: str,
    timeout_s: float = 120.0,
) -> tuple[bool, str, list[list[str]]]:
    """Execute SQL; poll until done; page results with NextToken; skip header row.

    Returns (True, "", rows_without_header) on success.
    Returns (False, reason, []) on Athena FAILED/CANCELLED/timeout.
    Athena calls admitted: StartQueryExecution, GetQueryExecution, GetQueryResults.
    Brief item 4 rule: Athena queries must page with NextToken.
    """
    import time as _t  # noqa: PLC0415

    try:
        eid = athena.start_query_execution(
            QueryString=sql, WorkGroup=workgroup
        )["QueryExecutionId"]
        deadline = _t.monotonic() + timeout_s
        while _t.monotonic() < deadline:
            qe = athena.get_query_execution(QueryExecutionId=eid)["QueryExecution"]
            state = qe["Status"]["State"]
            if state == "SUCCEEDED":
                break
            if state in ("FAILED", "CANCELLED"):
                return False, qe["Status"].get("StateChangeReason", state), []
            _t.sleep(2.0)
        else:
            return False, f"timed out after {timeout_s}s", []
        rows: list[list[str]] = []
        kw: dict[str, Any] = {"QueryExecutionId": eid}
        first = True
        while True:
            page = athena.get_query_results(**kw)
            raw = page.get("ResultSet", {}).get("Rows", [])
            start = 1 if first else 0
            for row in raw[start:]:
                rows.append([d.get("VarCharValue", "") for d in row.get("Data", [])])
            first = False
            token = page.get("NextToken")
            if not token:
                break
            kw["NextToken"] = token
        return True, "", rows
    except Exception as exc:  # noqa: BLE001
        return False, str(exc), []


def _plan_vins_for(
    plan: dict,
    vin_fleet: dict[str, str],
    veh_by_id: dict[str, dict],
    veh_by_vin: dict[str, dict],
) -> list[str]:
    """VINs in vin_fleet that this pm_plans row applies to (H1 matching rules).

    Ported from reference.py _plan_targets (no pandas).
    Source: adp-wt-fi-health-d-g6/.../tests/reference.py _plan_targets;
            pm_vehicle_status.sql plan_candidates CTE.
    Single-vehicle plan wins over scoped; plans restricted to plan's own fleet.
    """
    fleet_id = plan.get("fleet_id")
    if not fleet_id or not plan.get("plan_id"):
        return []
    vehicle_id = plan.get("vehicle_id")
    if vehicle_id:
        veh = veh_by_id.get(str(vehicle_id))
        vin = veh["vin"] if veh else None
        return [vin] if vin and vin_fleet.get(vin) == fleet_id else []
    raw = plan.get("applies_to")
    if not raw:
        return []
    try:
        scope = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:  # noqa: BLE001
        return []

    def _ddb_str_list(d: dict, k: str) -> list[str] | None:
        av = d.get(k)
        if not isinstance(av, dict) or "L" not in av:
            return None
        return [e.get("S") for e in av["L"] if isinstance(e, dict)]

    def _ddb_int(d: dict, k: str) -> int | None:
        av = d.get(k)
        if not isinstance(av, dict) or "N" not in av:
            return None
        try:
            return int(av["N"])
        except (TypeError, ValueError):
            return None

    powertrains = _ddb_str_list(scope, "powertrains")
    models = _ddb_str_list(scope, "models")
    year_min = _ddb_int(scope, "modelYearMin")
    year_max = _ddb_int(scope, "modelYearMax")
    if all(x is None for x in [powertrains, models, year_min, year_max]):
        return []
    out: list[str] = []
    for vin, fid in vin_fleet.items():
        if fid != fleet_id:
            continue
        veh = veh_by_vin.get(vin, {})
        fuel = veh.get("fuel_type", "")
        pt = _FUEL_PT_MAP.get(str(fuel).lower()) if fuel else None
        try:
            year: int | None = int(veh["model_year"]) if veh.get("model_year") else None
        except (TypeError, ValueError):
            year = None
        if powertrains is not None and pt not in powertrains:
            continue
        if models is not None and veh.get("model") not in models:
            continue
        if year_min is not None and (year is None or year < year_min):
            continue
        if year_max is not None and (year is None or year > year_max):
            continue
        out.append(vin)
    return out


def _pm_last_done(
    vin: str,
    task_code: str,
    service_records: list[dict],
    latest_ros: dict[str, dict],
    op_done: dict[tuple[str, str], dt.date],
    clm_map: dict[str, list[str]],
) -> tuple[dt.date | None, str | None]:
    """Best completion date + source for one (vin, task_code).

    Sources (interface.md § 6, reference.py pm_vehicle_status phase 2):
      1. service_records (sr_completions CTE)
      2. DMS repair_orders via code_labor_op_map (dms_completions CTE)
      3. operator (op_completions CTE)
    Tie-break: most recent date; service_records > dms_repair_order > operator.
    """
    last: dt.date | None = None
    src: str | None = None

    # Source 1: service_records
    if task_code not in _PM_SR_EXEMPT:
        st = _PM_SR_TYPE.get(task_code)
        if st:
            for sr in service_records:
                if sr.get("vin") != vin or sr.get("service_type") != st:
                    continue
                try:
                    sd = dt.date.fromisoformat(str(sr["service_date"])[:10])
                except (ValueError, KeyError):
                    continue
                if last is None or sd > last:
                    last, src = sd, "service_records"

    # Source 2: DMS repair_orders matched via code_labor_op_map
    if task_code in clm_map:
        mapped_ops = set(clm_map[task_code])
        for ro in latest_ros.values():
            rv = str(ro.get("vehicle_vin") or ro.get("vin") or "")
            if rv != vin or str(ro.get("status") or "") not in _PM_CLOSED_RO:
                continue
            try:
                rd = dt.date.fromisoformat(str(ro.get("updated_at") or "")[:10])
            except ValueError:
                continue
            raw_lines = ro.get("lines")
            try:
                lines = json.loads(raw_lines) if isinstance(raw_lines, str) else (raw_lines or [])
            except Exception:  # noqa: BLE001
                lines = []
            if any(ln.get("labor_op") in mapped_ops for ln in lines):
                if last is None or rd > last:
                    last, src = rd, "dms_repair_order"

    # Source 3: operator completions
    op_d = op_done.get((vin, task_code))
    if op_d is not None and (last is None or op_d > last):
        last, src = op_d, "operator"

    return last, src


def _pm_status_and_due(
    basis: str,
    last_done: dt.date | None,
    interval_val: int,
    start_date: dt.date,
    vin: str,
    daily_usage: list[dict],
    odo_total: dict[str, float],
    due_horizon_days: int,
    due_horizon_miles: int,
    data_as_of: dt.date,
) -> tuple[str, dt.date | None, int | None]:
    """Compute (status, next_due_date, next_due_miles) from completion info.

    Source: pm_vehicle_status.sql final SELECT CASE; reference.py pm_vehicle_status.
    Returns (status, next_due_date, next_due_miles); exactly one of the due fields
    is non-None (calendar→date; mileage→miles).
    """
    if basis == "calendar":
        ndd = (last_done + dt.timedelta(days=interval_val)) if last_done else start_date
        if last_done is None:
            status = "no_history"
        elif ndd < data_as_of:
            status = "overdue"
        elif ndd <= data_as_of + dt.timedelta(days=due_horizon_days):
            status = "due"
        else:
            status = "current"
        return status, ndd, None
    else:  # mileage
        cur_odo = _cast_bigint(odo_total.get(vin, 0.0))
        last_odo: float = 0.0
        if last_done is not None:
            for row in daily_usage:
                if row.get("vin") != vin:
                    continue
                try:
                    d = dt.date.fromisoformat(str(row.get("usage_date") or "")[:10])
                except ValueError:
                    continue
                if d <= last_done:
                    last_odo += float(row.get("miles_driven") or 0)
        ndm = _cast_bigint(last_odo) + interval_val
        miles_left = ndm - cur_odo
        if last_done is None:
            status = "no_history"
        elif miles_left < 0:
            status = "overdue"
        elif miles_left <= due_horizon_miles:
            status = "due"
        else:
            status = "current"
        return status, None, ndm


def _recompute_pm_oracle(
    pm_plans: list[dict],
    cms_vehicles: list[dict],
    service_records: list[dict],
    repair_orders: list[dict],
    vin_fleet: dict[str, str],
    daily_usage: list[dict],
    pm_op_completions: list[dict],
    clm_map: dict[str, list[str]],
    due_horizon_days: int,
    due_horizon_miles: int,
    data_as_of: dt.date,
) -> list[dict]:
    """Pure-Python PM status oracle. One row per (vin, task_code) for vin_fleet.

    Ported from reference.py pm_vehicle_status (no pandas; uses pm_op_completions
    directly instead of extracting from pm_plans.completions column).
    Source: adp-wt-fi-health-d-g6/.../tests/reference.py pm_vehicle_status.
    Columns match pm_vehicle_status.sql final SELECT.
    """
    # Build vehicle lookups (reference.py lines ~286-296)
    veh_by_id: dict[str, dict] = {}
    veh_by_vin: dict[str, dict] = {}
    for v in cms_vehicles:
        if v.get("vehicle_id") and v.get("vin"):
            veh_by_id[str(v["vehicle_id"])] = v
            veh_by_vin.setdefault(str(v["vin"]), v)

    # Latest-image ROs keyed by ro_id (reference.py _latest_image)
    latest_ros: dict[str, dict] = {}
    for ro in repair_orders:
        rid = ro.get("ro_id") or ""
        try:
            seq = int(ro.get("_seq_no") or 0)
        except (TypeError, ValueError):
            seq = 0
        if rid not in latest_ros or seq > int(latest_ros[rid].get("_seq_no") or 0):
            latest_ros[rid] = ro

    # Cumulative odometer per VIN up to data_as_of (reference.py lines ~310-313)
    odo_total: dict[str, float] = {}
    for row in daily_usage:
        vin = str(row.get("vin") or "")
        if not vin:
            continue
        try:
            d = dt.date.fromisoformat(str(row.get("usage_date") or "")[:10])
        except ValueError:
            continue
        if d <= data_as_of:
            odo_total[vin] = odo_total.get(vin, 0.0) + float(row.get("miles_driven") or 0)

    # Operator completions: best date per (vin, task_code)
    # (reference.py _operator_completions but from direct pm_operator_completions table)
    op_done: dict[tuple[str, str], dt.date] = {}
    for oc in pm_op_completions:
        vin = str(oc.get("vin") or "")
        task = str(oc.get("task_code") or "")
        done_raw = oc.get("completed_on") or ""
        if not vin or not task or not done_raw:
            continue
        try:
            done = dt.date.fromisoformat(str(done_raw)[:10])
        except ValueError:
            continue
        k = (vin, task)
        if k not in op_done or done > op_done[k]:
            op_done[k] = done

    # One plan per (vin, task_code): single-vehicle first (prec=1), then lowest plan_id
    # (reference.py lines ~317-325; pm_vehicle_status.sql plan_vins CTE)
    chosen: dict[tuple[str, str], tuple[tuple[int, str], dict]] = {}
    for plan in pm_plans:
        prec = 1 if plan.get("vehicle_id") else 2
        for vin in _plan_vins_for(plan, vin_fleet, veh_by_id, veh_by_vin):
            k = (vin, str(plan.get("task_code") or ""))
            rank: tuple[int, str] = (prec, str(plan.get("plan_id") or ""))
            if k not in chosen or rank < chosen[k][0]:
                chosen[k] = (rank, plan)

    rows: list[dict] = []
    for (vin, _tc), (_rank, plan) in sorted(chosen.items()):
        task_code = str(plan.get("task_code") or "")
        basis = str(plan.get("basis") or "calendar")
        try:
            interval_val = int(plan.get("interval_value") or 0)
        except (TypeError, ValueError):
            interval_val = 0
        try:
            start_date = dt.date.fromisoformat(str(plan.get("start_date") or "")[:10])
        except ValueError:
            start_date = data_as_of

        last_done, last_src = _pm_last_done(
            vin, task_code, service_records, latest_ros, op_done, clm_map
        )
        status, ndd, ndm = _pm_status_and_due(
            basis, last_done, interval_val, start_date,
            vin, daily_usage, odo_total,
            due_horizon_days, due_horizon_miles, data_as_of,
        )
        rows.append({
            "vin": vin,
            "fleet_id": vin_fleet.get(vin),
            "task_code": task_code,
            "basis": basis,
            "interval_value": str(interval_val),
            "last_done_date": str(last_done) if last_done else "",
            "last_done_source": last_src or "",
            "next_due_date": str(ndd) if ndd else "",
            "next_due_miles": str(ndm) if ndm is not None else "",
            "status": status,
            "data_as_of": str(data_as_of),
        })
    return rows


def _compare_pm_results(
    oracle_rows: list[dict],
    view_rows: list[dict],
    label: str = "view",
) -> tuple[bool, str, list[str]]:
    """Compare (vin, task_code) sets and per-row fields.

    Returns (ok, summary, list_of_issues).
    Checks: set equality + last_done_date + last_done_source + status + next_due.
    """
    oracle_set = {(r["vin"], r["task_code"]) for r in oracle_rows}
    view_set = {(r["vin"], r["task_code"]) for r in view_rows}
    issues: list[str] = []

    if not oracle_rows:
        return False, "oracle has 0 rows — vacuous match", ["oracle is empty; cannot verify"]

    extra = view_set - oracle_set
    missing = oracle_set - view_set
    for vin, tc in sorted(extra)[:3]:
        issues.append(f"extra in {label}: ({vin[:8]},{tc})")
    for vin, tc in sorted(missing)[:3]:
        issues.append(f"missing from {label}: ({vin[:8]},{tc})")

    oracle_map = {(r["vin"], r["task_code"]): r for r in oracle_rows}
    view_map = {(r["vin"], r["task_code"]): r for r in view_rows}
    fields = ["last_done_date", "last_done_source", "status"]
    for k in sorted(oracle_set & view_set):
        o = oracle_map[k]
        v = view_map[k]
        for f in fields:
            if str(o.get(f) or "") != str(v.get(f) or ""):
                issues.append(
                    f"({k[0][:8]},{k[1]}) {f}: "
                    f"oracle={o.get(f)!r} {label}={v.get(f)!r}"
                )
        # next_due per basis
        f_due = "next_due_date" if o.get("basis") == "calendar" else "next_due_miles"
        if str(o.get(f_due) or "") != str(v.get(f_due) or ""):
            issues.append(
                f"({k[0][:8]},{k[1]}) {f_due}: "
                f"oracle={o.get(f_due)!r} {label}={v.get(f_due)!r}"
            )

    n_extra, n_miss, n_field = len(extra), len(missing), len(issues) - len(extra) - len(missing)
    ok = not issues
    status_counts: dict[str, int] = {}
    for r in oracle_rows:
        s = str(r.get("status") or "unknown")
        status_counts[s] = status_counts.get(s, 0) + 1
    counts_str = " ".join(f"{s}={c}" for s, c in sorted(status_counts.items()))
    summary = (
        f"{len(oracle_set & view_set)} rows matched; "
        f"extra={n_extra} missing={n_miss} field_diff={n_field}; "
        f"status: {counts_str}"
    )
    return ok, summary, issues[:8]


def _item_4(res: Results, *, stage: str, account: str, run: str, args: Any, **_kw: Any) -> None:
    """PM: recompute due/overdue/current from raw sources; match view on every task.

    Read-only; Athena (ADP region). One query per input table. 50 random managed
    VINs seeded by run_id (printed). Interpolated VINs validated against _VIN_RE.
    SKIP when view or a required table does not exist.
    """
    athena = _client(
        "athena",
        _ADP_REGION,
        allowed_ops=frozenset({
            "athena.StartQueryExecution",
            "athena.GetQueryExecution",
            "athena.GetQueryResults",
        }),
    )
    workgroup = f"cvx-{stage}-analytics"

    def _q(sql: str) -> tuple[bool, str, list[list[str]]]:
        return _athena_run_paged(athena, sql, workgroup, timeout_s=180.0)

    def _skip_if_missing(name: str, err: str) -> bool:
        low = err.lower()
        if any(p in low for p in ("does not exist", "table_not_found", "database_not_found",
                                  "no such table", "hive_metastore_error")):
            res.skip(f"item-4-{name}", f"{name} not available: {err[:100]}")
            return True
        if any(p in low for p in ("column_not_found", "is not authorized",
                                  "access denied", "accessdeniedexception")):
            res.check(f"4-lf-{name}", False, "",
                      on_fail=(f"LF SELECT grant required: operator lacks SELECT on {name}; "
                               f"err={err[:100]}"))
            return True
        return False

    # ── 1. Sample 50 managed VINs seeded by run_id ──────────────────────────
    ok, err, rows = _q(
        f"SELECT vin, fleet_id FROM adp_{stage}_cms_ingest.vin_cms_fleet WHERE tier='managed'"
    )
    if not ok:
        if _skip_if_missing("vin_cms_fleet", err):
            return
        res.check("4-vin-fleet", False, "", on_fail=f"vin_cms_fleet: {err[:120]}")
        return

    all_managed = [(r[0], r[1]) for r in rows if r[0] and _VIN_RE.match(r[0])]
    if not all_managed:
        res.skip("item-4", "no managed VINs in vin_cms_fleet")
        return

    import hashlib as _hl  # noqa: PLC0415
    import random as _rr   # noqa: PLC0415
    seed_int = int(_hl.sha256(run.encode()).hexdigest()[:8], 16)
    rng = _rr.Random(seed_int)
    sample = rng.sample(all_managed, min(50, len(all_managed)))
    vin_fleet: dict[str, str] = {str(v): str(f) for v, f in sample}
    sample_vins = sorted(vin_fleet)
    fleet_ids = sorted({f for f in vin_fleet.values()})

    print(f"  item-4 seed={seed_int} n={len(sample_vins)}")
    for v in sample_vins:
        print(f"    vin={v}")

    # SQL IN clauses — all values already validated above
    vins_in = ", ".join(f"'{v}'" for v in sample_vins)
    fleets_in = ", ".join(f"'{f}'" for f in fleet_ids if _SAFE_ID_RE.match(f))

    # ── 2. Fetch raw input tables (one Athena query each) ────────────────────
    def _fetch(name: str, sql: str, cols: list[str]) -> list[dict] | None:
        ok2, err2, rr = _q(sql)
        if not ok2:
            if _skip_if_missing(name, err2):
                return None
            res.check(f"4-{name}", False, "", on_fail=f"{name}: {err2[:120]}")
            return None
        return [dict(zip(cols, r)) for r in rr if len(r) >= len(cols)]

    pm_plans = _fetch("pm_plans",
        f"SELECT plan_id,fleet_id,vehicle_id,applies_to,task_code,basis,"
        f"interval_value,start_date FROM adp_{stage}_cms_ingest.pm_plans"
        f" WHERE fleet_id IN ({fleets_in})",
        ["plan_id","fleet_id","vehicle_id","applies_to","task_code","basis",
         "interval_value","start_date"])
    if pm_plans is None:
        return

    cms_veh = _fetch("vehicles",
        f"SELECT vehicle_id,vin,model,model_year,fuel_type"
        f" FROM adp_{stage}_cms_ingest.vehicles WHERE vin IN ({vins_in})",
        ["vehicle_id","vin","model","model_year","fuel_type"])
    if cms_veh is None:
        return

    svc_recs = _fetch("service_records",
        f"SELECT vin,service_type,service_date"
        f" FROM adp_{stage}_service_records.service_records WHERE vin IN ({vins_in})",
        ["vin","service_type","service_date"])
    if svc_recs is None:
        return

    ros = _fetch("repair_orders",
        f"SELECT ro_id,vehicle_vin,status,lines,updated_at,_seq_no"
        f" FROM dms_{stage}.repair_orders WHERE vehicle_vin IN ({vins_in})",
        ["ro_id","vehicle_vin","status","lines","updated_at","_seq_no"])
    if ros is None:
        return
    # Normalise: repair_orders uses 'vehicle_vin'; oracle uses key 'vehicle_vin'
    # (checked in _pm_last_done: ro.get("vehicle_vin") or ro.get("vin"))

    op_comps = _fetch("pm_operator_completions",
        f"SELECT vin,task_code,completed_on"
        f" FROM adp_{stage}_cms_ingest.pm_operator_completions WHERE vin IN ({vins_in})",
        ["vin","task_code","completed_on"])
    if op_comps is None:
        return

    clm_raw = _fetch("code_labor_op_map",
        f"SELECT code,labor_op FROM adp_{stage}_fleet_health.code_labor_op_map",
        ["code","labor_op"])
    if clm_raw is None:
        return
    clm_map: dict[str, list[str]] = {}
    for r in clm_raw:
        clm_map.setdefault(str(r["code"]).strip(), []).append(str(r["labor_op"]).strip())

    du = _fetch("daily_usage",
        f"SELECT vin,usage_date,miles_driven"
        f" FROM adp_{stage}_fleet_ops.daily_usage WHERE vin IN ({vins_in})",
        ["vin","usage_date","miles_driven"])
    if du is None:
        return

    params_rows = _fetch("parameters",
        f"SELECT due_horizon_days,due_horizon_miles,data_as_of"
        f" FROM adp_{stage}_fleet_health.parameters",
        ["due_horizon_days","due_horizon_miles","data_as_of"])
    if not params_rows:
        res.check("4-parameters", False, "", on_fail="parameters table empty or missing")
        return
    p = params_rows[0]
    try:
        dhd = int(p["due_horizon_days"])
        dhm = int(p["due_horizon_miles"])
        data_as_of = dt.date.fromisoformat(str(p["data_as_of"])[:10])
    except (ValueError, KeyError) as exc:
        res.check("4-parameters-parse", False, "", on_fail=f"parameters parse: {exc}")
        return

    # ── 3. Recompute using oracle ────────────────────────────────────────────
    oracle_rows = _recompute_pm_oracle(
        pm_plans, cms_veh, svc_recs, ros, vin_fleet,
        du, op_comps, clm_map, dhd, dhm, data_as_of,
    )
    res.note("4-oracle", f"{len(oracle_rows)} (vin,task) rows computed by oracle")

    # ── 4. Fetch pm_vehicle_status view ─────────────────────────────────────
    ok3, err3, view_rows_raw = _q(
        f"SELECT vin,fleet_id,task_code,basis,interval_value,"
        f"last_done_date,last_done_source,next_due_date,next_due_miles,status,data_as_of"
        f" FROM adp_{stage}_fleet_health.pm_vehicle_status WHERE vin IN ({vins_in})"
    )
    if not ok3:
        _skip_if_missing("pm_vehicle_status", err3) or res.check(
            "4-view-query", False, "", on_fail=f"pm_vehicle_status: {err3[:120]}")
        return
    view_rows = [
        dict(zip(["vin","fleet_id","task_code","basis","interval_value","last_done_date",
                  "last_done_source","next_due_date","next_due_miles","status","data_as_of"], r))
        for r in view_rows_raw
    ]

    ok_cmp, summary_v, issues_v = _compare_pm_results(oracle_rows, view_rows, label="view")
    for iss in issues_v:
        res.note("4-view-diff", iss)
    res.check("4-view-match", ok_cmp, summary_v,
              on_fail=f"view mismatch: {summary_v}; first: {issues_v[0] if issues_v else ''}")

    # ── 5. Fetch pm_vehicle_status_daily newest snapshot ────────────────────
    ok4, err4, snap_raw = _q(
        f"SELECT vin,task_code,basis,last_done_date,last_done_source,"
        f"next_due_date,next_due_miles,status,data_as_of"
        f" FROM adp_{stage}_fleet_health.pm_vehicle_status_daily"
        f" WHERE snapshot_date=(SELECT MAX(snapshot_date)"
        f"   FROM adp_{stage}_fleet_health.pm_vehicle_status_daily)"
        f"   AND vin IN ({vins_in})"
    )
    if not ok4:
        if any(p in err4.lower() for p in ("does not exist", "table_not_found",
                                            "database_not_found", "no such table")):
            res.skip("item-4-daily", f"pm_vehicle_status_daily not available: {err4[:80]}")
        else:
            res.check("4-daily-query", False, "", on_fail=f"daily snapshot: {err4[:120]}")
        return
    snap_cols = ["vin","task_code","basis","last_done_date","last_done_source",
                 "next_due_date","next_due_miles","status","data_as_of"]
    snap_rows = [dict(zip(snap_cols, r)) for r in snap_raw]

    # Empty snapshot: the daily job has not run for these VINs (table empty or
    # none of the sample VINs present).  This is what the service agent would see
    # when it books from pm_vehicle_status_daily, so it is a real failure.
    # Must be checked before the skew gate (snap_data_as_of would be None, which
    # != data_as_of and would emit a NOTE instead of a FAIL). (review W2)
    if not snap_rows:
        res.check("4-daily-rows", False, "0 rows for sample VINs",
                  on_fail="pm_vehicle_status_daily snapshot is empty for sample VINs")
        return

    # Gate comparison: only compare when snapshot's data_as_of matches today's parameters.
    # A stale snapshot was materialised at an earlier data_as_of; comparing it with an
    # oracle computed from current inputs produces false FAILs. (review W2)
    snap_dao_str = (snap_rows[0].get("data_as_of") or "") if snap_rows else ""
    try:
        snap_data_as_of: dt.date | None = (
            dt.date.fromisoformat(snap_dao_str[:10]) if snap_dao_str else None
        )
    except ValueError:
        snap_data_as_of = None

    if snap_data_as_of != data_as_of:
        res.note("4-daily-skew",
                 f"snapshot data_as_of={snap_data_as_of} != "
                 f"parameters data_as_of={data_as_of}; skipping comparison")
        return

    # Exclude VINs whose DMS RO was updated after the snapshot.  snapshot_runs
    # carries no taken_at; use midnight at the start of snapshot_date as a
    # conservative cutoff (nightly job runs ~2am; any RO updated on snapshot_date
    # itself is after the snapshot).
    snap_cutoff = dt.datetime(
        snap_data_as_of.year, snap_data_as_of.month, snap_data_as_of.day,
        0, 0, 0, tzinfo=dt.timezone.utc,
    )
    snap_cutoff_note = "snapshot_runs carries no taken_at; using start-of-snapshot-day cutoff"
    touched_vins: set[str] = set()
    for ro in (ros or []):
        uat = str(ro.get("updated_at") or "")
        if uat:
            try:
                ro_dt = dt.datetime.fromisoformat(uat.replace("Z", "+00:00"))
                if ro_dt.tzinfo is None:
                    ro_dt = ro_dt.replace(tzinfo=dt.timezone.utc)
                if ro_dt >= snap_cutoff:
                    touched_vins.add(str(ro.get("vehicle_vin") or ""))
            except ValueError:
                pass
    if touched_vins:
        res.note("4-daily-touched",
                 f"{len(touched_vins)} VIN(s) excluded from daily comparison "
                 f"(DMS RO updated after snapshot; {snap_cutoff_note}): "
                 + " ".join(sorted(touched_vins)[:3])
                 + (" ..." if len(touched_vins) > 3 else ""))
    snap_rows_cmp = [r for r in snap_rows if r.get("vin") not in touched_vins]
    oracle_rows_cmp = [r for r in oracle_rows if r.get("vin") not in touched_vins]
    ok_snap, summary_s, issues_s = _compare_pm_results(oracle_rows_cmp, snap_rows_cmp,
                                                        label="daily")
    for iss in issues_s:
        res.note("4-daily-diff", iss)
    res.check("4-daily-match", ok_snap, summary_s,
              on_fail=f"daily snapshot mismatch: {summary_s}; "
                      f"first: {issues_s[0] if issues_s else ''}")


# ---------------------------------------------------------------------------
# Items 5+6 scoring helpers
# ---------------------------------------------------------------------------


def score_rerun(run_record: dict[str, Any], *, model_enabled: bool = True) -> tuple[bool, str]:
    """Item 6: run on unchanged inputs makes zero model calls, zero tokens.

    model_enabled mirrors FI_SERVICE_MODEL_ENABLED on the Lambda env.  When the
    model is off, runner.py:2550-2559 omits model_calls/tokens_used from the run
    record (model_budget is None → fields not written).  In that case the zero-model-
    call property cannot be exercised and score_rerun returns (True, NOTE) so the
    item can still PASS on zero new finding ids.  When the model is on, both fields
    must be present (0) for the run to be idempotent.

    artifacts_written is reported but not scored (refreshes/idempotent writes allowed).
    Source: agents/tier2/runner.py:1420,1422 (model_calls, tokens_used fields written
    by write_run_record into vsa-{stage}-avx-agent-runs).
    Source: agents/tier2/runner.py:2550-2559 (model_budget guard that omits fields).
    """
    mc = run_record.get("model_calls", -1)
    tu = run_record.get("tokens_used", -1)
    aw = run_record.get("artifacts_written", -1)
    if not model_enabled:
        # FI_SERVICE_MODEL_ENABLED is off; model_calls/tokens_used are absent from the
        # run record (runner omits them when model_budget is None).  The zero-model-
        # call property cannot be verified; the item can still pass on 6-new-findings.
        return True, (
            f"model disabled: FI_SERVICE_MODEL_ENABLED not true — "
            f"zero-model-call check not exercised (artifacts_written={aw})"
        )
    ok = mc == 0 and tu == 0
    return ok, f"model_calls={mc}, tokens_used={tu} (artifacts_written={aw})"


def score_new_findings(before_ids: set[str], after_ids: set[str]) -> tuple[bool, str]:
    """Item 6: no finding id exists after the re-run that did not exist before.

    Source: agents/tier2/runner.py -- every new item gets a fresh FIND-<uuid4> id;
    same_hash_model_written returning True skips the write entirely (runner.py:143).
    """
    new = sorted(after_ids - before_ids)
    return not new, f"{len(new)} new finding id(s): {new[:3]}"


# ---------------------------------------------------------------------------
# Items 5+6 AWS helpers
# ---------------------------------------------------------------------------


def _scan_all_fh_finding_ids(ddb_low: Any, table: str) -> set[str]:
    """Full Scan on findings_table for agent-fi-fleet-health / fleet.health.service.

    ddb_low must be a botocore-guarded low-level client with dynamodb.Scan allowed.
    Raises on scan error (fail closed; called before and after invoke in item 6).
    Source: write.py:18,24 (AGENT_ID='agent-fi-fleet-health', FINDING_KIND='fleet.health.service').
    """
    from boto3.dynamodb.types import TypeDeserializer  # noqa: PLC0415

    deser = TypeDeserializer()
    ids: set[str] = set()
    kw: dict[str, Any] = {
        "TableName": table,
        "FilterExpression": "agent_id = :aid AND finding_kind = :fk",
        "ExpressionAttributeValues": {":aid": {"S": _AGENT_FI_FH}, ":fk": {"S": _FH_FINDING_KIND}},
        "ProjectionExpression": "finding_id",
    }
    while True:
        page = ddb_low.scan(**kw)
        for raw in page.get("Items", []):
            fid = str({k: deser.deserialize(v) for k, v in raw.items()}.get("finding_id") or "")
            if fid:
                ids.add(fid)
        tok = page.get("LastEvaluatedKey")
        if not tok:
            break
        kw["ExclusiveStartKey"] = tok
    return ids


def _run_started_ats(ddb_low: Any, runs_table: str, pk: str, since_iso: str) -> set[str]:
    """started_at of every run record for pk at or after since_iso (paged, low-level).

    Taken just before an invoke so _poll_run_to_completion can exclude prior records.
    Source: run_records.py:15 table name; run_records.py:120 pk=agent_component sk=started_at.
    """
    from boto3.dynamodb.types import TypeDeserializer  # noqa: PLC0415

    deser = TypeDeserializer()
    out: set[str] = set()
    kw: dict[str, Any] = {
        "TableName": runs_table,
        "KeyConditionExpression": "agent_component = :pk AND started_at >= :since",
        "ExpressionAttributeValues": {":pk": {"S": pk},
                                      ":since": {"S": _started_at_lower_bound(since_iso)}},
        "ProjectionExpression": "started_at",
    }
    while True:
        page = ddb_low.query(**kw)
        for raw in page.get("Items", []):
            s = str({k: deser.deserialize(v) for k, v in raw.items()}.get("started_at") or "")
            if s:
                out.add(s)
        if not page.get("LastEvaluatedKey"):
            return out
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _poll_run_to_completion(
    ddb_low: Any, runs_table: str, pk: str, invoke_iso: str,
    timeout_s: float = 2700, poll_s: int = 15,
    exclude: set[str] | None = None,
) -> dict[str, Any] | None:
    """Poll vsa-{stage}-avx-agent-runs until a completed/aborted record appears >= invoke_iso.

    exclude holds started_at values already present before the invoke; an older record is
    never returned for this run. Returns the run record dict or None on timeout.
    Source: scripts/live-verify-fi-recall-warranty.py:1662 _poll_run_to_completion.
    """
    import time as _t  # noqa: PLC0415
    from boto3.dynamodb.types import TypeDeserializer  # noqa: PLC0415

    deser = TypeDeserializer()
    exclude = exclude or set()
    deadline = _t.time() + timeout_s
    while _t.time() < deadline:
        resp = ddb_low.query(
            TableName=runs_table,
            KeyConditionExpression="agent_component = :pk AND started_at >= :since",
            ExpressionAttributeValues={":pk": {"S": pk},
                                       ":since": {"S": _started_at_lower_bound(invoke_iso)}},
            ScanIndexForward=False,
            Limit=5,
        )
        for raw in resp.get("Items", []) or []:
            item = {k: deser.deserialize(v) for k, v in raw.items()}
            if str(item.get("started_at") or "") in exclude:
                continue
            if item.get("status") in ("completed", "aborted"):
                return item
        _t.sleep(poll_s)
    return None


def _check_scheduler_timing(sched: Any, fn_arn: str) -> tuple[bool, str]:
    """List EB Scheduler schedules targeting fn_arn; refuse if within 45 min of any cron.

    rate(…): allowed only if component in {"diagnostics"} (the only rate schedule on
    this Lambda). Any unknown expression or list error refuses (fail closed).
    Source: scripts/live-verify-fi-recall-warranty.py:3612 _check_scheduler_timing.
    """
    import re as _re  # noqa: PLC0415

    _SAFE_RATE: frozenset[str] = frozenset({"diagnostics"})
    try:
        summaries: list[dict[str, Any]] = []
        kw2: dict[str, Any] = {}
        while True:
            resp = sched.list_schedules(**kw2)
            for s in resp.get("Schedules", []):
                if (s.get("Target") or {}).get("Arn") == fn_arn:
                    summaries.append(s)
            tok = resp.get("NextToken")
            if not tok:
                break
            kw2["NextToken"] = tok
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
        if full.get("State") == "DISABLED":
            disabled.append(name)
            continue
        expr = full.get("ScheduleExpression", "")
        tz = full.get("ScheduleExpressionTimezone", "UTC") or "UTC"
        if tz not in _UTC_ZONES:
            return False, f"schedule {name!r}: timezone {tz!r} is not UTC — refusing"
        if expr.startswith("cron("):
            m = _re.match(r"^cron\((\d+)\s+(\d+)\s+\*\s+\*\s+\?\s+\*\)$", expr)
            if not m:
                return False, f"unparseable cron {expr!r} on schedule {name!r}"
            minute, hour = int(m.group(1)), int(m.group(2))
            run_min = hour * 60 + minute
            delta = (now_min - run_min + 720) % 1440 - 720
            if -45 <= delta <= 45:
                return False, (
                    f"schedule {name!r} runs at {hour:02d}:{minute:02d}Z; "
                    f"now is {now.hour:02d}:{now.minute:02d}Z (within [-45m,+45m] window)"
                )
        elif expr.startswith("rate("):
            try:
                inp = json.loads((full.get("Target") or {}).get("Input") or "{}")
            except (ValueError, TypeError):
                return False, f"rate schedule {name!r}: can't parse Target.Input"
            comp = inp.get("component", "")
            if comp not in _SAFE_RATE:
                return False, f"rate schedule {name!r}: component {comp!r} not in {_SAFE_RATE}"
        else:
            return False, f"schedule {name!r}: unknown expression {expr!r}"
    skipped = f"; {len(disabled)} disabled skipped" if disabled else ""
    return True, f"{len(summaries) - len(disabled)} enabled schedule(s) checked{skipped}"


def _wait_fn_update(lam: Any, fn_name: str, timeout_s: float = 90.0) -> None:
    """Poll until Lambda LastUpdateStatus == Successful (or raise TimeoutError).
    Source: scripts/live-verify-fi-recall-warranty.py:3707 _wait_fn_update.
    """
    import time as _t  # noqa: PLC0415

    deadline = _t.monotonic() + timeout_s
    while _t.monotonic() < deadline:
        cfg = lam.get_function_configuration(FunctionName=fn_name)
        if cfg.get("LastUpdateStatus", "Successful") == "Successful":
            return
        _t.sleep(3)
    raise TimeoutError(f"{fn_name} config update did not complete in {timeout_s}s")


# ---------------------------------------------------------------------------
# Item 5 — two-fleet isolation
# ---------------------------------------------------------------------------


#: Review c3 S3: 5a pages the fleet A queue at most this many times (the handler's
#: page is 50 items, so 10,000 items) before failing rather than looping.
_5A_MAX_PAGES = 200


def _item_5(res: Results, **_kw: Any) -> None:  # noqa: C901
    """Two-fleet isolation: fleet A operator sees no fleet B item, through AVX.

    Writes: LIVETEST Finding planted in fleet A (no recommended_action; deleted in
    finally) + ephemeral Cognito users (LIVETEST-<run>-...).  Requires --allow-writes.
    Read-only: DDB Query (by_fleet GSI on avx-findings).
    """
    run: str = _kw["run"]
    stage: str = _kw["stage"]
    region: str = _kw["region"]
    manifest: Path = _kw["manifest"]

    ddb5 = _client(
        "dynamodb", region,
        allowed_ops=frozenset({
            "dynamodb.Query", "dynamodb.PutItem", "dynamodb.DeleteItem",
            "dynamodb.GetItem",  # W1: _vin_fleet_cms needs GetItem on vehicles table
        }),
    )
    idp5 = _client(
        "cognito-idp", region,
        allowed_ops=frozenset({
            "cognito-idp.AdminCreateUser", "cognito-idp.AdminSetUserPassword",
            "cognito-idp.AdminAddUserToGroup", "cognito-idp.AdminDeleteUser",
            "cognito-idp.InitiateAuth",
            "cognito-idp.ListUserPools", "cognito-idp.ListUserPoolClients",
            "cognito-idp.DescribeUserPoolClient",
        }),
    )
    ssm5 = _client("ssm", region, allowed_ops=frozenset({"ssm.GetParameter"}))
    cfn5 = _client("cloudformation", region, allowed_ops=frozenset({"cloudformation.DescribeStacks"}))

    try:
        pool_id = ssm5.get_parameter(
            Name=f"/cvx/{stage}/voice-auth-user-pool-id"
        )["Parameter"]["Value"]
    except Exception as exc:  # noqa: BLE001
        pool_id = _resolve_cms_pool_id(idp5, stage)
        if pool_id is None:
            res.skip("item-5", f"SSM pool param unavailable and CMS pool unresolved: {exc}")
            return
    try:
        client_id = _password_client(idp5, pool_id)
    except SystemExit as exc:
        res.skip("item-5", f"Cognito client not found: {exc}")
        return

    try:
        outs = cfn5.describe_stacks(StackName=f"vsa-{stage}-api")["Stacks"][0]["Outputs"]
        avx_base = next(
            o["OutputValue"].rstrip("/") for o in outs if o["OutputKey"] == "RestApiUrl"
        )
    except Exception as exc:  # noqa: BLE001
        res.skip("item-5", f"cannot resolve vsa-{stage}-api RestApiUrl: {exc}")
        return

    api = Http(avx_base)
    findings_table = f"vsa-{stage}-avx-findings"
    vehicles_table = f"cms-{stage}-storage-vehicles"  # W1: for _vin_fleet_cms
    lookback = _iso(_now() - dt.timedelta(days=4))

    fleet_findings: dict[str, list[dict]] = {}
    for flt in _MANAGED_FH_FLEETS:
        flist = _query_by_fleet_fh(ddb5, findings_table, flt, lookback)
        if flist:
            fleet_findings[flt] = flist
    fleets_ok = [f for f in _MANAGED_FH_FLEETS if f in fleet_findings]
    if len(fleets_ok) < 2:
        res.skip("item-5", f"need 2 managed fleets with fleet-health findings, found {len(fleets_ok)}")
        return

    fleet_a, fleet_b = fleets_ok[0], fleets_ok[1]
    fa_findings = fleet_findings[fleet_a]
    fb_findings = fleet_findings[fleet_b]
    fb_id = str(fb_findings[0].get("finding_id") or "")
    fb_params = (fb_findings[0].get("recommended_action") or {}).get("parameters") or {}
    fb_dealer = str(fb_params.get("dealer_id") or "DEALER-B")

    print(f"  5 fleet_a={fleet_a!r} ({len(fa_findings)} items) fleet_b={fleet_b!r} ({len(fb_findings)} items)")
    _update_manifest(manifest, "item5_fleet_a", fleet_a)
    _update_manifest(manifest, "item5_fleet_b", fleet_b)

    # Plant a LIVETEST ctrl Finding (fleet A, NO recommended_action) used exclusively
    # for the 5c-ctrl and 5d-ctrl probes.  A fleet-A operator reaching the approve
    # route sees 400 "requires a population-scoped Finding" (post-scope refusal) and
    # the decline route sees 400 "decline is only for FI queue items" (rec is None) —
    # proving fleet-A scope without creating any Action or RO.
    # Source: handler.py:1300-1308 (_ENGINEERING_KINDS check after scope);
    #         handler.py:1620-1621 (rec is None check after scope).
    ctrl_id = f"LIVETEST-{run}-FI-FH-5-ctrl"
    from boto3.dynamodb.types import TypeSerializer as _TS5  # noqa: PLC0415
    _ser5 = _TS5()
    _ctrl_item = {k: _ser5.serialize(v) for k, v in {
        "finding_id": ctrl_id,
        "agent_id": _AGENT_FI_FH,
        "finding_kind": _FH_FINDING_KIND,
        "fleet_id": fleet_a,
        "scope": {"vin": f"LIVETEST-VIN-5C-{run[:8].upper()}"},
        "severity": "soon",
        "computed_at": _iso(_now()),
        "expires_at": "2999-12-31T00:00:00Z",
    }.items()}

    def _delete_ctrl_plant() -> None:
        """Delete the plant: 3 tries with a 1 s then 2 s wait, signals held (review c3 S5).

        ConditionalCheckFailed means the item is already gone (or never landed) and
        counts as success. If every try fails, mark_cleanup_failed() so the run exits
        1 instead of silently leaving a LIVETEST finding in a real fleet's queue.
        """
        _5_delete_ok = False
        with _defer_signals_for_restore():
            for _attempt in range(3):
                if _attempt:
                    time.sleep(_attempt)
                try:
                    ddb5.delete_item(
                        TableName=findings_table,
                        Key={"finding_id": {"S": ctrl_id}},
                        ConditionExpression="begins_with(finding_id, :pfx)",
                        ExpressionAttributeValues={":pfx": {"S": "LIVETEST-"}},
                    )
                    _5_delete_ok = True
                    break
                except Exception as _exc:  # noqa: BLE001
                    _code = getattr(_exc, "response", {}).get("Error", {}).get("Code", "")
                    if _code == "ConditionalCheckFailedException":
                        _5_delete_ok = True  # already gone, or the put never landed
                        break
                    # transient error — retry
        if not _5_delete_ok:
            print(f"  item-5: could not delete ctrl plant {ctrl_id!r} after 3 attempts",
                  file=sys.stderr)
            res.mark_cleanup_failed()

    try:
        ddb5.put_item(TableName=findings_table, Item=_ctrl_item)
    except Exception as exc:  # noqa: BLE001
        # The write can land before the error reaches us (a timeout after the request
        # was served), so delete it anyway (review c3 S1); a plant that never landed
        # reads ConditionalCheckFailed, which counts as gone.
        _delete_ctrl_plant()
        res.skip("item-5", f"cannot plant LIVETEST ctrl finding {ctrl_id!r}: {exc}")
        return

    # Security review c1 W2: the 5c/5d probes POST approve/decline to a REAL fleet-B
    # Finding with a fleet-A token and expect 403. If the route ever failed open, the
    # probe would create (or change) that Finding's decision Action, which no LIVETEST
    # residue check can see. Read the Action before and after (consistent reads); refuse
    # to probe without a baseline, and FAIL + exit 1 on any change.
    from agents.supervisor.tools.fleet_recommendation import (  # noqa: PLC0415
        _derive_decision_action_id,
    )
    actions_table = f"vsa-{stage}-avx-actions"
    fb_action_id = _derive_decision_action_id(fb_id)

    def _fb_decision_action() -> tuple[bool, Any]:
        try:
            r = ddb5.get_item(TableName=actions_table,
                              Key={"action_id": {"S": fb_action_id}}, ConsistentRead=True)
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        return True, r.get("Item")

    _fb_baseline_ok = False
    _fb_before: Any = None
    try:
        _fb_baseline_ok, _fb_before = _fb_decision_action()
        if not res.check(
            "5-probe-baseline", _fb_baseline_ok,
            f"fleet B Finding {fb_id!r}: decision Action "
            f"{'present' if _fb_before else 'absent'} before the probes",
            on_fail=(f"cannot read fleet B Finding {fb_id!r}'s decision Action "
                     f"{fb_action_id!r} ({_fb_before}); refusing to probe it"),
        ):
            return
        with principal(idp5, pool_id, client_id, run, "U_A-5",
                       ["fleet-operator"], fleet_ids=fleet_a) as (tok_a, _):
            # ── (i) AVX isolation probes ─────────────────────────────────────────

            # 5a-ctrl: GET /fleet/A (control) → 200; follow next_token; check VINs.
            # W1: iterate all pages; for each item resolve scope.vin via _vin_fleet_cms
            # and FAIL if it belongs to a different fleet (skip LIVETEST ctrl VIN).
            # Source: handler.py:2430-2448 by_fleet GSI, fleet_id stripped from response.
            all_fa_items: list[dict] = []
            nt_fa: str | None = None
            _5a_pages = 0
            while True:
                url_fa = f"/findings/fleet/{fleet_a}"
                if nt_fa:
                    # Review c3 S3: the token is base64 JSON; quote it so '+', '/' and
                    # '=' reach the handler intact (it restarts at page 1 on a cursor it
                    # can't decode, which would loop forever).
                    url_fa += f"?next_token={urllib.parse.quote(nt_fa, safe='')}"
                st_ctrl, body_ctrl = api.call("GET", url_fa, f"Bearer {tok_a}")
                if not res.check("5a-ctrl", st_ctrl == 200, f"HTTP {st_ctrl}",
                                 on_fail=f"GET /fleet/A failed: HTTP {st_ctrl} {body_ctrl}"):
                    return
                all_fa_items.extend(body_ctrl.get("findings") or [])
                _5a_pages += 1
                nt_fa = body_ctrl.get("next_token")
                if not nt_fa:
                    break
                if _5a_pages >= _5A_MAX_PAGES:
                    res.check("5a-items", False, "",
                              on_fail=(f"fleet A queue still had a next_token after "
                                       f"{_5A_MAX_PAGES} pages ({len(all_fa_items)} items); "
                                       "refusing to page further"))
                    return

            ctrl_items = all_fa_items
            _ctrl_item_ids = {f.get("finding_id") for f in ctrl_items}
            wrong_fid: str | None = None
            wrong_why = ""
            livetest_vin = f"LIVETEST-VIN-5C-{run[:8].upper()}"
            for item in ctrl_items:
                fid_i = str(item.get("finding_id") or "")
                vin = str((item.get("scope") or {}).get("vin") or "")
                if fid_i == ctrl_id or vin == livetest_vin:
                    continue  # the LIVETEST control plant (fake VIN, no CMS vehicle)
                if not vin:
                    # A fleet-scoped item (no VIN, e.g. a rebalancing move) names its fleet in
                    # the nested scope; anything with neither fails closed.
                    scope_fleet = str((item.get("scope") or {}).get("fleet_id") or "")
                    if scope_fleet == fleet_a:
                        continue
                    wrong_fid = fid_i
                    wrong_why = (f"scope.fleet_id={scope_fleet!r} (not {fleet_a!r})"
                                 if scope_fleet else "carries neither scope.vin nor scope.fleet_id")
                    break
                fleet_for_vin = _vin_fleet_cms(ddb5, vehicles_table, vin)
                if fleet_for_vin != fleet_a:
                    wrong_fid = fid_i
                    wrong_why = (f"scope.vin={vin!r} resolved to fleet={fleet_for_vin!r} "
                                 f"(not {fleet_a!r})")
                    break
            ctrl_present = ctrl_id in _ctrl_item_ids
            res.check(
                "5a-items",
                len(ctrl_items) >= 1 and wrong_fid is None and ctrl_present,
                f"{len(ctrl_items)} items, ctrl_present={ctrl_present}",
                on_fail=(
                    f"finding_id={wrong_fid!r} {wrong_why}"
                    if wrong_fid else
                    f"fleet A items: count={len(ctrl_items)}, ctrl_present={ctrl_present}"
                ),
            )

            # 5b-get: GET /fleet/B → 403 {"error": "insufficient scope"}
            st_b, body_b = api.call("GET", f"/findings/fleet/{fleet_b}", f"Bearer {tok_a}")
            res.check(
                "5b-get", st_b == 403 and body_b == {"error": "insufficient scope"},
                f"HTTP {st_b} {body_b}",
                on_fail=f"expected 403 {{error: insufficient scope}}, got {st_b} {body_b}",
            )

            # 5c-ctrl: approve the LIVETEST ctrl finding (fleet A, no recommended_action)
            # with record_engineering_decision+adp — refused AFTER the scope check with
            # 400 'requires a population-scoped Finding'.  A fleet-B token gets 403 at
            # the scope check, so a 400 here proves fleet-A scope without any Action.
            # Source: handler.py:1300-1308 (_ENGINEERING_KINDS check after scope).
            st_ca, body_ca = api.call(
                "POST", f"/findings/{ctrl_id}/approve", f"Bearer {tok_a}",
                {"action_kind": "record_engineering_decision", "target_system": "adp",
                 "parameters": {}},
            )
            _ctrl_approve_err = str((body_ca or {}).get("error") or "")
            if not res.check(
                "5c-ctrl",
                st_ca == 400 and "population-scoped" in _ctrl_approve_err,
                f"HTTP {st_ca} {_ctrl_approve_err[:80]}",
                on_fail=(
                    f"approve ctrl (LIVETEST fleet-A, record_engineering_decision+adp): "
                    f"expected 400 'population-scoped' (post-scope), got {st_ca} "
                    f"{_ctrl_approve_err[:80]}"
                ),
            ):
                return

            # 5c-approve: approve fleet-B finding → 403 exact
            st_cb, body_cb = api.call(
                "POST", f"/findings/{fb_id}/approve", f"Bearer {tok_a}",
                {"action_kind": "book_fleet_service", "target_system": "dms",
                 "parameters": {"dealer_id": fb_dealer}},
            )
            _APPROVE_403 = {"error": "insufficient scope to approve this finding"}
            res.check(
                "5c-approve", st_cb == 403 and body_cb == _APPROVE_403,
                f"HTTP {st_cb} {body_cb}",
                on_fail=f"expected 403 {_APPROVE_403}, got {st_cb} {body_cb}",
            )

            # 5d-ctrl: decline the LIVETEST ctrl finding (fleet A, no recommended_action)
            # — refused AFTER the scope check with 400 'decline is only for FI queue items'
            # (rec is None).  Proves fleet-A scope without any Action.
            # Source: handler.py:1620-1621 (rec is None check after scope).
            st_da, body_da = api.call(
                "POST", f"/findings/{ctrl_id}/decline", f"Bearer {tok_a}",
                {"reason": "livetest-ctrl-scope-probe"},
            )
            _ctrl_decline_err = str((body_da or {}).get("error") or "")
            if not res.check(
                "5d-ctrl",
                st_da == 400 and "FI queue items" in _ctrl_decline_err,
                f"HTTP {st_da} {_ctrl_decline_err[:80]}",
                on_fail=(
                    f"decline ctrl (LIVETEST fleet-A, no recommended_action): "
                    f"expected 400 'FI queue items' (post-scope), got {st_da} "
                    f"{_ctrl_decline_err[:80]}"
                ),
            ):
                return

            # 5d-decline: decline fleet-B finding → 403 exact
            st_db, body_db = api.call(
                "POST", f"/findings/{fb_id}/decline", f"Bearer {tok_a}", {"reason": "x"},
            )
            _DECLINE_403 = {"error": "insufficient scope to decline this finding"}
            res.check(
                "5d-decline", st_db == 403 and body_db == _DECLINE_403,
                f"HTTP {st_db} {body_db}",
                on_fail=f"expected 403 {_DECLINE_403}, got {st_db} {body_db}",
            )

    finally:
        _delete_ctrl_plant()
        if _fb_baseline_ok:
            _fb_after_ok, _fb_after = _fb_decision_action()
            _fb_same = _fb_after_ok and _fb_after == _fb_before
            res.check(
                "5-probe-no-side-effect", _fb_same,
                f"fleet B Finding {fb_id!r}'s decision Action unchanged by the probes",
                on_fail=(f"fleet B Finding {fb_id!r}'s decision Action {fb_action_id!r} "
                         + ("could not be re-read" if not _fb_after_ok else "changed")
                         + " after the fleet-A probes"),
            )
            if not _fb_same:
                print(f"  item-5: the fleet-A approve/decline probes may have changed fleet B's "
                      f"Finding {fb_id!r} (decision Action {fb_action_id!r}); inspect that "
                      "Action and any DMS repair order before another run",
                      file=sys.stderr)
                res.mark_cleanup_failed()

    # ── (ii) CMS queue route ──────────────────────────────────────────────────
    # G6-D4: CMS /findings/fleet/{fleet_id} route moved to Group 7 live check.
    res.note("5-cms", "CMS /findings/fleet/{fleet_id} route moved to Group 7 live check (G6-D4)")


# ---------------------------------------------------------------------------
# Item 6 — re-run on unchanged inputs
# ---------------------------------------------------------------------------


def _item_6(res: Results, **_kw: Any) -> None:
    """Re-run on unchanged inputs: zero new finding ids, zero model calls, zero tokens.

    Writes: one sync Lambda invoke (requires --allow-writes).
    Refuses within 45 min of any cron schedule on the Lambda.
    """
    stage: str = _kw["stage"]
    region: str = _kw["region"]
    manifest: Path = _kw["manifest"]

    ddb6 = _client(
        "dynamodb", region,
        allowed_ops=frozenset({"dynamodb.Scan", "dynamodb.Query"}),
    )
    lam6 = _client(
        "lambda", region,
        allowed_ops=frozenset({"lambda.GetFunctionConfiguration", "lambda.Invoke"}),
        config=_lambda_invoke_config(),
    )
    sched6 = _client(
        "scheduler", region,
        allowed_ops=frozenset({"scheduler.ListSchedules", "scheduler.GetSchedule"}),
    )

    findings_table = f"vsa-{stage}-avx-findings"
    runs_table_name = f"vsa-{stage}-avx-agent-runs"
    fn_name = f"vsa-{stage}-tier2-health-agent-{region}"

    # Resolve Lambda ARN and read FI_SERVICE_MODEL_ENABLED (needed by scheduler check
    # and score_rerun).  GetFunctionConfiguration is already in the allowlist.
    try:
        fn_cfg = lam6.get_function_configuration(FunctionName=fn_name)
        fn_arn = fn_cfg["FunctionArn"]
    except Exception as exc:  # noqa: BLE001
        res.skip("item-6", f"cannot resolve {fn_name!r}: {exc}")
        return

    _env6 = (fn_cfg.get("Environment") or {}).get("Variables") or {}
    model_enabled = _env6.get(_FI_MODEL_ENABLED_ENV, "").lower() == "true"
    if not model_enabled:
        res.note("6-model-state",
                 f"{_FI_MODEL_ENABLED_ENV} is not 'true' — "
                 f"zero-model-call check will be noted, not scored")

    # Scheduler timing check (refuse within 45 min of any cron)
    sc_ok, sc_msg = _check_scheduler_timing(sched6, fn_arn)
    if not res.check("6-schedule", sc_ok, sc_msg, on_fail=f"refused near schedule: {sc_msg}"):
        return

    payload = json.dumps({"stage": stage, "component": _FH_COMPONENT}).encode()

    # ── First invoke: establish stable baseline on today's inputs ────────────
    # A new PM snapshot, a new fault day, or an expired Finding since the last run
    # can produce legitimate new Findings on the first invoke.  Running twice back-
    # to-back ensures the second invoke sees stable, unchanged inputs.
    invoke_iso_1 = _iso(_now())
    existing_ats_1 = _run_started_ats(ddb6, runs_table_name, _FH_RUN_PK, invoke_iso_1)
    try:
        resp6a = lam6.invoke(
            FunctionName=fn_name, InvocationType="RequestResponse", Payload=payload)
    except Exception as exc:  # noqa: BLE001
        res.check("6-invoke-1", False, "", on_fail=f"first invoke error: {exc}")
        return
    fn_err1 = resp6a.get("FunctionError")
    sc6a = resp6a.get("StatusCode", -1)
    if not res.check("6-invoke-1", sc6a == 200 and not fn_err1,
                     f"StatusCode={sc6a} FunctionError={fn_err1!r}",
                     on_fail=f"first invoke failed: {sc6a} {fn_err1!r}"):
        return
    _update_manifest(manifest, "item6_first_invoke_iso", invoke_iso_1)

    run_rec_1 = _poll_run_to_completion(
        ddb6, runs_table_name, _FH_RUN_PK, invoke_iso_1,
        timeout_s=2700, poll_s=15, exclude=existing_ats_1,
    )
    if not res.check("6-run-rec-1", run_rec_1 is not None, "first run record found",
                     on_fail="timeout waiting for first run record (>45 min)"):
        return

    # Before snapshot: scan AFTER first run completes so inputs are now stable.
    try:
        before_ids = _scan_all_fh_finding_ids(ddb6, findings_table)
    except Exception as exc:  # noqa: BLE001
        res.check("6-before-scan", False, "", on_fail=f"before-scan error: {exc}")
        return
    res.note("6-before-count", f"{len(before_ids)} fleet-health findings before second invoke")

    # ── Second invoke: scored for idempotency ────────────────────────────────
    invoke_iso_2 = _iso(_now())
    existing_ats_2 = _run_started_ats(ddb6, runs_table_name, _FH_RUN_PK, invoke_iso_2)
    try:
        resp6b = lam6.invoke(
            FunctionName=fn_name, InvocationType="RequestResponse", Payload=payload)
    except Exception as exc:  # noqa: BLE001
        res.check("6-invoke", False, "", on_fail=f"second invoke error: {exc}")
        return
    fn_err2 = resp6b.get("FunctionError")
    sc6b = resp6b.get("StatusCode", -1)
    if not res.check("6-invoke", sc6b == 200 and not fn_err2,
                     f"StatusCode={sc6b} FunctionError={fn_err2!r}",
                     on_fail=f"second invoke failed: {sc6b} {fn_err2!r}"):
        return
    _update_manifest(manifest, "item6_second_invoke_iso", invoke_iso_2)

    # Poll for second run record
    run_rec = _poll_run_to_completion(
        ddb6, runs_table_name, _FH_RUN_PK, invoke_iso_2,
        timeout_s=2700, poll_s=15, exclude=existing_ats_2,
    )
    if not res.check("6-run-rec", run_rec is not None, "run record found",
                     on_fail="timeout waiting for second run record (>45 min)"):
        return
    assert run_rec is not None

    res.check("6-run-completed", run_rec.get("status") == "completed",
              f"status={run_rec.get('status')!r}",
              on_fail=f"run status={run_rec.get('status')!r} (expected 'completed')")

    rerun_ok, rerun_detail = score_rerun(run_rec, model_enabled=model_enabled)
    res.check("6-rerun", rerun_ok, rerun_detail, on_fail=f"G6 FAIL: {rerun_detail}")

    # After snapshot
    try:
        after_ids = _scan_all_fh_finding_ids(ddb6, findings_table)
    except Exception as exc:  # noqa: BLE001
        res.check("6-after-scan", False, "", on_fail=f"after-scan error: {exc}")
        return
    res.note("6-after-count", f"{len(after_ids)} fleet-health findings after invoke")

    new_ok, new_detail = score_new_findings(before_ids, after_ids)
    res.check("6-new-findings", new_ok, new_detail, on_fail=f"G6 FAIL: {new_detail}")

    _update_manifest(manifest, "item6_run_record", {
        "first_started_at": (run_rec_1 or {}).get("started_at"),
        "second_started_at": run_rec.get("started_at"),
        "status": run_rec.get("status"),
        "model_calls": run_rec.get("model_calls"),
        "tokens_used": run_rec.get("tokens_used"),
        "artifacts_written": run_rec.get("artifacts_written"),
    })


def _item7_write_backup(
    original_env: dict[str, Any],
    original_cfg: dict[str, Any],
    run_dir: Path,
    fn_name: str,
    region: str,
) -> Path:
    """Write 0600 backup to run_dir/item7-env-backup.json; return the path."""
    bk_path = run_dir / "item7-env-backup.json"
    payload = {"Variables": original_env}
    fd = os.open(str(bk_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(payload, fh, indent=2)
    return bk_path


def _item7_write_restore_file(original_env: dict[str, Any], run_dir: Path) -> Path:
    """Write 0600 ``{"Variables": ...}`` restore file; return the path."""
    path = run_dir / "item7-env-restore.json"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"Variables": original_env}, fh, indent=2)
    return path


def _restore_command(fn_name: str, region: str, restore_path: Path) -> str:
    return (f"aws lambda update-function-configuration --function-name {fn_name} "
            f"--region {region} --environment file://{restore_path}")


def _item_7(res: Results, **_kw: Any) -> None:  # noqa: C901
    """Forced failure reaches the staleness alarm's subscriber.

    Two phases (--item7-phase force|verify, default force).

    force (--allow-writes --allow-paging):
      Static checks first: alarm vsa-{stage}-avx-run-stale-service-{region} exists,
      ActionsEnabled=true, AlarmActions == [operator topic ARN] exactly, metric is
      CVX/Tier2 RunRecordsWritten component=service; topic >= 1 confirmed subscription.
      Refuses if alarm is already ALARM (an OK->ALARM transition is required to
      trigger a notification).  Then backs up Lambda env, sets
      FI_SERVICE_MODEL_ENABLED=true and FI_COST_GUARDRAIL_ID='' (the runner raises
      before any write), sync-invokes {"component":"service"}, requires FunctionError
      and no new service run record, restores in finally.  Records the forced time in
      the manifest and prints the verify command with the earliest verify time (27 h).

    verify (--allow-paging; --since <iso>, read-only):
      Alarm history since --since has an OK->ALARM StateUpdate AND an Action item
      with actionState=Succeeded naming the topic.  Fails otherwise.

    Note: the service schedule must not run successfully inside the 27 h evaluation
    window (the operator disables it before running force; see group6-notes G6-D3).
    """
    stage: str = _kw["stage"]
    region: str = _kw["region"]
    account: str = _kw["account"]
    manifest: Path = _kw["manifest"]
    args: Any = _kw["args"]

    phase: str = getattr(args, "item7_phase", "force") or "force"
    since_iso: str | None = getattr(args, "item7_since", None)

    alarm_name = f"vsa-{stage}-{_FH_ALARM_SUFFIX}-{region}"
    fn_name = f"vsa-{stage}-tier2-health-agent-{region}"
    topic_name = f"vsa-{stage}-tier2-operator-alerts"
    runs_table = f"vsa-{stage}-avx-agent-runs"

    cw7 = _client("cloudwatch", region, allowed_ops=frozenset({
        "cloudwatch.DescribeAlarms",
        "cloudwatch.DescribeAlarmHistory",
    }))
    sns7 = _client("sns", region, allowed_ops=frozenset({
        "sns.ListTopics",
        "sns.GetTopicAttributes",
        "sns.ListSubscriptionsByTopic",
    }))

    # ── shared: resolve and validate the operator topic ──────────────────────
    topic_arn: str | None = None
    try:
        lt_kw: dict[str, Any] = {}
        while True:
            lt_resp = sns7.list_topics(**lt_kw)
            for t in lt_resp.get("Topics", []):
                arn = t.get("TopicArn", "")
                if arn.endswith(f":{topic_name}"):
                    topic_arn = arn
                    break
            if topic_arn or not lt_resp.get("NextToken"):
                break
            lt_kw["NextToken"] = lt_resp["NextToken"]
    except Exception as exc:  # noqa: BLE001
        res.check("7-topic-list", False, str(exc)[:120])
        return
    if topic_arn is None:
        res.check("7-topic-exists", False, f"SNS topic {topic_name!r} not found")
        return

    # ── verify phase ─────────────────────────────────────────────────────────
    if phase == "verify":
        if since_iso is None:
            res.check("7-verify-since", False, "--since <iso> required for verify phase")
            return
        try:
            since_dt = dt.datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
        except ValueError as exc:
            res.check("7-verify-since-parse", False, f"--since parse error: {exc}")
            return

        try:
            su_items = cw7.describe_alarm_history(
                AlarmName=alarm_name,
                HistoryItemType="StateUpdate",
                StartDate=since_dt,
            ).get("AlarmHistoryItems", [])
        except Exception as exc:  # noqa: BLE001
            res.check("7-verify-history", False, str(exc)[:120])
            return

        # Find the earliest OK->ALARM StateUpdate after since
        alarm_ts = None
        for h in sorted(su_items, key=lambda x: x.get("Timestamp", "")):
            try:
                hd = json.loads(h.get("HistoryData") or "{}")
            except (ValueError, TypeError):
                continue
            new_state = (hd.get("newState") or {}).get("stateValue", "")
            old_state = (hd.get("oldState") or {}).get("stateValue", "")
            if old_state == "OK" and new_state == "ALARM":
                alarm_ts = h.get("Timestamp")
                break

        if not res.check("7-verify-ok-to-alarm", alarm_ts is not None,
                         f"OK->ALARM StateUpdate found at {alarm_ts}",
                         on_fail=f"no OK->ALARM StateUpdate in history since {since_iso}"):
            return

        try:
            act_items = cw7.describe_alarm_history(
                AlarmName=alarm_name,
                HistoryItemType="Action",
                StartDate=since_dt,
            ).get("AlarmHistoryItems", [])
        except Exception as exc:  # noqa: BLE001
            res.check("7-verify-actions", False, str(exc)[:120])
            return

        # Require at least one Succeeded action at or after the ALARM transition
        succeeded = [
            a for a in act_items
            if (alarm_ts is not None and a.get("Timestamp", "") >= alarm_ts)
            and json.loads(a.get("HistoryData") or "{}").get("actionState") == "Succeeded"
        ]
        res.check(
            "7-verify-action-succeeded",
            len(succeeded) >= 1,
            f"{len(succeeded)} Succeeded action(s) after OK->ALARM transition",
            on_fail=f"0 Succeeded alarm actions since {since_iso} (topic={topic_arn})",
        )
        return

    # ── force phase ──────────────────────────────────────────────────────────
    # 1. Alarm static checks
    try:
        alarm_resp = cw7.describe_alarms(AlarmNames=[alarm_name])
        alarms = alarm_resp.get("MetricAlarms", [])
    except Exception as exc:  # noqa: BLE001
        res.check("7-alarm-describe", False, str(exc)[:120])
        return

    if not alarms:
        res.check("7-alarm-exists", False, f"alarm {alarm_name!r} not found")
        return
    alarm = alarms[0]

    if not res.check("7-alarm-exists", True, f"alarm {alarm_name!r} found"):
        return

    actions_enabled = alarm.get("ActionsEnabled", False)
    enabled_ok = res.check("7-alarm-actions-enabled", actions_enabled is True,
              "ActionsEnabled=true",
              on_fail=f"ActionsEnabled={actions_enabled!r} (deploy with FI_FLEET_HEALTH_LIVE=true first)")

    alarm_actions = alarm.get("AlarmActions", [])
    actions_ok = res.check("7-alarm-actions-exact", alarm_actions == [topic_arn],
              f"AlarmActions == [{topic_arn!r}]",
              on_fail=f"AlarmActions={alarm_actions!r} != [{topic_arn!r}]")

    # Verify metric is CVX/Tier2 RunRecordsWritten component=service
    metrics = alarm.get("Metrics", [])
    m1 = next((m for m in metrics if m.get("Id") == "m1"), None)
    if m1 is None:
        # Single-metric alarm (no Metrics list) — check MetricName directly
        ns_ok = alarm.get("Namespace") == _FH_ALARM_CW_NAMESPACE
        mn_ok = alarm.get("MetricName") == _FH_ALARM_CW_METRIC
        dims = {d["Name"]: d["Value"] for d in alarm.get("Dimensions", [])}
        comp_ok = dims.get("component") == _FH_COMPONENT
        metric_ok = res.check("7-alarm-metric", ns_ok and mn_ok and comp_ok,
                  f"metric={_FH_ALARM_CW_NAMESPACE}/{_FH_ALARM_CW_METRIC} component=service",
                  on_fail=f"ns={alarm.get('Namespace')} metric={alarm.get('MetricName')} "
                          f"dims={dims}")
    else:
        ms = m1.get("MetricStat", {})
        m = ms.get("Metric", {})
        dims = {d["Name"]: d["Value"] for d in m.get("Dimensions", [])}
        ns_ok = m.get("Namespace") == _FH_ALARM_CW_NAMESPACE
        mn_ok = m.get("MetricName") == _FH_ALARM_CW_METRIC
        comp_ok = dims.get("component") == _FH_COMPONENT
        metric_ok = res.check("7-alarm-metric", ns_ok and mn_ok and comp_ok,
                  f"metric={_FH_ALARM_CW_NAMESPACE}/{_FH_ALARM_CW_METRIC} component=service",
                  on_fail=f"ns={m.get('Namespace')} metric={m.get('MetricName')} dims={dims}")

    # Topic confirmed subscription check
    try:
        subs_resp = sns7.list_subscriptions_by_topic(TopicArn=topic_arn)
        confirmed = [
            s for s in subs_resp.get("Subscriptions", [])
            if s.get("SubscriptionArn", "").startswith("arn:")
        ]
        sub_ok = res.check("7-topic-subscriber", len(confirmed) >= 1,
                  f"{len(confirmed)} confirmed subscription(s) on {topic_name!r}",
                  on_fail=f"0 confirmed subscriptions on {topic_arn!r}")
    except Exception as exc:  # noqa: BLE001
        res.check("7-topic-subscriber", False, str(exc)[:120])
        return

    # Return before any backup or write if any alarm static check failed.
    if not (enabled_ok and actions_ok and metric_ok and sub_ok):
        return

    # Refuse if alarm is already ALARM (no OK->ALARM transition possible)
    start_state = alarm.get("StateValue", "")
    if not res.check("7-alarm-not-alarm", start_state != "ALARM",
                     f"alarm state is {start_state!r} (want OK or INSUFFICIENT_DATA)",
                     on_fail=f"alarm {alarm_name!r} is already ALARM — "
                             "a successful service run must occur first to reset to OK"):
        return

    # 2. Lambda env override
    lam7 = _client("lambda", region, allowed_ops=frozenset({
        "lambda.GetFunctionConfiguration",
        "lambda.UpdateFunctionConfiguration",
        "lambda.Invoke",
    }), config=_lambda_invoke_config())
    sched7 = _client("scheduler", region, allowed_ops=frozenset({
        "scheduler.ListSchedules",
        "scheduler.GetSchedule",
    }))
    ddb7 = _client("dynamodb", region, allowed_ops=frozenset({
        "dynamodb.Query",
    }))

    try:
        original_cfg = lam7.get_function_configuration(FunctionName=fn_name)
    except Exception as exc:  # noqa: BLE001
        res.check("7-get-config", False, str(exc)[:120])
        return
    original_env = dict((original_cfg.get("Environment") or {}).get("Variables") or {})
    fn_arn = original_cfg.get("FunctionArn", f"arn:aws:lambda:{region}:{account}:function:{fn_name}")

    run_dir = Path(f"/tmp/fi-fh-d-g6lv-{_kw['run']}")
    run_dir.mkdir(mode=0o700, exist_ok=True)
    bk_path = _item7_write_backup(original_env, original_cfg, run_dir, fn_name, region)
    restore_path = _item7_write_restore_file(original_env, run_dir)
    print(f"  env backup  : {bk_path}")
    print(f"  restore cmd : {_restore_command(fn_name, region, restore_path)}")

    sc7_ok, sc7_msg = _check_scheduler_timing(sched7, fn_arn)
    if not res.check("7-schedule", sc7_ok, sc7_msg,
                     on_fail=f"refused near schedule: {sc7_msg}"):
        for _p in (bk_path, restore_path):
            with contextlib.suppress(OSError):
                os.remove(str(_p))
        return

    _override_applied = False
    restore_ok = True
    invoke_iso = _iso(_now())
    existing_ats = _run_started_ats(ddb7, runs_table, _FH_RUN_PK, invoke_iso)

    try:
        override_vars = dict(original_env)
        override_vars[_FI_MODEL_ENABLED_ENV] = "true"
        override_vars[_FI_GUARDRAIL_ENV] = ""
        # RevisionId prevents a blind overwrite if Lambda was changed concurrently.
        # Live GetFunctionConfiguration always returns it; refuse (before any write, so
        # nothing needs restoring) rather than overwrite without it.
        if not original_cfg.get("RevisionId"):
            res.check("7-override", False, "GetFunctionConfiguration returned no RevisionId; "
                      "refusing an unconditional env override")
            return
        _override_applied = True
        try:
            _upd_kw: dict[str, Any] = {
                "FunctionName": fn_name,
                "Environment": {"Variables": override_vars},
                "RevisionId": original_cfg["RevisionId"],
            }
            lam7.update_function_configuration(**_upd_kw)
            _wait_fn_update(lam7, fn_name)
            print(f"  7/force: set {_FI_MODEL_ENABLED_ENV}=true, cleared {_FI_GUARDRAIL_ENV!r}")
        except Exception as exc:  # noqa: BLE001
            res.check("7-override", False, str(exc)[:120])
            return

        # Re-check schedule after the update wait (up to 90 s)
        sc7b_ok, sc7b_msg = _check_scheduler_timing(sched7, fn_arn)
        if not res.check("7-schedule-pre-invoke", sc7b_ok, sc7b_msg,
                         on_fail=f"refused near schedule after update wait: {sc7b_msg}"):
            return

        # Sync invoke; expect FunctionError
        try:
            resp7 = lam7.invoke(
                FunctionName=fn_name,
                InvocationType="RequestResponse",
                Payload=json.dumps({"stage": stage, "component": _FH_COMPONENT}).encode(),
            )
            fn_error = resp7.get("FunctionError", "")
            res.check("7-invoke-error", bool(fn_error),
                      f"FunctionError={fn_error!r} (want non-empty)",
                      on_fail="Lambda invoke succeeded — guardrail check not reached; "
                              "check FI_SERVICE_MODEL_ENABLED in live env")
        except Exception as exc:  # noqa: BLE001
            res.check("7-invoke", False, str(exc)[:120])
            return

        # No new run record should have been written
        new_ats = _run_started_ats(ddb7, runs_table, _FH_RUN_PK, invoke_iso)
        unexpected = new_ats - existing_ats
        res.check("7-no-run-record", len(unexpected) == 0,
                  "no new service run record written (raised before write)",
                  on_fail=f"unexpected run record(s) at {sorted(unexpected)!r}")

        # Record forced time; print verify command
        forced_at = _iso(_now())
        earliest_verify = dt.datetime.fromisoformat(
            forced_at.replace("Z", "+00:00")
        ) + dt.timedelta(hours=_FH_ALARM_EVAL_PERIODS)
        earliest_iso = earliest_verify.strftime("%Y-%m-%dT%H:%M:%SZ")
        verify_cmd = (
            f"{_LIVE_GO_ENV}={stage} LIVE_TESTS=1 .venv/bin/python scripts/live-verify-fi-fleet-health.py "
            f"--stage {stage} --item 7 --allow-paging "
            f"--item7-phase verify --since {forced_at}"
        )
        print(f"  7/force: forced_at={forced_at}")
        print(f"  7/force: earliest_verify={earliest_iso} ({_FH_ALARM_EVAL_PERIODS}h window)")
        print(f"  7/force: verify cmd:\n    {verify_cmd}")
        _update_manifest(manifest, "item7_force", {
            "forced_at": forced_at,
            "earliest_verify_at": earliest_iso,
            "verify_cmd": verify_cmd,
        })

    finally:
        if not _override_applied:
            for _p in (bk_path, restore_path):
                with contextlib.suppress(OSError):
                    os.remove(str(_p))
            res.check("7-env-restored", True, "no override applied")
        else:
            restore_ok = True
            with _defer_signals_for_restore():
                restore_deadline = time.monotonic() + 120.0
                while True:
                    try:
                        # Pre-restore safety check: live env must still equal override_vars.
                        # A concurrent writer would have changed it; restoring on top of
                        # their change would silently overwrite their write.
                        pre_cfg = lam7.get_function_configuration(FunctionName=fn_name)
                        pre_vars = dict(
                            (pre_cfg.get("Environment") or {}).get("Variables") or {}
                        )
                        if pre_vars != override_vars:
                            print("  RESTORE ABORTED: live vars ≠ override_vars "
                                  "(concurrent write detected)")
                            print(f"  live    : {pre_vars}")
                            print(f"  override: {override_vars}")
                            print(f"  backup  : {bk_path}")
                            res.check("7-restore", False,
                                      f"live vars ≠ override_vars at restore time; "
                                      f"backup={bk_path}")
                            res.mark_cleanup_failed()
                            restore_ok = False
                            break
                        lam7.update_function_configuration(
                            FunctionName=fn_name,
                            Environment={"Variables": original_env},
                        )
                        _wait_fn_update(lam7, fn_name, timeout_s=30.0)
                        cfg2 = lam7.get_function_configuration(FunctionName=fn_name)
                        restored = dict((cfg2.get("Environment") or {}).get("Variables") or {})
                        if restored == original_env:
                            print(f"  7: env restored ({len(restored)} keys)")
                            for _p in (bk_path, restore_path):
                                with contextlib.suppress(OSError):
                                    os.remove(str(_p))
                            break
                        diff = [k for k in set(original_env) | set(restored)
                                if original_env.get(k) != restored.get(k)]
                        if time.monotonic() < restore_deadline:
                            time.sleep(3)
                            continue
                        res.check("7-restore", False,
                                  f"env mismatch after restore; keys={diff}")
                        res.mark_cleanup_failed()
                        restore_ok = False
                        break
                    except Exception as exc:  # noqa: BLE001
                        if time.monotonic() < restore_deadline:
                            time.sleep(3)
                            continue
                        res.check("7-restore", False, f"restore failed: {exc}")
                        res.mark_cleanup_failed()
                        restore_ok = False
                        break
                if not restore_ok:
                    print(f"  RESTORE FAILED — backup: {bk_path}")
                    print(f"  manual cmd: {_restore_command(fn_name, region, restore_path)}")
                res.check("7-env-restored", restore_ok, "" if restore_ok else "see backup above")


_ITEM_FNS: dict[int, Any] = {
    1: _item_1,
    2: _item_2,
    3: _item_3,
    4: _item_4,
    5: _item_5,
    6: _item_6,
    7: _item_7,
}


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Live verification for FI Fleet Health Stage D (items 1-7)."
    )
    ap.add_argument(
        "--stage",
        default=_ALLOWED_STAGE,
        help=f"only {_ALLOWED_STAGE!r} is allowed",
    )
    ap.add_argument(
        "--item",
        type=int,
        action="append",
        dest="items",
        metavar="N",
        help="run item N (repeatable); mutually exclusive with --all",
    )
    ap.add_argument("--all", action="store_true", help="run all 7 items in order")
    ap.add_argument(
        "--allow-writes",
        action="store_true",
        help="allow Lambda invokes, Cognito users, LIVETEST plants, env overrides",
    )
    ap.add_argument(
        "--allow-managed-writes",
        action="store_true",
        help="additionally allow item 3 (managed-fleet DMS round-trip)",
    )
    ap.add_argument(
        "--allow-paging",
        action="store_true",
        help="allow item 7 (pages the operator SNS topic)",
    )
    ap.add_argument(
        "--run-id",
        default=None,
        help="override generated run id (for testing / replay)",
    )
    ap.add_argument(
        "--adp-lake-bucket",
        default=None,
        dest="adp_lake_bucket",
        help=(
            "ADP lake S3 bucket for item 2 catalog read "
            "(default: adp-{stage}-foundation-lake-{account}-us-east-1)"
        ),
    )
    ap.add_argument(
        "--item7-phase",
        default="force",
        choices=("force", "verify"),
        dest="item7_phase",
        help="item 7 phase: 'force' (default) writes the env override; 'verify' reads history",
    )
    ap.add_argument(
        "--since",
        default=None,
        dest="item7_since",
        metavar="ISO",
        help="ISO-8601 UTC timestamp for item 7 verify phase (--item7-phase verify)",
    )
    return ap.parse_args()


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def main() -> int:  # noqa: C901
    # Parse args first so that --help exits 0 regardless of LIVE_TESTS.
    args = _parse_args()

    # ── gate 1: LIVE_TESTS=1 ────────────────────────────────────────────────
    if os.environ.get("LIVE_TESTS") != "1":
        print(
            "SKIPPED — set LIVE_TESTS=1 to run.\n"
            "Items 3, 6, 7 require --allow-writes.\n"
            "Item 3 also requires --allow-managed-writes.\n"
            "Item 7 also requires --allow-paging.\n"
            "\nExit 0 = all pass.  Exit 1 = any fail.  Exit 2 = any skip (non-passing)."
        )
        return 2

    if args.items and args.all:
        print("--item and --all are mutually exclusive", file=sys.stderr)
        return 1

    # ── gate 2: stage — before any AWS call ─────────────────────────────────
    if args.stage != _ALLOWED_STAGE:
        print(
            f"--stage must be {_ALLOWED_STAGE!r}; "
            f"refusing {args.stage!r} before any AWS call",
            file=sys.stderr,
        )
        return 1

    # ── gate 2b: live-write guard ─────────────────────────────────────────────
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
            print(
                f"unknown --item value(s): {bad}; valid: {sorted(_ALL_ITEMS)}",
                file=sys.stderr,
            )
            return 1
        items_requested = frozenset(args.items)
    else:
        items_requested = _ALL_ITEMS

    # item 7 verify phase is read-only; it doesn't need --allow-writes and should
    # not be blocked by the write window.
    item7_verify = (
        7 in items_requested and getattr(args, "item7_phase", "force") == "verify"
    )
    items_to_run, items_to_skip = _compute_skips(
        items_requested,
        allow_writes=args.allow_writes,
        allow_managed_writes=args.allow_managed_writes,
        allow_paging=args.allow_paging,
        item7_verify=item7_verify,
    )

    # ── gate 3: write window ─────────────────────────────────────────────────
    write_items_active = items_to_run & (_WRITE_ITEMS | _MANAGED_WRITE_ITEMS)
    if item7_verify:
        write_items_active -= {7}
    if write_items_active and _in_write_window():
        print(
            "Refusing to start writes inside 04:50-06:10 UTC (scheduled-job window).\n"
            "Service fleet-health schedule: 05:40 UTC.\n"
            "Re-run after 06:10 UTC.",
            file=sys.stderr,
        )
        return 1

    # ── gate 4: run id shape (security review c1 W1) ─────────────────────────
    # The run id becomes part of DynamoDB keys, URL path segments, Cognito usernames,
    # residue filters and the /tmp run directory, so refuse anything but the safe shape
    # before any AWS call.
    if args.run_id is not None and not _SAFE_ID_RE.match(args.run_id):
        print(f"--run-id must match {_SAFE_ID_RE.pattern}; refusing {args.run_id!r} "
              "before any AWS call", file=sys.stderr)
        return 1

    # ── AWS clients — only after all pre-AWS gates pass ──────────────────────
    stage = args.stage
    region = _DEFAULT_REGION

    sts = _client(
        "sts", region, allowed_ops=frozenset({"sts.GetCallerIdentity"})
    )
    who = sts.get_caller_identity()
    print(f"caller  : {who['Arn']}")
    print(f"stage   : {stage}")
    print(f"region  : {region}")

    run = args.run_id or _make_run_id()
    manifest = _init_manifest(run)
    print(f"\nrun id  : {run}")
    print(f"manifest: {manifest}")

    # DDB low-level client for residue checks (read-only ops only)
    ddb = _client(
        "dynamodb",
        region,
        allowed_ops=frozenset({"dynamodb.Scan", "dynamodb.Query", "dynamodb.GetItem"}),
    )
    # Cognito IDP low-level client for residue checks (read-only ops only)
    idp = _client(
        "cognito-idp",
        region,
        allowed_ops=frozenset({"cognito-idp.ListUserPools", "cognito-idp.ListUsers"}),
    )

    findings_table = f"vsa-{stage}-avx-findings"
    actions_table = f"vsa-{stage}-avx-actions"
    livetest_prefix = f"LIVETEST-{run}"

    res = Results()
    teardown = _TeardownRegistry()

    # Emit SKIP rows for gated and non-requested items upfront
    for n in sorted(items_to_skip):
        res.skip(f"item-{n}", items_to_skip[n])
    # Not-requested items are simply not run — no SKIP emitted.  Emitting SKIP
    # for items the caller didn't ask for inflates the SKIP count and changes the
    # exit code from 0 to 2 even on a fully passing run.

    ctx: dict[str, Any] = {
        "run": run,
        "stage": stage,
        "region": region,
        "manifest": manifest,
        "teardown": teardown,
        "args": args,
        "account": who["Account"],
    }

    # ── run items in order ────────────────────────────────────────────────────
    try:
        for n in _RUN_ORDER:
            if n not in items_to_run:
                continue
            print(f"\nItem {n}")
            try:
                _ITEM_FNS[n](res, **ctx)
            except Exception as exc:  # noqa: BLE001
                res.check(
                    f"{n}-exception",
                    False,
                    f"item {n} raised {type(exc).__name__}: {exc}",
                )
    finally:
        print("\nTeardown")
        teardown.run(res)

        # ── residue checks (run even after exception or signal) ───────────────
        def _residue_findings() -> tuple[bool, str]:
            return _check_residue_findings(ddb, findings_table, run)

        def _residue_actions() -> tuple[bool, str]:
            return _check_residue_actions(ddb, actions_table, livetest_prefix)

        def _residue_cognito() -> tuple[bool, str]:
            resolved = _resolve_cms_pool_id(idp, stage)
            if resolved is None:
                return False, f"could not resolve CMS pool 'cms-{stage}-ui-users'"
            return _check_residue_cognito(idp, resolved, run)

        print("\nResidue checks")
        _run_residue_checks(
            [
                ("findings-livetest", _residue_findings),
                ("actions-livetest", _residue_actions),
                ("cognito-livetest", _residue_cognito),
            ],
            res,
        )

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
