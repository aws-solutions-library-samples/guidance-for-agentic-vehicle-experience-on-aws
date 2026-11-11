"""Pin the FI fleet-health component's infrastructure contracts (task 4.3 B2).

Python regex pins over the CDK TypeScript source; comments are stripped first.
CI runs no jest (see test_run_staleness_alarm_pinned.py). Spec task 4.3 B2.

Five checks:
(a) Both constants in fi-fleet-health-live.ts are exactly ``false``.
(b) The stack derives both model-enabled env values from fiHealthModelLive and
    FAULT_GROUNDING_ENABLED from faultGroundingLive (``props.fiFaultGroundingLive ??
    FI_FAULT_GROUNDING_LIVE``, stage D 7.7).
(c) The service schedule is 05:40 with ``enabled: fiHealthLive``; its staleness
    window has ``actionsEnabled: fiHealthLive``; the investigations event source
    has ``enabled: fiHealthLive``.
(d) Queue pins: visibility >= _STALE_RUNNING_AGE_SECONDS; visibility + Lambda
    timeout < _STALE_QUEUED_AGE_SECONDS; maxReceiveCount == SQS_MAX_RECEIVE_COUNT
    == runner default; Lambda timeout == TIER2_FUNCTION_TIMEOUT_SECONDS default.
(e) Reads <-> grants both ways: every adp_${stage}_{db}.{table} in the allowed
    databases extracted from selection.py, fi_quality/readers.py, and
    snapshot_freshness.py equals the FiHealthGlueMetadataRead table list in
    vsa-tier2-stack.ts AND the fiHealthNamedTables list in vsa-analytics-stack.ts.
    Anti-vacuity: at least 10 tables.
"""
from __future__ import annotations

import re
from pathlib import Path

from agents.tier2.tests._tier2_stack_text import strip_comments

_REPO      = Path(__file__).resolve().parents[3]
_TIER2     = strip_comments((_REPO / "infrastructure" / "lib" / "vsa-tier2-stack.ts").read_text())
_ANALYTICS = strip_comments((_REPO / "infrastructure" / "lib" / "vsa-analytics-stack.ts").read_text())
_FLAG_FILE = strip_comments((_REPO / "infrastructure" / "lib" / "fi-fleet-health-live.ts").read_text())
_VSA_BIN   = (_REPO / "infrastructure" / "bin" / "vsa.ts").read_text()

# ── Allowed ADP databases for check (e) ──────────────────────────────────────
_ALLOWED_DBS = frozenset({"fleet_health", "engineering", "ota_campaigns"})

# Source files that hold the fleet-health SQL/table references.
_SELECTION_PY       = _REPO / "agents" / "fi_fleet_health" / "selection.py"
_FI_QUALITY_READERS = _REPO / "agents" / "fi_quality" / "readers.py"
_SNAPSHOT_FRESH     = _REPO / "agents" / "tier2" / "snapshot_freshness.py"


def _require(pattern: str, src: str, label: str = "") -> re.Match:
    """Fail-closed: raises AssertionError when *pattern* is absent from *src*."""
    m = re.search(pattern, src, re.S)
    assert m, f"pattern not found{f' ({label})' if label else ''}: {pattern[:100]!r}"
    return m


# ── (a) Live on (Group 6 rest-plan step 7), model off ────────────────────────

def test_live_flag_on_model_flag_off():
    """(a) ``FI_FLEET_HEALTH_LIVE`` is exactly ``true`` (Group 6 rest-plan step 7, after ADP's
    views, PM grants and snapshots job exist) and ``FI_FLEET_HEALTH_MODEL_LIVE`` is exactly
    ``false`` (its own later flip). Was test_flag_default_is_false until then. bin/vsa.ts must
    not pass either flag — the default in the module is the gate."""
    _require(r"\bFI_FLEET_HEALTH_LIVE\s*=\s*true\s*;", _FLAG_FILE, "FI_FLEET_HEALTH_LIVE")
    _require(r"\bFI_FLEET_HEALTH_MODEL_LIVE\s*=\s*false\s*;", _FLAG_FILE, "FI_FLEET_HEALTH_MODEL_LIVE")
    assert len(re.findall(r"\bexport const FI_FLEET_HEALTH_LIVE\b", _FLAG_FILE)) == 1
    assert len(re.findall(r"\bexport const FI_FLEET_HEALTH_MODEL_LIVE\b", _FLAG_FILE)) == 1
    assert "fiFleetHealthLive" not in _VSA_BIN, (
        "bin/vsa.ts must not pass fiFleetHealthLive — the module constant is the gate")
    assert "fiFleetHealthModelLive" not in _VSA_BIN, (
        "bin/vsa.ts must not pass fiFleetHealthModelLive — the module constant is the gate")


