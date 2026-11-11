"""Tests for scripts/live-verify-fi-fleet-health.py — safety core.

Covers (brief § Tests):
  E1  all items pass -> exit 0
  E2  a skip -> exit 2
  E3  a fail -> exit 1
  E4  fail + skip -> exit 1 (fail takes precedence)
  S1  stage refusal: --stage prod -> exit 1 before any AWS call
  G1  guard allows a listed op on a real client at http://127.0.0.1:1 (no GuardViolation)
  G2  guard refuses an unlisted op on a real client at http://127.0.0.1:1 (GuardViolation)
  D1  signal raised during restore hold is re-raised after context exits
  P1  subprocess runs the file from a temp cwd with PYTHONPATH stripped; --help exits 0
  R1  PASS rows carry no failure wording (on_fail not shown on PASS)

Run (AWS env stripped, fake creds injected):
    env -u AWS_PROFILE AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing \
      PYTHONDONTWRITEBYTECODE=1 \
      .venv/bin/python -m pytest scripts/tests/test_live_verify_fi_fleet_health_core.py -q
"""
from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Load the script as a module (hyphenated name requires importlib)
# ---------------------------------------------------------------------------

_SCRIPT = (
    Path(__file__).resolve().parents[1] / "live-verify-fi-fleet-health.py"
)
_spec = importlib.util.spec_from_file_location("live_verify_fi_fleet_health", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

# Names under test
Results = _mod.Results
GuardViolation = _mod.GuardViolation
_client = _mod._client
_defer_signals_for_restore = _mod._defer_signals_for_restore
_compute_skips = _mod._compute_skips
main = _mod.main
_ALL_ITEMS = _mod._ALL_ITEMS
_WRITE_ITEMS = _mod._WRITE_ITEMS
_MANAGED_WRITE_ITEMS = _mod._MANAGED_WRITE_ITEMS
_PAGING_ITEMS = _mod._PAGING_ITEMS


# ===========================================================================
# E1-E4: Exit mapping with fake items
# We test the exit-code logic that sits in main() by driving Results directly.
# The three conditions are:
#   fail or _cleanup_failed -> 1
#   has_any_skip (no fail)  -> 2
#   else                    -> 0
# ===========================================================================


def test_exit_all_pass_returns_0():
    """E1: every item passes -> exit 0."""
    res = Results()
    res.check("item-1", True, "looks good")
    res.check("item-2", True, "also good")
    # Simulate exit logic
    code = 1 if (res.failed or res._cleanup_failed) else (2 if res.has_any_skip else 0)
    assert code == 0


def test_exit_skip_returns_2():
    """E2: a skip (and no fail) -> exit 2."""
    res = Results()
    res.check("item-1", True, "good")
    res.skip("item-2", "not built yet")
    code = 1 if (res.failed or res._cleanup_failed) else (2 if res.has_any_skip else 0)
    assert code == 2


def test_exit_fail_returns_1():
    """E3: a fail -> exit 1."""
    res = Results()
    res.check("item-1", False, on_fail="something went wrong")
    code = 1 if (res.failed or res._cleanup_failed) else (2 if res.has_any_skip else 0)
    assert code == 1


def test_exit_fail_plus_skip_returns_1():
    """E4: fail + skip -> exit 1 (fail wins over skip)."""
    res = Results()
    res.check("item-1", False, on_fail="bad")
    res.skip("item-2", "not built yet")
    code = 1 if (res.failed or res._cleanup_failed) else (2 if res.has_any_skip else 0)
    assert code == 1


# ===========================================================================
# S1: Stage refusal before any AWS call
# main() gate 2 returns 1 when --stage != 'staging', BEFORE importing boto3.
# ===========================================================================


def test_stage_refusal_no_aws_call():
    """S1: --stage prod -> exit 1; boto3.client never called.

    Security review c1: boto3.client raises a sentinel, so a dropped stage gate fails
    on this test's own assertion at the first client call rather than running --all
    against MagicMock clients until the battery's 30-minute timeout.
    """
    mock_boto3 = MagicMock()
    mock_boto3.client.side_effect = _ReachedAws("boto3.client reached")

    with patch.object(sys, "argv", ["prog", "--stage", "prod", "--all"]):
        with patch.dict(os.environ, {"LIVE_TESTS": "1"}, clear=False):
            # boto3 is imported lazily inside main() after the stage gate.
            # Patching it at the module level confirms it is never reached.
            with patch.dict(sys.modules, {"boto3": mock_boto3}):
                try:
                    result = main()
                except _ReachedAws as exc:
                    result = exc

    assert result == 1, f"--stage prod: expected exit 1 before any AWS call; got {result!r}"
    mock_boto3.client.assert_not_called()


# ===========================================================================
# G1-G2: Botocore guard (real client, unreachable endpoint)
# A real boto3 client pointed at http://127.0.0.1:1 fires the before-call
# event before any network I/O.  Listed ops pass the guard (connection error
# is expected); unlisted ops raise GuardViolation immediately.
# ===========================================================================


def test_guard_allows_listed_op():
    """G1: listed op proceeds past guard to network layer (no GuardViolation)."""
    client = _client(
        "s3",
        "us-east-1",
        allowed_ops=frozenset({"s3.ListBuckets"}),
        endpoint_url="http://127.0.0.1:1",
    )
    # Must not raise GuardViolation; any connection error is expected and acceptable.
    try:
        client.list_buckets()
        pytest.fail("expected a network-level exception for unreachable endpoint")
    except GuardViolation:
        pytest.fail("should NOT raise GuardViolation for an allowed operation")
    except Exception:
        pass  # connection refused or similar — correct behaviour


def test_guard_refuses_unlisted_op():
    """G2: unlisted op raises GuardViolation (asserted by type, not message)."""
    client = _client(
        "s3",
        "us-east-1",
        allowed_ops=frozenset({"s3.ListBuckets"}),
        endpoint_url="http://127.0.0.1:1",
    )
    with pytest.raises(Exception) as ei:
        client.get_object(Bucket="test-bucket", Key="test-key")
    assert type(ei.value) is GuardViolation, (
        f"expected GuardViolation but got {type(ei.value).__name__}: {ei.value}"
    )


# ===========================================================================
# D1: Signal deferred during restore hold is re-raised after context exits
# ===========================================================================


def test_deferred_signal_reraised_after_context():
    """D1: SIGTERM sent inside _defer_signals_for_restore is re-raised after."""
    delivered: list[int] = []
    original_handler = signal.getsignal(signal.SIGTERM)
    # Install a recording handler so os.kill fires into it (not the default)
    signal.signal(signal.SIGTERM, lambda s, f: delivered.append(s))
    try:
        with _defer_signals_for_restore():
            # The context installs _record as the SIGTERM handler.
            # Call it directly to simulate a signal arriving during the hold.
            held_handler = signal.getsignal(signal.SIGTERM)
            assert callable(held_handler), "context should have installed a handler"
            held_handler(signal.SIGTERM, None)
            # Signal must NOT have been delivered yet (still held)
            assert delivered == [], "signal should be held, not delivered during context"
        # Context restored the recording handler and called os.kill(pid, SIGTERM),
        # which fired the recording handler -> delivered.
        assert delivered == [signal.SIGTERM], "signal should be re-raised after context"
    finally:
        signal.signal(signal.SIGTERM, original_handler)


# ===========================================================================
# D2: SIGHUP deferred during restore hold and re-raised after context exits
# ===========================================================================


def test_sighup_deferred_and_reraised_after_context():
    """D2: SIGHUP sent inside _defer_signals_for_restore is re-raised after."""
    delivered: list[int] = []
    original_handler = signal.getsignal(signal.SIGHUP)
    signal.signal(signal.SIGHUP, lambda s, f: delivered.append(s))
    try:
        with _defer_signals_for_restore():
            held_handler = signal.getsignal(signal.SIGHUP)
            # Before fix: SIGHUP is not held (SIG_DFL or original), so the handler above fires
            assert callable(held_handler), (
                "SIGHUP handler should be the deferring _record function"
            )
            held_handler(signal.SIGHUP, None)
            assert delivered == [], "SIGHUP should be held, not delivered during context"
        assert delivered == [signal.SIGHUP], "SIGHUP should be re-raised after context"
    finally:
        signal.signal(signal.SIGHUP, original_handler)


# ===========================================================================
# P1: Subprocess test — file runs from temp cwd with PYTHONPATH stripped
# ===========================================================================


def test_subprocess_help_exits_0(tmp_path: Path):
    """P1: running the file with --help from a temp cwd (no PYTHONPATH) exits 0."""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["AWS_ACCESS_KEY_ID"] = "testing"
    env["AWS_SECRET_ACCESS_KEY"] = "testing"
    env.pop("AWS_PROFILE", None)

    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--help"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"--help returned {result.returncode}; stderr={result.stderr[:400]!r}"
    )


