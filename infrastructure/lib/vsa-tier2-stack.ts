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
import { ConfigValue, isResolvedUserPoolId, resolveConfigValue } from './config-value';
import { FI_RECALL_WARRANTY_LIVE } from './fi-recall-warranty-live';
import { FI_REBALANCING_LIVE } from './fi-rebalancing-live';
import { FI_REBALANCING_AUTO_APPROVAL_LIVE } from './fi-rebalancing-auto-live';
import { FI_AUTO_REBOOK_LIVE } from './fi-auto-rebook-live';
import { FI_FAULT_GROUNDING_LIVE, FI_FLEET_HEALTH_LIVE, FI_FLEET_HEALTH_MODEL_LIVE } from './fi-fleet-health-live';

import * as cdk from 'aws-cdk-lib';
import * as bedrock from 'aws-cdk-lib/aws-bedrock';
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

/**
 * DMS lake tables the AVX Tier 2 role reads for FI recall/warranty: the same five its
 * FiRwGlueMetadataRead statement names. FiRwDmsLakeS3Read grants s3:GetObject on exactly these
 * prefixes (the DMS warehouse bucket is not Lake Formation-registered).
 */
export const FI_RW_DMS_LAKE_TABLES = [
  'repair_orders', 'parts_inventory', 'rooftops', 'warranty_candidates', 'warranty_terms',
] as const;

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
  /**
   * The CMS Cognito user pool (`cms-{stage}-ui-users`), the pool iOS signs in to.
   * `bin/vsa.ts` passes `voiceAuthUserPoolId`, which is the same pool. Tier B: the
   * ListUsers grant is added behind a synth-time check on this value, and a token
   * would satisfy any presence test unconditionally.
   *
   * Used to resolve a Finding's `owner_id` to the vehicle's assigned driver
   * (issue 2026-10-07-avx-finding-owner-is-purchaser-not-driver). Absent, or not a
   * concrete pool id, means no ListUsers grant, an empty `CMS_USER_POOL_ID`, and
   * every Finding written with no `owner_id` (fail closed, with a synth warning).
   */
  cmsUserPoolId?: string;
  /**
   * When true, the recall and warranty Scheduler schedules are ENABLED, their
   * run-staleness alarms fire on missed runs, and the analytics stack provisions
   * the fleet_recall Lake Formation grants.
   *
   * Defaults to FI_RECALL_WARRANTY_LIVE (false). bin/vsa.ts passes nothing, so
   * every deploy from main uses the constant. Task 5.4 flips the constant to true
   * in a reviewed commit after ADP task 5.1 ships (decisions.md call 55).
   */
  fiRecallWarrantyLive?: boolean;
  /**
   * When true, the investigations SQS event source on the Tier 2 Lambda is ENABLED,
   * the `service` schedule is ENABLED (B2), the `service` staleness alarm fires on
   * missed runs, and the analytics stack provisions stage D Lake Formation grants for
   * fleet_health / engineering / ota_campaigns (C).
   *
   * Defaults to FI_FLEET_HEALTH_LIVE (true since Group 6 rest-plan step 7). bin/vsa.ts
   * passes nothing; an explicit false turns the three switches back off.
   */
  fiFleetHealthLive?: boolean;
  /**
   * When true, sets FI_SERVICE_MODEL_ENABLED and FI_INVESTIGATION_MODEL_ENABLED to
   * 'true' on the Tier 2 Lambda. When false (default), no Bedrock client is built and
   * the rules path runs as in Group 3 (bedrock_client=None).
   *
   * Defaults to FI_FLEET_HEALTH_MODEL_LIVE (false). Flip condition: Fix A (3c7b8786)
   * merged and vsa-staging-tier2 deployed (decisions.md task43-notes.md item 1).
   */
  fiFleetHealthModelLive?: boolean;
  /**
   * When true, sets FAULT_GROUNDING_ENABLED to 'true' on the Tier 2 Lambda, so the
   * `service` component raises an operator-approved grounding item beside the booking
   * item for an open stop-driving fault (spec D9). Defaults to FI_FAULT_GROUNDING_LIVE.
   * bin/vsa.ts passes nothing; an explicit false is the rollback.
   */
  fiFaultGroundingLive?: boolean;
  /**
   * When true, the EventBridge Scheduler schedule for the ``rebalance``
   * component is ENABLED and its run-staleness alarm fires on missed runs
   * (``ActionsEnabled=true``).  The IAM grants in ``FiRebalanceTier2Policy``,
   * the Lambda env vars and every ``cdk-nag`` suppression ship unchanged
   * regardless.
   *
   * Defaults to ``FI_REBALANCING_LIVE`` (``false``) so the schedule ships
   * disabled.  Group 6's deploy flips the constant to ``true`` after ADP's
   * fleet_bookings/forecast views land and the live-verify step passes.
   * ``agents/tier2/tests/test_fi_rebalancing_live_defaults_off.py`` fails if
   * the constant is set to ``true`` outside Group 6 (spec D8, task 5.2).
   */
  fiRebalancingLive?: boolean;
  /**
   * Rule-approved moves (stage E Group 8, U6). Defaults to
   * ``FI_REBALANCING_AUTO_APPROVAL_LIVE`` (``true`` since task 8.4): on,
   * ``cmsFiInternalApiEndpoint`` is required. Off (an explicit ``false``), the auto-approver
   * has no CMS endpoint, switch, execute-api or escalation grants.
   */
  fiRebalancingAutoApprovalLive?: boolean;
  /** CMS's internal API base URL (Tier A, `/cvx/{stage}/cms-fi-internal-api-endpoint`). */
  cmsFiInternalApiEndpoint?: ConfigValue;
  /**
   * DMS internal API base URL (Tier A, `/cvx/{stage}/dms-fi-internal-api-endpoint`).
   *
   * Required for the simulated fleet operator to call the DMS repair-orders and
   * warranty-claims internal routes (spec D10, D10e).  When absent the operator
   * runs without DMS calls: ``VSA_DMS_INTERNAL_API_ENDPOINT`` is omitted from the
   * function environment and the two execute-api grants are not added.
   */
  dmsFiInternalApiEndpoint?: ConfigValue;
  /**
   * Rule-approved rebooking (FI Ground Book Return task 11.7 / Group 8, D7). Defaults to
   * ``FI_AUTO_REBOOK_LIVE`` (``true`` since 2026-11-08, prompt 347): on,
   * ``dmsFiInternalApiEndpoint`` is required. Off, the auto-approver
   * has no DMS rebook endpoint, switch, execute-api, findings/groundings/actions grants.
   */
  fiAutoRebookLive?: boolean;
  /**
   * Synthesize the simulated fleet operator (spec D10: ledger table, Lambda, schedule, alarms).
   * Default off; bin/vsa.ts passes ``true`` from task 11.9 (prompt 287).
   */
  fiSimOperator?: boolean;
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
  /** Per-fleet auto-approval rules (FI Action Queue Stage A, spec D9). */
  readonly avxApprovalRulesTable: dynamodb.Table;
  /** One record per agent run, read by CMS for "last ran" (spec D10). */
  readonly avxAgentRunsTable: dynamodb.Table;

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

    // task 4.3: one flag gates recall/warranty schedules, alarm actions, and analytics LF
    // grants. Defaults to the module constant; bin/vsa.ts passes nothing.
    const fiRwLive = props.fiRecallWarrantyLive ?? FI_RECALL_WARRANTY_LIVE;
    // task 4.3 B1: fleet-health flags (both off by default).
    const fiHealthLive        = props.fiFleetHealthLive        ?? FI_FLEET_HEALTH_LIVE;
    // task 4.3 B2: model switch. Off by default; flip after Fix A (3c7b8786) is deployed.
    const fiHealthModelLive   = props.fiFleetHealthModelLive   ?? FI_FLEET_HEALTH_MODEL_LIVE;
    // Stage D 7.7: the fault-grounding switch (spec D9). Off until its own flip.
    const faultGroundingLive  = props.fiFaultGroundingLive     ?? FI_FAULT_GROUNDING_LIVE;
    // task 5.2: rebalance schedule + alarm action switch. Off by default; Group 6's
    // deploy flips the FI_REBALANCING_LIVE constant to true after ADP's fleet_bookings
    // / forecast views land and the live-verify step passes (spec D8, decisions
    // 2026-10-14). Setting it back to false disables the schedule and alarm action.
    const fiRebalLive         = props.fiRebalancingLive        ?? FI_REBALANCING_LIVE;

    // ── F11.2: CMS vehicle-registry coordinates ──────────────────────────────
    //
    // The registry (cms-{stage}-storage-vehicles) supplies each VIN's vehicleId
    // and fleetId, which Tier 2 projects onto AVX Finding DDB items. The owner_id
    // projection comes from the CMS drivers table and the CMS pool instead (the
    // vehicle's assigned driver, issue 2026-10-07); `sold_to` is no longer read.
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
    const cmsDriversTableArn =
      `arn:aws:dynamodb:${cmsRegion}:${cmsAccount}:table/cms-${cmsStage}-storage-drivers`;
    // The CMS pool, for owner_id resolution. Concrete and well-formed, or nothing.
    const cmsUserPoolId = isResolvedUserPoolId(props.cmsUserPoolId)
      ? props.cmsUserPoolId!.trim()
      : undefined;
    if (cmsUserPoolId === undefined) {
      cdk.Annotations.of(this).addWarning(
        `No CMS user pool id (cmsUserPoolId / voiceAuthUserPoolId). AVX Findings ` +
        `will be written with no owner_id: the assigned-driver lookup needs the pool.`,
      );
    }

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
    //   "vsa-staging-avx-approval-rules"    = 30 chars (FI Action Queue Stage A)
    //   "vsa-staging-avx-agent-runs"        = 26 chars (FI Action Queue Stage A)
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
    // timeToLiveAttribute: 'ttl' — spec §B A1. DynamoDB deletes a row when its
    // `ttl` attribute (Number, epoch seconds) is in the past. TTL deletes arrive
    // as stream REMOVE events with userIdentity.principalId = dynamodb.amazonaws.com;
    // both stream consumers (broker, DMS event consumer) already filter REMOVE events.
    // Writers set ttl = expires_at + 7 days only on informational rows (no
    // recommended_action, not recall/quality agents) — see agents/tier2/writer.py.
    // A row without a `ttl` attribute is kept indefinitely (fail safe).
    this.avxFindingsTable = new dynamodb.Table(this, 'AvxFindings', {
      tableName:    `vsa-${stage}-avx-findings`,
      partitionKey: { name: 'finding_id', type: dynamodb.AttributeType.STRING },
      billingMode:  dynamodb.BillingMode.PAY_PER_REQUEST,
      stream:       dynamodb.StreamViewType.NEW_IMAGE,
      timeToLiveAttribute: 'ttl',
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
    // FI Action Queue Stage A, spec D11: the fleet's decisions, newest first. Sparse:
    // only Actions written by approve, decline or a rule carry the storage-only
    // top-level `fleet_id`. CMS reads it directly (spec D7); `by_<field>` naming per
    // the T10.1 rule, so any Python reader uses this exact name.
    this.avxActionsTable.addGlobalSecondaryIndex({
      indexName:    'by_fleet',
      partitionKey: { name: 'fleet_id',   type: dynamodb.AttributeType.STRING },
      sortKey:      { name: 'updated_at', type: dynamodb.AttributeType.STRING },
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
    // the AVX tables; the others use TableEncryption.DEFAULT per the
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

    // ── FI Action Queue Stage A: approval rules (spec D9) ─────────────────
    //
    // PK: fleet_id (S), SK: rule_id (S). Written by api-avx-approval-rules (API
    // stack, via the export below); read by avx-auto-approver (this stack) and by
    // CMS directly (spec D7). No GSI: every read is one fleet's partition.
    this.avxApprovalRulesTable = new dynamodb.Table(this, 'AvxApprovalRules', {
      tableName:    `vsa-${stage}-avx-approval-rules`,
      partitionKey: { name: 'fleet_id', type: dynamodb.AttributeType.STRING },
      sortKey:      { name: 'rule_id',  type: dynamodb.AttributeType.STRING },
      billingMode:  dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    new cdk.CfnOutput(this, 'AvxApprovalRulesTableName', {
      value:      this.avxApprovalRulesTable.tableName,
      exportName: `vsa-${stage}-avx-approval-rules-table-name`,
    });

    // ── FI Action Queue Stage A: run records (spec D10, contract § 8) ──────
    //
    // PK: agent_component (S) = "<agent_id>#<component>", SK: started_at (S).
    // Written by the Tier 2 runner (one per run) and the auto-approver (one per
    // batch); read by CMS directly for each agent's last run.
    this.avxAgentRunsTable = new dynamodb.Table(this, 'AvxAgentRuns', {
      tableName:    `vsa-${stage}-avx-agent-runs`,
      partitionKey: { name: 'agent_component', type: dynamodb.AttributeType.STRING },
      sortKey:      { name: 'started_at',      type: dynamodb.AttributeType.STRING },
      billingMode:  dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      // T1.5 Stage A hardening: 400-day TTL so stale run records age out.
      // The attribute name 'ttl' follows repo precedent for all AVX tables.
      // The writer (run_records.py) sets ttl = finished_at + 400 days (epoch seconds).
      timeToLiveAttribute: 'ttl',
    });

    new cdk.CfnOutput(this, 'AvxAgentRunsTableName', {
      value:      this.avxAgentRunsTable.tableName,
      exportName: `vsa-${stage}-avx-agent-runs-table-name`,
    });

    // ── task 4.3 B1: AVX Investigations table, DLQ, and queue ─────────────
    //
    // PK: investigation_id (S). One record per investigation request.
    // PAY_PER_REQUEST (workload is bursty; no predictable steady-state).
    // PITR: enabled (field carries PII-adjacent data).
    // RETAIN: prevents accidental loss of investigation history on stack destroy.
    //
    // DLQ and queue:
    //   Visibility timeout 5,400 s:
    //     ≥ running-stale 960 s (decision 3);
    //     5,400 + Tier2 Lambda timeout 900 s < queued-stale 7,200 s (decision 3).
    //   maxReceiveCount 2 = SQS_MAX_RECEIVE_COUNT: one retry on first-attempt failure.
    //   Both queues: SQS-managed encryption + enforceSSL (no customer key needed;
    //     visibility timeout > CMK re-encrypt window risk is irrelevant for SQS).
    const avxInvestigationsTable = new dynamodb.Table(this, 'AvxInvestigationsTable', {
      tableName:    `vsa-${stage}-avx-investigations`,
      partitionKey: { name: 'investigation_id', type: dynamodb.AttributeType.STRING },
      billingMode:  dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    new cdk.CfnOutput(this, 'AvxInvestigationsTableName', {
      value:      avxInvestigationsTable.tableName,
      exportName: `vsa-${stage}-avx-investigations-table-name`,
    });

    const avxInvestigationsDlq = new sqs.Queue(this, 'AvxInvestigationsDlq', {
      queueName:      `vsa-${stage}-avx-investigations-dlq`,
      retentionPeriod: cdk.Duration.days(14),
      enforceSSL:      true,
      encryption:      sqs.QueueEncryption.SQS_MANAGED,
    });

    const avxInvestigationsQueue = new sqs.Queue(this, 'AvxInvestigationsQueue', {
      queueName:           `vsa-${stage}-avx-investigations`,
      visibilityTimeout:   cdk.Duration.seconds(5400),
      deadLetterQueue:     { queue: avxInvestigationsDlq, maxReceiveCount: 2 },
      enforceSSL:          true,
      encryption:          sqs.QueueEncryption.SQS_MANAGED,
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

    // FI Action Queue Stage A, spec D10: one run record per run
    // (agents/tier2/run_records.py::write_run_record, put_item only). Table ARN only.
    lambdaRole.addToPolicy(new iam.PolicyStatement({
      sid: 'AvxAgentRunsWrite',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:PutItem'],
      resources: [this.avxAgentRunsTable.tableArn],
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
      // ProjectionExpression limited to `fleetId`; IAM cannot constrain
      // attribute projection, so the narrowing that matters here is the
      // single action and the single resource.
      resources: [cmsVehiclesTableArn],
    }));

    // ── owner_id: the vehicle's assigned driver (issue 2026-10-07) ───────────
    //
    // agents/tier2/owner_resolver.py AssignedDriverDirectory makes exactly two
    // kinds of call, once per run rather than once per Finding:
    //   - Scan on cms-{stage}-storage-drivers. The table is keyed on driverId and
    //     has NO index on assignedVehicleId (verified 2026-10-07), so a Scan is the
    //     only way to find a vehicle's driver. Its ProjectionExpression is limited
    //     to driverId, assignedVehicleId and cognitoEmail. The table's CMK allows
    //     DynamoDB-mediated use by any principal in the account, so no KMS grant.
    //   - ListUsers on the CMS pool, filtered to one email, to turn the driver's
    //     cognitoEmail into a sub. IAM cannot constrain the filter; the grant is
    //     one action on one pool. No AdminGetUser, no write action.
    //
    // Mutations guarded by infrastructure/test/vsa-tier2-stack-owner-driver.test.ts and
    // agents/tier2/tests/test_owner_driver_grants_pinned.py:
    //   resources -> ['*'], an extra action, or the Scan pointed at another table
    //   each fail a test.
    lambdaRole.addToPolicy(new iam.PolicyStatement({
      sid: 'CmsDriversAssignmentScan',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:Scan'],
      resources: [cmsDriversTableArn],
    }));

    if (cmsUserPoolId !== undefined) {
      const poolRegion = cmsUserPoolId.split('_')[0];
      lambdaRole.addToPolicy(new iam.PolicyStatement({
        sid: 'CmsUserPoolDriverLookup',
        effect: iam.Effect.ALLOW,
        actions: ['cognito-idp:ListUsers'],
        resources: [`arn:aws:cognito-idp:${poolRegion}:${cmsAccount}:userpool/${cmsUserPoolId}`],
      }));
    }

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
    // Correction, 2026-10-05 (FI cost Stage B decisions item 8): the lake bucket is
    // registered with Lake Formation, and Athena reads LF-governed tables with
    // credentials LF vends (`lakeformation:GetDataAccess` below), not with this role's
    // S3 grants: the brakes component reads the Iceberg `service_records` under
    // `iceberg/`, which no grant here names. The next paragraph is the 2026-08 reading.
    // These prefix grants are kept as they were; a new reader of a LF-governed table
    // needs Lake Formation and Glue grants, not an S3 prefix here.
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
    // Correction, 2026-10-05: as for the S3 grants above, Athena reads LF-governed
    // tables with credentials Lake Formation vends, so this grant is not on that path
    // (FI cost Stage B decisions item 8). It is kept as it was; the next paragraph is
    // the 2026-08 reading.
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

    // ── FI cost component (spec 2026-10-02-fi-cost-stage-b task 4.2) ────────
    //
    // The `cost` component (agent-fi-cost) runs in this Lambda daily at 04:00 UTC.
    // It reads ADP through the same Athena workgroup as the health components and
    // calls Sonnet 4.6 through the inference profile granted above (one Converse call
    // per changed vehicle, at most one retry, spec D6). What it adds here:
    //
    //   1. The Bedrock Guardrail its model narration (headline, detail) is checked
    //      against with ApplyGuardrail, source OUTPUT (D6 step 5).
    //   2. Glue metadata on the databases its SQL and the ADP views read. A standard
    //      Athena view is resolved as the caller, so the views' base tables count too
    //      (docs/tech.md § "Athena views over Lake Formation-governed tables").
    //      `dimensions` is reached for the one table `vins`, never the database's
    //      tables: it also holds `customers` (PII).
    //   3. No S3 grant on the ADP lake for any of it. The lake bucket is registered
    //      with Lake Formation, so Athena reads LF-governed tables with credentials
    //      LF vends after `lakeformation:GetDataAccess` (granted above); the Lake
    //      Formation grants are in vsa-analytics-stack.ts. Checked read-only
    //      2026-10-05: the Iceberg `service_records` the brakes component reads lives
    //      under `iceberg/`, a prefix this role has no S3 grant on, and brakes wrote
    //      2,000 Findings that night.
    //   4. An explicit Deny on the lake's `restricted/` prefix, where ADP keeps the
    //      planted-anomaly answer key (ADP decisions 2026-10-04 item 7). No Glue table
    //      points there, so no LF grant can reach it; the Deny keeps any future S3
    //      Allow on this role from reaching it either (spec R2).
    //   5. `ssm:GetParameter` on its one policy value, max_diagnoses_per_run.
    //
    // Guardrail limits are service-enforced, not synth-enforced (vsa-core-stack.ts,
    // KioskZone2Guardrail comment): the length checks below fail synth instead.
    const fiCostGuardrailName = `vsa-${stage}-fi-cost-guardrail`;
    const fiCostGuardrailDescription =
      'Output checks on the FI cost agent narration (headline, detail): PII, ' +
      'harmful content, and advice outside fleet vehicle operating cost.';
    const fiCostBlockedInputMessaging = 'This cost review input was blocked.';
    const fiCostBlockedOutputsMessaging =
      'This cost recommendation was withheld by a content check.';
    const fiCostTopics: bedrock.CfnGuardrail.TopicConfigProperty[] = [
      {
        name: 'personal-advice',
        // The 2026-10-10 04:00Z run tripped this topic on 74 model attempts
        // (`fi_cost.agent: attempt N — guardrail INTERVENED` lines in
        // `/aws/lambda/vsa-staging-tier2-health-agent-us-west-2`, run
        // 2026-10-10T04:00:48Z): legitimate cost replies that recommend an
        // *operational* action — "shift charging to depot off-peak", "reassign
        // or remarket the vehicle" — were classified as off-topic advice.
        //
        // The topic is meant to catch *personal* legal, medical, tax,
        // investment, or employment advice directed at the human reader of
        // the Finding, not operational recommendations grounded in fleet cost
        // data. The 2026-10-10 definition's trailing "or any statement outside
        // the operating cost of a fleet vehicle" and the "move the fleet budget
        // into index funds" example trained the matcher to treat "shift money"
        // as a trip; the 2026-10-12 fix dropped both.
        //
        // Driver-conduct advice ("dismiss this driver") is covered by the
        // `blame-of-a-person` topic below and no longer appears here —
        // keeping it in both led the matcher to blur the boundaries.
        //
        // 2026-10-12 to 2026-10-17: that narrowing (a definition ending
        // "Operational fleet-cost actions (shift charging, reassign vehicles,
        // inspect, coach) are not this topic.", under the old topic name
        // `advice-outside-vehicle-cost`) still blocked 4 of 7 good cost probe
        // replies and 7 of 8 real model cost replies.
        //
        // 2026-10-17, measured on temporary guardrails
        // (issues/2026-10-10-cost-guardrail-blocks-replies/2026-10-17-examples-attempt.md):
        //   - The NAME was the main cause. Bedrock's guidance is not to describe
        //     the topic in its name; `advice-outside-vehicle-cost` contained
        //     "advice" and "vehicle cost", which every cost recommendation is
        //     about. Renamed `personal-advice`, every good cost reply passed.
        //   - The exception sentence is gone too (Bedrock: don't define
        //     exceptions in a topic); it put the blocked replies' own words into
        //     the topic. The definition now only says what the topic is.
        //   - The rename alone let one personal-legal negative through ("you could
        //     sue the charging network in small claims court"). The STANDARD topic
        //     tier, set below, closed it: 0 good replies blocked, every negative
        //     blocked, the 538 real recall/warranty replies unchanged.
        // The name appears in the agents' `policies_blocked=` log lines.
        definition:
          'Personal legal, medical, tax, investment, or employment advice to the reader.',
        // Bedrock topic examples are samples of content the topic BLOCKS; there
        // is no allow-side example (API GuardrailTopicConfig.examples: 0..5
        // items, 1..100 chars each). So the boundary is taught from the block
        // side: one personal-advice example per category the definition names
        // (tax, legal, investment, medical, employment), and none in fleet
        // operations language. 2026-10-17: the medical and employment examples
        // fill the topic to Bedrock's limit of five
        // (issues/2026-10-10-cost-guardrail-blocks-replies/2026-10-17-examples-attempt.md).
        examples: [
          'This expense may be deductible on your personal 2026 tax return.',
          'You should see a lawyer before signing the fleet lease amendment.',
          'Buy an index fund to hedge the energy budget.',
          'Ask your physician about medication for your recurring migraines.',
          'You should ask your manager for a raise or start looking for a new job.',
        ],
        type: 'DENY',
      },
      {
        name: 'blame-of-a-person',
        definition:
          'Attributing cost to a named or identifiable person, or judging the character ' +
          'or conduct of a driver or staff member.',
        examples: [
          'The driver is careless and wasteful.',
          'Fire the person responsible for charging.',
          'You should dismiss this driver.',
        ],
        type: 'DENY',
      },
    ];
    const assertFiCostGuardrailLength = (prop: string, value: string, max: number): void => {
      if (value.length < 1 || value.length > max) {
        throw new Error(
          `FiCostGuardrail ${prop} is ${value.length} chars; Bedrock allows 1..${max}. ` +
            'This is rejected server-side mid-deploy, never by synth.',
        );
      }
    };
    assertFiCostGuardrailLength('Name', fiCostGuardrailName, 50);
    assertFiCostGuardrailLength('Description', fiCostGuardrailDescription, 200);
    assertFiCostGuardrailLength('BlockedInputMessaging', fiCostBlockedInputMessaging, 500);
    assertFiCostGuardrailLength('BlockedOutputsMessaging', fiCostBlockedOutputsMessaging, 500);
    for (const topic of fiCostTopics) {
      assertFiCostGuardrailLength(`topic '${topic.name}' name`, topic.name, 100);
      assertFiCostGuardrailLength(`topic '${topic.name}' definition`, topic.definition, 200);
      const examples = topic.examples ?? [];
      if (examples.length > 5) {
        throw new Error(`FiCostGuardrail topic '${topic.name}' has ${examples.length} examples; max 5.`);
      }
      examples.forEach((ex, i) =>
        assertFiCostGuardrailLength(`topic '${topic.name}' example[${i}]`, ex, 100));
    }

    // Cross-Region guardrail inference (the STANDARD topic tier requires it). The
    // US guardrail profile `us.guardrail.v1:0`, called from us-east-1, us-east-2
    // or us-west-2, routes each check to one of those same three Regions (Bedrock
    // user guide, "Supported Regions for cross-Region guardrail inference",
    // checked 2026-10-18). From us-west-1 it adds us-west-1, and outside the US it
    // isn't available, so synth refuses any other Region rather than ship a grant
    // for a destination set nobody checked. The checked text is the agents' model
    // narration (headline, detail). User decision 2026-10-18 (PO prompt 28,
    // option 1); issues/2026-10-10-cost-guardrail-blocks-replies/.
    const fiCostGuardrailProfileId = 'us.guardrail.v1:0';
    const fiCostGuardrailRegions = ['us-east-1', 'us-east-2', 'us-west-2'];
    if (cdk.Token.isUnresolved(region) || !fiCostGuardrailRegions.includes(region)) {
      throw new Error(
        `FiCostGuardrail uses the US guardrail profile ${fiCostGuardrailProfileId}, whose ` +
          `destinations are ${fiCostGuardrailRegions.join(', ')} only when called from one of ` +
          `them; this stack's Region is ${cdk.Token.isUnresolved(region) ? 'unresolved' : region}. ` +
          'Check the profile table in the Bedrock user guide and update the Region list ' +
          'and the ApplyFiCostGuardrail grant together.',
      );
    }
    const fiCostGuardrailProfileArn = (r: string): string =>
      `arn:aws:bedrock:${r}:${account}:guardrail-profile/${fiCostGuardrailProfileId}`;
    const fiCostGuardrail = new bedrock.CfnGuardrail(this, 'FiCostGuardrail', {
      name: fiCostGuardrailName,
      description: fiCostGuardrailDescription,
      blockedInputMessaging: fiCostBlockedInputMessaging,
      blockedOutputsMessaging: fiCostBlockedOutputsMessaging,
      topicPolicyConfig: {
        topicsConfig: fiCostTopics,
        // STANDARD, not the default CLASSIC: the only tier that both let every
        // good cost reply through and blocked every personal-advice negative
        // (see the personal-advice topic comment). It requires crossRegionConfig.
        topicsTierConfig: { tierName: 'STANDARD' },
      },
      crossRegionConfig: { guardrailProfileArn: fiCostGuardrailProfileArn(region) },
      // The narration names a vehicle by VIN and a fleet by id; neither is blocked.
      // People's identifiers are.
      sensitiveInformationPolicyConfig: {
        piiEntitiesConfig: [
          { type: 'NAME', action: 'BLOCK' },
          { type: 'EMAIL', action: 'BLOCK' },
          { type: 'PHONE', action: 'BLOCK' },
          { type: 'ADDRESS', action: 'BLOCK' },
          { type: 'US_SOCIAL_SECURITY_NUMBER', action: 'BLOCK' },
          { type: 'CREDIT_DEBIT_CARD_NUMBER', action: 'BLOCK' },
        ],
      },
      contentPolicyConfig: {
        filtersConfig: [
          { type: 'HATE', inputStrength: 'MEDIUM', outputStrength: 'MEDIUM' },
          { type: 'INSULTS', inputStrength: 'MEDIUM', outputStrength: 'MEDIUM' },
          { type: 'SEXUAL', inputStrength: 'MEDIUM', outputStrength: 'MEDIUM' },
          { type: 'VIOLENCE', inputStrength: 'MEDIUM', outputStrength: 'MEDIUM' },
          { type: 'MISCONDUCT', inputStrength: 'MEDIUM', outputStrength: 'MEDIUM' },
        ],
      },
    });

    // With cross-Region inference the caller needs ApplyGuardrail on the guardrail
    // in this Region and on the guardrail profile in every destination Region
    // (Bedrock user guide, "Permissions for using cross-Region inference with Amazon
    // Bedrock Guardrails", checked 2026-10-18). No guardrail ARN in another Region,
    // no other profile, no wildcard.
    lambdaRole.addToPolicy(new iam.PolicyStatement({
      sid: 'ApplyFiCostGuardrail',
      effect: iam.Effect.ALLOW,
      actions: ['bedrock:ApplyGuardrail'],
      resources: [
        fiCostGuardrail.attrGuardrailArn,
        ...fiCostGuardrailRegions.map(fiCostGuardrailProfileArn),
      ],
    }));

    lambdaRole.addToPolicy(new iam.PolicyStatement({
      sid: 'FiCostGlueMetadataRead',
      effect: iam.Effect.ALLOW,
      actions: ['glue:GetDatabase', 'glue:GetTable', 'glue:GetTables', 'glue:GetPartitions'],
      resources: [
        // The views and fleet_cost_events (fleet_cost); daily_usage (fleet_ops).
        `arn:aws:glue:${adpRegion}:${adpAccount}:database/adp_${adpStage}_fleet_cost`,
        `arn:aws:glue:${adpRegion}:${adpAccount}:table/adp_${adpStage}_fleet_cost/*`,
        `arn:aws:glue:${adpRegion}:${adpAccount}:database/adp_${adpStage}_fleet_ops`,
        `arn:aws:glue:${adpRegion}:${adpAccount}:table/adp_${adpStage}_fleet_ops/*`,
        // The vehicle dimension the monthly view joins for model and model_year.
        `arn:aws:glue:${adpRegion}:${adpAccount}:database/adp_${adpStage}_dimensions`,
        `arn:aws:glue:${adpRegion}:${adpAccount}:table/adp_${adpStage}_dimensions/vins`,
        // Sell timing's purchase price (D10).
        `arn:aws:glue:${adpRegion}:${adpAccount}:database/adp_${adpStage}_vehicle_pricing`,
        `arn:aws:glue:${adpRegion}:${adpAccount}:table/adp_${adpStage}_vehicle_pricing/vehicle_pricing`,
      ],
    }));

    lambdaRole.addToPolicy(new iam.PolicyStatement({
      sid: 'DenyAdpRestrictedObjects',
      effect: iam.Effect.DENY,
      actions: ['s3:GetObject', 's3:GetObjectVersion'],
      resources: [`${adpLakeBucketArn}/restricted/*`],
    }));
    lambdaRole.addToPolicy(new iam.PolicyStatement({
      sid: 'DenyAdpRestrictedListing',
      effect: iam.Effect.DENY,
      actions: ['s3:ListBucket'],
      resources: [adpLakeBucketArn],
      conditions: { StringLike: { 's3:prefix': ['restricted', 'restricted/*'] } },
    }));

    lambdaRole.addToPolicy(new iam.PolicyStatement({
      sid: 'FiCostPolicyParameterRead',
      effect: iam.Effect.ALLOW,
      actions: ['ssm:GetParameter'],
      // agents/tier2/runner.FI_COST_MAX_DIAGNOSES_PARAM; seeded from
      // deployment/config/avx-policy-defaults.json.
      resources: [
        `arn:aws:ssm:${region}:${account}:parameter/vsa/${stage}/avx/fi-cost/max-diagnoses-per-run`,
      ],
    }));

    // ── FI recall/warranty: Glue + CMS DynamoDB + AVX DynamoDB (task 4.3) ────
    //
    // Six statements (with FiRwDmsLakeS3Read below), unconditional on the IAM role:
    // IAM does not need the
    // resource to exist.  The analytics stack's fleet_recall LF grants are gated
    // on fiRwLive, but the Glue GetDatabase call would fail at query time (not
    // deploy time) without these, so the grant is always present.
    lambdaRole.addToPolicy(new iam.PolicyStatement({
      sid: 'FiRwGlueMetadataRead',
      effect: iam.Effect.ALLOW,
      actions: ['glue:GetDatabase', 'glue:GetTable', 'glue:GetTables', 'glue:GetPartitions'],
      resources: [
        // ADP fleet_recall database + all tables (recall component)
        `arn:aws:glue:${adpRegion}:${adpAccount}:database/adp_${adpStage}_fleet_recall`,
        `arn:aws:glue:${adpRegion}:${adpAccount}:table/adp_${adpStage}_fleet_recall/*`,
        // DMS database (warranty/recall) — in the ADP account and region
        `arn:aws:glue:${adpRegion}:${adpAccount}:database/dms_${adpStage}`,
        `arn:aws:glue:${adpRegion}:${adpAccount}:table/dms_${adpStage}/repair_orders`,
        `arn:aws:glue:${adpRegion}:${adpAccount}:table/dms_${adpStage}/parts_inventory`,
        `arn:aws:glue:${adpRegion}:${adpAccount}:table/dms_${adpStage}/rooftops`,
        `arn:aws:glue:${adpRegion}:${adpAccount}:table/dms_${adpStage}/warranty_candidates`,
        `arn:aws:glue:${adpRegion}:${adpAccount}:table/dms_${adpStage}/warranty_terms`,
      ],
    }));

    // The DMS Iceberg warehouse bucket is NOT registered with Lake Formation (only the ADP lake
    // is), so Athena reads its files with this role's own IAM: DMS's LF grants cover the catalog
    // only. recall_vehicle_status joins dms_{stage}.repair_orders; the recall run also reads
    // rooftops and parts_inventory, and the warranty run reads candidates and terms. One prefix per table the role holds catalog grants for, never the
    // bucket. SSE-S3, so no KMS grant. Issue 2026-10-09-tier2-role-cannot-read-dms-iceberg-s3.
    const fiRwDmsLakeBucket = `dms-${adpStage}-iceberg-${adpRegion}-${adpAccount}`;
    lambdaRole.addToPolicy(new iam.PolicyStatement({
      sid: 'FiRwDmsLakeS3Read',
      effect: iam.Effect.ALLOW,
      actions: ['s3:GetObject'],
      resources: FI_RW_DMS_LAKE_TABLES.map(
        (t) => `arn:aws:s3:::${fiRwDmsLakeBucket}/iceberg/dms_${adpStage}.db/${t}/*`),
    }));

    lambdaRole.addToPolicy(new iam.PolicyStatement({
      sid: 'FiRwCmsFleetLocationsQuery',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:Query'],
      resources: [`arn:aws:dynamodb:${cmsRegion}:${cmsAccount}:table/cms-${cmsStage}-storage-fleet-locations`],
    }));

    lambdaRole.addToPolicy(new iam.PolicyStatement({
      sid: 'FiRwCmsGroundingsQuery',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:Query'],
      resources: [`arn:aws:dynamodb:${cmsRegion}:${cmsAccount}:table/cms-${cmsStage}-storage-vehicle-groundings`],
    }));

    lambdaRole.addToPolicy(new iam.PolicyStatement({
      sid: 'FiRwFindingsByVinQuery',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:Query'],
      // Narrow: the by_vin GSI ARN only (task brief: only this index, not the table).
      resources: [`${this.avxFindingsTable.tableArn}/index/by_vin`],
    }));

    lambdaRole.addToPolicy(new iam.PolicyStatement({
      sid: 'FiRwActionsGetItem',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:GetItem'],
      resources: [this.avxActionsTable.tableArn],
    }));

    // ── FI stage D 9.3: the recall run's engineering reads (Glue) ──────────
    //
    // ADP's recall views read adp_{stage}_engineering.recall_campaigns_issued and
    // vehicle_build_components since stage D 9.1 (a standard view resolves as the caller),
    // and the recall query reads recall_campaigns_issued for the lots it narrates. The Lake
    // Formation half is in vsa-analytics-stack.ts (fiRwLive). The recall component owns its
    // own statement rather than leaning on FiHealthGlueMetadataRead, which is pinned to stage
    // D's fleet-health reads and would lose vehicle_build_components if those reads changed.
    // Its own ManagedPolicy: the role's inline policies are near IAM's 10,240-byte aggregate
    // (budget test in vsa-tier2-stack-fi-rebalance.test.ts). Named tables only; the catalog
    // ARN is in AdpGlueMetadataRead. Unconditional, like FiRwGlueMetadataRead: IAM doesn't
    // need the table to exist.
    new iam.ManagedPolicy(this, 'FiRwEngineeringReadPolicy', {
      roles: [lambdaRole],
      statements: [
        new iam.PolicyStatement({
          sid: 'FiRwEngineeringGlueRead',
          effect: iam.Effect.ALLOW,
          actions: ['glue:GetDatabase', 'glue:GetTable', 'glue:GetTables', 'glue:GetPartitions'],
          resources: [
            `arn:aws:glue:${adpRegion}:${adpAccount}:database/adp_${adpStage}_engineering`,
            `arn:aws:glue:${adpRegion}:${adpAccount}:table/adp_${adpStage}_engineering/recall_campaigns_issued`,
            `arn:aws:glue:${adpRegion}:${adpAccount}:table/adp_${adpStage}_engineering/vehicle_build_components`,
          ],
        }),
      ],
    });

    // ── task 4.3 B1: FI fleet-health Tier 2 IAM grants ────────────────────
    //
    // These three statements are grouped into an explicit ManagedPolicy so that
    // CDK never splits them into an overflow policy (the inline policy for this
    // role already approaches 10,000 chars when cmsUserPoolId is supplied).
    // Attaching via `roles` keeps the effect identical to addToPolicy.
    const fiHealthTier2Policy = new iam.ManagedPolicy(this, 'FiHealthTier2Policy', {
      roles: [lambdaRole],
      statements: [
        // FiHealthInvestigationsTable: exactly the two DynamoDB calls job.py makes
        // (get_item for the row read; update_item for all status transitions).
        // Resource: the investigations table ARN only (no index, no wildcard).
        new iam.PolicyStatement({
          sid: 'FiHealthInvestigationsTable',
          effect: iam.Effect.ALLOW,
          actions: ['dynamodb:GetItem', 'dynamodb:UpdateItem'],
          resources: [avxInvestigationsTable.tableArn],
        }),

        // FiHealthGlueMetadataRead: named tables only — no fleet_health/* wildcard.
        // 3 databases + 11 named tables that job.py's Athena queries touch
        // (decision 5 / task43-notes.md item 5).
        // vehicle_telemetry_aggregated omitted: read_telemetry_aggregates is a stub
        // returning [] — no live query runs against that table (S1).
        new iam.PolicyStatement({
          sid: 'FiHealthGlueMetadataRead',
          effect: iam.Effect.ALLOW,
          actions: ['glue:GetDatabase', 'glue:GetTable', 'glue:GetTables', 'glue:GetPartitions'],
          resources: [
            // fleet_health database + 5 named tables
            `arn:aws:glue:${adpRegion}:${adpAccount}:database/adp_${adpStage}_fleet_health`,
            `arn:aws:glue:${adpRegion}:${adpAccount}:table/adp_${adpStage}_fleet_health/vehicle_fault_status_daily`,
            `arn:aws:glue:${adpRegion}:${adpAccount}:table/adp_${adpStage}_fleet_health/pm_vehicle_status_daily`,
            `arn:aws:glue:${adpRegion}:${adpAccount}:table/adp_${adpStage}_fleet_health/vehicle_utilisation_daily`,
            `arn:aws:glue:${adpRegion}:${adpAccount}:table/adp_${adpStage}_fleet_health/fault_codes`,
            `arn:aws:glue:${adpRegion}:${adpAccount}:table/adp_${adpStage}_fleet_health/snapshot_runs`,
            // engineering database + 4 named tables
            `arn:aws:glue:${adpRegion}:${adpAccount}:database/adp_${adpStage}_engineering`,
            `arn:aws:glue:${adpRegion}:${adpAccount}:table/adp_${adpStage}_engineering/fault_patterns`,
            `arn:aws:glue:${adpRegion}:${adpAccount}:table/adp_${adpStage}_engineering/vehicle_build_components`,
            `arn:aws:glue:${adpRegion}:${adpAccount}:table/adp_${adpStage}_engineering/engineering_failure_modes`,
            `arn:aws:glue:${adpRegion}:${adpAccount}:table/adp_${adpStage}_engineering/engineering_remedy_options`,
            // ota_campaigns database + 2 named tables
            `arn:aws:glue:${adpRegion}:${adpAccount}:database/adp_${adpStage}_ota_campaigns`,
            `arn:aws:glue:${adpRegion}:${adpAccount}:table/adp_${adpStage}_ota_campaigns/ota_campaigns`,
            `arn:aws:glue:${adpRegion}:${adpAccount}:table/adp_${adpStage}_ota_campaigns/ota_campaign_events`,
          ],
        }),

        // FiHealthFindingsByFleetQuery: runner.py:2386 → selection.py:345-368
        // select_agent2_findings calls Query with IndexName "by_fleet".
        // Narrow: the by_fleet GSI ARN only (not the table, not any other index).
        new iam.PolicyStatement({
          sid: 'FiHealthFindingsByFleetQuery',
          effect: iam.Effect.ALLOW,
          actions: ['dynamodb:Query'],
          resources: [`${this.avxFindingsTable.tableArn}/index/by_fleet`],
        }),
      ],
    });

    // ── task 5.2 B: FI rebalancing Tier 2 IAM grants ──────────────────────
    //
    // One statement, grouped into its own ManagedPolicy for the same reason as
    // FiHealthTier2Policy: the aggregate inline policy on this role approaches
    // IAM's 10,240-byte per-role ceiling once ``cmsUserPoolId`` is supplied,
    // and new grants land here rather than inline so neither side can push the
    // other over.  Attaching via ``roles`` keeps the effect identical to
    // addToPolicy; the ManagedPolicy budget test
    // (vsa-tier2-stack-fi-rebalance.test.ts) asserts the aggregate stays under
    // the ceiling.
    //
    // The one Glue statement is scoped to the ``adp_{stage}_fleet_rebalance``
    // database and seven named tables that fi_rebalancing/sql/*.sql reads
    // directly.  No ``database/*`` wildcard — widening the resource list is
    // caught by test_glue_resources_are_named_tables_only (mutation M4).
    //
    // The agent also reads ``dms_{stage}.repair_orders`` and
    // ``adp_{stage}_service_records.*`` — but those are already granted
    // inline for the recall (DMS) and brakes (service_records) components, so
    // adding them here would be duplication.  The Athena workgroup grant and
    // ``LakeFormation:GetDataAccess`` are shared across components too, and
    // the CMS DynamoDB reads (fleet-locations, vehicle-groundings) are
    // covered by FiRwCmsFleetLocationsQuery and FiRwCmsGroundingsQuery.  See
    // the per-grant comment inline for which existing grant each read uses.
    new iam.ManagedPolicy(this, 'FiRebalanceTier2Policy', {
      roles: [lambdaRole],
      statements: [
        // FiRebalanceGlueMetadataRead: named tables only; no fleet_rebalance/*
        // wildcard.  The seven tables that fi_rebalancing/sql/*.sql reads
        // directly: booking_requests (busy_windows, request_lengths),
        // metro_events (events), fleet_bookings_forecast (forecast_days),
        // supply_demand_gaps_snapshot (gaps), move_outcomes_snapshot
        // (outcomes), vehicle_days (vehicle_days + vehicle_utilization),
        // vehicle_utilization_daily (vehicle_utilization).
        //
        // Mutation M4: widening any one resource (e.g. to
        // ``table/adp_${adpStage}_fleet_rebalance/*``) is caught by the
        // test_glue_resources_are_named_tables_only jest assertion.
        new iam.PolicyStatement({
          sid: 'FiRebalanceGlueMetadataRead',
          effect: iam.Effect.ALLOW,
          actions: ['glue:GetDatabase', 'glue:GetTable', 'glue:GetTables', 'glue:GetPartitions'],
          resources: [
            `arn:aws:glue:${adpRegion}:${adpAccount}:database/adp_${adpStage}_fleet_rebalance`,
            `arn:aws:glue:${adpRegion}:${adpAccount}:table/adp_${adpStage}_fleet_rebalance/booking_requests`,
            `arn:aws:glue:${adpRegion}:${adpAccount}:table/adp_${adpStage}_fleet_rebalance/metro_events`,
            `arn:aws:glue:${adpRegion}:${adpAccount}:table/adp_${adpStage}_fleet_rebalance/fleet_bookings_forecast`,
            `arn:aws:glue:${adpRegion}:${adpAccount}:table/adp_${adpStage}_fleet_rebalance/supply_demand_gaps_snapshot`,
            `arn:aws:glue:${adpRegion}:${adpAccount}:table/adp_${adpStage}_fleet_rebalance/move_outcomes_snapshot`,
            `arn:aws:glue:${adpRegion}:${adpAccount}:table/adp_${adpStage}_fleet_rebalance/vehicle_days`,
            `arn:aws:glue:${adpRegion}:${adpAccount}:table/adp_${adpStage}_fleet_rebalance/vehicle_utilization_daily`,
          ],
        }),
        // FiRebalancePolicyParameterRead: the one SSM parameter a rebalance run reads, its
        // U9 thresholds (agents/fi_rebalancing/policy.POLICY_PARAM), seeded from
        // deployment/config/avx-policy-defaults.json. Exactly this parameter, no wildcard.
        new iam.PolicyStatement({
          sid: 'FiRebalancePolicyParameterRead',
          effect: iam.Effect.ALLOW,
          actions: ['ssm:GetParameter'],
          resources: [
            `arn:aws:ssm:${region}:${account}:parameter/vsa/${stage}/avx/fi-rebalancing/policy`,
          ],
        }),
      ],
    });

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
        // | AVX_AGENT_RUNS_TABLE      | vsa-{stage}-avx-agent-runs                               |
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
        // Consumed by agents/tier2/owner_resolver.py for VIN -> vehicleId/fleetId
        // resolution, so the runner derives no name of its own.  This is the same
        // contract variable lambdas/shared/cms_client_real.py reads, so there is
        // one table-name contract across CVX rather than two.
        // IAM: CmsVehiclesVinIndexQuery (Query on vin-index) +
        //      CmsVehiclesOwnerGetItem (GetItem on the table) below.
        CMS_VEHICLES_TABLE:       `cms-${cmsStage}-storage-vehicles`,
        // owner_id = the vehicle's assigned driver (issue 2026-10-07). Read by
        // runner._assigned_driver_directory. IAM: CmsDriversAssignmentScan and
        // CmsUserPoolDriverLookup. An empty pool id turns owner resolution off.
        CMS_DRIVERS_TABLE:        `cms-${cmsStage}-storage-drivers`,
        CMS_USER_POOL_ID:         cmsUserPoolId ?? '',
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
        // FI Action Queue Stage A, spec D10: run records. runner.py fails closed
        // without it (no record, an ERROR log, and the run-staleness alarm fires).
        // IAM: AvxAgentRunsWrite.
        AVX_AGENT_RUNS_TABLE:     this.avxAgentRunsTable.tableName,
        DYNAMODB_REGION:          region,
        ADP_REGION:               adpRegion,
        ATHENA_WORKGROUP:         `cvx-${adpStage}-analytics`,
        // Fully resolved — account id and region are substituted at synth time.
        // The runner reads this value directly; it never reconstructs it from parts.
        ATHENA_RESULTS_BUCKET:    `cvx-${adpStage}-athena-results-${account}-${adpRegion}`,
        TIER2_MAX_TOKENS:         '50000',
        TIER2_MAX_WALL_SECONDS:   '600',
        // Per-component token caps (defect D3 fix, 2026-10-04 — resized in
        // review cycle 1 after the token-meter correction).
        //
        // The shared TIER2_MAX_TOKENS cap above is kept for legacy test paths
        // that call ``run(..., max_tokens=...)`` directly; production invocations
        // via the Lambda handler now resolve the token budget per component via
        // ``_component_max_tokens``, mirroring the per-component VIN caps below.
        //
        // Pre-fix review-cycle-1 figures (69,564 / 65,555 for a partial batch)
        // were inflated meter artifacts — the pre-C1 shape summed the lifetime-
        // cumulative counter on every call.  Measured per-call cost on the
        // stateless path: SYSTEM_PROMPT ~1,083 + user ~200 + reply ~150 ≈ 1,445
        // tokens.  Caps sized for the worst-case fleet composition after the
        // C5 needs-judgment filter and the C4 bypass:
        //   tires:   156 VINs × 1,500 × 1.5 ≈ 350,000
        //   battery: 375 VINs × 1,500 × 1.5 ≈ 850,000
        //   brakes:  2,000-VIN batch; selection only picks ``outcome IN
        //            ('parts_pending','follow_up_required')`` so every
        //            selected brake VIN is non-routine and the C5 needs-
        //            judgment filter removes none of them.  The 100-call
        //            circuit breaker (C5) therefore fires on every nightly
        //            brakes run.  Sizing: 100 LLM-called VINs × 1,500 ×
        //            2 ≈ 300,000, and the TIER2_MAX_LLM_CALLS_PER_BATCH
        //            circuit breaker also caps the LLM-call count at 100
        //            as a wall-clock safety.  Wall time, not tokens, is
        //            the binding dimension here; see
        //            TIER2_WALL_PER_VIN_WRITE_SECONDS below for the
        //            cycle-3 wall-time guard.
        // Operator should tune these as live-data active-brake rate becomes clear.
        TIER2_MAX_TOKENS_TIRES:   '350000',
        TIER2_MAX_TOKENS_BATTERY: '850000',
        TIER2_MAX_TOKENS_BRAKES:  '300000',
        // Per-batch LLM-call circuit breaker (C5 wall-clock safety).
        //
        // Wall time, not call count, binds a slow brakes batch.  The breaker
        // skips the LLM once remaining wall time drops below the per-call
        // safety margin PLUS the time it will take to run the writes for
        // every VIN still in the batch (see TIER2_WALL_PER_VIN_WRITE_SECONDS
        // below).  The call-count ceiling is a backstop — if latency drops
        // enough that the time-aware branch never fires, we still stop at
        // 100 model calls.
        TIER2_MAX_LLM_CALLS_PER_BATCH: '100',
        // W3 cycle-2 (2026-10-04): per-call safety margin.  Reserved for
        // the current model call itself (a slow outlier that happens to be
        // the one running when the breaker fires).  Above the observed
        // 4-6.6 s per-call latency with headroom for a slow outlier.
        TIER2_WALL_SAFETY_PER_CALL_SECONDS: '10',
        // W3 cycle-3 (2026-10-04): per-VIN WRITE cost reserved for the
        // rest of the batch after the breaker trips.  Each remaining VIN
        // still runs a legacy put + owner lookup + AVX write (~0.05-0.1 s
        // per VIN on staging, up from ~9 ms before the 2026-09-23 owner
        // + AVX dual-write).  Without this reservation a 10 s per-call
        // margin can leave ~1,900 × 0.05 = ~95 s of writes to do inside
        // a 10 s budget, which is what cycle 3 observed at 6.6 s/call on
        // the shared-fake-clock simulation.  0.1 is above the measured
        // ~0.05-0.096 s with headroom.
        TIER2_WALL_PER_VIN_WRITE_SECONDS: '0.1',
        // Per-component VIN caps (spec § D4, amended 2026-08-10).
        // Rows-per-VIN varies 224× across products; a single global cap sized
        // for the worst component (tires at 156) would limit brakes to 156/night
        // when brakes can safely process 2,000 within the same row ceiling.
        // Binding constraints per Group 1 measurements (2026-08-10):
        //   tires  → 156  rows-bound: 156 × 240 = 37,440 rows  (25.1% headroom under 50k)
        //   battery→ 600  one row per VIN since the 2026-10-04 dedup (<= 600 rows); raised from
        //                  375 by stage D 6.3 (decisions.md 2026-10-05 item 1, user 2026-10-12 (b))
        //                  so the managed pool is covered in 7 nights; wall time measured in 6.3
        //   brakes → 2000 write-path-bound: ~2,140 rows; bounded by sequential
        //                  conditional PutItem + agent_fn calls within 300s target
        TIER2_MAX_VINS_TIRES:     '156',
        TIER2_MAX_VINS_BATTERY:   '600',
        TIER2_MAX_VINS_BRAKES:    '2000',
        // Athena result-page ceiling.  Exceeding this raises AthenaResultTooLargeError
        // rather than returning a silently-truncated set (T6a.1).  Sized above the
        // 3-VIN test case (721 rows) with headroom for the default 25-VIN cap.
        TIER2_MAX_ROWS_PER_QUERY: '50000',
        // FI cost component (spec 2026-10-02-fi-cost-stage-b task 4.2): the Guardrail
        // agents/fi_cost/agent.py applies to the model's headline and detail. DRAFT so a
        // deployed guardrail change applies without cutting a version. runner.py fails the
        // cost run at startup when either is unset (event=fi_cost_guardrail_unset).
        FI_COST_GUARDRAIL_ID:      fiCostGuardrail.attrGuardrailId,
        FI_COST_GUARDRAIL_VERSION: 'DRAFT',
        // FI recall/warranty component (spec 2026-10-05-fi-recall-warranty-stage-c T3.5):
        // CMS tables for vehicle-location and grounding reads (G3-10), and the AVX
        // actions table for attempt history (G3-12). All fully resolved at synth time.
        CMS_FLEET_LOCATIONS_TABLE:    `cms-${cmsStage}-storage-fleet-locations`,
        CMS_VEHICLE_GROUNDINGS_TABLE: `cms-${cmsStage}-storage-vehicle-groundings`,
        AVX_ACTIONS_TABLE:            this.avxActionsTable.tableName,
        // S4 model wiring: caps, model id (S5 full stack pass finalises these defaults).
        FI_RECALL_MAX_MODEL_CALLS:    '60',
        FI_WARRANTY_MAX_MODEL_CALLS:  '30',
        FI_RW_MODEL_ID:               claudeProfileId,
        // task 5.2: FI rebalancing env contract (spec D8, U9's 12-model-call cap).
        // The agent reuses Stage B's guardrail (``FI_COST_GUARDRAIL_ID`` /
        // ``FI_COST_GUARDRAIL_VERSION``) per D8 step 7's parenthetical, so no
        // dedicated guardrail env var is needed.  ``FI_REBAL_MODEL_ID`` is the
        // same cross-region Claude Sonnet 4.6 profile the Tier 2 role already
        // holds (shared with FI-RW / FI-cost / fleet-health).
        FI_REBAL_MAX_MODEL_CALLS:     '12',
        FI_REBAL_MODEL_ID:            claudeProfileId,
        // task 4.3 B2: FI fleet-health env contract (task43-notes.md item 4).
        // All names are out of _NOT_YET_IN_STACK in test_runner.py after this change.
        AVX_INVESTIGATIONS_TABLE:         avxInvestigationsTable.tableName,
        SQS_MAX_RECEIVE_COUNT:            '2',
        FAULT_GROUNDING_ENABLED:          faultGroundingLive ? 'true' : 'false',
        TIER2_MAX_VINS_SERVICE:           '500',
        TIER2_MAX_VINS_INVESTIGATION:     '0',
        TIER2_MAX_TOKENS_SERVICE:         '1125000',
        TIER2_MAX_TOKENS_INVESTIGATION:   '100000',
        FI_HEALTH_MAX_MODEL_CALLS:        '60',
        // Both model switches derive from fiHealthModelLive so changing the constant
        // or the prop flips both at once. Parse: 'true'/'false'; absent → false (D5).
        FI_SERVICE_MODEL_ENABLED:         fiHealthModelLive ? 'true' : 'false',
        FI_INVESTIGATION_MODEL_ENABLED:   fiHealthModelLive ? 'true' : 'false',
      },
    });

    // ── task 4.3 B1: investigations SQS event source ─────────────────────
    //
    // investigation is event-triggered, not scheduled (task43-notes.md item 2).
    // batchSize 1: one investigation per Lambda invocation (D10 reserved concurrency 2
    //   applies only to this path; the whole function is not reserved).
    // reportBatchItemFailures: on a deadline miss the message is a batch item failure
    //   so SQS retries without needing the full visibility-timeout to expire.
    // maxConcurrency 2: matches D10 "reserved concurrency 2" for the investigation path.
    // enabled: gated on fiHealthLive (true since Group 6 rest-plan step 7).
    healthAgentFn.addEventSource(new lambdaEventSources.SqsEventSource(
      avxInvestigationsQueue,
      {
        batchSize:              1,
        reportBatchItemFailures: true,
        maxConcurrency:         2,
        enabled:                fiHealthLive,
      },
    ));
    // Security g43 c1 S-c (carried to the live flip): the mapping can't start delivering
    // investigations before the managed policy that holds the job's grants exists. The
    // mapping is the function's only SqsEventSource child.
    const investigationsMappings = healthAgentFn.node.children.filter(
      (c) => c instanceof lambda.EventSourceMapping && c.node.id.startsWith('SqsEventSource:'));
    if (investigationsMappings.length !== 1) {
      throw new Error(`expected one SQS event source mapping on the health agent, found ${investigationsMappings.length}`);
    }
    investigationsMappings[0].node.addDependency(fiHealthTier2Policy);

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
    const componentSchedules: Array<{ component: string; hour: string; minute: string; enabled: boolean }> = [
      { component: 'battery', hour: '2', minute: '0',  enabled: true },
      { component: 'tires',   hour: '2', minute: '20', enabled: true },
      { component: 'brakes',  hour: '2', minute: '40', enabled: true },
      // FI cost agent (spec 2026-10-02-fi-cost-stage-b D6): after the health runs.
      // Cost data changes monthly, so most daily runs skip every vehicle unchanged.
      { component: 'cost',    hour: '4', minute: '0',  enabled: true },
      // task 4.3: recall and warranty added here; enabled matches fiRwLive so both
      // schedules and their staleness alarms become active together after task 5.4
      // (decisions.md call 55, after ADP task 5.1 ships the fleet_recall database).
      { component: 'recall',  hour: '5', minute: '0',  enabled: fiRwLive },
      { component: 'warranty', hour: '5', minute: '20', enabled: fiRwLive },
      // task 4.3 B2: FI fleet-health service run. investigation is event-triggered
      // (SQS), not scheduled, so it is absent here (task43-notes.md item 2).
      // enabled: gated on fiHealthLive (true since Group 6 rest-plan step 7).
      { component: 'service', hour: '5', minute: '40', enabled: fiHealthLive },
      // task 5.2: FI rebalancing agent. 06:00 UTC after bookings (04:15) and
      // forecast (04:45), ADP's materialise step and cost (04:00), so the agent
      // reads already-settled daily views (spec D8, U9's 06:00 UTC decision).
      // Ships DISABLED (fiRebalLive = false) until Group 6's deploy flips the
      // FI_REBALANCING_LIVE constant; also removed from
      // ``_NOT_YET_SCHEDULED`` in agents/tier2/tests/test_run_staleness_alarm_pinned.py
      // in the SAME change (task 5.2 carried item 1).
      { component: 'rebalance', hour: '6', minute: '0', enabled: fiRebalLive },
    ];

    for (const { component, hour, minute, enabled } of componentSchedules) {
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
      const componentSchedule = new scheduler.Schedule(this, `Tier2Daily${capitalised}Schedule`, {
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
        // task 4.3: enabled comes from the componentSchedules entry (existing components:
        // true; recall/warranty: fiRwLive, flipped by task 5.4 after ADP task 5.1).
        enabled,
      });
      // Security g43 c1 S-c: the service run needs FiHealthTier2Policy's grants.
      if (component === 'service') componentSchedule.node.addDependency(fiHealthTier2Policy);
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

    // ── FI Action Queue Stage A, spec D10: run-staleness alarms ────────────
    //
    // Watches the `RunRecordsWritten` heartbeat that runner.py emits only after a run
    // record is stored (observability.emit_run_record_heartbeat). A run that did not
    // happen, crashed before its record, or failed or skipped the write all look the
    // same here: no heartbeat. That is exactly when CMS shows a stale "last ran".
    //
    // One alarm per component, dimensioned {stage, component} to match the emitter
    // (an alarm whose dimensions differ watches an empty series; see D5 above).
    //
    // Why FILL(m1, 0) at a 1-hour period (Group 3 review Cycle 10 W1): the heartbeat is
    // only ever 1, so nothing real breaches and the alarm rests on missing data. With a
    // plain metric, CloudWatch reads an evaluation range wider than the alarm's window
    // and, when it finds any real datapoint there, ignores the missing-data setting
    // (docs: "Configuring how CloudWatch alarms treat missing data"), so yesterday's
    // heartbeat would hide today's missed run. FILL makes every hour a datapoint (0 when
    // empty), so the last N hours are evaluated exactly. The alarm fires only when all N
    // are empty: 27 hours for the daily components, 2 hours for diagnostics (every 5
    // minutes). 27 covers the worst gap between two good daily runs: the 24-hour cadence,
    // the scheduler's 1-hour retry window, and Lambda's own two async retries of a
    // 15-minute run (about 25 h 48 m), so a late but successful run never flaps it.
    // BREACHING still covers the expression returning nothing at all.
    //
    // On first deploy every staleness alarm goes to ALARM until that component's first
    // stored record (within minutes for diagnostics, the next 02:xx UTC run for the
    // daily ones), as the no-vins alarms did; the OK action makes the recovery visible.
    const runStalenessWindows: Array<{ component: string; emptyHours: number; actionsEnabled: boolean }> = [
      ...alarmComponents.map((component) => ({ component, emptyHours: 27, actionsEnabled: true })),
      { component: 'diagnostics', emptyHours: 2, actionsEnabled: true },
      // FI cost agent: daily like the health components, so the same 27-hour window
      // (contract § 8). It is not in alarmComponents: zero anomalies selected and
      // three days without a new Finding are both normal for monthly cost data.
      { component: 'cost', emptyHours: 27, actionsEnabled: true },
      // task 4.3: recall and warranty schedules and alarms added together.
      // actionsEnabled matches fiRwLive: both become actionable after task 5.4
      // (decisions.md call 55, after ADP task 5.1 ships the fleet_recall database).
      { component: 'recall',   emptyHours: 27, actionsEnabled: fiRwLive },
      { component: 'warranty', emptyHours: 27, actionsEnabled: fiRwLive },
      // task 4.3 B2: FI fleet-health service. investigation is event-triggered;
      // see comment below for the three targeted alarms it gets instead.
      { component: 'service',  emptyHours: 27, actionsEnabled: fiHealthLive },
      // task 5.2: rebalance alarm. Same 27-hour daily window.  actionsEnabled
      // matches fiRebalLive — the alarm EXISTS so the first missed run after
      // Group 6's deploy produces a signal, but AlarmActions stay off until
      // then.  Removed from _NOT_YET_SCHEDULED in the same change.
      { component: 'rebalance', emptyHours: 27, actionsEnabled: fiRebalLive },
    ];
    const runStalenessAlarms: cloudwatch.Alarm[] = [];
    for (const { component, emptyHours, actionsEnabled } of runStalenessWindows) {
      const capitalised = component.charAt(0).toUpperCase() + component.slice(1);
      const heartbeats = new cloudwatch.MathExpression({
        expression: 'FILL(m1, 0)',
        usingMetrics: {
          m1: new cloudwatch.Metric({
            namespace:     'CVX/Tier2',
            metricName:    'RunRecordsWritten',
            dimensionsMap: { stage, component },
            period:        cdk.Duration.hours(1),
            statistic:     'Sum',
          }),
        },
        period: cdk.Duration.hours(1),
        label:  `RunRecordsWritten ${component} (missing hours as 0)`,
      });
      const alarm = new cloudwatch.Alarm(this, `AvxRunStalenessAlarm${capitalised}`, {
        alarmName:   `vsa-${stage}-avx-run-stale-${component}-${region}`,
        alarmDescription:
          `Tier 2 (${component}): no run record stored in vsa-${stage}-avx-agent-runs for ` +
          `${emptyHours} hours. CMS now shows this agent's last run as stale. Check the ` +
          'Lambda logs for event=run_record_table_unset or run_record_write_failed, ' +
          'and the schedule.',
        metric:             heartbeats,
        threshold:          1,
        evaluationPeriods:  emptyHours,
        datapointsToAlarm:  emptyHours,
        comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
        treatMissingData:   cloudwatch.TreatMissingData.BREACHING,
        // task 4.3: actionsEnabled is false for recall/warranty until fiRwLive is true;
        // the alarm is created (so the series is watched) but is silenced until task 5.4.
        actionsEnabled,
      });
      alarm.addAlarmAction(topicAction);
      alarm.addOkAction(topicAction);
      runStalenessAlarms.push(alarm);
    }

    // ── task 4.3 B1: AVX/Investigations alarms ───────────────────────────
    //
    // investigation is event-triggered: a run-staleness alarm would page
    // whenever nobody raises a new investigation for a day, so none is added.
    // Instead, three targeted alarms fire on actual failures (task43-notes.md item 2).
    //
    // All three: ActionsEnabled=true (they fire only on a real failure),
    // treatMissingData NOT_BREACHING (absence of the metric = no event, not a gap).
    //
    // 1. DLQ depth: a message reaching the DLQ means 2 consecutive Lambda failures
    //    for that investigation; operator action is required.
    new cloudwatch.Alarm(this, 'AvxInvestigationsDlqDepthAlarm', {
      alarmName:  `vsa-${stage}-avx-investigations-dlq-depth-${region}`,
      alarmDescription:
        'AVX investigations DLQ: ≥1 visible message. ' +
        'An investigation failed its two SQS delivery attempts. ' +
        'Check CloudWatch Logs for the investigation_id and set its status manually.',
      metric: new cloudwatch.Metric({
        namespace:     'AWS/SQS',
        metricName:    'ApproximateNumberOfMessagesVisible',
        dimensionsMap: { QueueName: avxInvestigationsDlq.queueName },
        period:        cdk.Duration.minutes(5),
        statistic:     'Maximum',
      }),
      threshold:          1,
      evaluationPeriods:  1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData:   cloudwatch.TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(topicAction);

    // 2. InvestigationMarkFailedError: the POST /investigations route failed to
    //    mark the investigation as failed after an SQS send error — the row stays
    //    in a non-terminal state and manual recovery is required.
    //    No dimensions: lambdas/api-avx-investigations/handler.py emits this metric
    //    without dimensions.
    new cloudwatch.Alarm(this, 'AvxInvestigationMarkFailedAlarm', {
      alarmName:  `vsa-${stage}-avx-investigations-mark-failed-error-${region}`,
      alarmDescription:
        'AVX/Investigations InvestigationMarkFailedError ≥1. ' +
        'POST /investigations failed to mark the row as failed after an SQS error; ' +
        'the row is stuck in a non-terminal state. Manual DynamoDB update required.',
      metric: new cloudwatch.Metric({
        namespace:     'AVX/Investigations',
        metricName:    'InvestigationMarkFailedError',
        // No dimensions — emitted without dimensions by handler.py.
        period:        cdk.Duration.minutes(5),
        statistic:     'Sum',
      }),
      threshold:          1,
      evaluationPeriods:  1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData:   cloudwatch.TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(topicAction);

    // 3. QualityIssueHookFailure: the findings Lambda's issue hook failed to write
    //    the quality.issue Finding — the investigation completed but its Finding was lost.
    //    No dimensions — emitted without dimensions by agents/fi_quality/issue.py.
    new cloudwatch.Alarm(this, 'AvxQualityIssueHookFailureAlarm', {
      alarmName:  `vsa-${stage}-avx-investigations-hook-failure-${region}`,
      alarmDescription:
        'AVX/Investigations QualityIssueHookFailure ≥1. ' +
        'The quality.issue hook in the findings Lambda failed to write a Finding. ' +
        'The investigation completed but its Finding was not created. ' +
        'Check CloudWatch Logs for the investigation_id.',
      metric: new cloudwatch.Metric({
        namespace:     'AVX/Investigations',
        metricName:    'QualityIssueHookFailure',
        // No dimensions — emitted without dimensions by agents/fi_quality/issue.py.
        period:        cdk.Duration.minutes(5),
        statistic:     'Sum',
      }),
      threshold:          1,
      evaluationPeriods:  1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData:   cloudwatch.TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(topicAction);

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

    // DynamoDB Streams access for avx-findings stream (event source):
    // no explicit statement here — the DynamoEventSource attached to
    // avxBrokerFn below (its single addEventSource call) auto-grants
    // GetRecords/GetShardIterator/DescribeStream on the exact stream ARN
    // (Fn::GetAtt: [..., "StreamArn"]) and ListStreams on "*".
    // Removing BrokerAvxFindingsStreamRead (T1.5) so the only stream-read
    // grant is the one CDK wires to the exact stream ARN, not to stream/*.

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

    // DynamoDB: the p0 notification ledger read (issue 2026-10-23-tier2-writer-
    // frozen-severity). Ledger rows share the rate-limits table (sk "p0:<finding_id>");
    // the broker reads one strongly consistently before an escalation push. Claims
    // and outcomes are UpdateItem, covered by the statement above. IAM cannot scope
    // a sort-key prefix, so the read covers the table; the counters it can also read
    // are the broker's own.
    brokerRole.addToPolicy(new iam.PolicyStatement({
      sid: 'BrokerAvxP0LedgerGetItem',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:GetItem'],
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
        // T1.3b: narrowed to exactly the one AVX APNs platform application.
        // CreatePlatformEndpoint mints device endpoint ARNs under this prefix; the
        // broker must not publish to any other application's endpoints.
        `arn:aws:sns:${region}:${account}:endpoint/APNS_SANDBOX/vsa-${stage}-avx-apns-sandbox/*`,
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
                // boto3 plus its transitive set, all exact (FI Action Queue Stage A,
                // Group 4): unpinned, the 2026-10-03 deploy took botocore 1.43.108,
                // uploaded the day before, inside the 7-day quarantine. These are the
                // versions live in this Lambda from 2026-09-28 until then.
                'pip install boto3==1.43.6 botocore==1.43.103 s3transfer==0.17.1',
                '  jmespath==1.1.0 python-dateutil==2.9.0.post0 six==1.17.0 urllib3==2.8.0',
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
    // Sorted: adp, chat, cms, dms, ios-owner, none, retail, vehicle-command
    // `cms` added by FI Action Queue Stage A (CmsExecutor; spec D4).
    // `adp` added by FI Fleet Health Stage D (AdpExecutor; spec registry v2.7).
    const brokerTargetSystems: string[] = [
      'adp',
      'chat',
      'cms',
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

    // Lost p0 push alarm (issue 2026-10-23-lost-p0-push-not-alarmed). The broker
    // writes one EMF count of AVX/Broker PushNotDelivered {Stage, Severity} for every
    // push that was due and did not happen (a publish/device failure, or a due push
    // blocked by the rate limit or cardinality cap). A p0 is the stop-driving class,
    // so its series pages the operator topic. EMF needs no IAM grant on the broker.
    // NOT_BREACHING: no data means no lost pushes. Name check:
    // "vsa-staging-avx-broker-p0-push-lost-ap-southeast-7" = 50 chars.
    // Pinned (CI runs Python, not jest) by
    // agents/supervisor/lambdas/tests/test_p0_push_lost_alarm_pinned.py.
    const brokerP0PushLostAlarm = new cloudwatch.Alarm(this, 'AvxBrokerP0PushLostAlarm', {
      alarmName:   `vsa-${stage}-avx-broker-p0-push-lost-${region}`,
      alarmDescription:
        `AVX broker could not deliver a p0 (stop-driving) push. The EMF line's ` +
        `reason and finding_id properties name the outcome; search /aws/lambda/` +
        `vsa-${stage}-avx-broker-${region} for push_not_delivered. The push does not ` +
        `retry by itself: only a write that changes the Finding (or a new Finding ` +
        `after it expires) tries again. The Finding card in the app already shows ` +
        `p0; reach the driver another way if the outage persists.`,
      metric: new cloudwatch.Metric({
        namespace:     'AVX/Broker',
        metricName:    'PushNotDelivered',
        dimensionsMap: { Stage: stage, Severity: 'p0' },
        period:        cdk.Duration.minutes(5),
        statistic:     'Sum',
      }),
      threshold:          1,
      evaluationPeriods:  1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData:   cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    brokerP0PushLostAlarm.addAlarmAction(new cloudwatchActions.SnsAction(operatorAlertTopic));

    // ── FI Action Queue Stage A, spec D9: the auto-approval evaluator ──────
    //
    // Second consumer of the avx-findings stream (the broker is the first). For a
    // Finding with a recommended_action and a fleet, it loads the fleet's enabled
    // rules, writes the Finding's one decision Action (deterministic action_id plus a
    // conditional put, so a replayed record or a race with an operator adds nothing)
    // and dispatches it. Stage A can auto-approve only token-free kinds
    // (acknowledge_only), whose AdvisoryExecutor makes no AWS call, so the role has
    // no events:PutEvents or cloudwatch:PutMetricData: an unexpected escalation fails
    // closed and is logged. (Stage E Group 8 adds both, and the CMS call, only when
    // FI_REBALANCING_AUTO_APPROVAL_LIVE is on; see below.)
    //
    // Asset: scripts/build-avx-lambda.sh -> infrastructure/build/avx-auto-approver/
    // (handler + agents/executors + policy + run_records + its requirements.txt).
    //
    // Name check: "vsa-staging-avx-auto-approver-ap-southeast-7" = 44 chars <= 64
    // (IAM role names are account-wide, so the region suffix is required).
    const autoApproverName = `vsa-${stage}-avx-auto-approver-${region}`;
    const autoApproverRole = new iam.Role(this, 'AvxAutoApproverRole', {
      roleName: autoApproverName,
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: `AVX auto-approval evaluator - findings stream consumer (${stage}, ${region})`,
    });
    autoApproverRole.addToPolicy(new iam.PolicyStatement({
      sid: 'AutoApproverCloudWatchLogs',
      effect: iam.Effect.ALLOW,
      actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
      resources: [
        `arn:aws:logs:${region}:${account}:log-group:/aws/lambda/${autoApproverName}`,
        `arn:aws:logs:${region}:${account}:log-group:/aws/lambda/${autoApproverName}:*`,
      ],
    }));
    // Stream read: no explicit statement. The DynamoEventSource below grants
    // DescribeStream/GetRecords/GetShardIterator on the findings table's exact StreamArn
    // (plus ListStreams, which takes no resource), which is narrower than `/stream/*`
    // (Group 3 review Cycle 10 S1).
    // handler.py::_load_rules: one Query on the fleet's partition. Table ARN only.
    autoApproverRole.addToPolicy(new iam.PolicyStatement({
      sid: 'AutoApproverRulesQuery',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:Query'],
      resources: [this.avxApprovalRulesTable.tableArn],
    }));
    // The decision Action: conditional put_item, then update_item with the dispatch
    // result. No GetItem: the condition does the existence check.
    autoApproverRole.addToPolicy(new iam.PolicyStatement({
      sid: 'AutoApproverActionsWrite',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:PutItem', 'dynamodb:UpdateItem'],
      resources: [this.avxActionsTable.tableArn],
    }));
    // One run record per batch (agents/tier2/run_records.py, put_item only).
    autoApproverRole.addToPolicy(new iam.PolicyStatement({
      sid: 'AutoApproverAgentRunsWrite',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:PutItem'],
      resources: [this.avxAgentRunsTable.tableArn],
    }));

    // Stage E Group 8 (U6): rule-approved moves, behind FI_REBALANCING_AUTO_APPROVAL_LIVE
    // (group8-notes G8-D7..D9). Off: none of this, so the evaluator never considers
    // move_vehicles and the role can't call CMS. On: the switch, the bounds read, CMS's
    // internal endpoint, execute-api on exactly that route of exactly the API and stage
    // the endpoint names (both taken from it at deploy time; CMS's resource policy is the
    // other half of the gate), and the two escalation calls a failed dispatch makes (as
    // the findings Lambda's). Not
    // granted, as for the findings Lambda: the executor's per-target retry and escalation
    // parameters, whose reads fall back to the defaults (base.py _read_json_param). A
    // move Action carries a decision, so escalation skips the owner card and needs no
    // owner-feed grants.
    const autoMovesLive = props.fiRebalancingAutoApprovalLive ?? FI_REBALANCING_AUTO_APPROVAL_LIVE;
    // One batch size for the findings stream and the timeout (review c2 W2): live, a batch
    // may make this many signed dispatches, each up to the executor's budget
    // (agents/executors/base.py SYNCHRONOUS_BUDGET_S), plus 50 s for the rest.
    const autoApproverBatchSize = 10;
    const autoApproverDispatchBudgetS = 25;
    const autoMovesEnv: Record<string, string> = {};
    if (autoMovesLive) {
      if (!props.cmsFiInternalApiEndpoint) {
        throw new Error(
          'VsaTier2Stack: cmsFiInternalApiEndpoint is required when rule-approved moves ' +
          'are live (FI_REBALANCING_AUTO_APPROVAL_LIVE); seed /cvx/<stage>/cms-fi-internal-api-endpoint.',
        );
      }
      const boundsParam = `/vsa/${stage}/avx/fi-rebalancing/auto-approval-bounds`;
      autoMovesEnv.FI_AUTO_MOVES_ENABLED = 'true';
      autoMovesEnv.AVX_AUTO_APPROVAL_BOUNDS_PARAM = boundsParam;
      const cmsInternalEndpoint = resolveConfigValue(
        this, 'cmsFiInternalApiEndpoint', props.cmsFiInternalApiEndpoint,
      );
      // https://<api-id>.execute-api.<region>.amazonaws.com/<stage>/ (CMS's
      // FiInternalApiEndpoint output): split on '/', element 2 is the host and 3 the stage;
      // the host's first label is the API id. A literal (env override) of any other shape
      // fails synth here (review c2 W1): without its stage it would deploy a grant that
      // matches nothing. Synth can't check a value from SSM (a deploy fails only on the
      // host-only form, where Fn::Select runs out of range); scripts/seed-stage-parameters.sh
      // refuses the same shapes, and the executor refuses them at dispatch
      // (agents/executors/cms.py _cms_internal_endpoint).
      if (!cdk.Token.isUnresolved(cmsInternalEndpoint)
          && !/^https:\/\/[a-z0-9]{1,32}\.execute-api\.[a-z]{2}(?:-[a-z]+)+-\d\.amazonaws\.com\/[A-Za-z0-9_-]+\/?$/
            .test(cmsInternalEndpoint)) {
        throw new Error(
          'VsaTier2Stack: cmsFiInternalApiEndpoint must be ' +
          'https://<api-id>.execute-api.<region>.amazonaws.com/<stage>/ (the ' +
          'FiInternalApiEndpoint output of cms-<stage>-ui, with its stage).',
        );
      }
      autoMovesEnv.VSA_CMS_FI_INTERNAL_API_ENDPOINT = cmsInternalEndpoint;
      autoMovesEnv.VSA_STAGE = stage;
      const endpointParts = cdk.Fn.split('/', cmsInternalEndpoint);
      const cmsInternalApiId = cdk.Fn.select(0, cdk.Fn.split('.', cdk.Fn.select(2, endpointParts)));
      const cmsInternalStage = cdk.Fn.select(3, endpointParts);
      autoApproverRole.addToPolicy(new iam.PolicyStatement({
        sid: 'AutoApproverCmsInternalMovesInvoke',
        effect: iam.Effect.ALLOW,
        actions: ['execute-api:Invoke'],
        resources: [
          `arn:aws:execute-api:${region}:${account}:${cmsInternalApiId}/${cmsInternalStage}/POST/api/v1/fleet-intelligence/internal/moves`,
        ],
      }));
      autoApproverRole.addToPolicy(new iam.PolicyStatement({
        sid: 'AutoApproverBoundsRead',
        effect: iam.Effect.ALLOW,
        actions: ['ssm:GetParameter'],
        resources: [`arn:aws:ssm:${region}:${account}:parameter${boundsParam}`],
      }));
      autoApproverRole.addToPolicy(new iam.PolicyStatement({
        sid: 'AutoApproverEscalationPutEvents',
        effect: iam.Effect.ALLOW,
        actions: ['events:PutEvents'],
        resources: [`arn:aws:events:${region}:${account}:event-bus/default`],
      }));
      autoApproverRole.addToPolicy(new iam.PolicyStatement({
        sid: 'AutoApproverEscalationPutMetric',
        effect: iam.Effect.ALLOW,
        actions: ['cloudwatch:PutMetricData'],
        resources: ['*'],
        conditions: { StringEquals: { 'cloudwatch:namespace': 'AVX/Executor' } },
      }));
    }

    // Stage G8 D7 (task 11.7): rule-approved rebooking, behind FI_AUTO_REBOOK_LIVE.
    // Off: none of this, so the evaluator never considers rule-decided book_fleet_service and
    // the role can't call DMS's internal repair-orders route. On: the switch, the DMS internal
    // endpoint, env vars (findings and groundings tables, the hold-list parameter), execute-api
    // on exactly that route, findings/groundings Query grants, a GetItem grant for the actions
    // table, and GetParameter on the simulated fleet operator's parameter (its hold list).
    //
    // Note: AutoApproverActionsWrite grants PutItem + UpdateItem only; _rebook_qualifies(d)
    // needs GetItem (prev action lookup), so AutoApproverActionsGetItem is always a separate
    // statement when this block is on.
    const autoRebookLive = props.fiAutoRebookLive ?? FI_AUTO_REBOOK_LIVE;
    const autoRebookEnv: Record<string, string> = {};
    if (autoRebookLive) {
      if (!props.dmsFiInternalApiEndpoint) {
        throw new Error(
          'VsaTier2Stack: dmsFiInternalApiEndpoint is required when rule-approved rebooking ' +
          'is live (FI_AUTO_REBOOK_LIVE); seed /cvx/<stage>/dms-fi-internal-api-endpoint.',
        );
      }
      const dmsEndpointRebook = resolveConfigValue(
        this, 'dmsFiInternalApiEndpointRebook', props.dmsFiInternalApiEndpoint,
      );
      if (!cdk.Token.isUnresolved(dmsEndpointRebook)
          && !/^https:\/\/[a-z0-9]{1,32}\.execute-api\.[a-z]{2}(?:-[a-z]+)+-\d\.amazonaws\.com\/[A-Za-z0-9_-]+\/?$/
            .test(dmsEndpointRebook)) {
        throw new Error(
          'VsaTier2Stack: dmsFiInternalApiEndpoint must be ' +
          'https://<api-id>.execute-api.<region>.amazonaws.com/<stage>/ ' +
          '(the DmsInternalApiEndpoint output with its stage).',
        );
      }
      autoRebookEnv.FI_AUTO_REBOOK_ENABLED = 'true';
      autoRebookEnv.VSA_DMS_INTERNAL_API_ENDPOINT = dmsEndpointRebook;
      autoRebookEnv.AVX_FINDINGS_TABLE = this.avxFindingsTable.tableName;
      autoRebookEnv.CMS_GROUNDINGS_TABLE = `cms-${stage}-storage-vehicle-groundings`;
      // The executors build their policy parameter paths from VSA_STAGE (base.py _stage(),
      // default "staging"); set it here too so the paths match the grant below when the
      // moves gate, which also sets it, is off (prompt 361).
      autoRebookEnv.VSA_STAGE = stage;
      // Predicate (g), prompt 347: a rebooking never books a car on the simulated fleet
      // operator's hold list; the handler reads that parameter and fails closed without it.
      const simOperatorParamName = `/cvx/${stage}/fi/sim-operator`;
      autoRebookEnv.FI_SIM_OPERATOR_PARAM = simOperatorParamName;
      autoApproverRole.addToPolicy(new iam.PolicyStatement({
        sid: 'AutoApproverSimOperatorHoldsRead',
        effect: iam.Effect.ALLOW,
        actions: ['ssm:GetParameter'],
        resources: [`arn:aws:ssm:${region}:${account}:parameter${simOperatorParamName}`],
      }));
      // The rebook's DMS dispatch reads the DMS executor's two policy parameters
      // (agents/executors/base.py retry_policy_param / escalation_param, target "dms"):
      // the retry policy on every dispatch, the escalation config when a dispatch
      // escalates. Without this the read was AccessDenied and fell back to the built-in
      // default (2026-11-09 05:41Z, the first rule rebook; prompt 361). Exactly those two.
      // Both are String parameters and the executor reads them without decryption, so no
      // KMS grant goes with this; a SecureString reseed would fall back to the default.
      autoApproverRole.addToPolicy(new iam.PolicyStatement({
        sid: 'AutoApproverDmsExecutorPolicyRead',
        effect: iam.Effect.ALLOW,
        actions: ['ssm:GetParameter'],
        resources: [
          `arn:aws:ssm:${region}:${account}:parameter/vsa/${stage}/avx/executor/dms/retry-policy`,
          `arn:aws:ssm:${region}:${account}:parameter/vsa/${stage}/avx/executor/dms/escalation`,
        ],
      }));
      const dmsPartsRebook = cdk.Fn.split('/', dmsEndpointRebook);
      const dmsApiIdRebook = cdk.Fn.select(0, cdk.Fn.split('.', cdk.Fn.select(2, dmsPartsRebook)));
      const dmsStageRebook = cdk.Fn.select(3, dmsPartsRebook);
      autoApproverRole.addToPolicy(new iam.PolicyStatement({
        sid: 'AutoApproverDmsRebookInvoke',
        effect: iam.Effect.ALLOW,
        actions: ['execute-api:Invoke'],
        resources: [
          `arn:aws:execute-api:${region}:${account}:${dmsApiIdRebook}/${dmsStageRebook}/POST/api/dms/internal/fleet/repair-orders`,
        ],
      }));
      autoApproverRole.addToPolicy(new iam.PolicyStatement({
        sid: 'AutoApproverFindingsByVinQuery',
        effect: iam.Effect.ALLOW,
        actions: ['dynamodb:Query'],
        resources: [`${this.avxFindingsTable.tableArn}/index/by_vin`],
      }));
      const cmsGroundingsRebookArn =
        `arn:aws:dynamodb:${cmsRegion}:${cmsAccount}:table/cms-${cmsStage}-storage-vehicle-groundings`;
      autoApproverRole.addToPolicy(new iam.PolicyStatement({
        sid: 'AutoApproverGroundingsRebookQuery',
        effect: iam.Effect.ALLOW,
        actions: ['dynamodb:Query'],
        resources: [`${cmsGroundingsRebookArn}/index/open-groundings-index`],
      }));
      // AutoApproverActionsWrite grants PutItem + UpdateItem only; GetItem is needed by
      // _rebook_qualifies (step d: prev-action lookup), so add it separately here.
      autoApproverRole.addToPolicy(new iam.PolicyStatement({
        sid: 'AutoApproverActionsGetItem',
        effect: iam.Effect.ALLOW,
        actions: ['dynamodb:GetItem'],
        resources: [this.avxActionsTable.tableArn],
      }));
      // Escalation grants: reuse the moves block's if live there, else add both.
      if (!autoMovesLive) {
        autoApproverRole.addToPolicy(new iam.PolicyStatement({
          sid: 'AutoApproverEscalationPutEventsRebook',
          effect: iam.Effect.ALLOW,
          actions: ['events:PutEvents'],
          resources: [`arn:aws:events:${region}:${account}:event-bus/default`],
        }));
        autoApproverRole.addToPolicy(new iam.PolicyStatement({
          sid: 'AutoApproverEscalationPutMetricRebook',
          effect: iam.Effect.ALLOW,
          actions: ['cloudwatch:PutMetricData'],
          resources: ['*'],
          conditions: { StringEquals: { 'cloudwatch:namespace': 'AVX/Executor' } },
        }));
      }
    }

    const autoApproverLogGroup = new logs.LogGroup(this, 'AvxAutoApproverLogs', {
      logGroupName: `/aws/lambda/${autoApproverName}`,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    const autoApproverFn = new lambda.Function(this, 'AvxAutoApproverFn', {
      functionName: autoApproverName,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      role: autoApproverRole,
      // With moves live, one signed dispatch may take the executor's whole budget, so the
      // timeout covers a full batch of them (review c1 W4, c2 W2); a timeout mid-batch
      // would leave written Actions pending_execution with no retry. Off, nothing
      // dispatched makes a network call.
      timeout: cdk.Duration.seconds(
        autoMovesLive ? autoApproverBatchSize * autoApproverDispatchBudgetS + 50 : 60),
      memorySize: 256,
      logGroup: autoApproverLogGroup,
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'build', 'avx-auto-approver')),
      environment: {
        // All three are required at call time; the handler raises (and the errors
        // alarm fires) rather than falling back to another stage's table.
        AVX_ACTIONS_TABLE:    this.avxActionsTable.tableName,
        AVX_RULES_TABLE:      this.avxApprovalRulesTable.tableName,
        AVX_AGENT_RUNS_TABLE: this.avxAgentRunsTable.tableName,
        ...autoMovesEnv,
        ...autoRebookEnv,
      },
      description: `AVX auto-approval evaluator - per-fleet rules on the findings stream (${stage}, FI Stage A; moves from Stage E G8 when live)`,
    });

    // Records that exhaust their retries land here. A dropped record leaves the
    // Finding in the operator's queue (it is never auto-approved), so this is a
    // visibility problem, not data loss; the alarm below makes it visible.
    const autoApproverDlq = new sqs.Queue(this, 'AvxAutoApproverDlq', {
      queueName: `vsa-${stage}-avx-auto-approver-dlq`,
      retentionPeriod: cdk.Duration.days(14),
      enforceSSL: true,
    });

    autoApproverFn.addEventSource(new lambdaEventSources.DynamoEventSource(
      this.avxFindingsTable,
      {
        // Same settings as the broker. TRIM_HORIZON is safe: the action_id is per
        // Finding and the put is conditional, so reprocessing adds nothing.
        startingPosition: lambda.StartingPosition.TRIM_HORIZON,
        retryAttempts: 3,
        onFailure: new lambdaEventSources.SqsDlq(autoApproverDlq),
        reportBatchItemFailures: true,
        batchSize: autoApproverBatchSize,
        bisectBatchOnError: true,
        filters: [
          // Only FI queue items. The handler returns early on any record without a
          // recommended_action or a top-level fleet_id; filtering on the same two
          // conditions here stops the 5-minute diagnostics keep-alive (a MODIFY on every
          // active diagnostics Finding) from invoking the evaluator and writing a run
          // record each time (Group 3 review Cycle 10 S2). `exists` matches leaf nodes
          // only, hence the typed paths (Lambda docs: "Using the Exists operator").
          lambda.FilterCriteria.filter({
            eventName: lambda.FilterRule.or('INSERT', 'MODIFY'),
            dynamodb: {
              NewImage: {
                recommended_action: { M: { action_kind: { S: lambda.FilterRule.exists() } } },
                fleet_id:           { S: lambda.FilterRule.exists() },
              },
            },
          }),
        ],
      },
    ));

    // Spec D10: the evaluator's alarms go to the operator topic, which already has a
    // confirmed subscriber. Errors covers a crash or a missing env var; a per-record
    // failure is a partial-batch response (not a Lambda error) and is caught by the
    // DLQ alarm once its retries run out.
    const autoApproverErrorsAlarm = new cloudwatch.Alarm(this, 'AvxAutoApproverErrorsAlarm', {
      alarmName:   `vsa-${stage}-avx-auto-approver-errors-${region}`,
      alarmDescription:
        'AVX auto-approval evaluator Lambda errors >= 1 in 5 minutes. Rules are not ' +
        'being applied; matching Findings stay in the operator queue. Check the logs.',
      metric: autoApproverFn.metricErrors({
        period:    cdk.Duration.minutes(5),
        statistic: 'Sum',
      }),
      threshold:          1,
      evaluationPeriods:  1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData:   cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    autoApproverErrorsAlarm.addAlarmAction(topicAction);
    autoApproverErrorsAlarm.addOkAction(topicAction);

    const autoApproverDlqAlarm = new cloudwatch.Alarm(this, 'AvxAutoApproverDlqAlarm', {
      alarmName:   `vsa-${stage}-avx-auto-approver-dlq-${region}`,
      alarmDescription:
        `AVX auto-approval evaluator DLQ has messages: a findings-stream record ` +
        `exhausted its retries and was not evaluated. Inspect ` +
        `vsa-${stage}-avx-auto-approver-dlq and the Lambda logs.`,
      metric: new cloudwatch.Metric({
        namespace:     'AWS/SQS',
        metricName:    'ApproximateNumberOfMessagesVisible',
        dimensionsMap: { QueueName: autoApproverDlq.queueName },
        period:        cdk.Duration.minutes(5),
        statistic:     'Maximum',
      }),
      threshold:          1,
      evaluationPeriods:  1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData:   cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    autoApproverDlqAlarm.addAlarmAction(topicAction);
    // OK action too, so a drained DLQ is visible (Group 3 review Cycle 10 S3).
    autoApproverDlqAlarm.addOkAction(topicAction);

    // Gated (prompt 287): synthesized only with `fiSimOperator: true`. bin/vsa.ts does not pass it yet;
    // it is wired at task 11.9, after DMS 11.5 and CMS 11.6 deploy, so nothing from main creates this
    // Lambda (or resolves the CMS endpoint for it) before then. Every other test builds the stack without it.
    if (props.fiSimOperator === true) {
      // ── FI Sim Operator (D10): scheduled deterministic stand-in for the four demo fleets
      //
      // Runs at 07:10 UTC daily — after all Tier 2 agents (recall 05:00, warranty 05:20,
      // service 05:40, rebalance 06:00) and DMS stand-ins (05:40, 06:00). Decides
      // grounding + booking + warranty-claim Actions for Findings on demo fleets,
      // labelled "Simulated fleet operator" everywhere a decision appears. No model;
      // Tier 2 timing (nobody waits, tokens_used 0), as defined in D10.
      //
      // Design: .kiro/specs/2026-11-02-fi-ground-book-return/design-d10-sim-operator.md
      //         D10a (config), D10c (ledger), D10h (schedule, alarms, deploy).
      //
      // Asset: scripts/build-avx-lambda.sh -> infrastructure/build/avx-sim-operator/
      //        (handler + agents/fi_sim_operator + agents/executors + run_records).
      //
      // Name check: "vsa-staging-avx-sim-operator-ap-southeast-7" = 45 chars ≤ 64 ✓
      //             (IAM role names are account-wide; region suffix required)

      // ── D10c: ledger table ─────────────────────────────────────────────────────────
      // SEEN#<fleet_id> / <finding_id> rows (first_seen_at, TTL 10 days) and
      // CAP#<fleet_id> / <YYYY-MM-DD> rows (count) track candidate visibility and the
      // daily cap per fleet.  PAY_PER_REQUEST / PITR / RETAIN as all AVX data-plane tables.
      //
      // Length: "vsa-staging-avx-sim-operator" = 27 chars ≤ 255 (DDB table name;
      // account-region scoped — no region suffix needed).
      const simOperatorLedger = new dynamodb.Table(this, 'AvxSimOperatorLedger', {
        tableName:    `vsa-${stage}-avx-sim-operator`,
        partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
        sortKey:      { name: 'sk', type: dynamodb.AttributeType.STRING },
        billingMode:  dynamodb.BillingMode.PAY_PER_REQUEST,
        timeToLiveAttribute: 'ttl',
        pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
        removalPolicy: cdk.RemovalPolicy.RETAIN,
      });

      // ── IAM role ───────────────────────────────────────────────────────────────────
      const simOperatorName = `vsa-${stage}-avx-sim-operator-${region}`;
      const simOperatorRole = new iam.Role(this, 'AvxSimOperatorRole', {
        roleName:   simOperatorName,
        assumedBy:  new iam.ServicePrincipal('lambda.amazonaws.com'),
        description: `AVX simulated fleet operator - demo-fleet decision Lambda (${stage}, ${region})`,
      });

      simOperatorRole.addToPolicy(new iam.PolicyStatement({
        sid:    'SimOperatorLogs',
        effect: iam.Effect.ALLOW,
        actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
        resources: [
          `arn:aws:logs:${region}:${account}:log-group:/aws/lambda/${simOperatorName}`,
          `arn:aws:logs:${region}:${account}:log-group:/aws/lambda/${simOperatorName}:*`,
        ],
      }));

      // Findings: Query by fleet (by_fleet GSI), all pages.  Table ARN + GSI suffix only.
      simOperatorRole.addToPolicy(new iam.PolicyStatement({
        sid:    'SimOperatorFindingsFleetQuery',
        effect: iam.Effect.ALLOW,
        actions: ['dynamodb:Query'],
        resources: [`${this.avxFindingsTable.tableArn}/index/by_fleet`],
      }));

      // Actions: GetItem (existence check D10e), PutItem (decision, conditional),
      // UpdateItem (executor result).  No index grant needed.
      simOperatorRole.addToPolicy(new iam.PolicyStatement({
        sid:    'SimOperatorActionsReadWrite',
        effect: iam.Effect.ALLOW,
        actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem'],
        resources: [this.avxActionsTable.tableArn],
      }));

      // Ledger: GetItem + PutItem (SEEN rows, conditional first-seen write),
      // UpdateItem + Query (CAP rows: conditional increment, daily-cap query).
      simOperatorRole.addToPolicy(new iam.PolicyStatement({
        sid:    'SimOperatorLedgerReadWrite',
        effect: iam.Effect.ALLOW,
        actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:Query'],
        resources: [simOperatorLedger.tableArn],
      }));

      // Run records: PutItem only (one record per run; agents/tier2/run_records.py).
      simOperatorRole.addToPolicy(new iam.PolicyStatement({
        sid:    'SimOperatorAgentRunsWrite',
        effect: iam.Effect.ALLOW,
        actions: ['dynamodb:PutItem'],
        resources: [this.avxAgentRunsTable.tableArn],
      }));

      // SSM config parameter (D10a): /cvx/{stage}/fi/sim-operator — one parameter read.
      // Note the ARN path omits the leading slash per the IAM SSM ARN format:
      //   arn:aws:ssm:<region>:<account>:parameter<path>   (no slash between "parameter" and path)
      simOperatorRole.addToPolicy(new iam.PolicyStatement({
        sid:    'SimOperatorConfigParamRead',
        effect: iam.Effect.ALLOW,
        actions: ['ssm:GetParameter'],
        resources: [`arn:aws:ssm:${region}:${account}:parameter/cvx/${stage}/fi/sim-operator`],
      }));

      // CMS vehicles: GetItem only (VIN → vehicleId for hold_vehicle_ids resolution, D10a).
      simOperatorRole.addToPolicy(new iam.PolicyStatement({
        sid:    'SimOperatorCmsVehiclesRead',
        effect: iam.Effect.ALLOW,
        actions: ['dynamodb:GetItem'],
        resources: [cmsVehiclesTableArn],
      }));

      // CMS groundings: Query on open-groundings-index only (check_grounded, D10b
      // fh_book / fh_ground_book classes, using agents/fi_recall_warranty/selection.py
      // make_groundings_reader pattern).
      const cmsGroundingsTableArn =
        `arn:aws:dynamodb:${cmsRegion}:${cmsAccount}:table/cms-${cmsStage}-storage-vehicle-groundings`;
      simOperatorRole.addToPolicy(new iam.PolicyStatement({
        sid:    'SimOperatorCmsGroundingsQuery',
        effect: iam.Effect.ALLOW,
        actions: ['dynamodb:Query'],
        resources: [`${cmsGroundingsTableArn}/index/open-groundings-index`],
      }));

      // CMS internal API: POST /api/v1/fleet-intelligence/internal/groundings.
      // Uses the same cmsFiInternalApiEndpoint prop as the auto-approver.
      // CDK's ssm.StringParameter.valueForStringParameter deduplicates by construct ID
      // derived from the parameterName, so resolving the same parameterName twice is safe.
      if (!props.cmsFiInternalApiEndpoint) {
        throw new Error(
          'VsaTier2Stack: cmsFiInternalApiEndpoint is required for the simulated fleet ' +
          'operator (VSA_CMS_FI_INTERNAL_API_ENDPOINT); seed /cvx/<stage>/cms-fi-internal-api-endpoint.',
        );
      }
      const cmsEndpointSimOp = resolveConfigValue(
        this, 'cmsFiInternalApiEndpointSimOp', props.cmsFiInternalApiEndpoint,
      );
      if (!cdk.Token.isUnresolved(cmsEndpointSimOp)
          && !/^https:\/\/[a-z0-9]{1,32}\.execute-api\.[a-z]{2}(?:-[a-z]+)+-\d\.amazonaws\.com\/[A-Za-z0-9_-]+\/?$/
            .test(cmsEndpointSimOp)) {
        throw new Error(
          'VsaTier2Stack: cmsFiInternalApiEndpoint must be ' +
          'https://<api-id>.execute-api.<region>.amazonaws.com/<stage>/ ' +
          '(the FiInternalApiEndpoint output of cms-<stage>-ui, with its stage).',
        );
      }
      const cmsPartsSimOp = cdk.Fn.split('/', cmsEndpointSimOp);
      const cmsApiIdSimOp = cdk.Fn.select(0, cdk.Fn.split('.', cdk.Fn.select(2, cmsPartsSimOp)));
      const cmsStageSimOp = cdk.Fn.select(3, cmsPartsSimOp);
      simOperatorRole.addToPolicy(new iam.PolicyStatement({
        sid:    'SimOperatorCmsGroundingsInvoke',
        effect: iam.Effect.ALLOW,
        actions: ['execute-api:Invoke'],
        resources: [
          `arn:aws:execute-api:${region}:${account}:${cmsApiIdSimOp}/${cmsStageSimOp}/POST/api/v1/fleet-intelligence/internal/groundings`,
        ],
      }));

      // DMS internal API: two routes for bookings and warranty claims (D10d, D10e).
      // Required when fiSimOperator is true: without it, pairs ground cars and then
      // fail every booking on a terminal local error (S5 synth guard).
      if (!props.dmsFiInternalApiEndpoint) {
        throw new Error(
          'VsaTier2Stack: dmsFiInternalApiEndpoint is required when the simulated fleet ' +
          'operator is live (fiSimOperator); seed /cvx/<stage>/dms-fi-internal-api-endpoint.',
        );
      }
      const simOpEnv: Record<string, string> = {};
      {
        const dmsEndpointSimOp = resolveConfigValue(
          this, 'dmsFiInternalApiEndpoint', props.dmsFiInternalApiEndpoint,
        );
        if (!cdk.Token.isUnresolved(dmsEndpointSimOp)
            && !/^https:\/\/[a-z0-9]{1,32}\.execute-api\.[a-z]{2}(?:-[a-z]+)+-\d\.amazonaws\.com\/[A-Za-z0-9_-]+\/?$/
              .test(dmsEndpointSimOp)) {
          throw new Error(
            'VsaTier2Stack: dmsFiInternalApiEndpoint must be ' +
            'https://<api-id>.execute-api.<region>.amazonaws.com/<stage>/ ' +
            '(the DmsInternalApiEndpoint output with its stage).',
          );
        }
        simOpEnv.VSA_DMS_INTERNAL_API_ENDPOINT = dmsEndpointSimOp;
        const dmsPartsSimOp = cdk.Fn.split('/', dmsEndpointSimOp);
        const dmsApiIdSimOp = cdk.Fn.select(0, cdk.Fn.split('.', cdk.Fn.select(2, dmsPartsSimOp)));
        const dmsStageSimOp = cdk.Fn.select(3, dmsPartsSimOp);
        simOperatorRole.addToPolicy(new iam.PolicyStatement({
          sid:    'SimOperatorDmsInternalInvoke',
          effect: iam.Effect.ALLOW,
          actions: ['execute-api:Invoke'],
          resources: [
            `arn:aws:execute-api:${region}:${account}:${dmsApiIdSimOp}/${dmsStageSimOp}/POST/api/dms/internal/fleet/repair-orders`,
            `arn:aws:execute-api:${region}:${account}:${dmsApiIdSimOp}/${dmsStageSimOp}/POST/api/dms/internal/fleet/warranty-claims`,
          ],
        }));
      }

      // Escalation: the sim-operator always dispatches non-acknowledge-only kinds
      // (ground_vehicle, book_fleet_service, book_recall_service, submit_warranty_claim)
      // through signed internal routes; a failed dispatch may call events:PutEvents
      // (base.py escalate) and cloudwatch:PutMetricData (base.py emit_metric).
      // Always granted — not behind a feature flag (unlike the auto-approver's moves
      // grants which are gated on FI_REBALANCING_AUTO_APPROVAL_LIVE).
      simOperatorRole.addToPolicy(new iam.PolicyStatement({
        sid:    'SimOperatorEscalationPutEvents',
        effect: iam.Effect.ALLOW,
        actions: ['events:PutEvents'],
        resources: [`arn:aws:events:${region}:${account}:event-bus/default`],
      }));
      simOperatorRole.addToPolicy(new iam.PolicyStatement({
        sid:    'SimOperatorEscalationPutMetric',
        effect: iam.Effect.ALLOW,
        actions: ['cloudwatch:PutMetricData'],
        resources: ['*'],
        conditions: { StringEquals: { 'cloudwatch:namespace': 'AVX/Executor' } },
      }));

      // ── Lambda function ────────────────────────────────────────────────────────────
      const simOperatorLogGroup = new logs.LogGroup(this, 'AvxSimOperatorLogs', {
        logGroupName:  `/aws/lambda/${simOperatorName}`,
        retention:     logs.RetentionDays.ONE_MONTH,
        removalPolicy: cdk.RemovalPolicy.RETAIN,
      });

      const simOperatorFn = new lambda.Function(this, 'AvxSimOperatorFn', {
        functionName: simOperatorName,
        runtime:      lambda.Runtime.PYTHON_3_12,
        handler:      'handler.handler',
        role:         simOperatorRole,
        // 900 s: the sim-operator may make up to 80 signed dispatches per run
        // (4 fleets × 20 decisions/fleet); each dispatch touches CMS or DMS over HTTPS.
        timeout:      cdk.Duration.seconds(900),
        memorySize:   256,
        logGroup:     simOperatorLogGroup,
        // No async retries (the Scheduler invokes asynchronously; Lambda's default is
        // 2). The handler raises after writing its run record when any unit erred, so
        // the Errors alarm fires; a retry would run the plan again inside the same day.
        retryAttempts: 0,
        code: lambda.Code.fromAsset(path.join(__dirname, '..', 'build', 'avx-sim-operator')),
        environment: {
          // FI_SIM_OPERATOR_ENABLED is set ONLY on this function.
          // Guardrails G2/G3 in registry.dispatch check for this flag to admit the
          // simulated_fleet_operator decider kind; every other process that imports
          // the guardails will not have it set and will refuse the decider.
          FI_SIM_OPERATOR_ENABLED:            'true',
          AVX_SIM_OPERATOR_TABLE:             simOperatorLedger.tableName,
          AVX_ACTIONS_TABLE:                  this.avxActionsTable.tableName,
          AVX_FINDINGS_TABLE:                 this.avxFindingsTable.tableName,
          AVX_AGENT_RUNS_TABLE:               this.avxAgentRunsTable.tableName,
          FI_SIM_OPERATOR_PARAM:              `/cvx/${stage}/fi/sim-operator`,
          CMS_VEHICLES_TABLE:                 `cms-${cmsStage}-storage-vehicles`,
          CMS_GROUNDINGS_TABLE:               `cms-${cmsStage}-storage-vehicle-groundings`,
          VSA_CMS_FI_INTERNAL_API_ENDPOINT:   cmsEndpointSimOp,
          ...simOpEnv,
        },
        description:
          `AVX simulated fleet operator — daily 07:10 UTC demo-fleet decisions (${stage})`,
      });

      // ── EventBridge Scheduler: cron(10 7 * * ? *) UTC ─────────────────────────────
      // Dedicated invoker role: scoped to exactly this Lambda, not the health-agent
      // schedulerRole.  Role name: "vsa-staging-avx-sim-op-sched-ap-southeast-7" = 42 ≤ 64 ✓
      // Retries: 0 — the next run is 24 hours away and re-does the same idempotent work
      // (decisions are conditional-put on fi_decision_id; cap counters are conditional add).
      const simOpSchedulerRole = new iam.Role(this, 'AvxSimOperatorSchedulerRole', {
        roleName:    `vsa-${stage}-avx-sim-op-sched-${region}`,
        assumedBy:   new iam.ServicePrincipal('scheduler.amazonaws.com', {
          conditions: { StringEquals: { 'aws:SourceAccount': account } },
        }),
        description: `EventBridge Scheduler invoker - AVX sim operator (${stage}, ${region})`,
      });
      simOpSchedulerRole.addToPolicy(new iam.PolicyStatement({
        sid:     'SimOpSchedulerInvoke',
        effect:  iam.Effect.ALLOW,
        actions: ['lambda:InvokeFunction'],
        resources: [simOperatorFn.functionArn],
      }));

      new scheduler.Schedule(this, 'AvxSimOperatorSchedule', {
        scheduleName: `vsa-${stage}-avx-sim-operator-sched-${region}`,
        schedule:     scheduler.ScheduleExpression.cron({
          minute: '10',
          hour:   '7',
          day:    '*',
          month:  '*',
          year:   '*',
        }),
        target: new schedulerTargets.LambdaInvoke(simOperatorFn, {
          role:          simOpSchedulerRole,
          retryAttempts: 0,
          maxEventAge:   cdk.Duration.hours(1),
          // Payload {} — the handler reads its config from SSM and env vars;
          // no component name is needed (unlike the health-agent scheduler).
          input: scheduler.ScheduleTargetInput.fromObject({}),
        }),
        description: `AVX simulated fleet operator — daily 07:10 UTC demo-fleet decisions (${stage})`,
        enabled:     true,
      });

      // ── Alarms ─────────────────────────────────────────────────────────────────────
      // Both to the operator-alert topic (vsa-${stage}-tier2-operator-alerts), via
      // the same topicAction used by the auto-approver and all other agents.
      // Portfolio note (Tier2 alarm subscribe): nobody may be subscribed yet.

      // 1. Errors: the Lambda crashed, required env var is missing, or dispatch blew up.
      const simOperatorErrorsAlarm = new cloudwatch.Alarm(this, 'AvxSimOperatorErrorsAlarm', {
        alarmName:   `vsa-${stage}-avx-sim-operator-errors-${region}`,
        alarmDescription:
          'AVX simulated fleet operator Lambda errors >= 1 in 5 minutes. ' +
          'Demo-fleet decisions are not being made. Check the Lambda logs.',
        metric: simOperatorFn.metricErrors({
          period:    cdk.Duration.minutes(5),
          statistic: 'Sum',
        }),
        threshold:          1,
        evaluationPeriods:  1,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        treatMissingData:   cloudwatch.TreatMissingData.NOT_BREACHING,
      });
      simOperatorErrorsAlarm.addAlarmAction(topicAction);
      simOperatorErrorsAlarm.addOkAction(topicAction);

      // 2. Staleness: no completed run in 27 hours.
      //
      // Alarm-window choice: 27 evaluation periods × 1-hour period = 27 hours.
      // Matches the existing daily-component run-staleness alarms (runStalenessWindows
      // emptyHours=27 for recall, warranty, service, rebalance), which cover the worst
      // case between two good daily runs: 24-hour cadence plus up to an hour of
      // scheduler delivery (maxEventAge 1 h); neither the schedule nor the function
      // retries.
      // CloudWatch limit for ≥60 s periods: maximum 1440 evaluation periods (source:
      // docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/AlarmThatSendsEmail.html
      // "The number of evaluation periods for an alarm cannot exceed 1440"); 27 is well
      // within that limit.
      //
      // FILL(m1, 0) forces every hour to be a datapoint; without it a prior run's
      // positive value can hide a missed run in CloudWatch's look-back window
      // (same reasoning as § D5 / Group 3 review Cycle 10 W1).
      //
      // Metric: AVX/SimOperator, SimOperatorRunCompleted (dimensionless — the handler
      // emits a single metric per run, not per fleet / component).
      // BREACHING when missing: a Lambda timeout writes no metric and nothing alarms
      // otherwise (D10g concern, research/sim-operator-cms.md Q4).
      const simOpHeartbeat = new cloudwatch.MathExpression({
        expression: 'FILL(m1, 0)',
        usingMetrics: {
          m1: new cloudwatch.Metric({
            namespace:  'AVX/SimOperator',
            metricName: 'SimOperatorRunCompleted',
            period:     cdk.Duration.hours(1),
            statistic:  'Sum',
          }),
        },
        period: cdk.Duration.hours(1),
        label:  'SimOperatorRunCompleted (missing hours as 0)',
      });
      const simOperatorStalenessAlarm = new cloudwatch.Alarm(this, 'AvxSimOperatorStalenessAlarm', {
        alarmName:   `vsa-${stage}-avx-sim-operator-stale-${region}`,
        alarmDescription:
          `AVX simulated fleet operator: no completed run in 27 hours. ` +
          `Check the Lambda logs and the EventBridge Scheduler schedule ` +
          `vsa-${stage}-avx-sim-operator-sched-${region}.`,
        metric:             simOpHeartbeat,
        threshold:          1,
        evaluationPeriods:  27,
        datapointsToAlarm:  27,
        comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
        treatMissingData:   cloudwatch.TreatMissingData.BREACHING,
      });
      simOperatorStalenessAlarm.addAlarmAction(topicAction);
      simOperatorStalenessAlarm.addOkAction(topicAction);
    }

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
