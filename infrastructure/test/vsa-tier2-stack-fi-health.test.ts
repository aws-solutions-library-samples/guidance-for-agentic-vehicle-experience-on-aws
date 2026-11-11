/**
 * task 4.3 B1 (spec 2026-10-05-fi-fleet-health-stage-d): Tier 2 stack investigations
 * table, DLQ, queue, SQS event source, alarms, and IAM grants.
 *
 * Two synths:
 *   default — no prop: FI_FLEET_HEALTH_LIVE decides (true since Group 6 rest-plan step 7)
 *   live    — fiFleetHealthLive = true (explicit prop)
 *   off     — fiFleetHealthLive = false (explicit prop; the gate, and a rollback)
 *
 * IAM is compared EXACTLY per Sid so widened grants fail at green.
 * Mutations recorded below.
 */

import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { VsaTier2Stack } from '../lib/vsa-tier2-stack';
import { FI_FAULT_GROUNDING_LIVE } from '../lib/fi-fleet-health-live';
import { TEST_CMS_FI_INTERNAL_API_ENDPOINT } from './helpers/cms-fi-internal-endpoint';
import { TEST_DMS_FI_INTERNAL_API_ENDPOINT } from './helpers/dms-fi-internal-endpoint';

const ACCOUNT    = '123456789012';
const REGION     = 'us-west-2';
const ADP_REGION = 'us-east-1';
const HEALTH_AGENT = `vsa-staging-tier2-health-agent-${REGION}`;

function makeStack(fiFleetHealthLive?: boolean, cmsUserPoolId?: string): Template {
  const prev = process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT;
  process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT = 'ops@example.invalid';
  try {
    const app = new cdk.App();
    const stack = new VsaTier2Stack(app, 'vsa-staging-tier2', {
      cmsFiInternalApiEndpoint: TEST_CMS_FI_INTERNAL_API_ENDPOINT,
      dmsFiInternalApiEndpoint: TEST_DMS_FI_INTERNAL_API_ENDPOINT,
      env: { account: ACCOUNT, region: REGION },
      stage: 'staging',
      adpRegion: ADP_REGION,
      adpAccountId: ACCOUNT,
      cmsKmsKeyArn: {
        literal: `arn:aws:kms:${REGION}:${ACCOUNT}:key/test-key-id`,
        parameterName: '/cvx/staging/cms-kms-key-arn',
      },
      ...(cmsUserPoolId !== undefined ? { cmsUserPoolId } : {}),
      ...(fiFleetHealthLive !== undefined ? { fiFleetHealthLive } : {}),
    });
    return Template.fromStack(stack);
  } finally {
    if (prev === undefined) delete process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT;
    else process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT = prev;
  }
}

let cachedDefault:     Template | undefined;
let cachedLive:        Template | undefined;
let cachedOff:         Template | undefined;
let cachedWithPoolId:  Template | undefined;
/** C1: no explicit prop — the ?? FI_FLEET_HEALTH_LIVE default is the gate (true since Group 6). */
function defaultStack():    Template { return (cachedDefault    ??= makeStack());      }
function liveStack():       Template { return (cachedLive       ??= makeStack(true));  }
/** The switch turned back off with an explicit prop: what a rollback to false deploys. */
function offStack():        Template { return (cachedOff        ??= makeStack(false)); }
/** W2: mirrors bin/vsa.ts which always passes cmsUserPoolId. */
// The pool id stays under 8 characters after the underscore: the publish scanner flags pool-id shapes.
function withPoolIdStack():  Template { return (cachedWithPoolId ??= makeStack(false, 'us-east-1_TestP1')); }

const asList = (x: unknown): unknown[] => (Array.isArray(x) ? x : [x]);

function tableId(t: Template, tableName: string): string {
  const ids = Object.keys(t.findResources('AWS::DynamoDB::Table', { Properties: { TableName: tableName } }));
  expect(ids).toHaveLength(1);
  return ids[0];
}

