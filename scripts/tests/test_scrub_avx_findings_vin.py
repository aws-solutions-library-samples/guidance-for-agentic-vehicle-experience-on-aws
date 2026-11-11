"""Tests for ``scripts/scrub_avx_findings_vin.py`` (ADP spec 2026-09-30, task 2.7 / 3.2c).

No test can reach AWS: an autouse fixture sets dummy credentials, empty config files and a
dead endpoint, and points every default ``/tmp`` path at ``tmp_path``. Flows run through
real botocore against ``_fake_dynamodb.FakeDynamoDB`` on 127.0.0.1 (one item evaluated
per page, so every reader must follow ``LastEvaluatedKey``). The read-only guard is proven
three ways: in-process on the session, through ``main()``, and in a separate process
running the CLI.

Old VINs are built from pieces so the retired shapes never appear literally (2.7 Verify 1).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import boto3  # type: ignore[import-untyped]
import pytest
from botocore.stub import Stubber  # type: ignore[import-untyped]

_CVX_ROOT = Path(__file__).resolve().parents[2]
if str(_CVX_ROOT) not in sys.path:
    sys.path.insert(0, str(_CVX_ROOT))

import scripts.scrub_avx_findings_vin as m  # noqa: E402
from agents.supervisor.lambdas import avx_broker  # noqa: E402
from agents.tier2.writer import compute_finding_key  # noqa: E402
from scripts.readonly_guard import ReadOnlyViolation  # noqa: E402
from scripts.tests._fake_dynamodb import FakeDynamoDB  # noqa: E402

_SCRIPT = _CVX_ROOT / "scripts" / "scrub_avx_findings_vin.py"

# ── Synthetic VINs, via the re-VIN rule ───────────────────────────────────────

_CSP, _DEM, _5YJ = "CS" + "PR", "DE" + "MO", "5YJ" + "3E1EA1PF"
OLD_A, NEW_A = _CSP + f"{31:013d}", "MRDN0000001000031"
OLD_B, NEW_B = _DEM + f"{13:013d}", "MRDN0000002000013"
OLD_C, NEW_C = _5YJ + "000004", "MRDN0000003000004"
VID_A, VID_B, VID_C = "VEH-TEST-001", "VEH-TEST-002", OLD_C  # 5YJ: vehicleId == old VIN
OTHER_VIN = "MRDN0000000000077"  # an unrelated vehicle that must never change

FINDINGS = "vsa-staging-avx-findings"
TRIAGE = "vsa-staging-triage-decisions"
ACTIONS = "vsa-staging-avx-actions"
APP_USER = "0f8fad5b-d9cb-469f-a165-70867728950e"  # Cognito-sub shaped owner
AGENT = "agent-2-vehicle-health"
FUTURE, PAST = "2099-01-01T00:00:00Z", "2000-01-01T00:00:00Z"


def S(v: str) -> dict[str, str]:
    return {"S": v}


def key_for(vin: str, kind: str = "health.battery", agent: str = AGENT) -> str:
    return compute_finding_key(agent_id=agent, scope_field="vin", scope_value=vin,
                               finding_kind=kind)


def finding(fid: str, vin: str, *, kind: str = "health.battery", expires: str = FUTURE,
            agent: str = AGENT) -> dict[str, Any]:
    return {
        "finding_id": S(fid), "vin": S(vin), "agent_id": S(agent), "finding_kind": S(kind),
        "scope": {"M": {"vin": S(vin)}}, "finding_key": S(key_for(vin, kind, agent)),
        "expires_at": S(expires), "headline": S("unchanged"),
    }


def fleet_finding(fid: str) -> dict[str, Any]:
    # Fleet-scoped: no top-level vin, so absent from by_vin (exactly-one-scope rule).
    return {"finding_id": S(fid), "agent_id": S(AGENT), "finding_kind": S("fleet.x"),
            "scope": {"M": {"fleet_id": S("FLEET-MERIDIAN-ONBOARD")}},
            "finding_key": S("f" * 64), "fleet_id": S("FLEET-MERIDIAN-ONBOARD"),
            "expires_at": S(FUTURE)}


def triage(sid: str, vin: str, ttl: int | None = None) -> dict[str, Any]:
    row = {"sessionId": S(sid), "vin": S(vin), "decision": S("unchanged")}
    if ttl is not None:
        row["ttl"] = {"N": str(ttl)}
    return row


def tables(findings: list[dict[str, Any]] = (), triage_rows: list[dict[str, Any]] = ()):  # type: ignore[assignment]
    return {
        FINDINGS: {"key": ["finding_id"],
                   "indexes": {"by_vin": "vin", "by_finding_key": "finding_key"},
                   "items": [fleet_finding("FLEET-1"), finding("OTHER-1", OTHER_VIN),
                             *findings, fleet_finding("FLEET-2")]},
        TRIAGE: {"key": ["sessionId"], "indexes": {},
                 "items": [triage("OTHER-SES", OTHER_VIN), *triage_rows]},
        ACTIONS: {"key": ["action_id"], "indexes": {}, "items": []},
    }


def standard_tables() -> dict[str, Any]:
    return tables(
        findings=[finding("F-A1", OLD_A), finding("F-A2", OLD_A, kind="health.brakes"),
                  finding("F-B1", OLD_B, kind="health.diagnostics"),
                  finding("F-A-EXPIRED", OLD_A, expires=PAST, kind="health.tires")],
        triage_rows=[triage("SES-A1", OLD_A), triage("SES-B1", OLD_B), triage("SES-B2", OLD_B)],
    )


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _no_aws(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """No test may reach AWS or write the operator's /tmp files, even if a check regresses."""
    for var in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_ENDPOINT_URL_DYNAMODB",
                "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS", "AWS_REGION", "AWS_DEFAULT_REGION"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-aws-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-aws-credentials"))
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_MAX_ATTEMPTS", "1")
    default_dir = tmp_path / "default-baseline-dir"
    default_dir.mkdir()
    monkeypatch.setattr(m, "DEFAULT_BASELINE_DIR", str(default_dir))
    monkeypatch.setattr(m, "POST_COUNT_DELAY_S", 0)
    monkeypatch.setattr(boto3, "DEFAULT_SESSION", None)  # restored after each test


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch):
    started: list[FakeDynamoDB] = []

    def start(tbls: dict[str, Any], **kwargs: Any) -> FakeDynamoDB:
        srv = FakeDynamoDB(tbls, **kwargs)
        monkeypatch.setenv("AWS_ENDPOINT_URL", srv.start())
        started.append(srv)
        return srv

    yield start
    for srv in started:
        srv.stop()


