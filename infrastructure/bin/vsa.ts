#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { AwsSolutionsChecks } from 'cdk-nag';
import { applyCvxNagSuppressions } from '../lib/nag-suppressions';
import { VsaCoreStack } from '../lib/vsa-core-stack';
import { TriageClassifierStack } from '../lib/triage-classifier-stack';
import { VsaApiStack } from '../lib/vsa-api-stack';
import { VsaFoundationStack } from '../lib/vsa-foundation-stack';
import { VsaAnalyticsStack } from '../lib/vsa-analytics-stack';
import { AcquireStack } from '../lib/vsa-acquire-stack';
import { VsaTier2Stack } from '../lib/vsa-tier2-stack';
import { VsaRsaStack } from '../lib/vsa-rsa-stack';
import { ConfigValue, cvxParameterName, isResolvedUserPoolId, resolveCmsStage } from '../lib/config-value';

const app = new cdk.App();

// Region is pinned. Do not rely on CDK_DEFAULT_REGION or AWS_REGION shell
// env vars — they have silently caused cross-region deploys in the past.
// AWS_REGION is read explicitly here and only here, so the value flows
// into the env block and STAGE_DEFAULTS lookup below.
const stage = process.env.VSA_STAGE || 'prod';
const region = process.env.AWS_REGION || 'us-east-1';

const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region,
};

// ─── Cost-allocation tags ──────────────────────────────────────────────────
//
// Applied at App scope so every stack — and every taggable resource inside
// them — inherits without per-stack wiring.
//
// Why this exists: the 2026-09-02 CVX prod teardown could not attribute a
// single dollar to CVX, because the prod stacks carried **zero** tags. Cost
// Explorer's `Tag:Stage=prod` filter returned 30 rows of nulls, leaving only an
// unfiltered account total ($8,261.90/30d) dominated by ADP prod's 152M rows.
// The teardown's cost estimate was consequently derived by counting resources
// rather than measuring, and came out ~30x high ($10-30/mo estimated vs ~$1/mo
// actual). Staging is now the sole CVX environment, so it is the one that will
// be asked about next.
// See `~/.kiro/portfolio/initiatives/2026-09-02-env-collapse/cvx-teardown-close-out.md`
// § "Cost: the estimate was wrong by an order of magnitude", and backlog row
// `CVX staging tagging`.
//
// Key shape follows the only pre-existing convention in the portfolio — ADP's
// `adp:project` / `adp:stage` / `adp:owner` (verified live on
// `adp-prod-foundation-vehicle-knowledge-base`, 2026-09-02). CMS currently
// carries no stack tags at all, so there is nothing to align with there.
// Trade-off accepted: project-prefixed keys mean a portfolio-wide "cost by
// stage" query must activate one key per project (`adp:stage`, `cvx:stage`, ...)
// rather than a single shared `Stage`. Consistency with the existing convention
// was preferred over inventing a third scheme; revisit as a portfolio decision
// if cross-project grouping becomes the common question.
//
// IMPORTANT — tagging alone does NOT make Cost Explorer work. `cvx:stage` must
// also be activated as a **cost allocation tag** at the account level (Billing
// console, or `aws ce update-cost-allocation-tags-status`). As of 2026-09-02 the
// only Active user-defined key in this account is `awsApplication`. Activation
// is **not retroactive** — it applies only to usage recorded afterwards, so this
// change cannot answer "what did CVX cost last month" and never will.
cdk.Tags.of(app).add('cvx:project', 'cvx');
cdk.Tags.of(app).add('cvx:stage', stage);
cdk.Tags.of(app).add('cvx:owner', 'platform-team');

// ─── cdk-nag (AWS Solutions rule pack) ─────────────────────────────────
//
// Opt-in via `CDK_NAG=1`, not always-on. Two reasons:
//   1. Deploys stay unaffected. cdk-nag reports through CDK annotations, and an
//      Error-level annotation fails `cdk synth` — so an always-on Aspect would
//      turn any new unsuppressed finding into a broken deploy for every
//      operator, rather than a report for the team to triage.
//   2. It keeps the security scan a deliberate, reproducible step that CI runs
//      and attaches, which is what the Guidance security review asks for
//      (Security Matrix + static-analysis results).
//
// Run it:
//   CDK_NAG=1 npx cdk synth --all        (findings print as annotations)
//
// Findings must be either fixed or suppressed with a documented reason via
// NagSuppressions.addResourceSuppressions(...). Do NOT blanket-suppress a rule
// to silence it — an undocumented suppression is indistinguishable from an
// unnoticed gap when the report is reviewed. Several `Resource: '*'` grants in
// this app are legitimately unavoidable (lakeformation:GetDataAccess,
// cloudwatch:PutMetricData, logs/X-Ray, sts:GetWebIdentityToken) and should
// carry a suppression that states why, citing the AWS docs limitation.
if (process.env.CDK_NAG === '1') {
  cdk.Aspects.of(app).add(new AwsSolutionsChecks({ verbose: true }));
}

