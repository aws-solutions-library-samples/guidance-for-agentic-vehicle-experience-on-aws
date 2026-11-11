/**
 * CDK unit tests for VsaRsaStack (Task 2.1).
 *
 * Assertions:
 *   (a) DynamoDB table exists with correct name and NEW_AND_OLD_IMAGES stream.
 *   (b) status-createdAt-index GSI exists.
 *   (c) Consumer Lambda exists with correct function name.
 *   (d) DynamoEventSource mapping has finite retryAttempts (≠ -1 / infinite).
 *   (e) IAM: consumer role has NO wildcard Resource, NO dynamodb:* action.
 *   (f) IAM: consumer role has presence-by-ACTION assertion for dynamodb:UpdateItem
 *       AND dynamodb:GetRecords (the two critical scoped grants).
 *
 * Mutation results (to be recorded after running mutations):
 *
 * Mutation A — Resource: "*" (no-wildcard assertion):
 *   Procedure: Change RsaDispatchTableReadWrite resources to ['*'], run tests.
 *   Result: test_consumer_role_no_wildcard_resource FAILS as expected.
 *   Recorded: <date>
 *
 * Mutation B — dynamodb:* action (no-dynamodb-star assertion):
 *   Procedure: Change RsaDispatchTableReadWrite actions to ['dynamodb:*'], run tests.
 *   Result: test_consumer_role_no_dynamodb_star FAILS as expected.
 *   Recorded: <date>
 */

import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { VsaRsaStack } from '../lib/vsa-rsa-stack';
import {
  LAMBDA_BASIC_EXECUTION_ROLE_ARN,
  actionCovers,
  actionsOf,
  allIamStatements,
  allRoleIds,
  logicalIdOf,
  malformedForBound,
  resourcesOf,
  roleIdOfFunction,
  roleManagedPolicyArns,
  roleStatements,
} from './helpers/iam-surface';

// ─── Stack fixture ─────────────────────────────────────────────────────────────

let stackCount = 0;

function makeStack(stage = 'staging'): { stack: VsaRsaStack; template: Template } {
  const id = ++stackCount;
  const app = new cdk.App();
  const env = { account: '123456789012', region: 'us-west-2' };

  const stack = new VsaRsaStack(app, `vsa-${stage}-rsa-${id}`, {
    env,
    stage,
    // F5.1: pass a non-blank literal so the test fixture creates an
    // AWS::SNS::Subscription (required for the subscription-count assertion).
    rsaAlarmEmail: {
      literal: 'operator@example.invalid',
      parameterName: `/cvx/${stage}/rsa-alarm-email`,
    },
  });

  return { stack, template: Template.fromStack(stack) };
}

// ─── (a) DynamoDB table ────────────────────────────────────────────────────────

describe('RSA dispatch table', () => {
  const { template } = makeStack();

  test('table has correct name', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-rsa-dispatch',
    });
  });

  test('table has NEW_AND_OLD_IMAGES stream', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-rsa-dispatch',
      StreamSpecification: {
        StreamViewType: 'NEW_AND_OLD_IMAGES',
      },
    });
  });

  test('table has RETAIN deletion policy', () => {
    template.hasResource('AWS::DynamoDB::Table', {
      DeletionPolicy: 'Retain',
      Properties: {
        TableName: 'vsa-staging-rsa-dispatch',
      },
    });
  });
});

// ─── (b) GSI ──────────────────────────────────────────────────────────────────

describe('status-createdAt-index GSI', () => {
  const { template } = makeStack();

  test('GSI exists with correct name', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-rsa-dispatch',
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: 'status-createdAt-index',
          KeySchema: Match.arrayWith([
            { AttributeName: 'status',    KeyType: 'HASH' },
            { AttributeName: 'createdAt', KeyType: 'RANGE' },
          ]),
        }),
      ]),
    });
  });
});

// ─── (c) Consumer Lambda ──────────────────────────────────────────────────────

describe('RSA dispatch consumer Lambda', () => {
  const { template } = makeStack();

  test('consumer Lambda has correct function name', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'vsa-staging-rsa-dispatch-consumer',
    });
  });

  test('consumer Lambda handler is handler.handler', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'vsa-staging-rsa-dispatch-consumer',
      Handler: 'handler.handler',
    });
  });

  test('consumer Lambda has RSA_DISPATCH_TABLE env var', () => {
    // The table name is a CloudFormation Ref, so we match on the key being present.
    const functions = template.findResources('AWS::Lambda::Function', {
      Properties: {
        FunctionName: 'vsa-staging-rsa-dispatch-consumer',
      },
    });
    const fnKeys = Object.keys(functions);
    expect(fnKeys.length).toBe(1);
    const envVars = functions[fnKeys[0]].Properties?.Environment?.Variables ?? {};
    expect(envVars).toHaveProperty('RSA_DISPATCH_TABLE');
  });

  test('consumer Lambda has VSA_RSA_CLIENT=fake', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'vsa-staging-rsa-dispatch-consumer',
      Environment: {
        Variables: Match.objectLike({
          VSA_RSA_CLIENT: 'fake',
        }),
      },
    });
  });
});

// ─── (d) DynamoEventSource — finite retryAttempts ─────────────────────────────

describe('DynamoEventSource mapping', () => {
  const { template } = makeStack();

  test('event source mapping exists for the RSA table stream', () => {
    template.hasResourceProperties('AWS::Lambda::EventSourceMapping', {
      FunctionName: Match.objectLike({ Ref: Match.anyValue() }),
      StartingPosition: 'TRIM_HORIZON',
    });
  });

  test('retryAttempts is finite (not -1)', () => {
    // Find the event source mapping for the RSA dispatch consumer.
    const mappings = template.findResources('AWS::Lambda::EventSourceMapping');
    const rsaMappings = Object.values(mappings).filter(
      (m: Record<string, unknown>) => {
        const props = (m as Record<string, unknown>)['Properties'] as Record<string, unknown> | undefined;
        if (!props) return false;
        // The ESM has a BisectBatchOnFunctionError or MaximumRetryAttempts key.
        return 'MaximumRetryAttempts' in (props as Record<string, unknown>);
      },
    );

    expect(rsaMappings.length).toBeGreaterThanOrEqual(1);
    for (const mapping of rsaMappings) {
      const props = (mapping as Record<string, unknown>)['Properties'] as Record<string, unknown>;
      const retryAttempts = props['MaximumRetryAttempts'] as number;
      expect(retryAttempts).not.toBe(-1);
      expect(retryAttempts).toBeGreaterThanOrEqual(1);
    }
  });

  test('event source mapping has a DestinationConfig (DLQ wired)', () => {
    const mappings = template.findResources('AWS::Lambda::EventSourceMapping');
    const hasDlq = Object.values(mappings).some(
      (m: Record<string, unknown>) => {
        const props = (m as Record<string, unknown>)['Properties'] as Record<string, unknown> | undefined;
        return props && 'DestinationConfig' in (props as Record<string, unknown>);
      },
    );
    expect(hasDlq).toBe(true);
  });
});

// ─── (e) IAM: no wildcard Resource, no dynamodb:* ────────────────────────────
//
// Task 2.1 mandates: widen the consumer's IAM Resource to "*" → MUST fail.
// widen action set to dynamodb:* → MUST fail.
// These tests assert the SCOPE, not just presence.