def test_fault_grounding_switch_value():
    """Stage D 7.7: ``FI_FAULT_GROUNDING_LIVE`` is exactly ``true`` (its reviewed flip; deployed
    only on the user's go, after the pm-schedules migration), declared once, and bin/vsa.ts
    doesn't pass ``fiFaultGroundingLive`` (the module constant is the gate)."""
    _require(r"\bFI_FAULT_GROUNDING_LIVE\s*=\s*true\s*;", _FLAG_FILE, "FI_FAULT_GROUNDING_LIVE")
    assert len(re.findall(r"\bexport const FI_FAULT_GROUNDING_LIVE\b", _FLAG_FILE)) == 1
    assert "fiFaultGroundingLive" not in _VSA_BIN, (
        "bin/vsa.ts must not pass fiFaultGroundingLive — the module constant is the gate")


# ── (b) Model env values derived from fiHealthModelLive; FAULT_GROUNDING off ──

def test_model_env_values_derived_from_switch():
    """(b) Both FI_SERVICE_MODEL_ENABLED and FI_INVESTIGATION_MODEL_ENABLED use the ternary
    ``fiHealthModelLive ? 'true' : 'false'`` (task43-notes.md items 1 and 4).
    FAULT_GROUNDING_ENABLED derives from faultGroundingLive the same way, set exactly once.

    Mutation: set one of them to a literal 'false' → this test fails.
    """
    _require(r"FI_SERVICE_MODEL_ENABLED:\s*fiHealthModelLive \? 'true' : 'false'", _TIER2,
             "FI_SERVICE_MODEL_ENABLED ternary")
    _require(r"FI_INVESTIGATION_MODEL_ENABLED:\s*fiHealthModelLive \? 'true' : 'false'", _TIER2,
             "FI_INVESTIGATION_MODEL_ENABLED ternary")
    _require(r"FAULT_GROUNDING_ENABLED:\s*faultGroundingLive \? 'true' : 'false'", _TIER2,
             "FAULT_GROUNDING_ENABLED ternary")
    # Review g78 D W1: count every form of the name (an addEnvironment() call too), not
    # only the object-literal key, and require the one setter inside the health agent's
    # own construct. Mutants caught: a second addEnvironment(...'true') setter (m1), and
    # the key moved to another function's env (m7, the agent then defaults to 'false').
    assert len(re.findall(r"FAULT_GROUNDING_ENABLED", _TIER2)) == 1, (
        "FAULT_GROUNDING_ENABLED must appear exactly once in the stack, from faultGroundingLive")
    _fn_block = _TIER2[_TIER2.index("'Tier2HealthAgentFn'"):
                       _TIER2.index("healthAgentFn.addEventSource")]
    _require(r"FAULT_GROUNDING_ENABLED:\s*faultGroundingLive \? 'true' : 'false'", _fn_block,
             "FAULT_GROUNDING_ENABLED ternary inside the Tier2HealthAgentFn construct")
    for other in sorted((_REPO / "infrastructure").glob("bin/*.ts")) + sorted(
            (_REPO / "infrastructure" / "lib").rglob("*.ts")):
        if other.name == "vsa-tier2-stack.ts":
            continue
        assert "FAULT_GROUNDING_ENABLED" not in strip_comments(other.read_text()), (
            f"{other} must not set FAULT_GROUNDING_ENABLED; the tier2 stack is its one setter")
    _require(
        r"const faultGroundingLive\s*=\s*props\.fiFaultGroundingLive\s*\?\?\s*FI_FAULT_GROUNDING_LIVE\s*;",
        _TIER2, "faultGroundingLive derivation",
    )
    # fiHealthLive must be derived from the prop with ?? FI_FLEET_HEALTH_LIVE (C1 pin).
    _require(
        r"const fiHealthLive\s*=\s*props\.fiFleetHealthLive\s*\?\?\s*FI_FLEET_HEALTH_LIVE",
        _TIER2, "fiHealthLive derivation",
    )
    # fiHealthModelLive must be derived from the prop (same pattern as fiHealthLive).
    _require(
        r"const fiHealthModelLive\s*=\s*props\.fiFleetHealthModelLive\s*\?\?\s*FI_FLEET_HEALTH_MODEL_LIVE",
        _TIER2, "fiHealthModelLive derivation",
    )
    # Review g43 cycle 2 S-c: the analytics stack's LF-grant gate has the same derivation,
    # and only jest (which runs in no CI job) checked it; ``?? true`` there would put LF
    # grants on stage D tables that don't exist yet and fail the analytics deploy.
    _require(
        r"const fiHealthLive\s*=\s*props\.fiFleetHealthLive\s*\?\?\s*FI_FLEET_HEALTH_LIVE\s*;"
        r"\s*if\s*\(\s*fiHealthLive\s*\)",
        _ANALYTICS, "analytics fiHealthLive derivation and gate",
    )


