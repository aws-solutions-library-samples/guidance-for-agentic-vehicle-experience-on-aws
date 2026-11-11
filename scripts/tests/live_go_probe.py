"""Shared harness for the per-script live-go guard tests (scripts/lib/live_write_guard.py).

A script's test file subclasses ``GuardSuite`` (named ``Test...``) and sets the class attributes::

    class TestFiCostGuard(GuardSuite):
        script = "live-verify-fi-cost.py"
        go_env = "FI_COST_LIVE_GO"
        write_argvs = {"--allow-writes": ["--stage", "staging", "--allow-writes"]}
        read_argv = ["--stage", "staging"]
        refusal_rc = 1

Every probe runs with the AWS transport marker removed (what plain ``python3`` has), ``LIVE_TESTS=1``, ``HOME`` and the
working directory in ``tmp_path``, and sentinels on everything that could leave the process: ``boto3.client`` /
``resource``, a non-loopback ``socket.connect``, ``subprocess.Popen``, ``time.sleep``. A sentinel is a
``BaseException``, so a script's broad ``except Exception`` cannot swallow it. A call that gets past the guard hits a
sentinel (or stops at a later refusal); a call the guard refuses returns before any of them. The suite tells the two
apart by the refusal text.
"""
from __future__ import annotations

import importlib.util
import inspect
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent

REFUSAL = "would use real AWS credentials"


class Sentinel(BaseException):
    """Something tried to leave the process (a client, a socket, a subprocess, a sleep)."""


class Result:
    def __init__(self, rc: int | None, sentinel: str | None, out: str, err: str) -> None:
        self.rc, self.sentinel, self.out, self.err = rc, sentinel, out, err

    @property
    def refused(self) -> bool:
        return REFUSAL in self.err

    def __repr__(self) -> str:  # shown in assertion failures
        return f"Result(rc={self.rc}, sentinel={self.sentinel}, err={self.err[-300:]!r})"


