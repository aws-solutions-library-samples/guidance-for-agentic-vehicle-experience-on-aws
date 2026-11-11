"""Pin the FI recall/warranty component's infrastructure contracts (task 4.3).

Python regex pins over the CDK TypeScript source; comments are stripped first.
CI runs no jest (see test_run_staleness_alarm_pinned.py). Spec task 4.3.
"""
from __future__ import annotations

import re
from pathlib import Path

from agents.tier2.tests._tier2_stack_text import strip_comments

_REPO      = Path(__file__).resolve().parents[3]
_TIER2     = strip_comments((_REPO / "infrastructure" / "lib" / "vsa-tier2-stack.ts").read_text())
_ANALYTICS = strip_comments((_REPO / "infrastructure" / "lib" / "vsa-analytics-stack.ts").read_text())
_FLAG_FILE = strip_comments((_REPO / "infrastructure" / "lib" / "fi-recall-warranty-live.ts").read_text())
_VSA_BIN   = (_REPO / "infrastructure" / "bin" / "vsa.ts").read_text()
_SQL_DIR   = _REPO / "agents" / "fi_recall_warranty" / "sql"


def _block(pattern: str, src: str) -> re.Match:
    """Fail-closed: raises AssertionError when *pattern* is absent from *src*."""
    m = re.search(pattern, src, re.S)
    assert m, f"pattern not found: {pattern[:80]!r}"
    return m


def test_flag_is_live():
    # (a) Task 5.4 (2026-10-09): live once ADP 5.1 created adp_{stage}_fleet_recall, so the
    # schedules run, the alarms notify and analytics grants fleet_recall. Was
    # test_flag_default_is_false until then (decisions.md calls 55 and 61).
    _block(r"\bFI_RECALL_WARRANTY_LIVE\s*=\s*true\s*;", _FLAG_FILE)


def test_bin_vsa_never_passes_the_flag():
    # (b) bin/vsa.ts must not forward fiRecallWarrantyLive — the default in the
    # stack file is what controls the gate (decisions.md call 55).
    assert "fiRecallWarrantyLive" not in _VSA_BIN


def test_recall_and_warranty_schedule_times_and_enabled_bound_to_flag():
    # (c) Recall at 05:00 UTC, warranty at 05:20 UTC; both enabled only when live.
    # Each match is bounded to its own object literal ([^{}]*), so one entry's binding
    # can't satisfy the other's (architect fix: the unbounded .*? let recall borrow
    # warranty's `enabled: fiRwLive`).
    _block(r"\{\s*component:\s*'recall',\s*hour:\s*'5',\s*minute:\s*'0',\s*enabled:\s*fiRwLive\s*\}", _TIER2)
    _block(r"\{\s*component:\s*'warranty',\s*hour:\s*'5',\s*minute:\s*'20',\s*enabled:\s*fiRwLive\s*\}", _TIER2)
    _block(r"const fiRwLive = props\.fiRecallWarrantyLive \?\? FI_RECALL_WARRANTY_LIVE;", _TIER2)


def test_recall_and_warranty_staleness_actions_enabled_bound_to_flag():
    # (d) Run-staleness alarms fire only once the flag is live.
    _block(r"\{\s*component:\s*'recall',[^{}]*actionsEnabled:\s*fiRwLive\s*\}", _TIER2)
    _block(r"\{\s*component:\s*'warranty',[^{}]*actionsEnabled:\s*fiRwLive\s*\}", _TIER2)


def test_analytics_fleet_recall_grant_gated_on_flag():
    # (e) LF grant for fleet_recall must be inside if(props.fiRecallWarrantyLive):
    # the database does not exist until ADP task 5.1 (decisions.md call 15/55).
    # CHANGED (architect fix): the analytics stack read `props.fiRecallWarrantyLive` with no
    # default, so flipping the shared constant at task 5.4 would have enabled the schedules
    # but never the fleet_recall grant. Both stacks must derive the flag the same way.
    _block(r"import \{ FI_RECALL_WARRANTY_LIVE \} from './fi-recall-warranty-live';", _ANALYTICS)
    _block(r"const fiRwLive = props\.fiRecallWarrantyLive \?\? FI_RECALL_WARRANTY_LIVE;"
           r"\s*if \(fiRwLive\) \{\s*const recallDbName[^;]*;"
           r"\s*new lakeformation\.CfnPrincipalPermissions\(this, 'LfDbTier2FleetRecall'", _ANALYTICS)
    # Exactly one place creates each grant, and both sit after that `if`.
    assert _ANALYTICS.count("'LfDbTier2FleetRecall'") == 1
    assert _ANALYTICS.count("'LfTblTier2FleetRecall'") == 1
    gate = _ANALYTICS.index("if (fiRwLive) {")
    assert _ANALYTICS.index("'LfTblTier2FleetRecall'") > gate


