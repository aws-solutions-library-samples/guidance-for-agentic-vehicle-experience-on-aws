"""Shared helpers for live-verify scripts.

Used by: live-verify-first-reply-lead.py, live-verify-book-consent.py.
Any change here applies to both callers automatically.
"""
from __future__ import annotations

import datetime as _dt
import sys


def log_lines(logs, group: str, start_ms: int, end_ms: int, pattern: str) -> list[str]:
    """Paginated CloudWatch filter_log_events → list of stripped message strings."""
    out: list[str] = []
    kw: dict = {}
    while True:
        resp = logs.filter_log_events(
            logGroupName=group, startTime=start_ms, endTime=end_ms,
            filterPattern=pattern, **kw,
        )
        out += [e["message"].strip() for e in resp.get("events", [])]
        if not resp.get("nextToken"):
            return out
        kw = {"nextToken": resp["nextToken"]}


def _query_session_bookings(
    history_table, vid: str, session_id: str, start_iso: str
) -> list:
    """Return all service-history rows this session wrote (windowed + paginated).

    Uses DdbKey("serviceDate").gte(start_iso) so the booking (written near session
    end) is always within range, and paginates on LastEvaluatedKey.
    """
    from boto3.dynamodb.conditions import Attr, Key as DdbKey  # noqa: PLC0415

    kw: dict = {}
    rows: list = []
    while True:
        resp = history_table.query(
            KeyConditionExpression=DdbKey("vehicleId").eq(vid)
            & DdbKey("serviceDate").gte(start_iso),
            FilterExpression=Attr("sessionId").eq(session_id),
            **kw,
        )
        rows.extend(resp.get("Items", []))
        nk = resp.get("LastEvaluatedKey")
        if not nk:
            break
        kw = {"ExclusiveStartKey": nk}
    return rows


def _delete_session_bookings(
    history_table, vid: str, session_id: str, start_iso: str
) -> None:
    """Delete any service-history rows this session wrote; print each deletion."""
    from boto3.dynamodb.conditions import Attr  # noqa: PLC0415

    rows = _query_session_bookings(history_table, vid, session_id, start_iso)
    for row in rows:
        history_table.delete_item(
            Key={"vehicleId": vid, "serviceDate": row["serviceDate"]},
            ConditionExpression=Attr("sessionId").eq(session_id),
        )
        print(
            f"cleanup: deleted booking {row.get('requestNumber')} "
            f"({vid}, {row['serviceDate']}) this session wrote"
        )
    if not rows:
        print(f"cleanup: no booking rows for session {session_id} on {vid}")


def _run_cleanup(
    *,
    idp,
    pool: str,
    user: str,
    created: bool,
    history_table,
    vid: str | None,
    session_id: str | None,
    start_ms: int | None,
    stage: str,
) -> None:
    """Cleanup in the finally block: session bookings first, then the throwaway user.

    Each step has its own try so a booking-cleanup failure does not skip user deletion.
    Takes a 5-minute margin off start_iso so a laptop/runtime clock difference cannot
    drop a booking row from the query window.
    """
    if (
        history_table is not None
        and vid is not None
        and session_id is not None
        and start_ms is not None
    ):
        try:
            start_dt = _dt.datetime.fromtimestamp(start_ms / 1000, tz=_dt.timezone.utc)
            start_iso = (start_dt - _dt.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
            _delete_session_bookings(history_table, vid, session_id, start_iso)
        except Exception as exc:  # noqa: BLE001
            print(
                f"cleanup WARN: failed to remove session bookings for {vid} "
                f"session={session_id}: {type(exc).__name__}: {exc}\n"
                f"  Inspect manually: cms-{stage}-storage-service-history "
                f"vehicleId={vid!r} sessionId={session_id!r}",
                file=sys.stderr,
            )
    if created:
        try:
            idp.admin_delete_user(UserPoolId=pool, Username=user)
            print(f"deleted {user}")
        except Exception as exc:  # noqa: BLE001
            print(
                f"cleanup WARN: failed to delete user {user!r}: "
                f"{type(exc).__name__}: {exc}\n"
                f"  Delete manually:\n"
                f"  aws cognito-idp admin-delete-user --region us-west-2 "
                f"--user-pool-id {pool} --username {user!r}",
                file=sys.stderr,
            )


def pick_vehicle(
    base: str,
    token: str,
    table,
    history,
    tenant: str,
    max_tries: int,
    excluded: frozenset[str],
    prefix: str,
    *,
    require_lead: bool = True,
) -> tuple:
    """Return (vin, vehicleId, grade, lead) for the first qualifying vehicle.

    require_lead=True  (first-reply-lead): needs P1/P2/P3 + a non-None first_reply_lead;
                        a 'limited' verdict is preferred, then any lead as fallback.
    require_lead=False (book-consent):     needs only P1/P2/P3 (not P0, which auto-books
                        and escalates); the lead in the tuple is None.  Avoids 'no vehicle'
                        exits when no lead is built.
    """
    import requests  # noqa: PLC0415

    candidates: list[str] = []
    for page in history.meta.client.get_paginator("scan").paginate(
        TableName=history.name,
        FilterExpression="attribute_exists(activeCode)",
        ProjectionExpression="vehicleId",
        PaginationConfig={"MaxItems": 2000},
    ):
        for item in page.get("Items", []):
            vid = item.get("vehicleId")
            vid = vid.get("S") if isinstance(vid, dict) else vid
            if vid and vid not in candidates and vid not in excluded:
                candidates.append(vid)

    fallback, tried = None, 0
    for vid in candidates[:max_tries]:
        row = table.get_item(Key={"vehicleId": vid}).get("Item") or {}
        vin = row.get("vin")
        if not vin or vin in excluded:
            continue
        tried += 1
        r = requests.post(
            f"{base}/triage",
            json={"vin": vin, "sessionId": f"{prefix}-pick", "tenantId": tenant},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        body = r.json() if r.ok else {}
        grade, codes = body.get("classification"), body.get("activeDtcs") or []

        if require_lead:
            from agents.supervisor.first_reply_lead import (  # noqa: PLC0415
                build_lead as _build,
            )
            lead = _build(codes, grade) if grade in ("P1", "P2", "P3") else None
            print(
                f"  try {vid}: {r.status_code} {grade} {codes[:4]} "
                f"lead={'yes ' + lead.verdict if lead else 'no'}"
            )
            if lead is not None and lead.verdict == "limited":
                return vin, vid, grade, lead
            if lead is not None and fallback is None:
                fallback = (vin, vid, grade, lead)
        else:
            print(f"  try {vid}: {r.status_code} {grade} {codes[:4]}")
            if grade in ("P1", "P2", "P3"):
                return vin, vid, grade, None

    if fallback:
        return fallback
    kind = "lead" if require_lead else "non-P0 grade"
    raise SystemExit(
        f"no vehicle with a {kind} among {tried} candidates with active codes"
    )
