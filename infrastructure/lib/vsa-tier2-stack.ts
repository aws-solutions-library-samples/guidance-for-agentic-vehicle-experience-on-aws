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
import { ConfigValue, resolveConfigValue } from './config-value';

import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cloudwatchActions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as events from 'aws-cdk-lib/aws-events';
import * as eventsTargets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as scheduler from 'aws-cdk-lib/aws-scheduler';
import * as schedulerTargets from 'aws-cdk-lib/aws-scheduler-targets';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as snsSubscriptions from 'aws-cdk-lib/aws-sns-subscriptions';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as lambdaEventSources from 'aws-cdk-lib/aws-lambda-event-sources';
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
  /**
   * KMS key ARN used to encrypt the operator-alert SNS topic (spec § D6).
   * The CVX CMK is already available in vsa-core-stack; pass via env var
   * CMS_KMS_KEY_ARN or CDK context. When absent, the topic is created
   * unencrypted — a synth-time warning is emitted. Not fail-closed because
   * the CMK ARN is Tier A (SSM-resolved by CloudFormation) and not available
   * as a concrete string at synth in the standard SSM-backed path.
   *
   * In tests and hermetic synths pass the ARN directly so the topic is
   * encrypted and the test assertions on KmsMasterKeyId pass.
   */
  cmsKmsKeyArn?: ConfigValue;
}

// ─── Stack ──────────────────────────────────────────────────────────────────

export class VsaTier2Stack extends cdk.Stack {
  /** Health-findings DynamoDB table — read by the Tier 1 health_findings() tool. */
  readonly healthFindingsTable: dynamodb.Table;

  // ── AVX data-plane tables (Group 3, Spec 1a) ──────────────────────────
  /** AVX Findings table — written by Tier 2 agents, read by Tier 1 tools. */
  readonly avxFindingsTable: dynamodb.Table;
  /** AVX Actions table — written by Tier 1 approve/dismiss routes. */
  readonly avxActionsTable: dynamodb.Table;
  /** AVX Feedback table — owner dismissal signals, 30-day TTL. */
  readonly avxFeedbackTable: dynamodb.Table;
  /** Notification devices table — APNs device tokens + per-category consent. */
  readonly notificationDevicesTable: dynamodb.Table;
  /** AVX rate-limit counters — per-owner day/hour windows with TTL. */
  readonly avxRateLimitsTable: dynamodb.Table;

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

    // ── F11.2: CMS vehicle-registry coordinates ──────────────────────────────
    //
    // The registry (cms-{stage}-storage-vehicles) is the authoritative VIN →
    // owner-of-record source; Tier 2 reads `sold_to` from it to project a
    // top-level `owner_id` onto AVX Finding DDB items.
    //
    // Stage tracks the CVX stage, matching the adpStage convention above: every
    // deployment to date pairs cms-staging with vsa-staging.
    //
    // Account and region default to this stack's own, because CMS staging and CVX
    // staging are co-located in the same account and region (verified 2026-09-23).
    // They are overridable via context so the IAM ARN can name an explicit
    // account and region if the two ever split, per F11.2 Accept 5 — the same
    // code shape works either way and only the context value changes.
    //
    // NOTE: a cross-account value here grants nothing on its own. The registry's
    // OWNING account would also need a resource-based policy or a role this
    // Lambda can assume. The warning below says so rather than letting a
    // context value imply cross-account support that does not exist yet.
    const cmsStage   = stage;
    const cmsAccount = this.node.tryGetContext('cmsAccount') ?? account;
    const cmsRegion  = this.node.tryGetContext('cmsRegion')  ?? region;
    const cmsVehiclesTableArn =
      `arn:aws:dynamodb:${cmsRegion}:${cmsAccount}:table/cms-${cmsStage}-storage-vehicles`;

    if (cmsAccount !== account || cmsRegion !== region) {
      cdk.Annotations.of(this).addWarning(
        `CMS vehicle registry is configured cross-account/cross-region ` +
        `(${cmsAccount}/${cmsRegion} vs this stack's ${account}/${region}). ` +
        `The Tier 2 IAM grant alone is NOT sufficient: the owning account must ` +
        `also permit this role via a resource policy or an assumable role, ` +
        `otherwise VIN -> owner resolution fails closed and every AVX Finding ` +
        `is written without an owner_id projection.`,
      );
    }

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

    // ── AVX data-plane tables (Group 3 — Spec 1a substrate) ──────────────
    //
    // Five tables that form the AVX findings-and-actions data plane.
    // All follow the same conventions as HealthFindings:
    //   - PAY_PER_REQUEST billing (no capacity planning)
    //   - PITR enabled (point-in-time recovery, 35-day window)
    //   - RemovalPolicy.RETAIN (prevent accidental deletion of live data)
    //   - No explicit encryption block — CDK default (TableEncryption.DEFAULT →
    //     AWS-owned key, no CloudTrail visibility). Matches HealthFindings above.
    //
    // Attribute names in GSIs MUST match the JSON Schema field names verbatim
    // (finding_id, finding_key, owner_id, vin, fleet_id, dealership_id,
    // district_id, agent_id, computed_at, action_id, created_by,
    // target_system, target_ref) per the Group 3 task brief and frozen
    // schemas in agents/tier2/schemas/{finding,action}.schema.json.
    //
    // Length budget (DynamoDB table names, account-region scoped, no suffix needed):
    //   "vsa-staging-avx-findings"          = 24 chars
    //   "vsa-staging-avx-actions"           = 23 chars
    //   "vsa-staging-avx-feedback"          = 24 chars
    //   "vsa-staging-notification-devices"  = 31 chars
    //   "vsa-staging-avx-rate-limits"       = 27 chars
    //   All well within the 255-char DynamoDB table name limit.

