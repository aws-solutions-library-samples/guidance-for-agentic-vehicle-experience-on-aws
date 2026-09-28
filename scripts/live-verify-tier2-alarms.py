#!/usr/bin/env python3
"""Live-verify that every Tier 2 CloudWatch alarm can actually deliver its action.

Closes the gate `issues/2026-09-24-tier2-operator-alarms-never-delivered/report.md`
describes and that no stubbed test can reach: an alarm can transition state
correctly, hold a non-empty ``AlarmActions``, and STILL fail to notify. CloudWatch
records that failure in the alarm's action history, and nowhere else. The 2026-09-24
defect went undetected because the CDK jest test asserted a key was attached, not
that the key could be used by the only publisher the topic has — a stub cannot fail
the way KMS does.

What it does
------------
Discovers every ``vsa-{stage}-tier2-*`` alarm (no hardcoded names — a new alarm
introduced by a future spec is covered automatically), forces each to transition to
ALARM, polls ``describe-alarm-history --history-item-type Action`` for the newest
entry after the transition, and asserts ``actionState == "Succeeded"``. On failure it
prints the two pre-fix signatures verbatim so the operator does not have to guess
which defect they are looking at:

    "Failed to execute action"
    "CloudWatch Alarms is not authorized to perform: SNS:Publish"

Restores each alarm to its original state in a ``finally`` block. Cleanup failures
print loudly but do not turn a genuine FAIL into a PASS.

Refuses to run without ``LIVE_TESTS=1`` because ``SetAlarmState`` fires the alarm's
real actions — that is the entire point of the check, and after the F32.10 fix ships
this WILL page whoever is subscribed to the operator alerts topic.

Anti-vacuity: refuses to exit 0 when the prefix matches zero alarms. A vacuous PASS
is worse than a FAIL — it says the fix is verified when nothing was checked.

Mutation verification
---------------------
Per ``~/.kiro/steering/testing.md`` § "Mutation testing at the green boundary":

    python3 scripts/live-verify-tier2-alarms.py --simulate success   # exits 0
    python3 scripts/live-verify-tier2-alarms.py --simulate failure   # exits 1

Both bypass AWS entirely and drive the parse + assertion path against a synthetic
``HistoryData`` payload. ``failure`` uses the exact SNS:Publish signature from
``report.md`` § Reproduction, so the mutation IS the defect the script is written
to catch. If ``--simulate failure`` ever exits 0, the assertion is asserting
presence, not the property.

Usage
-----
    LIVE_TESTS=1 python3 scripts/live-verify-tier2-alarms.py
    LIVE_TESTS=1 python3 scripts/live-verify-tier2-alarms.py --stage staging --region us-west-2

Exit codes: 0 all alarms delivered · 1 at least one failed · 2 skipped (no LIVE_TESTS)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Iterable

ALARM_PREFIX_TEMPLATE = "vsa-{stage}-tier2-"

# The exact signatures the pre-fix state emits, per
# ``issues/2026-09-24-tier2-operator-alarms-never-delivered/report.md`` §
# Reproduction. Named here so a FAIL print names WHICH defect, rather than
# leaving the reader to grep steering docs for a phrase.
FAILED_ACTION_MARKERS = (
    "Failed to execute action",
    "CloudWatch Alarms is not authorized to perform: SNS:Publish",
)


# ---------------------------------------------------------------------------
# discovery + parse (real path)
# ---------------------------------------------------------------------------

def _iter_alarms(cw, prefix: str) -> Iterable[dict]:
    """Yield every metric OR composite alarm whose name starts with `prefix`.

    Composite included for forward-compat: no tier2 alarms are composite today,
    but omitting them would make this script silently miss a class of alarms a
    future spec might add. Fail-open on discovery is exactly the bug this script
    exists to prevent elsewhere.
    """
    paginator = cw.get_paginator("describe_alarms")
    for page in paginator.paginate(
        AlarmNamePrefix=prefix,
        AlarmTypes=["MetricAlarm", "CompositeAlarm"],
    ):
        for a in page.get("MetricAlarms", []):
            yield a
        for a in page.get("CompositeAlarms", []):
            yield a


def _fetch_action_history(cw, alarm_name: str, since: datetime, timeout_s: int) -> dict | None:
    """Poll for the newest Action-type history entry AFTER `since`.

    Filtering by `since` matters: a stale action-history entry from a previous run
    would pass or fail this script for the wrong reason. The `set_alarm_state`
    timestamp is our line — anything older is not our transition.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        resp = cw.describe_alarm_history(
            AlarmName=alarm_name,
            HistoryItemType="Action",
            MaxRecords=5,
            ScanBy="TimestampDescending",
        )
        for item in resp.get("AlarmHistoryItems", []):
            ts = item.get("Timestamp")
            if ts and ts >= since:
                return item
        time.sleep(2)
    return None


