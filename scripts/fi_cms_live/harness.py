"""Shared harness for fi-cms-live live-verification scripts.

Stage gate, botocore guard, Results/exit codes, signals, run manifest,
Cognito temp users (CMS pool), CMS/AVX/DMS HTTP helpers, write-window
guards, and Cognito residue check.

Ported from AVX 83553340:scripts/live-verify-fi-fleet-health.py (reviewed)
and 49ff3719:scripts/live-verify-fi-loop.py (window guards).  Never import
those scripts directly.

Differences from sources
  - Allowlists are passed per part by the caller (``service.Operation``).
  - Temp usernames: ``livetest-<run>-<role>@example.invalid`` (deterministic,
    recorded in the manifest before ``AdminCreateUser``).
  - Manifest lives in ``~/.cache/fi-cms-live/`` (env ``FI_CMS_LIVE_MANIFEST_DIR``),
    written atomically 0600, refuses a stage mismatch on re-open.
  - ``--run-id`` validated against ``^[A-Za-z0-9-]{1,64}$``.
  - Write window 04:00–06:15 UTC (brief §Write windows).
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import secrets
import signal as _sig
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ALLOWED_STAGE = "staging"
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")

# Maintenance window: 04:00–06:15 UTC inclusive (brief §Write windows).
# _MAINT_START_MIN <= n < _MAINT_END_MIN  →  refused
_MAINT_START_MIN = 4 * 60        # 240  (inclusive)
_MAINT_END_MIN   = 6 * 60 + 16   # 376  (exclusive; 06:15 is last refused minute)

_SCHED_GUARD_MIN = 45   # ±45 min around any cron schedule
_UTC_ZONES = frozenset({"UTC", "Etc/UTC"})

_SIGNAL_STATE: dict[str, Any] = {}

# ---------------------------------------------------------------------------
# Stage / run-id gates
# ---------------------------------------------------------------------------


def check_stage(stage: str) -> None:
    """Raise SystemExit(1) for any stage other than 'staging'."""
    if stage != _ALLOWED_STAGE:
        raise SystemExit(f"--stage {stage!r} refused; only 'staging' is supported")


def validate_run_id(run_id: str) -> None:
    """Raise SystemExit(1) if run_id does not match ``^[A-Za-z0-9-]{1,64}$``."""
    if not _RUN_ID_RE.fullmatch(run_id):
        raise SystemExit(
            f"--run-id {run_id!r} rejected; must match ^[A-Za-z0-9-]{{1,64}}$"
        )


# ---------------------------------------------------------------------------
# Botocore guard
# ---------------------------------------------------------------------------


class GuardViolation(Exception):
    """Raised when a botocore call is not in the caller's declared allowlist."""


#: Cognito ``Admin*`` operations that only read; every other ``Admin*`` operation writes.
_COGNITO_ADMIN_READS = frozenset({"AdminGetUser"})


def _is_cognito_admin_write(op: str) -> bool:
    """True for an allowlist entry naming a Cognito ``Admin*`` write (qualified or plain)."""
    name = op.split(".", 1)[1] if op.startswith("cognito-idp.") else op
    if "." in name:
        return False
    return name.startswith("Admin") and name not in _COGNITO_ADMIN_READS