def test_subprocess_lazy_imports(tmp_path: Path) -> None:
    """P1b: lazily imported agents.* modules are importable from a temp cwd (no PYTHONPATH).

    The script's top-block adds _REPO_ROOT to sys.path so that inner
    imports of agents.fi_fleet_health.bands and
    agents.supervisor.tools.fleet_recommendation succeed even when the
    script is run from a directory outside the repo.
    """
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["AWS_ACCESS_KEY_ID"] = "testing"
    env["AWS_SECRET_ACCESS_KEY"] = "testing"
    env.pop("AWS_PROFILE", None)

    # Snippet: load the script (sets sys.path), then import both lazy modules.
    snippet = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('_lv', r'{_SCRIPT}')\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "from agents.fi_fleet_health.bands import classify_needs\n"
        "from agents.supervisor.tools.fleet_recommendation import _derive_decision_action_id\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", snippet],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"lazy import subprocess returned {result.returncode}; "
        f"stderr={result.stderr[:400]!r}"
    )


# ===========================================================================
# R1: PASS rows carry no failure wording
# ===========================================================================


def test_pass_rows_carry_no_failure_wording():
    """R1: on_fail text is NOT shown on a PASS row; detail is shown as-is."""
    res = Results()
    res.check(
        "my-check",
        True,
        "all clear",
        on_fail="FAILURE: critical problem found",
    )
    label, status, detail = res.rows[0]
    assert status == "PASS"
    assert detail == "all clear", "PASS row should show original detail"
    assert "FAILURE" not in detail, "on_fail text must not appear on a PASS row"
    assert "fail" not in detail.lower(), "no failure wording on PASS rows"



