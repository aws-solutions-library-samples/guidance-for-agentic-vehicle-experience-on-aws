/**
 * owner_id = the vehicle's assigned driver (issue
 * 2026-10-07-avx-finding-owner-is-purchaser-not-driver).
 *
 * The Tier 2 role gains exactly two grants for the lookup, and the Lambda two env
 * vars. Asserted on the synthesized template, with and without a CMS pool id.
 * Python mirror for CI: agents/tier2/tests/test_owner_driver_grants_pinned.py.
 *
 * Mutations, each caught here: resources -> ['*']; an extra action on either
 * statement; the Scan pointed at storage-vehicles; the ListUsers grant emitted when
 * the pool id is absent or malformed; CMS_USER_POOL_ID hardcoded.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { VsaTier2Stack } from '../lib/vsa-tier2-stack';
import { TEST_CMS_FI_INTERNAL_API_ENDPOINT } from './helpers/cms-fi-internal-endpoint';
import { TEST_DMS_FI_INTERNAL_API_ENDPOINT } from './helpers/dms-fi-internal-endpoint';

const POOL = 'us-west-2_TestJ1'; // under 8 chars: the publish scanner flags pool-id shapes

function synth(cmsUserPoolId?: string): Template {
  const app = new cdk.App();
  const stack = new VsaTier2Stack(app, 'vsa-staging-tier2', {
    cmsFiInternalApiEndpoint: TEST_CMS_FI_INTERNAL_API_ENDPOINT,
    dmsFiInternalApiEndpoint: TEST_DMS_FI_INTERNAL_API_ENDPOINT,
    env: { account: '123456789012', region: 'us-west-2' },
    stage: 'staging',
    adpRegion: 'us-east-1',
    adpAccountId: '123456789012',
    cmsKmsKeyArn: {
      literal: 'arn:aws:kms:us-west-2:123456789012:key/test-key-id',
      parameterName: '/cvx/staging/cms-kms-key-arn',
    },
    ...(cmsUserPoolId === undefined ? {} : { cmsUserPoolId }),
  });
  return Template.fromStack(stack);
}

function statements(t: Template): Array<Record<string, any>> {
  const out: Array<Record<string, any>> = [];
  for (const r of Object.values(t.findResources('AWS::IAM::Policy'))) {
    out.push(...((r as any).Properties?.PolicyDocument?.Statement ?? []));
  }
  return out;
}

const asArray = (v: unknown): unknown[] => (Array.isArray(v) ? v : [v]);

function render(r: unknown): string {
  if (typeof r === 'string') return r;
  const join = (r as any)?.['Fn::Join'];
  if (Array.isArray(join)) {
    return (join[1] as unknown[]).map((p) => (typeof p === 'string' ? p : JSON.stringify(p))).join(join[0]);
  }
  return JSON.stringify(r);
}

function tier2Env(t: Template): Record<string, string> {
  for (const fn of Object.values(t.findResources('AWS::Lambda::Function'))) {
    const vars = (fn as any).Properties?.Environment?.Variables ?? {};
    if ('AVX_FINDINGS_TABLE' in vars) return vars;
  }
  throw new Error('Tier 2 Lambda not found');
}

describe('assigned-driver owner lookup grants', () => {
  const withPool = synth(POOL);

  test('CmsDriversAssignmentScan is dynamodb:Scan on the drivers table only', () => {
    const s = statements(withPool).filter((x) => x.Sid === 'CmsDriversAssignmentScan');
    expect(s).toHaveLength(1);
    expect(asArray(s[0].Action)).toEqual(['dynamodb:Scan']);
    const res = asArray(s[0].Resource).map(render);
    expect(res).toEqual(['arn:aws:dynamodb:us-west-2:123456789012:table/cms-staging-storage-drivers']);
  });

  test('CmsUserPoolDriverLookup is cognito-idp:ListUsers on the CMS pool only', () => {
    const s = statements(withPool).filter((x) => x.Sid === 'CmsUserPoolDriverLookup');
    expect(s).toHaveLength(1);
    expect(asArray(s[0].Action)).toEqual(['cognito-idp:ListUsers']);
    expect(asArray(s[0].Resource).map(render)).toEqual([
      `arn:aws:cognito-idp:us-west-2:123456789012:userpool/${POOL}`,
    ]);
  });

  test('no other statement grants any Cognito action', () => {
    const cognito = statements(withPool).filter((x) =>
      asArray(x.Action).some((a) => String(a).startsWith('cognito-idp:') || a === '*'));
    expect(cognito.map((x) => x.Sid)).toEqual(['CmsUserPoolDriverLookup']);
  });

  test('the drivers table is reachable only through the Scan statement', () => {
    const touching = statements(withPool).filter((x) =>
      asArray(x.Resource).map(render).some((r) => r.includes('storage-drivers')));
    expect(touching.map((x) => x.Sid)).toEqual(['CmsDriversAssignmentScan']);
  });

  test('env carries the drivers table and the pool id', () => {
    const env = tier2Env(withPool);
    expect(env.CMS_DRIVERS_TABLE).toBe('cms-staging-storage-drivers');
    expect(env.CMS_USER_POOL_ID).toBe(POOL);
  });

  test.each([[undefined], [''], ['none'], ['not-a-pool-id']])(
    'no pool id (%p): no ListUsers grant and an empty CMS_USER_POOL_ID', (pool) => {
      const t = synth(pool);
      expect(statements(t).filter((x) => x.Sid === 'CmsUserPoolDriverLookup')).toHaveLength(0);
      expect(tier2Env(t).CMS_USER_POOL_ID).toBe('');
      // The Scan grant is harmless without the pool and keeps the template shape stable.
      expect(statements(t).filter((x) => x.Sid === 'CmsDriversAssignmentScan')).toHaveLength(1);
    });
});
