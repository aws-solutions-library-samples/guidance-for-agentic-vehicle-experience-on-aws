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
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';
import * as path from 'path';
import { TenantPool } from './tenant-pool';
import {
  ConfigValue,
  isResolvedUserPoolClientId,
  isResolvedUserPoolId,
  resolveConfigValue,
} from './config-value';

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
    // "ON EXISTENCE CHECKING".
    const cmsKmsKeyArn = resolveConfigValue(this, 'cmsKmsKeyArn', props.cmsKmsKeyArn);
    const redisEndpoint = resolveConfigValue(this, 'redisEndpoint', props.redisEndpoint);
    const assistantChatRuntimeArn = resolveConfigValue(
      this,
      'assistantChatRuntimeArn',
      props.assistantChatRuntimeArn,
    );

    // ─── CMS table names derived from cmsStage ─────────────────────────────
    const cms = (suffix: string) => `cms-${props.cmsStage}-${suffix}`;
    const cmsTable = {
      vehicles: cms('storage-vehicles'),
      drivers: cms('storage-drivers'),
      trips: cms('storage-trips'),
      serviceHistory: cms('storage-service-history'),
      safetyEvents: cms('storage-safety-events'),
      dtcHistory: cms('storage-dtc-history'),
      vfoActionQueue: cms('vfo-action-queue'),
    };

    // --- REST API -----------------------------------------------------------
    // Constructed BEFORE per-tenant pools so `this.restApi.restApiId` is a
    // valid CloudFormation Ref token when it is threaded into TenantPool via
    // the `vsaApiId` prop (security-review Cycle 6 W2 / decisions.md Q14).
    // Both constructs live in the same stack — no cross-stack dependency.
    this.restApi = new apigw.RestApi(this, 'RestApi', {
      restApiName: `vsa-${props.stage}-api`,
      defaultCorsPreflightOptions: {
        allowOrigins: apigw.Cors.ALL_ORIGINS,
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
          'Access-Control-Allow-Origin': "'*'",
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
      michelin: new TenantPool(this, 'PoolMichelin', {
        stage: props.stage, tenantId: 'michelin', displayName: 'Michelin Fleet',
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
      code: lambda.Code.fromAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'api-triage'),
      ),
      environment: {
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
      code: lambda.Code.fromAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'api-vehicle-service-history'),
      ),
      environment: {
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
      code: lambda.Code.fromAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'api-vehicle-context'),
      ),
      environment: {
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
      code: lambda.Code.fromAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'api-vehicle-trips'),
      ),
      environment: {
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
      code: lambda.Code.fromAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'api-drivers-me'),
      ),
      environment: {
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

    const apiVehicleLiveState = new lambda.Function(this, 'ApiVehicleLiveState', {
      functionName: `vsa-${props.stage}-api-vehicle-live-state`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      timeout: cdk.Duration.seconds(6),
      memorySize: 256,
      logRetention: logs.RetentionDays.ONE_MONTH,
      code: lambda.Code.fromAsset(
        path.join(__dirname, '..', 'build', 'api-vehicle-live-state'),
      ),
      vpc: cmsVpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [cmsRedisSg],
      environment: {
        REDIS_ENDPOINT: redisEndpoint,
        REDIS_PORT: '6379',
        CMS_VEHICLES_TABLE: cmsTable.vehicles,
      },
    });
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
      code: lambda.Code.fromAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'api-vehicle-safety-events'),
      ),
      environment: {
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
      code: lambda.Code.fromAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'api-tenant-config'),
      ),
      environment: {
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
    // For P0 contacts the Lambda also writes a PENDING row to
    // ${cmsTable.vfoActionQueue} so the CMS dispatcher UI can fulfill the
    // roadside job independently of the human chat. Write is best-effort;
    // failure doesn't block the chat.
    const apiEscalate = new lambda.Function(this, 'ApiEscalate', {
      functionName: `vsa-${props.stage}-api-escalate`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      timeout: cdk.Duration.seconds(15),
      logRetention: logs.RetentionDays.ONE_MONTH,
      code: lambda.Code.fromAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'api-escalate'),
      ),
      environment: {
        TENANT_CONFIG_TABLE: props.tenantConfigTable.tableName,
        RSA_QUEUE_TABLE: cmsTable.vfoActionQueue,
        CHAT_DURATION_MINUTES: '60',
      },
    });
    props.tenantConfigTable.grantReadData(apiEscalate);

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

    // Roadside-dispatch queue write. Table lives in CMS (cms-prod-*) so
    // the ARN is fully qualified. Only PutItem — we don't read, update,
    // or delete from here.
    apiEscalate.addToRolePolicy(new iam.PolicyStatement({
      sid: 'CmsActionQueueWrite',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:PutItem'],
      resources: [
        `arn:aws:dynamodb:${this.region}:${this.account}:table/${cmsTable.vfoActionQueue}`,
      ],
    }));
    // The CMS action-queue table is SSE-encrypted with the same CMS CMK
    // used by service-history / vehicles / drivers. Grant the same
    // via-service-gated KMS access the other Lambdas use.
    apiEscalate.addToRolePolicy(new iam.PolicyStatement({
      sid: 'CmsKmsAccessForSseTable',
      effect: iam.Effect.ALLOW,
      actions: ['kms:Decrypt', 'kms:DescribeKey', 'kms:GenerateDataKey'],
      resources: [cmsKmsKeyArn],
      conditions: {
        StringEquals: { 'kms:ViaService': `dynamodb.${this.region}.amazonaws.com` },
      },
    }));

    const escalate = this.restApi.root.addResource('escalate');
    escalate.addMethod('POST',
      new apigw.LambdaIntegration(apiEscalate, { allowTestInvoke: false }),
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
    const apiFindServiceCenter = new lambda.Function(this, 'ApiFindServiceCenter', {
      functionName: `vsa-${props.stage}-api-find-service-center`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      timeout: cdk.Duration.seconds(10),
      logRetention: logs.RetentionDays.ONE_MONTH,
      code: lambda.Code.fromAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'api-find-service-center'),
      ),
      environment: {
        ADP_SERVICE_NETWORK_TABLE: adpServiceNetworkTable,
      },
    });
    apiFindServiceCenter.addToRolePolicy(new iam.PolicyStatement({
      sid: 'AdpServiceNetworkRead',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:Scan', 'dynamodb:GetItem', 'dynamodb:Query'],
      resources: [
        `arn:aws:dynamodb:${this.region}:${this.account}:table/${adpServiceNetworkTable}`,
        `arn:aws:dynamodb:${this.region}:${this.account}:table/${adpServiceNetworkTable}/index/*`,
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
      code: lambda.Code.fromAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'api-book'),
      ),
      environment: {
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
      code: lambda.Code.fromAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'api-assistant-chat'),
      ),
      environment: {
        CVX_RUNTIME_ARN: assistantChatRuntimeArn,
        CVX_RUNTIME_REGION: props.assistantChatRuntimeRegion,
        DEFAULT_TENANT_ID: 'michelin',
        DEFAULT_PERSONA_ID: 'fleet_driver',
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
      code: lambda.Code.fromAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'api-acquire-catalog'),
      ),
      environment: {
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
      code: lambda.Code.fromAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'api-acquire-order'),
      ),
      environment: {
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
      code: lambda.Code.fromAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'api-acquire-offers'),
      ),
      environment: {
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
      code: lambda.Code.fromAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'api-acquire-leads'),
      ),
      environment: {
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

    // Single wildcard permission — one grant covers all routes/stages/methods.
    for (const fn of [apiTriage, apiConfig, apiServiceHistory, apiVehicleContext, apiVehicleTrips, apiDriversMe, apiVehicleLiveState, apiEscalate, apiFindServiceCenter, apiBook, apiAssistantChat, apiAcquireCatalog, apiAcquireOrder, apiAcquireOffers, apiAcquireLeads]) {
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
        `Edge throttle rate limit (req/s) applied to ALL routes via stage MethodSettings — ` +
        `decisions.md Q9 backstop. This IS the enforced limit. ` +
        `Adjust throttleRateLimit prop to override.`,
    });
    new cdk.CfnOutput(this, 'AcquireThrottleBurstLimit', {
      value: String(throttleBurst),
      description: 'Edge throttle burst limit (tokens) — stage MethodSettings, pair to AcquireThrottleRateLimit.',
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
      code: lambda.Code.fromAsset(
        path.join(__dirname, '..', 'build', 'api-ws-authorizer'),
      ),
      environment: {
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
      code: lambda.Code.fromAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'api-ws'),
      ),
      environment: {
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
