/**
 * task 4.3 (spec 2026-10-05-fi-recall-warranty-stage-c): Tier 2 stack and analytics
 * stack for the recall and warranty components.
 *
 * Two synths: default (fiRecallWarrantyLive = false constant) and explicit true.
 * Schedules, staleness alarms, and the five IAM statements are compared exactly.
 * Analytics: default has no fleet_recall LF grant; true has both.
 *
 * IAM resources compared exactly per Sid so a widened grant fails at green.
 * Mutations recorded in decisions.md (call 55, call 56).
 */

import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { VsaAnalyticsStack } from '../lib/vsa-analytics-stack';
import { VsaTier2Stack } from '../lib/vsa-tier2-stack';
import { TEST_CMS_FI_INTERNAL_API_ENDPOINT } from './helpers/cms-fi-internal-endpoint';
import { TEST_DMS_FI_INTERNAL_API_ENDPOINT } from './helpers/dms-fi-internal-endpoint';

const ACCOUNT = '123456789012';
const REGION  = 'us-west-2';
const ADP_REGION = 'us-east-1';
const HEALTH_AGENT = `vsa-staging-tier2-health-agent-${REGION}`;
const TIER2_ROLE   = `vsa-staging-tier2-scheduler-${REGION}`;

function makeTier2(fiRecallWarrantyLive?: boolean): Template {
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
      ...(fiRecallWarrantyLive !== undefined ? { fiRecallWarrantyLive } : {}),
    });
    return Template.fromStack(stack);
  } finally {
    if (prev === undefined) delete process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT;
    else process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT = prev;
  }
}

function makeAnalytics(fiRecallWarrantyLive?: boolean): Template {
  const app = new cdk.App();
  const stack = new VsaAnalyticsStack(app, 'vsa-staging-analytics', {
    env: { account: ACCOUNT, region: ADP_REGION },
    stage: 'staging',
    agentCoreRoleName: `AmazonBedrockAgentCoreSDKRuntime-${REGION}-staging`,
    tier2RoleName: TIER2_ROLE,
    ...(fiRecallWarrantyLive !== undefined ? { fiRecallWarrantyLive } : {}),
  });
  return Template.fromStack(stack);
}

let cachedDefault: Template | undefined;
let cachedLive: Template | undefined;
// The off state is synthesised explicitly since task 5.4 set the constant to true.
function tier2Default(): Template { return (cachedDefault ??= makeTier2(false)); }
function tier2Live():    Template { return (cachedLive    ??= makeTier2(true)); }

const asList = (x: unknown): unknown[] => (Array.isArray(x) ? x : [x]);

/** CDK logical ID of the DynamoDB table with the given TableName. */
function tableId(t: Template, tableName: string): string {
  const tbs = t.findResources('AWS::DynamoDB::Table', { Properties: { TableName: tableName } });
  expect(Object.keys(tbs)).toHaveLength(1);
  return Object.keys(tbs)[0];
}

