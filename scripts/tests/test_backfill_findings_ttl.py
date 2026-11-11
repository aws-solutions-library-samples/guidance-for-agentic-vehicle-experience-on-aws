# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests for scripts/backfill_findings_ttl.py (spec 2026-10-27-avx-findings-retention, prompt 365).

The script gives a ``ttl`` to the pre-deploy informational rows the writer never touched, using the
writer's own rule (``agents.tier2.writer._compute_ttl``), so DynamoDB TTL removes them with nobody
acting. Every test runs against moto's in-memory DynamoDB; no AWS call is made.

Commit history:
  RED   — this file committed before the module exists (ImportError = red)
  GREEN — backfill_findings_ttl.py created; all tests pass

Mutations recorded in .kiro/specs/2026-10-27-avx-findings-retention/mutations-P365.md.
"""
from __future__ import annotations

import csv
import hashlib
import os
import stat
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import boto3
import pytest

_CVX_ROOT = Path(__file__).resolve().parents[2]
if str(_CVX_ROOT) not in sys.path:
    sys.path.insert(0, str(_CVX_ROOT))

from moto import mock_aws  # noqa: E402

import scripts.backfill_findings_ttl as bf  # noqa: E402
from agents.tier2.writer import _compute_ttl  # noqa: E402

TABLE = "vsa-staging-avx-findings"
ACTIONS = "vsa-staging-avx-actions"
REGION = "us-west-2"

IN_GRACE = "2026-11-05T02:47:19Z"   # expired, inside the 7-day grace on 2026-11-09
LIVE = "2026-12-01T04:00:48Z"       # not yet expired
PAST_GRACE = "2026-11-01T00:00:00Z"  # expired more than 7 days ago


def _epoch(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


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


def _row(fid: str = "FIND-001", **kw: Any) -> dict:
    """A baseline eligible row: informational health finding, no owner, expired inside grace."""
    row: dict = {
        "finding_id": fid,
        "agent_id": "agent-2-vehicle-health",
        "finding_kind": "health.brakes",
        "severity": "attention",
        "vin": "MRDN0000001000001",
        "computed_at": "2026-10-27T04:00:00Z",
        "expires_at": IN_GRACE,
        "scope": {"vin": "MRDN0000001000001"},
    }
    row.update(kw)
    return {k: v for k, v in row.items() if v is not None}


def _drop(ddb, name: str) -> None:
    try:
        ddb.meta.client.delete_table(TableName=name)
    except ddb.meta.client.exceptions.ResourceNotFoundException:
        pass


def _tables(items: list[dict], acted: tuple[str, ...] = ()) -> Any:
    """Create the findings and actions tables in the current moto context; return the findings Table."""
    ddb = boto3.resource("dynamodb", region_name=REGION)
    for name in (TABLE, ACTIONS):
        _drop(ddb, name)
    key = "finding_id"
    ft = ddb.create_table(
        TableName=TABLE, BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}])
    at = ddb.create_table(
        TableName=ACTIONS, BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "action_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "action_id", "AttributeType": "S"}])
    for it in items:
        ft.put_item(Item=it)
    for i, fid in enumerate(acted):
        at.put_item(Item={"action_id": f"ACT-{i}", "finding_id": fid})
    return ft


def _get(ft, fid: str) -> dict | None:
    return ft.get_item(Key={"finding_id": fid}).get("Item")


def _sha(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _dry(tmp_path: Path, name: str = "list.csv") -> tuple[str, str, dict]:
    out = str(tmp_path / name)
    sha, report = bf.dry_run(TABLE, REGION, out, actions_table=ACTIONS, segments=2)
    return out, sha, report


def _apply(path: str, sha: str, **kw: Any) -> dict:
    return bf.apply_backfill(TABLE, REGION, path, sha, actions_table=ACTIONS,
                             rate_delay_secs=0, **kw)


# ── Eligibility (pure) ────────────────────────────────────────────────────────

class TestTtlFor:
    """``ttl_for(row)`` returns the writer's ttl for an eligible row, else ``None``."""

    def test_in_grace_row_gets_writers_ttl(self) -> None:
        row = _row()
        assert bf.ttl_for(row) == _epoch(IN_GRACE) + 604_800
        assert bf.ttl_for(row) == _compute_ttl(row)

    def test_live_row_gets_writers_ttl(self) -> None:
        assert bf.ttl_for(_row(expires_at=LIVE)) == _epoch(LIVE) + 604_800

    def test_past_grace_row_gets_a_ttl_already_in_the_past(self) -> None:
        # The spec's rule gives it expires_at + 7 d; TTL then deletes it on its next pass.
        assert bf.ttl_for(_row(expires_at=PAST_GRACE)) == _epoch(PAST_GRACE) + 604_800

    def test_fi_cost_informational_row_is_eligible(self) -> None:
        row = _row(agent_id="agent-fi-cost", finding_kind="fleet.cost.anomaly", expires_at=LIVE)
        assert bf.ttl_for(row) == _epoch(LIVE) + 604_800

    def test_fi_row_with_an_owner_is_eligible(self) -> None:
        # The broker skips every agent-fi-* row before it reads the owner.
        row = _row(agent_id="agent-fi-cost", owner_id="0b1f6c1e-1111-2222-3333-444455556666")
        assert bf.ttl_for(row) == _epoch(IN_GRACE) + 604_800

    def test_row_with_ttl_is_not_touched(self) -> None:
        assert bf.ttl_for(_row(ttl=Decimal(123))) is None

    def test_recommended_action_present_is_kept(self) -> None:
        assert bf.ttl_for(_row(recommended_action={"action_kind": "x"})) is None

    def test_recommended_action_key_with_null_value_is_kept(self) -> None:
        row = _row()
        row["recommended_action"] = None
        assert bf.ttl_for(row) is None

    def test_ttl_exempt_is_kept(self) -> None:
        assert bf.ttl_for(_row(ttl_exempt=True)) is None

    @pytest.mark.parametrize("agent", ["agent-fi-recall-warranty", "agent-fi-quality"])
    def test_kept_agents_are_kept(self, agent: str) -> None:
        assert bf.ttl_for(_row(agent_id=agent)) is None

    @pytest.mark.parametrize("raw", ["", "not-a-date", "2026-11-05T02:47:19", None,
                                     Decimal("1794451639"), {"S": IN_GRACE}],
                             ids=["empty", "garbage", "naive", "missing", "number", "map"])
    def test_malformed_or_naive_expires_at_is_kept(self, raw: Any) -> None:
        for base in (_row(), _row(owner_id="CUST-0000ABCD")):
            row = dict(base)
            if raw is None:
                row.pop("expires_at")
            else:
                row["expires_at"] = raw
            assert bf.ttl_for(row) is None

    def test_ttl_exempt_false_is_kept(self) -> None:
        # The condition tests presence (attribute_not_exists), so the list does too.
        assert bf.ttl_for(_row(ttl_exempt=False)) is None

    @pytest.mark.parametrize("agent", [None, "", 7])
    def test_missing_or_non_string_agent_is_kept(self, agent: Any) -> None:
        row = _row()
        if agent is None:
            row.pop("agent_id")
        else:
            row["agent_id"] = agent
        assert bf.ttl_for(row) is None

    def test_non_fi_row_with_top_level_owner_is_kept(self) -> None:
        # A MODIFY would reach the broker's owner gates: left for the purge.
        assert bf.ttl_for(_row(owner_id="CUST-0000ABCD")) is None

    def test_non_fi_row_with_scope_owner_is_kept(self) -> None:
        assert bf.ttl_for(_row(scope={"owner_id": "0b1f6c1e-1111-2222-3333-444455556666"})) is None

    def test_non_fi_row_with_an_empty_owner_attribute_is_kept(self) -> None:
        # The guard is on the attribute's presence, as the UpdateItem condition is
        # (attribute_not_exists), so the two can never disagree.
        assert bf.ttl_for(_row(owner_id="")) is None
        assert bf.ttl_for(_row(scope={"owner_id": ""})) is None

    def test_non_fi_row_whose_scope_has_no_owner_is_eligible(self) -> None:
        assert bf.ttl_for(_row(scope={"vin": "X"})) == _epoch(IN_GRACE) + 604_800
        row = _row()
        row.pop("scope")
        assert bf.ttl_for(row) == _epoch(IN_GRACE) + 604_800


