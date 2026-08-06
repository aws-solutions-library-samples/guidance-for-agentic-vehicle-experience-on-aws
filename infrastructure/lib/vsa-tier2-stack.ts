/**
 * VSA Tier 2 CDK Stack — Platform Health Agent (S1: battery, tires, brakes)
 *
 * Spec: .kiro/specs/2026-08-03-cvx-tier2-platform-health-agent/spec.md
 * Decisions:
 *   - Substrate: Plan B — EventBridge Scheduler → Lambda → Strands Agent → Bedrock (T1.1)
 *   - ADP lake + Glue catalog are in us-east-1 for BOTH stages even though staging
 *     compute runs in us-west-2.  The Athena workgroup `cvx-{stage}-analytics` and
 *     results bucket already exist in the analytics stack (us-east-1).
 *   - Sort key is content-addressed: FINDING#{component}#{inputs_hash} (D4, corrected)
 *
 * IAM scoping (no wildcard resource ARNs per task brief):
 *   - Athena: scoped to the named workgroup ARN + results bucket ARN
 *   - S3 (ADP lake): scoped to the ADP foundation-lake bucket ARN
 *   - S3 (results): scoped to the CVX Athena results bucket ARN
 *   - DynamoDB: scoped to the health-findings table ARN
 *   - Bedrock: two statements per the cross-region inference-profile pattern
 *       (inference-profile carries the account ID; foundation-model does not)
 *
 * Region-suffix discipline (cross-region-namespace.md):
 *   - IAM role name:  vsa-{stage}-tier2-scheduler-{region}  — IAM is account-wide
 *   - DynamoDB table: vsa-{stage}-health-findings            — account-region scope, no suffix needed
 *   - Lambda function: vsa-{stage}-tier2-health-agent-{region} — account-region scope, included for clarity
 *
 * Length budget check (IAM role name, longest region ap-southeast-7 = 14 chars):
 *   "vsa-staging-tier2-scheduler-ap-southeast-7" = 42 chars  ≤ 64 ✓
 *   "vsa-staging-tier2-health-agent-ap-southeast-7" = 45 chars ≤ 64 ✓
 */

import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as scheduler from 'aws-cdk-lib/aws-scheduler';
import * as schedulerTargets from 'aws-cdk-lib/aws-scheduler-targets';
import { Construct } from 'constructs';
import * as path from 'path';

// ─── Props ─────────────────────────────────────────────────────────────────

interface VsaTier2StackProps extends cdk.StackProps {
  /** VSA stage: 'staging' | 'prod'. */
  stage: string;
  /**
   * AWS account ID of the ADP account where the lake lives.
   * Defaults to the deploying account (same-account ADP setup used in staging).
   * Pass via CDK context `adpAccountId` or env var ADP_ACCOUNT_ID.
   */
  adpAccountId?: string;
  /**
   * Region where ADP's Glue catalog and Athena workgroup reside.
   * Per decisions.md: us-east-1 for BOTH stages.
   * Defaults to 'us-east-1'; override via CDK context `adpRegion`.
   */
  adpRegion?: string;
}

// ─── Stack ──────────────────────────────────────────────────────────────────

export class VsaTier2Stack extends cdk.Stack {
  /** Health-findings DynamoDB table — read by the Tier 1 health_findings() tool. */
  readonly healthFindingsTable: dynamodb.Table;

