#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Give pre-deploy informational AVX findings the writer's ``ttl`` (spec 2026-10-27-avx-findings-retention).

Rows written before the TTL deploy (2026-10-28 16:52Z) and never refreshed since carry no ``ttl``, so
DynamoDB TTL never deletes them and a manual purge pass would be needed after each one passes its
7-day grace. This script sets on each of them the ``ttl`` the writer would have set
(``agents.tier2.writer._compute_ttl``: epoch(``expires_at``) + 7 days), so TTL removes them on schedule.
A row whose ``ttl`` is already in the past is deleted on TTL's next pass, which is what the spec's rule
gives it. No new retention rule: eligibility is the writer's, plus the purge's guards.

Eligible = no ``ttl`` yet; ``_compute_ttl`` gives a value (no ``ttl_exempt``, no ``recommended_action``,
agent not recall-warranty or quality, ``expires_at`` a string that parses with a UTC offset); the
``recommended_action`` and ``ttl_exempt`` keys are absent; the ``finding_id`` has no row in the actions
table; and the row's agent is ``agent-fi-*`` or it has no ``owner_id`` / ``scope.owner_id`` attribute.
That last guard is why the spec's general rejection of a backfill does not apply here: each UpdateItem
is a stream MODIFY. The findings stream has two consumers. The auto-approver's event filter admits only
images carrying ``recommended_action.action_kind``, which the condition forbids. The broker
(``agents/supervisor/lambdas/avx_broker.py``) processes every MODIFY with no expiry check, but
``_process_finding`` returns ``fi_item`` for an ``agent-fi-*`` row and ``owner_absent`` for a row with
no owner before any read, write, card or push (checked at AVX ``c272fd55``; ``_is_fi_finding`` last
changed in ``8e35e3e1``, ``_resolve_owner_party_id`` in ``9fcb2581``).
``test_every_eligible_row_ends_at_a_broker_skip`` runs the broker on each eligible row shape, so a change
to the broker's owner resolution turns this script's tests red. CI does not run them: run
``scripts/tests/test_backfill_findings_ttl.py`` before any re-run, and re-read the deployed broker's
``CodeSha256`` against main's.
A row the guard leaves out is reported (``kept_owner``) and stays for the purge script.

Dry run (default), read-only: parallel Scan, CSV of the rows and their ``ttl`` (mode 0600), counts,
and the CSV's sha256.

    python3 scripts/backfill_findings_ttl.py --stage staging --region us-west-2 --out /tmp/ttl.csv

Apply: checks the sha256, recomputes every listed ``ttl`` from the listed row before the first write,
re-reads the actions table, then one conditioned UpdateItem per row (``SET ttl``). A failed condition
(row changed, gained a ``ttl`` or an owner, or is gone) is counted and skipped, never retried
unconditionally. Re-running the same list is safe.

    python3 scripts/backfill_findings_ttl.py --stage staging --region us-west-2 \\
        --apply --from-list /tmp/ttl.csv --expect-sha256 <hex>

Verify, read-only: counts the listed rows back (``ttl`` as listed, another ``ttl``, none, gone).

    python3 scripts/backfill_findings_ttl.py --stage staging --region us-west-2 --verify --from-list /tmp/ttl.csv

Never run --apply without the user's explicit go. Exit codes: 0 success; 1 any error.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import datetime
import hashlib
import os
import sys
import time
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

_CVX_ROOT = str(Path(__file__).resolve().parent.parent)
if _CVX_ROOT not in sys.path:
    sys.path.insert(0, _CVX_ROOT)

from agents.tier2.writer import _compute_ttl  # noqa: E402  the writer's own rule

CSV_FIELDS: list[str] = [
    "finding_id", "agent_id", "finding_kind", "fleet_id", "vin", "expires_at", "ttl",
]
FI_AGENT_PREFIX = "agent-fi-"
DEFAULT_SEGMENTS = 8
DEFAULT_RATE_DELAY_SECS = 0.05