# ── The broker skips every row the script would write (review p365 c1 W1) ─────

def _load_broker() -> Any:
    import importlib.util
    path = _CVX_ROOT / "agents" / "supervisor" / "lambdas" / "avx_broker.py"
    spec = importlib.util.spec_from_file_location("avx_broker_for_backfill_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_UUID = "0b1f6c1e-1111-2222-3333-444455556666"
# Every shape the backfill may meet: agent family x owner placement x owner value.
_BROKER_SHAPES = [
    pytest.param(dict(agent_id=a, **o), id=f"{a}-{oid}")
    for a in ("agent-2-vehicle-health", "agent-fi-cost", "agent-fi-rebalancing", "agent-dms-prebrief")
    for oid, o in (
        ("no-owner", {}),
        ("scope-vin-only", {"scope": {"vin": "MRDN0000001000001"}}),
        ("no-scope", {"scope": None}),
        ("fleet-scope", {"scope": {"fleet_id": "demo-rental-carshare"}, "fleet_id": "demo-rental-carshare"}),
        ("top-owner-uuid", {"owner_id": _UUID}),
        ("top-owner-cust", {"owner_id": "CUST-0000ABCD"}),
        ("scope-owner-uuid", {"scope": {"owner_id": _UUID}}),
        ("empty-owners", {"owner_id": "", "scope": {"owner_id": ""}}),
    )
]


@pytest.mark.parametrize("shape", _BROKER_SHAPES)
def test_every_eligible_row_ends_at_a_broker_skip(shape: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """A backfill UpdateItem is a stream MODIFY; the broker must stop before any read, write or push."""
    broker = _load_broker()

    def _no_aws(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the broker reached AWS for a row the backfill would write")

    for getter in ("_get_ddb", "_get_ssm", "_get_sns"):
        monkeypatch.setattr(broker, getter, _no_aws)
    # Any AWS call, however the client was built, fails the test before it leaves the process.
    import botocore.client
    monkeypatch.setattr(botocore.client.BaseClient, "_make_api_call", _no_aws)
    monkeypatch.setattr(boto3, "client", _no_aws)
    monkeypatch.setattr(boto3, "resource", _no_aws)
    row = _row(**shape)
    policy = {"apns_threshold": "routine", "rate_limit": {"day": 99, "hour": 99}, "cardinality_cap": 99}
    for severity in ("routine", "attention", "soon", "p0"):
        row["severity"] = severity
        ttl = bf.ttl_for(row)
        if ttl is None:
            continue
        # The MODIFY's NewImage carries the new ttl.
        result = broker._process_finding({**row, "ttl": int(ttl)}, policy)  # the broker deserialises N to int
        assert result["action"] == "skipped"
        assert result["skip_reason"] in {"fi_item", "owner_absent"}, (shape, severity, result)


def test_the_broker_shape_list_holds_written_rows_of_both_kinds() -> None:
    """Guard the test above against a shape list that the backfill never writes."""
    written = [p.values[0] for p in _BROKER_SHAPES if bf.ttl_for(_row(**p.values[0])) is not None]
    assert any(s["agent_id"].startswith("agent-fi-") and ("owner_id" in s or "owner_id" in (s.get("scope") or {}))
               for s in written)
    assert any(not s["agent_id"].startswith("agent-fi-") for s in written)


# ── Dry run ───────────────────────────────────────────────────────────────────

@mock_aws
def test_dry_run_lists_exactly_the_eligible_rows(tmp_path: Path) -> None:
    items = [
        _row("A-in-grace"),
        _row("B-live", expires_at=LIVE, agent_id="agent-fi-cost", finding_kind="fleet.cost.anomaly"),
        _row("C-past-grace", expires_at=PAST_GRACE),
        _row("D-has-ttl", ttl=Decimal(1)),
        _row("E-fi-item", recommended_action={"action_kind": "x"}),
        _row("F-exempt", ttl_exempt=True),
        _row("G-recall", agent_id="agent-fi-recall-warranty"),
        _row("H-quality", agent_id="agent-fi-quality"),
        _row("I-malformed", expires_at="bad"),
        _row("J-owner", owner_id="CUST-0000ABCD"),
        _row("K-acted"),
    ]
    _tables(items, acted=("K-acted",))
    out, sha, report = _dry(tmp_path)
    listed = list(csv.DictReader(open(out)))
    assert [r["finding_id"] for r in listed] == ["A-in-grace", "B-live", "C-past-grace"]
    assert listed[0]["ttl"] == str(_epoch(IN_GRACE) + 604_800)
    assert listed[1]["ttl"] == str(_epoch(LIVE) + 604_800)
    assert listed[0]["expires_at"] == IN_GRACE and listed[0]["agent_id"] == "agent-2-vehicle-health"
    assert sha == _sha(out)
    assert report["eligible"] == 3
    assert report["kept_action"] == 1
    assert report["kept_owner"] == 1
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600


@mock_aws
def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    ft = _tables([_row("A"), _row("B", expires_at=LIVE)])
    before = sorted(map(repr, ft.scan()["Items"]))
    _dry(tmp_path)
    assert sorted(map(repr, ft.scan()["Items"])) == before


@mock_aws
def test_dry_run_report_counts_exactly(tmp_path: Path) -> None:
    """The user's go rests on the printed counts (review p365 c1 W5)."""
    now = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)
    items = [
        _row("A", fleet_id="demo-rental-carshare"),                       # ttl 11-12, past? no
        _row("B", expires_at=PAST_GRACE, fleet_id="demo-rental-carshare"),  # ttl 11-08: already past
        _row("C", expires_at=LIVE, agent_id="agent-fi-cost", finding_kind="fleet.cost.anomaly",
             fleet_id="demo-lastmile-delivery"),
        _row("D", finding_kind="health.tires"),                            # no fleet
        _row("E-owner", owner_id="CUST-0000ABCD"),                         # kept_owner
        _row("F-owner-bad-exp", owner_id="CUST-0000ABCD", expires_at="bad"),  # not kept_owner
        _row("G-owner-decimal-exp", owner_id="CUST-0000ABCD", expires_at=Decimal(5)),  # not kept_owner
        _row("H-owner-fi-item", owner_id="CUST-0000ABCD", recommended_action={"k": "v"}),  # not kept_owner
    ]
    _tables(items)
    out = str(tmp_path / "r.csv")
    _, rep = bf.dry_run(TABLE, REGION, out, actions_table=ACTIONS, segments=2, now=now)
    ttl_a, ttl_b, ttl_c = (_epoch(IN_GRACE) + 604_800, _epoch(PAST_GRACE) + 604_800, _epoch(LIVE) + 604_800)
    assert rep["eligible"] == 4
    assert rep["kept_owner"] == 1
    assert rep["kept_action"] == 0
    assert rep["ttl_already_past"] == 1
    assert rep["ttl_min"] == ttl_b and rep["ttl_max"] == ttl_c
    assert ttl_a > int(now.timestamp())
    assert rep["by_agent_kind"] == {
        "agent-2-vehicle-health health.brakes": 2,
        "agent-2-vehicle-health health.tires": 1,
        "agent-fi-cost fleet.cost.anomaly": 1,
    }
    assert rep["by_fleet"] == {"demo-rental-carshare": 2, "demo-lastmile-delivery": 1}


@mock_aws
def test_dry_run_does_not_crash_on_an_owner_row_with_a_number_expires_at(tmp_path: Path) -> None:
    _tables([_row("A"), _row("B", owner_id="CUST-0000ABCD", expires_at=Decimal(5)),
             _row("C", expires_at={"M": "x"})])
    out, _, rep = _dry(tmp_path)
    assert [r["finding_id"] for r in csv.DictReader(open(out))] == ["A"]
    assert rep["kept_owner"] == 0


# ── Apply ─────────────────────────────────────────────────────────────────────

@mock_aws
def test_apply_sets_the_listed_ttl_and_nothing_else(tmp_path: Path) -> None:
    ft = _tables([_row("A"), _row("B", expires_at=LIVE), _row("Z-fi", recommended_action={"k": "v"})])
    out, sha, _ = _dry(tmp_path)
    ft.put_item(Item=_row("C-new-after-dry-run"))  # not listed: must stay without ttl
    res = _apply(out, sha)
    assert res == {"set": 2, "skipped_condition": 0, "skipped_action": 0}
    assert _get(ft, "A")["ttl"] == _epoch(IN_GRACE) + 604_800
    assert _get(ft, "B")["ttl"] == _epoch(LIVE) + 604_800
    assert "ttl" not in _get(ft, "Z-fi")
    assert "ttl" not in _get(ft, "C-new-after-dry-run")
    a = _get(ft, "A")
    assert {k: v for k, v in a.items() if k != "ttl"} == _row("A")


@mock_aws
def test_apply_refuses_a_sha_mismatch_and_writes_nothing(tmp_path: Path) -> None:
    ft = _tables([_row("A")])
    out, _, _ = _dry(tmp_path)
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        _apply(out, "0" * 64)
    assert "ttl" not in _get(ft, "A")


@mock_aws
def test_apply_refuses_a_list_whose_ttl_disagrees_with_the_rule(tmp_path: Path) -> None:
    ft = _tables([_row("A"), _row("B")])
    out, _, _ = _dry(tmp_path)
    rows = list(csv.DictReader(open(out)))
    rows[1]["ttl"] = str(int(rows[1]["ttl"]) + 86_400 * 365)
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=bf.CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)
    with pytest.raises(ValueError, match="ttl"):
        _apply(out, _sha(out))
    assert "ttl" not in _get(ft, "A") and "ttl" not in _get(ft, "B")


@mock_aws
def test_apply_refuses_a_listed_row_the_rule_now_keeps(tmp_path: Path) -> None:
    ft = _tables([_row("A")])
    out, _, _ = _dry(tmp_path)
    rows = list(csv.DictReader(open(out)))
    rows[0]["agent_id"] = "agent-fi-recall-warranty"
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=bf.CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)
    with pytest.raises(ValueError, match="ttl"):
        _apply(out, _sha(out))
    assert "ttl" not in _get(ft, "A")