# ── helpers for (f) ───────────────────────────────────────────────────────────

def _fi_rw_glue_resources() -> list[str]:
    m = re.search(r"sid:\s*'FiRwGlueMetadataRead'.*?resources:\s*\[(.*?)\]", _TIER2, re.S)
    assert m, "FiRwGlueMetadataRead not found"
    return re.findall(r"`([^`]+)`", m.group(1))


def _allow_role_glue_resources() -> list[str]:
    """All Glue resources from Allow-effect statements in the tier2 stack (excludes Deny).

    S1 (infra cycle 2): scanning ALL statements accidentally passed N5 because the
    table/ ARN for a database whose database/ ARN was deleted still matched the
    presence check.  Restrict to Allow SIDs and require both database/ and table/ ARNs.
    """
    result: list[str] = []
    for sid in ("AdpGlueMetadataRead", "FiCostGlueMetadataRead", "FiRwGlueMetadataRead"):
        st = _stmt(sid)
        # Architect fix (infra cycle 3 S1): the docstring promised Allow-only; check it.
        assert re.search(r"effect:\s*iam\.Effect\.ALLOW", st), f"{sid} must be an Allow"
        result.extend(re.findall(r"arn:aws:glue:[^`\s]+", st))
    return result


def _sql_refs() -> tuple[set[tuple[str, str]], set[str]]:
    """Returns (adp_pairs, dms_tables) from comment-stripped SQL files."""
    adp: set[tuple[str, str]] = set()
    dms: set[str] = set()
    for p in _SQL_DIR.glob("*.sql"):
        txt = re.sub(r"--[^\n]*", "", p.read_text())
        adp |= set(re.findall(r"adp_\$\{stage\}_(\w+)\.(\w+)", txt))
        dms |= set(re.findall(r"dms_\$\{stage\}\.(\w+)", txt))
    return adp, dms


def test_fi_rw_glue_statement_names_exact_dms_tables_and_sql_is_covered():
    # (f) Exact DMS table set; fleet_recall covered by wildcard; every SQL ref covered;
    # every recall_vehicle_status base-table database wildcard-covered (S1).
    resources = _fi_rw_glue_resources()
    dms_tables = {r.split("/dms_${adpStage}/")[1]
                  for r in resources if "/dms_${adpStage}/" in r}
    assert dms_tables == {"repair_orders", "parts_inventory", "rooftops",
                          "warranty_candidates", "warranty_terms"}, dms_tables
    assert any("fleet_recall/*" in r for r in resources), "fleet_recall/* missing"
    adp_pairs, dms_sql = _sql_refs()
    assert adp_pairs, "anti-vacuity: no adp refs found in SQL files"
    # Check SQL-referenced ADP databases against FiRwGlueMetadataRead only.
    # FI stage D 9.3: engineering is read by NAMED table, so an engineering ref is covered
    # only when FiRwEngineeringGlueRead names that exact table (no wildcard counts).
    eng_named = _fi_rw_engineering_glue_tables()
    for db, tbl in adp_pairs:
        if db == "engineering":
            assert tbl in eng_named, f"adp engineering.{tbl} not named in FiRwEngineeringGlueRead"
            continue
        assert any(f"_{db}/*" in r for r in resources), f"adp db {db} not wildcard-covered"
    for tbl in dms_sql:
        assert tbl in dms_tables, f"SQL dms table {tbl!r} not in CDK statement"
    # S1 (cycle 2): recall_vehicle_status base-table databases must be covered with BOTH
    # a database/ ARN and a table/ ARN in the health-agent role's Allow statements.
    # Restricting to Allow statements (not all statements) prevents a Deny-side Glue ARN
    # from satisfying the check.  Requiring both database/ and table/ catches N5 (the
    # database/ ARN for service_records was deleted; table/ remained and previously passed).
    allow_glue = _allow_role_glue_resources()
    assert allow_glue, "anti-vacuity: no Glue ARNs found in tier2 Allow statements"
    for db in _RECALL_VIEW_BASE_DATABASES:
        assert any(f"database/" in r and f"_{db}" in r for r in allow_glue), \
            f"recall base-table database {db!r}: database/ ARN missing from Allow statements"
        assert any(f"table/" in r and f"_{db}/" in r for r in allow_glue), \
            f"recall base-table database {db!r}: table/ ARN missing from Allow statements"
    # Analytics LF counterpart: each base database has DESCRIBE + SELECT in the analytics
    # stack (via tier2Products or fiCost lists).
    # (Architect tightening: the names must sit in the TIER 2 role's grant lists, not anywhere
    # in the file; 'vehicle_identity' also appears in the AgentCore role's adpProducts.)
    tier2_lf: set[str] = set()
    for name in ("tier2Products", "fiCostWildcardDbs"):
        m = re.search(rf"const {name}[^=]*=\s*\[(.*?)\];", _ANALYTICS, re.S)
        assert m, f"{name} not found in vsa-analytics-stack.ts"
        tier2_lf |= set(re.findall(r"'([^']+)'", m.group(1)))
    m = re.search(r"const fiCostNamedTables[^=]*=\s*\[(.*?)\];", _ANALYTICS, re.S)
    assert m, "fiCostNamedTables not found in vsa-analytics-stack.ts"
    tier2_lf |= {pair[0] for pair in re.findall(r"\['([^']+)',\s*'([^']+)'\]", m.group(1))}
    for db in _RECALL_VIEW_BASE_DATABASES:
        assert db in tier2_lf, f"recall base-table database {db!r} has no Tier 2 LF grant"