    // ── G3.T1: AvxFindings ────────────────────────────────────────────────
    //
    // PK: finding_id (S)
    // Stream: NEW_IMAGE — consumed by the broker (Group 7) to fan-out push notifications.
    //
    // GSIs (verbatim attribute names per schema):
    //   by_owner          → owner_id        (owner-scoped query)
    //   by_vin            → vin             (vehicle-scoped query)
    //   by_fleet          → fleet_id        (fleet-scoped query, sparse)
    //   by_dealership     → dealership_id   (dealership-scoped query, sparse)
    //   by_district       → district_id     (district-scoped query, sparse)
    //   by_agent_computed_at → agent_id (HASH) + computed_at (RANGE)  — per-agent chronological read
    //   by_finding_key    → finding_key     — D2 open-condition writer lookup (NOT sparse)
    //
    // Scope-field GSIs are sparse: only one of owner_id/vin/fleet_id/dealership_id/
    // district_id is populated per Finding (exactly-one-scope invariant from the JSON Schema).
    // by_finding_key is not sparse — every Finding has a finding_key (Addendum D2 mandatory).
    this.avxFindingsTable = new dynamodb.Table(this, 'AvxFindings', {
      tableName:    `vsa-${stage}-avx-findings`,
      partitionKey: { name: 'finding_id', type: dynamodb.AttributeType.STRING },
      billingMode:  dynamodb.BillingMode.PAY_PER_REQUEST,
      stream:       dynamodb.StreamViewType.NEW_IMAGE,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.avxFindingsTable.addGlobalSecondaryIndex({
      indexName:    'by_owner',
      partitionKey: { name: 'owner_id', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    this.avxFindingsTable.addGlobalSecondaryIndex({
      indexName:    'by_vin',
      partitionKey: { name: 'vin', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    // GET /findings/me driver standing (spec 2026-09-25-avx-own-vehicle-findings T2.1):
    // intersects the caller's trusted custom:vehicleId with the projected vehicle_id,
    // with no CMS read on the authorization path (identity contract § 6).
    this.avxFindingsTable.addGlobalSecondaryIndex({
      indexName:    'by_vehicle',
      partitionKey: { name: 'vehicle_id', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    this.avxFindingsTable.addGlobalSecondaryIndex({
      indexName:    'by_fleet',
      partitionKey: { name: 'fleet_id', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    this.avxFindingsTable.addGlobalSecondaryIndex({
      indexName:    'by_dealership',
      partitionKey: { name: 'dealership_id', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    this.avxFindingsTable.addGlobalSecondaryIndex({
      indexName:    'by_district',
      partitionKey: { name: 'district_id', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    this.avxFindingsTable.addGlobalSecondaryIndex({
      indexName:    'by_agent_computed_at',
      partitionKey: { name: 'agent_id',     type: dynamodb.AttributeType.STRING },
      sortKey:      { name: 'computed_at',  type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    this.avxFindingsTable.addGlobalSecondaryIndex({
      indexName:    'by_finding_key',
      partitionKey: { name: 'finding_key', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    new cdk.CfnOutput(this, 'AvxFindingsTableName', {
      value:      this.avxFindingsTable.tableName,
      exportName: `vsa-${stage}-avx-findings-table-name`,
    });

    // ── G3.T2: AvxActions ─────────────────────────────────────────────────
    //
    // PK: action_id (S)
    //
    // GSIs (verbatim attribute names per action.schema.json):
    //   by_finding      → finding_id       (look up all Actions for a Finding)
    //   by_created_by   → created_by       (all Actions by a given actor)
    //   by_target_system → target_system   (all Actions routed to a specific executor)
    //   by_target_ref   → target_ref       (reverse lookup from downstream ID; sparse —
    //                                       populated only after successful executor dispatch)
    this.avxActionsTable = new dynamodb.Table(this, 'AvxActions', {
      tableName:    `vsa-${stage}-avx-actions`,
      partitionKey: { name: 'action_id', type: dynamodb.AttributeType.STRING },
      billingMode:  dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.avxActionsTable.addGlobalSecondaryIndex({
      indexName:    'by_finding',
      partitionKey: { name: 'finding_id', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    this.avxActionsTable.addGlobalSecondaryIndex({
      indexName:    'by_created_by',
      partitionKey: { name: 'created_by', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    this.avxActionsTable.addGlobalSecondaryIndex({
      indexName:    'by_target_system',
      partitionKey: { name: 'target_system', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    this.avxActionsTable.addGlobalSecondaryIndex({
      indexName:    'by_target_ref',
      partitionKey: { name: 'target_ref', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    new cdk.CfnOutput(this, 'AvxActionsTableName', {
      value:      this.avxActionsTable.tableName,
      exportName: `vsa-${stage}-avx-actions-table-name`,
    });

    // ── G3.T3a: AvxFeedback ───────────────────────────────────────────────
    //
    // Owner dismissal history. 30-day TTL via the `ttl` attribute.
    // PK: owner_id (S), SK: feedback_id (S)
    // Access pattern: scan all feedback for an owner (owner-scoped reads only).
    this.avxFeedbackTable = new dynamodb.Table(this, 'AvxFeedback', {
      tableName:    `vsa-${stage}-avx-feedback`,
      partitionKey: { name: 'owner_id',    type: dynamodb.AttributeType.STRING },
      sortKey:      { name: 'feedback_id', type: dynamodb.AttributeType.STRING },
      billingMode:  dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    new cdk.CfnOutput(this, 'AvxFeedbackTableName', {
      value:      this.avxFeedbackTable.tableName,
      exportName: `vsa-${stage}-avx-feedback-table-name`,
    });

    // ── CMK resolution (shared by NotificationDevices table and SNS topic) ────
    //
    // The CVX CMK ARN arrives as a Tier A `ConfigValue` and is resolved HERE, in
    // stack scope, because `ssm.StringParameter.valueForStringParameter` calls
    // `Stack.of(scope)` and cannot run in `bin/vsa.ts`. This mirrors
    // `vsa-core-stack.ts:129`, which is the pattern every other Tier A consumer uses.
    //
    // BUG FIXED 2026-09-20 (found by the Fix Groups 10-14 security review, which
    // observed the deployed NotificationDevices table on an AWS-owned key): this
    // stack previously took `cmsKmsKeyArn?: string` and `bin/vsa.ts` passed
    // `cmsKmsKeyArn.literal`. `.literal` is populated ONLY from the `CMS_KMS_KEY_ARN`
    // env var or CDK context — never from SSM — and `scripts/with-stage-env.sh`
    // exports Tier B variables only. So on every REAL deploy the value was
    // `undefined`, the warning below fired, and both this table and the operator-alert
    // SNS topic silently fell back to no CMK. The CMK path worked only in tests and
    // hermetic synths, where the env var happens to be set.
    //
    // That is the degradation backwards: the production path got the weaker posture
    // and the test path got the stronger one, so no test could observe the gap. The
    // parameter `/cvx/{stage}/cms-kms-key-arn` existed in SSM the whole time.
    //
    // A resolved SSM token is always truthy, which is the trap documented at
    // vsa-core-stack.ts:119-129 — so the branch is on the PROP's presence, not on the
    // resolved string. Prop absent (tests that omit it) still exercises the documented
    // DEFAULT fallback; prop present resolves to a literal or a deploy-time token.
    let cmsKmsKey: kms.IKey | undefined;
    if (props.cmsKmsKeyArn) {
      const resolvedCmkArn = resolveConfigValue(this, 'cmsKmsKeyArn', props.cmsKmsKeyArn);
      cmsKmsKey = kms.Key.fromKeyArn(this, 'CvxCmk', resolvedCmkArn);
    } else {
      cdk.Annotations.of(this).addWarning(
        `No CMS KMS key ARN supplied (cmsKmsKeyArn / CMS_KMS_KEY_ARN). ` +
        `NotificationDevices will use TableEncryption.DEFAULT (AWS-owned key) ` +
        `instead of a customer-managed KMS key. ` +
        `The operator-alert SNS topic will also be created without a CMK. ` +
        `Set CMS_KMS_KEY_ARN to apply CMK encryption per spec § G3.T3 and § D6.`,
      );
    }

    // ── G3.T3b: NotificationDevices ───────────────────────────────────────
    //
    // APNs device tokens + per-category push consent.
    // PK: owner_id (S), SK: device_token (S)
    //
    // PII scope: device tokens are treated as secrets (APNs convention).
    // Encryption: CUSTOMER_MANAGED (CMK from cmsKmsKeyArn) when the prop is
    // provided; falls back to TableEncryption.DEFAULT (AWS-owned key) with a
    // synth-time warning when the ARN is absent (e.g. tests that don't pass
    // the ARN explicitly).  Only NotificationDevices gets this treatment among
    // the five AVX tables; the other four use TableEncryption.DEFAULT per the
    // comment block above — the PII-scope constraint from G3.T3 singles out
    // NotificationDevices (device tokens are secrets by APNs convention).
    // Access is additionally controlled by least-privilege IAM grants (see G3.T4).
    // No TTL — registrations are long-lived; de-registration is explicit.
    //
    // Decisions: see decisions.md § "2026-09-19 — Group 3 review W1: NotificationDevices
    // CMK encryption applied" for the rationale and mutation results.
    this.notificationDevicesTable = new dynamodb.Table(this, 'NotificationDevices', {
      tableName:    `vsa-${stage}-notification-devices`,
      partitionKey: { name: 'owner_id',     type: dynamodb.AttributeType.STRING },
      sortKey:      { name: 'device_token', type: dynamodb.AttributeType.STRING },
      billingMode:  dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption:   cmsKmsKey ? dynamodb.TableEncryption.CUSTOMER_MANAGED : dynamodb.TableEncryption.DEFAULT,
      ...(cmsKmsKey ? { encryptionKey: cmsKmsKey } : {}),
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    new cdk.CfnOutput(this, 'NotificationDevicesTableName', {
      value:      this.notificationDevicesTable.tableName,
      exportName: `vsa-${stage}-notification-devices-table-name`,
    });

    // ── G3.T3c: AvxRateLimits ─────────────────────────────────────────────
    //
    // Per-owner push-rate counters with TTL matching the counter window.
    // PK: owner_id (S), SK: window_key (S) e.g. "day:2026-09-19", "hour:2026-09-19T14"
    // TTL attribute: `ttl` — set to the end of the window so expired counters are
    // automatically reclaimed by DynamoDB.
    this.avxRateLimitsTable = new dynamodb.Table(this, 'AvxRateLimits', {
      tableName:    `vsa-${stage}-avx-rate-limits`,
      partitionKey: { name: 'owner_id',   type: dynamodb.AttributeType.STRING },
      sortKey:      { name: 'window_key', type: dynamodb.AttributeType.STRING },
      billingMode:  dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    new cdk.CfnOutput(this, 'AvxRateLimitsTableName', {
      value:      this.avxRateLimitsTable.tableName,
      exportName: `vsa-${stage}-avx-rate-limits-table-name`,
    });

    // ── F32.10: Customer-managed KMS key for both alert topics ─────────────
    //
    // Problem (D-G7-3 correction, 2026-09-24, verified live):
    //   Both alert topics previously used `cmsKmsKey`, which resolves to the
    //   AWS-managed `aws/dynamodb` key (KeyManager: AWS, policy conditioned on
    //   kms:ViaService: dynamodb.*). That key CANNOT be edited, and AWS requires
    //   a customer-managed key whose policy grants cloudwatch.amazonaws.com
    //   kms:Decrypt + kms:GenerateDataKey* before CloudWatch can publish to an
    //   encrypted topic. Every alarm action since deploy (2026-09-18 x3,
    //   2026-09-23 x2) has failed with "CloudWatch Alarms is not authorized to
    //   perform: SNS:Publish". The one confirmed subscriber has never received a
    //   notification.
    //
    // Fix: a new customer-managed key in this stack, RETAIN policy so a stack
    // update never accidentally deletes the key while topics still reference it.
    // Key alias: alias/vsa-{stage}-tier2-alerts
    // Policy grants cloudwatch.amazonaws.com Decrypt + GenerateDataKey* with
    // aws:SourceAccount condition (no wildcard principal).
    //
    // NOTE: cmsKmsKey keeps every other use (NotificationDevices table
    // encryption). Only the two SNS topics switch to this new key.
    const tier2AlertsKey = new kms.Key(this, 'Tier2AlertsKey', {
      alias:              `alias/vsa-${stage}-tier2-alerts`,
      description:        `CMK for vsa-${stage} Tier 2 alert topics (operator + escalation). ` +
                          `Grants CloudWatch Alarms sns:Publish permission via key policy.`,
      enableKeyRotation:  true,
      removalPolicy:      cdk.RemovalPolicy.RETAIN,
    });

    // Grant CloudWatch Alarms service the ability to use this key when
    // publishing to encrypted SNS topics. Both Decrypt (to verify the topic
    // is reachable) and GenerateDataKey* (to encrypt the message payload)
    // are required per AWS docs:
    //   https://repost.aws/knowledge-center/cloudwatch-configure-alarm-sns
    //   https://repost.aws/knowledge-center/cloudwatch-receive-sns-for-alarm-trigger
    // Condition: aws:SourceAccount scoped to this account so the grant is not
    // open to cross-account CloudWatch service calls.
    tier2AlertsKey.addToResourcePolicy(new iam.PolicyStatement({
      sid: 'AllowCloudWatchAlarmsToUseKey',
      effect: iam.Effect.ALLOW,
      principals: [new iam.ServicePrincipal('cloudwatch.amazonaws.com')],
      actions: [
        'kms:Decrypt',
        'kms:GenerateDataKey*',
      ],
      resources: ['*'],
      conditions: {
        StringEquals: { 'aws:SourceAccount': account },
      },
    }));

    // ── D6: Operator-alert SNS topic ───────────────────────────────────────
    //
    // Per spec § D6: `vsa-{stage}-tier2-operator-alerts`, KMS-encrypted.
    //
    // CHANGED (F32.10): switched from cmsKmsKey (AWS-managed aws/dynamodb key,
    // not editable, cannot admit CloudWatch) to tier2AlertsKey (customer-managed
    // key created above whose policy explicitly grants CloudWatch Decrypt +
    // GenerateDataKey*). This is the fix for the P1 issue
    // `issues/2026-09-24-tier2-operator-alarms-never-delivered/`.
    //
    // In test synths the KmsMasterKeyId assertion must reference tier2AlertsKey,
    // not the imported cmsKmsKey ARN.

    const operatorAlertTopic = new sns.Topic(this, 'Tier2OperatorAlertTopic', {
      topicName:  `vsa-${stage}-tier2-operator-alerts`,
      masterKey:  tier2AlertsKey,
      enforceSSL: true,
    });

    // F32.10: Allow CloudWatch Alarms to publish to the operator-alert topic.
    // enforceSSL (Deny on aws:SecureTransport=false) is applied automatically
    // by the CDK Topic construct. We add the explicit Allow for CloudWatch here.
    // Condition: aws:SourceAccount + ArnLike aws:SourceArn scoped to alarms in
    // this account/region.
    operatorAlertTopic.addToResourcePolicy(new iam.PolicyStatement({
      sid: 'AllowCloudWatchAlarmsPublish',
      effect: iam.Effect.ALLOW,
      principals: [new iam.ServicePrincipal('cloudwatch.amazonaws.com')],
      actions: ['sns:Publish'],
      resources: [operatorAlertTopic.topicArn],
      conditions: {
        StringEquals: { 'aws:SourceAccount': account },
        ArnLike:      { 'aws:SourceArn': `arn:aws:cloudwatch:${region}:${account}:alarm:*` },
      },
    }));

    // Optional email subscription — spec § D6.
    // Absence warns but does not throw (§ D6 states why this departs from the
    // fail-closed default of 2026-08-05-cvx-stage-defaults-parameterization).
    // No default, no hardcoded address — presence is purely operator-driven.
    const alarmEmail = process.env.CVX_TIER2_ALARM_EMAIL?.trim() ?? '';
    if (alarmEmail.length > 0) {
      // Email subscriptions require out-of-band confirmation; CDK cannot complete
      // the wiring unattended. The runbook carries the manual confirmation step.
      operatorAlertTopic.addSubscription(
        new snsSubscriptions.EmailSubscription(alarmEmail),
      );
    } else {
      cdk.Annotations.of(this).addWarning(
        `CVX_TIER2_ALARM_EMAIL is not set. The operator-alert SNS topic ` +
        `(vsa-${stage}-tier2-operator-alerts) has no email subscriber. ` +
        `Alarms will fire but notifications will not reach a human until ` +
        `an operator subscribes via: aws sns subscribe --protocol email ` +
        `--topic-arn <topic-arn> --notification-endpoint <email>. ` +
        `Set CVX_TIER2_ALARM_EMAIL or /cvx/${stage}/tier2-alarm-email in SSM.`,
      );
    }

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

    // ── DynamoDB: artifact table (write + cursor read) ─────────────────────
    //
    // Scoped to the specific table ARN (no wildcard).
    // The Tier 2 Lambda writes findings AND reads the cursor item (PK=CURSOR#{component}).
    //
    // BLOCKING — security review cycle 1: the role previously held PutItem only.
    // The cursor read in `selection.py` calls GetItem on `pk=CURSOR#{component}`.
    // Without GetItem the call raises AccessDeniedException; `selection.py`'s
    // fail-safe path catches it, treats the cursor as None, and restarts from the
    // pool beginning on every run. Every test passes, the agent reports success, and
    // the frozen-window defect this spec exists to remove survives in a new form.
    // GetItem on the same ARN is a functional requirement, not optional hardening.
    //
    // Reference: spec § D3, paragraph "Grant correction (2026-08-10, security review
    // cycle 1)"; tasks.md Group 4 "BLOCKING, elevated from security review cycle 1".
    lambdaRole.addToPolicy(new iam.PolicyStatement({
      sid: 'HealthFindingsTableReadWrite',
      effect: iam.Effect.ALLOW,
      actions: [
        // PutItem: write artifact findings + cursor state (unconditional via put_raw).
        // GetItem: read cursor state in selection.py — REQUIRED for the VIN window
        //          to advance. Without GetItem the cursor read raises
        //          AccessDeniedException, the fail-safe treats cursor as None, and
        //          every run silently restarts from the pool beginning (§ D3).
        'dynamodb:PutItem',
        'dynamodb:GetItem',
      ],
      resources: [this.healthFindingsTable.tableArn],
    }));

    // ── G3.T4: AVX Findings write grants (Tier 2 Lambda role) ─────────────
    //
    // The Tier 2 Lambda writes Findings via the D2 writer:
    //   PutItem  — new Findings (open-condition not met, or none found)
    //   UpdateItem — in-place re-emit (open-condition met via by_finding_key GSI)
    //   Query on by_finding_key index — the D2 open-condition lookup that precedes
    //              PutItem vs UpdateItem branching.
    //
    // Grant scope:
    //   Table ARN: specific to avx-findings (no wildcard).
    //   Index ARN: specific to by_finding_key GSI (no other index, no wildcard).
    //
    // NOT granted here (follow-on or separate role):
    //   Tier 1 AgentCore role (AmazonBedrockAgentCoreSDKRuntime-us-west-2-{stage}):
    //     needs GetItem/Query on avx-findings, avx-actions, notification-devices for
    //     the list_findings / list_actions / health_findings Tier 1 tools.
    //     Grant location: scripts/attach-agentcore-permissions.py
    //     (not agents/supervisor/scripts/ — the file lives at the repo root scripts/)
    //     Follow-on: extend scripts/attach-agentcore-permissions.py with an
    //                AvxFindingsRead + AvxActionsRead + NotificationDevicesRead
    //                statement block (see deployment/docs/avx-iam-inventory.md
    //                § "new-in-Spec-1a grants").
    //     Decisions: see decisions.md §
    //       "2026-09-19 — Tier 1 AgentCore read grants deferred to Group 4 CDK
    //        wiring + attach-agentcore-permissions.py"
    //     for the full grant specification and tracking.
    //
    //   Broker role (does not exist yet — Group 7 spec):
    //     needs DynamoDB Streams GetRecords/GetShardIterator/DescribeStream/ListStreams
    //     on avx-findings stream, PutItem/UpdateItem on avx-actions (for dismissal),
    //     and sns:Publish on the APNs platform-application ARNs.
    //     Grant location: Group 7 (VsaAvxBrokerStack or VsaTier2Stack amendment).
    //
    // Mutation note (G3.T4 mutation test):
    //   test file infrastructure/test/vsa-tier2-stack-avx.test.ts verifies that
    //   this policy block contains neither `Resource: "*"` nor `dynamodb:*`.
    //   Mutation: temporarily add `resources: ['*']` → test MUST fail.
    //   Mutation: temporarily use `dynamodb:*` → test MUST fail.
    //   Both mutations were exercised and caught; see decisions.md § "G3.T4 mutation".
    lambdaRole.addToPolicy(new iam.PolicyStatement({
      sid: 'AvxFindingsTableWrite',
      effect: iam.Effect.ALLOW,
      actions: [
        'dynamodb:PutItem',
        'dynamodb:UpdateItem',
      ],
      // Narrow: specific table ARN, no wildcard.
      resources: [this.avxFindingsTable.tableArn],
    }));

    lambdaRole.addToPolicy(new iam.PolicyStatement({
      sid: 'AvxFindingsKeyIndexQuery',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:Query'],
      // Narrow: specific GSI ARN on the specific table.
      // Required for the D2 writer's open-condition lookup:
      //   writer queries by_finding_key to decide UpdateItem vs PutItem.
      resources: [`${this.avxFindingsTable.tableArn}/index/by_finding_key`],
    }));

    // ── F11.2: CMS vehicle registry — VIN → owner-of-record resolution ───────
    //
    // TWO statements, because the registry's key schema requires two calls.
    // Verified against cms-staging-storage-vehicles 2026-09-23:
    //   - base table key schema is `vehicleId` (HASH) ONLY — a VIN is not a key,
    //     so a VIN cannot be GetItem-ed;
    //   - the `vin-index` GSI is keyed on `vin` but projects KEYS_ONLY, so it
    //     returns vin + vehicleId and CANNOT return `sold_to`.
    // So: Query the index for the key, then GetItem the table for the attribute.
    //
    // This is a deliberate, recorded deviation from F11.2 Accept 5, which
    // specifies "dynamodb:GetItem only ... No dynamodb:Query". That grant cannot
    // satisfy the lookup it is meant to authorize. See decisions.md
    // § "2026-09-23 — Fix Group 15 build: four spec defects".
    //
    // The action set is a TIGHT superset of the call sites in
    // agents/tier2/owner_resolver.py: Query on the index ARN only, GetItem on the
    // table ARN only. No Scan (the resolver has no Scan fallback, by design — the
    // index answer is authoritative and a fallback would widen this grant).
    //
    // Mutation notes (F11.2, M-FG11-6 / M-FG11-7), all exercised and reverted:
    //   Mutation: resources -> ['*'] on either statement
    //             → 'no DynamoDB statement uses Resource wildcard' MUST fail.
    //   Mutation: actions -> ['dynamodb:*']
    //             → 'no DynamoDB statement uses dynamodb:* catch-all' MUST fail.
    //   Mutation: add 'dynamodb:Scan' to either statement
    //             → the F11.2 action-set test MUST fail (no Scan call site).
    //   Mutation: point the Query at the table ARN instead of the index ARN
    //             → the F11.2 index-scoping test MUST fail.
    lambdaRole.addToPolicy(new iam.PolicyStatement({
      sid: 'CmsVehiclesVinIndexQuery',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:Query'],
      // Narrow: the vin-index GSI ARN only — NOT the table.  A Query grant on
      // the table would also authorize querying every other index on it.
      resources: [`${cmsVehiclesTableArn}/index/vin-index`],
    }));

    lambdaRole.addToPolicy(new iam.PolicyStatement({
      sid: 'CmsVehiclesOwnerGetItem',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:GetItem'],
      // Narrow: the table ARN only.  The resolver's GetItem uses a
      // ProjectionExpression limited to `sold_to`; IAM cannot constrain
      // attribute projection, so the narrowing that matters here is the
      // single action and the single resource.
      resources: [cmsVehiclesTableArn],
    }));

    // ── diagnostics component: CMS DTC history (spec 2026-09-25-avx-own-vehicle-findings T4.4)
    //
    // agents/health/cms_dtc_source.py Scans the table to find which vehicles have
    // DTC rows, then RealCmsClient.get_active_dtcs Queries each vehicle's partition
    // (base table, KeyConditionExpression vehicleId = :v).  Read-only, this one
    // table, no index: neither call uses a GSI.
    const cmsDtcHistoryTableArn =
      `arn:aws:dynamodb:${cmsRegion}:${cmsAccount}:table/cms-${cmsStage}-storage-dtc-history`;
    lambdaRole.addToPolicy(new iam.PolicyStatement({
      sid: 'CmsDtcHistoryRead',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:Scan', 'dynamodb:Query'],
      resources: [cmsDtcHistoryTableArn],
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
        // brakes query's model_year join (T4.3 W6)
        `${adpLakeBucketArn}/curated/vehicle_identity/*`,
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
            'curated/vehicle_identity',
            'curated/vehicle_identity/*',
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
        // vehicle_identity: the brakes query joins it for model_year (spec
        // 2026-09-25-avx-own-vehicle-findings T4.3, review Cycle 1 W6).
        `arn:aws:glue:${adpRegion}:${adpAccount}:database/adp_${adpStage}_vehicle_identity`,
        `arn:aws:glue:${adpRegion}:${adpAccount}:table/adp_${adpStage}_vehicle_identity/*`,
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

    // ── CloudWatch: PutMetricData scoped to CVX/Tier2 namespace ───────────
    //
    // Spec § D5: the observability module emits VinsSelected, ArtifactsWritten,
    // and DuplicatesSkipped per run into namespace CVX/Tier2.
    //
    // PutMetricData does not support resource-level permissions; Resource: "*"
    // is required. The cloudwatch:namespace condition key is the only available
    // scoping mechanism — it restricts this grant to CVX/Tier2 and prevents the
    // Lambda from emitting metrics into any other namespace.
    //
    // Verified in docs/tech.md § "CloudWatch PutMetricData — IAM action and
    // namespace condition key": the condition key is `cloudwatch:namespace`.
    lambdaRole.addToPolicy(new iam.PolicyStatement({
      sid: 'CloudWatchPutMetricData',
      effect: iam.Effect.ALLOW,
      actions: ['cloudwatch:PutMetricData'],
      // PutMetricData has no resource-level permission — Resource: "*" is required.
      // The cloudwatch:namespace condition key provides the effective scope.
      resources: ['*'],
      conditions: {
        StringEquals: { 'cloudwatch:namespace': 'CVX/Tier2' },
      },
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
          // ── Deterministic bundling block (Group 2 fix — root cause of tokens_used=0) ──
          //
          // Without this block, Code.fromAsset copies source files as-is and runs NO
          // pip install.  strands-agents is absent from the Lambda ZIP, make_strands_agent_fn
          // hits ImportError, and the deterministic fallback runs — producing tokens_used=0.
          //
          // Design decision — scoped requirements file:
          //   requirements-tier2-lambda.txt (repo root) carries only the three packages
          //   the Tier 2 Lambda actually needs: strands-agents==1.39.0, boto3==1.43.6,
          //   pydantic==2.13.4.  The repo-root requirements.txt serves the AgentCore
          //   supervisor and includes openapi-spec-validator, websockets, redis,
          //   bedrock-agentcore, aws_sdk_bedrock_runtime, and others the Lambda never
          //   imports.  Installing the full set adds dead weight and risks pulling in
          //   .so files incompatible with the manylinux Lambda execution environment.
          //   Bundle size, measured on this build 2026-09-18 (Group 2):
          //   101 MB unzipped / 39 MB zip, against the Accept gate of <200 MB and
          //   the Lambda unzipped ceiling of 250 MB.  Group 1's earlier figure of
          //   87.2 MB / 34.5 MB was measured against the same three packages at
          //   different transitive-dependency versions; both pass, and the 101 MB
          //   number is the one this bundling block actually produces.
          //   <!-- verify: du -sh infrastructure/cdk.out/asset.*/ (the one containing strands/) -->
          //
          // Known, deliberately not fixed here: the asset contains top-level
          // __pycache__ directories, because pip compiles on install and the
          // `exclude` list is applied to the copy-in, not to bundling output.
          // .pyc files are not byte-stable, so the artifact is deterministic in
          // its dependency set but not byte-identical across builds.  Closing
          // that needs `--no-compile` (which shifts compile cost to cold start)
          // — a tradeoff for review, not a silent change.
          //
          // Image pin:
          //   public.ecr.aws/sam/build-python3.12:1.165.0
          //   SAM CLI version tag; verified 2026-09-18 that tag 1.165.0 and the locally-
          //   cached :latest resolve to the same digest:
          //     sha256:eb0e1def1f98e4f1f556bc28ca5d2f1904583c3e0eca2b7d0b63aa2d8f6086f9
          //   To update: choose a new SAM CLI release tag from public.ecr.aws/sam/,
          //   verify its digest, and update here.  Never use :latest.
          //
          // Platform:
          //   linux/x86_64 (manylinux2014_x86_64) — matches Runtime.PYTHON_3_12 which
          //   deploys to the x86_64 Lambda execution environment.  If the stack ever
          //   moves to Runtime.PYTHON_3_12 on arm64, change to linux/arm64 and use
          //   manylinux2014_aarch64 wheels.
          bundling: {
            image: cdk.DockerImage.fromRegistry(
              // Semver-tagged SAM build image for Python 3.12 (x86_64).
              // Tag 1.165.0 verified 2026-09-18;
              // digest: sha256:eb0e1def1f98e4f1f556bc28ca5d2f1904583c3e0eca2b7d0b63aa2d8f6086f9
              'public.ecr.aws/sam/build-python3.12:1.165.0',
            ),
            platform: 'linux/x86_64',
            command: [
              'bash', '-c',
              [
                // Step 1: install dependencies into /asset-output so they are at
                // the root of the Lambda ZIP alongside the source.
                'pip install',
                '  -r /asset-input/requirements-tier2-lambda.txt',
                '  -t /asset-output/',
                '  --platform manylinux2014_x86_64',
                '  --python-version 3.12',
                '  --implementation cp',
                '  --only-binary=:all:',
                '  --no-cache-dir',
                '  --quiet',
                // Step 2: copy source into /asset-output.  Only agents/ and the
                // Tier 2-relevant top-level directories are copied; infrastructure/,
                // .venv/, node_modules/, evals/, corpora/, and docs/ are omitted to
                // keep the ZIP tight.
                '&& cp -r /asset-input/agents /asset-output/',
                '&& cp -r /asset-input/lambdas /asset-output/ 2>/dev/null || true',
                '&& cp -r /asset-input/scripts /asset-output/ 2>/dev/null || true',
              ].join(' \\\n  '),
            ],
          },
          // The exclude list guards the *host-side* asset hash computation (what CDK
          // hashes to detect changes) and the local copy before bundling runs.
          // With a bundling block the final ZIP content is controlled by the bundling
          // command above, not the exclude list.  These entries keep the invalidation
          // set tight so unrelated changes (docs edits, .venv churn) don't trigger
          // unnecessary Lambda redeployments.
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
        // | AVX_FINDINGS_TABLE        | vsa-{stage}-avx-findings                                 |
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
        // F11.2: CMS vehicle registry table, fully resolved by the stack.
        // Consumed by agents/tier2/owner_resolver.py for VIN -> owner-of-record
        // resolution, so the runner derives no name of its own.  This is the same
        // contract variable lambdas/shared/cms_client_real.py reads, so there is
        // one table-name contract across CVX rather than two.
        // IAM: CmsVehiclesVinIndexQuery (Query on vin-index) +
        //      CmsVehiclesOwnerGetItem (GetItem on the table) below.
        CMS_VEHICLES_TABLE:       `cms-${cmsStage}-storage-vehicles`,
        // T4.4 (spec 2026-09-25-avx-own-vehicle-findings): the diagnostics component's
        // DTC table, read by agents/health/cms_dtc_source.py and, through it, by
        // lambdas/shared/cms_client_real.py (same variable name). IAM: CmsDtcHistoryRead.
        CMS_DTC_HISTORY_TABLE:    `cms-${cmsStage}-storage-dtc-history`,
        // T16.3: AVX findings table name — consumed by the D2 writer in runner.py.
        // The runner uses this value as the DynamoDB table name for PutItem/UpdateItem
        // (new findings) and Query on by_finding_key (idempotency check).
        // The IAM grants for these calls are already on lambdaRole (AvxFindingsTableWrite
        // + AvxFindingsKeyIndexQuery statements, added in Group 3 T4).
        AVX_FINDINGS_TABLE:       `vsa-${stage}-avx-findings`,
        DYNAMODB_REGION:          region,
        ADP_REGION:               adpRegion,
        ATHENA_WORKGROUP:         `cvx-${adpStage}-analytics`,
        // Fully resolved — account id and region are substituted at synth time.
        // The runner reads this value directly; it never reconstructs it from parts.
        ATHENA_RESULTS_BUCKET:    `cvx-${adpStage}-athena-results-${account}-${adpRegion}`,
        TIER2_MAX_TOKENS:         '50000',
        TIER2_MAX_WALL_SECONDS:   '600',
        // Per-component VIN caps (spec § D4, amended 2026-08-10).
        // Rows-per-VIN varies 224× across products; a single global cap sized
        // for the worst component (tires at 156) would limit brakes to 156/night
        // when brakes can safely process 2,000 within the same row ceiling.
        // Binding constraints per Group 1 measurements (2026-08-10):
        //   tires  → 156  rows-bound: 156 × 240 = 37,440 rows  (25.1% headroom under 50k)
        //   battery→ 375  rows-bound: 375 × 100 = 37,500 rows  (25.0% headroom under 50k)
        //   brakes → 2000 write-path-bound: ~2,140 rows; bounded by sequential
        //                  conditional PutItem + agent_fn calls within 300s target
        TIER2_MAX_VINS_TIRES:     '156',
        TIER2_MAX_VINS_BATTERY:   '375',
        TIER2_MAX_VINS_BRAKES:    '2000',
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

    // ── diagnostics: every 5 minutes (spec 2026-09-25-avx-own-vehicle-findings T4.4, D4a)
    //
    // Active CMS DTCs → Findings (runner.run_diagnostics). Kept out of
    // componentSchedules on purpose: the per-component NoVinsSelected and
    // ArtifactsStale alarms below assume a daily ADP batch, and a fleet with no
    // active DTCs is a healthy state here, not an alarm. Failures still reach the
    // operator topic: run_diagnostics raises after any failed write, which is a
    // Lambda error on Tier2RunFailureAlarm.
    //
    // No retries: the next run is five minutes away and re-does the same work.
    new scheduler.Schedule(this, 'Tier2DiagnosticsSchedule', {
      scheduleName: `vsa-${stage}-tier2-diagnostics-${region}`,
      schedule: scheduler.ScheduleExpression.rate(cdk.Duration.minutes(5)),
      target: new schedulerTargets.LambdaInvoke(healthAgentFn, {
        role: schedulerRole,
        retryAttempts: 0,
        maxEventAge: cdk.Duration.minutes(5),
        input: scheduler.ScheduleTargetInput.fromObject({ stage, component: 'diagnostics' }),
      }),
      description: `Tier 2 health agent — diagnostics (active CMS DTCs) every 5 minutes — VSA ${stage}`,
      enabled: true,
    });

    // ── CloudWatch alarms ─────────────────────────────────────────────────
    //
    // Four alarms per spec § D5 + D6:
    //   1. Run failure — fires when the Lambda errors.
    //   2. Run duration — fires when duration exceeds threshold (leading indicator).
    //   3. No-vins-selected — fires when VinsSelected == 0 over 1 day (BREACHING).
    //   4. Artifacts-stale — fires when ArtifactsWritten == 0 for 3 consecutive days (BREACHING).
    //
    // All four have AlarmActions pointing at the operator alert topic (§ D6).
    // Alarms 1 and 3 (run-failure, no-vins) also have OKActions so recovery is
    // visible — a resolved-without-anyone-knowing incident is its own failure mode
    // for an unattended agent.
    const topicAction = new cloudwatchActions.SnsAction(operatorAlertTopic);

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
    // AlarmActions: notify on failure; OKActions: notify on recovery (spec § D6).
    runFailureAlarm.addAlarmAction(topicAction);
    runFailureAlarm.addOkAction(topicAction);

    // Duration alarm — fires if a run exceeds 10 minutes (600s).
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
    runDurationAlarm.addAlarmAction(topicAction);
    // No OKAction on duration: recovering from a long run is expected behaviour,
    // not a state transition the operator needs to be paged about.

    // ── D5: Per-component alarms — distinguishing "nothing to do" from "doing nothing" ──
    //
    // IMPORTANT: emit_metrics publishes dimensions {stage, component} (two dimensions).
    // An alarm with dimensionsMap: { stage } alone would query a DIFFERENT time series —
    // CloudWatch treats dimension sets as part of a metric's identity — and would find no data.
    // With treatMissingData: BREACHING, a {stage}-only alarm would fire immediately and
    // permanently on deploy, making it noise rather than signal (spec § D5, review cycle 4
    // Critical 1). Each alarm therefore specifies dimensionsMap: { stage, component } to
    // match exactly what observability.py emits.
    //
    // There are THREE no-vins alarms and THREE artifacts-stale alarms, one per component.
    // Rejected alternatives (spec § D5):
    //   - Aggregate {stage}-only datum: a second dimensioning scheme to maintain in sync.
    //   - Metric Math SEARCH expression: harder to assert in a synth guard.
    // Per-component alarms also read better operationally ("brakes selected nothing" vs "something selected nothing").
    const alarmComponents: string[] = ['battery', 'tires', 'brakes'];

    // Three no-vins alarms (one per component).
    // VinsSelected == 0 is a hard defect: selection returned nothing, which after
    // the wrap logic in D3 should be impossible while the pool is non-empty.
    // treatMissingData: BREACHING so the alarm fires even when the Lambda never ran —
    // that is the exact failure mode the empty AlarmActions hid (five days of
    // zero-artifact runs looked healthy because no data meant NOT_BREACHING).
    // OKAction: recovery is visible (spec § D6 — no-vins and run-failure both get OKActions).
    const noVinsAlarms: cloudwatch.Alarm[] = [];
    for (const component of alarmComponents) {
      const alarm = new cloudwatch.Alarm(this, `Tier2NoVinsSelectedAlarm${component.charAt(0).toUpperCase() + component.slice(1)}`, {
        alarmName:   `vsa-${stage}-tier2-no-vins-selected-${component}-${region}`,
        alarmDescription:
          `Tier 2 health agent (${component}): VinsSelected sum == 0 over 1 day. ` +
          'Selection returned no VINs after the pool-wrap retry — ' +
          'either the pool is genuinely empty or selection.py is broken. ' +
          'Check CloudWatch Logs and the CURSOR# item in the health-findings table.',
        metric: new cloudwatch.Metric({
          namespace:  'CVX/Tier2',
          metricName: 'VinsSelected',
          // Dimensions must match what observability.py emits: {stage, component}.
          // A {stage}-only dimensionsMap would watch a different (empty) time series.
          dimensionsMap: { stage, component },
          period:    cdk.Duration.days(1),
          statistic: 'Sum',
        }),
        threshold:          1,
        evaluationPeriods:  1,
        comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
        // BREACHING: a Lambda that never ran emits no data points at all.
        // NOT_BREACHING would silently hide a dead Lambda. Spec § D5.
        treatMissingData:   cloudwatch.TreatMissingData.BREACHING,
      });
      alarm.addAlarmAction(topicAction);
      alarm.addOkAction(topicAction);
      noVinsAlarms.push(alarm);
    }

    // Three artifacts-stale alarms (one per component).
    // ArtifactsWritten == 0 for 3 consecutive days.
    // The three-day window is deliberate: once the cursor completes a full pool pass,
    // revisiting unchanged vehicles legitimately produces zero new artifacts. A
    // twitchy alarm would fire on every wrap night. Three days of sustained silence
    // means the data has genuinely stopped changing OR the pipeline is silently broken —
    // both are conditions an operator should see. Spec § D5.
    const artifactsStaleAlarms: cloudwatch.Alarm[] = [];
    for (const component of alarmComponents) {
      const alarm = new cloudwatch.Alarm(this, `Tier2ArtifactsStaleAlarm${component.charAt(0).toUpperCase() + component.slice(1)}`, {
        alarmName:   `vsa-${stage}-tier2-artifacts-stale-${component}-${region}`,
        alarmDescription:
          `Tier 2 health agent (${component}): ArtifactsWritten sum == 0 for 3 consecutive days. ` +
          'Either the ADP data has genuinely stopped changing (all findings are duplicate) ' +
          'or the write path is silently broken. Check DynamoDB item counts and CloudWatch Logs.',
        metric: new cloudwatch.Metric({
          namespace:  'CVX/Tier2',
          metricName: 'ArtifactsWritten',
          // Dimensions must match what observability.py emits: {stage, component}.
          dimensionsMap: { stage, component },
          period:    cdk.Duration.days(1),
          statistic: 'Sum',
        }),
        threshold:          1,
        evaluationPeriods:  3,
        comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
        // BREACHING: same reasoning as no-vins. A dead Lambda produces no data points;
        // NOT_BREACHING would silently hide it for three days. Spec § D5.
        treatMissingData:   cloudwatch.TreatMissingData.BREACHING,
      });
      alarm.addAlarmAction(topicAction);
      // No OKAction on artifacts-stale: recovery from stale artifacts does not need a
      // separate notification once the no-vins alarm (with OKAction) has cleared.
      artifactsStaleAlarms.push(alarm);
    }

    // ── ImportError alarm (strands unavailable in the Lambda bundle) ─────────
    //
    // After the bundling fix that landed in commit 708eca4, strands-agents is
    // pip-installed into the Lambda ZIP and ImportError should NEVER fire in
    // production.  If it does, make_strands_agent_fn's fallback silently
    // degrades to run_health_agent (tokens_used stays 0) with no visible signal.
    //
    // This WARNING-level alarm makes that degradation visible.
    //
    // Pattern matched: the log line emitted by make_strands_agent_fn's
    // except ImportError block in agents/health/agent.py:
    //   "strands not available — falling back to deterministic agent"
    //
    // CloudWatch Logs metric filter:
    //   Log group:   /aws/lambda/vsa-{stage}-tier2-health-agent-{region}
    //   Filter:      strands not available
    //   Metric NS:   CVX/Tier2
    //   Metric name: StrandsImportError-{stage}   (stage is in the NAME, see below)
    //   Value:       1
    //
    // NO dimensions, and NO defaultValue. Both were learned from failed deploys
    // (2026-09-18, see issues/2026-09-18-metricfilter-dimensions-defaultvalue-conflict/).
    // Two DIFFERENT service-side rejections, in sequence, each arriving after a
    // clean `cdk synth` AND a clean `npx tsc --noEmit`:
    //   1. dimensions + defaultValue together ->
    //      "Invalid metric transformation: dimensions and default value are
    //       mutually exclusive properties"
    //   2. dimensions with a LITERAL filter pattern ->
    //      "The specified filter pattern does not support dimensions"
    // Metric-filter dimensions must be sourced from fields the pattern extracts;
    // a literal text match extracts none, so dimensions are unavailable here at
    // ANY value, including a constant like the stage string. The AWS docs for
    // AWS::Logs::MetricFilter MetricTransformation list both properties as
    // independently optional and document neither constraint.
    //
    // Stage separation therefore lives in the METRIC NAME. It cannot simply be
    // dropped: staging and prod share one account, so two stacks emitting an
    // undimensioned `StrandsImportError` would write to the SAME time series and
    // the alarm could not tell which stage had degraded. The namespace stays
    // `CVX/Tier2` because the Tier 2 role's cloudwatch:PutMetricData grant is
    // scoped to that namespace by condition.
    //
    // The alarm below MUST query this metric with NO dimensionsMap. An alarm
    // whose dimensions do not match its metric watches an empty time series and
    // never fires, silently. That exact defect is already on record against this
    // stack's other alarms (the {stage, component} note further up), and it would
    // be worse here, because catching a silent degradation is this alarm's only
    // job.
    //
    // Alarm threshold: ≥1 occurrence in any 5-minute window.
    // treatMissingData: NOT_BREACHING — absence of this log line means strands
    // loaded correctly, which is the normal case, and is also what makes
    // defaultValue unnecessary rather than merely unavailable.
    // Severity: WARNING (not critical).  The agent degrades gracefully
    // (tokens_used=0); the alarm exists to catch a mis-bundled deploy
    // before it silently runs the whole fleet on the deterministic path.
    const strandsImportErrorMetricName = `StrandsImportError-${stage}`;

    const strandsImportErrorFilter = new logs.MetricFilter(
      this,
      'StrandsImportErrorMetricFilter',
      {
        logGroup,
        filterPattern: logs.FilterPattern.literal('"strands not available"'),
        metricNamespace: 'CVX/Tier2',
        metricName:      strandsImportErrorMetricName,
        metricValue:     '1',
      },
    );

    const strandsImportErrorAlarm = new cloudwatch.Alarm(
      this,
      'Tier2StrandsImportErrorAlarm',
      {
        alarmName:   `vsa-${stage}-tier2-strands-import-error-${region}`,
        alarmDescription:
          'Tier 2 health agent: strands.Agent ImportError — make_strands_agent_fn ' +
          'fell back to the deterministic run_health_agent path. ' +
          'After commit 708eca4, strands-agents must be present in the Lambda bundle. ' +
          'Re-deploy vsa-{stage}-tier2 to restore the LLM path (tokens_used will be 0 until then).',
        metric: new cloudwatch.Metric({
          namespace:     'CVX/Tier2',
          metricName:    strandsImportErrorMetricName,
          // NO dimensionsMap — deliberately, and it must stay that way.
          // The metric filter above cannot carry dimensions (literal filter
          // patterns do not support them), so this metric has none. Adding a
          // dimensionsMap here would point the alarm at a time series that does
          // not exist, and with treatMissingData: NOT_BREACHING it would sit
          // permanently OK while reporting nothing. Stage is encoded in
          // metricName instead, which is why the shared constant is used on both
          // sides rather than the string being written out twice.
          period:        cdk.Duration.minutes(5),
          statistic:     'Sum',
        }),
        threshold:          1,
        evaluationPeriods:  1,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        // NOT_BREACHING: absence of the log line is the expected (healthy) state.
        treatMissingData:   cloudwatch.TreatMissingData.NOT_BREACHING,
      },
    );
    // AlarmActions only — no OKAction.  Recovery (ImportError stops appearing)
    // is implicitly visible via absence; an OKAction page on every successful
    // deploy would be noise.
    strandsImportErrorAlarm.addAlarmAction(topicAction);

    // Ensure the metric filter is created before the alarm that reads it.
    strandsImportErrorAlarm.node.addDependency(strandsImportErrorFilter);

    // ── AVX DMS event-consumer Lambda (Spec 2 T5.4 + T5.5) ────────────────
    //
    // Subscribes to `dms.ro.status_changed` on the DMS event bus
    // (`dms-{stage}-events`, same account in staging) and projects RO state
    // transitions onto the AVX Action row via direct primary-key UpdateItem
    // with ConditionExpression-based idempotency.
    //
    // See:
    //   - Handler: lambdas/api-avx-dms-event-consumer/handler.py
    //   - Design:  ~/guidance-for-dealer-management-system-on-aws/.kiro/specs/
    //              2026-09-18-dms-avx-agentic-surface/decisions.md
    //              § "2026-09-21 — T5.4 + T5.5 design"
    //   - Drafted IAM inventory (superseded here by narrower shape — no
    //     GetItem needed, ConditionExpression on UpdateItem enforces presence):
    //     deployment/docs/avx-iam-inventory.md § "Diff 2"

    // ── T5.5: dmsAccountId context key + same-account annotation ─────────
    //
    // Replicates the precedent in
    // ~/guidance-for-dealer-management-system-on-aws/deployment/stacks/dms_integration_stack.py:611-627:
    // an account filter that defaults to the deploying account (same-account
    // v1) with an Annotations.of.addInfo documenting that a same-account
    // forger of `dms.service-lane` events is NOT excluded by that filter —
    // the filter becomes load-bearing only on a cross-account split.
    const dmsAccountId: string =
      (this.node.tryGetContext('dmsAccountId') as string | undefined) ||
      process.env.DMS_ACCOUNT_ID ||
      account;

    if (dmsAccountId === account) {
      cdk.Annotations.of(this).addWarning(
        'AvxRoStatusChangedRule: dmsAccountId defaulted to this stack ' +
        'account (same-account v1 topology). The account filter therefore ' +
        'does not prevent a same-account forger of dms.service-lane events. ' +
        'Override via `--context dmsAccountId=<id>` once DMS moves to a ' +
        'separate account. See spec decisions.md § "Deviation from task ' +
        'text: same-account, cross-stack — not cross-account". ' +
        'Upgraded from addInfo to addWarning per security-review Cycle 1 S1 ' +
        '(2026-09-21) for CDK-nag visibility.',
      );
    }

    // ── T5.4: Lambda execution role + narrow IAM ─────────────────────────
    //
    // Name check: `vsa-staging-avx-dms-event-consumer-us-west-2` = 47 chars ≤ 64.
    // Longest region (ap-southeast-7) = 50 chars — still under the limit.
    const avxDmsEventConsumerRole = new iam.Role(this, 'AvxDmsEventConsumerRole', {
      roleName: `vsa-${stage}-avx-dms-event-consumer-${region}`,
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description:
        `AVX DMS event consumer - projects RO status transitions to ` +
        `avx-actions (${stage})`,
    });

    avxDmsEventConsumerRole.addToPolicy(new iam.PolicyStatement({
      sid: 'CloudWatchLogsWrite',
      effect: iam.Effect.ALLOW,
      actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
      resources: [
        `arn:aws:logs:${region}:${account}:log-group:` +
          `/aws/lambda/vsa-${stage}-avx-dms-event-consumer-${region}`,
        `arn:aws:logs:${region}:${account}:log-group:` +
          `/aws/lambda/vsa-${stage}-avx-dms-event-consumer-${region}:*`,
      ],
    }));

    // ── T5.4 MUTATION TARGET M2: IAM Resource scope ──────────────────────
    //
    // `UpdateItem` on the AVX actions table ARN only. No `GetItem`
    // (ConditionExpression on UpdateItem does the presence check
    // atomically). No `/index/*`. No wildcards. Widening Resource to `'*'`
    // is the mutation the CDK snapshot test guards against.
    avxDmsEventConsumerRole.addToPolicy(new iam.PolicyStatement({
      sid: 'AvxActionsProjection',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:UpdateItem'],
      resources: [this.avxActionsTable.tableArn],
    }));

    // Log group — RETAIN, matches portfolio convention.
    const avxDmsEventConsumerLogGroup = new logs.LogGroup(this, 'AvxDmsEventConsumerLogs', {
      logGroupName: `/aws/lambda/vsa-${stage}-avx-dms-event-consumer-${region}`,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // Lambda function.
    //
    // Asset is pre-assembled by scripts/build-avx-lambda.sh — this Lambda
    // does not use `_lib/avx_scope.py`, so the bundle call passes use_lib=0.
    const avxDmsEventConsumerFn = new lambda.Function(this, 'AvxDmsEventConsumer', {
      functionName: `vsa-${stage}-avx-dms-event-consumer-${region}`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      role: avxDmsEventConsumerRole,
      timeout: cdk.Duration.seconds(10),
      memorySize: 256,
      logGroup: avxDmsEventConsumerLogGroup,
      code: lambda.Code.fromAsset(
        path.join(__dirname, '..', 'build', 'api-avx-dms-event-consumer'),
      ),
      environment: {
        AVX_ACTIONS_TABLE: this.avxActionsTable.tableName,
      },
      description:
        `AVX DMS event consumer - projects RO status transitions to AVX ` +
        `Actions (${stage}, T5.4)`,
    });

    // ── T5.5 MUTATION TARGET M3: EventBridge rule event-pattern scope ───
    //
    // Rule on the DMS bus, imported by ARN (same-account works out of the
    // box). Pattern's source and detail-type MUST match DMS's publisher
    // constants at
    //   ~/guidance-for-dealer-management-system-on-aws/source/_lib/events.py:38,41
    // A snapshot test in
    //   infrastructure/test/vsa-tier2-stack-avx-dms-event-consumer.test.ts
    // pins these values so drift is visible in TS tests before it's visible
    // in production. Widening `source` to `['*']` is the mutation the
    // snapshot test guards against.
    const dmsEventsBus = events.EventBus.fromEventBusArn(
      this,
      'DmsEventsBus',
      `arn:aws:events:${region}:${dmsAccountId}:event-bus/dms-${stage}-events`,
    );

    const avxRoStatusChangedRule = new events.Rule(this, 'AvxRoStatusChangedRule', {
      ruleName: `vsa-${stage}-avx-ro-status-changed`,
      eventBus: dmsEventsBus,
      description:
        'Route DMS repair-order status transitions to the AVX DMS event ' +
        'consumer (Spec 2 T5.5)',
      eventPattern: {
        source:     ['dms.service-lane'],       // SOURCE_SERVICE_LANE
        detailType: ['dms.ro.status_changed'],  // DETAIL_TYPE_RO_STATUS_CHANGED
        account:    [dmsAccountId],
      },
    });

    avxRoStatusChangedRule.addTarget(
      new eventsTargets.LambdaFunction(avxDmsEventConsumerFn, {
        // EventBridge's default at-least-once retry. Idempotency in the
        // handler makes duplicate delivery safe (ConditionExpression no-ops
        // a redundant projection).
        retryAttempts: 2,
      }),
    );

    // ── Outputs ───────────────────────────────────────────────────────────

    // ── Group 7: AVX Broker Lambda ────────────────────────────────────────
    //
    // Subscribed to the `vsa-{stage}-avx-findings` DynamoDB stream (NEW_IMAGE).
    // Applies rate-limit, cardinality-cap, per-category consent, and feedback
    // demotion gates; creates in-app Action cards and publishes APNs pushes.
    //
    // Policy inputs read from SSM at cold start — never hardcoded:
    //   /vsa/{stage}/avx/broker/apns-severity-threshold
    //   /vsa/{stage}/avx/broker/rate-limit
    //   /vsa/{stage}/avx/broker/cardinality-cap
    //
    // Escalation SNS topic: vsa-{stage}-avx-action-terminal-failure-alerts
    //   Producers: CloudWatch alarms only (per-target executor terminal
    //   failures + the broker DLQ; F32.8). The broker does not publish to it.
    //   Subscriber endpoint: $CVX_AVX_BROKER_ESCALATION_ENDPOINT, which
    //   scripts/with-stage-env.sh resolves from /cvx/{stage}/avx-broker-escalation-endpoint.
    //   Absent endpoint → synth ERROR (no hardcoded address per public-mirror rule)
    //
    // Name check: "vsa-staging-avx-broker-ap-southeast-7" = 39 chars ≤ 64 ✓
    // IAM role name: "vsa-staging-avx-broker-role-ap-southeast-7" = 43 chars ≤ 64 ✓

    // ── Escalation SNS topic ───────────────────────────────────────────────
    //
    // Named exactly as the executor/dms/escalation SSM param seeds it:
    //   `vsa-{stage}-avx-action-terminal-failure-alerts`
    // Its producers are the CloudWatch alarms at the end of this section
    // (F32.8): escalation.py's per-target AVXActionTerminalFailure metric and
    // the broker DLQ. The absence of a subscriber FAILS synth (addError) — a
    // topic with zero subscriptions is the VFO defect this task names verbatim.
    //
    // CHANGED (F32.10): switched from snsKmsKey (cmsKmsKey = AWS-managed key,
    // incompatible with CloudWatch Alarms) to tier2AlertsKey, which admits
    // CloudWatch via its resource policy. Same rationale as operatorAlertTopic.
    const escalationTopic = new sns.Topic(this, 'AvxActionTerminalFailureAlertsTopic', {
      topicName:  `vsa-${stage}-avx-action-terminal-failure-alerts`,
      masterKey:  tier2AlertsKey,
      enforceSSL: true,
    });

    // F32.10: Allow CloudWatch Alarms to publish to the escalation topic.
    escalationTopic.addToResourcePolicy(new iam.PolicyStatement({
      sid: 'AllowCloudWatchAlarmsPublish',
      effect: iam.Effect.ALLOW,
      principals: [new iam.ServicePrincipal('cloudwatch.amazonaws.com')],
      actions: ['sns:Publish'],
      resources: [escalationTopic.topicArn],
      conditions: {
        StringEquals: { 'aws:SourceAccount': account },
        ArnLike:      { 'aws:SourceArn': `arn:aws:cloudwatch:${region}:${account}:alarm:*` },
      },
    }));

    // Subscriber endpoint from SSM at seed time; absent = FAIL synth, not warn.
    // F32.9: The endpoint parameter path is /cvx/{stage}/avx-broker-escalation-endpoint
    // (under /cvx/ alongside the other Tier B parameters, resolved by with-stage-env.sh).
    // It must NOT be hardcoded here: *@amazon.com is a forbidden pattern in the
    // public-mirror publish tree and infrastructure/ cannot be publish-excluded.
    const escalationEndpoint = process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT?.trim() ?? '';
    if (escalationEndpoint.length > 0) {
      escalationTopic.addSubscription(
        new snsSubscriptions.EmailSubscription(escalationEndpoint),
      );
    } else {
      cdk.Annotations.of(this).addError(
        `CVX_AVX_BROKER_ESCALATION_ENDPOINT is not set. The AVX broker escalation ` +
        `topic (vsa-${stage}-avx-action-terminal-failure-alerts) would have zero ` +
        `subscribers — this is the VFO defect the task names verbatim. ` +
        `Either export CVX_AVX_BROKER_ESCALATION_ENDPOINT before synth, or seed ` +
        `the SSM parameter /cvx/${stage}/avx-broker-escalation-endpoint and run ` +
        `the command through scripts/with-stage-env.sh, which exports it. ` +
        `The subscriber must then confirm the SNS email.`,
      );
    }

    // ── Broker Lambda execution role ──────────────────────────────────────
    const brokerRole = new iam.Role(this, 'AvxBrokerRole', {
      roleName: `vsa-${stage}-avx-broker-role-${region}`,
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      // ASCII only: IAM rejects a role Description outside
      // [\t\n\r\x20-\x7E\xA1-\xFF]. An em dash here failed the 2026-09-24 deploy
      // (issues/2026-09-24-tier2-deploy-iam-description-charset/).
      description: `AVX broker Lambda execution role - channel-policy chokepoint (${stage}, ${region})`,
    });

    brokerRole.addToPolicy(new iam.PolicyStatement({
      sid: 'BrokerCloudWatchLogs',
      effect: iam.Effect.ALLOW,
      actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
      resources: [
        `arn:aws:logs:${region}:${account}:log-group:/aws/lambda/vsa-${stage}-avx-broker-${region}`,
        `arn:aws:logs:${region}:${account}:log-group:/aws/lambda/vsa-${stage}-avx-broker-${region}:*`,
      ],
    }));

    // DynamoDB Streams access for avx-findings stream (event source)
    brokerRole.addToPolicy(new iam.PolicyStatement({
      sid: 'BrokerAvxFindingsStreamRead',
      effect: iam.Effect.ALLOW,
      actions: [
        'dynamodb:GetRecords',
        'dynamodb:GetShardIterator',
        'dynamodb:DescribeStream',
        'dynamodb:ListStreams',
      ],
      resources: [`${this.avxFindingsTable.tableArn}/stream/*`],
    }));

    // DynamoDB: write Actions (in-app card creation)
    brokerRole.addToPolicy(new iam.PolicyStatement({
      sid: 'BrokerAvxActionsWrite',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:PutItem'],
      resources: [this.avxActionsTable.tableArn],
    }));

    // DynamoDB: query Actions (cardinality cap count via by_created_by GSI)
    brokerRole.addToPolicy(new iam.PolicyStatement({
      sid: 'BrokerAvxActionsCreatedByQuery',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:Query'],
      resources: [`${this.avxActionsTable.tableArn}/index/by_created_by`],
    }));

    // DynamoDB: rate-limit counters (conditional increment, rollback and refund
    // are all UpdateItem). GetItem was granted here and never called; removed
    // in Fix Group 33 (review Cycle 5 S12) so the grant matches the code.
    brokerRole.addToPolicy(new iam.PolicyStatement({
      sid: 'BrokerAvxRateLimitsReadWrite',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:UpdateItem'],
      resources: [this.avxRateLimitsTable.tableArn],
    }));

    // DynamoDB: notification-devices (consent + endpoint ARN lookup)
    brokerRole.addToPolicy(new iam.PolicyStatement({
      sid: 'BrokerNotificationDevicesRead',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:Query'],
      resources: [this.notificationDevicesTable.tableArn],
    }));

    // DynamoDB: feedback (demotion check)
    brokerRole.addToPolicy(new iam.PolicyStatement({
      sid: 'BrokerAvxFeedbackRead',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:Query'],
      resources: [this.avxFeedbackTable.tableArn],
    }));

    // DynamoDB: GetItem on avx-actions for the already_carded idempotency check (F32.4/F32.11).
    // The broker checks whether an Action row already exists (in any status) before any gate,
    // so a MODIFY re-delivery or a dismissed card is skipped without consuming rate budget.
    // This is a separate statement from BrokerAvxActionsWrite so the two grants can be
    // audited and mutated independently.
    brokerRole.addToPolicy(new iam.PolicyStatement({
      sid: 'BrokerAvxActionsGetItem',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:GetItem'],
      resources: [this.avxActionsTable.tableArn],
    }));

    // SNS: Publish to owner endpoint ARNs (APNs) ONLY.
    // The escalation topic is no longer a broker-published resource: CloudWatch alarms
    // (F32.8) publish to it directly, and no broker code path calls Publish on a topic ARN.
    // Removing the topic ARN here enforces that invariant at the IAM layer (F32.11).
    // ONLY the broker may call sns:Publish on endpoint ARNs — test_no_direct_sns_publish.py enforces this.
    brokerRole.addToPolicy(new iam.PolicyStatement({
      sid: 'BrokerSnsPublish',
      effect: iam.Effect.ALLOW,
      actions: ['sns:Publish'],
      resources: [
        // APNs endpoint ARNs are dynamic — sns:Publish on * scoped to the account
        // This is the narrowest possible scope given that endpoint ARNs are minted
        // by CreatePlatformEndpoint and cannot be predicted at synth time.
        `arn:aws:sns:${region}:${account}:endpoint/*/*/*`,
        // NOTE: escalationTopic.topicArn is intentionally NOT included.
        // CloudWatch alarms publish to the escalation topic as the AWS service principal.
        // No broker code path calls Publish on a topic ARN (F32.6 removed the constant).
      ],
    }));

    // SSM: cold-start policy reads (broker parameters)
    brokerRole.addToPolicy(new iam.PolicyStatement({
      sid: 'BrokerSsmPolicyRead',
      effect: iam.Effect.ALLOW,
      actions: ['ssm:GetParameter'],
      resources: [
        `arn:aws:ssm:${region}:${account}:parameter/vsa/${stage}/avx/broker/*`,
      ],
    }));

    // ── Broker Lambda log group ────────────────────────────────────────────
    const brokerLogGroup = new logs.LogGroup(this, 'AvxBrokerLogs', {
      logGroupName: `/aws/lambda/vsa-${stage}-avx-broker-${region}`,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // ── Broker Lambda function ─────────────────────────────────────────────
    //
    // Handler: agents/supervisor/lambdas/avx_broker.py
    // Bundled from project root (same pattern as the Tier 2 health agent Lambda).
    // Only boto3 is needed at runtime; no additional packages.
    const avxBrokerFn = new lambda.Function(this, 'AvxBrokerFn', {
      functionName: `vsa-${stage}-avx-broker-${region}`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'agents.supervisor.lambdas.avx_broker.handler',
      code: lambda.Code.fromAsset(
        path.join(__dirname, '../../'),
        {
          bundling: {
            image: cdk.DockerImage.fromRegistry(
              'public.ecr.aws/sam/build-python3.12:1.165.0',
            ),
            platform: 'linux/x86_64',
            command: [
              'bash', '-c',
              [
                'pip install boto3==1.43.6',
                '  -t /asset-output/',
                '  --platform manylinux2014_x86_64',
                '  --python-version 3.12',
                '  --implementation cp',
                '  --only-binary=:all:',
                '  --no-cache-dir',
                '  --quiet',
                '&& cp -r /asset-input/agents /asset-output/',
              ].join(' \\\n  '),
            ],
          },
          exclude: [
            '.venv/**', '.venv', 'node_modules/**', 'infrastructure/**',
            'evals/**', 'corpora/**', 'documentation/**', 'docs/*.pdf',
            '.git/**', '**/*.pyc', '**/__pycache__', '.bedrock_agentcore/**',
            'scripts/build-api-*.sh',
          ],
        },
      ),
      role: brokerRole,
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      logGroup: brokerLogGroup,
      environment: {
        AVX_STAGE:                   stage,
        AVX_FINDINGS_TABLE:          this.avxFindingsTable.tableName,
        AVX_ACTIONS_TABLE:           this.avxActionsTable.tableName,
        AVX_FEEDBACK_TABLE:          this.avxFeedbackTable.tableName,
        AVX_NOTIFICATION_DEVICES_TABLE: this.notificationDevicesTable.tableName,
        AVX_RATE_LIMITS_TABLE:       this.avxRateLimitsTable.tableName,
      },
      description: `AVX broker - channel-policy chokepoint for customer-facing pushes (${stage}, G7)`,
    });

    // ── DDB stream event source ────────────────────────────────────────────
    //
    // Subscribed to avx-findings NEW_IMAGE stream.
    // DLQ for exhausted records — a dead broker is a silent failure.
    const brokerDlq = new sqs.Queue(this, 'AvxBrokerDlq', {
      queueName: `vsa-${stage}-avx-broker-dlq`,
      retentionPeriod: cdk.Duration.days(14),
    });

    avxBrokerFn.addEventSource(new lambdaEventSources.DynamoEventSource(
      this.avxFindingsTable,
      {
        startingPosition: lambda.StartingPosition.TRIM_HORIZON,
        retryAttempts: 3,
        onFailure: new lambdaEventSources.SqsDlq(brokerDlq),
        reportBatchItemFailures: true,
        batchSize: 10,
        bisectBatchOnError: true,
        filters: [
          // Only NEW_IMAGE events (INSERT + MODIFY)
          lambda.FilterCriteria.filter({
            eventName: lambda.FilterRule.or('INSERT', 'MODIFY'),
          }),
        ],
      },
    ));

    // ── F32.8: Per-target-system escalation alarms + broker DLQ alarm ────────
    //
    // Problem (architect finding N3, D-G7-3):
    //   `vsa-{stage}-avx-action-terminal-failure-alerts` had zero producers.
    //   The escalation.py executor emits `put_metric_data` into namespace
    //   `AVX/Executor` metric `AVXActionTerminalFailure` with dimension
    //   `target_system=<value>` — one dimension value per registered executor.
    //   CloudWatch SEARCH expressions cannot be used as alarm sources (no
    //   cross-dimension aggregation in alarm metrics), so there must be one
    //   alarm per dimension value.
    //
    // TARGET_SYSTEMS is pinned to the Python registry's TARGET_SYSTEMS tuple
    // (agents/executors/registry.py) by the Python test named below, which
    // fails when the two sets diverge in either direction.
    //
    // IMPORTANT: these alarms can only deliver because F32.10 replaced both
    // SNS topics' key with tier2AlertsKey (customer-managed, CloudWatch-admitted).
    // Without that change every alarm action would fail, as the nine existing
    // Tier 2 health alarms did since deploy (D-G7-3 correction).

    // The target systems list is kept in sorted order so test assertions are
    // stable. Pinned to agents/executors/registry.py TARGET_SYSTEMS, in both
    // directions, by agents/supervisor/lambdas/tests/test_escalation_alarm_targets_pinned.py
    // (Python so the gating CI job runs it; jest is in no CI job).
    // Sorted: chat, dms, ios-owner, none, retail, vehicle-command
    const brokerTargetSystems: string[] = [
      'chat',
      'dms',
      'ios-owner',
      'none',
      'retail',
      'vehicle-command',
    ];

    const escalationTopicAction = new cloudwatchActions.SnsAction(escalationTopic);

    for (const targetSystem of brokerTargetSystems) {
      // Logical ID: PascalCase without hyphens so CloudFormation tracks each
      // alarm independently. e.g. 'ios-owner' → 'IosOwner'.
      const logicalSuffix = targetSystem
        .split('-')
        .map(p => p.charAt(0).toUpperCase() + p.slice(1))
        .join('');

      const alarm = new cloudwatch.Alarm(
        this,
        `AvxExecutorTerminalFailureAlarm${logicalSuffix}`,
        {
          alarmName:
            `vsa-${stage}-avx-executor-terminal-failure-${targetSystem}-${region}`,
          alarmDescription:
            `AVX executor terminal failure: target_system=${targetSystem}. ` +
            `escalation.py emitted AVXActionTerminalFailure for this target. ` +
            `Investigate CloudWatch Logs and the Action row.`,
          metric: new cloudwatch.Metric({
            namespace:     'AVX/Executor',
            metricName:    'AVXActionTerminalFailure',
            dimensionsMap: { target_system: targetSystem },
            period:        cdk.Duration.minutes(5),
            statistic:     'Sum',
          }),
          threshold:          1,
          evaluationPeriods:  1,
          comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
          // NOT_BREACHING: absence means no terminal failures (healthy). BREACHING
          // would fire immediately on deploy for every target that has never emitted.
          treatMissingData:   cloudwatch.TreatMissingData.NOT_BREACHING,
        },
      );
      alarm.addAlarmAction(escalationTopicAction);
    }

    // Broker DLQ alarm — fires when any broker record exhausts retries and lands
    // in the DLQ. A message in the DLQ means a Finding was permanently dropped.
    const brokerDlqAlarm = new cloudwatch.Alarm(this, 'AvxBrokerDlqAlarm', {
      alarmName:   `vsa-${stage}-avx-broker-dlq-${region}`,
      alarmDescription:
        `AVX broker DLQ has messages. A broker record exhausted retries ` +
        `and a Finding was dropped. Inspect vsa-${stage}-avx-broker-dlq ` +
        `and CloudWatch Logs for the root cause.`,
      metric: new cloudwatch.Metric({
        namespace:     'AWS/SQS',
        metricName:    'ApproximateNumberOfMessagesVisible',
        dimensionsMap: { QueueName: brokerDlq.queueName },
        period:        cdk.Duration.minutes(5),
        statistic:     'Maximum',
      }),
      threshold:          1,
      evaluationPeriods:  1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData:   cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    brokerDlqAlarm.addAlarmAction(escalationTopicAction);

    // ── Outputs ───────────────────────────────────────────────────────────

    new cdk.CfnOutput(this, 'Tier2HealthAgentFnArn', {
      value:      healthAgentFn.functionArn,
      exportName: `vsa-${stage}-tier2-health-agent-fn-arn`,
      description: 'ARN of the Tier 2 platform health agent Lambda',
    });

    new cdk.CfnOutput(this, 'Tier2OperatorAlertTopicArn', {
      value:      operatorAlertTopic.topicArn,
      exportName: `vsa-${stage}-tier2-operator-alert-topic-arn`,
      description: 'ARN of the operator-alert SNS topic (spec § D6)',
    });

    new cdk.CfnOutput(this, 'Tier2RunFailureAlarmArn', {
      value:      runFailureAlarm.alarmArn,
      exportName: `vsa-${stage}-tier2-run-failure-alarm-arn`,
    });

    new cdk.CfnOutput(this, 'Tier2RunDurationAlarmArn', {
      value:      runDurationAlarm.alarmArn,
      exportName: `vsa-${stage}-tier2-run-duration-alarm-arn`,
    });

    new cdk.CfnOutput(this, 'AvxDmsEventConsumerFnArn', {
      value:      avxDmsEventConsumerFn.functionArn,
      exportName: `vsa-${stage}-avx-dms-event-consumer-fn-arn`,
      description: 'ARN of the AVX DMS event consumer Lambda (Spec 2 T5.4)',
    });

    new cdk.CfnOutput(this, 'AvxRoStatusChangedRuleArn', {
      value:      avxRoStatusChangedRule.ruleArn,
      exportName: `vsa-${stage}-avx-ro-status-changed-rule-arn`,
      description: 'ARN of the DMS ro.status_changed EventBridge rule (Spec 2 T5.5)',
    });

    new cdk.CfnOutput(this, 'AvxBrokerFnArn', {
      value:      avxBrokerFn.functionArn,
      exportName: `vsa-${stage}-avx-broker-fn-arn`,
      description: 'ARN of the AVX broker Lambda (Group 7 — channel-policy chokepoint)',
    });

    new cdk.CfnOutput(this, 'AvxActionTerminalFailureAlertsTopicArn', {
      value:      escalationTopic.topicArn,
      exportName: `vsa-${stage}-avx-action-terminal-failure-alerts-arn`,
      description: 'ARN of the AVX action terminal failure escalation topic',
    });

    // Per-component alarm ARN outputs (6 alarms: 3 no-vins + 3 artifacts-stale).
    for (let i = 0; i < alarmComponents.length; i++) {
      const component = alarmComponents[i];
      const capitalised = component.charAt(0).toUpperCase() + component.slice(1);
      new cdk.CfnOutput(this, `Tier2NoVinsAlarm${capitalised}Arn`, {
        value:      noVinsAlarms[i].alarmArn,
        exportName: `vsa-${stage}-tier2-no-vins-${component}-alarm-arn`,
      });
      new cdk.CfnOutput(this, `Tier2ArtifactsStaleAlarm${capitalised}Arn`, {
        value:      artifactsStaleAlarms[i].alarmArn,
        exportName: `vsa-${stage}-tier2-artifacts-stale-${component}-alarm-arn`,
      });
    }
  }
}
