/**
 * Rule-approved moves on the auto-approver (FI rebalancing stage E task 8.3, U6).
 *
 * Spec `.kiro/specs/2026-10-06-fi-rebalancing-stage-e/group8-notes.md` G8-D9: behind
 * FI_REBALANCING_AUTO_APPROVAL_LIVE (true since task 8.4's go, 2026-10-24). Off (an explicit
 * false, the rollback), the auto-approver has no move switch, no CMS endpoint and none of the
 * four grants; on, exactly those, each scoped as pinned here.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { FI_REBALANCING_AUTO_APPROVAL_LIVE } from '../lib/fi-rebalancing-auto-live';
import { VsaTier2Stack } from '../lib/vsa-tier2-stack';

const ACCOUNT = '123456789012';
const REGION = 'us-west-2';
const ROLE = 'vsa-staging-avx-auto-approver-us-west-2';
const ENDPOINT = 'https://yourapi000.execute-api.us-west-2.amazonaws.com/prod';
const INTERNAL_ARN = `arn:aws:execute-api:${REGION}:${ACCOUNT}:yourapi000/prod/POST/api/v1/fleet-intelligence/internal/moves`;
const ENDPOINT_PARAM = '/cvx/staging/cms-fi-internal-api-endpoint';
const BOUNDS_ARN = `arn:aws:ssm:${REGION}:${ACCOUNT}:parameter/vsa/staging/avx/fi-rebalancing/auto-approval-bounds`;

function make(live?: boolean, withEndpoint = true, fromSsm = false, endpoint = ENDPOINT): Template {
  const prev = process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT;
  process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT = 'ops@example.com';
  try {
    const app = new cdk.App();
    const stack = new VsaTier2Stack(app, 'vsa-staging-tier2', {
      env: { account: ACCOUNT, region: REGION },
      stage: 'staging',
      adpRegion: 'us-east-1',
      adpAccountId: ACCOUNT,
      cmsKmsKeyArn: {
        literal: `arn:aws:kms:${REGION}:${ACCOUNT}:key/test-key-id`,
        parameterName: '/cvx/staging/cms-kms-key-arn',
      },
      // Rule rebooking (on since 2026-11-08) is pinned by its own suite; off here so this
      // suite's auto-approver surface is the moves block's alone.
      fiAutoRebookLive: false,
      ...(live === undefined ? {} : { fiRebalancingAutoApprovalLive: live }),
      ...(withEndpoint
        ? { cmsFiInternalApiEndpoint: fromSsm ? { parameterName: ENDPOINT_PARAM } : { literal: endpoint, parameterName: ENDPOINT_PARAM } }
        : {}),
    });
    return Template.fromStack(stack);
  } finally {
    if (prev === undefined) delete process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT;
    else process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT = prev;
  }
}

let off: Template | undefined;
let on: Template | undefined;
let dflt: Template | undefined;
// The off template reads the endpoint from SSM, so a gate that leaked would show a Parameter.
// Off is an explicit false: since 8.4 the default is on.
const tOff = () => (off ??= make(false, true, true));
const tOn = () => (on ??= make(true));
const tDefault = () => (dflt ??= make(undefined));

function autoApproverEnv(t: Template): Record<string, unknown> {
  const fns = Object.values(t.findResources('AWS::Lambda::Function'))
    .filter((r: any) => r.Properties.FunctionName === ROLE);
  expect(fns).toHaveLength(1);
  return (fns[0] as any).Properties.Environment.Variables;
}

function roleLogicalId(t: Template): string {
  const roles = Object.entries(t.findResources('AWS::IAM::Role'))
    .filter(([, r]: any) => r.Properties.RoleName === ROLE);
  expect(roles).toHaveLength(1);
  return roles[0][0];
}

function roleStatements(t: Template): any[] {
  const id = roleLogicalId(t);
  const out: any[] = [];
  for (const type of ['AWS::IAM::Policy', 'AWS::IAM::ManagedPolicy']) {
    for (const r of Object.values(t.findResources(type)) as any[]) {
      if (!(r.Properties.Roles ?? []).some((x: any) => x?.Ref === id)) continue;
      out.push(...r.Properties.PolicyDocument.Statement);
    }
  }
  return out;
}

const asList = (x: unknown): unknown[] => (Array.isArray(x) ? x : [x]);
function render(x: unknown): string {
  if (typeof x === 'string') return x;
  if (x && typeof x === 'object' && 'Fn::Join' in (x as any)) {
    const [sep, parts] = (x as any)['Fn::Join'];
    return parts.map(render).join(sep);
  }
  return '*';
}
function globMatches(pattern: string, target: string): boolean {
  const re = new RegExp('^' + pattern.split('*').map((p) => p.replace(/[.+?^${}()|[\]\\]/g, '\\$&')).join('.*') + '$');
  return re.test(target);
}
function reaching(t: Template, action: string, arn: string): any[] {
  return roleStatements(t).filter((s) => s.Effect === 'Allow'
    && asList(s.Action).some((a) => globMatches(String(a), action))
    && asList(s.Resource).some((r) => globMatches(render(r), arn)));
}

test('the flag ships on (task 8.4)', () => {
  expect(FI_REBALANCING_AUTO_APPROVAL_LIVE).toBe(true);
});

describe('the default (no prop) is on', () => {
  test('the auto-approver gets the switch, bounds, endpoint and stage without the prop', () => {
    const env = autoApproverEnv(tDefault());
    expect(env.FI_AUTO_MOVES_ENABLED).toBe('true');
    expect(env.AVX_AUTO_APPROVAL_BOUNDS_PARAM).toBe('/vsa/staging/avx/fi-rebalancing/auto-approval-bounds');
    expect(env.VSA_CMS_FI_INTERNAL_API_ENDPOINT).toBe(ENDPOINT);
    expect(env.VSA_STAGE).toBe('staging');
    expect(reaching(tDefault(), 'execute-api:Invoke', INTERNAL_ARN).map((s) => s.Sid))
      .toEqual(['AutoApproverCmsInternalMovesInvoke']);
  });

  test('the default without the endpoint fails synth', () => {
    expect(() => make(undefined, false)).toThrow(/cmsFiInternalApiEndpoint is required/);
  });
});

describe('off (explicit false, the rollback)', () => {
  test('the auto-approver has no move switch, bounds or CMS endpoint', () => {
    const env = autoApproverEnv(tOff());
    for (const key of ['FI_AUTO_MOVES_ENABLED', 'AVX_AUTO_APPROVAL_BOUNDS_PARAM',
      'VSA_CMS_FI_INTERNAL_API_ENDPOINT', 'VSA_STAGE']) {
      expect(env).not.toHaveProperty(key);
    }
  });

  test('the role can reach neither CMS nor the bounds nor escalate', () => {
    expect(reaching(tOff(), 'execute-api:Invoke', INTERNAL_ARN)).toEqual([]);
    expect(reaching(tOff(), 'ssm:GetParameter', BOUNDS_ARN)).toEqual([]);
    expect(reaching(tOff(), 'events:PutEvents', `arn:aws:events:${REGION}:${ACCOUNT}:event-bus/default`)).toEqual([]);
    expect(roleStatements(tOff()).filter((s) => asList(s.Action).includes('cloudwatch:PutMetricData'))).toEqual([]);
  });

  test('no endpoint is resolved, so an unseeded parameter fails no deploy', () => {
    const params = JSON.stringify(tOff().toJSON().Parameters ?? {});
    expect(params).not.toContain('cms-fi-internal-api-endpoint');
    expect(() => make(false, false)).not.toThrow();
  });
});

describe('on', () => {
  test('the auto-approver gets the switch, the bounds parameter, the endpoint and the stage', () => {
    const env = autoApproverEnv(tOn());
    expect(env.FI_AUTO_MOVES_ENABLED).toBe('true');
    expect(env.AVX_AUTO_APPROVAL_BOUNDS_PARAM).toBe('/vsa/staging/avx/fi-rebalancing/auto-approval-bounds');
    expect(env.VSA_CMS_FI_INTERNAL_API_ENDPOINT).toBe(ENDPOINT);
    expect(env.VSA_STAGE).toBe('staging');
  });

  test('execute-api reaches exactly the internal move route', () => {
    const hits = reaching(tOn(), 'execute-api:Invoke', INTERNAL_ARN);
    expect(hits.map((s) => s.Sid)).toEqual(['AutoApproverCmsInternalMovesInvoke']);
    expect(asList(hits[0].Action)).toEqual(['execute-api:Invoke']);
    expect(asList(hits[0].Resource).map(render)).toEqual([INTERNAL_ARN]);
    for (const other of [
      `arn:aws:execute-api:${REGION}:${ACCOUNT}:abc/prod/POST/api/v1/fleet-intelligence/moves`,
      `arn:aws:execute-api:${REGION}:${ACCOUNT}:abc/prod/GET/api/v1/fleet-intelligence/internal/moves`,
      `arn:aws:execute-api:${REGION}:${ACCOUNT}:abc/prod/POST/api/v1/fleet-intelligence/groundings`,
      `arn:aws:execute-api:us-east-1:${ACCOUNT}:abc/prod/POST/api/v1/fleet-intelligence/internal/moves`,
      `arn:aws:execute-api:${REGION}:${ACCOUNT}:otherapi99/prod/POST/api/v1/fleet-intelligence/internal/moves`,
      `arn:aws:execute-api:${REGION}:${ACCOUNT}:yourapi000/test-invoke-stage/POST/api/v1/fleet-intelligence/internal/moves`,
    ]) {
      expect(reaching(tOn(), 'execute-api:Invoke', other)).toEqual([]);
    }
  });

  test('the bounds read is one parameter, read only', () => {
    const hits = reaching(tOn(), 'ssm:GetParameter', BOUNDS_ARN);
    expect(hits.map((s) => s.Sid)).toEqual(['AutoApproverBoundsRead']);
    expect(asList(hits[0].Action)).toEqual(['ssm:GetParameter']);
    expect(asList(hits[0].Resource).map(render)).toEqual([BOUNDS_ARN]);
  });

  test('escalation: the default bus only, and metrics only in AVX/Executor', () => {
    const events = roleStatements(tOn()).filter((s) => s.Sid === 'AutoApproverEscalationPutEvents');
    expect(events).toHaveLength(1);
    expect(asList(events[0].Resource).map(render)).toEqual([`arn:aws:events:${REGION}:${ACCOUNT}:event-bus/default`]);
    const metric = roleStatements(tOn()).filter((s) => s.Sid === 'AutoApproverEscalationPutMetric');
    expect(metric).toHaveLength(1);
    expect(metric[0].Condition).toEqual({ StringEquals: { 'cloudwatch:namespace': 'AVX/Executor' } });
  });

  test('from SSM, the grant names the API id and stage taken from the endpoint at deploy', () => {
    const t = make(true, true, true);
    const hits = roleStatements(t).filter((s) => s.Sid === 'AutoApproverCmsInternalMovesInvoke');
    expect(hits).toHaveLength(1);
    const json = JSON.stringify(hits[0].Resource);
    expect(json).not.toMatch(/:\*\//);   // no wildcard API id or stage
    expect(json).toContain('Fn::Split');
    expect(json).toContain('/POST/api/v1/fleet-intelligence/internal/moves');
    expect(JSON.stringify(t.toJSON().Parameters ?? {})).toContain(ENDPOINT_PARAM);
  });

  test('the timeout covers a batch of signed dispatches when on, and stays 60 s off', () => {
    // Review c2 W2: the property is Timeout >= BatchSize x one dispatch's 25 s budget
    // (agents/executors/base.py SYNCHRONOUS_BUDGET_S), read from the template, not a literal.
    const fnOf = (t: Template) => Object.entries(t.findResources('AWS::Lambda::Function'))
      .find(([, r]: any) => r.Properties.FunctionName === ROLE) as [string, any];
    const batchOf = (t: Template) => {
      const [fnId] = fnOf(t);
      const maps = Object.values(t.findResources('AWS::Lambda::EventSourceMapping'))
        .filter((r: any) => r.Properties.FunctionName?.Ref === fnId) as any[];
      expect(maps).toHaveLength(1);
      return maps[0].Properties.BatchSize ?? 100;   // DynamoDB streams default
    };
    const [, onFn] = fnOf(tOn());
    expect(onFn.Properties.Timeout).toBeGreaterThanOrEqual(batchOf(tOn()) * 25);
    expect(onFn.Properties.Timeout).toBeLessThanOrEqual(900);
    expect(fnOf(tOff())[1].Properties.Timeout).toBe(60);
    expect(batchOf(tOff())).toBe(batchOf(tOn()));
  });

  test('a literal endpoint without its stage fails synth; CMS output shapes pass', () => {
    // Review c2 W1. CMS's FiInternalApiEndpoint is RestApi.url, which ends in "/<stage>/".
    for (const ok of [`${ENDPOINT}/`, ENDPOINT]) {
      expect(() => make(true, true, false, ok)).not.toThrow();
    }
    for (const bad of [
      'https://yourapi000.execute-api.us-west-2.amazonaws.com',
      'https://yourapi000.execute-api.us-west-2.amazonaws.com/',
      `${ENDPOINT}/extra`,
      `${ENDPOINT}//`,
      `cms-staging-ui.FiInternalApiEndpoint = ${ENDPOINT}/`,
      `${ENDPOINT}/"`,
      'https://cms.example.com/prod/',
    ]) {
      expect(() => make(true, true, false, bad)).toThrow(/must be https:\/\/<api-id>\.execute-api/);
    }
  });

  test('from CMS\'s real output shape, the grant names that API id and stage', () => {
    const hits = reaching(make(true, true, false, `${ENDPOINT}/`), 'execute-api:Invoke', INTERNAL_ARN);
    expect(hits.map((s) => s.Sid)).toEqual(['AutoApproverCmsInternalMovesInvoke']);
    expect(asList(hits[0].Resource).map(render)).toEqual([INTERNAL_ARN]);
  });

  test('turning it on without the endpoint fails synth', () => {
    expect(() => make(true, false)).toThrow(/cmsFiInternalApiEndpoint is required/);
  });
});

describe('on adds exactly the gated block to off', () => {
  // Since 8.4 the deployed shape is "on". vsa-tier2-stack-fi-queue.test.ts pins the
  // auto-approver's off shape exactly (Stage A); this pins the difference exactly, so the
  // deployed shape is pinned whole: nothing beyond the four statements, the four env keys,
  // the timeout and the endpoint parameter. Both templates read the endpoint from SSM, as a
  // deploy does.
  let ssmOn: Template | undefined;
  const fromSsmOn = () => (ssmOn ??= make(true, true, true));
  const stmtKeys = (t: Template) => roleStatements(t).map((s) => JSON.stringify(s)).sort();

  test('the role gains exactly the four gated statements', () => {
    const on = stmtKeys(fromSsmOn());
    const off = stmtKeys(tOff());
    const added = [...on];
    for (const s of off) {
      const i = added.indexOf(s);
      expect(i).toBeGreaterThanOrEqual(0);   // nothing off has is missing on
      added.splice(i, 1);
    }
    expect(added.map((s) => JSON.parse(s).Sid).sort()).toEqual([
      'AutoApproverBoundsRead', 'AutoApproverCmsInternalMovesInvoke',
      'AutoApproverEscalationPutEvents', 'AutoApproverEscalationPutMetric',
    ]);
  });

  test('the env gains exactly the four keys, and keeps the rest unchanged', () => {
    const on = autoApproverEnv(fromSsmOn());
    const off = autoApproverEnv(tOff());
    for (const [k, v] of Object.entries(off)) expect(on[k]).toEqual(v);
    expect(Object.keys(on).filter((k) => !(k in off)).sort()).toEqual([
      'AVX_AUTO_APPROVAL_BOUNDS_PARAM', 'FI_AUTO_MOVES_ENABLED',
      'VSA_CMS_FI_INTERNAL_API_ENDPOINT', 'VSA_STAGE',
    ]);
  });

  test('no other resource changes, and the only new parameter is the endpoint', () => {
    const on = fromSsmOn().toJSON();
    const off = tOff().toJSON();
    const fnId = Object.entries(on.Resources).find(([, r]: any) => r.Properties?.FunctionName === ROLE)![0];
    const policyIds = Object.entries(on.Resources)
      .filter(([, r]: any) => r.Type === 'AWS::IAM::Policy'
        && (r.Properties.Roles ?? []).some((x: any) => x?.Ref === roleLogicalId(fromSsmOn())))
      .map(([id]) => id);
    expect(Object.keys(on.Resources).sort()).toEqual(Object.keys(off.Resources).sort());
    const changed = Object.keys(on.Resources)
      .filter((id) => JSON.stringify(on.Resources[id]) !== JSON.stringify(off.Resources[id]))
      .sort();
    expect(changed).toEqual([fnId, ...policyIds].sort());
    // Review flip c2 S5 (mutant M14: the same policy also attached to another role passed):
    // the role's policy differs on vs off only in its statement list.
    const stripStatements = (r: any) => {
      const c = JSON.parse(JSON.stringify(r));
      delete c.Properties.PolicyDocument.Statement;
      return c;
    };
    for (const id of policyIds) expect(stripStatements(on.Resources[id])).toEqual(stripStatements(off.Resources[id]));
    const onParams = Object.entries(on.Parameters ?? {}).filter(([k]) => !(k in (off.Parameters ?? {})));
    expect(onParams).toHaveLength(1);
    // Review flip c1 S2: the deployed form. The env value is a Ref to that parameter, which
    // CloudFormation resolves from SSM at deploy.
    const [paramId, param] = onParams[0] as [string, any];
    expect(param.Type).toBe('AWS::SSM::Parameter::Value<String>');
    expect(param.Default).toBe(ENDPOINT_PARAM);
    expect(autoApproverEnv(fromSsmOn()).VSA_CMS_FI_INTERNAL_API_ENDPOINT).toEqual({ Ref: paramId });
    for (const k of Object.keys(off.Parameters ?? {})) expect(on.Parameters[k]).toEqual(off.Parameters[k]);
  });

  test('the auto-approver function differs only in env and timeout, and the timeout is the batch budget', () => {
    // Review flip c1 W1 (mutant M6: a property following the flag passed the block above).
    const fnOf = (t: Template) => Object.entries(t.toJSON().Resources)
      .find(([, r]: any) => r.Properties?.FunctionName === ROLE) as [string, any];
    const [fnId, onFn] = fnOf(fromSsmOn());
    const [, offFn] = fnOf(tOff());
    const strip = (r: any) => {
      const c = JSON.parse(JSON.stringify(r));
      delete c.Properties.Environment.Variables;
      delete c.Properties.Timeout;
      return c;
    };
    expect(strip(onFn)).toEqual(strip(offFn));
    const maps = Object.values(fromSsmOn().findResources('AWS::Lambda::EventSourceMapping'))
      .filter((m: any) => m.Properties.FunctionName?.Ref === fnId) as any[];
    expect(maps).toHaveLength(1);
    expect(onFn.Properties.Timeout).toBe(maps[0].Properties.BatchSize * 25 + 50);
    expect(offFn.Properties.Timeout).toBe(60);
  });

  test('Outputs, Conditions, Rules and Mappings are the same on and off', () => {
    // Review flip c1 W1 (mutant M7: an Output added inside the gated block passed).
    const on = fromSsmOn().toJSON();
    const off = tOff().toJSON();
    for (const section of ['Outputs', 'Conditions', 'Rules', 'Mappings']) {
      expect(on[section] ?? null).toEqual(off[section] ?? null);
    }
    const sections = (t: any) => Object.keys(t).sort();
    expect(sections(on)).toEqual(sections(off));
  });
});