# _BASE_TABLE_DATABASES: the ADP databases that recall_vehicle_status reads via its
# base tables (apply_ddl.py _BASE_TABLE_TEMPLATES, commit dc600697 in
# ~/automotive-data-platform-on-aws platform-foundation/source/data-products/
# recall_campaigns/apply_ddl.py).  fleet_recall is already covered by the
# fleet_recall/* wildcard asserted in test (f); vehicle_identity / service_records /
# fleet_ops / dimensions are covered only via other components' grants — add them
# here so a narrowing of those grants is caught without this component's tests
# succeeding (cf. test_fi_cost_grants_pinned._VIEW_BASE_DATABASES pattern).
_RECALL_VIEW_BASE_DATABASES: frozenset[str] = frozenset({
    "vehicle_identity",
    "service_records",
    "fleet_ops",
    "dimensions",
    # fleet_ops.daily_usage is itself a view over energy_usage (FI cost Stage B), and
    # warranty_coverage reads daily_usage (infra cycle 3 S1).
    "energy_usage",
})


def test_lakeformation_get_data_access_statement_exact():
    # (g) Selected by SID; effect must be ALLOW and resources exactly ['*'].
    # M2 mutant: effect ALLOW → DENY — this assertion fails. resource must be
    # ['*'] (LakeFormation:GetDataAccess has no ARN-level scoping, AWS constraint).
    st = _stmt("LakeFormationGetDataAccess")
    assert re.search(r"iam\.Effect\.ALLOW", st), "GetDataAccess effect must be ALLOW"
    assert re.search(r"actions:\s*\['lakeformation:GetDataAccess'\]", st), \
        "actions must be exactly ['lakeformation:GetDataAccess']"
    assert re.search(r"resources:\s*\['\*'\]", st), "resources must be exactly ['*']"


def test_no_allow_names_restricted_and_deny_covers_restricted_prefix():
    # (h) DenyAdpRestrictedObjects resources must be exactly `…/restricted/*`;
    # no Allow statement may include 'restricted' in its resource list.
    _block(r"sid:\s*'DenyAdpRestrictedObjects'.*?/restricted/\*\x60\]", _TIER2)
    allows = re.findall(r"iam\.Effect\.ALLOW.*?resources:\s*\[([^\]]*)\]", _TIER2, re.S)
    assert allows, "anti-vacuity: no Allow statements found"
    for lst in allows:
        assert "restricted" not in lst, f"Allow resource names restricted: {lst[:80]}"


# ── helper for (i) ────────────────────────────────────────────────────────────

def _stmt(sid: str) -> str:
    start = _TIER2.index(f"sid: '{sid}'")
    return _TIER2[start:_TIER2.index("}));", start)]