@pytest.mark.parametrize("change", [
    "expires_at_moved", "gained_recommended_action", "gained_ttl_exempt", "gained_ttl",
    "gained_owner_id", "gained_scope_owner", "agent_changed", "deleted",
])
@mock_aws
def test_a_row_changed_after_the_dry_run_is_skipped(tmp_path: Path, change: str) -> None:
    ft = _tables([_row("A"), _row("B")])
    out, sha, _ = _dry(tmp_path)
    updates = {
        "expires_at_moved": ("SET expires_at = :v", {":v": LIVE}),
        "gained_recommended_action": ("SET recommended_action = :v", {":v": {"action_kind": "x"}}),
        "gained_ttl_exempt": ("SET ttl_exempt = :v", {":v": True}),
        "gained_ttl": ("SET #t = :v", {":v": 42}),
        "gained_owner_id": ("SET owner_id = :v", {":v": "CUST-0000ABCD"}),
        "gained_scope_owner": ("SET #s.owner_id = :v", {":v": "0b1f6c1e-1111-2222-3333-444455556666"}),
        "agent_changed": ("SET agent_id = :v", {":v": "agent-other"}),
    }
    if change == "deleted":
        ft.delete_item(Key={"finding_id": "B"})
    else:
        expr, vals = updates[change]
        kw: dict = {"Key": {"finding_id": "B"}, "UpdateExpression": expr, "ExpressionAttributeValues": vals}
        names = {n: a for n, a in (("#t", "ttl"), ("#s", "scope")) if n in expr}
        if names:
            kw["ExpressionAttributeNames"] = names
        ft.update_item(**kw)
    before_b = _get(ft, "B")
    res = _apply(out, sha)
    assert res == {"set": 1, "skipped_condition": 1, "skipped_action": 0}
    assert _get(ft, "A")["ttl"] == _epoch(IN_GRACE) + 604_800
    assert _get(ft, "B") == before_b  # untouched (or still gone)


