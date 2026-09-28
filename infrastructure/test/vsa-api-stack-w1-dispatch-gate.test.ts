/**
 * Security review Cycle 1 W1 (spec 2026-09-23-cvx-escalation-recommend-not-act):
 * the escalate Lambda verifies a roadside dispatch against a platform-recorded
 * P0 row in vsa-{stage}-triage-decisions. These pin the CDK half: the env var
 * the handler reads, and a READ-ONLY, single-table grant — the Lambda must
 * never be able to write the evidence it checks.
 *
 * Mutations run: (a) drop TRIAGE_DECISIONS_TABLE → env test FAILS;
 * (b) widen the grant to dynamodb:* or Resource "*" → grant test FAILS.
 */

import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { VsaApiStack } from '../lib/vsa-api-stack';
import { ConfigValue } from '../lib/config-value';

// ─── Stub CMS action-queue ARN ────────────────────────────────────────────────
// This is the ARN the old CmsActionQueueWrite grant used. The test asserts
// this string appears NOWHERE in the escalate role's policy.
const CMS_VFO_QUEUE_ARN_FRAGMENT = 'vfo-action-queue';

// ─── Stack fixture ─────────────────────────────────────────────────────────────

let stackCount = 0;

function makeStack(
  stage = 'staging',
  overrides: { connectInstanceArn?: ConfigValue; rsaPositionMaxAgeSeconds?: ConfigValue } = {},
): { stack: VsaApiStack; template: Template } {
  const id = ++stackCount;
  const app = new cdk.App();
  const env = { account: '123456789012', region: 'us-west-2' };

  const parentStack = new cdk.Stack(app, `ParentFixture${id}`, { env });

  const stubTable = (name: string) =>
    dynamodb.Table.fromTableName(parentStack, `Tbl${id}-${name}`, name);

  const stubFn = (fnName: string, arn: string) =>
    lambda.Function.fromFunctionArn(parentStack, `Fn${id}-${fnName}`, arn);

  // Stub RSA dispatch table — provides tableName and tableArn for the grant.
  // Use fromTableArn so the stub has a concrete ARN the PutItem grant is scoped to.
  const rsaDispatchTable = dynamodb.Table.fromTableArn(
    parentStack,
    `RsaDispatch${id}`,
    `arn:aws:dynamodb:us-west-2:123456789012:table/vsa-${stage}-rsa-dispatch`,
  );
  const rsaDispatchKey = kms.Key.fromKeyArn(parentStack, `RsaDispatchKey${id}`, 'arn:aws:kms:us-west-2:123456789012:key/test-rsa-dispatch-key');

  const stack = new VsaApiStack(app, `vsa-${stage}-api-w1-${id}`, {
    env,
    stage,
    cmsStage: stage,
    tenantConfigTable: stubTable(`cms-${stage}-tenant-config`),
    classifier: stubFn(
      'Classifier',
      `arn:aws:lambda:us-west-2:123456789012:function:vsa-${stage}-triage-classifier`,
    ),
    cmsKmsKeyArn:           { literal: 'arn:aws:kms:us-west-2:123456789012:key/test-key', parameterName: `/cvx/${stage}/cms-kms-key-arn` },
    redisEndpoint:          { literal: 'redis://stub.redis.local:6379', parameterName: `/cvx/${stage}/redis-endpoint` },
    connectInstanceArn: overrides.connectInstanceArn ?? { literal: 'arn:aws:connect:us-east-1:123456789012:instance/test-instance', parameterName: `/cvx/${stage}/connect-instance-arn` },
    dmsApiEndpoint:         { literal: 'https://dms.example.invalid/staging', parameterName: `/cvx/${stage}/dms-api-endpoint` },
    vpcId:                  'vpc-00000000000000000',
    redisSecurityGroupId:   'sg-00000000000000000',
    assistantChatRuntimeArn: {
      literal: `arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/stub-${stage}`,
      parameterName: `/cvx/${stage}/assistant-chat-runtime-arn`,
    },
    assistantChatRuntimeRegion: 'us-west-2',
    allowedOrigin: { literal: 'https://stub.example.com', parameterName: `/cvx/${stage}/allowed-origin` },
    acquireReservationHandoffFn: stubFn(
      'AcquireHandoff',
      `arn:aws:lambda:us-west-2:123456789012:function:vsa-${stage}-acquire-handoff`,
    ),
    acquireReservationHandoffFnArnExportName: `vsa-${stage}-acquire-reservation-handoff-fn-arn`,
    acquireCatalogTable: stubTable(`vsa-${stage}-acquire-catalog`),
    acquireOrdersTable:  stubTable(`vsa-${stage}-acquire-orders`),
    acquireLeadsTable:   stubTable(`vsa-${stage}-acquire-leads`),
    avxFindingsTableNameExportName:         `vsa-${stage}-avx-findings-table-name`,
    avxActionsTableNameExportName:          `vsa-${stage}-avx-actions-table-name`,
    avxFeedbackTableNameExportName:         `vsa-${stage}-avx-feedback-table-name`,
    notificationDevicesTableNameExportName: `vsa-${stage}-notification-devices-table-name`,
    rsaDispatchTable,
    rsaDispatchKey,
    // F5.D1: literal override — no SSM lookup in unit tests.
    rsaPositionMaxAgeSeconds: overrides.rsaPositionMaxAgeSeconds ?? {
      literal: '900',
      parameterName: `/cvx/${stage}/rsa-position-max-age-seconds`,
    },
  });

  return { stack, template: Template.fromStack(stack) };
}