describe('Consumer IAM — narrowness assertions', () => {
  const { template } = makeStack();

  /**
   * Every statement attached to any role in the stack, through every channel
   * the template can show: `AWS::IAM::Policy`, `AWS::IAM::ManagedPolicy`, and
   * inline role `Policies`. F12.1: the local collector this replaced skipped
   * `AWS::IAM::ManagedPolicy`, so a `dynamodb:*`-on-`*` statement attached that
   * way passed both narrowness tests. `ManagedPolicyArns` cannot be read; the
   * F12.1 test below pins them for every role.
   */
  function getAllIamStatements(template: Template): Record<string, unknown>[] {
    return allIamStatements(template);
  }

  test('consumer role has no wildcard Resource ("*")', () => {
    const statements = getAllIamStatements(template);
    // Filter to only DynamoDB data-plane actions (not ListStreams which requires *
    // at account level, and not CloudWatch Logs which also requires * for some ops).
    const wildcardResources = statements.filter(s => {
      const actions = s['Action'];
      const res = s['Resource'];
      const hasWildcardResource = (res === '*' || (Array.isArray(res) && (res as string[]).includes('*')));
      if (!hasWildcardResource) return false;
      // Allowed to have Resource: * for:
      //   - dynamodb:ListStreams (AWS requirement — table-scoped list is not supported)
      //   - logs:* (log group retention policies require * at the account level)
      //
      // F4.9: sts:, kms:, xray: had ZERO matching statements in this stack and
      // were removed. A kms:Decrypt on Resource:"*" would pass silently under a
      // test named "no wildcard Resource" — a test promising more than it checks.
      // See review Cycle 4 W7.
      const ALLOWED_WILDCARD_PREFIXES = ['logs:'];
      const normalizeActions = (a: unknown): string[] => {
        if (typeof a === 'string') return [a];
        if (Array.isArray(a)) return a as string[];
        return [];
      };
      const actionList = normalizeActions(actions);
      // All actions must be in the allowed-wildcard list, or be dynamodb:ListStreams
      return actionList.some(action => {
        const actionLower = action.toLowerCase();
        if (actionLower === 'dynamodb:liststreams') return false; // expected *
        if (ALLOWED_WILDCARD_PREFIXES.some(prefix => actionLower.startsWith(prefix))) return false;
        return true; // this action has wildcard resource AND is not exempted
      });
    });
    expect(wildcardResources).toHaveLength(0);
  });

  test('consumer role has no dynamodb:* action', () => {
    // F12.1: an action "covers" dynamodb:* when it is dynamodb:* or `*`. The
    // literal-string check this replaced let `Action: "*"` through.
    const statements = getAllIamStatements(template);
    const dynamoStar = statements.filter(s =>
      actionsOf(s).some(a => actionCovers(a, 'dynamodb:*')),
    );
    expect(dynamoStar).toHaveLength(0);
  });

  // F12.1 (review Cycle 3, 3.2 W2 cross-cutting): the one channel no collector
  // can read. Every role in this stack carries AWSLambdaBasicExecutionRole or
  // nothing (the scheduler role). An AWS-managed AmazonDynamoDBFullAccess on
  // any role → this test MUST fail.
  test('every role in the stack carries only AWSLambdaBasicExecutionRole, or no managed policy', () => {
    const roleIds = allRoleIds(template);
    expect(roleIds.length).toBeGreaterThanOrEqual(3); // consumer, advancer, scheduler (+ log retention)
    for (const id of roleIds) {
      const arns = roleManagedPolicyArns(template, id);
      for (const arn of arns) expect(arn).toEqual(LAMBDA_BASIC_EXECUTION_ROLE_ARN);
      expect(arns.length).toBeLessThanOrEqual(1);
    }
    const consumerRoleId = roleIdOfFunction(template, 'vsa-staging-rsa-dispatch-consumer');
    expect(roleManagedPolicyArns(template, consumerRoleId)).toEqual([LAMBDA_BASIC_EXECUTION_ROLE_ARN]);
  });

  // F14.2 (review Cycle 5, 3.2 S1): an attachment that names a role by its
  // literal physical name (Role.fromRoleName, CfnRolePolicy roleName: '<name>')
  // is collected stack-wide but attributed to no role, so per-role bounds miss
  // it. Every attachment must resolve to a role in this template.
  // Mutations SC12 (iam.Policy on Role.fromRoleName), SC13 (CfnRolePolicy by
  // name) → this test MUST fail.
  test('every IAM policy attachment in the stack names a role defined in the stack (F14.2)', () => {
    const roleIds = new Set(allRoleIds(template));
    const resources = template.toJSON().Resources as Record<string, Record<string, unknown>>;
    let attachments = 0;
    for (const [id, res] of Object.entries(resources)) {
      const props = (res['Properties'] as Record<string, unknown>) ?? {};
      let refs: unknown[] = [];
      if (res['Type'] === 'AWS::IAM::Policy' || res['Type'] === 'AWS::IAM::ManagedPolicy') {
        refs = (props['Roles'] as unknown[]) ?? [];
      } else if (res['Type'] === 'AWS::IAM::RolePolicy') {
        refs = [props['RoleName']];
      } else {
        continue;
      }
      for (const r of refs) {
        attachments++;
        const target = logicalIdOf(r);
        expect({ policy: id, role: target !== null && roleIds.has(target) }).toEqual({ policy: id, role: true });
      }
    }
    expect(attachments).toBeGreaterThanOrEqual(3); // anti-vacuity: consumer, advancer, scheduler (+ log retention)
  });

  test('every statement in the stack is a plain Allow with an Action list (F12.1)', () => {
    for (const s of getAllIamStatements(template)) {
      expect(malformedForBound(s)).toEqual([]);
    }
  });

  // ── F4.9: Anti-vacuity for ALLOWED_WILDCARD_PREFIXES ──────────────────
  //
  // Review Cycle 4 W7: the exemption list previously included sts:, kms:,
  // xray: with ZERO matching statements — a kms:Decrypt on Resource:"*"
  // would pass the guard above without being flagged. These two tests
  // ensure (a) every remaining exemption still matches a real statement, and
  // (b) the exemption set has not silently grown to cover everything.

  test('every allowed wildcard prefix matches at least one real statement (anti-vacuity)', () => {
    // F4.9: the only remaining prefix is 'logs:'. Assert it actually matches
    // a statement in the template so a stale exemption fails and must be deleted.
    const ALLOWED_WILDCARD_PREFIXES = ['logs:'];

    const statements = getAllIamStatements(template);
    const wildcardStatements = statements.filter(s => {
      const res = s['Resource'];
      return (res === '*' || (Array.isArray(res) && (res as string[]).includes('*')));
    });

    for (const prefix of ALLOWED_WILDCARD_PREFIXES) {
      const matchesAtLeastOne = wildcardStatements.some(s => {
        const actions = s['Action'];
        const actionList: string[] = typeof actions === 'string'
          ? [actions]
          : Array.isArray(actions) ? (actions as string[]) : [];
        return actionList.some(a => a.toLowerCase().startsWith(prefix));
      });
      expect(matchesAtLeastOne).toBe(true); // fails if prefix has no matching statement
    }
  });

  test('wildcard Resource exemptions have not grown to cover all wildcard statements (anti-vacuity)', () => {
    // If every wildcard statement is exempted, the no-wildcard-resource test
    // is vacuously satisfied. Pin that at least one non-exempted check fires
    // when we introduce a non-logs wildcard statement (i.e. the guard has teeth).
    //
    // This test does NOT mutate the stack — it verifies that the exemption set
    // is not a superset of all wildcard actions in the real template.
    // The mutation that adds kms:Decrypt on Resource:"*" is run manually
    // (see decisions-fix4-cdk.md).
    const ALLOWED_WILDCARD_PREFIXES = ['logs:'];

    const statements = getAllIamStatements(template);
    const wildcardStatements = statements.filter(s => {
      const res = s['Resource'];
      return (res === '*' || (Array.isArray(res) && (res as string[]).includes('*')));
    });

    const nonExemptWildcardActions = wildcardStatements.flatMap(s => {
      const actions = s['Action'];
      const actionList: string[] = typeof actions === 'string'
        ? [actions]
        : Array.isArray(actions) ? (actions as string[]) : [];
      return actionList.filter(a => {
        const al = a.toLowerCase();
        if (al === 'dynamodb:liststreams') return false; // also exempt
        return !ALLOWED_WILDCARD_PREFIXES.some(p => al.startsWith(p));
      });
    });

    // Expect all wildcard statements are either dynamodb:ListStreams or logs:*
    // — i.e., no non-exempted action has Resource:"*" in the real template.
    // If this fails it means a new wildcard-resource statement was added that
    // is neither exempted nor caught by the no-wildcard-resource test.
    expect(nonExemptWildcardActions).toHaveLength(0);
  });
});

// ─── (f) IAM presence-by-ACTION assertions ────────────────────────────────────
//
// Presence tests alone do NOT count — a statement that existed could be
// deleted silently and the above narrowness tests would still pass. These
// assertions pin that the specific actions exist, so deletion fails them.

describe('Consumer IAM — presence-by-action pins', () => {
  const { template } = makeStack();

  function allActions(template: Template): Set<string> {
    const allStmts: Record<string, unknown>[] = [];
    const roles = template.findResources('AWS::IAM::Role');
    for (const role of Object.values(roles)) {
      const roleProps = ((role as {Properties?: Record<string, unknown>}).Properties) ?? {};
      const policies = (roleProps['Policies'] as unknown[]) ?? [];
      if (!Array.isArray(policies)) continue;
      for (const policy of policies) {
        const doc = ((policy as Record<string, unknown>)['PolicyDocument']) as Record<string, unknown> | undefined;
        const stmts = (doc?.['Statement'] as Record<string, unknown>[]) ?? [];
        if (Array.isArray(stmts)) allStmts.push(...stmts);
      }
    }
    const iamPolicies = template.findResources('AWS::IAM::Policy');
    for (const p of Object.values(iamPolicies)) {
      const pProps = ((p as {Properties?: Record<string, unknown>}).Properties) ?? {};
      const doc = (pProps['PolicyDocument'] as Record<string, unknown>) ?? {};
      const stmts = (doc['Statement'] as Record<string, unknown>[]) ?? [];
      if (Array.isArray(stmts)) allStmts.push(...stmts);
    }
    const actions = new Set<string>();
    for (const stmt of allStmts) {
      const a = stmt['Action'];
      if (typeof a === 'string') actions.add(a);
      if (Array.isArray(a)) (a as string[]).forEach(v => actions.add(v));
    }
    return actions;
  }

  test('dynamodb:UpdateItem grant exists (consumer writeback)', () => {
    const actions = allActions(template);
    expect(actions.has('dynamodb:UpdateItem')).toBe(true);
  });

  test('dynamodb:GetRecords grant exists (stream read)', () => {
    const actions = allActions(template);
    expect(actions.has('dynamodb:GetRecords')).toBe(true);
  });

  test('dynamodb:GetItem grant exists (idempotency pre-check)', () => {
    const actions = allActions(template);
    expect(actions.has('dynamodb:GetItem')).toBe(true);
  });
});


// ─── (g) DLQ alarm with non-empty AlarmActions (F4.4) ─────────────────────────
//
// F4.4 (review Cycle 4 W2): "a DLQ with no alarm is a silent failure".
// Both prior Tier 2 agents shipped with empty AlarmActions and one failed
// silently for two weeks. This test asserts:
//   1. A CloudWatch Alarm exists on the DLQ queue depth metric.
//   2. At least one AlarmAction is configured (non-empty AlarmActions list).
//
// Mutation: empty the AlarmActions list → this test MUST fail.

describe('DLQ depth alarm — F4.4', () => {
  const { template } = makeStack();

  test('DLQ depth CloudWatch alarm exists', () => {
    // The alarm should reference a metric on the DLQ queue.
    const alarms = template.findResources('AWS::CloudWatch::Alarm');
    const dlqAlarms = Object.values(alarms).filter((alarm) => {
      const props = (alarm as Record<string, unknown>).Properties as Record<string, unknown> | undefined;
      if (!props) return false;
      const alarmName = props['AlarmName'] as string | undefined;
      return typeof alarmName === 'string' && alarmName.includes('rsa-dispatch-dlq');
    });
    expect(dlqAlarms.length).toBeGreaterThanOrEqual(1);
  });

  test('DLQ depth alarm has at least one AlarmAction (non-empty AlarmActions)', () => {
    // This is the mutation guard: empty AlarmActions → this test fails.
    const alarms = template.findResources('AWS::CloudWatch::Alarm');
    const dlqAlarms = Object.values(alarms).filter((alarm) => {
      const props = (alarm as Record<string, unknown>).Properties as Record<string, unknown> | undefined;
      if (!props) return false;
      const alarmName = props['AlarmName'] as string | undefined;
      return typeof alarmName === 'string' && alarmName.includes('rsa-dispatch-dlq');
    });

    expect(dlqAlarms.length).toBeGreaterThanOrEqual(1);

    for (const alarm of dlqAlarms) {
      const props = (alarm as Record<string, unknown>).Properties as Record<string, unknown>;
      const alarmActions = props['AlarmActions'];
      // AlarmActions must be a non-empty array.
      expect(Array.isArray(alarmActions)).toBe(true);
      expect((alarmActions as unknown[]).length).toBeGreaterThanOrEqual(1);
    }
  });

  test('DLQ depth alarm metric targets the DLQ queue (not a different resource)', () => {
    // Anti-vacuity: the alarm must target a SQS queue metric, not be wired to
    // an unrelated resource. The metric dimension 'QueueName' must be present.
    const alarms = template.findResources('AWS::CloudWatch::Alarm');
    const dlqAlarms = Object.values(alarms).filter((alarm) => {
      const props = (alarm as Record<string, unknown>).Properties as Record<string, unknown> | undefined;
      if (!props) return false;
      const alarmName = props['AlarmName'] as string | undefined;
      return typeof alarmName === 'string' && alarmName.includes('rsa-dispatch-dlq');
    });

    for (const alarm of dlqAlarms) {
      const props = (alarm as Record<string, unknown>).Properties as Record<string, unknown>;
      const namespace = props['Namespace'] as string | undefined;
      // SQS CloudWatch namespace
      expect(namespace).toBe('AWS/SQS');
    }
  });
});


// ─── (h) F5.1: SNS subscription exists (alarm actually has a recipient) ───────
//
// F5.1 (review Cycle 5): F4.4 gave the alarm a non-empty AlarmActions but
// produced 0 AWS::SNS::Subscription resources. The alarm fired; nobody was
// notified. This is the portfolio's third instance of the same defect.
//
// Assert: the template contains at least one AWS::SNS::Subscription.
// The fixture passes a non-blank rsaAlarmEmail literal so the subscription
// is created at synth time.
//
// Mutation: remove the EmailSubscription from the alarm block → this test MUST fail.
// An alarm nobody receives is the failure mode; assert the SUBSCRIBER, not the alarm.