def write_tsv(path: Path, rows: list[tuple[str, str, str]]) -> Path:
    # Exactly the CMS writer's format: header-less, three columns, "\n" endings.
    path.write_text("".join(f"{a}\t{b}\t{c}\n" for a, b, c in rows))
    return path


ROWS = [(VID_A, OLD_A, NEW_A), (VID_B, OLD_B, NEW_B), (VID_C, OLD_C, NEW_C)]


def run_main(tmp_path: Path, *extra: str, rows=ROWS) -> int:
    tsv = write_tsv(tmp_path / "map.tsv", rows)
    return m.main(["--tsv-file", str(tsv), "--baseline-dir", str(tmp_path),
                   "--log-level", "WARNING", *extra])


def apply_args(rows=ROWS) -> list[str]:
    return ["--apply", "--expected-rows", str(len(rows))]


def item(srv: FakeDynamoDB, table: str, key_attr: str, key: str) -> dict[str, Any]:
    return next(i for i in srv.items(table) if i[key_attr]["S"] == key)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


# ── TSV ───────────────────────────────────────────────────────────────────────

class TestTsv:
    def test_reads_the_cms_writer_format(self, tmp_path):
        path = write_tsv(tmp_path / "map.tsv", ROWS)
        rows = m.load_tsv(str(path))
        assert rows == [m.VinRow(*r) for r in ROWS]
        m.validate_rows(rows, expected_rows=3)  # includes the vehicleId == oldVin row

    def test_blank_and_comment_lines_skipped(self, tmp_path):
        path = tmp_path / "map.tsv"
        path.write_text(f"# note\n\n{VID_A}\t{OLD_A}\t{NEW_A}\n\n")
        assert len(m.load_tsv(str(path))) == 1

    def test_wrong_column_count_refused(self, tmp_path):
        path = tmp_path / "map.tsv"
        path.write_text(f"{VID_A}\t{OLD_A}\n")
        with pytest.raises(m.InputError, match="3 tab-separated"):
            m.load_tsv(str(path))

    @pytest.mark.parametrize("rows, expected, match", [
        ([], None, "no data rows"),
        ([(VID_A, OLD_A, NEW_A)], 93, "expected 93"),
        ([(VID_A, OLD_A, NEW_A), (VID_A, OLD_B, NEW_B)], None, "duplicate vehicleId"),
        ([(VID_A, OLD_A, NEW_A), (VID_B, OLD_A, NEW_B)], None, "duplicate oldVin"),
        ([(VID_A, OLD_A, NEW_A), (VID_B, OLD_B, NEW_A)], None, "duplicate newVin"),
        ([("vehicleId", "oldVin", "newVin")], None, "MRDN"),
        ([(VID_A, OLD_A, "MRDN000000100003")], None, "MRDN"),
        ([(VID_A, OTHER_VIN, NEW_A)], None, "retired demo VIN shape"),
        ([(VID_A, OLD_A, "MRDN0000001000032")], None, "differs from the re-VIN map"),
    ])
    def test_refusals(self, rows, expected, match):
        with pytest.raises(m.InputError, match=match):
            m.validate_rows([m.VinRow(*r) for r in rows], expected_rows=expected)

    def test_rule_matches_the_cms_map(self):
        # Values from CMS revin_map.new_vin (decisions.md decision 1).
        assert m.expected_new_vin(OLD_A) == NEW_A
        assert m.expected_new_vin(OLD_B) == NEW_B
        assert m.expected_new_vin(OLD_C) == NEW_C
        assert m.expected_new_vin(OTHER_VIN) is None
        assert m.expected_new_vin(_CSP + f"{0:013d}") is None


# ── Read-only guard on the session ────────────────────────────────────────────

_WRITE = dict(TableName=FINDINGS, Key={"finding_id": S("x")},
              UpdateExpression="SET a = :a", ExpressionAttributeValues={":a": S("b")})


class TestSessionGuard:
    def test_dry_run_session_refuses_a_queued_write(self):
        client = m.make_session("us-west-2", apply=False).client("dynamodb")
        with Stubber(client) as stub:
            # The write is queued, so only the guard can stop it.
            stub.add_response("update_item", {})
            with pytest.raises(ReadOnlyViolation, match="UpdateItem"):
                client.update_item(**_WRITE)
            with pytest.raises(AssertionError):
                stub.assert_no_pending_responses()  # the stubbed write never ran

    def test_dry_run_session_allows_reads(self):
        client = m.make_session("us-west-2", apply=False).client("dynamodb")
        with Stubber(client) as stub:
            stub.add_response("query", {"Count": 2, "Items": []})
            assert client.query(TableName=FINDINGS, IndexName="by_vin",
                                KeyConditionExpression="vin = :v",
                                ExpressionAttributeValues={":v": S(NEW_A)})["Count"] == 2

    def test_guard_covers_a_later_default_session_client(self):
        m.make_session("us-west-2", apply=False)
        client = boto3.client("dynamodb")
        with Stubber(client) as stub:
            stub.add_response("update_item", {})
            with pytest.raises(ReadOnlyViolation):
                client.update_item(**_WRITE)

    def test_apply_session_is_not_guarded(self):
        client = m.make_session("us-west-2", apply=True).client("dynamodb")
        with Stubber(client) as stub:
            stub.add_response("update_item", {})
            client.update_item(**_WRITE)
            stub.assert_no_pending_responses()


# ── Dry run ───────────────────────────────────────────────────────────────────