def _client(
    service: str,
    region: str,
    *,
    allowed_ops: frozenset[str],
    endpoint_url: str | None = None,
    config: Any = None,
    cognito_username_prefix: str = "",
    cognito_pool_id: str = "",
) -> Any:
    """Build a guarded boto3 client.

    Raises ``GuardViolation`` for any operation whose qualified name
    (``service.OperationName``) or plain name is not in *allowed_ops*.
    Allowlists are passed per part by the caller (``service_name.Operation``).

    When *cognito_username_prefix* is non-empty, any ``cognito-idp.Admin*``
    write (except ``AdminGetUser``) whose ``Username`` does not start with
    that prefix raises ``GuardViolation``.  When *cognito_pool_id* is
    non-empty, any such write whose ``UserPoolId`` differs from that value
    also raises ``GuardViolation``.

    Security review c1 (M W1): a ``cognito-idp`` client whose allowlist admits any
    ``Admin*`` write must carry BOTH a non-empty prefix and a non-empty pool id, or it
    isn't built (``GuardViolation``); an empty value would otherwise switch its guard off.
    """
    import boto3  # noqa: PLC0415

    if service == "cognito-idp" and any(_is_cognito_admin_write(o) for o in allowed_ops) \
            and not (cognito_username_prefix and cognito_pool_id):
        raise GuardViolation(
            "cognito-idp client with Admin* write ops needs a non-empty "
            f"cognito_username_prefix and cognito_pool_id (got {cognito_username_prefix!r}, "
            f"{cognito_pool_id!r})"
        )

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

    # Registered on the fully-qualified pattern so it shares a node with any other
    # before-call.*.* handler and its bucket order holds (botocore calls handlers of a
    # more specific event name first); the fake world routes calls from that node.
    boto_client.meta.events.register("before-call.*.*", _guard)

    # Cognito username / pool guard — must run at before-parameter-build so
    # that params is still the raw Python dict (Username / UserPoolId accessible
    # directly).  before-call params are the serialised HTTP request.
    def _cognito_param_guard(model: Any, params: Any, **_kw: Any) -> None:
        op = model.name
        svc = model.service_model.service_name
        if svc == "cognito-idp" and op.startswith("Admin") and op not in ("AdminGetUser",) and not str(params.get("Username", "")).startswith(cognito_username_prefix):
            raise GuardViolation(
                f"cognito-idp.{op}: Username {params.get('Username')!r} is not under "
                f"prefix {cognito_username_prefix!r}"
            )
        if cognito_pool_id and svc == "cognito-idp" and op.startswith("Admin") and op not in ("AdminGetUser",) and params.get("UserPoolId") != cognito_pool_id:
            raise GuardViolation(
                f"cognito-idp.{op}: UserPoolId {params.get('UserPoolId')!r} != "
                f"expected {cognito_pool_id!r}"
            )

    boto_client.meta.events.register("before-parameter-build", _cognito_param_guard)
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

    @property
    def exit_code(self) -> int:
        if self.failed or self._cleanup_failed:
            return 1
        if self.has_any_skip:
            return 2
        return 0

    def summary(self) -> None:
        print("\nSummary")
        for label, status, detail in self.rows:
            print(f"  {status:4}  {label}{(': ' + detail[:120]) if detail else ''}")
        n_pass = sum(1 for _, s, _ in self.rows if s == "PASS")
        n_fail = sum(1 for _, s, _ in self.rows if s == "FAIL")
        n_skip = sum(1 for _, s, _ in self.rows if s == "SKIP")
        print(f"\n  pass={n_pass}  fail={n_fail}  skip={n_skip}")
        if self._cleanup_failed:
            print("  WARN: at least one cleanup step failed — manual intervention may be needed")


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


def make_run_id() -> str:
    """Generate ``g78lv-<utcstamp>-<4hex>``."""
    stamp = _now().strftime("%Y%m%dT%H%M%S")
    return f"g78lv-{stamp}-{secrets.token_hex(4)}"

# ---------------------------------------------------------------------------
# Signal handlers
# ---------------------------------------------------------------------------


def _install_signal_handlers() -> None:
    """SIGINT, SIGTERM, SIGHUP each raise SystemExit(143) once; second is ignored."""
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
    """Hold SIGINT/SIGTERM/SIGHUP during a critical section; re-deliver after.

    A signal arriving inside the block is recorded and sent via ``os.kill``
    after the original handlers are reinstated, so the critical section
    completes first.  Mirrors live-verify-fi-recall-warranty.py:301-322.
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
# Manifest (durable 0700 dir, atomic 0600 writes, stage-mismatch guard)
# ---------------------------------------------------------------------------


def _manifest_dir() -> Path:
    """Return the manifest directory (env ``FI_CMS_LIVE_MANIFEST_DIR`` or default)."""
    return Path(
        os.environ.get("FI_CMS_LIVE_MANIFEST_DIR",
                       str(Path.home() / ".cache" / "fi-cms-live"))
    )


def _write_0600_atomic(path: Path, text: str) -> None:
    """Write *text* to *path* atomically; file permissions are 0600.

    Flushes and fsyncs before rename so a power-loss after ``os.replace``
    doesn't leave an empty file.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise
    os.replace(str(tmp), str(path))