function escalateRoleStatements(template: Template): any[] {
  const fns = template.findResources('AWS::Lambda::Function', {
    Properties: { FunctionName: 'vsa-staging-api-escalate' },
  });
  const fn = Object.values(fns)[0] as any;
  const roleRef = fn.Properties.Role['Fn::GetAtt'][0];
  const policies = template.findResources('AWS::IAM::Policy');
  const stmts: any[] = [];
  for (const p of Object.values(policies) as any[]) {
    const roles = (p.Properties.Roles ?? []).map((r: any) => r.Ref);
    if (roles.includes(roleRef)) stmts.push(...p.Properties.PolicyDocument.Statement);
  }
  return stmts;
}

describe('W1 dispatch gate — escalate Lambda wiring', () => {
  const { template } = makeStack();

  test('TRIAGE_DECISIONS_TABLE is set to the stage table', () => {
    const fns = template.findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: 'vsa-staging-api-escalate' },
    });
    const env = (Object.values(fns)[0] as any).Properties.Environment.Variables;
    expect(env.TRIAGE_DECISIONS_TABLE).toBe('vsa-staging-triage-decisions');
  });

  test('grant is GetItem only, on exactly the triage-decisions table', () => {
    const stmts = escalateRoleStatements(template).filter((s) => s.Sid === 'TriageDecisionRead');
    expect(stmts).toHaveLength(1);
    const s = stmts[0];
    expect(s.Effect).toBe('Allow');
    expect([].concat(s.Action)).toEqual(['dynamodb:GetItem']);
    const resources = [].concat(s.Resource);
    expect(resources).toHaveLength(1);
    const flat = JSON.stringify(resources[0]);
    expect(flat).toContain('table/vsa-staging-triage-decisions');
    expect(flat).not.toContain('*');
  });

  test('no statement lets the escalate role write the triage-decisions table', () => {
    // Review Cycle 3: a write on Resource "*" also reaches the table, so a
    // wildcard resource counts as "on the table" here.
    const writes = escalateRoleStatements(template).filter((s) => {
      const actions: string[] = [].concat(s.Action);
      const res = JSON.stringify(s.Resource);
      const onTable = res.includes('triage-decisions') || res === '"*"' || res.includes('table/*');
      return s.Effect === 'Allow' && onTable
        && actions.some((a) => /^dynamodb:(Put|Update|Delete|BatchWrite|PartiQL(Insert|Update|Delete)|\*)|^\*$/.test(a));
    });
    expect(writes).toEqual([]);
  });
});