describe('DLQ alarm operator subscription — F5.1', () => {
  const { template } = makeStack();

  test('template contains at least one AWS::SNS::Subscription (alarm has a recipient)', () => {
    // This is the F5.1 mutation guard. Remove the subscription → count drops to 0.
    const subs = template.findResources('AWS::SNS::Subscription');
    expect(Object.keys(subs).length).toBeGreaterThanOrEqual(1);
  });

  test('the subscription points at the RSA operator alert topic', () => {
    const subs = template.findResources('AWS::SNS::Subscription');
    // At least one subscription's TopicArn references the RSA alert topic construct.
    const rsaSubs = Object.values(subs).filter((sub) => {
      const props = (sub as Record<string, unknown>).Properties as Record<string, unknown> | undefined;
      if (!props) return false;
      const topicArn = JSON.stringify(props['TopicArn']);
      return topicArn.includes('RsaOperatorAlertTopic');
    });
    expect(rsaSubs.length).toBeGreaterThanOrEqual(1);
  });
});

// ─── (i) F5.7a: FunctionResponseTypes on the event source ─────────────────────
//
// F5.7a (review Cycle 5): F4.3 correctly changed the handler to return
// batchItemFailures — but nothing guards the CDK event source side. Deleting
// `reportBatchItemFailures: true` from the event source makes CDK omit
// `FunctionResponseTypes: ['ReportBatchItemFailures']` from the ESM, and
// Lambda silently treats every handler response as complete-batch-success.
// All 65 Python + 27 jest tests would still pass.
//
// Mutation: remove `reportBatchItemFailures: true` from the DynamoEventSource
//   → the template no longer has FunctionResponseTypes → this test MUST fail.

describe('Event source FunctionResponseTypes — F5.7a', () => {
  const { template } = makeStack();

  test('DynamoEventSource has FunctionResponseTypes: [ReportBatchItemFailures]', () => {
    const mappings = template.findResources('AWS::Lambda::EventSourceMapping');
    // Find the mapping for the RSA dispatch consumer (it has MaximumRetryAttempts set).
    const rsaMappings = Object.values(mappings).filter((m) => {
      const props = (m as Record<string, unknown>).Properties as Record<string, unknown> | undefined;
      return props && 'MaximumRetryAttempts' in props;
    });
    expect(rsaMappings.length).toBeGreaterThanOrEqual(1);

    for (const mapping of rsaMappings) {
      const props = (mapping as Record<string, unknown>).Properties as Record<string, unknown>;
      const responseTypes = props['FunctionResponseTypes'] as string[] | undefined;
      // Mutation guard: if reportBatchItemFailures is removed, this array is absent
      // or empty, and the test fails.
      expect(Array.isArray(responseTypes)).toBe(true);
      expect((responseTypes as string[])).toContain('ReportBatchItemFailures');
    }
  });
});


// ─── (j) F6.5: none → zero subscriptions (negative control) ──────────────────
//
// F6.5 (review Cycle 6 W6-3): `CVX_RSA_ALARM_EMAIL=none` previously synthesized
// `Endpoint: none`, a phantom subscriber that looks like coverage and provides none.
// The existing F5.1 test only measures the email-path fixture and never exercises
// the `none` path — so the phantom subscriber never triggered a test failure.
//
// This test uses a separate stack instance with `literal: 'none'` and asserts:
//   1. The template contains ZERO `AWS::SNS::Subscription` resources.
//   2. The SNS topic itself still exists (the alarm fires, but nobody gets it).
//
// Mutation gate: revert to the old `rsaAlarmEmail.length > 0` check (removing
// `&& rsaAlarmEmail !== 'none'`) → the count becomes 1 → this test MUST fail.

describe('CVX_RSA_ALARM_EMAIL=none — negative control (F6.5)', () => {
  // Build a dedicated fixture with `literal: 'none'` — the explicit opt-out.
  const id = ++stackCount;
  const app = new cdk.App();
  const stack = new VsaRsaStack(app, `vsa-staging-rsa-none-${id}`, {
    env: { account: '123456789012', region: 'us-west-2' },
    stage: 'staging',
    rsaAlarmEmail: {
      literal: 'none',
      parameterName: '/cvx/staging/rsa-alarm-email',
    },
  });
  const noneTemplate = Template.fromStack(stack);

  test('none literal produces ZERO AWS::SNS::Subscription resources', () => {
    // This is the F6.5 mutation guard.
    // If the `&& rsaAlarmEmail !== "none"` branch check is removed, one
    // subscription with Endpoint:"none" is created and count becomes 1.
    const subs = noneTemplate.findResources('AWS::SNS::Subscription');
    expect(Object.keys(subs).length).toBe(0);
  });

  test('SNS topic still exists when email=none (alarm topic present, no subscriber)', () => {
    // The topic must remain — the alarm still fires, the operator is just not
    // subscribed. Removing the topic would drop the AlarmAction entirely.
    noneTemplate.hasResourceProperties('AWS::SNS::Topic', {
      TopicName: 'vsa-staging-rsa-operator-alerts',
    });
  });
});


// ─── (k) F6.7: BisectBatchOnFunctionError pinned ─────────────────────────────
//
// F6.7 (review Cycle 6 W6-6): Cycle 6 judged F5.5's `raise` safe BECAUSE
// bisection isolates the malformed record — but `grep BisectBatch
// infrastructure/test/` found only a comment, never an assertion.  The safety
// argument rests entirely on an unasserted flag.
//
// Delete `bisectBatchOnError: true` from the DynamoEventSource props → this
// test MUST fail. With it absent, the same `raise` drops every record in the
// batch after three attempts, with a green suite.

describe('BisectBatchOnFunctionError — F6.7', () => {
  const { template } = makeStack();

  test('DynamoEventSource has BisectBatchOnFunctionError: true', () => {
    // Collect all event source mappings that belong to the RSA consumer
    // (identified by having MaximumRetryAttempts — set only on the RSA ESM).
    const mappings = template.findResources('AWS::Lambda::EventSourceMapping');
    const rsaMappings = Object.values(mappings).filter((m) => {
      const props = (m as Record<string, unknown>).Properties as Record<string, unknown> | undefined;
      return props && 'MaximumRetryAttempts' in props;
    });
    expect(rsaMappings.length).toBeGreaterThanOrEqual(1);

    for (const mapping of rsaMappings) {
      const props = (mapping as Record<string, unknown>).Properties as Record<string, unknown>;
      // Mutation gate: remove `bisectBatchOnError: true` from DynamoEventSource
      // → CloudFormation omits BisectBatchOnFunctionError → this assertion fails.
      const bisect = props['BisectBatchOnFunctionError'] as boolean | undefined;
      expect(bisect).toBe(true);
    }
  });
});



// ─── (l) Fix Group 13 item 2: the dispatch table's own key ───────────────────
//
// F8.2 reused /cvx/{stage}/cms-kms-key-arn. On staging that parameter names the
// AWS-managed alias/aws/dynamodb key, so the template said CUSTOMER_MANAGED and
// the live table was on a key whose policy admits every principal in the
// account through DynamoDB. F8.2's test only asserted KMSMasterKeyId was
// truthy, which that ARN satisfied (issues/2026-09-25-rsa-dispatch-table-on-aws-
// managed-key/). These tests pin the key to one created in this stack, and pin
// every KMS grant in the stack to that key.
//
// Mutations (each MUST fail a test below):
//   K1 table back on kms.Key.fromKeyArn(<external ARN>)  -> KMSMasterKeyId test
//   K2 deletionProtection: false                         -> deletion protection test
//   K3 enableKeyRotation: false                          -> rotation test
//   K4 key removalPolicy DESTROY                         -> RETAIN test
//   K5 delete RsaDispatchConsumerKmsAccess               -> consumer grant test
//   K6 consumer grant on '*'                             -> consumer grant test

describe('rsa-dispatch table key and deletion protection (Fix Group 13 item 2)', () => {
  const { template } = makeStack();

  const keyLogicalId = (): string => {
    const keys = template.findResources('AWS::KMS::Key');
    const ids = Object.keys(keys);
    expect(ids).toHaveLength(1);
    return ids[0];
  };
  const tableProps = (): Record<string, any> => {
    const tables = template.findResources('AWS::DynamoDB::Table', {
      Properties: { TableName: 'vsa-staging-rsa-dispatch' },
    });
    const ids = Object.keys(tables);
    expect(ids).toHaveLength(1);
    return tables[ids[0]].Properties;
  };

  test('the stack creates exactly one KMS key, with rotation on', () => {
    const keys = template.findResources('AWS::KMS::Key');
    expect(Object.keys(keys)).toHaveLength(1);
    expect(keys[keyLogicalId()].Properties.EnableKeyRotation).toBe(true);
  });

  test('the key is retained on stack deletion and replacement', () => {
    const key = template.findResources('AWS::KMS::Key')[keyLogicalId()];
    expect(key.DeletionPolicy).toBe('Retain');
    expect(key.UpdateReplacePolicy).toBe('Retain');
  });

  test('alias/vsa-staging-rsa-dispatch targets the key', () => {
    template.hasResourceProperties('AWS::KMS::Alias', {
      AliasName: 'alias/vsa-staging-rsa-dispatch',
      TargetKeyId: { 'Fn::GetAtt': [keyLogicalId(), 'Arn'] },
    });
  });

  test('the table is encrypted with the in-stack key, not an external ARN', () => {
    const sse = tableProps().SSESpecification;
    expect(sse).toEqual({
      SSEEnabled: true,
      SSEType: 'KMS',
      KMSMasterKeyId: { 'Fn::GetAtt': [keyLogicalId(), 'Arn'] },
    });
  });

  test('the table has deletion protection on', () => {
    expect(tableProps().DeletionProtectionEnabled).toBe(true);
  });

  test('the stack no longer reads the shared cms-kms-key-arn parameter', () => {
    expect(JSON.stringify(template.toJSON())).not.toContain('cms-kms-key-arn');
  });

  test('consumer role: RsaDispatchConsumerKmsAccess is exact', () => {
    const roleId = roleIdOfFunction(template, 'vsa-staging-rsa-dispatch-consumer');
    const matches = roleStatements(template, roleId).filter(
      (st) => st['Sid'] === 'RsaDispatchConsumerKmsAccess',
    );
    expect(matches).toHaveLength(1);
    const s = matches[0];
    expect(s['Resource']).toEqual({ 'Fn::GetAtt': [keyLogicalId(), 'Arn'] });
    expect(s['Condition']).toEqual({
      StringEquals: { 'kms:ViaService': 'dynamodb.us-west-2.amazonaws.com' },
    });
    expect(([] as string[]).concat(s['Action'] as string | string[]).sort()).toEqual([
      'kms:Decrypt', 'kms:DescribeKey', 'kms:GenerateDataKey',
    ]);
  });

  test('every KMS grant in the stack names the in-stack key and nothing else', () => {
    const keyRef = { 'Fn::GetAtt': [keyLogicalId(), 'Arn'] };
    let kmsStatements = 0;
    for (const st of allIamStatements(template)) {
      const acts = ([] as string[]).concat((st['Action'] ?? []) as string | string[]);
      if (!acts.some((a) => a.toLowerCase().startsWith('kms:'))) continue;
      kmsStatements++;
      const res = ([] as unknown[]).concat(st['Resource'] as unknown);
      expect(res).toEqual([keyRef]);
    }
    // Consumer explicit + consumer stream-read (CDK) + advancer.
    expect(kmsStatements).toBeGreaterThanOrEqual(3);
  });
});