# ===========================================================================
# BG (boto3 guard): D1 — main() must never call boto3.resource; every client
# built in main() for residue ops must carry the guard (write ops -> GuardViolation).
# ===========================================================================


def test_d1_main_no_boto3_resource_and_clients_guarded(tmp_path, monkeypatch):
    """BG1/BG2/BG3: main() never calls boto3.resource; residue clients are guarded.

    RED before D1 fix: main() calls boto3.resource("dynamodb") -> resource_calls != [].
    GREEN after D1 fix: main() builds all clients via _client() -> resource_calls == [];
    DDB/IDP clients raise GuardViolation on write ops.
    """
    import boto3 as _bt3

    resource_calls: list = []
    clients_built: dict[str, list] = {}

    # Record boto3.resource calls (should never happen after fix)
    monkeypatch.setattr(_bt3, "resource", lambda svc, **kw: resource_calls.append(svc) or MagicMock())

    # Wrap boto3.client to record clients; redirect to unreachable endpoint
    real_make_client = _bt3.client

    def spy_client(service, **kw):
        kw2 = {k: v for k, v in kw.items() if k != "endpoint_url"}
        c = real_make_client(service, endpoint_url="http://127.0.0.1:1", **kw2)
        clients_built.setdefault(service, []).append(c)
        return c

    monkeypatch.setattr(_bt3, "client", spy_client)

    # Patch _client so STS returns a working mock (avoids network); DDB/IDP get real guarded clients.
    original_client_fn = _mod._client

    def patching_client(service, region, *, allowed_ops, endpoint_url=None):
        c = original_client_fn(service, region, allowed_ops=allowed_ops, endpoint_url="http://127.0.0.1:1")
        if service == "sts":
            c.get_caller_identity = lambda: {"Arn": "arn:aws:iam::123:role/t", "Account": "123", "UserId": "u"}
        return c

    monkeypatch.setattr(_mod, "_client", patching_client)

    # Stub residue checks so no real DDB/Cognito I/O happens
    monkeypatch.setattr(_mod, "_check_residue_findings", lambda *a, **kw: (True, "ok"))
    monkeypatch.setattr(_mod, "_check_residue_actions", lambda *a, **kw: (True, "ok"))
    monkeypatch.setattr(_mod, "_check_residue_cognito", lambda *a, **kw: (True, "ok"))

    # Stub all 7 items to skip (source: _ITEM_FNS dict in the module)
    monkeypatch.setattr(
        _mod,
        "_ITEM_FNS",
        {n: (lambda _n: lambda res, **kw: res.skip(f"item-{_n}", "stubbed"))(n) for n in range(1, 8)},
    )

    with patch.object(sys, "argv", ["prog", "--all"]):
        with patch.dict(os.environ, {"LIVE_TESTS": "1"}):
            main()

    # BG1: boto3.resource must never be called
    assert resource_calls == [], f"boto3.resource was called with services: {resource_calls}"

    # BG2: the DDB client built in main() must have the guard (PutItem is not allowed)
    ddb_clients = clients_built.get("dynamodb", [])
    assert ddb_clients, "expected at least one dynamodb client to be built in main()"
    with pytest.raises(GuardViolation):
        ddb_clients[-1].put_item(TableName="test", Item={"pk": {"S": "x"}})

    # BG3: the Cognito IDP client built in main() must have the guard (CreateUserPool not allowed)
    idp_clients = clients_built.get("cognito-idp", [])
    assert idp_clients, "expected at least one cognito-idp client to be built in main()"
    with pytest.raises(GuardViolation):
        idp_clients[-1].create_user_pool(PoolName="test")