function healthAgentStatements(t: Template): any[] {
  const fns = t.findResources('AWS::Lambda::Function', { Properties: { FunctionName: HEALTH_AGENT } });
  const fnId = Object.keys(fns)[0];
  const roleRef = JSON.stringify(t.toJSON().Resources[fnId].Properties.Role);
  const roleId = /"Fn::GetAtt":\["([A-Za-z0-9]+)"/.exec(roleRef)![1];
  // Collect statements from both the inline AWS::IAM::Policy (CDK default) and any
  // explicit AWS::IAM::ManagedPolicy attached via roles:[lambdaRole] (W2 fix).
  const inlineStatements = Object.values(t.findResources('AWS::IAM::Policy'))
    .filter((p: any) => JSON.stringify(p.Properties.Roles).includes(roleId))
    .flatMap((p: any) => p.Properties.PolicyDocument.Statement);
  const managedStatements = Object.values(t.findResources('AWS::IAM::ManagedPolicy'))
    .filter((p: any) => JSON.stringify(p.Properties.Roles ?? []).includes(roleId))
    .flatMap((p: any) => p.Properties.PolicyDocument.Statement);
  return [...inlineStatements, ...managedStatements];
}

function bySid(t: Template, sid: string): any {
  const found = healthAgentStatements(t).filter((s: any) => s.Sid === sid);
  expect(found).toHaveLength(1);
  return found[0];
}

/** IAM resource glob (`*`, `?`) → RegExp, for the denial tests. */
function globMatches(resource: unknown, target: string): boolean {
  if (typeof resource !== 'string') return false;
  const re = new RegExp('^' + resource.replace(/[.+^${}()|[\]\\]/g, '\\$&')
    .replace(/\*/g, '.*').replace(/\?/g, '.') + '$');
  return re.test(target);
}

// ── Table ──────────────────────────────────────────────────────────────────

describe('avx-investigations table', () => {
  test('exists with PK investigation_id (S), PAY_PER_REQUEST, PITR, RETAIN', () => {
    const t = defaultStack();
    const id = tableId(t, 'vsa-staging-avx-investigations');
    const props = t.toJSON().Resources[id].Properties;
    expect(props.KeySchema).toContainEqual({ AttributeName: 'investigation_id', KeyType: 'HASH' });
    expect(props.BillingMode).toBe('PAY_PER_REQUEST');
    expect(props.PointInTimeRecoverySpecification).toEqual({ PointInTimeRecoveryEnabled: true });
    expect(t.toJSON().Resources[id].DeletionPolicy).toBe('Retain');
  });

  test('CfnOutput exportName vsa-staging-avx-investigations-table-name', () => {
    const outputs = defaultStack().toJSON().Outputs ?? {};
    const found = Object.values(outputs).find(
      (o: any) => o.Export?.Name === 'vsa-staging-avx-investigations-table-name',
    );
    expect(found).toBeDefined();
  });
});

// ── Queue ──────────────────────────────────────────────────────────────────

describe('avx-investigations queue', () => {
  test('visibility 5400 s', () => {
    const queues = Object.values(defaultStack().findResources('AWS::SQS::Queue', {
      Properties: { QueueName: 'vsa-staging-avx-investigations' },
    })) as any[];
    expect(queues).toHaveLength(1);
    expect(queues[0].Properties.VisibilityTimeout).toBe(5400);
  });

  test('redrive: maxReceiveCount 2, points to the DLQ', () => {
    const t = defaultStack();
    const dlqIds = Object.keys(t.findResources('AWS::SQS::Queue', {
      Properties: { QueueName: 'vsa-staging-avx-investigations-dlq' },
    }));
    expect(dlqIds).toHaveLength(1);
    const dlqArn = { 'Fn::GetAtt': [dlqIds[0], 'Arn'] };

    const queueProps = (Object.values(t.findResources('AWS::SQS::Queue', {
      Properties: { QueueName: 'vsa-staging-avx-investigations' },
    })) as any[])[0].Properties;
    expect(queueProps.RedrivePolicy.maxReceiveCount).toBe(2);
    expect(queueProps.RedrivePolicy.deadLetterTargetArn).toEqual(dlqArn);
  });

  test('DLQ 14-day retention', () => {
    const dlqProps = (Object.values(defaultStack().findResources('AWS::SQS::Queue', {
      Properties: { QueueName: 'vsa-staging-avx-investigations-dlq' },
    })) as any[])[0].Properties;
    expect(dlqProps.MessageRetentionPeriod).toBe(14 * 86400);
  });

  // Security review g43 cycle 1 W1: neither encryption at rest nor the TLS-only policy was
  // pinned, so dropping both from the queue and DLQ passed every test.
  test.each([
    'vsa-staging-avx-investigations',
    'vsa-staging-avx-investigations-dlq',
  ])('%s: SSE-SQS at rest, and a policy denying every non-TLS request', (queueName) => {
    const t = defaultStack();
    const queues = t.findResources('AWS::SQS::Queue', { Properties: { QueueName: queueName } });
    const ids = Object.keys(queues);
    expect(ids).toHaveLength(1);
    const props = (queues[ids[0]] as any).Properties;
    expect(props.SqsManagedSseEnabled).toBe(true);
    expect(props.KmsMasterKeyId).toBeUndefined();

    const policies = Object.values(t.findResources('AWS::SQS::QueuePolicy')) as any[];
    const mine = policies.filter((p) =>
      (p.Properties.Queues ?? []).some((q: any) => q?.Ref === ids[0]));
    expect(mine).toHaveLength(1);
    const denyTls = (mine[0].Properties.PolicyDocument.Statement as any[]).filter((s) =>
      s.Effect === 'Deny'
      && s.Condition?.Bool?.['aws:SecureTransport'] === 'false'
      && JSON.stringify(s.Principal) === JSON.stringify({ AWS: '*' })
      && ([] as string[]).concat(s.Action).includes('sqs:*'));
    expect(denyTls).toHaveLength(1);
    expect(denyTls[0].Resource).toEqual({ 'Fn::GetAtt': [ids[0], 'Arn'] });
  });
});

// ── SQS event source ────────────────────────────────────────────────────────

describe('SQS event source', () => {
  function investigationsQueueId(t: Template): string {
    const ids = Object.keys(t.findResources('AWS::SQS::Queue', {
      Properties: { QueueName: 'vsa-staging-avx-investigations' },
    }));
    expect(ids).toHaveLength(1);
    return ids[0];
  }

  function investigationsEventSourceMapping(t: Template): any {
    const queueId  = investigationsQueueId(t);
    const fnId     = Object.keys(t.findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: HEALTH_AGENT },
    }))[0];
    const mappings = Object.values(t.findResources('AWS::Lambda::EventSourceMapping', {
      Properties: {
        EventSourceArn: { 'Fn::GetAtt': [queueId, 'Arn'] },
        FunctionName:   { Ref: fnId },
      },
    })) as any[];
    expect(mappings).toHaveLength(1);
    return mappings[0].Properties;
  }

  test('event source present, BatchSize 1, ReportBatchItemFailures, MaximumConcurrency 2', () => {
    const p = investigationsEventSourceMapping(defaultStack());
    expect(p.BatchSize).toBe(1);
    expect(p.FunctionResponseTypes).toContain('ReportBatchItemFailures');
    expect(p.ScalingConfig?.MaximumConcurrency).toBe(2);
  });

  // Group 6 rest-plan step 7: the default is now live. offStack() keeps the gate tested.
  test('M1: event source Enabled=false when fiFleetHealthLive=false', () => {
    const p = investigationsEventSourceMapping(offStack());
    expect(p.Enabled).toBe(false);
  });

  test('event source Enabled=true when fiFleetHealthLive=true', () => {
    const p = investigationsEventSourceMapping(liveStack());
    // CDK omits Enabled from the template when true (it is the default).
    expect(p.Enabled).not.toBe(false);
  });

  test('G6: the default synth (no prop, the shared constant) enables the event source', () => {
    const p = investigationsEventSourceMapping(defaultStack());
    expect(p.Enabled).not.toBe(false);
  });

  // Security g43 c1 S-c, carried to this flip: the mapping may not start delivering
  // investigations before the managed policy holding the job's grants exists.
  test.each([['default', defaultStack], ['off', offStack]])(
    'S-c: the event source DependsOn FiHealthTier2Policy (%s synth)', (_n, mk) => {
      const t = mk();
      const policyIds = Object.keys(t.findResources('AWS::IAM::ManagedPolicy'))
        .filter((id) => id.startsWith('FiHealthTier2Policy'));
      expect(policyIds).toHaveLength(1);
      const queueId = investigationsQueueId(t);
      const esms = Object.values(t.toJSON().Resources).filter((r: any) =>
        r.Type === 'AWS::Lambda::EventSourceMapping'
        && JSON.stringify(r.Properties.EventSourceArn) === JSON.stringify({ 'Fn::GetAtt': [queueId, 'Arn'] })) as any[];
      expect(esms).toHaveLength(1);
      expect(asList(esms[0].DependsOn ?? [])).toContain(policyIds[0]);
    });
});

