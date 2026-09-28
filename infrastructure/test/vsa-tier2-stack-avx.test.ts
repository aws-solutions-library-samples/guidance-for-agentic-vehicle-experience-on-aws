/**
 * CDK unit tests for AVX data-plane tables and IAM grants (Group 3, Spec 1a).
 *
 * Tests use aws-cdk-lib/assertions (Template.fromStack + hasResourceProperties)
 * — the same pattern the task brief specifies.
 *
 * Mutation results recorded below:
 *
 * G3.T1 mutation — by_finding_key GSI:
 *   Procedure: comment out the addGlobalSecondaryIndex block for 'by_finding_key'
 *              in vsa-tier2-stack.ts, then run this test file.
 *   Result: test_avx_findings_has_by_finding_key_gsi FAILS with
 *           "Unable to match object at any path in array" because the GSI
 *           array in the CloudFormation template has only 6 entries, not 7,
 *           and the by_finding_key entry is absent. The test is guarding
 *           the property (GSI exists), not merely that the table exists.
 *   Restored: yes — test remains green in main.
 *   Recorded: this comment, 2026-09-19.
 *
 * G3.T4 mutation — IAM scope (no wildcard resource, no dynamodb:*):
 *   Procedure: change `resources: [this.avxFindingsTable.tableArn]` on
 *              the AvxFindingsTableWrite statement to `resources: ['*']`,
 *              then run this test file.
 *   Result: test_tier2_lambda_role_avx_findings_write_is_not_wildcard_resource
 *           FAILS because the PolicyDocument now contains a statement with
 *           Resource "*" matching our "not contains wildcard resource" assertion.
 *   Second mutation: change actions to `['dynamodb:*']` with original ARN.
 *   Result: test_tier2_lambda_role_avx_findings_write_no_dynamodb_star FAILS.
 *   Both restored: yes — test remains green in main.
 *   Recorded: this comment, 2026-09-19.
 */

import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { VsaTier2Stack } from '../lib/vsa-tier2-stack';

// ─── Stack fixture ────────────────────────────────────────────────────────────

// Memoised per stage.  Every makeStack() call triggers a full CDK asset bundle
// (Docker + pip install for the Tier 2 Lambda), which is ~30s each; the suite
// calls this once per describe block and the inputs are identical per stage, so
// caching turns N bundles into one-per-distinct-stage.  Template assertions are
// read-only, so sharing the object across describes is safe.
const _stackCache = new Map<string, { stack: VsaTier2Stack; template: Template }>();

function makeStack(stage = 'staging'): { stack: VsaTier2Stack; template: Template } {
  const cached = _stackCache.get(stage);
  if (cached) return cached;

  const app = new cdk.App();
  const stack = new VsaTier2Stack(app, `vsa-${stage}-tier2`, {
    env: { account: '123456789012', region: 'us-west-2' },
    stage,
    adpRegion: 'us-east-1',
    adpAccountId: '123456789012',
    // Pass a synthetic KMS ARN so the SNS topic gets an explicit KmsMasterKeyId
    // and the assertions don't need to special-case an absent CMK.
    // Tier A ConfigValue shape (matches vsa-api-stack-avx.test.ts). The literal is
    // what makes the CMK assertion below resolvable at synth; the parameterName is
    // the path a real deploy resolves through.
    cmsKmsKeyArn: {
      literal: 'arn:aws:kms:us-west-2:123456789012:key/test-key-id',
      parameterName: '/cvx/staging/cms-kms-key-arn',
    },
  });
  const result = { stack, template: Template.fromStack(stack) };
  _stackCache.set(stage, result);
  return result;
}

// ─── AvxFindings table (G3.T1) ────────────────────────────────────────────────