// ─── (m) Task 3.2: Lifecycle advancer Lambda ─────────────────────────────────
//
// Asserts the advancer Lambda, scheduler, IAM, and alarms added by Task 3.2.
// All tests use the existing makeStack() fixture (rsaAlarmEmail=operator@...).

describe('Lifecycle advancer Lambda — Task 3.2', () => {
  const { template } = makeStack();

  test('advancer Lambda has correct function name', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'vsa-staging-rsa-lifecycle-advancer',
    });
  });

  test('advancer Lambda handler is lifecycle.handler', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'vsa-staging-rsa-lifecycle-advancer',
      Handler: 'lifecycle.handler',
    });
  });

  test('advancer Lambda has RSA_DISPATCH_TABLE env var', () => {
    const functions = template.findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: 'vsa-staging-rsa-lifecycle-advancer' },
    });
    const keys = Object.keys(functions);
    expect(keys.length).toBe(1);
    const envVars = functions[keys[0]].Properties?.Environment?.Variables ?? {};
    expect(envVars).toHaveProperty('RSA_DISPATCH_TABLE');
  });

  test('advancer Lambda has timing env vars with non-empty defaults', () => {
    const functions = template.findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: 'vsa-staging-rsa-lifecycle-advancer' },
    });
    const keys = Object.keys(functions);
    expect(keys.length).toBe(1);
    const envVars = functions[keys[0]].Properties?.Environment?.Variables ?? {};
    // Default values must be numeric strings, not empty or 'abc'
    expect(envVars['RSA_SIM_EN_ROUTE_AFTER_SECONDS']).toMatch(/^[1-9][0-9]*$/);
    expect(envVars['RSA_SIM_ARRIVED_AFTER_SECONDS']).toMatch(/^[1-9][0-9]*$/);
    expect(envVars['RSA_REQUESTED_STALE_AFTER_SECONDS']).toMatch(/^[1-9][0-9]*$/);
  });
});

// ─── (n) Task 3.2: Scheduler schedule ────────────────────────────────────────

describe('EventBridge Scheduler schedule — Task 3.2', () => {
  const { template } = makeStack();

  test('a CfnSchedule resource exists for the advancer', () => {
    // aws-cdk-lib/aws-scheduler Schedule renders as AWS::Scheduler::Schedule
    const schedules = template.findResources('AWS::Scheduler::Schedule');
    expect(Object.keys(schedules).length).toBeGreaterThanOrEqual(1);
  });

  test('schedule has rate(1 minute) expression', () => {
    const schedules = template.findResources('AWS::Scheduler::Schedule');
    const rateSchedules = Object.values(schedules).filter((s) => {
      const props = (s as Record<string, unknown>).Properties as Record<string, unknown> | undefined;
      if (!props) return false;
      const expr = props['ScheduleExpression'] as string | undefined;
      return typeof expr === 'string' && expr.includes('rate(1 minute)');
    });
    expect(rateSchedules.length).toBeGreaterThanOrEqual(1);
  });
});

// ─── (o) Task 3.2: IAM — advancer narrowness assertions ──────────────────────
//
// Mutation guard (b-iii): add dynamodb:Scan → test MUST fail.

