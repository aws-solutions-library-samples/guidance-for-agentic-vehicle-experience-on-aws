/**
 * CDK unit tests for the AVX DMS event-consumer Lambda + EventBridge rule
 * (DMS Spec 2 T5.4 + T5.5).
 *
 * Test targets (from DMS spec `decisions.md § "2026-09-21 — T5.4 + T5.5
 * design" → Tests`):
 *
 *   1. `test_iam_scoped_to_actions_table_only` — Resource is the actions
 *      table ARN, nothing else. Mutation target M2: widening Resource to
 *      `'*'` must fail.
 *   2. `test_iam_actions_are_updateitem_only` — the AvxActionsProjection
 *      statement contains exactly `['dynamodb:UpdateItem']`. Adding
 *      `PutItem` or `DeleteItem` must fail.
 *   3. `test_rule_event_pattern_matches_publisher_constants` — Mutation
 *      target M3: source is exactly `['dms.service-lane']`, detail-type is
 *      exactly `['dms.ro.status_changed']`, account is `[dmsAccountId]`.
 *      Widening source to `['*']` must fail.
 *   4. `test_rule_targets_the_consumer_lambda` — rule target is the T5.4
 *      Lambda's ARN.
 *
 * Mutation results recorded after the mutations were verified (see spec
 * decisions.md § "T5.4 + T5.5 mutation ledger" for the full record).
 */

import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { VsaTier2Stack } from '../lib/vsa-tier2-stack';

const TEST_ACCOUNT = '123456789012';
const TEST_REGION  = 'us-west-2';

function makeStack(stage = 'staging'): { stack: VsaTier2Stack; template: Template } {
  const app = new cdk.App();
  const stack = new VsaTier2Stack(app, `vsa-${stage}-tier2`, {
    env: { account: TEST_ACCOUNT, region: TEST_REGION },
    stage,
    adpRegion: 'us-east-1',
    adpAccountId: TEST_ACCOUNT,
    // Synthetic KMS key ARN so the SNS topic gets an explicit KmsMasterKeyId.
    cmsKmsKeyArn: {
      literal: `arn:aws:kms:${TEST_REGION}:${TEST_ACCOUNT}:key/test-key-id`,
      parameterName: '/cvx/staging/cms-kms-key-arn',
    },
  });
  return { stack, template: Template.fromStack(stack) };
}

// ─── Lambda existence ──────────────────────────────────────────────────────

describe('AVX DMS event-consumer Lambda (T5.4)', () => {
  test('has a Lambda function named vsa-{stage}-avx-dms-event-consumer-{region}', () => {
    const { template } = makeStack('staging');
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: `vsa-staging-avx-dms-event-consumer-${TEST_REGION}`,
      Runtime: 'python3.12',
      Handler: 'handler.handler',
    });
  });

  test('has environment variable AVX_ACTIONS_TABLE set to the actions table name', () => {
    const { template } = makeStack('staging');
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: `vsa-staging-avx-dms-event-consumer-${TEST_REGION}`,
      Environment: {
        Variables: Match.objectLike({
          // CDK emits this as `{"Ref": "AvxActions..."}` intrinsic. Match on
          // the Ref shape rather than the resolved string — the resolved
          // value is `vsa-staging-avx-actions`, but CFN synth doesn't
          // resolve intrinsics for us.
          AVX_ACTIONS_TABLE: Match.objectLike({ Ref: Match.stringLikeRegexp('^AvxActions') }),
        }),
      },
    });
  });
});

// ─── IAM scope (T5.4 M2 mutation target) ──────────────────────────────────

