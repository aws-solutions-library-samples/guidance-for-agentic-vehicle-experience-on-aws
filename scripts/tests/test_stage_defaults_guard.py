"""Guard tests for spec `2026-08-05-cvx-stage-defaults-parameterization`.

WHY THIS EXISTS
---------------
`infrastructure/bin/vsa.ts` currently bakes per-stage ARNs, pool IDs and other
sensitive values as STAGE_DEFAULTS literals and reads them via `|| ''` fallbacks.
The spec replaces each baked default with one of three mechanisms:

  Tier A  — SSM `valueForStringParameter` (opaque strings, resolved at deploy time)
  Tier B  — env var or CDK `-c` context flag, **no baked default**, fail-closed throw
  Tier C  — keep baked (stage / region, non-sensitive, not matched by scanner)

This file tests the Tier B guard (`requireSynthValue`) and the `memoryKmsKeyArn`
truthiness branch in `vsa-core-stack.ts`, following the same pattern as
`test_guardrail_template_parity.py`: shell out to `npx cdk synth <stack> --json`,
`pytest.fail` on non-zero synth, parse `Resources`, assert on real properties.

RED-PHASE STATUS (pre-Group-2)
-------------------------------
Tests 1, 3, 4, 5b fail because `requireSynthValue` does not exist and the guard is
unwired — the current code uses `|| ''` fallbacks that silently succeed.

Test 2 fails because `requireSynthValue` is not yet present anywhere in
`infrastructure/bin/vsa.ts` — the source-code assertion catches the missing symbol.

Test 5a (real ARN → grant present) currently passes with the existing truthiness
branch — included deliberately so the test cannot be dismissed as always-green.
The suite still fails (tests 1, 3, 4, 5b all fail), which is the required red-phase
result.

FIXTURE CORRECTION (Group 2, 2026-08-05)
----------------------------------------
`_base_env()` originally set only `CVX_RUNTIME_ARN` and `CMS_KMS_KEY_ARN`, because at
the time it was written those were the only two fields with any guard at all. That is
not a valid configuration once all six Tier B fields are fail-closed: `cdk synth
<stack>` executes the whole of `bin/vsa.ts` and constructs every stack, so a synth
missing five Tier B fields throws on whichever one resolves first — and test 1 would
then assert on `memoryKmsKeyArn` and find `vpcId` in stderr.

So `_base_env()` now supplies a complete valid configuration, and each negative test
removes or poisons exactly the one field it is about. Every assertion gets sharper:
a failure can only be attributed to the field named in the test.

`TestEveryTierBFieldIsGuarded` then extends the coverage from one field to all six.
The recorded failure mode in this repository is a guard that cannot fire, so proving
one guard and inferring the rest would repeat the mistake this spec exists to fix.
See `decisions.md` Decision 1.

PLACEHOLDER SAFETY
------------------
This file is scanned by `scripts/lib/secret-scan.py` and it DOES ship — it is in
neither `scan_exclude` nor `.publish-exclude`. Every value below is therefore chosen
to be shape-valid but scanner-clean: the account segment is all-ones, and pool IDs use
an `eu-central-1_` prefix because the scanner's `cognito_user_pool_id` pattern is
anchored to the two US regions CVX actually deploys to.

Do NOT re-prefix these with one of those US regions to make them look more realistic.
A pool-shaped string under a matched region prefix is a critical finding regardless of
whether the suffix is real, which would put a scanner finding inside the very test
suite that guards against scanner findings.

That is not hypothetical: the first version of this paragraph spelled out the unsafe
prefix-and-suffix form as an example of what to avoid, and the scanner flagged the
warning itself. It is the same mechanism as the four exposures in
`~/.kiro/steering/public-mirror-publish.md` § "The exclusion-mechanism rule" — a
denylist written in terms of the value embeds the value. Describe the shape; never
write a specimen of it.

MODELLED ON
-----------
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
VSA_TS = INFRA_DIR / "bin" / "vsa.ts"

# ── Placeholder values used across tests ─────────────────────────────────────
# Use non-real ARN shapes that are still recognisable as ARNs to the CDK
# truthiness branch.  Account segment is all-ones to avoid matching the
# scanner's 12-digit-account-ID pattern.
_TEST_ACCOUNT = "111122223333"
_TEST_REGION = "us-west-2"

# A real-looking KMS ARN (not a live key).
_MEM_KMS_ARN = f"arn:aws:kms:{_TEST_REGION}:{_TEST_ACCOUNT}:key/aaaabbbb-test-mem-key"
_CMS_KMS_ARN = f"arn:aws:kms:{_TEST_REGION}:{_TEST_ACCOUNT}:key/ccccdddd-test-cms-key"
_RUNTIME_ARN = (
    f"arn:aws:bedrock-agentcore:{_TEST_REGION}:{_TEST_ACCOUNT}"
    f":runtime/vsa_supervisor_text_test-TESTRUNTIME"
)

# A CFN dynamic-reference string — the shape CDK emits for SSM tokens.
# This is *always truthy* as a Python/JS string, which is the bug the spec found.
_CFN_SSM_TOKEN = "{{resolve:ssm:/cvx/test/memory-kms-key-arn}}"

# ── Tier B placeholder values ────────────────────────────────────────────────
# Shape-valid so the synth-time shape checks in lib/config-value.ts accept them,
# and scanner-clean: the pool-ID pattern is anchored to `us-(east|west)-[12]_`,
# so an `eu-central-1_` prefix is safe to commit. See PLACEHOLDER SAFETY above.
_VPC_ID = "vpc-00000000000000000"
_REDIS_SG_ID = "sg-00000000000000000"
_VOICE_POOL_ID = "eu-central-1_TESTPOOL01"
_VOICE_CLIENT_ID = "testvoiceclientid00000"
_EXTRA_POOL_IDS = "eu-central-1_TESTPOOL02"

# The literal opt-out. `requireSynthValue(..., optional: true)` maps this to '',
# meaning "this stage deliberately has none" as distinct from "unset".
_OPT_OUT = "none"

# `extraCognitoUserPoolIds` requires this instead of the generic `none`.
_TRUST_OPT_OUT = "cvx-only-trust"

# Every Tier B field: the env var that supplies it, the CDK context key, the SSM
# parameter path named in the fail-closed message, a valid value, and whether the
# field accepts the `none` opt-out.
_TIER_B_FIELDS: list[dict[str, Any]] = [
    {
        "field": "memoryKmsKeyArn",
        "env": "VSA_MEMORY_KMS_KEY_ARN",
        "param": "/cvx/test/memory-kms-key-arn",
        "value": _MEM_KMS_ARN,
        "optional": True,
    },
    {
        "field": "vpcId",
        "env": "CMS_VPC_ID",
        "param": "/cvx/test/vpc-id",
        "value": _VPC_ID,
        "optional": False,
    },
    {
        "field": "redisSecurityGroupId",
        "env": "CMS_REDIS_SG_ID",
        "param": "/cvx/test/redis-security-group-id",
        "value": _REDIS_SG_ID,
        "optional": False,
    },
    {
        "field": "voiceAuthUserPoolId",
        "env": "VOICE_AUTH_USER_POOL_ID",
        "param": "/cvx/test/voice-auth-user-pool-id",
        "value": _VOICE_POOL_ID,
        "optional": True,
    },
    {
        "field": "voiceAuthUserPoolClientId",
        "env": "VOICE_AUTH_USER_POOL_CLIENT_ID",
        "param": "/cvx/test/voice-auth-user-pool-client-id",
        "value": _VOICE_CLIENT_ID,
        "optional": True,
    },
    {
        "field": "extraCognitoUserPoolIds",
        "env": "EXTRA_USER_POOL_IDS",
        "param": "/cvx/test/extra-cognito-user-pool-ids",
        "value": _EXTRA_POOL_IDS,
        "optional": True,
        # Distinct sentinel: this field gates a live authorization decision whose
        # absence is silent, so it must not share the word used for every other
        # optional field. See security review cycle 1, W1.
        "opt_out": _TRUST_OPT_OUT,
    },
]


def _skip_if_no_tooling() -> None:
    """Skip (not fail) when infrastructure/node_modules or npx is absent."""
    if not (INFRA_DIR / "node_modules").is_dir():
        pytest.skip("infrastructure/node_modules absent — run `make install` first")
    if shutil.which("npx") is None:
        pytest.skip("npx not available")


def _base_env(**overrides: str) -> dict[str, str]:
    """Return a COMPLETE valid synth environment for `VSA_STAGE=test`.

    Complete is the operative word. `cdk synth <stack>` runs the whole app, so every
    Tier B field must be present or synth throws on whichever one resolves first —
    which would make each negative test below assert against the wrong field. Tests
    that exercise absence remove exactly one key from this dict via `_without`.

    Pass `KEY=""` to force a field to look unset without removing it, or
    `KEY="   "` to exercise the whitespace path.
    """
    env = {
        **os.environ,
        "VSA_STAGE": "test",
        "AWS_REGION": _TEST_REGION,
        "CDK_DEFAULT_ACCOUNT": _TEST_ACCOUNT,
        # ── Tier A overrides ──────────────────────────────────────────────────
        # Supplied as literals so the tests never depend on an SSM parameter
        # existing. `resolveConfigValue` prefers the literal, so no CFN
        # `AWS::SSM::Parameter::Value<String>` parameter is emitted for these and
        # synth stays hermetic.
        "CVX_RUNTIME_ARN": _RUNTIME_ARN,
        "CMS_KMS_KEY_ARN": _CMS_KMS_ARN,
        # ── Tier B: all six, fail-closed ──────────────────────────────────────
        **{f["env"]: f["value"] for f in _TIER_B_FIELDS},
    }
    env.update(overrides)
    return env


def _without(*env_vars: str, **overrides: str) -> dict[str, str]:
    """`_base_env()` with the named env vars removed entirely.

    Removal rather than blanking, because the inherited `os.environ` may already
    define one of these on a developer machine — and a test that silently passes
    because the ambient environment supplied the value it was asserting was absent
    is exactly the vacuous check this suite exists to prevent.
    """
    env = _base_env(**overrides)
    for var in env_vars:
        env.pop(var, None)
    return env


def _synth(stack: str, env: dict[str, str], extra_args: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run `npx cdk synth <stack> --json` and return the completed process."""
    cmd = ["npx", "cdk", "synth", stack, "--json"] + (extra_args or [])
    # Static argv (constant cmd list), shell=False.
    # nosemgrep: dangerous-subprocess-use-audit
    return subprocess.run(
        cmd,
        cwd=INFRA_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )


