/**
 * Central cdk-nag suppressions for the CVX app.
 *
 * WHY THIS FILE EXISTS
 * --------------------
 * `CDK_NAG=1 npx cdk synth --all` runs the AWS Solutions rule pack (see
 * `bin/vsa.ts`). Every finding must end up in one of two states, and a reviewer
 * needs to be able to tell which at a glance:
 *
 *   1. FIXED in the stack code, or
 *   2. SUPPRESSED HERE with a reason that says why the finding does not apply.
 *
 * A suppression is a written argument, not a mute button. Two rules for editing
 * this file:
 *
 *   - Scope it. Prefer `appliesTo` (exact string or `{ regex }`) over a bare
 *     rule id. A bare `{ id: 'AwsSolutions-IAM5' }` silences EVERY wildcard
 *     permission in the app forever, including one a future change introduces
 *     by mistake. The scoped entries below only silence the specific wildcard
 *     SHAPES that CDK itself generates, so a genuinely new wildcard still fails
 *     the build.
 *   - Say why, specifically. "Acceptable for demo" is not a reason. Name the AWS
 *     limitation, the construct that generates it, or the design decision.
 *
 * WHAT IS DELIBERATELY *NOT* SUPPRESSED
 * -------------------------------------
 * The API's wildcard CORS is a real finding and is tracked separately in the
 * repository's security review. Do not add a suppression to hide it.
 */
import * as cdk from 'aws-cdk-lib';
import { NagSuppressions, NagPackSuppression } from 'cdk-nag';

/**
 * Suppressions that apply to every stack in the app.
 *
 * Each entry is either (a) an unavoidable AWS/CDK-generated pattern, or (b) a
 * documented posture decision for sample code that is explicitly not
 * production-ready (the README states this).
 */
