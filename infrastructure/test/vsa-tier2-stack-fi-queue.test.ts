/**
 * FI Action Queue Stage A, Group 3 (T3.1): the Tier 2 stack's new tables, the
 * auto-approval evaluator, the runner's run-record grant, and the D10 alarms.
 *
 * Spec: .kiro/specs/2026-10-02-fi-action-queue-stage-a/spec.md (D9, D10, D11).
 *
 * IAM is compared EXACTLY per Sid (actions and the whole Resource value), as in
 * vsa-tier2-stack-avx-broker.test.ts, so a grant cannot widen to `table/*`,
 * `index/*` or `*` at green. Presence is checked by action, not by Sid.
 * Mutations run against these tests are recorded in the spec's decisions.md.
 */

import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { VsaTier2Stack } from '../lib/vsa-tier2-stack';

const ACCOUNT = '123456789012';
const REGION = 'us-west-2';

let cached: Template | undefined;

function template(): Template {
  if (cached) return cached;
  const prev = process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT;
  process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT = 'ops@example.invalid';
  try {
    const app = new cdk.App();
    // Stage A's contract is the auto-approver with rule-approved moves off, as every deploy
    // was until task 8.4. Since 8.4 the default is on, so this file asks for off explicitly;
    // vsa-tier2-stack-fi-auto-moves.test.ts pins exactly what turning it on adds.
    const stack = new VsaTier2Stack(app, 'vsa-staging-tier2', {
      fiRebalancingAutoApprovalLive: false,
      fiAutoRebookLive: false,
      env: { account: ACCOUNT, region: REGION },
      stage: 'staging',
      adpRegion: 'us-east-1',
      adpAccountId: ACCOUNT,
      cmsKmsKeyArn: {
        literal: `arn:aws:kms:${REGION}:${ACCOUNT}:key/test-key-id`,
        parameterName: '/cvx/staging/cms-kms-key-arn',
      },
    });
    cached = Template.fromStack(stack);
    return cached;
  } finally {
    if (prev === undefined) delete process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT;
    else process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT = prev;
  }
}

function logicalIdOf(type: string, props: Record<string, unknown>): string {
  const ids = Object.keys(template().findResources(type, { Properties: props }));
  expect(ids).toHaveLength(1);
  return ids[0];
}

const asList = (x: unknown): unknown[] => (Array.isArray(x) ? x : [x]);
const tableId = (name: string) => logicalIdOf('AWS::DynamoDB::Table', { TableName: name });
const arn = (id: string) => ({ 'Fn::GetAtt': [id, 'Arn'] });

const RULES = 'vsa-staging-avx-approval-rules';
const RUNS = 'vsa-staging-avx-agent-runs';
const ACTIONS = 'vsa-staging-avx-actions';
const FINDINGS = 'vsa-staging-avx-findings';
const OPERATOR_TOPIC = 'vsa-staging-tier2-operator-alerts';
const EVALUATOR = `vsa-staging-avx-auto-approver-${REGION}`;
const HEALTH_AGENT = `vsa-staging-tier2-health-agent-${REGION}`;

function roleStatements(roleName: string): any[] {
  const roleId = logicalIdOf('AWS::IAM::Role', { RoleName: roleName });
  return Object.values(template().findResources('AWS::IAM::Policy'))
    .filter((p: any) => JSON.stringify(p.Properties.Roles).includes(roleId))
    .flatMap((p: any) => p.Properties.PolicyDocument.Statement);
}

