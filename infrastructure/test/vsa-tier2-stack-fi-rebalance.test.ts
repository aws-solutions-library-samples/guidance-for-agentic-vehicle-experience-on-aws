/**
 * Task 5.2 (spec 2026-10-06-fi-rebalancing-stage-e): Tier 2 stack additions
 * for the rebalance component.
 *
 * Three synths: default (the ``FI_REBALANCING_LIVE`` constant, ``true`` since Group 6
 * task 6.2), explicit ``true`` and explicit ``false``.  Verifies:
 *
 *   1. The 06:00 UTC rebalance schedule exists; is ENABLED by default and with
 *      ``fiRebalancingLive = true``, DISABLED with ``fiRebalancingLive = false``.
 *   2. The rebalance run-staleness alarm exists; ``ActionsEnabled`` matches
 *      ``fiRebalancingLive``.
 *   3. The ``FiRebalanceTier2Policy`` managed policy exists on the Tier 2
 *      lambda role with EXACTLY the Glue grants on
 *      ``adp_${stage}_fleet_rebalance`` and its seven named tables — no
 *      ``database/*`` wildcard (mutation M4 anchor).
 *   4. The Lambda env carries ``FI_REBAL_MODEL_ID`` and
 *      ``FI_REBAL_MAX_MODEL_CALLS = '12'``.
 *   5. The inline aggregate on the Tier 2 role stays well under IAM's
 *      10,240-byte per-role ceiling (budget guard, same pattern as
 *      ``vsa-core-stack-agentcore-restricted.test.ts``).
 */

import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { VsaTier2Stack } from '../lib/vsa-tier2-stack';
import { TEST_CMS_FI_INTERNAL_API_ENDPOINT } from './helpers/cms-fi-internal-endpoint';
import { TEST_DMS_FI_INTERNAL_API_ENDPOINT } from './helpers/dms-fi-internal-endpoint';

const ACCOUNT = '123456789012';
const REGION  = 'us-west-2';
const ADP_REGION = 'us-east-1';
const HEALTH_AGENT = `vsa-staging-tier2-health-agent-${REGION}`;

function makeTier2(fiRebalancingLive?: boolean): Template {
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
      ...(fiRebalancingLive !== undefined ? { fiRebalancingLive } : {}),
    });
    return Template.fromStack(stack);
  } finally {
    if (prev === undefined) delete process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT;
    else process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT = prev;
  }
}

let cachedDefault: Template | undefined;
let cachedLive: Template | undefined;
let cachedOff: Template | undefined;
function tier2Default(): Template { return (cachedDefault ??= makeTier2()); }
function tier2Live():    Template { return (cachedLive    ??= makeTier2(true)); }
function tier2Off():     Template { return (cachedOff     ??= makeTier2(false)); }

/** Logical IDs of every CfnPolicy and ManagedPolicy that applies to the Tier 2 Lambda role. */
function tier2RolePolicies(t: Template): Array<{ id: string; stmts: unknown[] }> {
  // The Tier 2 Lambda role is the IAM role attached to the Tier2HealthAgent
  // Lambda function.  Find it by that function's Role attribute, not by
  // name convention (every Lambda role trusts lambda.amazonaws.com — scanning
  // all of them picks up unrelated roles).
  const fns = t.findResources('AWS::Lambda::Function', {
    Properties: { FunctionName: HEALTH_AGENT },
  });
  const [, fnRes] = Object.entries(fns)[0] as [string, { Properties: { Role: { 'Fn::GetAtt': [string, string] } } }];
  const roleId = fnRes.Properties.Role['Fn::GetAtt'][0];

  const results: Array<{ id: string; stmts: unknown[] }> = [];
  const cfn = t.findResources('AWS::IAM::Policy');
  for (const [pid, pres] of Object.entries(cfn)) {
    const p = pres as { Properties: { Roles?: Array<{ Ref?: string }>; PolicyDocument: { Statement: unknown[] } } };
    if ((p.Properties.Roles || []).some((rref) => rref.Ref === roleId)) {
      results.push({ id: pid, stmts: p.Properties.PolicyDocument.Statement });
    }
  }
  const mps = t.findResources('AWS::IAM::ManagedPolicy');
  for (const [mid, mres] of Object.entries(mps)) {
    const m = mres as { Properties: { Roles?: Array<{ Ref?: string } | string>; PolicyDocument: { Statement: unknown[] } } };
    const refs = (m.Properties.Roles || []).map(
      (r) => (typeof r === 'string' ? r : r.Ref)
    );
    if (refs.includes(roleId)) {
      results.push({ id: mid, stmts: m.Properties.PolicyDocument.Statement });
    }
  }
  // Dedup by id (defensive — the two loops above shouldn't overlap).
  const seen = new Set<string>();
  return results.filter((r) => (seen.has(r.id) ? false : (seen.add(r.id), true)));
}

