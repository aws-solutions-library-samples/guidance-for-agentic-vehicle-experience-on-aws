/**
 * FI stage D task 9.3 (spec 2026-10-05-fi-fleet-health-stage-d): the live recall run's
 * reads of adp_{stage}_engineering.
 *
 * Since stage D 9.1, ADP's recall views read two engineering tables: the received view
 * unions recall_campaigns_issued, and recall_vehicle_status joins recall_campaigns_issued
 * and vehicle_build_components for the lot rule. A standard Athena view resolves as the
 * caller, so the AVX Tier 2 role, which runs the live recall component
 * (FI_RECALL_WARRANTY_LIVE = true), needs Lake Formation and Glue read on both, whether
 * or not stage D's fleet-health switch (FI_FLEET_HEALTH_LIVE) is on. Without them the
 * recall run's query fails once ADP applies the 9.1 view DDL, which stops do-not-drive
 * recall items.
 *
 * Properties pinned:
 *   (a) analytics, fiRw on: the role holds exactly DESCRIBE on the engineering database
 *       and SELECT+DESCRIBE on those two named tables there, no grant option, no wildcard;
 *   (b) analytics, fiRw off: none of the three (with fiHealth off);
 *   (c) at every flag state, no two LF grants in the stack share (principal, resource)
 *       (LF fails the second create);
 *   (d) the grants both switches need keep ONE logical id at every flag state, so flipping
 *       either switch never deletes one and creates its twin (CloudFormation creates the
 *       new one first, and LF refuses it);
 *   (e) tier2: FiRwEngineeringGlueRead names exactly the engineering database and the two
 *       tables, the four Glue read actions, on a ManagedPolicy attached to the role (the
 *       role's inline policies are near IAM's 10,240-byte aggregate), and no Glue Allow on
 *       the role reaches an unnamed engineering table.
 */

import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { VsaAnalyticsStack } from '../lib/vsa-analytics-stack';
import { VsaTier2Stack } from '../lib/vsa-tier2-stack';
import { TEST_CMS_FI_INTERNAL_API_ENDPOINT } from './helpers/cms-fi-internal-endpoint';
import { TEST_DMS_FI_INTERNAL_API_ENDPOINT } from './helpers/dms-fi-internal-endpoint';

const ACCOUNT    = '123456789012';
const ADP_REGION = 'us-east-1';
const REGION     = 'us-west-2';
const STAGE      = 'staging';
const TIER2_ROLE = `vsa-${STAGE}-tier2-scheduler-${REGION}`;
const TIER2_ARN  = `arn:aws:iam::${ACCOUNT}:role/${TIER2_ROLE}`;
const ENG_DB     = `adp_${STAGE}_engineering`;
const HEALTH_AGENT = `vsa-${STAGE}-tier2-health-agent-${REGION}`;

const asList = (x: unknown): unknown[] => (Array.isArray(x) ? x : [x]);

// ─── Analytics stack ───────────────────────────────────────────────────────

type Flags = { fiRw?: boolean; fiHealth?: boolean };

const analyticsCache = new Map<string, Template>();
function analytics(flags: Flags = {}): Template {
  const key = JSON.stringify(flags);
  const hit = analyticsCache.get(key);
  if (hit) return hit;
  const app = new cdk.App();
  const stack = new VsaAnalyticsStack(app, `vsa-${STAGE}-analytics`, {
    env: { account: ACCOUNT, region: ADP_REGION },
    stage: STAGE,
    agentCoreRoleName: `AmazonBedrockAgentCoreSDKRuntime-${REGION}-${STAGE}`,
    tier2RoleName: TIER2_ROLE,
    // No prop = the shared constant decides, exactly as bin/vsa.ts deploys.
    ...(flags.fiRw !== undefined ? { fiRecallWarrantyLive: flags.fiRw } : {}),
    ...(flags.fiHealth !== undefined ? { fiFleetHealthLive: flags.fiHealth } : {}),
  });
  const t = Template.fromStack(stack);
  analyticsCache.set(key, t);
  return t;
}

type Grant = { id: string; props: any };

