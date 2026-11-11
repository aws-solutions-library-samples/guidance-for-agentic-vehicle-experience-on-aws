# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests for scripts/purge_expired_avx_findings.py (spec 2026-10-27-avx-findings-retention, A2).

All tests run against moto's in-memory DynamoDB — no AWS call is made.

Commit history:
  RED   — this file committed before the module exists (ImportError = red)
  GREEN — purge_expired_avx_findings.py created; all tests pass

Mutations recorded in .kiro/specs/2026-10-27-avx-findings-retention/mutations-A2.md.
"""
from __future__ import annotations

import csv
import hashlib
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import boto3
import pytest

_CVX_ROOT = Path(__file__).resolve().parents[2]
if str(_CVX_ROOT) not in sys.path:
    sys.path.insert(0, str(_CVX_ROOT))

from moto import mock_aws  # noqa: E402

import scripts.purge_expired_avx_findings as purge  # noqa: E402

# ── Constants ──────────────────────────────────────────────────────────────────

TABLE = "vsa-staging-avx-findings"

# Reference instant
NOW = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
GRACE_DAYS = 7
GRACE_CUTOFF = NOW - timedelta(days=GRACE_DAYS)

# expires_at strings for different cases
EXPIRED_LONG = "2026-10-15T00:00:00Z"     # 12 days before NOW → past grace
EXPIRED_IN_GRACE = "2026-10-22T00:00:00Z"  # 5 days before NOW → inside grace
UNEXPIRED = "2099-01-01T00:00:00Z"


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _no_aws(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No test may reach real AWS."""
    for var in (
        "AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_ENDPOINT_URL",
        "AWS_ENDPOINT_URL_DYNAMODB", "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS",
        "AWS_REGION", "AWS_DEFAULT_REGION",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-aws-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-aws-creds"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


def _make_row(**kwargs: Any) -> dict:
    """A baseline eligible row (expired past grace, no recommended_action)."""
    row: dict = {
        "finding_id": "FIND-001",
        "agent_id": "agent-2-vehicle-health",
        "finding_kind": "health.brakes",
        "fleet_id": "FLEET-001",
        "vin": "MRDN0000001000001",
        "computed_at": "2026-10-15T00:00:00Z",
        "expires_at": EXPIRED_LONG,
    }
    row.update(kwargs)
    return row


def _drop_if_exists(ddb, name: str) -> None:
    """Delete ``name`` if a previous test left it.

    Isolation guard: when another scripts test leaks an active ``mock_aws`` (e.g. a world
    whose setup raised before its ExitStack closed), the per-test ``@mock_aws`` here nests
    inside it and tables persist across tests. Dropping first keeps each test independent.
    """
    try:
        ddb.meta.client.delete_table(TableName=name)
    except ddb.meta.client.exceptions.ResourceNotFoundException:
        pass


def _create_table(items: list[dict]) -> None:
    """Create TABLE in the current moto context and populate it."""
    ddb = boto3.resource("dynamodb", region_name="us-west-2")
    _drop_if_exists(ddb, TABLE)
    table = ddb.create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "finding_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "finding_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    for item in items:
        table.put_item(Item=item)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


# ── is_eligible ───────────────────────────────────────────────────────────────

class TestIsEligible:
    """Unit tests for purge.is_eligible() — no DynamoDB needed."""

    def test_eligible_row_expired_past_grace(self):
        assert purge.is_eligible(_make_row(expires_at=EXPIRED_LONG), GRACE_CUTOFF) is True

    def test_ineligible_fi_row_with_recommended_action(self):
        row = _make_row(expires_at=EXPIRED_LONG, recommended_action={"action": "book_service"})
        assert purge.is_eligible(row, GRACE_CUTOFF) is False

    def test_ineligible_recall_warranty_agent(self):
        row = _make_row(expires_at=EXPIRED_LONG, agent_id="agent-fi-recall-warranty")
        assert purge.is_eligible(row, GRACE_CUTOFF) is False

    def test_ineligible_quality_agent(self):
        row = _make_row(expires_at=EXPIRED_LONG, agent_id="agent-fi-quality")
        assert purge.is_eligible(row, GRACE_CUTOFF) is False

    def test_ineligible_unexpired_row(self):
        assert purge.is_eligible(_make_row(expires_at=UNEXPIRED), GRACE_CUTOFF) is False

    def test_ineligible_expired_inside_grace(self):
        assert purge.is_eligible(_make_row(expires_at=EXPIRED_IN_GRACE), GRACE_CUTOFF) is False

    def test_ineligible_malformed_expires_at(self):
        row = _make_row(expires_at="not-a-date")
        assert purge.is_eligible(row, GRACE_CUTOFF) is False

    def test_ineligible_missing_expires_at(self):
        row = _make_row()
        del row["expires_at"]
        assert purge.is_eligible(row, GRACE_CUTOFF) is False

    def test_ineligible_empty_expires_at(self):
        assert purge.is_eligible(_make_row(expires_at=""), GRACE_CUTOFF) is False

    def test_ineligible_ttl_exempt_row(self):
        """A row with ttl_exempt=True (once-FI, permanently kept) is never purged.

        Mutation guard (FA3 Suggestion 1): remove the ``if row.get("ttl_exempt"):``
        check → ttl_exempt row passes is_eligible → this assertion fails.
        """
        row = _make_row(expires_at=EXPIRED_LONG, ttl_exempt=True)
        assert purge.is_eligible(row, GRACE_CUTOFF) is False, (
            "FA3/S1: a row with ttl_exempt=True must not be eligible for purge"
        )


# ── Dry run ───────────────────────────────────────────────────────────────────

class TestDryRun:
    """Dry-run: scans table, writes CSV, returns sha256."""

    @mock_aws
    def test_eligible_row_appears_in_csv(self, tmp_path: Path) -> None:
        _create_table([_make_row(finding_id="FIND-A")])
        _create_actions_table([])
        out = str(tmp_path / "out.csv")
        sha, _, _, _ = purge.dry_run(TABLE, "us-west-2", GRACE_CUTOFF, out, segments=1,
                                     actions_table=ACTIONS)
        rows = list(csv.DictReader(open(out, newline="", encoding="utf-8")))
        assert len(rows) == 1
        assert rows[0]["finding_id"] == "FIND-A"
        assert sha == _sha256(out)

    @mock_aws
    def test_fi_row_excluded_from_csv(self, tmp_path: Path) -> None:
        _create_table([_make_row(finding_id="FIND-FI", expires_at=EXPIRED_LONG,
                                  recommended_action={"action": "book"})])
        _create_actions_table([])
        out = str(tmp_path / "out.csv")
        purge.dry_run(TABLE, "us-west-2", GRACE_CUTOFF, out, segments=1, actions_table=ACTIONS)
        rows = list(csv.DictReader(open(out, newline="", encoding="utf-8")))
        assert len(rows) == 0

    @mock_aws
    def test_recall_row_excluded_from_csv(self, tmp_path: Path) -> None:
        _create_table([_make_row(finding_id="FIND-RECALL", agent_id="agent-fi-recall-warranty")])
        _create_actions_table([])
        out = str(tmp_path / "out.csv")
        purge.dry_run(TABLE, "us-west-2", GRACE_CUTOFF, out, segments=1, actions_table=ACTIONS)
        rows = list(csv.DictReader(open(out, newline="", encoding="utf-8")))
        assert len(rows) == 0

    @mock_aws
    def test_quality_row_excluded_from_csv(self, tmp_path: Path) -> None:
        _create_table([_make_row(finding_id="FIND-QUAL", agent_id="agent-fi-quality")])
        _create_actions_table([])
        out = str(tmp_path / "out.csv")
        purge.dry_run(TABLE, "us-west-2", GRACE_CUTOFF, out, segments=1, actions_table=ACTIONS)
        rows = list(csv.DictReader(open(out, newline="", encoding="utf-8")))
        assert len(rows) == 0

    @mock_aws
    def test_unexpired_row_excluded_from_csv(self, tmp_path: Path) -> None:
        _create_table([_make_row(finding_id="FIND-LIVE", expires_at=UNEXPIRED)])
        _create_actions_table([])
        out = str(tmp_path / "out.csv")
        purge.dry_run(TABLE, "us-west-2", GRACE_CUTOFF, out, segments=1, actions_table=ACTIONS)
        rows = list(csv.DictReader(open(out, newline="", encoding="utf-8")))
        assert len(rows) == 0

    @mock_aws
    def test_expired_inside_grace_excluded_from_csv(self, tmp_path: Path) -> None:
        _create_table([_make_row(finding_id="FIND-GRACE", expires_at=EXPIRED_IN_GRACE)])
        _create_actions_table([])
        out = str(tmp_path / "out.csv")
        purge.dry_run(TABLE, "us-west-2", GRACE_CUTOFF, out, segments=1, actions_table=ACTIONS)
        rows = list(csv.DictReader(open(out, newline="", encoding="utf-8")))
        assert len(rows) == 0

    @mock_aws
    def test_malformed_expires_at_excluded_from_csv(self, tmp_path: Path) -> None:
        _create_table([_make_row(finding_id="FIND-BAD", expires_at="bad-date")])
        _create_actions_table([])
        out = str(tmp_path / "out.csv")
        purge.dry_run(TABLE, "us-west-2", GRACE_CUTOFF, out, segments=1, actions_table=ACTIONS)
        rows = list(csv.DictReader(open(out, newline="", encoding="utf-8")))
        assert len(rows) == 0

    @mock_aws
    def test_csv_sorted_by_finding_id(self, tmp_path: Path) -> None:
        items = [
            _make_row(finding_id="FIND-Z", vin="MRDN0000001000003"),
            _make_row(finding_id="FIND-A", vin="MRDN0000001000001"),
            _make_row(finding_id="FIND-M", vin="MRDN0000001000002"),
        ]
        _create_table(items)
        _create_actions_table([])
        out = str(tmp_path / "out.csv")
        purge.dry_run(TABLE, "us-west-2", GRACE_CUTOFF, out, segments=1, actions_table=ACTIONS)
        rows = list(csv.DictReader(open(out, newline="", encoding="utf-8")))
        fids = [r["finding_id"] for r in rows]
        assert fids == sorted(fids)

    @mock_aws
    def test_csv_has_all_required_columns(self, tmp_path: Path) -> None:
        _create_table([_make_row()])
        _create_actions_table([])
        out = str(tmp_path / "out.csv")
        purge.dry_run(TABLE, "us-west-2", GRACE_CUTOFF, out, segments=1, actions_table=ACTIONS)
        rows = list(csv.DictReader(open(out, newline="", encoding="utf-8")))
        assert len(rows) == 1
        for col in purge.CSV_FIELDS:
            assert col in rows[0], f"missing column {col!r}"

    @mock_aws
    def test_sha256_matches_actual_file(self, tmp_path: Path) -> None:
        _create_table([_make_row()])
        _create_actions_table([])
        out = str(tmp_path / "out.csv")
        sha, _, _, _ = purge.dry_run(TABLE, "us-west-2", GRACE_CUTOFF, out, segments=1,
                                     actions_table=ACTIONS)
        assert sha == _sha256(out)

    @mock_aws
    def test_counts_by_agent_kind_fleet(self, tmp_path: Path) -> None:
        items = [
            _make_row(finding_id="F1", agent_id="agent-2-vehicle-health",
                      finding_kind="health.brakes", fleet_id="FLEET-A"),
            _make_row(finding_id="F2", agent_id="agent-2-vehicle-health",
                      finding_kind="health.battery", fleet_id="FLEET-B"),
            _make_row(finding_id="F3", agent_id="agent-tier2-platform-health",
                      finding_kind="health.brakes", fleet_id=""),
        ]
        _create_table(items)
        _create_actions_table([])
        out = str(tmp_path / "out.csv")
        _, by_agent, by_kind, by_fleet = purge.dry_run(TABLE, "us-west-2", GRACE_CUTOFF, out,
                                                        segments=1, actions_table=ACTIONS)
        assert by_agent.get("agent-2-vehicle-health") == 2
        assert by_agent.get("agent-tier2-platform-health") == 1
        assert by_kind.get("health.brakes") == 2
        assert by_kind.get("health.battery") == 1
        assert by_fleet.get("FLEET-A") == 1
        assert by_fleet.get("FLEET-B") == 1
        # F3 has no fleet_id so not counted by fleet
        assert "" not in by_fleet

    @mock_aws
    def test_scan_with_multiple_segments(self, tmp_path: Path) -> None:
        """segments=4 with more rows than segments exercises the parallel scan path.

        Pins that every segment is scanned and no segment is silently skipped.
        The default-session mutation (replace per-thread Session with the shared
        default session) is NOT caught by this test: moto executes synchronously
        in a single thread, so thread-safety issues cannot be reproduced here.
        """
        items = [
            _make_row(finding_id=f"FIND-{i:03d}", vin=f"MRDN000000100{i:04d}")
            for i in range(10)
        ]
        _create_table(items)
        _create_actions_table([])
        out = str(tmp_path / "out.csv")
        sha, by_agent, _, _ = purge.dry_run(TABLE, "us-west-2", GRACE_CUTOFF, out,
                                             segments=4, actions_table=ACTIONS)
        total = sum(by_agent.values())
        assert total == 10, f"segments=4: expected 10 rows, got {total}"
        rows = list(csv.DictReader(open(out, newline="", encoding="utf-8")))
        assert len(rows) == 10


# ── Apply ─────────────────────────────────────────────────────────────────────

class TestApply:
    """Apply mode: verify sha, delete with condition, skip on condition failure."""

    @mock_aws
    def test_sha_mismatch_refused(self, tmp_path: Path) -> None:
        _create_table([_make_row()])
        _create_actions_table([])
        out = str(tmp_path / "out.csv")
        purge.dry_run(TABLE, "us-west-2", GRACE_CUTOFF, out, segments=1, actions_table=ACTIONS)
        with pytest.raises(ValueError, match="[Ss][Hh][Aa]"):
            purge.apply_purge(TABLE, "us-west-2", out, "wrong-sha256-hex",
                              actions_table=ACTIONS)

    @mock_aws
    def test_apply_deletes_eligible_row(self, tmp_path: Path) -> None:
        row = _make_row(finding_id="FIND-DEL")
        _create_table([row])
        _create_actions_table([])
        out = str(tmp_path / "out.csv")
        sha, _, _, _ = purge.dry_run(TABLE, "us-west-2", GRACE_CUTOFF, out, segments=1,
                                     actions_table=ACTIONS)
        deleted, skipped_cond, skipped_action = purge.apply_purge(TABLE, "us-west-2", out, sha,
                                                                   rate_delay_secs=0,
                                                                   actions_table=ACTIONS)
        assert deleted == 1
        assert skipped_cond == 0
        assert skipped_action == 0
        ddb = boto3.resource("dynamodb", region_name="us-west-2")
        resp = ddb.Table(TABLE).get_item(Key={"finding_id": "FIND-DEL"})
        assert "Item" not in resp

    @mock_aws
    def test_row_changed_between_list_and_apply_is_skipped(self, tmp_path: Path) -> None:
        """A row that gains recommended_action after list is skipped (condition fails)."""
        row = _make_row(finding_id="FIND-CHANGED")
        _create_table([row])
        _create_actions_table([])
        out = str(tmp_path / "out.csv")
        sha, _, _, _ = purge.dry_run(TABLE, "us-west-2", GRACE_CUTOFF, out, segments=1,
                                     actions_table=ACTIONS)

        # Between dry-run and apply: the row gains recommended_action (becomes an FI item)
        ddb = boto3.resource("dynamodb", region_name="us-west-2")
        ddb.Table(TABLE).update_item(
            Key={"finding_id": "FIND-CHANGED"},
            UpdateExpression="SET recommended_action = :a",
            ExpressionAttributeValues={":a": {"action": "book"}},
        )

        deleted, skipped_cond, skipped_action = purge.apply_purge(TABLE, "us-west-2", out, sha,
                                                                   rate_delay_secs=0,
                                                                   actions_table=ACTIONS)
        assert deleted == 0
        assert skipped_cond == 1  # condition failed (gained recommended_action)
        assert skipped_action == 0

    @mock_aws
    def test_resumability_already_deleted_row_skipped(self, tmp_path: Path) -> None:
        """Rows already gone on re-run are counted as skipped_condition, not errors."""
        row = _make_row(finding_id="FIND-GONE")
        _create_table([row])
        _create_actions_table([])
        out = str(tmp_path / "out.csv")
        sha, _, _, _ = purge.dry_run(TABLE, "us-west-2", GRACE_CUTOFF, out, segments=1,
                                     actions_table=ACTIONS)

        # First run: deletes the row
        deleted1, skipped_cond1, _ = purge.apply_purge(TABLE, "us-west-2", out, sha,
                                                        rate_delay_secs=0, actions_table=ACTIONS)
        assert deleted1 == 1

        # Second run (resume): row is already gone
        deleted2, skipped_cond2, _ = purge.apply_purge(TABLE, "us-west-2", out, sha,
                                                        rate_delay_secs=0, actions_table=ACTIONS)
        assert deleted2 == 0
        assert skipped_cond2 == 1

    @mock_aws
    def test_apply_does_not_delete_fi_row_if_not_in_list(self, tmp_path: Path) -> None:
        """If an FI row was excluded from the dry-run CSV, it must not be deleted."""
        fi_row = _make_row(finding_id="FIND-FI", expires_at=EXPIRED_LONG,
                           recommended_action={"action": "book"})
        eligible_row = _make_row(finding_id="FIND-OK")
        _create_table([fi_row, eligible_row])
        _create_actions_table([])
        out = str(tmp_path / "out.csv")
        sha, _, _, _ = purge.dry_run(TABLE, "us-west-2", GRACE_CUTOFF, out, segments=1,
                                     actions_table=ACTIONS)
        # CSV only contains FIND-OK; applying it should not touch FIND-FI
        purge.apply_purge(TABLE, "us-west-2", out, sha, rate_delay_secs=0, actions_table=ACTIONS)
        ddb = boto3.resource("dynamodb", region_name="us-west-2")
        resp = ddb.Table(TABLE).get_item(Key={"finding_id": "FIND-FI"})
        assert "Item" in resp, "FI row must not be deleted"


# ── CLI smoke ─────────────────────────────────────────────────────────────────

class TestCli:
    def test_help_exits_zero(self) -> None:
        with pytest.raises(SystemExit) as exc:
            purge.main(["--help"])
        assert exc.value.code == 0

    @mock_aws
    def test_dry_run_main_exits_zero(self, tmp_path: Path) -> None:
        _create_table([_make_row()])
        _create_actions_table([])
        out = str(tmp_path / "out.csv")
        rc = purge.main(["--stage", "staging", "--region", "us-west-2",
                          "--grace-days", "7", "--out", out,
                          "--segments", "1"])
        assert rc == 0
        assert Path(out).exists()

    def test_dry_run_without_out_exits_one(self) -> None:
        rc = purge.main(["--stage", "staging", "--region", "us-west-2"])
        assert rc == 1

    def test_apply_without_from_list_exits_one(self) -> None:
        rc = purge.main(["--stage", "staging", "--region", "us-west-2", "--apply"])
        assert rc == 1


# ── FA1 fix tests ─────────────────────────────────────────────────────────────


class TestApplyExpiresAtClause:
    """W1: expires_at and agent_id clauses in DeleteItem condition must each be pinned.

    The expires_at test uses a past-grace but DIFFERENT value so that the
    ``expires_at <= :cutoff`` clause still holds — only the equality clause
    ``expires_at = :eat`` can catch the change.
    """

    @mock_aws
    def test_expires_at_changed_to_past_grace_but_different_is_skipped(
        self, tmp_path: Path
    ) -> None:
        """Row whose expires_at changes to a different past-grace value is skipped.

        New value: still before the grace cutoff (= still past grace), so the
        ``expires_at <= :cutoff`` clause does NOT catch the change.  Only
        ``expires_at = :eat`` catches it.

        Mutation guard (mutations-FA2.md § W1):
            Drop ``AND expires_at = :eat`` → row is deleted → deleted=1 → FAIL.
        """
        row = _make_row(finding_id="FIND-EAT-CHANGED", expires_at=EXPIRED_LONG)
        _create_table([row])
        _create_actions_table([])
        out = str(tmp_path / "out.csv")
        sha, _, _, _ = purge.dry_run(TABLE, "us-west-2", GRACE_CUTOFF, out, segments=1,
                                     actions_table=ACTIONS)

        # Move expires_at to a DIFFERENT value that is still past the grace cutoff.
        new_ea = (NOW - timedelta(days=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert new_ea != EXPIRED_LONG, "test setup: new_ea must differ from original"
        ddb = boto3.resource("dynamodb", region_name="us-west-2")
        ddb.Table(TABLE).update_item(
            Key={"finding_id": "FIND-EAT-CHANGED"},
            UpdateExpression="SET expires_at = :new_ea",
            ExpressionAttributeValues={":new_ea": new_ea},
        )

        deleted, skipped_cond, _ = purge.apply_purge(TABLE, "us-west-2", out, sha,
                                                     rate_delay_secs=0, actions_table=ACTIONS)
        assert deleted == 0, (
            f"W1: row with moved expires_at (still past grace) must not be deleted; "
            f"got deleted={deleted}"
        )
        assert skipped_cond == 1, (
            f"W1: row with moved expires_at must be skipped; got skipped_cond={skipped_cond}"
        )

    @mock_aws
    def test_agent_id_changed_after_dry_run_is_skipped(self, tmp_path: Path) -> None:
        """Row whose agent_id changes after dry run is skipped at apply.

        Mutation guard (mutations-FA2.md § W1):
            Drop ``AND agent_id = :aid`` → condition no longer re-verifies
            agent_id; the row is deleted → deleted=1 → FAIL.
        """
        row = _make_row(finding_id="FIND-AID-CHANGED",
                        expires_at=EXPIRED_LONG, agent_id="agent-2-vehicle-health")
        _create_table([row])
        _create_actions_table([])
        out = str(tmp_path / "out.csv")
        sha, _, _, _ = purge.dry_run(TABLE, "us-west-2", GRACE_CUTOFF, out, segments=1,
                                     actions_table=ACTIONS)

        # Between dry-run and apply: agent_id changed
        ddb = boto3.resource("dynamodb", region_name="us-west-2")
        ddb.Table(TABLE).update_item(
            Key={"finding_id": "FIND-AID-CHANGED"},
            UpdateExpression="SET agent_id = :new_aid",
            ExpressionAttributeValues={":new_aid": "agent-tier2-other"},
        )

        deleted, skipped_cond, _ = purge.apply_purge(TABLE, "us-west-2", out, sha,
                                                     rate_delay_secs=0, actions_table=ACTIONS)
        assert deleted == 0, (
            f"W1: row with changed agent_id must not be deleted; got deleted={deleted}"
        )
        assert skipped_cond == 1, (
            f"W1: row with changed agent_id must be skipped; got skipped_cond={skipped_cond}"
        )


class TestGraceCutoff:
    """W3: grace cutoff must be enforced in the dry-run and CLI.

    These tests are RED until:
    1. is_eligible / dry_run properly handles rows inside the grace period.
    2. main() rejects --grace-days < 0.
    """

    def test_is_eligible_in_grace_returns_false(self) -> None:
        """A row with expires_at inside the grace period is not eligible."""
        row = _make_row(expires_at=EXPIRED_IN_GRACE)   # 5 days before NOW
        assert not purge.is_eligible(row, GRACE_CUTOFF), (
            "W3: row inside grace period must NOT be eligible; "
            f"expires_at={EXPIRED_IN_GRACE}, grace_cutoff={GRACE_CUTOFF}"
        )

    def test_is_eligible_unexpired_returns_false(self) -> None:
        """An unexpired row is not eligible."""
        row = _make_row(expires_at=UNEXPIRED)
        assert not purge.is_eligible(row, GRACE_CUTOFF), (
            "W3: unexpired row must NOT be eligible"
        )

    @mock_aws
    def test_dry_run_excludes_in_grace_row(self, tmp_path: Path) -> None:
        """Dry run with a real now-relative grace period does not list in-grace rows.

        Mutation guard (recorded in mutations-FA1.md): replace the grace
        cutoff with `now + timedelta(...)` → in-grace rows are listed, this
        test fails because the CSV is non-empty.
        """
        # Inject now so the grace cutoff is deterministic.
        now = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
        grace_days = 7
        cutoff = now - timedelta(days=grace_days)

        in_grace = _make_row(
            finding_id="FIND-GRACE",
            expires_at=(now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        unexpired = _make_row(
            finding_id="FIND-UNEXP",
            expires_at=(now + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        past_grace = _make_row(
            finding_id="FIND-PAST",
            expires_at=(now - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        _create_table([in_grace, unexpired, past_grace])
        _create_actions_table([])
        out = str(tmp_path / "out.csv")
        sha, by_agent, _, _ = purge.dry_run(TABLE, "us-west-2", cutoff, out, segments=1,
                                            actions_table=ACTIONS)

        total = sum(by_agent.values())
        assert total == 1, (
            f"W3: dry run must only list rows past grace; expected 1, got {total}"
        )
        with open(out) as f:
            rows = list(csv.DictReader(f))
        ids = {r["finding_id"] for r in rows}
        assert ids == {"FIND-PAST"}, f"W3: only FIND-PAST should be listed; got {ids}"

    def test_main_rejects_negative_grace_days(self) -> None:
        """--grace-days < 0 must exit with code 1.

        Mutation guard (recorded in mutations-FA1.md): remove the
        negative-grace-days check → main() proceeds and may list or delete
        rows from the future, but this test fails because rc == 0 instead of 1.
        """
        rc = purge.main(["--stage", "staging", "--region", "us-west-2",
                         "--grace-days", "-1", "--out", "/dev/null"])
        assert rc == 1, f"W3: --grace-days -1 must return exit code 1; got {rc}"

    @mock_aws
    def test_main_dry_run_grace_cutoff_driven_by_now(self, tmp_path: Path) -> None:
        """main()'s grace-cutoff computation is exercised end-to-end.

        RED: main() does not accept _now_fn yet → TypeError.

        Fix: add a ``_now_fn`` kwarg to main() so tests can inject a fixed instant.
        Then main() uses ``_now_fn()`` to build grace_cutoff instead of the real
        clock, making the mutation ``- timedelta(days=args.grace_days)``
        → ``+ timedelta(...)`` catchable through main().

        Mutation guards (mutations-FA2.md § W2):
            Replace ``- timedelta(days=args.grace_days)`` with ``+ ...`` in
            main() → in-grace and unexpired rows appear in CSV → total != 1.
            Set grace_days = 0 in main() → same outcome.
        """
        _now = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
        grace_days = 7

        in_grace = _make_row(
            finding_id="FIND-GRACE",
            expires_at=(_now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        unexpired = _make_row(
            finding_id="FIND-UNEXP",
            expires_at=(_now + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        past_grace = _make_row(
            finding_id="FIND-PAST",
            expires_at=(_now - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        _create_table([in_grace, unexpired, past_grace])
        _create_actions_table([])
        out = str(tmp_path / "out.csv")

        # Pass _now_fn to main() — TypeError until fix.
        rc = purge.main(
            ["--stage", "staging", "--region", "us-west-2",
             "--grace-days", str(grace_days), "--out", out, "--segments", "1"],
            _now_fn=lambda: _now,
        )
        assert rc == 0

        with open(out) as f:
            rows = list(csv.DictReader(f))
        ids = {r["finding_id"] for r in rows}
        assert ids == {"FIND-PAST"}, (
            f"W2: main() dry run must only list rows past grace; got {ids}"
        )


class TestNaiveTimestamp:
    """Suggestion: naive expires_at (no UTC offset) must be treated as malformed."""

    def test_naive_expires_at_not_eligible(self) -> None:
        """A row with a naive datetime string is not eligible (fail-safe: kept)."""
        row = _make_row(expires_at="2026-10-10T00:00:00")   # no '+00:00' or 'Z'
        assert not purge.is_eligible(row, GRACE_CUTOFF), (
            "Suggestion: naive expires_at must be treated as malformed (row kept)"
        )


# ── Rows that have an Action are never purged (FA1 Accept; decisions.md ~20:00Z) ──

ACTIONS = "vsa-staging-avx-actions"


def _create_actions_table(finding_ids: list[str]) -> None:
    """Create the actions table (key action_id) with one Action per finding id."""
    ddb = boto3.resource("dynamodb", region_name="us-west-2")
    _drop_if_exists(ddb, ACTIONS)
    table = ddb.create_table(
        TableName=ACTIONS,
        KeySchema=[{"AttributeName": "action_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "action_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    for i, fid in enumerate(finding_ids):
        table.put_item(Item={"action_id": f"ACT-{i}", "finding_id": fid, "decision": "approved"})


class TestRowsWithAnActionAreKept:
    """A decided Finding can look informational (its recommended_action removed by a
    later re-emit); the actions table is the only place that shows it was decided."""

    @mock_aws
    def test_dry_run_excludes_a_row_that_has_an_action(self, tmp_path: Path) -> None:
        _create_table([_make_row(finding_id="FIND-DECIDED"), _make_row(finding_id="FIND-PLAIN")])
        _create_actions_table(["FIND-DECIDED"])
        out = str(tmp_path / "out.csv")
        purge.dry_run(TABLE, "us-west-2", GRACE_CUTOFF, out, segments=1, actions_table=ACTIONS)
        ids = [r["finding_id"] for r in csv.DictReader(open(out, newline="", encoding="utf-8"))]
        assert ids == ["FIND-PLAIN"]

    @mock_aws
    def test_apply_skips_a_row_that_gained_an_action_after_the_dry_run(self, tmp_path: Path) -> None:
        _create_table([_make_row(finding_id="FIND-LATE")])
        _create_actions_table([])
        out = str(tmp_path / "out.csv")
        sha, _, _, _ = purge.dry_run(TABLE, "us-west-2", GRACE_CUTOFF, out, segments=1,
                                     actions_table=ACTIONS)
        boto3.resource("dynamodb", region_name="us-west-2").Table(ACTIONS).put_item(
            Item={"action_id": "ACT-LATE", "finding_id": "FIND-LATE", "decision": "declined"})
        deleted, skipped_cond, skipped_action = purge.apply_purge(TABLE, "us-west-2", out, sha, rate_delay_secs=0,
                                             actions_table=ACTIONS)
        assert (deleted, skipped_action) == (0, 1)
        item = boto3.resource("dynamodb", region_name="us-west-2").Table(TABLE).get_item(
            Key={"finding_id": "FIND-LATE"})
        assert "Item" in item

    @mock_aws
    def test_main_reads_the_stage_actions_table(self, tmp_path: Path) -> None:
        _create_table([_make_row(finding_id="FIND-DECIDED"), _make_row(finding_id="FIND-PLAIN")])
        _create_actions_table(["FIND-DECIDED"])
        out = str(tmp_path / "out.csv")
        rc = purge.main(["--stage", "staging", "--region", "us-west-2", "--out", out, "--segments", "1"])
        assert rc == 0
        ids = [r["finding_id"] for r in csv.DictReader(open(out, newline="", encoding="utf-8"))]
        assert ids == ["FIND-PLAIN"]

    @mock_aws
    def test_main_fails_closed_when_the_actions_table_cannot_be_read(self, tmp_path: Path) -> None:
        _create_table([_make_row(finding_id="FIND-PLAIN")])  # no actions table
        _drop_if_exists(boto3.resource("dynamodb", region_name="us-west-2"), ACTIONS)
        out = str(tmp_path / "out.csv")
        rc = purge.main(["--stage", "staging", "--region", "us-west-2", "--out", out, "--segments", "1"])
        assert rc == 1
        assert not Path(out).exists()

    @mock_aws
    def test_main_apply_uses_the_given_grace_days(self, tmp_path: Path) -> None:
        # Listed with --grace-days 7 (expired 12 days relative to _now), applied
        # with --grace-days 30: the apply-time cutoff must use 30, so the row
        # is skipped.  _now_fn is injected so the test is clock-independent.
        #
        # RED: main() and apply_purge() do not accept _now_fn / _now yet →
        # TypeError until fix.
        #
        # Mutation guard (mutations-FA2.md § W3): _now is fixed well before the real
        # clock, so an apply that ignores the injected now (real clock - 30 days, a
        # cutoff after expires_at) deletes the row and this test fails on any date.
        _now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        expires_at = (_now - timedelta(days=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _create_table([_make_row(finding_id="FIND-A", expires_at=expires_at)])
        _create_actions_table([])
        out = str(tmp_path / "out.csv")
        sha, _, _, _ = purge.dry_run(TABLE, "us-west-2",
                                     _now - timedelta(days=7), out, segments=1,
                                     actions_table=ACTIONS)
        rc = purge.main(["--stage", "staging", "--region", "us-west-2", "--apply",
                         "--from-list", out, "--expect-sha256", sha,
                         "--grace-days", "30", "--rate-delay", "0"],
                        _now_fn=lambda: _now)
        assert rc == 0
        item = boto3.resource("dynamodb", region_name="us-west-2").Table(TABLE).get_item(
            Key={"finding_id": "FIND-A"})
        assert "Item" in item


# ── The row list is readable by its owner only (security review cycle 1, S4) ──

def test_row_list_csv_is_owner_only(tmp_path: Path) -> None:
    out = str(tmp_path / "rows.csv")
    purge.write_csv([_make_row(finding_id="FIND-A")], out)
    assert os.stat(out).st_mode & 0o777 == 0o600