def open_manifest(run: str, stage: str) -> Path:
    """Open or create the run manifest; refuse on stage mismatch.

    The manifest lives in ``_manifest_dir()/<run>.json``.  On re-open the
    recorded stage must match *stage* or ``SystemExit`` is raised.
    """
    d = _manifest_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(str(d), 0o700)
    manifest = d / f"{run}.json"
    if manifest.exists():
        data = json.loads(manifest.read_text())
        if data.get("stage") != stage:
            raise SystemExit(
                f"run {run!r}: manifest stage is {data.get('stage')!r}; "
                f"refusing to re-use with stage {stage!r}"
            )
        return manifest
    _write_0600_atomic(
        manifest,
        json.dumps({"run_id": run, "stage": stage, "items": {}}, indent=2),
    )
    return manifest


def update_manifest(manifest: Path, key: str, value: Any) -> None:
    """Append / update *key* under ``items{}``; preserves atomicity and 0600.

    Takes an exclusive flock on ``<run>.lock`` for the duration of the
    read-modify-write so that a resumed part and a concurrent cleanup can't
    both read the same stale content and lose each other's updates.
    """
    from decimal import Decimal  # noqa: PLC0415

    def _dec(o: Any) -> float | int:
        if isinstance(o, Decimal):
            return int(o) if o % 1 == 0 else float(o)
        raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")

    lock_path = manifest.with_suffix(".lock")
    lock_fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        data = json.loads(manifest.read_text())
        data["items"][key] = value
        _write_0600_atomic(manifest, json.dumps(data, indent=2, default=_dec))
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

# ---------------------------------------------------------------------------
# Teardown registry
# ---------------------------------------------------------------------------


class TeardownRegistry:
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
# Cognito helpers
# ---------------------------------------------------------------------------