function healthAgentStatements(t: Template): any[] {
  const fns = t.findResources('AWS::Lambda::Function', { Properties: { FunctionName: HEALTH_AGENT } });
  const fnId = Object.keys(fns)[0];
  const roleRef = JSON.stringify(t.toJSON().Resources[fnId].Properties.Role);
  const roleId = /"Fn::GetAtt":\["([A-Za-z0-9]+)"/.exec(roleRef)![1];
  return Object.values(t.findResources('AWS::IAM::Policy'))
    .filter((p: any) => JSON.stringify(p.Properties.Roles).includes(roleId))
    .flatMap((p: any) => p.Properties.PolicyDocument.Statement);
}

function bySid(t: Template, sid: string): any {
  const found = healthAgentStatements(t).filter((s) => s.Sid === sid);
  expect(found).toHaveLength(1);
  return found[0];
}

function scheduleProps(t: Template, component: string): any {
  const schedules = Object.values(t.findResources('AWS::Scheduler::Schedule', {
    Properties: { Name: `vsa-staging-tier2-daily-${component}-${REGION}` },
  })) as any[];
  expect(schedules).toHaveLength(1);
  return schedules[0].Properties;
}

function alarmProps(t: Template, component: string): any {
  const ids = Object.keys(t.findResources('AWS::CloudWatch::Alarm', {
    Properties: { AlarmName: `vsa-staging-avx-run-stale-${component}-${REGION}` },
  }));
  expect(ids).toHaveLength(1);
  return t.toJSON().Resources[ids[0]].Properties;
}

// ── Schedules ──────────────────────────────────────────────────────────────

describe('recall/warranty schedules — fiRecallWarrantyLive: false', () => {
  test.each([
    ['recall',   'cron(0 5 * * ? *)'],
    ['warranty', 'cron(20 5 * * ? *)'],
  ])('%s schedule: DISABLED, correct cron, component in input', (component, cron) => {
    const props = scheduleProps(tier2Default(), component);
    expect(props.State).toBe('DISABLED');
    expect(props.ScheduleExpression).toBe(cron);
    expect(JSON.parse(props.Target.Input)).toEqual({ stage: 'staging', component });
  });
});

describe('recall/warranty schedules — fiRecallWarrantyLive: true', () => {
  test.each([
    ['recall',   'cron(0 5 * * ? *)'],
    ['warranty', 'cron(20 5 * * ? *)'],
  ])('%s schedule: ENABLED, correct cron', (component, cron) => {
    const props = scheduleProps(tier2Live(), component);
    expect(props.State).toBe('ENABLED');
    expect(props.ScheduleExpression).toBe(cron);
  });
});

describe('recall/warranty schedules — no prop follows FI_RECALL_WARRANTY_LIVE (true since 5.4)', () => {
  test.each(['recall', 'warranty'])('%s schedule: ENABLED with no prop', (component) => {
    expect(scheduleProps(makeTier2(), component).State).toBe('ENABLED');
  });
});

// ── Staleness alarms ────────────────────────────────────────────────────────

describe('recall/warranty staleness alarms — fiRecallWarrantyLive: false', () => {
  test.each(['recall', 'warranty'])('%s alarm: ActionsEnabled false, actions still set', (c) => {
    const props = alarmProps(tier2Default(), c);
    expect(props.ActionsEnabled).toBe(false);
    // AlarmActions still points at the operator topic (wired; just silenced).
    expect(props.AlarmActions).toHaveLength(1);
    expect(props.OKActions).toHaveLength(1);
  });
});

describe('recall/warranty staleness alarms — fiRecallWarrantyLive: true', () => {
  test.each(['recall', 'warranty'])('%s alarm: ActionsEnabled true', (c) => {
    const props = alarmProps(tier2Live(), c);
    // CDK omits ActionsEnabled from the template when true (it is the default).
    expect(props.ActionsEnabled).not.toBe(false);
  });
});

describe('recall/warranty staleness alarm metric', () => {
  test.each(['recall', 'warranty'])(
    '%s: FILL(m1,0) over RunRecordsWritten, 27-hour window, BREACHING',
    (component) => {
      const props = alarmProps(tier2Default(), component);
      expect(props.EvaluationPeriods).toBe(27);
      expect(props.DatapointsToAlarm).toBe(27);
      expect(props.TreatMissingData).toBe('breaching');
      expect(props.ComparisonOperator).toBe('LessThanThreshold');
      const m1 = props.Metrics.find((m: any) => m.Id === 'm1');
      expect(m1.MetricStat.Metric.Namespace).toBe('CVX/Tier2');
      expect(m1.MetricStat.Metric.MetricName).toBe('RunRecordsWritten');
      expect(m1.MetricStat.Metric.Dimensions).toContainEqual(
        { Name: 'component', Value: component });
      const expr = props.Metrics.find((m: any) => m.Expression !== undefined);
      expect(expr.Expression).toBe('FILL(m1, 0)');
    },
  );
});

// ── IAM statements ─────────────────────────────────────────────────────────

describe('FiRwGlueMetadataRead', () => {
  test('exactly the fleet_recall and five DMS tables in adp account/region', () => {
    const st = bySid(tier2Default(), 'FiRwGlueMetadataRead');
    expect(st.Effect).toBe('Allow');
    expect(asList(st.Action).sort()).toEqual([
      'glue:GetDatabase', 'glue:GetPartitions', 'glue:GetTable', 'glue:GetTables',
    ]);
    const g = `arn:aws:glue:${ADP_REGION}:${ACCOUNT}`;
    expect(asList(st.Resource)).toEqual([
      `${g}:database/adp_staging_fleet_recall`,
      `${g}:table/adp_staging_fleet_recall/*`,
      `${g}:database/dms_staging`,
      `${g}:table/dms_staging/repair_orders`,
      `${g}:table/dms_staging/parts_inventory`,
      `${g}:table/dms_staging/rooftops`,
      `${g}:table/dms_staging/warranty_candidates`,
      `${g}:table/dms_staging/warranty_terms`,
    ]);
  });
});

describe('FiRwDmsLakeS3Read', () => {
  // The DMS Iceberg bucket is not LF-registered, so Athena reads it with this role's IAM
  // (issue 2026-10-09-tier2-role-cannot-read-dms-iceberg-s3). Pinned literally: one prefix per
  // DMS table the role holds catalog grants for, never the bucket or the whole warehouse.
  const bucket = `dms-staging-iceberg-${ADP_REGION}-${ACCOUNT}`;
  const tables = ['repair_orders', 'parts_inventory', 'rooftops', 'warranty_candidates',
    'warranty_terms'];

  test('s3:GetObject only, on exactly the five table prefixes', () => {
    const st = bySid(tier2Default(), 'FiRwDmsLakeS3Read');
    expect(st.Effect).toBe('Allow');
    expect(asList(st.Action)).toEqual(['s3:GetObject']);
    expect(st.Condition).toBeUndefined();
    expect(asList(st.Resource)).toEqual(
      tables.map((t) => `arn:aws:s3:::${bucket}/iceberg/dms_staging.db/${t}/*`));
  });

  test('the S3 tables are the Glue statement\'s DMS tables', () => {
    const glue = asList(bySid(tier2Default(), 'FiRwGlueMetadataRead').Resource) as string[];
    const glueDms = glue.filter((r) => r.includes(':table/dms_staging/'))
      .map((r) => r.split('/').pop());
    expect(glueDms.sort()).toEqual([...tables].sort());
  });

  test('no other statement on the role reaches the DMS bucket', () => {
    // Glob-matched against real object keys (review g51-dms-s3 W1): a substring check missed
    // `*`, `arn:aws:s3:::*` and `arn:aws:s3:::dms-*/*`. Pattern from the fi-cost restricted/ test.
    const glob = (resource: unknown, target: string): boolean => {
      if (typeof resource !== 'string') return false;
      const re = new RegExp('^' + resource.replace(/[.+^${}()|[\]\\]/g, '\\$&')
        .replace(/\*/g, '.*').replace(/\?/g, '.') + '$');
      return re.test(target);
    };
    const key = (t: string, rest: string) =>
      `arn:aws:s3:::${bucket}/iceberg/dms_staging.db/${t}/${rest}`;
    const granted = key('repair_orders', 'metadata/01572-x.metadata.json');
    const notGranted = [key('warranty_claims', 'metadata/00001-x.metadata.json'),
      key('dealer_performance', 'data/part-0.parquet'), `arn:aws:s3:::${bucket}/other/x`];
    const s3Get = (s: any) => asList(s.Action).some((a: any) => /^s3:(\*|Get)/.test(String(a)) || a === '*');
    const statements = (Object.values(tier2Default().findResources('AWS::IAM::Policy')) as any[])
      .flatMap((p) => p.Properties.PolicyDocument.Statement)
      .filter((s: any) => s.Effect === 'Allow' && s3Get(s));
    const reaching = (target: string) => statements
      .filter((s: any) => asList(s.Resource).some((r) => glob(r, target)))
      .map((s: any) => String(s.Sid));
    expect(reaching(granted)).toEqual(['FiRwDmsLakeS3Read']);
    for (const k of notGranted) expect(reaching(k)).toEqual([]);
  });
});

describe('FiRwCmsFleetLocationsQuery', () => {
  test('dynamodb:Query on the fleet-locations table ARN only', () => {
    const st = bySid(tier2Default(), 'FiRwCmsFleetLocationsQuery');
    expect(st.Effect).toBe('Allow');
    expect(asList(st.Action)).toEqual(['dynamodb:Query']);
    expect(st.Resource).toBe(
      `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/cms-staging-storage-fleet-locations`);
  });
});

describe('FiRwCmsGroundingsQuery', () => {
  test('dynamodb:Query on the vehicle-groundings table ARN only', () => {
    const st = bySid(tier2Default(), 'FiRwCmsGroundingsQuery');
    expect(st.Effect).toBe('Allow');
    expect(asList(st.Action)).toEqual(['dynamodb:Query']);
    expect(st.Resource).toBe(
      `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/cms-staging-storage-vehicle-groundings`);
  });
});

describe('FiRwFindingsByVinQuery', () => {
  test('dynamodb:Query on the by_vin index of the avx-findings table', () => {
    const t = tier2Default();
    const st = bySid(t, 'FiRwFindingsByVinQuery');
    expect(st.Effect).toBe('Allow');
    expect(asList(st.Action)).toEqual(['dynamodb:Query']);
    // Resource must be the findings table's by_vin index — not the table itself,
    // not another table, not a wildcard. (M3 guard: swapping to avxActionsTable fails here.)
    const fid = tableId(t, 'vsa-staging-avx-findings');
    expect(st.Resource).toEqual({
      'Fn::Join': ['', [{ 'Fn::GetAtt': [fid, 'Arn'] }, '/index/by_vin']],
    });
  });
});

describe('FiRwActionsGetItem', () => {
  test('dynamodb:GetItem on the avx-actions table ARN only', () => {
    const t = tier2Default();
    const st = bySid(t, 'FiRwActionsGetItem');
    expect(st.Effect).toBe('Allow');
    expect(asList(st.Action)).toEqual(['dynamodb:GetItem']);
    // Resource must be exactly the actions table — not findings, not a wildcard,
    // not an index. (M3 guard: swapping to avxFindingsTable fails here.)
    const aid = tableId(t, 'vsa-staging-avx-actions');
    expect(st.Resource).toEqual({ 'Fn::GetAtt': [aid, 'Arn'] });
  });
});

// ── LakeFormationGetDataAccess ──────────────────────────────────────────────

describe('LakeFormationGetDataAccess', () => {
  test('effect Allow, exactly lakeformation:GetDataAccess, resource *', () => {
    // W2 guard: M2 mutant (effect ALLOW→DENY) must fail here.
    const st = bySid(tier2Default(), 'LakeFormationGetDataAccess');
    expect(st.Effect).toBe('Allow');
    expect(asList(st.Action)).toEqual(['lakeformation:GetDataAccess']);
    expect(st.Resource).toBe('*');
  });
});

// ── Analytics: fleet_recall LF grants ──────────────────────────────────────

describe('analytics stack fleet_recall grants', () => {
  test('fiRecallWarrantyLive: false — no LfDbTier2FleetRecall or LfTblTier2FleetRecall resource', () => {
    const t = makeAnalytics(false);
    const perms = Object.values(t.findResources('AWS::LakeFormation::PrincipalPermissions'));
    const fleetRecallGrants = perms.filter((p: any) =>
      JSON.stringify(p.Properties.Resource).includes('fleet_recall'));
    expect(fleetRecallGrants).toHaveLength(0);
  });

  test('fiRecallWarrantyLive: true — both DB and table-wildcard grants present', () => {
    const t = makeAnalytics(true);
    const perms = Object.values(t.findResources('AWS::LakeFormation::PrincipalPermissions'));
    const fleetRecallGrants = perms.filter((p: any) =>
      JSON.stringify(p.Properties.Resource).includes('fleet_recall'));
    expect(fleetRecallGrants).toHaveLength(2);
    const dbGrant = fleetRecallGrants.find((p: any) => p.Properties.Resource.Database);
    const tblGrant = fleetRecallGrants.find((p: any) => p.Properties.Resource.Table);
    expect(dbGrant).toBeDefined();
    expect(tblGrant).toBeDefined();
    expect(dbGrant!.Properties.Permissions).toEqual(['DESCRIBE']);
    expect(tblGrant!.Properties.Permissions).toEqual(['SELECT', 'DESCRIBE']);
    // Table grant must use tableWildcard, not a named table.
    expect(tblGrant!.Properties.Resource.Table.TableWildcard).toBeDefined();
  });
});