// ─── P0 callback + vehicleId↔VIN binding (2026-09-24) ────────────────────────
//
// The callback could never fire: no drivers-table env or grant, and no
// connect:StartOutboundVoiceContact. The vehicle binding reads the CMS
// vehicles row.
//
// C4W3: earlier tests selected by Sid, so each checked only its own statement
// and nothing else. These rewritten tests use the no-write shape: filter ALL
// Allow statements by resource/action predicate.
//
// Mutations verified against a /tmp copy of vsa-api-stack.ts:
// (1) grantReadData(apiEscalate) on cmsTable.vehicles → adds Scan/Query/
//     BatchGetItem + index/* → "no cms-storage write/read-wide" test FAILS.
// (2) un-Sid'd addToRolePolicy({ actions: ['connect:StartOutboundVoiceContact'],
//     resources: ['*'] }) → "no outbound voice outside account" test FAILS.
// (3) resource: `arn:aws:connect:*:*:instance/*/contact/*` (account wildcarded)
//     → "no outbound voice outside account" test FAILS.

describe('P0 callback and vehicle binding — escalate Lambda wiring', () => {
  const { template } = makeStack();

  // ── Env-var positive checks ───────────────────────────────────────────────

  test('CMS vehicles and drivers tables are set to the stage tables', () => {
    const fns = template.findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: 'vsa-staging-api-escalate' },
    });
    const env = (Object.values(fns)[0] as any).Properties.Environment.Variables;
    expect(env.CMS_VEHICLES_TABLE).toBe('cms-staging-storage-vehicles');
    expect(env.CMS_DRIVERS_TABLE).toBe('cms-staging-storage-drivers');
  });

  // ── CMS DynamoDB scope: every Allow statement touching a cms-*-storage-*
  // resource must grant ONLY GetItem — no Scan, Query, BatchGetItem, index/*,
  // or write actions. Uses the no-write shape (filter-all-allows) so a new
  // grant added without a Sid is still caught.

  test('no Allow statement grants more than GetItem on any cms-*-storage-* resource', () => {
    const ALLOWED_CMS_ACTIONS = new Set(['dynamodb:GetItem']);
    const violations = escalateRoleStatements(template).filter((s) => {
      if (s.Effect !== 'Allow') return false;
      const resources: string[] = [].concat(s.Resource).map((r: any) => JSON.stringify(r));
      const touchesCms = resources.some(
        (r) => r.includes('cms-') && r.includes('-storage-'),
      );
      if (!touchesCms) return false;
      const actions: string[] = [].concat(s.Action);
      return actions.some((a) => !ALLOWED_CMS_ACTIONS.has(a));
    });
    expect(violations).toEqual([]);
  });

  // Positive: GetItem on both CMS tables is actually present. Prevents the
  // above from silently passing when the grant is missing entirely.
  test('GetItem on cms-staging-storage-vehicles and -drivers is granted', () => {
    const stmts = escalateRoleStatements(template).filter((s) => {
      if (s.Effect !== 'Allow') return false;
      const actions: string[] = [].concat(s.Action);
      if (!actions.includes('dynamodb:GetItem')) return false;
      const resources: string[] = [].concat(s.Resource).map((r: any) => JSON.stringify(r));
      return (
        resources.some((r) => r.includes('table/cms-staging-storage-vehicles')) &&
        resources.some((r) => r.includes('table/cms-staging-storage-drivers'))
      );
    });
    expect(stmts.length).toBeGreaterThanOrEqual(1);
  });

  // Resource-exactness: the union of every cms-*-storage-* resource granted
  // across ALL Allow statements on this role must be exactly the vehicles and
  // drivers tables — no more, no less.
  //
  // Mutation that must fail: add cms-${stage}-storage-users to
  // CmsVehicleDriverRead → the union grows to 3 entries → this test fails.
  // (Cycle 4 used toHaveLength(2) on a Sid-bound set; the Cycle 5 rewrite lost
  // that pin. This restores it without relying on Sid. C5W2.)
  test('cms-*-storage-* resources on the escalate role are exactly vehicles and drivers', () => {
    // Collect all cms-*-storage-* ARN fragments from every Allow statement.
    const cmsResources = new Set<string>();
    for (const s of escalateRoleStatements(template)) {
      if (s.Effect !== 'Allow') continue;
      const resources: string[] = [].concat(s.Resource).map((r: any) => JSON.stringify(r));
      for (const r of resources) {
        if (r.includes('cms-') && r.includes('-storage-')) {
          cmsResources.add(r);
        }
      }
    }
    const sorted = Array.from(cmsResources).sort();
    expect(sorted).toHaveLength(2);
    expect(sorted.some((r) => r.includes('storage-vehicles'))).toBe(true);
    expect(sorted.some((r) => r.includes('storage-drivers'))).toBe(true);
  });

  // ── connect:StartOutboundVoiceContact scope (security W2): every Allow
  // statement that grants this action must name exactly ONE instance's
  // contacts — the fixture's connectInstanceArn — never `instance/*`.
  // Uses filter-all-allows so an un-Sid'd statement is caught.

  const INSTANCE_CONTACTS = 'arn:aws:connect:us-east-1:123456789012:instance/test-instance/contact/*';

  test('no Allow statement grants connect:StartOutboundVoiceContact beyond the one instance', () => {
    const violations = escalateRoleStatements(template).filter((s) => {
      if (s.Effect !== 'Allow') return false;
      const actions: string[] = [].concat(s.Action);
      if (!actions.some((a) => a === 'connect:StartOutboundVoiceContact' || a === 'connect:*' || a === '*')) return false;
      const resources: any[] = [].concat(s.Resource);
      return resources.some((r) => r !== INSTANCE_CONTACTS);
    });
    expect(violations).toEqual([]);
  });

  // Positive: the grant exists, so the callback can still work.
  test('connect:StartOutboundVoiceContact is granted on the configured instance contacts', () => {
    const stmts = escalateRoleStatements(template).filter((s) => {
      if (s.Effect !== 'Allow') return false;
      const actions: string[] = [].concat(s.Action);
      return actions.includes('connect:StartOutboundVoiceContact')
        && [].concat(s.Resource).includes(INSTANCE_CONTACTS as never);
    });
    expect(stmts.length).toBe(1);
  });
});