class TestDryRun:
    def test_default_is_a_dry_run_that_writes_nothing(self, tmp_path, fake):
        srv = fake(standard_tables())
        before = json.dumps(srv.tables, sort_keys=True)
        assert run_main(tmp_path) == m.EXIT_OK
        assert srv.writes() == []
        assert json.dumps(srv.tables, sort_keys=True) == before
        assert {"Query", "Scan"} <= set(srv.operations())

    def test_dry_run_writes_only_dryrun_files(self, tmp_path, fake):
        fake(standard_tables())
        assert run_main(tmp_path) == m.EXIT_OK
        assert not list(tmp_path.glob("revin_prebaseline_*.json"))
        f = read_json(tmp_path / "revin_dryrun_avx_findings.json")
        t = read_json(tmp_path / "revin_dryrun_triage_decisions.json")
        assert f["applied"] is False and f["mode"] == "dry-run"
        assert f["vehicles"][VID_A]["pre_old"] == 3  # F-A1, F-A2, F-A-EXPIRED
        assert f["vehicles"][VID_A]["would_update"] == 3
        assert f["vehicles"][VID_A]["updated"] == 0
        assert f["vehicles"][VID_B]["pre_old"] == 1
        assert t["vehicles"][VID_B]["pre_old"] == 2

    def test_dry_run_leaves_an_existing_prebaseline_alone(self, tmp_path, fake):
        fake(standard_tables())
        sentinel = tmp_path / "revin_prebaseline_avx_findings.json"
        sentinel.write_text('{"applied": true, "sentinel": 1}')
        assert run_main(tmp_path) == m.EXIT_OK
        assert sentinel.read_text() == '{"applied": true, "sentinel": 1}'

    def test_main_guards_its_client_when_a_write_is_forced(self, tmp_path, fake, monkeypatch):
        """A code path that ignores --apply is stopped by the session guard (exit 4)."""
        srv = fake(standard_tables())
        real = m.scrub_findings
        monkeypatch.setattr(m, "scrub_findings",
                            lambda ddb, table, plan, dry_run: real(ddb, table, plan, False))
        assert run_main(tmp_path) == m.EXIT_READONLY
        assert srv.writes() == []

    def test_positive_control_the_forced_write_does_reach_the_fake_with_apply(
            self, tmp_path, fake, monkeypatch):
        srv = fake(standard_tables())
        real = m.scrub_findings
        monkeypatch.setattr(m, "scrub_findings",
                            lambda ddb, table, plan, dry_run: real(ddb, table, plan, False))
        assert run_main(tmp_path, *apply_args()) == m.EXIT_OK
        assert any(op == "UpdateItem" for op, _ in srv.writes())


# ── Apply ─────────────────────────────────────────────────────────────────────

class TestApply:
    def test_moves_findings_and_triage(self, tmp_path, fake):
        srv = fake(standard_tables())
        assert run_main(tmp_path, *apply_args()) == m.EXIT_OK
        a1 = item(srv, FINDINGS, "finding_id", "F-A1")
        assert a1["vin"] == S(NEW_A) and a1["scope"]["M"]["vin"] == S(NEW_A)
        assert a1["finding_key"] == S(key_for(NEW_A))  # writer's own function, new VIN
        assert a1["headline"] == S("unchanged")
        b1 = item(srv, FINDINGS, "finding_id", "F-B1")
        assert b1["finding_key"] == S(key_for(NEW_B, "health.diagnostics"))
        assert item(srv, FINDINGS, "finding_id", "F-A-EXPIRED")["vin"] == S(NEW_A)
        assert item(srv, FINDINGS, "finding_id", "OTHER-1")["vin"] == S(OTHER_VIN)
        assert item(srv, TRIAGE, "sessionId", "SES-B2")["vin"] == S(NEW_B)
        assert item(srv, TRIAGE, "sessionId", "OTHER-SES")["vin"] == S(OTHER_VIN)
        assert {op for op, _ in srv.writes()} == {"UpdateItem"}

    def test_writes_the_original_baseline_and_postcount(self, tmp_path, fake):
        fake(standard_tables())
        assert run_main(tmp_path, *apply_args()) == m.EXIT_OK
        pre = read_json(tmp_path / "revin_prebaseline_avx_findings.json")
        assert pre["applied"] is True and pre["surface"] == "avx_findings"
        assert (pre["vehicles"][VID_A]["pre_old"], pre["vehicles"][VID_A]["pre_new"]) == (3, 0)
        tpre = read_json(tmp_path / "revin_prebaseline_triage_decisions.json")
        assert tpre["vehicles"][VID_B]["pre_old"] == 2
        post = read_json(tmp_path / "revin_postcount_avx_findings.json")
        assert post["invariant_ok"] is True
        assert (post["vehicles"][VID_A]["post_old"], post["vehicles"][VID_A]["post_new"]) == (0, 3)

    def test_count_files_are_owner_only(self, tmp_path, fake):
        fake(standard_tables())
        assert run_main(tmp_path) == m.EXIT_OK
        assert run_main(tmp_path, *apply_args()) == m.EXIT_OK
        files = list(tmp_path.glob("revin_*.json"))
        assert len(files) == 6  # dryrun, prebaseline, postcount for each surface
        assert all((f.stat().st_mode & 0o777) == 0o600 for f in files)
        assert not list(tmp_path.glob("*.tmp"))

    def test_apply_needs_expected_rows(self, tmp_path, fake):
        srv = fake(standard_tables())
        assert run_main(tmp_path, "--apply") == m.EXIT_INPUT
        assert srv.calls == []

    def test_prebaseline_is_on_disk_before_the_first_write(self, tmp_path, fake, monkeypatch):
        srv = fake(standard_tables())

        def boom(*_a, **_k):
            raise RuntimeError("simulated crash in the first write step")
        monkeypatch.setattr(m, "scrub_findings", boom)
        with pytest.raises(RuntimeError, match="simulated crash"):
            run_main(tmp_path, *apply_args())
        assert srv.writes() == []
        pre = read_json(tmp_path / "revin_prebaseline_avx_findings.json")
        assert pre["vehicles"][VID_A]["pre_old"] == 3

    def test_triage_rows_already_on_the_new_vin_are_counted(self, tmp_path, fake):
        """W2: post(new) must equal pre(old) + pre(new), including pre(new) > 0."""
        t = standard_tables()
        t[TRIAGE]["items"].append(triage("SES-A-NEW", NEW_A))
        srv = fake(t)
        assert run_main(tmp_path, *apply_args()) == m.EXIT_OK
        post = read_json(tmp_path / "revin_postcount_triage_decisions.json")
        v = post["vehicles"][VID_A]
        assert (v["pre_old"], v["pre_new"], v["post_old"], v["post_new"]) == (1, 1, 0, 2)
        assert item(srv, TRIAGE, "sessionId", "SES-A1")["vin"] == S(NEW_A)

    def test_every_scan_is_consistent_and_no_index_read_asks_for_it(self, tmp_path, fake):
        srv = fake(standard_tables())
        assert run_main(tmp_path, *apply_args()) == m.EXIT_OK
        scans = [b for op, b in srv.calls if op == "Scan"]
        assert scans and all(b.get("ConsistentRead") is True for b in scans)

    def test_a_vin_row_without_scope_vin_moves_only_the_top_level_vin(self, tmp_path, fake):
        t = standard_tables()
        odd = finding("F-ODD", OLD_B, kind="health.odd")
        odd["scope"] = {"M": {"fleet_id": S("FLEET-MERIDIAN-ONBOARD")}}
        odd_key = odd["finding_key"]
        t[FINDINGS]["items"].append(odd)
        srv = fake(t)
        assert run_main(tmp_path, *apply_args()) == m.EXIT_OK
        moved = item(srv, FINDINGS, "finding_id", "F-ODD")
        assert moved["vin"] == S(NEW_B)
        assert moved["finding_key"] == odd_key
        assert moved["scope"] == {"M": {"fleet_id": S("FLEET-MERIDIAN-ONBOARD")}}

    def test_unknown_service_error_exits_6(self, tmp_path, fake):
        t = standard_tables()
        del t[TRIAGE]  # the fake answers ResourceNotFoundException for an unknown table
        fake(t)
        assert run_main(tmp_path, *apply_args()) == m.EXIT_AWS