function findSid(stmts: unknown[], sid: string): Record<string, unknown> | undefined {
  for (const s of stmts) {
    const stmt = s as { Sid?: string };
    if (stmt.Sid === sid) return s as Record<string, unknown>;
  }
  return undefined;
}

// ---------------------------------------------------------------------------
// Schedule (06:00 UTC)
// ---------------------------------------------------------------------------
describe('Schedule — ``rebalance`` component at 06:00 UTC', () => {
  test('default synth: schedule exists and is ENABLED (Group 6 task 6.2)', () => {
    const t = tier2Default();
    const scheds = t.findResources('AWS::Scheduler::Schedule', {
      Properties: { Name: `vsa-staging-tier2-daily-rebalance-${REGION}` },
    });
    expect(Object.keys(scheds)).toHaveLength(1);
    const sched = Object.values(scheds)[0] as { Properties: Record<string, unknown> };
    expect(sched.Properties.ScheduleExpression).toBe('cron(0 6 * * ? *)');
    expect(sched.Properties.State).toBe('ENABLED');
    // The input must carry the component name, otherwise the runner defaults to
    // the battery path (T6a.5).  Scheduler input is a JSON STRING.
    expect(sched.Properties.Target).toMatchObject({ Input: expect.any(String) });
    const input = JSON.parse(((sched.Properties.Target as { Input: string }).Input));
    expect(input).toEqual({ stage: 'staging', component: 'rebalance' });
  });

  test('live synth: schedule is ENABLED when fiRebalancingLive = true', () => {
    const t = tier2Live();
    const scheds = t.findResources('AWS::Scheduler::Schedule', {
      Properties: { Name: `vsa-staging-tier2-daily-rebalance-${REGION}` },
    });
    expect(Object.keys(scheds)).toHaveLength(1);
    const sched = Object.values(scheds)[0] as { Properties: Record<string, unknown> };
    expect(sched.Properties.State).toBe('ENABLED');
  });

  test('off synth: schedule is DISABLED when fiRebalancingLive = false', () => {
    const t = tier2Off();
    const scheds = t.findResources('AWS::Scheduler::Schedule', {
      Properties: { Name: `vsa-staging-tier2-daily-rebalance-${REGION}` },
    });
    expect(Object.keys(scheds)).toHaveLength(1);
    const sched = Object.values(scheds)[0] as { Properties: Record<string, unknown> };
    expect(sched.Properties.State).toBe('DISABLED');
  });
});