// ─── Per-stage configuration ───────────────────────────────────────────
//
// This file used to carry each stage's real KMS key ARNs, Cognito user pool
// IDs, VPC/SG IDs, Redis endpoint and AgentCore runtime ARN as literals in
// `STAGE_DEFAULTS`. It no longer does. `bin/vsa.ts` is the CDK app entrypoint,
// so it is a file this repository cannot exclude from publication — which made
// it the one place where baking live coordinates was both most convenient and
// least acceptable.
//
// Spec: `.kiro/specs/2026-08-05-cvx-stage-defaults-parameterization/spec.md`
// Per-field consumer analysis: `docs/tech.md` § "STAGE_DEFAULTS field tier analysis"
// Operator runbook: `docs/DEPLOYMENT.md` § "Per-stage parameters"
//
// Each value now sits in one of three tiers, chosen by *how it is consumed*
// rather than by what it is. There is no single mechanism that works for all of
// them, and the reason is worth understanding before adding a field:
//
//   Tier A — SSM parameter, resolved by CloudFormation at deploy time.
//            For values used as opaque strings: an IAM policy resource, a
//            Lambda environment variable. `cmsKmsKeyArn`, `redisEndpoint`,
//            `assistantChatRuntimeArn`.
//
//   Tier B — env var or `-c` context, no default, throws at synth if absent.
//            For values that must be *concrete before the template exists*:
//            `Vpc.fromLookup` rejects tokens outright; a comma-separated pool
//            list becomes N distinct constructs, so N must be known at synth;
//            and three IAM/trust decisions are made by synth-time conditionals.
//            `vpcId`, `redisSecurityGroupId`, `extraCognitoUserPoolIds`,
//            `memoryKmsKeyArn`, `voiceAuthUserPoolId`, `voiceAuthUserPoolClientId`.
//
//   Tier C — stage and region strings. Still baked below: not sensitive, and
//            not matched by any scanner pattern. `cmsStage`, `foundationRegion`.
//
// ADDING A FIELD: read its consumer first, then pick the tier. A field whose
// tier you cannot establish from the consuming code is Tier B — Tier B always
// works, and Tier A can fail silently. Getting this backwards is not a
// theoretical risk: `memoryKmsKeyArn` and the `voiceAuthUserPool*` pair were
// both initially classified Tier A, and both would have granted permissions
// that were never intended, because a CFN token is always truthy.

type StageDefaults = {
  /** CMS deployment stage. Drives `cms-${cmsStage}-storage-*` table names. */
  cmsStage: string;
  /**
   * Region of the Foundation stack (AgentCore Memory + CMK). Introduced
   * 2026-07-16 by spec `2026-07-16-cvx-prod-cutover` to colocate the prod
   * Memory with the prod runtime + KB (us-east-1) — the previous hard-coded
   * `us-west-2` broke that colocation for prod. Staging retains `us-west-2`
   * (Nova Sonic voice runtime colocation). The fall-through in
   * `foundationEnv.region` below preserves the pre-2026-07-16 default when
   * unset, so a NEW stage that omits this field silently pins to us-west-2 —
   * set it explicitly.
   */
  foundationRegion: string;
};

const STAGE_DEFAULTS: Record<string, Partial<StageDefaults>> = {
  prod: {
    cmsStage: 'prod',
    foundationRegion: 'us-east-1',
  },
  staging: {
    cmsStage: 'staging',
    foundationRegion: 'us-west-2',
  },
};

const defaults = STAGE_DEFAULTS[stage] || {};

// ─── Tier B resolution: env var → context → throw ──────────────────────
//
// The explicit opt-out. Three fields below are legitimately absent in some
// configurations — a stage without a Foundation Memory has no CMK, and a stage
// whose clients do not use the shared CMS pool has no extra voice-auth trust.
// Those cases are expressed by setting the value to `none`, which records the
// decision in the deploy environment where it can be audited.
//
// This is the distinction the old `|| ''` fallbacks could not make: "the
// operator decided this stage has none" and "the operator forgot" produced the
// same empty string, so a forgotten value silently degraded the deployment
// instead of stopping it.
const OPT_OUT = 'none';

/**
 * A field-specific opt-out sentinel, for the one field where `none` is too easy to
 * reach by habit.
 *
 * `extraCognitoUserPoolIds` controls whether the CMS web UI's Cognito pool is
 * trusted by the VSA API's authorizer. Its absence does not fail loudly — it
 * returns 401 with no other symptom, which is the 2026-07-13 landmine. A stage
 * with no external pool to trust is still a real configuration, so the opt-out
 * has to exist; it just must not be reachable by typing the same word an
 * operator uses for every other optional field.
 *
 * Security review cycle 1 (W1) caught the earlier version accepting plain `none`
 * here, with a comment below telling operators not to use it — a prose rule
 * against something the code permitted, which is the same defect shape as a
 * guard that cannot fire.
 */
const TRUST_OPT_OUT = 'cvx-only-trust';

/**
 * Resolve a Tier B value, or throw naming everything needed to fix it.
 *
 * Resolution order is env var, then `-c <field>=<value>` context, then throw.
 * A blank or whitespace-only value counts as absent at every step — JS `||`
 * treats `"   "` as present, which previously let a whitespace-only ARN reach
 * an IAM policy resource with no complaint at synth.
 *
 * @param optional when set, the named sentinel resolves to `''` — meaning
 *                 "deliberately absent". Absence still throws. Only pass this
 *                 for a field whose consumer genuinely handles absence; do not
 *                 use it to quiet a field you have not configured.
 */