@mock_aws
def test_a_row_decided_after_the_dry_run_is_skipped(tmp_path: Path) -> None:
    ft = _tables([_row("A"), _row("B")])
    out, sha, _ = _dry(tmp_path)
    boto3.resource("dynamodb", region_name=REGION).Table(ACTIONS).put_item(
        Item={"action_id": "ACT-late", "finding_id": "B"})
    res = _apply(out, sha)
    assert res == {"set": 1, "skipped_condition": 0, "skipped_action": 1}
    assert "ttl" not in _get(ft, "B")


@mock_aws
def test_apply_is_resumable(tmp_path: Path) -> None:
    ft = _tables([_row("A"), _row("B", expires_at=LIVE)])
    out, sha, _ = _dry(tmp_path)
    assert _apply(out, sha)["set"] == 2
    again = _apply(out, sha)
    assert again == {"set": 0, "skipped_condition": 2, "skipped_action": 0}
    assert _get(ft, "B")["ttl"] == _epoch(LIVE) + 604_800


@mock_aws
def test_apply_writes_an_fi_row_that_carries_owner_fields(tmp_path: Path) -> None:
    """The condition's FI branch admits agent-fi-* rows with an owner (the broker skips them as fi_item)."""
    ft = _tables([_row("F", agent_id="agent-fi-cost", finding_kind="fleet.cost.anomaly", expires_at=LIVE,
                       owner_id=_UUID, scope={"owner_id": _UUID})])
    out, sha, _ = _dry(tmp_path)
    assert _apply(out, sha) == {"set": 1, "skipped_condition": 0, "skipped_action": 0}
    assert _get(ft, "F")["ttl"] == _epoch(LIVE) + 604_800