# ===========================================================================
# RES (residue checks): D2 — findings residue must scan for LIVETEST-<run>
# marker; Cognito residue must resolve pool by name (not accept pool_id=None).
# ===========================================================================

# ── helper: build a fake low-level DDB client from a list of pages ──────────
# Source shape: ddb.scan() returns {"Items": [...], "LastEvaluatedKey": {...}}
# Each Item is in DDB wire format: {"finding_id": {"S": "..."}, ...}

def _fake_ddb_scan(*pages):
    """Return a fake low-level DDB client whose scan() yields the given pages in order.

    pages: each is a list of raw DDB items (wire format dicts).
    The last page has no LastEvaluatedKey; earlier pages have {"page": "N"}.
    """
    page_list = list(pages)
    call_count = [0]

    class FakeDDB:
        def scan(self, **kw):
            idx = call_count[0]
            call_count[0] += 1
            items = page_list[idx] if idx < len(page_list) else []
            resp: dict = {"Items": items, "Count": len(items)}
            if idx + 1 < len(page_list):
                resp["LastEvaluatedKey"] = {"page": {"N": str(idx + 1)}}
            return resp

        def get_call_count(self):
            return call_count[0]

    return FakeDDB()


def _make_finding_item(finding_id: str, subject_key: str = "k", disclosure_txt: str = "d") -> dict:
    """Wire-format DDB item for a finding. Source shape: vsa-{stage}-avx-findings."""
    return {
        "finding_id": {"S": finding_id},
        "subject_key": {"S": subject_key},
        "disclosure_txt": {"S": disclosure_txt},
    }


def _find_fn():
    """Return the (possibly renamed) _check_residue_findings from the module."""
    return _mod._check_residue_findings


def _resolve_fn():
    """Return _resolve_cms_pool_id helper (added in D2 fix)."""
    fn = getattr(_mod, "_resolve_cms_pool_id", None)
    if fn is None:
        pytest.skip("_resolve_cms_pool_id not yet implemented (D2 not fixed)")
    return fn


# ── RES1: planted LIVETEST row FAILs ────────────────────────────────────────

def test_res1_planted_livetest_row_fails():
    """RES1: a finding whose finding_id contains LIVETEST-r1 causes FAIL.

    RED before D2: _check_residue_findings(ddb, table, [], []) ignores the item.
    GREEN after D2: full Scan finds the LIVETEST row -> FAIL.
    """
    run = "r1"
    item = _make_finding_item(f"LIVETEST-{run}-abc", f"LIVETEST-{run}-sk")
    ddb = _fake_ddb_scan([item])  # one page with the planted item
    ok, detail = _find_fn()(ddb, "findings-table", run)
    assert not ok, f"expected FAIL with a planted LIVETEST item; got ok=True detail={detail!r}"
    assert "LIVETEST" in detail or "1" in detail, f"detail should name the hit: {detail!r}"


# ── RES2: clean table PASSes ─────────────────────────────────────────────────