describe('AvxFindings table', () => {
  const { template } = makeStack();

  test('table name is vsa-{stage}-avx-findings', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-avx-findings',
    });
  });

  test('partition key is finding_id (String)', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-avx-findings',
      KeySchema: Match.arrayWith([
        { AttributeName: 'finding_id', KeyType: 'HASH' },
      ]),
    });
  });

  test('PITR is enabled', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-avx-findings',
      PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
    });
  });

  test('RemovalPolicy is RETAIN (DeletionPolicy)', () => {
    // CDK translates RemovalPolicy.RETAIN to DeletionPolicy: Retain on the L1.
    template.hasResource('AWS::DynamoDB::Table', {
      Properties: { TableName: 'vsa-staging-avx-findings' },
      DeletionPolicy: 'Retain',
    });
  });

  test('BillingMode is PAY_PER_REQUEST', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-avx-findings',
      BillingMode: 'PAY_PER_REQUEST',
    });
  });

  test('DynamoDB stream is NEW_IMAGE', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-avx-findings',
      StreamSpecification: { StreamViewType: 'NEW_IMAGE' },
    });
  });

  // GSI assertions — each uses indexName + partitionKey pair.

  test('has by_owner GSI on owner_id', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-avx-findings',
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: 'by_owner',
          KeySchema: Match.arrayWith([{ AttributeName: 'owner_id', KeyType: 'HASH' }]),
        }),
      ]),
    });
  });

  test('has by_vin GSI on vin', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-avx-findings',
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: 'by_vin',
          KeySchema: Match.arrayWith([{ AttributeName: 'vin', KeyType: 'HASH' }]),
        }),
      ]),
    });
  });

  test('has by_vehicle GSI on vehicle_id (GET /findings/me driver standing)', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-avx-findings',
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: 'by_vehicle',
          KeySchema: Match.arrayWith([{ AttributeName: 'vehicle_id', KeyType: 'HASH' }]),
        }),
      ]),
    });
  });

  test('has by_fleet GSI on fleet_id', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-avx-findings',
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: 'by_fleet',
          KeySchema: Match.arrayWith([{ AttributeName: 'fleet_id', KeyType: 'HASH' }]),
        }),
      ]),
    });
  });

  test('has by_dealership GSI on dealership_id', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-avx-findings',
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: 'by_dealership',
          KeySchema: Match.arrayWith([{ AttributeName: 'dealership_id', KeyType: 'HASH' }]),
        }),
      ]),
    });
  });

  test('has by_district GSI on district_id', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-avx-findings',
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: 'by_district',
          KeySchema: Match.arrayWith([{ AttributeName: 'district_id', KeyType: 'HASH' }]),
        }),
      ]),
    });
  });

  test('has by_agent_computed_at GSI on agent_id (HASH) + computed_at (RANGE)', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-avx-findings',
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: 'by_agent_computed_at',
          KeySchema: Match.arrayWith([
            { AttributeName: 'agent_id',    KeyType: 'HASH'  },
            { AttributeName: 'computed_at', KeyType: 'RANGE' },
          ]),
        }),
      ]),
    });
  });

  /**
   * G3.T1 mutation guard — by_finding_key GSI (Addendum D2 mandatory lookup).
   *
   * Mutation procedure recorded in the file-level docstring above.
   * This test MUST fail when by_finding_key is removed from the stack.
   * It stayed green when by_finding_key was present (baseline) and failed
   * when commented out (mutation). Mutation reverted; test is green in main.
   */
  test('has by_finding_key GSI on finding_key — MUTATION GUARD D2', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-avx-findings',
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: 'by_finding_key',
          KeySchema: Match.arrayWith([{ AttributeName: 'finding_key', KeyType: 'HASH' }]),
        }),
      ]),
    });
  });

  /**
   * Review cycle 2 (W2) — by_finding_key must project ALL attributes.
   *
   * The guard above checks IndexName and KeySchema only, so the projection was
   * asserted NOWHERE in the repo while a correctness fix silently depended on it.
   * Fix Group 26's F26.3 made the writer's no-op path compare the PERSISTED standing
   * attributes against the desired ones, reading them from the row this GSI returns.
   * Narrow the projection and that comparison loses every standing fact, so
   * `desired == persisted == {}` holds vacuously, the no-op fires, and a stale
   * projected fleet_id survives the full 8-day TTL — authorizing the previous fleet's
   * operators. Fail-open, and invisible: the obvious "vehicle moved fleet" spot-check
   * still converges under a narrowed projection, so a manual check would report the
   * fix healthy.
   *
   * Mutation: change this GSI's projectionType to INCLUDE or KEYS_ONLY in
   * vsa-tier2-stack.ts -> this test MUST fail.
   */
  test('by_finding_key projects ALL — F26.3 standing convergence depends on it', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-avx-findings',
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: 'by_finding_key',
          Projection: Match.objectLike({ ProjectionType: 'ALL' }),
        }),
      ]),
    });
  });

  test('CfnOutput exports vsa-{stage}-avx-findings-table-name', () => {
    template.hasOutput('AvxFindingsTableName', {
      Export: { Name: 'vsa-staging-avx-findings-table-name' },
    });
  });
});

// ─── AvxActions table (G3.T2) ─────────────────────────────────────────────────

describe('AvxActions table', () => {
  const { template } = makeStack();

  test('table name is vsa-{stage}-avx-actions', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-avx-actions',
    });
  });

  test('partition key is action_id (String)', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-avx-actions',
      KeySchema: Match.arrayWith([
        { AttributeName: 'action_id', KeyType: 'HASH' },
      ]),
    });
  });

  test('PITR is enabled', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-avx-actions',
      PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
    });
  });

  test('RemovalPolicy is RETAIN', () => {
    template.hasResource('AWS::DynamoDB::Table', {
      Properties: { TableName: 'vsa-staging-avx-actions' },
      DeletionPolicy: 'Retain',
    });
  });

  test('BillingMode is PAY_PER_REQUEST', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-avx-actions',
      BillingMode: 'PAY_PER_REQUEST',
    });
  });

  test('has by_finding GSI on finding_id', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-avx-actions',
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: 'by_finding',
          KeySchema: Match.arrayWith([{ AttributeName: 'finding_id', KeyType: 'HASH' }]),
        }),
      ]),
    });
  });

  test('has by_created_by GSI on created_by', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-avx-actions',
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: 'by_created_by',
          KeySchema: Match.arrayWith([{ AttributeName: 'created_by', KeyType: 'HASH' }]),
        }),
      ]),
    });
  });

  test('has by_target_system GSI on target_system', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-avx-actions',
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: 'by_target_system',
          KeySchema: Match.arrayWith([{ AttributeName: 'target_system', KeyType: 'HASH' }]),
        }),
      ]),
    });
  });

  test('has by_target_ref GSI on target_ref (sparse)', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-avx-actions',
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: 'by_target_ref',
          KeySchema: Match.arrayWith([{ AttributeName: 'target_ref', KeyType: 'HASH' }]),
        }),
      ]),
    });
  });

  test('CfnOutput exports vsa-{stage}-avx-actions-table-name', () => {
    template.hasOutput('AvxActionsTableName', {
      Export: { Name: 'vsa-staging-avx-actions-table-name' },
    });
  });
});