# ── (c) Schedule, staleness window, event source all gated on fiHealthLive ────

def test_service_schedule_and_staleness_bound_to_flag():
    """(c) The service schedule is at 05:40 with ``enabled: fiHealthLive``; the service
    staleness window has ``actionsEnabled: fiHealthLive``; the investigations SQS event
    source has ``enabled: fiHealthLive`` (gated).

    Mutation: change any ``fiHealthLive`` to ``true`` → the respective test fires.
    """
    _require(
        r"\{\s*component:\s*'service',\s*hour:\s*'5',\s*minute:\s*'40',\s*enabled:\s*fiHealthLive\s*\}",
        _TIER2, "service schedule entry",
    )
    _require(
        r"\{\s*component:\s*'service',[^{}]*actionsEnabled:\s*fiHealthLive\s*\}",
        _TIER2, "service staleness window",
    )
    # The investigations event source has enabled: fiHealthLive (B1 wired this).
    _require(
        r"SqsEventSource\([^)]*avxInvestigationsQueue[^)]*\)|addEventSource\(new lambdaEventSources\.SqsEventSource",
        _TIER2, "SqsEventSource call",
    )
    _require(r"enabled:\s*fiHealthLive,", _TIER2, "event source enabled: fiHealthLive")


def test_switched_on_paths_depend_on_the_fi_health_policy():
    """Security g43 c1 S-c, carried to the Group 6 live flip: the investigations event
    source mapping and the service schedule wait for ``FiHealthTier2Policy`` (their runs need
    its grants), and nothing else is made to. jest checks the synthesised DependsOn; this pin
    keeps it in CI, which runs no jest."""
    _require(r"const fiHealthTier2Policy\s*=\s*new iam\.ManagedPolicy\(this,\s*'FiHealthTier2Policy'",
             _TIER2, "FiHealthTier2Policy bound to a name")
    _require(r"investigationsMappings\[0\]\.node\.addDependency\(fiHealthTier2Policy\);",
             _TIER2, "event source mapping depends on the policy")
    _require(r"if \(component === 'service'\) componentSchedule\.node\.addDependency\(fiHealthTier2Policy\);",
             _TIER2, "service schedule depends on the policy")
    assert len(re.findall(r"addDependency\(fiHealthTier2Policy\)", _TIER2)) == 2


# ── (d) Queue pins ────────────────────────────────────────────────────────────

