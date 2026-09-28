import * as cdk from 'aws-cdk-lib';
import * as apigw from 'aws-cdk-lib/aws-apigateway';
import * as apigwv2 from 'aws-cdk-lib/aws-apigatewayv2';
import * as apigwv2int from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import * as apigwv2auth from 'aws-cdk-lib/aws-apigatewayv2-authorizers';
import * as budgets from 'aws-cdk-lib/aws-budgets';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';
import * as path from 'path';
import { pythonLambdaAsset } from './python-lambda-asset';
import { assertStagedCopyMatchesSource } from './staged-asset-check';
import { TenantPool } from './tenant-pool';
import {
  ConfigValue,
  isResolvedUserPoolClientId,
  isResolvedUserPoolId,
  resolveConfigValue,
} from './config-value';

/**
 * Upper limit, in seconds, for `/escalate`'s position freshness threshold
 * (`RSA_POSITION_MAX_AGE_SECONDS`). A larger value, like a non-finite one, turns
 * the stale-position check into no check. Mirrored by the handler's
 * `_RSA_POSITION_MAX_AGE_CEILING_SECONDS` and the Makefile's `_deploy-cdk` guard;
 * `lambdas/api-escalate/tests` pins all three to one value.
 */
export const RSA_POSITION_MAX_AGE_CEILING_SECONDS = 3600;

export interface VsaApiStackProps extends cdk.StackProps {
  stage: string;
  /** CMS deployment stage — used to derive DynamoDB table names (e.g. "dev" → "cms-dev-storage-vehicles") */
  cmsStage: string;
  tenantConfigTable: dynamodb.ITable;
  classifier: lambda.IFunction;
  /**
   * KMS key ARN used by CMS DynamoDB tables (SSE-KMS), as a Tier A
   * `ConfigValue`: env-var override, else the `/cvx/{stage}/cms-kms-key-arn`
   * SSM parameter. Resolved once in the constructor.
   */
  cmsKmsKeyArn: ConfigValue;
  /**
   * Redis endpoint for vehicle live state (from CMS MSK stack), as a Tier A
   * `ConfigValue`: env-var override, else `/cvx/{stage}/redis-endpoint`.
   */
  redisEndpoint: ConfigValue;
  /**
   * Base URL of the DMS REST API, as a Tier A `ConfigValue`: env-var override, else
   * `/cvx/{stage}/dms-api-endpoint`.
   *
   * Threaded onto the AVX findings Lambda as `VSA_DMS_API_ENDPOINT`, which
   * `agents/executors/dms.py:67` reads when the approve route dispatches
   * `target_system=dms`. Before this existed the env var was READ and never SET — a
   * declared input with no writer, so a wired dispatch would have called an empty base
   * URL and failed in a way that looks like a DMS outage rather than a config gap.
   *
   * Not `cdk.Fn.importValue('dms-{stage}-api-url')`, though that export exists: see the
   * `dmsApiEndpoint` declaration in `bin/vsa.ts` for why a cross-accelerator CFN import
   * was rejected.
   */
  dmsApiEndpoint: ConfigValue;
  /**
   * ARN of the Amazon Connect instance the P0 outbound callback dials from, as a Tier A
   * `ConfigValue`: env-var override, else `/cvx/{stage}/connect-instance-arn`. Scopes
   * `connect:StartOutboundVoiceContact` to that instance's contacts (security W2).
   */
  connectInstanceArn: ConfigValue;
  /**
   * Operator attestation that `custom:fleetIds` is safe to authorize on, as a Tier A
   * `ConfigValue`: env-var override, else `/cvx/{stage}/avx-fleet-ids-claim-trusted`.
   *
   * Threaded onto BOTH the AVX findings Lambda (`vsa-{stage}-api-avx-findings`) and the
   * AVX actions Lambda (`vsa-{stage}-api-avx-actions`), as `AVX_FLEET_IDS_CLAIM_TRUSTED`,
   * which `lambdas/_lib/avx_scope.py` compares against exactly `"true"`. Any other value,
   * including `"false"`, keeps fleet standing denied. Per
   * `docs/identity-and-standing-contract.md` § 5.1, set `true` only after confirming the
   * claim is excluded from every trusted pool's app-client `WriteAttributes` and that the
   * owning repo guards that exclusion.
   *
   * Optional so fixtures that do not exercise fleet standing need not supply it. Omitted
   * means the variable is not set, which is the untrusted default. `bin/vsa.ts` always
   * supplies it, so a real deploy fails at CloudFormation if the parameter is unseeded:
   * the operator has to decide, in one direction or the other.
   */
  avxFleetIdsClaimTrusted?: ConfigValue;
  /**
   * Operator attestation that `custom:customerId` is safe to authorize on, as a Tier A
   * `ConfigValue`: env-var override, else `/cvx/{stage}/avx-customer-id-claim-trusted`.
   *
   * Threaded onto the AVX findings Lambda ONLY (`vsa-{stage}-api-avx-findings`) as
   * `AVX_CUSTOMER_ID_CLAIM_TRUSTED`. `lambdas/_lib/avx_scope.py` compares against
   * exactly `"true"`. Per `docs/identity-and-standing-contract.md` § 5.1, set `true`
   * only after confirming the claim is excluded from every trusted pool's app-client
   * `WriteAttributes` and that the owning repo guards that exclusion.
   *
   * Optional; omitted means untrusted. `bin/vsa.ts` always supplies it, so a real
   * deploy fails at CloudFormation if the parameter is unseeded.
   */
  avxCustomerIdClaimTrusted?: ConfigValue;
  /**
   * Operator attestation that `custom:vehicleId` is safe to authorize on, as a Tier A
   * `ConfigValue`: env-var override, else `/cvx/{stage}/avx-vehicle-id-claim-trusted`.
   *
   * Threaded onto the AVX findings Lambda ONLY (`vsa-{stage}-api-avx-findings`) as
   * `AVX_VEHICLE_ID_CLAIM_TRUSTED`. `lambdas/_lib/avx_scope.py` compares against
   * exactly `"true"`. Per `docs/identity-and-standing-contract.md` § 5.1, set `true`
   * only after confirming the claim is excluded from every trusted pool's app-client
   * `WriteAttributes` and that the owning repo guards that exclusion.
   *
   * Optional; omitted means untrusted. `bin/vsa.ts` always supplies it, so a real
   * deploy fails at CloudFormation if the parameter is unseeded.
   */
  avxVehicleIdClaimTrusted?: ConfigValue;
  /**
   * VPC the live-state Lambda runs in. Must be the CMS-owned VPC that
   * hosts the Redis cluster — Redis has no public endpoint, so the
   * Lambda has to live inside the same VPC. Sourced per-stage from
   * `STAGE_DEFAULTS` in `bin/vsa.ts`, overridable via `CMS_VPC_ID` env.
   */
  vpcId: string;
  /**
   * Security group ID attached to the live-state Lambda. This SHOULD
   * be the CMS Redis SG itself — it allows ingress from itself on
   * 6379, so reusing it grants connectivity with no new rules.
   * Sourced per-stage from `STAGE_DEFAULTS` in `bin/vsa.ts`,
   * overridable via `CMS_REDIS_SG_ID` env.
   */
  redisSecurityGroupId: string;
  /**
   * Additional Cognito User Pool IDs whose tokens should be accepted by
   * the VSA REST API Cognito authorizer, in addition to the per-tenant
   * iOS pools defined in this stack.
   *
   * Use case: the CMS UI runs against a separate Cognito user pool
   * (it predates the VSA stack), but the Vehicle Detail page now
   * embeds a `VehicleHealthScoreWidget` that fetches the
   * server-computed health score from `/vehicles/{id}/context`. The
   * CMS UI signs that fetch with its own pool's id-token; without
   * this list the authorizer rejects it as 401 Unauthorized.
   *
   * Trust implication: every additional pool is granted the same
   * (read + write) access surface as the iOS tenant pools — there is
   * no per-pool method-level scoping today. Only add pools whose
   * users are authorized for the same fleet operations the iOS
   * surface allows.
   *
   * Wired from `EXTRA_USER_POOL_IDS` env var in bin/vsa.ts (comma-
   * separated). Empty / unset is fine — the array is concatenated.
   */
  extraCognitoUserPoolIds?: string[];

  /**
   * The externally-owned Cognito user pool (id + app client id) the iOS
   * client actually authenticates against for voice — i.e. the consolidated
   * CMS UI pool. Added as an additional trusted provider on each tenant's
   * voice identity pool so `GetCredentialsForIdentity` accepts CMS-pool
   * tokens (fixes NotAuthorizedException "Token is not from a supported
   * provider"). Both must be set for the trust to be wired; either unset =
   * no-op (legacy CVX-pool-only behaviour).
   *
   * Wired from VOICE_AUTH_USER_POOL_ID / VOICE_AUTH_USER_POOL_CLIENT_ID env
   * vars (or STAGE_DEFAULTS) in bin/vsa.ts.
   */
  voiceAuthUserPoolId?: string;
  voiceAuthUserPoolClientId?: string;

  /**
   * Full ARN of the AgentCore text runtime the /assistant/chat route Lambda
   * invokes via SigV4 (bedrock-agentcore:InvokeAgentRuntime). Same-account,
   * same-region as this stack. The IAM grant is scoped to this exact ARN.
   *
   * For staging this is the vsa_supervisor_bidi_staging runtime ARN
   * (the text entrypoint and bidi voice share the same container).
   *
   * Tier A `ConfigValue`: `CVX_RUNTIME_ARN` / `-c cvxRuntimeArn` override,
   * else the `/cvx/{stage}/assistant-chat-runtime-arn` SSM parameter. The
   * parameter must be updated whenever `agentcore deploy` remints the
   * runtime — the ARN's trailing suffix changes each time.
   */
  assistantChatRuntimeArn: ConfigValue;

  /**
   * AWS region where the AgentCore runtime above lives. Passed to the
   * Lambda as CVX_RUNTIME_REGION. Same region as this stack for staging.
   * Wired from CVX_RUNTIME_REGION env var (default: stack region) in bin/vsa.ts.
   */
  assistantChatRuntimeRegion: string;

  /**
   * Allowed CORS origin — scopes API Gateway `defaultCorsPreflightOptions`,
   * both `GatewayResponse` blocks (`DEFAULT_4XX`, `DEFAULT_5XX`), and every
   * Lambda handler's `Access-Control-Allow-Origin` response header. Every
   * cross-origin authenticated call must come from this origin.
   *
   * Tier A `ConfigValue`: `CVX_ALLOWED_ORIGIN` / `-c cvxAllowedOrigin` override
   * else the `/cvx/{stage}/allowed-origin` SSM parameter. Missing = CFN deploy
   * fails at parameter resolution, matching the fail-closed pattern for other
   * SSM-backed values.
   *
   * Set to the CMS UI CloudFront distribution URL (scheme + host, no trailing
   * slash), or to `*` to explicitly opt in to demo-mode wildcard behavior.
   */
  allowedOrigin: ConfigValue;

  // ── Acquire resources (from AcquireStack) ────────────────────────────────
  //
  // The reservation_handoff Lambda is deployed by AcquireStack (spec
  // `2026-07-31-zone2-order-backend-and-kiosk` Group 4). It is integrated here
  // rather than re-instantiated to avoid deploying a duplicate; it owns the
  // SFN start-execution + EventBridge publish side-effects. The three API-side
  // GET handlers (catalog, order, offers, leads) are created directly in this
  // stack following the existing api-* Lambda pattern — they are read-only
  // wrappers with no overlap with the pipeline Lambdas in AcquireStack.
  /** POST /acquire/reservations — reservation_handoff from AcquireStack */
  acquireReservationHandoffFn: lambda.IFunction;
  /**
   * CloudFormation export name for the reservation_handoff function ARN.
   * Used by VsaApiStack to wire the Lambda permission via CfnPermission +
   * Fn.importValue rather than a direct construct cross-reference, breaking
   * the CDK DependencyCycle between AcquireStack and VsaApiStack.
   * Value: `AcquireStack.reservationHandoffFnArnExportName`.
   * See decisions.md Q14 for the full cycle-break rationale.
   */
  acquireReservationHandoffFnArnExportName: string;
  /** Catalog DynamoDB table — read by api-acquire-catalog + api-acquire-offers */
  acquireCatalogTable: dynamodb.ITable;
  /** Orders DynamoDB table — read by api-acquire-order */
  acquireOrdersTable: dynamodb.ITable;
  /** Leads DynamoDB table — read+write by api-acquire-leads */
  acquireLeadsTable: dynamodb.ITable;

  // ── Edge throttling config (decisions.md Q9) ─────────────────────────────
  // Backstop that does NOT rely on the client-supplied session id. Limits must
  // comfortably exceed the 60-80 visitors/hour design point so that throttling
  // a real visitor is a worse outcome than an unexpected bill.
  //
  // Rule-of-thumb sizing:
  //   - 80 concurrent visitors × up to ~5 req/s each = 400 req/s sustained
  //   - Provide 2× headroom → 800 req/s rate limit / 200 burst
  //   - Daily max: 80 visitors × 8-hour show day × 20 req each = 12,800 requests
  //     (comfortably under a daily quota of 200,000)
  //
  // Overridable via stack config so the limits can be adjusted without a code edit.
  /** Steady-state token bucket fill rate (requests/second). Default: 500. */
  throttleRateLimit?: number;
  /** Maximum burst size (tokens). Default: 200. */
  throttleBurstLimit?: number;

  // ── AVX API wiring (Group 4, Spec 1a) ────────────────────────────────────
  //
  // These four export names are the CFN-export keys produced by VsaTier2Stack
  // (vsa-${stage}-avx-{findings,actions,feedback}-table-name and
  // vsa-${stage}-notification-devices-table-name). They are passed explicitly
  // because VsaTier2Stack is instantiated AFTER VsaApiStack in bin/vsa.ts and
  // cannot be referenced as a construct — the Fn.importValue cycle-break
  // pattern (same as acquireReservationHandoffFnArnExportName) is used instead.
  //
  // All four are optional: when absent, a synth-time warning is emitted and the
  // AVX Lambda constructs are skipped entirely (fail-graceful, matching the
  // acquire pattern for incremental roll-out).
  /**
   * CFN export name for the AVX Findings DynamoDB table name.
   * Value: `vsa-${stage}-avx-findings-table-name` (from VsaTier2Stack).
   */
  avxFindingsTableNameExportName?: string;
  /**
   * CFN export name for the AVX Actions DynamoDB table name.
   * Value: `vsa-${stage}-avx-actions-table-name` (from VsaTier2Stack).
   */
  avxActionsTableNameExportName?: string;
  /**
   * CFN export name for the AVX Feedback DynamoDB table name.
   * Value: `vsa-${stage}-avx-feedback-table-name` (from VsaTier2Stack).
   */
  avxFeedbackTableNameExportName?: string;
  /**
   * CFN export name for the Notification Devices DynamoDB table name.
   * Value: `vsa-${stage}-notification-devices-table-name` (from VsaTier2Stack).
   */
  notificationDevicesTableNameExportName?: string;