def test_four_dynamodb_statements_have_exact_action_and_resource():
    # (i) Each statement pins its action list literally and the correct table/index.
    # M3 mutants: Scan added to FiRwCmsGroundingsQuery; FiRwActionsGetItem swapped to
    # avxFindingsTable.  Both mutants now caught by the assertions below.
    # N1 (W-1 cycle 2): FiRwActionsGetItem resources widened to [avxActionsTable.tableArn, '*'].
    # Presence checks ('"this.avxActionsTable.tableArn" in st') passed N1; replaced with
    # exact-list regex for all four statements to catch any resource widening.
    st = _stmt("FiRwCmsFleetLocationsQuery")
    assert re.search(r"actions:\s*\['dynamodb:Query'\]", st), "FiRwCmsFleetLocationsQuery action not exact"
    assert re.search(
        r"resources:\s*\[`arn:aws:dynamodb:\$\{cmsRegion\}:\$\{cmsAccount\}:table/cms-\$\{cmsStage\}-storage-fleet-locations`\]",
        st,
    ), "FiRwCmsFleetLocationsQuery resources must be exactly the fleet-locations table"

    st = _stmt("FiRwCmsGroundingsQuery")
    assert re.search(r"actions:\s*\['dynamodb:Query'\]", st), "FiRwCmsGroundingsQuery action not exact"
    assert re.search(
        r"resources:\s*\[`arn:aws:dynamodb:\$\{cmsRegion\}:\$\{cmsAccount\}:table/cms-\$\{cmsStage\}-storage-vehicle-groundings`\]",
        st,
    ), "FiRwCmsGroundingsQuery resources must be exactly the vehicle-groundings table"

    st = _stmt("FiRwFindingsByVinQuery")
    assert re.search(r"actions:\s*\['dynamodb:Query'\]", st), "FiRwFindingsByVinQuery action not exact"
    assert re.search(r"resources:\s*\[`[^`]*/index/by_vin`\]", st), "by_vin index not sole resource"
    assert "avxFindingsTable.tableArn" in st, "FiRwFindingsByVinQuery must use avxFindingsTable"

    st = _stmt("FiRwActionsGetItem")
    assert re.search(r"actions:\s*\['dynamodb:GetItem'\]", st), "FiRwActionsGetItem action not exact"
    assert "index/" not in st
    # Exact resource list: exactly [this.avxActionsTable.tableArn] — no '*' or second entry.
    assert re.search(r"resources:\s*\[this\.avxActionsTable\.tableArn\]", st), \
        "FiRwActionsGetItem resources must be exactly [this.avxActionsTable.tableArn]"
    assert "avxFindingsTable" not in st, "FiRwActionsGetItem must not reference avxFindingsTable"


def test_model_id_env_is_the_granted_inference_profile():
    """Architect fix (infra cycle 1 Suggestion 3): the recall/warranty model id the Lambda gets
    is the inference profile the Bedrock statements are scoped to, and the runner's default is
    the same id, so the model and its IAM grant can't drift apart."""
    from agents.tier2 import runner

    m = re.search(r"const claudeProfileId = '([^']+)';", _TIER2)
    assert m, "claudeProfileId literal not found in vsa-tier2-stack.ts"
    assert re.search(r"FI_RW_MODEL_ID:\s*claudeProfileId,", _TIER2), (
        "FI_RW_MODEL_ID must be set from claudeProfileId, not a literal")
    assert runner._FI_RW_DEFAULT_MODEL_ID == m.group(1)



def test_scheduler_schedule_passes_enabled_from_loop_entry():
    # Critical (infra cycle 2): the `new scheduler.Schedule(...)` inside the
    # componentSchedules for-loop must pass `enabled` from the loop variable, not a
    # literal.  Failing to pass it (or hardcoding `true`) silently enables recall and
    # warranty schedules even when fiRwLive is false.
    # N2 mutant: `enabled,` → `enabled: true,`; N2-del: line deleted entirely.
    # Each variant must fail this test on its own assertion.
    #
    # Strategy: find the for-loop body that iterates componentSchedules, then assert
    # that the Schedule constructor call inside it passes `enabled` as a shorthand or
    # explicit `enabled: enabled` — not `enabled: true` or absent.
    m = re.search(
        r"for\s*\(\s*const\s*\{[^}]*enabled[^}]*\}\s*of\s*componentSchedules\s*\)(.*?)\n    \}",
        _TIER2, re.S,
    )
    assert m, "componentSchedules for-loop not found"
    loop_body = m.group(1)
    # The Schedule constructor must be present in this body.
    assert "new scheduler.Schedule" in loop_body, \
        "scheduler.Schedule not found inside componentSchedules loop"
    # Must pass enabled as shorthand (`enabled,`) or explicit (`enabled: enabled`).
    # Must NOT pass `enabled: true` or omit the property.
    assert re.search(r"\benabled,|\benabled:\s*enabled\b", loop_body), \
        "scheduler.Schedule must pass enabled from the loop entry (not literal true, not absent)"
    assert not re.search(r"\benabled:\s*true\b", loop_body), \
        "scheduler.Schedule must not hardcode enabled: true"


