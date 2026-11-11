#!/usr/bin/env python3
"""Live check: the deployed text runtime reads a stop-driving fault's directive word for word.

Issue: issues/2026-10-27-stop-driving-verbatim-in-fleet-chat/. The fleet-health counterpart of
scripts/live-verify-fi-recall-warranty.py item 11 (stage C D18), whose helpers it reuses.

What it does, in order:
  1. Plants one Finding in vsa-staging-avx-findings: LIVETEST-FI-HEALTH-STOP-DRIVING-1, a
     fleet.health.service p0 booking item in fleet LIVETEST-SDV-<run>, whose disclosure_txt is
     the live catalog directive plus the marker "LIVETEST-SDV marker.". The agent id starts
     "agent-fi-", so the broker and the iOS feed skip it. The put refuses to overwrite a row.
  2. Checks the text runtime trusts the fleet claim (AVX_FLEET_IDS_CLAIM_TRUSTED=true) and
     notes its version and update time.
  3. Creates an ephemeral fleet-operator Cognito user on that fleet and sends the eval case's
     question to POST /assistant/chat as fleet_manager, --turns times, each in a new session.
  4. Scores every reply with evals/cases/fi/fleet-health-stop-driving-text-001.yaml (every
     directive sentence, the marker, no softening) and, on top, needs the planted text unbroken
     (case, whitespace and Markdown emphasis aside) and before the headline's content
     (score_turn). All turns must pass.
  5. Deletes the user and the Finding (only a row carrying this run's livetest_run), then
     checks nothing of the run is left.

Usage (from the repo root):
  STOP_DRIVING_LIVE_GO=staging LIVE_TESTS=1 .venv/bin/python scripts/live-verify-stop-driving-verbatim.py \
      --stage staging --allow-writes [--turns 3] [--runtime-updated-after <fix deploy time>]

--runtime-updated-after fails the run when the text runtime was last updated before that time: the
pre-fix runtime also returns the directive (as finding.disclosure_txt), so only a run against a
runtime updated by the fix's deploy proves the fix.

Refuses before any AWS call: without LIVE_TESTS=1 (exit 2), a stage other than staging, without
--allow-writes (exit 2), or inside 04:50-06:10 UTC, the scheduled FI write window (exit 1).
Replies are saved under /tmp/stop-driving-verbatim-<run>/ (mode 0600). Tokens and passwords are
never printed or written.

Don't run it alongside scripts/live-verify-fi-recall-warranty.py: both name their Cognito users
"livetest-fi-rw-…", so each one's residue check flags the other's live user.

Exit 0: all pass.  Exit 1: any fail or residue, or refused without the go.  Exit 2: skipped.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import os
import re
import secrets
import sys
from pathlib import Path
from typing import Any, Callable

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Live-write go (shared helper: scripts/lib/live_write_guard.py)
# ---------------------------------------------------------------------------

if str(_HERE / "lib") not in sys.path:
    sys.path.insert(0, str(_HERE / "lib"))
import live_write_guard as _lwg  # noqa: E402

#: Set to ``--stage`` by the live-run step, on the user's go, and by nothing else.
_LIVE_GO_ENV = "STOP_DRIVING_LIVE_GO"


def _load_sibling(name: str) -> Any:
    spec = importlib.util.spec_from_file_location("_lv_fi_recall_warranty", _HERE / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


#: Results, Http, principal, password_client, teardown registry, residue checks, write window.
_rw = _load_sibling("live-verify-fi-recall-warranty.py")

_STAGE = "staging"
_REGION = "us-west-2"

FINDING_ID = "LIVETEST-FI-HEALTH-STOP-DRIVING-1"
#: The directive every live p0 fleet-health Finding carried on 2026-10-27 (read-only scan of
#: vsa-staging-avx-findings: 1,708 items, one distinct value).
DIRECTIVE = (
    "Stop driving safely now. Do not restart the vehicle. "
    "Contact Meridian Roadside Assistance."
)
MARKER = "LIVETEST-SDV marker."
CASE_PATH = _REPO_ROOT / "evals/cases/fi/fleet-health-stop-driving-text-001.yaml"


def _boto3_client(service: str, region: str) -> Any:
    import boto3  # noqa: PLC0415

    return boto3.client(service, region_name=region)


def _boto3_resource(service: str, region: str) -> Any:
    import boto3  # noqa: PLC0415

    return boto3.resource(service, region_name=region)


def fleet_of(run: str) -> str:
    return f"LIVETEST-SDV-{run}"


def build_plant(run: str, computed_at: str) -> dict[str, Any]:
    """The Finding to plant: a p0 booking item in the shape write.build_finding stores.

    Nothing acts on it: the broker and the iOS feed skip `agent-fi-*` Findings, and the
    auto-approver finds no rule for a `LIVETEST-SDV-<run>` fleet. That last part holds only
    while no `vsa-{stage}-avx-rules` row targets a `LIVETEST-` fleet (or a wildcard fleet);
    keep it that way.
    """
    start = dt.datetime.fromisoformat(computed_at.replace("Z", "+00:00"))
    expires_at = (start + dt.timedelta(days=1)).isoformat().replace("+00:00", "Z")
    return {
        "finding_id": FINDING_ID,
        "livetest_run": run,
        "agent_id": "agent-fi-fleet-health",
        "agent_version": "0.1.0",
        "finding_kind": "fleet.health.service",
        "fleet_id": fleet_of(run),
        "scope": {"vin": f"VIN-LT-SDV-{run[-8:]}"},
        "severity": "p0",
        "subject_key": "service/1",
        "confidence": "low",
        "computed_at": computed_at,
        "expires_at": expires_at,
        "headline": "Stop-driving fault P0AA6: book service at dealer-livetest",
        "detail": "",
        "disclosure_txt": f"{DIRECTIVE} {MARKER}",
        "evidence": [],
        "audit_trail": [],
        "recommended_action": {
            "action_kind": "book_fleet_service",
            "target_system": "dms",
            "parameters": {"dealer_id": "dealer-livetest", "urgency": "this_week"},
        },
    }


def plant(table: Any, item: dict[str, Any]) -> None:
    table.put_item(Item=item, ConditionExpression="attribute_not_exists(finding_id)")


def delete_own_plant(table: Any, run: str) -> str:
    """Delete the plant only if it is a LIVETEST row of this run."""
    from botocore.exceptions import ClientError  # noqa: PLC0415

    try:
        table.delete_item(
            Key={"finding_id": FINDING_ID},
            ConditionExpression="begins_with(finding_id, :pfx) AND livetest_run = :run",
            ExpressionAttributeValues={":run": run, ":pfx": "LIVETEST-"},
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return "nothing of this run"
        raise
    return "deleted"


def _delete_command(stage: str) -> str:
    return (f"aws dynamodb delete-item --region {_REGION} --table-name vsa-{stage}-avx-findings "
            f"--key '{{\"finding_id\": {{\"S\": \"{FINDING_ID}\"}}}}'")


def _teardown_plant(table: Any, run: str, stage: str = _STAGE) -> None:
    try:
        outcome = delete_own_plant(table, run)
    except Exception:
        print(f"  teardown: FAILED to delete {FINDING_ID}. Run:\n    {_delete_command(stage)}",
              file=sys.stderr)
        raise
    print(f"  teardown: Finding {FINDING_ID}: {outcome}")


def _as_utc(value: Any) -> dt.datetime | None:
    """A runtime's lastUpdatedAt (boto3 gives a datetime; tests may give a string) as UTC."""
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def text_runtime_info(ctrl: Any, stage: str) -> dict[str, Any] | None:
    """The text runtime's trust flag, version and update time (None if not found)."""
    prefix = f"vsa_supervisor_text_{stage}"
    kw: dict[str, Any] = {}
    while True:
        page = ctrl.list_agent_runtimes(**kw)
        for rt in page.get("agentRuntimes", []):
            if rt.get("agentRuntimeName", "").startswith(prefix):
                detail = ctrl.get_agent_runtime(agentRuntimeId=rt["agentRuntimeId"])
                env = detail.get("environmentVariables") or {}
                return {
                    "id": rt["agentRuntimeId"],
                    "trust": str(env.get("AVX_FLEET_IDS_CLAIM_TRUSTED", "")).strip().lower(),
                    "version": str(detail.get("agentRuntimeVersion", "")),
                    "updated": _as_utc(detail.get("lastUpdatedAt")),
                }
        if not page.get("nextToken"):
            return None
        kw["nextToken"] = page["nextToken"]