def load_script(name: str, module_name: str | None = None) -> Any:
    spec = importlib.util.spec_from_file_location(module_name or ("lvg_" + name.replace("-", "_")), SCRIPTS_DIR / name)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def block_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transport without the conftest marker: what plain ``python3`` has."""
    import botocore.endpoint as be

    def _send(self: object, request: object) -> None:
        raise Sentinel("a request reached the transport")

    monkeypatch.setattr(be.Endpoint, "_send", _send)


def install_sentinels(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import boto3

    def _boom(what: str):
        def _f(*_a: object, **_k: object) -> None:
            raise Sentinel(what)

        return _f

    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(boto3, "client", _boom("boto3.client"))
    monkeypatch.setattr(boto3, "resource", _boom("boto3.resource"))
    monkeypatch.setattr(boto3.session.Session, "client", _boom("Session.client"))
    monkeypatch.setattr(boto3.session.Session, "resource", _boom("Session.resource"))
    real_connect = socket.socket.connect

    def _connect(self: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise Sentinel(f"socket.connect {host}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", _connect)
    monkeypatch.setattr(subprocess, "Popen", _boom("subprocess.Popen"))
    monkeypatch.setattr(time, "sleep", _boom("time.sleep"))


def call(mod: Any, argv: list[str], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> Result:
    """Run ``mod.main`` the way the script takes arguments (``main(argv)`` or ``main()`` reading ``sys.argv``)."""
    params = list(inspect.signature(mod.main).parameters.values())
    takes_argv = bool(params) and params[0].kind in (
        inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )
    monkeypatch.setattr(sys, "argv", [mod.__name__] + argv)
    rc: int | None = None
    sentinel: str | None = None
    try:
        rc = mod.main(argv) if takes_argv else mod.main()
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else 2
    except Sentinel as exc:
        sentinel = str(exc)
    cap = capsys.readouterr()
    return Result(rc, sentinel, cap.out, cap.err)


class GuardSuite:
    """Subclass as ``Test<Script>Guard`` and set the class attributes."""

    script: str = ""
    go_env: str = ""
    #: write flag -> an argv that gives only that flag (plus what the script needs to get past argument parsing)
    write_argvs: dict[str, list[str]] = {}
    #: an argv with no write flag
    read_argv: list[str] = []
    #: the exit code the script uses for a refusal
    refusal_rc: int = 1
    stage: str = "staging"

    # -- fixtures -------------------------------------------------------------------------------------------
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        saved = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
        install_sentinels(monkeypatch, tmp_path)
        monkeypatch.delenv(self.go_env, raising=False)
        self.mod = load_script(self.script)
        yield
        for s, h in saved.items():
            signal.signal(s, h)

    def _run(self, argv, capsys, monkeypatch) -> Result:
        return call(self.mod, argv, capsys, monkeypatch)

    # -- the checks ------------------------------------------------------------------------------------------
    def test_there_is_a_write_flag_to_guard(self) -> None:
        assert self.write_argvs, "list every write flag the script has"

    def test_block_off_without_go_refuses_every_write_flag(self, monkeypatch, capsys) -> None:
        block_off(monkeypatch)
        for flag, argv in self.write_argvs.items():
            r = self._run(argv, capsys, monkeypatch)
            assert r.refused and r.sentinel is None, (flag, r)
            assert r.rc == self.refusal_rc, (flag, r)
            assert self.go_env in r.err and flag in r.err, (flag, r)

    def test_refusal_points_a_debug_session_at_the_safe_path(self, monkeypatch, capsys) -> None:
        block_off(monkeypatch)
        argv = next(iter(self.write_argvs.values()))
        r = self._run(argv, capsys, monkeypatch)
        assert "conftest" in r.err and "helper must not" in r.err, r

    @pytest.mark.parametrize("value", ["", "production", "Staging", "staging ", "1"])
    def test_block_off_with_a_wrong_go_refuses(self, value, monkeypatch, capsys) -> None:
        block_off(monkeypatch)
        monkeypatch.setenv(self.go_env, value)
        for flag, argv in self.write_argvs.items():
            r = self._run(argv, capsys, monkeypatch)
            assert r.refused, (flag, value, r)

    def test_block_off_with_the_go_passes_the_guard(self, monkeypatch, capsys) -> None:
        block_off(monkeypatch)
        monkeypatch.setenv(self.go_env, self.stage)
        for flag, argv in self.write_argvs.items():
            r = self._run(argv, capsys, monkeypatch)
            assert not r.refused, (flag, r)

    def test_block_on_needs_no_go(self, monkeypatch, capsys) -> None:
        for flag, argv in self.write_argvs.items():
            r = self._run(argv, capsys, monkeypatch)
            assert not r.refused, (flag, r)

    def test_a_read_only_run_is_not_guarded(self, monkeypatch, capsys) -> None:
        block_off(monkeypatch)
        r = self._run(self.read_argv, capsys, monkeypatch)
        assert not r.refused, r

    def test_docstring_write_examples_carry_go_env(self) -> None:
        """Every docstring command that includes a write flag must start with <GO_ENV>=<stage>."""
        import re
        doc = self.mod.__doc__ or ""
        # Join continuation lines: "cmd \\\n    --flag" → "cmd --flag"
        joined = re.sub(r"\s*\\\n\s*", " ", doc)
        write_flag_re = re.compile(r"--(allow-[a-z-]+|approve-move|cleanup-only)\b")
        for line in joined.splitlines():
            if self.script in line and write_flag_re.search(line):
                assert line.lstrip().startswith(f"{self.go_env}={self.stage} LIVE_TESTS=1 "), (
                    f"docstring write-flag example must start with "
                    f"{self.go_env}={self.stage} LIVE_TESTS=1; got: {line!r}"
                )

    def test_a_refused_call_leaves_nothing_on_disk(self, monkeypatch, capsys, tmp_path) -> None:
        """A guard refusal must not write anything under HOME or cwd (no manifest partial write)."""
        block_off(monkeypatch)
        for flag, argv in self.write_argvs.items():
            r = self._run(argv, capsys, monkeypatch)
            assert r.refused, (flag, r)
            leftovers = list(tmp_path.rglob("*"))
            assert not leftovers, (flag, f"guard refused but wrote {leftovers}")

    def test_a_live_pass_is_not_a_block(self, monkeypatch, capsys) -> None:
        import botocore.endpoint as be

        monkeypatch.setitem(be.Endpoint._send.__globals__["_LIVE_PASS"], "active", True)
        for flag, argv in self.write_argvs.items():
            r = self._run(argv, capsys, monkeypatch)
            assert r.refused, (flag, r)