def test_staleness_alarm_passes_actions_enabled_from_loop_entry():
    # Critical (infra cycle 2): the `new cloudwatch.Alarm(...)` for AvxRunStalenessAlarm
    # inside the runStalenessWindows for-loop must pass `actionsEnabled` from the loop
    # variable.  Hardcoding or omitting it silently enables paging even when fiRwLive
    # is false.
    # N3 mutant: `actionsEnabled,` → `actionsEnabled: true,`; N3-del: line deleted.
    m = re.search(
        r"for\s*\(\s*const\s*\{[^}]*actionsEnabled[^}]*\}\s*of\s*runStalenessWindows\s*\)(.*?)\n    \}",
        _TIER2, re.S,
    )
    assert m, "runStalenessWindows for-loop not found"
    loop_body = m.group(1)
    assert "new cloudwatch.Alarm" in loop_body, \
        "cloudwatch.Alarm not found inside runStalenessWindows loop"
    # Must pass actionsEnabled as shorthand or explicit `actionsEnabled: actionsEnabled`.
    assert re.search(r"\bactionsEnabled,|\bactionsEnabled:\s*actionsEnabled\b", loop_body), \
        "cloudwatch.Alarm must pass actionsEnabled from the loop entry (not literal true, not absent)"
    assert not re.search(r"\bactionsEnabled:\s*true\b", loop_body), \
        "cloudwatch.Alarm must not hardcode actionsEnabled: true"


def test_every_tier2_lf_grant_is_describe_db_and_select_describe_tables():
    """Architect fix (infra cycle 3 S1, reviewer mutant NM4): each Tier 2 Lake Formation grant in
    vsa-analytics-stack.ts is DESCRIBE on the database and exactly SELECT + DESCRIBE on tables,
    no grant option. Dropping SELECT would leave the recall view unreadable while every name
    check above still passes."""
    grants = re.findall(
        r"new lakeformation\.CfnPrincipalPermissions\(this,\s*[`'](LfDbTier2|LfTblTier2)[^`']*[`'],"
        r"\s*\{(.*?)\n\s*\}\);", _ANALYTICS, re.S)
    db = [body for kind, body in grants if kind == "LfDbTier2"]
    tbl = [body for kind, body in grants if kind == "LfTblTier2"]
    assert len(db) == 5 and len(tbl) == 5, (len(db), len(tbl))
    # three loops + fleet_recall + the 9.3 grantTier2Engineering helper (one database, one
    # table construct; architect-authorized count change, FI stage D 9.3)
    for body in db:
        assert re.search(r"permissions:\s*\['DESCRIBE'\],", body), body
        assert re.search(r"permissionsWithGrantOption:\s*\[\],", body), body
    for body in tbl:
        assert re.search(r"permissions:\s*\['SELECT', 'DESCRIBE'\],", body), body
        assert re.search(r"permissionsWithGrantOption:\s*\[\],", body), body


# ── FI stage D 9.3: the recall run's engineering reads ────────────────────────
#
# ADP 9.1 (ADP 9bb82e96, platform-foundation/source/data-products/recall_campaigns/views/):
# recall_campaigns_received unions engineering.recall_campaigns_issued and
# recall_vehicle_status joins it and engineering.vehicle_build_components (the lot rule).
# A standard view resolves as the caller, so the live recall run needs Glue and Lake
# Formation read on both. The recall query's own engineering refs (select_recalls.sql)
# are added to this set below, so a new engineering read there needs its grants too.
_RECALL_VIEW_ENGINEERING_TABLES: frozenset[str] = frozenset({
    "recall_campaigns_issued",
    "vehicle_build_components",
})


def _recall_engineering_tables() -> set[str]:
    adp_pairs, _ = _sql_refs()
    return set(_RECALL_VIEW_ENGINEERING_TABLES) | {t for db, t in adp_pairs if db == "engineering"}


def _managed_stmt(sid: str) -> str:
    """A statement inside a ManagedPolicy's ``statements: [...]`` (ends ``}),``)."""
    start = _TIER2.index(f"sid: '{sid}'")
    return _TIER2[start:_TIER2.index("}),", start)]


def _fi_rw_engineering_glue_tables() -> set[str]:
    st = _managed_stmt("FiRwEngineeringGlueRead")
    return set(re.findall(r"table/adp_\$\{adpStage\}_engineering/(\w+)`", st))


