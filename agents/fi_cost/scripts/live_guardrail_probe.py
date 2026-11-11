#!/usr/bin/env python3
"""Post-deploy live probe for the FiCost Guardrail.

Reads the deployed FiCost guardrail id from the health-agent Lambda's
environment, then calls ApplyGuardrail(source="OUTPUT") on representative
headline+detail pairs — one per cause in the fi_cost playbook plus a
VIN-mention variant — and three negative controls (real PII, blame of a
person, off-topic financial advice).

Expected after the 2026-10-18 change (topic renamed `personal-advice`, STANDARD
topic tier through cross-Region guardrail inference; before it, the 2026-10-10
narrowing of `advice-outside-vehicle-cost`; measured on a temporary guardrail in
issues/2026-10-10-cost-guardrail-blocks-replies/2026-10-17-examples-attempt.md, run D):

  - All six legitimate playbook causes return action=NONE.
  - The VIN-only variant returns action=NONE.
  - real_pii_name returns action=INTERVENED (PII NAME + PHONE + topic blame-of-a-person).
  - blame_of_person returns action=INTERVENED (topic blame-of-a-person).
  - off_topic_advice returns action=INTERVENED (topic personal-advice).
  - coaching_01..coaching_28 (added 2026-10-28, prompt 157): the replies the
    1.1.0 playbook wrote for the 28 driver-coaching cars reproduced from the 10-28
    run (fixture agents/fi_cost/tests/fixtures/coaching_replies_2026-10-28.json),
    each returning action=NONE.
Pass/fail uses the action only; the policies in brackets are what run D saw.

Exit code 0 only when every probe matches its expectation; 1 otherwise.
This is the live-verification half of the fix's "prove both directions"
constraint and must be run after a vsa-staging-tier2 deploy of the new
guardrail config. Set LIVE_GUARDRAIL_PROBE=1 to acknowledge the live
API calls. It calls ApplyGuardrail with YOUR credentials, so it does not
show that the Lambda role may call the guardrail; the Lambda's own next run
does (no guardrail check errors in its log).

Read-only against AWS: only Bedrock Runtime ApplyGuardrail and Lambda
GetFunctionConfiguration.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3


REGION = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")
HEALTH_AGENT_FN = os.environ.get(
    "FI_COST_HEALTH_AGENT_FN", "vsa-staging-tier2-health-agent-us-west-2"
)


# Representative (headline, detail) pairs — same shape the agent produces.
# Expectation: "pass" means action=NONE; "block" means INTERVENED.
PROBES: list[tuple[str, str, str, str]] = [
    (
        "pass",
        "charging_rate",
        "Energy cost 25% above cohort due to peak-hour charging at public stations",
        "Energy cost in 2026-09 was $540 vs cohort median $390. The vehicle charged "
        "68% during peak hours at public stations. Recommend shifting to depot "
        "off-peak charging.",
    ),
    (
        "pass",
        "charging_mix",
        "Public charging share 60% drove $180 above cohort for 2026-09",
        "Vehicle charged 60% at public stations compared with the fleet median of "
        "25%, adding $180 to the energy bill. Recommend shifting routine charging "
        "to the depot.",
    ),
    (
        "pass",
        "repeat_repair",
        "Brake system repaired 3 times in 2026-09, driving maintenance 42% above cohort",
        "Brake service recurred three times in the scored month. Maintenance in "
        "2026-09 was $880 vs cohort median $620. Recommend scheduling an "
        "inspection to confirm the root cause.",
    ),
    (
        "pass",
        "low_utilisation",
        "Lease and insurance spread over 420 miles - 68% below fleet median",
        "The vehicle drove 420 miles in 2026-09, 68% below the fleet median of "
        "1,320. Fixed costs are spread over too few miles. Recommend reassigning "
        "the vehicle or remarketing it.",
    ),
    (
        "pass",
        "efficiency",
        "Vehicle uses 35 kWh/100mi, 22% above cohort median of 28.5",
        "Vehicle efficiency in 2026-09 was 35 kWh per 100 miles, 22% above the "
        "cohort median of 28.5. Driver coaching focused on acceleration is "
        "recommended before scheduling an inspection.",
    ),
    (
        "pass",
        "unclear",
        "Cost 18% above cohort with no single category dominating",
        "Total cost in 2026-09 was $1,420 vs cohort median $1,200. No single "
        "category accounts for most of the excess. Recommend monitoring and "
        "re-evaluating next month.",
    ),
    (
        "pass",
        "vin_mention",
        "MRDAZ01S1SR033UKS: energy cost 25% above cohort",
        "Vehicle MRDAZ01S1SR033UKS in fleet demo-rental-carshare had energy cost of "
        "$540 in 2026-09 vs cohort median $390, 25% above the baseline. Recommend "
        "shifting routine charging to the depot off-peak slot.",
    ),
    (
        "block",
        "real_pii_name",
        "Driver Jane Doe exceeded cohort by $180 in 2026-09",
        "Driver Jane Doe's vehicle MRDAZ01S1SR033UKS had energy cost of $540 in "
        "2026-09. Driver's phone is 555-123-4567. Recommend shifting routine "
        "charging to the depot off-peak slot.",
    ),
    (
        "block",
        "blame_of_person",
        "Driver's wasteful habits caused $540 energy excess in 2026-09",
        "The driver is careless and wasteful. Fire the person responsible for the "
        "vehicle's charging. Recommend dismissing them.",
    ),
    (
        "block",
        "off_topic_advice",
        "Buy index funds to hedge the energy budget",
        "You should hire a tax attorney to deduct this expense on your 2026 "
        "personal return. See a doctor about back pain while driving.",
    ),
]

#: Prompt 157 (2026-10-28): the 28 driver-coaching cars reproduced from the 10-28
#: cost run, with the replies the 1.1.0 playbook wrote for them.
COACHING_FIXTURE = (
    Path(__file__).resolve().parents[1] / "tests" / "fixtures"
    / "coaching_replies_2026-10-28.json"
)


def _coaching_probes() -> list[tuple[str, str, str, str]]:
    """One pass case per reproduced car (``coaching_01``..): its new-prompt reply.

    Named by position, not VIN, so probe output carries no VIN fragment.
    """
    cars = json.loads(COACHING_FIXTURE.read_text())["cars"]
    return [
        ("pass", f"coaching_{i:02d}",
         car["new_prompt"]["headline"], car["new_prompt"]["detail"])
        for i, car in enumerate(cars, start=1)
    ]


PROBES += _coaching_probes()


def discover_guardrail_id() -> tuple[str, str]:
    lam = boto3.client("lambda", region_name=REGION)
    env = (
        lam.get_function_configuration(FunctionName=HEALTH_AGENT_FN)
        .get("Environment", {})
        .get("Variables", {})
    )
    gid = env.get("FI_COST_GUARDRAIL_ID", "")
    gver = env.get("FI_COST_GUARDRAIL_VERSION", "DRAFT")
    if not gid:
        raise RuntimeError(
            f"{HEALTH_AGENT_FN} has no FI_COST_GUARDRAIL_ID — nothing to probe."
        )
    return gid, gver


def main() -> int:
    if os.environ.get("LIVE_GUARDRAIL_PROBE") != "1":
        print(
            "Refusing to run: this probe makes live Bedrock ApplyGuardrail calls. "
            "Set LIVE_GUARDRAIL_PROBE=1 to acknowledge.",
            file=sys.stderr,
        )
        return 2
    gid, gver = discover_guardrail_id()
    br = boto3.client("bedrock-runtime", region_name=REGION)
    now = datetime.now(timezone.utc).isoformat()
    print(f"# live probe guardrail={gid} version={gver} ts={now}")
    failures: list[str] = []
    rows: list[dict] = []
    for expected, name, headline, detail in PROBES:
        resp = br.apply_guardrail(
            guardrailIdentifier=gid,
            guardrailVersion=gver,
            source="OUTPUT",
            content=[
                {"text": {"text": headline}},
                {"text": {"text": detail}},
            ],
        )
        action = resp.get("action")
        trips: list[tuple[str, str, str]] = []
        for a in resp.get("assessments", []):
            for t in (a.get("topicPolicy") or {}).get("topics", []) or []:
                trips.append(("topic", t.get("name"), t.get("action")))
            for p in (a.get("sensitiveInformationPolicy") or {}).get("piiEntities", []) or []:
                trips.append(("pii", p.get("type"), p.get("action")))
            for c in (a.get("contentPolicy") or {}).get("filters", []) or []:
                trips.append(("content", c.get("type"), c.get("action")))
        ok = (expected == "pass" and action == "NONE") or (
            expected == "block" and action == "GUARDRAIL_INTERVENED"
        )
        rows.append(
            {
                "name": name,
                "expected": expected,
                "action": action,
                "trips": trips,
                "ok": ok,
            }
        )
        print(f"  {'OK ' if ok else 'FAIL'} {expected:<6} {name:<22} action={action}")
        if not ok:
            failures.append(f"{name}: expected={expected} action={action}")
            for kind, nm, act in trips:
                print(f"       trip: [{kind}] name={nm} action={act}")
    if failures:
        print(f"\nFAILED: {len(failures)}/{len(PROBES)} probes did not match expectation:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"\nOK: {len(PROBES)}/{len(PROBES)} probes matched expectation.")
    print(json.dumps({"guardrail_id": gid, "guardrail_version": gver, "rows": rows},
                     default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