  // ── RSA dispatch wiring (spec `2026-09-23-cvx-rsa-stub-dispatch`) ────────
  /**
   * The RSA dispatch table from VsaRsaStack.
   *
   * Passed as a construct rather than a CFN export name so the API stack
   * can grant PutItem on the exact ARN (from the construct) and thread the
   * tableName directly into the escalate Lambda's environment without a
   * hand-built string literal — the same risk the ADP_REGION comment at
   * the escalate block warns against: "fixing only one converts a
   * ResourceNotFoundException into an AccessDeniedException."
   *
   * Required (F4.6 / review Cycle 4 W4): an absent table would silently
   * omit both the RSA_DISPATCH_TABLE env var and the PutItem IAM grant,
   * causing every /escalate call to 502 at Lambda init on the missing env var.
   * That failure mode is broader than roadside — every P0 escalation fails.
   * Matches the repo's fail-closed pattern from
   * 2026-08-05-cvx-stage-defaults-parameterization.
   */
  rsaDispatchTable: dynamodb.ITable;

  /**
   * The RSA dispatch table's own customer-managed key, from VsaRsaStack
   * (Fix Group 13 item 2). The escalate writer and the status reader are
   * granted on this key only. Required: without it both would lose table
   * access at the first KMS call, and nothing at synth would say so.
   */
  rsaDispatchKey: kms.IKey;

  /**
   * Freshness threshold for vehicle position lookup in `/escalate` (seconds).
   *
   * Option D (F5.D1): before writing the RSA dispatch row, the handler invokes
   * `api-vehicle-live-state` and gates dispatch on the returned position being fresh.
   * `api-vehicle-live-state` uses 5 min as its "connected" threshold; 900 s (15 min)
   * is deliberately more tolerant for dispatch than for liveness — a slightly stale
   * coordinate is better than refusing dispatch outright if the vehicle just went
   * quiet. **This is a policy choice, not a technical default** — flag to the user
   * before adjusting.
   *
   * Tier A `ConfigValue`: `CVX_RSA_POSITION_MAX_AGE_SECONDS` env-var override,
   * else the `/cvx/{stage}/rsa-position-max-age-seconds` SSM parameter.
   *
   * Default (literal fallback): 900 (15 minutes). Wired by `bin/vsa.ts` as
   * `{ literal: process.env.CVX_RSA_POSITION_MAX_AGE_SECONDS || '900',
   *    parameterName: \`/cvx/\${stage}/rsa-position-max-age-seconds\` }`.
   *
   * Bound (Fix Group 13): a literal must be a whole number from 1 to
   * `RSA_POSITION_MAX_AGE_CEILING_SECONDS` (3600) or the constructor throws. An
   * SSM-resolved value is not checked at synth; the handler fails closed on it.
   *
   * Mirror of the api-vehicle-context → api-vehicle-live-state pattern at
   * vsa-api-stack.ts:854-856.
   */
  rsaPositionMaxAgeSeconds?: ConfigValue;
}

export class VsaApiStack extends cdk.Stack {
  readonly restApi: apigw.RestApi;
  readonly wsApi: apigwv2.WebSocketApi;
  readonly tenantPools: Record<string, TenantPool>;