# ── Invariant ─────────────────────────────────────────────────────────────────

class TestInvariant:
    def c(self, **kw: int) -> m.SurfaceCounts:
        return m.SurfaceCounts(**kw)

    def test_passes(self):
        assert m.invariant_failures("t", {"V": self.c(pre_old=2, pre_new=1, post_new=3)}, None) == []

    @pytest.mark.parametrize("counts", [
        dict(pre_old=2, pre_new=0, post_old=1, post_new=2),   # old VIN still present
        dict(pre_old=2, pre_new=0, post_old=0, post_new=1),   # a row was lost
        dict(pre_old=1, pre_new=1, post_old=0, post_new=1),   # pre(new) row lost
        dict(pre_old=0, pre_new=0, post_old=1, post_new=0),   # nothing to move, old appears
    ])
    def test_fails(self, counts):
        assert m.invariant_failures("t", {"V": self.c(**counts)}, None)

    def test_original_baseline_loss_fails(self):
        counts = {"V": self.c(pre_old=0, pre_new=1, post_new=1)}  # consistent with this run
        assert m.invariant_failures("t", counts, {"V": {"pre_old": 2, "pre_new": 0}})

    def test_growth_since_original_is_not_a_failure(self):
        counts = {"V": self.c(pre_old=0, pre_new=3, post_new=3)}
        assert m.invariant_failures("t", counts, {"V": {"pre_old": 2, "pre_new": 0}}) == []
        assert m.growth_since_original(counts, {"V": {"pre_old": 2, "pre_new": 0}}) == ["V"]

    def test_index_lag_is_retried(self, tmp_path, fake):
        # The first post-count pages on by_vin still show F-A1/F-A2 under the old VIN.
        fake(standard_tables(), stale_index_reads=3, stale_index_name="by_vin")
        assert run_main(tmp_path, *apply_args()) == m.EXIT_OK
        assert read_json(tmp_path / "revin_postcount_avx_findings.json")["invariant_ok"] is True

    def test_lag_that_never_clears_exits_3(self, tmp_path, fake):
        fake(standard_tables(), stale_index_reads=10_000, stale_index_name="by_vin")
        assert run_main(tmp_path, *apply_args()) == m.EXIT_INVARIANT
        post = read_json(tmp_path / "revin_postcount_avx_findings.json")
        assert post["invariant_ok"] is False and post["failures"]

    def test_row_moved_elsewhere_concurrently_is_counted_and_fails(self, tmp_path, fake, monkeypatch):
        srv = fake(standard_tables())
        real = m.scrub_findings

        def racing(ddb, table, plan, dry_run):
            item(srv, FINDINGS, "finding_id", "F-A2")["vin"] = S(OTHER_VIN)
            return real(ddb, table, plan, dry_run)
        monkeypatch.setattr(m, "scrub_findings", racing)
        assert run_main(tmp_path, *apply_args()) == m.EXIT_INVARIANT
        post = read_json(tmp_path / "revin_postcount_avx_findings.json")
        assert post["vehicles"][VID_A]["changed_concurrently"] == 1

    def test_row_moved_to_the_new_vin_concurrently_passes(self, tmp_path, fake, monkeypatch):
        srv = fake(standard_tables())
        real = m.scrub_triage

        def racing(ddb, table, plan, dry_run):
            item(srv, TRIAGE, "sessionId", "SES-B1")["vin"] = S(NEW_B)
            return real(ddb, table, plan, dry_run)
        monkeypatch.setattr(m, "scrub_triage", racing)
        assert run_main(tmp_path, *apply_args()) == m.EXIT_OK
        post = read_json(tmp_path / "revin_postcount_triage_decisions.json")
        assert post["vehicles"][VID_B]["changed_concurrently"] == 1


# ── Resume and re-run ─────────────────────────────────────────────────────────