def _parse_action_state(history_item: dict) -> tuple[str, str]:
    """Return ``(actionState, error_or_reason)`` from a history item.

    HistoryData is a JSON string per the CloudWatch API contract; ``actionState`` is
    the field the report's reproduction reads (``"Succeeded"``, ``"Failed"``, etc.).
    """
    hd_raw = history_item.get("HistoryData", "{}")
    try:
        hd = json.loads(hd_raw)
    except json.JSONDecodeError:
        return ("Unparseable", hd_raw[:400])
    action_state = str(hd.get("actionState", "Missing"))
    # `error` is the field when publish is denied; `stateReason` covers other
    # shapes of failure. Take whichever is present, empty string otherwise.
    error = str(hd.get("error") or hd.get("stateReason") or "")
    return (action_state, error)


# ---------------------------------------------------------------------------
# assertion (shared between real and simulated paths — this is what the
# mutation test is actually testing)
# ---------------------------------------------------------------------------

def _check_action(alarm_name: str, action_state: str, error: str) -> bool:
    """Emit the per-alarm summary line. Return True on pass, False on fail.

    This is the ONE function the mutation verification is really exercising —
    everything else is either data fetch or output formatting.
    """
    if action_state == "Succeeded":
        print(f"  PASS  {alarm_name} — actionState=Succeeded")
        return True

    print(f"  FAIL  {alarm_name} — actionState={action_state}")
    if error:
        # Truncate to a readable length; 400 chars comfortably fits the full
        # SNS:Publish denial phrasing without dumping page-fulls of noise.
        print(f"        error: {error[:400]}")
    for marker in FAILED_ACTION_MARKERS:
        if marker in error:
            print(f"        signature match: '{marker}'")
            print(f"        This is the pre-fix state from")
            print(f"        issues/2026-09-24-tier2-operator-alarms-never-delivered/report.md")
            print(f"        The topic KMS key or resource policy is still misconfigured.")
            break
    return False


def _print_banner(passes: int, fails: int, total: int) -> None:
    print()
    if fails == 0:
        print("=" * 68)
        print(f"PASS — {passes}/{total} alarms delivered their action.")
        print("=" * 68)
        print("The Tier 2 operator alerts topic is publishable. The 2026-09-24 defect")
        print("(issues/2026-09-24-tier2-operator-alarms-never-delivered/) is not in")
        print("the state this script was written to detect.")
    else:
        print("=" * 68)
        print(f"FAIL — {fails}/{total} alarms could not deliver their action.")
        print("=" * 68)
        print("The Tier 2 operator alerts topic is NOT publishable. A broken sweep")
        print("today produces a stale artifact that Tier 1 keeps narrating with a")
        print("receding computed_at, and nobody hears — the exact failure mode")
        print("~/.kiro/steering/agentic-tiers.md § 'Prior art' names as the reason")
        print("this alerting path exists at all.")


# ---------------------------------------------------------------------------
# simulate mode (bypasses AWS entirely — used to mutation-verify the script)
# ---------------------------------------------------------------------------