// ── Alarms ─────────────────────────────────────────────────────────────────

function operatorTopicArn(t: Template): unknown {
  const topics = t.findResources('AWS::SNS::Topic', {
    Properties: { TopicName: 'vsa-staging-tier2-operator-alerts' },
  });
  const id = Object.keys(topics)[0];
  return { Ref: id };
}

function alarmByName(t: Template, name: string): any {
  const ids = Object.keys(t.findResources('AWS::CloudWatch::Alarm', {
    Properties: { AlarmName: name },
  }));
  expect(ids).toHaveLength(1);
  return t.toJSON().Resources[ids[0]].Properties;
}

describe('AVX investigations alarms', () => {
  test('DLQ depth alarm: SQS/ApproximateNumberOfMessagesVisible, threshold 1, NOT_BREACHING', () => {
    const p = alarmByName(
      defaultStack(),
      `vsa-staging-avx-investigations-dlq-depth-${REGION}`,
    );
    expect(p.Namespace).toBe('AWS/SQS');
    expect(p.MetricName).toBe('ApproximateNumberOfMessagesVisible');
    expect(p.Threshold).toBe(1);
    expect(p.TreatMissingData).toBe('notBreaching');
    expect(p.ComparisonOperator).toBe('GreaterThanOrEqualToThreshold');
    // AlarmActions points at the operator topic.
    expect(asList(p.AlarmActions)).toHaveLength(1);
    expect(asList(p.AlarmActions)[0]).toEqual(operatorTopicArn(defaultStack()));
  });

  // Mutation M2: maxReceiveCount 3 on the DLQ → redrive test fails (checked separately above).
  // The DLQ depth alarm is the proof it is wired.

  test('InvestigationMarkFailedError alarm: AVX/Investigations namespace, threshold 1, no dimensions, NOT_BREACHING', () => {
    const p = alarmByName(
      defaultStack(),
      `vsa-staging-avx-investigations-mark-failed-error-${REGION}`,
    );
    expect(p.Namespace).toBe('AVX/Investigations');
    expect(p.MetricName).toBe('InvestigationMarkFailedError');
    expect(p.Dimensions).toBeUndefined();
    expect(p.Threshold).toBe(1);
    expect(p.TreatMissingData).toBe('notBreaching');
    expect(asList(p.AlarmActions)).toHaveLength(1);
    expect(asList(p.AlarmActions)[0]).toEqual(operatorTopicArn(defaultStack()));
  });

  test('QualityIssueHookFailure alarm: AVX/Investigations namespace, threshold 1, no dimensions, NOT_BREACHING', () => {
    const p = alarmByName(
      defaultStack(),
      `vsa-staging-avx-investigations-hook-failure-${REGION}`,
    );
    expect(p.Namespace).toBe('AVX/Investigations');
    expect(p.MetricName).toBe('QualityIssueHookFailure');
    expect(p.Dimensions).toBeUndefined();
    expect(p.Threshold).toBe(1);
    expect(p.TreatMissingData).toBe('notBreaching');
    expect(asList(p.AlarmActions)).toHaveLength(1);
    expect(asList(p.AlarmActions)[0]).toEqual(operatorTopicArn(defaultStack()));
  });
});