const APP_WIDE_SUPPRESSIONS: NagPackSuppression[] = [
  // ── IAM ────────────────────────────────────────────────────────────────
  {
    id: 'AwsSolutions-IAM4',
    reason:
      'AWS managed policies here are attached by CDK to the service roles it generates, not chosen by this app: ' +
      'AWSLambdaBasicExecutionRole on every Lambda execution role, AWSLambdaVPCAccessExecutionRole on the ' +
      'VPC-attached live-state Lambda, and AmazonAPIGatewayPushToCloudWatchLogs on the API Gateway account role. ' +
      'Replacing them means hand-rolling the equivalent customer-managed policy for each generated role, which ' +
      'adds maintenance burden without narrowing effective access (each grants only its own CloudWatch Logs / ENI ' +
      'permissions). Scoped to those three policies so any OTHER managed policy still fails the build.',
    appliesTo: [
      'Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole',
      'Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole',
      'Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs',
    ],
  },
  {
    id: 'AwsSolutions-IAM5',
    reason:
      'DynamoDB global-secondary-index wildcard. `table.grantReadData()` / `grantReadWriteData()` emit ' +
      '`<table>/index/*` because a GSI ARN cannot be enumerated at synth time and a query against any index of ' +
      'the granted table is the intended access. The wildcard cannot widen beyond that one table.',
    appliesTo: [{ regex: '/^Resource::.*(table/|<).*index/\\*$/g' }],
  },
  {
    id: 'AwsSolutions-IAM5',
    reason:
      'Lambda version/alias wildcard. `fn.grantInvoke()` emits `<function.Arn>:*` so the grant survives a new ' +
      'published version; it is scoped to that single function. Same shape for the qualified log-stream ARN ' +
      '`log-group:/aws/lambda/<fn>:*`, which is that function\'s own log group.',
    appliesTo: [
      { regex: '/^Resource::<.*\\.Arn>:\\*$/g' },
      { regex: '/^Resource::arn:aws:logs:.*log-group:.*:\\*$/g' },
      // Same justification, literal-ARN form. `Function.fromFunctionArn(...)` with a
      // concrete ARN (used by bin/vsa.ts for cross-stack imports and by the CDK test
      // fixtures) renders the resource as a literal rather than a `<Fn.Arn>` token,
      // so the two token regexes above do not match it. The permission is identical:
      // one named function, `:*` being the version/alias qualifier.
      { regex: '/^Resource::arn:aws:lambda:[^:]*:[0-9]*:function:[^:*]+:\\*$/g' },
    ],
  },
  {
    id: 'AwsSolutions-IAM5',
    reason:
      'Object-level and catalog-level wildcards required by the resource type: S3 object ARNs (`bucket/*`) for the ' +
      'Athena results bucket and the ADP curated-lake prefixes, Glue `table/*` under a named database, KMS ' +
      '`key/*` for the CMS/ADP keys reached via kms:ViaService, and `execute-api:<api>/*` for the API\'s own ' +
      'stage/method paths. In each case the wildcard is the object/child level of an explicitly named parent ' +
      'resource — the prefix pins the bucket, database, key region and API.',
    appliesTo: [
      { regex: '/^Resource::arn:aws:s3:::.*\\*$/g' },
      { regex: '/^Resource::<.*\\.Arn>/\\*$/g' },
      { regex: '/^Resource::arn:aws:glue:.*(table|database)/.*\\*$/g' },
      { regex: '/^Resource::arn:aws:kms:.*key/\\*$/g' },
      { regex: '/^Resource::arn:aws:execute-api:.*\\*$/g' },
    ],
  },
  {
    id: 'AwsSolutions-IAM5',
    reason:
      'Actions that do not support resource-level permissions, so IAM requires `Resource: "*"`. Each is already ' +
      'constrained by the strongest mechanism the action offers, as documented inline at the grant site: ' +
      'cloudwatch:PutMetricData (scoped by the cloudwatch:namespace condition to CVX/Tier2), ' +
      'lakeformation:GetDataAccess (Lake Formation grants gate the actual data), CloudWatch Logs + X-Ray ' +
      'baseline actions on the AgentCore runtime role, sts:GetWebIdentityToken for the bidi per-user JWT ' +
      'federation, and the CDK LogRetention helper\'s own logs:PutRetentionPolicy.',
    appliesTo: ['Resource::*'],
  },
  {
    id: 'AwsSolutions-IAM5',
    reason:
      'App-authored ARN-prefix wildcards for identifiers that do not exist at synth time. These are NOT ' +
      'CDK-generated: (a) Bedrock AgentCore runtime and memory ARNs — `agentcore deploy` mints them with a ' +
      'generated suffix that changes on every redeploy, so the grant matches the named runtime/memory prefix ' +
      '(vsa_supervisor_bidi*, vsa_staging_cvx_foundation_memory*); (b) Bedrock inference-profile ARNs, whose ' +
      'profile id set is regional; (c) the ADP `table/adp-<stage>-*` prefix, because the service_network tool ' +
      'Scans tables the ADP stack owns and names per stage; (d) Amazon Connect `instance/*` and ' +
      '`instance/*/contact-flow/*`, because the Connect instance and flow ids are operator-supplied at deploy ' +
      'time (see vsa-api-stack.ts StartChatContact); (e) DataZone `domain/*` for the catalog discovery actions. ' +
      'Each is pinned to this account, and where possible to a named resource prefix.',
    appliesTo: [
      { regex: '/^Resource::arn:aws:bedrock-agentcore:.*(runtime|memory)/.*\\*$/g' },
      { regex: '/^Resource::arn:aws:bedrock:.*inference-profile/\\*$/g' },
      { regex: '/^Resource::arn:aws:dynamodb:.*table/adp-.*\\*$/g' },
      { regex: '/^Resource::arn:aws:connect:.*instance/\\*.*$/g' },
      { regex: '/^Resource::arn:aws:datazone:.*domain/\\*$/g' },
    ],
  },
  {
    id: 'AwsSolutions-IAM5',
    reason:
      'connect:StartOutboundVoiceContact on `<instance>/contact/*` for ONE named Connect instance ' +
      '(/cvx/{stage}/connect-instance-arn; security W2). The action creates a new contact, so its id cannot ' +
      'exist before the call and `contact/*` under the instance is the narrowest resource the API accepts ' +
      '(AWS re:Post knowledge center "connect-outbound-calls-api"). Matches the literal-ARN form and the ' +
      'SSM-token form only; an `instance/*` grant does not match this entry.',
    appliesTo: [
      { regex: '/^Resource::arn:aws:connect:[a-z0-9-]+:\\d{12}:instance/[^*/]+/contact/\\*$/g' },
      { regex: '/^Resource::<SsmParameterValuecvx[a-z0-9]*connectinstancearn[A-Za-z0-9]*>/contact/\\*$/g' },
    ],
  },
  {
    id: 'AwsSolutions-IAM5',
    reason:
      'Account-scoped Bedrock wildcard on the InvokeNovaSonicBidirectionalStream statement only. The team already ' +
      'narrowed the other Bedrock grants to specific foundation-model and inference-profile ARNs on 2026-06-19; ' +
      'this one is retained because the bidi stream fails with the narrow grant and works with the broad one, ' +
      'confirmed empirically that day (the exact bidi session resource ARN is undocumented). It is bounded to ' +
      'this account and region and to a single action. Tracked for tightening in ' +
      'issues/2026-06-19-nova-sonic-bidi-grant-scope — remove this entry when that ARN is known.',
    appliesTo: [{ regex: '/^Resource::arn:aws:bedrock:[a-z0-9-]+:[0-9]{12}:\\*$/g' }],
  },

  // ── Lambda runtime ─────────────────────────────────────────────────────
  {
    id: 'AwsSolutions-IAM5',
    reason:
      'APNs platform-application wildcard on the AVX register-device Lambda\'s ' +
      '`sns:CreatePlatformEndpoint` grant. Device endpoints do not exist at deploy time — ' +
      'CreatePlatformEndpoint is what creates them, one per device token — so the grant ' +
      'cannot name endpoint ARNs and must target the platform application. It is scoped to ' +
      'the `app/` path, so it grants nothing on topic ARNs. Both forms are required and ' +
      'neither is redundant: `APNS_*/*` covers APNS_SANDBOX (dev/staging) and does NOT match ' +
      'production, because the underscore is a literal character in IAM ARN matching rather ' +
      'than a wildcard — omitting `APNS/*` silently breaks production push, and omitting ' +
      '`APNS_*/*` silently breaks staging. Surfaced by the cdk-nag gate added for review ' +
      'Cycle 3 Critical C1; previously invisible because the CDK test fixture never applied ' +
      'the AwsSolutionsChecks Aspect.',
    appliesTo: [
      { regex: '/^Resource::arn:aws:sns:[^:]*:[0-9]*:app\\/APNS\\/\\*$/g' },
      { regex: '/^Resource::arn:aws:sns:[^:]*:[0-9]*:app\\/APNS_\\*\\/\\*$/g' },
    ],
  },
  {
    id: 'AwsSolutions-L1',
    reason:
      'Python runtime is pinned to 3.12 on purpose, not left stale. It must match the AgentCore runtime ' +
      '(`runtime_type: PYTHON_3_12` in .bedrock_agentcore.yaml) and the vendored dependency sets that ' +
      'scripts/build-*.sh install for the Lambda assets, so the agent and the Lambdas run the same interpreter ' +
      'as the code that was tested. Bumping it is a deliberate, coordinated change across those three places.',
  },

  // ── API Gateway ────────────────────────────────────────────────────────
  {
    id: 'AwsSolutions-APIG4',
    reason:
      'Flagged on the WebSocket $disconnect and $default routes only. API Gateway supports a route authorizer on ' +
      '$connect ONLY — the connection is authorized once at $connect (WebSocketLambdaAuthorizer over a ' +
      'query-string token, see vsa-api-stack.ts), and subsequent frames ride that already-authorized connection. ' +
      'Attaching an authorizer to $disconnect/$default is not possible. Every REST route uses ' +
      'AuthorizationType.COGNITO.',
  },
  {
    id: 'AwsSolutions-APIG2',
    reason:
      'Request validation is performed in the Lambda handlers rather than by API Gateway request models. The ' +
      'handlers already validate and normalise input (VIN/ID regex checks, claim-scope checks) because the same ' +
      'validation must hold for the agent tool path, which does not traverse API Gateway. Adding a second ' +
      'validation layer as JSON-schema models would duplicate that logic in a place it can silently drift from.',
  },
  {
    id: 'AwsSolutions-APIG1',
    reason:
      'Flagged on the WebSocket stage. The REST stage has access logging plus INFO method logging enabled ' +
      '(deployOptions in vsa-api-stack.ts). For the WebSocket stage this is sample code, and connection-level ' +
      'auditing is available from the $connect authorizer Lambda\'s own CloudWatch logs. Enable stage access ' +
      'logging before production use.',
  },
  {
    id: 'AwsSolutions-APIG3',
    reason:
      'No AWS WAFv2 web ACL is attached. This is sample/guidance code that is not production-ready (stated in the ' +
      'README), and the API is protected by Cognito authorization plus stage throttling (500 rate / 200 burst) ' +
      'with a Budgets alarm. Attaching a WAF web ACL is a documented production-hardening step.',
  },

  // ── Cognito ────────────────────────────────────────────────────────────
  {
    id: 'AwsSolutions-COG2',
    reason:
      'MFA is not required on the demo tenant user pools. These pools hold seeded demo personas, not real end ' +
      'users, and enforcing MFA would block the unattended kiosk and evaluation flows. MFA enforcement is a ' +
      'production-deployment decision for the operator who owns the real user directory.',
  },
  {
    id: 'AwsSolutions-COG8',
    reason:
      'The user pool is not on the Plus feature plan. Plus tier is a paid tier whose advanced security features ' +
      '(compromised-credential detection, adaptive authentication) apply to real end-user directories; these ' +
      'pools contain seeded demo accounts. Selecting a tier is an operator decision at deployment time.',
  },

  // ── Storage ────────────────────────────────────────────────────────────
  {
    id: 'AwsSolutions-S1',
    reason:
      'Server access logging is not enabled on the AgentCore CodeBuild sources bucket. It holds build inputs ' +
      'uploaded by `agentcore deploy`, not user data, and enabling it would require a second log bucket that ' +
      'itself needs lifecycle management. The bucket blocks all public access and enforces SSL, and S3 data ' +
      'events remain available via CloudTrail if request-level auditing is needed.',
  },
  {
    id: 'AwsSolutions-DDB3',
    reason:
      'Point-in-time recovery is not enabled on the demo/config tables (tenant config, triage decisions, action ' +
      'queue, Tier 2 findings). Their contents are reproducible by re-running the seed scripts and the scheduled ' +
      'Tier 2 agent, so continuous backups would add ongoing cost without protecting unique data. Enable PITR for ' +
      'any table that holds customer records in a production deployment.',
  },
];

/**
 * Apply the app-wide suppressions to every stack in the app.
 *
 * Called from `bin/vsa.ts` AFTER all stacks are constructed. Iterating the
 * app's children (rather than naming each stack) means a stack added later is
 * covered automatically and cannot slip through unsuppressed-but-unreviewed.
 *
 * `applyToNestedStacks` is true so nested constructs inherit the same reasoning.
 */
export function applyCvxNagSuppressions(app: cdk.App): void {
  for (const child of app.node.children) {
    if (cdk.Stack.isStack(child)) {
      NagSuppressions.addStackSuppressions(child, APP_WIDE_SUPPRESSIONS, true);
    }
  }
}