def _synthetic_history(mode: str) -> tuple[str, str]:
    """Fabricate the parsed (actionState, error) pair the real path would return.

    ``failure`` uses the exact SNS:Publish denial phrasing from ``report.md`` §
    Reproduction. If the assertion path ever misses this string, the script is
    asserting presence, not the property — the mutation would silently pass.
    """
    if mode == "success":
        return ("Succeeded", "")
    return (
        "Failed",
        'Failed to execute action arn:aws:sns:us-west-2:000000000000:'
        'vsa-staging-tier2-operator-alerts. Received error: "CloudWatch Alarms '
        "is not authorized to perform: SNS:Publish on resource: "
        "arn:aws:sns:us-west-2:000000000000:vsa-staging-tier2-operator-alerts "
        'because no resource-based policy allows the SNS:Publish action."',
    )


def _run_simulated(mode: str, stage: str, region: str) -> int:
    prefix = ALARM_PREFIX_TEMPLATE.format(stage=stage)
    # Two synthetic alarms so the summary line + banner arithmetic is meaningful.
    # Names track the real ones (vsa-tier2-stack.ts:1243 / :1262) so a reader
    # comparing to the deployed alarms sees the shape they expect.
    synthetic_alarms = [
        f"{prefix}run-failure-{region}",
        f"{prefix}run-duration-{region}",
    ]
    print(f"SIMULATE mode='{mode}' — NOT hitting AWS.")
    print(f"  stage    : {stage}")
    print(f"  region   : {region}")
    print(f"  alarms   : {len(synthetic_alarms)} synthetic")
    print()

    passes = fails = 0
    for name in synthetic_alarms:
        action_state, error = _synthetic_history(mode)
        if _check_action(name, action_state, error):
            passes += 1
        else:
            fails += 1
    _print_banner(passes, fails, len(synthetic_alarms))
    return 0 if fails == 0 else 1


# ---------------------------------------------------------------------------
# live mode (the real path — the one that pages a human when it FAILS)
# ---------------------------------------------------------------------------

