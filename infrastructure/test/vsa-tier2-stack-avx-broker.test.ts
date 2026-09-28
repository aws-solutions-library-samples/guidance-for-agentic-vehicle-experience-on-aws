/**
 * AVX broker CDK wiring — Fix Group 32 (F32.8, F32.10, F32.11).
 *
 * Spec: .kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/tasks-g7-g9-fragment.md
 * Issue: issues/2026-09-24-tier2-operator-alarms-never-delivered/
 *
 * These assertions prove the template is shaped right. They cannot prove an
 * alarm notification is delivered: that needs a deploy, `aws cloudwatch
 * set-alarm-state`, and an Action history item reading "Successfully
 * executed". jest is also in no CI job, so this file is local-only until a
 * follow-on wires it (the Python target-system pin runs in CI instead).
 */
import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { VsaTier2Stack } from '../lib/vsa-tier2-stack';

const ACCOUNT = '123456789012';
const REGION = 'us-west-2';
const IMPORTED_CMK = `arn:aws:kms:${REGION}:${ACCOUNT}:key/test-key-id`;

let cached: Template | undefined;

function template(): Template {
  if (cached) return cached;
  const prev = process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT;
  process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT = 'ops@example.invalid';
  try {
    const app = new cdk.App();
    const stack = new VsaTier2Stack(app, 'vsa-staging-tier2', {
      env: { account: ACCOUNT, region: REGION },
      stage: 'staging',
      adpRegion: 'us-east-1',
      adpAccountId: ACCOUNT,
      cmsKmsKeyArn: { literal: IMPORTED_CMK, parameterName: '/cvx/staging/cms-kms-key-arn' },
    });
    cached = Template.fromStack(stack);
    return cached;
  } finally {
    if (prev === undefined) delete process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT;
    else process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT = prev;
  }
}

function logicalIdOf(type: string, props: Record<string, unknown>): string {
  const found = template().findResources(type, { Properties: props });
  const ids = Object.keys(found);
  expect(ids).toHaveLength(1);
  return ids[0];
}

const OPERATOR_TOPIC = 'vsa-staging-tier2-operator-alerts';
const ESCALATION_TOPIC = 'vsa-staging-avx-action-terminal-failure-alerts';
const TARGET_SYSTEMS = ['chat', 'dms', 'ios-owner', 'none', 'retail', 'vehicle-command'];

// ───────────────────────────────────────────────────────────────────────────
// F32.10 — alert topics must be publishable by CloudWatch
// ───────────────────────────────────────────────────────────────────────────

