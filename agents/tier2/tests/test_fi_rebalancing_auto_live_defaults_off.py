"""Pin the rule-approved-moves switch (FI rebalancing stage E Group 8, U6; group8-notes G8-D9).

``FI_REBALANCING_AUTO_APPROVAL_LIVE`` in ``infrastructure/lib/fi-rebalancing-auto-live.ts``
stayed ``false`` until task 8.4's go (CMS's internal API deployed, the bounds parameter
seeded, ``/cvx/{stage}/cms-fi-internal-api-endpoint`` seeded). It is now ``true``: that flip
is what lets the auto-approver approve a ``move_vehicles`` Action and call CMS, and it was
its own reviewed commit. Turning it off again is likewise one commit that changes it and
this test together.

The second half pins that everything the flag withholds is actually behind it in
``vsa-tier2-stack.ts``: the four auto-approver grants are made inside the
``if (autoMovesLive)`` block and no other grant to the auto-approver's role names an
``execute-api``, ``ssm``, ``events`` or ``cloudwatch`` action; the move switch, the bounds
name and the CMS endpoint are set only there; and the auto-approver Lambda's own
``environment`` carries none of the four keys except through ``...autoMovesEnv``. A grant or
an env var moved out of the block would ship with the flag off, so turning it off would
not withhold it.

Python, because this directory runs in the gating CI job (verify_fi_tier2_pins) and jest
runs in none.
"""
from __future__ import annotations

import os
import re

import pytest


_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_LIVE_TS = os.path.join(_REPO_ROOT, "infrastructure", "lib", "fi-rebalancing-auto-live.ts")
_TIER2_TS = os.path.join(_REPO_ROOT, "infrastructure", "lib", "vsa-tier2-stack.ts")

_GATED_SIDS = (
    "AutoApproverCmsInternalMovesInvoke",
    "AutoApproverBoundsRead",
    "AutoApproverEscalationPutEvents",
    "AutoApproverEscalationPutMetric",
)
_GATED_ENV = (
    "FI_AUTO_MOVES_ENABLED",
    "AVX_AUTO_APPROVAL_BOUNDS_PARAM",
    "VSA_CMS_FI_INTERNAL_API_ENDPOINT",
    "VSA_STAGE",
)

# Sids that are inside the rebook gate, not the moves gate.
_REBOOK_GATED_SIDS = (
    "AutoApproverDmsRebookInvoke",
    "AutoApproverFindingsByVinQuery",
    "AutoApproverGroundingsRebookQuery",
    "AutoApproverActionsGetItem",
    "AutoApproverEscalationPutEventsRebook",
    "AutoApproverEscalationPutMetricRebook",
    # Predicate (g), prompt 347: the hold list (ssm:GetParameter on one parameter).
    "AutoApproverSimOperatorHoldsRead",
    # Prompt 361: the DMS executor's retry-policy and escalation parameters.
    "AutoApproverDmsExecutorPolicyRead",
)


def _read(path: str) -> str:
    with open(path) as fh:
        return fh.read()


def _gated_block(src: str) -> tuple[str, str]:
    """Return (block body, file with the block cut out) for ``if (autoMovesLive) {...}``."""
    m = re.search(r"if\s*\(\s*autoMovesLive\s*\)\s*\{", src)
    assert m, "`if (autoMovesLive) {` not found in vsa-tier2-stack.ts"
    depth, i = 1, m.end()
    while depth:
        assert i < len(src), "unbalanced braces after `if (autoMovesLive) {`"
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        i += 1
    return src[m.end():i - 1], src[:m.start()] + src[i:]


def test_auto_approval_live_value():
    m = re.search(
        r"export\s+const\s+FI_REBALANCING_AUTO_APPROVAL_LIVE\s*(?::\s*boolean)?\s*=\s*(true|false)\s*;",
        _read(_LIVE_TS),
    )
    assert m, "FI_REBALANCING_AUTO_APPROVAL_LIVE declaration not found"
    assert m.group(1) == "true", (
        "FI_REBALANCING_AUTO_APPROVAL_LIVE is true since task 8.4's go; turning it off is one "
        "reviewed commit that changes it and this test together."
    )