describe('AVX DMS event-consumer IAM (T5.4 M2)', () => {
  test('test_iam_scoped_to_actions_table_only', () => {
    const { template } = makeStack('staging');

    // Find the AvxActionsProjection statement. It must exist AND its Resource
    // must be exactly the actions-table ARN. Mutation M2 widens Resource to
    // '*' — this assertion catches that by asserting a NEGATIVE match on '*'.
    const policies = template.findResources('AWS::IAM::Policy');
    let sawProjection = false;
    let sawWildcardResource = false;

    for (const policy of Object.values(policies)) {
      const statements = policy.Properties?.PolicyDocument?.Statement || [];
      for (const stmt of statements) {
        if (stmt.Sid === 'AvxActionsProjection') {
          sawProjection = true;
          // Resource shape can be a plain string, a Fn::GetAtt intrinsic, or
          // a Fn::Join intrinsic — CDK-emitted table ARNs typically arrive as
          // Fn::GetAtt on the AvxActions table. What we assert is a
          // NEGATIVE: the Resource must NOT be `'*'`, and MUST resolve to
          // the actions table (via any of those shapes).
          const res = stmt.Resource;
          if (res === '*') sawWildcardResource = true;
          if (Array.isArray(res)) {
            for (const r of res) if (r === '*') sawWildcardResource = true;
          }
          // Positive shape check: resource is the actions table ARN via
          // Fn::GetAtt or a matching literal. The intrinsic form is what
          // CDK emits from `this.avxActionsTable.tableArn`.
          const resStr = JSON.stringify(res);
          expect(resStr).toMatch(/AvxActions/);
          expect(resStr).toMatch(/Arn/);
        }
      }
    }

    expect(sawProjection).toBe(true);
    expect(sawWildcardResource).toBe(false);
  });

  test('test_iam_actions_are_updateitem_only (T5.4 M4 mutation target)', () => {
    const { template } = makeStack('staging');
    const policies = template.findResources('AWS::IAM::Policy');
    let sawProjection = false;

    for (const policy of Object.values(policies)) {
      const statements = policy.Properties?.PolicyDocument?.Statement || [];
      for (const stmt of statements) {
        if (stmt.Sid === 'AvxActionsProjection') {
          sawProjection = true;
          // Actions is an array. Must be EXACTLY UpdateItem.
          const actions = Array.isArray(stmt.Action) ? stmt.Action : [stmt.Action];
          expect(actions).toEqual(['dynamodb:UpdateItem']);
          // Explicit no-list — these are the mutations M4 introduces.
          expect(actions).not.toContain('dynamodb:PutItem');
          expect(actions).not.toContain('dynamodb:DeleteItem');
          expect(actions).not.toContain('dynamodb:*');
          expect(actions).not.toContain('*');
        }
      }
    }
    expect(sawProjection).toBe(true);
  });

  test('has CloudWatch Logs write grants scoped to the consumer log group only', () => {
    const { template } = makeStack('staging');
    // Assert both actions AND resource scope. The Resource must reference
    // ONLY the consumer's own log group ARN; a wildcard would let this role
    // write logs to any log group in the account.
    const policies = template.findResources('AWS::IAM::Policy');
    let sawLogsStmt = false;
    for (const policy of Object.values(policies)) {
      const statements = policy.Properties?.PolicyDocument?.Statement || [];
      for (const stmt of statements) {
        if (stmt.Sid === 'CloudWatchLogsWrite') {
          sawLogsStmt = true;
          expect(stmt.Effect).toBe('Allow');
          expect(stmt.Action).toEqual([
            'logs:CreateLogGroup',
            'logs:CreateLogStream',
            'logs:PutLogEvents',
          ]);
          const resStr = JSON.stringify(stmt.Resource);
          // Must contain the consumer's log-group name and MUST NOT contain '*'.
          expect(resStr).toMatch(/avx-dms-event-consumer/);
          expect(resStr).not.toMatch(/"\*"/);  // literal '*' resource
        }
      }
    }
    expect(sawLogsStmt).toBe(true);
  });
});

// ─── EventBridge rule (T5.5 M3 mutation target) ─────────────────────────

