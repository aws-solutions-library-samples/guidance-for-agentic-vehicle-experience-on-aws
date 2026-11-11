#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Purge expired informational AVX findings (spec 2026-10-27-avx-findings-retention, B).

Dry run (default) — parallel Scan; writes every eligible row to a CSV and prints
counts plus the CSV's sha256.  Nothing is deleted.

    python3 scripts/purge_expired_avx_findings.py \\
        --stage staging --region us-west-2 [--grace-days 7] --out /tmp/to_delete.csv

Apply — verifies the CSV's sha256 against the caller's --expect-sha256, then deletes
exactly the listed rows with a conditioned DeleteItem.  A condition failure (row changed
or already gone) is counted and skipped, never retried as unconditional.

    python3 scripts/purge_expired_avx_findings.py \\
        --stage staging --region us-west-2 \\
        --apply --from-list /tmp/to_delete.csv --expect-sha256 <hex>

Never run --apply without the user's explicit go.  The parent prompt holds that gate.

Exit codes: 0 success · 1 any error (including sha mismatch, missing flags).
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

# ── Constants ──────────────────────────────────────────────────────────────────

# agent_id values whose rows are kept regardless of expires_at (spec B).
KEPT_AGENTS: frozenset[str] = frozenset({"agent-fi-recall-warranty", "agent-fi-quality"})

# CSV output columns, in this order.
CSV_FIELDS: list[str] = [
    "finding_id", "agent_id", "finding_kind", "fleet_id",
    "vin", "computed_at", "expires_at",
]

DEFAULT_GRACE_DAYS: int = 7
DEFAULT_SEGMENTS: int = 8
DEFAULT_RATE_DELAY_SECS: float = 0.05  # ~20 deletes/s, well within DDB limits


# ── Eligibility ───────────────────────────────────────────────────────────────

