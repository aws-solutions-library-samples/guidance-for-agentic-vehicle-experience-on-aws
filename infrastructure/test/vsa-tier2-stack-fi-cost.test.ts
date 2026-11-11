/**
 * FI cost Stage B, task 4.2: the Tier 2 stack and the analytics stack for the `cost`
 * component (agent-fi-cost).
 *
 * Spec: .kiro/specs/2026-10-02-fi-cost-stage-b/spec.md (D6, D9, D13); tasks.md 4.2.
 *
 * IAM is compared EXACTLY per Sid (actions and the whole Resource value), so a grant
 * cannot widen to `database/*`, a table wildcard on `dimensions`, or `*` at green. The
 * answer-key denial is checked two ways: the Deny is present, and no Allow on the role
 * reaches an object under `restricted/`. Mutations run against these tests are
 * recorded in the spec's decisions.md.
 */

import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { VsaAnalyticsStack } from '../lib/vsa-analytics-stack';
import { VsaTier2Stack } from '../lib/vsa-tier2-stack';
import {
  actionCovers, actionsOf, resourcesOf, roleIdOfFunction, roleManagedPolicyArns,
  roleStatements as roleSurface,
} from './helpers/iam-surface';
import { TEST_CMS_FI_INTERNAL_API_ENDPOINT } from './helpers/cms-fi-internal-endpoint';
import { TEST_DMS_FI_INTERNAL_API_ENDPOINT } from './helpers/dms-fi-internal-endpoint';

const ACCOUNT = '123456789012';
const REGION = 'us-west-2';
const ADP_REGION = 'us-east-1';
const LAKE = `adp-staging-foundation-lake-${ACCOUNT}-${ADP_REGION}`;
const HEALTH_AGENT = `vsa-staging-tier2-health-agent-${REGION}`;
const TIER2_ROLE = `vsa-staging-tier2-scheduler-${REGION}`;

let cachedTier2: Template | undefined;
let cachedAnalytics: Template | undefined;

function tier2(): Template {
  if (cachedTier2) return cachedTier2;
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
    });
    cachedTier2 = Template.fromStack(stack);
    return cachedTier2;
  } finally {
    if (prev === undefined) delete process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT;
    else process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT = prev;
  }
}

function analytics(): Template {
  if (cachedAnalytics) return cachedAnalytics;
  const app = new cdk.App();
  const stack = new VsaAnalyticsStack(app, 'vsa-staging-analytics', {
    env: { account: ACCOUNT, region: ADP_REGION },
    stage: 'staging',
    agentCoreRoleName: `AmazonBedrockAgentCoreSDKRuntime-${REGION}-staging`,
    tier2RoleName: TIER2_ROLE,
  });
  cachedAnalytics = Template.fromStack(stack);
  return cachedAnalytics;
}