def test_the_stack_defaults_to_the_constant():
    src = _read(_TIER2_TS)
    assert re.search(
        r"const\s+autoMovesLive\s*=\s*props\.fiRebalancingAutoApprovalLive\s*\?\?\s*"
        r"FI_REBALANCING_AUTO_APPROVAL_LIVE\s*;",
        src,
    ), "autoMovesLive must default to FI_REBALANCING_AUTO_APPROVAL_LIVE"


def _call_bodies(src: str, opener: str) -> list[str]:
    """The text inside each ``<opener>(...)`` call, matched by parentheses."""
    out = []
    for m in re.finditer(re.escape(opener) + r"\(", src):
        depth, i = 1, m.end()
        while depth:
            depth += {"(": 1, ")": -1}.get(src[i], 0)
            i += 1
        out.append(src[m.end():i - 1])
    return out


def _auto_approver_fn(src: str) -> list[str]:
    """The props of ``new lambda.Function(this, 'AvxAutoApproverFn', {...})`` (exactly one)."""
    return [b for b in _call_bodies(src, "new lambda.Function")
            if b.lstrip().startswith("this, 'AvxAutoApproverFn'")]


#: The simulated fleet operator (FI D10, 11.4) is a different function with its own
#: role; it needs the CMS internal API for the groundings route, never the move route.
#: Its block is checked on its own below, and taken out of ``rest`` for the gate checks.
_SIM_OPERATOR_START = "// ── FI Sim Operator (D10)"
_SIM_OPERATOR_END = "// ── Outputs"


def _split_sim_operator(rest: str) -> tuple[str, str]:
    start = rest.index(_SIM_OPERATOR_START)
    end = rest.index(_SIM_OPERATOR_END, start)
    return rest[start:end], rest[:start] + rest[end:]


def _rebook_block(src: str) -> tuple[str, str]:
    """Return (block body, file with the block cut out) for ``if (autoRebookLive) {...}``."""
    m = re.search(r"if\s*\(\s*autoRebookLive\s*\)\s*\{", src)
    assert m, "`if (autoRebookLive) {` not found in vsa-tier2-stack.ts"
    depth, i = 1, m.end()
    while depth:
        assert i < len(src), "unbalanced braces after `if (autoRebookLive) {`"
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        i += 1
    return src[m.end():i - 1], src[:m.start()] + src[i:]


def test_sim_operator_block_never_reaches_the_move_route_or_the_auto_approver():
    _, rest = _gated_block(_read(_TIER2_TS))
    sim, _ = _split_sim_operator(rest)
    assert "internal/moves" not in sim
    assert "auto-approval-bounds" not in sim
    assert "autoApprover" not in sim
    assert "autoMovesEnv" not in sim
    # Its CMS grant is the groundings route only.
    assert "POST/api/v1/fleet-intelligence/internal/groundings" in sim