describe('Lifecycle advancer IAM — narrowness (Task 3.2)', () => {
  const { template } = makeStack();

  // F12.1: the shared collector (every channel the template shows) replaces a
  // local copy that skipped AWS::IAM::ManagedPolicy.
  function getAllIamStatements(tmpl: Template): Record<string, unknown>[] {
    return allIamStatements(tmpl);
  }

  test('no dynamodb:Scan in any IAM statement (mutation guard b-iii)', () => {
    // Mutation: add dynamodb:Scan to advancer policy → this test FAILS.
    // G3-D2 mandates GSI Query only; no Scan.
    const statements = getAllIamStatements(template);
    const scanStatements = statements.filter((s) => {
      const a = s['Action'];
      const normalise = (x: unknown): string[] =>
        typeof x === 'string' ? [x] : Array.isArray(x) ? (x as string[]) : [];
      return normalise(a).some(
        (action) => action.toLowerCase() === 'dynamodb:scan',
      );
    });
    expect(scanStatements).toHaveLength(0);
  });

  test('dynamodb:Query grant targets GSI ARN (not table ARN)', () => {
    // G3-D2: Query must be on the GSI ARN, not the table ARN.
    const statements = getAllIamStatements(template);
    const queryStmts = statements.filter((s) => {
      const a = s['Action'];
      const normalise = (x: unknown): string[] =>
        typeof x === 'string' ? [x] : Array.isArray(x) ? (x as string[]) : [];
      return normalise(a).some(
        (action) => action.toLowerCase() === 'dynamodb:query',
      );
    });
    // At least one Query statement must exist
    expect(queryStmts.length).toBeGreaterThanOrEqual(1);
    // Every Query statement's Resource must reference an index ARN.
    // CDK renders the GSI ARN as {"Fn::Join":["", [{"Fn::GetAtt":["...","Arn"]}, "/index/..."]]}
    // so we JSON.stringify the Resource and check for '/index/' in the serialised form.
    for (const stmt of queryStmts) {
      const resourceStr = JSON.stringify(stmt['Resource']);
      expect(resourceStr).toMatch(/index\//);
    }
  });
});

// ─── (p) Task 3.2: Advancer alarms (G3-D11) ──────────────────────────────────
//
// Two alarms on rsaAlarmTopic:
//   1. Advancer Lambda Errors ≥ 1 over 5 minutes.
//   2. Reconciliation count ≥ 1 over 5 minutes (from CloudWatch Logs metric filter).
//
// Mutation guard (b-i): empty the AlarmActions → test MUST fail.
// Mutation guard (b-ii): remove the SNS subscription → subscription test fails.

describe('Lifecycle advancer alarms — Task 3.2 (G3-D11)', () => {
  const { template } = makeStack();

  test('advancer error alarm exists', () => {
    const alarms = template.findResources('AWS::CloudWatch::Alarm');
    const advancerAlarms = Object.values(alarms).filter((a) => {
      const props = (a as Record<string, unknown>).Properties as Record<string, unknown> | undefined;
      const name = props?.['AlarmName'] as string | undefined;
      return typeof name === 'string' && name.includes('rsa-lifecycle-advancer-errors');
    });
    expect(advancerAlarms.length).toBeGreaterThanOrEqual(1);
  });

  test('advancer error alarm has non-empty AlarmActions (mutation guard b-i)', () => {
    // Mutation: empty AlarmActions → this test FAILS.
    const alarms = template.findResources('AWS::CloudWatch::Alarm');
    const advancerAlarms = Object.values(alarms).filter((a) => {
      const props = (a as Record<string, unknown>).Properties as Record<string, unknown> | undefined;
      const name = props?.['AlarmName'] as string | undefined;
      return typeof name === 'string' && name.includes('rsa-lifecycle-advancer-errors');
    });
    for (const alarm of advancerAlarms) {
      const props = (alarm as Record<string, unknown>).Properties as Record<string, unknown>;
      const actions = props['AlarmActions'] as unknown[] | undefined;
      expect(Array.isArray(actions)).toBe(true);
      expect((actions as unknown[]).length).toBeGreaterThanOrEqual(1);
    }
  });

  test('reconciliation alarm exists', () => {
    const alarms = template.findResources('AWS::CloudWatch::Alarm');
    const reconcAlarms = Object.values(alarms).filter((a) => {
      const props = (a as Record<string, unknown>).Properties as Record<string, unknown> | undefined;
      const name = props?.['AlarmName'] as string | undefined;
      return typeof name === 'string' && name.includes('rsa-reconciliation-detected');
    });
    expect(reconcAlarms.length).toBeGreaterThanOrEqual(1);
  });

  test('reconciliation alarm has non-empty AlarmActions (mutation guard b-i)', () => {
    const alarms = template.findResources('AWS::CloudWatch::Alarm');
    const reconcAlarms = Object.values(alarms).filter((a) => {
      const props = (a as Record<string, unknown>).Properties as Record<string, unknown> | undefined;
      const name = props?.['AlarmName'] as string | undefined;
      return typeof name === 'string' && name.includes('rsa-reconciliation-detected');
    });
    for (const alarm of reconcAlarms) {
      const props = (alarm as Record<string, unknown>).Properties as Record<string, unknown>;
      const actions = props['AlarmActions'] as unknown[] | undefined;
      expect(Array.isArray(actions)).toBe(true);
      expect((actions as unknown[]).length).toBeGreaterThanOrEqual(1);
    }
  });

  test('each new alarm AlarmActions references the same SNS topic as the Subscription (F9.4)', () => {
    // F9.4: AlarmActions must reference the SAME topic that the SNS Subscription
    // targets. A mismatch would mean the alarm fires but nobody is notified
    // — the exact VFO failure mode this alarm exists to catch.
    //
    // Find the Subscription resource and extract the topic ARN reference,
    // then verify both new alarms' AlarmActions use the same Ref/GetAtt.
    const subs = template.findResources('AWS::SNS::Subscription');
    const subKeys = Object.keys(subs);
    expect(subKeys.length).toBeGreaterThanOrEqual(1);

    // Extract the topic ARN used in the subscription (a Ref or Fn::GetAtt to the Topic)
    const subTopicArn = (subs[subKeys[0]].Properties as Record<string, unknown>)?.['TopicArn'];
    const subTopicArnStr = JSON.stringify(subTopicArn);

    // Find both new alarms (advancer-errors and reconciliation-detected)
    const alarms = template.findResources('AWS::CloudWatch::Alarm');
    const newAlarms = Object.values(alarms).filter((a) => {
      const props = (a as Record<string, unknown>).Properties as Record<string, unknown> | undefined;
      const name = props?.['AlarmName'] as string | undefined;
      return typeof name === 'string' &&
        (name.includes('rsa-lifecycle-advancer-errors') || name.includes('rsa-reconciliation-detected'));
    });
    expect(newAlarms.length).toBe(2);

    for (const alarm of newAlarms) {
      const props = (alarm as Record<string, unknown>).Properties as Record<string, unknown>;
      const actions = props['AlarmActions'] as unknown[];
      // Each action must reference the same logical resource as the subscription's TopicArn
      const actionStr = JSON.stringify(actions);
      // Both the subscription and the alarm action reference the same SNS topic.
      // In CDK-synthesized templates this is typically { "Ref": "<TopicLogicalId>" }
      // in the subscription and { "Ref": "<TopicLogicalId>" } in AlarmActions.
      // We check that the logical ID referenced in the subscription's TopicArn
      // appears somewhere in the alarm's action list.
      const subRef = (subTopicArn as Record<string, unknown>)?.['Ref'] ??
                     ((subTopicArn as Record<string, unknown>)?.['Fn::GetAtt'] as string[] | undefined)?.[0];
      if (subRef) {
        expect(actionStr).toContain(String(subRef));
      } else {
        // Fallback: ensure the topic ARN serialisation matches
        expect(actionStr).toContain(subTopicArnStr.slice(1, -1)); // strip outer quotes
      }
    }
  });

  test('CloudWatch Logs metric filter exists for reconciliation token', () => {
    // Mutation: drop the MetricFilter → reconciliation alarm has no data source.
    const filters = template.findResources('AWS::Logs::MetricFilter');
    expect(Object.keys(filters).length).toBeGreaterThanOrEqual(1);
    const reconcFilters = Object.values(filters).filter((f) => {
      const props = (f as Record<string, unknown>).Properties as Record<string, unknown> | undefined;
      const pattern = props?.['FilterPattern'] as string | undefined;
      return typeof pattern === 'string' && pattern.includes('RSA_RECONCILIATION');
    });
    expect(reconcFilters.length).toBeGreaterThanOrEqual(1);
  });

  test('reconciliation metric filter uses quoted pattern (F9.2 mutation guard)', () => {
    // F9.2: the filter pattern must be the quoted term "[RSA_RECONCILIATION]"
    // (with outer quotes), not the bare unquoted version.
    //
    // Unquoted [RSA_RECONCILIATION] is CloudWatch Logs' space-delimited field
    // syntax — it matches ANY log line with a space-separated first field,
    // which includes every Lambda "lifecycle run complete" line.  This would
    // keep the alarm permanently in ALARM state, making it useless.
    //
    // Mutation guard: restore the unquoted pattern → this test MUST fail.
    const filters = template.findResources('AWS::Logs::MetricFilter');
    const reconcFilters = Object.values(filters).filter((f) => {
      const props = (f as Record<string, unknown>).Properties as Record<string, unknown> | undefined;
      const pattern = props?.['FilterPattern'] as string | undefined;
      return typeof pattern === 'string' && pattern.includes('RSA_RECONCILIATION');
    });
    expect(reconcFilters.length).toBeGreaterThanOrEqual(1);
    for (const filter of reconcFilters) {
      const props = (filter as Record<string, unknown>).Properties as Record<string, unknown>;
      const pattern = props['FilterPattern'] as string;
      // Must be the quoted form: "\"[RSA_RECONCILIATION]\""
      // CDK renders logs.FilterPattern.literal('"[RSA_RECONCILIATION]"') as
      // the string: "[RSA_RECONCILIATION]" (with the outer double-quotes).
      expect(pattern).toBe('"[RSA_RECONCILIATION]"');
    }
  });
});

// ─── (p2) F12.2: errors + reconciliation alarms — subject and trigger pinned ──
//
// Review Cycle 3, 3.2 W1: the tests above pin only that each alarm exists, has
// a non-empty AlarmActions, and correlates with the subscribed topic. These all
// survived at 70/70: errors alarm on the consumer; on Throttles; threshold 1000;
// LessThanThreshold; period 1 day; OKAction dropped; reconciliation threshold
// 1000; its OKAction dropped; filter metricValue '0'; and actionsEnabled: false
// on any advancer alarm. The errors alarm is the only guard for "runs but
// fails every minute" (F9.1 made the handler raise so it could see that); the
// heartbeat cannot, because a failing advancer still has Invocations >= 1.
// Mutations: each of the above → a test below MUST fail.

describe('Advancer errors + reconciliation alarms — subject and trigger pinned (F12.2)', () => {
  const { template } = makeStack();

  const alarmNamed = (fragment: string): Record<string, unknown> => {
    const matches = Object.values(template.findResources('AWS::CloudWatch::Alarm')).filter((a) => {
      const name = ((a as Record<string, unknown>).Properties as Record<string, unknown>)?.['AlarmName'];
      return typeof name === 'string' && name.includes(fragment);
    });
    expect(matches.length).toBe(1);
    return (matches[0] as Record<string, unknown>).Properties as Record<string, unknown>;
  };
  const subscribedTopic = (): unknown => {
    const subs = Object.values(template.findResources('AWS::SNS::Subscription'));
    expect(subs.length).toBe(1);
    return ((subs[0] as Record<string, unknown>).Properties as Record<string, unknown>)['TopicArn'];
  };
  const advancerFnId = (): string => {
    const ids = Object.keys(template.findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: 'vsa-staging-rsa-lifecycle-advancer' },
    }));
    expect(ids.length).toBe(1);
    return ids[0];
  };

  test('F12.2: errors alarm is advancer Errors Sum >= 1 over one 300 s period, both edges to the subscribed topic', () => {
    const p = alarmNamed('rsa-lifecycle-advancer-errors');
    expect(p['Namespace']).toBe('AWS/Lambda');
    expect(p['MetricName']).toBe('Errors');
    expect(p['Dimensions']).toEqual([{ Name: 'FunctionName', Value: { Ref: advancerFnId() } }]);
    expect(p['ComparisonOperator']).toBe('GreaterThanOrEqualToThreshold');
    expect(p['Threshold']).toBe(1);
    expect(p['EvaluationPeriods']).toBe(1);
    expect(p['Period']).toBe(300);
    expect(p['Statistic']).toBe('Sum');
    expect(p['TreatMissingData']).toBe('notBreaching');
    // F13.3 (review Cycle 4 S2): a Unit that disagrees with the published metric
    // leaves the alarm in INSUFFICIENT_DATA; none is set today, so none is pinned.
    expect(p['Unit']).toBeUndefined();
    expect(p['AlarmActions']).toEqual([subscribedTopic()]);
    expect(p['OKActions']).toEqual([subscribedTopic()]);
  });

  test('F12.2: reconciliation alarm is ReconciliationCount Sum >= 1 over one 300 s period, both edges to the subscribed topic', () => {
    const p = alarmNamed('rsa-reconciliation-detected');
    expect(p['Namespace']).toBe('VSA/staging/RSA');
    expect(p['MetricName']).toBe('ReconciliationCount');
    expect(p['Dimensions']).toBeUndefined(); // the filter emits no dimensions; one here would read nothing
    expect(p['ComparisonOperator']).toBe('GreaterThanOrEqualToThreshold');
    expect(p['Threshold']).toBe(1);
    expect(p['EvaluationPeriods']).toBe(1);
    expect(p['Period']).toBe(300);
    expect(p['Statistic']).toBe('Sum');
    expect(p['TreatMissingData']).toBe('notBreaching');
    // F13.3 (review Cycle 4 S2): a Unit that disagrees with the published metric
    // leaves the alarm in INSUFFICIENT_DATA; none is set today, so none is pinned.
    expect(p['Unit']).toBeUndefined();
    expect(p['AlarmActions']).toEqual([subscribedTopic()]);
    expect(p['OKActions']).toEqual([subscribedTopic()]);
  });

  test('F12.2: the reconciliation filter emits 1 per matching line into the metric the alarm reads', () => {
    const filters = Object.values(template.findResources('AWS::Logs::MetricFilter'));
    expect(filters.length).toBe(1);
    const t = (((filters[0] as Record<string, unknown>).Properties as Record<string, unknown>)
      ['MetricTransformations'] as Record<string, unknown>[]);
    expect(t).toEqual([{
      MetricName: 'ReconciliationCount',
      MetricNamespace: 'VSA/staging/RSA',
      MetricValue: '1',
      Unit: 'Count',
    }]);
  });

  test('F12.2: every RSA alarm has actions enabled (ActionsEnabled absent or true)', () => {
    // actionsEnabled: false keeps the alarm changing state while notifying nobody —
    // issues/2026-09-24-tier2-operator-alarms-never-delivered/, where a non-empty
    // AlarmActions "looked sufficient".
    const alarms = Object.values(template.findResources('AWS::CloudWatch::Alarm'));
    expect(alarms.length).toBe(4); // DLQ depth, advancer errors, heartbeat, reconciliation
    for (const a of alarms) {
      const p = (a as Record<string, unknown>).Properties as Record<string, unknown>;
      expect([undefined, true]).toContain(p['ActionsEnabled']);
    }
  });
});

// ─── (q) Task 3.2: CMK grant for advancer (G3-D10) ──────────────────────────
//
// NOT a mutation guard for the advancer's grant (F11.6). This test collects kms:*
// from EVERY role in the stack, and the consumer role carries kms:Decrypt via
// DynamoEventSource, so dropping the advancer's statement leaves it green. The
// advancer-scoped guards are F10.6 / F11.4 below. Kept as a stack-level check.

describe('Lifecycle advancer KMS grant — G3-D10', () => {
  test('advancer role has kms:Decrypt (the table always has its own key)', () => {
    const id = ++stackCount;
    const app = new cdk.App();
    const stack = new VsaRsaStack(app, `vsa-staging-rsa-cmk-adv-${id}`, {
      env: { account: '123456789012', region: 'us-west-2' },
      stage: 'staging',
      rsaAlarmEmail: {
        literal: 'operator@example.invalid',
        parameterName: '/cvx/staging/rsa-alarm-email',
      },
    });
    const cmkTemplate = Template.fromStack(stack);

    // Collect all KMS actions across all IAM statements (shared collector, F12.1)
    const allStmts = allIamStatements(cmkTemplate);

    const kmsActions = new Set<string>();
    for (const stmt of allStmts) {
      const a = stmt['Action'];
      const normalise = (x: unknown): string[] =>
        typeof x === 'string' ? [x] : Array.isArray(x) ? (x as string[]) : [];
      normalise(a)
        .filter((action) => action.toLowerCase().startsWith('kms:'))
        .forEach((action) => kmsActions.add(action));
    }

    // Stack-wide presence only — see the block comment above (F11.6).
    expect(kmsActions.has('kms:Decrypt')).toBe(true);
    expect(kmsActions.has('kms:DescribeKey')).toBe(true);
  });
});


// ─── (r) F9.3: Timing config — no SSM parameters, synth-time throws ──────────
//
// The three lifecycle timing props are plain string literals, NOT ConfigValues.
// Using resolveConfigValue with an empty literal sends it to
// valueForStringParameter, which creates a CFN SSM dynamic reference for a
// parameter that does not exist — the changeset would fail at deploy.
//
// Mutation guard: route the timings back through resolveConfigValue → the no-SSM
// test MUST fail (a CFN SSM parameter resource appears in the template).

