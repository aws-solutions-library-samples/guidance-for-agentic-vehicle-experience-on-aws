#!/usr/bin/env python3
"""Live verification for FI Action Queue Stage A (spec 2026-10-02-fi-action-queue-stage-a, Group 4).

Runs against REAL deployed staging: the AVX REST API, the AVX DynamoDB tables, the
auto-approval evaluator on the findings stream, DMS's fleet repair-order route, CMS's
grounding and FI queue routes (item 11: one refused grounding request through AVX's
executor, and one queue read), and CloudWatch. Nothing is stubbed. Item 11 can't change
CMS data: CMS refuses its campaign id before any read, and its VIN is not a registry
vehicle, so even a reordered route would stop at the vehicle lookup.

What it creates, all deleted at the end (in ``finally``):
  * three ephemeral users in the CMS Cognito pool: a fleet-operator on fleet A, a
    fleet-operator on fleet B, and a platform-admin + fleet-operator (item 10 only);
  * ``LIVETEST-`` Findings, their decision Actions, and ``LIVETEST-`` approval rules.
Fleets are ``LIVETEST-FLEET-<A|B>-<run>``, which no iOS or CMS user holds (spec D12).

Items (tasks.md Group 4, live-verification task):
  1. fleet A's operator reads /findings/fleet/A (200) and is refused fleet B (403)
  2. operator approve of an acknowledge_only item: decided_by.id is the token's sub;
     fleet B's operator is refused the same item (403, AVX's own gate)
  3. approve with changed parameters: decision overridden
  4. decline: status and decision declined
  5. a rule (stored disabled, then enabled) auto-approves a new item within 2 minutes
  6. a ground_vehicle rule through the API: 400
  7. a ground_vehicle rule row written to the table directly: the evaluator receives the
     item (a new evaluator run record) and writes no decision
  8. run records exist for the diagnostics component and the evaluator; the diagnostics
     run-staleness alarm is OK. The daily components' records come after 02:xx UTC and are
     reported, not failed, when absent
  9. a forced evaluator failure moves the errors alarm to ALARM (pages the operator topic)
 10. a DMS 400 is recorded as failed with failure_reason and error_status (fires the D5
     operator escalation)
 11. contract v2.8 § 10b: a CMS refusal keeps CMS's own words. A ground_vehicle with a
     campaign id CMS's route rejects (``Invalid campaignId: '<id>'``, a 400 returned before
     any CMS read or write) is recorded declined/target_refused with error_status 400, the
     § 10a failure_reason, and target_reason equal to CMS's text. With
     ``--expect-cms-display`` it also checks CMS's queue route shows that text as the item's
     statusReason (needs CMS's matching change deployed); otherwise it reports what CMS shows.
Items 9 and 10 page whoever is subscribed; ``--no-paging`` skips them. Item 11 does not page
(a refusal never escalates).

G4 (complete_recall) and the G1-G4 blocks themselves have no live path in Stage A
(spec D5, D-FG5-4); they are unit- and mutation-tested only, and this script says so.

Usage:
  LIVE_TESTS=1 .venv/bin/python3 scripts/live-verify-fi-action-queue.py --stage staging --region us-west-2
Exit 0 pass, 1 fail, 2 skipped (LIVE_TESTS unset). Tokens and passwords are never printed.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Iterator

EXPECTED_DMS_400_REASON = "DMS did not accept the request (HTTP 400)."
EXPECTED_CMS_400_REASON = "CMS refused the request (HTTP 400)."


def cms_campaign_refusal(campaign_id: str) -> str:
    """CMS groundings route's 400 text for a campaign id outside ``^MRD-\\d{4}-\\d{3}$``
    (CMS services/fleet_intelligence/index.py ``_handle_groundings_create``)."""
    return f"Invalid campaignId: {campaign_id!r}"


# ── small helpers ────────────────────────────────────────────────────────────

def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def _iso(t: dt.datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")


def _started_at_lower_bound(iso: str) -> str:
    """Lower bound for a ``started_at >=`` key condition on the agent-runs table.

    The auto-approver's run records carry millisecond ``started_at`` (``...T11:36:01.881Z``,
    ``lambdas/avx-auto-approver/handler.py`` ``_now_iso``) and ``_iso(_now())`` is a whole second
    with a ``Z``. DynamoDB compares strings and ``.`` sorts below ``Z``, so ``...:01.881Z >=
    ...:01Z`` is false and a run that starts in the bound's own second would be missed. Without
    the ``Z`` the bound is a prefix of every value in that second, so it sorts below all of them
    and above every earlier second (issue ``2026-10-27-fi-sweep-avx-ms-run-start-readers``).
    """
    return iso[:-1] if iso.endswith("Z") else iso


def _evaluator_runs_since(runs: Any, ts: str) -> list:
    """The auto-approval evaluator's run records with ``started_at`` at or after ``ts``."""
    from boto3.dynamodb.conditions import Key  # noqa: PLC0415
    return runs.query(KeyConditionExpression=Key("agent_component").eq("agent-fi-evaluator#evaluator")
                      & Key("started_at").gte(_started_at_lower_bound(ts))).get("Items", [])