_API_STACK = "vsa-test-api"


def _synth_api(env: dict[str, str], extra_args: list[str] | None = None) -> str:
    """Synthesize the API stack and return its template JSON.

    WHY `--ignore-errors`
    ---------------------
    The API stack calls `ec2.Vpc.fromLookup`, which requires a VPC that really
    exists — a placeholder ID fails with "Could not find any VPCs matching ...".
    Seeding the answer via `-c` does not help: CLI context values arrive as strings,
    so the `vpc-provider` key still reads as missing and the lookup is attempted
    anyway (verified 2026-08-05). Using a real VPC ID would put live infrastructure
    coordinates into a file that ships.

    `--ignore-errors` lets synthesis complete despite that one unresolvable lookup,
    which is sound here because the voice-auth trust does not depend on the VPC.

    WHY THAT IS NOT A HOLE
    ----------------------
    The flag makes the exit code uninformative, so these assertions are made against
    template *content*, never against `returncode`. And
    `test_valid_pair_adds_the_trust` is a positive control: it asserts the pool ID IS
    present for a valid pair. If synthesis ever degraded to the point of emitting
    nothing meaningful, that control fails — so the absence assertions below cannot
    pass vacuously, which is the failure mode this suite exists to prevent.
    """
    proc = _synth(_API_STACK, env, ["--ignore-errors"] + (extra_args or []))
    if not proc.stdout.strip().startswith("{"):
        pytest.fail(
            "cdk synth produced no template for the API stack even with "
            f"--ignore-errors.\nstderr:\n{proc.stderr[-2000:]}"
        )
    return proc.stdout