// ─── AvxFeedback table (G3.T3) ────────────────────────────────────────────────

describe('AvxFeedback table', () => {
  const { template } = makeStack();

  test('table name is vsa-{stage}-avx-feedback', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-avx-feedback',
    });
  });

  test('partition key is owner_id, sort key is feedback_id', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-avx-feedback',
      KeySchema: Match.arrayWith([
        { AttributeName: 'owner_id',    KeyType: 'HASH'  },
        { AttributeName: 'feedback_id', KeyType: 'RANGE' },
      ]),
    });
  });

  test('PITR is enabled', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-avx-feedback',
      PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
    });
  });

  test('RemovalPolicy is RETAIN', () => {
    template.hasResource('AWS::DynamoDB::Table', {
      Properties: { TableName: 'vsa-staging-avx-feedback' },
      DeletionPolicy: 'Retain',
    });
  });

  test('BillingMode is PAY_PER_REQUEST', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-avx-feedback',
      BillingMode: 'PAY_PER_REQUEST',
    });
  });

  test('TTL attribute is ttl (30-day window)', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-avx-feedback',
      TimeToLiveSpecification: { AttributeName: 'ttl', Enabled: true },
    });
  });

  test('CfnOutput exports vsa-{stage}-avx-feedback-table-name', () => {
    template.hasOutput('AvxFeedbackTableName', {
      Export: { Name: 'vsa-staging-avx-feedback-table-name' },
    });
  });
});

// ─── NotificationDevices table (G3.T3) ───────────────────────────────────────

describe('NotificationDevices table', () => {
  const { template } = makeStack();

  test('table name is vsa-{stage}-notification-devices', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-notification-devices',
    });
  });

  test('partition key is owner_id, sort key is device_token', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-notification-devices',
      KeySchema: Match.arrayWith([
        { AttributeName: 'owner_id',     KeyType: 'HASH'  },
        { AttributeName: 'device_token', KeyType: 'RANGE' },
      ]),
    });
  });

  test('PITR is enabled', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-notification-devices',
      PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
    });
  });

  test('RemovalPolicy is RETAIN', () => {
    template.hasResource('AWS::DynamoDB::Table', {
      Properties: { TableName: 'vsa-staging-notification-devices' },
      DeletionPolicy: 'Retain',
    });
  });

  test('BillingMode is PAY_PER_REQUEST', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-notification-devices',
      BillingMode: 'PAY_PER_REQUEST',
    });
  });

  test('CfnOutput exports vsa-{stage}-notification-devices-table-name', () => {
    template.hasOutput('NotificationDevicesTableName', {
      Export: { Name: 'vsa-staging-notification-devices-table-name' },
    });
  });

  /**
   * W1 guard (a) — NotificationDevices uses CMK when cmsKmsKeyArn is provided.
   *
   * Builds a stack with a synthetic KMS ARN (the makeStack() fixture already does this)
   * and asserts that the CloudFormation SSESpecification on the NotificationDevices
   * resource carries a KMSMasterKeyId that resolves to the provided ARN.
   *
   * Mutation procedure: hardcode `encryption: dynamodb.TableEncryption.DEFAULT`
   * on NotificationDevices regardless of cmsKmsKey presence.
   * Expected result: this test FAILS — SSESpecification is absent (DEFAULT renders
   * no SSESpecification), so the assertion on KMSMasterKeyId finds no match.
   * Mutation reverted; test green in main.
   * Recorded: decisions.md § "2026-09-19 — Group 3 review W1: NotificationDevices
   * CMK encryption applied", 2026-09-19.
   */
  test('NotificationDevices uses CMK when cmsKmsKeyArn is provided', () => {
    // makeStack() passes cmsKmsKeyArn: 'arn:aws:kms:us-west-2:123456789012:key/test-key-id'
    // so the template already has the CMK wired. Find the NotificationDevices resource and
    // assert SSESpecification.KMSMasterKeyId references the resolved key.
    const tables = template.findResources('AWS::DynamoDB::Table', {
      Properties: { TableName: 'vsa-staging-notification-devices' },
    });
    const tableKeys = Object.keys(tables);
    expect(tableKeys).toHaveLength(1);
    const tableProps = tables[tableKeys[0]].Properties;
    expect(tableProps).toHaveProperty('SSESpecification');
    expect(tableProps.SSESpecification.SSEEnabled).toBe(true);
    // The KMSMasterKeyId is a { 'Fn::GetAtt': [...] } or { Ref: ... } referencing the key
    // resource; it must be defined (non-falsy) — we cannot assert the literal ARN string
    // because CDK renders the key as a CloudFormation resource reference, not the raw ARN.
    expect(tableProps.SSESpecification.KMSMasterKeyId).toBeTruthy();
  });

  /**
   * W1 guard (b) — NotificationDevices falls back to DEFAULT when cmsKmsKeyArn absent.
   *
   * Builds a stack WITHOUT cmsKmsKeyArn and asserts that SSESpecification is absent
   * from the NotificationDevices CloudFormation resource (DEFAULT encryption renders
   * no SSESpecification in the template — it is the DynamoDB service default).
   */
  test('NotificationDevices falls back to DEFAULT when cmsKmsKeyArn absent', () => {
    const app = new cdk.App();
    const noKeyStack = new VsaTier2Stack(app, 'vsa-staging-tier2-nokey', {
      env: { account: '123456789012', region: 'us-west-2' },
      stage: 'staging',
      adpRegion: 'us-east-1',
      adpAccountId: '123456789012',
      // Intentionally omitting cmsKmsKeyArn to test the fallback path.
    });
    const noKeyTemplate = Template.fromStack(noKeyStack);

    const tables = noKeyTemplate.findResources('AWS::DynamoDB::Table', {
      Properties: { TableName: 'vsa-staging-notification-devices' },
    });
    const tableKeys = Object.keys(tables);
    expect(tableKeys).toHaveLength(1);
    const tableProps = tables[tableKeys[0]].Properties;
    // TableEncryption.DEFAULT renders SSESpecification with SSEEnabled: false
    // (not absent — CDK explicitly sets false). Assert SSEEnabled is false and
    // KMSMasterKeyId is absent (no customer-managed key).
    const sseSpec = tableProps.SSESpecification;
    if (sseSpec !== undefined) {
      // CDK may render {SSEEnabled: false} for DEFAULT — verify no CMK key is set.
      expect(sseSpec.SSEEnabled).not.toBe(true);
      expect(sseSpec.KMSMasterKeyId).toBeUndefined();
    }
    // Either absent or {SSEEnabled: false} without KMSMasterKeyId — both are acceptable
    // representations of the AWS-owned key default (no customer-managed key).
  });
});

