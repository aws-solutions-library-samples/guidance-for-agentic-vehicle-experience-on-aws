"""Unit tests for scripts/fi_cms_live/harness.py — stage H.

Covers (brief §Tests):
  G1  guard allows a listed op → EndpointConnectionError at http://127.0.0.1:1
  G2  guard refuses an unlisted op → type(exc) is GuardViolation
  C1  temp user's failed delete marks cleanup failed
  C2  user still present after delete marks cleanup failed
  M1  manifest stage mismatch refused
  W1  maintenance window edges: 04:49/04:50/06:10/06:11
  S1  ±45 min of a cron schedule, wrapping midnight
  T1  SIGTERM during cleanup finishes delete then raises SystemExit(143)

Run:
    PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest \\
        scripts/tests/test_fi_cms_live_harness.py -q
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# Ensure the repo root is on sys.path (mirrors the entry-point scripts).
_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# Fake AWS creds so boto3 doesn't try to read ~/.aws or the metadata endpoint.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "harness-test-key")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "harness-test-secret")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

from scripts.fi_cms_live.harness import (  # noqa: E402
    GuardViolation,
    Results,
    _check_residue_cognito,
    _cron_minute_of_day,
    _defer_signals_for_restore,
    _delete_principal,
    _discover_schedule_group,
    _install_signal_handlers,
    _maint_window_guard,
    _resolve_cms_pool_id,
    _schedule_guard,
    _write_0600_atomic,
    open_manifest,
    principal,
    update_manifest,
    _client,
    require_write_window,
    delete_manifest_users,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_token(claims: dict | None = None) -> str:
    """Minimal unsigned JWT-like token whose payload is decodable."""
    if claims is None:
        claims = {"sub": "fake-sub", "email": "fake@example.invalid"}
    head = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(b"sig").rstrip(b"=").decode()
    return f"{head}.{body}.{sig}"


def _mock_idp(delete_raises: Exception | None = None, get_user_raises: Exception | None = None) -> Any:
    """Return a mock idp that gets through creation and configures cleanup."""
    m = MagicMock()
    m.admin_create_user.return_value = {"User": {}}
    m.admin_set_user_password.return_value = {}
    m.admin_add_user_to_group.return_value = {}
    m.initiate_auth.return_value = {
        "AuthenticationResult": {"IdToken": _fake_token()}
    }
    if delete_raises is not None:
        m.admin_delete_user.side_effect = delete_raises
    else:
        m.admin_delete_user.return_value = {}
    if get_user_raises is not None:
        m.admin_get_user.side_effect = get_user_raises
    else:
        m.admin_get_user.return_value = {"Username": "still-there"}
    return m


# ---------------------------------------------------------------------------
# G1 / G2: botocore guard
# ---------------------------------------------------------------------------


class TestGuard:
    """G1: allowed op → EndpointConnectionError; G2: unlisted op → GuardViolation."""

    def test_g1_allowed_op_raises_endpoint_error(self) -> None:
        """G1: op in allowlist hits the dead endpoint and gets EndpointConnectionError."""
        from botocore.exceptions import EndpointConnectionError  # noqa: PLC0415

        c = _client(
            "sts", "us-east-1",
            allowed_ops=frozenset({"sts.GetCallerIdentity", "GetCallerIdentity"}),
            endpoint_url="http://127.0.0.1:1",
        )
        with pytest.raises(EndpointConnectionError):
            c.get_caller_identity()

    def test_g2_refused_op_raises_guard_violation(self) -> None:
        """G2: op not in allowlist raises GuardViolation before any network I/O."""
        c = _client(
            "sts", "us-east-1",
            allowed_ops=frozenset({"sts.AssumeRole"}),
            endpoint_url="http://127.0.0.1:1",
        )
        with pytest.raises(Exception) as exc_info:
            c.get_caller_identity()
        assert type(exc_info.value) is GuardViolation, (
            f"expected GuardViolation, got {type(exc_info.value).__name__}: {exc_info.value}"
        )

    def test_g3_admin_delete_user_foreign_username_refused(self) -> None:
        """G3: AdminDeleteUser with a foreign username → GuardViolation;
        own livetest-* username → EndpointConnectionError (guard passes)."""
        from botocore.exceptions import EndpointConnectionError  # noqa: PLC0415

        run_prefix = "livetest-run-g3-"
        c = _client(
            "cognito-idp", "us-east-1",
            allowed_ops=frozenset({"cognito-idp.AdminDeleteUser"}),
            endpoint_url="http://127.0.0.1:1",
            cognito_username_prefix=run_prefix,
            # security c1 W1: a write client now needs a pool id too (setup only; the
            # assertions below are unchanged and still pin the username guard).
            cognito_pool_id="us-east-1_X",
        )
        # Foreign username → GuardViolation
        with pytest.raises(GuardViolation):
            c.admin_delete_user(
                UserPoolId="us-east-1_X",
                Username="real-user@example.com",
            )
        # Own livetest- username → guard passes → dead-endpoint error
        with pytest.raises(EndpointConnectionError):
            c.admin_delete_user(
                UserPoolId="us-east-1_X",
                Username=f"{run_prefix}tester@example.invalid",
            )


# ---------------------------------------------------------------------------
# C1 / C2: cleanup
# ---------------------------------------------------------------------------


class TestCleanup:
    """C1: failed delete marks cleanup failed; C2: user still present marks cleanup failed."""

    def test_c1_failed_delete_marks_cleanup_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """C1: admin_delete_user raises → res.mark_cleanup_failed() called."""
        monkeypatch.setenv("FI_CMS_LIVE_MANIFEST_DIR", str(tmp_path))
        manifest = open_manifest("run-c1", "staging")
        res = Results()
        idp = _mock_idp(delete_raises=RuntimeError("delete boom"))
        with principal(
            idp, "pool-c1", "client-c1", "run-c1", "tester", manifest, res, ["fleet-operator"]
        ):
            pass
        assert res._cleanup_failed, "mark_cleanup_failed not called on failed delete"

    def test_c2_user_still_present_marks_cleanup_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """C2: delete succeeds but AdminGetUser returns a user → mark_cleanup_failed."""
        monkeypatch.setenv("FI_CMS_LIVE_MANIFEST_DIR", str(tmp_path))
        manifest = open_manifest("run-c2", "staging")
        res = Results()
        # admin_delete_user succeeds; admin_get_user returns a user (still present).
        idp = _mock_idp(get_user_raises=None)  # admin_get_user returns {"Username": "still-there"}
        with principal(
            idp, "pool-c2", "client-c2", "run-c2", "tester", manifest, res, ["fleet-operator"]
        ):
            pass
        assert res._cleanup_failed, "mark_cleanup_failed not called when user still present"

    def test_c3_create_interrupted_after_commit_still_deleted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """C3: AdminCreateUser commits user then raises SystemExit; delete is still called.

        Regression for W1: previously ``if created:`` gated the delete, so an
        interrupted create left the user in the pool permanently.
        """
        from botocore.exceptions import ClientError  # noqa: PLC0415

        monkeypatch.setenv("FI_CMS_LIVE_MANIFEST_DIR", str(tmp_path))
        deleted: list[str] = []

        idp = MagicMock()
        # AdminCreateUser: "creates" the user in Cognito then raises SystemExit
        # (simulates lost connection after Cognito committed the user).
        idp.admin_create_user.side_effect = SystemExit(1)
        def _delete(**kw: Any) -> dict:
            deleted.append(kw["Username"])
            return {}
        idp.admin_delete_user.side_effect = _delete
        # AdminGetUser raises UserNotFoundException → confirms deletion.
        idp.admin_get_user.side_effect = ClientError(
            {"Error": {"Code": "UserNotFoundException", "Message": ""}},
            "AdminGetUser",
        )

        manifest = open_manifest("run-c3a", "staging")
        res = Results()

        with pytest.raises(SystemExit):
            with principal(
                idp, "pool-c3a", "client-c3a", "run-c3a",
                "tester", manifest, res, ["fleet-operator"]
            ):
                pass

        assert deleted, "AdminDeleteUser must be called even when AdminCreateUser raised"
        assert not res._cleanup_failed, "delete of a UserNotFoundException-confirmed user must not mark cleanup failed"

    def test_c3_admin_get_user_access_denied_marks_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """C3 (W6): AdminGetUser raises AccessDeniedException → mark_cleanup_failed.

        A non-UserNotFound error on the confirming read must be treated as a
        failed cleanup, not as proof of deletion.  Guards against the
        ``if True:`` mutant that would accept any exception as confirmed.
        """
        from botocore.exceptions import ClientError  # noqa: PLC0415

        monkeypatch.setenv("FI_CMS_LIVE_MANIFEST_DIR", str(tmp_path))
        res = Results()
        idp = MagicMock()
        idp.admin_delete_user.return_value = {}
        idp.admin_get_user.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "Access denied"}},
            "AdminGetUser",
        )
        _delete_principal(
            idp, "pool-c3b", "livetest-run-c3b-tester@example.invalid", "tester", res
        )
        assert res._cleanup_failed, (
            "AccessDeniedException on AdminGetUser must mark cleanup failed"
        )


# ---------------------------------------------------------------------------
# M1: manifest stage mismatch
# ---------------------------------------------------------------------------


class TestManifest:
    """M1: re-opening a manifest with a different stage raises SystemExit."""

    def test_m1_stage_mismatch_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FI_CMS_LIVE_MANIFEST_DIR", str(tmp_path))
        open_manifest("run-m1", "staging")           # first open: staging
        with pytest.raises(SystemExit) as exc_info:
            open_manifest("run-m1", "prod")          # re-open with different stage
        assert "staging" in str(exc_info.value).lower() or "prod" in str(exc_info.value).lower()

    def test_m2_user_recorded_before_create(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M2: manifest user/<role> entry is present when AdminCreateUser fires.

        Regression for W1: the manifest must be written BEFORE the Cognito call
        so that a crash-recovery pass can discover and delete orphaned users.
        """
        from botocore.exceptions import ClientError  # noqa: PLC0415

        monkeypatch.setenv("FI_CMS_LIVE_MANIFEST_DIR", str(tmp_path))
        manifest_check: dict = {}
        manifest_path: list[Path] = []

        def _create(**kw: Any) -> None:
            # The manifest must already contain user/tester at this point.
            data = json.loads(manifest_path[0].read_text())
            manifest_check["present"] = "user/tester" in data.get("items", {})
            manifest_check["status"] = data.get("items", {}).get("user/tester", {}).get("status")
            raise SystemExit(1)  # bail so the test stays fast

        idp = MagicMock()
        idp.admin_create_user.side_effect = _create
        idp.admin_delete_user.return_value = {}
        idp.admin_get_user.side_effect = ClientError(
            {"Error": {"Code": "UserNotFoundException", "Message": ""}},
            "AdminGetUser",
        )

        mfest = open_manifest("run-m2", "staging")
        manifest_path.append(mfest)
        res = Results()

        with pytest.raises(SystemExit):
            with principal(
                idp, "pool-m2", "client-m2", "run-m2",
                "tester", mfest, res, ["fleet-operator"]
            ):
                pass

        assert manifest_check.get("present"), "user/tester must be in manifest before AdminCreateUser"
        assert manifest_check.get("status") == "pending"

    def test_m3_manifest_mode_0600_dir_0700(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M3 (W5): manifest file is 0600; its containing directory is 0700."""
        import stat  # noqa: PLC0415

        monkeypatch.setenv("FI_CMS_LIVE_MANIFEST_DIR", str(tmp_path))
        manifest = open_manifest("run-m3", "staging")

        file_mode = stat.S_IMODE(manifest.stat().st_mode)
        dir_mode = stat.S_IMODE(manifest.parent.stat().st_mode)
        assert file_mode == 0o600, f"manifest must be 0600, got {oct(file_mode)}"
        assert dir_mode == 0o700, f"manifest dir must be 0700, got {oct(dir_mode)}"

        # Mode must be preserved after an update.
        update_manifest(manifest, "test-key", {"val": 1})
        file_mode_after = stat.S_IMODE(manifest.stat().st_mode)
        assert file_mode_after == 0o600, (
            f"manifest must be 0600 after update_manifest, got {oct(file_mode_after)}"
        )

    def test_m4_update_is_atomic(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M4 (W5): if os.replace fails the original manifest content is unchanged.

        Guards against the mutant that replaces _write_0600_atomic(manifest, ...)
        with manifest.write_text(...) — which writes directly and never raises
        from the mocked os.replace, causing the assertion below to fail.
        """
        monkeypatch.setenv("FI_CMS_LIVE_MANIFEST_DIR", str(tmp_path))
        manifest = open_manifest("run-m4", "staging")
        original = manifest.read_text()

        def _raise_on_replace(*args: Any, **kw: Any) -> None:
            raise OSError("simulated replace failure")

        monkeypatch.setattr("scripts.fi_cms_live.harness.os.replace", _raise_on_replace)

        with pytest.raises(OSError, match="simulated replace failure"):
            update_manifest(manifest, "test-key", {"val": 99})

        assert manifest.read_text() == original, (
            "manifest content must be unchanged when os.replace fails"
        )

    def test_wc1_manifest_dir_0700_new_subdir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """W-C: open_manifest sets dir to 0700 even when mkdir starts at 0755.

        The existing test_m3 points at pytest's tmp_path which is already 0700,
        so HM1 (replacing os.chmod with pass) survives it. This test points at a
        not-yet-created subdir so the chmod must actually run.
        Mutant HM1 (os.chmod -> pass) is caught here."""
        import stat  # noqa: PLC0415
        sub = tmp_path / "sub"
        assert not sub.exists(), "sub must not exist before the test"
        monkeypatch.setenv("FI_CMS_LIVE_MANIFEST_DIR", str(sub))
        open_manifest("run-wc1", "staging")
        dir_mode = stat.S_IMODE(sub.stat().st_mode)
        assert dir_mode == 0o700, (
            f"manifest dir must be 0700 after open_manifest; got {oct(dir_mode)}"
        )

    def test_wc2_update_manifest_blocks_on_flock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """W-C: update_manifest holds LOCK_EX for the whole read-modify-write.

        Uses a subprocess to hold LOCK_EX on the .lock file (same-process BSD
        flock on macOS does not block between fds; only cross-process blocks).
        Asserts the update thread is still alive after a short join while the
        subprocess holds the lock, then finishes after the subprocess exits.
        Mutant HM2 (drop the fcntl.flock LOCK_EX line) is caught here."""
        import subprocess  # noqa: PLC0415
        import threading  # noqa: PLC0415

        monkeypatch.setenv("FI_CMS_LIVE_MANIFEST_DIR", str(tmp_path))
        manifest = open_manifest("run-wc2", "staging")
        lock_path = manifest.with_suffix(".lock")

        # Subprocess acquires LOCK_EX and signals via stdout, then sleeps.
        locker_code = "\n".join([
            "import fcntl, os, sys, time",
            f"fd = os.open({str(lock_path)!r}, os.O_WRONLY | os.O_CREAT, 0o600)",
            "fcntl.flock(fd, fcntl.LOCK_EX)",
            "sys.stdout.write('locked\\n'); sys.stdout.flush()",
            "time.sleep(10)",
        ])
        locker = subprocess.Popen(
            [sys.executable, "-c", locker_code],
            stdout=subprocess.PIPE,
        )
        try:
            assert locker.stdout.read(7) == b"locked\n"  # wait for lock to be held
            errors: list[Exception] = []

            def _update() -> None:
                try:
                    update_manifest(manifest, "test-key", {"v": 1})
                except Exception as exc:
                    errors.append(exc)

            t = threading.Thread(target=_update, daemon=True)
            t.start()
            t.join(timeout=0.4)
            assert t.is_alive(), (
                "update_manifest must block while another process holds LOCK_EX"
            )
            locker.terminate()
            locker.wait(timeout=2)
            t.join(timeout=2.0)
            assert not t.is_alive(), "update_manifest did not finish after lock release"
            assert not errors, f"update_manifest raised: {errors}"
        finally:
            if locker.poll() is None:
                locker.terminate()
                locker.wait(timeout=2)


# ---------------------------------------------------------------------------
# W1: maintenance window edges
# ---------------------------------------------------------------------------


def _utc(hhmm: str) -> dt.datetime:
    h, m = map(int, hhmm.split(":"))
    return dt.datetime(2026, 1, 1, h, m, 0, tzinfo=dt.timezone.utc)


class TestWindowGuard:
    """W1: 04:00–06:15 inclusive; 03:59 and 06:16 are outside."""

    @pytest.mark.parametrize("hhmm,expect_refused", [
        ("03:59", False),  # one minute before window start
        ("04:00", True),   # window start (inclusive)
        ("06:15", True),   # window end (inclusive)
        ("06:16", False),  # one minute after window end
    ])
    def test_w1_maint_window_edges(self, hhmm: str, expect_refused: bool) -> None:
        result = _maint_window_guard(_utc(hhmm))
        if expect_refused:
            assert result is not None, f"{hhmm} should be inside the window"
        else:
            assert result is None, f"{hhmm} should be outside the window; got {result!r}"


# ---------------------------------------------------------------------------
# S1: schedule guard wrapping midnight
# ---------------------------------------------------------------------------


def _mock_scheduler(name: str, group: str, cron_expr: str) -> Any:
    """Minimal mock scheduler for _schedule_guard tests."""
    sched = MagicMock()
    sched.list_schedules.return_value = {
        "Schedules": [{"Name": name, "GroupName": group}]
    }
    sched.get_schedule.return_value = {
        "ScheduleExpression": cron_expr,
        "ScheduleExpressionTimezone": "UTC",
    }
    return sched


class TestScheduleGuard:
    """S1: ±45 min guard wraps midnight correctly."""

    # Schedule at 00:30.  ±45 min: [23:45..01:15].
    # gap = min((n - 30) % 1440, (30 - n) % 1440)
    # 23:46 → gap 44 → refused;  23:44 → gap 46 → allowed
    # 01:14 → gap 44 → refused;  01:15 → gap 45 → allowed (45 is NOT < 45)
    @pytest.mark.parametrize("hhmm,expect_refused", [
        ("23:46", True),   # 44 min before 00:30, wraps midnight
        ("23:44", False),  # 46 min before 00:30
        ("01:14", True),   # 44 min after 00:30
        ("01:15", False),  # 45 min after 00:30 (boundary — exactly 45, not refused)
    ])
    def test_s1_schedule_guard_wrapping_midnight(self, hhmm: str, expect_refused: bool) -> None:
        sched = _mock_scheduler("test-schedule", "default", "cron(30 0 * * ? *)")
        names = [("test-schedule", "default", "us-east-1")]
        now = _utc(hhmm)
        result = _schedule_guard(sched, names, "staging", now)
        if expect_refused:
            assert result is not None, f"{hhmm} should be refused (near 00:30); got None"
        else:
            assert result is None, f"{hhmm} should be allowed; got {result!r}"

    def test_s2_dms_fi_schedule_discovered_by_prefix(self) -> None:
        """S2: require_write_window finds dms-staging-fi-* by NamePrefix and refuses at 03:57."""
        sched = MagicMock()

        def _list(NamePrefix: str = "", **kw: Any) -> dict:
            if NamePrefix.startswith("dms-staging-fi-"):
                return {
                    "Schedules": [
                        {"Name": "dms-staging-fi-warranty-candidates",
                         "GroupName": "dms-staging-fi"}
                    ]
                }
            return {"Schedules": []}

        sched.list_schedules.side_effect = _list
        sched.get_schedule.return_value = {
            "ScheduleExpression": "cron(40 4 * * ? *)",
            "ScheduleExpressionTimezone": "UTC",
        }

        # 03:57 UTC — 43 min before 04:40 cron, outside maintenance window → refused by schedule guard
        now = dt.datetime(2026, 1, 1, 3, 57, 0, tzinfo=dt.timezone.utc)
        result = require_write_window(sched, "staging", now)
        assert result is not None, "03:57 should be refused (43 min before 04:40 schedule)"
        assert "dms-staging-fi-warranty-candidates" in result

    # Staging's schedules as read live 2026-10-22 (get-schedule; group default).
    _LIVE_SCHEDULES = {
        "vsa-staging-tier2-daily-rebalance-us-west-2": ("cron(0 6 * * ? *)", "Etc/UTC"),
        "vsa-staging-tier2-daily-tires-us-west-2": ("cron(20 2 * * ? *)", "Etc/UTC"),
        "vsa-staging-tier2-diagnostics-us-west-2": ("rate(5 minutes)", "Etc/UTC"),
        "vsa-staging-tier2-daily-battery-us-west-2": ("cron(0 2 * * ? *)", "Etc/UTC"),
        "vsa-staging-tier2-daily-brakes-us-west-2": ("cron(40 2 * * ? *)", "Etc/UTC"),
        "vsa-staging-tier2-daily-service-us-west-2": ("cron(40 5 * * ? *)", "Etc/UTC"),
        "dms-staging-fi-warranty-adjudicator": ("cron(0 6 * * ? *)", "UTC"),
        "dms-staging-fi-dealer-progress": ("cron(40 5 * * ? *)", "UTC"),
        "dms-staging-fi-warranty-candidates": ("cron(40 4 * * ? *)", "UTC"),
        "vsa-staging-tier2-daily-cost-us-west-2": ("cron(0 4 * * ? *)", "Etc/UTC"),
        "vsa-staging-tier2-daily-recall-us-west-2": ("cron(0 5 * * ? *)", "Etc/UTC"),
        "vsa-staging-tier2-daily-warranty-us-west-2": ("cron(20 5 * * ? *)", "Etc/UTC"),
    }

    def _live_sched(self, extra: "dict | None" = None) -> Any:
        table = dict(self._LIVE_SCHEDULES, **(extra or {}))
        sched = MagicMock()
        sched.list_schedules.side_effect = lambda NamePrefix="", **kw: {"Schedules": [
            {"Name": n, "GroupName": "default"} for n in table if n.startswith(NamePrefix)]}
        sched.get_schedule.side_effect = lambda Name, GroupName: {
            "ScheduleExpression": table[Name][0], "ScheduleExpressionTimezone": table[Name][1]}
        return sched

    def test_s3_live_schedules_rate_skipped_writes_allowed_midday(self) -> None:
        """The 5-minute diagnostics rate() schedule must not forbid every write.

        Mutant caught: dropping the rate() skip (every time refused)."""
        now = dt.datetime(2026, 10, 22, 14, 0, 0, tzinfo=dt.timezone.utc)
        assert require_write_window(self._live_sched(), "staging", now) is None

    def test_s4_live_schedules_refuse_near_each_cron(self) -> None:
        """06:44:59 is 44 min 59 s after the 06:00 crons: refused; 06:45:00 allowed;
        01:15:01 is under 45 min before 02:00: refused."""
        s = self._live_sched()
        at = lambda h, m, sec=0: dt.datetime(2026, 10, 22, h, m, sec, tzinfo=dt.timezone.utc)  # noqa: E731
        assert require_write_window(s, "staging", at(6, 44, 59)) is not None
        assert require_write_window(s, "staging", at(6, 45, 0)) is None
        assert require_write_window(s, "staging", at(1, 15, 1)) is not None
        assert require_write_window(s, "staging", at(1, 15, 0)) is None

    def test_s5_at_schedule_still_refuses(self) -> None:
        """A one-off at() schedule is unreadable as a daily cron: fail closed."""
        s = self._live_sched({"vsa-staging-tier2-oneoff": ("at(2026-10-22T14:00:00)", "UTC")})
        now = dt.datetime(2026, 10, 22, 14, 0, 0, tzinfo=dt.timezone.utc)
        assert require_write_window(s, "staging", now) is not None

    def test_wb2_non_utc_tz_refused(self) -> None:
        """W-B: a schedule in Europe/Berlin timezone fails closed.
        Mutant HW2 (if False: for the _UTC_ZONES check) survives without this test."""
        sched = MagicMock()
        sched.list_schedules.return_value = {
            "Schedules": [{"Name": "vsa-staging-tier2-daily-berlin", "GroupName": "default"}]
        }
        sched.get_schedule.return_value = {
            "ScheduleExpression": "cron(0 6 * * ? *)",
            "ScheduleExpressionTimezone": "Europe/Berlin",
        }
        now = dt.datetime(2026, 1, 1, 14, 0, 0, tzinfo=dt.timezone.utc)
        result = require_write_window(sched, "staging", now)
        assert result is not None, (
            "Europe/Berlin timezone must refuse writes; require_write_window returned None"
        )
        assert "Europe/Berlin" in result, (
            f"refusal reason should mention the timezone name; got: {result!r}"
        )

    def test_wb3_list_schedules_error_refused(self) -> None:
        """W-B: list_schedules raising an exception fails closed.
        Mutant HW3 (return None on list error) survives without this test."""
        sched = MagicMock()
        sched.list_schedules.side_effect = RuntimeError("connection refused")
        now = dt.datetime(2026, 1, 1, 14, 0, 0, tzinfo=dt.timezone.utc)
        result = require_write_window(sched, "staging", now)
        assert result is not None, (
            "list_schedules error must refuse writes; require_write_window returned None"
        )

    def test_wb1_near_cron_on_page2_refused(self) -> None:
        """W-B: a near cron only on page 2 of list_schedules is still refused.
        Mutant HW1 (break instead of following NextToken) survives without this test."""
        sched = MagicMock()

        def _list(NamePrefix: str = "", **kw: Any) -> dict:
            if kw.get("NextToken") == "tok1":
                return {"Schedules": [{"Name": f"{NamePrefix}page2-job", "GroupName": "default"}]}
            return {"Schedules": [], "NextToken": "tok1"}

        sched.list_schedules.side_effect = _list
        # cron(10 14 * * ? *) = 14:10; now=14:00 → 10 min gap → refused (< 45)
        sched.get_schedule.return_value = {
            "ScheduleExpression": "cron(10 14 * * ? *)",
            "ScheduleExpressionTimezone": "UTC",
        }
        now = dt.datetime(2026, 1, 1, 14, 0, 0, tzinfo=dt.timezone.utc)
        result = require_write_window(sched, "staging", now)
        assert result is not None, (
            "near cron only on page 2 must still be refused; require_write_window returned None"
        )


# ---------------------------------------------------------------------------
# Residue check
# ---------------------------------------------------------------------------


class TestResidue:
    """S1: _check_residue_cognito returns FAIL (not skip) when pool_id is None."""

    def test_s1_none_pool_returns_fail(self) -> None:
        """S1: pool_id=None must return (False, ...) so cleanup can't slip through."""
        idp = MagicMock()
        ok, msg = _check_residue_cognito(idp, None, "run-s1")
        assert not ok, (
            f"_check_residue_cognito(pool_id=None) must return FAIL, got ok={ok!r}: {msg!r}"
        )
        assert "None" in msg or "pool" in msg.lower() or "unknown" in msg.lower(), (
            f"message should mention pool/None: {msg!r}"
        )


# ---------------------------------------------------------------------------
# T1: SIGTERM during cleanup
# ---------------------------------------------------------------------------


class TestSigterm:
    """T1: SIGTERM during the delete is deferred; delete finishes; SystemExit(143) raised."""

    def test_t1_sigterm_during_cleanup_finishes_delete_then_exits_143(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FI_CMS_LIVE_MANIFEST_DIR", str(tmp_path))

        # Save original handlers to restore after the test.
        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        old_hup = signal.getsignal(signal.SIGHUP)

        delete_log: list[str] = []

        def delete_side_effect(**kw: Any) -> dict:
            # Raise SIGTERM mid-delete; _defer_signals_for_restore captures it.
            signal.raise_signal(signal.SIGTERM)
            delete_log.append("deleted")
            return {}

        def get_user_side_effect(**kw: Any) -> None:
            # Real ClientError so the harness correctly recognises UserNotFoundException
            # and does NOT mark cleanup as failed (W6).
            from botocore.exceptions import ClientError  # noqa: PLC0415
            raise ClientError(
                {"Error": {"Code": "UserNotFoundException", "Message": ""}},
                "AdminGetUser",
            )

        idp = _mock_idp()
        idp.admin_delete_user.side_effect = delete_side_effect
        idp.admin_delete_user.return_value = {}
        idp.admin_get_user.side_effect = get_user_side_effect

        manifest = open_manifest("run-t1", "staging")
        res = Results()

        try:
            _install_signal_handlers()   # SIGTERM → SystemExit(143), as the brief requires
            with pytest.raises(SystemExit) as exc_info:
                with principal(
                    idp, "pool-t1", "client-t1", "run-t1",
                    "tester", manifest, res, ["fleet-operator"]
                ):
                    pass   # normal exit triggers finally / cleanup
            assert exc_info.value.code == 143, exc_info.value.code
            assert "deleted" in delete_log, "delete must run before SystemExit(143)"
            assert not res._cleanup_failed, (
                "UserNotFoundException on AdminGetUser must not mark cleanup failed"
            )
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)
            signal.signal(signal.SIGHUP, old_hup)


class TestCognitoPoolGuard:
    """Battery c1 HA6: an Admin* write to another pool is refused even for a run user."""

    def test_g4_admin_write_other_pool_refused(self) -> None:
        from botocore.exceptions import EndpointConnectionError  # noqa: PLC0415

        prefix = "livetest-run-g4-"
        c = _client(
            "cognito-idp", "us-west-2",
            allowed_ops=frozenset({"cognito-idp.AdminDeleteUser"}),
            endpoint_url="http://127.0.0.1:1",
            cognito_username_prefix=prefix,
            cognito_pool_id="us-west-2_RunPool",
        )
        with pytest.raises(GuardViolation):
            c.admin_delete_user(UserPoolId="us-west-2_Other", Username=f"{prefix}a@example.invalid")
        with pytest.raises(EndpointConnectionError):
            c.admin_delete_user(UserPoolId="us-west-2_RunPool", Username=f"{prefix}a@example.invalid")


# ---------------------------------------------------------------------------
# Security review c1 (M W1, architect): a Cognito client that may write needs BOTH a
# non-empty username prefix and pool id, or it isn't built.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prefix,pool", [
    ("", "us-west-2_POOL"), ("livetest-r1-", ""), ("", ""),
])
@pytest.mark.parametrize("write_op", ["cognito-idp.AdminCreateUser", "AdminDeleteUser",
                                      "cognito-idp.AdminAddUserToGroup"])