class TestResume:
    def test_resume_after_a_partial_run_keeps_the_original_baseline(self, tmp_path, fake, monkeypatch):
        srv = fake(standard_tables())
        real = m.scrub_triage

        def crash(*_a, **_k):
            raise RuntimeError("simulated crash after findings moved")
        monkeypatch.setattr(m, "scrub_triage", crash)
        with pytest.raises(RuntimeError):
            run_main(tmp_path, *apply_args())
        original = (tmp_path / "revin_prebaseline_avx_findings.json").read_bytes()
        original_t = (tmp_path / "revin_prebaseline_triage_decisions.json").read_bytes()
        assert item(srv, TRIAGE, "sessionId", "SES-A1")["vin"] == S(OLD_A)

        monkeypatch.setattr(m, "scrub_triage", real)
        assert run_main(tmp_path, *apply_args()) == m.EXIT_OK
        assert (tmp_path / "revin_prebaseline_avx_findings.json").read_bytes() == original
        assert (tmp_path / "revin_prebaseline_triage_decisions.json").read_bytes() == original_t
        resumes = list(tmp_path.glob("revin_resume_avx_findings_*.json"))
        assert len(resumes) == 1
        assert read_json(resumes[0])["vehicles"][VID_A]["pre_new"] == 3  # already moved
        assert item(srv, TRIAGE, "sessionId", "SES-A1")["vin"] == S(NEW_A)

    def test_resume_detects_a_finding_lost_between_runs(self, tmp_path, fake, monkeypatch):
        """Cycle-1 (fresh) W2: the resume compares against the original baseline."""
        srv = fake(standard_tables())
        real = m.scrub_triage

        def crash(*_a, **_k):
            raise RuntimeError("simulated crash after findings moved")
        monkeypatch.setattr(m, "scrub_triage", crash)
        with pytest.raises(RuntimeError):
            run_main(tmp_path, *apply_args())
        srv.items(FINDINGS).remove(item(srv, FINDINGS, "finding_id", "F-A2"))  # lost
        monkeypatch.setattr(m, "scrub_triage", real)
        assert run_main(tmp_path, *apply_args()) == m.EXIT_INVARIANT
        post = read_json(tmp_path / "revin_postcount_avx_findings.json")
        assert any("original baseline" in f for f in post["failures"])

    def test_a_finding_already_on_the_new_vin_lost_across_a_resume_fails(
            self, tmp_path, fake, monkeypatch):
        """Original check counts pre(new) too (review fresh cycle 2 S1 O2)."""
        t = standard_tables()
        t[FINDINGS]["items"].append(finding("F-A-ALREADY-NEW", NEW_A, kind="health.charging"))
        srv = fake(t)
        real = m.scrub_triage

        def crash(*_a, **_k):
            raise RuntimeError("simulated crash after findings moved")
        monkeypatch.setattr(m, "scrub_triage", crash)
        with pytest.raises(RuntimeError):
            run_main(tmp_path, *apply_args())
        srv.items(FINDINGS).remove(item(srv, FINDINGS, "finding_id", "F-A-ALREADY-NEW"))
        monkeypatch.setattr(m, "scrub_triage", real)
        assert run_main(tmp_path, *apply_args()) == m.EXIT_INVARIANT
        post = read_json(tmp_path / "revin_postcount_avx_findings.json")
        assert any(VID_A in f and "original baseline" in f for f in post["failures"])
        tpost = read_json(tmp_path / "revin_postcount_triage_decisions.json")
        assert tpost["invariant_ok"] is True and tpost["run_invariant_ok"] is False
        assert tpost["failures"] == []

    def test_a_baseline_for_another_surface_is_refused(self, tmp_path, fake):
        srv = fake(standard_tables())
        assert run_main(tmp_path, *apply_args()) == m.EXIT_OK
        n = len(srv.writes())
        (tmp_path / "revin_prebaseline_avx_findings.json").write_bytes(
            (tmp_path / "revin_prebaseline_triage_decisions.json").read_bytes())
        assert run_main(tmp_path, *apply_args()) == m.EXIT_INPUT
        assert len(srv.writes()) == n

    def test_rerun_after_success_passes_and_writes_nothing(self, tmp_path, fake):
        srv = fake(standard_tables())
        assert run_main(tmp_path, *apply_args()) == m.EXIT_OK
        writes_after_first = len(srv.writes())
        assert run_main(tmp_path, *apply_args()) == m.EXIT_OK
        assert len(srv.writes()) == writes_after_first

    @pytest.mark.parametrize("content", ['{}', '{"applied": false, "surface": "avx_findings"}'])
    def test_a_non_apply_baseline_is_refused(self, tmp_path, fake, content):
        srv = fake(standard_tables())
        (tmp_path / "revin_prebaseline_avx_findings.json").write_text(content)
        assert run_main(tmp_path, *apply_args()) == m.EXIT_INPUT
        assert srv.writes() == []

    def test_a_dry_run_record_at_the_baseline_path_is_refused(self, tmp_path, fake):
        """Same surface and mapping, but ``applied: false``: only that field refuses it."""
        srv = fake(standard_tables())
        assert run_main(tmp_path) == m.EXIT_OK
        (tmp_path / "revin_prebaseline_avx_findings.json").write_bytes(
            (tmp_path / "revin_dryrun_avx_findings.json").read_bytes())
        assert run_main(tmp_path, *apply_args()) == m.EXIT_INPUT
        assert srv.writes() == []

    def test_a_baseline_for_another_mapping_is_refused(self, tmp_path, fake):
        srv = fake(standard_tables())
        rows = ROWS[:2]
        assert run_main(tmp_path, *apply_args(rows), rows=rows) == m.EXIT_OK
        writes = len(srv.writes())
        assert run_main(tmp_path, *apply_args()) == m.EXIT_INPUT
        assert len(srv.writes()) == writes


# ── Duplicate live findings (review cycle 2 W3) ───────────────────────────────

