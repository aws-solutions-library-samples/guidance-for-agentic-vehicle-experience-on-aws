#!/usr/bin/env python3
"""Live check: a carded Finding that escalates to p0 reaches the push path once, and a
lost p0 push pages the operator topic.

Issues ``issues/2026-10-23-tier2-writer-frozen-severity`` and
``issues/2026-10-23-lost-p0-push-not-alarmed`` (design § W1, B1, L1). Written with the
fix; NOT run before the combined tier2 deploy that ships it.

What it proves against the DEPLOYED broker and alarm
----------------------------------------------------
Staging has no SNS platform application (``aws sns list-platform-applications`` in
us-west-2 was empty on 2026-10-23), so no push there can succeed. The check therefore
uses a device row whose endpoint ARN names no endpoint: every publish fails with
NotFound, which is exactly the lost-push path. It shows:

1. Escalation reaches the push: a ``soon`` Finding is carded, then the same condition is
   written at ``p0`` (the real Tier 2 writer, C0161 text). The deployed broker takes the
   escalation path on the existing card, attempts the push, records the ledger row
   ``failed`` and logs ``push_not_delivered severity=p0 reason=publish_failed``. The
   stored Finding says ``p0`` and its headline carries the C0161 directive verbatim.
2. A lost p0 push alarms: the EMF metric ``AVX/Broker PushNotDelivered {Stage, Severity=p0}``
   gets a datapoint, the alarm ``vsa-{stage}-avx-broker-p0-push-lost-{region}`` goes to
   ALARM, and its action history shows the SNS action succeeded. **This emails the
   operator topic's subscriber**, which is why ``--allow-page`` is required.
3. A delivered p0 is not pushed again: the ledger row is set to ``published`` (what a
   successful publish leaves), the same Finding is written at ``p0`` once more, and the
   broker logs ``already_carded`` for it with no push attempt after that write.

The writer half is exercised by the script's own local writer (this checkout); the
precondition step also checks the DEPLOYED Tier 2 bundle carries the same writer change.

Data it writes (all synthetic, all deleted in ``finally``): one device row and its rate
rows for a random Cognito-sub-shaped owner that is not a real user, two Finding writes
on a random VIN with the unassigned WMI ``ZZZ``, the broker's card for it. No real
vehicle, owner, phone or Cognito user is touched; the Tier 2 schedules never read this
VIN, so no write window applies. The alarm returns to OK by itself once the datapoint
ages out (NOT_BREACHING, no OK action); the script waits for that and does not force it.

Usage::

    P0_ESCALATION_LIVE_GO=staging LIVE_TESTS=1 python3 scripts/live-verify-p0-escalation.py --allow-writes --allow-page
    P0_ESCALATION_LIVE_GO=staging LIVE_TESTS=1 python3 scripts/live-verify-p0-escalation.py --allow-writes \\
        --cleanup-only --run-id <id>
    LIVE_TESTS=1 python3 scripts/live-verify-p0-escalation.py --pre-deploy   # read-only,
        # run BEFORE the deploy: cards that would push once at the transition

Exit codes: 0 all checks passed, 1 a check or cleanup failed, 1 refused without the go,
2 skipped (no LIVE_TESTS, or a write flag missing).
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import secrets
import signal
import sys
import time
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Live-write go (shared helper: scripts/lib/live_write_guard.py)
# ---------------------------------------------------------------------------

_HERE_P0 = Path(__file__).resolve().parent
if str(_HERE_P0 / "lib") not in sys.path:
    sys.path.insert(0, str(_HERE_P0 / "lib"))
import live_write_guard as _lwg  # noqa: E402

#: Set to ``--stage`` by the live-run step, on the user's go, and by nothing else.
_LIVE_GO_ENV = "P0_ESCALATION_LIVE_GO"

STAGE = "staging"
REGION = "us-west-2"

RUN_ID_RE = re.compile(r"^p0esc-\d{8}T\d{6}Z-[0-9a-f]{6}$")
_VIN_CHARS = "ABCDEFGHJKLMNPRSTUVWXYZ0123456789"  # no I, O, Q
_COGNITO_SUB_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

MANIFEST_DIR = Path.home() / ".cache" / "avx-live-verify" / "p0-escalation"

FINDINGS_TABLE = f"vsa-{STAGE}-avx-findings"
ACTIONS_TABLE = f"vsa-{STAGE}-avx-actions"
RATE_TABLE = f"vsa-{STAGE}-avx-rate-limits"
DEVICES_TABLE = f"vsa-{STAGE}-notification-devices"
BROKER_FN = f"vsa-{STAGE}-avx-broker-{REGION}"
TIER2_FN = f"vsa-{STAGE}-tier2-health-agent-{REGION}"
BROKER_LOG_GROUP = f"/aws/lambda/{BROKER_FN}"
ALARM_NAME = f"vsa-{STAGE}-avx-broker-p0-push-lost-{REGION}"
OPERATOR_TOPIC_NAME = f"vsa-{STAGE}-tier2-operator-alerts"
SSM_PREFIX = f"/vsa/{STAGE}/avx/broker/"

# Markers the DEPLOYED code must carry: the fix's identifiers plus the lines that carry
# its properties (review cycle 1 S3), so a bundle with the names but an older body fails.
BROKER_MARKERS = (
    "def _push_p0_once", "PushNotDelivered", '_P0_LEDGER_PREFIX = "p0:"',
    # Review cycle 2 W1: the claim refuses only a published row, and a p0 whose card
    # write conflicts goes on to the push.
    '"attribute_not_exists(window_key) OR attribute_not_exists(#st) OR #st <> :published"',
    "if not created and severity != _P0:",
    'if not created and push_outcome == "already_notified":',
)
WRITER_MARKERS = (
    'update_expr += ", #severity = :severity"',
    'severity_unchanged = live_row.get("severity") == finding.get("severity")',
)
#: Cleanup waits this long after removing the device row, so a delivery still in the
#: stream stops at consent_off before the rest is deleted (review cycle 1 S2). At least
#: the broker's 30 s Lambda timeout plus a margin (review cycle 2 S5): a delivery that
#: read consent just before the row went can still be running for that long. Pinned
#: against the stack in scripts/tests/test_live_verify_p0_escalation.py.
CLEANUP_DRAIN_SECONDS = 35

# ---------------------------------------------------------------------------
# Output that survives a dead terminal
# ---------------------------------------------------------------------------


def _out(msg: str, *, err: bool = False) -> None:
    try:
        print(msg, file=sys.stderr if err else sys.stdout, flush=True)
    except (OSError, ValueError):
        pass


class Results:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []
        self.cleanup_failed = False

    def check(self, label: str, ok: bool, detail: str = "", on_fail: str = "") -> bool:
        status = "PASS" if ok else "FAIL"
        text = detail if ok else (on_fail or detail)
        self.rows.append((label, status, text))
        _out(f"  [{status}] {label}{(': ' + text) if text else ''}")
        return ok

    def note(self, label: str, detail: str) -> None:
        self.rows.append((label, "NOTE", detail))
        _out(f"  [NOTE] {label}: {detail}")

    @property
    def failed(self) -> bool:
        return self.cleanup_failed or any(s == "FAIL" for _, s, _ in self.rows)

    def summary(self) -> None:
        passes = sum(1 for _, s, _ in self.rows if s == "PASS")
        fails = sum(1 for _, s, _ in self.rows if s == "FAIL")
        _out(f"\nSUMMARY: {passes} passed, {fails} failed"
             + (" (cleanup failed)" if self.cleanup_failed else ""))


# ---------------------------------------------------------------------------
# Signals: one hold around the whole cleanup
# ---------------------------------------------------------------------------

_SIGNAL_STATE = {"installed": False}


def _install_signal_handlers() -> None:
    """SIGTERM/SIGHUP become SystemExit(143), so ``finally`` blocks run."""
    def _term(signum: int, frame: object) -> None:
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGHUP, _term)
    _SIGNAL_STATE["installed"] = True


@contextlib.contextmanager
def _signals_held() -> Iterator[None]:
    held: list[int] = []

    def _hold(signum: int, frame: object) -> None:
        held.append(signum)

    sigs = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    old = {s: signal.getsignal(s) for s in sigs}
    for s in sigs:
        signal.signal(s, _hold)
    try:
        yield
    finally:
        for s, handler in old.items():
            signal.signal(s, handler)
        if held:
            if held[0] == signal.SIGINT:
                raise KeyboardInterrupt
            raise SystemExit(143)


# ---------------------------------------------------------------------------
# Run identity and manifest
# ---------------------------------------------------------------------------


def new_run_id(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"p0esc-{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"


def validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
        raise SystemExit(f"refusing: run id {run_id!r} does not match {RUN_ID_RE.pattern}")
    return run_id


def new_vin() -> str:
    return "ZZZ" + "".join(secrets.choice(_VIN_CHARS) for _ in range(14))


def _manifest_path(run_id: str, base: Path | None = None) -> Path:
    return (base or MANIFEST_DIR) / f"{validate_run_id(run_id)}.json"


def save_manifest(manifest: dict[str, Any], base: Path | None = None) -> Path:
    """Atomic 0600 write in a 0700 directory."""
    path = _manifest_path(manifest["run_id"], base)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
    return path


def load_manifest(run_id: str, base: Path | None = None) -> dict[str, Any]:
    data = json.loads(_manifest_path(run_id, base).read_text())
    if data.get("run_id") != run_id or data.get("stage") != STAGE:
        raise SystemExit("refusing: manifest run_id/stage mismatch")
    if not _COGNITO_SUB_RE.match(str(data.get("owner", ""))):
        raise SystemExit("refusing: manifest owner is not a sub-shaped synthetic id")
    if not re.match(r"^ZZZ[A-HJ-NPR-Z0-9]{14}$", str(data.get("vin", ""))):
        raise SystemExit("refusing: manifest VIN is not a ZZZ test VIN")
    for fid in data.get("finding_ids") or []:
        if not re.match(r"^FIND-[0-9a-f-]{36}$", str(fid)):
            raise SystemExit(f"refusing: manifest finding id {fid!r} has the wrong shape")
    return data


# ---------------------------------------------------------------------------
# Request guard: the script can only touch its own rows
# ---------------------------------------------------------------------------


class GuardViolation(RuntimeError):
    pass


_READ_OPS = {
    ("lambda", "GetFunction"), ("cloudwatch", "DescribeAlarms"),
    ("cloudwatch", "DescribeAlarmHistory"), ("cloudwatch", "GetMetricStatistics"),
    ("sns", "ListSubscriptionsByTopic"), ("ssm", "GetParameter"),
    ("logs", "FilterLogEvents"), ("sts", "GetCallerIdentity"),
    ("dynamodb", "GetItem"), ("dynamodb", "Query"), ("dynamodb", "Scan"),
}
_WRITE_OPS = {("dynamodb", "PutItem"), ("dynamodb", "UpdateItem"), ("dynamodb", "DeleteItem")}


def _s(v: Any) -> str | None:
    return v.get("S") if isinstance(v, dict) else None


def make_guard(manifest: dict[str, Any], read_only: bool = False) -> Callable[..., None]:
    """A botocore ``before-parameter-build`` handler admitting only this run's requests.

    ``before-parameter-build`` hands over the caller's own keyword arguments (``params``)
    and the operation model, before serialisation; ``before-call`` would see the
    serialised request instead. ``read_only`` refuses every write, whatever its target
    (the ``--pre-deploy`` count, review cycle 2 S6c: its manifest has no owner or VIN,
    so the per-row checks below cannot be what keeps it read-only).
    """
    def guard(model: Any, params: dict[str, Any], **_: Any) -> None:
        service = model.service_model.service_name
        op = model.name
        if (service, op) not in _READ_OPS | _WRITE_OPS:
            raise GuardViolation(f"{service}.{op} is not allowed")
        if read_only and (service, op) in _WRITE_OPS:
            raise GuardViolation(f"{service}.{op} in a read-only run")
        if service == "ssm" and not str(params.get("Name", "")).startswith(SSM_PREFIX):
            raise GuardViolation("ssm parameter outside the broker prefix")
        if service == "logs" and params.get("logGroupName") != BROKER_LOG_GROUP:
            raise GuardViolation("log group is not the broker's")
        if service == "lambda" and params.get("FunctionName") not in (BROKER_FN, TIER2_FN):
            raise GuardViolation("lambda function not allowed")
        if service != "dynamodb":
            return
        table = params.get("TableName")
        if table not in (FINDINGS_TABLE, ACTIONS_TABLE, RATE_TABLE, DEVICES_TABLE):
            raise GuardViolation(f"table {table!r} not allowed")
        if (service, op) not in _WRITE_OPS:
            # Security review cycle 1 S3: only the read-only pre-deploy count scans.
            if op == "Scan" and (table != ACTIONS_TABLE or not read_only):
                raise GuardViolation("only the Actions table may be scanned, and only in the "
                                     "read-only pre-deploy count")
            if op == "Query" and table in (RATE_TABLE, DEVICES_TABLE):
                vals = params.get("ExpressionAttributeValues") or {}
                if _s(vals.get(":o")) != manifest["owner"]:
                    raise GuardViolation("query of another owner's partition")
            # Security review cycle 1 S2: in a write run every point read is this run's row.
            # The read-only count reads other owners' rows by design (it has no manifest).
            if op == "GetItem" and not read_only:
                key = params.get("Key") or {}
                if table in (RATE_TABLE, DEVICES_TABLE) and _s(key.get("owner_id")) != manifest["owner"]:
                    raise GuardViolation("read of another owner's row")
                if table == FINDINGS_TABLE and \
                        _s(key.get("finding_id")) not in (manifest.get("finding_ids") or []):
                    raise GuardViolation("Finding read outside this run's ids")
                if table == ACTIONS_TABLE and \
                        _s(key.get("action_id")) not in (manifest.get("action_ids") or []):
                    raise GuardViolation("Action read outside this run's ids")
            return
        owner = manifest["owner"]
        if table in (RATE_TABLE, DEVICES_TABLE):
            key = params.get("Key") or params.get("Item") or {}
            if _s(key.get("owner_id")) != owner:
                raise GuardViolation("write to another owner's row")
            return
        if table == FINDINGS_TABLE:
            if op == "PutItem":
                scope = (params.get("Item") or {}).get("scope", {}).get("M", {})
                if _s(scope.get("vin")) != manifest["vin"]:
                    raise GuardViolation("Finding put for another VIN")
                return
            fid = _s((params.get("Key") or {}).get("finding_id"))
            if fid not in (manifest.get("finding_ids") or []):
                raise GuardViolation("Finding write outside this run's ids")
            return
        if table == ACTIONS_TABLE:
            if op != "DeleteItem":
                raise GuardViolation("the script never writes Actions; it only deletes its own")
            aid = _s((params.get("Key") or {}).get("action_id"))
            if aid not in (manifest.get("action_ids") or []):
                raise GuardViolation("Action delete outside this run's ids")
    return guard


def guarded_clients(manifest: dict[str, Any], session_factory: Callable[[], Any] | None = None,
                    read_only: bool = False) -> dict[str, Any]:
    import boto3  # noqa: PLC0415
    session = (session_factory or (lambda: boto3.session.Session(region_name=REGION)))()
    session.events.register("before-parameter-build", make_guard(manifest, read_only))
    return {name: session.client(name, region_name=REGION)
            for name in ("dynamodb", "lambda", "cloudwatch", "sns", "ssm", "logs", "sts")}


# ---------------------------------------------------------------------------
# Preconditions (read-only)
# ---------------------------------------------------------------------------


def _deployed_file(lam: Any, fn: str, member: str,
                   fetch: Callable[[str], bytes] | None = None) -> str | None:
    loc = lam.get_function(FunctionName=fn)["Code"]["Location"]
    if not loc.startswith("https://"):
        return None
    data = (fetch or (lambda u: urllib.request.urlopen(u, timeout=60).read()))(loc)  # noqa: S310
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        try:
            return zf.read(member).decode("utf-8")
        except KeyError:
            return None


def topic_arn(account: str) -> str:
    return f"arn:aws:sns:{REGION}:{account}:{OPERATOR_TOPIC_NAME}"


def check_preconditions(c: dict[str, Any], res: Results, account: str,
                        fetch: Callable[[str], bytes] | None = None) -> bool:
    ok = True
    broker_src = _deployed_file(c["lambda"], BROKER_FN, "agents/supervisor/lambdas/avx_broker.py",
                                fetch)
    ok &= res.check("deployed broker carries the p0 ledger and lost-push metric",
                    bool(broker_src) and all(m in broker_src for m in BROKER_MARKERS),
                    detail="markers found",
                    on_fail="deploy the combined tier2 change first")
    writer_src = _deployed_file(c["lambda"], TIER2_FN, "agents/tier2/writer.py", fetch)
    ok &= res.check("deployed Tier 2 writer SETs severity in place",
                    bool(writer_src) and all(m in writer_src for m in WRITER_MARKERS),
                    detail="marker found", on_fail="deploy the combined tier2 change first")

    alarms = c["cloudwatch"].describe_alarms(AlarmNames=[ALARM_NAME]).get("MetricAlarms", [])
    a = alarms[0] if len(alarms) == 1 else {}
    dims = sorted((d["Name"], d["Value"]) for d in a.get("Dimensions", []))
    ok &= res.check(
        "alarm watches PushNotDelivered {Stage, Severity=p0} and pages the operator topic",
        bool(a) and a.get("Namespace") == "AVX/Broker"
        and a.get("MetricName") == "PushNotDelivered"
        and dims == [("Severity", "p0"), ("Stage", STAGE)]
        and a.get("ActionsEnabled") is True
        and a.get("AlarmActions") == [topic_arn(account)]
        and a.get("TreatMissingData") == "notBreaching",
        detail=ALARM_NAME, on_fail=f"alarm config differs or is missing: {a or 'absent'}")
    ok &= res.check("alarm is not already in ALARM",
                    a.get("StateValue") in ("OK", "INSUFFICIENT_DATA"),
                    detail=str(a.get("StateValue")),
                    on_fail=f"state {a.get('StateValue')}: the run's transition could not be seen")

    subs = c["sns"].list_subscriptions_by_topic(TopicArn=topic_arn(account)).get(
        "Subscriptions", [])
    confirmed = [s for s in subs if str(s.get("SubscriptionArn", "")).startswith("arn:aws:sns:")]
    ok &= res.check("operator topic has a confirmed subscriber", bool(confirmed),
                    detail=f"{len(confirmed)} confirmed",
                    on_fail="no confirmed subscription: the page would reach nobody")

    rate = json.loads(c["ssm"].get_parameter(Name=f"{SSM_PREFIX}rate-limit")["Parameter"]["Value"])
    cap = int(c["ssm"].get_parameter(Name=f"{SSM_PREFIX}cardinality-cap")["Parameter"]["Value"])
    ok &= res.check("broker policy leaves room for the run's two pushes",
                    int(rate.get("hour", 0)) >= 2 and int(rate.get("day", 0)) >= 2 and cap >= 1,
                    detail=f"rate={rate} cap={cap}", on_fail=f"rate={rate} cap={cap}")
    return bool(ok)


# ---------------------------------------------------------------------------
# The writes (the real Tier 2 writer)
# ---------------------------------------------------------------------------

_SOON_TEXT = "Front brake pads are wearing faster than expected. Book a service soon."


def _p0_text() -> str:
    from agents.health.prompt import inject_p0_verbatim_phrases  # noqa: PLC0415
    return inject_p0_verbatim_phrases(
        "Brake pressure readings dropped sharply on the last two trips.", ["C0161"])


def build_finding(vin: str, severity_hint: str, text: str, tag: str) -> dict[str, Any]:
    from agents.tier2 import runner  # noqa: PLC0415
    from agents.tier2.finding_transform import health_artifact_to_finding  # noqa: PLC0415
    rows = [{"vin": vin, "livetest_tag": tag}]
    fields = {"finding": text, "severity_hint": severity_hint, "confidence": "high",
              "evidence": [{"source": "livetest.p0_escalation", "value": {"tag": tag},
                            "provenance": "live"}]}
    artifact, _ = runner._build_artifact(vin, "brakes", rows, fields)
    return health_artifact_to_finding(artifact)


def write(ddb: Any, manifest: dict[str, Any], finding: dict[str, Any],
          base: Path | None) -> dict[str, Any]:
    from agents.tier2.owner_resolver import StandingFacts  # noqa: PLC0415
    from agents.tier2.writer import write_finding  # noqa: PLC0415
    ids = manifest.setdefault("finding_ids", [])
    if finding["finding_id"] not in ids:
        ids.append(finding["finding_id"])
    save_manifest(manifest, base)  # recorded before the request
    result = write_finding(finding, table_name=FINDINGS_TABLE, dynamodb_client=ddb,
                           standing=StandingFacts(owner_id=manifest["owner"], fleet_id=None,
                                                  vehicle_id=None))
    if result["finding_id"] not in ids:
        ids.append(result["finding_id"])
        save_manifest(manifest, base)
    return result


def card_id(finding_id: str, owner: str) -> str:
    sys.path.insert(0, str(_REPO_ROOT / "agents" / "supervisor" / "lambdas"))
    import avx_broker  # noqa: PLC0415
    action_id, _, _ = avx_broker._card_identity(
        {"finding_id": finding_id, "finding_kind": "health.brakes"}, owner)
    return action_id


def _poll(fn: Callable[[], Any], timeout_s: float, interval_s: float = 3.0,
          sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time
          ) -> Any:
    deadline = clock() + timeout_s
    while True:
        value = fn()
        if value:
            return value
        if clock() >= deadline:
            return value
        sleep(interval_s)


def _get(ddb: Any, table: str, key: dict[str, Any]) -> dict[str, Any] | None:
    return ddb.get_item(TableName=table, Key=key, ConsistentRead=True).get("Item")


def _broker_lines(logs: Any, finding_id: str, since_ms: int) -> list[str]:
    out, token = [], None
    for _ in range(20):
        kw = {"logGroupName": BROKER_LOG_GROUP, "startTime": since_ms,
              "filterPattern": f'"{finding_id}"'}
        if token:
            kw["nextToken"] = token
        resp = logs.filter_log_events(**kw)
        out.extend(e.get("message", "") for e in resp.get("events", []))
        token = resp.get("nextToken")
        if not token:
            return out
    raise RuntimeError("broker log search did not finish within 20 pages")


def run_checks(c: dict[str, Any], manifest: dict[str, Any], res: Results, account: str,
               base: Path | None, sleep: Callable[[float], None] = time.sleep) -> None:
    from agents.health.prompt import C0161_VERBATIM_REMEDY  # noqa: PLC0415
    ddb, owner, vin = c["dynamodb"], manifest["owner"], manifest["vin"]

    # Device row with an endpoint ARN that names no endpoint: every publish fails.
    manifest["device_written"] = True
    save_manifest(manifest, base)
    ddb.put_item(TableName=DEVICES_TABLE, Item={
        "owner_id": {"S": owner}, "device_token": {"S": manifest["device_token"]},
        "endpoint_arn": {"S": f"arn:aws:sns:{REGION}:{account}:endpoint/APNS_SANDBOX/"
                              f"livetest/{uuid.uuid4()}"},
        "per_category_consent": {"M": {"safety": {"BOOL": True}, "coverage": {"BOOL": True},
                                       "service": {"BOOL": True}}},
        "platform": {"S": "livetest"},
    })

    first = write(ddb, manifest, build_finding(vin, "soon", _SOON_TEXT, "a"), base)
    fid = first["finding_id"]
    action_id = card_id(fid, owner)
    manifest.setdefault("action_ids", [])
    if action_id not in manifest["action_ids"]:
        manifest["action_ids"].append(action_id)
        save_manifest(manifest, base)
    card = _poll(lambda: _get(ddb, ACTIONS_TABLE, {"action_id": {"S": action_id}}), 120, sleep=sleep)
    if not res.check("the soon Finding is carded by the deployed broker", bool(card),
                     detail=action_id, on_fail="no card after 120 s"):
        return

    t_escalate = int(time.time() * 1000) - 5000
    second = write(ddb, manifest, build_finding(vin, "p0", _p0_text(), "b"), base)
    res.check("the p0 write updates the same Finding in place",
              second == {"action": "update", "finding_id": fid}, detail=fid,
              on_fail=f"writer returned {second}")
    stored = _get(ddb, FINDINGS_TABLE, {"finding_id": {"S": fid}}) or {}
    res.check("the stored Finding says p0 with the C0161 directive verbatim",
              _s(stored.get("severity")) == "p0"
              and C0161_VERBATIM_REMEDY in (_s(stored.get("headline")) or ""),
              detail="severity p0, directive in headline",
              on_fail=f"severity={_s(stored.get('severity'))}")

    ledger_key = {"owner_id": {"S": owner}, "window_key": {"S": f"p0:{fid}"}}
    ledger = _poll(lambda: (lambda i: i if i and _s(i.get("state")) in ("failed", "published")
                            else None)(_get(ddb, RATE_TABLE, ledger_key)), 120, sleep=sleep)
    if not res.check("the deployed broker attempted the escalation push and recorded it failed",
                     bool(ledger) and _s(ledger.get("state")) == "failed",
                     detail="ledger state failed",
                     on_fail=f"ledger after 120 s: {ledger}"):
        return
    lines = _poll(lambda: [l for l in _broker_lines(c["logs"], fid, t_escalate)
                           if "push_not_delivered severity=p0 reason=publish_failed" in l],
                  90, sleep=sleep)
    res.check("the broker logged the lost p0 push", bool(lines), detail=f"{len(lines)} line(s)",
              on_fail="no push_not_delivered line")

    def _datapoints() -> float:
        now = datetime.now(timezone.utc)
        resp = c["cloudwatch"].get_metric_statistics(
            Namespace="AVX/Broker", MetricName="PushNotDelivered",
            Dimensions=[{"Name": "Stage", "Value": STAGE}, {"Name": "Severity", "Value": "p0"}],
            StartTime=datetime.fromtimestamp(t_escalate / 1000 - 300, timezone.utc), EndTime=now,
            Period=60, Statistics=["Sum"])
        return sum(p.get("Sum", 0) for p in resp.get("Datapoints", []))
    total = _poll(_datapoints, 300, interval_s=15, sleep=sleep)
    res.check("the EMF metric has a p0 datapoint", bool(total) and total >= 1,
              detail=f"sum={total}", on_fail="no datapoint after 5 min")

    def _alarm_action() -> dict | None:
        resp = c["cloudwatch"].describe_alarm_history(
            AlarmName=ALARM_NAME, HistoryItemType="Action", MaxRecords=5,
            ScanBy="TimestampDescending")
        for item in resp.get("AlarmHistoryItems", []):
            ts = item.get("Timestamp")
            if ts and ts.timestamp() * 1000 >= t_escalate:
                return item
        return None
    action = _poll(_alarm_action, 480, interval_s=15, sleep=sleep)
    state = ""
    if action:
        try:
            state = json.loads(action.get("HistoryData", "{}")).get("actionState", "")
        except json.JSONDecodeError:
            state = "unparseable"
    res.check("the alarm fired and its SNS action succeeded", state == "Succeeded",
              detail="actionState=Succeeded", on_fail=f"action history: {action}")

    # A delivered p0 is not pushed again: mark the ledger as a delivered push would.
    ddb.update_item(TableName=RATE_TABLE, Key=ledger_key,
                    UpdateExpression="SET #st = :p, outcome = :o",
                    ExpressionAttributeNames={"#st": "state"},
                    ExpressionAttributeValues={":p": {"S": "published"},
                                               ":o": {"S": "livetest_marked_published"}})
    t_repeat = int(time.time() * 1000)
    write(ddb, manifest, build_finding(vin, "p0", _p0_text(), "c"), base)
    seen = _poll(lambda: [l for l in _broker_lines(c["logs"], fid, t_repeat)
                          if "already_carded" in l], 120, sleep=sleep)
    sleep(20)  # let any (wrong) push attempt for the same record land in the log
    # A push attempt on this path logs "p0 escalation on carded finding ... finding_id=<id>"
    # first, and a failed one "push_not_delivered ... finding_id=<id>" (the apns_push
    # failure line itself carries no finding id).
    attempts = [l for l in _broker_lines(c["logs"], fid, t_repeat)
                if "p0 escalation on carded finding" in l or "push_not_delivered" in l
                or "apns_push: published" in l]
    res.check("a repeat p0 write after a delivered push is already_carded", bool(seen),
              detail="already_carded logged",
              on_fail="no already_carded line for the repeat write")
    res.check("a repeat p0 write after a delivered push makes no push attempt", not attempts,
              detail="0 push attempts", on_fail=f"{len(attempts)} push attempt line(s)")


# ---------------------------------------------------------------------------
# Cleanup and residue (one hold, one pass, every exit)
# ---------------------------------------------------------------------------


def _delete(ddb: Any, res: Results, label: str, **kw: Any) -> None:
    for attempt in range(3):
        try:
            ddb.delete_item(**kw)
            return
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                return  # gone, or not ours: the residue read decides
            if attempt == 2:
                res.cleanup_failed = True
                _out(f"cleanup: {label} delete failed: {type(exc).__name__}", err=True)
                return
            time.sleep(1 + attempt)


def cleanup(c: dict[str, Any], manifest: dict[str, Any], res: Results,
            sleep: Callable[[float], None] = time.sleep) -> None:
    """Device row first: a broker delivery still in the stream then stops at consent_off
    and writes nothing, so the drain wait is all it needs before the rest goes."""
    ddb, owner = c["dynamodb"], manifest["owner"]
    if manifest.get("device_written"):
        _delete(ddb, res, "device row", TableName=DEVICES_TABLE,
                Key={"owner_id": {"S": owner}, "device_token": {"S": manifest["device_token"]}})
        sleep(CLEANUP_DRAIN_SECONDS)
    for fid in manifest.get("finding_ids") or []:
        _delete(ddb, res, f"finding {fid}", TableName=FINDINGS_TABLE,
                Key={"finding_id": {"S": fid}},
                ConditionExpression="#sc.vin = :v",
                ExpressionAttributeNames={"#sc": "scope"},  # SCOPE is a reserved word
                ExpressionAttributeValues={":v": {"S": manifest["vin"]}})
    for aid in manifest.get("action_ids") or []:
        _delete(ddb, res, f"action {aid}", TableName=ACTIONS_TABLE,
                Key={"action_id": {"S": aid}},
                ConditionExpression="created_by = :c",
                ExpressionAttributeValues={":c": {"S": f"owner-{owner}"}})
    try:
        for row in _partition(ddb, RATE_TABLE, owner):
            _delete(ddb, res, f"rate row {_s(row.get('window_key'))}", TableName=RATE_TABLE,
                    Key={"owner_id": {"S": owner}, "window_key": row["window_key"]})
    except Exception as exc:  # noqa: BLE001
        res.cleanup_failed = True
        _out(f"cleanup: rate-limit partition read failed: {type(exc).__name__}", err=True)


def _partition(ddb: Any, table: str, owner: str) -> list[dict[str, Any]]:
    out, kw = [], {"TableName": table, "KeyConditionExpression": "owner_id = :o",
                   "ExpressionAttributeValues": {":o": {"S": owner}}, "ConsistentRead": True}
    for _ in range(20):
        resp = ddb.query(**kw)
        out.extend(resp.get("Items", []))
        if not resp.get("LastEvaluatedKey"):
            return out
        kw["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    raise RuntimeError(f"{table} partition read did not finish within 20 pages")


def residue(c: dict[str, Any], manifest: dict[str, Any], res: Results) -> None:
    ddb, owner, left = c["dynamodb"], manifest["owner"], []
    try:
        for fid in manifest.get("finding_ids") or []:
            if _get(ddb, FINDINGS_TABLE, {"finding_id": {"S": fid}}):
                left.append(f"finding {fid}")
        for aid in manifest.get("action_ids") or []:
            if _get(ddb, ACTIONS_TABLE, {"action_id": {"S": aid}}):
                left.append(f"action {aid}")
        left += [f"rate row {_s(r.get('window_key'))}" for r in _partition(ddb, RATE_TABLE, owner)]
        left += [f"device row {_s(r.get('device_token'))}"
                 for r in _partition(ddb, DEVICES_TABLE, owner)]
    except Exception as exc:  # noqa: BLE001
        res.check("residue check", False, on_fail=f"read failed: {type(exc).__name__}")
        return
    res.check("no rows of this run remain", not left,
              detail=f"checked {len(manifest.get('finding_ids') or [])} finding(s), "
                     f"{len(manifest.get('action_ids') or [])} action(s), both owner partitions",
              on_fail="left behind: " + ", ".join(left))


def wait_alarm_ok(c: dict[str, Any], res: Results, sleep: Callable[[float], None] = time.sleep
                  ) -> None:
    def _ok() -> bool:
        a = c["cloudwatch"].describe_alarms(AlarmNames=[ALARM_NAME]).get("MetricAlarms", [])
        return bool(a) and a[0].get("StateValue") == "OK"
    res.check("the alarm returned to OK on its own", bool(_poll(_ok, 900, interval_s=30,
                                                                sleep=sleep)),
              detail="OK", on_fail="still not OK after 15 min; check it by hand")


# ---------------------------------------------------------------------------
# Pre-deploy count (read-only): cards that would push once at the transition
# ---------------------------------------------------------------------------


def pre_deploy_count(c: dict[str, Any], res: Results,
                     now: datetime | None = None) -> list[str]:
    """Live p0 Findings carded before the fix, with no ledger row (design § B1).

    Each one pushes once on its next p0 write after the deploy, because nothing
    recorded the push its card got. Run before the combined tier2 deploy (review
    cycle 1 W2); a non-empty list is for the operator to accept or clear first.
    """
    ddb = c["dynamodb"]
    now_iso = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds").replace(
        "+00:00", "Z")
    cards, kw = [], {"TableName": ACTIONS_TABLE, "FilterExpression": "target_system = :t",
                     "ExpressionAttributeValues": {":t": {"S": "ios-owner"}},
                     "ConsistentRead": True}
    for _ in range(200):
        resp = ddb.scan(**kw)
        cards.extend(resp.get("Items", []))
        if not resp.get("LastEvaluatedKey"):
            break
        kw["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    else:
        raise RuntimeError("Actions scan did not finish within 200 pages")
    from agents.health.prompt import C0161_VERBATIM_REMEDY  # noqa: PLC0415
    would_push, transition = [], []
    for card in cards:
        fid, created_by = _s(card.get("finding_id")), _s(card.get("created_by")) or ""
        if not fid or not created_by.startswith("owner-"):
            continue
        finding = _get(ddb, FINDINGS_TABLE, {"finding_id": {"S": fid}}) or {}
        if (_s(finding.get("expires_at")) or "") <= now_iso:
            continue
        if _s(finding.get("severity")) != "p0":
            # Stored below p0 under the frozen-severity bug while its text already
            # carries the C0161 directive: the first write after the deploy (a same-input
            # night included) records p0 and pushes once. Correct, but it is the deploy's
            # own push burst (review cycle 2 S7). Only the C0161 directive is matched;
            # other p0 phrasings are not counted.
            if C0161_VERBATIM_REMEDY in (_s(finding.get("headline")) or ""):
                transition.append(fid)
            continue
        owner = created_by[len("owner-"):]
        if not _get(ddb, RATE_TABLE, {"owner_id": {"S": owner},
                                      "window_key": {"S": f"p0:{fid}"}}):
            would_push.append(fid)
    res.check("no live p0 card would push again at the transition", not would_push,
              detail=f"{len(cards)} ios-owner card(s) checked, none would push",
              on_fail=f"{len(would_push)} would push once after the deploy: "
                      + ", ".join(would_push))
    res.note("cards stored below p0 whose headline carries the C0161 directive",
             f"{len(transition)}; each escalates and pushes once on its next write after the "
             "deploy" + (": " + ", ".join(transition) if transition else ""))
    return would_push


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None, *, clients: dict[str, Any] | None = None,
         base: Path | None = None, fetch: Callable[[str], bytes] | None = None,
         sleep: Callable[[float], None] = time.sleep) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--stage", default=STAGE)
    p.add_argument("--allow-writes", action="store_true")
    p.add_argument("--allow-page", action="store_true",
                   help="the run pages the operator topic's subscriber")
    p.add_argument("--cleanup-only", action="store_true")
    p.add_argument("--pre-deploy", action="store_true",
                   help="read-only: count live p0 cards that would push once after the deploy")
    p.add_argument("--run-id")
    p.add_argument("--no-wait-alarm-ok", action="store_true")
    args = p.parse_args(argv)

    if os.environ.get("LIVE_TESTS") != "1":
        _out("SKIPPED: set LIVE_TESTS=1 to run (writes to staging and pages the operator topic).")
        return 2
    if args.stage != STAGE:
        _out(f"refusing: this check is written for stage {STAGE!r} only")
        return 1

    _refusal = _lwg.check_write_flags(
        stage=args.stage, go_env=_LIVE_GO_ENV,
        flags={"--allow-writes": args.allow_writes, "--allow-page": args.allow_page},
    )
    if _refusal:
        print(_refusal, file=sys.stderr)
        return 1

    if args.pre_deploy:
        res = Results()
        c = clients or guarded_clients({"owner": None, "vin": None}, read_only=True)
        try:
            pre_deploy_count(c, res)
        except Exception as exc:  # noqa: BLE001
            res.check("pre-deploy count", False, on_fail=f"read failed: {type(exc).__name__}")
        res.summary()
        return 1 if res.failed else 0
    if not args.allow_writes:
        _out("SKIPPED: --allow-writes is required")
        return 2

    res = Results()
    if args.cleanup_only:
        if not args.run_id:
            _out("refusing: --cleanup-only needs --run-id")
            return 1
        manifest = load_manifest(validate_run_id(args.run_id), base)
        c = clients or guarded_clients(manifest)
        with _signals_held():
            cleanup(c, manifest, res, sleep=sleep)
        residue(c, manifest, res)
        res.summary()
        return 1 if res.failed else 0

    if not args.allow_page:
        _out("SKIPPED: --allow-page is required (the alarm emails the operator topic)")
        return 2
    run_id = validate_run_id(args.run_id or new_run_id())
    manifest = {"run_id": run_id, "stage": STAGE, "owner": str(uuid.uuid4()), "vin": new_vin(),
                "device_token": f"livetest-{run_id}", "finding_ids": [], "action_ids": [],
                "device_written": False}
    c = clients or guarded_clients(manifest)
    account = c["sts"].get_caller_identity()["Account"]
    _out(f"run {run_id}: owner {manifest['owner']} vin {manifest['vin']}")
    if not check_preconditions(c, res, account, fetch):
        res.summary()
        return 1
    save_manifest(manifest, base)
    _out(f"cleanup if interrupted: {_LIVE_GO_ENV}={STAGE} LIVE_TESTS=1 python3 scripts/live-verify-p0-escalation.py "
         f"--allow-writes --cleanup-only --run-id {run_id}")
    try:
        run_checks(c, manifest, res, account, base, sleep=sleep)
    except Exception as exc:  # noqa: BLE001
        res.check("run", False, on_fail=f"stopped by {type(exc).__name__}: {exc}")
    finally:
        with _signals_held():
            cleanup(c, manifest, res, sleep=sleep)
        residue(c, manifest, res)
        if not args.no_wait_alarm_ok:
            wait_alarm_ok(c, res, sleep=sleep)
        res.summary()
    return 1 if res.failed else 0


if __name__ == "__main__":
    _install_signal_handlers()
    sys.exit(main())