function requireSynthValue(opts: {
  field: string;
  envVar: string;
  kebab: string;
  optional?: boolean;
  /** Overrides the sentinel that means "deliberately absent". Defaults to `none`. */
  optOut?: string;
}): string {
  const { field, envVar, kebab, optional = false, optOut = OPT_OUT } = opts;
  const parameterName = cvxParameterName(stage, kebab);

  const candidates = [process.env[envVar], app.node.tryGetContext(field)];
  const supplied = candidates.find(
    (v): v is string => typeof v === 'string' && v.trim().length > 0,
  );
  const value = supplied?.trim() ?? '';
  const lowered = value.toLowerCase();

  if (optional && lowered === optOut) {
    return '';
  }

  // A field with a custom sentinel must reject the generic one outright. Otherwise
  // `none` falls through as an ordinary value — and for a comma-separated pool list
  // that means a "pool" literally named `none`, which is worse than either
  // outcome the operator intended.
  if (lowered === OPT_OUT && optOut !== OPT_OUT) {
    throw new Error(
      [
        `${field} does not accept "${OPT_OUT}".`,
        '',
        `This field controls a live authorization decision, so opting out of it has to be`,
        `deliberate and unambiguous rather than the same word used for every other`,
        `optional field. If stage "${stage}" genuinely has no external Cognito pool to`,
        `trust, set:`,
        `  ${envVar}=${optOut}`,
        '',
        `Be sure that is what you want. Omitting this trust does not fail loudly — it`,
        `returns 401 to the CMS web UI on the /assistant/chat route with no other`,
        `symptom. That was caught live during the 2026-07-13 CDK-cleanup deploy.`,
        `The canonical value for this stage is at ${parameterName}.`,
      ].join('\n'),
    );
  }

  if (!optional && lowered === OPT_OUT) {
    // Rejected explicitly rather than passed through. `vpcId` and
    // `redisSecurityGroupId` have no valid absent case — `Vpc.fromLookup` needs a
    // concrete VPC and the live-state Lambda cannot reach Redis without the
    // security group. Letting `none` through would defer a certain failure to a
    // VPC-lookup error that names neither this field nor the env var.
    throw new Error(
      `${field} does not support the "${OPT_OUT}" opt-out — it is structurally ` +
        `required at synth time and has no valid absent case. Supply a real value ` +
        `via ${envVar} or -c ${field}=<value>. The canonical value for stage ` +
        `"${stage}" is at ${parameterName}.`,
    );
  }
  if (value) {
    return value;
  }

  throw new Error(
    [
      `${field} is required for stage "${stage}" and was not supplied.`,
      '',
      'Supply it one of two ways:',
      `  • export ${envVar}=<value>`,
      `  • pass -c ${field}=<value> to the cdk command`,
      '',
      `The canonical value for this stage is recorded in SSM at ${parameterName}:`,
      `  aws ssm get-parameter --name ${parameterName} --query Parameter.Value --output text`,
      ...(optional
        ? [
            '',
            `If stage "${stage}" genuinely has no ${field}, set ${envVar}=${optOut} to`,
            'record that as a deliberate choice rather than leaving it unset.',
          ]
        : []),
      '',
      'This value is deliberately not baked into bin/vsa.ts — see',
      '.kiro/specs/2026-08-05-cvx-stage-defaults-parameterization/spec.md.',
    ].join('\n'),
  );
}

/**
 * Describe a Tier A value for a stack to resolve.
 *
 * Returns a `ConfigValue`, not a string: `valueForStringParameter` needs a
 * `Stack` scope and there is none here, so the actual read happens inside each
 * consuming stack's constructor. See `lib/config-value.ts`.
 */
function ssmBacked(opts: { envVar: string; kebab: string; contextKey?: string }): ConfigValue {
  const candidates = [
    process.env[opts.envVar],
    opts.contextKey ? app.node.tryGetContext(opts.contextKey) : undefined,
  ];
  const literal = candidates.find(
    (v): v is string => typeof v === 'string' && v.trim().length > 0,
  );
  return { literal, parameterName: cvxParameterName(stage, opts.kebab) };
}

// ─── Tier C ────────────────────────────────────────────────────────────
// Fails closed when an ambient CMS_STAGE contradicts this stage's canonical
// default, instead of letting the environment silently outrank it. The old
// form was `process.env.CMS_STAGE || defaults.cmsStage || 'dev'`; see
// resolveCmsStage's comment block and
// issues/2026-09-21-cms-stage-env-var-overrides-stage-default/.
const cmsStage = resolveCmsStage({
  stage,
  envValue: process.env.CMS_STAGE,
  stageDefault: defaults.cmsStage,
});

// ─── Tier A ────────────────────────────────────────────────────────────
//
// No synth-time guard on any of these, deliberately. Each resolves to a CFN
// token, and a token is never empty — so an emptiness check would be a guard
// that cannot fail. Two such checks used to live here and in
// `vsa-core-stack.ts`; both were deleted rather than ported. Existence is
// enforced by CloudFormation, which resolves
// `AWS::SSM::Parameter::Value<String>` before provisioning and fails the stack
// operation when the parameter is missing. See `lib/config-value.ts` §
// "ON EXISTENCE CHECKING" for why a synth-time check is not available without
// making synth non-hermetic.

/** KMS key ARN used by CMS DynamoDB tables (SSE-KMS). */
const cmsKmsKeyArn = ssmBacked({
  envVar: 'CMS_KMS_KEY_ARN',
  kebab: 'cms-kms-key-arn',
});

/** Redis endpoint backing vehicle live state (from the CMS MSK/cache stack). */
const redisEndpoint = ssmBacked({
  envVar: 'REDIS_ENDPOINT',
  kebab: 'redis-endpoint',
});

/**
 * Base URL of the DMS REST API, consumed by `agents/executors/dms.py` (which reads
 * `VSA_DMS_API_ENDPOINT` at :67) when the approve route dispatches `target_system=dms`.
 *
 * DMS *does* export this as the CloudFormation export `dms-{stage}-api-url`, so
 * `cdk.Fn.importValue` would work. It is deliberately not used: a cross-stack import
 * makes this stack a deletion-lock on DMS's export, coupling two accelerators'
 * lifecycles for a value that changes only if DMS's REST API is replaced. There was no
 * existing CVX→DMS infrastructure reference to follow, and the precedent worth following
 * is `cmsKmsKeyArn` / `redisEndpoint` above — likewise values owned by ANOTHER project
 * and read through `/cvx/{stage}/*`.
 *
 * Seed it from DMS's own output rather than typing it; see the `dmsApiEndpoint` entry in
 * `scripts/seed-stage-parameters.sh`, which also records why `list-exports --output text`
 * is the wrong way to derive it (it paginates, and emits `None` per non-matching page).
 */