class TestDuplicateFindings:
    def live_new_row_tables(self) -> dict[str, Any]:
        t = standard_tables()
        # A Tier 2 writer ran with the new VIN before the scrub: a live row on F-A1's new key.
        t[FINDINGS]["items"].append(finding("F-WRITER-NEW", NEW_A))
        return t

    def test_dry_run_reports_the_conflict(self, tmp_path, fake):
        fake(self.live_new_row_tables())
        assert run_main(tmp_path) == m.EXIT_CONFLICT
        conflicts = read_json(tmp_path / "revin_dryrun_avx_findings.json")["conflicts"]
        assert any("F-WRITER-NEW" in c for c in conflicts)

    def test_apply_refuses_before_writing_anything(self, tmp_path, fake):
        srv = fake(self.live_new_row_tables())
        assert run_main(tmp_path, *apply_args()) == m.EXIT_CONFLICT
        assert srv.writes() == []
        assert not list(tmp_path.glob("revin_prebaseline_*.json"))

    def test_conflict_appearing_after_the_plan_stops_the_write(self, tmp_path, fake, monkeypatch):
        srv = fake(standard_tables())
        real = m.scrub_findings

        def writer_runs_now(ddb, table, plan, dry_run):
            srv.items(FINDINGS).append(finding("F-WRITER-LATE", NEW_A))
            return real(ddb, table, plan, dry_run)
        monkeypatch.setattr(m, "scrub_findings", writer_runs_now)
        assert run_main(tmp_path, *apply_args()) == m.EXIT_CONFLICT
        assert item(srv, FINDINGS, "finding_id", "F-A1")["vin"] == S(OLD_A)

    def test_an_expired_row_on_the_new_key_is_not_a_conflict(self, tmp_path, fake):
        t = standard_tables()
        t[FINDINGS]["items"].append(finding("F-OLD-NEWKEY", NEW_A, expires=PAST))
        fake(t)
        assert run_main(tmp_path, *apply_args()) == m.EXIT_OK

    def test_an_expired_old_row_may_take_a_key_a_live_row_holds(self, tmp_path, fake):
        # F-A-EXPIRED (tires) moves onto the key a live new-VIN tires row holds; only
        # one of the two is live, so the writer's lookup still finds exactly one.
        t = standard_tables()
        t[FINDINGS]["items"].append(finding("F-LIVE-TIRES", NEW_A, kind="health.tires"))
        srv = fake(t)
        assert run_main(tmp_path, *apply_args()) == m.EXIT_OK
        assert item(srv, FINDINGS, "finding_id", "F-A-EXPIRED")["vin"] == S(NEW_A)

    def test_two_live_old_rows_that_would_share_a_key_conflict(self, tmp_path, fake):
        t = standard_tables()
        t[FINDINGS]["items"].append(finding("F-A1-TWIN", OLD_A))
        srv = fake(t)
        assert run_main(tmp_path, *apply_args()) == m.EXIT_CONFLICT
        assert srv.writes() == []

    @pytest.mark.parametrize("field_name", ["agent_id", "finding_kind"])
    def test_a_vin_scoped_row_that_cannot_be_keyed_is_refused(self, tmp_path, fake, field_name):
        t = standard_tables()
        bad = finding("F-BAD", OLD_B, kind="health.brakes")
        bad[field_name] = S("")
        t[FINDINGS]["items"].append(bad)
        srv = fake(t)
        assert run_main(tmp_path, *apply_args()) == m.EXIT_CONFLICT
        assert srv.writes() == []


# ── Triage TTL (cycle-1 (fresh) W1) ───────────────────────────────────────────

DAY = 24 * 3600