// ── IAM ────────────────────────────────────────────────────────────────────

describe('FiHealthInvestigationsTable', () => {
  test('exactly GetItem and UpdateItem on the investigations table ARN only', () => {
    const t = defaultStack();
    const st = bySid(t, 'FiHealthInvestigationsTable');
    expect(st.Effect).toBe('Allow');
    expect(asList(st.Action).sort()).toEqual(['dynamodb:GetItem', 'dynamodb:UpdateItem']);
    const tid = tableId(t, 'vsa-staging-avx-investigations');
    expect(st.Resource).toEqual({ 'Fn::GetAtt': [tid, 'Arn'] });
  });
});

describe('FiHealthGlueMetadataRead', () => {
  const G = `arn:aws:glue:${ADP_REGION}:${ACCOUNT}`;

  test('exactly the four Glue actions', () => {
    const st = bySid(defaultStack(), 'FiHealthGlueMetadataRead');
    expect(st.Effect).toBe('Allow');
    expect(asList(st.Action).sort()).toEqual([
      'glue:GetDatabase', 'glue:GetPartitions', 'glue:GetTable', 'glue:GetTables',
    ]);
  });

  test('exact resource list — 3 databases + 11 named tables', () => {
    const st = bySid(defaultStack(), 'FiHealthGlueMetadataRead');
    expect(asList(st.Resource)).toEqual([
      // fleet_health
      `${G}:database/adp_staging_fleet_health`,
      `${G}:table/adp_staging_fleet_health/vehicle_fault_status_daily`,
      `${G}:table/adp_staging_fleet_health/pm_vehicle_status_daily`,
      `${G}:table/adp_staging_fleet_health/vehicle_utilisation_daily`,
      `${G}:table/adp_staging_fleet_health/fault_codes`,
      `${G}:table/adp_staging_fleet_health/snapshot_runs`,
      // engineering
      `${G}:database/adp_staging_engineering`,
      `${G}:table/adp_staging_engineering/fault_patterns`,
      `${G}:table/adp_staging_engineering/vehicle_build_components`,
      `${G}:table/adp_staging_engineering/engineering_failure_modes`,
      `${G}:table/adp_staging_engineering/engineering_remedy_options`,
      // ota_campaigns
      `${G}:database/adp_staging_ota_campaigns`,
      `${G}:table/adp_staging_ota_campaigns/ota_campaigns`,
      `${G}:table/adp_staging_ota_campaigns/ota_campaign_events`,
    ]);
  });

  // M3: a fleet_health table wildcard on the role — no Glue Allow on the role
  // reaches table/adp_staging_fleet_health/* as a wildcard.
  test('M3: no Glue Allow on the role reaches a fleet_health table wildcard', () => {
    const t = defaultStack();
    // Positive control: the legitimate named table IS reached.
    const positiveTarget = `${G}:table/adp_staging_fleet_health/vehicle_fault_status_daily`;
    const wildcardTarget = `${G}:table/adp_staging_fleet_health/any_other_table`;

    const glueAllow = (s: any) => s.Effect === 'Allow' &&
      asList(s.Action).some((a: any) => String(a).startsWith('glue:'));

    // Scan both inline (AWS::IAM::Policy) and explicit (AWS::IAM::ManagedPolicy) resources
    // so that the positive control still finds FiHealthGlueMetadataRead after the W2 move.
    const allPolicies = [
      ...(Object.values(t.findResources('AWS::IAM::Policy')) as any[]),
      ...(Object.values(t.findResources('AWS::IAM::ManagedPolicy')) as any[]),
    ].flatMap((p) => p.Properties.PolicyDocument.Statement)
      .filter((s: any) => glueAllow(s));

    const reaches = (target: string) => allPolicies
      .filter((s: any) => asList(s.Resource).some((r) => globMatches(r, target)))
      .map((s: any) => String(s.Sid));

    // Positive control: named table is reached (proves the test is not vacuous).
    expect(reaches(positiveTarget)).toContain('FiHealthGlueMetadataRead');
    // Denial: wildcard-matched target is NOT reached.
    expect(reaches(wildcardTarget)).not.toContain('FiHealthGlueMetadataRead');
    expect(reaches(wildcardTarget)).toHaveLength(0);
  });

  // C3: the role must never grant Glue access to any adp_staging_cms_ingest object.
  // Scans all Glue Allow statements on the health-agent role (inline + managed policies).
  // Uses correct ARN paths per tasks.md 4.3 (adp_{stage}_cms_ingest).
  // Positive control: a legitimate fleet_health table IS reached (anti-vacuity).
  // M3: a new statement granting glue:GetTable on table/adp_staging_cms_ingest/vehicles
  //     must fail this test regardless of whether it lands in inline or managed policy.
  test('C3: no Glue Allow on the role reaches any adp_staging_cms_ingest object', () => {
    const t = defaultStack();
    const G = `arn:aws:glue:${ADP_REGION}:${ACCOUNT}`;

    const glueAllows = healthAgentStatements(t).filter((s: any) =>
      s.Effect === 'Allow' && asList(s.Action).some((a: any) => String(a).startsWith('glue:')));

    const reaches = (target: string) =>
      glueAllows.filter((s: any) => asList(s.Resource).some((r) => globMatches(r, target)));

    // Positive control: the legitimate fleet_health table is reached (test is not vacuous).
    expect(reaches(`${G}:table/adp_staging_fleet_health/vehicle_fault_status_daily`))
      .not.toHaveLength(0);

    // Denial: no Glue Allow on the role reaches any cms_ingest object.
    const cmsIngestTargets = [
      `${G}:database/adp_staging_cms_ingest`,
      `${G}:table/adp_staging_cms_ingest/vin_cms_fleet`,
      `${G}:table/adp_staging_cms_ingest/vehicles`,
      `${G}:table/adp_staging_cms_ingest/fleets`,
      `${G}:table/adp_staging_cms_ingest/fleet_enrollment`,
      `${G}:table/adp_staging_cms_ingest/pm_plans`,
      `${G}:table/adp_staging_cms_ingest/pm_operator_completions`,
    ];
    for (const target of cmsIngestTargets) {
      expect(reaches(target)).toHaveLength(0);
    }
  });
});