function lfGrants(t: Template): Grant[] {
  return Object.entries(t.findResources('AWS::LakeFormation::PrincipalPermissions'))
    .map(([id, r]: [string, any]) => ({ id, props: r.Properties }));
}

/** The Tier 2 role's grants on the engineering database, keyed `db` or `db::table`. */
function tier2EngineeringGrants(t: Template): Map<string, Grant> {
  const out = new Map<string, Grant>();
  for (const g of lfGrants(t)) {
    if (g.props?.Principal?.DataLakePrincipalIdentifier !== TIER2_ARN) continue;
    const res = g.props.Resource ?? {};
    const db = res.Database?.Name ?? res.Table?.DatabaseName ?? res.TableWithColumns?.DatabaseName;
    if (db !== ENG_DB) continue;
    const key = res.Database ? db : `${db}::${res.Table?.Name ?? res.TableWithColumns?.Name ?? '<wildcard>'}`;
    expect(out.has(key)).toBe(false);   // a duplicate is property (c)'s failure
    out.set(key, g);
  }
  return out;
}

const RECALL_KEYS = [
  ENG_DB,
  `${ENG_DB}::recall_campaigns_issued`,
  `${ENG_DB}::vehicle_build_components`,
];
const FI_HEALTH_ENG_KEYS = [
  ENG_DB,
  `${ENG_DB}::engineering_failure_modes`,
  `${ENG_DB}::engineering_remedy_options`,
  `${ENG_DB}::fault_patterns`,
  `${ENG_DB}::vehicle_build_components`,
];

/** The logical id each grant both switches can need is pinned to (property d). */
const SHARED_IDS: Record<string, string> = {
  [ENG_DB]: 'LfDbTier2Engineering',
  [`${ENG_DB}::vehicle_build_components`]: 'LfTblTier2EngineeringVehicleBuildComponents',
  [`${ENG_DB}::recall_campaigns_issued`]: 'LfTblTier2EngineeringRecallCampaignsIssued',
};

const ALL_STATES: Array<[string, Flags]> = [
  ['default (constants)', {}],
  ['fiRw on, fiHealth off', { fiRw: true, fiHealth: false }],
  ['fiRw on, fiHealth on', { fiRw: true, fiHealth: true }],
  ['fiRw off, fiHealth on', { fiRw: false, fiHealth: true }],
  ['fiRw off, fiHealth off', { fiRw: false, fiHealth: false }],
];

describe('(a) analytics, recall run live: engineering grants for the Tier 2 role', () => {
  test('fiRw on, fiHealth off: exactly the engineering database and the two tables the recall views read', () => {
    const got = tier2EngineeringGrants(analytics({ fiRw: true, fiHealth: false }));
    expect([...got.keys()].sort()).toEqual([...RECALL_KEYS].sort());
  });

  // Group 6 rest-plan step 7 set FI_FLEET_HEALTH_LIVE = true, so the constants now hold
  // both switches on: the recall run's three plus stage D's (one construct per resource).
  test('default (constants, both live): the recall run\'s three plus stage D\'s engineering grants', () => {
    const got = tier2EngineeringGrants(analytics());
    expect([...got.keys()].sort()).toEqual(
      [...new Set([...RECALL_KEYS, ...FI_HEALTH_ENG_KEYS])].sort());
  });

  test.each([
    ['fiRw on, fiHealth off', { fiRw: true, fiHealth: false }, 3],
    ['default (constants)', {}, 6],
  ])('%s: database grant is DESCRIBE only; table grants SELECT+DESCRIBE, named, no grant option', (_l, f, n) => {
    const got = tier2EngineeringGrants(analytics(f as Flags));
    expect(got.size).toBe(n);   // anti-vacuity
    for (const [key, g] of got) {
      const res = g.props.Resource;
      expect(g.props.PermissionsWithGrantOption).toEqual([]);
      if (key === ENG_DB) {
        expect(res.Database).toEqual({ CatalogId: ACCOUNT, Name: ENG_DB });
        expect(g.props.Permissions).toEqual(['DESCRIBE']);
      } else {
        expect(res.TableWildcard).toBeUndefined();
        expect(res.Table.TableWildcard).toBeUndefined();
        expect(res.Table).toEqual({
          CatalogId: ACCOUNT, DatabaseName: ENG_DB, Name: key.split('::')[1],
        });
        expect(g.props.Permissions).toEqual(['SELECT', 'DESCRIBE']);
      }
    }
  });
});