describe('F32.10 alert-topic encryption and policy', () => {
  test('a stack-owned CMK with rotation and RETAIN exists', () => {
    const keyId = logicalIdOf('AWS::KMS::Key', { EnableKeyRotation: true });
    const key = template().toJSON().Resources[keyId];
    expect(key.DeletionPolicy).toBe('Retain');
    template().hasResourceProperties('AWS::KMS::Alias', {
      AliasName: 'alias/vsa-staging-tier2-alerts',
      TargetKeyId: { 'Fn::GetAtt': [keyId, 'Arn'] },
    });
  });

  test('key policy admits cloudwatch.amazonaws.com for Decrypt + GenerateDataKey* from this account only', () => {
    const keyId = logicalIdOf('AWS::KMS::Key', { EnableKeyRotation: true });
    const statements: any[] = template().toJSON().Resources[keyId].Properties.KeyPolicy.Statement;
    const cw = statements.filter((s) => s.Principal?.Service === 'cloudwatch.amazonaws.com');
    expect(cw).toHaveLength(1);
    expect([...cw[0].Action].sort()).toEqual(['kms:Decrypt', 'kms:GenerateDataKey*']);
    expect(cw[0].Condition).toEqual({ StringEquals: { 'aws:SourceAccount': ACCOUNT } });
    // No other service principal on the alerts key.
    const services = statements.map((s) => s.Principal?.Service).filter(Boolean);
    expect(services).toEqual(['cloudwatch.amazonaws.com']);
  });

  test.each([OPERATOR_TOPIC, ESCALATION_TOPIC])('%s uses the stack CMK, not the imported cmsKmsKey', (name) => {
    const keyId = logicalIdOf('AWS::KMS::Key', { EnableKeyRotation: true });
    const topic = template().findResources('AWS::SNS::Topic', { Properties: { TopicName: name } });
    const [props] = Object.values(topic).map((t: any) => t.Properties);
    expect(props.KmsMasterKeyId).toEqual({ 'Fn::GetAtt': [keyId, 'Arn'] });
    expect(JSON.stringify(props.KmsMasterKeyId)).not.toContain('test-key-id');
  });

  test.each([OPERATOR_TOPIC, ESCALATION_TOPIC])('%s policy allows CloudWatch alarms alongside the SSL deny', (name) => {
    const topicId = logicalIdOf('AWS::SNS::Topic', { TopicName: name });
    const policies = Object.values(template().findResources('AWS::SNS::TopicPolicy'))
      .filter((p: any) => JSON.stringify(p.Properties.Topics).includes(topicId));
    expect(policies).toHaveLength(1);
    const statements: any[] = (policies[0] as any).Properties.PolicyDocument.Statement;
    const allow = statements.filter(
      (s) => s.Effect === 'Allow' && s.Principal?.Service === 'cloudwatch.amazonaws.com',
    );
    expect(allow).toHaveLength(1);
    expect(allow[0].Action).toBe('sns:Publish');
    expect(allow[0].Condition.StringEquals).toEqual({ 'aws:SourceAccount': ACCOUNT });
    expect(JSON.stringify(allow[0].Condition.ArnLike)).toContain(':alarm:*');
    expect(statements.some((s) => s.Effect === 'Deny'
      && JSON.stringify(s.Condition).includes('aws:SecureTransport'))).toBe(true);
  });
});

// ───────────────────────────────────────────────────────────────────────────
// F32.8 — the escalation topic has producers; the broker DLQ has an alarm
// ───────────────────────────────────────────────────────────────────────────

describe('F32.8 escalation alarms', () => {
  const escalationTopicId = () => logicalIdOf('AWS::SNS::Topic', { TopicName: ESCALATION_TOPIC });

  test.each(TARGET_SYSTEMS)('terminal-failure alarm for target_system=%s publishes to the escalation topic', (target) => {
    template().hasResourceProperties('AWS::CloudWatch::Alarm', {
      Namespace: 'AVX/Executor',
      MetricName: 'AVXActionTerminalFailure',
      Dimensions: [{ Name: 'target_system', Value: target }],
      Statistic: 'Sum',
      Threshold: 1,
      ComparisonOperator: 'GreaterThanOrEqualToThreshold',
      TreatMissingData: 'notBreaching',
      AlarmActions: [{ Ref: escalationTopicId() }],
    });
  });

  test('exactly one terminal-failure alarm per target system', () => {
    const alarms = template().findResources('AWS::CloudWatch::Alarm', {
      Properties: { MetricName: 'AVXActionTerminalFailure' },
    });
    expect(Object.keys(alarms)).toHaveLength(TARGET_SYSTEMS.length);
  });

  test('broker DLQ alarm publishes to the escalation topic', () => {
    template().hasResourceProperties('AWS::CloudWatch::Alarm', {
      Namespace: 'AWS/SQS',
      MetricName: 'ApproximateNumberOfMessagesVisible',
      Dimensions: [{ Name: 'QueueName', Value: Match.anyValue() }],
      Threshold: 1,
      AlarmActions: [{ Ref: escalationTopicId() }],
    });
  });

  test('broker event source reports batch item failures into the DLQ', () => {
    template().hasResourceProperties('AWS::Lambda::EventSourceMapping', {
      FunctionName: Match.anyValue(),
      FunctionResponseTypes: ['ReportBatchItemFailures'],
      BisectBatchOnFunctionError: true,
      MaximumRetryAttempts: 3,
      DestinationConfig: { OnFailure: { Destination: Match.anyValue() } },
    });
  });
});