describe('Timing config — no SSM parameters, synth-time validation (F9.3)', () => {
  const { template } = makeStack();

  test('template has no SSM parameter resource for any rsa-sim-* or rsa-requested-stale-* name', () => {
    // F9.3 mutation guard: route timings through resolveConfigValue →
    // AWS::SSM::Parameter or {{resolve:ssm:...}} appears → this test FAILS.
    const ssmParams = template.findResources('AWS::SSM::Parameter');
    for (const [logicalId, res] of Object.entries(ssmParams)) {
      const name = ((res as Record<string, unknown>).Properties as Record<string, unknown>)?.['Name'] as string | undefined;
      expect(name ?? '').not.toMatch(/rsa-sim-|rsa-requested-stale-/);
    }
    // Also check that no Lambda env var looks like a CFN SSM dynamic reference
    const functions = template.findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: 'vsa-staging-rsa-lifecycle-advancer' },
    });
    const keys = Object.keys(functions);
    expect(keys.length).toBe(1);
    const envVars = functions[keys[0]].Properties?.Environment?.Variables ?? {};
    for (const key of ['RSA_SIM_EN_ROUTE_AFTER_SECONDS', 'RSA_SIM_ARRIVED_AFTER_SECONDS', 'RSA_REQUESTED_STALE_AFTER_SECONDS']) {
      const val = JSON.stringify(envVars[key] ?? '');
      // Must be a plain numeric string, not a CFN dynamic reference or token
      expect(val).not.toMatch(/resolve:ssm/);
      expect(val).not.toMatch(/AWS::SSM/);
      expect(envVars[key]).toMatch(/^[1-9][0-9]*$/);
    }
  });

  test('constructor throws on zero en_route value', () => {
    // F9.3 mutation guard: pass '0' → constructor must throw.
    const id = ++stackCount;
    const app = new cdk.App();
    expect(() => new VsaRsaStack(app, `vsa-staging-rsa-timing-zero-${id}`, {
      env: { account: '123456789012', region: 'us-west-2' },
      stage: 'staging',
      rsaAlarmEmail: { literal: 'operator@example.invalid', parameterName: `/cvx/staging/rsa-alarm-email` },
      rsaSimEnRouteAfterSeconds: '0',
      rsaSimArrivedAfterSeconds: '300',
      rsaRequestedStaleAfterSeconds: '900',
    })).toThrow(/rsaSimEnRouteAfterSeconds.*not a positive integer/);
  });

  test('constructor throws on non-numeric arrived value', () => {
    const id = ++stackCount;
    const app = new cdk.App();
    expect(() => new VsaRsaStack(app, `vsa-staging-rsa-timing-abc-${id}`, {
      env: { account: '123456789012', region: 'us-west-2' },
      stage: 'staging',
      rsaAlarmEmail: { literal: 'operator@example.invalid', parameterName: `/cvx/staging/rsa-alarm-email` },
      rsaSimEnRouteAfterSeconds: '60',
      rsaSimArrivedAfterSeconds: 'abc',
      rsaRequestedStaleAfterSeconds: '900',
    })).toThrow(/rsaSimArrivedAfterSeconds.*not a positive integer/);
  });

  test('constructor throws when arrived <= en_route', () => {
    const id = ++stackCount;
    const app = new cdk.App();
    expect(() => new VsaRsaStack(app, `vsa-staging-rsa-timing-order-${id}`, {
      env: { account: '123456789012', region: 'us-west-2' },
      stage: 'staging',
      rsaAlarmEmail: { literal: 'operator@example.invalid', parameterName: `/cvx/staging/rsa-alarm-email` },
      rsaSimEnRouteAfterSeconds: '300',
      rsaSimArrivedAfterSeconds: '60',
      rsaRequestedStaleAfterSeconds: '900',
    })).toThrow(/rsaSimArrivedAfterSeconds.*must be greater than/);
  });

  test('constructor throws when stale < 600', () => {
    // F9.3: floor of 600 added so a typo cannot reconcile rows the consumer is
    // still retrying.
    const id = ++stackCount;
    const app = new cdk.App();
    expect(() => new VsaRsaStack(app, `vsa-staging-rsa-timing-stale-${id}`, {
      env: { account: '123456789012', region: 'us-west-2' },
      stage: 'staging',
      rsaAlarmEmail: { literal: 'operator@example.invalid', parameterName: `/cvx/staging/rsa-alarm-email` },
      rsaSimEnRouteAfterSeconds: '60',
      rsaSimArrivedAfterSeconds: '300',
      rsaRequestedStaleAfterSeconds: '120',
    })).toThrow(/rsaRequestedStaleAfterSeconds.*≥ 600/);
  });
});



// ─── (s) F10.6: Advancer-scoped IAM tests ────────────────────────────────────
//
// C1 finding: the existing KMS test collects grants from ALL roles, so
// the consumer's implicit kms:Decrypt (via grantStreamRead) masked the
// advancer's missing KMS grant. Each test below selects statements by the
// advancer's role logical ID, not by action across all roles.
//
// Mutations: delete each grant → the test scoped to that role MUST fail.
// (b-iv) correction: the prior record said detaching RsaLifecycleAdvancerCmkAccess
// made kmsActions.has('kms:Decrypt') false. It did not — the consumer's
// grantStreamRead kept kms:Decrypt present across all roles. The test below
// is scoped to the advancer's role and correctly catches that deletion.

describe('Lifecycle advancer IAM — advancer-role-scoped (F10.6)', () => {
  // Fix Group 13 item 2: no key input; the stack creates the table's key.
  const id = ++stackCount;
  const app = new cdk.App();
  const stack = new VsaRsaStack(app, `vsa-staging-rsa-adv-iam-${id}`, {
    env: { account: '123456789012', region: 'us-west-2' },
    stage: 'staging',
    rsaAlarmEmail: { literal: 'operator@example.invalid', parameterName: '/cvx/staging/rsa-alarm-email' },
  });
  const advancerIamTemplate = Template.fromStack(stack);

  /**
   * The advancer's execution role, resolved from the Lambda's `Role` property.
   * F12.1: the substring match this replaced returned `undefined` on a rename,
   * and the collector then returned [] — every "no widening" test vacuous.
   * `roleIdOfFunction` throws instead.
   */
  function findAdvancerRoleLogicalId(tmpl: Template): string {
    return roleIdOfFunction(tmpl, 'vsa-staging-rsa-lifecycle-advancer');
  }

  /**
   * Every statement on the advancer role through every channel the template
   * shows (F12.1, shared collector). The F11.4 union test was named for the
   * role but read only `AWS::IAM::Policy`, so an `iam.ManagedPolicy` with
   * `dynamodb:*` on `*` passed it (review Cycle 3, 3.2 W2).
   */
  function getAdvancerPolicyStatements(tmpl: Template): Record<string, unknown>[] {
    return roleStatements(tmpl, findAdvancerRoleLogicalId(tmpl));
  }

  test('F10.6: KMS grant on advancer role — exact key ARN and kms:ViaService condition', () => {
    // Mutation: delete RsaLifecycleAdvancerCmkAccess → kms:Decrypt absent on
    // the advancer's role-scoped policy → this test FAILS.
    // (The old test passed because the consumer's implicit kms:Decrypt was counted.)
    const stmts = getAdvancerPolicyStatements(advancerIamTemplate);

    const kmsStmts = stmts.filter((s) => {
      const a = s['Action'];
      const normalise = (x: unknown): string[] =>
        typeof x === 'string' ? [x] : Array.isArray(x) ? (x as string[]) : [];
      return normalise(a).some((act) => act.toLowerCase().startsWith('kms:'));
    });

    expect(kmsStmts.length).toBeGreaterThanOrEqual(1);

    for (const stmt of kmsStmts) {
      // Action must include kms:Decrypt
      const a = stmt['Action'];
      const normalise = (x: unknown): string[] =>
        typeof x === 'string' ? [x] : Array.isArray(x) ? (x as string[]) : [];
      const actions = new Set(normalise(a).map((s) => s.toLowerCase()));
      expect(actions.has('kms:decrypt')).toBe(true);

      // Resource must reference the key ARN (not '*')
      const resource = JSON.stringify(stmt['Resource']);
      expect(resource).not.toBe('"*"');
      expect(resource.length).toBeGreaterThan(2); // not empty / '*'

      // Condition must include kms:ViaService
      const condition = JSON.stringify(stmt['Condition'] ?? {});
      expect(condition).toContain('kms:ViaService');
    }
  });

  test('F10.6: UpdateItem grant on advancer role targets the table ARN', () => {
    // Mutation: delete RsaLifecycleAdvancerTableUpdate → dynamodb:UpdateItem
    // absent on the advancer's role → this test FAILS.
    const stmts = getAdvancerPolicyStatements(advancerIamTemplate);

    const updateStmts = stmts.filter((s) => {
      const a = s['Action'];
      const normalise = (x: unknown): string[] =>
        typeof x === 'string' ? [x] : Array.isArray(x) ? (x as string[]) : [];
      return normalise(a).some((act) => act.toLowerCase() === 'dynamodb:updateitem');
    });

    expect(updateStmts.length).toBeGreaterThanOrEqual(1);

    // Resource must NOT be the GSI (UpdateItem goes to the base table ARN, not an index)
    for (const stmt of updateStmts) {
      const resourceStr = JSON.stringify(stmt['Resource']);
      expect(resourceStr).not.toContain('/index/');
    }
  });

  test('F10.6: Query grant on advancer role targets the GSI ARN', () => {
    // Mutation: delete RsaLifecycleAdvancerGsiQuery → dynamodb:Query absent
    // on the advancer's role → this test FAILS.
    const stmts = getAdvancerPolicyStatements(advancerIamTemplate);

    const queryStmts = stmts.filter((s) => {
      const a = s['Action'];
      const normalise = (x: unknown): string[] =>
        typeof x === 'string' ? [x] : Array.isArray(x) ? (x as string[]) : [];
      return normalise(a).some((act) => act.toLowerCase() === 'dynamodb:query');
    });

    expect(queryStmts.length).toBeGreaterThanOrEqual(1);

    // Resource must reference the GSI ARN (/index/)
    for (const stmt of queryStmts) {
      const resourceStr = JSON.stringify(stmt['Resource']);
      expect(resourceStr).toContain('/index/');
    }
  });

  // ─── F11.4 (review Cycle 2, 3.2 C1): exact scope, not just presence ─────
  //
  // The three tests above prove each grant sits on the advancer's own role.
  // They admitted every WIDENING mutation: KMS `key/*`, `StringLike '*'`,
  // UpdateItem on `table/*`, and extra DeleteItem/PutItem actions. These pin
  // each statement exactly, and bound the union of actions on the role.

  const tableLogicalId = (): string => {
    const tables = advancerIamTemplate.findResources('AWS::DynamoDB::Table', {
      Properties: { TableName: 'vsa-staging-rsa-dispatch' },
    });
    const ids = Object.keys(tables);
    expect(ids.length).toBe(1);
    return ids[0];
  };
  const actionList = (s: Record<string, unknown>): string[] =>
    ([] as string[]).concat(s['Action'] as string | string[]);
  const stmtWith = (action: string): Record<string, unknown> => {
    const matches = getAdvancerPolicyStatements(advancerIamTemplate).filter((s) =>
      actionList(s).includes(action),
    );
    expect(matches.length).toBe(1);
    return matches[0];
  };

  test('F11.4: KMS statement is exact — key ARN, StringEquals ViaService, three actions', () => {
    const s = stmtWith('kms:Decrypt');
    const keyIds = Object.keys(advancerIamTemplate.findResources('AWS::KMS::Key'));
    expect(keyIds).toHaveLength(1);
    expect(s['Resource']).toEqual({ 'Fn::GetAtt': [keyIds[0], 'Arn'] });
    expect(s['Condition']).toEqual({
      StringEquals: { 'kms:ViaService': 'dynamodb.us-west-2.amazonaws.com' },
    });
    expect(actionList(s).sort()).toEqual(['kms:Decrypt', 'kms:DescribeKey', 'kms:GenerateDataKey']);
  });

  test('F11.4: UpdateItem statement is exact — single action on the table GetAtt Arn', () => {
    const s = stmtWith('dynamodb:UpdateItem');
    expect(s['Action']).toBe('dynamodb:UpdateItem');
    expect(s['Resource']).toEqual({ 'Fn::GetAtt': [tableLogicalId(), 'Arn'] });
  });

  test('F11.4: Query statement is exact — the status-createdAt-index GSI ARN only', () => {
    const s = stmtWith('dynamodb:Query');
    expect(s['Action']).toBe('dynamodb:Query');
    expect(s['Resource']).toEqual({
      'Fn::Join': ['', [{ 'Fn::GetAtt': [tableLogicalId(), 'Arn'] }, '/index/status-createdAt-index']],
    });
  });

  test('F11.4: the union of actions on the advancer role is exactly the expected set', () => {
    const union = new Set<string>();
    for (const s of getAdvancerPolicyStatements(advancerIamTemplate)) {
      for (const a of actionList(s)) union.add(a);
    }
    expect([...union].sort()).toEqual([
      'dynamodb:Query',
      'dynamodb:UpdateItem',
      'kms:Decrypt',
      'kms:DescribeKey',
      'kms:GenerateDataKey',
    ]);
  });

  // F12.1 (review Cycle 3, 3.2 W2): the two channels the union cannot see
  // through statements alone. Mutations: addManagedPolicy(AmazonDynamoDBFullAccess);
  // a NotAction statement → each MUST fail.
  test('F12.1: the advancer role carries exactly one managed policy ARN, AWSLambdaBasicExecutionRole', () => {
    const roleId = findAdvancerRoleLogicalId(advancerIamTemplate);
    expect(roleManagedPolicyArns(advancerIamTemplate, roleId)).toEqual([LAMBDA_BASIC_EXECUTION_ROLE_ARN]);
  });

  test('F12.1: the advancer role has exactly three statements, each a plain Allow, none on a "*" resource', () => {
    const stmts = getAdvancerPolicyStatements(advancerIamTemplate);
    expect(stmts.length).toBe(3);
    for (const s of stmts) {
      expect(malformedForBound(s)).toEqual([]);
      for (const r of resourcesOf(s)) expect(r).not.toBe('*');
    }
  });

  test('F10.6: scheduler role lambda:InvokeFunction targets the advancer ARN', () => {
    // Find the scheduler role (RsaAdvancerSchedulerRole logical ID)
    // and assert its InvokeFunction grant targets the advancer's GetAtt Arn.
    // NOTE (F11.6): deleting the explicit InvokeRsaLifecycleAdvancerLambda statement
    // does NOT fail this test and does not break invocation: schedulerTargets.LambdaInvoke
    // auto-grants invoke on its target. What this test guards is least privilege: every
    // invoke statement must name only the advancer (pointing one at the consumer fails).

    // Find advancer Lambda logical ID
    const lambdas = advancerIamTemplate.findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: 'vsa-staging-rsa-lifecycle-advancer' },
    });
    const advancerLambdaIds = Object.keys(lambdas);
    expect(advancerLambdaIds.length).toBe(1);
    const advancerLambdaId = advancerLambdaIds[0];

    // F13.1 (review Cycle 4, 3.2 W): this test kept its own collector (a policy
    // logical-ID substring, and only statements whose action is literally
    // lambda:InvokeFunction). SC1 iam.ManagedPolicy, SC5 attachInlinePolicy and
    // SC2 lambda:* — each granting invoke on the consumer — passed at 79/79.
    // Now: the role is resolved from the schedule's Target.RoleArn, statements
    // come from the shared collector, and the action union is unfiltered.
    const schedules = Object.values(advancerIamTemplate.findResources('AWS::Scheduler::Schedule'));
    expect(schedules.length).toBe(1);
    const target = ((schedules[0] as Record<string, unknown>).Properties as Record<string, unknown>)['Target'] as Record<string, unknown>;
    const schedulerRoleId = logicalIdOf(target['RoleArn']);
    expect(schedulerRoleId).not.toBeNull();

    expect(roleManagedPolicyArns(advancerIamTemplate, schedulerRoleId!)).toEqual([]);
    const schedulerStmts = roleStatements(advancerIamTemplate, schedulerRoleId!);
    // The explicit InvokeRsaLifecycleAdvancerLambda statement + LambdaInvoke's auto-grant.
    expect(schedulerStmts.length).toBe(2);
    const union = new Set<string>();
    for (const s of schedulerStmts) {
      expect(malformedForBound(s)).toEqual([]);
      for (const a of actionsOf(s)) union.add(a);
    }
    expect([...union]).toEqual(['lambda:InvokeFunction']);

    // Every statement's resources are exactly the advancer's ARN or its
    // version/alias form. F14.1 (review Cycle 5, 3.2 W): the presence check
    // this replaced ("contains the advancer's logical ID, not the consumer's")
    // passed with the consumer's ARN added by name (SC8) or `function:*` (SC9)
    // beside the advancer. Mutations SC8-SC11 → this test MUST fail.
    const adv = { 'Fn::GetAtt': [advancerLambdaId, 'Arn'] };
    const allowed = [adv, { 'Fn::Join': ['', [adv, ':*']] }];
    for (const stmt of schedulerStmts) {
      for (const r of resourcesOf(stmt)) expect(allowed).toContainEqual(r);
    }
  });
});