def test_grants_and_env_sit_only_inside_the_gate():
    src = _read(_TIER2_TS)
    block, rest = _gated_block(src)
    _, rest = _split_sim_operator(rest)
    # Carve out the rebook gate for the _REBOOK_GATED_SIDS presence/absence checks;
    # the moves-gate absence checks (Sids, env keys, routes) still scan the full rest
    # so a mutant that slips moves content into the rebook block is caught.
    rebook_block, rest_no_rebook = _rebook_block(rest)
    for sid in _GATED_SIDS:
        assert f"'{sid}'" in block, f"{sid} missing from the gated block"
        # Use quoted comparison so "AutoApproverEscalationPutEvents" does not
        # match "AutoApproverEscalationPutEventsRebook".
        assert f"'{sid}'" not in rest, f"{sid} appears outside `if (autoMovesLive)`"
    for key in _GATED_ENV:
        assert re.search(rf"autoMovesEnv\.{key}\s*=", block), f"{key} not set in the gated block"
    # Absence outside the gate (rebook block included so M6/M8 mutants are caught).
    # VSA_STAGE is set on other Lambdas, so it's checked only in the auto-approver's
    # own environment below.
    for key in ("FI_AUTO_MOVES_ENABLED", "AVX_AUTO_APPROVAL_BOUNDS_PARAM",
                "VSA_CMS_FI_INTERNAL_API_ENDPOINT"):
        assert key not in rest, f"{key} appears outside `if (autoMovesLive)`"
    assert "internal/moves" not in rest, "the internal move route is named outside the gate"
    assert "auto-approval-bounds" not in rest, "the bounds parameter is read outside the gate"
    assert "cmsFiInternalApiEndpoint" not in rest.split("interface VsaTier2StackProps", 1)[1] \
        .split("export class VsaTier2Stack", 1)[1], "the endpoint is resolved outside the gate"
    # The env object is declared empty outside the block and spread into the Lambda.
    assert re.search(r"const\s+autoMovesEnv\s*:\s*Record<string,\s*string>\s*=\s*\{\s*\}\s*;", rest)
    (fn,) = _auto_approver_fn(rest_no_rebook)
    env = fn.split("environment:", 1)[1].split("},", 1)[0]
    assert "...autoMovesEnv" in env
    for key in _GATED_ENV:
        assert key not in env, f"{key} set directly in the auto-approver's environment"
    assert "autoApproverFn.addEnvironment" not in src
    # No other grant to the role reaches what the gate withholds. The rebook block's
    # own statements are exempt by Sid (explicit allowlist); any other statement with
    # execute-api/ssm/events/cloudwatch is a defect.
    for opener in ("autoApproverRole.addToPolicy", "autoApproverRole.addToPrincipalPolicy"):
        for body in _call_bodies(rest, opener):
            if any(f"sid: '{sid}'" in body for sid in _REBOOK_GATED_SIDS):
                continue
            assert not re.search(r"'(execute-api|ssm|events|cloudwatch):", body), body
    for other in ("autoApproverRole.attachInlinePolicy", "autoApproverRole.addManagedPolicy",
                  "autoApproverFn.addToRolePolicy", "autoApproverFn.role"):
        assert other not in src, f"{other} grants outside the pinned statements"
    # Rebook-specific Sids sit inside the rebook gate, not in the double-rest.
    for sid in _REBOOK_GATED_SIDS:
        assert f"'{sid}'" in rebook_block, f"{sid} missing from the rebook gate"
        assert f"'{sid}'" not in rest_no_rebook, f"{sid} appears outside both gates"


def test_the_flag_file_says_why():
    src = _read(_LIVE_TS)
    assert "8.4" in src and "U6" in src


def test_this_pin_runs_in_both_ci_files():
    """This file is a pytest target in GitLab's ``verify_fi_tier2_pins`` (JUnit checked by
    fail-on-skipped-tests.py and listed as an artifact) and in an uncommented lint.yml line,
    as stage D's gate tests are (test_fi_health_infra_pinned (f))."""
    import yaml

    if not os.path.exists(os.path.join(_REPO_ROOT, ".gitlab-ci.yml")):
        # The public mirror ships lint.yml but not .gitlab-ci.yml (.publish-exclude); GitLab's
        # fail-on-skipped-tests check keeps this from skipping in the internal pipeline.
        pytest.skip(".gitlab-ci.yml is not in this tree")
    path = "agents/tier2/tests/test_fi_rebalancing_auto_live_defaults_off.py"
    gitlab = yaml.safe_load(_read(os.path.join(_REPO_ROOT, ".gitlab-ci.yml")))
    job = gitlab.get("verify_fi_tier2_pins")
    assert isinstance(job, dict), "GitLab job verify_fi_tier2_pins not found"
    script = [s for s in job.get("script", []) if isinstance(s, str)]
    runs = [s for s in script if "pytest" in s and s.split()[-1] == path]
    assert len(runs) == 1, f"expected one pytest entry for {path}, got {len(runs)}"
    jm = re.search(r"--junitxml=(\S+)", runs[0])
    assert jm, "its pytest entry writes no --junitxml report"
    assert f"python3 scripts/lib/fail-on-skipped-tests.py {jm.group(1)}" in script
    assert jm.group(1) in job.get("artifacts", {}).get("reports", {}).get("junit", [])
    lint = [ln.strip() for ln in _read(
        os.path.join(_REPO_ROOT, ".github", "workflows", "lint.yml")).splitlines()]
    assert f"python3 -m pytest {path} -q" in lint