@mock_aws
def test_apply_raises_on_a_non_condition_error(tmp_path: Path) -> None:
    from botocore.exceptions import ClientError
    _tables([_row("A")])
    out, sha, _ = _dry(tmp_path)
    with pytest.raises(ClientError) as ei:
        bf.apply_backfill("vsa-missing-table", REGION, out, sha, actions_table=ACTIONS,
                          rate_delay_secs=0)
    assert ei.value.response["Error"]["Code"] == "ResourceNotFoundException"


# ── Verify (count back) ───────────────────────────────────────────────────────

@mock_aws
def test_verify_counts_back(tmp_path: Path) -> None:
    ft = _tables([_row("A"), _row("B"), _row("C"), _row("D")])
    out, sha, _ = _dry(tmp_path)
    _apply(out, sha)
    ft.update_item(Key={"finding_id": "B"}, UpdateExpression="SET #t = :v",
                   ExpressionAttributeNames={"#t": "ttl"}, ExpressionAttributeValues={":v": 7})
    ft.update_item(Key={"finding_id": "C"}, UpdateExpression="REMOVE #t",
                   ExpressionAttributeNames={"#t": "ttl"})
    ft.delete_item(Key={"finding_id": "D"})
    assert bf.verify(TABLE, REGION, out) == {
        "listed": 4, "ttl_as_listed": 1, "ttl_other": 1, "no_ttl": 1, "gone": 1}