function healthAgentStatements(): any[] {
  const t = tier2();
  const fns = t.findResources('AWS::Lambda::Function', { Properties: { FunctionName: HEALTH_AGENT } });
  const fnId = Object.keys(fns)[0];
  const roleRef = JSON.stringify(t.toJSON().Resources[fnId].Properties.Role);
  const roleId = /"Fn::GetAtt":\["([A-Za-z0-9]+)"/.exec(roleRef)![1];
  return Object.values(t.findResources('AWS::IAM::Policy'))
    .filter((p: any) => JSON.stringify(p.Properties.Roles).includes(roleId))
    .flatMap((p: any) => p.Properties.PolicyDocument.Statement);
}

function bySid(sid: string): any {
  const found = healthAgentStatements().filter((s) => s.Sid === sid);
  expect(found).toHaveLength(1);
  return found[0];
}

const asList = (x: unknown): unknown[] => (Array.isArray(x) ? x : [x]);

function guardrailId(): string {
  const ids = Object.keys(tier2().findResources('AWS::Bedrock::Guardrail', {
    Properties: { Name: 'vsa-staging-fi-cost-guardrail' },
  }));
  expect(ids).toHaveLength(1);
  return ids[0];
}

/** IAM resource glob (only `*` and `?`) to a RegExp. Non-string resources never match. */
function globMatches(resource: unknown, target: string): boolean {
  if (typeof resource !== 'string') return false;
  const re = new RegExp('^' + resource.replace(/[.+^${}()|[\]\\]/g, '\\$&')
    .replace(/\*/g, '.*').replace(/\?/g, '.') + '$');
  return re.test(target);
}

describe('FI cost Guardrail', () => {
  test('one Guardrail: the six PII blocks, the two denied topics, five output filters at MEDIUM', () => {
    const props = tier2().toJSON().Resources[guardrailId()].Properties;
    const pii = props.SensitiveInformationPolicyConfig.PiiEntitiesConfig
      .map((e: any) => `${e.Type}:${e.Action}`).sort();
    expect(pii).toEqual([
      'ADDRESS:BLOCK', 'CREDIT_DEBIT_CARD_NUMBER:BLOCK', 'EMAIL:BLOCK', 'NAME:BLOCK',
      'PHONE:BLOCK', 'US_SOCIAL_SECURITY_NUMBER:BLOCK',
    ]);
    expect(props.SensitiveInformationPolicyConfig.RegexesConfig).toBeUndefined();
    expect(props.TopicPolicyConfig.TopicsConfig.map((t: any) => `${t.Name}:${t.Type}`).sort())
      .toEqual(['blame-of-a-person:DENY', 'personal-advice:DENY']);
    const filters = props.ContentPolicyConfig.FiltersConfig
      .map((f: any) => `${f.Type}:${f.OutputStrength}`).sort();
    expect(filters).toEqual(
      ['HATE:MEDIUM', 'INSULTS:MEDIUM', 'MISCONDUCT:MEDIUM', 'SEXUAL:MEDIUM', 'VIOLENCE:MEDIUM']);
  });

  test('ApplyGuardrail is granted on this Guardrail and the US guardrail profile in its three destination Regions only', () => {
    // 2026-10-18: cross-Region guardrail inference (the STANDARD topic tier needs it)
    // makes the caller need ApplyGuardrail on the profile in every destination Region
    // too. The whole Resource value is compared, so a dropped Region, a wildcard
    // Region, another profile or `guardrail-profile/*` fails here.
    const st = bySid('ApplyFiCostGuardrail');
    expect(st.Effect).toBe('Allow');
    expect(asList(st.Action)).toEqual(['bedrock:ApplyGuardrail']);
    expect(st.Condition).toBeUndefined();
    expect(st.Resource).toEqual([
      { 'Fn::GetAtt': [guardrailId(), 'GuardrailArn'] },
      `arn:aws:bedrock:us-east-1:${ACCOUNT}:guardrail-profile/us.guardrail.v1:0`,
      `arn:aws:bedrock:us-east-2:${ACCOUNT}:guardrail-profile/us.guardrail.v1:0`,
      `arn:aws:bedrock:us-west-2:${ACCOUNT}:guardrail-profile/us.guardrail.v1:0`,
    ]);
  });

  test('topic checks use the STANDARD tier through the US guardrail profile in the stack Region', () => {
    const props = tier2().toJSON().Resources[guardrailId()].Properties;
    expect(props.TopicPolicyConfig.TopicsTierConfig).toEqual({ TierName: 'STANDARD' });
    expect(props.CrossRegionConfig).toEqual({
      GuardrailProfileArn: `arn:aws:bedrock:${REGION}:${ACCOUNT}:guardrail-profile/us.guardrail.v1:0`,
    });
    // Only the topic tier changed on 2026-10-18; the content filters keep their
    // default tier (what the temporary guardrail measured).
    expect(props.ContentPolicyConfig.ContentFiltersTierConfig).toBeUndefined();
  });

  test('the Guardrail carries exactly the policy kinds run D measured, nothing added', () => {
    // A new WordPolicyConfig, ContextualGroundingPolicyConfig,
    // AutomatedReasoningPolicyConfig or KmsKeyArn would be an unmeasured change to
    // every FI agent's checks.
    const props = tier2().toJSON().Resources[guardrailId()].Properties;
    expect(Object.keys(props).sort()).toEqual([
      'BlockedInputMessaging', 'BlockedOutputsMessaging', 'ContentPolicyConfig', 'CrossRegionConfig',
      'Description', 'Name', 'SensitiveInformationPolicyConfig', 'TopicPolicyConfig',
    ]);
    expect(Object.keys(props.TopicPolicyConfig).sort()).toEqual(['TopicsConfig', 'TopicsTierConfig']);
  });

  test('the Lambda gets the Guardrail id and the DRAFT version', () => {
    const fns = tier2().findResources('AWS::Lambda::Function', { Properties: { FunctionName: HEALTH_AGENT } });
    const env = (Object.values(fns)[0] as any).Properties.Environment.Variables;
    expect(env.FI_COST_GUARDRAIL_ID).toEqual({ 'Fn::GetAtt': [guardrailId(), 'GuardrailId'] });
    expect(env.FI_COST_GUARDRAIL_VERSION).toBe('DRAFT');
  });
});

describe('FI cost Guardrail: cross-Region inference (2026-10-18)', () => {
  /**
   * Every statement on the health agent role through every channel the template
   * shows (AWS::IAM::Policy, AWS::IAM::ManagedPolicy, AWS::IAM::RolePolicy, the
   * role's own inline Policies), from the shared collector. The role's
   * ManagedPolicyArns (AWS-managed policies, whose statements aren't in the
   * template) are pinned separately below.
   */
  const roleId = (): string => roleIdOfFunction(tier2(), HEALTH_AGENT);
  const roleStatements = (): any[] => roleSurface(tier2(), roleId());

  /** Our own guardrail's ARN, as the GetAtt it synthesizes to, rendered concrete. */
  const OWN_GUARDRAIL = `arn:aws:bedrock:${REGION}:${ACCOUNT}:guardrail/fi-cost-own-id`;

  /**
   * A resource as the pattern it can reach. Fails closed: an Fn::Join renders its
   * non-string parts as `*`, any other intrinsic as `*`, except the GetAtt of the FI
   * cost guardrail's own ARN, which renders as OWN_GUARDRAIL.
   */
  function renderResource(r: unknown): string {
    if (typeof r === 'string') return r;
    if (JSON.stringify(r) === JSON.stringify({ 'Fn::GetAtt': [guardrailId(), 'GuardrailArn'] })) {
      return OWN_GUARDRAIL;
    }
    const join = (r as any)?.['Fn::Join'];
    if (Array.isArray(join)) {
      return (join[1] as unknown[]).map((p) => (typeof p === 'string' ? p : '*')).join(join[0]);
    }
    return '*';
  }

  /** Allow statements whose actions cover bedrock:ApplyGuardrail (IAM matches case-insensitively), or that use NotAction. */
  const applyGuardrailStatements = (): any[] => roleStatements()
    .filter((st) => st.Effect === 'Allow')
    .filter((st) => st.NotAction !== undefined
      || actionsOf(st).some((a) => actionCovers(a, 'bedrock:ApplyGuardrail')));

  const label = (st: any): string => st.Sid ?? `<no sid: ${JSON.stringify(st).slice(0, 80)}>`;

  /** Labels of the ApplyGuardrail-granting statements whose resources reach `arn`. */
  function reaching(arn: string): string[] {
    return applyGuardrailStatements()
      .filter((st) => st.NotResource !== undefined
        || resourcesOf(st).some((r) => globMatches(renderResource(r), arn)))
      .map(label);
  }

  const profile = (region: string, account = ACCOUNT, id = 'us.guardrail.v1:0') =>
    `arn:aws:bedrock:${region}:${account}:guardrail-profile/${id}`;

  test('the role surface is read through every channel, and carries no managed-policy ARN', () => {
    // Anti-vacuity: one Sid from each policy the role has today (inline DefaultPolicy,
    // FiHealthTier2Policy, FiRebalanceTier2Policy), so the collector demonstrably read
    // both kinds. AWS-managed policies (e.g. AmazonBedrockFullAccess) can't be read
    // from the template, so the ARN list is pinned exactly.
    const sids = roleStatements().map((st) => st.Sid);
    for (const sid of ['CloudWatchLogsBaseline', 'FiHealthInvestigationsTable', 'FiRebalanceGlueMetadataRead']) {
      expect(sids).toContain(sid);
    }
    expect(roleManagedPolicyArns(tier2(), roleId())).toEqual([]);
  });

  test('ApplyFiCostGuardrail is the only statement on the role that grants ApplyGuardrail at all', () => {
    // With the exact Resource pin above, this bounds every guardrail and profile the
    // role can call, not a sample of them.
    expect(applyGuardrailStatements().map(label)).toEqual(['ApplyFiCostGuardrail']);
  });

  test('only ApplyFiCostGuardrail reaches the guardrail and each destination profile (positive control)', () => {
    for (const arn of [OWN_GUARDRAIL, profile('us-east-1'), profile('us-east-2'), profile('us-west-2')]) {
      expect(reaching(arn)).toEqual(['ApplyFiCostGuardrail']);
    }
  });

  test('examples of what nothing on the role reaches: another Region, profile, account or guardrail', () => {
    for (const arn of [
      profile('us-west-1'),                                   // the US profile's 4th destination from us-west-1
      profile('eu-west-1', ACCOUNT, 'eu.guardrail.v1:0'),
      profile('us-west-2', ACCOUNT, 'us.guardrail.v2:0'),
      profile('us-west-2', '999999999999'),
      `arn:aws:bedrock:${REGION}:${ACCOUNT}:guardrail/another-guardrail`,
      `arn:aws:bedrock:us-east-1:${ACCOUNT}:guardrail/fi-cost-own-id`,
    ]) {
      expect(reaching(arn)).toEqual([]);
    }
  });

  function synthAt(region: string | undefined): Template {
    const prev = process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT;
    process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT = 'ops@example.invalid';
    try {
      const app = new cdk.App();
      const stack = new VsaTier2Stack(app, 'vsa-staging-tier2', {
        cmsFiInternalApiEndpoint: TEST_CMS_FI_INTERNAL_API_ENDPOINT,
        dmsFiInternalApiEndpoint: TEST_DMS_FI_INTERNAL_API_ENDPOINT,
        env: region === undefined ? { account: ACCOUNT } : { account: ACCOUNT, region },
        stage: 'staging',
        adpRegion: ADP_REGION,
        adpAccountId: ACCOUNT,
        cmsKmsKeyArn: {
          literal: `arn:aws:kms:${region ?? REGION}:${ACCOUNT}:key/test-key-id`,
          parameterName: '/cvx/staging/cms-kms-key-arn',
        },
      });
      return Template.fromStack(stack);
    } finally {
      if (prev === undefined) delete process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT;
      else process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT = prev;
    }
  }

  test('from us-east-1 the profile is called in us-east-1 and the grant keeps the same three destinations', () => {
    const t = synthAt('us-east-1');
    const g = Object.entries(t.findResources('AWS::Bedrock::Guardrail', {
      Properties: { Name: 'vsa-staging-fi-cost-guardrail' },
    }));
    expect(g).toHaveLength(1);
    const [gid, res] = g[0] as [string, any];
    expect(res.Properties.CrossRegionConfig).toEqual({ GuardrailProfileArn: profile('us-east-1') });
    const sts = Object.values(t.findResources('AWS::IAM::Policy'))
      .flatMap((p: any) => asList(p.Properties.PolicyDocument.Statement))
      .filter((s: any) => s.Sid === 'ApplyFiCostGuardrail');
    expect(sts).toHaveLength(1);
    expect((sts[0] as any).Resource).toEqual([
      { 'Fn::GetAtt': [gid, 'GuardrailArn'] },
      profile('us-east-1'), profile('us-east-2'), profile('us-west-2'),
    ]);
  });

  test.each(['us-west-1', 'eu-west-1', 'ca-central-1'])(
    'synth refuses %s, where the US profile routes elsewhere or is unavailable', (region) => {
      expect(() => synthAt(region)).toThrow(
        new RegExp(`US guardrail profile us\\.guardrail\\.v1:0.*Region is ${region}\\.`));
    });

  test('synth refuses an unresolved (environment-agnostic) Region', () => {
    expect(() => synthAt(undefined)).toThrow(/US guardrail profile us\.guardrail\.v1:0.*Region is unresolved\./);
  });
});

describe('FI cost data access', () => {
  test('Athena is the one analytics workgroup, three actions', () => {
    const st = bySid('AthenaWorkgroupAccess');
    expect(asList(st.Action).sort()).toEqual(
      ['athena:GetQueryExecution', 'athena:GetQueryResults', 'athena:StartQueryExecution']);
    expect(st.Resource).toBe(`arn:aws:athena:${ADP_REGION}:${ACCOUNT}:workgroup/cvx-staging-analytics`);
  });

  test('Glue metadata is exactly the four cost databases, dimensions by the vins table only', () => {
    const st = bySid('FiCostGlueMetadataRead');
    const g = `arn:aws:glue:${ADP_REGION}:${ACCOUNT}`;
    expect(asList(st.Action).sort()).toEqual(
      ['glue:GetDatabase', 'glue:GetPartitions', 'glue:GetTable', 'glue:GetTables']);
    expect(asList(st.Resource)).toEqual([
      `${g}:database/adp_staging_fleet_cost`,
      `${g}:table/adp_staging_fleet_cost/*`,
      `${g}:database/adp_staging_fleet_ops`,
      `${g}:table/adp_staging_fleet_ops/*`,
      `${g}:database/adp_staging_dimensions`,
      `${g}:table/adp_staging_dimensions/vins`,
      `${g}:database/adp_staging_vehicle_pricing`,
      `${g}:table/adp_staging_vehicle_pricing/vehicle_pricing`,
    ]);
  });

  test('no Glue grant on the role reaches the customers table', () => {
    const customers = `arn:aws:glue:${ADP_REGION}:${ACCOUNT}:table/adp_staging_dimensions/customers`;
    const reaching = healthAgentStatements()
      .filter((s) => s.Effect === 'Allow' && asList(s.Action).some((a: any) => String(a).startsWith('glue:')))
      .filter((s) => asList(s.Resource).some((r) => globMatches(r, customers)));
    expect(reaching).toEqual([]);
  });

  test('the answer-key prefix is denied for objects and listing', () => {
    const objects = bySid('DenyAdpRestrictedObjects');
    expect(objects.Effect).toBe('Deny');
    expect(asList(objects.Action).sort()).toEqual(['s3:GetObject', 's3:GetObjectVersion']);
    expect(objects.Resource).toBe(`arn:aws:s3:::${LAKE}/restricted/*`);
    // Unconditioned: a Deny that applies only to some requests is not a denial.
    expect(objects.Condition).toBeUndefined();
    const listing = bySid('DenyAdpRestrictedListing');
    expect(listing.Effect).toBe('Deny');
    expect(asList(listing.Action)).toEqual(['s3:ListBucket']);
    expect(listing.Resource).toBe(`arn:aws:s3:::${LAKE}`);
    expect(listing.Condition).toEqual({ StringLike: { 's3:prefix': ['restricted', 'restricted/*'] } });
  });

  test('no Allow on the role reaches an object under restricted/', () => {
    const key = `arn:aws:s3:::${LAKE}/restricted/fleet_cost/plants/year_month=2026-09/answer_key.parquet`;
    const reaching = healthAgentStatements()
      .filter((s) => s.Effect === 'Allow')
      .filter((s) => asList(s.Action).some((a: any) => /^s3:(\*|Get)/.test(String(a)) || a === '*'))
      .filter((s) => asList(s.Resource).some((r) => globMatches(r, key)));
    expect(reaching).toEqual([]);
  });

  test('the policy parameter read is the one cost parameter', () => {
    const st = bySid('FiCostPolicyParameterRead');
    expect(asList(st.Action)).toEqual(['ssm:GetParameter']);
    expect(st.Resource).toBe(
      `arn:aws:ssm:${REGION}:${ACCOUNT}:parameter/vsa/staging/avx/fi-cost/max-diagnoses-per-run`);
  });
});

describe('FI cost schedule and staleness alarm', () => {
  test('daily at 04:00 UTC with component cost', () => {
    const schedules = tier2().findResources('AWS::Scheduler::Schedule', {
      Properties: { Name: `vsa-staging-tier2-daily-cost-${REGION}` },
    });
    const props = (Object.values(schedules)[0] as any).Properties;
    expect(Object.keys(schedules)).toHaveLength(1);
    expect(props.ScheduleExpression).toBe('cron(0 4 * * ? *)');
    // 04:00 in UTC, not local time (Group 4 review cycle 2: a time zone moves the run
    // by four or five hours and eats the staleness window's margin on DST changes).
    expect(props.ScheduleExpressionTimezone).toBe('Etc/UTC');
    // No flexible window: a 4-hour one would let the run start as late as 08:00 UTC and a
    // late but good run trip the 27-hour staleness alarm (Group 4 review cycle 3).
    expect(props.FlexibleTimeWindow).toEqual({ Mode: 'OFF' });
    expect(JSON.parse(props.Target.Input)).toEqual({ stage: 'staging', component: 'cost' });
    expect(props.State).toBe('ENABLED');
    const fnId = Object.keys(tier2().findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: HEALTH_AGENT } }))[0];
    expect(props.Target.Arn).toEqual({ 'Fn::GetAtt': [fnId, 'Arn'] });
    expect(props.Target.RetryPolicy).toEqual({ MaximumEventAgeInSeconds: 3600, MaximumRetryAttempts: 2 });
  });

  test('cost has no no-vins-selected or artifacts-stale alarm (monthly data, decision 12)', () => {
    const names = Object.values(tier2().findResources('AWS::CloudWatch::Alarm'))
      .map((a: any) => String(a.Properties.AlarmName));
    expect(names.filter((n) => /-(no-vins-selected|artifacts-stale)-cost-/.test(n))).toEqual([]);
    expect(names).toContain(`vsa-staging-tier2-no-vins-selected-battery-${REGION}`);   // anti-vacuity
  });

  test('the cost run budget (TIER2_MAX_WALL_SECONDS) is within the run-duration alarm', () => {
    const fns = tier2().findResources('AWS::Lambda::Function', { Properties: { FunctionName: HEALTH_AGENT } });
    const budgetS = Number((Object.values(fns)[0] as any).Properties.Environment.Variables.TIER2_MAX_WALL_SECONDS);
    const alarms = tier2().findResources('AWS::CloudWatch::Alarm', {
      Properties: { AlarmName: `vsa-staging-tier2-run-duration-${REGION}` },
    });
    const thresholdMs = (Object.values(alarms)[0] as any).Properties.Threshold;
    expect(budgetS).toBeGreaterThan(0);
    expect(budgetS * 1000).toBeLessThanOrEqual(thresholdMs);
  });

  test('a 27-hour run-staleness alarm on the cost heartbeat', () => {
    const alarms = tier2().findResources('AWS::CloudWatch::Alarm', {
      Properties: { AlarmName: `vsa-staging-avx-run-stale-cost-${REGION}` },
    });
    const props = (Object.values(alarms)[0] as any).Properties;
    expect(props.EvaluationPeriods).toBe(27);
    expect(props.DatapointsToAlarm).toBe(27);
    const m1 = props.Metrics.find((m: any) => m.Id === 'm1').MetricStat.Metric;
    expect(m1.MetricName).toBe('RunRecordsWritten');
    expect(m1.Dimensions).toEqual(expect.arrayContaining([{ Name: 'component', Value: 'cost' }]));
    expect(props.AlarmActions).toHaveLength(1);
  });
});