function functionRoleStatements(functionName: string): any[] {
  const fnId = logicalIdOf('AWS::Lambda::Function', { FunctionName: functionName });
  const roleRef = JSON.stringify(template().toJSON().Resources[fnId].Properties.Role);
  const roleId = /"Fn::GetAtt":\["([A-Za-z0-9]+)"/.exec(roleRef)![1];
  return Object.values(template().findResources('AWS::IAM::Policy'))
    .filter((p: any) => JSON.stringify(p.Properties.Roles).includes(roleId))
    .flatMap((p: any) => p.Properties.PolicyDocument.Statement);
}

/** Every (action, Resource) pair granting `action` in `statements`. */
function grantsOf(statements: any[], action: string): unknown[] {
  return statements
    .filter((s) => s.Effect === 'Allow' && asList(s.Action).includes(action))
    .map((s) => s.Resource);
}

// ───────────────────────────────────────────────────────────────────────────
// Tables (Accept 1)
// ───────────────────────────────────────────────────────────────────────────

describe('Stage A tables', () => {
  test.each([
    [RULES, 'fleet_id', 'rule_id', 'vsa-staging-avx-approval-rules-table-name'],
    [RUNS, 'agent_component', 'started_at', 'vsa-staging-avx-agent-runs-table-name'],
  ])('%s: keys, on-demand, PITR, RETAIN, no GSI, exported', (name, pk, sk, exportName) => {
    const t = template().toJSON().Resources[tableId(name)];
    expect(t.DeletionPolicy).toBe('Retain');
    expect(t.UpdateReplacePolicy).toBe('Retain');
    expect(t.Properties.KeySchema).toEqual([
      { AttributeName: pk, KeyType: 'HASH' },
      { AttributeName: sk, KeyType: 'RANGE' },
    ]);
    expect(t.Properties.BillingMode).toBe('PAY_PER_REQUEST');
    expect(t.Properties.PointInTimeRecoverySpecification).toEqual({ PointInTimeRecoveryEnabled: true });
    expect(t.Properties.GlobalSecondaryIndexes).toBeUndefined();
    template().hasOutput('*', { Export: { Name: exportName } });
  });

  test('actions table gains a by_fleet GSI: pk fleet_id, sk updated_at, projection ALL', () => {
    const t = template().toJSON().Resources[tableId(ACTIONS)];
    const gsi = t.Properties.GlobalSecondaryIndexes.filter((g: any) => g.IndexName === 'by_fleet');
    expect(gsi).toHaveLength(1);
    expect(gsi[0].KeySchema).toEqual([
      { AttributeName: 'fleet_id', KeyType: 'HASH' },
      { AttributeName: 'updated_at', KeyType: 'RANGE' },
    ]);
    expect(gsi[0].Projection).toEqual({ ProjectionType: 'ALL' });
  });

  test('the actions table keeps its four existing GSIs (one GSI added per update)', () => {
    const t = template().toJSON().Resources[tableId(ACTIONS)];
    expect(t.Properties.GlobalSecondaryIndexes.map((g: any) => g.IndexName).sort())
      .toEqual(['by_created_by', 'by_finding', 'by_fleet', 'by_target_ref', 'by_target_system']);
  });
});

// ───────────────────────────────────────────────────────────────────────────
// Tier 2 runner: run-record env var and grant (Accepts 4, 5)
// ───────────────────────────────────────────────────────────────────────────

describe('Tier 2 runner run records', () => {
  test('the health agent gets AVX_AGENT_RUNS_TABLE referencing the runs table', () => {
    const fnId = logicalIdOf('AWS::Lambda::Function', { FunctionName: HEALTH_AGENT });
    const env = template().toJSON().Resources[fnId].Properties.Environment.Variables;
    expect(env.AVX_AGENT_RUNS_TABLE).toEqual({ Ref: tableId(RUNS) });
  });

  test('PutItem on the runs table is granted, on the table ARN exactly', () => {
    const resources = grantsOf(functionRoleStatements(HEALTH_AGENT), 'dynamodb:PutItem');
    expect(resources).toContainEqual(arn(tableId(RUNS)));
    const s = functionRoleStatements(HEALTH_AGENT).filter((x) => x.Sid === 'AvxAgentRunsWrite');
    expect(s).toHaveLength(1);
    expect(asList(s[0].Action)).toEqual(['dynamodb:PutItem']);
    expect(s[0].Resource).toEqual(arn(tableId(RUNS)));
  });

  test('no other statement on the health agent role reaches the runs or rules table', () => {
    const runs = tableId(RUNS);
    const rules = tableId(RULES);
    const offenders = functionRoleStatements(HEALTH_AGENT)
      .filter((s) => s.Sid !== 'AvxAgentRunsWrite')
      .filter((s) => JSON.stringify(s.Resource).includes(runs) || JSON.stringify(s.Resource).includes(rules));
    expect(offenders).toEqual([]);
  });
});

// ───────────────────────────────────────────────────────────────────────────
// The auto-approval evaluator (Accepts 3, 5)
// ───────────────────────────────────────────────────────────────────────────

describe('avx-auto-approver Lambda', () => {
  test('function shape and environment', () => {
    const fnId = logicalIdOf('AWS::Lambda::Function', { FunctionName: EVALUATOR });
    const props = template().toJSON().Resources[fnId].Properties;
    expect(props.Handler).toBe('handler.handler');
    expect(props.Runtime).toBe('python3.12');
    expect(props.Environment.Variables).toEqual({
      AVX_ACTIONS_TABLE: { Ref: tableId(ACTIONS) },
      AVX_RULES_TABLE: { Ref: tableId(RULES) },
      AVX_AGENT_RUNS_TABLE: { Ref: tableId(RUNS) },
    });
  });

  test('consumes the findings stream with partial-batch failures into its own DLQ', () => {
    const fnId = logicalIdOf('AWS::Lambda::Function', { FunctionName: EVALUATOR });
    const dlq = logicalIdOf('AWS::SQS::Queue', { QueueName: 'vsa-staging-avx-auto-approver-dlq' });
    template().hasResourceProperties('AWS::Lambda::EventSourceMapping', {
      FunctionName: { Ref: fnId },
      EventSourceArn: { 'Fn::GetAtt': [tableId(FINDINGS), 'StreamArn'] },
      StartingPosition: 'TRIM_HORIZON',
      FunctionResponseTypes: ['ReportBatchItemFailures'],
      BisectBatchOnFunctionError: true,
      MaximumRetryAttempts: 3,
      DestinationConfig: { OnFailure: { Destination: { 'Fn::GetAtt': [dlq, 'Arn'] } } },
    });
  });

  // Review Cycle 10 S2: only FI queue items reach the evaluator. The pattern is the
  // handler's own early-return condition, on leaf nodes (Exists matches leaves only).
  test('the stream filter admits only records with recommended_action.action_kind and fleet_id', () => {
    const fnId = logicalIdOf('AWS::Lambda::Function', { FunctionName: EVALUATOR });
    const esms = Object.values(template().findResources('AWS::Lambda::EventSourceMapping', {
      Properties: { FunctionName: { Ref: fnId } },
    })) as any[];
    expect(esms).toHaveLength(1);
    const filters = esms[0].Properties.FilterCriteria.Filters;
    expect(filters).toHaveLength(1);
    expect(JSON.parse(filters[0].Pattern)).toEqual({
      eventName: ['INSERT', 'MODIFY'],
      dynamodb: {
        NewImage: {
          recommended_action: { M: { action_kind: { S: [{ exists: true }] } } },
          fleet_id: { S: [{ exists: true }] },
        },
      },
    });
  });

  test('every Sid statement has exactly the expected actions and Resource', () => {
    const logGroup = `arn:aws:logs:${REGION}:${ACCOUNT}:log-group:/aws/lambda/${EVALUATOR}`;
    const expected: Record<string, { actions: string[]; resource: unknown }> = {
      AutoApproverCloudWatchLogs: {
        actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
        resource: [logGroup, `${logGroup}:*`],
      },
      AutoApproverRulesQuery: { actions: ['dynamodb:Query'], resource: arn(tableId(RULES)) },
      AutoApproverActionsWrite: { actions: ['dynamodb:PutItem', 'dynamodb:UpdateItem'], resource: arn(tableId(ACTIONS)) },
      AutoApproverAgentRunsWrite: { actions: ['dynamodb:PutItem'], resource: arn(tableId(RUNS)) },
    };
    const actual: Record<string, { actions: string[]; resource: unknown }> = {};
    for (const s of roleStatements(EVALUATOR).filter((x) => x.Sid)) {
      actual[s.Sid] = { actions: (asList(s.Action) as string[]).sort(), resource: s.Resource };
    }
    expect(actual).toEqual(expected);
  });

  test('statements CDK adds without a Sid are exactly the event-source and DLQ grants', () => {
    const dlq = logicalIdOf('AWS::SQS::Queue', { QueueName: 'vsa-staging-avx-auto-approver-dlq' });
    const actual = roleStatements(EVALUATOR).filter((x) => !x.Sid)
      .map((s) => ({ actions: (asList(s.Action) as string[]).sort(), resource: s.Resource }));
    expect(actual).toEqual(expect.arrayContaining([
      { actions: ['sqs:GetQueueAttributes', 'sqs:GetQueueUrl', 'sqs:SendMessage'], resource: arn(dlq) },
      { actions: ['dynamodb:ListStreams'], resource: '*' },
      {
        actions: ['dynamodb:DescribeStream', 'dynamodb:GetRecords', 'dynamodb:GetShardIterator'],
        resource: { 'Fn::GetAtt': [tableId(FINDINGS), 'StreamArn'] },
      },
    ]));
    expect(actual).toHaveLength(3);
  });

  // Presence, selected by ACTION (not Sid): each call the handler makes has a grant
  // on the right table. A deleted statement fails here even if a Sid list is edited.
  test.each([
    ['dynamodb:Query', RULES],
    ['dynamodb:PutItem', ACTIONS],
    ['dynamodb:UpdateItem', ACTIONS],
    ['dynamodb:PutItem', RUNS],
  ])('%s is granted on %s', (action, table) => {
    expect(grantsOf(roleStatements(EVALUATOR), action)).toContainEqual(arn(tableId(table)));
  });

  test('no read of Findings, no delete, no scan, no escalation side effects', () => {
    const acts = roleStatements(EVALUATOR).flatMap((s) => asList(s.Action));
    for (const forbidden of [
      'dynamodb:GetItem', 'dynamodb:DeleteItem', 'dynamodb:Scan', 'dynamodb:BatchWriteItem',
      'events:PutEvents', 'cloudwatch:PutMetricData', 'sns:Publish',
    ]) {
      expect(acts).not.toContain(forbidden);
    }
  });
});

// ───────────────────────────────────────────────────────────────────────────
// Alarms to the operator topic (Accept 6)
// ───────────────────────────────────────────────────────────────────────────

describe('Stage A alarms', () => {
  const operatorTopic = () => logicalIdOf('AWS::SNS::Topic', { TopicName: OPERATOR_TOPIC });

  test('evaluator errors alarm notifies the operator topic on alarm and recovery', () => {
    const fnId = logicalIdOf('AWS::Lambda::Function', { FunctionName: EVALUATOR });
    template().hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmName: `vsa-staging-avx-auto-approver-errors-${REGION}`,
      Namespace: 'AWS/Lambda',
      MetricName: 'Errors',
      Dimensions: [{ Name: 'FunctionName', Value: { Ref: fnId } }],
      Threshold: 1,
      ComparisonOperator: 'GreaterThanOrEqualToThreshold',
      TreatMissingData: 'notBreaching',
      AlarmActions: [{ Ref: operatorTopic() }],
      OKActions: [{ Ref: operatorTopic() }],
    });
  });

  test('evaluator DLQ alarm notifies the operator topic', () => {
    template().hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmName: `vsa-staging-avx-auto-approver-dlq-${REGION}`,
      Namespace: 'AWS/SQS',
      MetricName: 'ApproximateNumberOfMessagesVisible',
      Dimensions: [{ Name: 'QueueName', Value: Match.anyValue() }],
      Threshold: 1,
      AlarmActions: [{ Ref: operatorTopic() }],
      OKActions: [{ Ref: operatorTopic() }],
    });
  });

  // The component set is pinned to agents/tier2/contract.py VALID_COMPONENTS by the
  // Python test agents/tier2/tests/test_run_staleness_alarm_pinned.py (CI runs it;
  // jest is in no CI job).
  /** The alarm resource, its m1 metric stat and its FILL expression. */
  function stalenessAlarm(component: string): { props: any; m1: any; expr: any } {
    const id = logicalIdOf('AWS::CloudWatch::Alarm', {
      AlarmName: `vsa-staging-avx-run-stale-${component}-${REGION}`,
    });
    const props = template().toJSON().Resources[id].Properties;
    const m1 = props.Metrics.find((m: any) => m.Id === 'm1');
    const expr = props.Metrics.find((m: any) => m.Expression !== undefined);
    return { props, m1, expr };
  }

  // Review Cycle 10 W1: these pin the property (an empty window of N hours, every hour a
  // datapoint), not just a configuration. A plain metric would let CloudWatch's wider
  // evaluation range use an older heartbeat and ignore missing data.
  test.each(['battery', 'tires', 'brakes', 'diagnostics'])(
    'run-staleness alarm for %s: FILL(m1, 0) over the heartbeat, every hour must be empty',
    (component) => {
      const { props, m1, expr } = stalenessAlarm(component);
      expect(props.Metrics).toHaveLength(2);
      expect(m1.ReturnData).toBe(false);
      expect(m1.MetricStat).toEqual({
        Metric: {
          Namespace: 'CVX/Tier2',
          MetricName: 'RunRecordsWritten',
          // CDK renders dimensions sorted by name; CloudWatch ignores the order.
          Dimensions: [
            { Name: 'component', Value: component },
            { Name: 'stage', Value: 'staging' },
          ],
        },
        Period: 3600,
        Stat: 'Sum',
      });
      expect(expr.Expression).toBe('FILL(m1, 0)');
      // The expression carries no Period of its own in the rendered alarm; the alarm's
      // period is m1's, pinned above at 3600.
      expect(expr.Period).toBeUndefined();
      expect(expr.ReturnData).not.toBe(false);
      expect(props.Threshold).toBe(1);
      expect(props.ComparisonOperator).toBe('LessThanThreshold');
      expect(props.DatapointsToAlarm).toBe(props.EvaluationPeriods);
      expect(props.TreatMissingData).toBe('breaching');
      expect(props.AlarmActions).toEqual([{ Ref: operatorTopic() }]);
      expect(props.OKActions).toEqual([{ Ref: operatorTopic() }]);
      // CloudWatch's limit on Period x EvaluationPeriods for periods of an hour or more.
      expect(3600 * props.EvaluationPeriods).toBeLessThanOrEqual(7 * 86400);
    },
  );

  test.each(['battery', 'tires', 'brakes'])(
    'the %s window outlasts the worst gap between two successful daily runs',
    (component) => {
      // Read the margins from the template, so a longer retry window, more retries or a
      // longer timeout that would make the alarm flap on a late (but successful) run
      // fails here. The worst gap: the 24-hour cadence, plus the scheduler's retry window,
      // plus Lambda's async invocation (the scheduler invokes asynchronously): the first
      // attempt and each async retry can each run to the timeout, with about 1 and 2
      // minutes of backoff between them (review Cycle 11 S2).
      const capitalised = component.charAt(0).toUpperCase() + component.slice(1);
      const schedules = Object.values(template().findResources('AWS::Scheduler::Schedule', {
        Properties: { Name: `vsa-staging-tier2-daily-${component}-${REGION}` },
      })) as any[];
      expect({ capitalised, n: schedules.length }).toEqual({ capitalised, n: 1 });
      const maxEventAge = schedules[0].Properties.Target.RetryPolicy.MaximumEventAgeInSeconds;
      const healthFn = logicalIdOf('AWS::Lambda::Function', { FunctionName: HEALTH_AGENT });
      const timeout = template().toJSON().Resources[healthFn].Properties.Timeout;
      // No EventInvokeConfig means Lambda's default of 2 async retries.
      const invokeConfigs = Object.values(template().findResources('AWS::Lambda::EventInvokeConfig'))
        .filter((c: any) => JSON.stringify(c.Properties.FunctionName).includes(healthFn)) as any[];
      const asyncRetries = invokeConfigs.length ? invokeConfigs[0].Properties.MaximumRetryAttempts : 2;
      const backoff = 3 * 60;
      const worstGap = 86400 + maxEventAge + (1 + asyncRetries) * timeout + backoff;
      const { props } = stalenessAlarm(component);
      // Plus 30 minutes for scheduler jitter and the work before the record is written.
      expect(3600 * props.EvaluationPeriods).toBeGreaterThan(worstGap + 1800);
    },
  );

  test('the diagnostics window covers at least twelve missed 5-minute runs', () => {
    const { props } = stalenessAlarm('diagnostics');
    expect(3600 * props.EvaluationPeriods).toBeGreaterThanOrEqual(3600);
    expect(3600 * props.EvaluationPeriods).toBeLessThanOrEqual(3 * 3600);
  });

  test('exactly seven run-staleness alarms (recall + warranty added by task 4.3)', () => {
    // task 4.3 adds recall and warranty; both alarms are created (ActionsEnabled: false)
    // even in the default synth so the series is watched from day 1.
    // task 4.3 B2 adds service (actions on since FI_FLEET_HEALTH_LIVE = true, Group 6 rest-plan step 7).
    // rebalance (FI stage E 5.2) has its alarm in every synth (actions off until FI_REBALANCING_LIVE;
    // that state is pinned in vsa-tier2-stack-fi-rebalance.test.ts). This list pins existence only.
    const names = Object.values(template().findResources('AWS::CloudWatch::Alarm'))
      .map((a: any) => a.Properties.AlarmName)
      .filter((n: unknown) => typeof n === 'string' && n.includes('-avx-run-stale-'));
    expect(names.sort()).toEqual(['battery', 'brakes', 'cost', 'diagnostics', 'rebalance', 'recall', 'service', 'tires', 'warranty']
      .map((c) => `vsa-staging-avx-run-stale-${c}-${REGION}`));
  });

  test('every Stage A alarm has an action (none is silent)', () => {
    const names = [
      `vsa-staging-avx-auto-approver-errors-${REGION}`,
      `vsa-staging-avx-auto-approver-dlq-${REGION}`,
      ...['battery', 'tires', 'brakes', 'diagnostics'].map((c) => `vsa-staging-avx-run-stale-${c}-${REGION}`),
    ];
    for (const name of names) {
      const id = logicalIdOf('AWS::CloudWatch::Alarm', { AlarmName: name });
      const actions = template().toJSON().Resources[id].Properties.AlarmActions;
      expect({ name, actions }).toEqual({ name, actions: [{ Ref: operatorTopic() }] });
    }
  });
});

// ───────────────────────────────────────────────────────────────────────────
// Global names (Accept 7)
// ───────────────────────────────────────────────────────────────────────────

describe('Stage A global names', () => {
  test('the evaluator IAM role name is region-suffixed and fits in 64 chars for every region', () => {
    // Review Cycle 10 S6: measure the synthesised RoleName, not a string built here.
    const roles = Object.values(template().findResources('AWS::IAM::Role')) as any[];
    const names = roles.map((r) => r.Properties.RoleName)
      .filter((n: unknown) => typeof n === 'string' && n.includes('avx-auto-approver'));
    expect(names).toEqual([EVALUATOR]);
    expect(names[0].endsWith(`-${REGION}`)).toBe(true);
    const longestRegion = 'ap-southeast-7';
    expect(names[0].replace(REGION, longestRegion).length).toBeLessThanOrEqual(64);
  });
});
