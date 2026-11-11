"""Pin the cost component's data grants to its SQL (spec 2026-10-02-fi-cost-stage-b 4.2).

Python, because this directory runs in the gating CI job and jest runs in none
(test_run_staleness_alarm_pinned.py). The stacks are read with regexes, not compiled;
``infrastructure/test/vsa-tier2-stack-fi-cost.test.ts`` checks the synthesized template.

* Every ADP database the component's SQL names, and every base table the two cost
  views read, has a Lake Formation grant for the Tier 2 role and a Glue metadata grant.
* ``dimensions`` is granted by the named ``vins`` table only (it also holds
  ``customers``, PII) in both places.
* The answer-key prefix (``restricted/``) is denied, and no Allow names the whole lake.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_TIER2 = (_REPO / "infrastructure" / "lib" / "vsa-tier2-stack.ts").read_text()
_ANALYTICS = (_REPO / "infrastructure" / "lib" / "vsa-analytics-stack.ts").read_text()
_SQL_DIR = _REPO / "agents" / "fi_cost" / "sql"

#: The views' base tables outside the databases the SQL names directly (decisions
#: 2026-10-04 "Group 1 findings" item 1; ADP vehicle_cost_monthly.sql).
_VIEW_BASE_DATABASES = {"fleet_ops", "energy_usage", "service_records", "dimensions"}


def _sql_databases() -> set[str]:
    found: set[str] = set()
    for path in _SQL_DIR.glob("*.sql"):
        text = re.sub(r"--[^\n]*", "", path.read_text())
        found |= set(re.findall(r"\badp_\$\{stage\}_(\w+)\.\w+", text))
    return found


def _string_list(name: str, src: str) -> list[str]:
    m = re.search(rf"const {name}[^=]*=\s*\[(.*?)\];", src, re.S)
    assert m, f"{name} not found"
    # Any quoted entry counts, digits included ('customer_360' must not slip past).
    return re.findall(r"'([^']+)'", re.sub(r"//[^\n]*", "", m.group(1)))


def _named_tables() -> list[tuple[str, str]]:
    m = re.search(r"const fiCostNamedTables[^=]*=\s*\[(.*?)\];", _ANALYTICS, re.S)
    assert m, "fiCostNamedTables not found"
    return re.findall(r"\['([^']+)',\s*'([^']+)'\]", m.group(1))


def _tier2_block() -> str:
    start = _ANALYTICS.index("if (props.tier2RoleName)")
    return _ANALYTICS[start:]


def test_every_database_the_component_reads_has_a_lake_formation_grant():
    block = _tier2_block()
    wildcard = set(_string_list("tier2Products", block)) | set(_string_list("fiCostWildcardDbs", block))
    named = {db for db, _ in _named_tables()}
    needed = _sql_databases() | _VIEW_BASE_DATABASES
    assert {"fleet_cost", "vehicle_pricing"} <= _sql_databases()   # anti-vacuity
    assert needed <= wildcard | named, sorted(needed - (wildcard | named))


def test_dimensions_is_granted_by_the_vins_table_only():
    block = _tier2_block()
    wildcard = set(_string_list("tier2Products", block)) | set(_string_list("fiCostWildcardDbs", block))
    assert "dimensions" not in wildcard
    assert ("dimensions", "vins") in _named_tables()
    assert [t for db, t in _named_tables() if db == "dimensions"] == ["vins"]


def _glue_resources() -> list[str]:
    m = re.search(r"sid: 'FiCostGlueMetadataRead'.*?resources: \[(.*?)\]", _TIER2, re.S)
    assert m, "FiCostGlueMetadataRead not found"
    return re.findall(r"`([^`]+)`", m.group(1))


def test_glue_metadata_covers_the_same_databases_and_dimensions_only_by_vins():
    resources = _glue_resources()
    dbs = {re.search(r"database/adp_\$\{adpStage\}_(\w+)$", r).group(1)
           for r in resources if "database/" in r}
    existing = set(re.findall(r"database/adp_\$\{adpStage\}_(\w+)`",
                              _TIER2[_TIER2.index("sid: 'AdpGlueMetadataRead'"):
                                     _TIER2.index("sid: 'LakeFormationGetDataAccess'")]))
    needed = _sql_databases() | _VIEW_BASE_DATABASES
    assert needed <= dbs | existing, sorted(needed - (dbs | existing))
    dims = [r for r in resources if "adp_${adpStage}_dimensions" in r]
    assert sorted(dims) == sorted([
        "arn:aws:glue:${adpRegion}:${adpAccount}:database/adp_${adpStage}_dimensions",
        "arn:aws:glue:${adpRegion}:${adpAccount}:table/adp_${adpStage}_dimensions/vins",
    ])


def test_the_answer_key_prefix_is_denied_and_no_allow_names_the_whole_lake():
    deny = re.search(r"sid: 'DenyAdpRestrictedObjects',\s*effect: iam\.Effect\.DENY,.*?"
                     r"resources: \[`\$\{adpLakeBucketArn\}/restricted/\*`\]", _TIER2, re.S)
    assert deny, "DenyAdpRestrictedObjects must deny ${adpLakeBucketArn}/restricted/*"
    listing = re.search(r"sid: 'DenyAdpRestrictedListing',\s*effect: iam\.Effect\.DENY,.*?"
                        r"'s3:prefix': \['restricted', 'restricted/\*'\]", _TIER2, re.S)
    assert listing, "DenyAdpRestrictedListing must deny listing restricted/"
    # A whole-lake object ARN in any statement would reach the answer key.
    assert "${adpLakeBucketArn}/*`" not in _TIER2
    assert "`${adpLakeBucketArn}/restricted" not in _TIER2.replace(
        "resources: [`${adpLakeBucketArn}/restricted/*`]", "")


def test_the_runner_agent_id_and_the_schedule_component_agree():
    from agents.fi_cost.finding_keys import AGENT_ID
    from agents.tier2 import runner

    assert runner.FI_COST_AGENT_ID == AGENT_ID
    assert re.search(r"\{ component: 'cost',\s+hour: '4', minute: '0'", _TIER2)  # task 4.3: entry gains `enabled` field



def _statement(sid: str) -> str:
    start = _TIER2.index(f"sid: '{sid}'")
    return _TIER2[start:_TIER2.index("}));", start)]


def test_the_answer_key_deny_is_unconditioned_and_covers_both_reads():
    """A conditioned Deny (say, only over plain HTTP) is not a denial (cycle 1 W3); a Deny
    on one of the two object reads leaves the other open (cycle 2 S2)."""
    st = _statement("DenyAdpRestrictedObjects")
    assert "conditions" not in st and "iam.Effect.DENY" in st
    assert "actions: ['s3:GetObject', 's3:GetObjectVersion']," in st


def test_the_daily_schedules_run_on_utc():
    """Cycle 2 W1: the schedule loop sets no time zone, so CDK renders Etc/UTC."""
    loop = _TIER2[_TIER2.index("for (const { component, hour, minute, enabled } of componentSchedules)"):]  # task 4.3: enabled added
    loop = loop[:loop.index("// ── diagnostics")]
    assert "timeZone" not in loop and "TimeZone" not in loop
    # No flexible window either (cycle 3): a late start eats the staleness window's margin.
    assert "timeWindow" not in loop and "TimeWindow" not in loop
    assert "scheduler.ScheduleExpression.cron({" in loop


def test_the_cost_lake_formation_lists_are_exactly_these():
    """Cycle 3 infra W2: task 4.2 says 'pinned exactly'. Adding a database (customer_360,
    say: PII) to either list must fail here."""
    block = _tier2_block()
    assert _string_list("fiCostWildcardDbs", block) == ["fleet_cost", "fleet_ops"]
    assert _named_tables() == [("dimensions", "vins"), ("vehicle_pricing", "vehicle_pricing")]
    assert _string_list("tier2Products", block) == [
        "energy_usage", "tire_health", "service_records", "vehicle_identity"]


def _staleness_entries() -> dict[str, int]:
    m = re.search(r"const runStalenessWindows:.*?const runStalenessAlarms", _TIER2, re.S)
    assert m, "runStalenessWindows not found"
    # task 4.3: entries now have actionsEnabled; allow trailing fields before the closing }.
    return {c: int(h) for c, h in re.findall(r"\{ component: '([a-z]+)', emptyHours: (\d+)[^}]*\}", m.group(0))}


def test_cost_has_the_daily_staleness_window_and_no_health_alarms():
    """Cycle 1 W4: cost's window is the daily one, and cost is not in alarmComponents
    (zero anomalies and three quiet days are normal for monthly cost data)."""
    # task 4.3: alarmComponents.map entries gain actionsEnabled; match up to emptyHours only.
    daily = int(re.search(r"\.\.\.alarmComponents\.map\(\(component\) => \(\{ component, emptyHours: (\d+)",
                          _TIER2).group(1))
    assert _staleness_entries()["cost"] == daily == 27
    assert "cost" not in _string_list("alarmComponents", _TIER2)
    assert {"battery", "tires", "brakes"} <= set(_string_list("alarmComponents", _TIER2))


def test_the_wall_budget_is_inside_the_run_duration_alarm():
    """Cycle 1 W5: the cost run's clock is capped at TIER2_MAX_WALL_SECONDS (runner), so the
    budget must not exceed the shared run-duration alarm's threshold."""
    budget = int(re.search(r"TIER2_MAX_WALL_SECONDS:\s+'(\d+)'", _TIER2).group(1))
    alarm = _TIER2[_TIER2.index("'Tier2RunDurationAlarm'"):]
    threshold_ms = int(re.search(r"threshold:\s+([\d_]+),", alarm).group(1).replace("_", ""))
    assert 0 < budget * 1000 <= threshold_ms


