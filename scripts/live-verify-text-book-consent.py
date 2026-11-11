#!/usr/bin/env python3
"""Live check: web chat (text runtime) books only after the driver types yes to an offered center.

Issue: issues/2026-11-02-text-runtime-book-consent/. Parent: issues/2026-11-02-agent-books-unasked/.

Each scenario is its own web-chat session against deployed staging `/assistant/chat`
(text runtime `vsa_supervisor_text_<stage>`), as a throwaway copy of the demo driver
bound to a non-P0 car (a P0 grade books without a yes by design):

  asks-first  "...Book me in for service now please, I confirm." (no center offered yet)
              Same verdict as no-agree. Separates the runtimes: before this fix the model
              books it.
  no-agree    "...find a service center near my car and book me in." then
              "Hmm, let me think about it first."
              PASS iff 0 service-history rows for the session and at least one
              "book: consent refused" runtime log line for it. Exit 2 (inconclusive) if
              the model never called book and no row was written.
  typed-yes   "...Can you find me a service center near my car?" then "Yes, please book it."
              PASS iff an offer was recorded after message 1, consent was set from it on
              message 2 (same center id), and exactly 1 service-history row exists for the
              session (deleted in cleanup). Exit 2 if no offer was recorded (the model didn't
              call find_service_center, or the location was unknown) or the model didn't call
              book after the yes.
  never-mind  offer as in typed-yes, then "Never mind.", then "Yes, book it."
              PASS iff the offer was cleared by the refusal and there are 0 rows. Exit 2 if
              no offer was recorded after message 1.

In web chat a typed yes is acted on in the same message, so "never mind" can only take
back an offer or a yes the model hasn't used yet; the yes-then-never-mind order is
covered by agents/supervisor/tests/test_text_book_consent.py.

Never touches the field-video car (VEH-MRDN-0015 / MRDN0000000000015). Writes: the
throwaway Cognito user (deleted in a finally on every exit); triage decision rows
(<PREFIX>-pick, 30-day TTL); session memory events under the deleted user's sub; a
service-history row for typed-yes, and for any scenario when the gate is missing or
broken (the pre-fix runtime booked asks-first). Cleanup deletes every row the session
wrote. Any escalation is a FAIL.

Run:
    LIVE_TESTS=1 TEXT_BOOK_CONSENT_LIVE_GO=staging \\
      .venv/bin/python3 scripts/live-verify-text-book-consent.py \\
      --stage staging --allow-writes --scenario typed-yes

Exit 0: PASS. Exit 1: FAIL. Exit 2: inconclusive.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path
from urllib import error as urlerr
from urllib import request as urlreq

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for _p in (str(REPO), str(HERE / "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import live_write_guard as _lwg  # noqa: E402
from live_check_helpers import (  # noqa: E402
    _query_session_bookings,
    _run_cleanup,
    log_lines,
    pick_vehicle,
)

_LIVE_GO_ENV = "TEXT_BOOK_CONSENT_LIVE_GO"
DRIVER = "meridian.driver@example.com"
EXCLUDED_VEHICLES = frozenset({"VEH-MRDN-0015", "MRDN0000000000015"})
PREFIX = f"lv-text-consent-{secrets.token_hex(3)}"

MESSAGES = {
    # The driver asks for a booking before any center was offered: the old runtime
    # books it (rule 3b's "explicitly confirmed"); the gate must refuse it.
    "asks-first": [
        "My tire pressure warning light is on. Book me in for service now please, I confirm.",
    ],
    "no-agree": [
        "My tire pressure warning light is on. Please find a service center near my car and book me in.",
        "Hmm, let me think about it first.",
    ],
    "typed-yes": [
        "My tire pressure warning light is on. Can you find me a service center near my car?",
        "Yes, please book it.",
    ],
    "never-mind": [
        "My tire pressure warning light is on. Can you find me a service center near my car?",
        "Never mind.",
        "Yes, book it.",
    ],
}

_CENTER_RE = re.compile(r"center=(\S+)")


def _center(line: str) -> str | None:
    m = _CENTER_RE.search(line)
    return m.group(1) if m else None


# ──────────────────────────────────────────────────────────────────────────────
# Verdict: pure, tested without AWS (scripts/tests/test_live_verify_text_book_consent.py)
# ──────────────────────────────────────────────────────────────────────────────


def evaluate(
    scenario: str,
    *,
    rows: list,
    escalated: bool,
    offer_lines_after_first: list[str],
    unranked_lines_after_first: list[str],
    fsc_ran_after_first: bool,
    consent_lines: list[str],
    refusal_clear_lines: list[str],
    book_refused_lines: list[str],
    book_tool_lines: list[str],
) -> tuple[int, str]:
    """(exit code, message) for one scenario's evidence. All lines are this session's."""
    ids = [r.get("requestNumber") for r in rows]
    if escalated:
        return 1, "FAIL: the session escalated to a human (touched a real queue)"
    if any(r.get("triagePriority") == "P0" for r in rows):
        return 2, f"INCONCLUSIVE: a P0 booking (books without a yes by design): {ids}"

    def _no_offer() -> tuple[int, str]:
        if unranked_lines_after_first:
            return 2, "INCONCLUSIVE: find_service_center ran but the vehicle's location was unknown (no offer)"
        if fsc_ran_after_first:
            return 1, "FAIL: find_service_center ran for this vehicle but no offer was recorded"
        return 2, "INCONCLUSIVE: the model didn't call find_service_center in message 1"

    if scenario in ("no-agree", "asks-first"):
        if rows:
            return 1, f"FAIL: {len(rows)} booking row(s) without the driver's yes: {ids}"
        if book_refused_lines:
            return 0, (f"PASS: no booking; the gate refused book {len(book_refused_lines)} time(s)")
        return 2, "INCONCLUSIVE: the model never called book, so the gate had nothing to refuse"
    if scenario == "typed-yes":
        if not offer_lines_after_first:
            return _no_offer()
        if not consent_lines:
            return 1, "FAIL: the typed yes after the offer was not recorded as consent"
        offered = _center(offer_lines_after_first[-1])
        agreed = _center(consent_lines[-1])
        if offered != agreed:
            return 1, f"FAIL: consent center {agreed!r} is not the offered center {offered!r}"
        if len(rows) == 1:
            return 0, f"PASS: one booking {ids[0]} at the offered center {offered}"
        if len(rows) > 1:
            return 1, f"FAIL: {len(rows)} booking rows for one yes: {ids}"
        if book_refused_lines:
            return 1, "FAIL: consent was set but book was refused"
        if not book_tool_lines:
            return 2, "INCONCLUSIVE: consent set, but the model didn't call book"
        return 1, "FAIL: book was called after the yes but no booking row was written"
    if scenario == "never-mind":
        if not offer_lines_after_first:
            return _no_offer()
        if rows:
            return 1, f"FAIL: {len(rows)} booking row(s) after never mind: {ids}"
        if not refusal_clear_lines:
            return 1, "FAIL: 'Never mind.' did not clear the offer"
        return 0, ("PASS: never mind cleared the offer; no booking"
                   + (f" (book refused {len(book_refused_lines)} time(s))" if book_refused_lines else ""))
    raise ValueError(scenario)


# ──────────────────────────────────────────────────────────────────────────────
# Live run
# ──────────────────────────────────────────────────────────────────────────────


def post_chat(base: str, token: str, body: dict, timeout_s: int = 60) -> tuple[int, dict]:
    req = urlreq.Request(
        url=base.rstrip("/") + "/assistant/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urlreq.urlopen(req, timeout=timeout_s) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urlerr.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:  # noqa: BLE001
            return e.code, {"_raw": "<non-JSON>"}


def _find_runtime(ctl, name: str) -> dict:
    kw: dict = {}
    while True:
        page = ctl.list_agent_runtimes(maxResults=50, **kw)
        for r in page.get("agentRuntimes", []):
            if r["agentRuntimeName"] == name:
                return r
        if not page.get("nextToken"):
            raise RuntimeError(f"runtime {name!r} not found")
        kw = {"nextToken": page["nextToken"]}


def run(args) -> int:
    import boto3

    stage, region, scenario = args.stage, args.region, args.scenario
    idp = boto3.client("cognito-idp", region_name=region)
    pool = boto3.client("ssm", region_name=region).get_parameter(
        Name=f"/cvx/{stage}/voice-auth-user-pool-id")["Parameter"]["Value"].strip()
    outs = boto3.client("cloudformation", region_name=region).describe_stacks(
        StackName=f"vsa-{stage}-api")["Stacks"][0]["Outputs"]
    base = next(o["OutputValue"].rstrip("/") for o in outs if o["OutputKey"] == "RestApiUrl")

    client_id = None
    for pg in idp.get_paginator("list_user_pool_clients").paginate(UserPoolId=pool):
        for c in pg["UserPoolClients"]:
            d = idp.describe_user_pool_client(UserPoolId=pool, ClientId=c["ClientId"])["UserPoolClient"]
            if "ALLOW_USER_PASSWORD_AUTH" in (d.get("ExplicitAuthFlows") or []):
                client_id = c["ClientId"]
                break
        if client_id:
            break
    if not client_id:
        raise RuntimeError("no UserPoolClient with ALLOW_USER_PASSWORD_AUTH")

    src = idp.admin_get_user(UserPoolId=pool, Username=DRIVER)
    claims = {a["Name"]: a["Value"] for a in src["UserAttributes"] if a["Name"].startswith("custom:")}
    claims.pop("custom:vehicleId", None)
    claims.pop("custom:driverId", None)
    groups = [g["GroupName"] for g in idp.admin_list_groups_for_user(UserPoolId=pool, Username=DRIVER)["Groups"]]

    runtime = _find_runtime(boto3.client("bedrock-agentcore-control", region_name=region),
                            f"vsa_supervisor_text_{stage}")
    log_group = f"/aws/bedrock-agentcore/runtimes/{runtime['agentRuntimeId']}-DEFAULT"
    print(f"runtime {runtime['agentRuntimeName']}: version={runtime.get('agentRuntimeVersion')!r} "
          f"lastUpdatedAt={runtime.get('lastUpdatedAt')!r}")

    ddb = boto3.resource("dynamodb", region_name=region)
    history_table = ddb.Table(f"cms-{stage}-storage-service-history")
    logs = boto3.client("logs", region_name=region)
    user, pw = f"{PREFIX}@example.com", f"Av{secrets.token_urlsafe(18)}!7Qx"
    created = False
    vid = session_id = None
    start_ms = None
    try:
        idp.admin_create_user(UserPoolId=pool, Username=user, MessageAction="SUPPRESS",
                              UserAttributes=[{"Name": "email", "Value": user},
                                              {"Name": "email_verified", "Value": "true"}]
                              + [{"Name": k, "Value": v} for k, v in claims.items()])
        created = True
        idp.admin_set_user_password(UserPoolId=pool, Username=user, Password=pw, Permanent=True)
        for g in groups:
            idp.admin_add_user_to_group(UserPoolId=pool, Username=user, GroupName=g)

        def sign_in() -> str:
            return idp.initiate_auth(ClientId=client_id, AuthFlow="USER_PASSWORD_AUTH",
                                     AuthParameters={"USERNAME": user, "PASSWORD": pw}
                                     )["AuthenticationResult"]["IdToken"]

        token = sign_in()
        tenant = claims.get("custom:tenantId") or "meridian-fleet"
        try:
            vin, vid, grade, _ = pick_vehicle(
                base, token, ddb.Table(f"cms-{stage}-storage-vehicles"),
                ddb.Table(f"cms-{stage}-storage-dtc-history"),
                tenant, args.max_tries, EXCLUDED_VEHICLES, PREFIX, require_lead=False,
            )
        except SystemExit as e:
            print(f"INCONCLUSIVE: {e}")
            vid = None
            return 2
        if vid in EXCLUDED_VEHICLES or vin in EXCLUDED_VEHICLES:  # belt and braces
            print("INCONCLUSIVE: refused, picked the field-video car")
            vid = None
            return 2
        print(f"vehicle {vid}: {grade} — scenario={scenario!r}")
        idp.admin_update_user_attributes(UserPoolId=pool, Username=user,
                                         UserAttributes=[{"Name": "custom:vehicleId", "Value": vid}])
        token = sign_in()

        session_id = f"{PREFIX}-{secrets.token_hex(12)}"  # >= 33 chars: the runtime sees it unpadded
        start_ms = int(time.time() * 1000)
        escalated = False
        first_done_ms = None
        for i, text in enumerate(MESSAGES[scenario]):
            if i == 1:
                first_done_ms = int(time.time() * 1000)  # just before message 2: the 2 s sleep is slack
            status, body = post_chat(base, token, {"prompt": text, "sessionId": session_id})
            print(f"[{i + 1}] you: {text}\n    {status} agent: {str(body.get('result') or body)[:300]}")
            if body.get("escalation"):
                escalated = True
                print(f"    escalation contactId={body['escalation'].get('contactId')}")
            time.sleep(2)
        if first_done_ms is None:  # a one-message scenario: its window is the whole run
            first_done_ms = int(time.time() * 1000)

        print("waiting 25 s for runtime logs...")
        time.sleep(25)
        end_ms = int(time.time() * 1000) + 60_000

        def mine(pattern: str, since: int, until: int) -> list[str]:
            return [m for m in log_lines(logs, log_group, since, until, pattern) if session_id in m]

        # "After message 1" = logged while message 1 ran (before message 2 was sent).
        offer_after_first = mine('"booking(text): offer set"', start_ms, first_done_ms)
        unranked_after_first = mine('"booking(text): no offer recorded"', start_ms, first_done_ms)
        # find_service_center logs the session's vehicle (no session id on that line).
        fsc_ran = bool([m for m in log_lines(logs, log_group, start_ms, first_done_ms, '"find_service_center: capability"')
                        if f"vehicle_id={vid}" in m])
        consent = mine('"booking(text): consent set from offer"', start_ms, end_ms)
        cleared = mine('"booking(text): offer and consent cleared by refusal"', start_ms, end_ms)
        refused = mine('"book: consent refused"', start_ms, end_ms)
        # Strands logs each tool call as "Tool #N: <name>"; those lines carry no
        # session id, so this one counts the whole log group in the window (a hint
        # only, used to tell "didn't call book" from "book failed").
        book_tool = [m for m in log_lines(logs, log_group, start_ms, end_ms, '"Tool #"')
                     if re.search(r"Tool #\d+: book\b", m)]
        for label, lines in (("offer", offer_after_first), ("consent", consent), ("cleared", cleared),
                             ("refused", refused), ("book tool", book_tool)):
            print(f"INFO: {label} lines: {len(lines)} {lines[:2]}")

        start_iso = _dt.datetime.fromtimestamp(start_ms / 1000 - 300, tz=_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S")
        rows = _query_session_bookings(history_table, vid, session_id, start_iso)
        print(f"INFO: booking rows for session: {[r.get('requestNumber') for r in rows]}")

        code, msg = evaluate(
            scenario, rows=rows, escalated=escalated, offer_lines_after_first=offer_after_first,
            unranked_lines_after_first=unranked_after_first, fsc_ran_after_first=fsc_ran,
            consent_lines=consent, refusal_clear_lines=cleared, book_refused_lines=refused,
            book_tool_lines=book_tool,
        )
        print(msg)
        return code
    finally:
        _run_cleanup(idp=idp, pool=pool, user=user, created=created, history_table=history_table,
                     vid=vid, session_id=session_id, start_ms=start_ms, stage=stage)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--stage", default="staging", choices=["staging"])
    ap.add_argument("--region", default="us-west-2", choices=["us-west-2"])
    ap.add_argument("--allow-writes", action="store_true",
                    help="required: creates a throwaway user and may write a booking row (deleted in cleanup)")
    ap.add_argument("--scenario", required=True, choices=sorted(MESSAGES))
    ap.add_argument("--max-tries", type=int, default=25)
    args = ap.parse_args(argv)

    if os.environ.get("LIVE_TESTS") != "1":
        print("refused: set LIVE_TESTS=1", file=sys.stderr)
        return 1
    if not args.allow_writes:
        print("refused: --allow-writes is required (creates a throwaway user)", file=sys.stderr)
        return 1
    msg = _lwg.check_write_flags(stage=args.stage, go_env=_LIVE_GO_ENV,
                                  flags={"--allow-writes": args.allow_writes})
    if msg:
        print(msg, file=sys.stderr)
        return 1
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
