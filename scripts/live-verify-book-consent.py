#!/usr/bin/env python3
"""Live check: the voice book tool refuses unless the driver said yes to an offered center.

Issue: issues/2026-11-02-agent-books-unasked/report.md. Fix branch: cvx-wt-book-consent.

Interpreter: .venv/bin/python3 (needs websockets, bedrock_agentcore, requests, boto3).

Three scenarios, each its own session, selected by --scenario:

  unasked  — a typed warning-light question plus a follow-up, streamed silence throughout,
             the driver never agrees.
             PASS iff the model called book, at least one consent-refusal log line was emitted
             for this session, and there are 0 service-history rows.
             Exit 2 (inconclusive) if the model never called book (the consent gate had no
             booking path to refuse), or if book was called but no refusal line was logged and
             no row was written (another refusal path may have fired).

  typed-yes  — typed "Can you find me a service center near my car?", waits for the
             find_service_center tool.result (not just tool.call), then for the assistant's
             final transcript, then a 1.5 s quiet gap, then typed "Yes.", waits for the
             booking.
             PASS iff exactly one service-history row exists, its serviceDate is at or after
             yes_sent_iso, no consent-refused line was logged after yes, and the requestNumber
             matches a book tool.result received after yes.  Row deleted in cleanup.

  spoken-yes — same as typed-yes, but both driver turns are synthesized by Amazon Polly at
             16 kHz mono PCM and streamed as audio.chunk frames (200 ms each).  Offer-wait
             timeout is longer (90 s) because voice round-trips are slower.

The check never touches the field-video car (VEH-MRDN-0015 / MRDN0000000000015).  It creates a
throwaway Cognito user that is deleted in a finally block on every exit path.

Writes: the throwaway user; triage decision rows (<PREFIX>-pick, 30-day TTL); session memory
events under the deleted user's sub; and, for typed-yes / spoken-yes, one service-history row
(deleted in cleanup).  Triage rows and memory events are left for their TTL.

Run:
    LIVE_TESTS=1 BOOK_CONSENT_LIVE_GO=staging \\
      .venv/bin/python3 scripts/live-verify-book-consent.py \\
      --stage staging --allow-writes --scenario unasked

Exit 0: PASS. Exit 1: FAIL. Exit 2: inconclusive.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as _dt
import json
import os
import secrets
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for _p in (str(REPO), str(HERE / "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import live_write_guard as _lwg  # noqa: E402
from live_check_helpers import (  # noqa: E402
    _delete_session_bookings,
    _query_session_bookings,
    _run_cleanup,
    log_lines,
    pick_vehicle,
)

_LIVE_GO_ENV = "BOOK_CONSENT_LIVE_GO"

_QUESTION_WARNING = "A warning light just came on. What's wrong, and is it safe to drive?"
_QUESTION_FOLLOW_UP = "Where can I get it checked?"
_QUESTION_BOOK = "Can you find me a service center near my car?"
_ANSWER_YES = "Yes."

EXCLUDED_VEHICLES = frozenset({"VEH-MRDN-0015", "MRDN0000000000015"})
PREFIX = f"lv-book-consent-{secrets.token_hex(3)}"

_FRAME_BYTES = 16000 * 2 * 200 // 1000  # 200 ms of 16 kHz 16-bit mono PCM = 6400 bytes


# ──────────────────────────────────────────────────────────────────────────────
# Verdict logic — pure function, fully testable without AWS
# ──────────────────────────────────────────────────────────────────────────────


def evaluate(
    scenario: str,
    tools: list[str],
    booking_rows: list[dict],
    session_refusal_lines: list[str],
    post_yes_refusal_lines: list[str],
    fallback_lines: list[str],
    yes_sent_iso: str | None,
    book_results_after_yes: list[str],
) -> tuple[int, str]:
    """Return (exit_code, message) for the scenario.

    exit_code 0 = PASS, 1 = FAIL, 2 = INCONCLUSIVE.
    """
    book_called = "book" in tools

    if scenario == "unasked":
        if booking_rows:
            ids = [r.get("requestNumber") for r in booking_rows]
            return 1, (
                f"FAIL: {len(booking_rows)} booking row(s) written without driver consent: {ids}"
            )
        if not book_called:
            return 2, (
                "INCONCLUSIVE: the model never called book; "
                "the consent gate was not exercised"
            )
        if not session_refusal_lines:
            return 2, (
                "INCONCLUSIVE: book was called but no consent-refusal line logged and no row "
                "written; another refusal path may have fired — inspect the book tool.result"
            )
        return 0, (
            f"PASS: no service-history row — the consent gate refused the booking "
            f"({len(session_refusal_lines)} refusal line(s) in log)"
        )

    # ── yes scenarios ─────────────────────────────────────────────────────────
    if fallback_lines:
        return 2, (
            "INCONCLUSIVE: auto-book-fallback fired — the model's gated tool path was not "
            f"exercised: {fallback_lines[:1]}"
        )
    if post_yes_refusal_lines:
        return 1, (
            f"FAIL: consent refused after yes was sent "
            f"({len(post_yes_refusal_lines)} line(s)); the yes was not consumed by the gate: "
            f"{post_yes_refusal_lines[:1]}"
        )
    if not booking_rows:
        return 1, "FAIL: no booking row despite driver saying yes"
    if len(booking_rows) > 1:
        ids = [r.get("requestNumber") for r in booking_rows]
        return 1, f"FAIL: {len(booking_rows)} booking rows — expected exactly 1: {ids}"
    row = booking_rows[0]
    svc_date = row.get("serviceDate", "")
    if yes_sent_iso and svc_date < yes_sent_iso:
        return 1, (
            f"FAIL: booking pre-dates yes (row serviceDate={svc_date!r} < "
            f"yes_sent={yes_sent_iso!r}) — the gate may be broken (booking before consent)"
        )
    rn = row.get("requestNumber")
    if rn and book_results_after_yes and rn not in book_results_after_yes:
        return 1, (
            f"FAIL: row requestNumber={rn!r} not in book tool.results received after yes "
            f"{book_results_after_yes!r} — untracked booking path"
        )
    return 0, f"PASS: exactly one booking row ({rn}) after driver consent"


# ──────────────────────────────────────────────────────────────────────────────
# Audio helpers (spoken-yes scenario)
# ──────────────────────────────────────────────────────────────────────────────


def _polly_pcm_frames(polly_client, text: str, region: str) -> list[str]:
    """Synthesize text via Amazon Polly → list of base64-encoded 200 ms PCM frames.

    Uses the same region as everything else (neural Joanna available in us-west-2).
    """
    resp = polly_client.synthesize_speech(
        Text=text,
        OutputFormat="pcm",
        SampleRate="16000",
        VoiceId="Joanna",
        Engine="neural",
    )
    pcm: bytes = resp["AudioStream"].read()
    frames = []
    for i in range(0, len(pcm), _FRAME_BYTES):
        chunk = pcm[i: i + _FRAME_BYTES]
        if len(chunk) < _FRAME_BYTES:
            chunk += b"\x00" * (_FRAME_BYTES - len(chunk))
        frames.append(base64.b64encode(chunk).decode())
    return frames


_SILENCE_FRAME = base64.b64encode(b"\x00" * _FRAME_BYTES).decode()


# ──────────────────────────────────────────────────────────────────────────────
# Core session runner
# ──────────────────────────────────────────────────────────────────────────────


async def run(args) -> int:
    import boto3
    from websockets.asyncio.client import connect as ws_connect
    import websockets

    region, stage, scenario = args.region, args.stage, args.scenario

    idp = boto3.client("cognito-idp", region_name=region)
    pool = boto3.client("ssm", region_name=region).get_parameter(
        Name=f"/cvx/{stage}/voice-auth-user-pool-id")["Parameter"]["Value"].strip()
    outs = boto3.client("cloudformation", region_name=region).describe_stacks(
        StackName=f"vsa-{stage}-api")["Stacks"][0]["Outputs"]
    base = next(o["OutputValue"].rstrip("/") for o in outs if o["OutputKey"] == "RestApiUrl")

    client_id: str | None = None
    for _pg in idp.get_paginator("list_user_pool_clients").paginate(UserPoolId=pool):
        for c in _pg["UserPoolClients"]:
            detail = idp.describe_user_pool_client(
                UserPoolId=pool, ClientId=c["ClientId"])["UserPoolClient"]
            if "ALLOW_USER_PASSWORD_AUTH" in (detail.get("ExplicitAuthFlows") or []):
                client_id = c["ClientId"]
                break
        if client_id:
            break
    if not client_id:
        raise RuntimeError("no UserPoolClient with ALLOW_USER_PASSWORD_AUTH in the voice-auth pool")

    md = idp.admin_get_user(UserPoolId=pool, Username="meridian.driver@example.com")
    claims = {a["Name"]: a["Value"] for a in md["UserAttributes"] if a["Name"].startswith("custom:")}
    claims.pop("custom:vehicleId", None)
    claims.pop("custom:driverId", None)
    groups = [g["GroupName"] for g in idp.admin_list_groups_for_user(
        UserPoolId=pool, Username="meridian.driver@example.com")["Groups"]]

    # Paginate runtimes; print version for the deployment-validation record.
    name = f"vsa_supervisor_bidi_{stage}"
    ctl = boto3.client("bedrock-agentcore-control", region_name=region)
    runtime = None
    _rt_kw: dict = {}
    while True:
        _rt_page = ctl.list_agent_runtimes(maxResults=50, **_rt_kw)
        for r in _rt_page.get("agentRuntimes", []):
            if r["agentRuntimeName"] == name:
                runtime = r
                break
        if runtime or not _rt_page.get("nextToken"):
            break
        _rt_kw = {"nextToken": _rt_page["nextToken"]}
    if not runtime:
        raise RuntimeError(f"bidi runtime {name!r} not found in {region}")

    arn, runtime_id = runtime["agentRuntimeArn"], runtime["agentRuntimeId"]
    log_group = f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"
    print(f"runtime {name}: version={runtime.get('agentRuntimeVersion')!r} "
          f"lastUpdatedAt={runtime.get('lastUpdatedAt')!r}")

    user, pw = f"{PREFIX}@example.com", f"Av{secrets.token_urlsafe(18)}!7Qx"
    created = False
    vid: str | None = None
    session_id: str | None = None
    start_ms: int | None = None
    yes_sent_ms: int | None = None
    history_table = boto3.resource("dynamodb", region_name=region).Table(
        f"cms-{stage}-storage-service-history")

    # Polly uses the same region; neural Joanna is available in us-west-2.
    polly = None if scenario != "spoken-yes" else boto3.client("polly", region_name=region)

    try:
        idp.admin_create_user(UserPoolId=pool, Username=user, MessageAction="SUPPRESS",
                              UserAttributes=[{"Name": "email", "Value": user},
                                             {"Name": "email_verified", "Value": "true"}]
                              + [{"Name": k, "Value": v} for k, v in claims.items()])
        created = True
        idp.admin_set_user_password(UserPoolId=pool, Username=user, Password=pw, Permanent=True)
        for g in groups:
            idp.admin_add_user_to_group(UserPoolId=pool, Username=user, GroupName=g)

        def sign_in():
            return idp.initiate_auth(ClientId=client_id, AuthFlow="USER_PASSWORD_AUTH",
                                     AuthParameters={"USERNAME": user, "PASSWORD": pw}
                                     )["AuthenticationResult"]["IdToken"]

        token = sign_in()
        vehicles = boto3.resource("dynamodb", region_name=region).Table(f"cms-{stage}-storage-vehicles")
        tenant = claims.get("custom:tenantId") or "meridian-fleet"
        history = boto3.resource("dynamodb", region_name=region).Table(f"cms-{stage}-storage-dtc-history")
        # book-consent only needs a non-P0 grade (P1/P2/P3); no first_reply_lead needed.
        vin, vid, grade, _lead = pick_vehicle(
            base, token, vehicles, history, tenant, args.max_tries,
            EXCLUDED_VEHICLES, PREFIX, require_lead=False,
        )
        print(f"vehicle {vid}: {grade} — scenario={scenario!r}")
        idp.admin_update_user_attributes(UserPoolId=pool, Username=user,
                                         UserAttributes=[{"Name": "custom:vehicleId", "Value": vid}])
        token = sign_in()

        from bedrock_agentcore.runtime import AgentCoreRuntimeClient
        session_id = f"{PREFIX}-{secrets.token_hex(12)}"
        ws_url, headers = AgentCoreRuntimeClient(region=region).generate_ws_connection(
            runtime_arn=arn, session_id=session_id)
        headers.update({
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token": token,
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Tenant-Id": tenant,
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Vin": vin,
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-VehicleId": vid,
        })
        start_ms = int(time.time() * 1000)
        print(f"session {session_id} started {time.strftime('%H:%M:%SZ', time.gmtime())}")

        events: list[dict] = []
        offer_result_evt = asyncio.Event()  # find_service_center tool.result received
        speech_queue: asyncio.Queue[list[str]] = asyncio.Queue()
        # last_assistant_final[0]: monotonic time of the last isFinal assistant transcript
        # after the offer result arrived.
        last_assistant_final: list[float] = [0.0]

        async with ws_connect(ws_url, additional_headers=headers, max_size=2**24, open_timeout=15) as ws:

            async def recv() -> None:
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=args.timeout)
                    except (asyncio.TimeoutError, websockets.ConnectionClosed):
                        return
                    evt = json.loads(raw)
                    if evt.get("type") == "audio.chunk":
                        continue
                    events.append(evt)
                    t = evt.get("type")
                    if t == "transcript":
                        mark = "" if evt.get("isFinal") else "~"
                        print(f"  transcript{mark} {evt.get('role')}: {evt.get('text', '')[:200]}")
                        if (evt.get("role") == "assistant" and evt.get("isFinal")
                                and offer_result_evt.is_set()):
                            last_assistant_final[0] = time.monotonic()
                    elif t in ("tool.call", "classification"):
                        print(f"  {t}: {evt.get('name') or evt.get('value')}")
                    elif t == "escalation":
                        print(f"  escalation: status={evt.get('status')} "
                              f"contactId={evt.get('contactId')} rsaActionId={evt.get('rsaActionId')}")
                    if t == "tool.result" and evt.get("name") == "find_service_center":
                        offer_result_evt.set()

            async def audio_sender() -> None:
                while True:
                    try:
                        frames = speech_queue.get_nowait()
                        for frame in frames:
                            await ws.send(json.dumps({"type": "audio.chunk",
                                                       "sampleRate": 16000, "data": frame}))
                            await asyncio.sleep(0.2)
                        speech_queue.task_done()
                    except asyncio.QueueEmpty:
                        await ws.send(json.dumps({"type": "audio.chunk",
                                                   "sampleRate": 16000, "data": _SILENCE_FRAME}))
                        await asyncio.sleep(0.2)

            rt = asyncio.create_task(recv())
            at = asyncio.create_task(audio_sender())
            await asyncio.sleep(3)

            # ── first driver turn ──────────────────────────────────────────────
            if scenario == "unasked":
                await ws.send(json.dumps({"type": "text.input", "text": _QUESTION_WARNING}))
                # Follow-up increases the chance of a booking attempt (seen 2026-10-30).
                await asyncio.sleep(args.after / 2)
                await ws.send(json.dumps({"type": "text.input", "text": _QUESTION_FOLLOW_UP}))
            elif scenario == "typed-yes":
                await ws.send(json.dumps({"type": "text.input", "text": _QUESTION_BOOK}))
            else:  # spoken-yes
                frames = _polly_pcm_frames(polly, _QUESTION_BOOK, region)
                await speech_queue.put(frames)
                await asyncio.sleep(0.2)

            # ── yes scenarios: wait for offer result → transcript+gap → send yes ──
            offer_missed = False
            yes_sent_iso: str | None = None
            if scenario in ("typed-yes", "spoken-yes"):
                offer_timeout = 60 if scenario != "spoken-yes" else 90
                try:
                    await asyncio.wait_for(offer_result_evt.wait(), timeout=offer_timeout)
                except asyncio.TimeoutError:
                    offer_missed = True
                    print(f"INFO: no find_service_center tool.result in {offer_timeout}s; "
                          "checking for unexpected bookings/escalations")

                if not offer_missed:
                    # W1 fix: wait for the assistant's final transcript + 1.5 s quiet gap
                    # before sending yes, so the offer is in ctx.booking_offer.
                    _TRANSCRIPT_WAIT_S = 30.0
                    _QUIET_GAP_S = 1.5
                    deadline = time.monotonic() + _TRANSCRIPT_WAIT_S
                    while time.monotonic() < deadline:
                        laf = last_assistant_final[0]
                        if laf > 0 and (time.monotonic() - laf) >= _QUIET_GAP_S:
                            break
                        await asyncio.sleep(0.25)
                    else:
                        print("INFO: no final assistant transcript + quiet gap after offer; "
                              "sending yes now")

                    yes_sent_iso = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
                    yes_sent_ms = int(time.time() * 1000)
                    if scenario == "typed-yes":
                        await ws.send(json.dumps({"type": "text.input", "text": _ANSWER_YES}))
                    else:
                        frames = _polly_pcm_frames(polly, _ANSWER_YES, region)
                        await speech_queue.put(frames)

            await asyncio.sleep(args.after)
            at.cancel()
            rt.cancel()

        end_ms = int(time.time() * 1000) + 30_000
        await asyncio.sleep(20)  # log delivery

        # ── log reads ──────────────────────────────────────────────────────────
        logs = boto3.client("logs", region_name=region)
        book_lines = log_lines(logs, log_group, start_ms, end_ms,
                               f'"book" "session={session_id}"')
        consent_refusal_lines = log_lines(logs, log_group, start_ms, end_ms,
                                           '"book: consent refused"')
        session_refusal_lines = [m for m in consent_refusal_lines if session_id in m]
        fallback_lines_raw = log_lines(logs, log_group, start_ms, end_ms, '"auto-book-fallback"')
        fallback_lines = [m for m in fallback_lines_raw if session_id in m]
        post_yes_refusal_lines: list[str] = []
        if yes_sent_ms is not None:
            raw = log_lines(logs, log_group, yes_sent_ms, end_ms, '"book: consent refused"')
            post_yes_refusal_lines = [m for m in raw if session_id in m]

        tools = [e.get("name") for e in events if e.get("type") == "tool.call"]
        escalation_evts = [e for e in events if e.get("type") == "escalation"]
        # W3 fix: the tool is escalate_to_human, not escalate.
        escalate_calls = [t for t in tools if t == "escalate_to_human"]

        # book tool.results after yes (for gated-booking check)
        book_results_after_yes: list[str] = [
            e.get("result", {}).get("requestNumber")
            for e in events
            if e.get("type") == "tool.result"
            and e.get("name") == "book"
            and e.get("result", {}).get("requestNumber")
        ]

        print(f"\nINFO: tools in order: {tools}")
        print(f"INFO: book log lines for session: {book_lines[:3]}")
        print(f"INFO: consent-refusal lines for session: {session_refusal_lines[:3]}")
        print(f"INFO: fallback lines for session: {fallback_lines[:2]}")
        if yes_sent_iso:
            print(f"INFO: yes sent at {yes_sent_iso}; "
                  f"post-yes refusal lines: {post_yes_refusal_lines[:2]}")

        # ── escalation: always exit 1 ──────────────────────────────────────────
        if escalate_calls or escalation_evts:
            print("FAIL: escalate_to_human was called or fired — this run touched a real queue.",
                  file=sys.stderr)
            for ev in escalation_evts:
                print(f"  contactId={ev.get('contactId')} rsaActionId={ev.get('rsaActionId')}",
                      file=sys.stderr)
            return 1

        # ── booking rows ───────────────────────────────────────────────────────
        start_iso = _dt.datetime.fromtimestamp(
            start_ms / 1000, tz=_dt.timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%S")
        booking_rows = _query_session_bookings(history_table, vid, session_id, start_iso)
        booking_ids = [r.get("requestNumber") for r in booking_rows]
        print(f"INFO: booking rows for session: {booking_ids}")

        # ── W2 fix: on the no-offer path, check for unexpected rows/escalations ──
        if offer_missed:
            if booking_rows:
                print(f"FAIL: {len(booking_rows)} booking row(s) without an offer — "
                      f"the defect itself: {booking_ids}")
                return 1
            print("INCONCLUSIVE: no find_service_center offer; the consent gate was not "
                  "exercised (exit 2)")
            return 2

        # ── evaluate verdict ───────────────────────────────────────────────────
        code, msg = evaluate(
            scenario=scenario,
            tools=tools,
            booking_rows=booking_rows,
            session_refusal_lines=session_refusal_lines,
            post_yes_refusal_lines=post_yes_refusal_lines,
            fallback_lines=fallback_lines,
            yes_sent_iso=yes_sent_iso,
            book_results_after_yes=book_results_after_yes,
        )
        print(msg)
        return code

    finally:
        _run_cleanup(
            idp=idp,
            pool=pool,
            user=user,
            created=created,
            history_table=history_table,
            vid=vid,
            session_id=session_id,
            start_ms=start_ms,
            stage=stage,
        )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--stage", default="staging", choices=["staging"])
    ap.add_argument("--region", default="us-west-2", choices=["us-west-2"])
    ap.add_argument("--allow-writes", action="store_true",
                    help="required: creates a throwaway user and (for yes scenarios) a booking row")
    ap.add_argument("--scenario", required=True, choices=["unasked", "typed-yes", "spoken-yes"],
                    help="which consent gate scenario to exercise")
    ap.add_argument("--max-tries", type=int, default=25)
    ap.add_argument("--timeout", type=float, default=45.0,
                    help="per-message receive timeout in seconds")
    ap.add_argument("--after", type=float, default=30.0,
                    help="seconds to wait after offer/answer before closing the session")
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
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
