#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
scrub_avx_findings_vin.py — move AVX rows from the retired demo VINs to their MRDN VINs.

Part of the CMS re-VIN migration (ADP spec ``2026-09-30-cms-adp-convergence-stage1``,
§ D14; task 2.7, run in Group 3 as task 3.2c).

Tables touched:
  vsa-{stage}-avx-findings     — top-level ``vin``; for vin-scoped findings also
                                  ``scope.vin`` and ``finding_key`` (recomputed with the
                                  writer's own ``compute_finding_key`` over the new VIN)
  vsa-{stage}-triage-decisions — top-level ``vin``

When to run (decisions.md rulings 15 and 20):
  1. The four ``vsa-staging-tier2-*`` EventBridge schedules are disabled.
  2. The CMS anchor flip and the ADP rename have both landed.
  3. Run this script with ``--apply``, and confirm it exits 0.
  4. Only then re-enable the schedules.
  If a schedule runs before step 3, a writer can create a live finding under the new
  VIN's key. The script then refuses (exit 5) rather than leave two live findings on one
  ``finding_key``, which the broker would show as two cards.

Safety:
  * Dry run by default; ``--apply`` writes.
  * The read-only guard is installed on the boto3 default session at start-up, before
    any client exists, so in a dry run every non-read call raises before it is signed
    or sent (decisions.md ruling 8; escalation process lesson). Exit code 4 if it fires.
  * Stage must be ``staging``; table names must start with ``vsa-staging-``.
  * The TSV is validated before any AWS call: one row per vehicle, one-to-one, every
    old VIN a retired demo shape and every new VIN equal to the re-VIN map's value.
  * Every update is conditional on the row still holding the old VIN; a failed
    condition is counted (``changed_concurrently``), not fatal by itself.
  * Every finding update is a stream MODIFY that the AVX broker evaluates. Before any
    write the script checks, with the broker's own functions, that no old-VIN finding
    belongs to an app user without a card (the only case the broker could card or push).
    On 2026-10-02 none did: 13 owners were registry ids and 1 had no owner.

Input: header-less TSV, ``vehicleId<TAB>oldVin<TAB>newVin``, one row per vehicle (the
file CMS ``revin_non_mrd_meridian.write_vin_remap_tsv`` writes). Blank lines and lines
starting with ``#`` are ignored.

Files, in ``--baseline-dir`` (default /tmp), one per surface (``avx_findings``,
``triage_decisions``), per decisions.md ruling 21:
  dry run   revin_dryrun_<surface>.json        counts and planned moves; never a baseline
  --apply   revin_prebaseline_<surface>.json   written once, before the first write, with
                                               ``"applied": true``; a later run never
                                               overwrites it
            revin_resume_<surface>_<UTC>.json  a later --apply run's own starting counts,
                                               written alongside the original
            revin_postcount_<surface>.json     the latest --apply run's result

Invariant after --apply, per vehicle and table (index reads retried for lag):
  post(old) == 0  and  post(new) == pre(old) + pre(new)      (this run's pre-counts)
  post(new) >= original pre(old) + original pre(new)          (nothing lost since the
                                                               first run's baseline)
Triage rows have a 30-day TTL. Each run fixes a horizon of now + 24 h. Rows whose ttl
falls before it are moved but not counted, in pre and post alike (rows with no ttl are
always counted), and the check against the original baseline counts only the original
rows with no ttl or a ttl beyond the same horizon.
Because both sides use one horizon, a TTL deletion never reads as a lost row.

Exit codes:
  0  success (or dry run complete with nothing blocking an apply)
  1  bad input or refused precondition (stage, TSV, baseline file)
  2  table-name guard refused
  3  invariant failed after --apply
  4  the read-only guard refused a write during a dry run
  5  conflict: a live finding already holds a new key, a row cannot be keyed, or the
     broker could notify on the update; nothing is written if found before the writes
  6  AWS error (throttling, access, network); after --apply, re-run to resume

Usage:
  python3 scripts/scrub_avx_findings_vin.py --tsv-file <map.tsv> [--expected-rows 93]
  python3 scripts/scrub_avx_findings_vin.py --tsv-file <map.tsv> --expected-rows 93 --apply
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import boto3  # type: ignore[import-untyped]
from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-untyped]

# Make agents.* and scripts.* importable whether run as a script or imported.
_CVX_ROOT = str(Path(__file__).resolve().parent.parent)
if _CVX_ROOT not in sys.path:
    sys.path.insert(0, _CVX_ROOT)

from agents.supervisor.lambdas import avx_broker  # noqa: E402
from agents.tier2.writer import compute_finding_key  # noqa: E402
from scripts import readonly_guard  # noqa: E402

log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

ALLOWED_STAGE = "staging"
TABLE_PREFIX = "vsa-staging-"
FINDINGS_TABLE_SUFFIX = "avx-findings"
TRIAGE_TABLE_SUFFIX = "triage-decisions"
ACTIONS_TABLE_SUFFIX = "avx-actions"
FINDINGS_VIN_GSI = "by_vin"
FINDING_KEY_GSI = "by_finding_key"
DEFAULT_REGION = "us-west-2"
DEFAULT_BASELINE_DIR = "/tmp"
SURFACE_FINDINGS = "avx_findings"
SURFACE_TRIAGE = "triage_decisions"
SCRIPT_NAME = "scrub_avx_findings_vin.py"

# Index (GSI) post-counts can lag the writes. Re-read up to this many times.
POST_COUNT_ATTEMPTS = 4
POST_COUNT_DELAY_S = 2.0

# Triage rows carry a DynamoDB TTL (``ttl``, 30 days). A row whose ttl falls before
# now + this margin may be deleted by TTL at any moment, so it is moved but not counted:
# its disappearance must not read as a lost row.
TTL_MARGIN_S = 24 * 3600

EXIT_OK = 0
EXIT_INPUT = 1
EXIT_TABLE_GUARD = 2
EXIT_INVARIANT = 3
EXIT_READONLY = 4
EXIT_CONFLICT = 5
EXIT_AWS = 6

# The re-VIN map (CMS ``deployment/scripts/revin_map.py``, decisions.md decision 1),
# vendored so the TSV is checked against the rule here too. Legacy prefixes are spelled
# in pieces so the retired VIN shapes never appear literally in this file.
_LEGACY_BLOCKS: tuple[tuple[str, int, int], ...] = (
    ("CS" + "PR", 13, 1_000_000),
    ("DE" + "MO", 13, 2_000_000),
    ("5YJ" + "3E1EA1PF", 6, 3_000_000),
)
_NEW_VIN_RE = re.compile(r"^MRDN[0-9]{13}$")


class InputError(ValueError):
    """Bad TSV, bad arguments, or a baseline file that must not be used."""


class TableGuardError(RuntimeError):
    """A table name outside ``vsa-staging-*``."""


class ConflictError(RuntimeError):
    """Moving a row would leave two live findings on one ``finding_key``."""


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class VinRow:
    vehicle_id: str
    old_vin: str
    new_vin: str


@dataclass
class SurfaceCounts:
    pre_old: int = 0
    pre_new: int = 0
    post_old: int = 0
    post_new: int = 0
    would_update: int = 0
    updated: int = 0
    changed_concurrently: int = 0
    # Triage only: rows moved but not counted because their TTL is near, and the ttl of
    # every counted pre row (old and new), so a later run can discount TTL deletions.
    excluded_expiring: int = 0
    ttls: list[int | None] | None = None


@dataclass
class FindingMove:
    vehicle_id: str
    finding_id: str
    old_vin: str
    new_vin: str
    vin_scoped: bool
    new_key: str | None
    live: bool


@dataclass
class TriageMove:
    vehicle_id: str
    session_id: str
    old_vin: str
    new_vin: str


@dataclass
class Plan:
    findings: dict[str, SurfaceCounts] = field(default_factory=dict)
    triage: dict[str, SurfaceCounts] = field(default_factory=dict)
    finding_moves: list[FindingMove] = field(default_factory=list)
    triage_moves: list[TriageMove] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


# ── Guards and input ───────────────────────────────────────────────────────────

def assert_staging_table(table_name: str) -> None:
    """Refuse any table whose name does not start with ``vsa-staging-``."""
    if not table_name.startswith(TABLE_PREFIX):
        raise TableGuardError(
            f"table {table_name!r} does not start with {TABLE_PREFIX!r}; refused"
        )


def expected_new_vin(old_vin: str) -> str | None:
    """The re-VIN map's MRDN VIN for ``old_vin``, or None if it is not a retired shape."""
    for prefix, ndigits, offset in _LEGACY_BLOCKS:
        tail = old_vin[len(prefix):]
        if old_vin.startswith(prefix) and len(tail) == ndigits and tail.isdigit():
            seq = int(tail)
            if 0 < seq < 1_000_000:
                return f"MRDN{offset + seq:013d}"
    return None


def load_tsv(path: str) -> list[VinRow]:
    rows: list[VinRow] = []
    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.rstrip("\n")
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) != 3:
                raise InputError(
                    f"TSV line {lineno}: expected 3 tab-separated columns, got {len(parts)}"
                )
            vehicle_id, old_vin, new_vin = (p.strip() for p in parts)
            if not vehicle_id or not old_vin or not new_vin:
                raise InputError(f"TSV line {lineno}: empty column")
            rows.append(VinRow(vehicle_id=vehicle_id, old_vin=old_vin, new_vin=new_vin))
    return rows


def validate_rows(rows: list[VinRow], expected_rows: int | None) -> None:
    """Refuse a TSV that could silently merge, skip or mis-map vehicles."""
    if not rows:
        raise InputError("TSV has no data rows")
    if expected_rows is not None and len(rows) != expected_rows:
        raise InputError(f"TSV has {len(rows)} data rows; expected {expected_rows}")
    seen: dict[str, set[str]] = {"vehicleId": set(), "oldVin": set(), "newVin": set()}
    for n, row in enumerate(rows, 1):
        for col, value in (("vehicleId", row.vehicle_id), ("oldVin", row.old_vin),
                           ("newVin", row.new_vin)):
            if value in seen[col]:
                raise InputError(f"TSV data row {n}: duplicate {col}")
            seen[col].add(value)
        if not _NEW_VIN_RE.match(row.new_vin):
            raise InputError(f"TSV data row {n}: newVin is not MRDN + 13 digits")
        want = expected_new_vin(row.old_vin)
        if want is None:
            raise InputError(f"TSV data row {n}: oldVin is not a retired demo VIN shape")
        if want != row.new_vin:
            raise InputError(f"TSV data row {n}: newVin differs from the re-VIN map")
    if seen["oldVin"] & seen["newVin"]:
        raise InputError("TSV maps a VIN that is also another row's target")


def mapping_digest(rows: list[VinRow]) -> str:
    canonical = "\n".join(
        sorted(f"{r.vehicle_id}\t{r.old_vin}\t{r.new_vin}" for r in rows)
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def make_session(region: str, *, apply: bool) -> Any:
    """Return the boto3 default session; read-only unless ``apply``.

    The guard goes on the session before any client is created, so every client made
    from it — including any later ``boto3.client(...)`` call — inherits it.
    """
    boto3.setup_default_session(region_name=region)
    session = boto3.DEFAULT_SESSION
    if not apply:
        readonly_guard.install(session)
    return session


def epoch_now() -> int:
    return int(time.time())


def utc_now() -> str:
    """ISO-8601 UTC with a ``Z``, the format the Tier 2 writer compares ``expires_at`` to."""
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


# ── Readers (all paginated to completion) ──────────────────────────────────────

def _paged(call: Any, kwargs: dict[str, Any]) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    while True:
        resp = call(**kwargs)
        if not isinstance(resp, dict):
            raise TypeError("unexpected DynamoDB response shape")
        pages.append(resp)
        last = resp.get("LastEvaluatedKey")
        if not last:
            return pages
        if not isinstance(last, dict):
            raise TypeError("unexpected LastEvaluatedKey shape")
        kwargs = {**kwargs, "ExclusiveStartKey": last}


def query_vin_items(ddb: Any, table: str, vin: str) -> list[dict[str, Any]]:
    pages = _paged(ddb.query, {
        "TableName": table,
        "IndexName": FINDINGS_VIN_GSI,
        "KeyConditionExpression": "#vin = :vin",
        "ExpressionAttributeNames": {"#vin": "vin"},
        "ExpressionAttributeValues": {":vin": {"S": vin}},
    })
    return [item for page in pages for item in page.get("Items", [])]


def query_vin_count(ddb: Any, table: str, vin: str) -> int:
    pages = _paged(ddb.query, {
        "TableName": table,
        "IndexName": FINDINGS_VIN_GSI,
        "KeyConditionExpression": "#vin = :vin",
        "ExpressionAttributeNames": {"#vin": "vin"},
        "ExpressionAttributeValues": {":vin": {"S": vin}},
        "Select": "COUNT",
    })
    return sum(page.get("Count", 0) for page in pages)


def live_finding_ids_for_key(ddb: Any, table: str, finding_key: str, now: str) -> set[str]:
    """``finding_id``s of live rows on ``finding_key`` — the writer's own lookup."""
    pages = _paged(ddb.query, {
        "TableName": table,
        "IndexName": FINDING_KEY_GSI,
        "KeyConditionExpression": "finding_key = :fk",
        "FilterExpression": "expires_at > :now",
        "ExpressionAttributeValues": {":fk": {"S": finding_key}, ":now": {"S": now}},
    })
    return {
        item.get("finding_id", {}).get("S", "")
        for page in pages for item in page.get("Items", [])
    }


def _ttl(item: dict[str, Any]) -> int | None:
    raw = item.get("ttl", {}).get("N")
    try:
        return int(float(raw)) if raw is not None else None
    except ValueError:
        return None


def scan_triage_vins(ddb: Any, table: str) -> dict[str, list[tuple[str, int | None]]]:
    """One consistent scan of the triage table: ``vin`` → ``(sessionId, ttl)`` pairs."""
    pages = _paged(ddb.scan, {
        "TableName": table,
        "ConsistentRead": True,
        "ProjectionExpression": "#sid, #vin, #ttl",
        "ExpressionAttributeNames": {"#sid": "sessionId", "#vin": "vin", "#ttl": "ttl"},
    })
    by_vin: dict[str, list[tuple[str, int | None]]] = {}
    for page in pages:
        for item in page.get("Items", []):
            vin = item.get("vin", {}).get("S")
            sid = item.get("sessionId", {}).get("S")
            if vin and sid:
                by_vin.setdefault(vin, []).append((sid, _ttl(item)))
    return by_vin


def counted(entries: list[tuple[str, int | None]], horizon: int) -> list[tuple[str, int | None]]:
    """Rows TTL cannot delete before ``horizon`` (no ttl, or ttl after it)."""
    return [e for e in entries if e[1] is None or e[1] > horizon]


# ── Read-only plan ─────────────────────────────────────────────────────────────

def build_plan(ddb: Any, findings_table: str, triage_table: str, actions_table: str,
               rows: list[VinRow], now: str, horizon: int) -> Plan:
    """Count, list the moves and find conflicts. Reads only."""
    assert_staging_table(findings_table)
    assert_staging_table(triage_table)
    assert_staging_table(actions_table)
    plan = Plan()

    for row in rows:
        items = query_vin_items(ddb, findings_table, row.old_vin)
        counts = SurfaceCounts(
            pre_old=len(items),
            pre_new=query_vin_count(ddb, findings_table, row.new_vin),
        )
        plan.findings[row.vehicle_id] = counts
        for item in items:
            finding_id = item.get("finding_id", {}).get("S", "")
            if not finding_id:
                plan.conflicts.append(f"vehicle {row.vehicle_id}: a finding has no finding_id")
                continue
            vin_scoped = bool(item.get("scope", {}).get("M", {}).get("vin"))
            new_key = None
            if vin_scoped:
                agent_id = item.get("agent_id", {}).get("S", "")
                finding_kind = item.get("finding_kind", {}).get("S", "")
                if not agent_id or not finding_kind:
                    plan.conflicts.append(
                        f"finding {finding_id}: vin-scoped but agent_id or finding_kind "
                        "is empty, so its key cannot be recomputed"
                    )
                    continue
                new_key = compute_finding_key(
                    agent_id=agent_id, scope_field="vin",
                    scope_value=row.new_vin, finding_kind=finding_kind,
                )
            if _broker_would_notify(ddb, actions_table, item):
                plan.conflicts.append(
                    f"finding {finding_id}: its owner is an app user with no card yet, so "
                    "the AVX broker would evaluate it on this update and could create a card "
                    "and send a push. Stop and escalate: only a data decision clears this "
                    "(write the card row, which suppresses the card, or clear owner_id)"
                )
                continue
            expires_at = item.get("expires_at", {}).get("S", "")
            plan.finding_moves.append(FindingMove(
                vehicle_id=row.vehicle_id, finding_id=finding_id,
                old_vin=row.old_vin, new_vin=row.new_vin,
                vin_scoped=vin_scoped, new_key=new_key, live=expires_at > now,
            ))

    # Duplicate-live-finding check (review cycle 2 W3): a live vin-scoped row may move
    # only if no other live row holds, or will hold, its new key.
    live_moves = [m for m in plan.finding_moves if m.live and m.new_key]
    by_key: dict[str, list[str]] = {}
    for move in live_moves:
        by_key.setdefault(move.new_key, []).append(move.finding_id)  # type: ignore[arg-type]
    for key, ids in by_key.items():
        if len(ids) > 1:
            plan.conflicts.append(
                f"findings {', '.join(sorted(ids))} would share one live finding_key "
                f"{key[:12]}…"
            )
        others = live_finding_ids_for_key(ddb, findings_table, key, now) - set(ids)
        if others:
            plan.conflicts.append(
                f"finding {ids[0]}: live finding(s) {', '.join(sorted(others))} already "
                f"hold its new finding_key {key[:12]}…, so a Tier 2 writer ran before this "
                "scrub (rulings 15, 20). Resolve by hand: keep the finding the owner's card "
                "points at, expire the other (set expires_at in the past), then re-run"
            )

    triage_by_vin = scan_triage_vins(ddb, triage_table)
    for row in rows:
        olds = triage_by_vin.get(row.old_vin, [])
        old_counted = counted(olds, horizon)
        new_counted = counted(triage_by_vin.get(row.new_vin, []), horizon)
        plan.triage[row.vehicle_id] = SurfaceCounts(
            pre_old=len(old_counted), pre_new=len(new_counted),
            excluded_expiring=len(olds) - len(old_counted),
            ttls=[t for _sid, t in old_counted + new_counted],
        )
        for sid, _ttl_value in olds:  # every old row moves, counted or not
            plan.triage_moves.append(TriageMove(
                vehicle_id=row.vehicle_id, session_id=sid,
                old_vin=row.old_vin, new_vin=row.new_vin,
            ))
    return plan


def _broker_would_notify(ddb: Any, actions_table: str, item: dict[str, Any]) -> bool:
    """True if updating this finding could make the AVX broker card or push it.

    The broker (``agents/supervisor/lambdas/avx_broker.py``) processes every stream
    MODIFY. It skips a finding with no owner, an owner that is not an app user (a
    Cognito sub), or one whose card already exists. Only an app-user owner with no
    card reaches its consent and rate gates. Uses the broker's own functions, so the
    two cannot disagree about which card is this finding's.
    """
    finding = avx_broker._deserialise_ddb_item(item)
    owner = avx_broker._resolve_owner_party_id(finding)
    if not owner or not avx_broker._COGNITO_SUB_RE.match(owner):
        return False
    action_id, _kind, _created_by = avx_broker._card_identity(finding, owner)
    resp = ddb.get_item(TableName=actions_table, Key={"action_id": {"S": action_id}},
                        ProjectionExpression="action_id", ConsistentRead=True)
    return "Item" not in resp


# ── Writes ─────────────────────────────────────────────────────────────────────

def scrub_findings(ddb: Any, table: str, plan: Plan, dry_run: bool) -> None:
    assert_staging_table(table)
    for move in plan.finding_moves:
        counts = plan.findings[move.vehicle_id]
        if dry_run:
            counts.would_update += 1
            continue
        if move.live and move.new_key:
            # Re-check just before the write: the plan's check was minutes ago.
            others = live_finding_ids_for_key(ddb, table, move.new_key, utc_now())
            others.discard(move.finding_id)
            if others:
                raise ConflictError(
                    f"finding {move.finding_id}: live finding(s) {', '.join(sorted(others))} "
                    "now hold its new finding_key; stopped before writing it. Rows already "
                    "moved are fine. Pause the Tier 2 schedules, resolve the duplicate as for "
                    "a plan-time conflict, then re-run (it resumes)"
                )
        names = {"#top_vin": "vin"}
        values: dict[str, Any] = {
            ":new_vin": {"S": move.new_vin}, ":old_vin": {"S": move.old_vin},
        }
        update = "SET #top_vin = :new_vin"
        if move.vin_scoped:
            names.update({"#scope": "scope", "#nested_vin": "vin", "#fk": "finding_key"})
            values[":new_fk"] = {"S": move.new_key}
            update = "SET #top_vin = :new_vin, #scope.#nested_vin = :new_vin, #fk = :new_fk"
        try:
            ddb.update_item(
                TableName=table,
                Key={"finding_id": {"S": move.finding_id}},
                UpdateExpression=update,
                ConditionExpression="#top_vin = :old_vin",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
            counts.updated += 1
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            log.warning("finding %s no longer holds the old VIN; skipped", move.finding_id)
            counts.changed_concurrently += 1


def scrub_triage(ddb: Any, table: str, plan: Plan, dry_run: bool) -> None:
    assert_staging_table(table)
    for move in plan.triage_moves:
        counts = plan.triage[move.vehicle_id]
        if dry_run:
            counts.would_update += 1
            continue
        try:
            ddb.update_item(
                TableName=table,
                Key={"sessionId": {"S": move.session_id}},
                UpdateExpression="SET #vin = :new_vin",
                ConditionExpression="#vin = :old_vin",
                ExpressionAttributeNames={"#vin": "vin"},
                ExpressionAttributeValues={
                    ":new_vin": {"S": move.new_vin}, ":old_vin": {"S": move.old_vin},
                },
            )
            counts.updated += 1
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            log.warning("triage %s no longer holds the old VIN; skipped", move.session_id)
            counts.changed_concurrently += 1


# ── Post-counts and invariant ──────────────────────────────────────────────────

def measure_post(ddb: Any, findings_table: str, triage_table: str,
                 rows: list[VinRow], plan: Plan, horizon: int) -> None:
    triage_by_vin = scan_triage_vins(ddb, triage_table)
    for row in rows:
        f = plan.findings[row.vehicle_id]
        f.post_old = query_vin_count(ddb, findings_table, row.old_vin)
        f.post_new = query_vin_count(ddb, findings_table, row.new_vin)
        t = plan.triage[row.vehicle_id]
        t.post_old = len(counted(triage_by_vin.get(row.old_vin, []), horizon))
        t.post_new = len(counted(triage_by_vin.get(row.new_vin, []), horizon))


def original_expected(o: dict[str, Any], horizon: int | None) -> int:
    """Rows the original baseline counted that must still exist (TTL-aware for triage)."""
    ttls = o.get("ttls")
    if isinstance(ttls, list) and horizon is not None:
        return sum(1 for t in ttls if t is None or t > horizon)
    return int(o["pre_old"]) + int(o["pre_new"])


def invariant_failures(surface: str, counts: dict[str, SurfaceCounts],
                       original: dict[str, dict[str, Any]] | None,
                       horizon: int | None = None) -> list[str]:
    failures: list[str] = []
    for vid, c in counts.items():
        if c.post_old != 0:
            failures.append(f"{surface} {vid}: post(old)={c.post_old}, want 0 (old-VIN rows "
                            "remain; if they were written during the run, re-run)")
        want = c.pre_old + c.pre_new
        if c.post_new != want:
            hint = ("rows were lost" if c.post_new < want else
                    "new rows appeared during the run; re-run to confirm")
            failures.append(
                f"{surface} {vid}: post(new)={c.post_new}, want pre(old)+pre(new)={want} "
                f"({hint})"
            )
        if original is not None:
            o = original.get(vid)
            if o is None:
                failures.append(f"{surface} {vid}: missing from the original baseline")
            elif c.post_new < original_expected(o, horizon):
                failures.append(
                    f"{surface} {vid}: post(new)={c.post_new} is below the "
                    f"{original_expected(o, horizon)} rows of the original baseline that "
                    "should still exist; rows were lost"
                )
    return failures


def growth_since_original(counts: dict[str, SurfaceCounts],
                          original: dict[str, dict[str, Any]] | None,
                          horizon: int | None = None) -> list[str]:
    if original is None:
        return []
    return [
        vid for vid, c in counts.items()
        if vid in original and c.post_new > original_expected(original[vid], horizon)
    ]


# ── Baseline files (decisions.md ruling 21) ────────────────────────────────────

def _path(baseline_dir: str, kind: str, surface: str) -> str:
    return os.path.join(baseline_dir, f"revin_{kind}_{surface}.json")


def _record(*, surface: str, table: str, measure: str, apply: bool, stage: str,
            rows: list[VinRow], counts: dict[str, SurfaceCounts], tsv_path: str,
            extra: dict[str, Any] | None = None) -> dict[str, Any]:
    by_vid = {r.vehicle_id: r for r in rows}
    record: dict[str, Any] = {
        "surface": surface,
        "script": SCRIPT_NAME,
        "mode": "apply" if apply else "dry-run",
        "applied": apply,
        "stage": stage,
        "table": table,
        "measure": measure,
        "written_at": utc_now(),
        "tsv_path": tsv_path,
        "mapping_sha256": mapping_digest(rows),
        "vehicles": {
            vid: {"old_vin": by_vid[vid].old_vin, "new_vin": by_vid[vid].new_vin,
                  **asdict(c)}
            for vid, c in counts.items()
        },
    }
    if extra:
        record.update(extra)
    return record


def _write_new(path: str, record: dict[str, Any]) -> None:
    """Create ``path`` (owner-only, it holds VINs); fail if it exists, so it is written once."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2)


def _write_replace(path: str, record: dict[str, Any]) -> None:
    """Replace ``path`` atomically with an owner-only file (``mkstemp``: O_EXCL, 0600)."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".",
                               prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def load_original_baseline(path: str, surface: str, rows: list[VinRow]) -> dict[str, Any] | None:
    """The first --apply run's baseline, or None if there is none yet.

    Refuses a file that did not come from ``--apply`` (for example a dry-run file left
    at this path by an older version of the script) or that names a different mapping.
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise InputError(f"cannot read existing baseline {path}: {exc}") from exc
    if not isinstance(record, dict) or record.get("applied") is not True:
        raise InputError(
            f"{path} exists but was not written by --apply; move it aside by hand "
            "before applying"
        )
    if record.get("surface") != surface:
        raise InputError(f"{path} is for surface {record.get('surface')!r}, not {surface!r}")
    if record.get("mapping_sha256") != mapping_digest(rows):
        raise InputError(f"{path} was written for a different TSV mapping; refused")
    return record


# ── Entrypoint ─────────────────────────────────────────────────────────────────

def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--tsv-file", required=True,
                        help="header-less TSV: vehicleId<TAB>oldVin<TAB>newVin")
    parser.add_argument("--apply", action="store_true",
                        help="write; omit for a dry run (default)")
    parser.add_argument("--expected-rows", type=int, default=None,
                        help="required with --apply: the TSV's data row count (93)")
    parser.add_argument("--region", default=DEFAULT_REGION,
                        help=f"AWS region of the vsa tables (default {DEFAULT_REGION})")
    parser.add_argument("--stage", default=ALLOWED_STAGE,
                        help=f"only {ALLOWED_STAGE!r} is accepted")
    parser.add_argument("--baseline-dir", default=None,
                        help=f"where count files go (default {DEFAULT_BASELINE_DIR})")
    parser.add_argument("--log-level", default="INFO",
                        choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # botocore's DEBUG output echoes request bodies, which carry VINs; keep it at INFO.
    for noisy in ("botocore", "boto3", "urllib3"):
        logging.getLogger(noisy).setLevel(max(logging.INFO, getattr(logging, args.log_level)))
    apply = bool(args.apply)
    baseline_dir = args.baseline_dir or DEFAULT_BASELINE_DIR

    if args.stage != ALLOWED_STAGE:
        log.error("stage %r refused; only %r exists (CVX prod was torn down 2026-09-02)",
                  args.stage, ALLOWED_STAGE)
        return EXIT_INPUT
    if apply and args.expected_rows is None:
        log.error("--apply needs --expected-rows")
        return EXIT_INPUT
    findings_table = f"vsa-{args.stage}-{FINDINGS_TABLE_SUFFIX}"
    triage_table = f"vsa-{args.stage}-{TRIAGE_TABLE_SUFFIX}"
    try:
        assert_staging_table(findings_table)
        assert_staging_table(triage_table)
    except TableGuardError as exc:
        log.error("%s", exc)
        return EXIT_TABLE_GUARD

    try:
        rows = load_tsv(args.tsv_file)
        validate_rows(rows, args.expected_rows)
    except (OSError, UnicodeDecodeError, InputError) as exc:
        log.error("TSV refused: %s", exc)
        return EXIT_INPUT
    if not os.path.isdir(baseline_dir):
        log.error("baseline dir %s does not exist", baseline_dir)
        return EXIT_INPUT

    session = make_session(args.region, apply=apply)
    ddb = session.client("dynamodb")
    log.info("%s: %d vehicles, tables %s and %s",
             "APPLY" if apply else "DRY RUN", len(rows), findings_table, triage_table)

    try:
        return _run(ddb, args, rows, apply, baseline_dir, findings_table, triage_table)
    except readonly_guard.ReadOnlyViolation as exc:
        log.error("read-only guard: %s", exc)
        return EXIT_READONLY
    except TableGuardError as exc:
        log.error("%s", exc)
        return EXIT_TABLE_GUARD
    except InputError as exc:
        log.error("%s", exc)
        return EXIT_INPUT
    except ConflictError as exc:
        log.error("%s", exc)
        return EXIT_CONFLICT
    except (ClientError, BotoCoreError) as exc:
        log.error("AWS error: %s (after --apply, re-run to resume)", exc)
        return EXIT_AWS
    except OSError as exc:
        log.error("file error: %s", exc)
        return EXIT_INPUT


def _run(ddb: Any, args: argparse.Namespace, rows: list[VinRow], apply: bool,
         baseline_dir: str, findings_table: str, triage_table: str) -> int:
    surfaces = (
        (SURFACE_FINDINGS, findings_table, f"{FINDINGS_VIN_GSI} query count"),
        (SURFACE_TRIAGE, triage_table, "consistent table scan count"),
    )
    horizon = epoch_now() + TTL_MARGIN_S
    actions_table = f"vsa-{args.stage}-{ACTIONS_TABLE_SUFFIX}"
    plan = build_plan(ddb, findings_table, triage_table, actions_table, rows, utc_now(),
                      horizon)
    counts_of = {SURFACE_FINDINGS: plan.findings, SURFACE_TRIAGE: plan.triage}

    if not apply:
        scrub_findings(ddb, findings_table, plan, dry_run=True)
        scrub_triage(ddb, triage_table, plan, dry_run=True)
        for surface, table, measure in surfaces:
            _write_replace(_path(baseline_dir, "dryrun", surface), _record(
                surface=surface, table=table, measure=measure, apply=False,
                stage=args.stage, rows=rows, counts=counts_of[surface],
                tsv_path=args.tsv_file, extra={"conflicts": plan.conflicts},
            ))
        _log_summary(plan, apply=False)
        for conflict in plan.conflicts:
            log.error("conflict: %s", conflict)
        return EXIT_CONFLICT if plan.conflicts else EXIT_OK

    if plan.conflicts:
        for conflict in plan.conflicts:
            log.error("conflict: %s", conflict)
        log.error("nothing written: resolve the conflicts first")
        return EXIT_CONFLICT

    # Baselines: the first --apply run writes the original once; a later run keeps it
    # and records its own starting counts alongside. Validate both before writing either.
    loaded = {
        surface: load_original_baseline(_path(baseline_dir, "prebaseline", surface),
                                        surface, rows)
        for surface, _table, _measure in surfaces
    }
    originals: dict[str, dict[str, Any] | None] = {}
    stamp = utc_now().replace(":", "").replace("-", "")
    for surface, table, measure in surfaces:
        path = _path(baseline_dir, "prebaseline", surface)
        record = _record(surface=surface, table=table, measure=measure, apply=True,
                         stage=args.stage, rows=rows, counts=counts_of[surface],
                         tsv_path=args.tsv_file)
        original = loaded[surface]
        if original is None:
            _write_new(path, record)
            log.info("baseline written: %s", path)
            originals[surface] = None
        else:
            resume_path = os.path.join(baseline_dir, f"revin_resume_{surface}_{stamp}.json")
            _write_new(resume_path, record)
            log.info("original baseline kept (%s); this run's counts: %s", path, resume_path)
            originals[surface] = original["vehicles"]

    scrub_findings(ddb, findings_table, plan, dry_run=False)
    scrub_triage(ddb, triage_table, plan, dry_run=False)

    failures: list[str] = []
    for attempt in range(1, POST_COUNT_ATTEMPTS + 1):
        measure_post(ddb, findings_table, triage_table, rows, plan, horizon)
        failures = [
            *invariant_failures(SURFACE_FINDINGS, plan.findings, originals[SURFACE_FINDINGS],
                                horizon),
            *invariant_failures(SURFACE_TRIAGE, plan.triage, originals[SURFACE_TRIAGE],
                                horizon),
        ]
        if not failures or attempt == POST_COUNT_ATTEMPTS:
            break
        log.warning("post-counts do not match yet (attempt %d of %d); re-reading in %.0fs",
                    attempt, POST_COUNT_ATTEMPTS, POST_COUNT_DELAY_S)
        time.sleep(POST_COUNT_DELAY_S)

    for surface, table, measure in surfaces:
        counts = counts_of[surface]
        _write_replace(_path(baseline_dir, "postcount", surface), _record(
            surface=surface, table=table, measure=measure, apply=True, stage=args.stage,
            rows=rows, counts=counts, tsv_path=args.tsv_file,
            extra={
                "invariant_ok": not [f for f in failures if f.startswith(surface + " ")],
                "run_invariant_ok": not failures,
                "failures": [f for f in failures if f.startswith(surface + " ")],
                "grew_since_original": growth_since_original(counts, originals[surface],
                                                             horizon),
                "ttl_horizon": horizon,
            },
        ))
        grew = growth_since_original(counts, originals[surface], horizon)
        if grew:
            log.warning("%s: %d vehicle(s) have more rows than the original baseline "
                        "(new rows since then, not a loss)", surface, len(grew))
    _log_summary(plan, apply=True)
    if failures:
        for failure in failures:
            log.error("invariant: %s", failure)
        return EXIT_INVARIANT
    return EXIT_OK


def _log_summary(plan: Plan, *, apply: bool) -> None:
    for surface, counts in ((SURFACE_FINDINGS, plan.findings), (SURFACE_TRIAGE, plan.triage)):
        log.info(
            "[%s] %s: pre(old)=%d pre(new)=%d %s=%d changed_concurrently=%d",
            "APPLY" if apply else "DRY RUN", surface,
            sum(c.pre_old for c in counts.values()),
            sum(c.pre_new for c in counts.values()),
            "updated" if apply else "would_update",
            sum((c.updated if apply else c.would_update) for c in counts.values()),
            sum(c.changed_concurrently for c in counts.values()),
        )


if __name__ == "__main__":
    sys.exit(main())