def _get_kms_decrypt_stmt(template_json: str) -> dict[str, Any] | None:
    """Return the `CvxAgentCoreMemoryKmsDecrypt` policy statement, or None."""
    resources = json.loads(template_json).get("Resources", {})
    for v in resources.values():
        for stmt in v.get("Properties", {}).get("PolicyDocument", {}).get("Statement", []):
            if stmt.get("Sid") == "CvxAgentCoreMemoryKmsDecrypt":
                return stmt
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Test 1 — absent Tier B field fails synth, error names field + env var + param path
# ═══════════════════════════════════════════════════════════════════════════════

class TestTierBAbsentFailsSynth:
    """
    When a Tier B field is absent from *both* the env and CDK context, `cdk synth`
    must exit non-zero and the error message must name:
      • the field name            (e.g. "memoryKmsKeyArn")
      • the env-var name          (e.g. "VSA_MEMORY_KMS_KEY_ARN")
      • the SSM parameter path    (e.g. "/cvx/test/memory-kms-key-arn")

    RED PHASE: the current code uses `|| ''` for every Tier B field — synth
    succeeds silently.  After Group 2 adds `requireSynthValue`, synth throws.
    """

    def test_missing_memory_kms_key_arn_fails_synth_naming_field_envvar_param(self) -> None:
        _skip_if_no_tooling()

        # Everything present EXCEPT memoryKmsKeyArn, so the throw can only be
        # attributable to that field.  No baked default exists for VSA_STAGE=test.
        env = _without("VSA_MEMORY_KMS_KEY_ARN")

        proc = _synth("vsa-test-core", env)

        # Must exit non-zero.
        assert proc.returncode != 0, (
            "cdk synth succeeded (exit 0) when memoryKmsKeyArn was absent — "
            "expected requireSynthValue to throw.  Current code uses `|| ''` fallback; "
            "Group 2 must replace it with a throw."
        )

        stderr = proc.stderr
        assert "memoryKmsKeyArn" in stderr, (
            f"Error message did not name the field 'memoryKmsKeyArn'.\nstderr:\n{stderr[-1000:]}"
        )
        assert "VSA_MEMORY_KMS_KEY_ARN" in stderr, (
            f"Error message did not name the env var 'VSA_MEMORY_KMS_KEY_ARN'.\nstderr:\n{stderr[-1000:]}"
        )
        # SSM parameter path per spec § Design naming convention: /cvx/{stage}/{kebab-field}
        assert "/cvx/test/memory-kms-key-arn" in stderr, (
            f"Error message did not name the SSM parameter path '/cvx/test/memory-kms-key-arn'.\n"
            f"stderr:\n{stderr[-1000:]}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Test 2 — env var value reaches the template (requireSynthValue must exist)
# ═══════════════════════════════════════════════════════════════════════════════

class TestEnvVarReachesTemplate:
    """
    When the env var for a Tier B field is set, `requireSynthValue` must accept
    it and the value must appear in the synthesized template.

    RED PHASE: the `requireSynthValue` symbol does not exist anywhere in
    `infrastructure/bin/vsa.ts` yet — the source-code assertion below fails.
    After Group 2 adds the helper and wires it, both assertions pass:
      (a) the symbol is present in the source file
      (b) the env-var value reaches the policy statement in the template
    """

    def test_require_synth_value_symbol_exists_in_vsa_ts(self) -> None:
        """requireSynthValue must be defined/called in bin/vsa.ts (source check)."""
        content = VSA_TS.read_text(encoding="utf-8")
        assert "requireSynthValue" in content, (
            "The symbol 'requireSynthValue' was not found in infrastructure/bin/vsa.ts.  "
            "Group 2 must define this helper and apply it to every Tier B field."
        )

    def test_env_var_value_appears_in_kms_policy_resource(self) -> None:
        """When VSA_MEMORY_KMS_KEY_ARN is set, the value must appear in the KMS grant.

        Also asserts `requireSynthValue` is present in bin/vsa.ts — both conditions
        must hold for the env-var path to be correctly guarded by Group 2.

        RED PHASE: `requireSynthValue` is absent → first assertion fails immediately.
        """
        _skip_if_no_tooling()

        # Prerequisite: requireSynthValue must exist before we can trust the env-var
        # path is properly guarded (not just falling through `|| ''`).
        content = VSA_TS.read_text(encoding="utf-8")
        assert "requireSynthValue" in content, (
            "The symbol 'requireSynthValue' was not found in infrastructure/bin/vsa.ts.  "
            "The env-var path cannot be considered correctly guarded until the helper "
            "exists and is wired to the field."
        )

        env = _base_env(VSA_MEMORY_KMS_KEY_ARN=_MEM_KMS_ARN)
        proc = _synth("vsa-test-core", env)
        if proc.returncode != 0:
            pytest.fail(
                f"cdk synth failed unexpectedly when all required env vars were set.\n"
                f"stderr:\n{proc.stderr[-2000:]}"
            )

        stmt = _get_kms_decrypt_stmt(proc.stdout)
        assert stmt is not None, (
            "CvxAgentCoreMemoryKmsDecrypt policy statement not found in synthesized template "
            "even though VSA_MEMORY_KMS_KEY_ARN was set."
        )
        resources = stmt.get("Resource", stmt.get("Resources"))
        assert _MEM_KMS_ARN in (resources if isinstance(resources, list) else [resources]), (
            f"Expected KMS resource {_MEM_KMS_ARN!r} in policy statement.\n"
            f"Actual statement: {json.dumps(stmt, indent=2)}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Test 3 — CDK context value reaches the template when env var is unset
# ═══════════════════════════════════════════════════════════════════════════════

class TestContextReachesTemplate:
    """
    When the env var is absent but a CDK `-c memoryKmsKeyArn=<value>` context flag
    is supplied, `requireSynthValue` must accept it and the value must reach the
    synthesized template.

    RED PHASE: the current resolution chain is
      `process.env.VSA_MEMORY_KMS_KEY_ARN || defaults.memoryKmsKeyArn || ''`
    CDK context is never consulted for this field — the context value is silently
    ignored and `memoryKmsKeyArn` falls to `''`.  Two failures result:
      (a) synth succeeds when it should succeed with the context value, but
          the context ARN does NOT appear in the template
      (b) no grant is emitted (empty string → falsy branch), so the assertion
          that the grant IS present fails

    After Group 2 uses `app.node.tryGetContext('memoryKmsKeyArn')` as the middle
    tier, the context value reaches the template and the grant is emitted.
    """

    def test_context_value_appears_in_kms_policy_resource(self) -> None:
        _skip_if_no_tooling()

        # VSA_MEMORY_KMS_KEY_ARN is deliberately absent; supply value via context.
        env = _without("VSA_MEMORY_KMS_KEY_ARN")
        proc = _synth("vsa-test-core", env, extra_args=[f"-c", f"memoryKmsKeyArn={_MEM_KMS_ARN}"])

        if proc.returncode != 0:
            pytest.fail(
                f"cdk synth failed when memoryKmsKeyArn was supplied via context.\n"
                f"After Group 2, -c memoryKmsKeyArn=<arn> must be accepted.\n"
                f"stderr:\n{proc.stderr[-2000:]}"
            )

        stmt = _get_kms_decrypt_stmt(proc.stdout)
        assert stmt is not None, (
            "CvxAgentCoreMemoryKmsDecrypt policy statement not found.  "
            "The context value was not accepted as a Tier B resolution path.\n"
            "Current code ignores CDK context for this field entirely."
        )
        resources = stmt.get("Resource", stmt.get("Resources"))
        assert _MEM_KMS_ARN in (resources if isinstance(resources, list) else [resources]), (
            f"Context ARN {_MEM_KMS_ARN!r} did not reach the policy statement resource.\n"
            f"Actual statement: {json.dumps(stmt, indent=2)}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Test 4 — whitespace-only env var is NOT treated as present
# ═══════════════════════════════════════════════════════════════════════════════

class TestWhitespaceEnvVarFails:
    """
    A whitespace-only value (e.g. `"   "`) must be treated identically to absent:
    synth exits non-zero with an error naming the field, env var, and parameter path.

    RED PHASE: the current code uses the JS `||` operator.  In JS `"   "` is truthy,
    so `process.env.VSA_MEMORY_KMS_KEY_ARN || ''` evaluates to `"   "`, the branch
    `if (props.memoryKmsKeyArn)` is taken, and a KMS grant is emitted with `"   "`
    as the resource ARN — a malformed ARN that will fail at deploy time with no
    indication at synth.  More critically: synth exits 0, so the assert-non-zero
    below fails.

    After Group 2, `requireSynthValue` must `.trim()` the value and treat it as
    absent if the result is empty, throwing identically to Test 1.
    """

    def test_whitespace_only_memory_kms_key_arn_fails_synth(self) -> None:
        _skip_if_no_tooling()

        env = _base_env(VSA_MEMORY_KMS_KEY_ARN="   ")
        proc = _synth("vsa-test-core", env)

        assert proc.returncode != 0, (
            "cdk synth succeeded (exit 0) when VSA_MEMORY_KMS_KEY_ARN was set to "
            "whitespace only ('   ').  requireSynthValue must treat whitespace-only "
            "values as absent and throw.  Current code uses JS `||` which treats "
            "non-empty strings as present regardless of content."
        )

        stderr = proc.stderr
        assert "memoryKmsKeyArn" in stderr, (
            f"Error for whitespace value did not name 'memoryKmsKeyArn'.\nstderr:\n{stderr[-1000:]}"
        )
        assert "VSA_MEMORY_KMS_KEY_ARN" in stderr, (
            f"Error for whitespace value did not name 'VSA_MEMORY_KMS_KEY_ARN'.\nstderr:\n{stderr[-1000:]}"
        )
        assert "/cvx/test/memory-kms-key-arn" in stderr, (
            f"Error for whitespace value did not name '/cvx/test/memory-kms-key-arn'.\n"
            f"stderr:\n{stderr[-1000:]}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Test 5 — memoryKmsKeyArn KMS grant: correct on both arms (THE critical test)
# ═══════════════════════════════════════════════════════════════════════════════

class TestMemoryKmsKeyArnTruthiness:
    """
    The `if (props.memoryKmsKeyArn)` branch in `vsa-core-stack.ts:556` is a
    truthiness check.  A CFN SSM token — the shape CDK emits for Tier A fields —
    is *always truthy*, so if `memoryKmsKeyArn` were ever made Tier A, the
    `CvxAgentCoreMemoryKmsDecrypt` policy statement would be unconditionally
    emitted, silently granting KMS decrypt to the AgentCore execution role even
    when no memory CMK exists.

    This test asserts *both arms* of the branch, ensuring neither is vacuous:

      Arm A (5a): real ARN supplied → grant IS present in template.
      Arm B (5b): token-like string supplied → grant must NOT be present.

    Why a CFN-resolve token string for Arm B?  `{{resolve:ssm:...}}` is the exact
    format CDK emits for `ssm.StringParameter.valueForStringParameter` tokens.  It
    is non-empty (truthy in JS), so the current `if (props.memoryKmsKeyArn)` branch
    is taken and the grant is emitted — that is the bug the spec found.

    RED PHASE:
      Arm A (5a) currently PASSES — the truthiness branch works for a real string.
                 Included deliberately so the suite cannot be dismissed as trivial.
      Arm B (5b) currently FAILS — the truthy resolve string causes the grant to be
                 emitted even though no real key exists.

    After Group 2 restructures the branch in `vsa-core-stack.ts` to gate on an
    explicit presence signal (e.g. checking for ARN prefix or a separate boolean
    prop), Arm B will pass: the token-like string is no longer treated as a real ARN.
    """

    def test_5a_real_arn_emits_kms_decrypt_grant(self) -> None:
        """Arm A — real ARN → CvxAgentCoreMemoryKmsDecrypt must be present.

        Also asserts `requireSynthValue` is present in bin/vsa.ts — without it
        the Tier B guard is unwired and the grant being emitted is coincidental
        (the `|| ''` fallback happened to pass the truthy check for a non-empty
        env var, which is not the same as the field being validated by the guard).

        RED PHASE: `requireSynthValue` absent → assertion fails before synth runs.
        """
        _skip_if_no_tooling()

        # Prerequisite: the guard must exist and be wired before we can rely on
        # the KMS branch being correctly controlled.
        content = VSA_TS.read_text(encoding="utf-8")
        assert "requireSynthValue" in content, (
            "The symbol 'requireSynthValue' was not found in infrastructure/bin/vsa.ts.  "
            "Arm A of Test 5 cannot be trusted until the guard is wired — the current "
            "`|| ''` fallback emits the grant for any truthy value without validation."
        )

        env = _base_env(VSA_MEMORY_KMS_KEY_ARN=_MEM_KMS_ARN)
        proc = _synth("vsa-test-core", env)
        if proc.returncode != 0:
            pytest.fail(
                f"Unexpected synth failure with real ARN set.\nstderr:\n{proc.stderr[-2000:]}"
            )

        stmt = _get_kms_decrypt_stmt(proc.stdout)
        assert stmt is not None, (
            "CvxAgentCoreMemoryKmsDecrypt statement absent when a real ARN was supplied.  "
            f"Expected grant for resource {_MEM_KMS_ARN!r}."
        )
        resources = stmt.get("Resource", stmt.get("Resources"))
        assert _MEM_KMS_ARN in (resources if isinstance(resources, list) else [resources]), (
            f"Grant present but resource is wrong.\n"
            f"Expected: {_MEM_KMS_ARN!r}\nActual statement: {json.dumps(stmt, indent=2)}"
        )

    def test_5b_cfn_token_like_string_does_not_emit_kms_grant(self) -> None:
        """Arm B — CFN-token-shaped string must NOT trigger the KMS grant.

        A `{{resolve:ssm:...}}` string is *truthy* in JS.  With the current
        `if (props.memoryKmsKeyArn)` check it causes the grant to be emitted with
        the literal token string as the resource — which is both a malformed ARN
        and an over-grant.  After Group 2 the branch gates on an explicit presence
        signal so the token is treated as absent.
        """
        _skip_if_no_tooling()

        env = _base_env(VSA_MEMORY_KMS_KEY_ARN=_CFN_SSM_TOKEN)
        proc = _synth("vsa-test-core", env)
        if proc.returncode != 0:
            pytest.fail(
                f"Unexpected synth failure when token-like string was supplied.\n"
                f"stderr:\n{proc.stderr[-2000:]}"
            )

        stmt = _get_kms_decrypt_stmt(proc.stdout)
        assert stmt is None, (
            "CvxAgentCoreMemoryKmsDecrypt grant was emitted for a CFN-token-shaped "
            "string — the truthiness bug.  The current `if (props.memoryKmsKeyArn)` "
            "branch treats any non-empty string as a real ARN.  Group 2 must "
            "restructure the branch to gate on an explicit presence signal.\n"
            f"Offending statement: {json.dumps(stmt, indent=2)}"
        )

    def test_5c_opt_out_sentinel_does_not_emit_kms_grant(self) -> None:
        """Arm C — the explicit `none` opt-out synthesises cleanly and grants nothing.

        This is the configuration a stage with no Foundation Memory uses. It must be
        distinguishable from "unset" (test 1, which throws) — that distinction is the
        whole reason the sentinel exists rather than a bare fail-closed throw.
        """
        _skip_if_no_tooling()

        proc = _synth("vsa-test-core", _base_env(VSA_MEMORY_KMS_KEY_ARN=_OPT_OUT))
        if proc.returncode != 0:
            pytest.fail(
                f"cdk synth failed with the documented `none` opt-out. A stage without a "
                f"Foundation Memory must still synthesise.\nstderr:\n{proc.stderr[-2000:]}"
            )

        assert _get_kms_decrypt_stmt(proc.stdout) is None, (
            "CvxAgentCoreMemoryKmsDecrypt was emitted for VSA_MEMORY_KMS_KEY_ARN=none. "
            "The opt-out must mean 'no CMK', not 'grant against the literal string none'."
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Test 6 — every Tier B field is guarded, not just the one under test above
# ═══════════════════════════════════════════════════════════════════════════════

class TestEveryTierBFieldIsGuarded:
    """
    Tests 1 and 4 prove the guard fires for `memoryKmsKeyArn`. That is one field out
    of six, and the failure this repository actually ships is a guard that cannot
    fire — proving one and inferring five would repeat it.

    Each case removes exactly one Tier B field from an otherwise complete and valid
    configuration, so a passing assertion cannot be explained by any other field
    being absent. The error must name all three things an operator needs: the field,
    the environment variable, and the SSM parameter path holding the canonical value.

    See `decisions.md` Decision 1.
    """

    @pytest.mark.parametrize(
        "spec",
        _TIER_B_FIELDS,
        ids=[f["field"] for f in _TIER_B_FIELDS],
    )
    def test_absent_tier_b_field_fails_synth_naming_field_envvar_and_param(
        self, spec: dict[str, Any]
    ) -> None:
        _skip_if_no_tooling()

        proc = _synth("vsa-test-core", _without(spec["env"]))

        assert proc.returncode != 0, (
            f"cdk synth succeeded (exit 0) with {spec['field']} absent. Every Tier B "
            f"field must be fail-closed — an unset value has to stop synth, not fall "
            f"through to an empty string. Guard missing or unwired for this field."
        )
        for expected in (spec["field"], spec["env"], spec["param"]):
            assert expected in proc.stderr, (
                f"The failure for absent {spec['field']} did not mention {expected!r}. "
                f"An operator reading only this message must be able to fix the deploy "
                f"without reading the source.\nstderr:\n{proc.stderr[-1200:]}"
            )

    @pytest.mark.parametrize(
        "spec",
        [f for f in _TIER_B_FIELDS if f["optional"]],
        ids=[f["field"] for f in _TIER_B_FIELDS if f["optional"]],
    )
    def test_opt_out_sentinel_is_accepted_only_where_documented(
        self, spec: dict[str, Any]
    ) -> None:
        """The documented opt-out sentinel must synthesise for legitimately-optional fields."""
        _skip_if_no_tooling()

        sentinel = spec.get("opt_out", _OPT_OUT)
        proc = _synth("vsa-test-core", _base_env(**{spec["env"]: sentinel}))
        if proc.returncode != 0:
            pytest.fail(
                f"{spec['field']} rejected its documented opt-out sentinel {sentinel!r}.\n"
                f"stderr:\n{proc.stderr[-2000:]}"
            )

    @pytest.mark.parametrize(
        "spec",
        [f for f in _TIER_B_FIELDS if not f["optional"]],
        ids=[f["field"] for f in _TIER_B_FIELDS if not f["optional"]],
    )
    def test_structurally_required_fields_reject_the_opt_out(
        self, spec: dict[str, Any]
    ) -> None:
        """`vpcId` and `redisSecurityGroupId` have no valid absent case.

        `Vpc.fromLookup` needs a concrete VPC and the live-state Lambda cannot reach
        Redis without the security group, so `none` must not quietly produce a
        deployment that fails later at the VPC attachment.
        """
        _skip_if_no_tooling()

        proc = _synth("vsa-test-core", _base_env(**{spec["env"]: _OPT_OUT}))

        assert proc.returncode != 0, (
            f"{spec['field']} accepted `none`, but it has no valid absent case — it is "
            f"structurally required at synth. Accepting the opt-out here defers a "
            f"certain failure to deploy time."
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Test 7 — the voice-auth trust pair: the third instance of the same trap
# ═══════════════════════════════════════════════════════════════════════════════

class TestVoiceAuthUserPoolTruthiness:
    """
    `vsa-api-stack.ts` used to read
    `props.voiceAuthUserPoolId && props.voiceAuthUserPoolClientId` before adding the
    shared CMS pool as a trusted voice-auth provider on each tenant's identity pool.
    Same shape as the `memoryKmsKeyArn` branch, and worse in consequence: a
    truthy-but-meaningless value registers a *trusted identity provider* that resolves
    to nothing.

    Found in Group 1 (`decisions.md` Correction 3). This field pair was missing from
    the spec's original 9-field tier table, and `voiceAuthUserPoolId` is itself
    scanner-critical (`bin/vsa.ts:88,146` — 2 of the 12 findings), so omitting it
    would have left the publish gate failing.

    All four assertions are on template content; see `_synth_api` for why exit codes
    are not used here and why that is not a hole.
    """

    def test_valid_pair_adds_the_trust(self) -> None:
        """POSITIVE CONTROL — a valid pair must reach the template.

        This is what stops the three absence assertions below from passing
        vacuously: if synthesis stopped emitting the trust for any reason, this test
        fails first and the others become meaningless rather than falsely green.
        """
        _skip_if_no_tooling()

        template = _synth_api(_base_env())
        assert _VOICE_POOL_ID in template, (
            "A valid voiceAuthUserPoolId / voiceAuthUserPoolClientId pair did not reach "
            "the synthesized template. Either the trust is no longer wired, or the "
            "shape validation is rejecting a value it should accept — check "
            "isResolvedUserPoolId / isResolvedUserPoolClientId in lib/config-value.ts. "
            "Until this passes, the absence assertions in this class prove nothing."
        )

    def test_cfn_token_shaped_pair_adds_no_trust(self) -> None:
        """The assertion that fails against a truthiness check.

        Both values are non-empty strings, so `a && b` is true and the old code added
        the trust. Gating on shape rejects them.
        """
        _skip_if_no_tooling()

        template = _synth_api(
            _base_env(
                VOICE_AUTH_USER_POOL_ID="{{resolve:ssm:/cvx/test/voice-auth-user-pool-id}}",
                VOICE_AUTH_USER_POOL_CLIENT_ID=(
                    "{{resolve:ssm:/cvx/test/voice-auth-user-pool-client-id}}"
                ),
            )
        )
        assert "{{resolve:ssm:" not in template, (
            "A CloudFormation dynamic-reference string reached the synthesized template. "
            "The voice-auth branch accepted a token-shaped value as a real pool ID — the "
            "truthiness bug. Gate on shape, not on truthiness."
        )

    def test_opt_out_pair_adds_no_trust(self) -> None:
        """`none`/`none` is the documented no-shared-CMS-pool configuration."""
        _skip_if_no_tooling()

        template = _synth_api(
            _base_env(
                VOICE_AUTH_USER_POOL_ID=_OPT_OUT,
                VOICE_AUTH_USER_POOL_CLIENT_ID=_OPT_OUT,
            )
        )
        assert _VOICE_POOL_ID not in template, (
            "The voice-auth pool ID reached the template despite the `none` opt-out."
        )
        assert f'"{_OPT_OUT}"' not in template, (
            "The literal opt-out sentinel `none` was emitted into the template as a "
            "value. It must resolve to absent, not be passed through as a pool ID."
        )

    def test_half_configured_pair_adds_no_partial_trust(self) -> None:
        """One half valid, the other opted out → no trust at all.

        A pool without its app client cannot complete the AgentCore credential
        exchange, so half a pair must add nothing. The old code fell back silently
        here; the stack now also warns, but the safe template is what this asserts.
        """
        _skip_if_no_tooling()

        template = _synth_api(_base_env(VOICE_AUTH_USER_POOL_CLIENT_ID=_OPT_OUT))
        assert _VOICE_POOL_ID not in template, (
            "The voice-auth pool ID was wired into the template even though its app "
            "client was absent. A half-configured pair must add no trust at all."
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Test 8 — W1: the Cognito trust opt-out must be deliberate, not the generic word
# ═══════════════════════════════════════════════════════════════════════════════

class TestCognitoTrustOptOutIsDeliberate:
    """
    `extraCognitoUserPoolIds` decides whether the CMS web UI's Cognito pool is trusted
    by the VSA API authorizer. Omitting it does not fail loudly — it returns 401 with no
    other symptom, which is the landmine caught live on 2026-07-13.

    Security review cycle 1 (W1) found the field accepting the generic `none` opt-out
    while a source comment told operators not to use it. A rule that lives only in a
    comment is not a control. These tests are that control.
    """

    def test_generic_none_is_rejected_naming_the_correct_sentinel(self) -> None:
        _skip_if_no_tooling()

        proc = _synth("vsa-test-core", _base_env(EXTRA_USER_POOL_IDS=_OPT_OUT))

        assert proc.returncode != 0, (
            "EXTRA_USER_POOL_IDS=none was accepted. That resolves to an empty pool list and "
            "silently drops the CMS-pool trust — the 2026-07-13 silent-401. This field must "
            "require its own explicit sentinel."
        )
        for expected in ("extraCognitoUserPoolIds", _TRUST_OPT_OUT, "401"):
            assert expected in proc.stderr, (
                f"The rejection message did not mention {expected!r}. An operator must be told "
                f"the correct sentinel and why the field matters.\nstderr:\n{proc.stderr[-1200:]}"
            )

    def test_explicit_sentinel_is_accepted(self) -> None:
        """The deliberate opt-out must still work — a stage may have no external pool."""
        _skip_if_no_tooling()

        proc = _synth("vsa-test-core", _base_env(EXTRA_USER_POOL_IDS=_TRUST_OPT_OUT))
        if proc.returncode != 0:
            pytest.fail(
                f"The explicit {_TRUST_OPT_OUT!r} opt-out failed to synthesise. A stage with no "
                f"external Cognito pool to trust is a valid configuration.\n"
                f"stderr:\n{proc.stderr[-2000:]}"
            )

    def test_delimiter_only_value_is_rejected(self) -> None:
        """`,` is non-blank and not the sentinel, but splits to zero pools.

        Closing the `none` route left this one open: a stray trailing comma or a typo
        produces an empty trust list and the same silent 401. Security review cycle 2
        flagged it; this is the executable form of the fix.
        """
        _skip_if_no_tooling()

        for bad in (",", ",,", " , "):
            proc = _synth("vsa-test-core", _base_env(EXTRA_USER_POOL_IDS=bad))
            assert proc.returncode != 0, (
                f"EXTRA_USER_POOL_IDS={bad!r} was accepted. It splits to zero pool IDs, which "
                f"silently drops the CMS-pool trust \u2014 the same outcome the `none` rejection "
                f"exists to prevent."
            )
            assert "extraCognitoUserPoolIds" in proc.stderr, (
                f"Rejection of {bad!r} did not name the field.\nstderr:\n{proc.stderr[-800:]}"
            )

    def test_malformed_pool_id_is_rejected_at_synth(self) -> None:
        """A non-pool-shaped entry must fail here, not at deploy.

        Each entry becomes a trusted ProviderARN on the authorizer. A malformed one
        otherwise surfaces as a CloudFormation error naming neither the field nor the
        env var.
        """
        _skip_if_no_tooling()

        proc = _synth("vsa-test-core", _base_env(EXTRA_USER_POOL_IDS="not-a-pool-id"))
        assert proc.returncode != 0, (
            "A malformed pool ID was accepted at synth and would have become a trusted "
            "ProviderARN."
        )
        assert "extraCognitoUserPoolIds" in proc.stderr, (
            f"Rejection did not name the field.\nstderr:\n{proc.stderr[-800:]}"
        )

    def test_explicit_sentinel_adds_no_authorizer_pool(self) -> None:
        """Opting out must produce no extra ProviderARN, and must not leak the sentinel."""
        _skip_if_no_tooling()

        opted_out = _synth_api(_base_env(EXTRA_USER_POOL_IDS=_TRUST_OPT_OUT))
        assert _TRUST_OPT_OUT not in opted_out, (
            "The opt-out sentinel reached the synthesized template. It must resolve to an "
            "empty list, not be passed through as a pool ID."
        )
        assert _EXTRA_POOL_IDS not in opted_out, (
            "An extra pool ID appears in the template despite the opt-out."
        )

        # Positive control: with a real value the pool DOES reach the template, so the
        # absence assertions above cannot pass for the wrong reason.
        configured = _synth_api(_base_env())
        assert _EXTRA_POOL_IDS in configured, (
            "The configured extra pool ID did not reach the template, so the opt-out "
            "assertions above prove nothing. Check the authorizer wiring first."
        )