describe('AVX DMS event-consumer EventBridge rule (T5.5 M3)', () => {
  test('test_rule_event_pattern_matches_publisher_constants', () => {
    const { template } = makeStack('staging');

    // Find our rule. There may be other Events::Rule resources in this stack
    // (schedulers etc.), so we pin by Name.
    const rules = template.findResources('AWS::Events::Rule');
    let sawRule = false;

    for (const rule of Object.values(rules)) {
      const props = rule.Properties;
      if (props?.Name === `vsa-staging-avx-ro-status-changed`) {
        sawRule = true;

        // EventPattern MUST be exactly these three constants — no wildcards,
        // no widening. Mutation M3 changes source to '*'; that produces
        // `source: ['*']` here and this equality assertion fails.
        expect(props.EventPattern).toEqual({
          source:       ['dms.service-lane'],
          'detail-type': ['dms.ro.status_changed'],
          account:      [TEST_ACCOUNT],  // same-account topology: dmsAccountId defaults to own account
        });

        // Belt-and-braces negative pins in case a future contributor widens
        // any of the three without touching the exact-shape equality above.
        expect(props.EventPattern.source).not.toContain('*');
        expect(props.EventPattern.source).not.toContain('dms.*');
        expect(props.EventPattern['detail-type']).not.toContain('*');
      }
    }

    expect(sawRule).toBe(true);
  });

  test('test_rule_targets_the_consumer_lambda', () => {
    const { template } = makeStack('staging');
    const rules = template.findResources('AWS::Events::Rule');

    for (const rule of Object.values(rules)) {
      const props = rule.Properties;
      if (props?.Name === `vsa-staging-avx-ro-status-changed`) {
        // Rule must have exactly one target: the consumer Lambda.
        expect(props.Targets).toHaveLength(1);

        // Target Arn is a Fn::GetAtt on the Lambda function. Serialising and
        // matching by substring on the logical id keeps the test robust to
        // CDK hash suffixes.
        const targetArn = JSON.stringify(props.Targets[0].Arn);
        expect(targetArn).toMatch(/AvxDmsEventConsumer/);
        expect(targetArn).toMatch(/Arn/);

        // Retry configuration matches the design (retryAttempts: 2).
        expect(props.Targets[0].RetryPolicy?.MaximumRetryAttempts).toBe(2);
      }
    }
  });

  test('rule is attached to the DMS event bus (not the default account bus)', () => {
    const { template } = makeStack('staging');
    const rules = template.findResources('AWS::Events::Rule');

    for (const rule of Object.values(rules)) {
      const props = rule.Properties;
      if (props?.Name === `vsa-staging-avx-ro-status-changed`) {
        // EventBusName must resolve to `dms-staging-events`. CDK emits it as
        // the exact bus ARN when the bus is imported by ARN via
        // events.EventBus.fromEventBusArn(). Assert on the ARN's suffix so
        // the assertion is robust to CDK's intrinsic serialisation.
        const eb = JSON.stringify(props.EventBusName);
        expect(eb).toMatch(/dms-staging-events/);
      }
    }
  });
});

// ─── Lambda–rule linkage (T5.5 M3 belt-and-braces) ──────────────────────

describe('AVX DMS event-consumer wiring integrity', () => {
  test('the Lambda has an EventBridge invoke permission from the rule', () => {
    const { template } = makeStack('staging');

    // CDK auto-adds an AWS::Lambda::Permission resource when a rule targets
    // a Lambda. Its Principal must be events.amazonaws.com and its
    // SourceArn must point at OUR rule (not a broader wildcard).
    // Security-review Cycle 1 S3 (2026-09-21): tightened from Principal-only
    // to include SourceArn shape assertion.
    const permissions = template.findResources('AWS::Lambda::Permission');
    let sawRulePermission = false;
    for (const perm of Object.values(permissions)) {
      const props = perm.Properties;
      if (props?.Principal === 'events.amazonaws.com') {
        const sourceArnStr = JSON.stringify(props.SourceArn);
        // SourceArn must reference our rule by Fn::GetAtt. A missing SourceArn
        // (unconstrained) or a wildcard would fail this assertion.
        if (sourceArnStr.includes('AvxRoStatusChangedRule')) {
          sawRulePermission = true;
          expect(props.Action).toBe('lambda:InvokeFunction');
          expect(sourceArnStr).toMatch(/AvxRoStatusChangedRule/);
          expect(sourceArnStr).toMatch(/Arn/);
          // Negative: no '*' in the SourceArn.
          expect(sourceArnStr).not.toMatch(/"\*"/);
        }
      }
    }
    expect(sawRulePermission).toBe(true);
  });
});