def decision_id(finding_id: str) -> str:
    """Contract § 4: the per-Finding decision action_id."""
    h = hashlib.sha256(f"{finding_id}|fi-decision".encode()).hexdigest()[:32]
    return f"AVX-ACT-{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _claims(id_token: str) -> dict:
    import base64
    payload = id_token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


class Http:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def call(self, method: str, path: str, token: str, body: Any = None) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.base + path, method=method, data=data,
            headers={"Authorization": token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw) if raw else {}
            except ValueError:
                return exc.code, {"raw": raw[:200].decode(errors="replace")}


class Results:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def check(self, item: str, ok: bool, detail: str) -> bool:
        self.rows.append((item, "PASS" if ok else "FAIL", detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {item}: {detail}", flush=True)
        return ok

    def note(self, item: str, detail: str) -> None:
        self.rows.append((item, "NOTE", detail))
        print(f"  [NOTE] {item}: {detail}", flush=True)

    @property
    def failed(self) -> list[tuple[str, str, str]]:
        return [r for r in self.rows if r[1] == "FAIL"]


# ── Cognito principals ───────────────────────────────────────────────────────

@contextlib.contextmanager
def principal(idp: Any, pool_id: str, client_id: str, label: str, groups: list[str],
              fleet_ids: str | None) -> Iterator[tuple[str, dict]]:
    """Yield (id_token, claims) for an ephemeral CMS-pool user; delete it in finally."""
    username = f"livetest-fi-{label}-{int(time.time())}-{secrets.token_hex(3)}@example.invalid"
    password = f"Lv{secrets.token_urlsafe(18)}!9Qz"
    attrs = [{"Name": "email", "Value": username}, {"Name": "email_verified", "Value": "true"}]
    if fleet_ids is not None:
        attrs.append({"Name": "custom:fleetIds", "Value": fleet_ids})
    created = False
    try:
        idp.admin_create_user(UserPoolId=pool_id, Username=username,
                              MessageAction="SUPPRESS", UserAttributes=attrs)
        created = True
        idp.admin_set_user_password(UserPoolId=pool_id, Username=username,
                                    Password=password, Permanent=True)
        for g in groups:
            idp.admin_add_user_to_group(UserPoolId=pool_id, Username=username, GroupName=g)
        auth = idp.initiate_auth(ClientId=client_id, AuthFlow="USER_PASSWORD_AUTH",
                                 AuthParameters={"USERNAME": username, "PASSWORD": password})
        token = auth["AuthenticationResult"]["IdToken"]
        print(f"  principal {label}: groups={groups} fleetIds={fleet_ids!r} (ephemeral)")
        yield token, _claims(token)
    finally:
        if created:
            try:
                idp.admin_delete_user(UserPoolId=pool_id, Username=username)
                print(f"  cleanup: deleted principal {label}")
            except Exception as exc:  # noqa: BLE001
                print(f"  cleanup: FAILED to delete {username}: {type(exc).__name__}. Run:\n"
                      f"    aws cognito-idp admin-delete-user --user-pool-id {pool_id} "
                      f"--username {username}", file=sys.stderr)


def password_client(idp: Any, pool_id: str) -> str:
    clients = idp.list_user_pool_clients(UserPoolId=pool_id, MaxResults=30)["UserPoolClients"]
    for c in clients:
        desc = idp.describe_user_pool_client(UserPoolId=pool_id, ClientId=c["ClientId"])
        if "ALLOW_USER_PASSWORD_AUTH" in (desc["UserPoolClient"].get("ExplicitAuthFlows") or []):
            return c["ClientId"]
    raise SystemExit(f"no app client on {pool_id} allows USER_PASSWORD_AUTH")


# ── fixtures ─────────────────────────────────────────────────────────────────

def make_finding(fid: str, fleet: str, rec: dict, *, agent: str = "agent-fi-cost",
                 kind: str = "fleet.cost.anomaly", vin: str | None = None) -> dict:
    now = _now()
    item: dict[str, Any] = {
        "finding_id": fid,
        "finding_key": hashlib.sha256(fid.encode()).hexdigest(),
        "agent_id": agent,
        "agent_version": "0.0.0-livetest",
        "scope": {"vin": vin} if vin else {"fleet_id": fleet},
        "fleet_id": fleet,  # storage-only projection the by_fleet GSI and the approve check read
        "severity": "attention",
        "computed_at": _iso(now),
        "expires_at": _iso(now + dt.timedelta(days=1)),
        "confidence": "high",
        "finding_kind": kind,
        "headline": "LIVETEST fixture (FI Action Queue Stage A live verification)",
        "detail": "LIVETEST fixture; deleted by scripts/live-verify-fi-action-queue.py.",
        "evidence": [],
        "inputs_hash": hashlib.sha256(b"livetest").hexdigest(),
        "tokens_used": 0,
        "cost_usd": 0,
        "audit_trail": [],
        "subject_key": fid,
        "recommended_action": rec,
    }
    if vin:
        item["vin"] = vin
    return item


ACK = {"action_kind": "acknowledge_only", "target_system": "none", "parameters": {}}


def wait_for(fn, timeout_s: float, every_s: float = 5.0):
    deadline = time.time() + timeout_s
    while True:
        value = fn()
        if value or time.time() >= deadline:
            return value
        time.sleep(every_s)


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    if os.environ.get("LIVE_TESTS") != "1":
        print("SKIPPED: set LIVE_TESTS=1. This writes LIVETEST- rows to the AVX staging tables, "
              "creates ephemeral Cognito users and (items 9-10) pages the operator topics.")
        return 2
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="staging")
    ap.add_argument("--region", default="us-west-2")
    ap.add_argument("--no-paging", action="store_true", help="skip items 9 and 10")
    ap.add_argument("--expect-cms-display", action="store_true",
                    help="item 11: require CMS's queue to show the target's words (CMS change deployed)")
    args = ap.parse_args()
    if args.stage != "staging":
        raise SystemExit("staging only (tasks.md Group 4)")

    import boto3
    from boto3.dynamodb.conditions import Key

    region, stage = args.region, args.stage
    ddb = boto3.resource("dynamodb", region_name=region)
    findings = ddb.Table(f"vsa-{stage}-avx-findings")
    actions = ddb.Table(f"vsa-{stage}-avx-actions")
    rules = ddb.Table(f"vsa-{stage}-avx-approval-rules")
    runs = ddb.Table(f"vsa-{stage}-avx-agent-runs")
    cw = boto3.client("cloudwatch", region_name=region)
    lam = boto3.client("lambda", region_name=region)
    idp = boto3.client("cognito-idp", region_name=region)
    ssm = boto3.client("ssm", region_name=region)

    outs = boto3.client("cloudformation", region_name=region).describe_stacks(
        StackName=f"vsa-{stage}-api")["Stacks"][0]["Outputs"]
    api = Http(next(o["OutputValue"] for o in outs if o["OutputKey"] == "RestApiUrl"))
    cms_api = Http(ssm.get_parameter(Name=f"/cvx/{stage}/cms-api-endpoint")["Parameter"]["Value"])
    pool_id = ssm.get_parameter(Name=f"/cvx/{stage}/voice-auth-user-pool-id")["Parameter"]["Value"]
    client_id = password_client(idp, pool_id)

    run = f"{int(time.time())}"
    fleet_a, fleet_b = f"LIVETEST-FLEET-A-{run}", f"LIVETEST-FLEET-B-{run}"
    fid = {n: f"LIVETEST-FIND-{run}-{n}" for n in ("2", "3", "4", "5", "7", "10", "11")}
    rule_ok, rule_guarded = f"LIVETEST-rule-ack-{run}", f"LIVETEST-rule-ground-{run}"
    started = _now()
    res = Results()
    written_findings: list[str] = []

    def put_finding(item: dict) -> None:
        findings.put_item(Item=item)
        written_findings.append(item["finding_id"])

    def stored_action(finding_id: str) -> dict | None:
        return actions.get_item(Key={"action_id": decision_id(finding_id)},
                                ConsistentRead=True).get("Item")

    print(f"run {run}: fleets {fleet_a} / {fleet_b}; API {api.base}")
    try:
        with principal(idp, pool_id, client_id, "opA", ["fleet-operator"], fleet_a) as (tok_a, cl_a), \
             principal(idp, pool_id, client_id, "opB", ["fleet-operator"], fleet_b) as (tok_b, _cl_b):
            res.check("setup", cl_a.get("custom:fleetIds") == fleet_a,
                      "fleet A operator's ID token carries custom:fleetIds")

            # Fixtures for items 1-4 (no rule exists yet for these fleets).
            for n in ("2", "4"):
                put_finding(make_finding(fid[n], fleet_a, ACK))
            put_finding(make_finding(fid["3"], fleet_a, {**ACK, "parameters": {"note": "as recommended"}}))

            print("\nItem 1: fleet read scope")
            s, body = api.call("GET", f"/findings/fleet/{fleet_a}", tok_a)
            ids = {f.get("finding_id") for f in (body.get("findings") or [])} if isinstance(body, dict) else set()
            res.check("1a", s == 200 and fid["2"] in ids, f"GET /findings/fleet/A as A -> {s}, sees fixture: {fid['2'] in ids}")
            s, _ = api.call("GET", f"/findings/fleet/{fleet_b}", tok_a)
            res.check("1b", s == 403, f"GET /findings/fleet/B as A -> {s}")

            print("\nItem 2: operator approve records the token's sub; fleet B is refused")
            s, _ = api.call("POST", f"/findings/{fid['2']}/approve", tok_b, ACK)
            res.check("2a", s == 403, f"fleet B operator approves fleet A's item -> {s}")
            s, body = api.call("POST", f"/findings/{fid['2']}/approve", tok_a, ACK)
            row = stored_action(fid["2"]) or {}
            res.check("2b", s == 200 and body.get("decision") == "approved",
                      f"approve -> {s}, decision={body.get('decision')!r}, status={body.get('status')!r}")
            res.check("2c", (row.get("decided_by") or {}) == {"kind": "operator", "id": cl_a["sub"]},
                      "stored decided_by == {kind: operator, id: <token sub>}")
            res.check("2d", row.get("fleet_id") == fleet_a and "fleet_id" not in body,
                      "stored Action carries fleet_id; the response strips it")
            s2, body2 = api.call("POST", f"/findings/{fid['2']}/approve", tok_a, ACK)
            res.check("2e", s2 == 200 and body2.get("action_id") == body.get("action_id"),
                      f"repeat approve -> {s2}, same action_id")
            s3, body3 = api.call("POST", f"/findings/{fid['2']}/decline", tok_a, {"reason": "LIVETEST"})
            res.check("2f", s3 == 409 and body3.get("error") == "already_decided",
                      f"decline after approve -> {s3} {body3.get('error')!r}")

            print("\nItem 3: approve with changed parameters is recorded overridden")
            s, body = api.call("POST", f"/findings/{fid['3']}/approve", tok_a,
                               {**ACK, "parameters": {"note": "changed by operator"}})
            res.check("3", s == 200 and body.get("decision") == "overridden",
                      f"approve with changed parameters -> {s}, decision={body.get('decision')!r}")

            print("\nItem 4: decline")
            s, body = api.call("POST", f"/findings/{fid['4']}/decline", tok_a, {"reason": "LIVETEST decline"})
            row = stored_action(fid["4"]) or {}
            res.check("4", s == 200 and row.get("status") == "declined" and row.get("decision") == "declined",
                      f"decline -> {s}, stored status={row.get('status')!r} decision={row.get('decision')!r}")

            print("\nItem 5: a rule auto-approves a new item")
            s, body = api.call("PUT", f"/approval-rules/fleet/{fleet_a}/{rule_ok}", tok_a,
                               {"action_kind": "acknowledge_only"})
            res.check("5a", s in (200, 201) and body.get("enabled") is False,
                      f"create rule -> {s}, enabled={body.get('enabled')!r}")
            s, body = api.call("PUT", f"/approval-rules/fleet/{fleet_a}/{rule_ok}", tok_a,
                               {"action_kind": "acknowledge_only", "enabled": True})
            res.check("5b", s == 200 and body.get("enabled") is True, f"enable rule -> {s}")
            s, body = api.call("GET", f"/approval-rules/fleet/{fleet_a}", tok_b)
            res.check("5c", s == 403, f"fleet B operator lists fleet A's rules -> {s}")
            t5 = time.time()
            put_finding(make_finding(fid["5"], fleet_a, ACK))
            row = wait_for(lambda: stored_action(fid["5"]), 120) or {}
            res.check("5d", row.get("decision") == "auto_approved"
                      and (row.get("decided_by") or {}) == {"kind": "rule", "id": f"{fleet_a}/{rule_ok}"},
                      f"evaluator decision after {time.time() - t5:.0f}s: decision={row.get('decision')!r}, "
                      f"status={row.get('status')!r}")

            print("\nItem 6: a guarded kind cannot be ruled through the API")
            s, _ = api.call("PUT", f"/approval-rules/fleet/{fleet_a}/{rule_guarded}-api", tok_a,
                            {"action_kind": "ground_vehicle"})
            res.check("6", s == 400, f"PUT rule for ground_vehicle -> {s}")

            print("\nItem 7: a guarded rule written to the table directly does nothing")
            rules.put_item(Item={"fleet_id": fleet_a, "rule_id": rule_guarded, "action_kind": "ground_vehicle",
                                 "enabled": True, "created_by": "livetest-direct-write",
                                 "updated_at": _iso(_now())})
            before = _iso(_now())
            put_finding(make_finding(fid["7"], fleet_a,
                                     {"action_kind": "ground_vehicle", "target_system": "cms", "parameters": {}},
                                     agent="agent-fi-rebalancing", kind="fleet.rebalance.gap"))

            def evaluator_ran_after(ts: str):
                return _evaluator_runs_since(runs, ts)
            ran = wait_for(lambda: evaluator_ran_after(before), 120)
            time.sleep(10)
            res.check("7a", bool(ran), f"the evaluator received the item (run records since: {len(ran or [])})")
            res.check("7b", stored_action(fid["7"]) is None, "no decision Action was written")
            res.note("7", "the evaluator's token-free gate stops this before dispatch(); the G2 block "
                          "itself has no live path in Stage A (D-FG5-4) and is unit- and mutation-tested")

            print("\nItem 8: run records and the diagnostics staleness alarm")
            diag = runs.query(KeyConditionExpression=Key("agent_component").eq("agent-2-vehicle-health#diagnostics")
                              & Key("started_at").gte(
                                  _started_at_lower_bound(_iso(started - dt.timedelta(hours=2))))
                              ).get("Items", [])
            res.check("8a", bool(diag), f"diagnostics run records in the last 2 h: {len(diag)}")
            ev = evaluator_ran_after(_iso(started))
            res.check("8b", bool(ev), f"evaluator run records this run: {len(ev)}")
            for comp in ("battery", "tires", "brakes"):
                daily = runs.query(KeyConditionExpression=Key("agent_component").eq(f"agent-2-vehicle-health#{comp}"),
                                   ScanIndexForward=False, Limit=1).get("Items", [])
                res.note(f"8-{comp}", f"latest run record: {daily[0]['started_at'] if daily else 'none yet (next 02:xx UTC run)'}")
            alarm = cw.describe_alarms(AlarmNames=[f"vsa-{stage}-avx-run-stale-diagnostics-{region}"])["MetricAlarms"]
            res.check("8c", bool(alarm) and alarm[0]["StateValue"] == "OK",
                      f"diagnostics run-staleness alarm state: {alarm[0]['StateValue'] if alarm else 'missing'}")

            print("\nItem 11: a CMS refusal keeps CMS's own words (contract v2.8 § 10b)")
            # The campaign id fails CMS's ^MRD-\d{4}-\d{3}$ check, which CMS makes before any
            # read or write, so CMS refuses with 400 and changes nothing.
            recall11 = f"LIVETEST-recall-{run}"
            want11 = cms_campaign_refusal(recall11)
            rec11 = {"action_kind": "ground_vehicle", "target_system": "cms",
                     "parameters": {"recall_id": recall11}}
            put_finding(make_finding(fid["11"], fleet_a, rec11, agent="agent-fi-recall-warranty",
                                     kind="fleet.recall.vehicle", vin="LVTSTAVX1" + run[-8:]))
            s, body = api.call("POST", f"/findings/{fid['11']}/approve", tok_a, rec11)
            last11 = ((body.get("audit_trail") or [{}])[-1]) if isinstance(body, dict) else {}
            stored11 = ((stored_action(fid["11"]) or {}).get("audit_trail") or [{}])[-1]
            res.check("11a", s == 200 and body.get("status") == "declined"
                      and last11.get("failure_kind") == "target_refused"
                      and int(last11.get("error_status", 0)) == 400
                      and last11.get("failure_reason") == EXPECTED_CMS_400_REASON,
                      f"approve -> {s}, status={body.get('status')!r}, "
                      f"failure_kind={last11.get('failure_kind')!r}, "
                      f"error_status={last11.get('error_status')!r}, "
                      f"failure_reason={last11.get('failure_reason')!r}")
            res.check("11b", last11.get("target_reason") == want11
                      and stored11.get("target_reason") == want11,
                      f"response and stored target_reason: {last11.get('target_reason')!r} / "
                      f"{stored11.get('target_reason')!r} (want {want11!r})")

            def cms_item11():
                st, qb = cms_api.call("GET", f"/api/v1/fleet-intelligence/actions?fleetId={fleet_a}", tok_a)
                items = (qb.get("items") or []) if isinstance(qb, dict) else []
                return next((i for i in items if i.get("findingId") == fid["11"]
                             and i.get("queueStatus") == "refused"), None)
            item11 = wait_for(cms_item11, 60) or {}
            shown = item11.get("statusReason")
            if args.expect_cms_display:
                res.check("11c", shown == want11,
                          f"CMS queue statusReason: {shown!r} (want {want11!r})")
            else:
                res.note("11c", f"CMS queue statusReason: {shown!r} (with CMS's change deployed it "
                                f"is {want11!r}; pass --expect-cms-display to require it)")

        if not args.no_paging:
            print("\nItem 9: forced evaluator failure moves the errors alarm to ALARM")
            name = f"vsa-{stage}-avx-auto-approver-errors-{region}"
            resp = lam.invoke(FunctionName=f"vsa-{stage}-avx-auto-approver-{region}",
                              Payload=json.dumps({"Records": ["LIVETEST-malformed-record"]}).encode())
            res.check("9a", resp.get("FunctionError") == "Unhandled",
                      f"malformed batch -> FunctionError={resp.get('FunctionError')!r}")
            t9 = time.time()
            state = wait_for(lambda: (cw.describe_alarms(AlarmNames=[name])["MetricAlarms"][0]["StateValue"]
                                      == "ALARM") or None, 600, 15)
            res.check("9b", bool(state), f"{name} -> ALARM after {time.time() - t9:.0f}s")

            print("\nItem 10: a DMS 400 is recorded as failed, with the reason and status")
            vin = "LVTSTAVX0000" + run[-5:]
            with principal(idp, pool_id, client_id, "admin", ["platform-admin", "fleet-operator"], None) \
                    as (tok_admin, _cl):
                # Stage C (G3-18 / G3-7 corollary): a booking recommendation carries recall_id,
                # so the approve route accepts it; the dealer stays OUTSIDE the DMS roster so
                # DMS still answers 400 and the check below still sees `failed`.
                rec10 = {"action_kind": "book_recall_service", "target_system": "dms",
                         "parameters": {"dealer_id": f"LIVETEST-dealer-{run}",
                                        "recall_id": f"LIVETEST-recall-{run}"}}
                put_finding(make_finding(fid["10"], fleet_a, rec10, agent="agent-fi-recall-warranty",
                                         kind="fleet.recall.vehicle", vin=vin))
                s, body = api.call("POST", f"/findings/{fid['10']}/approve", tok_admin, rec10)
            last = ((body.get("audit_trail") or [{}])[-1]) if isinstance(body, dict) else {}
            row = stored_action(fid["10"]) or {}
            stored_last = (row.get("audit_trail") or [{}])[-1]
            res.check("10a", s == 200 and body.get("status") == "failed"
                      and last.get("failure_kind") == "executor_terminal_error",
                      f"approve -> {s}, status={body.get('status')!r}, failure_kind={last.get('failure_kind')!r}")
            res.check("10b", last.get("failure_reason") == EXPECTED_DMS_400_REASON
                      and int(last.get("error_status", 0)) == 400,
                      f"response audit entry: failure_reason={last.get('failure_reason')!r}, "
                      f"error_status={last.get('error_status')!r}")
            res.check("10c", stored_last.get("failure_reason") == EXPECTED_DMS_400_REASON
                      and int(stored_last.get("error_status", 0)) == 400
                      and "400" not in str(stored_last.get("note", "")),
                      "stored audit entry carries the same reason and status; note carries no status")
            res.note("10", "D-FG5-3 (no owner card for a decision Action) has no discriminating live "
                           "check: this Action carries no owner_id; it is unit- and mutation-tested only")
        else:
            res.note("9-10", "skipped (--no-paging)")
        res.note("G4", "complete_recall is not executable in Stage A, so G4 has no live path (spec D5)")
    finally:
        print("\nTeardown")
        for f in written_findings:
            findings.delete_item(Key={"finding_id": f})
            actions.delete_item(Key={"action_id": decision_id(f)})
        for r in (rule_ok, rule_guarded, f"{rule_guarded}-api"):
            rules.delete_item(Key={"fleet_id": fleet_a, "rule_id": r})
        print(f"  deleted {len(written_findings)} Findings, their decision Actions, and the LIVETEST rules")

        # Residue: nothing LIVETEST- may survive in the three tables.
        def scan_count(table, expr, names, values) -> int:
            n, kwargs = 0, {"FilterExpression": expr, "ExpressionAttributeNames": names,
                            "ExpressionAttributeValues": values, "ProjectionExpression": "#k"}
            while True:
                page = table.scan(**kwargs)
                n += len(page.get("Items", []))
                if "LastEvaluatedKey" not in page:
                    return n
                kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        residue = {
            "findings": scan_count(findings, "begins_with(#k, :p)", {"#k": "finding_id"}, {":p": "LIVETEST-"}),
            "actions": scan_count(actions, "begins_with(#k, :p)", {"#k": "finding_id"}, {":p": "LIVETEST-"}),
            "rules": scan_count(rules, "begins_with(#k, :p)", {"#k": "rule_id"}, {":p": "LIVETEST-"}),
        }
        res.check("residue", not any(residue.values()), f"LIVETEST- rows left: {residue}")

    print("\nSummary")
    for item, status, detail in res.rows:
        print(f"  {status:4}  {item:10}  {detail}")
    if res.failed:
        print(f"\nFAIL: {len(res.failed)} check(s) failed")
        return 1
    print("\nPASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