describe('FiHealthFindingsByFleetQuery', () => {
  // W1: runner.py select_agent2_findings calls dynamodb:Query with IndexName "by_fleet".
  // The grant must be exactly on the by_fleet GSI — not the table, not any other index.
  test('exactly dynamodb:Query on the avx-findings by_fleet index only', () => {
    const t = defaultStack();
    const st = bySid(t, 'FiHealthFindingsByFleetQuery');
    expect(st.Effect).toBe('Allow');
    expect(asList(st.Action)).toEqual(['dynamodb:Query']);
    const fid = tableId(t, 'vsa-staging-avx-findings');
    expect(st.Resource).toEqual({
      'Fn::Join': ['', [{ 'Fn::GetAtt': [fid, 'Arn'] }, '/index/by_fleet']],
    });
  });
});

// W2: The FI health statements are in an explicit ManagedPolicy, not the role's inline
// policy. CDK must not have emitted an overflow policy in either fixture layout.
describe('W2: no OverflowPolicy in any fixture layout', () => {
  test('default fixture (no cmsUserPoolId): no resource logical id contains OverflowPolicy', () => {
    const ids = Object.keys(defaultStack().toJSON().Resources);
    expect(ids.filter(id => id.includes('OverflowPolicy'))).toHaveLength(0);
  });

  test('with-cmsUserPoolId fixture (bin/vsa.ts layout): no resource logical id contains OverflowPolicy', () => {
    const ids = Object.keys(withPoolIdStack().toJSON().Resources);
    expect(ids.filter(id => id.includes('OverflowPolicy'))).toHaveLength(0);
  });
});