# The UpdateItem's condition: the same facts the dry run listed, re-checked at write time.
CONDITION = (
    "attribute_exists(finding_id)"
    " AND attribute_not_exists(#t)"
    " AND attribute_not_exists(recommended_action)"
    " AND attribute_not_exists(ttl_exempt)"
    " AND agent_id = :aid"
    " AND expires_at = :eat"
    " AND (begins_with(agent_id, :fi)"
    " OR (attribute_not_exists(#own) AND attribute_not_exists(#scope.#own)))"
)


# ── Eligibility ───────────────────────────────────────────────────────────────

def _has_owner_attribute(row: dict) -> bool:
    """True when the row carries ``owner_id`` or ``scope.owner_id`` at all (any value)."""
    if "owner_id" in row:
        return True
    scope = row.get("scope")
    return isinstance(scope, dict) and "owner_id" in scope


def _writer_ttl(row: dict) -> int | None:
    """The writer's ``ttl`` for ``row``, or ``None``; a non-string ``expires_at`` is malformed (kept).

    ``_compute_ttl`` calls ``expires_at.replace``, which raises on a stored Number or Map, so the type
    is checked here first (review p365 c1 W4).
    """
    if not isinstance(row.get("expires_at"), str):
        return None
    return _compute_ttl(row)


def ttl_for(row: dict) -> int | None:
    """The ``ttl`` to set on ``row``, or ``None`` when the row must be left alone.

    Presence, not value, is tested for every attribute the UpdateItem condition tests with
    ``attribute_not_exists`` (``ttl``, ``recommended_action``, ``ttl_exempt``, the owner fields), so
    the dry-run list holds only rows the apply can write.
    """
    if "ttl" in row or "recommended_action" in row or "ttl_exempt" in row:
        return None
    agent = row.get("agent_id")
    if not isinstance(agent, str) or not agent:
        return None  # the condition's agent_id = :aid could never hold
    if not agent.startswith(FI_AGENT_PREFIX) and _has_owner_attribute(row):
        return None
    return _writer_ttl(row)


# ── Reads ─────────────────────────────────────────────────────────────────────

def _scan_all(table_name: str, region: str, segments: int) -> list[dict]:
    def _segment(segment: int) -> list[dict]:
        table = boto3.session.Session().resource("dynamodb", region_name=region).Table(table_name)
        out: list[dict] = []
        kw: dict[str, Any] = {"TotalSegments": segments, "Segment": segment}
        while True:
            resp = table.scan(**kw)
            out.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                return out
            kw["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    rows: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=segments) as ex:
        for fut in [ex.submit(_segment, i) for i in range(segments)]:
            rows.extend(fut.result())
    return rows


def read_acted_finding_ids(actions_table: str, region: str) -> set[str]:
    """Every ``finding_id`` named in the actions table (all pages). Raises on any read error."""
    table = boto3.resource("dynamodb", region_name=region).Table(actions_table)
    acted: set[str] = set()
    kw: dict[str, Any] = {"ProjectionExpression": "finding_id"}
    while True:
        resp = table.scan(**kw)
        acted.update(i["finding_id"] for i in resp.get("Items", [])
                     if isinstance(i.get("finding_id"), str) and i["finding_id"])
        if "LastEvaluatedKey" not in resp:
            return acted
        kw["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


# ── CSV ───────────────────────────────────────────────────────────────────────

def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_csv(rows: list[dict], path: str) -> str:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(path, 0o600)  # an existing file keeps its old mode through O_CREAT
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in sorted(rows, key=lambda x: x["finding_id"]):
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})
    return _sha256(path)


def _read_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ── Dry run ───────────────────────────────────────────────────────────────────