const dmsApiEndpoint = ssmBacked({
  envVar: 'VSA_DMS_API_ENDPOINT',
  kebab: 'dms-api-endpoint',
});

/**
 * The one Amazon Connect instance the P0 outbound callback may dial from (security
 * review Cycle 2/3 W2). `connect:StartOutboundVoiceContact` was granted on the
 * contacts of every instance in the account; it is now scoped to this one instance's
 * contacts. Owned by the Connect setup, not by this repo, so
 * it is read through `/cvx/{stage}/connect-instance-arn` like `dmsApiEndpoint` above.
 * A tenant whose config names a different instance gets AccessDenied on the callback,
 * which is best-effort and never blocks the chat.
 */
const connectInstanceArn = ssmBacked({
  envVar: 'CVX_CONNECT_INSTANCE_ARN',
  kebab: 'connect-instance-arn',
});

/**
 * Operator attestation that `custom:fleetIds` may be used for AVX fleet standing.
 * The value is `"true"` or `"false"`; anything but exactly `"true"` keeps fleet
 * standing denied (`lambdas/_lib/avx_scope.py`).
 *
 * Tier A, and deliberately so. It is only passed through to a Lambda env var, never
 * tested at synth. And an unseeded parameter FAILS the deploy instead of defaulting,
 * so trusting a claim is always a recorded decision (the SSM value), never an
 * omission. `docs/identity-and-standing-contract.md` § 5.1 states what the operator
 * attests by seeding `"true"`.
 *
 * The synth-time override is `CVX_AVX_FLEET_IDS_CLAIM_TRUSTED`, NOT the Lambda's own
 * `AVX_FLEET_IDS_CLAIM_TRUSTED`. Unit tests set the Lambda name freely, and an operator
 * shell that ran them must not carry a trust decision into a deploy.
 */
const avxFleetIdsClaimTrusted = ssmBacked({
  envVar: 'CVX_AVX_FLEET_IDS_CLAIM_TRUSTED',
  kebab: 'avx-fleet-ids-claim-trusted',
});

/**
 * Operator attestation that `custom:customerId` may be used for AVX owner standing.
 * The value is `"true"` or `"false"`; anything but exactly `"true"` keeps owner
 * standing denied (`lambdas/_lib/avx_scope.py`).
 *
 * Tier A. Threaded to the AVX findings Lambda ONLY (`vsa-{stage}-api-avx-findings`)
 * as `AVX_CUSTOMER_ID_CLAIM_TRUSTED` — NOT to api-avx-actions (no code path in the
 * actions Lambda reads this variable). An unseeded parameter FAILS the deploy instead
 * of defaulting.
 *
 * The synth-time override is `CVX_AVX_CUSTOMER_ID_CLAIM_TRUSTED`, NOT the Lambda's
 * own `AVX_CUSTOMER_ID_CLAIM_TRUSTED`. Unit tests set the Lambda name freely, and
 * an operator shell that ran them must not carry a trust decision into a deploy.
 */
const avxCustomerIdClaimTrusted = ssmBacked({
  envVar: 'CVX_AVX_CUSTOMER_ID_CLAIM_TRUSTED',
  kebab: 'avx-customer-id-claim-trusted',
});

/**
 * Operator attestation that `custom:vehicleId` may be used for AVX driver standing.
 * The value is `"true"` or `"false"`; anything but exactly `"true"` keeps driver
 * standing denied (`lambdas/_lib/avx_scope.py`).
 *
 * Tier A. Threaded to the AVX findings Lambda ONLY (`vsa-{stage}-api-avx-findings`)
 * as `AVX_VEHICLE_ID_CLAIM_TRUSTED` — NOT to api-avx-actions (no code path in the
 * actions Lambda reads this variable). An unseeded parameter FAILS the deploy instead
 * of defaulting.
 *
 * The synth-time override is `CVX_AVX_VEHICLE_ID_CLAIM_TRUSTED`, NOT the Lambda's
 * own `AVX_VEHICLE_ID_CLAIM_TRUSTED`. Unit tests set the Lambda name freely, and
 * an operator shell that ran them must not carry a trust decision into a deploy.
 */
const avxVehicleIdClaimTrusted = ssmBacked({
  envVar: 'CVX_AVX_VEHICLE_ID_CLAIM_TRUSTED',
  kebab: 'avx-vehicle-id-claim-trusted',
});

/**
 * Full ARN of the AgentCore text runtime the `/assistant/chat` route invokes.
 *
 * AgentCore Runtimes are provisioned outside CDK (via `agentcore deploy`), so
 * there is no CFN export to import from. The ARN's trailing suffix changes on
 * every redeploy, which means this parameter MUST be updated whenever the text
 * runtime is reminted:
 *
 *   aws ssm put-parameter --overwrite --type String \
 *     --name /cvx/<stage>/assistant-chat-runtime-arn --value <arn>
 *
 * Source of truth for the new value: the runtime block for the stage in
 * `.bedrock_agentcore.yaml`, field `bedrock_agentcore.agent_arn`.
 *
 * `CVX_RUNTIME_ARN` / `-c cvxRuntimeArn=<arn>` still override, which is the
 * path to use when temporarily pointing a deploy at a different runtime.
 */