class TestTriageTtl:
    def test_rows_near_their_ttl_move_but_are_not_counted(self, tmp_path, fake):
        now = int(time.time())
        t = standard_tables()
        t[TRIAGE]["items"] += [triage("SES-A-SOON", OLD_A, ttl=now + 60),
                               triage("SES-A-PAST", OLD_A, ttl=now - 60),
                               triage("SES-A-LATER", OLD_A, ttl=now + 10 * DAY)]
        srv = fake(t)
        assert run_main(tmp_path, *apply_args()) == m.EXIT_OK
        for sid in ("SES-A-SOON", "SES-A-PAST", "SES-A-LATER", "SES-A1"):
            assert item(srv, TRIAGE, "sessionId", sid)["vin"] == S(NEW_A)
        v = read_json(tmp_path / "revin_postcount_triage_decisions.json")["vehicles"][VID_A]
        assert (v["pre_old"], v["excluded_expiring"], v["post_new"]) == (2, 2, 2)

    def test_an_expiring_row_deleted_by_ttl_during_the_run_is_not_a_loss(
            self, tmp_path, fake, monkeypatch):
        now = int(time.time())
        t = standard_tables()
        t[TRIAGE]["items"].append(triage("SES-A-PAST", OLD_A, ttl=now - 60))
        srv = fake(t)
        real = m.scrub_triage

        def ttl_sweep_then_scrub(ddb, table, plan, dry_run):
            srv.items(TRIAGE).remove(item(srv, TRIAGE, "sessionId", "SES-A-PAST"))
            return real(ddb, table, plan, dry_run)
        monkeypatch.setattr(m, "scrub_triage", ttl_sweep_then_scrub)
        assert run_main(tmp_path, *apply_args()) == m.EXIT_OK

    def test_ttl_deletion_between_a_partial_run_and_the_resume_is_not_a_loss(
            self, tmp_path, fake, monkeypatch):
        now = int(time.time())
        t = standard_tables()
        t[TRIAGE]["items"].append(triage("SES-B-2DAYS", OLD_B, ttl=now + 2 * DAY))
        srv = fake(t)
        real = m.scrub_triage

        def crash(*_a, **_k):
            raise RuntimeError("simulated crash before triage moved")
        monkeypatch.setattr(m, "scrub_triage", crash)
        with pytest.raises(RuntimeError):
            run_main(tmp_path, *apply_args())
        pre = read_json(tmp_path / "revin_prebaseline_triage_decisions.json")
        assert pre["vehicles"][VID_B]["pre_old"] == 3  # SES-B-2DAYS was counted

        # Three days later the TTL sweep has deleted it; the resume must not call that a loss.
        srv.items(TRIAGE).remove(item(srv, TRIAGE, "sessionId", "SES-B-2DAYS"))
        monkeypatch.setattr(m, "epoch_now", lambda: now + 3 * DAY)
        monkeypatch.setattr(m, "scrub_triage", real)
        assert run_main(tmp_path, *apply_args()) == m.EXIT_OK

    def _partial(self, tmp_path, fake, monkeypatch, t):
        srv = fake(t)
        real = m.scrub_triage

        def crash(*_a, **_k):
            raise RuntimeError("simulated crash before triage moved")
        monkeypatch.setattr(m, "scrub_triage", crash)
        with pytest.raises(RuntimeError):
            run_main(tmp_path, *apply_args())
        monkeypatch.setattr(m, "scrub_triage", real)
        return srv

    def test_a_row_inside_the_resume_margin_is_not_a_loss(self, tmp_path, fake, monkeypatch):
        """The original check uses the same horizon as post(new) (review fresh cycle 2 S1 T10)."""
        now = int(time.time())
        t = standard_tables()
        t[TRIAGE]["items"].append(triage("SES-B-2D", OLD_B, ttl=now + 2 * DAY))
        srv = self._partial(tmp_path, fake, monkeypatch, t)
        # 1.5 days later the row still exists, but its ttl is inside this run's 24 h margin.
        monkeypatch.setattr(m, "epoch_now", lambda: now + 36 * 3600)
        assert run_main(tmp_path, *apply_args()) == m.EXIT_OK
        assert item(srv, TRIAGE, "sessionId", "SES-B-2D")["vin"] == S(NEW_B)

    def test_a_triage_row_already_on_the_new_vin_lost_across_a_resume_fails(
            self, tmp_path, fake, monkeypatch):
        """The baseline's ttls cover pre(new) rows too (review fresh cycle 2 S1 T4)."""
        t = standard_tables()
        t[TRIAGE]["items"].append(triage("SES-B-ALREADY-NEW", NEW_B))
        srv = self._partial(tmp_path, fake, monkeypatch, t)
        srv.items(TRIAGE).remove(item(srv, TRIAGE, "sessionId", "SES-B-ALREADY-NEW"))
        assert run_main(tmp_path, *apply_args()) == m.EXIT_INVARIANT
        post = read_json(tmp_path / "revin_postcount_triage_decisions.json")
        assert any(VID_B in f and "original baseline" in f for f in post["failures"])

    def test_a_counted_row_lost_between_runs_still_fails(self, tmp_path, fake, monkeypatch):
        """Positive control for the discount: a row with a distant ttl is still checked."""
        now = int(time.time())
        t = standard_tables()
        t[TRIAGE]["items"].append(triage("SES-B-LATER", OLD_B, ttl=now + 30 * DAY))
        srv = fake(t)
        real = m.scrub_triage

        def crash(*_a, **_k):
            raise RuntimeError("simulated crash before triage moved")
        monkeypatch.setattr(m, "scrub_triage", crash)
        with pytest.raises(RuntimeError):
            run_main(tmp_path, *apply_args())
        srv.items(TRIAGE).remove(item(srv, TRIAGE, "sessionId", "SES-B-LATER"))
        monkeypatch.setattr(m, "epoch_now", lambda: now + 3 * DAY)
        monkeypatch.setattr(m, "scrub_triage", real)
        assert run_main(tmp_path, *apply_args()) == m.EXIT_INVARIANT


# ── AVX broker exposure (cycle-1 (fresh) W3) ──────────────────────────────────

class TestBrokerExposure:
    def owned(self, owner: str) -> dict[str, Any]:
        t = standard_tables()
        f = finding("F-OWNED", OLD_B, kind="health.brakes")
        f["owner_id"] = S(owner)
        t[FINDINGS]["items"].append(f)
        return t

    def test_an_app_user_finding_without_a_card_is_refused(self, tmp_path, fake):
        srv = fake(self.owned(APP_USER))
        assert run_main(tmp_path) == m.EXIT_CONFLICT
        conflicts = read_json(tmp_path / "revin_dryrun_avx_findings.json")["conflicts"]
        assert any("F-OWNED" in c and "card" in c for c in conflicts)
        assert run_main(tmp_path, *apply_args()) == m.EXIT_CONFLICT
        assert srv.writes() == []

    def test_an_expired_app_user_finding_without_a_card_is_refused_too(self, tmp_path, fake):
        """The writer never re-emits an expired row, so the scrub's MODIFY is the one new way
        the broker could card it; expiry must not exempt it (review fresh cycle 2 W1)."""
        t = standard_tables()
        f = finding("F-OWNED-OLD", OLD_B, kind="health.brakes", expires=PAST)
        f["owner_id"] = S(APP_USER)
        t[FINDINGS]["items"].append(f)
        srv = fake(t)
        assert run_main(tmp_path, *apply_args()) == m.EXIT_CONFLICT
        assert srv.writes() == []

    def test_an_app_user_finding_with_a_card_moves(self, tmp_path, fake):
        t = self.owned(APP_USER)
        raw = next(i for i in t[FINDINGS]["items"] if i["finding_id"] == S("F-OWNED"))
        action_id, _, _ = avx_broker._card_identity(avx_broker._deserialise_ddb_item(raw),
                                                    APP_USER)
        t[ACTIONS]["items"].append({"action_id": S(action_id), "status": S("dismissed")})
        srv = fake(t)
        assert run_main(tmp_path, *apply_args()) == m.EXIT_OK
        assert item(srv, FINDINGS, "finding_id", "F-OWNED")["vin"] == S(NEW_B)

    def test_a_registry_owner_is_skipped_by_the_broker_so_it_moves(self, tmp_path, fake):
        srv = fake(self.owned("CUST-0A1B2C3D"))
        assert run_main(tmp_path, *apply_args()) == m.EXIT_OK
        assert "GetItem" not in srv.operations()


# ── Stage and table guards ────────────────────────────────────────────────────