def _run_live(stage: str, region: str, wait_seconds: int) -> int:
    import boto3

    sts = boto3.client("sts", region_name=region)
    who = sts.get_caller_identity()
    account = who["Account"]

    cw = boto3.client("cloudwatch", region_name=region)
    prefix = ALARM_PREFIX_TEMPLATE.format(stage=stage)

    print(f"caller   : {who['Arn']}")
    print(f"account  : {account}")
    print(f"stage    : {stage}")
    print(f"region   : {region}")
    print(f"prefix   : {prefix}")
    print()

    alarms = list(_iter_alarms(cw, prefix))
    if not alarms:
        # Anti-vacuity floor: a prefix that matches zero alarms would otherwise
        # let this script exit 0 while asserting nothing.
        print(f"REFUSING TO CONTINUE — 0 alarms found with prefix {prefix!r}.")
        print("  A vacuous PASS is worse than a FAIL: it says the fix is verified")
        print("  when nothing was checked. Confirm the vsa-{stage}-tier2 stack is")
        print(f"  deployed in {region} and try again.")
        return 1

    print(f"discovered {len(alarms)} alarm(s):")
    for a in alarms:
        actions = a.get("AlarmActions") or []
        enabled = a.get("ActionsEnabled", True)
        note = ""
        if not actions:
            # A tier2 alarm with no AlarmActions is a spec-level defect (F32.10's
            # sibling case) — call it out but do not silently skip. The action
            # history for such an alarm will legitimately never contain an
            # Action-type entry, so the polling loop below will FAIL it with a
            # clear message.
            note = " [WARN: no AlarmActions — cannot deliver]"
        elif not enabled:
            note = " [WARN: ActionsEnabled=false — will not fire]"
        print(f"  - {a['AlarmName']} (state={a['StateValue']}){note}")
    print()

    original_state = {a["AlarmName"]: a["StateValue"] for a in alarms}
    fired_names: list[str] = []
    results: list[tuple[str, bool]] = []

    try:
        # Force OK → ALARM. An alarm already in ALARM would produce a no-op
        # transition and no new Action history entry, so pre-transition to OK
        # is the difference between measuring the fix and measuring nothing.
        pre_transition_at = datetime.now(timezone.utc)
        for a in alarms:
            name = a["AlarmName"]
            if a["StateValue"] == "ALARM":
                cw.set_alarm_state(
                    AlarmName=name,
                    StateValue="OK",
                    StateReason="live-verify-tier2-alarms.py: pre-transition to OK",
                )
                # Very short pause so the OK transition is recorded distinctly
                # from the ALARM transition that follows. Not correctness-critical
                # (`since` filter would still catch the right entry), but it makes
                # the alarm's history readable to a human debugging afterwards.
                time.sleep(1)
        # Rebaseline `since` AFTER any pre-transitions, so we cannot pick up
        # OK-transition history entries as our result.
        fired_at = datetime.now(timezone.utc)
        for a in alarms:
            name = a["AlarmName"]
            print(f"firing   : {name}")
            cw.set_alarm_state(
                AlarmName=name,
                StateValue="ALARM",
                StateReason="live-verify-tier2-alarms.py: verifying SNS publish path",
            )
            fired_names.append(name)
        print()

        # Poll each alarm's action history. Firing all first then polling all
        # parallelises the 5-10s CloudWatch delay across alarms rather than
        # serialising it.
        for name in fired_names:
            print(f"polling  : {name} (up to {wait_seconds}s for Action history)")
            history = _fetch_action_history(cw, name, fired_at, wait_seconds)
            if history is None:
                # Two possible causes: (a) alarm has no AlarmActions / actions
                # disabled — flagged in preflight above; (b) CloudWatch backend
                # delay > wait_seconds. Bump --wait-seconds if this is (b).
                print(f"  FAIL  {name} — no Action-type history entry within {wait_seconds}s")
                print(f"        Either the alarm has no AlarmActions, ActionsEnabled=false,")
                print(f"        or CloudWatch has not yet recorded the action. Re-run with")
                print(f"        --wait-seconds 60 before concluding this is (a).")
                results.append((name, False))
                continue
            action_state, error = _parse_action_state(history)
            results.append((name, _check_action(name, action_state, error)))
    finally:
        # Best-effort reset. A cleanup failure logs to stderr but never overrides
        # the assertion verdict — leaving an alarm in ALARM after a genuine FAIL
        # is safer than falsely reporting PASS to appease cleanup.
        print()
        for name in fired_names:
            target = original_state.get(name, "OK")
            # Only three states are valid StateValues for set_alarm_state; if
            # we saw anything else (shouldn't happen) default to OK.
            if target not in ("OK", "ALARM", "INSUFFICIENT_DATA"):
                target = "OK"
            try:
                cw.set_alarm_state(
                    AlarmName=name,
                    StateValue=target,
                    StateReason="live-verify-tier2-alarms.py: reset to original state",
                )
                print(f"reset    : {name} → {target}")
            except Exception as exc:  # noqa: BLE001
                print(
                    f"reset    : FAILED for {name}: {type(exc).__name__}: {exc}\n"
                    f"           Reset manually: aws cloudwatch set-alarm-state "
                    f"--alarm-name {name} --state-value {target} "
                    f"--state-reason 'manual reset after live-verify' --region {region}",
                    file=sys.stderr,
                )

    passes = sum(1 for _, ok in results if ok)
    fails = len(results) - passes
    _print_banner(passes, fails, len(alarms))
    return 0 if fails == 0 else 1


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--stage", default="staging",
                        help="Stage name (default: staging). Matches the alarm-name prefix.")
    parser.add_argument("--region", default="us-west-2",
                        help="AWS region (default: us-west-2).")
    parser.add_argument("--wait-seconds", type=int, default=30,
                        help="Max seconds to poll for Action history per alarm (default: 30).")
    parser.add_argument(
        "--simulate",
        choices=("success", "failure"),
        default=None,
        help="Bypass AWS and drive the parse+assertion path against a synthetic "
             "history entry. Used for mutation verification — see docstring.",
    )
    args = parser.parse_args()

    if args.simulate is not None:
        return _run_simulated(args.simulate, args.stage, args.region)

    if os.environ.get("LIVE_TESTS") != "1":
        print("SKIPPED — set LIVE_TESTS=1 to run (this fires real alarm actions).")
        return 2

    return _run_live(args.stage, args.region, args.wait_seconds)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(2)