describe('(b) analytics, recall run off', () => {
  test('fiRw off, fiHealth off: no engineering grant for the Tier 2 role', () => {
    expect(tier2EngineeringGrants(analytics({ fiRw: false, fiHealth: false })).size).toBe(0);
  });

  test('fiRw off, fiHealth on: stage D\'s engineering grants only, no recall_campaigns_issued', () => {
    const got = tier2EngineeringGrants(analytics({ fiRw: false, fiHealth: true }));
    expect([...got.keys()].sort()).toEqual([...FI_HEALTH_ENG_KEYS].sort());
  });

  test('both on: the union, each pair once', () => {
    const got = tier2EngineeringGrants(analytics({ fiRw: true, fiHealth: true }));
    expect([...got.keys()].sort()).toEqual(
      [...new Set([...RECALL_KEYS, ...FI_HEALTH_ENG_KEYS])].sort());
  });
});

describe('(c) no duplicate (principal, resource) LF grant at any flag state', () => {
  test.each(ALL_STATES)('%s', (_l, f) => {
    const seen = new Map<string, string>();
    const grants = lfGrants(analytics(f));
    expect(grants.length).toBeGreaterThan(0);   // anti-vacuity
    for (const g of grants) {
      const key = JSON.stringify([g.props.Principal, g.props.Resource]);
      expect({ key, first: seen.get(key) ?? null }).toEqual({ key, first: null });
      seen.set(key, g.id);
    }
  });
});

describe('(d) grants both switches need keep one logical id at every flag state', () => {
  test.each(ALL_STATES)('%s', (_l, f) => {
    const got = tier2EngineeringGrants(analytics(f));
    for (const [key, id] of Object.entries(SHARED_IDS)) {
      const g = got.get(key);
      if (g) expect({ key, id: g.id }).toEqual({ key, id });
    }
  });

  test('anti-vacuity: each pinned id is present in at least one state', () => {
    for (const [key, id] of Object.entries(SHARED_IDS)) {
      const ids = ALL_STATES.map(([, f]) => tier2EngineeringGrants(analytics(f)).get(key)?.id);
      expect(ids).toContain(id);
    }
  });
});

// ─── Tier 2 stack ─────────────────────────────────────────────────────────

let tier2Cached: Template | undefined;
function tier2(): Template {
  if (tier2Cached) return tier2Cached;
  const prev = process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT;
  process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT = 'ops@example.invalid';
  try {
    const app = new cdk.App();
    const stack = new VsaTier2Stack(app, `vsa-${STAGE}-tier2`, {
      cmsFiInternalApiEndpoint: TEST_CMS_FI_INTERNAL_API_ENDPOINT,
      dmsFiInternalApiEndpoint: TEST_DMS_FI_INTERNAL_API_ENDPOINT,
      env: { account: ACCOUNT, region: REGION },
      stage: STAGE,
      adpRegion: ADP_REGION,
      adpAccountId: ACCOUNT,
      cmsKmsKeyArn: {
        literal: `arn:aws:kms:${REGION}:${ACCOUNT}:key/test-key-id`,
        parameterName: `/cvx/${STAGE}/cms-kms-key-arn`,
      },
      // No fiFleetHealthLive / fiRecallWarrantyLive: the shared constants decide.
    });
    tier2Cached = Template.fromStack(stack);
    return tier2Cached;
  } finally {
    if (prev === undefined) delete process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT;
    else process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT = prev;
  }
}