def _statement(block: str, sid: str) -> str:
    i = block.index(f"sid: '{sid}'")
    return block[i:block.index("}));", i)]


def test_the_gated_grants_are_scoped():
    """What the role gets when the flag is on, at source level (jest's synth test runs in no
    CI job): execute-api on exactly the internal move route; the one bounds parameter, read
    only; the default bus only; metrics only in AVX/Executor. And turning the flag on without
    CMS's endpoint throws at synth."""
    block, _ = _gated_block(_read(_TIER2_TS))
    invoke = _statement(block, "AutoApproverCmsInternalMovesInvoke")
    assert re.search(r"actions:\s*\['execute-api:Invoke'\]", invoke)
    assert re.findall(r"`([^`]*)`", invoke) == [
        "arn:aws:execute-api:${region}:${account}:${cmsInternalApiId}/${cmsInternalStage}"
        "/POST/api/v1/fleet-intelligence/internal/moves"]
    # The API id and stage come from the endpoint the Lambda signs to, not a wildcard.
    assert "const endpointParts = cdk.Fn.split('/', cmsInternalEndpoint);" in block
    assert ("const cmsInternalApiId = cdk.Fn.select(0, cdk.Fn.split('.', "
            "cdk.Fn.select(2, endpointParts)));") in block
    assert "const cmsInternalStage = cdk.Fn.select(3, endpointParts);" in block
    assert "autoMovesEnv.VSA_CMS_FI_INTERNAL_API_ENDPOINT = cmsInternalEndpoint;" in block
    bounds = _statement(block, "AutoApproverBoundsRead")
    assert re.search(r"actions:\s*\['ssm:GetParameter'\]", bounds)
    assert re.findall(r"`([^`]*)`", bounds) == ["arn:aws:ssm:${region}:${account}:parameter${boundsParam}"]
    assert "const boundsParam = `/vsa/${stage}/avx/fi-rebalancing/auto-approval-bounds`;" in block
    events = _statement(block, "AutoApproverEscalationPutEvents")
    assert re.search(r"actions:\s*\['events:PutEvents'\]", events)
    assert re.findall(r"`([^`]*)`", events) == ["arn:aws:events:${region}:${account}:event-bus/default"]
    metric = _statement(block, "AutoApproverEscalationPutMetric")
    assert re.search(r"actions:\s*\['cloudwatch:PutMetricData'\]", metric)
    assert "conditions: { StringEquals: { 'cloudwatch:namespace': 'AVX/Executor' } }" in metric
    assert re.search(r"if \(!props\.cmsFiInternalApiEndpoint\) \{\s*throw new Error\(", block)


def _auto_approver_event_source(src: str) -> str:
    """The options of ``autoApproverFn.addEventSource(new ...DynamoEventSource(...))``."""
    (call,) = _call_bodies(src, "autoApproverFn.addEventSource")
    return call