// ---------------------------------------------------------------------------
// Run-staleness alarm
// ---------------------------------------------------------------------------
describe('Run-staleness alarm for ``rebalance``', () => {
  test('default synth: alarm exists; ActionsEnabled is true (Group 6 task 6.2)', () => {
    const t = tier2Default();
    const alarms = t.findResources('AWS::CloudWatch::Alarm', {
      Properties: { AlarmName: `vsa-staging-avx-run-stale-rebalance-${REGION}` },
    });
    expect(Object.keys(alarms)).toHaveLength(1);
    const alarm = Object.values(alarms)[0] as { Properties: Record<string, unknown> };
    expect(alarm.Properties.ActionsEnabled).toBe(true);
    expect(alarm.Properties.TreatMissingData).toBe('breaching');
  });

  test('off synth: ActionsEnabled is false when fiRebalancingLive = false', () => {
    const t = tier2Off();
    const alarms = t.findResources('AWS::CloudWatch::Alarm', {
      Properties: { AlarmName: `vsa-staging-avx-run-stale-rebalance-${REGION}` },
    });
    expect(Object.keys(alarms)).toHaveLength(1);
    const alarm = Object.values(alarms)[0] as { Properties: Record<string, unknown> };
    expect(alarm.Properties.ActionsEnabled).toBe(false);
  });

  test('live synth: ActionsEnabled is true when fiRebalancingLive = true', () => {
    const t = tier2Live();
    const alarms = t.findResources('AWS::CloudWatch::Alarm', {
      Properties: { AlarmName: `vsa-staging-avx-run-stale-rebalance-${REGION}` },
    });
    const alarm = Object.values(alarms)[0] as { Properties: Record<string, unknown> };
    expect(alarm.Properties.ActionsEnabled).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// FiRebalanceTier2Policy managed policy — mutation M4 anchor
// ---------------------------------------------------------------------------
describe('FiRebalanceTier2Policy managed policy', () => {
  test('exists as a ManagedPolicy with the Tier 2 lambda role in Roles', () => {
    const t = tier2Default();
    const policies = tier2RolePolicies(t);
    const fiRebal = policies.find((p) => {
      return p.stmts.some((s) => (s as { Sid?: string }).Sid === 'FiRebalanceGlueMetadataRead');
    });
    expect(fiRebal).toBeDefined();
    // Confirm it is a ManagedPolicy, not an inline policy.
    const mps = t.findResources('AWS::IAM::ManagedPolicy');
    const match = Object.entries(mps).find(([id]) => id === fiRebal!.id);
    expect(match).toBeDefined();
  });

  test('FiRebalanceGlueMetadataRead lists named tables only — no database/* wildcard (M4)', () => {
    const t = tier2Default();
    const policies = tier2RolePolicies(t);
    const stmts = policies.flatMap((p) => p.stmts);
    const grant = findSid(stmts, 'FiRebalanceGlueMetadataRead');
    expect(grant).toBeDefined();
    expect(grant!.Effect).toBe('Allow');

    const actions = (grant!.Action as string[] | string);
    expect(new Set(Array.isArray(actions) ? actions : [actions])).toEqual(new Set([
      'glue:GetDatabase',
      'glue:GetTable',
      'glue:GetTables',
      'glue:GetPartitions',
    ]));

    const resources = (grant!.Resource as string[] | string);
    const resList = Array.isArray(resources) ? resources : [resources];
    // Snapshot: fail if the list widens or loses a table.  Order-independent compare.
    const expected = new Set([
      `arn:aws:glue:${ADP_REGION}:${ACCOUNT}:database/adp_staging_fleet_rebalance`,
      `arn:aws:glue:${ADP_REGION}:${ACCOUNT}:table/adp_staging_fleet_rebalance/booking_requests`,
      `arn:aws:glue:${ADP_REGION}:${ACCOUNT}:table/adp_staging_fleet_rebalance/metro_events`,
      `arn:aws:glue:${ADP_REGION}:${ACCOUNT}:table/adp_staging_fleet_rebalance/fleet_bookings_forecast`,
      `arn:aws:glue:${ADP_REGION}:${ACCOUNT}:table/adp_staging_fleet_rebalance/supply_demand_gaps_snapshot`,
      `arn:aws:glue:${ADP_REGION}:${ACCOUNT}:table/adp_staging_fleet_rebalance/move_outcomes_snapshot`,
      `arn:aws:glue:${ADP_REGION}:${ACCOUNT}:table/adp_staging_fleet_rebalance/vehicle_days`,
      `arn:aws:glue:${ADP_REGION}:${ACCOUNT}:table/adp_staging_fleet_rebalance/vehicle_utilization_daily`,
    ]);
    expect(new Set(resList)).toEqual(expected);

    // Explicit mutation-M4 guard: no wildcard anywhere.
    for (const r of resList) {
      expect(r).not.toMatch(/table\/adp_staging_fleet_rebalance\/\*/);
      expect(r).not.toMatch(/database\/\*/);
      // Narrow is deliberately *fleet_rebalance*, not *fleet_recall*.
      expect(r).toContain('fleet_rebalance');
    }
  });
});

// ---------------------------------------------------------------------------
// Lambda env — the two new rebalance env vars
// ---------------------------------------------------------------------------
describe('FiRebalancePolicyParameterRead — the rebalance run reads exactly its policy parameter', () => {
  const POLICY_ARN = `arn:aws:ssm:${REGION}:${ACCOUNT}:parameter/vsa/staging/avx/fi-rebalancing/policy`;

  test('one Allow of ssm:GetParameter on exactly the policy parameter, in FiRebalanceTier2Policy', () => {
    const t = tier2Default();
    const policies = tier2RolePolicies(t);
    const owner = policies.find((p) => findSid(p.stmts, 'FiRebalancePolicyParameterRead') !== undefined);
    expect(owner).toBeDefined();
    expect(Object.keys(t.findResources('AWS::IAM::ManagedPolicy'))).toContain(owner!.id);
    const grant = findSid(owner!.stmts, 'FiRebalancePolicyParameterRead')!;
    expect(grant.Effect).toBe('Allow');
    expect(grant.Action).toEqual('ssm:GetParameter');
    expect(grant.Resource).toEqual(POLICY_ARN);
  });

  test('no statement on the role reaches the policy parameter except that one (no wildcard)', () => {
    // Every Allow on the Tier 2 role whose actions can read SSM: its resources, as strings.
    // Probe set: the policy parameter and a sibling a wildcard would also admit.
    const t = tier2Default();
    const stmts = tier2RolePolicies(t).flatMap((p) => p.stmts) as Array<Record<string, unknown>>;
    const probes = [POLICY_ARN, `arn:aws:ssm:${REGION}:${ACCOUNT}:parameter/vsa/staging/avx/fi-rebalancing/other`];
    // GetParametersByPath is authorized against the requested PATH, not each parameter it
    // returns, so a grant on any ancestor path also reads the policy (review g6fix cycle 2 W1).
    const ancestors = ['vsa', 'vsa/staging', 'vsa/staging/avx', 'vsa/staging/avx/fi-rebalancing']
      .flatMap((p) => [`arn:aws:ssm:${REGION}:${ACCOUNT}:parameter/${p}`, `arn:aws:ssm:${REGION}:${ACCOUNT}:parameter/${p}/`]);
    const pathCapable = (a: string) => a === '*' || /^ssm:(\*|Get\*|GetParameter\*|GetParametersByPath)$/i.test(a);
    const glob = (pattern: string, s: string) =>
      new RegExp('^' + pattern.split('*').map((x) => x.replace(/[.+?^${}()|[\]\\/]/g, '\\$&')).join('.*') + '$').test(s);
    const reaching: string[] = [];
    let examined = 0;
    for (const s of stmts) {
      if (s.Effect !== 'Allow') continue;
      const actions = ([] as string[]).concat(s.Action as string | string[]);
      // Any action that can read a parameter: GetParameter, GetParameters, GetParametersByPath,
      // GetParameterHistory, and wildcards (review g6fix cycle 1 W3: GetParameters was missed).
      if (!actions.some((a) => a === '*' || /^ssm:(\*|Get\*|GetParameter.*)$/i.test(a))) continue;
      for (const r of ([] as unknown[]).concat(s.Resource as unknown)) {
        examined += 1;
        const text = typeof r === 'string' ? r : JSON.stringify(r).includes('Fn::') ? '*' : String(r);
        const targets = actions.some(pathCapable) ? [...probes, ...ancestors] : probes;
        for (const probe of targets) {
          if (glob(text, probe)) reaching.push(`${s.Sid ?? '(no sid)'} ${text} -> ${probe}`);
        }
      }
    }
    expect(examined).toBeGreaterThan(0);
    expect(reaching).toEqual([`FiRebalancePolicyParameterRead ${POLICY_ARN} -> ${POLICY_ARN}`]);
  });
});

describe('Lambda env — rebalance env vars', () => {
  test('FI_REBAL_MODEL_ID and FI_REBAL_MAX_MODEL_CALLS are set on the Tier 2 Lambda', () => {
    const t = tier2Default();
    const fns = t.findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: HEALTH_AGENT },
    });
    expect(Object.keys(fns)).toHaveLength(1);
    const fn = Object.values(fns)[0] as { Properties: { Environment: { Variables: Record<string, unknown> } } };
    const env = fn.Properties.Environment.Variables;
    expect(env.FI_REBAL_MAX_MODEL_CALLS).toBe('12');
    expect(env.FI_REBAL_MODEL_ID).toBeDefined();
    // Model id uses the shared ``claudeProfileId`` which is a string literal.
    expect(typeof env.FI_REBAL_MODEL_ID).toBe('string');
  });
});

// ---------------------------------------------------------------------------
// IAM inline-aggregate budget (10,240-byte ceiling, 300-byte margin)
// ---------------------------------------------------------------------------
describe('IAM inline-aggregate budget on the Tier 2 Lambda role', () => {
  const IAM_INLINE_LIMIT = 10240;
  const SAFETY_MARGIN = 300;

  test('inline aggregate stays under 10,240 - 300 bytes', () => {
    const t = tier2Default();
    // Sum the size of every INLINE policy (AWS::IAM::Policy) attached to the
    // Tier 2 Lambda role.  ManagedPolicy sizes are measured against a separate
    // 6,144-byte-per-policy limit, not the aggregate inline ceiling — hence
    // moving the FI stage D and E grants to a ManagedPolicy.
    const policies = tier2RolePolicies(t);
    const inlineIds = Object.keys(t.findResources('AWS::IAM::Policy'));
    const inlineOnly = policies.filter((p) => {
      return inlineIds.includes(p.id);
    });
    const totalBytes = inlineOnly
      .map((p) => JSON.stringify(p.stmts).length)
      .reduce((a, b) => a + b, 0);
    expect(totalBytes).toBeGreaterThan(0);
    expect(totalBytes).toBeLessThan(IAM_INLINE_LIMIT - SAFETY_MARGIN);
  });

  test('FiRebalanceTier2Policy lives on an AWS::IAM::ManagedPolicy, not inline', () => {
    const t = tier2Default();
    // The policy with FiRebalanceGlueMetadataRead must be a ManagedPolicy
    // resource, not an inline Policy — mutation: moving the grants inline
    // pushes aggregate inline bytes toward the IAM ceiling.
    const mps = t.findResources('AWS::IAM::ManagedPolicy');
    const mpWithRebal = Object.values(mps).find((res) => {
      const r = res as { Properties: { PolicyDocument: { Statement: unknown[] } } };
      return (r.Properties.PolicyDocument.Statement || []).some(
        (s) => (s as { Sid?: string }).Sid === 'FiRebalanceGlueMetadataRead'
      );
    });
    expect(mpWithRebal).toBeDefined();

    const inlines = t.findResources('AWS::IAM::Policy');
    const inlineHasRebal = Object.values(inlines).some((res) => {
      const r = res as { Properties: { PolicyDocument: { Statement: unknown[] } } };
      return (r.Properties.PolicyDocument.Statement || []).some(
        (s) => (s as { Sid?: string }).Sid === 'FiRebalanceGlueMetadataRead'
      );
    });
    expect(inlineHasRebal).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Removed: _NOT_YET_SCHEDULED must no longer carry 'rebalance' (python pin covers it)
// ---------------------------------------------------------------------------
// The Python test agents/tier2/tests/test_run_staleness_alarm_pinned.py pins
// that _NOT_YET_SCHEDULED is empty; keeping that assertion in Python means CI's
// gating job catches a schedule removal without jest.