def test_queue_pins():
    """(d) Stack queue values are consistent with write.py's stale-age module constants
    and the Lambda timeout default (task43-notes.md item 3).

    Regexes are anchored to their construct (AvxInvestigationsQueue block;
    Tier2HealthAgentFn block) so position changes in the file cannot mask a mismatch.

    visibility >= fq_write._STALE_RUNNING_AGE_SECONDS
    visibility + Lambda timeout < fq_write._STALE_QUEUED_AGE_SECONDS
    maxReceiveCount == SQS_MAX_RECEIVE_COUNT env var == runner.py's default
    Lambda timeout == TIER2_FUNCTION_TIMEOUT_SECONDS default (900 s)

    Cap pins (M8): TIER2_MAX_VINS_SERVICE/INVESTIGATION and
    TIER2_MAX_TOKENS_SERVICE/INVESTIGATION stack values must equal runner defaults;
    FI_HEALTH_MAX_MODEL_CALLS must equal agents.fi_fleet_health.llm.SERVICE_MAX_MODEL_CALLS.

    M4: write.py _STALE_QUEUED_AGE_SECONDS 7200→6000 → 5400+900=6300 >= 6000 → fails.
    M8: TIER2_MAX_VINS_SERVICE stack '500'→'5000' → 5000 != runner default 500 → fails.
    """
    import agents.fi_quality.write as fq_write
    import agents.tier2.runner as runner_mod
    from agents.fi_fleet_health.llm import SERVICE_MAX_MODEL_CALLS as _llm_cap

    # ── Anchor: AvxInvestigationsQueue construct block ──────────────────────
    _q_start = _TIER2.index("'AvxInvestigationsQueue'")
    _q_end   = _TIER2.index("});", _q_start) + 3
    _q_block = _TIER2[_q_start:_q_end]

    vm = _require(r"visibilityTimeout:\s*cdk\.Duration\.seconds\((\d+)\)", _q_block,
                  "visibilityTimeout (AvxInvestigationsQueue)")
    visibility = int(vm.group(1))

    rm = _require(r"maxReceiveCount:\s*(\d+)", _q_block,
                  "maxReceiveCount (AvxInvestigationsQueue)")
    stack_max_receive = int(rm.group(1))

    # ── Anchor: Tier2HealthAgentFn Lambda construct block ───────────────────
    _fn_start  = _TIER2.index("'Tier2HealthAgentFn'")
    _fn_rest   = _TIER2[_fn_start:]
    _next_fn   = _fn_rest.find("new lambda.Function", 1)
    _fn_block  = _fn_rest[:_next_fn] if _next_fn > 0 else _fn_rest

    tm = _require(r"timeout:\s*cdk\.Duration\.minutes\((\d+)\)", _fn_block,
                  "Lambda timeout (Tier2HealthAgentFn)")
    timeout_s = int(tm.group(1)) * 60

    # ── SQS_MAX_RECEIVE_COUNT Lambda env var ─────────────────────────────────
    em = _require(r"SQS_MAX_RECEIVE_COUNT:\s*'(\d+)'", _TIER2, "SQS_MAX_RECEIVE_COUNT env")
    env_max_receive = int(em.group(1))

    # ── write.py module constants (no env overrides in CI) ───────────────────
    stale_running = fq_write._STALE_RUNNING_AGE_SECONDS
    stale_queued  = fq_write._STALE_QUEUED_AGE_SECONDS

    # ── runner.py's own default for SQS_MAX_RECEIVE_COUNT ────────────────────
    import re as _re
    _runner_src = Path(runner_mod.__file__).read_text()
    _rcm = _re.search(r'"SQS_MAX_RECEIVE_COUNT",\s*"(\d+)"', _runner_src)
    assert _rcm, "SQS_MAX_RECEIVE_COUNT default not found in runner.py"
    runner_max_receive = int(_rcm.group(1))

    assert visibility >= stale_running, (
        f"visibility {visibility}s < _STALE_RUNNING_AGE_SECONDS {stale_running}s; "
        "a running row would look stale before the Lambda finishes")
    assert visibility + timeout_s < stale_queued, (
        f"visibility {visibility}s + timeout {timeout_s}s >= _STALE_QUEUED_AGE_SECONDS {stale_queued}s; "
        "a requeued message could arrive after the row is already stale")
    assert stack_max_receive == env_max_receive == runner_max_receive, (
        f"maxReceiveCount={stack_max_receive}, SQS_MAX_RECEIVE_COUNT env={env_max_receive}, "
        f"runner default={runner_max_receive}; all three must agree")
    assert timeout_s == 900, (
        f"Lambda timeout {timeout_s}s != TIER2_FUNCTION_TIMEOUT_SECONDS default 900")

    # ── Cap pins: stack env values == runner module defaults (M8 guard) ──────
    def _stack_env_int(name: str) -> int:
        m = re.search(rf"{re.escape(name)}:\s*'(\d+)'", _TIER2)
        assert m, f"{name} not found in vsa-tier2-stack.ts env block"
        return int(m.group(1))

    assert _stack_env_int("TIER2_MAX_VINS_SERVICE")       == runner_mod._MAX_VINS_SERVICE, \
        "TIER2_MAX_VINS_SERVICE stack != runner default"
    assert _stack_env_int("TIER2_MAX_VINS_INVESTIGATION") == runner_mod._MAX_VINS_INVESTIGATION, \
        "TIER2_MAX_VINS_INVESTIGATION stack != runner default"
    assert _stack_env_int("TIER2_MAX_TOKENS_SERVICE")     == runner_mod._MAX_TOKENS_SERVICE, \
        "TIER2_MAX_TOKENS_SERVICE stack != runner default"
    assert _stack_env_int("TIER2_MAX_TOKENS_INVESTIGATION") == runner_mod._MAX_TOKENS_INVESTIGATION, \
        "TIER2_MAX_TOKENS_INVESTIGATION stack != runner default"
    assert _stack_env_int("FI_HEALTH_MAX_MODEL_CALLS")    == _llm_cap, \
        "FI_HEALTH_MAX_MODEL_CALLS stack != SERVICE_MAX_MODEL_CALLS default"