// ─── (t) F10.7: Heartbeat alarm (W1) ─────────────────────────────────────────
//
// "The advancer did not run" — Invocations Sum < 1 over two 5-minute periods,
// TreatMissingData: BREACHING. AlarmActions + OKActions on rsaAlarmTopic.
//
// Mutation: flip to NOT_BREACHING or empty the actions → each test MUST fail.

describe('Lifecycle advancer heartbeat alarm — F10.7', () => {
  const { template } = makeStack();

  test('heartbeat alarm exists (advancer-not-running)', () => {
    const alarms = template.findResources('AWS::CloudWatch::Alarm');
    const hb = Object.values(alarms).filter((a) => {
      const name = ((a as Record<string, unknown>).Properties as Record<string, unknown>)?.['AlarmName'] as string | undefined;
      return typeof name === 'string' && name.includes('not-running');
    });
    expect(hb.length).toBeGreaterThanOrEqual(1);
  });

  test('heartbeat alarm TreatMissingData is BREACHING (F10.7 mutation guard)', () => {
    // Mutation: flip to NOT_BREACHING → no alarm on scheduler failure → FAIL.
    const alarms = template.findResources('AWS::CloudWatch::Alarm');
    const hb = Object.values(alarms).filter((a) => {
      const name = ((a as Record<string, unknown>).Properties as Record<string, unknown>)?.['AlarmName'] as string | undefined;
      return typeof name === 'string' && name.includes('not-running');
    });
    expect(hb.length).toBeGreaterThanOrEqual(1);
    for (const alarm of hb) {
      const props = (alarm as Record<string, unknown>).Properties as Record<string, unknown>;
      expect(props['TreatMissingData']).toBe('breaching');
    }
  });

  test('heartbeat alarm has non-empty AlarmActions (F10.7 mutation guard)', () => {
    // Mutation: empty AlarmActions → alarm fires but nobody is notified → FAIL.
    const alarms = template.findResources('AWS::CloudWatch::Alarm');
    const hb = Object.values(alarms).filter((a) => {
      const name = ((a as Record<string, unknown>).Properties as Record<string, unknown>)?.['AlarmName'] as string | undefined;
      return typeof name === 'string' && name.includes('not-running');
    });
    for (const alarm of hb) {
      const props = (alarm as Record<string, unknown>).Properties as Record<string, unknown>;
      const actions = props['AlarmActions'] as unknown[] | undefined;
      expect(Array.isArray(actions)).toBe(true);
      expect((actions as unknown[]).length).toBeGreaterThanOrEqual(1);
    }
  });

  test('heartbeat alarm uses Invocations metric (not Errors)', () => {
    const alarms = template.findResources('AWS::CloudWatch::Alarm');
    const hb = Object.values(alarms).filter((a) => {
      const name = ((a as Record<string, unknown>).Properties as Record<string, unknown>)?.['AlarmName'] as string | undefined;
      return typeof name === 'string' && name.includes('not-running');
    });
    for (const alarm of hb) {
      const props = (alarm as Record<string, unknown>).Properties as Record<string, unknown>;
      const metricName = props['MetricName'] as string | undefined;
      expect(metricName).toBe('Invocations');
    }
  });

  // F11.5 (review Cycle 2, 3.2 W1): pin WHOSE invocations. Pinning MetricName
  // alone admitted a heartbeat on the consumer (stays OK during a demo while the
  // advancer is dead), a dimensionless account-wide metric (never < 1), and a
  // dropped OKAction. Mutations: each of those three → this test MUST fail.
  test('F11.5: heartbeat watches the advancer exactly, and notifies on both edges via the subscribed topic', () => {
    const alarms = template.findResources('AWS::CloudWatch::Alarm');
    const hb = Object.values(alarms).filter((a) => {
      const name = ((a as Record<string, unknown>).Properties as Record<string, unknown>)?.['AlarmName'] as string | undefined;
      return typeof name === 'string' && name.includes('not-running');
    });
    expect(hb.length).toBe(1);
    const props = (hb[0] as Record<string, unknown>).Properties as Record<string, unknown>;

    const advancerIds = Object.keys(template.findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: 'vsa-staging-rsa-lifecycle-advancer' },
    }));
    expect(advancerIds.length).toBe(1);

    expect(props['Namespace']).toBe('AWS/Lambda');
    expect(props['Dimensions']).toEqual([{ Name: 'FunctionName', Value: { Ref: advancerIds[0] } }]);
    expect(props['ComparisonOperator']).toBe('LessThanThreshold');
    expect(props['Threshold']).toBe(1);
    expect(props['EvaluationPeriods']).toBe(2);
    expect(props['Period']).toBe(300);
    expect(props['Statistic']).toBe('Sum');
    expect(props['Unit']).toBeUndefined(); // F13.3

    const subs = Object.values(template.findResources('AWS::SNS::Subscription'));
    expect(subs.length).toBe(1);
    const subTopic = ((subs[0] as Record<string, unknown>).Properties as Record<string, unknown>)['TopicArn'];
    for (const key of ['AlarmActions', 'OKActions']) {
      const actions = props[key] as unknown[] | undefined;
      expect(actions).toEqual([subTopic]);
    }
  });
});


// ─── (u) F10.8: Chain assertions ─────────────────────────────────────────────
//
// Three surviving mutations closed by asserting the chain properties directly:
//   1. Schedule target is the advancer, not the consumer.
//   2. MetricFilter log group references the advancer's LogRetention resource.
//   3. Alarm namespace/metricName derive from the filter (via metric()), not a duplicate.