// W2: bySid must find the three FI health Sids in the with-cmsUserPoolId fixture too
// (confirms the ManagedPolicy scanning reaches them regardless of pool presence).
describe('FI health Sids present in with-cmsUserPoolId fixture', () => {
  for (const sid of ['FiHealthInvestigationsTable', 'FiHealthGlueMetadataRead', 'FiHealthFindingsByFleetQuery']) {
    test(`${sid} found`, () => { expect(() => bySid(withPoolIdStack(), sid)).not.toThrow(); });
  }
});


// ── B2: service schedule and staleness alarm ────────────────────────────────
//
// Two synths (default / live) already cached above.

function makeModelStack(fiFleetHealthModelLive: boolean): Template {
  const prev = process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT;
  process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT = 'ops@example.invalid';
  try {
    const app = new cdk.App();
    const stack = new VsaTier2Stack(app, 'vsa-staging-tier2', {
      cmsFiInternalApiEndpoint: TEST_CMS_FI_INTERNAL_API_ENDPOINT,
      dmsFiInternalApiEndpoint: TEST_DMS_FI_INTERNAL_API_ENDPOINT,
      env: { account: ACCOUNT, region: REGION },
      stage: 'staging',
      adpRegion: ADP_REGION,
      adpAccountId: ACCOUNT,
      cmsKmsKeyArn: {
        literal: `arn:aws:kms:${REGION}:${ACCOUNT}:key/test-key-id`,
        parameterName: '/cvx/staging/cms-kms-key-arn',
      },
      fiFleetHealthModelLive,
    });
    return Template.fromStack(stack);
  } finally {
    if (prev === undefined) delete process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT;
    else process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT = prev;
  }
}

let cachedModelOff: Template | undefined;
let cachedModelOn:  Template | undefined;
function modelOffStack(): Template { return (cachedModelOff ??= makeModelStack(false)); }
function modelOnStack():  Template { return (cachedModelOn  ??= makeModelStack(true));  }

function scheduleByName(t: Template, name: string): any {
  const ids = Object.keys(t.findResources('AWS::Scheduler::Schedule', {
    Properties: { Name: name },
  }));
  expect(ids).toHaveLength(1);
  return t.toJSON().Resources[ids[0]].Properties;
}

function stalenessAlarmByComponent(t: Template, component: string): any {
  return alarmByName(t, `vsa-staging-avx-run-stale-${component}-${REGION}`);
}