// ─── AvxRateLimits table (G3.T3) ─────────────────────────────────────────────

describe('AvxRateLimits table', () => {
  const { template } = makeStack();

  test('table name is vsa-{stage}-avx-rate-limits', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-avx-rate-limits',
    });
  });

  test('partition key is owner_id, sort key is window_key', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-avx-rate-limits',
      KeySchema: Match.arrayWith([
        { AttributeName: 'owner_id',   KeyType: 'HASH'  },
        { AttributeName: 'window_key', KeyType: 'RANGE' },
      ]),
    });
  });

  test('PITR is enabled', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-avx-rate-limits',
      PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
    });
  });

  test('RemovalPolicy is RETAIN', () => {
    template.hasResource('AWS::DynamoDB::Table', {
      Properties: { TableName: 'vsa-staging-avx-rate-limits' },
      DeletionPolicy: 'Retain',
    });
  });

  test('BillingMode is PAY_PER_REQUEST', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-avx-rate-limits',
      BillingMode: 'PAY_PER_REQUEST',
    });
  });

  test('TTL attribute is ttl (window expiry)', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-avx-rate-limits',
      TimeToLiveSpecification: { AttributeName: 'ttl', Enabled: true },
    });
  });

  test('CfnOutput exports vsa-{stage}-avx-rate-limits-table-name', () => {
    template.hasOutput('AvxRateLimitsTableName', {
      Export: { Name: 'vsa-staging-avx-rate-limits-table-name' },
    });
  });
});

// ─── G3.T4 IAM grants — Tier 2 Lambda role ──────────────────────────────────
//
// Asserts:
//   1. lambdaRole has PutItem + UpdateItem on avx-findings table ARN (specific).
//   2. lambdaRole has Query on by_finding_key GSI ARN (specific).
//   3. No statement on lambdaRole uses Resource: "*" for DynamoDB actions.
//   4. No statement on lambdaRole uses the dynamodb:* action catch-all.
//
// Mutation results recorded in the file-level docstring above.