def test_res2_clean_table_passes():
    """RES2: an empty table PASSes the residue check."""
    ddb = _fake_ddb_scan([])  # empty page
    ok, detail = _find_fn()(ddb, "findings-table", "r2")
    assert ok, f"expected PASS on empty table; got ok=False detail={detail!r}"


# ── RES3: scan error FAILs closed ────────────────────────────────────────────

def test_res3_scan_error_fails_closed():
    """RES3: a DDB error during the scan returns FAIL (fail-closed)."""

    class ErrorDDB:
        def scan(self, **kw):
            raise RuntimeError("DDB unavailable")

    ok, detail = _find_fn()(ErrorDDB(), "findings-table", "r3")
    assert not ok, f"expected FAIL on scan error; got ok=True detail={detail!r}"
    assert "error" in detail.lower() or "DDB" in detail or "unavailable" in detail


# ── RES4: two scan pages are both read ──────────────────────────────────────

def test_res4_two_pages_both_read():
    """RES4: the function follows LastEvaluatedKey and reads both pages."""
    run = "r4"
    # Page 1: one non-LIVETEST item; page 2: one LIVETEST item
    page1 = [_make_finding_item("unrelated-item")]
    page2 = [_make_finding_item(f"LIVETEST-{run}-xyz")]
    ddb = _fake_ddb_scan(page1, page2)
    ok, _detail = _find_fn()(ddb, "findings-table", run)
    assert ddb.get_call_count() == 2, "must read both pages (pagination)"
    assert not ok, "second page contains a LIVETEST item — must FAIL"


# ── COG1-COG3: Cognito residue must resolve pool by name ────────────────────

def _fake_idp_list_pools(*pool_pages):
    """Fake Cognito IDP client for ListUserPools pagination tests.

    pool_pages: each is a list of {"Id": "...", "Name": "..."} dicts.
    """
    page_list = list(pool_pages)
    call_count = [0]

    class FakeIDP:
        def list_user_pools(self, **kw):
            idx = call_count[0]
            call_count[0] += 1
            pools = page_list[idx] if idx < len(page_list) else []
            resp: dict = {"UserPools": pools}
            if idx + 1 < len(page_list):
                resp["NextToken"] = f"tok{idx + 1}"
            return resp

        def get_call_count(self):
            return call_count[0]

    return FakeIDP()


def test_cog1_resolves_pool_by_name_passes():
    """COG1: a clean Cognito pool (no LIVETEST users) PASSes after name resolution.

    RED before D2: _check_residue_cognito(..., pool_id=None, ...) returns (True, 'skipped').
    GREEN after D2: _resolve_cms_pool_id finds the pool; list_users returns no hits -> PASS.
    """
    stage = "staging"
    pool_name = f"cms-{stage}-ui-users"

    idp_lp = _fake_idp_list_pools([{"Id": "us-west-2_ABCDE", "Name": pool_name}])
    resolved = _resolve_fn()(idp_lp, stage)
    assert resolved == "us-west-2_ABCDE", f"expected pool id, got {resolved!r}"


def test_cog2_unresolved_pool_fails():
    """COG2: if ListUserPools returns no matching pool, the check is a FAIL."""
    stage = "staging"
    # No pool with the expected name
    idp_lp = _fake_idp_list_pools([{"Id": "us-west-2_OTHER", "Name": "other-pool"}])
    resolved = _resolve_fn()(idp_lp, stage)
    assert resolved is None, f"expected None for unresolved pool, got {resolved!r}"


def test_cog3_resolve_pages_two_calls():
    """COG3: _resolve_cms_pool_id follows NextToken to find a pool on page 2."""
    stage = "staging"
    pool_name = f"cms-{stage}-ui-users"

    page1 = [{"Id": "us-west-2_OTHER", "Name": "other-pool"}]
    page2 = [{"Id": "us-west-2_TARGET", "Name": pool_name}]
    idp_lp = _fake_idp_list_pools(page1, page2)

    resolved = _resolve_fn()(idp_lp, stage)
    assert idp_lp.get_call_count() == 2, "must page through both pages"
    assert resolved == "us-west-2_TARGET", f"expected pool id from page 2, got {resolved!r}"



# ===========================================================================
# CTX (context shape): D3 — ctx passed to items must not contain 'ddb' or 'idp';
# must contain run, stage, region, manifest, teardown, args.
# ===========================================================================


