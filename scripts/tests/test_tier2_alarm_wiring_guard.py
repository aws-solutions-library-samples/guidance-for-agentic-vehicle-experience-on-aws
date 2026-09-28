"""Guard tests for spec `2026-08-10-cvx-tier2-vin-selection-and-alerting`.

WHY THIS EXISTS
---------------
The Tier 2 platform health agent shipped on 2026-08-05 with two alarms that have
empty `AlarmActions` — a run that reads 6,000 rows, writes zero artifacts and exits 0
is indistinguishable from a healthy run on every metric currently emitted, and even a
real Lambda error notifies nobody.

This spec adds:
  - An SNS operator-alert topic (KMS-encrypted, enforceSSL)
  - `AlarmActions` on all four alarms pointing at the topic
  - `OKActions` on run-failure and no-vins alarms
  - Two new alarms (`no-vins-selected`, `artifacts-stale`) with `treatMissingData: BREACHING`
  - `cloudwatch:PutMetricData` IAM grant scoped by `cloudwatch:namespace` condition
  - Per-component batch limits: tires 156, battery 375, brakes 2,000
  - Optional email subscription (no default; warning when absent)

This file follows the convention established by `test_stage_defaults_guard.py` and
`test_guardrail_template_parity.py`: shell out to `npx cdk synth <stack> --json`,
parse `Resources`, and assert on real template properties from Python.

RED-PHASE STATUS
----------------
The `TestPositiveControls` class (tests that pass today) is included first so the
suite cannot be dismissed as always-red. It asserts on the two existing alarms and the
DynamoDB table — things that are present before this spec's changes land.

All other test classes fail against the current stack because the SNS topic, alarm
actions, new alarms, PutMetricData grant, and per-component batch limits do not yet
exist.

FIXTURE CORRECTION (matching test_stage_defaults_guard.py's lesson)
--------------------------------------------------------------------
`_base_env()` supplies a COMPLETE valid Tier B configuration. `cdk synth <stack>`
executes the whole of `bin/vsa.ts` and constructs every stack, so a synth missing a
Tier B field throws on whichever field resolves first — and the assertion lands on the
wrong error. Supply every field; each negative test removes or changes exactly one.

The tier2 stack itself is simple (no VPC lookups, no auth conditionals), but the app
still instantiates every other stack first and any missing Tier B field stops the
whole app before it reaches the tier2 stack.

PLACEHOLDER SAFETY
------------------
This file ships to the public mirror and is scanned by `scripts/lib/secret-scan.py`.
Account segment is all-ones to avoid matching the 12-digit account-ID scanner pattern.
Pool IDs use `eu-central-1_` prefix because the scanner's `cognito_user_pool_id`
pattern is anchored to the two US regions CVX actually deploys to. See
`test_stage_defaults_guard.py` § "PLACEHOLDER SAFETY" for the full reasoning.

SYNTH INVOCATION
----------------
  VSA_STAGE=test npx cdk synth vsa-test-tier2 --json
  (run from infrastructure/, with all Tier B env vars set via _base_env())

MODELLED ON
-----------
`scripts/tests/test_stage_defaults_guard.py` and
`scripts/tests/test_guardrail_template_parity.py` — module-scoped synth fixture,
skip-on-absent-node_modules, pytest.fail on non-zero exit, Resources parse.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INFRA_DIR = REPO_ROOT / "infrastructure"

# ── Placeholder values ───────────────────────────────────────────────────────
_TEST_ACCOUNT = "111122223333"
_TEST_REGION   = "us-west-2"

_MEM_KMS_ARN = f"arn:aws:kms:{_TEST_REGION}:{_TEST_ACCOUNT}:key/aaaabbbb-test-mem-key"
_CMS_KMS_ARN = f"arn:aws:kms:{_TEST_REGION}:{_TEST_ACCOUNT}:key/ccccdddd-test-cms-key"
_RUNTIME_ARN = (
    f"arn:aws:bedrock-agentcore:{_TEST_REGION}:{_TEST_ACCOUNT}"
    f":runtime/vsa_supervisor_text_test-TESTRUNTIME"
)

# Scanner-safe pool IDs (eu-central-1_ prefix is not in the scanner's anchored pattern).
_VPC_ID          = "vpc-00000000000000000"
_REDIS_SG_ID     = "sg-00000000000000000"
_VOICE_POOL_ID   = "eu-central-1_TESTPOOL01"
_VOICE_CLIENT_ID = "testvoiceclientid00000"
_EXTRA_POOL_IDS  = "eu-central-1_TESTPOOL02"
_TRUST_OPT_OUT   = "cvx-only-trust"
_OPT_OUT         = "none"

# A test email that is clearly synthetic and not a real address.
_TEST_EMAIL = "tier2-alerts-test@example.invalid"

# Stack name used throughout.
_TIER2_STACK = "vsa-test-tier2"


def _skip_if_no_tooling() -> None:
    if not (INFRA_DIR / "node_modules").is_dir():
        pytest.skip("infrastructure/node_modules absent — run `make install` first")
    if shutil.which("npx") is None:
        pytest.skip("npx not available")


def _base_env(**overrides: str) -> dict[str, str]:
    """Return a COMPLETE valid synth environment for VSA_STAGE=test.

    Every Tier B field must be present or synth throws before reaching the tier2
    stack. See the FIXTURE CORRECTION note in the module docstring.
    """
    env = {
        **os.environ,
        "VSA_STAGE":   "test",
        "AWS_REGION":  _TEST_REGION,
        "CDK_DEFAULT_ACCOUNT": _TEST_ACCOUNT,
        # Tier A overrides — literals bypass SSM so synth stays hermetic.
        "CVX_RUNTIME_ARN": _RUNTIME_ARN,
        "CMS_KMS_KEY_ARN": _CMS_KMS_ARN,
        # Tier B — all six fields, fail-closed.
        "VSA_MEMORY_KMS_KEY_ARN":        _MEM_KMS_ARN,
        "CMS_VPC_ID":                    _VPC_ID,
        "CMS_REDIS_SG_ID":               _REDIS_SG_ID,
        "VOICE_AUTH_USER_POOL_ID":       _VOICE_POOL_ID,
        "VOICE_AUTH_USER_POOL_CLIENT_ID": _VOICE_CLIENT_ID,
        "EXTRA_USER_POOL_IDS":           _EXTRA_POOL_IDS,
    }
    env.update(overrides)
    return env


def _synth(stack: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run `npx cdk synth <stack> --json` and return the completed process."""
    # Static argv, shell=False.
    # nosemgrep: dangerous-subprocess-use-audit
    return subprocess.run(
        ["npx", "cdk", "synth", stack, "--json"],
        cwd=INFRA_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )


# ── Module-scoped fixture: synthesise once, share across all test classes ────