describe('G3.T4 — IAM grants on Tier 2 Lambda role', () => {
  const { template } = makeStack();

  /**
   * Assert PutItem + UpdateItem on avx-findings table.
   * CDK renders IAM policies as AWS::IAM::Policy resources attached to the role.
   * We look for a policy document containing the specific sid.
   */
  test('AvxFindingsTableWrite statement has PutItem and UpdateItem', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: 'AvxFindingsTableWrite',
            Effect: 'Allow',
            Action: Match.arrayWith(['dynamodb:PutItem', 'dynamodb:UpdateItem']),
          }),
        ]),
      },
    });
  });

  test('AvxFindingsKeyIndexQuery statement has Query action', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: 'AvxFindingsKeyIndexQuery',
            Effect: 'Allow',
            Action: 'dynamodb:Query',
          }),
        ]),
      },
    });
  });

  /**
   * G3.T4 mutation guard — no wildcard resource.
   *
   * This test scans ALL IAM policies in the template and asserts none of the
   * DynamoDB-actioned statements use Resource: "*".
   *
   * Mutation procedure: change `resources: [this.avxFindingsTable.tableArn]`
   * to `resources: ['*']` in the AvxFindingsTableWrite statement.
   * Result: this test FAILS because the rendered template now has a statement
   * with both a dynamodb: action AND Resource "*".
   * Mutation reverted; test is green in main.
   */
  test('no DynamoDB statement uses Resource wildcard — MUTATION GUARD G3.T4', () => {
    // Named exemption (2026-09-24, Fix Group 32): a statement whose ONLY action
    // is dynamodb:ListStreams may use Resource "*". The AVX broker's
    // DynamoEventSource makes CDK add exactly that grant to AvxBrokerRoleDefaultPolicy:
    // aws-cdk-lib 2.269.0 `Table.grantListStreams` hardcodes resourceArns ["*"]
    // (node_modules/aws-cdk-lib/aws-dynamodb/lib/table.js). The broker's own
    // BrokerAvxFindingsStreamRead statement separately scopes ListStreams to the
    // table's stream ARNs. The broker landed in 9fcb258 without this suite being
    // run, so this guard was red from then until now.
    // Anything else on "*" — including ListStreams bundled with another action —
    // still fails. Anti-vacuity for the exemption is the test below.
    const isListStreamsOnly = (actions: unknown[]) =>
      actions.length === 1 && actions[0] === 'dynamodb:ListStreams';
    const policyResources = template.findResources('AWS::IAM::Policy');
    for (const [logicalId, resource] of Object.entries(policyResources)) {
      const statements: Array<Record<string, unknown>> =
        resource.Properties?.PolicyDocument?.Statement ?? [];
      for (const stmt of statements) {
        const actions = Array.isArray(stmt.Action) ? stmt.Action : [stmt.Action];
        const hasDynamoDbAction = actions.some(
          (a: unknown) => typeof a === 'string' && a.toLowerCase().startsWith('dynamodb:'),
        );
        if (!hasDynamoDbAction) continue;
        if (isListStreamsOnly(actions)) continue;

        const resources = Array.isArray(stmt.Resource) ? stmt.Resource : [stmt.Resource];
        const hasWildcard = resources.some((r: unknown) => r === '*');
        expect({ logicalId, hasWildcard }).toEqual({ logicalId, hasWildcard: false });
      }
    }
  });

  test('ListStreams-on-* exemption is real and narrow (anti-vacuity for G3.T4)', () => {
    const statements = Object.values(template.findResources('AWS::IAM::Policy'))
      .flatMap((r) => (r as any).Properties?.PolicyDocument?.Statement ?? []);
    const wildcardDynamo = statements.filter((s: any) => {
      const actions = Array.isArray(s.Action) ? s.Action : [s.Action];
      const resources = Array.isArray(s.Resource) ? s.Resource : [s.Resource];
      return resources.includes('*') && actions.some((a: string) => a.startsWith('dynamodb:'));
    });
    // (1) the exemption still matches something, so it is not a dead rule.
    expect(wildcardDynamo.length).toBeGreaterThanOrEqual(1);
    // (2) everything it matches is ListStreams alone.
    for (const s of wildcardDynamo) {
      expect(s.Action).toEqual('dynamodb:ListStreams');
    }
  });

  /**
   * G3.T4 mutation guard — no dynamodb:* catch-all.
   *
   * Mutation procedure: change actions in AvxFindingsTableWrite to ['dynamodb:*'].
   * Result: this test FAILS.
   * Mutation reverted; test is green in main.
   */
  test('no DynamoDB statement uses dynamodb:* catch-all — MUTATION GUARD G3.T4', () => {
    const policyResources = template.findResources('AWS::IAM::Policy');
    for (const [_logicalId, resource] of Object.entries(policyResources)) {
      const statements: Array<Record<string, unknown>> =
        resource.Properties?.PolicyDocument?.Statement ?? [];
      for (const stmt of statements) {
        const actions = Array.isArray(stmt.Action) ? stmt.Action : [stmt.Action];
        const hasCatchAll = actions.some(
          (a: unknown) => typeof a === 'string' && a.toLowerCase() === 'dynamodb:*',
        );
        expect(hasCatchAll).toBe(false);
      }
    }
  });
});


// ─── T16.3 — AVX_FINDINGS_TABLE env var on Tier 2 Lambda (T16.3) ─────────────
//
// Asserts that the Tier 2 health-agent Lambda's environment carries
// AVX_FINDINGS_TABLE = 'vsa-{stage}-avx-findings'.
//
// This is the D2 writer's table-name contract: the runner must receive the
// table name via env var (not hard-code it) so the stack is the single source
// of truth.  The IAM grants (PutItem+UpdateItem on the table, Query on
// by_finding_key GSI) were already pinned in G3.T4; this test closes the
// complementary env-var half of the contract.
//
// Mutation note (T16.3 mutation):
//   Procedure: change `AVX_FINDINGS_TABLE: \`vsa-\${stage}-avx-findings\`` to
//              `AVX_FINDINGS_TABLE: 'vsa-staging-avx-findings'` (hardcoded literal)
//              and re-run this suite with stage='prod'.
//   Expected result: test 'Tier 2 Lambda carries AVX_FINDINGS_TABLE env var for prod'
//                    FAILS because the rendered value is 'vsa-staging-avx-findings'
//                    while the test expects 'vsa-prod-avx-findings'.
//   This demonstrates the test asserts the VALUE (stage-parameterised), not merely
//   that the key is present — presence-only would not catch a hardcoded table name.
//   Mutation exercised and caught 2026-09-21; reverted, test green in main.