# ── CLI ───────────────────────────────────────────────────────────────────────

@mock_aws
def test_main_dry_run_then_apply_then_verify(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    ft = _tables([_row("A"), _row("B", expires_at=LIVE)])
    out = str(tmp_path / "l.csv")
    assert bf.main(["--stage", "staging", "--region", REGION, "--out", out, "--segments", "2"]) == 0
    printed = capsys.readouterr().out
    assert f"sha256={_sha(out)}" in printed and "eligible=2" in printed
    assert bf.main(["--stage", "staging", "--region", REGION, "--apply", "--from-list", out,
                    "--expect-sha256", _sha(out), "--rate-delay", "0"]) == 0
    assert "set=2" in capsys.readouterr().out
    assert bf.main(["--stage", "staging", "--region", REGION, "--verify", "--from-list", out]) == 0
    assert "ttl_as_listed=2" in capsys.readouterr().out
    assert _get(ft, "A")["ttl"] == _epoch(IN_GRACE) + 604_800


@pytest.mark.parametrize("argv", [
    ["--apply"],
    ["--apply", "--from-list", "x.csv"],
    ["--apply", "--expect-sha256", "ab"],
    ["--verify"],
    [],
])
def test_main_refuses_missing_flags(argv: list[str]) -> None:
    assert bf.main(["--stage", "staging", "--region", REGION, *argv]) == 1


@mock_aws
def test_main_apply_with_wrong_sha_exits_1(tmp_path: Path) -> None:
    ft = _tables([_row("A")])
    out = str(tmp_path / "l.csv")
    assert bf.main(["--stage", "staging", "--region", REGION, "--out", out, "--segments", "2"]) == 0
    assert bf.main(["--stage", "staging", "--region", REGION, "--apply", "--from-list", out,
                    "--expect-sha256", "f" * 64]) == 1
    assert "ttl" not in _get(ft, "A")