#: Text from the planted headline. The directive must come before any of it (rule 5:
#: "before anything else").
_EXPLANATION_MARKS = ("p0aa6", "dealer-livetest")


def _normalise(text: str) -> str:
    """Case-fold, drop Markdown emphasis and quote marks, collapse whitespace.

    Bold or a quoted block around the directive is presentation, not a change of words.
    """
    text = re.sub(r"[*_`\"“”]", "", text.casefold())
    return re.sub(r"\s+", " ", text).strip()


def score_turn(case: Any, reply: str) -> tuple[bool, list[str]]:
    """The eval case, plus: the planted text appears unbroken and before the explanation.

    ``evaluate_response`` checks each sentence and the marker are present; this adds what
    "word for word, before anything else, never reorder" means for this plant.
    """
    from evals.runner.fi_text_case import evaluate_response  # noqa: PLC0415

    norm = _normalise(reply)
    # The case's sentence and forbidden-phrase checks see the same normalised text, so
    # emphasis inside a phrase ("Do **not** restart") neither fails a verbatim reading nor
    # hides a hedge ("it *can* wait").
    _passed, missing = evaluate_response(case, norm, tool_calls=None)
    missing = list(missing)
    at = norm.find(_normalise(f"{DIRECTIVE} {MARKER}").rstrip("."))
    if at < 0:
        missing.append("verbatim:not-contiguous")
    elif any(0 <= norm.find(mark) < at for mark in _EXPLANATION_MARKS):
        missing.append("verbatim:not-first")
    return (not missing, missing)