# ── (e) Reads <-> grants ──────────────────────────────────────────────────────

_DB_TABLE_RE = re.compile(r"adp_\$?\{stage\}_(\w+)\.(\w+)")


def _sql_tables_in_allowed_dbs(path: Path) -> set[tuple[str, str]]:
    """Extract (db, table) pairs from ``adp_${stage}_{db}.{table}`` SQL string constants,
    filtering to _ALLOWED_DBS. Strips ``#`` and ``--`` comment lines.

    vehicle_telemetry_aggregated is referenced only in docstrings in readers.py, not
    in any live SQL; removing it from _ALLOWED_DBS excludes those docstring references
    from the grant check."""
    src = re.sub(r"(#|--).*", "", path.read_text())
    return {(db, tbl) for db, tbl in _DB_TABLE_RE.findall(src) if db in _ALLOWED_DBS}


def _glue_tables_from_statement(sid: str) -> set[tuple[str, str]]:
    """table/ ARNs from the named Glue IAM statement → (db, table) pairs.

    Scoped to the Sid block: start at ``sid: '<sid>'`` and stop at the next
    PolicyStatement close.  The close can be either ``}));`` (inline
    ``addToPolicy(new PolicyStatement({...}))``) or ``}),`` followed by a
    newline (a ``PolicyStatement`` passed as an array item inside a
    ``ManagedPolicy``'s ``statements: [ ... ]``).  Picking the first of
    either terminator keeps the extraction tight to this one statement
    even when the file holds multiple ManagedPolicy constructs after it
    (e.g., FI stage E's FiRebalanceGlueMetadataRead).
    """
    start = _TIER2.index(f"sid: '{sid}'")
    rest = _TIER2[start:]
    # Earliest of the two close patterns.
    close_inline = rest.find("}));")
    close_array = rest.find("}),\n")
    cands = [c for c in (close_inline, close_array) if c != -1]
    assert cands, f"no terminator found after sid: '{sid}'"
    end = start + min(cands)
    block = _TIER2[start:end]
    pairs: set[tuple[str, str]] = set()
    for m in re.finditer(r"table/adp_\$\{adpStage\}_(\w+)/(\w+)", block):
        pairs.add((m.group(1), m.group(2)))
    return pairs


def _analytics_lf_tables(const_name: str) -> set[tuple[str, str]]:
    """Named-table (db, table) pairs from a ``const fiHealthNamedTables`` literal."""
    m = re.search(rf"const {re.escape(const_name)}\b[^=]*=\s*\[(.*?)\];", _ANALYTICS, re.S)
    assert m, f"{const_name} not found in vsa-analytics-stack.ts"
    return {(db, tbl) for db, tbl in re.findall(r"\['(\w+)',\s*'(\w+)'\]", m.group(1))}


def test_reads_match_glue_grants_and_lf_grants():
    """(e) The (db, table) pairs read by the fleet-health agent code exactly match the
    FiHealthGlueMetadataRead statement in the tier2 stack AND the fiHealthNamedTables list
    in the analytics stack. Anti-vacuity: at least 10 tables.

    Mutation: add a reader table to selection.py's SQL without a matching grant → fails.
    """
    src_tables: set[tuple[str, str]] = set()
    src_tables |= _sql_tables_in_allowed_dbs(_SELECTION_PY)
    src_tables |= _sql_tables_in_allowed_dbs(_FI_QUALITY_READERS)
    src_tables |= _sql_tables_in_allowed_dbs(_SNAPSHOT_FRESH)

    glue_tables     = _glue_tables_from_statement("FiHealthGlueMetadataRead")
    analytics_tables = _analytics_lf_tables("fiHealthNamedTables")

    assert len(src_tables) >= 10, f"anti-vacuity: only {len(src_tables)} tables extracted from sources"

    assert src_tables == glue_tables, (
        f"source reads vs. FiHealthGlueMetadataRead mismatch: "
        f"missing grants {sorted(src_tables - glue_tables)}, "
        f"extra grants {sorted(glue_tables - src_tables)}"
    )
    assert src_tables == analytics_tables, (
        f"source reads vs. fiHealthNamedTables mismatch: "
        f"missing LF grants {sorted(src_tables - analytics_tables)}, "
        f"extra LF grants {sorted(analytics_tables - src_tables)}"
    )