def _parse_expires_at(raw: Any) -> datetime.datetime | None:
    """Parse an ISO-8601 UTC string; return None on any parse failure.

    Naive datetimes (no UTC offset) are treated as malformed: comparing them
    with the timezone-aware grace_cutoff would raise TypeError, and we cannot
    safely assume a timezone for an ambiguous value.  Fail-safe: row is kept.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            # Naive datetime — treat as malformed (fail-safe: row kept).
            return None
        return dt
    except ValueError:
        return None


def is_eligible(row: dict, grace_cutoff: datetime.datetime) -> bool:
    """True iff this row should be purged.

    Eligible: no ``recommended_action`` attribute, ``agent_id`` not in
    ``KEPT_AGENTS``, ``ttl_exempt`` not set (FA3 Suggestion 1: a row that was
    once an FI item is kept indefinitely regardless of its current
    ``recommended_action``), and ``expires_at`` parses to a datetime ≤
    ``grace_cutoff``.

    Failure-safe: a malformed or missing ``expires_at`` returns False (row kept).
    """
    if "recommended_action" in row:
        return False
    if row.get("agent_id", "") in KEPT_AGENTS:
        return False
    # FA3 Suggestion 1: the sticky ttl_exempt marker means the row was once an
    # FI queue item (writer.py's permanent-exemption rule).  The actions table
    # has no TTL so decided rows stay protected by the Action guard; but a row
    # that lost its recommended_action before the writer deployed (or whose
    # Action was never written) is also permanently kept by this flag.  Purging
    # it would contradict the writer's own retention promise.
    if row.get("ttl_exempt"):
        return False
    dt = _parse_expires_at(row.get("expires_at", ""))
    if dt is None:
        return False
    return dt <= grace_cutoff


# ── Scan ──────────────────────────────────────────────────────────────────────

def scan_eligible(
    table_name: str,
    region: str,
    grace_cutoff: datetime.datetime,
    segments: int = DEFAULT_SEGMENTS,
) -> list[dict]:
    """Parallel Scan; return rows that pass is_eligible.

    Each segment creates its own boto3 resource to avoid thread-safety issues
    (boto3 resources are not documented as thread-safe).  Each segment follows
    LastEvaluatedKey to completion before returning its slice.
    """
    def _scan_segment(segment: int) -> list[dict]:
        # Create a session per thread: the default boto3 session is not
        # thread-safe (session credentials are lazily initialised on first use
        # under a race when called from multiple threads simultaneously).
        sess = boto3.session.Session()
        ddb_thread = sess.resource("dynamodb", region_name=region)
        table_thread = ddb_thread.Table(table_name)
        items: list[dict] = []
        kwargs: dict[str, Any] = {
            "TotalSegments": segments,
            "Segment": segment,
        }
        while True:
            resp = table_thread.scan(**kwargs)
            for item in resp.get("Items", []):
                if is_eligible(item, grace_cutoff):
                    items.append(item)
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return items

    eligible: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=segments) as executor:
        futures = [executor.submit(_scan_segment, i) for i in range(segments)]
        for future in concurrent.futures.as_completed(futures):
            eligible.extend(future.result())
    return eligible


# ── CSV helpers ───────────────────────────────────────────────────────────────

def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def write_csv(rows: list[dict], out_path: str) -> str:
    """Write rows sorted by finding_id to a CSV readable by its owner only; return its sha256.

    The list names VINs and fleets, so the file is created with mode 0600 (security review
    cycle 1, S4) rather than the process umask.
    """
    sorted_rows = sorted(rows, key=lambda r: r.get("finding_id", ""))
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(out_path, 0o600)  # an existing file keeps its old mode through O_CREAT
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in sorted_rows:
            writer.writerow({k: row.get(k, "") for k in CSV_FIELDS})
    return _sha256(out_path)


def _read_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ── Findings with an Action ───────────────────────────────────────────────────

def read_acted_finding_ids(actions_table: str, region: str) -> set[str]:
    """Every ``finding_id`` that has a row in the actions table (full Scan, all pages).

    A decided Finding can look informational: a later re-emit may have removed its
    ``recommended_action``. The actions table is the only record that it was decided,
    so any Finding named there is kept. Raises on any read error (the caller fails closed).
    """
    table = boto3.resource("dynamodb", region_name=region).Table(actions_table)
    acted: set[str] = set()
    kwargs: dict[str, Any] = {"ProjectionExpression": "finding_id"}
    while True:
        resp = table.scan(**kwargs)
        for item in resp.get("Items", []):
            fid = item.get("finding_id")
            if isinstance(fid, str) and fid:
                acted.add(fid)
        if "LastEvaluatedKey" not in resp:
            return acted
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


# ── Dry run ───────────────────────────────────────────────────────────────────

def dry_run(
    table_name: str,
    region: str,
    grace_cutoff: datetime.datetime,
    out_path: str,
    *,
    segments: int = DEFAULT_SEGMENTS,
    actions_table: str,
) -> tuple[str, dict[str, int], dict[str, int], dict[str, int]]:
    """Scan, write CSV, return (sha256, by_agent, by_kind, by_fleet).

    Rows whose ``finding_id`` has an Action are left out and reported separately.
    ``actions_table`` is required: it names the stage's actions table so the guard
    is always active (not opt-in).
    """
    rows = scan_eligible(table_name, region, grace_cutoff, segments)
    acted = read_acted_finding_ids(actions_table, region)
    kept_action = [r for r in rows if r.get("finding_id") in acted]
    if kept_action:
        print(f"  kept {len(kept_action)} otherwise-eligible row(s) that have an Action",
              flush=True)
    rows = [r for r in rows if r.get("finding_id") not in acted]
    sha = write_csv(rows, out_path)

    by_agent: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    by_fleet: dict[str, int] = {}
    for row in rows:
        agent = row.get("agent_id", "")
        kind = row.get("finding_kind", "")
        fleet = row.get("fleet_id", "")
        by_agent[agent] = by_agent.get(agent, 0) + 1
        by_kind[kind] = by_kind.get(kind, 0) + 1
        if fleet:
            by_fleet[fleet] = by_fleet.get(fleet, 0) + 1

    return sha, by_agent, by_kind, by_fleet


# ── Apply ─────────────────────────────────────────────────────────────────────

def apply_purge(
    table_name: str,
    region: str,
    from_list: str,
    expect_sha256: str,
    *,
    rate_delay_secs: float = DEFAULT_RATE_DELAY_SECS,
    grace_days: int = DEFAULT_GRACE_DAYS,
    actions_table: str,
    _now: datetime.datetime | None = None,
) -> tuple[int, int, int]:
    """Delete rows listed in the CSV after verifying its sha256.

    Returns (deleted_count, skipped_condition, skipped_action).
    skipped_condition: rows where the ConditionExpression failed (changed/gone).
    skipped_action: rows skipped because they now have an Action (decided Finding).
    Neither is ever retried as an unconditional delete.

    Raises ValueError on sha mismatch.
    Raises ClientError for any non-condition AWS error (caller gets exit 1).

    ``grace_days``: used to compute an apply-time cutoff for defence-in-depth:
    ``expires_at <= :cutoff`` is added to the condition so a row whose expiry
    moved back inside the grace window between the dry run and the apply is
    skipped rather than deleted.  ISO Z strings compare correctly as strings.

    ``actions_table``: required — names the stage's actions table so the
    Action-guard is always active (not opt-in for a library caller).

    ``_now``: injectable datetime for tests; defaults to the real UTC clock.
    """
    actual_sha = _sha256(from_list)
    if actual_sha != expect_sha256:
        raise ValueError(
            f"SHA256 mismatch: expected {expect_sha256!r}, got {actual_sha!r}. "
            "Re-run the dry-run to regenerate the list."
        )

    # Apply-time cutoff: re-compute from _now (or the real clock) so rows that
    # regained freshness between dry-run and apply are skipped rather than deleted.
    _apply_now = _now if _now is not None else datetime.datetime.now(datetime.timezone.utc)
    apply_cutoff = (
        _apply_now - datetime.timedelta(days=grace_days)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    rows = _read_csv(from_list)
    # Re-read at apply time: an Action written since the dry run keeps its Finding.
    acted = read_acted_finding_ids(actions_table, region)
    ddb = boto3.resource("dynamodb", region_name=region)
    table = ddb.Table(table_name)

    deleted = 0
    skipped_condition = 0
    skipped_action = 0
    total = len(rows)
    for idx, row in enumerate(rows, start=1):
        if row["finding_id"] in acted:
            skipped_action += 1  # has an Action now: kept, never deleted
            continue
        try:
            table.delete_item(
                Key={"finding_id": row["finding_id"]},
                ConditionExpression=(
                    "attribute_not_exists(recommended_action)"
                    " AND agent_id = :aid"
                    " AND expires_at = :eat"
                    " AND expires_at <= :cutoff"
                ),
                ExpressionAttributeValues={
                    ":aid": row["agent_id"],
                    ":eat": row["expires_at"],
                    ":cutoff": apply_cutoff,
                },
            )
            deleted += 1
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                skipped_condition += 1  # row changed or already gone
            else:
                raise  # propagate unexpected errors
        if idx % 1000 == 0 or idx == total:
            print(
                f"  apply progress: {idx}/{total} rows — "
                f"deleted={deleted} skipped_condition={skipped_condition} "
                f"skipped_action={skipped_action}",
                flush=True,
            )
        if rate_delay_secs > 0:
            time.sleep(rate_delay_secs)

    return deleted, skipped_condition, skipped_action


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None, *, _now_fn: Any = None) -> int:
    """Entry point.  Returns an exit code.

    ``_now_fn``: optional callable ``() -> datetime`` injected by tests so the
    grace-cutoff computation and the apply-cutoff computation both use a fixed
    instant.  Defaults to ``datetime.datetime.now(datetime.timezone.utc)``.
    """
    ap = argparse.ArgumentParser(
        description=(
            "Dry run (default): scan and write eligible rows to --out CSV.\n"
            "Apply: --apply --from-list <csv> --expect-sha256 <hex>"
        )
    )
    ap.add_argument("--stage", default="staging",
                    help="Deployment stage (default: %(default)s)")
    ap.add_argument("--region", default="us-west-2", help="AWS region")
    ap.add_argument("--grace-days", type=int, default=DEFAULT_GRACE_DAYS,
                    help="Grace period in days (default: %(default)s)")
    ap.add_argument("--out", metavar="CSV",
                    help="Output CSV path (required for dry run)")
    ap.add_argument("--apply", action="store_true",
                    help="Execute deletions (default: dry run)")
    ap.add_argument("--from-list", metavar="CSV",
                    help="CSV from a previous dry run (required with --apply)")
    ap.add_argument("--expect-sha256", metavar="HEX",
                    help="sha256 of the CSV (required with --apply)")
    # Hidden flags for testability
    ap.add_argument("--segments", type=int, default=DEFAULT_SEGMENTS,
                    help=argparse.SUPPRESS)
    ap.add_argument("--rate-delay", type=float, default=DEFAULT_RATE_DELAY_SECS,
                    help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.grace_days < 0:
        print("error: --grace-days must be >= 0", file=sys.stderr)
        return 1

    table_name = f"vsa-{args.stage}-avx-findings"
    actions_table = f"vsa-{args.stage}-avx-actions"
    _now = (_now_fn if _now_fn is not None else
            lambda: datetime.datetime.now(datetime.timezone.utc))()
    grace_cutoff = _now - datetime.timedelta(days=args.grace_days)

    try:
        if args.apply:
            if not args.from_list or not args.expect_sha256:
                print(
                    "error: --apply requires both --from-list and --expect-sha256",
                    file=sys.stderr,
                )
                return 1
            deleted, skipped_cond, skipped_action = apply_purge(
                table_name, args.region, args.from_list, args.expect_sha256,
                rate_delay_secs=args.rate_delay,
                grace_days=args.grace_days,
                actions_table=actions_table,
                _now=_now,
            )
            print(f"deleted={deleted}  skipped_condition={skipped_cond}  skipped_action={skipped_action}")
        else:
            if not args.out:
                print("error: --out is required for dry run", file=sys.stderr)
                return 1
            sha, by_agent, by_kind, by_fleet = dry_run(
                table_name, args.region, grace_cutoff, args.out,
                segments=args.segments,
                actions_table=actions_table,
            )
            total = sum(by_agent.values())
            print(f"eligible={total}  sha256={sha}")
            print(f"  csv: {args.out}")
            if by_agent:
                print("by agent:")
                for agent, count in sorted(by_agent.items()):
                    print(f"  {agent}: {count}")
            if by_kind:
                print("by kind:")
                for kind, count in sorted(by_kind.items()):
                    print(f"  {kind}: {count}")
            if by_fleet:
                print("by fleet (top 20):")
                for fleet, count in sorted(by_fleet.items(), key=lambda kv: -kv[1])[:20]:
                    print(f"  {fleet}: {count}")

    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