def test_sec_w1_cognito_write_client_needs_prefix_and_pool(prefix: str, pool: str,
                                                           write_op: str) -> None:
    """Deliberate break: drop the build-time check -> the client builds and no error."""
    try:
        _client("cognito-idp", "us-west-2",
                allowed_ops=frozenset({write_op, "cognito-idp.ListUsers"}),
                endpoint_url="http://127.0.0.1:1",
                cognito_username_prefix=prefix, cognito_pool_id=pool)
    except Exception as exc:  # noqa: BLE001
        assert type(exc) is GuardViolation, f"{type(exc).__name__}: {exc}"
        assert "needs a non-empty" in str(exc), str(exc)
    else:
        raise AssertionError(f"built a Cognito write client with prefix={prefix!r} pool={pool!r}")


def test_sec_w1_cognito_read_client_builds_without_prefix() -> None:
    """Direction 2: read-only Cognito clients (pre, residue checks) still build with no
    prefix/pool, and a write client with both builds and still refuses a foreign pool."""
    ro = _client("cognito-idp", "us-west-2",
                 allowed_ops=frozenset({"cognito-idp.ListUsers", "cognito-idp.AdminGetUser",
                                        "cognito-idp.ListUserPoolClients"}),
                 endpoint_url="http://127.0.0.1:1")
    assert ro is not None
    rw = _client("cognito-idp", "us-west-2",
                 allowed_ops=frozenset({"cognito-idp.AdminCreateUser"}),
                 endpoint_url="http://127.0.0.1:1",
                 cognito_username_prefix="livetest-r1-", cognito_pool_id="us-west-2_POOL")
    try:
        rw.admin_create_user(UserPoolId="us-west-2_OTHER", Username="livetest-r1-x@example.invalid")
    except Exception as exc:  # noqa: BLE001
        assert type(exc) is GuardViolation, f"{type(exc).__name__}: {exc}"
    else:
        raise AssertionError("foreign pool admitted")