@pytest.fixture(scope="module")
def tier2_template() -> dict[str, Any]:
    """Synthesise vsa-test-tier2 once for the whole module.

    The environment has no `CVX_TIER2_ALARM_EMAIL`, which is the base case:
    topic created, no email subscription, synth-time warning emitted.
    """
    _skip_if_no_tooling()
    proc = _synth(_TIER2_STACK, _base_env())
    if proc.returncode != 0:
        pytest.fail(
            f"cdk synth {_TIER2_STACK} failed with a complete valid config.\n"
            f"stderr:\n{proc.stderr[-3000:]}"
        )
    if not proc.stdout.strip().startswith("{"):
        pytest.fail(
            f"cdk synth produced no JSON output.\nstderr:\n{proc.stderr[-2000:]}"
        )
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def tier2_resources(tier2_template: dict[str, Any]) -> dict[str, Any]:
    """Return the Resources dict from the synthesised tier2 template."""
    return tier2_template.get("Resources", {})


@pytest.fixture(scope="module")
def tier2_template_with_email() -> dict[str, Any]:
    """Synthesise vsa-test-tier2 with CVX_TIER2_ALARM_EMAIL set.

    Used only by the email-subscription tests so most tests stay on the
    cheaper module-scoped fixture above.
    """
    _skip_if_no_tooling()
    proc = _synth(_TIER2_STACK, _base_env(CVX_TIER2_ALARM_EMAIL=_TEST_EMAIL))
    if proc.returncode != 0:
        pytest.fail(
            f"cdk synth {_TIER2_STACK} with email set failed unexpectedly.\n"
            f"stderr:\n{proc.stderr[-3000:]}"
        )
    return json.loads(proc.stdout)

# ── Helper functions ─────────────────────────────────────────────────────────

def _resources_by_type(resources: dict[str, Any], cfn_type: str) -> list[dict[str, Any]]:
    """Return all resource dicts whose Type matches cfn_type."""
    return [r for r in resources.values() if r.get("Type") == cfn_type]


def _find_alarm(resources: dict[str, Any], alarm_name_fragment: str) -> dict[str, Any] | None:
    """Return the first CloudWatch Alarm whose AlarmName contains the fragment."""
    for r in resources.values():
        if r.get("Type") == "AWS::CloudWatch::Alarm":
            name = r.get("Properties", {}).get("AlarmName", "")
            if alarm_name_fragment in name:
                return r
    return None


def _find_topic(resources: dict[str, Any]) -> dict[str, Any] | None:
    """Return the first SNS Topic resource, or None."""
    topics = _resources_by_type(resources, "AWS::SNS::Topic")
    return topics[0] if topics else None


def _all_alarms(resources: dict[str, Any]) -> list[dict[str, Any]]:
    return _resources_by_type(resources, "AWS::CloudWatch::Alarm")