describe('T16.3 — AVX_FINDINGS_TABLE env var on Tier 2 Lambda', () => {
  /**
   * Locate the Lambda function named 'vsa-{stage}-tier2-health-agent-{region}'
   * and assert AVX_FINDINGS_TABLE is set to 'vsa-{stage}-avx-findings'.
   *
   * CDK renders Lambda environment as:
   *   Environment: { Variables: { KEY: VALUE, ... } }
   * under the AWS::Lambda::Function resource.
   *
   * We use findResources to get the raw CloudFormation resource and then
   * inspect Variables directly — this lets us assert the EXACT value, not
   * merely that the key is present (presence-only = the defect this repo
   * shipped before; see vsa-tier2-stack-avx.test.ts file-level docstring).
   */
  test('Tier 2 Lambda carries AVX_FINDINGS_TABLE env var (staging)', () => {
    const { template } = makeStack('staging');
    const functions = template.findResources('AWS::Lambda::Function', {
      Properties: {
        FunctionName: 'vsa-staging-tier2-health-agent-us-west-2',
      },
    });
    const keys = Object.keys(functions);
    expect(keys).toHaveLength(1);
    const env = functions[keys[0]].Properties?.Environment?.Variables ?? {};
    expect(env['AVX_FINDINGS_TABLE']).toBe('vsa-staging-avx-findings');
  });

  /**
   * Stage-parameterisation guard (the mutation target).
   *
   * Builds a 'prod' stack (still us-west-2 / synthetic account) and asserts
   * the rendered value is 'vsa-prod-avx-findings', not a hardcoded staging
   * string.  A stack that resolves the table name via template literal passes;
   * one with a baked 'staging' constant fails here (the mutation).
   */
  test('Tier 2 Lambda carries AVX_FINDINGS_TABLE env var for prod — MUTATION GUARD T16.3', () => {
    const app = new cdk.App();
    const prodStack = new VsaTier2Stack(app, 'vsa-prod-tier2', {
      env: { account: '123456789012', region: 'us-west-2' },
      stage: 'prod',
      adpRegion: 'us-east-1',
      adpAccountId: '123456789012',
    });
    const prodTemplate = Template.fromStack(prodStack);
    const functions = prodTemplate.findResources('AWS::Lambda::Function', {
      Properties: {
        FunctionName: 'vsa-prod-tier2-health-agent-us-west-2',
      },
    });
    const keys = Object.keys(functions);
    expect(keys).toHaveLength(1);
    const env = functions[keys[0]].Properties?.Environment?.Variables ?? {};
    // Mutation: if AVX_FINDINGS_TABLE is hardcoded to 'vsa-staging-avx-findings',
    // this expectation FAILS (got 'vsa-staging-avx-findings', expected 'vsa-prod-avx-findings').
    expect(env['AVX_FINDINGS_TABLE']).toBe('vsa-prod-avx-findings');
  });
});



// ─── F11.2 (Fix Group 15) — CMS vehicle-registry grants + env var ─────────────
//
// The registry lookup is necessarily TWO calls, because the deployed table's key
// schema makes one impossible. Verified live against cms-staging-storage-vehicles
// 2026-09-23: base key schema is `vehicleId` (HASH) only, and the `vin-index` GSI
// projects KEYS_ONLY so it cannot return `sold_to`.
//
// That is why these tests assert a Query grant AND a GetItem grant, rather than
// the GetItem-only grant F11.2 Accept 5 specifies. The deviation and its evidence
// are recorded in decisions.md § "2026-09-23 — Fix Group 15 build: four spec
// defects found while implementing F11.2/F11.3".
//
// Mutation results (M-FG11-6 / M-FG11-7), each exercised and reverted:
//
//   M-FG11-6a: resources -> ['*'] on CmsVehiclesOwnerGetItem
//     → 'no DynamoDB statement uses Resource wildcard' FAILS. Reverted.
//   M-FG11-6b: actions -> ['dynamodb:*'] on CmsVehiclesOwnerGetItem
//     → 'no DynamoDB statement uses dynamodb:* catch-all' FAILS. Reverted.
//   M-FG11-7a: add 'dynamodb:Scan' to CmsVehiclesOwnerGetItem
//     → 'CMS registry grants carry no action beyond the two call sites' FAILS.
//       Reverted.
//   M-FG11-7b: point CmsVehiclesVinIndexQuery at the TABLE arn instead of the
//       index arn → 'vin-index Query is scoped to the index, not the table'
//       FAILS. This is the mutation that matters most: a Query grant on the
//       table would also authorize querying every other index on it, and an
//       absence-only guard cannot see the difference. Reverted.