const assistantChatRuntimeArn = ssmBacked({
  envVar: 'CVX_RUNTIME_ARN',
  contextKey: 'cvxRuntimeArn',
  kebab: 'assistant-chat-runtime-arn',
});

/**
 * Allowed CORS origin for the CVX REST API — scopes both API Gateway
 * `defaultCorsPreflightOptions.allowOrigins` and the 14 Lambda handlers'
 * `Access-Control-Allow-Origin` response header to a single origin per stage.
 *
 * The single-origin choice is deliberate: the only browser-cross-origin
 * caller of CVX APIs is the CMS UI SPA (served from one CloudFront
 * distribution per stage). The iOS client is a native app and does not send
 * an `Origin` header; Amazon Connect and AgentCore call server-side with
 * SigV4 and do not participate in CORS. So `allowed-origin` is the CMS UI
 * CloudFront domain per stage.
 *
 * Tier A `ConfigValue`: `CVX_ALLOWED_ORIGIN` env or `-c cvxAllowedOrigin`
 * override, else the `/cvx/{stage}/allowed-origin` SSM parameter. Set to
 * the exact CloudFront distribution URL (scheme + host, no trailing slash),
 * or `*` to explicitly opt in to demo-mode wildcard behavior. Missing =
 * CloudFormation deploy fails at parameter resolution, matching the
 * fail-closed pattern for other SSM-backed values.
 *
 * See `~/.kiro/steering/secrets-handling.md` for why the value lives in
 * SSM (not context) — SSM is per-stage and operator-manageable without
 * a code deploy.
 */
const allowedOrigin = ssmBacked({
  envVar: 'CVX_ALLOWED_ORIGIN',
  contextKey: 'cvxAllowedOrigin',
  kebab: 'allowed-origin',
});

// ─── Tier B ────────────────────────────────────────────────────────────

/**
 * ARN of the AgentCore Memory CMK (foundation stack output `MemoryKmsKeyArn`).
 *
 * Consumed by `vsa-core-stack`'s `CvxAgentCoreMemoryKmsDecrypt` statement,
 * which grants the AgentCore execution role decrypt permission for
 * RetrieveMemoryRecords / CreateEvent against the Foundation Memory.
 *
 * Tier B rather than Tier A because that grant sits behind a synth-time
 * conditional. A CFN token would satisfy the conditional unconditionally and
 * grant KMS decrypt for a key that does not exist. Set to `none` for a stage
 * with no Foundation Memory.
 */
const memoryKmsKeyArn = requireSynthValue({
  field: 'memoryKmsKeyArn',
  envVar: 'VSA_MEMORY_KMS_KEY_ARN',
  kebab: 'memory-kms-key-arn',
  optional: true,
});

/**
 * CMS VPC the Redis-backed Lambdas join.
 *
 * Tier B is structural here, not a judgement call: `ec2.Vpc.fromLookup` calls
 * `Token.isUnresolved` and throws "All arguments to Vpc.fromLookup() must be
 * concrete (no Tokens)".
 */
const vpcId = requireSynthValue({
  field: 'vpcId',
  envVar: 'CMS_VPC_ID',
  kebab: 'vpc-id',
});

/**
 * Security group granting the live-state Lambda access to CMS Redis.
 *
 * `SecurityGroup.fromSecurityGroupId` does tolerate a token, so this field
 * could be Tier A. It is Tier B because it is read two lines away from the
 * `Vpc.fromLookup` above, which cannot be — keeping the pair symmetric removes
 * a class of error rather than documenting it. See `docs/tech.md` open
 * question (a).
 */
const redisSecurityGroupId = requireSynthValue({
  field: 'redisSecurityGroupId',
  envVar: 'CMS_REDIS_SG_ID',
  kebab: 'redis-security-group-id',
});

/**
 * Consolidated CMS UI pool that the iOS client authenticates against for voice,
 * plus its app client. Registered as a trusted provider on each tenant's voice
 * identity pool so the AgentCore SigV4 credential exchange accepts CMS-pool
 * tokens.
 *
 * The two move together — `vsa-api-stack` requires both before it will add the
 * trust, so configuring one without the other yields a half-configured pool
 * that is silently ignored. Set both to `none` for a stage whose clients do not
 * use the shared CMS pool.
 *
 * Tier B for the same reason as `memoryKmsKeyArn`: the trust is added behind a
 * synth-time conditional, and a token would satisfy it unconditionally.
 */
const voiceAuthUserPoolId = requireSynthValue({
  field: 'voiceAuthUserPoolId',
  envVar: 'VOICE_AUTH_USER_POOL_ID',
  kebab: 'voice-auth-user-pool-id',
  optional: true,
});
const voiceAuthUserPoolClientId = requireSynthValue({
  field: 'voiceAuthUserPoolClientId',
  envVar: 'VOICE_AUTH_USER_POOL_CLIENT_ID',
  kebab: 'voice-auth-user-pool-client-id',
  optional: true,
});