class TestGuards:
    @pytest.mark.parametrize("name", ["vsa-prod-avx-findings", "evil-vsa-staging-x",
                                      "cms-staging-storage-vehicles"])
    def test_table_guard(self, name):
        with pytest.raises(m.TableGuardError):
            m.assert_staging_table(name)

    def test_writers_refuse_a_non_staging_table(self):
        with pytest.raises(m.TableGuardError):
            m.scrub_findings(None, "vsa-prod-avx-findings", m.Plan(), dry_run=True)
        with pytest.raises(m.TableGuardError):
            m.scrub_triage(None, "vsa-prod-triage-decisions", m.Plan(), dry_run=True)

    def test_prod_stage_refused_before_any_call(self, tmp_path, fake):
        srv = fake(standard_tables())
        assert run_main(tmp_path, "--stage", "prod") == m.EXIT_INPUT
        assert srv.calls == []

    def test_a_non_utf8_tsv_is_refused_cleanly(self, tmp_path, fake):
        srv = fake(standard_tables())
        tsv = tmp_path / "map.tsv"
        tsv.write_bytes(b"VEH\t\xff\xfe\tMRDN\n")
        assert m.main(["--tsv-file", str(tsv), "--baseline-dir", str(tmp_path)]) == m.EXIT_INPUT
        assert srv.calls == []

    def test_debug_logging_keeps_botocore_at_info(self, tmp_path, fake):
        import logging
        fake(standard_tables())
        assert run_main(tmp_path, "--log-level", "DEBUG") == m.EXIT_OK
        assert logging.getLogger("botocore").level == logging.INFO

    def test_bad_tsv_refused_before_any_call(self, tmp_path, fake):
        srv = fake(standard_tables())
        assert run_main(tmp_path, rows=[(VID_A, OLD_A, NEW_B)]) == m.EXIT_INPUT
        assert srv.calls == []


# ── Separate process: the CLI as Group 3 runs it ──────────────────────────────

_DRIVER = r'''
import sys
sys.path.insert(0, __ROOT__)
import boto3
import scripts.scrub_avx_findings_vin as m

mode, argv = sys.argv[1], sys.argv[2:]
if mode == "force-write":
    real = m.scrub_findings
    m.scrub_findings = lambda ddb, table, plan, dry_run: real(ddb, table, plan, False)
    sys.exit(m.main(argv))
if mode == "stray-client":
    m.make_session("us-west-2", apply=False)
    try:
        boto3.client("dynamodb").update_item(
            TableName="vsa-staging-avx-findings", Key={"finding_id": {"S": "F-A1"}},
            UpdateExpression="SET vin = :v", ExpressionAttributeValues={":v": {"S": "X"}})
    except m.readonly_guard.ReadOnlyViolation:
        sys.exit(40)
    sys.exit(41)
'''


def _child_env(tmp_path: Path, url: str) -> dict[str, str]:
    """Only what the child needs: dummy credentials, the fake endpoint, a dead proxy."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_CONFIG_FILE": str(tmp_path / "no-aws-config"),
        "AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "no-aws-credentials"),
        "AWS_ENDPOINT_URL": url,
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_MAX_ATTEMPTS": "1",
        # Anything not bound for the fake goes to a dead proxy, so a regression that
        # ignored the endpoint override still could not reach AWS.
        "HTTPS_PROXY": "http://127.0.0.1:1",
        "HTTP_PROXY": "http://127.0.0.1:1",
        "NO_PROXY": "127.0.0.1,localhost",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


class TestSubprocess:
    def _run(self, tmp_path: Path, srv_url: str, args: list[str], driver_mode: str | None = None):
        tsv = write_tsv(tmp_path / "map.tsv", ROWS)
        common = ["--tsv-file", str(tsv), "--baseline-dir", str(tmp_path)]
        if driver_mode is None:
            cmd = [sys.executable, str(_SCRIPT), *common, *args]
        else:
            driver = tmp_path / "driver.py"
            driver.write_text(_DRIVER.replace("__ROOT__", repr(str(_CVX_ROOT))))
            cmd = [sys.executable, str(driver), driver_mode, *common, *args]
        return subprocess.run(cmd, env=_child_env(tmp_path, srv_url), capture_output=True,
                              text=True, timeout=120, cwd=str(tmp_path))

    def test_cli_dry_run_reads_and_writes_nothing(self, tmp_path):
        srv = FakeDynamoDB(standard_tables())
        url = srv.start()
        try:
            proc = self._run(tmp_path, url, [])
        finally:
            srv.stop()
        assert proc.returncode == m.EXIT_OK, proc.stderr
        assert srv.writes() == []
        assert {"Query", "Scan"} <= set(srv.operations())  # it really ran against the fake
        assert (tmp_path / "revin_dryrun_avx_findings.json").exists()
        assert not list(tmp_path.glob("revin_prebaseline_*.json"))

    def test_cli_forced_write_in_a_dry_run_is_refused_by_the_session_guard(self, tmp_path):
        srv = FakeDynamoDB(standard_tables())
        url = srv.start()
        try:
            proc = self._run(tmp_path, url, [], driver_mode="force-write")
        finally:
            srv.stop()
        assert proc.returncode == m.EXIT_READONLY, proc.stderr
        assert "dry run refused mutating call" in proc.stderr
        assert srv.writes() == []

    def test_positive_control_the_same_forced_write_lands_with_apply(self, tmp_path):
        srv = FakeDynamoDB(standard_tables())
        url = srv.start()
        try:
            proc = self._run(tmp_path, url, ["--apply", "--expected-rows", "3"],
                             driver_mode="force-write")
        finally:
            srv.stop()
        assert proc.returncode == m.EXIT_OK, proc.stderr
        assert any(op == "UpdateItem" for op, _ in srv.writes())

    def test_a_later_default_session_client_is_guarded_too(self, tmp_path):
        srv = FakeDynamoDB(standard_tables())
        url = srv.start()
        try:
            proc = self._run(tmp_path, url, [], driver_mode="stray-client")
        finally:
            srv.stop()
        assert proc.returncode == 40, proc.stderr
        assert srv.writes() == []
