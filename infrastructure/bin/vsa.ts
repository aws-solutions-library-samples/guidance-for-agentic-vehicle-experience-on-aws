#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { VsaCoreStack } from '../lib/vsa-core-stack';
import { TriageClassifierStack } from '../lib/triage-classifier-stack';
import { VsaApiStack } from '../lib/vsa-api-stack';
import { VsaFoundationStack } from '../lib/vsa-foundation-stack';
import { VsaAnalyticsStack } from '../lib/vsa-analytics-stack';
import { AcquireStack } from '../lib/vsa-acquire-stack';
import { VsaTier2Stack } from '../lib/vsa-tier2-stack';
import { ConfigValue, cvxParameterName, isResolvedUserPoolId } from '../lib/config-value';

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
const cmsStage = process.env.CMS_STAGE || defaults.cmsStage || 'dev';

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

new VsaApiStack(app, `vsa-${stage}-api`, {
  env,
  stage,
  cmsStage,
  tenantConfigTable: core.tenantConfigTable,
  classifier: classifierStack.classifier,
  cmsKmsKeyArn,
  redisEndpoint,
  vpcId,
  redisSecurityGroupId,
  extraCognitoUserPoolIds,
  voiceAuthUserPoolId,
  voiceAuthUserPoolClientId,
  assistantChatRuntimeArn,
  assistantChatRuntimeRegion,
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