def test_apply_guardrail_is_scoped_to_the_cost_guardrail():
    """Security review cycle 1 suggestion: ApplyGuardrail on '*' would let the role use any
    guardrail; jest checks the template, this pin runs in CI.

    Since 839ef3de (2026-10-18, cross-Region guardrail inference for the STANDARD topic
    tier) the grant also names the US guardrail profile in each of its three destination
    Regions. The resource list, the Region list and the profile ARN template that build it
    are each pinned exactly, mirroring the jest pin in vsa-tier2-stack-fi-cost.test.ts.

    Mutations run 2026-11-10, all caught: resources widened to ['*']; the profile ARN's
    Region, account or profile id replaced by '*'; a fourth Region (us-west-1) in the list;
    a second guardrail ARN added to the resources.
    """
    st = _statement("ApplyFiCostGuardrail")
    assert "actions: ['bedrock:ApplyGuardrail']," in st
    assert "*" not in st, st
    m = re.search(r"resources: \[(.*?)\],", st, re.S)
    assert m, st
    entries = [e.strip() for e in re.sub(r"//[^\n]*", "", m.group(1)).split(",") if e.strip()]
    assert entries == [
        "fiCostGuardrail.attrGuardrailArn",
        "...fiCostGuardrailRegions.map(fiCostGuardrailProfileArn)",
    ], entries
    assert _string_list("fiCostGuardrailRegions", _TIER2) == ["us-east-1", "us-east-2", "us-west-2"]
    assert re.search(r"const fiCostGuardrailProfileId = 'us\.guardrail\.v1:0';", _TIER2)
    profile = re.search(
        r"const fiCostGuardrailProfileArn = \(r: string\): string =>\s*`([^`]+)`;", _TIER2)
    assert profile, "fiCostGuardrailProfileArn not found"
    assert profile.group(1) == (
        "arn:aws:bedrock:${r}:${account}:guardrail-profile/${fiCostGuardrailProfileId}")