// Mutation M4: service schedule enabled true by default → default synth has State=DISABLED.
describe('service schedule (B2)', () => {
  const SCHEDULE_NAME = `vsa-staging-tier2-daily-service-${REGION}`;

  test('M4: service schedule DISABLED when fiFleetHealthLive=false', () => {
    const p = scheduleByName(offStack(), SCHEDULE_NAME);
    expect(p.State).toBe('DISABLED');
  });

  test('service schedule ENABLED when fiFleetHealthLive=true', () => {
    const p = scheduleByName(liveStack(), SCHEDULE_NAME);
    expect(p.State).toBe('ENABLED');
  });

  test('G6: service schedule ENABLED in the default synth (the shared constant)', () => {
    const p = scheduleByName(defaultStack(), SCHEDULE_NAME);
    expect(p.State).toBe('ENABLED');
  });

  test.each([['default', defaultStack], ['off', offStack]])(
    'S-c: the service schedule DependsOn FiHealthTier2Policy (%s synth)', (_n, mk) => {
      const t = mk();
      const policyIds = Object.keys(t.findResources('AWS::IAM::ManagedPolicy'))
        .filter((id) => id.startsWith('FiHealthTier2Policy'));
      expect(policyIds).toHaveLength(1);
      const ids = Object.keys(t.findResources('AWS::Scheduler::Schedule', {
        Properties: { Name: SCHEDULE_NAME } }));
      expect(ids).toHaveLength(1);
      expect(asList(t.toJSON().Resources[ids[0]].DependsOn ?? [])).toContain(policyIds[0]);
    });

  test('S-c: no other daily schedule gains a DependsOn on FiHealthTier2Policy', () => {
    const t = defaultStack();
    const policyId = Object.keys(t.findResources('AWS::IAM::ManagedPolicy'))
      .filter((id) => id.startsWith('FiHealthTier2Policy'))[0];
    const dependents = Object.entries(t.toJSON().Resources)
      .filter(([, r]: [string, any]) => r.Type === 'AWS::Scheduler::Schedule'
        && asList(r.DependsOn ?? []).includes(policyId))
      .map(([, r]: [string, any]) => r.Properties.Name);
    expect(dependents).toEqual([SCHEDULE_NAME]);
  });

  test('service schedule at 05:40 UTC with correct input', () => {
    const p = scheduleByName(defaultStack(), SCHEDULE_NAME);
    // ScheduleExpression: cron(40 5 * * ? *)
    expect(p.ScheduleExpression).toBe('cron(40 5 * * ? *)');
    const input = JSON.parse(p.Target.Input);
    expect(input.stage).toBe('staging');
    expect(input.component).toBe('service');
  });

  test('every other daily schedule State unchanged (enabled=true)', () => {
    const t = defaultStack();
    // battery/tires/brakes/cost: always enabled (never gated on fiFleetHealthLive).
    for (const comp of ['battery', 'tires', 'brakes', 'cost']) {
      const p = scheduleByName(t, `vsa-staging-tier2-daily-${comp}-${REGION}`);
      expect(p.State).toBe('ENABLED');
    }
    // recall/warranty: gated on FI_RECALL_WARRANTY_LIVE which is true (task 5.4 flipped it).
    // M6: battery enabled:false → this block catches the mutation.
    for (const comp of ['recall', 'warranty']) {
      const p = scheduleByName(t, `vsa-staging-tier2-daily-${comp}-${REGION}`);
      expect(p.State).toBe('ENABLED');
    }
  });
});

// Mutation M5: service staleness alarm ActionsEnabled true by default.
describe('service staleness alarm (B2)', () => {
  test('M5: service staleness alarm ActionsEnabled=false when fiFleetHealthLive=false', () => {
    const p = stalenessAlarmByComponent(offStack(), 'service');
    expect(p.ActionsEnabled).toBe(false);
  });

  test('service staleness alarm ActionsEnabled=true when fiFleetHealthLive=true', () => {
    const p = stalenessAlarmByComponent(liveStack(), 'service');
    // CDK omits ActionsEnabled when true (it is the default).
    expect(p.ActionsEnabled).not.toBe(false);
  });

  test('G6: service staleness alarm actions on in the default synth, pointing at the operator topic', () => {
    const t = defaultStack();
    const p = stalenessAlarmByComponent(t, 'service');
    expect(p.ActionsEnabled).not.toBe(false);
    expect(asList(p.AlarmActions)).toEqual([operatorTopicArn(t)]);
  });
});