def _get_lambda_role_policy_stmts(resources: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the flattened list of IAM policy statements on the Lambda execution role."""
    stmts: list[dict[str, Any]] = []
    for r in resources.values():
        if r.get("Type") in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
            doc = r.get("Properties", {}).get("PolicyDocument", {})
            stmts.extend(doc.get("Statement", []))
    # Also check inline policies on IAM Roles
    for r in resources.values():
        if r.get("Type") == "AWS::IAM::Role":
            for policy in r.get("Properties", {}).get("Policies", []):
                doc = policy.get("PolicyDocument", {})
                stmts.extend(doc.get("Statement", []))
    return stmts


def _stmt_allows_action(stmt: dict[str, Any], action: str) -> bool:
    """Return True if the statement allows the given IAM action."""
    if stmt.get("Effect") != "Allow":
        return False
    actions = stmt.get("Action", [])
    if isinstance(actions, str):
        actions = [actions]
    return any(a == action or a == "*" for a in actions)


def _get_lambda_env(resources: dict[str, Any]) -> dict[str, str]:
    """Return the environment variables dict from the first Lambda::Function resource."""
    for r in resources.values():
        if r.get("Type") == "AWS::Lambda::Function":
            env_block = (
                r.get("Properties", {})
                 .get("Environment", {})
                 .get("Variables", {})
            )
            return {k: str(v) for k, v in env_block.items()}
    return {}


# ═══════════════════════════════════════════════════════════════════════════════
# Positive controls — these PASS against the CURRENT stack (before this spec)
# The suite is not always-red: if these fail, infrastructure regressed.
# ═══════════════════════════════════════════════════════════════════════════════

class TestPositiveControls:
    """Assertions that pass TODAY, before any Group 4 changes land.

    Included deliberately per the precedent in test_stage_defaults_guard.py §
    "RED PHASE STATUS": a suite with no passing test can always be dismissed as
    broken rather than as a real failure signal. These pin the existing
    infrastructure so a regression would also be visible here.
    """

    def test_run_failure_alarm_exists(self, tier2_resources: dict[str, Any]) -> None:
        """The run-failure alarm shipped on 2026-08-05 and must still be present."""
        alarm = _find_alarm(tier2_resources, "run-failure")
        assert alarm is not None, (
            "vsa-test-tier2-run-failure alarm not found in synthesised template. "
            "This alarm shipped on 2026-08-05 — if it is gone something regressed."
        )

    def test_run_duration_alarm_exists(self, tier2_resources: dict[str, Any]) -> None:
        """The run-duration alarm shipped on 2026-08-05 and must still be present."""
        alarm = _find_alarm(tier2_resources, "run-duration")
        assert alarm is not None, (
            "vsa-test-tier2-run-duration alarm not found in synthesised template. "
            "This alarm shipped on 2026-08-05 — if it is gone something regressed."
        )

    def test_health_findings_table_exists(self, tier2_resources: dict[str, Any]) -> None:
        """The DynamoDB artifact table shipped on 2026-08-05."""
        tables = _resources_by_type(tier2_resources, "AWS::DynamoDB::Table")
        table_names = [
            t.get("Properties", {}).get("TableName", "") for t in tables
        ]
        assert any("health-findings" in n for n in table_names), (
            f"No DynamoDB table with 'health-findings' in its name found. "
            f"Found tables: {table_names}"
        )

    def test_lambda_function_exists(self, tier2_resources: dict[str, Any]) -> None:
        """The Tier 2 Lambda function shipped on 2026-08-05."""
        fns = _resources_by_type(tier2_resources, "AWS::Lambda::Function")
        assert len(fns) >= 1, (
            "No Lambda function found in the synthesised tier2 template. "
            "The health-agent function is the core of this stack."
        )

    def test_max_rows_per_query_is_50000(self, tier2_resources: dict[str, Any]) -> None:
        """TIER2_MAX_ROWS_PER_QUERY must remain 50,000 — spec § D4 states why it must not be raised."""
        env = _get_lambda_env(tier2_resources)
        assert env.get("TIER2_MAX_ROWS_PER_QUERY") == "50000", (
            f"TIER2_MAX_ROWS_PER_QUERY changed from 50000. "
            f"Spec § D4: raising this pushes more rows into a paginated reader inside a 512 MB Lambda "
            f"without a memory measurement at the new ceiling. Do not change this value without "
            f"measuring memory at the new batch size. Got: {env.get('TIER2_MAX_ROWS_PER_QUERY')!r}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# SNS Topic — must exist with KMS encryption and enforceSSL
# ═══════════════════════════════════════════════════════════════════════════════

class TestSnsOperatorTopic:
    """The operator-alert SNS topic per spec § D6.

    RED PHASE: No SNS topic exists in the current tier2 stack. All tests fail.
    After Group 4 lands, the topic exists with KmsMasterKeyId set and an SSL
    enforce policy.
    """

    def test_sns_topic_exists(self, tier2_resources: dict[str, Any]) -> None:
        """At least one AWS::SNS::Topic must be present in the tier2 stack."""
        topic = _find_topic(tier2_resources)
        assert topic is not None, (
            "No AWS::SNS::Topic found in synthesised vsa-test-tier2 template. "
            "Spec § D6 requires an operator-alert topic `vsa-{stage}-tier2-operator-alerts`. "
            "This is a red-phase failure — the topic does not exist yet."
        )

    def test_sns_topic_has_kms_master_key_id(self, tier2_resources: dict[str, Any]) -> None:
        """The SNS topic must be encrypted with the CVX CMK (KmsMasterKeyId set).

        Spec § D6: 'SNS topic vsa-{stage}-tier2-operator-alerts, KMS-encrypted with the
        existing CVX CMK, enforceSSL'.

        RED PHASE: topic does not exist yet, so this fails at the topic-existence check.
        """
        topic = _find_topic(tier2_resources)
        assert topic is not None, (
            "No AWS::SNS::Topic found — cannot check KmsMasterKeyId."
        )
        props = topic.get("Properties", {})
        kms_key = props.get("KmsMasterKeyId")
        assert kms_key is not None and kms_key != "", (
            f"SNS topic has no KmsMasterKeyId. Spec § D6 requires the topic to be "
            f"encrypted with the existing CVX CMK. Topic properties: {json.dumps(props, indent=2)}"
        )

    def test_sns_topic_has_ssl_enforce_policy(self, tier2_resources: dict[str, Any]) -> None:
        """enforceSSL: true must produce an AWS::SNS::TopicPolicy denying non-HTTPS publishes.

        The CDK `enforceSSL: true` prop on sns.Topic adds a resource policy with a
        deny statement on sns:Publish when aws:SecureTransport == false.

        RED PHASE: topic does not exist yet.
        """
        topic_policies = _resources_by_type(tier2_resources, "AWS::SNS::TopicPolicy")
        assert len(topic_policies) >= 1, (
            "No AWS::SNS::TopicPolicy found. `enforceSSL: true` on the SNS topic must "
            "produce a topic policy with a deny on non-HTTPS publishes (spec § D6)."
        )
        # At least one policy must have a Deny statement with aws:SecureTransport condition.
        found_ssl_deny = False
        for policy in topic_policies:
            for stmt in policy.get("Properties", {}).get("PolicyDocument", {}).get("Statement", []):
                if stmt.get("Effect") == "Deny":
                    cond = stmt.get("Condition", {})
                    if "Bool" in cond and "aws:SecureTransport" in str(cond):
                        found_ssl_deny = True
        assert found_ssl_deny, (
            "No Deny statement with aws:SecureTransport condition found in any "
            "AWS::SNS::TopicPolicy. CDK `enforceSSL: true` must produce this statement."
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Alarm actions — all four alarms must have non-empty AlarmActions
# ═══════════════════════════════════════════════════════════════════════════════

class TestAlarmActions:
    """All four alarms must have non-empty AlarmActions pointing at the SNS topic.

    RED PHASE: The current stack has AlarmActions=[] on all alarms. Every assertion
    in this class fails today. This is the core defect the spec fixes — a run that
    reads 6,000 rows, writes zero artifacts and exits 0 notifies nobody.
    """

    def test_all_four_alarms_have_non_empty_alarm_actions(
        self, tier2_resources: dict[str, Any]
    ) -> None:
        """Every alarm in the tier2 stack must have at least one AlarmAction.

        This is the single most important assertion: the predecessor spec's
        follow-on #14 documented that both alarms had empty AlarmActions and
        an unattended nightly failure notified nobody.
        """
        alarms = _all_alarms(tier2_resources)
        assert len(alarms) >= 4, (
            f"Expected at least 4 alarms in vsa-test-tier2, got {len(alarms)}. "
            f"The two existing alarms plus the two new alarms (no-vins, artifacts-stale) "
            f"must all be present."
        )
        for alarm in alarms:
            props = alarm.get("Properties", {})
            name  = props.get("AlarmName", "<unnamed>")
            actions = props.get("AlarmActions", [])
            assert len(actions) > 0, (
                f"Alarm {name!r} has empty AlarmActions. "
                f"Spec § D6 requires all four alarms to have AlarmActions pointing at "
                f"the operator SNS topic. This is the defect that made five days of "
                f"zero-artifact runs look healthy."
            )

    def test_run_failure_alarm_has_ok_action(self, tier2_resources: dict[str, Any]) -> None:
        """The run-failure alarm must also have OKActions so recovery is visible.

        Spec § D6: 'the run-failure and no-vins alarms also get OKActions so recovery
        is visible — a resolved-without-anyone-knowing incident is its own failure mode
        for an unattended agent.'

        RED PHASE: current alarm has OKActions=[].
        """
        alarm = _find_alarm(tier2_resources, "run-failure")
        assert alarm is not None, "run-failure alarm not found"
        ok_actions = alarm.get("Properties", {}).get("OKActions", [])
        assert len(ok_actions) > 0, (
            "vsa-test-tier2-run-failure alarm has empty OKActions. "
            "Spec § D6 requires OKActions on this alarm so recovery is visible."
        )

    def test_alarm_actions_reference_the_sns_topic(
        self, tier2_resources: dict[str, Any]
    ) -> None:
        """AlarmActions must reference the tier2 SNS topic, not an arbitrary ARN.

        This rules out the defect where AlarmActions is non-empty but points at a
        topic in a different stack or a hard-coded ARN that may not exist.
        """
        topic = _find_topic(tier2_resources)
        assert topic is not None, (
            "No SNS topic found — cannot verify AlarmActions reference it."
        )

        # The SNS topic ARN in CDK is a Ref or Fn::GetAtt to the logical ID of the topic.
        # Collect all logical IDs for SNS topics.
        topic_logical_ids = {
            lid for lid, r in tier2_resources.items()
            if r.get("Type") == "AWS::SNS::Topic"
        }

        alarms = _all_alarms(tier2_resources)
        assert len(alarms) >= 1, "No alarms found"
        for alarm in alarms:
            props   = alarm.get("Properties", {})
            name    = props.get("AlarmName", "<unnamed>")
            actions = props.get("AlarmActions", [])
            if not actions:
                continue  # Already caught by test_all_four_alarms_have_non_empty_alarm_actions
            # Each action should be a CFN reference to the SNS topic logical ID.
            for action in actions:
                action_str = json.dumps(action)
                topic_referenced = any(lid in action_str for lid in topic_logical_ids)
                assert topic_referenced, (
                    f"Alarm {name!r} has an AlarmAction that does not reference any "
                    f"SNS topic in this stack.\n"
                    f"Action: {action}\n"
                    f"Known topic logical IDs: {topic_logical_ids}"
                )


# ═══════════════════════════════════════════════════════════════════════════════
# New alarms — no-vins-selected and artifacts-stale with treatMissingData: BREACHING
# ═══════════════════════════════════════════════════════════════════════════════

class TestNewAlarms:
    """The two new alarms introduced by spec § D5.

    RED PHASE: Neither alarm exists in the current stack. All tests fail.

    The reason for BREACHING on a `Sum == 0` alarm is documented in spec § D5 and
    in `docs/tech.md` § "TreatMissingData — BREACHING semantics": a Lambda that never
    ran emits no data points at all, and BREACHING ensures CloudWatch treats missing
    periods as threshold-breaching so the alarm fires even with zero data points. This
    is the exact failure mode that hid five days of zero-artifact runs.
    """

    def test_no_vins_alarm_exists(self, tier2_resources: dict[str, Any]) -> None:
        """The no-vins-selected alarm must be present."""
        alarm = _find_alarm(tier2_resources, "no-vins")
        assert alarm is not None, (
            "No alarm with 'no-vins' in its name found in vsa-test-tier2 template. "
            "Spec § D5 requires `vsa-{stage}-tier2-no-vins-selected-{region}`. "
            "RED PHASE: alarm does not exist yet."
        )

    def test_no_vins_alarm_has_treat_missing_data_breaching(
        self, tier2_resources: dict[str, Any]
    ) -> None:
        """The no-vins alarm must use TreatMissingData: BREACHING.

        Spec § D5: 'Missing data breaches because a Lambda that never ran emits
        nothing at all, and that is the exact failure the empty AlarmActions hid.'

        RED PHASE: alarm does not exist yet.
        """
        alarm = _find_alarm(tier2_resources, "no-vins")
        assert alarm is not None, "no-vins alarm not found"
        tmd = alarm.get("Properties", {}).get("TreatMissingData")
        assert tmd == "breaching", (
            f"no-vins alarm TreatMissingData is {tmd!r}, expected 'breaching'. "
            f"Spec § D5: BREACHING ensures the alarm fires when the Lambda never ran "
            f"and emitted no data points — NOT_BREACHING would silently hide it."
        )

    def test_no_vins_alarm_has_ok_action(self, tier2_resources: dict[str, Any]) -> None:
        """The no-vins alarm must have OKActions (spec § D6 — recovery must be visible)."""
        alarm = _find_alarm(tier2_resources, "no-vins")
        assert alarm is not None, "no-vins alarm not found"
        ok_actions = alarm.get("Properties", {}).get("OKActions", [])
        assert len(ok_actions) > 0, (
            "no-vins alarm has empty OKActions. Spec § D6 requires OKActions on the "
            "no-vins alarm alongside the run-failure alarm."
        )

    def test_artifacts_stale_alarm_exists(self, tier2_resources: dict[str, Any]) -> None:
        """The artifacts-stale alarm must be present."""
        alarm = _find_alarm(tier2_resources, "artifacts-stale")
        assert alarm is not None, (
            "No alarm with 'artifacts-stale' in its name found in vsa-test-tier2 template. "
            "Spec § D5 requires `vsa-{stage}-tier2-artifacts-stale-{region}`. "
            "RED PHASE: alarm does not exist yet."
        )

    def test_artifacts_stale_alarm_has_treat_missing_data_breaching(
        self, tier2_resources: dict[str, Any]
    ) -> None:
        """The artifacts-stale alarm must use TreatMissingData: BREACHING.

        Spec § D5: 'Missing data breaches because a Lambda that never ran emits nothing.'
        The three-day window is deliberate (sustained silence on ArtifactsWritten means
        data has genuinely stopped changing or the pipeline is silently broken), but it
        still must breach on missing data for the same reason as the no-vins alarm.

        RED PHASE: alarm does not exist yet.
        """
        alarm = _find_alarm(tier2_resources, "artifacts-stale")
        assert alarm is not None, "artifacts-stale alarm not found"
        tmd = alarm.get("Properties", {}).get("TreatMissingData")
        assert tmd == "breaching", (
            f"artifacts-stale alarm TreatMissingData is {tmd!r}, expected 'breaching'. "
            f"Spec § D5: BREACHING is required so the alarm fires even when the Lambda "
            f"never ran. NOT_BREACHING would hide a dead Lambda for three days silently."
        )


# ═══════════════════════════════════════════════════════════════════════════════
# PutMetricData IAM grant — scoped by cloudwatch:namespace condition
# ═══════════════════════════════════════════════════════════════════════════════

class TestPutMetricDataGrant:
    """The Lambda execution role must be granted cloudwatch:PutMetricData, scoped to CVX/Tier2.

    Spec § D5: 'cloudwatch:PutMetricData is a new grant on the Lambda role, scoped by
    cloudwatch:namespace condition to CVX/Tier2.'

    Verified API facts from `docs/tech.md` § "CloudWatch PutMetricData — IAM action and
    namespace condition key": PutMetricData has no resource-level permission (Resource: "*"
    is required); the condition key `cloudwatch:namespace` scopes by namespace.

    RED PHASE: the current IAM policy has no PutMetricData statement. All tests fail.
    """

    def test_put_metric_data_action_is_granted(
        self, tier2_resources: dict[str, Any]
    ) -> None:
        """The Lambda execution role must allow cloudwatch:PutMetricData."""
        stmts = _get_lambda_role_policy_stmts(tier2_resources)
        matching = [
            s for s in stmts if _stmt_allows_action(s, "cloudwatch:PutMetricData")
        ]
        assert len(matching) >= 1, (
            "No IAM policy statement allowing cloudwatch:PutMetricData found in the "
            "tier2 stack. Spec § D5 requires this grant on the Lambda execution role "
            "so the observability module can emit VinsSelected/ArtifactsWritten/DuplicatesSkipped "
            "metrics. RED PHASE: grant does not exist yet."
        )

    def test_put_metric_data_scoped_by_namespace_condition(
        self, tier2_resources: dict[str, Any]
    ) -> None:
        """The PutMetricData grant must be scoped to the CVX/Tier2 namespace.

        Spec § D5: 'scoped by cloudwatch:namespace condition to CVX/Tier2'.
        Resource is always '*' for PutMetricData (the service does not support
        resource-level permissions), so the condition key is the only scope mechanism.

        RED PHASE: no PutMetricData grant exists yet.
        """
        stmts = _get_lambda_role_policy_stmts(tier2_resources)
        matching = [
            s for s in stmts if _stmt_allows_action(s, "cloudwatch:PutMetricData")
        ]
        assert len(matching) >= 1, (
            "No PutMetricData statement found — cannot check namespace condition."
        )
        found_namespace_condition = False
        for stmt in matching:
            cond = stmt.get("Condition", {})
            # Condition shape: {"StringEquals": {"cloudwatch:namespace": "CVX/Tier2"}}
            for _op, kv in cond.items():
                if isinstance(kv, dict):
                    for key, val in kv.items():
                        if key == "cloudwatch:namespace" and val == "CVX/Tier2":
                            found_namespace_condition = True

        assert found_namespace_condition, (
            "The PutMetricData grant exists but lacks a cloudwatch:namespace condition "
            "scoped to 'CVX/Tier2'. Without this condition the Lambda could emit metrics "
            "into any namespace. Required condition:\n"
            '  {"StringEquals": {"cloudwatch:namespace": "CVX/Tier2"}}\n'
            "Verified in docs/tech.md § 'CloudWatch PutMetricData — IAM action and "
            "namespace condition key'."
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Per-component batch limits — spec § D4 (amended)
# ═══════════════════════════════════════════════════════════════════════════════

class TestPerComponentBatchLimits:
    """The three per-component VIN caps must be distinct values.

    Spec § D4 (amended 2026-08-10): A single global `TIER2_MAX_VINS_PER_RUN` set by
    the worst component and applied to all three is the specific defect § D4 rejects.
    Tires' 240 rows/VIN would cap brakes at 156 VINs/night when brakes could process
    ~2,000 within the same row ceiling.

    Measured values:
      tires   → 156  (binding constraint: rows — 156 × 240 = 37,440 < 50,000 with 25% headroom)
      battery → 375  (binding constraint: rows — 375 × 100 = 37,500 < 50,000 with 25% headroom)
      brakes  → 2000 (binding constraint: write-path wall-clock, not rows — ~2,140 rows total)

    RED PHASE: the current stack has TIER2_MAX_VINS_PER_RUN='25' as a single shared
    variable. Every assertion in this class fails (wrong values, wrong variable names).
    The test that a single shared value MUST fail is the core assertion of § D4.
    """

    def test_tires_batch_limit_is_156(self, tier2_resources: dict[str, Any]) -> None:
        """TIER2_MAX_VINS_TIRES must be '156' in the Lambda environment.

        Binding constraint: rows (156 × 240 rows/VIN = 37,440 ≤ 50,000 with 25.1% headroom).
        Measured 2026-08-10 per spec § D4.

        RED PHASE: variable does not exist yet; current value is the stale '25' shared variable.
        """
        env = _get_lambda_env(tier2_resources)
        val = env.get("TIER2_MAX_VINS_TIRES")
        assert val == "156", (
            f"TIER2_MAX_VINS_TIRES is {val!r}, expected '156'. "
            f"Spec § D4: tires has 240 rows/VIN; 156 × 240 = 37,440 rows (25.1% headroom under 50k). "
            f"If the variable is absent, the runner falls back to a global default — "
            f"that fallback path is the defect this test exists to prevent."
        )

    def test_battery_batch_limit_is_375(self, tier2_resources: dict[str, Any]) -> None:
        """TIER2_MAX_VINS_BATTERY must be '375' in the Lambda environment.

        Binding constraint: rows (375 × 100 rows/VIN = 37,500 ≤ 50,000 with 25% headroom).
        Measured 2026-08-10 per spec § D4.

        RED PHASE: variable does not exist yet.
        """
        env = _get_lambda_env(tier2_resources)
        val = env.get("TIER2_MAX_VINS_BATTERY")
        assert val == "375", (
            f"TIER2_MAX_VINS_BATTERY is {val!r}, expected '375'. "
            f"Spec § D4: battery has 100 rows/VIN; 375 × 100 = 37,500 rows (25% headroom under 50k). "
            f"Measured 2026-08-10."
        )

    def test_brakes_batch_limit_is_2000(self, tier2_resources: dict[str, Any]) -> None:
        """TIER2_MAX_VINS_BRAKES must be '2000' in the Lambda environment.

        Binding constraint: write-path wall-clock, NOT rows. Brakes has ~1.07 rows/VIN,
        so 2,000 VINs × 1.07 ≈ 2,140 rows — well under the 50k row ceiling. The binding
        factor is 2,000 sequential conditional PutItem + agent_fn invocations kept well
        inside the 300s working target. Measured 2026-08-10 per spec § D4.

        RED PHASE: variable does not exist yet.
        """
        env = _get_lambda_env(tier2_resources)
        val = env.get("TIER2_MAX_VINS_BRAKES")
        assert val == "2000", (
            f"TIER2_MAX_VINS_BRAKES is {val!r}, expected '2000'. "
            f"Spec § D4: brakes has ~1.07 rows/VIN; bound by write-path wall-clock, not rows. "
            f"2,000 VINs keeps the run inside 300s. Measured 2026-08-10."
        )

    def test_single_shared_vin_cap_does_not_apply_to_all_three(
        self, tier2_resources: dict[str, Any]
    ) -> None:
        """A single TIER2_MAX_VINS_PER_RUN applying to all three components must FAIL.

        This is the defect § D4 explicitly names and rejects. With a single shared cap
        sized for tires (156), brakes would be needlessly limited to 156 VINs/night when
        it could handle 2,000 — wasting 92% of the available capacity.

        Passes if the three per-component variables have DIFFERENT values (proving the
        cap is not a single number silently applied to all).

        RED PHASE: the current stack sets TIER2_MAX_VINS_PER_RUN='25' for all three,
        so all three would have the same value — this assertion fails.
        """
        env = _get_lambda_env(tier2_resources)
        tires   = env.get("TIER2_MAX_VINS_TIRES")
        battery = env.get("TIER2_MAX_VINS_BATTERY")
        brakes  = env.get("TIER2_MAX_VINS_BRAKES")

        # All three must exist.
        assert tires   is not None, "TIER2_MAX_VINS_TIRES not found in Lambda env"
        assert battery is not None, "TIER2_MAX_VINS_BATTERY not found in Lambda env"
        assert brakes  is not None, "TIER2_MAX_VINS_BRAKES not found in Lambda env"

        # They must not all be the same value (that would be the global-cap defect).
        values = {tires, battery, brakes}
        assert len(values) > 1, (
            f"All three per-component VIN caps are the same value: {tires!r}. "
            f"A single shared value applied to all three components is the defect "
            f"spec § D4 exists to prevent: tires needs 156, battery 375, brakes 2,000. "
            f"Using the same number for all three either wastes 92% of brakes capacity "
            f"(if sized for tires) or risks exceeding the row ceiling for tires "
            f"(if sized for brakes)."
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Email subscription — absent by default, present when env var is set
# ═══════════════════════════════════════════════════════════════════════════════

class TestEmailSubscription:
    """Optional email subscription behaviour per spec § D6.

    Spec § D6: 'Absence does NOT throw at synth ... synth emits a warning ...
    topic created with no subscriber.'

    No default: absent → warn + no subscription resource.
    Present (CVX_TIER2_ALARM_EMAIL set) → exactly one AWS::SNS::Subscription.

    RED PHASE: both tests fail because the SNS topic does not exist yet, so neither
    the absent-subscription nor the present-subscription path is exercisable.
    """

    def test_no_subscription_when_email_absent(
        self, tier2_resources: dict[str, Any]
    ) -> None:
        """When CVX_TIER2_ALARM_EMAIL is not set, no AWS::SNS::Subscription must be present.

        Spec § D6: 'Absence does not throw at synth... the missing value's consequence
        is a topic with no subscriber — worse than a subscribed topic, better than today's
        no topic at all, and strictly visible: synth emits a warning.'

        RED PHASE: SNS topic does not exist yet; subscription test is moot but still
        fails because the resource type check is correct.
        """
        # No subscription resources expected when email is absent.
        subscriptions = _resources_by_type(tier2_resources, "AWS::SNS::Subscription")
        # Filter to email subscriptions only (the stack might have other subscription types
        # in theory, though none are expected in the current tier2 stack).
        email_subs = [
            s for s in subscriptions
            if s.get("Properties", {}).get("Protocol") == "email"
        ]
        assert len(email_subs) == 0, (
            f"Found {len(email_subs)} email subscription(s) in the template even though "
            f"CVX_TIER2_ALARM_EMAIL was not set. Spec § D6: absence must produce no "
            f"subscription (with a synth-time warning), not a subscription with an empty "
            f"or placeholder endpoint."
        )

    def test_exactly_one_subscription_when_email_present(
        self, tier2_template_with_email: dict[str, Any]
    ) -> None:
        """When CVX_TIER2_ALARM_EMAIL is set, exactly one email subscription must appear.

        The subscription endpoint must contain the test email value (proving the env var
        reached the template, not a hardcoded address).

        RED PHASE: SNS topic does not exist yet; this test will fail at the topic check.
        """
        resources = tier2_template_with_email.get("Resources", {})
        subscriptions = _resources_by_type(resources, "AWS::SNS::Subscription")
        email_subs = [
            s for s in subscriptions
            if s.get("Properties", {}).get("Protocol") == "email"
        ]
        assert len(email_subs) == 1, (
            f"Expected exactly 1 email subscription when CVX_TIER2_ALARM_EMAIL is set, "
            f"got {len(email_subs)}. Spec § D6: one operator email subscription."
        )
        endpoint = email_subs[0].get("Properties", {}).get("Endpoint", "")
        assert _TEST_EMAIL in str(endpoint), (
            f"Email subscription endpoint does not contain the expected address "
            f"{_TEST_EMAIL!r}. Endpoint: {endpoint!r}. The env var must reach the "
            f"template, not be replaced by a hardcoded or placeholder value."
        )

    def test_synth_succeeds_when_email_absent(self) -> None:
        """Synth must not throw when CVX_TIER2_ALARM_EMAIL is absent.

        Spec § D6: absence 'does not throw at synth' — this departs deliberately from
        the fail-closed default of `2026-08-05-cvx-stage-defaults-parameterization` for
        the specific reason stated: a topic with no subscriber is still better than today's
        no topic at all, and blocking a staging deploy on an unseeded email would trade
        a real improvement for a procedural one.

        This test is satisfied by the fact that `tier2_template` was synthesised without
        CVX_TIER2_ALARM_EMAIL and succeeded (the module fixture would have called
        pytest.fail otherwise). Kept as an explicit test so the contract is visible.
        """
        _skip_if_no_tooling()
        # Synth without the email env var; expect exit 0.
        proc = _synth(_TIER2_STACK, _base_env())
        assert proc.returncode == 0, (
            "cdk synth failed when CVX_TIER2_ALARM_EMAIL was absent. "
            "Spec § D6 explicitly states that absence must warn, not throw — "
            "blocking a staging deploy on an unseeded email departs from the spec's intent. "
            f"stderr:\n{proc.stderr[-2000:]}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# CDK / runner default agreement — spec Fix Group 3a ("Pin the CDK/runner default agreement")
# ═══════════════════════════════════════════════════════════════════════════════

class TestCDKRunnerDefaultAgreement:
    """Assert that the synthesised template's per-component VIN env vars agree with
    the defaults baked into the runner module.

    WHY THIS EXISTS
    ---------------
    ``agents/tier2/runner.py`` reads the three per-component batch limits at startup:

        _MAX_VINS_TIRES   = int(os.environ.get("TIER2_MAX_VINS_TIRES",   "156"))
        _MAX_VINS_BATTERY = int(os.environ.get("TIER2_MAX_VINS_BATTERY", "375"))
        _MAX_VINS_BRAKES  = int(os.environ.get("TIER2_MAX_VINS_BRAKES",  "2000"))

    ``infrastructure/lib/vsa-tier2-stack.ts`` sets the same three values in the Lambda
    environment block.  Nothing in the existing tests detects the two sides drifting
    apart — a one-sided edit (e.g. updating the CDK stack without updating the runner
    default) would pass every other test in this file and in ``test_runner.py``.

    This class closes that gap by importing the runner's defaults directly and comparing
    them to the synthesised template.  The constants are imported, **not re-typed as
    literals** — a re-typed literal would reproduce the duplication this task exists to
    detect and would pass even if both sides drifted together away from the correct value.

    PASSING STATE
    -------------
    All three env vars landed in the stack during Group 3 (``vsa-tier2-stack.ts`` was
    updated with the correct values).  This test therefore PASSES against the current
    stack.  It becomes a regression guard: the test fails if either side is changed
    without updating the other.

    VERIFY COMMAND
    --------------
    ``python3 -m pytest scripts/tests/test_tier2_alarm_wiring_guard.py -q -k default``
    """

    def test_per_component_defaults_agree_with_runner_module(
        self, tier2_resources: dict[str, Any]
    ) -> None:
        """The three Lambda env-var values in the CDK template must equal the runner's defaults.

        Imports the constants from ``agents.tier2.runner`` — not re-typed literals — so
        that a one-sided change on either the CDK side or the runner side causes a failure.
        If the runner's default is updated (e.g. TIER2_MAX_VINS_TIRES default raised from
        156 to 200) without a matching CDK update, the template will still carry "156" and
        this assertion fails.  The inverse is equally true: a CDK edit without a runner
        update fails here.

        This is a POSITIVE-PHASE test: it passes against the current stack (both sides
        agree) and its value comes from detecting future one-sided drift, not from catching
        a pre-existing bug.
        """
        # Import defaults from the runner module rather than spelling them as literals.
        # A re-typed literal would silently pass even if both sides drifted to the same
        # wrong value — exactly the duplication this task exists to prevent.
        from agents.tier2.runner import (  # noqa: PLC0415  (local import intentional)
            _MAX_VINS_BATTERY,
            _MAX_VINS_BRAKES,
            _MAX_VINS_TIRES,
        )

        env = _get_lambda_env(tier2_resources)

        # Tires
        template_tires = env.get("TIER2_MAX_VINS_TIRES")
        assert template_tires == str(_MAX_VINS_TIRES), (
            f"CDK template TIER2_MAX_VINS_TIRES={template_tires!r} does not match "
            f"runner._MAX_VINS_TIRES={_MAX_VINS_TIRES!r}. "
            f"Update vsa-tier2-stack.ts to match the runner default (or vice-versa). "
            f"Spec Fix Group 3a: both sides must be kept in sync."
        )

        # Battery
        template_battery = env.get("TIER2_MAX_VINS_BATTERY")
        assert template_battery == str(_MAX_VINS_BATTERY), (
            f"CDK template TIER2_MAX_VINS_BATTERY={template_battery!r} does not match "
            f"runner._MAX_VINS_BATTERY={_MAX_VINS_BATTERY!r}. "
            f"Update vsa-tier2-stack.ts to match the runner default (or vice-versa). "
            f"Spec Fix Group 3a: both sides must be kept in sync."
        )

        # Brakes
        template_brakes = env.get("TIER2_MAX_VINS_BRAKES")
        assert template_brakes == str(_MAX_VINS_BRAKES), (
            f"CDK template TIER2_MAX_VINS_BRAKES={template_brakes!r} does not match "
            f"runner._MAX_VINS_BRAKES={_MAX_VINS_BRAKES!r}. "
            f"Update vsa-tier2-stack.ts to match the runner default (or vice-versa). "
            f"Spec Fix Group 3a: both sides must be kept in sync."
        )


# ═══════════════════════════════════════════════════════════════════════════════
# DynamoDB GetItem grant — Fix Group 4a Critical 2, plus the guard for Critical 1
# ═══════════════════════════════════════════════════════════════════════════════

class TestDynamoDBGetItemGrant:
    """Assert that the Lambda role holds dynamodb:GetItem on the health-findings table.

    WHY THIS EXISTS
    ---------------
    The Group 4 stack task required this test and the task was marked [x] without it.
    The grant itself is correctly present at ``vsa-tier2-stack.ts:236`` — this test
    pins it so a future IAM-hardening pass cannot silently remove it.

    Failure mode of a missing GetItem grant:
      1. The cursor read in ``selection.py._read_cursor_safe`` raises
         ``AccessDeniedException``.
      2. The fail-safe path swallows it and treats the cursor as ``None``.
      3. Every run silently restarts from the pool beginning.
      4. All unit tests still pass (they stub the DynamoDB client).
      5. The smoke test still reports ``completed``.
      6. The frozen-window defect this entire spec exists to remove is back.

    This is a higher-stakes missing test than usual: it guards against a class of
    defect (silent IAM regression) that is invisible to every other test in the suite.

    ASSERTION SCOPE
    ---------------
    (a) Some IAM policy statement allows ``dynamodb:GetItem``.
    (b) That statement's Resource resolves to the health-findings table ARN — not a
        wildcard, not a different table. A grant on ``*`` satisfies (a) but not (b),
        and would be a separate security concern.

    VERIFY
    ------
    ``python3 -m pytest scripts/tests/test_tier2_alarm_wiring_guard.py -q -k getitem``
    """

    def test_getitem_action_is_granted(
        self, tier2_resources: dict[str, Any]
    ) -> None:
        """The Lambda execution role must explicitly allow dynamodb:GetItem.

        If this grant is removed, ``selection.py._read_cursor_safe`` raises
        ``AccessDeniedException`` on every cursor read, the fail-safe treats the
        cursor as ``None``, and the window never advances — while every test stays
        green and the runner reports ``completed``.
        """
        stmts = _get_lambda_role_policy_stmts(tier2_resources)
        matching = [
            s for s in stmts if _stmt_allows_action(s, "dynamodb:GetItem")
        ]
        assert len(matching) >= 1, (
            "No IAM policy statement allowing dynamodb:GetItem found in the tier2 stack.\n"
            "The grant must be present at vsa-tier2-stack.ts (HealthFindingsTableReadWrite "
            "policy statement).\n"
            "\n"
            "Without GetItem, selection.py._read_cursor_safe raises AccessDeniedException,\n"
            "the fail-safe swallows it, and every run restarts from the pool beginning —\n"
            "the frozen-window defect this spec exists to remove, re-introduced by a routine\n"
            "IAM tightening that no unit test would flag.\n"
            "\n"
            "Add 'dynamodb:GetItem' to the actions list in the HealthFindingsTableReadWrite\n"
            "policy statement in vsa-tier2-stack.ts."
        )

    def test_getitem_is_scoped_to_health_findings_table(
        self, tier2_resources: dict[str, Any]
    ) -> None:
        """The GetItem grant must be scoped to the health-findings table ARN, not '*'.

        A GetItem grant on '*' would satisfy the functional requirement but represent
        an over-privilege — the cursor reader only needs to read from one table.
        This test ensures the grant references the health-findings table's ARN.

        The CDK resource reference is an ``Fn::GetAtt`` intrinsic pointing at the
        DynamoDB Table's logical ID, which resolves to the table ARN at deploy time.
        """
        stmts = _get_lambda_role_policy_stmts(tier2_resources)
        getitem_stmts = [
            s for s in stmts if _stmt_allows_action(s, "dynamodb:GetItem")
        ]
        assert len(getitem_stmts) >= 1, (
            "No GetItem statement found — cannot check resource scoping."
        )

        # Collect the logical IDs of all DynamoDB tables in the stack.
        table_logical_ids = {
            lid for lid, r in tier2_resources.items()
            if r.get("Type") == "AWS::DynamoDB::Table"
        }
        # Filter to those whose name contains "health-findings".
        health_findings_ids = {
            lid for lid in table_logical_ids
            if "health" in lid.lower() or any(
                "health-findings" in str(r.get("Properties", {}).get("TableName", ""))
                for r in [tier2_resources[lid]]
            )
        }
        assert len(health_findings_ids) >= 1, (
            "No DynamoDB table with 'health-findings' in its name found. "
            "Cannot verify that GetItem is scoped to the correct table."
        )

        # Verify that at least one GetItem statement references the health-findings table.
        found_scoped_grant = False
        for stmt in getitem_stmts:
            resource = stmt.get("Resource")
            resource_str = json.dumps(resource)
            for lid in health_findings_ids:
                if lid in resource_str:
                    found_scoped_grant = True
                    break
            if found_scoped_grant:
                break

        assert found_scoped_grant, (
            f"dynamodb:GetItem is granted but not scoped to the health-findings table ARN.\n"
            f"Health-findings table logical IDs: {health_findings_ids}\n"
            f"GetItem statements' resources:\n"
            + "\n".join(
                f"  {json.dumps(s.get('Resource'))}"
                for s in getitem_stmts
            )
            + "\n\n"
            "The grant must reference the table via Fn::GetAtt on its logical ID, "
            "not a wildcard or a different table's ARN.\n"
            "See vsa-tier2-stack.ts HealthFindingsTableReadWrite policy statement."
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Alarm/metric dimension invariant — Fix Group 4a Critical 1 guard
# ═══════════════════════════════════════════════════════════════════════════════

class TestAlarmDimensionInvariant:
    """Every alarm's Dimensions set must match what observability.py actually emits.

    WHY THIS EXISTS — Fix Group 4a Critical 1
    ------------------------------------------
    Before the concurrent rewrite (Fix Group 4a task 1), the two new alarms used
    ``dimensionsMap: { stage }`` (one dimension) while ``emit_metrics`` publishes
    ``{stage, component}`` (two dimensions). CloudWatch treats dimension sets as
    part of a metric's unique identity — a ``{stage}``-only alarm queries a
    completely different time series from ``{stage, component}`` data points and
    sees no data. With ``treatMissingData: BREACHING``, both alarms would have
    fired immediately and permanently on deploy.

    This class closes the detection gap so the same mismatch cannot slip back in.

    DERIVATION APPROACH — NOT A RE-TYPED LITERAL
    --------------------------------------------
    The expected dimension names are derived by calling ``emit_metrics`` with a
    capturing client stub and reading the names from the call record. This
    follows the same principle as Fix Group 3a's CDK/runner default-agreement
    test: a re-typed literal ``{"stage", "component"}`` would pass even if both
    sides drifted to the same wrong name together. Deriving from the emission code
    means either side changing alone fails the test.

    SCOPE
    -----
    The two existing alarms (run-failure, run-duration) use Lambda-metric
    dimensions (``{FunctionName}``), which are correct for their metric source and
    intentionally different from the custom-metric dimensions. This test is scoped
    to alarms that share the ``CVX/Tier2`` namespace with ``emit_metrics`` — i.e.,
    the per-component alarms only.

    VERIFY
    ------
    ``python3 -m pytest scripts/tests/test_tier2_alarm_wiring_guard.py -q -k dimension``
    """

    @staticmethod
    def _get_emitted_dimension_names() -> frozenset[str]:
        """Call emit_metrics with a stub and return the set of dimension names it publishes.

        This derives the expected dimension set from the actual emission code, not
        from re-typed literals. If ``observability.emit_metrics`` is changed to add,
        remove, or rename a dimension, this method automatically reflects the change,
        causing the alarm-side assertions to fail.

        The stub records the MetricData argument; we extract dimension names from the
        first datum (every datum shares the same dimensions list object).
        """
        from agents.tier2.observability import emit_metrics  # local import — module not loaded at import time

        class _CapturingClient:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []
            def put_metric_data(self, **kwargs: Any) -> None:
                self.calls.append(kwargs)

        stub = _CapturingClient()
        emit_metrics(
            stub, "test-stage", "test-component",
            vins_selected=1, artifacts_written=1, duplicates_skipped=0,
        )
        assert len(stub.calls) == 1, "emit_metrics did not call put_metric_data exactly once"
        first_datum = stub.calls[0]["MetricData"][0]
        return frozenset(d["Name"] for d in first_datum["Dimensions"])

    def test_per_component_alarms_have_stage_and_component_dimensions(
        self, tier2_resources: dict[str, Any]
    ) -> None:
        """Every per-component alarm's Dimensions set equals what emit_metrics publishes.

        Derives the expected dimension names from the emission code — not re-typed.
        If emit_metrics adds or renames a dimension without a matching alarm update,
        this test fails on the alarm side. If an alarm's dimensionsMap loses a
        dimension without a matching change to emit_metrics, it also fails.

        The two existing Lambda-metric alarms (run-failure, run-duration) use
        ``{FunctionName}`` dimensions from the Lambda service — those are correct
        and intentionally excluded from this check.
        """
        expected_dim_names = self._get_emitted_dimension_names()

        # Identify per-component alarms by namespace: they use the CVX/Tier2 custom
        # namespace, while the Lambda-metric alarms use AWS/Lambda implicitly.
        alarms = _all_alarms(tier2_resources)
        cvx_tier2_alarms = []
        for alarm in alarms:
            props = alarm.get("Properties", {})
            metric = props.get("Metrics")  # for Math expressions — not used here
            namespace = props.get("Namespace")
            if namespace == "CVX/Tier2":
                cvx_tier2_alarms.append(alarm)

        assert len(cvx_tier2_alarms) >= 6, (
            f"Expected at least 6 per-component alarms in the CVX/Tier2 namespace "
            f"(3 no-vins + 3 artifacts-stale), found {len(cvx_tier2_alarms)}. "
            f"The per-component alarm rewrite (Fix Group 4a task 1) may not have landed."
        )

        mismatched: list[str] = []
        for alarm in cvx_tier2_alarms:
            props = alarm.get("Properties", {})
            name = props.get("AlarmName", "<unnamed>")
            dims = props.get("Dimensions", [])
            actual_names = frozenset(d["Name"] for d in dims)
            if actual_names != expected_dim_names:
                mismatched.append(
                    f"  alarm={name!r}: "
                    f"alarm_dims={sorted(actual_names)}, "
                    f"emitter_dims={sorted(expected_dim_names)}"
                )

        assert not mismatched, (
            "One or more per-component alarms have a Dimensions set that does not match\n"
            "what observability.emit_metrics publishes.\n\n"
            "A mismatch means the alarm queries a different (empty) CloudWatch time series\n"
            "from the one the metrics are written to. With treatMissingData: BREACHING,\n"
            "the alarm fires continuously from the moment it deploys.\n\n"
            "Mismatched alarms:\n"
            + "\n".join(mismatched)
            + "\n\n"
            "Fix: update the alarm's dimensionsMap in vsa-tier2-stack.ts to match the\n"
            "dimension names emitted by observability.emit_metrics. Both sides must use\n"
            "exactly the same set of dimension names.\n\n"
            "Expected dimension names (derived from emit_metrics call): "
            f"{sorted(expected_dim_names)}"
        )

    def test_dimension_invariant_fails_if_alarm_loses_component_dimension(
        self, tier2_resources: dict[str, Any]
    ) -> None:
        """Detector control: this test proves the invariant would catch a dimension drop.

        Constructs a synthetic alarm dict with only the ``stage`` dimension (the
        pre-fix bug shape from before Fix Group 4a task 1) and verifies that the
        dimension-comparison logic used in the main assertion would flag it.

        This is a DETECTOR CONTROL — it proves the detector fires on a known
        violation, so the main test cannot be dismissed as always-passing even
        when both sides are wrong in the same way.
        """
        expected_dim_names = self._get_emitted_dimension_names()

        # Simulate a stage-only alarm (the pre-fix defect shape).
        synthetic_alarm_dims = frozenset(["stage"])  # missing "component"

        assert synthetic_alarm_dims != expected_dim_names, (
            "Detector control failure: the synthetic {stage}-only dimension set equals\n"
            "the set emit_metrics publishes. This means emit_metrics itself only emits\n"
            "one dimension, which contradicts spec § D5.\n"
            f"emit_metrics dimension names: {sorted(expected_dim_names)}"
        )

    def test_eight_alarms_total_in_tier2_stack(
        self, tier2_resources: dict[str, Any]
    ) -> None:
        """The tier2 stack must contain exactly 8 alarms after Fix Group 4a.

        2 existing (run-failure, run-duration) + 3 no-vins + 3 artifacts-stale = 8.

        This count is the shape the spec specifies — 6 per-component alarms replacing
        the 2 stage-only alarms that were the pre-fix defect. If the count is 6 (4 + 2
        old), the per-component rewrite did not fully land.
        """
        alarms = _all_alarms(tier2_resources)
        assert len(alarms) == 8, (
            f"Expected exactly 8 alarms in vsa-test-tier2, found {len(alarms)}.\n"
            f"Expected breakdown: 2 existing (run-failure + run-duration) + "
            f"3 no-vins (battery/tires/brakes) + 3 artifacts-stale (battery/tires/brakes).\n"
            f"Alarm names found:\n"
            + "\n".join(
                f"  {a['Properties'].get('AlarmName', '<unnamed>')}"
                for a in alarms
            )
        )