// ───────────────────────────────────────────────────────────────────────────
// F32.11 — broker IAM matches what the code calls
// ───────────────────────────────────────────────────────────────────────────

describe('F32.11 broker role', () => {
  function brokerStatements(): any[] {
    const roleId = logicalIdOf('AWS::IAM::Role', { RoleName: 'vsa-staging-avx-broker-role-us-west-2' });
    return Object.values(template().findResources('AWS::IAM::Policy'))
      .filter((p: any) => JSON.stringify(p.Properties.Roles).includes(roleId))
      .flatMap((p: any) => p.Properties.PolicyDocument.Statement);
  }
  const asList = (x: unknown) => (Array.isArray(x) ? x : [x]);
  const tableId = (name: string) => logicalIdOf('AWS::DynamoDB::Table', { TableName: name });
  /** Every CloudFormation logical ID a Resource list references, sorted. */
  const refs = (resource: unknown): string[] => {
    const blob = JSON.stringify(resource);
    const ids = new Set<string>();
    for (const m of blob.matchAll(/"Fn::GetAtt":\["([A-Za-z0-9]+)"/g)) ids.add(m[1]);
    for (const m of blob.matchAll(/"Ref":"([A-Za-z0-9]+)"/g)) ids.add(m[1]);
    return [...ids].sort();
  };

  // Review Cycle 5 W4 / Cycle 6 W3: the Accept asks for the statement set
  // EXACTLY, per Sid. Cycle 5 found a presence check; Cycle 6 found that
  // comparing only the referenced logical IDs still let a grant widen to
  // `table/*` (a string ARN), `index/*`, or move off its index, at 83/83 green.
  // So the whole Resource value is compared.
  test('every Sid statement has exactly the expected actions and the exact Resource', () => {
    const arn = (id: string) => ({ 'Fn::GetAtt': [id, 'Arn'] });
    const join = (id: string, suffix: string) => ({ 'Fn::Join': ['', [arn(id), suffix]] });
    const findings = tableId('vsa-staging-avx-findings');
    const actions = tableId('vsa-staging-avx-actions');
    const logGroup = `arn:aws:logs:${REGION}:${ACCOUNT}:log-group:/aws/lambda/vsa-staging-avx-broker-${REGION}`;
    const expected: Record<string, { actions: string[]; resource: unknown }> = {
      BrokerCloudWatchLogs:           { actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'], resource: [logGroup, `${logGroup}:*`] },
      BrokerAvxFindingsStreamRead:    { actions: ['dynamodb:DescribeStream', 'dynamodb:GetRecords', 'dynamodb:GetShardIterator', 'dynamodb:ListStreams'], resource: join(findings, '/stream/*') },
      BrokerAvxActionsWrite:          { actions: ['dynamodb:PutItem'], resource: arn(actions) },
      BrokerAvxActionsCreatedByQuery: { actions: ['dynamodb:Query'], resource: join(actions, '/index/by_created_by') },
      BrokerAvxRateLimitsReadWrite:   { actions: ['dynamodb:UpdateItem'], resource: arn(tableId('vsa-staging-avx-rate-limits')) },
      BrokerNotificationDevicesRead:  { actions: ['dynamodb:Query'], resource: arn(tableId('vsa-staging-notification-devices')) },
      BrokerAvxFeedbackRead:          { actions: ['dynamodb:Query'], resource: arn(tableId('vsa-staging-avx-feedback')) },
      BrokerAvxActionsGetItem:        { actions: ['dynamodb:GetItem'], resource: arn(actions) },
      BrokerSnsPublish:               { actions: ['sns:Publish'], resource: `arn:aws:sns:${REGION}:${ACCOUNT}:endpoint/*/*/*` },
      BrokerSsmPolicyRead:            { actions: ['ssm:GetParameter'], resource: `arn:aws:ssm:${REGION}:${ACCOUNT}:parameter/vsa/staging/avx/broker/*` },
    };
    const actual: Record<string, { actions: string[]; resource: unknown }> = {};
    for (const s of brokerStatements().filter((x) => x.Sid)) {
      actual[s.Sid] = { actions: asList(s.Action).sort(), resource: s.Resource };
    }
    expect(actual).toEqual(expected);
  });

  test('the GetItem grant is the actions table ARN itself, not an index', () => {
    const s = brokerStatements().find((x) => x.Sid === 'BrokerAvxActionsGetItem');
    expect(s.Resource).toEqual({ 'Fn::GetAtt': [tableId('vsa-staging-avx-actions'), 'Arn'] });
  });

  test('statements CDK adds without a Sid are exactly the event-source and DLQ grants', () => {
    const findings = tableId('vsa-staging-avx-findings');
    const dlq = logicalIdOf('AWS::SQS::Queue', { QueueName: 'vsa-staging-avx-broker-dlq' });
    const actual = brokerStatements().filter((x) => !x.Sid)
      .map((s) => ({ actions: asList(s.Action).sort(), resource: s.Resource }));
    expect(actual).toEqual(expect.arrayContaining([
      { actions: ['sqs:GetQueueAttributes', 'sqs:GetQueueUrl', 'sqs:SendMessage'], resource: { 'Fn::GetAtt': [dlq, 'Arn'] } },
      // aws-cdk-lib Table.grantListStreams hardcodes "*"; see vsa-tier2-stack-avx.test.ts.
      { actions: ['dynamodb:ListStreams'], resource: '*' },
      { actions: ['dynamodb:DescribeStream', 'dynamodb:GetRecords', 'dynamodb:GetShardIterator'], resource: { 'Fn::GetAtt': [findings, 'StreamArn'] } },
    ]));
    expect(actual).toHaveLength(3);
  });

  test('sns:Publish is granted on platform endpoints only, never on a topic', () => {
    const publish = brokerStatements().filter((x) => asList(x.Action).includes('sns:Publish'));
    expect(publish).toHaveLength(1);
    const resources = asList(publish[0].Resource);
    expect(resources).toHaveLength(1);
    expect(JSON.stringify(resources[0])).toContain(':endpoint/*/*/*');
    expect(refs(resources)).toEqual([]);
  });

  test('the broker role cannot delete from any table', () => {
    const acts = brokerStatements().flatMap((x) => asList(x.Action));
    expect(acts).not.toContain('dynamodb:DeleteItem');
  });
});


// ───────────────────────────────────────────────────────────────────────────
// Deploy-time character-set constraint that synth does not check
// ───────────────────────────────────────────────────────────────────────────

describe('IAM description character set', () => {
  // IAM's documented pattern for Role (and Policy) Description. CDK synth does
  // not validate it; CloudFormation fails the deploy instead. On 2026-09-24 an
  // em dash (U+2014) in the broker role's description rolled back the whole
  // vsa-staging-tier2 update (issues/2026-09-24-tier2-deploy-iam-description-charset/).
  // Scheduler and CloudWatch accept U+2014, which is why earlier descriptions
  // in this stack never tripped it.
  const IAM_DESCRIPTION = /^[\u0009\u000A\u000D\u0020-\u007E\u00A1-\u00FF]*$/;

  test('every IAM Role and ManagedPolicy description is within IAM\'s allowed characters', () => {
    const offenders: string[] = [];
    for (const type of ['AWS::IAM::Role', 'AWS::IAM::ManagedPolicy']) {
      for (const [id, r] of Object.entries(template().findResources(type))) {
        const d = (r as any).Properties?.Description;
        if (typeof d === 'string' && !IAM_DESCRIPTION.test(d)) offenders.push(`${id}: ${JSON.stringify(d)}`);
      }
    }
    expect(offenders).toEqual([]);
  });

  test('the check has something to check (anti-vacuity)', () => {
    const described = Object.values(template().findResources('AWS::IAM::Role'))
      .filter((r: any) => typeof r.Properties?.Description === 'string');
    expect(described.length).toBeGreaterThanOrEqual(1);
  });
});
