/**
 * Rule-approved rebooking on the auto-approver (FI Ground Book Return task 11.7, D7).
 *
 * G8-AVX-2: behind FI_AUTO_REBOOK_LIVE (true since 2026-11-08, prompt 347). Off, the
 * auto-approver has no rebook switch, no DMS internal endpoint, and none of the
 * rebook-specific grants; on, exactly those, each scoped as pinned here.
 *
 * Mutants:
 *   C1: the flag env set with live off  → 'off: no rebook env keys' catches it
 *   C2: invoke grant widened to POST/api/dms/internal/*  → 'invoke … exactly repair-orders' catches it
 *   C3: groundings Query on the table, not the index  → 'groundings Query … exact index ARN' catches it
 *   C4: hold-list GetParameter widened to parameter/cvx/staging/*  → 'hold list … exactly' catches it
 *   C5: FI_SIM_OPERATOR_PARAM dropped from the env  → 'the auto-approver gets …' catches it (prompt 347)
 *   C6: DMS executor policy GetParameter widened to `*` or to parameter/vsa/staging/avx/executor/*
 *       → 'DMS executor policy … exactly' catches it (prompt 361)
 *   C7: VSA_STAGE dropped from the rebook env  → 'rebook on + moves off … VSA_STAGE' catches it
 *   C8: the grant hardcodes /vsa/staging/  → 'DMS executor policy: the parameter paths follow the stage'
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { FI_AUTO_REBOOK_LIVE } from '../lib/fi-auto-rebook-live';
import { VsaTier2Stack } from '../lib/vsa-tier2-stack';

const ACCOUNT  = '123456789012';
const REGION   = 'us-west-2';
const ROLE     = 'vsa-staging-avx-auto-approver-us-west-2';

// CMS endpoint — needed for the stack to build (fiRebalancingAutoApprovalLive is on by default).
const CMS_ENDPOINT = 'https://cmsapi000.execute-api.us-west-2.amazonaws.com/staging';
const CMS_EP_PARAM = '/cvx/staging/cms-fi-internal-api-endpoint';

// DMS internal endpoint for rebook tests.
const DMS_ENDPOINT = 'https://dmsapi111.execute-api.us-west-2.amazonaws.com/staging';
const DMS_EP_PARAM = '/cvx/staging/dms-fi-internal-api-endpoint';

// Expected grant ARNs derived from DMS_ENDPOINT.
const DMS_INVOKE_ARN =
  `arn:aws:execute-api:${REGION}:${ACCOUNT}:dmsapi111/staging/POST/api/dms/internal/fleet/repair-orders`;
const FINDINGS_INDEX_ARN_SUFFIX = '/index/by_vin';
const GROUNDINGS_INDEX_ARN_SUFFIX = '/index/open-groundings-index';
const GROUNDINGS_TABLE_NAME = `cms-staging-storage-vehicle-groundings`;
// Predicate (g), prompt 347: the simulated fleet operator's parameter (its hold list).
const HOLDS_PARAM = '/cvx/staging/fi/sim-operator';
const HOLDS_PARAM_ARN = `arn:aws:ssm:${REGION}:${ACCOUNT}:parameter${HOLDS_PARAM}`;
// Prompt 361: the DMS executor's two policy parameters (agents/executors/base.py).
const DMS_POLICY_ARNS = ['retry-policy', 'escalation'].map(
  (p) => `arn:aws:ssm:${REGION}:${ACCOUNT}:parameter/vsa/staging/avx/executor/dms/${p}`);

function make(opts: {
  rebookLive?: boolean;
  movesLive?: boolean;
  withDmsEndpoint?: boolean;
  dmsEndpointFromSsm?: boolean;
  stage?: string;
}): Template {
  const stage = opts.stage ?? 'staging';
  const prev = process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT;
  process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT = 'ops@example.com';
  try {
    const app = new cdk.App({ context: { 'aws:cdk:bundling-stacks': [] } });
    const stack = new VsaTier2Stack(app, `vsa-${stage}-tier2`, {
      env: { account: ACCOUNT, region: REGION },
      stage,
      adpRegion: 'us-east-1',
      adpAccountId: ACCOUNT,
      cmsKmsKeyArn: {
        literal: `arn:aws:kms:${REGION}:${ACCOUNT}:key/test-key-id`,
        parameterName: '/cvx/staging/cms-kms-key-arn',
      },
      // Keep the CMS endpoint for moves (on by default since task 8.4).
      cmsFiInternalApiEndpoint: opts.movesLive === false
        ? undefined
        : { literal: CMS_ENDPOINT, parameterName: CMS_EP_PARAM },
      ...(opts.rebookLive !== undefined ? { fiAutoRebookLive: opts.rebookLive } : {}),
      ...(opts.movesLive !== undefined ? { fiRebalancingAutoApprovalLive: opts.movesLive } : {}),
      ...(opts.withDmsEndpoint !== false
        ? {
          dmsFiInternalApiEndpoint: opts.dmsEndpointFromSsm
            ? { parameterName: DMS_EP_PARAM }
            : { literal: DMS_ENDPOINT, parameterName: DMS_EP_PARAM },
        }
        : {}),
    });
    return Template.fromStack(stack);
  } finally {
    if (prev === undefined) delete process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT;
    else process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT = prev;
  }
}

// Template builders (lazy, cached).
let tOff_: Template | undefined;
let tOn_: Template | undefined;
// Off: rebook live=false, moves live=false so no accidental CMS-endpoint requirement.
const tOff = () => (tOff_ ??= make({ rebookLive: false, movesLive: false }));
const tOn  = () => (tOn_  ??= make({ rebookLive: true }));

// ── Helpers copied from vsa-tier2-stack-fi-auto-moves.test.ts ──────────────

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
  const re = new RegExp(
    '^' + pattern.split('*').map((p) => p.replace(/[.+?^${}()|[\]\\]/g, '\\$&')).join('.*') + '$',
  );
  return re.test(target);
}

function roleLogicalId(t: Template, roleName: string = ROLE): string {
  const roles = Object.entries(t.findResources('AWS::IAM::Role'))
    .filter(([, r]: any) => r.Properties.RoleName === roleName);
  expect(roles).toHaveLength(1);
  return roles[0][0];
}
function roleStatements(t: Template, roleName: string = ROLE): any[] {
  const id = roleLogicalId(t, roleName);
  const out: any[] = [];
  for (const type of ['AWS::IAM::Policy', 'AWS::IAM::ManagedPolicy']) {
    for (const r of Object.values(t.findResources(type)) as any[]) {
      if (!(r.Properties.Roles ?? []).some((x: any) => x?.Ref === id)) continue;
      out.push(...r.Properties.PolicyDocument.Statement);
    }
  }
  return out;
}
function reaching(t: Template, action: string, arn: string): any[] {
  return roleStatements(t).filter((s) => s.Effect === 'Allow'
    && asList(s.Action).some((a) => globMatches(String(a), action))
    && asList(s.Resource).some((r) => globMatches(render(r), arn)));
}
function autoApproverEnv(t: Template): Record<string, unknown> {
  const fns = Object.values(t.findResources('AWS::Lambda::Function'))
    .filter((r: any) => r.Properties.FunctionName === ROLE);
  expect(fns).toHaveLength(1);
  return (fns[0] as any).Properties.Environment.Variables;
}

// ── Flag value ─────────────────────────────────────────────────────────────

test('the flag is on (user, 2026-11-08, prompt 347)', () => {
  expect(FI_AUTO_REBOOK_LIVE).toBe(true);
});

// ── Off (explicit false, the default) ──────────────────────────────────────

describe('off (default)', () => {
  test('no rebook env keys on the auto-approver (C1 guard)', () => {
    const env = autoApproverEnv(tOff());
    for (const key of [
      'FI_AUTO_REBOOK_ENABLED',
      'VSA_DMS_INTERNAL_API_ENDPOINT',
      'AVX_FINDINGS_TABLE',
      'CMS_GROUNDINGS_TABLE',
      'FI_SIM_OPERATOR_PARAM',
      'VSA_STAGE',
    ]) {
      expect(env).not.toHaveProperty(key);
    }
  });

  test('no rebook-specific grants when off', () => {
    for (const sid of [
      'AutoApproverDmsRebookInvoke',
      'AutoApproverFindingsByVinQuery',
      'AutoApproverGroundingsRebookQuery',
      'AutoApproverActionsGetItem',
      'AutoApproverSimOperatorHoldsRead',
      'AutoApproverDmsExecutorPolicyRead',
    ]) {
      expect(roleStatements(tOff()).filter((s) => s.Sid === sid)).toEqual([]);
    }
  });

  test('off without a DMS endpoint does not fail synth', () => {
    expect(() => make({ rebookLive: false, movesLive: false, withDmsEndpoint: false })).not.toThrow();
  });

  test('the sim-operator Lambda never gets FI_AUTO_REBOOK_ENABLED', () => {
    // The stack without fiSimOperator does not synthesize the sim-operator Lambda at all,
    // so we confirm that no Lambda function other than the auto-approver (which does NOT
    // get it when off) carries the env var.
    const t = tOff();
    const allFns = Object.values(t.findResources('AWS::Lambda::Function'));
    for (const fn of allFns as any[]) {
      const env: Record<string, unknown> = fn.Properties?.Environment?.Variables ?? {};
      expect(env).not.toHaveProperty('FI_AUTO_REBOOK_ENABLED');
    }
  });
});

// ── On ─────────────────────────────────────────────────────────────────────

describe('on', () => {
  test('the auto-approver gets the rebook switch, DMS endpoint, findings and groundings tables', () => {
    const env = autoApproverEnv(tOn());
    expect(env.FI_AUTO_REBOOK_ENABLED).toBe('true');
    expect(env.VSA_DMS_INTERNAL_API_ENDPOINT).toBe(DMS_ENDPOINT);
    // avxFindingsTable is created in the same stack → tableName resolves to a CloudFormation
    // Ref in the template (not a plain string). Just verify the key is present and non-null.
    expect(env.AVX_FINDINGS_TABLE).toBeTruthy();
    expect(env.CMS_GROUNDINGS_TABLE).toBe(GROUNDINGS_TABLE_NAME);
    expect(env.FI_SIM_OPERATOR_PARAM).toBe(HOLDS_PARAM);
  });

  test('hold list: GetParameter on exactly the simulated fleet operator parameter (C4 guard)', () => {
    const hits = roleStatements(tOn()).filter((s) => s.Sid === 'AutoApproverSimOperatorHoldsRead');
    expect(hits).toHaveLength(1);
    expect(asList(hits[0].Action)).toEqual(['ssm:GetParameter']);
    expect(asList(hits[0].Resource).map(render)).toEqual([HOLDS_PARAM_ARN]);
    // No other statement on the role reaches the parameter, and none reaches a sibling.
    expect(reaching(tOn(), 'ssm:GetParameter', HOLDS_PARAM_ARN).map((s) => s.Sid))
      .toEqual(['AutoApproverSimOperatorHoldsRead']);
    const sibling = `arn:aws:ssm:${REGION}:${ACCOUNT}:parameter/cvx/staging/dms-fi-internal-api-endpoint`;
    expect(reaching(tOn(), 'ssm:GetParameter', sibling)
      .filter((s) => s.Sid === 'AutoApproverSimOperatorHoldsRead')).toEqual([]);
  });

  test('DMS executor policy: GetParameter on exactly the two DMS executor parameters (C6 guard)', () => {
    const hits = roleStatements(tOn()).filter((s) => s.Sid === 'AutoApproverDmsExecutorPolicyRead');
    expect(hits).toHaveLength(1);
    expect(asList(hits[0].Action)).toEqual(['ssm:GetParameter']);
    expect(asList(hits[0].Resource).map(render)).toEqual(DMS_POLICY_ARNS);
    // Each parameter is reached by this statement alone.
    for (const arn of DMS_POLICY_ARNS) {
      expect(reaching(tOn(), 'ssm:GetParameter', arn).map((s) => s.Sid))
        .toEqual(['AutoApproverDmsExecutorPolicyRead']);
    }
    // Not another target's parameters, not a third DMS one, not the hold list.
    const others = [
      `arn:aws:ssm:${REGION}:${ACCOUNT}:parameter/vsa/staging/avx/executor/cms/retry-policy`,
      `arn:aws:ssm:${REGION}:${ACCOUNT}:parameter/vsa/staging/avx/executor/dms/other`,
      `arn:aws:ssm:${REGION}:${ACCOUNT}:parameter/vsa/prod/avx/executor/dms/retry-policy`,
      HOLDS_PARAM_ARN,
    ];
    for (const arn of others) {
      expect(reaching(tOn(), 'ssm:GetParameter', arn)
        .filter((s) => s.Sid === 'AutoApproverDmsExecutorPolicyRead')).toEqual([]);
    }
  });

  test('DMS executor policy: the parameter paths follow the stage (C8 guard)', () => {
    // A second stage, so a hardcoded /vsa/staging/ in the grant can't pass.
    const t = make({ rebookLive: true, stage: 'dev' });
    const hits = roleStatements(t, 'vsa-dev-avx-auto-approver-us-west-2')
      .filter((s) => s.Sid === 'AutoApproverDmsExecutorPolicyRead');
    expect(hits).toHaveLength(1);
    expect(asList(hits[0].Resource).map(render)).toEqual(DMS_POLICY_ARNS.map(
      (a) => a.replace('/vsa/staging/', '/vsa/dev/')));
  });

  test('execute-api invoke reaches exactly the repair-orders route, not warranty-claims (C2 guard)', () => {
    const hits = reaching(tOn(), 'execute-api:Invoke', DMS_INVOKE_ARN);
    expect(hits.map((s) => s.Sid)).toEqual(['AutoApproverDmsRebookInvoke']);
    expect(asList(hits[0].Action)).toEqual(['execute-api:Invoke']);
    const resources = asList(hits[0].Resource).map(render);
    expect(resources).toEqual([DMS_INVOKE_ARN]);
    // Not warranty-claims.
    const wcArn = DMS_INVOKE_ARN.replace('repair-orders', 'warranty-claims');
    expect(reaching(tOn(), 'execute-api:Invoke', wcArn)).toEqual([]);
    // Not a wildcard.
    const wildcardArn = `arn:aws:execute-api:${REGION}:${ACCOUNT}:dmsapi111/staging/POST/api/dms/internal/*`;
    expect(reaching(tOn(), 'execute-api:Invoke', wildcardArn).filter(
      (s) => s.Sid === 'AutoApproverDmsRebookInvoke',
    )).toEqual([]);
  });

  test('findings Query scoped to /index/by_vin only, no * (C2 variant)', () => {
    const hits = roleStatements(tOn()).filter((s) => s.Sid === 'AutoApproverFindingsByVinQuery');
    expect(hits).toHaveLength(1);
    expect(asList(hits[0].Action)).toEqual(['dynamodb:Query']);
    const resources = asList(hits[0].Resource).map(render);
    expect(resources.every((r: string) => r.endsWith(FINDINGS_INDEX_ARN_SUFFIX))).toBe(true);
    // Must be the index ARN, not the table ARN (no wildcard).
    expect(resources.every((r: string) => !r.endsWith('/table/*'))).toBe(true);
  });

  test('groundings Query scoped to /index/open-groundings-index only, not the table (C3 guard)', () => {
    const hits = roleStatements(tOn()).filter((s) => s.Sid === 'AutoApproverGroundingsRebookQuery');
    expect(hits).toHaveLength(1);
    expect(asList(hits[0].Action)).toEqual(['dynamodb:Query']);
    const resources = asList(hits[0].Resource).map(render);
    // Must name the index, not just the table.
    expect(resources.every((r: string) => r.endsWith(GROUNDINGS_INDEX_ARN_SUFFIX))).toBe(true);
    // Must name the groundings table.
    expect(resources.some((r: string) => r.includes(GROUNDINGS_TABLE_NAME))).toBe(true);
    // Table-only ARN (no index suffix) must not appear in this statement (C3 mutant).
    expect(resources.every((r: string) => r.includes('/index/'))).toBe(true);
  });

  test('actions GetItem on the actions table only, no wildcard action or resource', () => {
    const hits = roleStatements(tOn()).filter((s) => s.Sid === 'AutoApproverActionsGetItem');
    expect(hits).toHaveLength(1);
    expect(asList(hits[0].Action)).toEqual(['dynamodb:GetItem']);
    // No '*' in action or resource.
    expect(asList(hits[0].Action).every((a: unknown) => a !== '*')).toBe(true);
    expect(asList(hits[0].Resource).every((r: unknown) => r !== '*')).toBe(true);
  });

  test('from SSM, the grant names the API id and stage taken from the endpoint at deploy', () => {
    const t = make({ rebookLive: true, dmsEndpointFromSsm: true });
    const hits = roleStatements(t).filter((s) => s.Sid === 'AutoApproverDmsRebookInvoke');
    expect(hits).toHaveLength(1);
    const json = JSON.stringify(hits[0].Resource);
    expect(json).not.toMatch(/:\*\//);  // no wildcard API id or stage
    expect(json).toContain('Fn::Split');
    expect(json).toContain('/POST/api/dms/internal/fleet/repair-orders');
    expect(JSON.stringify(t.toJSON().Parameters ?? {})).toContain(DMS_EP_PARAM);
  });

  test('on without a DMS endpoint throws at synth', () => {
    expect(() => make({ rebookLive: true, withDmsEndpoint: false })).toThrow(
      /dmsFiInternalApiEndpoint is required/,
    );
  });

  test('no new statement has * as action or resource', () => {
    const newSids = new Set([
      'AutoApproverDmsRebookInvoke',
      'AutoApproverFindingsByVinQuery',
      'AutoApproverGroundingsRebookQuery',
      'AutoApproverActionsGetItem',
      'AutoApproverSimOperatorHoldsRead',
      'AutoApproverDmsExecutorPolicyRead',
    ]);
    const newStmts = roleStatements(tOn()).filter((s) => newSids.has(s.Sid));
    expect(newStmts).toHaveLength(6);
    for (const stmt of newStmts) {
      for (const a of asList(stmt.Action)) {
        expect(a).not.toBe('*');
      }
      for (const r of asList(stmt.Resource)) {
        if (typeof r === 'string') {
          // cloudwatch:PutMetricData legitimately uses '*' with a condition; rebook stmts must not.
          expect(r).not.toBe('*');
        }
      }
    }
  });
});

// ── Interaction with moves block ────────────────────────────────────────────

describe('rebook on + moves on: escalation grants come from moves block (not duplicated)', () => {
  test('only one PutEvents statement when both are live', () => {
    const t = make({ rebookLive: true });  // moves is on by default
    const putEvents = roleStatements(t).filter(
      (s) => asList(s.Action).includes('events:PutEvents'),
    );
    // Only one PutEvents statement (the moves block's); the rebook block skips it.
    expect(putEvents).toHaveLength(1);
    expect(putEvents[0].Sid).toBe('AutoApproverEscalationPutEvents');
  });
});

describe('rebook on + moves off: escalation grants come from rebook block', () => {
  test('escalation grants appear when moves are off and rebook is on', () => {
    // moves=false means no CMS endpoint; rebook=true means DMS endpoint required.
    const t = make({ rebookLive: true, movesLive: false });
    const putEvents = roleStatements(t).filter(
      (s) => asList(s.Action).includes('events:PutEvents'),
    );
    expect(putEvents).toHaveLength(1);
    expect(putEvents[0].Sid).toBe('AutoApproverEscalationPutEventsRebook');
    const putMetric = roleStatements(t).filter(
      (s) => asList(s.Action).includes('cloudwatch:PutMetricData'),
    );
    expect(putMetric).toHaveLength(1);
    expect(putMetric[0].Sid).toBe('AutoApproverEscalationPutMetricRebook');
  });

  test('VSA_STAGE is set, so the executor reads the parameters the grant names (C7 guard)', () => {
    // base.py builds /vsa/<VSA_STAGE>/avx/executor/... (default "staging"); the moves gate
    // also sets it, so this case (moves off) is the one that shows the rebook gate does.
    const env = autoApproverEnv(make({ rebookLive: true, movesLive: false }));
    expect(env.VSA_STAGE).toBe('staging');
    // A second stage, so a hardcoded 'staging' can't pass (review p361 c1 W2).
    const devFns = Object.values(make({ rebookLive: true, movesLive: false, stage: 'dev' })
      .findResources('AWS::Lambda::Function'))
      .filter((r: any) => r.Properties.FunctionName === 'vsa-dev-avx-auto-approver-us-west-2');
    expect(devFns).toHaveLength(1);
    expect((devFns[0] as any).Properties.Environment.Variables.VSA_STAGE).toBe('dev');
  });
});