/**
 * Additional Cognito user pool IDs whose tokens the VSA API's Cognito
 * authorizer must also accept, comma-separated.
 *
 * ⚠️  READ THIS BEFORE CHANGING OR OMITTING IT.
 *
 * This is how the CMS web UI's pool gets trusted alongside the per-tenant iOS
 * pools. Omitting it does not fail loudly — it silently returns 401 to the CMS
 * web UI on the CVX `/assistant/chat` route, with no other symptom. That
 * landmine was caught live during the 2026-07-13 CDK-cleanup deploy, and the
 * prod trust was separately verified before the 2026-07-16 cutover (see
 * `.kiro/specs/2026-07-16-cvx-prod-cutover/pre-cutover-state.md` § (d)).
 *
 * The value used to be baked in for prod and staging precisely so an operator
 * could not forget it. It is no longer baked, so the protection is now the
 * fail-closed throw above: an unset value stops synth instead of shipping a
 * deployment that 401s. That is a stronger guarantee than the baked default
 * gave — a baked default was also silently wrong for any stage it did not
 * cover — but it only holds while the throw remains.
 *
 * This field deliberately does NOT accept the generic `none` opt-out that the
 * other optional fields take; `none` is rejected with an error. A stage with no
 * external pool to trust must say so explicitly with
 * `EXTRA_USER_POOL_IDS=cvx-only-trust`. An earlier version accepted `none` and
 * carried a comment asking operators not to use it, which security review
 * cycle 1 correctly flagged (W1): a rule that lives only in a comment is not a
 * control, and this particular rule guards an authorization decision whose
 * failure is silent.
 *
 * Tier B because the string is split into N distinct
 * `cognito.UserPool.fromUserPoolId` constructs in `vsa-api-stack`, so N must be
 * known at synth. A token is one opaque string and would yield one construct
 * however many pools it names at deploy.
 */
const extraCognitoUserPoolIdsRaw = requireSynthValue({
  field: 'extraCognitoUserPoolIds',
  envVar: 'EXTRA_USER_POOL_IDS',
  kebab: 'extra-cognito-user-pool-ids',
  optional: true,
  optOut: TRUST_OPT_OUT,
});
const extraCognitoUserPoolIds = extraCognitoUserPoolIdsRaw
  .split(',')
  .map(s => s.trim())
  .filter(s => s.length > 0);

// Rejecting `none` (above) closed the obvious route to an empty trust list. This closes
// the rest of them. A value of `,` or `,,` is non-blank, is not the opt-out sentinel, and
// splits to zero entries — so it would have produced exactly the silent 401 that the
// sentinel check exists to prevent, by a different path. Security review cycle 2 flagged
// it as a suggestion; it is fixed rather than logged because it is the same defect, and
// the likely cause is an operator typo or a stray trailing comma rather than an attacker.
//
// Entries are also shape-checked. An unparseable pool ID would otherwise reach the
// authorizer as a ProviderARN and fail at deploy with a CloudFormation error naming
// neither this field nor the env var. Failing here names both.
if (extraCognitoUserPoolIdsRaw !== '' && extraCognitoUserPoolIds.length === 0) {
  throw new Error(
    `extraCognitoUserPoolIds was supplied but contains no pool IDs (value was punctuation ` +
      `only, e.g. "," ). That would silently drop the CMS-pool trust and return 401 on ` +
      `/assistant/chat with no other symptom. Supply a comma-separated list of pool IDs, or ` +
      `set EXTRA_USER_POOL_IDS=${TRUST_OPT_OUT} if stage "${stage}" has no external pool to trust.`,
  );
}
const malformedPools = extraCognitoUserPoolIds.filter(id => !isResolvedUserPoolId(id));
if (malformedPools.length > 0) {
  throw new Error(
    `extraCognitoUserPoolIds contains ${malformedPools.length} entry/entries that are not ` +
      `Cognito user pool IDs (expected {region}_{suffix}). Each becomes a trusted ProviderARN ` +
      `on the vsa-${stage}-api authorizer, so a malformed entry fails the deploy with an error ` +
      `naming neither this field nor EXTRA_USER_POOL_IDS. Fix the value at ` +
      `${cvxParameterName(stage, 'extra-cognito-user-pool-ids')} or in EXTRA_USER_POOL_IDS.`,
  );
}

const assistantChatRuntimeRegion = process.env.CVX_RUNTIME_REGION || region;

// CVX Acquire spec 2026-08-04-cvx-oem-discover-order-meridian — Group 2.
// AcquireStack provisions the two DynamoDB tables independently of the core
// runtime stack so the tables can be deployed / destroyed without touching
// the AgentCore execution role or the existing runtime infrastructure.
// Both table props are passed to VsaCoreStack below so it can emit IAM
// grants to the AgentCore execution role (catalog: read, orders: read+write).
const acquire = new AcquireStack(app, `vsa-${stage}-acquire`, {
  env,
  stage,
});

const core = new VsaCoreStack(app, `vsa-${stage}-core`, {
  env,
  stage,
  cmsStage,
  cmsKmsKeyArn,
  memoryKmsKeyArn,
  vsaAcquireCatalog: acquire.vsaAcquireCatalog,
  vsaAcquireOrders:  acquire.vsaAcquireOrders,
  vsaAcquireLeads:   acquire.vsaAcquireLeads,
});

// CVX-on-foundation v1 — Foundation stack (AgentCore Memory + CMK).
// Region resolves via per-stage `foundationRegion` (introduced 2026-07-16
// by spec `2026-07-16-cvx-prod-cutover` for prod cutover — prod uses
// us-east-1 to colocate Memory with runtime + KB; staging stays on
// us-west-2 to colocate with the Nova Sonic bidi runtime). Fall-through
// to `us-west-2` preserves pre-2026-07-16 behavior when the stage default
// is unset (adding a new stage that omits `foundationRegion` will silently
// pin to us-west-2 — set the field explicitly for any new stage).
// Cross-stack reads of the Memory ID happen via env-var injected at
// runtime, not via cdk.Fn.importValue, so there is no cross-stack synth
// dependency on the region choice.
const foundationEnv = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: defaults.foundationRegion || 'us-west-2',
};
new VsaFoundationStack(app, `vsa-${stage}-foundation`, {
  env: foundationEnv,
  stage,
});