  constructor(scope: Construct, id: string, props: VsaApiStackProps) {
    super(scope, id, props);

    // ─── Tier A values ────────────────────────────────────────────────────
    //
    // Resolved here rather than in `bin/vsa.ts` because
    // `ssm.StringParameter.valueForStringParameter` calls `Stack.of(scope)`
    // and throws when handed the `App`. Each is either the operator's env-var
    // override or a deploy-time SSM token.
    //
    // None of them is guarded here, deliberately: a token is never empty, so
    // an emptiness check could not fail. CloudFormation enforces that the
    // parameter exists, at deploy. See lib/config-value.ts §
    // "ON EXISTENCE CHECKING". (rsaPositionMaxAgeSeconds, below, is the
    // exception: a concrete literal is range-checked, Fix Group 13.)
    const cmsKmsKeyArn = resolveConfigValue(this, 'cmsKmsKeyArn', props.cmsKmsKeyArn);
    const redisEndpoint = resolveConfigValue(this, 'redisEndpoint', props.redisEndpoint);
    const dmsApiEndpoint = resolveConfigValue(this, 'dmsApiEndpoint', props.dmsApiEndpoint);
    const connectInstanceArn = resolveConfigValue(this, 'connectInstanceArn', props.connectInstanceArn);
    const assistantChatRuntimeArn = resolveConfigValue(
      this,
      'assistantChatRuntimeArn',
      props.assistantChatRuntimeArn,
    );
    const allowedOrigin = resolveConfigValue(this, 'allowedOrigin', props.allowedOrigin);
    // F5.D1: position freshness gate for RSA dispatch (Option D). Default 900 s.
    // See props.rsaPositionMaxAgeSeconds JSDoc for the policy rationale.
    const rsaPositionMaxAgeSeconds = resolveConfigValue(
      this,
      'rsaPositionMaxAgeSeconds',
      props.rsaPositionMaxAgeSeconds ?? {
        literal: '900',
        parameterName: `/cvx/${props.stage}/rsa-position-max-age-seconds`,
      },
    );
    // Fix Group 13: validate a concrete value at synth. A token (the SSM path)
    // cannot be checked here; the handler fails closed on it at runtime.
    if (!cdk.Token.isUnresolved(rsaPositionMaxAgeSeconds)) {
      const raw = rsaPositionMaxAgeSeconds.trim();
      if (!/^[1-9][0-9]*$/.test(raw) || Number(raw) > RSA_POSITION_MAX_AGE_CEILING_SECONDS) {
        throw new Error(
          `rsaPositionMaxAgeSeconds must be a whole number of seconds from 1 to ` +
          `${RSA_POSITION_MAX_AGE_CEILING_SECONDS}; got '${rsaPositionMaxAgeSeconds}'. ` +
          `/escalate refuses every vehicle position for a value outside that range.`,
        );
      }
    }

    // ─── CMS table names derived from cmsStage ─────────────────────────────
    const cms = (suffix: string) => `cms-${props.cmsStage}-${suffix}`;
    const cmsTable = {
      vehicles: cms('storage-vehicles'),
      drivers: cms('storage-drivers'),
      trips: cms('storage-trips'),
      serviceHistory: cms('storage-service-history'),
      safetyEvents: cms('storage-safety-events'),
      dtcHistory: cms('storage-dtc-history'),
      // vfoActionQueue removed — spec R1 (`2026-09-23-cvx-rsa-stub-dispatch`).
      // The escalate Lambda now writes to vsa-{stage}-rsa-dispatch instead.
      // grep confirms the escalate Lambda was its only consumer; removing the
      // definition and the live grant eliminates standing privilege with no caller.
    };

    // --- REST API -----------------------------------------------------------
    // Constructed BEFORE per-tenant pools so `this.restApi.restApiId` is a
    // valid CloudFormation Ref token when it is threaded into TenantPool via
    // the `vsaApiId` prop (security-review Cycle 6 W2 / decisions.md Q14).
    // Both constructs live in the same stack — no cross-stack dependency.
    this.restApi = new apigw.RestApi(this, 'RestApi', {
      restApiName: `vsa-${props.stage}-api`,
      defaultCorsPreflightOptions: {
        // Scoped to the single origin resolved from `/cvx/{stage}/allowed-origin`.
        // The CMS UI SPA is the only browser-cross-origin caller; iOS is a native
        // app (no `Origin` header, so unaffected by CORS); Connect + AgentCore
        // call server-side via SigV4. See VsaApiStackProps.allowedOrigin JSDoc.
        //
        // A wildcard here (via SSM value `*`) is explicitly opt-in for demos, but
        // note: the CSR 2026-09-08 review flagged authenticated-CORS-wildcard as
        // a High finding — this stack no longer produces it unless the operator
        // deliberately sets the SSM parameter to `*`.
        allowOrigins: [allowedOrigin],
        allowMethods: apigw.Cors.ALL_METHODS,
        allowHeaders: ['Content-Type', 'Authorization'],
      },
      deployOptions: {
        stageName: props.stage,
        loggingLevel: apigw.MethodLoggingLevel.INFO,
        dataTraceEnabled: false,
        // Stage-level throttle — this IS the enforced limit for all callers.
        // Values updated from the previous 100/50 to match the documented
        // 500/200 design intent (decisions.md Q9 + security-review Cycle 6 W3).
        // The UsagePlan below was inert (no API keys attached) and has been
        // removed; stage-level MethodSettings is the correct mechanism for
        // throttling requests that don't require API keys.
        //
        // Sizing (see VsaApiStackProps):
        //   80 concurrent visitors × burst ~5 req/s = 400 req/s peak.
        //   2× headroom → 500 rate / 200 burst. At this rate a stolen 5-min
        //   kiosk access token drives 500 rps × 300 s × $0.02/turn ≈ $3,000
        //   max against /assistant/chat — bounded by the Budgets alarm below.
        //   Throttling real visitors is the WORSE outcome; 500/200 comfortably
        //   exceeds the 60-80 visitors/hour design point.
        throttlingBurstLimit: props.throttleBurstLimit ?? 200,
        throttlingRateLimit: props.throttleRateLimit ?? 500,
      },
    });
    for (const respType of [apigw.ResponseType.DEFAULT_4XX, apigw.ResponseType.DEFAULT_5XX]) {
      this.restApi.addGatewayResponse(`Gw${respType.responseType}`, {
        type: respType,
        responseHeaders: {
          // API Gateway response mapping values must be surrounded by single quotes
          // at the CFN level. `allowedOrigin` here is a string (either a literal or
          // a CFN token from SSM); the template-literal produces the required
          // `'<origin>'` shape after CFN token resolution.
          //
          // Same scoping decision as the preflight above — see JSDoc.
          'Access-Control-Allow-Origin': `'${allowedOrigin}'`,
          'Access-Control-Allow-Headers': "'Content-Type,Authorization'",
          'Access-Control-Allow-Methods': "'GET,POST,PUT,DELETE,OPTIONS'",
        },
      });
    }

    // --- Per-tenant Cognito pools -------------------------------------------
    // The `voiceRuntimeArnPattern` prop is what turns the tenant's user pool
    // into a full identity pool + authenticated role combo. Wildcard on the
    // runtime name suffix so redeploys that mint a new suffix don't require
    // a stack update. The runtime itself is deployed separately via the
    // AgentCore CLI — see `.bedrock_agentcore.yaml`.
    // ─── Shared CMS-pool voice-auth trust ─────────────────────────────────
    //
    // Computed once, before the tenant pools, so the pair is validated in one
    // place rather than inside a conditional expression per tenant. Both halves
    // must be concrete and correctly shaped; a half-configured pair is a
    // configuration error rather than a silent fallback, because it means an
    // operator intended the trust and it will not exist.
    const voiceAuthPoolOk = isResolvedUserPoolId(props.voiceAuthUserPoolId);
    const voiceAuthClientOk = isResolvedUserPoolClientId(props.voiceAuthUserPoolClientId);
    const voiceAuthTrust =
      voiceAuthPoolOk && voiceAuthClientOk
        ? [{
            userPoolId: props.voiceAuthUserPoolId!.trim(),
            userPoolClientId: props.voiceAuthUserPoolClientId!.trim(),
          }]
        : [];

    if (!voiceAuthPoolOk !== !voiceAuthClientOk) {
      // Exactly one of the pair is usable. Falling back to CVX-pool-only trust
      // is safe, but silence here would mean an iOS client that signs into the
      // shared CMS pool fails the AgentCore credential exchange with nothing
      // pointing at the cause.
      console.warn(
        `⚠️  voiceAuthUserPoolId / voiceAuthUserPoolClientId are half-configured ` +
          `(pool ${voiceAuthPoolOk ? 'ok' : 'missing or malformed'}, client ` +
          `${voiceAuthClientOk ? 'ok' : 'missing or malformed'}). The shared CMS-pool ` +
          `voice-auth trust was NOT added — voice credential exchange will fall back ` +
          `to CVX-pool-only trust. Set both, or set both to "none".`,
      );
    }

    this.tenantPools = {
      "meridian-fleet": new TenantPool(this, 'PoolMeridianFleet', {
        stage: props.stage, tenantId: 'meridian-fleet', displayName: 'Meridian Fleet Services',
        voiceRuntimeArnPattern:
          `arn:aws:bedrock-agentcore:${cdk.Stack.of(this).region}:${cdk.Stack.of(this).account}:runtime/vsa_supervisor_bidi*`,
        // Trust the consolidated CMS UI pool (what iOS signs into) for the
        // voice credential exchange, when configured. Both id + client id
        // required; otherwise falls back to CVX-pool-only trust.
        //
        // GATED ON SHAPE, NOT ON TRUTHINESS — do not simplify this back to
        // `props.voiceAuthUserPoolId && props.voiceAuthUserPoolClientId`. Both
        // props are plain strings, and every non-empty string is truthy: a CDK
        // token (were these fields ever re-tiered to SSM), a CloudFormation
        // dynamic reference passed through an env var, or a typo would all
        // satisfy that test and register a *trusted identity provider* that
        // resolves to nothing. This is the same trap as
        // `CvxAgentCoreMemoryKmsDecrypt` in vsa-core-stack.ts, and adding a
        // bogus trust to an auth path is the more dangerous of the two.
        //
        // `none` in either field means "this stage has no shared CMS pool" and
        // resolves to '' in bin/vsa.ts, so it lands in the `[]` arm here.
        // Spec: 2026-08-05-cvx-stage-defaults-parameterization, correction 3.
        additionalVoiceAuthUserPools: voiceAuthTrust,
        // Zone-2 kiosk identity surface (spec `2026-07-31-zone2-order-backend-and-kiosk`).
        // enableKioskGroup MUST be `true` here for the kiosk Cognito group, IAM role, and
        // app client to appear in the synthesized template. Without this the kiosk identity
        // is dead code that never deploys. See decisions.md Q14: "verify effects, not existence."
        //
        // vsaApiId scopes the kiosk role's execute-api:Invoke to this specific REST API
        // (security-review Cycle 6 W2). The kiosk credential lives on a tablet the public
        // physically handles; a wildcard `*:*:*` grant would allow a stolen refresh token
        // to call any API Gateway in any region/account. Threading the API id here narrows
        // the blast radius to the six /acquire/* routes the kiosk actually needs.
        //
        // CDK dependency: this.restApi is constructed above (intra-stack) so
        // this.restApi.restApiId is a valid Ref token at synthesis time.
        enableKioskGroup: true,
        vsaApiId: this.restApi.restApiId,
      }),
    };
    const allPools = Object.values(this.tenantPools).map(p => p.userPool);
    const poolInfos = Object.entries(this.tenantPools).map(([tid, p]) => ({
      tenantId: tid, userPoolId: p.userPool.userPoolId,
    }));

    // Externally-owned pools that should also be trusted by this API's
    // Cognito authorizer (e.g. the CMS UI's pool — see VsaApiStackProps
    // docstring). Imported as `IUserPool` references; CDK won't try to
    // mutate or recreate them, only feed the ARN into the authorizer.
    const extraPools = (props.extraCognitoUserPoolIds ?? [])
      .map(id => id.trim())
      .filter(id => id.length > 0)
      .map((id, i) => cognito.UserPool.fromUserPoolId(this, `ExtraUserPool${i}`, id));
    const authorizerPools = [...allPools, ...extraPools];

    const authorizer = new apigw.CognitoUserPoolsAuthorizer(this, 'Authz', {
      cognitoUserPools: authorizerPools,
      authorizerName: `vsa-${props.stage}-authz`,
    });

    // --- /triage ------------------------------------------------------------
    const apiTriage = new lambda.Function(this, 'ApiTriage', {
      functionName: `vsa-${props.stage}-api-triage`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      timeout: cdk.Duration.seconds(15),
      logRetention: logs.RetentionDays.ONE_MONTH,
      code: pythonLambdaAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'api-triage'),
      ),
      environment: {
        ALLOWED_ORIGIN: allowedOrigin,
        CLASSIFIER_FUNCTION_ARN: props.classifier.functionArn,
      },
    });
    props.classifier.grantInvoke(apiTriage);

    const triage = this.restApi.root.addResource('triage');
    triage.addMethod('POST',
      new apigw.LambdaIntegration(apiTriage, { allowTestInvoke: false }),
      { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
    );

    // --- /vehicles/{vehicleId}/service-history -----------------------------
    // Reads CMS-side ${cmsTable.serviceHistory} directly. Returns both
    // scheduled (upcoming) and completed rows so iOS can render both
    // sections in a single network call.
    const apiServiceHistory = new lambda.Function(this, 'ApiServiceHistory', {
      functionName: `vsa-${props.stage}-api-service-history`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      timeout: cdk.Duration.seconds(10),
      // 512 MB so DynamoDB scan/query CPU isn't throttled. At the
      // 128 MB default, drivers-me / vehicle-context / service-history
      // / trips were each spending 2.5–3 s on every Home tab load —
      // dashboard would just spin until ~12s of serial RTTs finished.
      // 512 MB ≈ 4× CPU and drops per-call to <500 ms.
      memorySize: 512,
      logRetention: logs.RetentionDays.ONE_MONTH,
      code: pythonLambdaAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'api-vehicle-service-history'),
      ),
      environment: {
        ALLOWED_ORIGIN: allowedOrigin,
        CMS_SERVICE_HISTORY_TABLE: cmsTable.serviceHistory,
      },
    });
    // Read from CMS service-history + decrypt via the CMS CMK. Scoped to the
    // specific table + via-service condition on KMS so the grant can't be
    // used against unrelated resources.
    apiServiceHistory.addToRolePolicy(new iam.PolicyStatement({
      sid: 'CmsServiceHistoryRead',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:Query', 'dynamodb:GetItem', 'dynamodb:DeleteItem'],
      resources: [
        `arn:aws:dynamodb:${this.region}:${this.account}:table/${cmsTable.serviceHistory}`,
        `arn:aws:dynamodb:${this.region}:${this.account}:table/${cmsTable.serviceHistory}/index/*`,
      ],
    }));
    apiServiceHistory.addToRolePolicy(new iam.PolicyStatement({
      sid: 'CmsKmsAccessForSseTable',
      effect: iam.Effect.ALLOW,
      actions: ['kms:Decrypt', 'kms:DescribeKey'],
      resources: [cmsKmsKeyArn],
      conditions: {
        StringEquals: { 'kms:ViaService': `dynamodb.${this.region}.amazonaws.com` },
      },
    }));

    const vehicles = this.restApi.root.addResource('vehicles');
    const vehicle = vehicles.addResource('{vehicleId}');
    const serviceHistory = vehicle.addResource('service-history');
    serviceHistory.addMethod('GET',
      new apigw.LambdaIntegration(apiServiceHistory, { allowTestInvoke: false }),
      { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
    );
    // DELETE → cleanup endpoint used by iOS Reset Demo button. Removes
    // only rows tagged source=voice-assistant for this vehicle, leaving
    // seeded historical service untouched. Same Cognito auth as GET.
    serviceHistory.addMethod('DELETE',
      new apigw.LambdaIntegration(apiServiceHistory, { allowTestInvoke: false }),
      { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
    );

    // --- /vehicles/{vehicleId}/context -------------------------------------
    // Returns vehicle + assigned driver in one call. iOS uses this at
    // voice-session open; backend will eventually use it to enrich the
    // voice prompt with real make/model/driver-name context.
    const apiVehicleContext = new lambda.Function(this, 'ApiVehicleContext', {
      functionName: `vsa-${props.stage}-api-vehicle-context`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      timeout: cdk.Duration.seconds(10),
      memorySize: 512, // see ApiServiceHistory note
      logRetention: logs.RetentionDays.ONE_MONTH,
      code: pythonLambdaAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'api-vehicle-context'),
      ),
      environment: {
        ALLOWED_ORIGIN: allowedOrigin,
        CMS_VEHICLES_TABLE: cmsTable.vehicles,
        CMS_DRIVERS_TABLE: cmsTable.drivers,
        CMS_DTC_HISTORY_TABLE: cmsTable.dtcHistory,
        // serviceHistory is read by the health-score helper to apply
        // the "scheduled service overdue" deduction. The handler also
        // accepts CMS_SERVICE_HISTORY_TABLE via env so the table name
        // tracks cmsStage without code changes.
        CMS_SERVICE_HISTORY_TABLE: cmsTable.serviceHistory,
      },
    });
    apiVehicleContext.addToRolePolicy(new iam.PolicyStatement({
      sid: 'CmsVehicleDriverRead',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:GetItem', 'dynamodb:Scan', 'dynamodb:Query'],
      resources: [
        `arn:aws:dynamodb:${this.region}:${this.account}:table/${cmsTable.vehicles}`,
        `arn:aws:dynamodb:${this.region}:${this.account}:table/${cmsTable.drivers}`,
        // dtc-history is queried by the Lambda to populate
        // `activeDtcs` in the response (used by iOS Home tab's
        // vehicle health score and Alerts tab's faults section).
        // GSI ARN included even though we don't currently use a
        // secondary index, so adding one later doesn't require a
        // policy update.
        `arn:aws:dynamodb:${this.region}:${this.account}:table/${cmsTable.dtcHistory}`,
        `arn:aws:dynamodb:${this.region}:${this.account}:table/${cmsTable.dtcHistory}/index/*`,
        // service-history is queried by the health-score helper
        // (_load_first_scheduled_service) to pull the soonest
        // scheduled row and apply a deduction when it's overdue.
        // GSI ARN included for the same reason as dtc-history above.
        `arn:aws:dynamodb:${this.region}:${this.account}:table/${cmsTable.serviceHistory}`,
        `arn:aws:dynamodb:${this.region}:${this.account}:table/${cmsTable.serviceHistory}/index/*`,
      ],
    }));
    apiVehicleContext.addToRolePolicy(new iam.PolicyStatement({
      sid: 'CmsKmsAccessForSseTable',
      effect: iam.Effect.ALLOW,
      actions: ['kms:Decrypt', 'kms:DescribeKey'],
      resources: [cmsKmsKeyArn],
      conditions: {
        StringEquals: { 'kms:ViaService': `dynamodb.${this.region}.amazonaws.com` },
      },
    }));

    const vehicleContext = vehicle.addResource('context');
    vehicleContext.addMethod('GET',
      new apigw.LambdaIntegration(apiVehicleContext, { allowTestInvoke: false }),
      { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
    );

    // --- /vehicles/{vehicleId}/trips ---------------------------------------
    // Recent trips for a vehicle via the vehicleId-startTime-index GSI on
    // ${cmsTable.trips}. Route arrays are stripped from each item for
    // response-size sanity.
    const apiVehicleTrips = new lambda.Function(this, 'ApiVehicleTrips', {
      functionName: `vsa-${props.stage}-api-vehicle-trips`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      timeout: cdk.Duration.seconds(10),
      memorySize: 512, // see ApiServiceHistory note
      logRetention: logs.RetentionDays.ONE_MONTH,
      code: pythonLambdaAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'api-vehicle-trips'),
      ),
      environment: {
        ALLOWED_ORIGIN: allowedOrigin,
        CMS_TRIPS_TABLE: cmsTable.trips,
        CMS_TRIPS_GSI: 'vehicleId-startTime-index',
      },
    });
    apiVehicleTrips.addToRolePolicy(new iam.PolicyStatement({
      sid: 'CmsTripsRead',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:Query', 'dynamodb:GetItem'],
      resources: [
        `arn:aws:dynamodb:${this.region}:${this.account}:table/${cmsTable.trips}`,
        `arn:aws:dynamodb:${this.region}:${this.account}:table/${cmsTable.trips}/index/*`,
      ],
    }));
    apiVehicleTrips.addToRolePolicy(new iam.PolicyStatement({
      sid: 'CmsKmsAccessForSseTable',
      effect: iam.Effect.ALLOW,
      actions: ['kms:Decrypt', 'kms:DescribeKey'],
      resources: [cmsKmsKeyArn],
      conditions: {
        StringEquals: { 'kms:ViaService': `dynamodb.${this.region}.amazonaws.com` },
      },
    }));

    const vehicleTrips = vehicle.addResource('trips');
    vehicleTrips.addMethod('GET',
      new apigw.LambdaIntegration(apiVehicleTrips, { allowTestInvoke: false }),
      { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
    );

    // --- /drivers/me -------------------------------------------------------
    // Resolves the signed-in Cognito user to their CMS driver + assigned
    // vehicle. Called by iOS once after sign-in.
    const apiDriversMe = new lambda.Function(this, 'ApiDriversMe', {
      functionName: `vsa-${props.stage}-api-drivers-me`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      timeout: cdk.Duration.seconds(10),
      memorySize: 512, // see ApiServiceHistory note
      logRetention: logs.RetentionDays.ONE_MONTH,
      code: pythonLambdaAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'api-drivers-me'),
      ),
      environment: {
        ALLOWED_ORIGIN: allowedOrigin,
        CMS_DRIVERS_TABLE: cmsTable.drivers,
        CMS_VEHICLES_TABLE: cmsTable.vehicles,
      },
    });
    apiDriversMe.addToRolePolicy(new iam.PolicyStatement({
      sid: 'CmsDriverResolve',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:Scan', 'dynamodb:GetItem'],
      resources: [
        `arn:aws:dynamodb:${this.region}:${this.account}:table/${cmsTable.drivers}`,
        `arn:aws:dynamodb:${this.region}:${this.account}:table/${cmsTable.vehicles}`,
      ],
    }));
    apiDriversMe.addToRolePolicy(new iam.PolicyStatement({
      sid: 'CmsKmsAccessForSseTable',
      effect: iam.Effect.ALLOW,
      actions: ['kms:Decrypt', 'kms:DescribeKey'],
      resources: [cmsKmsKeyArn],
      conditions: {
        StringEquals: { 'kms:ViaService': `dynamodb.${this.region}.amazonaws.com` },
      },
    }));

    const drivers = this.restApi.root.addResource('drivers');
    const driversMe = drivers.addResource('me');
    driversMe.addMethod('GET',
      new apigw.LambdaIntegration(apiDriversMe, { allowTestInvoke: false }),
      { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
    );

    // --- /vehicles/{vehicleId}/live-state ----------------------------------
    // Reads the same Redis hash (vehicle:{id}:meta) the CMS main_api Lambda
    // reads, so iOS can see the same realtime connection state the CMS UI
    // shows. Sits inside the CMS VPC because Redis has no public endpoint.
    //
    // VPC note: we reuse the CMS Redis security group directly, matching
    // how the CMS main_api Lambda is configured. That SG allows ingress
    // from itself on 6379, so no new rules are needed. If ownership of
    // that SG is ever split, migrate this to a dedicated VFO SG with an
    // explicit cross-SG ingress rule on 6379.
    //
    // VPC ID and SG ID are stage-scoped — wired from `STAGE_DEFAULTS`
    // in `bin/vsa.ts` so prod (us-east-1) and staging (us-west-2) can
    // each point at their own CMS VPC. Construct IDs (`CmsVpc`,
    // `CmsRedisSg`) are kept stable so changing the *value* doesn't
    // force a logical-ID-driven CloudFormation replacement.
    const cmsVpc = ec2.Vpc.fromLookup(this, 'CmsVpc', {
      vpcId: props.vpcId,
    });
    const cmsRedisSg = ec2.SecurityGroup.fromSecurityGroupId(
      this, 'CmsRedisSg', props.redisSecurityGroupId,
      { mutable: false },
    );

    // ── Optional TLS + AUTH on the Redis wire (2026-09-16) ──────────────────
    //
    // Both are opt-in via CDK context and both default OFF, because they are
    // inert until CMS enables the matching cluster-side setting
    // (`transitEncryptionEnabled=true`, `authToken`). See the DMS sibling
    // `~/guidance-for-dealer-management-system-on-aws/issues/
    // 2026-09-16-dms-redis-cert-cache-no-tls-no-auth/summary.md` for the
    // coordinated rollout and the same-shape client-side change.
    //
    // Context values (both string; presence is what matters):
    //   `cmsRedisTlsEnabled` — set to `"true"` (case-insensitive) to upgrade
    //     the wire to TLS with server-cert verification.
    //   `cmsRedisAuthTokenSecretArn` — Secrets Manager ARN whose SecretString
    //     is the raw AUTH token. Never pass the token itself as context —
    //     Lambda env vars are visible to any principal with
    //     `lambda:GetFunctionConfiguration` per
    //     `~/.kiro/steering/secrets-handling.md` § "Runtime launch parameters".
    const cmsRedisTlsEnabled = this.node.tryGetContext('cmsRedisTlsEnabled');
    const cmsRedisAuthTokenSecretArn = this.node.tryGetContext('cmsRedisAuthTokenSecretArn');
    const redisTlsAuthEnv: { [key: string]: string } = {};
    if (cmsRedisTlsEnabled) {
      redisTlsAuthEnv.REDIS_TLS_ENABLED = String(cmsRedisTlsEnabled);
    }
    if (cmsRedisAuthTokenSecretArn) {
      redisTlsAuthEnv.REDIS_AUTH_TOKEN_SECRET_ARN = String(cmsRedisAuthTokenSecretArn);
    }

    const apiVehicleLiveState = new lambda.Function(this, 'ApiVehicleLiveState', {
      functionName: `vsa-${props.stage}-api-vehicle-live-state`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      timeout: cdk.Duration.seconds(6),
      memorySize: 256,
      logRetention: logs.RetentionDays.ONE_MONTH,
      code: pythonLambdaAsset(
        path.join(__dirname, '..', 'build', 'api-vehicle-live-state'),
      ),
      vpc: cmsVpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [cmsRedisSg],
      environment: {
        ALLOWED_ORIGIN: allowedOrigin,
        REDIS_ENDPOINT: redisEndpoint,
        REDIS_PORT: '6379',
        CMS_VEHICLES_TABLE: cmsTable.vehicles,
        ...redisTlsAuthEnv,
      },
    });
    // Scoped grant: only when the operator supplied a specific secret ARN.
    // Do NOT broaden to `*` even for convenience — if the ARN is mistyped or
    // wildcarded, the fetch fails at call time (fail-open in `_fetch_auth_token`)
    // rather than succeeding against an unintended secret.
    if (cmsRedisAuthTokenSecretArn) {
      apiVehicleLiveState.addToRolePolicy(new iam.PolicyStatement({
        sid: 'ReadRedisAuthTokenSecret',
        effect: iam.Effect.ALLOW,
        actions: ['secretsmanager:GetSecretValue'],
        resources: [String(cmsRedisAuthTokenSecretArn)],
      }));
    }
    apiVehicleLiveState.addToRolePolicy(new iam.PolicyStatement({
      sid: 'CmsVehiclesRead',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:GetItem'],
      resources: [
        `arn:aws:dynamodb:${this.region}:${this.account}:table/${cmsTable.vehicles}`,
      ],
    }));
    apiVehicleLiveState.addToRolePolicy(new iam.PolicyStatement({
      sid: 'CmsKmsAccessForSseTable',
      effect: iam.Effect.ALLOW,
      actions: ['kms:Decrypt', 'kms:DescribeKey'],
      resources: [cmsKmsKeyArn],
      conditions: {
        StringEquals: { 'kms:ViaService': `dynamodb.${this.region}.amazonaws.com` },
      },
    }));

    const vehicleLiveState = vehicle.addResource('live-state');
    vehicleLiveState.addMethod('GET',
      new apigw.LambdaIntegration(apiVehicleLiveState, { allowTestInvoke: false }),
      { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
    );

    // --- /vehicles/{vehicleId}/safety-events -------------------------------
    // Powers the iOS Alerts tab. Queries CMS safety events via the
    // `vehicleId-timestamp-index` GSI (lookback `days` + `limit` from the
    // query string). Handler is self-contained (no shared deps), so the
    // asset is the raw `lambdas/` dir like api-vehicle-context. Previously
    // the handler existed but was never wired into the API — the iOS Alerts
    // tab got a 4xx ("couldn't load safety events"); issue
    // cms/2026-06-22-ios-alerts-tab-infinite-spinner.
    const apiVehicleSafetyEvents = new lambda.Function(this, 'ApiVehicleSafetyEvents', {
      functionName: `vsa-${props.stage}-api-vehicle-safety-events`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      timeout: cdk.Duration.seconds(10),
      memorySize: 256,
      logRetention: logs.RetentionDays.ONE_MONTH,
      code: pythonLambdaAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'api-vehicle-safety-events'),
      ),
      environment: {
        ALLOWED_ORIGIN: allowedOrigin,
        CMS_SAFETY_EVENTS_TABLE: cmsTable.safetyEvents,
        CMS_SAFETY_EVENTS_GSI: 'vehicleId-timestamp-index',
        CMS_STAGE: props.cmsStage,
      },
    });
    apiVehicleSafetyEvents.addToRolePolicy(new iam.PolicyStatement({
      sid: 'CmsSafetyEventsRead',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:Query', 'dynamodb:GetItem'],
      resources: [
        `arn:aws:dynamodb:${this.region}:${this.account}:table/${cmsTable.safetyEvents}`,
        `arn:aws:dynamodb:${this.region}:${this.account}:table/${cmsTable.safetyEvents}/index/*`,
      ],
    }));
    apiVehicleSafetyEvents.addToRolePolicy(new iam.PolicyStatement({
      sid: 'CmsKmsAccessForSseTable',
      effect: iam.Effect.ALLOW,
      actions: ['kms:Decrypt', 'kms:DescribeKey'],
      resources: [cmsKmsKeyArn],
      conditions: {
        StringEquals: { 'kms:ViaService': `dynamodb.${this.region}.amazonaws.com` },
      },
    }));

    const vehicleSafetyEvents = vehicle.addResource('safety-events');
    vehicleSafetyEvents.addMethod('GET',
      new apigw.LambdaIntegration(apiVehicleSafetyEvents, { allowTestInvoke: false }),
      { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
    );

    // ── api-vehicle-context invokes api-vehicle-live-state ───────────────
    // The context Lambda's health-score helper uses the canonical
    // Redis-backed connectivity signal (api-vehicle-live-state). Grant
    // the invoke permission and pass the function name via env var so
    // the helper's `_load_live_connection_state` can resolve the target
    // without hardcoding it. This keeps iOS, CMS UI, and the health
    // score on a single source of truth for "is this vehicle live
    // right now?" — the previous DDB-only path lagged in both
    // directions and the lastSeenAt-based freshness override produced
    // false positives (vehicle had recent telemetry but actually
    // disconnected).
    apiVehicleLiveState.grantInvoke(apiVehicleContext);
    apiVehicleContext.addEnvironment('LIVE_STATE_FN_NAME', apiVehicleLiveState.functionName);

    // --- /tenants/{tenantId}/config ----------------------------------------
    const apiConfig = new lambda.Function(this, 'ApiConfig', {
      functionName: `vsa-${props.stage}-api-tenant-config`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      timeout: cdk.Duration.seconds(5),
      logRetention: logs.RetentionDays.ONE_MONTH,
      code: pythonLambdaAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'api-tenant-config'),
      ),
      environment: {
        ALLOWED_ORIGIN: allowedOrigin,
        TENANT_CONFIG_TABLE: props.tenantConfigTable.tableName,
      },
    });
    props.tenantConfigTable.grantReadData(apiConfig);

    const tenants = this.restApi.root.addResource('tenants');
    const tenant = tenants.addResource('{tenantId}');
    const config = tenant.addResource('config');
    config.addMethod('GET',
      new apigw.LambdaIntegration(apiConfig, { allowTestInvoke: false }),
      { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
    );

    // --- /escalate ---------------------------------------------------------
    // Hands a driver off to a human fleet-support agent via Amazon Connect
    // chat. Auto-called by the supervisor runtime on P0 triage, or
    // driver-confirmed for P1. Reads tenant Connect config from
    // vsa-prod-tenant-config so the same Lambda works for every tenant.
    //
    // For P0 contacts the Lambda also writes a REQUESTED row to the RSA
    // dispatch table (vsa-{stage}-rsa-dispatch) — see spec R1 and Task 2.4
    // in .kiro/specs/2026-09-23-cvx-rsa-stub-dispatch/tasks.md.
    // Write is best-effort; failure doesn't block the chat.
    //
    // Asset path: infrastructure/build/api-escalate/ — pre-staged by
    // scripts/build-rsa-lambda.sh, which copies shared/rsa_client*.py into
    // the bundle so `from shared.rsa_client import ...` resolves at cold start.
    const triageDecisionsTableName = `vsa-${props.stage}-triage-decisions`;
    // Review Cycle 4 C4W5: the staged bundle must equal its source, or a
    // narrow `cdk deploy` ships stale code while reporting success.
    assertStagedCopyMatchesSource({
      buildDir: path.join(__dirname, '..', 'build', 'api-escalate'),
      sourceDir: path.join(__dirname, '..', '..', 'lambdas', 'api-escalate'),
      sharedDir: path.join(__dirname, '..', '..', 'lambdas', 'shared'),
      sharedFiles: ['rsa_client.py', 'rsa_client_fake.py', 'rsa_client_real.py'],
      restageCommand: 'bash scripts/build-rsa-lambda.sh',
    });
    const apiEscalate = new lambda.Function(this, 'ApiEscalate', {
      functionName: `vsa-${props.stage}-api-escalate`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      timeout: cdk.Duration.seconds(15),
      logRetention: logs.RetentionDays.ONE_MONTH,
      code: pythonLambdaAsset(
        // Build directory pre-assembled by scripts/build-rsa-lambda.sh. Contains
        // handler.py + shared/rsa_client*.py so `from shared.rsa_client import ...`
        // resolves at Lambda cold start. A raw `lambdas/api-escalate` asset cannot
        // work: Code.fromAsset bundles one directory and cannot reach a sibling, so
        // the import raised ModuleNotFoundError while every unit test passed (pytest
        // runs from the repo root, where the import genuinely resolves). See Task 2.5.
        //
        // Routed through pythonLambdaAsset, not a bare lambda.Code.fromAsset: the
        // helper carries PYTHON_ASSET_EXCLUDES, without which __pycache__/.pyc and
        // nested tests/ re-enter the bundle and move its asset hash on every local
        // pytest run (issues/2026-09-21-cdk-asset-hash-drift-unrelated-lambdas/).
        path.join(__dirname, '..', 'build', 'api-escalate'),
      ),
      environment: {
        ALLOWED_ORIGIN: allowedOrigin,
        TENANT_CONFIG_TABLE: props.tenantConfigTable.tableName,
        // RSA_DISPATCH_TABLE: threaded from VsaRsaStack so the env var and the
        // IAM grant below reference the same table. No default — missing value
        // fails loudly at Lambda init rather than writing to a wrong table.
        // See decisions.md "Group 2 had a missing task — the escalate Lambda's
        // env + IAM rewiring" for the original defect class this task fixes.
        RSA_DISPATCH_TABLE: props.rsaDispatchTable.tableName,
        CHAT_DURATION_MINUTES: '60',
        // F5.D1 (Option D): direct invoke of api-vehicle-live-state for position
        // resolution before writing the RSA dispatch row. Same pattern as
        // api-vehicle-context → api-vehicle-live-state at :854-856.
        // LIVE_STATE_FN_NAME: threaded so the handler can resolve the target
        // without hardcoding a function name; see handler.py _LIVE_STATE_FN_NAME.
        LIVE_STATE_FN_NAME: apiVehicleLiveState.functionName,
        // RSA_POSITION_MAX_AGE_SECONDS: freshness gate for the live-state lookup.
        // Default 900 (15 min) — deliberately more tolerant than live-state's own
        // 5-min "connected" threshold. Policy choice; see props JSDoc.
        RSA_POSITION_MAX_AGE_SECONDS: rsaPositionMaxAgeSeconds,
        // Security review W1 (spec 2026-09-23-cvx-escalation-recommend-not-act):
        // a roadside dispatch is honoured only when triageSessionId names a
        // recent P0 row in this table for the caller's tenant + VIN. The row is
        // written by the classifier Lambda or the supervisor's driver-confirmation
        // path — never by a client. Name derived from the core stack's
        // `${p}-triage-decisions` convention so bin/vsa.ts needs no new prop.
        TRIAGE_DECISIONS_TABLE: triageDecisionsTableName,
        // P0 side effects (spec 2026-09-23-cvx-escalation-recommend-not-act,
        // 2026-09-24): CMS_VEHICLES_TABLE binds the body's vehicleId to its VIN
        // before any dispatch or callback; CMS_DRIVERS_TABLE resolves the
        // assigned driver's phone for the callback and the RSA row. Without
        // these the handler defaulted to cms-dev-storage-drivers and the
        // callback silently never fired.
        CMS_VEHICLES_TABLE: cmsTable.vehicles,
        CMS_DRIVERS_TABLE: cmsTable.drivers,
      },
    });
    props.tenantConfigTable.grantReadData(apiEscalate);

    // F5.D1 (Option D): grant escalate invoke permission on the live-state Lambda.
    // Direct invoke is IAM-authorized — no JWT plumbing needed. The live-state
    // handler already supports direct invocation (api-vehicle-live-state/handler.py:329
    // "the same handler works whether invoked via API [Gateway] or direct").
    // Mirror of api-vehicle-context → api-vehicle-live-state at :854-856.
    apiVehicleLiveState.grantInvoke(apiEscalate);

    // Connect chat. Scoped to the current account/region; the Lambda
    // picks the specific instance per-tenant from config. Connect's
    // StartChatContact requires both connect:StartChatContact (on the
    // instance + contact-flow resources) and implicit permissions it
    // enforces on the contact flow itself.
    apiEscalate.addToRolePolicy(new iam.PolicyStatement({
      sid: 'ConnectStartChatContact',
      effect: iam.Effect.ALLOW,
      actions: ['connect:StartChatContact'],
      // Connect instances are regional and resolved per-tenant from
      // tenant-config (currently us-east-1), which is NOT necessarily this
      // stack's region (us-west-2). The handler targets the instance's own
      // region (parsed from its ARN), so this grant must not be pinned to
      // `${this.region}` — that pinning caused StartChatContact to fail with
      // "Resource not found of Type: INSTANCE" against the us-east-1 instance
      // (issue 2026-06-22-escalate-connect-region-mismatch). The region
      // wildcard keeps the grant account- and resource-scoped while allowing
      // any tenant's instance region. Codifies the CVX-escalate-connect-us-east-1
      // inline policy applied live 2026-06-22.
      resources: [
        `arn:aws:connect:*:${this.account}:instance/*`,
        `arn:aws:connect:*:${this.account}:instance/*/contact-flow/*`,
      ],
    }));

    // RSA dispatch table write — PutItem on the new vsa-{stage}-rsa-dispatch
    // table, threaded from VsaRsaStack via props.rsaDispatchTable.
    //
    // The dead CmsActionQueueWrite grant on cms-{stage}-vfo-action-queue is
    // removed per spec R1: /escalate now writes to the RSA table, not the
    // retired VFO action queue. The escalate Lambda was its only consumer
    // (grep confirms); removing the grant eliminates standing privilege with
    // no caller. See decisions.md "Group 2 had a missing task" for the full
    // rationale, and the ADP_REGION comment two hundred lines below for the
    // same class of defect: fixing only the handler without fixing the IAM
    // converts a ResourceNotFoundException into an AccessDeniedException.
    apiEscalate.addToRolePolicy(new iam.PolicyStatement({
      sid: 'RsaDispatchTableWrite',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:PutItem'],
      resources: [props.rsaDispatchTable.tableArn],
    }));

    // Security W1: GetItem ONLY, on the one triage-decisions table, so the
    // dispatch gate can read the platform-recorded verdict. No Query/Scan,
    // no write — the escalate Lambda must never be able to mint the evidence
    // it checks.
    apiEscalate.addToRolePolicy(new iam.PolicyStatement({
      sid: 'TriageDecisionRead',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:GetItem'],
      resources: [`arn:aws:dynamodb:${this.region}:${this.account}:table/${triageDecisionsTableName}`],
    }));

    // GetItem ONLY on the two CMS rows the P0 side effects read: the vehicle
    // (vehicleId -> vin binding) and the driver (assignment + phone). Both are
    // SSE-KMS; decrypt is covered by CmsKmsAccessForSseTable below.
    apiEscalate.addToRolePolicy(new iam.PolicyStatement({
      sid: 'CmsVehicleDriverRead',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:GetItem'],
      resources: [
        `arn:aws:dynamodb:${this.region}:${this.account}:table/${cmsTable.vehicles}`,
        `arn:aws:dynamodb:${this.region}:${this.account}:table/${cmsTable.drivers}`,
      ],
    }));

    // P0 outbound callback. Resource shape is Connect's `contact` type,
    // `instance/{id}/contact/*`. Security W2: scoped to the ONE instance named by
    // /cvx/{stage}/connect-instance-arn, not every instance in the account. The
    // ARN carries its own region, so the cross-region case (tenant instances in
    // us-east-1, this stack in us-west-2) needs no wildcard.
    apiEscalate.addToRolePolicy(new iam.PolicyStatement({
      sid: 'ConnectStartOutboundVoiceContact',
      effect: iam.Effect.ALLOW,
      actions: ['connect:StartOutboundVoiceContact'],
      resources: [`${connectInstanceArn}/contact/*`],
    }));

    // KMS access for CMS SSE-KMS tables still needed. The handler reads
    // CMS_DRIVERS_TABLE (handler.py:_resolve_driver_phone) for callback
    // lookups — this is NOT the retired VFO action-queue table. The grant
    // is for CMS table access via DynamoDB service. The RSA dispatch table
    // has its own key and its own grant (RsaDispatchKmsAccess, below).
    // Verified: handler.py line ~570 reads _CMS_DRIVERS_TABLE; KMS access
    // cannot be removed.
    apiEscalate.addToRolePolicy(new iam.PolicyStatement({
      sid: 'CmsKmsAccessForSseTable',
      effect: iam.Effect.ALLOW,
      actions: ['kms:Decrypt', 'kms:DescribeKey', 'kms:GenerateDataKey'],
      resources: [cmsKmsKeyArn],
      conditions: {
        StringEquals: { 'kms:ViaService': `dynamodb.${this.region}.amazonaws.com` },
      },
    }));

    // Fix Group 13 item 2: KMS on the RSA dispatch table's own key, for the
    // RsaDispatchTableWrite PutItem above. Same actions and ViaService
    // condition as the RSA stack's consumer and advancer grants.
    apiEscalate.addToRolePolicy(new iam.PolicyStatement({
      sid: 'RsaDispatchKmsAccess',
      effect: iam.Effect.ALLOW,
      actions: ['kms:Decrypt', 'kms:DescribeKey', 'kms:GenerateDataKey'],
      resources: [props.rsaDispatchKey.keyArn],
      conditions: {
        StringEquals: { 'kms:ViaService': `dynamodb.${this.region}.amazonaws.com` },
      },
    }));

    const escalate = this.restApi.root.addResource('escalate');
    escalate.addMethod('POST',
      new apigw.LambdaIntegration(apiEscalate, { allowTestInvoke: false }),
      { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
    );

    // --- /rsa/dispatch/{dispatchId} (GET) ----------------------------------
    // Status read endpoint for a single RSA dispatch row.
    // Spec: .kiro/specs/2026-09-23-cvx-rsa-stub-dispatch/ Task 3.1
    //
    // Design (G3-D8/D9/D10):
    //   - Returns an allowlisted projection — vin, tenantId, failureReason,
    //     etc. are never returned.
    //   - Tenant-scoped: caller's custom:tenantId claim is compared to the
    //     row; absent/mismatch → 404 (same body as not-found, G3-D9).
    //   - VSA_RSA_CLIENT=fake explicit so simulated-fallback leans toward
    //     true in demos rather than null (G3-D7).
    //   - GetItem scoped to the exact RSA dispatch table ARN (G3-D10).
    //   - KMS: explicit kms:Decrypt + kms:DescribeKey with kms:ViaService
    //     condition on the RSA table's own key (G3-D10; Fix Group 13 item 2
    //     moved it off the CMS key).
    const apiRsaDispatch = new lambda.Function(this, 'ApiRsaDispatch', {
      functionName: `vsa-${props.stage}-api-rsa-dispatch`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      timeout: cdk.Duration.seconds(10),
      logRetention: logs.RetentionDays.ONE_MONTH,
      // Direct source-tree asset — no shared/ imports required, so no
      // pre-staging step is needed (unlike api-escalate).
      code: pythonLambdaAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'api-rsa-dispatch'),
      ),
      environment: {
        // RSA_DISPATCH_TABLE threaded from VsaRsaStack via props so the
        // env var and GetItem grant both reference the same table. Same
        // pattern as the escalate Lambda's RSA_DISPATCH_TABLE.
        RSA_DISPATCH_TABLE: props.rsaDispatchTable.tableName,
        // G3-D7: fake mode so the simulated-absent fallback is true in demos.
        VSA_RSA_CLIENT: 'fake',
        // F9.4: CORS header threaded the same way as every other API Lambda.
        ALLOWED_ORIGIN: allowedOrigin,
      },
    });

    // GetItem on the exact RSA dispatch table ARN (G3-D10).
    // Scoped to the table's primary-key lookup only — no Scan, no Query.
    apiRsaDispatch.addToRolePolicy(new iam.PolicyStatement({
      sid: 'RsaDispatchTableRead',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:GetItem'],
      resources: [props.rsaDispatchTable.tableArn],
    }));

    // KMS access for the key that encrypts the RSA dispatch table (G3-D10).
    // Fix Group 13 item 2: the table has its own key now, so this grant names
    // that key instead of the CMS key; this Lambda reads no CMS table.
    // Conditioned on kms:ViaService = dynamodb so the key cannot be used for
    // any other purpose — same pattern as every SSE-KMS table reader in this
    // stack (grep for CmsKmsAccessForSseTable for prior art).
    apiRsaDispatch.addToRolePolicy(new iam.PolicyStatement({
      sid: 'RsaDispatchKmsDecrypt',
      effect: iam.Effect.ALLOW,
      actions: ['kms:Decrypt', 'kms:DescribeKey'],
      resources: [props.rsaDispatchKey.keyArn],
      conditions: {
        StringEquals: { 'kms:ViaService': `dynamodb.${this.region}.amazonaws.com` },
      },
    }));

    const rsa = this.restApi.root.addResource('rsa');
    const rsaDispatch = rsa.addResource('dispatch');
    const rsaDispatchById = rsaDispatch.addResource('{dispatchId}');
    rsaDispatchById.addMethod('GET',
      new apigw.LambdaIntegration(apiRsaDispatch, { allowTestInvoke: false }),
      { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
    );

    // --- /find-service-center ----------------------------------------------
    // Native-app booking flow's "search" step. Mirrors the voice
    // agent's find_service_center tool — same DDB table, same
    // capability map, same persona-segment filter — so the iOS native
    // booking screen and Nova surface identical results. Logic is
    // duplicated rather than shared with the agent tool today; future
    // refactor should pull the haversine + capability map into a
    // shared module under lambdas/shared/.
    //
    // Reads adp-dev-service-network. The table name is hardcoded to
    // the ADP_STAGE=dev value because the demo deploys all three
    // personas against the same service-center catalog; per-tenant
    // catalogs would extend this with a stage prefix.
    const adpServiceNetworkTable = `adp-${process.env.ADP_STAGE || 'dev'}-service-network`;
    // ADP's tables are in us-east-1 while this stack deploys us-west-2, so both the
    // client and the IAM resource ARN need the ADP region explicitly. Same default as
    // agents/supervisor/tools/service_network.py.
    const adpRegion = process.env.ADP_REGION || 'us-east-1';
    const apiFindServiceCenter = new lambda.Function(this, 'ApiFindServiceCenter', {
      functionName: `vsa-${props.stage}-api-find-service-center`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      timeout: cdk.Duration.seconds(10),
      logRetention: logs.RetentionDays.ONE_MONTH,
      code: pythonLambdaAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'api-find-service-center'),
      ),
      environment: {
        ALLOWED_ORIGIN: allowedOrigin,
        ADP_SERVICE_NETWORK_TABLE: adpServiceNetworkTable,
        // The table is in ADP's region, not this stack's. Must be passed, and the IAM
        // grant below must use the same value — fixing only one converts a
        // ResourceNotFoundException into an AccessDeniedException.
        ADP_REGION: adpRegion,
      },
    });
    apiFindServiceCenter.addToRolePolicy(new iam.PolicyStatement({
      sid: 'AdpServiceNetworkRead',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:Scan', 'dynamodb:GetItem', 'dynamodb:Query'],
      resources: [
        `arn:aws:dynamodb:${adpRegion}:${this.account}:table/${adpServiceNetworkTable}`,
        `arn:aws:dynamodb:${adpRegion}:${this.account}:table/${adpServiceNetworkTable}/index/*`,
      ],
    }));

    const findServiceCenter = this.restApi.root.addResource('find-service-center');
    findServiceCenter.addMethod('POST',
      new apigw.LambdaIntegration(apiFindServiceCenter, { allowTestInvoke: false }),
      { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
    );

    // --- /book -------------------------------------------------------------
    // Native-app booking flow's "confirm" step. Persists a "scheduled"
    // row to cms-prod-storage-service-history matching what the voice
    // agent's book tool writes when Nova books via voice. Both flows
    // are surface-compatible — iOS can render either booking through
    // the same Service-tab list.
    const apiBook = new lambda.Function(this, 'ApiBook', {
      functionName: `vsa-${props.stage}-api-book`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      timeout: cdk.Duration.seconds(10),
      logRetention: logs.RetentionDays.ONE_MONTH,
      code: pythonLambdaAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'api-book'),
      ),
      environment: {
        ALLOWED_ORIGIN: allowedOrigin,
        CMS_SERVICE_HISTORY_TABLE: cmsTable.serviceHistory,
        CMS_VEHICLES_TABLE: cmsTable.vehicles,
      },
    });
    // Write to service-history (PutItem) and read vehicles for the
    // make/model/odometer enrichment that matches seeded historical
    // rows. Scoped to the specific tables; KMS access mirrors the
    // service-history Lambda's pattern (via-service condition).
    apiBook.addToRolePolicy(new iam.PolicyStatement({
      sid: 'CmsServiceHistoryWrite',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:PutItem', 'dynamodb:UpdateItem'],
      resources: [
        `arn:aws:dynamodb:${this.region}:${this.account}:table/${cmsTable.serviceHistory}`,
      ],
    }));
    apiBook.addToRolePolicy(new iam.PolicyStatement({
      sid: 'CmsVehiclesRead',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:GetItem'],
      resources: [
        `arn:aws:dynamodb:${this.region}:${this.account}:table/${cmsTable.vehicles}`,
      ],
    }));
    apiBook.addToRolePolicy(new iam.PolicyStatement({
      sid: 'CmsKmsAccessForBookingWrite',
      effect: iam.Effect.ALLOW,
      actions: ['kms:Decrypt', 'kms:Encrypt', 'kms:GenerateDataKey', 'kms:DescribeKey'],
      resources: [cmsKmsKeyArn],
      conditions: {
        StringEquals: { 'kms:ViaService': `dynamodb.${this.region}.amazonaws.com` },
      },
    }));

    const book = this.restApi.root.addResource('book');
    book.addMethod('POST',
      new apigw.LambdaIntegration(apiBook, { allowTestInvoke: false }),
      { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
    );

    // --- /assistant/chat ---------------------------------------------------
    // Proxy route that forwards a text prompt to the CVX AgentCore text
    // runtime (same-account, same-region) and returns the agent's response.
    // Auth: existing Cognito authorizer (already trusts the CMS pool via
    // EXTRA_USER_POOL_IDS — no new authorizer needed).
    const apiAssistantChat = new lambda.Function(this, 'ApiAssistantChat', {
      functionName: `vsa-${props.stage}-api-assistant-chat`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      timeout: cdk.Duration.seconds(30),  // AgentCore turn latency ~3-7s; 30s covers multi-tool turns
      memorySize: 256,
      logRetention: logs.RetentionDays.ONE_MONTH,
      code: pythonLambdaAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'api-assistant-chat'),
      ),
      environment: {
        ALLOWED_ORIGIN: allowedOrigin,
        CVX_RUNTIME_ARN: assistantChatRuntimeArn,
        CVX_RUNTIME_REGION: props.assistantChatRuntimeRegion,
        DEFAULT_TENANT_ID: 'meridian-fleet',
        DEFAULT_PERSONA_ID: 'fleet_driver',
        // Fix Group 18 (spec 2026-09-23-cvx-escalation-recommend-not-act): a caller
        // with a `custom:vehicleId` claim chats about that vehicle, and the handler
        // reads its VIN from this table (lambdas/api-assistant-chat/vehicle_scope.py).
        CMS_VEHICLES_TABLE: cmsTable.vehicles,
      },
    });
    // Least-privilege: single action, scoped to the specific runtime ARN
    // AND its endpoint sub-resources. AgentCore evaluates the action against
    // `<runtime-arn>/runtime-endpoint/<qualifier>` (e.g. `/runtime-endpoint/DEFAULT`),
    // not the bare runtime ARN — granting on the bare ARN alone yields
    // AccessDeniedException at invoke time. Mirrors the `voiceRuntimeArnPattern`
    // grant in tenant-pool.ts. Same-account, same-region — no AssumeRole or
    // resource policy required.
    apiAssistantChat.addToRolePolicy(new iam.PolicyStatement({
      sid: 'InvokeAssistantChatRuntime',
      effect: iam.Effect.ALLOW,
      actions: ['bedrock-agentcore:InvokeAgentRuntime'],
      resources: [
        assistantChatRuntimeArn,
        `${assistantChatRuntimeArn}/*`,
      ],
    }));
    // Fix Group 18: one GetItem on the CMS vehicles table (the claim vehicle's
    // VIN), nothing else. No Query/Scan and no index: the handler reads one row
    // by its key. KMS scoped to DynamoDB use because the table is SSE-KMS on the
    // CMS key. Test: infrastructure/test/vsa-api-stack-assistant-chat-vehicle.test.ts.
    apiAssistantChat.addToRolePolicy(new iam.PolicyStatement({
      sid: 'CmsVehiclesRead',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:GetItem'],
      resources: [
        `arn:aws:dynamodb:${this.region}:${this.account}:table/${cmsTable.vehicles}`,
      ],
    }));
    apiAssistantChat.addToRolePolicy(new iam.PolicyStatement({
      sid: 'CmsKmsAccessForSseTable',
      effect: iam.Effect.ALLOW,
      actions: ['kms:Decrypt', 'kms:DescribeKey'],
      resources: [cmsKmsKeyArn],
      conditions: {
        StringEquals: { 'kms:ViaService': `dynamodb.${this.region}.amazonaws.com` },
      },
    }));

    const assistant = this.restApi.root.addResource('assistant');
    assistant.addResource('chat').addMethod('POST',
      new apigw.LambdaIntegration(apiAssistantChat, { allowTestInvoke: false }),
      { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
    );

    // ── /acquire/* routes (spec `2026-07-31-zone2-order-backend-and-kiosk`) ──
    //
    // Six routes the iOS `AcquireCatalogClient.swift` is written against. All
    // behind the single shared Cognito authorizer already constructed above.
    //
    // POST /acquire/reservations uses the `reservationHandoffFn` exported by
    // AcquireStack — it runs the core order pipeline and starts the SFN state
    // machine; we must not create a duplicate.
    //
    // GET routes use per-route API Lambdas defined here following the same
    // `api-*` pattern used throughout this file.  They have their own
    // `api-acquire-{catalog,order,offers,leads}` handler directories and are
    // separate from the pipeline Lambdas in AcquireStack.
    //
    // check-api-routes.py reconstructs full resource paths from the CloudFormation
    // resource tree. Run `python3 scripts/check-api-routes.py --expect-acquire`
    // after synth to verify all six paths are present.

    // ── api-acquire-catalog ───────────────────────────────────────────────────
    // GET /acquire/catalog/{tenantId}
    // Queries the catalog table, returning categories/models/variants/colors/
    // accessories/interiors/ivePackages for the given tenant. tenantId in the
    // path is clamped to the caller's Cognito claim — 403 on mismatch.
    const apiAcquireCatalog = new lambda.Function(this, 'ApiAcquireCatalog', {
      functionName: `vsa-${props.stage}-api-acquire-catalog`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      timeout: cdk.Duration.seconds(28),
      memorySize: 256,
      logRetention: logs.RetentionDays.ONE_MONTH,
      code: pythonLambdaAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'api-acquire-catalog'),
      ),
      environment: {
        ALLOWED_ORIGIN: allowedOrigin,
        VSA_ACQUIRE_CATALOG_TABLE: props.acquireCatalogTable.tableName,
      },
    });
    apiAcquireCatalog.addToRolePolicy(new iam.PolicyStatement({
      sid: 'AcquireCatalogRead',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:GetItem', 'dynamodb:Query', 'dynamodb:BatchGetItem'],
      resources: [
        props.acquireCatalogTable.tableArn,
        `${props.acquireCatalogTable.tableArn}/index/*`,
      ],
    }));

    // ── api-acquire-order ─────────────────────────────────────────────────────
    // GET /acquire/orders/{orderId}
    // Returns the order item for the given orderId, scoped to the caller's
    // ownerSub claim (decisions.md Q8 — owner-match, not just tenant-match).
    const apiAcquireOrder = new lambda.Function(this, 'ApiAcquireOrder', {
      functionName: `vsa-${props.stage}-api-acquire-order`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      timeout: cdk.Duration.seconds(10),
      memorySize: 256,
      logRetention: logs.RetentionDays.ONE_MONTH,
      code: pythonLambdaAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'api-acquire-order'),
      ),
      environment: {
        ALLOWED_ORIGIN: allowedOrigin,
        VSA_ACQUIRE_ORDERS_TABLE: props.acquireOrdersTable.tableName,
      },
    });
    apiAcquireOrder.addToRolePolicy(new iam.PolicyStatement({
      sid: 'AcquireOrdersRead',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:GetItem'],
      resources: [props.acquireOrdersTable.tableArn],
    }));

    // ── api-acquire-offers ────────────────────────────────────────────────────
    // GET /acquire/offers/{modelId}
    // Reads OFFER# rows from the catalog table for the given modelId,
    // scoped to the caller's tenantId claim.
    const apiAcquireOffers = new lambda.Function(this, 'ApiAcquireOffers', {
      functionName: `vsa-${props.stage}-api-acquire-offers`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      timeout: cdk.Duration.seconds(10),
      memorySize: 256,
      logRetention: logs.RetentionDays.ONE_MONTH,
      code: pythonLambdaAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'api-acquire-offers'),
      ),
      environment: {
        ALLOWED_ORIGIN: allowedOrigin,
        VSA_ACQUIRE_CATALOG_TABLE: props.acquireCatalogTable.tableName,
      },
    });
    apiAcquireOffers.addToRolePolicy(new iam.PolicyStatement({
      sid: 'AcquireOffersRead',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:Query'],
      resources: [
        props.acquireCatalogTable.tableArn,
        `${props.acquireCatalogTable.tableArn}/index/*`,
      ],
    }));

    // ── api-acquire-leads ─────────────────────────────────────────────────────
    // POST /acquire/leads  — create a lead (gated on VSA_ACQUIRE_LEAD_CAPTURE_ENABLED)
    // GET  /acquire/leads/{leadId}  — read-back a lead by id
    const apiAcquireLeads = new lambda.Function(this, 'ApiAcquireLeads', {
      functionName: `vsa-${props.stage}-api-acquire-leads`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      timeout: cdk.Duration.seconds(10),
      memorySize: 256,
      logRetention: logs.RetentionDays.ONE_MONTH,
      code: pythonLambdaAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'api-acquire-leads'),
      ),
      environment: {
        ALLOWED_ORIGIN: allowedOrigin,
        VSA_ACQUIRE_LEADS_TABLE: props.acquireLeadsTable.tableName,
        // Feature flag — defaulting off; operator enables for show floor.
        // When off, POST /acquire/leads returns 404 regardless of body.
        VSA_ACQUIRE_LEAD_CAPTURE_ENABLED: 'false',
      },
    });
    apiAcquireLeads.addToRolePolicy(new iam.PolicyStatement({
      sid: 'AcquireLeadsReadWrite',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:Query'],
      resources: [
        props.acquireLeadsTable.tableArn,
        `${props.acquireLeadsTable.tableArn}/index/*`,
      ],
    }));

    // ── Route wiring ──────────────────────────────────────────────────────────
    const acquire = this.restApi.root.addResource('acquire');

    // GET /acquire/catalog/{tenantId}
    const acquireCatalogResource = acquire.addResource('catalog');
    acquireCatalogResource.addResource('{tenantId}').addMethod('GET',
      new apigw.LambdaIntegration(apiAcquireCatalog, { allowTestInvoke: false }),
      { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
    );

    // POST /acquire/reservations  — uses reservation_handoff from AcquireStack
    acquire.addResource('reservations').addMethod('POST',
      new apigw.LambdaIntegration(props.acquireReservationHandoffFn, { allowTestInvoke: false }),
      { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
    );

    // GET /acquire/orders/{orderId}
    const acquireOrdersResource = acquire.addResource('orders');
    acquireOrdersResource.addResource('{orderId}').addMethod('GET',
      new apigw.LambdaIntegration(apiAcquireOrder, { allowTestInvoke: false }),
      { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
    );

    // GET /acquire/offers/{modelId}
    const acquireOffersResource = acquire.addResource('offers');
    acquireOffersResource.addResource('{modelId}').addMethod('GET',
      new apigw.LambdaIntegration(apiAcquireOffers, { allowTestInvoke: false }),
      { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
    );

    // POST /acquire/leads  +  GET /acquire/leads/{leadId}
    const acquireLeadsResource = acquire.addResource('leads');
    acquireLeadsResource.addMethod('POST',
      new apigw.LambdaIntegration(apiAcquireLeads, { allowTestInvoke: false }),
      { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
    );
    acquireLeadsResource.addResource('{leadId}').addMethod('GET',
      new apigw.LambdaIntegration(apiAcquireLeads, { allowTestInvoke: false }),
      { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
    );

    // ── AVX API surface: /findings/* + /actions/* + /notifications/* ─────────
    //
    // Spec 1a Group 4 (G4.T5). Three Lambdas wired into the existing REST API
    // with the same Cognito authorizer used by all other routes. Table names
    // are resolved via Fn.importValue from VsaTier2Stack exports — same
    // Fn.importValue cycle-break pattern as acquireReservationHandoffFn.
    //
    // All four export-name props are optional; if any are absent (incremental
    // roll-out scenario) we emit a synth-time warning and skip this entire block.
    const avxLambdas: lambda.Function[] = [];
    if (
      props.avxFindingsTableNameExportName &&
      props.avxActionsTableNameExportName &&
      props.avxFeedbackTableNameExportName &&
      props.notificationDevicesTableNameExportName
    ) {
      // Resolve table names via CFN Fn.importValue. Each is a CFN-level token;
      // at deploy time CloudFormation substitutes the real table name.
      const avxFindingsTableName     = cdk.Fn.importValue(props.avxFindingsTableNameExportName);
      const avxActionsTableName      = cdk.Fn.importValue(props.avxActionsTableNameExportName);
      const avxFeedbackTableName     = cdk.Fn.importValue(props.avxFeedbackTableNameExportName);
      const notificationDevicesTableName = cdk.Fn.importValue(props.notificationDevicesTableNameExportName);

      // Reify ITable references from names for CDK grant methods.
      // Table.fromTableName only imports the L2 shell; it does NOT look up
      // the table at synth time — consistent with the Fn.importValue pattern.
      const avxFindingsTable = dynamodb.Table.fromTableName(
        this, 'AvxFindingsTableRef', avxFindingsTableName,
      );
      const avxActionsTable = dynamodb.Table.fromTableName(
        this, 'AvxActionsTableRef', avxActionsTableName,
      );
      const avxFeedbackTable = dynamodb.Table.fromTableName(
        this, 'AvxFeedbackTableRef', avxFeedbackTableName,
      );
      const notificationDevicesTable = dynamodb.Table.fromTableName(
        this, 'NotificationDevicesTableRef', notificationDevicesTableName,
      );

      // ── Lambda: api-avx-findings ─────────────────────────────────────────
      // Handles all /findings/* GET routes + POST /findings/{id}/approve.
      // A single Lambda handles multiple resources via event['resource'].
      const apiAvxFindings = new lambda.Function(this, 'ApiAvxFindings', {
        functionName: `vsa-${props.stage}-api-avx-findings`,
        runtime: lambda.Runtime.PYTHON_3_12,
        handler: 'handler.handler',
        // 30s, not 10s (Fix Group 35). POST /approve dispatches synchronously, and
        // agents/executors/base.py SYNCHRONOUS_BUDGET_S gives the executor's retry
        // loop 25s. A 10s timeout killed the function mid-dispatch, possibly after
        // the downstream work was done, with the Action left on pending_execution
        // and no receipt. 30s also outlives API Gateway's 29s integration limit, so
        // the function is usually still able to record the result after the HTTP
        // response has timed out. Only usually: the margin over the 25s budget is 5s,
        // and the boto3 clients keep their default connect/read timeouts and retries.
        timeout: cdk.Duration.seconds(30),
        memorySize: 256,
        logRetention: logs.RetentionDays.ONE_MONTH,
        code: pythonLambdaAsset(
          // Build directory pre-assembled by scripts/build-avx-lambda.sh.
          // Contains handler.py + _lib/avx_scope.py so `from _lib.avx_scope import ...`
          // resolves at Lambda cold start without the sys.path hack being necessary.
          // (sys.path.insert remains in handler.py for local-dev/test compatibility.)
          path.join(__dirname, '..', 'build', 'api-avx-findings'),
        ),
        environment: {
          AVX_STAGE:          props.stage,
          AVX_FINDINGS_TABLE: avxFindingsTableName,
          AVX_ACTIONS_TABLE:  avxActionsTableName,
          // Read by agents/executors/dms.py:67 when the approve route dispatches
          // target_system=dms. This Lambda bundles agents/executors/ (see
          // scripts/build-avx-lambda.sh :: bundle_executors), so the executor code runs
          // in THIS function's process and reads THIS env var — it is not a separate
          // service's configuration.
          VSA_DMS_API_ENDPOINT: dmsApiEndpoint,
          // Standing-claim trust attestations (see each prop's doc). Passed through as
          // strings, never tested at synth: a CFN token is always truthy. All three reach
          // this Lambda (api-avx-findings); see T3.1 in the spec. Customer-id and
          // vehicle-id trust reach ONLY this Lambda — they are NOT wired to api-avx-actions.
          // Fleet-ids trust is wired to BOTH this Lambda and api-avx-actions.
          ...(props.avxFleetIdsClaimTrusted
            ? {
                AVX_FLEET_IDS_CLAIM_TRUSTED: resolveConfigValue(
                  this, 'avxFleetIdsClaimTrusted', props.avxFleetIdsClaimTrusted,
                ),
              }
            : {}),
          ...(props.avxCustomerIdClaimTrusted
            ? {
                AVX_CUSTOMER_ID_CLAIM_TRUSTED: resolveConfigValue(
                  this, 'avxCustomerIdClaimTrusted', props.avxCustomerIdClaimTrusted,
                ),
              }
            : {}),
          ...(props.avxVehicleIdClaimTrusted
            ? {
                AVX_VEHICLE_ID_CLAIM_TRUSTED: resolveConfigValue(
                  this, 'avxVehicleIdClaimTrusted', props.avxVehicleIdClaimTrusted,
                ),
              }
            : {}),
        },
      });
      // Narrow IAM: read+write on AvxActions (via AvxActionsWriteForApprove); query + GetItem
      // on AvxFindings; no AvxFeedback, no SNS here.
      // S1 fix: DeleteItem removed.
      // T10.6 fix: UpdateItem removed — the reachable code never calls update_item() on the
      //   findings table.
      // T11.3 fix: PutItem removed from AvxFindingsReadWrite — the reachable code never calls
      //   put_item on _FINDINGS_TABLE.
      // T13.1 fix: GetItem restored to AvxFindingsReadWrite — _lib/avx_scope.py::_load_finding_by_id
      //   calls get_item on _FINDINGS_TABLE and is reached from handler.py:394 (approve).
      //   _lib/avx_scope.py is bundled into this Lambda (confirmed in infrastructure/build/
      //   api-avx-findings/_lib/avx_scope.py). T11.3 incorrectly scoped its analysis to
      //   handler.py alone, missing the bundled module.
      apiAvxFindings.addToRolePolicy(new iam.PolicyStatement({
        sid: 'AvxFindingsReadWrite',
        effect: iam.Effect.ALLOW,
        actions: [
          'dynamodb:Query',
          'dynamodb:GetItem',
        ],
        resources: [
          avxFindingsTable.tableArn,
          `${avxFindingsTable.tableArn}/index/*`,
        ],
      }));
      // T10.6 removed UpdateItem and Query when the approve handler only called
      //   put_item and get_item. Fix Group 35 restores UpdateItem with a call site:
      //   `_record_dispatch_result` writes the executor's result (status, target_ref,
      //   audit_trail) onto the Action with a conditional update_item. Query stays
      //   out; the handler still never queries the actions table. The T11.3 two-way
      //   call-site guard in test/vsa-api-stack-avx.test.ts enforces both directions.
      apiAvxFindings.addToRolePolicy(new iam.PolicyStatement({
        sid: 'AvxActionsWriteForApprove',
        effect: iam.Effect.ALLOW,
        actions: [
          'dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem',
        ],
        resources: [
          avxActionsTable.tableArn,
          `${avxActionsTable.tableArn}/index/*`,
        ],
      }));

      // ── Executor dispatch: the D5 terminal-failure escalation ─────────────
      //
      // Step-5 prerequisite 4. This Lambda bundles `agents/executors/` (see
      // scripts/build-avx-lambda.sh :: bundle_executors), and
      // `agents/executors/registry.py::dispatch` fires the Addendum D5 escalation for
      // any result carrying `escalate=True`. That escalation makes two AWS calls from
      // `agents/executors/escalation.py`, and this role previously had DynamoDB only —
      // recorded as unwired in HANDOFF-group5.md § "What is NOT done" item 3.
      //
      // Both are scoped rather than wildcarded, from reading the call sites:
      //
      //   * `put_events` (escalation.py:295) passes NO `EventBusName`, so it targets the
      //     account's DEFAULT bus. Scoped to that one bus ARN — not `event-bus/*`, which
      //     would let this role publish onto the DMS and CMS integration buses.
      //   * `put_metric_data` (escalation.py:304) publishes to namespace `AVX/Executor`.
      //     CloudWatch has no resource-level ARN for PutMetricData, so `*` is the only
      //     possible Resource; the `cloudwatch:namespace` condition is what actually
      //     narrows it, and without that condition this would be a grant to write ANY
      //     metric in the account, including overwriting others' alarm inputs.
      //
      // Deliberately NOT granted: `ssm:GetParameter`. `base.py::load_retry_policy` reads
      // SSM for per-target retry tuning and falls back to `DEFAULT_RETRY_POLICY` on any
      // failure (base.py:681-689), so its absence degrades tuning rather than dispatch.
      // Adding it would widen this role for a value that has a working default.
      apiAvxFindings.addToRolePolicy(new iam.PolicyStatement({
        sid: 'AvxExecutorEscalationPutEvents',
        effect: iam.Effect.ALLOW,
        actions: ['events:PutEvents'],
        resources: [
          `arn:aws:events:${cdk.Stack.of(this).region}:${cdk.Stack.of(this).account}:event-bus/default`,
        ],
      }));
      apiAvxFindings.addToRolePolicy(new iam.PolicyStatement({
        sid: 'AvxExecutorEscalationPutMetric',
        effect: iam.Effect.ALLOW,
        actions: ['cloudwatch:PutMetricData'],
        resources: ['*'],
        conditions: {
          StringEquals: { 'cloudwatch:namespace': 'AVX/Executor' },
        },
      }));
      avxLambdas.push(apiAvxFindings);

      // ── Lambda: api-avx-actions ──────────────────────────────────────────
      // Handles /actions/* GET routes + PATCH /actions/{id} + POST /actions/{id}/dismiss.
      const apiAvxActions = new lambda.Function(this, 'ApiAvxActions', {
        functionName: `vsa-${props.stage}-api-avx-actions`,
        runtime: lambda.Runtime.PYTHON_3_12,
        handler: 'handler.handler',
        timeout: cdk.Duration.seconds(10),
        memorySize: 256,
        logRetention: logs.RetentionDays.ONE_MONTH,
        code: pythonLambdaAsset(
          // Build directory pre-assembled by scripts/build-avx-lambda.sh.
          // Contains handler.py + _lib/avx_scope.py.
          path.join(__dirname, '..', 'build', 'api-avx-actions'),
        ),
        environment: {
          AVX_STAGE:           props.stage,
          AVX_ACTIONS_TABLE:   avxActionsTableName,
          AVX_FEEDBACK_TABLE:  avxFeedbackTableName,
          AVX_FINDINGS_TABLE:  avxFindingsTableName,
          // Standing-claim trust attestations: of the three standing-claim flags, only
          // AVX_FLEET_IDS_CLAIM_TRUSTED is read by api-avx-actions/handler.py. It is read
          // via _trusted_claim_list in _patch_action's fleet-operator PATCH gate, and via
          // is_fleet_vehicle_caller, check_vehicle_scope, check_owner_fleet_scope and
          // check_fleet_scope (handler.py:842 -> _lib/avx_scope.py:406). The actions
          // Lambda also reads the dealer and district flags, which are deliberately left
          // unset everywhere (fail closed; pinned by the trust-flags test).
          // AVX_CUSTOMER_ID_CLAIM_TRUSTED and AVX_VEHICLE_ID_CLAIM_TRUSTED are NOT
          // read by any code path reachable from handler.py in this Lambda; wiring
          // them would ship inert config.
          // See docs/identity-and-standing-contract.md § 5.2 and the test in
          // test/vsa-api-stack-avx-trust-flags.test.ts for the exact per-function allowlist.
          ...(props.avxFleetIdsClaimTrusted
            ? {
                AVX_FLEET_IDS_CLAIM_TRUSTED: resolveConfigValue(
                  this, 'avxFleetIdsClaimTrustedActions', props.avxFleetIdsClaimTrusted,
                ),
              }
            : {}),
        },
      });
      // Narrow IAM: read+write on AvxActions; query + GetItem on AvxFindings; write on AvxFeedback.
      // S1 fix: DeleteItem removed.
      // T11.3 fix: PutItem removed from AvxActionsReadWrite — the reachable code never calls
      //   put_item on _ACTIONS_TABLE. The only put_item in the deployable targets _FEEDBACK_TABLE
      //   (see _write_feedback_row). get_item, update_item, and query on _ACTIONS_TABLE are all
      //   confirmed call sites.
      apiAvxActions.addToRolePolicy(new iam.PolicyStatement({
        sid: 'AvxActionsReadWrite',
        effect: iam.Effect.ALLOW,
        actions: [
          'dynamodb:GetItem', 'dynamodb:UpdateItem',
          'dynamodb:Query',
        ],
        resources: [
          avxActionsTable.tableArn,
          `${avxActionsTable.tableArn}/index/*`,
        ],
      }));
      // T11.3 fix: GetItem removed from AvxFindingsReadForEvidence — handler.py alone only
      //   calls query on _FINDINGS_TABLE.
      // T13.1 fix: GetItem restored to AvxFindingsReadForEvidence — _lib/avx_scope.py::_load_finding_by_id
      //   calls get_item on _FINDINGS_TABLE and is reached from handler.py:461 (dismiss).
      //   _lib/avx_scope.py is bundled into this Lambda (confirmed in infrastructure/build/
      //   api-avx-actions/_lib/avx_scope.py). T11.3 incorrectly scoped its analysis to
      //   handler.py alone, missing the bundled module.
      apiAvxActions.addToRolePolicy(new iam.PolicyStatement({
        sid: 'AvxFindingsReadForEvidence',
        effect: iam.Effect.ALLOW,
        actions: ['dynamodb:Query', 'dynamodb:GetItem'],
        resources: [
          avxFindingsTable.tableArn,
          `${avxFindingsTable.tableArn}/index/*`,
        ],
      }));
      apiAvxActions.addToRolePolicy(new iam.PolicyStatement({
        sid: 'AvxFeedbackWrite',
        effect: iam.Effect.ALLOW,
        actions: ['dynamodb:PutItem'],
        resources: [avxFeedbackTable.tableArn],
      }));
      avxLambdas.push(apiAvxActions);

      // ── Lambda: api-avx-notifications-register-device ────────────────────
      // Handles POST /notifications/register-device.
      const apiAvxRegisterDevice = new lambda.Function(this, 'ApiAvxRegisterDevice', {
        functionName: `vsa-${props.stage}-api-avx-register-device`,
        runtime: lambda.Runtime.PYTHON_3_12,
        handler: 'handler.handler',
        timeout: cdk.Duration.seconds(10),
        memorySize: 256,
        logRetention: logs.RetentionDays.ONE_MONTH,
        code: pythonLambdaAsset(
          // Build directory pre-assembled by scripts/build-avx-lambda.sh.
          // No _lib dependency for this Lambda (standalone).
          path.join(__dirname, '..', 'build', 'api-avx-notifications-register-device'),
        ),
        environment: {
          AVX_STAGE:                    props.stage,
          NOTIFICATION_DEVICES_TABLE:   notificationDevicesTableName,
          // Empty string → handler skips SNS call and records endpoint_arn=None.
          // Populated when the APNs platform application is created (AVX push enablement spec).
          AVX_APNS_PLATFORM_APP_ARN: '',
        },
      });
      // Narrow IAM on NotificationDevices; sns:CreatePlatformEndpoint scoped
      // to APNs platform-application ARNs only (app/APNS_*/*). NOT sns:Publish or sns:*.
      // T10.6 fix removed GetItem, UpdateItem and Query because the handler only
      // called put_item, and required any later need to come back with a call site.
      // F32.1-alt (2026-09-24, D-SCOPE-1) is that call site: register-device now
      // Queries the owner's rows and DeleteItems every other device before the
      // upsert, so an owner has at most one row and the broker's Limit=1 reads
      // are correct by construction. Query + DeleteItem on the table ARN only —
      // no index ARN, no wildcard. GetItem and UpdateItem stay removed.
      apiAvxRegisterDevice.addToRolePolicy(new iam.PolicyStatement({
        sid: 'NotificationDevicesReadWrite',
        effect: iam.Effect.ALLOW,
        actions: [
          'dynamodb:PutItem',
          'dynamodb:Query',
          'dynamodb:DeleteItem',
        ],
        resources: [notificationDevicesTable.tableArn],
      }));
      apiAvxRegisterDevice.addToRolePolicy(new iam.PolicyStatement({
        sid: 'SnsCreateApnsEndpoint',
        effect: iam.Effect.ALLOW,
        actions: ['sns:CreatePlatformEndpoint'],
        // Scoped to APNs platform application ARNs only — NOT topic ARNs.
        // Pattern covers both APNS (production) and APNS_SANDBOX (staging).
        // `APNS_*/*` alone would NOT match `APNS/AppName` in production
        // (underscore is a literal, not a wildcard character in IAM ARN matching).
        resources: [
          `arn:aws:sns:${this.region}:${this.account}:app/APNS/*`,
          `arn:aws:sns:${this.region}:${this.account}:app/APNS_*/*`,
        ],
      }));
      avxLambdas.push(apiAvxRegisterDevice);

      // ── Tier 1 AgentCore read grants (decisions.md 2026-09-19) ───────────
      // The AmazonBedrockAgentCoreSDKRuntime-{region}-{stage} role is
      // auto-provisioned by `agentcore configure` and lives outside CDK.
      // We import it by name (iam.Role.fromRoleName); this is safe because:
      //   (a) the role name is deterministic and predictable,
      //   (b) fromRoleName does NOT look up the role at synth time — it only
      //       constructs an ARN token for use in policy statements.
      // If the role does not yet exist at the time of this stack's deploy, the
      // grant statements simply reference a non-existent principal; no deploy
      // error is emitted. The grants become effective once `agentcore configure`
      // provisions the role.
      const agentCoreRoleName = `AmazonBedrockAgentCoreSDKRuntime-${this.region}-${props.stage}`;
      const agentCoreRole = iam.Role.fromRoleName(
        this, 'AgentCoreRoleRef', agentCoreRoleName,
      );
      // ITable.grantReadData covers GetItem + Query + Scan + ConditionCheckItem.
      // This is correct for Tier 1 read-only access; no write grants added.
      avxFindingsTable.grantReadData(agentCoreRole);
      avxActionsTable.grantReadData(agentCoreRole);
      notificationDevicesTable.grantReadData(agentCoreRole);

      // ── API Gateway resources ─────────────────────────────────────────────
      const findingsRoot = this.restApi.root.addResource('findings');
      // /findings/me — standing derived server-side from trusted claims; no path identifier
      findingsRoot.addResource('me').addMethod(
        'GET',
        new apigw.LambdaIntegration(apiAvxFindings, { allowTestInvoke: false }),
        { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
      );
      // /findings/vehicle/{vin}
      findingsRoot.addResource('vehicle').addResource('{vin}').addMethod(
        'GET',
        new apigw.LambdaIntegration(apiAvxFindings, { allowTestInvoke: false }),
        { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
      );
      // /findings/owner/{owner_id}
      findingsRoot.addResource('owner').addResource('{owner_id}').addMethod(
        'GET',
        new apigw.LambdaIntegration(apiAvxFindings, { allowTestInvoke: false }),
        { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
      );
      // /findings/owner-fleet/{owner_id}
      findingsRoot.addResource('owner-fleet').addResource('{owner_id}').addMethod(
        'GET',
        new apigw.LambdaIntegration(apiAvxFindings, { allowTestInvoke: false }),
        { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
      );
      // /findings/fleet/{fleet_id}
      findingsRoot.addResource('fleet').addResource('{fleet_id}').addMethod(
        'GET',
        new apigw.LambdaIntegration(apiAvxFindings, { allowTestInvoke: false }),
        { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
      );
      // /findings/dealership/{dealer_id}
      findingsRoot.addResource('dealership').addResource('{dealer_id}').addMethod(
        'GET',
        new apigw.LambdaIntegration(apiAvxFindings, { allowTestInvoke: false }),
        { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
      );
      // /findings/district/{district_id}
      findingsRoot.addResource('district').addResource('{district_id}').addMethod(
        'GET',
        new apigw.LambdaIntegration(apiAvxFindings, { allowTestInvoke: false }),
        { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
      );
      // /findings/{finding_id}/approve
      findingsRoot.addResource('{finding_id}').addResource('approve').addMethod(
        'POST',
        new apigw.LambdaIntegration(apiAvxFindings, { allowTestInvoke: false }),
        { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
      );

      const actionsRoot = this.restApi.root.addResource('actions');
      // /actions/vehicle/{vin}
      actionsRoot.addResource('vehicle').addResource('{vin}').addMethod(
        'GET',
        new apigw.LambdaIntegration(apiAvxActions, { allowTestInvoke: false }),
        { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
      );
      // /actions/owner/{owner_id}
      actionsRoot.addResource('owner').addResource('{owner_id}').addMethod(
        'GET',
        new apigw.LambdaIntegration(apiAvxActions, { allowTestInvoke: false }),
        { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
      );
      // /actions/owner-fleet/{owner_id}
      actionsRoot.addResource('owner-fleet').addResource('{owner_id}').addMethod(
        'GET',
        new apigw.LambdaIntegration(apiAvxActions, { allowTestInvoke: false }),
        { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
      );
      // /actions/fleet/{fleet_id}
      actionsRoot.addResource('fleet').addResource('{fleet_id}').addMethod(
        'GET',
        new apigw.LambdaIntegration(apiAvxActions, { allowTestInvoke: false }),
        { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
      );
      // /actions/dealership/{dealer_id}
      actionsRoot.addResource('dealership').addResource('{dealer_id}').addMethod(
        'GET',
        new apigw.LambdaIntegration(apiAvxActions, { allowTestInvoke: false }),
        { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
      );
      // /actions/district/{district_id}
      actionsRoot.addResource('district').addResource('{district_id}').addMethod(
        'GET',
        new apigw.LambdaIntegration(apiAvxActions, { allowTestInvoke: false }),
        { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
      );
      // /actions/ios-owner/{owner_id}
      actionsRoot.addResource('ios-owner').addResource('{owner_id}').addMethod(
        'GET',
        new apigw.LambdaIntegration(apiAvxActions, { allowTestInvoke: false }),
        { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
      );
      // /actions/{action_id}  PATCH
      const actionById = actionsRoot.addResource('{action_id}');
      actionById.addMethod(
        'PATCH',
        new apigw.LambdaIntegration(apiAvxActions, { allowTestInvoke: false }),
        { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
      );
      // /actions/{action_id}/dismiss  POST
      actionById.addResource('dismiss').addMethod(
        'POST',
        new apigw.LambdaIntegration(apiAvxActions, { allowTestInvoke: false }),
        { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
      );

      // /notifications/register-device  POST
      const notificationsRoot = this.restApi.root.addResource('notifications');
      notificationsRoot.addResource('register-device').addMethod(
        'POST',
        new apigw.LambdaIntegration(apiAvxRegisterDevice, { allowTestInvoke: false }),
        { authorizer, authorizationType: apigw.AuthorizationType.COGNITO },
      );
    } else {
      // Fail-graceful: emit a synth-time warning when AVX export names are absent.
      cdk.Annotations.of(this).addWarning(
        'AVX Lambda wiring skipped: one or more avxFindingsTableNameExportName / ' +
        'avxActionsTableNameExportName / avxFeedbackTableNameExportName / ' +
        'notificationDevicesTableNameExportName props are absent. ' +
        'Pass these props from VsaTier2Stack to enable the /findings, /actions, ' +
        'and /notifications/register-device routes.',
      );
    }

    // Single wildcard permission — one grant covers all routes/stages/methods.
    for (const fn of [apiTriage, apiConfig, apiServiceHistory, apiVehicleContext, apiVehicleTrips, apiDriversMe, apiVehicleLiveState, apiEscalate, apiFindServiceCenter, apiBook, apiAssistantChat, apiAcquireCatalog, apiAcquireOrder, apiAcquireOffers, apiAcquireLeads, ...avxLambdas]) {
      fn.addPermission('ApiGwInvoke', {
        principal: new iam.ServicePrincipal('apigateway.amazonaws.com'),
        sourceArn: this.restApi.arnForExecuteApi(),
      });
    }
    // reservationHandoffFn is a cross-stack reference (IFunction from AcquireStack).
    // CDK's addPermission() emits the CfnPermission resource in the function's own
    // stack (AcquireStack). Adding sourceArn pointing to this stack's RestApi would
    // therefore create a cross-stack ARN reference (AcquireStack → VsaApiStack.RestApi.Ref),
    // which combined with VsaApiStack's existing dependency on AcquireStack.fn.Arn
    // produces a CDK DependencyCycle.
    //
    // Cycle-break approach (decisions.md Q14):
    //   1. AcquireStack exports the reservation_handoff ARN as a CFN output.
    //   2. VsaApiStack uses lambda.CfnPermission (L1) directly with scope=this, so
    //      the resource lives in VsaApiStack rather than in AcquireStack.
    //   3. functionName is read via cdk.Fn.importValue — a CFN-level import that does
    //      NOT create a CDK construct dependency (the stack dependency is documented
    //      by the CFN export/import relationship itself).
    //   4. sourceArn is a local Ref to this.restApi — no cross-stack reference.
    //
    // Result: VsaApiStack → AcquireStack (via CFN import) is a one-way deploy-order
    // dependency (AcquireStack deploys first). No CDK DependencyCycle. The Lambda
    // permission is now scoped to POST /acquire/reservations on this specific REST API,
    // closing the intra-account authorizer bypass documented in security-review Cycle 6 W2.
    new lambda.CfnPermission(this, 'AcquireReservationsApiGwInvoke', {
      action: 'lambda:InvokeFunction',
      principal: 'apigateway.amazonaws.com',
      // Fn.importValue reads the CFN export from AcquireStack; no CDK construct dep.
      functionName: cdk.Fn.importValue(props.acquireReservationHandoffFnArnExportName),
      // Scoped to POST /acquire/reservations on this specific REST API + stage.
      // arnForExecuteApi(method, resource, stage) is a local Ref — no cross-stack dep.
      sourceArn: this.restApi.arnForExecuteApi('POST', '/acquire/reservations', props.stage),
    });

    // ── Edge throttling + budget backstop (decisions.md Q9) ──────────────────
    //
    // This is the backstop the per-session turn counter deliberately cannot be,
    // because that counter keys on a client-supplied `runtimeSessionId` and
    // fails open on store errors. Rate limiting at the edge is the control that
    // actually bounds spend, and it cannot be rotated away by a client.
    //
    // Throttle mechanism: stage-level MethodSettings on the RestApi
    // (deployOptions.throttlingRateLimit/BurstLimit set above at RestApi
    // construction). This is the actual enforced limit for all callers.
    //
    // The previous UsagePlan-based approach was INERT: API Gateway usage plans
    // apply throttle limits only to requests that supply an API key matching a
    // UsagePlanKey member. No API keys were attached, so zero callers were ever
    // throttled by that plan. The stage-level MethodSettings throttle does NOT
    // require API keys — it applies to every caller unconditionally.
    // (security-review Cycle 6 W3 — "the operator's mental model of the abuse
    // ceiling is off by 5×"). The UsagePlan has been removed.
    //
    // Sizing rationale (see VsaApiStackProps comments):
    //   80 concurrent show-floor visitors × burst ~5 req/s = 400 req/s peak.
    //   2× headroom → throttleRateLimit default 500 req/s / burst 200.
    //   Throttling real visitors is the WORSE outcome — this is an abuse bound,
    //   not a capacity control.
    const throttleRate = props.throttleRateLimit ?? 500;
    const throttleBurst = props.throttleBurstLimit ?? 200;

    // CfnOutput: expose throttle limits so operators know what's deployed.
    // These values match the stage-level MethodSettings set in deployOptions above.
    new cdk.CfnOutput(this, 'AcquireThrottleRateLimit', {
      value: String(throttleRate),
      description:
        `Edge throttle rate limit (req/s) applied to ALL routes via stage MethodSettings - ` +
        `decisions.md Q9 backstop. This IS the enforced limit. ` +
        `Adjust throttleRateLimit prop to override.`,
    });
    new cdk.CfnOutput(this, 'AcquireThrottleBurstLimit', {
      value: String(throttleBurst),
      description: 'Edge throttle burst limit (tokens) - stage MethodSettings, pair to AcquireThrottleRateLimit.',
    });

    // ── AWS Budgets backstop (decisions.md Q9 second line of defence) ────────
    //
    // The per-session Bedrock turn counter is a UX guard that fails OPEN
    // on store errors (intentional: a broken counter must not take the
    // booth down). The stage-level API Gateway throttle is the primary
    // spend bound. This Budgets alarm is the second-line backstop that
    // fires when daily AWS spend on this account exceeds the show-week
    // envelope, regardless of what a client does.
    //
    // Threshold: $500/day. Show-floor abuse math (security-review Cycle 6 W3):
    //   500 rps × 300 s (5-min access token) × $0.02/turn ≈ $3,000 worst case
    //   per stolen credential per burst. $500/day is below the worst-case single
    //   burst but above the expected show-day spend (~$50-100 for 60-80 visitors).
    //   The alarm alerts the operator to investigate before the show week's total
    //   exceeds the show-budget guardrail. Operators receiving the notification
    //   should revoke kiosk credentials and re-evaluate the throttle.
    //
    // Notification target: the operator email set via VSA_BUDGET_ALERT_EMAIL
    // env var at synth time. When unset, defaults to a placeholder that must
    // be replaced before production cutover (documented in DEPLOYMENT.md).
    const budgetAlertEmail = process.env.VSA_BUDGET_ALERT_EMAIL || 'operator@example.com';

    new budgets.CfnBudget(this, 'ShowFloorDailyBudget', {
      budget: {
        budgetName: `vsa-${props.stage}-show-floor-daily`,
        budgetType: 'COST',
        timeUnit: 'DAILY',
        budgetLimit: {
          amount: 500,
          unit: 'USD',
        },
      },
      notificationsWithSubscribers: [
        {
          notification: {
            notificationType: 'ACTUAL',
            comparisonOperator: 'GREATER_THAN',
            threshold: 80,          // alert at 80 % of daily budget ($400)
            thresholdType: 'PERCENTAGE',
          },
          subscribers: [
            {
              subscriptionType: 'EMAIL',
              address: budgetAlertEmail,
            },
          ],
        },
      ],
    });

    // --- WebSocket connections table ---------------------------------------
    const wsConnections = new dynamodb.Table(this, 'WsConnections', {
      tableName: `vsa-${props.stage}-ws-connections`,
      partitionKey: { name: 'connectionId', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    wsConnections.addGlobalSecondaryIndex({
      indexName: 'byTenant',
      partitionKey: { name: 'tenantId', type: dynamodb.AttributeType.STRING },
    });

    // --- WebSocket authorizer Lambda ---------------------------------------
    const wsAuthFn = new lambda.Function(this, 'WsAuthorizerFn', {
      functionName: `vsa-${props.stage}-ws-authorizer`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      timeout: cdk.Duration.seconds(5),
      logRetention: logs.RetentionDays.ONE_MONTH,
      code: pythonLambdaAsset(
        path.join(__dirname, '..', 'build', 'api-ws-authorizer'),
      ),
      environment: {
        ALLOWED_ORIGIN: allowedOrigin,
        TENANT_POOL_IDS: JSON.stringify(poolInfos),
      },
    });

    // --- WebSocket connect/disconnect Lambda -------------------------------
    const wsFn = new lambda.Function(this, 'WsFn', {
      functionName: `vsa-${props.stage}-ws`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      timeout: cdk.Duration.seconds(10),
      logRetention: logs.RetentionDays.ONE_MONTH,
      code: pythonLambdaAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'api-ws'),
      ),
      environment: {
        ALLOWED_ORIGIN: allowedOrigin,
        WS_CONNECTIONS_TABLE: wsConnections.tableName,
      },
    });
    wsConnections.grantReadWriteData(wsFn);

    this.wsApi = new apigwv2.WebSocketApi(this, 'WsApi', {
      apiName: `vsa-${props.stage}-ws-api`,
      connectRouteOptions: {
        integration: new apigwv2int.WebSocketLambdaIntegration('WsConnectInt', wsFn),
        authorizer: new apigwv2auth.WebSocketLambdaAuthorizer('WsAuth', wsAuthFn, {
          identitySource: ['route.request.querystring.token'],
        }),
      },
      disconnectRouteOptions: {
        integration: new apigwv2int.WebSocketLambdaIntegration('WsDisconnectInt', wsFn),
      },
      defaultRouteOptions: {
        integration: new apigwv2int.WebSocketLambdaIntegration('WsDefaultInt', wsFn),
      },
    });
    new apigwv2.WebSocketStage(this, 'WsStage', {
      webSocketApi: this.wsApi,
      stageName: props.stage,
      autoDeploy: true,
    });

    // --- Outputs -----------------------------------------------------------
    new cdk.CfnOutput(this, 'RestApiUrl', { value: this.restApi.url });
    new cdk.CfnOutput(this, 'WsApiUrl', {
      value: `${this.wsApi.apiEndpoint}/${props.stage}`,
    });
  }
}