function healthAgentRoleId(t: Template): string {
  const fns = t.findResources('AWS::Lambda::Function', { Properties: { FunctionName: HEALTH_AGENT } });
  expect(Object.keys(fns)).toHaveLength(1);
  const roleRef = JSON.stringify((Object.values(fns)[0] as any).Properties.Role);
  return /"Fn::GetAtt":\["([A-Za-z0-9]+)"/.exec(roleRef)![1];
}

type Stmt = { Sid?: string; Effect: string; Action: unknown; Resource: unknown };

/** Every statement on the health-agent role: inline policies, managed policies, role-inline. */
function roleStatements(t: Template): Array<{ from: string; stmt: Stmt }> {
  const roleId = healthAgentRoleId(t);
  const out: Array<{ from: string; stmt: Stmt }> = [];
  const attached = (p: any) => JSON.stringify(p.Properties.Roles ?? []).includes(`"${roleId}"`);
  for (const p of Object.values(t.findResources('AWS::IAM::Policy')) as any[]) {
    if (attached(p)) for (const s of p.Properties.PolicyDocument.Statement) out.push({ from: 'inline', stmt: s });
  }
  for (const p of Object.values(t.findResources('AWS::IAM::ManagedPolicy')) as any[]) {
    if (attached(p)) for (const s of p.Properties.PolicyDocument.Statement) out.push({ from: 'managed', stmt: s });
  }
  const role = t.toJSON().Resources[roleId].Properties;
  for (const p of role.Policies ?? []) for (const s of p.PolicyDocument.Statement) out.push({ from: 'role', stmt: s });
  return out;
}

/** Resource glob → match; a non-string resource (an intrinsic) is treated as `*` (fail closed). */
function reaches(resource: unknown, target: string): boolean {
  if (typeof resource !== 'string') return true;
  const re = new RegExp('^' + resource.replace(/[.+^${}()|[\]\\]/g, '\\$&')
    .replace(/\*/g, '.*').replace(/\?/g, '.') + '$');
  return re.test(target);
}

/** Glue action globs (`glue:Get*`, `glue:*`, `*`) that cover glue:GetTable. */
function coversGetTable(action: unknown): boolean {
  const a = String(action).toLowerCase();
  return a === '*' || a === 'glue:*' || a === 'glue:gettable' || a === 'glue:get*';
}

describe('(e) tier2: FiRwEngineeringGlueRead', () => {
  const G = `arn:aws:glue:${ADP_REGION}:${ACCOUNT}`;

  test('one Allow statement, the four Glue read actions, exactly the database and the two tables', () => {
    const found = roleStatements(tier2()).filter(({ stmt }) => stmt.Sid === 'FiRwEngineeringGlueRead');
    expect(found).toHaveLength(1);
    const st = found[0].stmt;
    expect(st.Effect).toBe('Allow');
    expect(asList(st.Action).map(String).sort()).toEqual([
      'glue:GetDatabase', 'glue:GetPartitions', 'glue:GetTable', 'glue:GetTables',
    ]);
    expect(asList(st.Resource).map(String).sort()).toEqual([
      `${G}:database/${ENG_DB}`,
      `${G}:table/${ENG_DB}/recall_campaigns_issued`,
      `${G}:table/${ENG_DB}/vehicle_build_components`,
    ]);
  });

  test('it sits on a ManagedPolicy attached to the health-agent role, not inline', () => {
    const found = roleStatements(tier2()).filter(({ stmt }) => stmt.Sid === 'FiRwEngineeringGlueRead');
    expect(found.map((f) => f.from)).toEqual(['managed']);
  });

  test('the issued table is reached by it; no Glue Allow reaches an unnamed engineering table', () => {
    const glueAllows = roleStatements(tier2()).map((f) => f.stmt).filter((s) =>
      s.Effect === 'Allow' && asList(s.Action).some(coversGetTable));
    const hits = (target: string) => glueAllows
      .filter((s) => asList(s.Resource).some((r) => reaches(r, target)))
      .map((s) => String(s.Sid));
    expect(hits(`${G}:table/${ENG_DB}/recall_campaigns_issued`)).toEqual(['FiRwEngineeringGlueRead']);
    expect(hits(`${G}:table/${ENG_DB}/some_future_table`)).toEqual([]);
  });
});