def _password_client(idp: Any, pool_id: str) -> str:
    """Return the first app client allowing USER_PASSWORD_AUTH (paged).

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


def _delete_principal(
    idp: Any, pool_id: str, username: str, role: str, res: Results
) -> None:
    """Delete *username* and confirm via AdminGetUser → UserNotFoundException."""
    try:
        idp.admin_delete_user(UserPoolId=pool_id, Username=username)
    except Exception as exc:  # noqa: BLE001
        # Accept UserNotFoundException: user was never committed (W1 — interrupted create).
        code = ""
        if hasattr(exc, "response"):
            code = exc.response.get("Error", {}).get("Code", "")
        if "UserNotFoundException" in code or "UserNotFoundException" in type(exc).__name__:
            print(f"  cleanup: {username} not found (ok — never committed to pool)")
            return
        print(
            f"  cleanup: FAILED to delete {username}: {type(exc).__name__}\n"
            f"    aws cognito-idp admin-delete-user "
            f"--user-pool-id {pool_id} --username {username}",
            file=sys.stderr,
        )
        res.mark_cleanup_failed()
        return
    # Confirm deleted: AdminGetUser must raise UserNotFoundException.
    try:
        idp.admin_get_user(UserPoolId=pool_id, Username=username)
        # If we reach here, user is still present.
        print(f"  cleanup: {username} still present after delete", file=sys.stderr)
        res.mark_cleanup_failed()
    except Exception as exc:  # noqa: BLE001
        # Accept UserNotFoundException by name (ClientError or AwsError wrapper).
        code = ""
        if hasattr(exc, "response"):
            code = exc.response.get("Error", {}).get("Code", "")
        if "UserNotFoundException" in code or "UserNotFoundException" in type(exc).__name__:
            print(f"  cleanup: deleted principal {role!r}")
        else:
            print(f"  cleanup: AdminGetUser error for {username}: {exc}", file=sys.stderr)
            res.mark_cleanup_failed()


@contextlib.contextmanager
def principal(
    idp: Any,
    pool_id: str,
    client_id: str,
    run: str,
    role: str,
    manifest: Path,
    res: Results,
    groups: list[str],
    fleet_ids: str | None = None,
    dealer_ids: str | None = None,
) -> Iterator[tuple[str, dict]]:
    """Yield (id_token, claims) for a deterministic ephemeral CMS Cognito user.

    Username ``livetest-<run>-<role>@example.invalid`` is recorded in the
    manifest before ``AdminCreateUser``.  Cleanup defers signals and confirms
    deletion via ``AdminGetUser`` → ``UserNotFoundException``.
    """
    import base64  # noqa: PLC0415

    username = f"livetest-{run}-{role}@example.invalid"
    password = f"Lv{secrets.token_urlsafe(18)}!9Qz"
    attrs: list[dict[str, str]] = [
        {"Name": "email", "Value": username},
        {"Name": "email_verified", "Value": "true"},
    ]
    if fleet_ids is not None:
        attrs.append({"Name": "custom:fleetIds", "Value": fleet_ids})
    if dealer_ids is not None:
        attrs.append({"Name": "custom:dealerIds", "Value": dealer_ids})

    # Record in manifest BEFORE creating the user.
    update_manifest(manifest, f"user/{role}", {"username": username, "status": "pending"})

    # Delete in `finally` whatever happened: an interrupted AdminCreateUser may have
    # committed (review g78 C W1), and a never-created user's delete reads as gone.
    try:
        idp.admin_create_user(
            UserPoolId=pool_id, Username=username,
            MessageAction="SUPPRESS", UserAttributes=attrs,
        )
        update_manifest(manifest, f"user/{role}", {"username": username, "status": "created"})
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
        print(f"  principal {role!r}: groups={groups} fleetIds={fleet_ids!r} (ephemeral)")
        yield token, claims
    finally:
        with _defer_signals_for_restore():
            _delete_principal(idp, pool_id, username, role, res)

# ---------------------------------------------------------------------------
# Cognito residue checks
# ---------------------------------------------------------------------------


def _resolve_cms_pool_id(idp: Any, stage: str) -> str | None:
    """Return the Id of pool named ``cms-{stage}-ui-users`` by paging ListUserPools."""
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
    """0 Cognito users whose email begins with ``livetest-<run>-`` (paged scan)."""
    if pool_id is None:
        return False, "Cognito pool_id is None (unknown pool); residue check FAIL"
    prefix = f"livetest-{run}-"
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


def delete_manifest_users(
    idp: Any, pool_id: str, manifest: Path, res: Results
) -> None:
    """Delete every manifest ``user/*`` entry (pending or created).

    For cleanup parts to call.  Reads the manifest and calls
    ``_delete_principal`` for every key whose name starts with ``user/``.
    Accepts ``UserNotFoundException`` from ``AdminDeleteUser`` so that users
    that were never committed (interrupted ``AdminCreateUser``) do not mark
    cleanup as failed.
    """
    data = json.loads(manifest.read_text())
    for key, entry in data.get("items", {}).items():
        if key.startswith("user/"):
            role = key[len("user/"):]
            username = entry.get("username", "")
            if username:
                _delete_principal(idp, pool_id, username, role, res)


# ---------------------------------------------------------------------------
# Window guards  (ported from 49ff3719:scripts/live-verify-fi-loop.py)
# ---------------------------------------------------------------------------


def _cron_minute_of_day(expr: str) -> int:
    """Minute of day of ``cron(M H * * ? *)`` with plain numbers; anything else raises."""
    m = re.fullmatch(r"cron\((\d{1,2}) (\d{1,2}) \* \* \? \*\)", expr.strip())
    if not m or int(m.group(1)) > 59 or int(m.group(2)) > 23:
        raise ValueError(f"unsupported schedule expression {expr!r}")
    return int(m.group(2)) * 60 + int(m.group(1))


def _discover_schedule_group(scheduler: Any, name: str) -> str:
    """Return the EventBridge Scheduler group for *name* via list_schedules.

    Raises ValueError if not found.  Brief §Windows / K8: don't assume 'default'.
    """
    kwargs: dict[str, Any] = {}
    while True:
        page = scheduler.list_schedules(**kwargs)
        for s in page.get("Schedules", []):
            if s.get("Name") == name:
                return s["GroupName"]
        token = page.get("NextToken")
        if not token:
            break
        kwargs["NextToken"] = token
    raise ValueError(f"schedule {name!r} not found in any group via list_schedules")


def _schedule_guard(
    scheduler: Any,
    names: list[tuple[str, str, str]],
    stage: str,
    now: dt.datetime,
) -> str | None:
    """Return a reason to refuse if *now* is within ±45 min of any schedule, else None."""
    n = now.hour * 60 + now.minute
    for tmpl, _ignored_group, _region in names:
        name = tmpl.format(stage=stage, region=_region)
        try:
            group = _discover_schedule_group(scheduler, name)
        except ValueError as exc:
            return str(exc)
        sc = scheduler.get_schedule(Name=name, GroupName=group)
        tz = sc.get("ScheduleExpressionTimezone") or "UTC"
        if tz not in _UTC_ZONES:
            return f"{name} uses timezone {tz!r}; only UTC/Etc/UTC are supported"
        try:
            mnt = _cron_minute_of_day(sc["ScheduleExpression"])
        except ValueError as exc:
            return (
                f"{name} has unsupported schedule {sc['ScheduleExpression']!r}: {exc}"
            )
        gap = min((n - mnt) % 1440, (mnt - n) % 1440)
        if gap < _SCHED_GUARD_MIN:
            return (
                f"{name} ({sc['ScheduleExpression']}) is {gap} min away; "
                f"needs {_SCHED_GUARD_MIN}"
            )
    return None


def _maint_window_guard(now: dt.datetime) -> str | None:
    """Return a reason if *now* is in the 04:00–06:15 UTC write window."""
    n = now.hour * 60 + now.minute
    if _MAINT_START_MIN <= n < _MAINT_END_MIN:
        return (
            f"current time {now.strftime('%H:%M')} UTC is in the maintenance "
            f"window 04:00\u201306:15 UTC"
        )
    return None


def require_write_window(
    scheduler: Any,
    stage: str,
    now: dt.datetime,
) -> str | None:
    """Return a refusal reason if writes are unsafe at *now*.

    Checks the maintenance window first, then pages ``list_schedules`` with
    ``NamePrefix`` for ``vsa-{stage}-tier2-`` and ``dms-{stage}-fi-`` across
    every group, and refuses if *now* is within 45 minutes (measured in
    seconds, not floored minutes) of any cron schedule found.

    ``rate()`` schedules are not cron schedules and are skipped: staging's
    ``vsa-{stage}-tier2-diagnostics-*`` runs ``rate(5 minutes)`` all day, so treating it
    as a refusal would forbid every write. Fails closed for non-UTC timezones,
    ``at()`` or unreadable expressions, and any list/get error. Returns ``None`` when
    safe.
    """
    maint = _maint_window_guard(now)
    if maint is not None:
        return maint

    n_sec = now.hour * 3600 + now.minute * 60 + now.second
    sched_guard_sec = _SCHED_GUARD_MIN * 60

    for prefix in (f"vsa-{stage}-tier2-", f"dms-{stage}-fi-"):
        kwargs: dict[str, Any] = {"NamePrefix": prefix}
        while True:
            try:
                page = scheduler.list_schedules(**kwargs)
            except Exception as exc:  # fail closed
                return f"list_schedules(NamePrefix={prefix!r}) failed: {exc}"
            for s in page.get("Schedules", []):
                name = s.get("Name", "")
                group = s.get("GroupName", "default")
                try:
                    sc = scheduler.get_schedule(Name=name, GroupName=group)
                except Exception as exc:  # fail closed
                    return f"get_schedule({name!r}) failed: {exc}"
                tz = sc.get("ScheduleExpressionTimezone") or "UTC"
                if tz not in _UTC_ZONES:
                    return (
                        f"{name} uses timezone {tz!r}; only UTC/Etc/UTC are supported"
                    )
                expr = sc.get("ScheduleExpression", "")
                if expr.strip().startswith("rate("):
                    continue  # not a cron schedule (see docstring)
                try:
                    mnt = _cron_minute_of_day(expr)
                except ValueError as exc:  # at()/unreadable → fail closed
                    return f"{name} has unsupported schedule {expr!r}: {exc}"
                mnt_sec = mnt * 60
                gap_sec = min(
                    (n_sec - mnt_sec) % 86400,
                    (mnt_sec - n_sec) % 86400,
                )
                if gap_sec < sched_guard_sec:
                    gap_min = gap_sec // 60
                    return (
                        f"{name} ({expr}) is {gap_min} min away; "
                        f"needs {_SCHED_GUARD_MIN}"
                    )
            token = page.get("NextToken")
            if not token:
                break
            kwargs["NextToken"] = token
    return None