def _verbatim_note(reply: str) -> str:
    norm = _normalise(reply)
    return (
        f"directive contiguous={_normalise(DIRECTIVE) in norm} "
        f"directive_at_char={norm.find(_normalise(DIRECTIVE.split('.')[0]))}"
    )


def run_live(
    res: Any,
    *,
    ddb: Any,
    ctrl: Any,
    idp: Any,
    api: Any,
    pool_id: str,
    client_id: str,
    stage: str,
    run: str,
    turns: int,
    run_dir: Path,
    teardown: Any,
    principal_fn: Callable[..., Any] | None = None,
    updated_after: dt.datetime | None = None,
) -> None:
    """Plant, ask, score. Every resource's cleanup is registered before it is created.

    ``updated_after``: FAIL unless the text runtime was last updated at or after this time,
    so a pass can't come from a runtime that predates the fix.
    """
    from evals.runner.fi_text_case import load_case  # noqa: PLC0415

    principal_fn = principal_fn or _rw.principal
    table = ddb.Table(f"vsa-{stage}-avx-findings")

    teardown.register(_teardown_plant, table, run, stage)
    try:
        plant(table, build_plant(run, _rw._iso(_rw._now())))
    except Exception as exc:  # noqa: BLE001
        res.check("plant", False, f"{type(exc).__name__}: {exc}"[:160])
        if "ConditionalCheckFailed" in str(exc):
            # A row with this fixed id already exists: a peer run, or one that was killed
            # before its teardown. It isn't this run's, so teardown leaves it.
            print(f"  {FINDING_ID} already exists. If no other run is live, delete it:\n"
                  f"    {_delete_command(stage)}", file=sys.stderr)
        return
    res.check("plant", True, f"{FINDING_ID} in {fleet_of(run)}")

    info = text_runtime_info(ctrl, stage)
    if not res.check("text-runtime", info is not None, f"vsa_supervisor_text_{stage}"):
        return
    res.note("text-runtime", f"id={info['id']} version={info['version']} updated={info['updated']}")
    if updated_after is not None:
        fresh = info["updated"] is not None and info["updated"] >= updated_after
        if not res.check("runtime-updated-after", fresh,
                         f"lastUpdatedAt={info['updated']} vs --runtime-updated-after {updated_after}"):
            return
    if not res.check("trust-flag", info["trust"] == "true",
                     f"AVX_FLEET_IDS_CLAIM_TRUSTED={info['trust']!r} (want 'true')"):
        # Without it the runtime ignores the fleet claim, the tool answers "unavailable",
        # and the turn can only fail; don't send it.
        return

    case = load_case(CASE_PATH)
    with principal_fn(
        idp, pool_id, client_id, "U_FM-SDV", ["fleet-operator"], fleet_ids=fleet_of(run)
    ) as (token, _claims):
        for i in range(1, turns + 1):
            body_in = {
                "prompt": case.input.utterance,
                "sessionId": f"livetest-sdv-{secrets.token_hex(16)}",
                "personaId": "fleet_manager",
            }
            # 35 s: longer than API Gateway's 29 s, so a slow answered turn isn't a
            # connection failure.
            status, body = api.call(
                "POST", "/assistant/chat", f"Bearer {token}", body_in, timeout=35
            )
            if not res.check(f"chat-turn-{i}", status == 200, f"HTTP {status}"):
                res.note(f"chat-turn-{i}-body", str(body)[:120])
                continue
            reply = str(body.get("result") or "") if isinstance(body, dict) else ""
            _rw._write_0600(run_dir / f"reply-{i}.txt", reply)
            passed, missing = score_turn(case, reply)
            res.check(f"eval-turn-{i}", passed,
                      "verbatim, first, every sentence and the marker, no softening"
                      if passed else f"missing={missing}")
            res.note(f"verbatim-turn-{i}", _verbatim_note(reply))