def test_the_timeout_covers_a_batch_only_when_live():
    """Review c2 W2: the property is "timeout >= batch size x one dispatch's budget", so the
    stream's batch size and the timeout come from one pair of constants, the budget equals
    the executor's own, and the result fits Lambda's 900 s."""
    src = _read(_TIER2_TS)
    _, rest = _gated_block(src)
    batch = re.findall(r"const autoApproverBatchSize = (\d+);", rest)
    budget = re.findall(r"const autoApproverDispatchBudgetS = (\d+);", rest)
    assert len(batch) == 1 and len(budget) == 1, (batch, budget)
    batch_size, budget_s = int(batch[0]), int(budget[0])
    executor_src = _read(os.path.join(_REPO_ROOT, "agents", "executors", "base.py"))
    (executor_budget,) = re.findall(r"^SYNCHRONOUS_BUDGET_S = ([0-9.]+)$", executor_src, re.M)
    assert float(executor_budget) == budget_s, "the dispatch budget must be the executor's"
    (fn,) = _auto_approver_fn(rest)
    flat = re.sub(r"\s+", " ", fn)
    assert re.findall(r"timeout: ([^,]+),", flat) == [
        "cdk.Duration.seconds( autoMovesLive ? autoApproverBatchSize * autoApproverDispatchBudgetS"
        " + 50 : 60)"], re.findall(r"timeout: ([^,]+),", flat)
    assert batch_size >= 1 and batch_size * budget_s + 50 <= 900
    opts = _auto_approver_event_source(rest)
    assert re.findall(r"batchSize:\s*([^,\n]+),", opts) == ["autoApproverBatchSize"]
    assert "maxBatchingWindow" not in opts


def test_a_literal_endpoint_without_its_stage_fails_synth():
    """Review c2 W1: a concrete endpoint (env override) is checked against CMS's output shape
    before any grant is built from it; a token (SSM) is left to the seeder and the executor."""
    block, _ = _gated_block(_read(_TIER2_TS))
    opener = "if (!cdk.Token.isUnresolved(cmsInternalEndpoint)"
    assert block.count(opener) == 1, "the concrete-endpoint shape check is missing"
    check = block.index(opener)
    for later in ("autoMovesEnv.VSA_CMS_FI_INTERNAL_API_ENDPOINT =", "const endpointParts"):
        assert later in block and check < block.index(later), f"the check must precede {later}"
    pattern = re.search(r"&& !/(.+?)/\s*\.test\(cmsInternalEndpoint\)\) \{\s*throw new Error\(",
                        block[check:], re.S)
    assert pattern, "the shape check must throw"
    shape = re.compile(pattern.group(1).replace("\\/", "/"))
    good = ["https://yourapi000.execute-api.us-west-2.amazonaws.com/prod/",
            "https://yourapi000.execute-api.us-west-2.amazonaws.com/prod"]
    bad = ["https://yourapi000.execute-api.us-west-2.amazonaws.com",
           "https://yourapi000.execute-api.us-west-2.amazonaws.com/",
           "https://yourapi000.execute-api.us-west-2.amazonaws.com//",
           "https://yourapi000.execute-api.us-west-2.amazonaws.com/prod/extra",
           "https://yourapi000.execute-api.us-west-2.amazonaws.com/prod//",
           "https://yourapi000.execute-api.us-west-2.amazonaws.com/prod ",
           "http://yourapi000.execute-api.us-west-2.amazonaws.com/prod/",
           "https://cms.example.com/prod/",
           "https://yourapi000.execute-api.us-west-2.amazonaws.com.evil.test/prod/",
           # Review c3 S1: JS .test searches, so the pattern's own ^ and $ must reject these.
           "cms-staging-ui.FiInternalApiEndpoint = https://yourapi000.execute-api.us-west-2.amazonaws.com/prod/",
           'https://yourapi000.execute-api.us-west-2.amazonaws.com/prod/"']
    assert [bool(shape.search(v)) for v in good] == [True] * len(good)
    assert [v for v in bad if shape.search(v)] == []
