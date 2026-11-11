/**
 * AVX sim-operator infrastructure (spec D10, task 11.4).
 *
 * Pins:
 *   - FI_SIM_OPERATOR_ENABLED is set ONLY on the sim-operator function (mutant: must fail
 *     if also set on the auto-approver or any other function).
 *   - execute-api grants cover exactly the three internal routes (CMS groundings, DMS
 *     repair-orders, DMS warranty-claims); no wildcard actions; no other routes.
 *   - EventBridge Scheduler: cron(10 7 * * ? *), retryAttempts 0.
 *   - Both alarms have their actions pointing at the operator-alert topic.
 *   - The ledger table's timeToLiveAttribute is 'ttl'.
 *
 * 'aws:cdk:bundling-stacks': [] skips Docker asset bundling so the test runs
 * without Docker (same approach used across all VsaTier2Stack unit tests in this repo).
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { VsaTier2Stack } from '../lib/vsa-tier2-stack';
import { TEST_CMS_FI_INTERNAL_API_ENDPOINT } from './helpers/cms-fi-internal-endpoint';

const ACCOUNT = '123456789012';
const REGION  = 'us-west-2';

const SIM_OP_FN        = 'vsa-staging-avx-sim-operator-us-west-2';
const AUTO_APPROVER_FN = 'vsa-staging-avx-auto-approver-us-west-2';

// Literal CMS endpoint so the auto-approver's endpoint-shape check passes at synth.
// Use a different API id from the test helper so the CMS groundings ARN is unambiguous.
const CMS_ENDPOINT = 'https://cmsapi999.execute-api.us-west-2.amazonaws.com/prod';
const DMS_ENDPOINT = 'https://dmsapi111.execute-api.us-west-2.amazonaws.com/prod';

const CMS_GROUNDINGS_ARN =
  `arn:aws:execute-api:${REGION}:${ACCOUNT}:cmsapi999/prod/POST/api/v1/fleet-intelligence/internal/groundings`;
const DMS_REPAIR_ORDERS_ARN =
  `arn:aws:execute-api:${REGION}:${ACCOUNT}:dmsapi111/prod/POST/api/dms/internal/fleet/repair-orders`;
const DMS_WARRANTY_CLAIMS_ARN =
  `arn:aws:execute-api:${REGION}:${ACCOUNT}:dmsapi111/prod/POST/api/dms/internal/fleet/warranty-claims`;
const OPERATOR_TOPIC_NAME = 'vsa-staging-tier2-operator-alerts';

// CMS internal API endpoint helper does the same thing — use the same parameterName so
// CDK deduplicates the SSM parameter ref when both auto-approver and sim-operator call
// resolveConfigValue with the same parameterName.
const CMS_ENDPOINT_PROP = {
  literal:       CMS_ENDPOINT,
  parameterName: '/cvx/staging/cms-fi-internal-api-endpoint',
};

function make(opts: { withDms?: boolean; noSimOperator?: boolean } = {}): Template {
  const prev = process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT;
  process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT = 'ops@example.com';
  try {
    // 'aws:cdk:bundling-stacks': [] tells CDK that no stack needs Docker bundling
    // (bundlingRequired = false), so tests run without a Docker daemon.
    const app   = new cdk.App({ context: { 'aws:cdk:bundling-stacks': [] } });
    const stack = new VsaTier2Stack(app, 'vsa-staging-tier2', {
      env:          { account: ACCOUNT, region: REGION },
      stage:        'staging',
      adpRegion:    'us-east-1',
      adpAccountId: ACCOUNT,
      cmsKmsKeyArn: {
        literal:       `arn:aws:kms:${REGION}:${ACCOUNT}:key/test-key-id`,
        parameterName: '/cvx/staging/cms-kms-key-arn',
      },
      cmsFiInternalApiEndpoint: CMS_ENDPOINT_PROP,
      ...(opts.noSimOperator ? {} : { fiSimOperator: true }),
      // S5: DMS is now required when fiSimOperator:true; default to included.
      // Use withDms: false only to test the synth throw (no fiSimOperator in that case).
      ...(opts.withDms !== false
        ? {
            dmsFiInternalApiEndpoint: {
              literal:       DMS_ENDPOINT,
              parameterName: '/cvx/staging/dms-fi-internal-api-endpoint',
            },
          }
        : {}),
    });
    return Template.fromStack(stack);
  } finally {
    if (prev === undefined) delete process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT;
    else process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT = prev;
  }
}

// ── Lazy template singletons (one synth per variant) ─────────────────────────
let tNoDms:   Template | undefined;
let tWithDms: Template | undefined;
const TNoDms   = () => (tNoDms   ??= make());          // DMS included (now required)
const TWithDms = () => (tWithDms ??= make());

// ── Helpers (modelled on vsa-tier2-stack-fi-auto-moves.test.ts) ──────────────

function findFnByName(t: Template, name: string): any {
  const fns = Object.values(t.findResources('AWS::Lambda::Function'))
    .filter((r: any) => r.Properties.FunctionName === name);
  expect(fns).toHaveLength(1);
  return fns[0];
}

function simOpEnv(t: Template): Record<string, unknown> {
  return (findFnByName(t, SIM_OP_FN) as any).Properties.Environment.Variables;
}

function roleLogicalIdForFn(t: Template, fnName: string): string {
  const roles = Object.entries(t.findResources('AWS::IAM::Role'))
    .filter(([, r]: any) => r.Properties.RoleName === fnName);
  expect(roles).toHaveLength(1);
  return roles[0][0];
}

function statementsForRole(t: Template, roleId: string): any[] {
  const out: any[] = [];
  for (const type of ['AWS::IAM::Policy', 'AWS::IAM::ManagedPolicy']) {
    for (const r of Object.values(t.findResources(type)) as any[]) {
      if (!(r.Properties.Roles ?? []).some((x: any) => x?.Ref === roleId)) continue;
      out.push(...r.Properties.PolicyDocument.Statement);
    }
  }
  return out;
}

const asList = (x: unknown): unknown[] => (Array.isArray(x) ? x : [x]);

function render(x: unknown): string {
  if (typeof x === 'string') return x;
  if (x && typeof x === 'object') {
    if ('Fn::Join' in (x as any)) {
      const [sep, parts] = (x as any)['Fn::Join'];
      return (parts as unknown[]).map(render).join(sep);
    }
    if ('Fn::Sub' in (x as any)) return String((x as any)['Fn::Sub']);
    if ('Fn::GetAtt' in (x as any)) {
      // Render as "LogicalId.Attr" so callers can match on "OperatorAlertTopic"
      const [logicalId, attr] = (x as any)['Fn::GetAtt'];
      return `${logicalId}.${attr}`;
    }
    if ('Ref' in (x as any)) {
      // SNS topic Ref resolves to the topic ARN at deploy; the logical ID contains
      // "OperatorAlertTopic" so alarm-action assertions can match on it.
      return String((x as any)['Ref']);
    }
  }
  return '*';
}

function globMatches(pattern: string, target: string): boolean {
  const re = new RegExp(
    '^' + pattern.split('*').map((p) => p.replace(/[.+?^${}()|[\]\\]/g, '\\$&')).join('.*') + '$',
  );
  return re.test(target);
}

function reaching(t: Template, roleId: string, action: string, arn: string): any[] {
  return statementsForRole(t, roleId).filter(
    (s) =>
      s.Effect === 'Allow'
      && asList(s.Action).some((a) => globMatches(String(a), action))
      && asList(s.Resource).some((r) => globMatches(render(r), arn)),
  );
}

// ── Flag: FI_SIM_OPERATOR_ENABLED is ONLY on the sim-operator ────────────────

test('FI_SIM_OPERATOR_ENABLED=true is set on the sim-operator Lambda', () => {
  expect(simOpEnv(TNoDms()).FI_SIM_OPERATOR_ENABLED).toBe('true');
});

test('FI_SIM_OPERATOR_ENABLED is not set on the auto-approver', () => {
  const env = (findFnByName(TNoDms(), AUTO_APPROVER_FN) as any).Properties.Environment.Variables;
  expect(env).not.toHaveProperty('FI_SIM_OPERATOR_ENABLED');
});

test('FI_SIM_OPERATOR_ENABLED is not set on any other Lambda function', () => {
  // Every Lambda in the template except the sim-operator must lack this env var.
  const allFns = Object.values(TNoDms().findResources('AWS::Lambda::Function')) as any[];
  const withFlag = allFns.filter(
    (r) =>
      r.Properties.FunctionName !== SIM_OP_FN
      && r.Properties.Environment?.Variables?.FI_SIM_OPERATOR_ENABLED !== undefined,
  );
  expect(withFlag).toHaveLength(0);
});

// ── execute-api grants: exactly the three internal routes ─────────────────────

test('CMS groundings invoke grant covers POST/internal/groundings and not /moves', () => {
  const roleId = roleLogicalIdForFn(TNoDms(), SIM_OP_FN);
  const hits = reaching(TNoDms(), roleId, 'execute-api:Invoke', CMS_GROUNDINGS_ARN);
  expect(hits.map((s) => s.Sid)).toEqual(['SimOperatorCmsGroundingsInvoke']);
  expect(asList(hits[0].Action)).toEqual(['execute-api:Invoke']);
  // Must not reach the auto-approver's /moves route
  expect(
    reaching(
      TNoDms(),
      roleId,
      'execute-api:Invoke',
      `arn:aws:execute-api:${REGION}:${ACCOUNT}:cmsapi999/prod/POST/api/v1/fleet-intelligence/internal/moves`,
    ),
  ).toHaveLength(0);
});

// ── S5: fiSimOperator: true without dmsFiInternalApiEndpoint throws at synth ──

test('S5: fiSimOperator without dmsFiInternalApiEndpoint throws at synth', () => {
  expect(() => make({ withDms: false, noSimOperator: false })).toThrow(
    /dmsFiInternalApiEndpoint is required/,
  );
});

test('with DMS endpoint: repair-orders and warranty-claims granted in one statement', () => {
  const roleId = roleLogicalIdForFn(TWithDms(), SIM_OP_FN);
  const hitsRo  = reaching(TWithDms(), roleId, 'execute-api:Invoke', DMS_REPAIR_ORDERS_ARN);
  const hitsWc  = reaching(TWithDms(), roleId, 'execute-api:Invoke', DMS_WARRANTY_CLAIMS_ARN);
  expect(hitsRo.map((s) => s.Sid)).toEqual(['SimOperatorDmsInternalInvoke']);
  expect(hitsWc.map((s) => s.Sid)).toEqual(['SimOperatorDmsInternalInvoke']);
  expect(hitsRo[0]).toBe(hitsWc[0]);  // same statement object
});

test('with DMS endpoint: exactly two DMS resources, no wildcard resource', () => {
  const roleId   = roleLogicalIdForFn(TWithDms(), SIM_OP_FN);
  const dmsStmts = statementsForRole(TWithDms(), roleId).filter(
    (s) => s.Sid === 'SimOperatorDmsInternalInvoke',
  );
  expect(dmsStmts).toHaveLength(1);
  const resources = asList(dmsStmts[0].Resource).map(render);
  expect(resources).toHaveLength(2);
  expect(resources).toContain(DMS_REPAIR_ORDERS_ARN);
  expect(resources).toContain(DMS_WARRANTY_CLAIMS_ARN);
  expect(resources.some((r) => r.includes('*'))).toBe(false);
});

test('with DMS endpoint: VSA_DMS_INTERNAL_API_ENDPOINT env var is set', () => {
  expect(simOpEnv(TWithDms()).VSA_DMS_INTERNAL_API_ENDPOINT).toBe(DMS_ENDPOINT);
});

// ── No wildcard actions anywhere in the sim-operator role ─────────────────────

test('no statement in the sim-operator role uses a wildcard action', () => {
  const roleId       = roleLogicalIdForFn(TWithDms(), SIM_OP_FN);
  const wildcardStmts = statementsForRole(TWithDms(), roleId).filter((s) =>
    asList(s.Action).some((a) => String(a) === '*' || String(a).endsWith(':*')),
  );
  // cloudwatch:PutMetricData uses Resource '*' but that is a resource, not an action
  expect(wildcardStmts).toHaveLength(0);
});

// ── Schedule: cron(10 7 * * ? *) and retries 0 ───────────────────────────────

test('the schedule uses cron(10 7 * * ? *) and is enabled', () => {
  const schedules = Object.values(TNoDms().findResources('AWS::Scheduler::Schedule')) as any[];
  const simOpSched = schedules.find(
    (r) => r.Properties.Name === `vsa-staging-avx-sim-operator-sched-${REGION}`,
  );
  expect(simOpSched).toBeDefined();
  const expr: string = simOpSched.Properties.ScheduleExpression;
  expect(expr).toMatch(/cron\(10\s+7\s+\*\s+\*\s+\?\s+\*\)/);
  expect(simOpSched.Properties.State).toBe('ENABLED');
});

test('the schedule has retryAttempts 0', () => {
  const schedules = Object.values(TNoDms().findResources('AWS::Scheduler::Schedule')) as any[];
  const simOpSched = schedules.find(
    (r) => r.Properties.Name === `vsa-staging-avx-sim-operator-sched-${REGION}`,
  );
  const retryPolicy = simOpSched?.Properties?.Target?.RetryPolicy;
  // CDK renders MaximumRetryAttempts
  expect(
    retryPolicy?.MaximumRetryAttempts ?? retryPolicy?.maximumRetryAttempts,
  ).toBe(0);
});

test('the sim-operator function has no async retries (EventInvokeConfig MaximumRetryAttempts 0)', () => {
  // The Scheduler invokes asynchronously and Lambda retries an async error twice by
  // default; the handler raises after its run record when a unit erred, so a retry
  // would run the day's plan again (architect, prompt 287).
  const t = TNoDms();
  const fnId = Object.entries(t.findResources('AWS::Lambda::Function')).find(
    ([, r]) => (r as any).Properties.FunctionName === `vsa-staging-avx-sim-operator-${REGION}`,
  )?.[0];
  expect(fnId).toBeDefined();
  const configs = Object.values(t.findResources('AWS::Lambda::EventInvokeConfig')) as any[];
  const mine = configs.filter((c) => JSON.stringify(c.Properties.FunctionName).includes(fnId!));
  expect(mine).toHaveLength(1);
  expect(mine[0].Properties.MaximumRetryAttempts).toBe(0);
});

// ── Both alarms publish to the operator-alert topic ──────────────────────────

test('errors alarm and staleness alarm both have AlarmActions to the operator topic', () => {
  const allAlarms = Object.values(TNoDms().findResources('AWS::CloudWatch::Alarm')) as any[];
  const simOpAlarms = allAlarms.filter((r) =>
    String(r.Properties.AlarmName ?? '').includes('sim-operator'),
  );
  expect(simOpAlarms).toHaveLength(2);
  // The operator topic's logical ID contains 'Tier2OperatorAlertTopic'; render() returns
  // the logical ID for Ref values so the match below catches SNS Ref or Fn::GetAtt.
  for (const alarm of simOpAlarms) {
    const actions: string[] = asList(alarm.Properties.AlarmActions ?? []).map(render);
    expect(actions.some((a) => a.includes('Tier2OperatorAlertTopic'))).toBe(true);
  }
});

test('errors alarm and staleness alarm both have OKActions to the operator topic', () => {
  const allAlarms = Object.values(TNoDms().findResources('AWS::CloudWatch::Alarm')) as any[];
  const simOpAlarms = allAlarms.filter((r) =>
    String(r.Properties.AlarmName ?? '').includes('sim-operator'),
  );
  expect(simOpAlarms).toHaveLength(2);
  for (const alarm of simOpAlarms) {
    const okActions: string[] = asList(alarm.Properties.OKActions ?? []).map(render);
    expect(okActions.some((a) => a.includes('Tier2OperatorAlertTopic'))).toBe(true);
  }
});

// ── Ledger table TTL and durability ──────────────────────────────────────────

test('ledger table (vsa-staging-avx-sim-operator) has TTL attribute "ttl"', () => {
  const tables = Object.values(TNoDms().findResources('AWS::DynamoDB::Table')) as any[];
  const ledger  = tables.find((r) => r.Properties.TableName === 'vsa-staging-avx-sim-operator');
  expect(ledger).toBeDefined();
  expect(ledger.Properties.TimeToLiveSpecification).toEqual({
    AttributeName: 'ttl',
    Enabled:       true,
  });
});

test('ledger table has PITR enabled and DeletionPolicy Retain', () => {
  const cfnEntry = Object.entries(
    TNoDms().findResources('AWS::DynamoDB::Table'),
  ).find(([, r]: any) => r.Properties.TableName === 'vsa-staging-avx-sim-operator') as
    [string, any] | undefined;
  expect(cfnEntry).toBeDefined();
  const [, resource] = cfnEntry!;
  expect(resource.Properties.PointInTimeRecoverySpecification?.PointInTimeRecoveryEnabled).toBe(
    true,
  );
  expect(resource.DeletionPolicy).toBe('Retain');
});

// ── Env vars present ──────────────────────────────────────────────────────────

test('sim-operator has all required env vars, and AVX_SIM_OPERATOR_TABLE and FI_SIM_OPERATOR_PARAM are correct', () => {
  const env = simOpEnv(TNoDms());
  for (const key of [
    'FI_SIM_OPERATOR_ENABLED',
    'AVX_SIM_OPERATOR_TABLE',
    'AVX_ACTIONS_TABLE',
    'AVX_FINDINGS_TABLE',
    'AVX_AGENT_RUNS_TABLE',
    'FI_SIM_OPERATOR_PARAM',
    'CMS_VEHICLES_TABLE',
    'CMS_GROUNDINGS_TABLE',
    'VSA_CMS_FI_INTERNAL_API_ENDPOINT',
  ]) {
    expect(env).toHaveProperty(key);
  }
  // AVX_SIM_OPERATOR_TABLE: CDK emits a Ref to the ledger table logical ID since the table
  // is defined in the same stack; the logical ID contains 'AvxSimOperatorLedger'.
  const tableVal = render(env.AVX_SIM_OPERATOR_TABLE);
  expect(tableVal).toMatch(/AvxSimOperatorLedger/);
  expect(env.FI_SIM_OPERATOR_PARAM).toBe('/cvx/staging/fi/sim-operator');
  expect(env.CMS_GROUNDINGS_TABLE).toBe('cms-staging-storage-vehicle-groundings');
  expect(env.CMS_VEHICLES_TABLE).toBe('cms-staging-storage-vehicles');
});

// ── Gate (prompt 287): nothing is synthesized without fiSimOperator: true ──────

test('without fiSimOperator the stack has no sim-operator function, table, schedule or role', () => {
  const t = make({ noSimOperator: true });
  const names = (type: string, key: string) =>
    Object.values(t.findResources(type)).map((r: any) => JSON.stringify(r.Properties?.[key] ?? ''));
  expect(names('AWS::Lambda::Function', 'FunctionName').filter((n) => n.includes('sim-operator'))).toEqual([]);
  expect(names('AWS::DynamoDB::Table', 'TableName').filter((n) => n.includes('sim-operator'))).toEqual([]);
  expect(names('AWS::Scheduler::Schedule', 'Name').filter((n) => n.includes('sim-operator'))).toEqual([]);
  expect(names('AWS::IAM::Role', 'RoleName').filter((n) => n.includes('sim-op'))).toEqual([]);
  const flagged = Object.values(t.findResources('AWS::Lambda::Function'))
    .filter((r: any) => r.Properties?.Environment?.Variables?.FI_SIM_OPERATOR_ENABLED !== undefined);
  expect(flagged).toEqual([]);
});

test('the sim-operator role descriptions are plain ASCII (IAM description character set)', () => {
  const roles = Object.values(TNoDms().findResources('AWS::IAM::Role')) as any[];
  const mine = roles.filter((r) => JSON.stringify(r.Properties.RoleName ?? '').includes('sim-op'));
  expect(mine.length).toBe(2);
  for (const r of mine) expect(String(r.Properties.Description)).toMatch(/^[\x20-\x7e]*$/);
});

// ── S6: execute-api:Invoke resources equal exactly the three internal routes ───

test('S6: execute-api:Invoke on the sim-operator role equals exactly the three internal ARNs', () => {
  // With DMS: CMS groundings + DMS repair-orders + DMS warranty-claims.
  const roleId = roleLogicalIdForFn(TWithDms(), SIM_OP_FN);
  const allInvokeResources = statementsForRole(TWithDms(), roleId)
    .filter((s) => s.Effect === 'Allow' && asList(s.Action).includes('execute-api:Invoke'))
    .flatMap((s) => asList(s.Resource).map(render));
  expect(allInvokeResources).toHaveLength(3);
  expect(allInvokeResources).toContain(CMS_GROUNDINGS_ARN);
  expect(allInvokeResources).toContain(DMS_REPAIR_ORDERS_ARN);
  expect(allInvokeResources).toContain(DMS_WARRANTY_CLAIMS_ARN);
});

// ── W5: staleness alarm is pinned for the property that makes it fire ─────────

test('W5: staleness alarm expression is FILL(m1, 0) — not FILL(m1, 1)', () => {
  const allAlarms = Object.values(TNoDms().findResources('AWS::CloudWatch::Alarm')) as any[];
  const stale = allAlarms.find((r) =>
    String(r.Properties.AlarmName ?? '').includes('avx-sim-operator-stale'),
  );
  expect(stale).toBeDefined();
  // The metric is a MathExpression; CDK renders it as Metrics[] in the CloudFormation resource.
  const metrics: any[] = stale.Properties.Metrics ?? [];
  const exprMetric = metrics.find((m: any) => m.Expression);
  expect(exprMetric).toBeDefined();
  expect(exprMetric.Expression).toBe('FILL(m1, 0)');
});

test('W5: staleness alarm MetricStat uses AVX/SimOperator / SimOperatorRunCompleted, no dimensions', () => {
  const allAlarms = Object.values(TNoDms().findResources('AWS::CloudWatch::Alarm')) as any[];
  const stale = allAlarms.find((r) =>
    String(r.Properties.AlarmName ?? '').includes('avx-sim-operator-stale'),
  );
  const metrics: any[] = stale.Properties.Metrics ?? [];
  const statMetric = metrics.find((m: any) => m.MetricStat);
  expect(statMetric).toBeDefined();
  const { Namespace, MetricName, Dimensions } = statMetric.MetricStat.Metric;
  expect(Namespace).toBe('AVX/SimOperator');
  expect(MetricName).toBe('SimOperatorRunCompleted');
  // Dimensionless metric (empty list or absent).
  expect(Dimensions ?? []).toEqual([]);
});

test('W5: staleness alarm period, evaluation windows, threshold, comparison, missing-data', () => {
  const allAlarms = Object.values(TNoDms().findResources('AWS::CloudWatch::Alarm')) as any[];
  const stale = allAlarms.find((r) =>
    String(r.Properties.AlarmName ?? '').includes('avx-sim-operator-stale'),
  );
  expect(stale.Properties.EvaluationPeriods).toBe(27);
  expect(stale.Properties.DatapointsToAlarm).toBe(27);
  // CDK renders LessThanThreshold as 'LessThanThreshold'.
  expect(stale.Properties.ComparisonOperator).toBe('LessThanThreshold');
  expect(stale.Properties.Threshold).toBe(1);
  expect(stale.Properties.TreatMissingData).toBe('breaching');
  // All MetricStat periods must be 3600 s (1 hour).
  const metrics: any[] = stale.Properties.Metrics ?? [];
  for (const m of metrics) {
    if (m.MetricStat?.Period !== undefined) {
      expect(m.MetricStat.Period).toBe(3600);
    }
  }
});

test('W5: errors alarm threshold >= 1, period 5 min, comparison >= threshold', () => {
  const allAlarms = Object.values(TNoDms().findResources('AWS::CloudWatch::Alarm')) as any[];
  const errsAlarm = allAlarms.find((r) =>
    String(r.Properties.AlarmName ?? '').includes('avx-sim-operator-errors'),
  );
  expect(errsAlarm).toBeDefined();
  expect(errsAlarm.Properties.Threshold).toBe(1);
  expect(errsAlarm.Properties.ComparisonOperator).toBe('GreaterThanOrEqualToThreshold');
  // Period: CDK uses metricErrors() which defaults 5-minute period = 300 s.
  const metricPeriod = errsAlarm.Properties.Period ?? errsAlarm.Properties.Metrics?.[0]?.MetricStat?.Period;
  expect(metricPeriod).toBe(300);
});