  constructor(scope: Construct, id: string, props: VsaTier2StackProps) {
    super(scope, id, props);

    const region  = this.region;
    const account = this.account;
    const stage   = props.stage;

    // ADP coordinates.  Per decisions.md T1.2: ADP lake + Glue catalog are in
    // us-east-1 for BOTH CVX stages regardless of where CVX compute runs.
    const adpRegion  = props.adpRegion  ?? 'us-east-1';
    const adpAccount = props.adpAccountId ?? account;  // same-account by default

    // ADP stage mapping — analytics stack uses the VSA stage directly.
    // Both 'staging' and 'prod' Athena workgroups already exist in the analytics stack.
    const adpStage = stage;   // e.g. 'staging' → adp_staging_energy_usage

    // ── Bedrock model IDs (mirrors vsa-core-stack.ts) ──
    const claudeProfileId = 'us.anthropic.claude-sonnet-4-6';
    const claudeFmId      = 'anthropic.claude-sonnet-4-6';
    // Claude Sonnet 4.6 US inference profile routes to these three regions.
    const claudeFmRegions = ['us-east-1', 'us-east-2', 'us-west-2'];

    // ── D4: Artifact table ─────────────────────────────────────────────────
    //
    // Table name: vsa-{stage}-health-findings
    // DynamoDB tables are account-region scoped — no region suffix needed to
    // prevent cross-region collisions (unlike S3 buckets or IAM roles).
    // The table is accessed from both the Tier 2 Lambda (compute region) and
    // the Tier 1 tool (runtime region) via client-injected boto3 clients, so
    // the table lives in the stack's deployment region.
    this.healthFindingsTable = new dynamodb.Table(this, 'HealthFindings', {
      tableName: `vsa-${stage}-health-findings`,
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
      sortKey:      { name: 'sk', type: dynamodb.AttributeType.STRING },
      billingMode:  dynamodb.BillingMode.PAY_PER_REQUEST,
      // TTL attribute per spec § D4 — superseded findings age out.
      timeToLiveAttribute: 'ttl',
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    new cdk.CfnOutput(this, 'HealthFindingsTableName', {
      value:      this.healthFindingsTable.tableName,
      exportName: `vsa-${stage}-health-findings-table-name`,
    });

    // ── Lambda execution role ──────────────────────────────────────────────
    //
    // IAM roles are account-wide — must include region suffix so a second-region
    // deploy cannot collide with the primary-region role (cross-region-namespace.md).
    // Name: vsa-{stage}-tier2-scheduler-{region}
    // Length check: "vsa-staging-tier2-scheduler-ap-southeast-7" = 42 chars ≤ 64 ✓
    const lambdaRole = new iam.Role(this, 'Tier2HealthAgentRole', {
      roleName: `vsa-${stage}-tier2-scheduler-${region}`,
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: `Execution role for the Tier 2 platform health agent Lambda (${stage}, ${region})`,
    });

    // ── CloudWatch Logs baseline ───────────────────────────────────────────
    lambdaRole.addToPolicy(new iam.PolicyStatement({
      sid: 'CloudWatchLogsBaseline',
      effect: iam.Effect.ALLOW,
      actions: [
        'logs:CreateLogGroup',
        'logs:CreateLogStream',
        'logs:PutLogEvents',
      ],
      // Lambda log groups follow the /aws/lambda/{function-name} pattern.
      // Scoped to the function's log group; the function name is deterministic.
      resources: [
        `arn:aws:logs:${region}:${account}:log-group:/aws/lambda/vsa-${stage}-tier2-health-agent-${region}`,
        `arn:aws:logs:${region}:${account}:log-group:/aws/lambda/vsa-${stage}-tier2-health-agent-${region}:*`,
      ],
    }));

    // ── DynamoDB: artifact table (write + read-back) ───────────────────────
    //
    // Scoped to the specific table ARN (no wildcard).
    // The Tier 2 Lambda writes findings; it also reads to enforce idempotency
    // (the conditional PutItem relies on ConditionExpression, not a prior read,
    // but PutItem requires no additional read permission).
    lambdaRole.addToPolicy(new iam.PolicyStatement({
      sid: 'HealthFindingsTableWrite',
      effect: iam.Effect.ALLOW,
      actions: [
        // PutItem only.  The Tier 2 writer calls put_item and nothing else; the
        // conditional put relies on ConditionExpression, which needs no read grant.
        // The Tier 1 health_findings() read runs in the AgentCore runtime role,
        // NOT this Lambda, so GetItem/Query do not belong here.
        'dynamodb:PutItem',
      ],
      resources: [this.healthFindingsTable.tableArn],
    }));

    // ── Athena: workgroup scoped to the CVX analytics workgroup ───────────
    //
    // The Athena workgroup `cvx-{stage}-analytics` lives in us-east-1 (analytics
    // stack).  Athena workgroup ARNs are cross-region from the Lambda's perspective
    // (Lambda runs in this.region; Athena workgroup is in adpRegion=us-east-1).
    // IAM actions are evaluated against the resource ARN regardless of caller region.
    const athenaWorkgroupArn =
      `arn:aws:athena:${adpRegion}:${account}:workgroup/cvx-${adpStage}-analytics`;

    lambdaRole.addToPolicy(new iam.PolicyStatement({
      sid: 'AthenaWorkgroupAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        // Exactly the three calls agents/tier2/adp_source.py makes.  Passing
        // WorkGroup= to StartQueryExecution does not require athena:GetWorkGroup,
        // and nothing cancels a query, so StopQueryExecution is unused.
        'athena:StartQueryExecution',
        'athena:GetQueryExecution',
        'athena:GetQueryResults',
      ],
      resources: [athenaWorkgroupArn],
    }));

    // ── S3: Athena results bucket ──────────────────────────────────────────
    //
    // Bucket name from analytics stack: cvx-{stage}-athena-results-{account}-us-east-1
    // Scoped to this specific bucket + its objects (no wildcard buckets).
    const athenaResultsBucketArn =
      `arn:aws:s3:::cvx-${adpStage}-athena-results-${account}-${adpRegion}`;

    lambdaRole.addToPolicy(new iam.PolicyStatement({
      sid: 'AthenaResultsBucketAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        's3:PutObject',
        's3:GetObject',
        's3:GetBucketLocation',
        's3:ListBucket',
      ],
      resources: [
        athenaResultsBucketArn,
        `${athenaResultsBucketArn}/*`,
      ],
    }));

    // ── S3: ADP foundation lake (direct parquet read) ──────────────────────
    //
    // LF-registered tables do not vend S3 credentials for this query path.
    // Athena reads the underlying Parquet with the CALLER's IAM identity.
    // Bucket: adp-{stage}-foundation-lake-{adpAccount}-{adpRegion}
    //
    // T3.6 (security-review Cycle 4): scope GetObject to the energy_usage
    // prefix and add an s3:prefix condition on ListBucket.  The whole-bucket
    // grant allowed the Lambda to read any ADP product's parquet directly once
    // LF opened any product.  Athena reads use the caller's IAM identity (not
    // LF-vended credentials), so the IAM policy IS the data-plane authority
    // for this path.  Prefix-scoping bounds blast radius if a future LF grant
    // is added or mis-scoped — defense in depth.
    //
    // Prefix convention: ADP lake convention is curated/{product_name}/…
    // (decisions.md § 2026-08-03; ADP foundation-v1 schema docs).
    // Three products are read in S1: energy_usage (battery), tire_health (tires),
    // service_records (brakes).  All three were unblocked and verified live by 2026-08-04.
    const adpLakeBucketArn =
      `arn:aws:s3:::adp-${adpStage}-foundation-lake-${adpAccount}-${adpRegion}`;

    lambdaRole.addToPolicy(new iam.PolicyStatement({
      sid: 'AdpLakeS3Read',
      effect: iam.Effect.ALLOW,
      actions: [
        's3:GetObject',
        's3:GetBucketLocation',
      ],
      resources: [
        // Object access scoped to the three product prefixes only (T4.5 extends T3.6).
        // No bucket-wide GetObject — T3.6 established prefix-scoping as defense in depth.
        `${adpLakeBucketArn}/curated/energy_usage/*`,
        `${adpLakeBucketArn}/curated/tire_health/*`,
        `${adpLakeBucketArn}/curated/service_records/*`,
      ],
    }));

    lambdaRole.addToPolicy(new iam.PolicyStatement({
      sid: 'AdpLakeS3List',
      effect: iam.Effect.ALLOW,
      actions: ['s3:ListBucket'],
      resources: [adpLakeBucketArn],
      conditions: {
        // s3:prefix condition limits ListBucket to the three product prefixes.
        // Both forms are included per prefix: the bare form for directory listings
        // and the wildcard form for object-key prefix matching.
        StringLike: {
          's3:prefix': [
            'curated/energy_usage',
            'curated/energy_usage/*',
            'curated/tire_health',
            'curated/tire_health/*',
            'curated/service_records',
            'curated/service_records/*',
          ],
        },
      },
    }));

    // ── KMS: ADP foundation lake SSE-KMS decrypt ───────────────────────────
    //
    // T3.5 (security-review Cycle 4): the ADP foundation lake bucket is
    // SSE-KMS encrypted.  Even after LF grants open the metadata plane, Athena
    // reads the underlying Parquet via the caller's IAM identity and S3 requires
    // kms:Decrypt on the bucket's CMK.  Without this grant the read fails with
    // KMS AccessDenied before any data is returned.
    //
    // Verified against live bucket encryption state 2026-08-04: the ADP lake
    // bucket has SSEAlgorithm aws:kms with a CMK and BucketKeyEnabled true.
    // Identifiers are deliberately not reproduced here — this file is on the
    // public-mirror publish path and is neither scan_exclude'd nor
    // publish-exclude'd.  The ADP lake stack exports the key ARN as
    // `LakeKmsKeyArn` if you need to confirm it.
    //
    // Precedent: vsa-core-stack.ts sid `AdpLakeKmsDecrypt` — same key scope with
    // the same ViaService condition.
    //
    // Why the wildcard key scope rather than the specific key ARN: NOT because
    // of key rotation — AWS rotation preserves the key ARN and rotates only the
    // backing material, so pinning would survive it fine.  The actual reason is
    // that CloudFormation exports are region-scoped, so Fn::ImportValue cannot
    // reach the ADP lake stack's export from this stack's region, which would
    // push the literal ARN into bin/vsa.ts STAGE_DEFAULTS.  The wildcard is
    // bounded twice over: the ViaService condition restricts it to S3-mediated
    // decrypts in adpRegion, and the AdpLakeS3Read grant is scoped to a single
    // product prefix.  An env-var-passed prop (mirroring cmsKmsKeyArn) is the
    // available hardening and is filed as a follow-on.
    //
    // kms:GenerateDataKey is NOT needed for reads — only writes require it.
    lambdaRole.addToPolicy(new iam.PolicyStatement({
      sid: 'AdpLakeKmsDecrypt',
      effect: iam.Effect.ALLOW,
      actions: ['kms:Decrypt', 'kms:DescribeKey'],
      // Scoped to any CMK in the ADP account/region — mirrors vsa-core-stack.ts.
      // See the rationale above: the ViaService condition plus the prefix-scoped
      // S3 grant are the binding constraints, not the resource ARN.
      resources: [`arn:aws:kms:${adpRegion}:${adpAccount}:key/*`],
      conditions: {
        // Binds Decrypt to S3 calls in the ADP lake region only.
        // An unconditioned Decrypt on the ADP account's KMS keys would be
        // over-broad; ViaService ensures this grant fires only when S3 needs
        // to decrypt an object on the caller's behalf.
        StringEquals: { 'kms:ViaService': `s3.${adpRegion}.amazonaws.com` },
      },
    }));

    // ── Glue + Lake Formation (Athena metadata plane) ──────────────────────
    //
    // Glue catalogue access is needed for Athena to resolve database/table
    // metadata.  Lake Formation's GetDataAccess issues short-lived S3 credentials.
    // Three products are read in S1: energy_usage (battery), tire_health (tires),
    // service_records (brakes).  Glue resources are scoped to those three databases.
    // LakeFormation:GetDataAccess has no ARN-level scoping — must be *.
    lambdaRole.addToPolicy(new iam.PolicyStatement({
      sid: 'AdpGlueMetadataRead',
      effect: iam.Effect.ALLOW,
      actions: [
        'glue:GetDatabase',
        'glue:GetTable',
        'glue:GetTables',
        'glue:GetPartitions',
      ],
      resources: [
        // Catalog (required alongside database grants)
        `arn:aws:glue:${adpRegion}:${adpAccount}:catalog`,
        // energy_usage database + all its tables (battery component)
        `arn:aws:glue:${adpRegion}:${adpAccount}:database/adp_${adpStage}_energy_usage`,
        `arn:aws:glue:${adpRegion}:${adpAccount}:table/adp_${adpStage}_energy_usage/*`,
        // tire_health database + all its tables (tires component, unblocked 2026-08-04)
        `arn:aws:glue:${adpRegion}:${adpAccount}:database/adp_${adpStage}_tire_health`,
        `arn:aws:glue:${adpRegion}:${adpAccount}:table/adp_${adpStage}_tire_health/*`,
        // service_records database + all its tables (brakes component, unblocked 2026-08-04)
        `arn:aws:glue:${adpRegion}:${adpAccount}:database/adp_${adpStage}_service_records`,
        `arn:aws:glue:${adpRegion}:${adpAccount}:table/adp_${adpStage}_service_records/*`,
      ],
    }));

    lambdaRole.addToPolicy(new iam.PolicyStatement({
      sid: 'LakeFormationGetDataAccess',
      effect: iam.Effect.ALLOW,
      actions: ['lakeformation:GetDataAccess'],
      resources: ['*'],
      // Note: LakeFormation:GetDataAccess does not support resource-level
      // conditions — AWS docs confirm * is required here.
    }));

    // ── Bedrock: Claude Sonnet 4.6 — two statements per cross-region pattern ──
    //
    // Per ~/.kiro/skills/ verified Bedrock IAM patterns (cross-region inference profiles):
    //   Statement 1 — inference-profile ARN: carries the account ID
    //   Statement 2 — foundation-model ARN: does NOT carry account ID (empty between :::)
    //
    // Both statements are required.  Scoped to the specific profile/model IDs.
    lambdaRole.addToPolicy(new iam.PolicyStatement({
      sid: 'BedrockInvokeClaudeInferenceProfile',
      effect: iam.Effect.ALLOW,
      actions: [
        'bedrock:InvokeModel',
        'bedrock:InvokeModelWithResponseStream',
      ],
      // Inference-profile ARNs in BOTH regions where CVX runs today.
      // Account ID is REQUIRED in inference-profile ARNs.
      resources: [
        `arn:aws:bedrock:us-east-1:${account}:inference-profile/${claudeProfileId}`,
        `arn:aws:bedrock:us-west-2:${account}:inference-profile/${claudeProfileId}`,
      ],
    }));

    lambdaRole.addToPolicy(new iam.PolicyStatement({
      sid: 'BedrockInvokeClaudeFoundationModel',
      effect: iam.Effect.ALLOW,
      actions: [
        'bedrock:InvokeModel',
        'bedrock:InvokeModelWithResponseStream',
      ],
      // Foundation-model ARNs have NO account ID (double-colon between partition and qualifier).
      // This covers the three regions the US cross-region inference profile can route to.
      resources: claudeFmRegions.map(r => `arn:aws:bedrock:${r}::foundation-model/${claudeFmId}`),
    }));

    // ── Lambda function ────────────────────────────────────────────────────
    //
    // Function name: vsa-{stage}-tier2-health-agent-{region}
    // Length check: "vsa-staging-tier2-health-agent-ap-southeast-7" = 45 chars ≤ 64 ✓
    // Lambda functions are account-region scoped; region suffix included for explicitness
    // and to mirror the role naming convention.
    const logGroup = new logs.LogGroup(this, 'Tier2HealthAgentLogs', {
      logGroupName: `/aws/lambda/vsa-${stage}-tier2-health-agent-${region}`,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    const healthAgentFn = new lambda.Function(this, 'Tier2HealthAgentFn', {
      functionName: `vsa-${stage}-tier2-health-agent-${region}`,
      runtime: lambda.Runtime.PYTHON_3_12,
      // The asset bundles from the project root (path.join(__dirname, '../../')), so the
      // package prefix is required.  Module path: agents/tier2/runner.py, function: lambda_handler.
      // Dot-separated from the asset root: agents.tier2.runner.lambda_handler
      handler: 'agents.tier2.runner.lambda_handler',
      // The runner lives in agents/tier2/; the Lambda asset bundles from project root
      // so relative imports work correctly.
      code: lambda.Code.fromAsset(
        path.join(__dirname, '../../'),
        {
          // Bundle only the tier2 platform + health agent code.
          // Exclude test files, heavy assets, and the venv.
          exclude: [
            '.venv/**',
            '.venv',
            'node_modules/**',
            'infrastructure/**',
            'evals/**',
            'corpora/**',
            'documentation/**',
            'docs/*.pdf',
            '.git/**',
            '**/*.pyc',
            '**/__pycache__',
            '.bedrock_agentcore/**',
            'scripts/build-api-*.sh',
          ],
        },
      ),
      role: lambdaRole,
      timeout: cdk.Duration.minutes(15),
      memorySize: 512,
      logGroup,
      environment: {
        // ── Spec § "The environment contract between the stack and the runner" ──────
        // The stack is the source of truth.  It resolves account, region, and stage at
        // synth time so the runner consumes resolved values and derives nothing.
        // Variable names match the spec contract table exactly — any rename here MUST
        // be reflected in runner.py and the bidirectional contract test.
        //
        // | Variable                  | Set to                                                   |
        // | ------------------------- | -------------------------------------------------------- |
        // | VSA_STAGE                 | stage                                                    |
        // | HEALTH_FINDINGS_TABLE     | vsa-{stage}-health-findings                              |
        // | DYNAMODB_REGION           | Lambda's own region                                      |
        // | ADP_REGION                | us-east-1  (ADP lake is always us-east-1)                |
        // | ATHENA_WORKGROUP          | cvx-{stage}-analytics                                    |
        // | ATHENA_RESULTS_BUCKET     | fully resolved bucket name (account + region embedded)   |
        // | TIER2_MAX_TOKENS          | budget token ceiling default                             |
        // | TIER2_MAX_WALL_SECONDS    | budget wall-clock ceiling default (seconds)              |
        // | TIER2_MAX_VINS_PER_RUN    | VIN cap per run; deterministic ORDER BY vin subquery     |
        // | TIER2_MAX_ROWS_PER_QUERY  | Athena result-page ceiling; AthenaResultTooLargeError     |
        //
        // ADP_ACCOUNT is intentionally absent from the runner's environment:
        // the runner never needs to derive a bucket name from an account id — it consumes
        // ATHENA_RESULTS_BUCKET fully resolved.  Client-side reconstruction of a bucket
        // name from an account id is the defect class that produced Critical #2.
        // The IAM role's cross-account Glue/S3 grants are scoped at synth time from
        // adpAccount; no runtime variable is needed.
        VSA_STAGE:                stage,
        HEALTH_FINDINGS_TABLE:    `vsa-${stage}-health-findings`,
        DYNAMODB_REGION:          region,
        ADP_REGION:               adpRegion,
        ATHENA_WORKGROUP:         `cvx-${adpStage}-analytics`,
        // Fully resolved — account id and region are substituted at synth time.
        // The runner reads this value directly; it never reconstructs it from parts.
        ATHENA_RESULTS_BUCKET:    `cvx-${adpStage}-athena-results-${account}-${adpRegion}`,
        TIER2_MAX_TOKENS:         '50000',
        TIER2_MAX_WALL_SECONDS:   '600',
        // VIN cap: how many distinct VINs to sample per component run.
        // The ORDER BY vin subquery makes selection deterministic (T6a.3) — same VINs
        // every run, which is required for inputs_hash stability (M6 idempotency).
        TIER2_MAX_VINS_PER_RUN:   '25',
        // Athena result-page ceiling.  Exceeding this raises AthenaResultTooLargeError
        // rather than returning a silently-truncated set (T6a.1).  Sized above the
        // 3-VIN test case (721 rows) with headroom for the default 25-VIN cap.
        TIER2_MAX_ROWS_PER_QUERY: '50000',
      },
    });

    // ── D2: Per-component EventBridge Schedules (net-new to CVX) ─────────────
    //
    // One schedule per component, staggered 20 minutes apart, each passing the
    // component name as its Input (T6a.5).  This ensures:
    //
    //   1. The budget cap means "per component run" unambiguously — a single
    //      invocation looping three components would share or silently apply three
    //      caps; separate invocations each start with a fresh cap.
    //   2. Without an explicit Input the runner silently falls back to battery-only,
    //      so tires and brakes would never execute — T6a.5 fixes this.
    //   3. Staggered times prevent concurrent workgroup contention.
    //
    // Scheduler role: separate IAM role so the scheduler service principal can
    // call lambda:InvokeFunction without conflating it with the Lambda's own role.
    //
    // Role name: vsa-{stage}-tier2-sched-invoker-{region}
    // Length: "vsa-staging-tier2-sched-invoker-ap-southeast-7" = 46 chars ≤ 64 ✓
    const schedulerRole = new iam.Role(this, 'Tier2SchedulerInvokerRole', {
      roleName: `vsa-${stage}-tier2-sched-invoker-${region}`,
      assumedBy: new iam.ServicePrincipal('scheduler.amazonaws.com', {
        conditions: {
          StringEquals: { 'aws:SourceAccount': account },
        },
      }),
      description: `EventBridge Scheduler invoker role for Tier 2 health agent (${stage}, ${region})`,
    });

    // Scope to exactly this Lambda function ARN.
    schedulerRole.addToPolicy(new iam.PolicyStatement({
      sid: 'InvokeTier2HealthAgentLambda',
      effect: iam.Effect.ALLOW,
      actions: ['lambda:InvokeFunction'],
      resources: [healthAgentFn.functionArn],
    }));

    // Three schedules, one per component, staggered by 20 minutes.
    // Input carries {"stage": "...", "component": "..."} so runner.py
    // knows which FindingSource to instantiate — without it the runner falls
    // through to the battery-only default path (the defect T6a.5 was filed to fix).
    const componentSchedules: Array<{ component: string; hour: string; minute: string }> = [
      { component: 'battery', hour: '2', minute: '0'  },
      { component: 'tires',   hour: '2', minute: '20' },
      { component: 'brakes',  hour: '2', minute: '40' },
    ];

    for (const { component, hour, minute } of componentSchedules) {
      const scheduleTarget = new schedulerTargets.LambdaInvoke(healthAgentFn, {
        role: schedulerRole,
        // Retry policy: 2 attempts, maximum event age 1 hour.
        retryAttempts: 2,
        maxEventAge: cdk.Duration.hours(1),
        // ScheduleTargetInput.fromObject serialises to JSON at synth time and
        // CloudFormation writes a literal JSON string into the CfnSchedule Input field.
        input: scheduler.ScheduleTargetInput.fromObject({ stage, component }),
      });

      // Logical ID uses the PascalCase component name so CloudFormation tracks each
      // schedule independently rather than replacing the earlier single schedule.
      const capitalised = component.charAt(0).toUpperCase() + component.slice(1);
      new scheduler.Schedule(this, `Tier2Daily${capitalised}Schedule`, {
        scheduleName: `vsa-${stage}-tier2-daily-${component}-${region}`,
        schedule: scheduler.ScheduleExpression.cron({
          minute,
          hour,
          day:   '*',
          month: '*',
          year:  '*',
        }),
        target: scheduleTarget,
        description: `Daily Tier 2 health agent run — ${component} component — VSA ${stage}`,
        // Enabled by default; operator can disable individually for controlled rollout.
        enabled: true,
      });
    }

    // ── CloudWatch alarms ─────────────────────────────────────────────────
    //
    // Two alarms per spec task brief:
    //   1. Run failure — fires when the Lambda errors (including budget-cap aborts
    //      that exit non-zero).
    //   2. Run cost / estimated charge — fires when billed duration exceeds threshold.
    //      Serves as a proxy for runaway LLM cost before billing alerts trigger.
    const runFailureAlarm = new cloudwatch.Alarm(this, 'Tier2RunFailureAlarm', {
      alarmName:   `vsa-${stage}-tier2-run-failure-${region}`,
      alarmDescription:
        'Tier 2 health agent Lambda error rate > 0 in a 5-minute window. ' +
        'Investigate CloudWatch Logs for the abort reason.',
      metric: healthAgentFn.metricErrors({
        period:    cdk.Duration.minutes(5),
        statistic: 'Sum',
      }),
      threshold:          1,
      evaluationPeriods:  1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData:   cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // Duration alarm — fires if a run exceeds 10 minutes (600s).  This is a
    // leading indicator of runaway reasoning before the hard cap kicks in.
    const runDurationAlarm = new cloudwatch.Alarm(this, 'Tier2RunDurationAlarm', {
      alarmName:   `vsa-${stage}-tier2-run-duration-${region}`,
      alarmDescription:
        'Tier 2 health agent Lambda duration p99 > 600s (10 min). ' +
        'Verify the per-run budget cap (TIER2_MAX_WALL_SECONDS) is being enforced.',
      metric: healthAgentFn.metricDuration({
        period:    cdk.Duration.minutes(5),
        statistic: 'p99',
      }),
      threshold:          600_000,  // Lambda metric is in milliseconds
      evaluationPeriods:  1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData:   cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // ── Outputs ───────────────────────────────────────────────────────────

    new cdk.CfnOutput(this, 'Tier2HealthAgentFnArn', {
      value:      healthAgentFn.functionArn,
      exportName: `vsa-${stage}-tier2-health-agent-fn-arn`,
      description: 'ARN of the Tier 2 platform health agent Lambda',
    });

    new cdk.CfnOutput(this, 'Tier2RunFailureAlarmArn', {
      value:      runFailureAlarm.alarmArn,
      exportName: `vsa-${stage}-tier2-run-failure-alarm-arn`,
    });

    new cdk.CfnOutput(this, 'Tier2RunDurationAlarmArn', {
      value:      runDurationAlarm.alarmArn,
      exportName: `vsa-${stage}-tier2-run-duration-alarm-arn`,
    });
  }
}