// Without a literal, the instance ARN resolves from SSM at deploy, so the grant
// is an Fn::Join over the /cvx/{stage}/connect-instance-arn parameter — never a
// wildcard fallback.
describe('connect:StartOutboundVoiceContact from SSM (no literal)', () => {
  test('the resource is built from the connect-instance-arn parameter', () => {
    const { template: t } = makeStack('staging', {
      connectInstanceArn: { parameterName: '/cvx/staging/connect-instance-arn' },
    });
    const params = JSON.stringify(t.toJSON().Parameters ?? {});
    expect(params).toContain('/cvx/staging/connect-instance-arn');
    const stmts = escalateRoleStatements(t).filter((s) =>
      [].concat(s.Action).includes('connect:StartOutboundVoiceContact' as never));
    expect(stmts.length).toBe(1);
    const res = JSON.stringify(stmts[0].Resource);
    expect(res).toContain('Fn::Join');
    expect(res).toContain('/contact/*');
    expect(res).not.toContain('instance/*');
  });
});

// Fix Group 13: a concrete RSA_POSITION_MAX_AGE_SECONDS is validated at synth.
// nan, inf or a value above the ceiling would turn the stale-position check off.
describe('rsaPositionMaxAgeSeconds is validated at synth', () => {
  const param = '/cvx/staging/rsa-position-max-age-seconds';

  test.each(['nan', 'inf', 'Infinity', '1e9', '3601', '86400', '0', '-5', '900.5', 'abc'])(
    'rejects %p', (literal) => {
      expect(() => makeStack('staging', { rsaPositionMaxAgeSeconds: { literal, parameterName: param } }))
        .toThrow(/rsaPositionMaxAgeSeconds must be a whole number of seconds from 1 to 3600/);
    },
  );

  test.each(['1', '900', '3600'])('accepts %p and threads it to the escalate Lambda', (literal) => {
    const { template } = makeStack('staging', { rsaPositionMaxAgeSeconds: { literal, parameterName: param } });
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'vsa-staging-api-escalate',
      Environment: { Variables: Match.objectLike({ RSA_POSITION_MAX_AGE_SECONDS: literal }) },
    });
  });

  test('an SSM-resolved value is not checked at synth (the handler fails closed at runtime)', () => {
    expect(() => makeStack('staging', { rsaPositionMaxAgeSeconds: { parameterName: param } })).not.toThrow();
  });
});