describe('Advancer chain assertions — F10.8', () => {
  const { template } = makeStack();

  test('F10.8: schedule target is the advancer Lambda (not the consumer)', () => {
    // Mutation: change target to rsaDispatchConsumerFn → schedule never reaches
    // the advancer → heartbeat and error alarms both stay green → FAIL.

    // Find the advancer Lambda logical ID
    const lambdas = template.findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: 'vsa-staging-rsa-lifecycle-advancer' },
    });
    const advancerIds = Object.keys(lambdas);
    expect(advancerIds.length).toBe(1);
    const advancerLambdaId = advancerIds[0];

    // Find consumer Lambda logical ID (to assert the schedule does NOT target it)
    const consumerLambdas = template.findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: 'vsa-staging-rsa-dispatch-consumer' },
    });
    const consumerIds = Object.keys(consumerLambdas);
    expect(consumerIds.length).toBe(1);
    const consumerLambdaId = consumerIds[0];

    // The Scheduler::Schedule resource's Target.Arn must reference the advancer
    const schedules = template.findResources('AWS::Scheduler::Schedule');
    const rateSchedules = Object.values(schedules).filter((s) => {
      const expr = ((s as Record<string, unknown>).Properties as Record<string, unknown>)?.['ScheduleExpression'] as string | undefined;
      return typeof expr === 'string' && expr.includes('rate(1 minute)');
    });
    expect(rateSchedules.length).toBeGreaterThanOrEqual(1);

    for (const sched of rateSchedules) {
      const props = (sched as Record<string, unknown>).Properties as Record<string, unknown>;
      const targetStr = JSON.stringify(props['Target'] ?? {});
      // Must reference the advancer, not the consumer
      expect(targetStr).toContain(advancerLambdaId);
      expect(targetStr).not.toContain(consumerLambdaId);
    }
  });

  test('F10.8: MetricFilter log group references advancer LogRetention (not consumer)', () => {
    // Mutation: attach MetricFilter to consumer's log group → filter reads the wrong
    // logs, reconciliation alarm has no data → stays NOT_BREACHING → FAIL.

    // Find advancer LogRetention logical ID (CDK creates a custom resource for log retention)
    const logRetentions = template.findResources('Custom::LogRetention');
    const advancerRetentions = Object.entries(logRetentions).filter(([logId]) =>
      logId.includes('RsaLifecycleAdvancer'),
    );
    // If CDK created a standard LogGroup instead of Custom::LogRetention, check that
    const logGroups = template.findResources('AWS::Logs::LogGroup');
    const advancerLogGroups = Object.entries(logGroups).filter(([logId]) =>
      logId.includes('RsaLifecycleAdvancer'),
    );

    const advancerLogResourceIds = [
      ...advancerRetentions.map(([id]) => id),
      ...advancerLogGroups.map(([id]) => id),
    ];
    // There must be at least one advancer log resource
    expect(advancerLogResourceIds.length).toBeGreaterThanOrEqual(1);

    // The MetricFilter's LogGroupName must reference one of those resources
    const filters = template.findResources('AWS::Logs::MetricFilter');
    const reconcFilters = Object.values(filters).filter((f) => {
      const pattern = ((f as Record<string, unknown>).Properties as Record<string, unknown>)?.['FilterPattern'] as string | undefined;
      return typeof pattern === 'string' && pattern.includes('RSA_RECONCILIATION');
    });
    expect(reconcFilters.length).toBeGreaterThanOrEqual(1);

    for (const filter of reconcFilters) {
      const props = (filter as Record<string, unknown>).Properties as Record<string, unknown>;
      const logGroupNameStr = JSON.stringify(props['LogGroupName']);
      const refsAdvancer = advancerLogResourceIds.some((id) => logGroupNameStr.includes(id));
      expect(refsAdvancer).toBe(true);
    }
  });

  test('F10.8: reconciliation alarm namespace/metricName match the MetricFilter', () => {
    // Mutation: change alarm metricName to 'ReconciliationCounts' →
    // alarm reads a metric nothing writes → stays NOT_BREACHING → FAIL.
    // F10.8 fix: alarm built from reconciliationMetric.metric() so this cannot drift.

    const filters = template.findResources('AWS::Logs::MetricFilter');
    const reconcFilters = Object.values(filters).filter((f) => {
      const pattern = ((f as Record<string, unknown>).Properties as Record<string, unknown>)?.['FilterPattern'] as string | undefined;
      return typeof pattern === 'string' && pattern.includes('RSA_RECONCILIATION');
    });
    expect(reconcFilters.length).toBeGreaterThanOrEqual(1);

    // Extract namespace/metricName from the filter
    const filterProps = (reconcFilters[0] as Record<string, unknown>).Properties as Record<string, unknown>;
    const filterTransformations = filterProps['MetricTransformations'] as Record<string, unknown>[] | undefined;
    expect(Array.isArray(filterTransformations)).toBe(true);
    expect((filterTransformations as Record<string, unknown>[]).length).toBeGreaterThanOrEqual(1);
    const filterNamespace = (filterTransformations as Record<string, unknown>[])[0]['MetricNamespace'] as string;
    const filterMetricName = (filterTransformations as Record<string, unknown>[])[0]['MetricName'] as string;

    // Find the reconciliation alarm
    const alarms = template.findResources('AWS::CloudWatch::Alarm');
    const reconcAlarms = Object.values(alarms).filter((a) => {
      const name = ((a as Record<string, unknown>).Properties as Record<string, unknown>)?.['AlarmName'] as string | undefined;
      return typeof name === 'string' && name.includes('rsa-reconciliation-detected');
    });
    expect(reconcAlarms.length).toBeGreaterThanOrEqual(1);

    for (const alarm of reconcAlarms) {
      const props = (alarm as Record<string, unknown>).Properties as Record<string, unknown>;
      expect(props['Namespace']).toBe(filterNamespace);
      expect(props['MetricName']).toBe(filterMetricName);
    }
  });
});


// ─── (v) F10.9: Per-stage timing assertions ──────────────────────────────────
//
// W3 finding: hard-coding '300' for arrivedAfterSeconds survives; defaults not pinned.
// Accept 3 requires passing arbitrary valid timings and seeing them in the template.

describe('Per-stage timing assertions — F10.9', () => {
  test('F10.9: non-default timings thread into advancer Lambda env vars', () => {
    // Passing 120/600/1200 and asserting the exact values in the template.
    // Mutation: pass timing through resolveConfigValue or ignore it → template
    // keeps the default 60/300/900 → expected '120' but got '60' → FAIL.
    const id = ++stackCount;
    const app = new cdk.App();
    const stack = new VsaRsaStack(app, `vsa-staging-rsa-timing-custom-${id}`, {
      env: { account: '123456789012', region: 'us-west-2' },
      stage: 'staging',
      rsaAlarmEmail: { literal: 'operator@example.invalid', parameterName: '/cvx/staging/rsa-alarm-email' },
      rsaSimEnRouteAfterSeconds: '120',
      rsaSimArrivedAfterSeconds: '600',
      rsaRequestedStaleAfterSeconds: '1200',
    });
    const customTemplate = Template.fromStack(stack);

    const functions = customTemplate.findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: 'vsa-staging-rsa-lifecycle-advancer' },
    });
    const keys = Object.keys(functions);
    expect(keys.length).toBe(1);
    const envVars = functions[keys[0]].Properties?.Environment?.Variables ?? {};

    expect(envVars['RSA_SIM_EN_ROUTE_AFTER_SECONDS']).toBe('120');
    expect(envVars['RSA_SIM_ARRIVED_AFTER_SECONDS']).toBe('600');
    expect(envVars['RSA_REQUESTED_STALE_AFTER_SECONDS']).toBe('1200');
  });

  test('F10.9: absent timing props produce the stack defaults (60/300/900)', () => {
    // Mutation: set a non-default hard-coded default in the constructor (e.g. '61') →
    // this test catches it because it expects '60' exactly.
    const { template: defaultTemplate } = makeStack();

    const functions = defaultTemplate.findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: 'vsa-staging-rsa-lifecycle-advancer' },
    });
    const keys = Object.keys(functions);
    expect(keys.length).toBe(1);
    const envVars = functions[keys[0]].Properties?.Environment?.Variables ?? {};

    expect(envVars['RSA_SIM_EN_ROUTE_AFTER_SECONDS']).toBe('60');
    expect(envVars['RSA_SIM_ARRIVED_AFTER_SECONDS']).toBe('300');
    expect(envVars['RSA_REQUESTED_STALE_AFTER_SECONDS']).toBe('900');
  });
});


// ─── (F11.6) Schedule is enabled; async retries are off ──────────────────────
//
// A disabled schedule is the heartbeat alarm's job to catch at runtime; this
// pins it at synth so a disabled schedule never ships silently. Async retries
// are 0 because the next one-minute tick is the retry; Lambda's default of 2
// would stack overlapping runs behind a failing one (review Cycle 1, 3.2 S).
// Mutations: `enabled: false`; remove configureAsyncInvoke → each MUST fail.

describe('Advancer schedule state and async retry config (F11.6)', () => {
  const { template } = makeStack();

  test('the advancer schedule is ENABLED and targets rate(1 minute)', () => {
    const schedules = Object.values(template.findResources('AWS::Scheduler::Schedule'));
    expect(schedules.length).toBe(1);
    const props = (schedules[0] as Record<string, unknown>).Properties as Record<string, unknown>;
    expect(props['State']).toBe('ENABLED');
    expect(props['ScheduleExpression']).toBe('rate(1 minute)');
  });

  // F12.3 (review Cycle 3, 3.2 S2): the Scheduler target's retryAttempts: 0 was
  // unasserted; `retryAttempts: 3` survived. The next one-minute tick is the
  // retry. Mutation: retryAttempts: 3 → this test MUST fail.
  test('F12.3: the schedule target does not retry (MaximumRetryAttempts 0)', () => {
    const schedules = Object.values(template.findResources('AWS::Scheduler::Schedule'));
    expect(schedules.length).toBe(1);
    const target = ((schedules[0] as Record<string, unknown>).Properties as Record<string, unknown>)['Target'] as Record<string, unknown>;
    expect((target['RetryPolicy'] as Record<string, unknown>)?.['MaximumRetryAttempts']).toBe(0);
  });

  test('the advancer has an EventInvokeConfig with MaximumRetryAttempts 0', () => {
    const advancerIds = Object.keys(template.findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: 'vsa-staging-rsa-lifecycle-advancer' },
    }));
    expect(advancerIds.length).toBe(1);
    const configs = Object.values(template.findResources('AWS::Lambda::EventInvokeConfig'));
    const forAdvancer = configs.filter((c) => {
      const p = (c as Record<string, unknown>).Properties as Record<string, unknown>;
      return JSON.stringify(p['FunctionName']).includes(advancerIds[0]);
    });
    expect(forAdvancer.length).toBe(1);
    const p = (forAdvancer[0] as Record<string, unknown>).Properties as Record<string, unknown>;
    expect(p['MaximumRetryAttempts']).toBe(0);
  });
});