def test_d3_ctx_has_no_ddb_or_idp_keys(tmp_path, monkeypatch):
    """CTX1/CTX2: ctx received by items must not contain 'ddb' or 'idp'.

    RED before D3: ctx["ddb"] and ctx["idp"] exist (items inherit residue clients).
    GREEN after D3: ctx carries only run/stage/region/manifest/teardown/args.
    """
    import boto3 as _bt3

    captured_ctx: dict = {}

    # Stub item-1 to capture the ctx it receives
    def capturing_item(res, **kw):
        captured_ctx.update(kw)
        res.skip("item-1", "captured")

    monkeypatch.setattr(_mod, "_ITEM_FNS", {
        1: capturing_item,
        **{n: (lambda _n: lambda res, **kw: res.skip(f"item-{_n}", "stubbed"))(n)
           for n in range(2, 8)},
    })

    # Stub STS and block real AWS calls
    original_client_fn = _mod._client

    def patching_client(service, region, *, allowed_ops, endpoint_url=None):
        c = original_client_fn(service, region, allowed_ops=allowed_ops, endpoint_url="http://127.0.0.1:1")
        if service == "sts":
            c.get_caller_identity = lambda: {"Arn": "arn:aws:iam::123:role/t", "Account": "123", "UserId": "u"}
        return c

    monkeypatch.setattr(_mod, "_client", patching_client)

    # Stub residue checks
    monkeypatch.setattr(_mod, "_check_residue_findings", lambda *a, **kw: (True, "ok"))
    monkeypatch.setattr(_mod, "_check_residue_actions", lambda *a, **kw: (True, "ok"))
    monkeypatch.setattr(_mod, "_check_residue_cognito", lambda *a, **kw: (True, "ok"))
    monkeypatch.setattr(_mod, "_resolve_cms_pool_id", lambda *a, **kw: "pool-id")

    with patch.object(sys, "argv", ["prog", "--item", "1"]):
        with patch.dict(os.environ, {"LIVE_TESTS": "1"}):
            main()

    assert captured_ctx, "item-1 should have been called and captured ctx"

    # CTX1: no clients in ctx
    assert "ddb" not in captured_ctx, f"ctx must not carry 'ddb'; keys={list(captured_ctx)}"
    assert "idp" not in captured_ctx, f"ctx must not carry 'idp'; keys={list(captured_ctx)}"

    # CTX2: required keys are present
    for key in ("run", "stage", "region", "manifest", "teardown", "args"):
        assert key in captured_ctx, f"ctx must carry {key!r}; keys={list(captured_ctx)}"



# ===========================================================================
# SIGHUP subprocess test (w-sighup mutant: removing `_sig.signal(_sig.SIGHUP, _exit_once)`)
# ===========================================================================