describe('F11.2 — CMS vehicle registry grants on the Tier 2 Lambda role', () => {
  const { template } = makeStack();

  /** Every statement across every policy, flattened. */
  function allStatements(): Array<Record<string, any>> {
    const out: Array<Record<string, any>> = [];
    for (const resource of Object.values(template.findResources('AWS::IAM::Policy'))) {
      const statements: Array<Record<string, any>> =
        (resource as any).Properties?.PolicyDocument?.Statement ?? [];
      out.push(...statements);
    }
    return out;
  }

  function statementBySid(sid: string): Record<string, any> {
    const match = allStatements().filter((s) => s.Sid === sid);
    expect(match.length).toBe(1);
    return match[0];
  }

  function asArray(v: unknown): unknown[] {
    return Array.isArray(v) ? v : [v];
  }

  /** Render a Resource entry (string or Fn::Join) to a comparable string. */
  function renderResource(r: unknown): string {
    if (typeof r === 'string') return r;
    const join = (r as any)?.['Fn::Join'];
    if (Array.isArray(join)) {
      const [sep, parts] = join;
      return (parts as unknown[])
        .map((p) => (typeof p === 'string' ? p : JSON.stringify(p)))
        .join(sep as string);
    }
    return JSON.stringify(r);
  }

  test('CmsVehiclesVinIndexQuery grants exactly dynamodb:Query', () => {
    const stmt = statementBySid('CmsVehiclesVinIndexQuery');
    expect(asArray(stmt.Action)).toEqual(['dynamodb:Query']);
    expect(stmt.Effect).toBe('Allow');
  });

  test('vin-index Query is scoped to the index, not the table — MUTATION GUARD M-FG11-7b', () => {
    const stmt = statementBySid('CmsVehiclesVinIndexQuery');
    const resources = asArray(stmt.Resource).map(renderResource);
    expect(resources).toHaveLength(1);
    const arn = resources[0];

    expect(arn).toContain('table/cms-staging-storage-vehicles');
    expect(arn).toContain('/index/vin-index');
    // A Query grant that stops at the table would cover every other index too.
    expect(arn.endsWith('/index/vin-index')).toBe(true);
  });

  test('CmsVehiclesOwnerGetItem grants exactly dynamodb:GetItem on the table', () => {
    const stmt = statementBySid('CmsVehiclesOwnerGetItem');
    expect(asArray(stmt.Action)).toEqual(['dynamodb:GetItem']);

    const resources = asArray(stmt.Resource).map(renderResource);
    expect(resources).toHaveLength(1);
    expect(resources[0]).toContain('table/cms-staging-storage-vehicles');
    // The base-table ARN, NOT an index ARN — GetItem does not apply to indexes.
    expect(resources[0]).not.toContain('/index/');
  });

  test('CMS registry grants carry no action beyond the two call sites — MUTATION GUARD M-FG11-7a', () => {
    // agents/tier2/owner_resolver.py makes exactly two calls: query() on the
    // index and get_item() on the table. Anything else is over-provision.
    // Deliberately no Scan: the resolver has no Scan fallback, and adding one
    // would widen this grant for a case the index already answers.
    const permitted = new Set(['dynamodb:Query', 'dynamodb:GetItem']);
    for (const sid of ['CmsVehiclesVinIndexQuery', 'CmsVehiclesOwnerGetItem']) {
      const actions = asArray(statementBySid(sid).Action) as string[];
      for (const action of actions) {
        expect(permitted.has(action)).toBe(true);
      }
      expect(actions).not.toContain('dynamodb:Scan');
      expect(actions).not.toContain('dynamodb:PutItem');
      expect(actions).not.toContain('dynamodb:UpdateItem');
      expect(actions).not.toContain('dynamodb:DeleteItem');
    }
  });

  test('neither CMS grant uses a wildcard resource — MUTATION GUARD M-FG11-6a', () => {
    for (const sid of ['CmsVehiclesVinIndexQuery', 'CmsVehiclesOwnerGetItem']) {
      const resources = asArray(statementBySid(sid).Resource).map(renderResource);
      expect(resources).not.toContain('*');
      for (const r of resources) {
        expect(r).not.toBe('*');
      }
    }
  });

  test('the CMS grants do not reach any table other than the vehicle registry', () => {
    // Guards against a copy-paste that re-points one of these at the findings
    // table, which would be a silent privilege grant on AVX data.
    for (const sid of ['CmsVehiclesVinIndexQuery', 'CmsVehiclesOwnerGetItem']) {
      const resources = asArray(statementBySid(sid).Resource).map(renderResource);
      for (const r of resources) {
        expect(r).toContain('cms-staging-storage-vehicles');
        expect(r).not.toContain('avx-findings');
        expect(r).not.toContain('health-findings');
      }
    }
  });
});