def test_c3_no_cms_ingest_in_tier2_or_analytics_grants():
    """C3: The comment-stripped text of vsa-tier2-stack.ts and vsa-analytics-stack.ts
    must contain no occurrence of 'cms_ingest'.  After comment stripping, only live
    code remains, so any 'cms_ingest' in the stripped text is a live IAM resource
    reference (or similar grant), which is forbidden.

    Anti-vacuity: the scan must find the FiHealthGlueMetadataRead Sid in the tier2
    source and the fiHealthNamedTables const in the analytics source, proving neither
    file was empty or unparsed.

    Mutation M3: a new statement granting glue:GetTable on
    table/adp_${adpStage}_cms_ingest/vehicles → fails because the stripped source
    contains 'cms_ingest'.
    """
    # Anti-vacuity: both source files contain the expected FI-health markers.
    assert "FiHealthGlueMetadataRead" in _TIER2, \
        "anti-vacuity: FiHealthGlueMetadataRead Sid not found in vsa-tier2-stack.ts"
    assert "fiHealthNamedTables" in _ANALYTICS, \
        "anti-vacuity: fiHealthNamedTables not found in vsa-analytics-stack.ts"

    assert "cms_ingest" not in _TIER2, (
        "vsa-tier2-stack.ts (comment-stripped) contains 'cms_ingest' — "
        "no IAM grant must reference adp_${stage}_cms_ingest"
    )
    assert "cms_ingest" not in _ANALYTICS, (
        "vsa-analytics-stack.ts (comment-stripped) contains 'cms_ingest' — "
        "no LF or IAM grant must reference adp_${stage}_cms_ingest"
    )



# ── (f) The gate tests run in CI ──────────────────────────────────────────────

#: Stage D's Python tests that guard the switch gates (both default off), the infra pins and
#: the runner's model-path wiring. Review g43 cycle 2 W-a: none ran in any CI job, and jest
#: (the synthesized-template tests) runs in none either.
_STAGE_D_GATE_TESTS = (
    "agents/tier2/tests/test_fi_health_infra_pinned.py",
    "agents/tier2/tests/test_runner_service_model_wiring.py",
    "agents/tier2/tests/test_runner_investigation_model_wiring.py",
)


def test_stage_d_gate_tests_run_in_both_ci_files():
    """(f) Each stage D gate test is a pytest target in GitLab's ``verify_fi_tier2_pins``
    (with its JUnit report checked by fail-on-skipped-tests.py and listed as an artifact)
    and in a ``python3 -m pytest`` line of lint.yml that isn't a comment.

    Mutation: delete any one file's lines from either CI file → this test fails.

    The public mirror ships neither ``.gitlab-ci.yml`` nor ``.publish-exclude``; there only
    the lint.yml half runs (lint.yml is the mirror's CI). In the internal repo a missing
    ``.gitlab-ci.yml`` still fails.
    """
    import yaml

    gitlab_path = _REPO / ".gitlab-ci.yml"
    public_copy = not gitlab_path.exists() and not (_REPO / ".publish-exclude").exists()
    lint_lines = [ln.strip() for ln in (_REPO / ".github" / "workflows" / "lint.yml")
                  .read_text().splitlines()]
    for path in _STAGE_D_GATE_TESTS:
        assert f"python3 -m pytest {path} -q" in lint_lines, (
            f"{path}: no uncommented `python3 -m pytest {path} -q` line in lint.yml")
    if public_copy:
        return

    gitlab = yaml.safe_load(gitlab_path.read_text())
    job = gitlab.get("verify_fi_tier2_pins")
    assert isinstance(job, dict), "GitLab job verify_fi_tier2_pins not found"
    script = [s for s in job.get("script", []) if isinstance(s, str)]
    reports = job.get("artifacts", {}).get("reports", {}).get("junit", [])

    for path in _STAGE_D_GATE_TESTS:
        runs = [s for s in script if "pytest" in s and s.split()[-1] == path]
        assert len(runs) == 1, f"{path}: expected one pytest entry in verify_fi_tier2_pins, got {len(runs)}"
        jm = re.search(r"--junitxml=(\S+)", runs[0])
        assert jm, f"{path}: its pytest entry writes no --junitxml report"
        junit = jm.group(1)
        assert f"python3 scripts/lib/fail-on-skipped-tests.py {junit}" in script, (
            f"{path}: {junit} is not checked by fail-on-skipped-tests.py")
        assert junit in reports, f"{path}: {junit} is not in the job's junit artifacts"