def test_subprocess_sighup_exits_143(tmp_path: Path) -> None:
    """Subprocess receiving SIGHUP exits with code 143.

    Catches w-sighup mutant: removing `_sig.signal(_sig.SIGHUP, _exit_once)`.
    Without that registration, SIGHUP delivers the OS default action (terminate
    the process) and the exit code is NOT 143 (it would be -signal.SIGHUP).
    The script installs _exit_once for SIGHUP which raises SystemExit(143).

    Source: _install_signal_handlers (live-verify-fi-fleet-health.py:345-357).
    """
    # Script snippet: install signal handlers then sleep so we can send SIGHUP.
    snippet = (
        "import importlib.util, sys, time\n"
        f"spec = importlib.util.spec_from_file_location('_lv', r'{_SCRIPT}')\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "mod._install_signal_handlers()\n"
        "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
        "time.sleep(30)\n"
    )
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["AWS_ACCESS_KEY_ID"] = "testing"
    env["AWS_SECRET_ACCESS_KEY"] = "testing"
    env.pop("AWS_PROFILE", None)

    proc = subprocess.Popen(
        [sys.executable, "-c", snippet],
        cwd=str(tmp_path),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Wait until the process signals readiness.
    ready_line = proc.stdout.readline()
    assert ready_line.strip() == b"ready", f"process did not signal ready: {ready_line!r}"

    proc.send_signal(signal.SIGHUP)
    proc.wait(timeout=10)
    assert proc.returncode == 143, (
        f"expected exit 143 after SIGHUP, got {proc.returncode}; "
        f"stderr={proc.stderr.read()[:200]!r}"
    )


# ---------------------------------------------------------------------------
# C1: item 5 ctrl plant must be visible to the residue scan
# The plant id is LIVETEST-{run}-FI-FH-5-ctrl (marker prefix).
# RED before fix: old id LIVETEST-FI-FH-{run}-5-ctrl has LIVETEST-FI-FH-r5c
# as the string but NOT "LIVETEST-r5c" as a substring, so the scan misses it
# and residue-findings-livetest returns PASS despite a left-over.
# GREEN after fix: id starts with LIVETEST-{run}- so the scan finds it.
# ---------------------------------------------------------------------------

def test_c1_ctrl_plant_visible_to_residue_scan() -> None:
    """RES-C1: a left-over item-5 ctrl plant causes residue FAIL.

    RED before fix: _item_5 plants LIVETEST-FI-FH-{run}-5-ctrl; the
    residue scan looks for contains(finding_id, 'LIVETEST-{run}') which is
    false for that format, so a left-over plant is invisible and the residue
    check returns PASS (ok=True) instead of FAIL.

    GREEN after fix: the plant is renamed to LIVETEST-{run}-FI-FH-5-ctrl so
    the marker IS a substring and the scan returns FAIL (ok=False).
    """
    run = "r5c"
    # New format used by _item_5 after the fix: marker IS a substring (prefix).
    # Before the fix the plant was LIVETEST-FI-FH-{run}-5-ctrl where marker
    # LIVETEST-{run} was NOT a substring, so the scan missed it (ok=True).
    ctrl_id = f"LIVETEST-{run}-FI-FH-5-ctrl"   # new format (post-fix)
    item = _make_finding_item(ctrl_id)
    ddb = _fake_ddb_scan([item])
    ok, detail = _find_fn()(ddb, "findings-table", run)
    assert not ok, (
        f"expected FAIL: left-over ctrl plant {ctrl_id!r} must be caught by the "
        f"residue scan; got ok=True detail={detail!r}"
    )
    assert ctrl_id in detail or "LIVETEST" in detail, (
        f"detail should name the left-over id; detail={detail!r}"
    )


def test_update_manifest_writes_ddb_decimals(tmp_path) -> None:
    """Run records read from DynamoDB carry Decimal; the manifest must take them (item 6
    records one). Kills cycle 2 mutant manifest-decimal-revert."""
    import json as _json
    from decimal import Decimal

    m = tmp_path / "manifest.json"
    m.write_text(_json.dumps({"run_id": "r", "items": {}}))
    try:
        _mod._update_manifest(m, "item6_run", {"tokens_used": Decimal("0"), "cost": Decimal("1.5")})
    except Exception as exc:  # the write must succeed: a raise is this test's failure
        raise AssertionError(f"_update_manifest refused Decimal: {type(exc).__name__}: {exc}") from exc
    assert _json.loads(m.read_text())["items"]["item6_run"] == {"tokens_used": 0, "cost": 1.5}


# ===========================================================================
# Security review c1 W1: --run-id is refused before any AWS call unless it has the
# safe id shape (it becomes DynamoDB keys, URL path segments, Cognito usernames,
# residue filters and the /tmp run directory).
# ===========================================================================

class _ReachedAws(Exception):
    pass


def _run_main_with_run_id(run_id: str) -> tuple[Any, MagicMock, str]:
    import io  # noqa: PLC0415
    mock_boto3 = MagicMock()
    mock_boto3.client.side_effect = _ReachedAws("boto3.client reached")
    err = io.StringIO()
    argv = ["prog", "--stage", "staging", "--item", "1", "--run-id", run_id]
    out: Any
    with patch.object(sys, "argv", argv), \
         patch.dict(os.environ, {"LIVE_TESTS": "1"}, clear=False), \
         patch.dict(sys.modules, {"boto3": mock_boto3}), \
         patch.object(sys, "stderr", err), \
         patch.object(_mod, "_in_write_window", return_value=False):
        try:
            out = main()
        except _ReachedAws as exc:
            out = exc
    return out, mock_boto3, err.getvalue()


@pytest.mark.parametrize("bad", ["a/b", "../etc/passwd", "x" * 65, "run id", "run;id", ""])
def test_unsafe_run_id_refused_before_any_aws_call(bad: str) -> None:
    out, mock_boto3, err = _run_main_with_run_id(bad)
    assert out == 1, f"--run-id {bad!r}: expected exit 1, got {out!r}"
    mock_boto3.client.assert_not_called()
    assert "--run-id must match" in err, err


def test_safe_run_id_passes_the_gate() -> None:
    """Positive control: a safe run id gets past every pre-AWS gate (reaches boto3)."""
    out, mock_boto3, err = _run_main_with_run_id("g6lv-20261021T001500-ab12cd34")
    assert isinstance(out, _ReachedAws), f"expected to reach boto3; got {out!r}; stderr={err!r}"
    assert "--run-id must match" not in err
    assert _mod._SAFE_ID_RE.match(_mod._make_run_id()), "the generated run id must pass its own gate"


# ===========================================================================
# Security review c1 (mutants that survived): the two highest-impact gates pinned.
# ===========================================================================

def _run_main_argv(extra: list[str], *, in_window: bool) -> tuple[Any, MagicMock, str]:
    import io  # noqa: PLC0415
    mock_boto3 = MagicMock()
    mock_boto3.client.side_effect = _ReachedAws("boto3.client reached")
    err = io.StringIO()
    out: Any
    with patch.object(sys, "argv", ["prog", "--stage", "staging", *extra]), \
         patch.dict(os.environ, {"LIVE_TESTS": "1"}, clear=False), \
         patch.dict(sys.modules, {"boto3": mock_boto3}), \
         patch.object(sys, "stderr", err), \
         patch.object(_mod, "_in_write_window", return_value=in_window):
        try:
            out = main()
        except _ReachedAws as exc:
            out = exc
    return out, mock_boto3, err.getvalue()


@pytest.mark.parametrize("extra", [
    ["--item", "5", "--allow-writes"],
    ["--item", "3", "--allow-writes", "--allow-managed-writes"],
    ["--item", "7", "--allow-writes", "--allow-paging", "--item7-phase", "force"],
    ["--all", "--allow-writes", "--allow-managed-writes", "--allow-paging"],
])
def test_writes_refused_inside_the_scheduled_job_window(extra: list[str]) -> None:
    """Gate 3: any write item inside 04:50-06:10 UTC exits 1 before any AWS call."""
    out, mock_boto3, err = _run_main_argv(extra, in_window=True)
    assert out == 1, f"{extra}: expected exit 1 inside the window, got {out!r}"
    mock_boto3.client.assert_not_called()
    assert "Refusing to start writes inside 04:50-06:10 UTC" in err, err


@pytest.mark.parametrize("extra", [
    ["--item", "1"],
    ["--item", "7", "--allow-paging", "--item7-phase", "verify", "--since", "2026-10-20T00:00:00Z"],
])
def test_read_only_items_are_not_refused_inside_the_window(extra: list[str]) -> None:
    """Positive control: read-only items (and item 7's verify phase) get past gate 3."""
    out, _b, err = _run_main_argv(extra, in_window=True)
    assert isinstance(out, _ReachedAws), f"{extra}: expected to reach boto3; got {out!r}; {err!r}"


def test_write_items_pass_the_window_gate_outside_it() -> None:
    out, _b, err = _run_main_argv(["--item", "5", "--allow-writes"], in_window=False)
    assert isinstance(out, _ReachedAws), f"expected to reach boto3; got {out!r}; {err!r}"


def test_item3_runs_only_with_allow_managed_writes() -> None:
    """Item 3 books real managed-fleet ROs: --allow-writes alone must skip it."""
    every = frozenset({1, 2, 3, 4, 5, 6, 7})
    to_run, to_skip = _mod._compute_skips(every, allow_writes=True,
                                          allow_managed_writes=False, allow_paging=True)
    assert 3 not in to_run, to_run
    assert to_skip.get(3) == "item 3 requires --allow-managed-writes", to_skip
    assert {5, 7} <= to_run, to_run
    to_run2, to_skip2 = _mod._compute_skips(every, allow_writes=True,
                                            allow_managed_writes=True, allow_paging=True)
    assert 3 in to_run2 and 3 not in to_skip2, (to_run2, to_skip2)
    to_run3, _ = _mod._compute_skips(every, allow_writes=False,
                                     allow_managed_writes=True, allow_paging=True)
    assert 3 not in to_run3, to_run3