// CVX-on-foundation v1 — Analytics stack (Athena workgroup + results bucket).
// Pinned to us-east-1 because Athena cannot query a Glue catalog in a
// different region; ADP's Glue catalog and Iceberg lake are us-east-1
// (per ADP foundation-v1 close-out). The CVX AgentCore execution role
// (in vsa-core-stack, us-west-2) holds athena:* + s3:* IAM grants
// scoped to this workgroup + bucket via the AdpGlueAthenaRead +
// CvxAthenaResultsS3 sids in Group 2.A.
const analyticsEnv = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: 'us-east-1',
};
const analyticsStack = new VsaAnalyticsStack(app, `vsa-${stage}-analytics`, {
  env: analyticsEnv,
  stage,
  // Core-stack region role name (IAM is global); used for LF grants to the
  // persona-loader's execution role. `region` is the core/runtime region.
  agentCoreRoleName: `AmazonBedrockAgentCoreSDKRuntime-${region}-${stage}`,
  // Tier 2 Lambda execution role name (IAM is account-wide; region-suffixed).
  // Grants LF SELECT + DESCRIBE on adp_{stage}_energy_usage so the Tier 2
  // role can run Athena queries against the ADP lake (strict-mode LF means
  // IAM alone is insufficient — security-review Cycle 4, T3.4).
  // `region` here is the Lambda's deployment region (staging → us-west-2).
  tier2RoleName: `vsa-${stage}-tier2-scheduler-${region}`,
});

// CVX-on-foundation v1 — emit CVX-side principal ARNs for ADP KB resource-policy attachment.
// Operator passes this output to ADP CDK via `-c cvxKbPrincipals=<value>`.
// Single-element comma-sep string in v1; expands to multiple ARNs in future stages.
new cdk.CfnOutput(core, 'CvxKbPrincipalArns', {
  value: core.agentCoreExecutionRole.roleArn,
  description: 'Comma-separated CVX role ARNs for ADP Bedrock KB resource-policy attachment (v1: single element)',
  exportName: `cvx-${stage}-cvx-kb-principal-arns`,
});

const classifierStack = new TriageClassifierStack(app, `vsa-${stage}-triage-classifier`, {
  env,
  stage,
  cmsStage,
  cmsKmsKeyArn,
  tenantConfigTable: core.tenantConfigTable,
  triageDecisionsTable: core.triageDecisionsTable,
});

// ─── RSA dispatch stack (spec `2026-09-23-cvx-rsa-stub-dispatch`) ──────
//
// vsa-{stage}-rsa-dispatch table + DLQ + stream consumer Lambda.
// Instantiated before VsaApiStack so the table construct can be passed as
// a prop and the API stack can grant PutItem on the correct ARN rather than
// a hand-built string literal.
const rsaStack = new VsaRsaStack(app, `vsa-${stage}-rsa`, {
  env,
  stage,
  // F5.1: operator email for the DLQ alarm subscriber. Pass as a literal so
  // the subscription is created at synth time when the env var is set.
  // The SSM parameter /cvx/{stage}/rsa-alarm-email is the canonical home for
  // this value; the env-var override is for emergency deploy-time injection.
  // See vsa-rsa-stack.ts VsaRsaStackProps.rsaAlarmEmail for posture details.
  rsaAlarmEmail: {
    literal: process.env.CVX_RSA_ALARM_EMAIL || '',
    parameterName: `/cvx/${stage}/rsa-alarm-email`,
  },
  // F8.2's `cmsKmsKeyArn` pass-through is gone (Fix Group 13 item 2): the
  // dispatch table now gets its own customer-managed key inside VsaRsaStack.
  // /cvx/{stage}/cms-kms-key-arn named the AWS-managed alias/aws/dynamodb key
  // on staging, so reusing it gave the table no key-level control.
  // G3-D6 (amended F9.3): simulated lifecycle timing — plain string literals,
  // NOT ConfigValues. The /cvx/{stage}/rsa-sim-* and
  // /cvx/{stage}/rsa-requested-stale-* SSM parameters do not exist; using
  // resolveConfigValue here would send an empty literal to
  // valueForStringParameter and fail the changeset at deploy.
  //
  // Absent/empty env var → pass undefined so VsaRsaStack uses its built-in
  // defaults (60/300/900). Non-empty values are validated at synth time by
  // VsaRsaStack's constructor (throws on abc/-1/0, and on stale < 600).
  rsaSimEnRouteAfterSeconds:   process.env.CVX_RSA_SIM_EN_ROUTE_AFTER_SECONDS   || undefined,
  rsaSimArrivedAfterSeconds:   process.env.CVX_RSA_SIM_ARRIVED_AFTER_SECONDS    || undefined,
  rsaRequestedStaleAfterSeconds: process.env.CVX_RSA_REQUESTED_STALE_AFTER_SECONDS || undefined,
});