// Group 6 rest-plan step 7: only the live switch flipped. The model switches stay off in the
// default synth; fault grounding is on since stage D 7.7 (its own flip, below).
describe('G6: the default synth keeps the model path off', () => {
  function envOf(t: Template): Record<string, string> {
    const fn = Object.values(t.findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: HEALTH_AGENT },
    }))[0] as any;
    return fn.Properties.Environment.Variables;
  }

  test.each(['FI_SERVICE_MODEL_ENABLED', 'FI_INVESTIGATION_MODEL_ENABLED'])(
    "%s is 'false'", (name) => {
      expect(envOf(defaultStack())[name]).toBe('false');
    });
  test("7.7: FAULT_GROUNDING_ENABLED is 'true' in the default synth", () => {
    expect(envOf(defaultStack()).FAULT_GROUNDING_ENABLED).toBe('true');
  });
});

// Stage D 7.7: the fault-grounding switch follows fiFaultGroundingLive (default
// FI_FAULT_GROUNDING_LIVE) and changes nothing else on the health agent's environment.
describe('7.7: FAULT_GROUNDING_ENABLED follows fiFaultGroundingLive', () => {
  function makeGroundingStack(fiFaultGroundingLive: boolean): Template {
    const prev = process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT;
    process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT = 'ops@example.invalid';
    try {
      const app = new cdk.App();
      const stack = new VsaTier2Stack(app, 'vsa-staging-tier2', {
        cmsFiInternalApiEndpoint: TEST_CMS_FI_INTERNAL_API_ENDPOINT,
        dmsFiInternalApiEndpoint: TEST_DMS_FI_INTERNAL_API_ENDPOINT,
        env: { account: ACCOUNT, region: REGION },
        stage: 'staging',
        adpRegion: ADP_REGION,
        adpAccountId: ACCOUNT,
        cmsKmsKeyArn: {
          literal: `arn:aws:kms:${REGION}:${ACCOUNT}:key/test-key-id`,
          parameterName: '/cvx/staging/cms-kms-key-arn',
        },
        fiFaultGroundingLive,
      });
      return Template.fromStack(stack);
    } finally {
      if (prev === undefined) delete process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT;
      else process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT = prev;
    }
  }
  function envOf(t: Template): Record<string, string> {
    const fn = Object.values(t.findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: HEALTH_AGENT },
    }))[0] as any;
    return fn.Properties.Environment.Variables;
  }
  let on: Template | undefined;
  let off: Template | undefined;
  const onStack  = (): Template => (on  ??= makeGroundingStack(true));
  const offStackG = (): Template => (off ??= makeGroundingStack(false));

  test("explicit true sets 'true'", () => {
    expect(envOf(onStack()).FAULT_GROUNDING_ENABLED).toBe('true');
  });
  test("explicit false (the rollback) sets 'false'", () => {
    expect(envOf(offStackG()).FAULT_GROUNDING_ENABLED).toBe('false');
  });
  test('the switch changes only FAULT_GROUNDING_ENABLED on the health agent', () => {
    const a = { ...envOf(onStack()) };
    const b = { ...envOf(offStackG()) };
    delete a.FAULT_GROUNDING_ENABLED;
    delete b.FAULT_GROUNDING_ENABLED;
    expect(a).toEqual(b);
  });
  test('the default synth equals the explicit value of FI_FAULT_GROUNDING_LIVE', () => {
    const expected = FI_FAULT_GROUNDING_LIVE ? 'true' : 'false';
    expect(envOf(defaultStack()).FAULT_GROUNDING_ENABLED).toBe(expected);
  });
});

// Mutation M6: FI_*_MODEL_ENABLED 'true' by default.
describe('FI model-enabled env vars (B2)', () => {
  function envOf(t: Template): Record<string, string> {
    const fn = Object.values(t.findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: HEALTH_AGENT },
    }))[0] as any;
    return fn.Properties.Environment.Variables;
  }

  test("M6: FI_SERVICE_MODEL_ENABLED='false' when fiFleetHealthModelLive=false", () => {
    expect(envOf(modelOffStack()).FI_SERVICE_MODEL_ENABLED).toBe('false');
  });

  test("M6: FI_INVESTIGATION_MODEL_ENABLED='false' when fiFleetHealthModelLive=false", () => {
    expect(envOf(modelOffStack()).FI_INVESTIGATION_MODEL_ENABLED).toBe('false');
  });

  test("FI_SERVICE_MODEL_ENABLED='true' when fiFleetHealthModelLive=true", () => {
    expect(envOf(modelOnStack()).FI_SERVICE_MODEL_ENABLED).toBe('true');
  });

  test("FI_INVESTIGATION_MODEL_ENABLED='true' when fiFleetHealthModelLive=true", () => {
    expect(envOf(modelOnStack()).FI_INVESTIGATION_MODEL_ENABLED).toBe('true');
  });
});