describe('F11.2 — CMS_VEHICLES_TABLE env var on the Tier 2 Lambda', () => {
  /**
   * Asserts the VALUE, stage-parameterised — not merely that the key exists.
   *
   * Mutation: hardcode `CMS_VEHICLES_TABLE: 'cms-staging-storage-vehicles'` in
   * the stack and run the prod case below.
   * Result: 'resolves per stage (prod)' FAILS — rendered value is the staging
   * table while the test expects the prod one. Presence-only assertions cannot
   * catch a hardcoded table name; this is the same reasoning as T16.3's mutation
   * note above. Reverted; green in main.
   */
  function tier2Env(stage: string): Record<string, string> {
    const { template } = makeStack(stage);
    const fns = template.findResources('AWS::Lambda::Function');
    for (const fn of Object.values(fns)) {
      const props = (fn as any).Properties;
      const vars = props?.Environment?.Variables ?? {};
      if ('AVX_FINDINGS_TABLE' in vars) return vars;
    }
    throw new Error('Tier 2 health-agent Lambda not found in template');
  }

  test('resolves per stage (staging)', () => {
    expect(tier2Env('staging').CMS_VEHICLES_TABLE).toBe('cms-staging-storage-vehicles');
  });

  test('resolves per stage (prod)', () => {
    expect(tier2Env('prod').CMS_VEHICLES_TABLE).toBe('cms-prod-storage-vehicles');
  });

  test('the runner never has to derive the registry name from parts', () => {
    // The stack ships a fully-resolved name, matching the ATHENA_RESULTS_BUCKET
    // convention. A partial value (bare stage, or an ARN) would push string
    // assembly into the runner, which is the defect class that produced
    // Critical #2 in review Cycle 6.
    const value = tier2Env('staging').CMS_VEHICLES_TABLE;
    expect(value.startsWith('cms-')).toBe(true);
    expect(value.endsWith('-storage-vehicles')).toBe(true);
    expect(value).not.toContain('arn:');
    expect(value).not.toContain('${');
  });
});


// ─── diagnostics component (spec 2026-09-25-avx-own-vehicle-findings T4.4) ────

describe('diagnostics component', () => {
  const { template } = makeStack();

  test('a 5-minute schedule invokes the health agent with component=diagnostics', () => {
    const schedules = template.findResources('AWS::Scheduler::Schedule');
    const diag = Object.values(schedules).filter((s: any) =>
      String(s.Properties?.Target?.Input ?? '').includes('"diagnostics"'));
    expect(diag).toHaveLength(1);
    const props = (diag[0] as any).Properties;
    expect(props.ScheduleExpression).toBe('rate(5 minutes)');
    expect(JSON.parse(props.Target.Input)).toEqual({ stage: 'staging', component: 'diagnostics' });
    expect(props.Target.RetryPolicy.MaximumRetryAttempts).toBe(0);
    expect(props.State).toBe('ENABLED');
  });

  test('the daily schedules are unchanged: three, cron, ADP components only', () => {
    const schedules = Object.values(template.findResources('AWS::Scheduler::Schedule')) as any[];
    const daily = schedules.filter(s => String(s.Properties.ScheduleExpression).startsWith('cron('));
    expect(daily.map(s => JSON.parse(s.Properties.Target.Input).component).sort())
      .toEqual(['battery', 'brakes', 'tires']);
  });

  test('DTC history read is Scan+Query on that one table, no index, nothing else', () => {
    const policies = template.findResources('AWS::IAM::Policy');
    const stmts = Object.values(policies).flatMap((p: any) => p.Properties.PolicyDocument.Statement);
    const dtc = stmts.filter((s: any) => s.Sid === 'CmsDtcHistoryRead');
    expect(dtc).toHaveLength(1);
    expect([...dtc[0].Action].sort()).toEqual(['dynamodb:Query', 'dynamodb:Scan']);
    expect(dtc[0].Resource).toBe('arn:aws:dynamodb:us-west-2:123456789012:table/cms-staging-storage-dtc-history');
    // No other statement names the DTC table, and nothing grants write on it.
    const others = stmts.filter((s: any) => s.Sid !== 'CmsDtcHistoryRead'
      && JSON.stringify(s.Resource ?? '').includes('storage-dtc-history'));
    expect(others).toEqual([]);
  });

  test('the Lambda gets CMS_DTC_HISTORY_TABLE fully resolved', () => {
    const fns = Object.values(template.findResources('AWS::Lambda::Function')) as any[];
    const vals = fns.map(f => f.Properties?.Environment?.Variables?.CMS_DTC_HISTORY_TABLE).filter(Boolean);
    expect(vals).toEqual(['cms-staging-storage-dtc-history']);
  });

  test('brakes model_year join: Glue metadata and S3 prefix for vehicle_identity', () => {
    const stmts = Object.values(template.findResources('AWS::IAM::Policy'))
      .flatMap((p: any) => p.Properties.PolicyDocument.Statement) as any[];
    const glue = stmts.find(s => s.Sid === 'AdpGlueMetadataRead');
    expect(glue.Resource).toContain('arn:aws:glue:us-east-1:123456789012:table/adp_staging_vehicle_identity/*');
    const s3 = stmts.find(s => s.Sid === 'AdpLakeS3Read');
    expect(JSON.stringify(s3.Resource)).toContain('/curated/vehicle_identity/*');
  });
});