new VsaApiStack(app, `vsa-${stage}-api`, {
  env,
  stage,
  cmsStage,
  tenantConfigTable: core.tenantConfigTable,
  classifier: classifierStack.classifier,
  cmsKmsKeyArn,
  redisEndpoint,
  dmsApiEndpoint,
  connectInstanceArn,
  avxFleetIdsClaimTrusted,
  avxCustomerIdClaimTrusted,
  avxVehicleIdClaimTrusted,
  vpcId,
  redisSecurityGroupId,
  extraCognitoUserPoolIds,
  voiceAuthUserPoolId,
  voiceAuthUserPoolClientId,
  assistantChatRuntimeArn,
  assistantChatRuntimeRegion,
  allowedOrigin,
  // ── Acquire resources (spec `2026-07-31-zone2-order-backend-and-kiosk`) ──
  // The reservation_handoff Lambda is integrated into /acquire/reservations rather
  // than re-instantiated — it owns the SFN start-execution side-effect.
  // Table references let the API stack create its own read-only api-acquire-* handlers.
  acquireReservationHandoffFn: acquire.reservationHandoffFn,
  // Export name for the cycle-break pattern (decisions.md Q14): VsaApiStack reads the
  // reservation_handoff ARN via Fn.importValue (CfnPermission L1) rather than a direct
  // construct cross-reference, breaking the CDK DependencyCycle on RestApi.Ref.
  acquireReservationHandoffFnArnExportName: acquire.reservationHandoffFnArnExportName,
  acquireCatalogTable: acquire.vsaAcquireCatalog,
  acquireOrdersTable: acquire.vsaAcquireOrders,
  acquireLeadsTable: acquire.vsaAcquireLeads,
  // ── AVX API wiring (Group 4.T5, Spec 1a) ────────────────────────────────
  // Table-name CFN export names from VsaTier2Stack, resolved via Fn.importValue
  // inside VsaApiStack (same cycle-break pattern as acquireReservationHandoffFn).
  // VsaTier2Stack is instantiated AFTER VsaApiStack so direct construct references
  // are not possible — the Fn.importValue cycle-break is the canonical pattern here.
  avxFindingsTableNameExportName:         `vsa-${stage}-avx-findings-table-name`,
  avxActionsTableNameExportName:          `vsa-${stage}-avx-actions-table-name`,
  avxFeedbackTableNameExportName:         `vsa-${stage}-avx-feedback-table-name`,
  notificationDevicesTableNameExportName: `vsa-${stage}-notification-devices-table-name`,
  // ── RSA dispatch wiring (spec `2026-09-23-cvx-rsa-stub-dispatch` Task 2.4) ─
  // Pass the table construct from VsaRsaStack so the API stack can thread
  // the tableName into the escalate Lambda's env AND grant PutItem on the
  // correct ARN, rather than rebuilding a string literal that could silently
  // diverge from the grant (the ADP_REGION defect class, documented inline).
  rsaDispatchTable: rsaStack.rsaDispatchTable,
  // Fix Group 13 item 2: the table's own key, so escalate (PutItem) and the
  // status reader (GetItem) are granted on it. A cross-stack reference: the
  // RSA stack must deploy before this one.
  rsaDispatchKey: rsaStack.rsaDispatchKey,
  // F5.D1: position freshness gate for Option D live-state invocation.
  // Default 900 s (15 min) — a policy choice, not a technical constant.
  // Override via CVX_RSA_POSITION_MAX_AGE_SECONDS or the SSM parameter.
  rsaPositionMaxAgeSeconds: {
    literal: process.env.CVX_RSA_POSITION_MAX_AGE_SECONDS || '900',
    parameterName: `/cvx/${stage}/rsa-position-max-age-seconds`,
  },
});

// ─── Tier 2: Platform Health Agent ────────────────────────────────────────
//
// Spec: `.kiro/specs/2026-08-03-cvx-tier2-platform-health-agent/spec.md`
// Decisions: Plan B (Lambda + Strands + Bedrock), ADP in us-east-1 for both stages.
// Deployed to this.region (staging → us-west-2, prod → us-east-1) for the Lambda
// and artifact table; Athena workgroup + lake are always in us-east-1.
//
// ADP account ID: same as CVX in the current single-account staging setup.
// Override via CDK context `adpAccountId` or env var ADP_ACCOUNT_ID when ADP
// migrates to a dedicated account.
const tier2Stack = new VsaTier2Stack(app, `vsa-${stage}-tier2`, {
  env,
  stage,
  // ADP is in us-east-1 for both stages per decisions.md T1.2.
  adpRegion:    'us-east-1',
  // Same account by default; override when ADP migrates to its own account.
  adpAccountId: process.env.ADP_ACCOUNT_ID || app.node.tryGetContext('adpAccountId'),
  // Pass the full Tier A ConfigValue so the stack can resolve it in stack scope
  // (valueForStringParameter needs Stack.of(scope)) — the same shape every other
  // Tier A consumer receives. Previously this passed `.literal`, which is populated
  // only from the CMS_KMS_KEY_ARN env var or context and never from SSM: on every
  // real deploy it was undefined, so NotificationDevices and the operator-alert SNS
  // topic silently fell back to no CMK while tests (which set the env var) saw the
  // CMK path. Found by the Fix Groups 10-14 security review against the deployed
  // table. See vsa-tier2-stack.ts for the full note.
  cmsKmsKeyArn,
});

// Analytics stack depends on Tier 2 so that the Lambda execution role exists
// before Lake Formation grants reference it.  Without this edge the analytics
// stack is deployed first (it was instantiated earlier in this file) and the
// six CfnPrincipalPermissions resources fail with "Invalid principal" because
// vsa-{stage}-tier2-scheduler-{region} does not exist yet.
// Confirmed via live rollback at 2026-08-05T14:26:18Z — see
// issues/2026-08-05-tier2-adp-read-path-never-exercised-live/report.md § F1.
// The role name is passed as a plain string across a region boundary (Tier 2
// is in this.region; analytics is in us-east-1), so addDependency is correct
// here — a cross-stack L2 reference would not produce a resolvable cross-region
// CFN export.
analyticsStack.addDependency(tier2Stack);

// ─── cdk-nag suppressions ──────────────────────────────────────────────
//
// Applied LAST, because addStackSuppressions needs the stacks to exist. Every
// suppression carries a written reason and is scoped with `appliesTo` wherever
// the finding shape allows it — see lib/nag-suppressions.ts for the rules on
// editing that list. Harmless when CDK_NAG is unset: suppressions are only
// construct metadata, and nothing reads them unless the Aspect is running.
applyCvxNagSuppressions(app);