def dry_run(table_name: str, region: str, out_path: str, *, actions_table: str,
            segments: int = DEFAULT_SEGMENTS,
            now: datetime.datetime | None = None) -> tuple[str, dict[str, Any]]:
    """Scan, list every eligible row with its ``ttl``; return (sha256, report). Writes nothing to AWS."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    acted = read_acted_finding_ids(actions_table, region)
    listed: list[dict] = []
    kept_action = kept_owner = 0
    for row in _scan_all(table_name, region, segments):
        ttl = ttl_for(row)
        if ttl is None:
            if ("ttl" not in row and "recommended_action" not in row and "ttl_exempt" not in row
                    and _has_owner_attribute(row) and _writer_ttl(row) is not None):
                kept_owner += 1
            continue
        if row.get("finding_id") in acted:
            kept_action += 1
            continue
        listed.append({**{k: row.get(k, "") for k in CSV_FIELDS}, "ttl": ttl})
    sha = _write_csv(listed, out_path)
    now_s = int(now.timestamp())
    report: dict[str, Any] = {
        "eligible": len(listed), "kept_action": kept_action, "kept_owner": kept_owner,
        "ttl_already_past": sum(1 for r in listed if r["ttl"] <= now_s),
        "ttl_min": min((r["ttl"] for r in listed), default=None),
        "ttl_max": max((r["ttl"] for r in listed), default=None),
        "by_agent_kind": {}, "by_fleet": {},
    }
    for r in listed:
        ak = f"{r['agent_id']} {r['finding_kind']}"
        report["by_agent_kind"][ak] = report["by_agent_kind"].get(ak, 0) + 1
        if r["fleet_id"]:
            report["by_fleet"][r["fleet_id"]] = report["by_fleet"].get(r["fleet_id"], 0) + 1
    return sha, report


# ── Apply ─────────────────────────────────────────────────────────────────────

def _checked_rows(path: str) -> list[dict]:
    """The listed rows, each ``ttl`` recomputed from the listed facts. Raises before any write."""
    rows = _read_csv(path)
    for r in rows:
        expected = ttl_for({"finding_id": r["finding_id"], "agent_id": r["agent_id"],
                            "expires_at": r["expires_at"]})
        if expected is None or str(expected) != r["ttl"]:
            raise ValueError(
                f"listed ttl for {r['finding_id']} is {r['ttl']!r}; the rule gives {expected!r}. "
                "Re-run the dry run.")
    return rows


def apply_backfill(table_name: str, region: str, from_list: str, expect_sha256: str, *,
                   actions_table: str,
                   rate_delay_secs: float = DEFAULT_RATE_DELAY_SECS) -> dict[str, int]:
    """Set ``ttl`` on the listed rows. Returns counts {set, skipped_condition, skipped_action}.

    Raises ValueError on a sha mismatch or a listed ``ttl`` the rule does not give, before any
    write; raises ClientError on any AWS error other than a failed condition.
    """
    actual = _sha256(from_list)
    if actual != expect_sha256:
        raise ValueError(f"SHA256 mismatch: expected {expect_sha256!r}, got {actual!r}. "
                         "Re-run the dry run.")
    rows = _checked_rows(from_list)
    acted = read_acted_finding_ids(actions_table, region)
    table = boto3.resource("dynamodb", region_name=region).Table(table_name)
    counts = {"set": 0, "skipped_condition": 0, "skipped_action": 0}
    for idx, r in enumerate(rows, start=1):
        if r["finding_id"] in acted:
            counts["skipped_action"] += 1
        else:
            try:
                table.update_item(
                    Key={"finding_id": r["finding_id"]},
                    UpdateExpression="SET #t = :ttl",
                    ConditionExpression=CONDITION,
                    ExpressionAttributeNames={"#t": "ttl", "#own": "owner_id", "#scope": "scope"},
                    ExpressionAttributeValues={
                        ":ttl": int(r["ttl"]), ":aid": r["agent_id"], ":eat": r["expires_at"],
                        ":fi": FI_AGENT_PREFIX,
                    },
                )
                counts["set"] += 1
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                    raise
                counts["skipped_condition"] += 1
        if idx % 500 == 0 or idx == len(rows):
            print(f"  apply progress: {idx}/{len(rows)} {counts}", flush=True)
        if rate_delay_secs > 0:
            time.sleep(rate_delay_secs)
    return counts


# ── Verify ────────────────────────────────────────────────────────────────────

def verify(table_name: str, region: str, from_list: str) -> dict[str, int]:
    """Count the listed rows back. Read-only."""
    rows = _read_csv(from_list)
    want = {r["finding_id"]: r["ttl"] for r in rows}
    client = boto3.client("dynamodb", region_name=region)
    got: dict[str, Any] = {}
    ids = list(want)
    for i in range(0, len(ids), 100):
        req: dict | None = {table_name: {
            "Keys": [{"finding_id": {"S": x}} for x in ids[i:i + 100]],
            "ProjectionExpression": "finding_id, #t", "ExpressionAttributeNames": {"#t": "ttl"}}}
        delay = 0.1
        while req:
            resp = client.batch_get_item(RequestItems=req)
            for it in resp["Responses"].get(table_name, []):
                got[it["finding_id"]["S"]] = it.get("ttl", {}).get("N")
            req = resp.get("UnprocessedKeys") or None
            if req:
                time.sleep(delay)  # throttled: back off before re-asking for the rest
                delay = min(delay * 2, 5.0)
    out = {"listed": len(rows), "ttl_as_listed": 0, "ttl_other": 0, "no_ttl": 0, "gone": 0}
    for fid, ttl in want.items():
        if fid not in got:
            out["gone"] += 1
        elif got[fid] is None:
            out["no_ttl"] += 1
        elif got[fid] == ttl:
            out["ttl_as_listed"] += 1
        else:
            out["ttl_other"] += 1
    return out


# ── CLI ───────────────────────────────────────────────────────────────────────

def _iso(epoch: int | None) -> str:
    if epoch is None:
        return "-"
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Dry run (default) / --apply / --verify; see the module docstring.")
    ap.add_argument("--stage", default="staging")
    ap.add_argument("--region", default="us-west-2")
    ap.add_argument("--out", metavar="CSV", help="dry run: where to write the list")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--from-list", metavar="CSV")
    ap.add_argument("--expect-sha256", metavar="HEX")
    ap.add_argument("--segments", type=int, default=DEFAULT_SEGMENTS, help=argparse.SUPPRESS)
    ap.add_argument("--rate-delay", type=float, default=DEFAULT_RATE_DELAY_SECS, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    table = f"vsa-{args.stage}-avx-findings"
    actions = f"vsa-{args.stage}-avx-actions"
    try:
        if args.apply and args.verify:
            print("error: --apply and --verify are separate steps", file=sys.stderr)
            return 1
        if args.apply:
            if not args.from_list or not args.expect_sha256:
                print("error: --apply requires --from-list and --expect-sha256", file=sys.stderr)
                return 1
            c = apply_backfill(table, args.region, args.from_list, args.expect_sha256,
                               actions_table=actions, rate_delay_secs=args.rate_delay)
            print(f"set={c['set']}  skipped_condition={c['skipped_condition']}  "
                  f"skipped_action={c['skipped_action']}")
        elif args.verify:
            if not args.from_list:
                print("error: --verify requires --from-list", file=sys.stderr)
                return 1
            v = verify(table, args.region, args.from_list)
            print("  ".join(f"{k}={n}" for k, n in v.items()))
        else:
            if not args.out:
                print("error: --out is required for a dry run", file=sys.stderr)
                return 1
            sha, rep = dry_run(table, args.region, args.out, actions_table=actions,
                               segments=args.segments)
            print(f"eligible={rep['eligible']}  sha256={sha}")
            print(f"  csv: {args.out}")
            print(f"  kept_action={rep['kept_action']}  kept_owner={rep['kept_owner']}  "
                  f"ttl_already_past={rep['ttl_already_past']}  "
                  f"ttl {_iso(rep['ttl_min'])} .. {_iso(rep['ttl_max'])}")
            for k, n in sorted(rep["by_agent_kind"].items()):
                print(f"  {k}: {n}")
            for k, n in sorted(rep["by_fleet"].items(), key=lambda kv: -kv[1]):
                print(f"  fleet {k}: {n}")
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