describe('Lake Formation grants for the Tier 2 role (analytics stack)', () => {
  const role = `arn:aws:iam::${ACCOUNT}:role/${TIER2_ROLE}`;

  function grants(): any[] {
    return Object.values(analytics().findResources('AWS::LakeFormation::PrincipalPermissions'))
      .map((r: any) => r.Properties)
      .filter((p: any) => p.Principal.DataLakePrincipalIdentifier === role);
  }

  function on(db: string): any[] {
    return grants().filter((p) => (p.Resource.Database?.Name ?? p.Resource.Table?.DatabaseName) === `adp_staging_${db}`);
  }

  test('fleet_cost and fleet_ops: database DESCRIBE plus table wildcard SELECT/DESCRIBE', () => {
    for (const db of ['fleet_cost', 'fleet_ops']) {
      const g = on(db);
      expect(g).toHaveLength(2);
      const dbGrant = g.find((p) => p.Resource.Database);
      const tblGrant = g.find((p) => p.Resource.Table);
      expect(dbGrant.Permissions).toEqual(['DESCRIBE']);
      expect(tblGrant.Resource.Table.TableWildcard).toEqual({});
      expect(tblGrant.Permissions).toEqual(['SELECT', 'DESCRIBE']);
      for (const p of g) expect(p.PermissionsWithGrantOption).toEqual([]);
    }
  });

  test('dimensions and vehicle_pricing: database DESCRIBE plus the one named table', () => {
    for (const [db, table] of [['dimensions', 'vins'], ['vehicle_pricing', 'vehicle_pricing']]) {
      const g = on(db);
      expect(g).toHaveLength(2);
      const tblGrant = g.find((p) => p.Resource.Table);
      expect(tblGrant.Resource.Table.Name).toBe(table);
      expect(tblGrant.Resource.Table.TableWildcard).toBeUndefined();
      expect(tblGrant.Permissions).toEqual(['SELECT', 'DESCRIBE']);
      expect(g.find((p) => p.Resource.Database).Permissions).toEqual(['DESCRIBE']);
      for (const p of g) expect(p.PermissionsWithGrantOption).toEqual([]);
    }
  });

  test('the role is granted on exactly these databases (Group 4 review cycle 3)', () => {
    // An extra database, customer_360 say (PII), must fail here, not pass by default.
    // fleet_recall: FI stage C call 61, present since task 5.4 set FI_RECALL_WARRANTY_LIVE.
    // engineering: FI stage D 9.3 (architect-authorized change, review g93 c1 C1): the live
    // recall run reads recall_campaigns_issued and vehicle_build_components through ADP 9.1's
    // recall views, and recall_campaigns_issued directly in select_recalls.sql (DESCRIBE on the
    // database + SELECT/DESCRIBE on those two named tables).
    // fleet_health, ota_campaigns (and 3 more engineering tables): FI stage D, present since
    // Group 6 rest-plan step 7 set FI_FLEET_HEALTH_LIVE: the 12 LfFiHealth grants (2 database
    // DESCRIBE + 10 named tables; the engineering database and vehicle_build_components are
    // the shared helper's above). Their scope is pinned in vsa-analytics-stack-fi-health.test.ts.
    const dbs = new Set(grants().map((p) => p.Resource.Database?.Name ?? p.Resource.Table?.DatabaseName));
    expect([...dbs].sort()).toEqual([
      'dimensions', 'energy_usage', 'engineering', 'fleet_cost', 'fleet_health', 'fleet_ops',
      'fleet_recall', 'ota_campaigns', 'service_records', 'tire_health', 'vehicle_identity',
      'vehicle_pricing',
    ].map((d) => `adp_staging_${d}`));
    expect(grants()).toHaveLength(21 + 12);
  });

  test('no grant to the role is wider than SELECT/DESCRIBE', () => {
    for (const p of grants()) {
      for (const perm of p.Permissions) expect(['SELECT', 'DESCRIBE']).toContain(perm);
    }
  });
});