def test_recall_engineering_glue_statement_exact():
    """FiRwEngineeringGlueRead: an Allow of the four Glue read actions on exactly the
    engineering database and the recall run's engineering tables, no wildcard, inside its
    own ManagedPolicy on the health-agent role (the inline policies are near IAM's
    aggregate cap). Mutants: drop a table; add table/<eng>/*; move it inline."""
    st = _managed_stmt("FiRwEngineeringGlueRead")
    assert re.search(r"effect:\s*iam\.Effect\.ALLOW", st), "must be an Allow"
    assert re.search(
        r"actions:\s*\['glue:GetDatabase', 'glue:GetTable', 'glue:GetTables', 'glue:GetPartitions'\]",
        st), "actions must be exactly the four Glue reads"
    arns = re.findall(r"`(arn:aws:glue:[^`]+)`", st)
    expected = {"arn:aws:glue:${adpRegion}:${adpAccount}:database/adp_${adpStage}_engineering"} | {
        f"arn:aws:glue:${{adpRegion}}:${{adpAccount}}:table/adp_${{adpStage}}_engineering/{t}"
        for t in _recall_engineering_tables()}
    assert len(arns) == len(set(arns)), arns
    assert set(arns) == expected, sorted(set(arns) ^ expected)
    # Its ManagedPolicy, attached to the health-agent role.
    m = re.search(r"new iam\.ManagedPolicy\(this, 'FiRwEngineeringReadPolicy', \{\s*roles: \[lambdaRole\],"
                  r"\s*statements: \[\s*new iam\.PolicyStatement\(\{\s*sid: 'FiRwEngineeringGlueRead'", _TIER2)
    assert m, "FiRwEngineeringGlueRead must be the statement of FiRwEngineeringReadPolicy on lambdaRole"
    assert _TIER2.count("'FiRwEngineeringGlueRead'") == 1


def test_recall_engineering_lf_grants_under_the_recall_switch():
    """Lake Formation half: the analytics stack's fiRwEngineeringTables is exactly the recall
    run's engineering tables, and inside ``if (fiRwLive) {`` it grants the database and each
    of them through grantTier2Engineering, which makes DESCRIBE on the database and SELECT +
    DESCRIBE on a named table, no grant option. Mutants: drop a table from the list; move the
    calls out of the fiRw block; grant ALL; give a grant option."""
    m = re.search(r"const fiRwEngineeringTables\s*=\s*\[(.*?)\];", _ANALYTICS, re.S)
    assert m, "fiRwEngineeringTables not found"
    assert set(re.findall(r"'(\w+)'", m.group(1))) == _recall_engineering_tables()

    gate = _ANALYTICS.index("if (fiRwLive) {")
    end = _ANALYTICS.index("const fiHealthLive", gate)
    block = _ANALYTICS[gate:end]
    assert re.search(r"grantTier2Engineering\(\);\s*"
                     r"for \(const table of fiRwEngineeringTables\) grantTier2Engineering\(table\);",
                     block), "the fiRw block must grant the database and every recall engineering table"

    h = re.search(r"const grantTier2Engineering = \(table\?: string\): void => \{(.*?)\n      \};",
                  _ANALYTICS, re.S)
    assert h, "grantTier2Engineering helper not found"
    body = h.group(1)
    assert re.search(r"if \(tier2EngineeringGrants\.has\(key\)\) return;", body), "helper must de-duplicate"
    grants = re.findall(r"new lakeformation\.CfnPrincipalPermissions\(this, ([^,]+), \{(.*?)\n\s*\}\);",
                        body, re.S)
    assert [g[0] for g in grants] == ["'LfDbTier2Engineering'", "`LfTblTier2Engineering${tblSuffix}`"], grants
    db_body, tbl_body = grants[0][1], grants[1][1]
    assert "dataLakePrincipalIdentifier: tier2RoleArn" in db_body and \
        "dataLakePrincipalIdentifier: tier2RoleArn" in tbl_body
    assert re.search(r"permissions:\s*\['DESCRIBE'\],\s*permissionsWithGrantOption:\s*\[\],", db_body)
    assert re.search(r"permissions:\s*\['SELECT', 'DESCRIBE'\],\s*permissionsWithGrantOption:\s*\[\],",
                     tbl_body)
    assert "tableWildcard" not in body
    assert re.search(r"name:\s*table\s*\}", tbl_body), "table grant must name the table"