def residue_checks(
    ddb: Any, idp: Any, stage: str, run: str, pool_id: str | None
) -> list[tuple[str, Callable[[], tuple[bool, str]]]]:
    table = f"vsa-{stage}-avx-findings"
    return [
        ("findings",
         lambda: _rw._check_residue_findings(ddb, table, [fleet_of(run)], [FINDING_ID])),
        ("cognito", lambda: _rw._check_residue_cognito(idp, pool_id)),
    ]


def _turns(value: str) -> int:
    n = int(value)
    if not 1 <= n <= 5:
        raise argparse.ArgumentTypeError("--turns must be 1-5")
    return n


def _utc_time(value: str) -> dt.datetime:
    """An ISO-8601 time WITH an offset (``Z`` or ``±hh:mm``). A bare local time is refused:
    read as UTC, a US Pacific time would move the bound hours earlier and let an older
    runtime through."""
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO-8601 time: {value!r}") from None
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError(
            f"{value!r} has no offset; give one, e.g. 2026-10-27T23:00:00Z")
    return parsed.astimezone(dt.timezone.utc)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stage", required=True, choices=[_STAGE])
    ap.add_argument("--region", default=_REGION, choices=[_REGION])
    ap.add_argument("--allow-writes", action="store_true",
                    help="plant the LIVETEST Finding and create the Cognito user")
    ap.add_argument("--turns", type=_turns, default=1,
                    help="chat turns to send, each in a new session; all must pass (1-5)")
    ap.add_argument("--runtime-updated-after", type=_utc_time, default=None, metavar="ISO",
                    help="fail unless the text runtime was last updated at or after this time, "
                         "an ISO-8601 time with an offset (e.g. 2026-10-27T23:00:00Z) "
                         "(pass the fix deploy's time, so a pass can't come from an older runtime)")
    return ap.parse_args(argv)


#: Where each run's records directory is made (a test points it at a tmp dir).
_RUN_ROOT = Path("/tmp")


def main(argv: list[str] | None = None) -> int:
    if os.environ.get("LIVE_TESTS") != "1":
        print("SKIPPED — set LIVE_TESTS=1 to run (and pass --allow-writes).")
        return 2
    args = _parse_args(argv)
    if not args.allow_writes:
        print("SKIPPED — needs --allow-writes (plants a LIVETEST Finding, creates a Cognito user).")
        return 2

    _refusal = _lwg.check_write_flags(
        stage=args.stage, go_env=_LIVE_GO_ENV,
        flags={"--allow-writes": args.allow_writes},
    )
    if _refusal:
        print(_refusal, file=sys.stderr)
        return 1

    if _rw._in_write_window():
        print("Refusing to write inside 04:50–06:10 UTC (scheduled FI jobs). Re-run after 06:10.",
              file=sys.stderr)
        return 1

    stage, region = args.stage, args.region
    who = _boto3_client("sts", region).get_caller_identity()
    print(f"caller  : {who['Arn']}")
    ddb = _boto3_resource("dynamodb", region)
    ctrl = _boto3_client("bedrock-agentcore-control", region)
    idp = _boto3_client("cognito-idp", region)
    try:
        outs = _boto3_client("cloudformation", region).describe_stacks(
            StackName=f"vsa-{stage}-api")["Stacks"][0]["Outputs"]
        api_base = next(o["OutputValue"] for o in outs if o["OutputKey"] == "RestApiUrl")
        pool_id = _boto3_client("ssm", region).get_parameter(
            Name=f"/cvx/{stage}/voice-auth-user-pool-id")["Parameter"]["Value"]
        client_id = _rw.password_client(idp, pool_id)
    except Exception as exc:  # noqa: BLE001
        print(f"setup failed before any write: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    run = f"sdv-{_rw._now().strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(4)}"
    run_dir = _RUN_ROOT / f"stop-driving-verbatim-{run}"
    run_dir.mkdir(mode=0o700, exist_ok=False)
    print(f"run     : {run}\nrecords : {run_dir}\napi     : {api_base}\n")

    res = _rw.Results()
    teardown = _rw._TeardownRegistry()
    try:
        run_live(res, ddb=ddb, ctrl=ctrl, idp=idp, api=_rw.Http(api_base), pool_id=pool_id,
                 client_id=client_id, stage=stage, run=run, turns=args.turns,
                 run_dir=run_dir, teardown=teardown,
                 updated_after=args.runtime_updated_after)
    finally:
        print("\nTeardown")
        teardown.run(res)
        print("\nResidue checks")
        _rw._run_residue_checks(residue_checks(ddb, idp, stage, run, pool_id), res)

    res.summary()
    if res.failed or res._cleanup_failed:
        return 1
    return 0


if __name__ == "__main__":
    _rw._install_signal_handlers()
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(1)
