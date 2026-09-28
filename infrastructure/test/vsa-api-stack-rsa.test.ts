/**
 * CDK unit tests for Task 2.4: VsaApiStack escalate Lambda RSA wiring.
 *
 * Assertions:
 *   (a) Escalate Lambda has RSA_DISPATCH_TABLE env var set (and no RSA_QUEUE_TABLE).
 *   (b) Escalate Lambda role has a PutItem grant on the RSA dispatch table ARN.
 *   (c) No CMS action-queue ARN appears anywhere in the escalate Lambda's role.
 *   (d) CmsKmsAccessForSseTable grant is still present (handler.py reads CMS_DRIVERS_TABLE).
 *
 * Mutation results:
 *
 * Mutation A — remove RSA_DISPATCH_TABLE env entry:
 *   Procedure: Remove RSA_DISPATCH_TABLE from environment in vsa-api-stack.ts escalate block.
 *   Result: test_escalate_lambda_has_rsa_dispatch_table_env FAILS as expected.
 *   Recorded: <date>
 *
 * Mutation B — point PutItem grant at CMS action-queue ARN:
 *   Procedure: Change RsaDispatchTableWrite resources to the CMS queue ARN.
 *   Result: test_cms_action_queue_arn_absent_from_escalate_role FAILS as expected.
 *   Recorded: <date>
 */

import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { VsaApiStack } from '../lib/vsa-api-stack';

// ─── Stub CMS action-queue ARN ────────────────────────────────────────────────
// This is the ARN the old CmsActionQueueWrite grant used. The test asserts
// this string appears NOWHERE in the escalate role's policy.
const CMS_VFO_QUEUE_ARN_FRAGMENT = 'vfo-action-queue';

// ─── Stack fixture ─────────────────────────────────────────────────────────────

let stackCount = 0;

function makeStack(stage = 'staging'): { stack: VsaApiStack; template: Template } {
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

  const stack = new VsaApiStack(app, `vsa-${stage}-api-rsa-${id}`, {
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
    connectInstanceArn: { literal: 'arn:aws:connect:us-east-1:123456789012:instance/test-instance', parameterName: `/cvx/${stage}/connect-instance-arn` },
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
    rsaPositionMaxAgeSeconds: {
      literal: '900',
      parameterName: `/cvx/${stage}/rsa-position-max-age-seconds`,
    },
  });

  return { stack, template: Template.fromStack(stack) };
}

// ─── (a) RSA_DISPATCH_TABLE env var ──────────────────────────────────────────

describe('Escalate Lambda RSA_DISPATCH_TABLE env var', () => {
  const { template } = makeStack();

  test('escalate Lambda has RSA_DISPATCH_TABLE set', () => {
    const functions = template.findResources('AWS::Lambda::Function', {
      Properties: {
        FunctionName: 'vsa-staging-api-escalate',
      },
    });
    const fnKeys = Object.keys(functions);
    expect(fnKeys.length).toBe(1);
    const envVars = functions[fnKeys[0]].Properties?.Environment?.Variables ?? {};
    expect(envVars).toHaveProperty('RSA_DISPATCH_TABLE');
  });

  test('escalate Lambda does NOT have legacy RSA_QUEUE_TABLE env var', () => {
    const functions = template.findResources('AWS::Lambda::Function', {
      Properties: {
        FunctionName: 'vsa-staging-api-escalate',
      },
    });
    const fnKeys = Object.keys(functions);
    expect(fnKeys.length).toBe(1);
    const envVars = functions[fnKeys[0]].Properties?.Environment?.Variables ?? {};
    expect(envVars).not.toHaveProperty('RSA_QUEUE_TABLE');
  });
});

// ─── Module-level helpers ─────────────────────────────────────────────────────

/**
 * Returns all statements from IAM policies whose logical IDs contain the given
 * fragment (CDK embeds the construct hierarchy in policy names).
 * Used by multiple test sections; defined at module scope so all can access it.
 */
function statementsForPolicy(template: Template, logicalIdFragment: string): Record<string, unknown>[] {
  const stmts: Record<string, unknown>[] = [];
  const policies = template.findResources('AWS::IAM::Policy');
  for (const [logicalId, p] of Object.entries(policies)) {
    if (!logicalId.includes(logicalIdFragment)) continue;
    const pProps = ((p as {Properties?: Record<string, unknown>}).Properties) ?? {};
    const doc = (pProps['PolicyDocument'] as Record<string, unknown>) ?? {};
    const s = (doc['Statement'] as Record<string, unknown>[]) ?? [];
    if (Array.isArray(s)) stmts.push(...s);
  }
  return stmts;
}

// ─── (b) PutItem grant on RSA dispatch table ──────────────────────────────────
//
// F4.7 (review Cycle 4 W5): the assertion must target the ESCALATE Lambda's role
// and the RSA table's ARN — not "a PutItem exists anywhere in the stack".
// There are 7 dynamodb:PutItem statements in the full API template; deleting
// RsaDispatchTableWrite leaves 6 and a presence-anywhere test still passes.
//
// Strategy: find the AWS::IAM::Policy whose PolicyName contains the escalate
// Lambda's logical ID (CDK derives it from the construct id 'ApiEscalate'), then
// assert that policy has a Statement with action=PutItem and resource pointing at
// the RSA table ARN fragment. Cross-stack references resolve to Fn::ImportValue
// tokens in the template, so we match on the fragment 'rsa-dispatch' in the
// serialised resource rather than the full ARN.

describe('Escalate Lambda RSA dispatch table IAM grant', () => {
  const { template } = makeStack();

  test('escalate role has RsaDispatchTableWrite PutItem grant scoped to the RSA table', () => {
    // CDK names the role policy for ApiEscalate with its logical ID fragment.
    // The logical ID is derived from the construct id 'ApiEscalate'.
    const stmts = statementsForPolicy(template, 'ApiEscalate');

    // Confirm we found the escalate policy at all (anti-vacuity: if the
    // policy name changes, the test must fail loudly rather than silently pass
    // on an empty stmts list).
    expect(stmts.length).toBeGreaterThan(0);

    const putItemOnRsaTable = stmts.find(s => {
      const actions = s['Action'];
      const hasPutItem = (
        (typeof actions === 'string' && actions === 'dynamodb:PutItem') ||
        (Array.isArray(actions) && (actions as string[]).includes('dynamodb:PutItem'))
      );
      if (!hasPutItem) return false;
      // Resource resolves to a cross-stack Fn::ImportValue token in the
      // synthesized template. Stringify and check for the RSA table fragment.
      const resStr = JSON.stringify(s['Resource']);
      return resStr.includes('rsa-dispatch');
    });

    expect(putItemOnRsaTable).toBeDefined();
  });

  test('escalate role PutItem grant does not target the old CMS VFO queue', () => {
    const stmts = statementsForPolicy(template, 'ApiEscalate');
    const putItemStmts = stmts.filter(s => {
      const actions = s['Action'];
      if (typeof actions === 'string' && actions === 'dynamodb:PutItem') return true;
      if (Array.isArray(actions) && (actions as string[]).includes('dynamodb:PutItem')) return true;
      return false;
    });
    for (const stmt of putItemStmts) {
      const resStr = JSON.stringify(stmt['Resource']);
      expect(resStr).not.toContain(CMS_VFO_QUEUE_ARN_FRAGMENT);
    }
  });
});

// ─── (c) Old CMS action-queue ARN absent from escalate role ──────────────────

describe('CMS action-queue ARN absent from escalate role', () => {
  const { template } = makeStack();

  test('vfo-action-queue appears nowhere in the escalate Lambda role', () => {
    // Stringify the entire template and check for the CMS queue fragment.
    // This catches hand-built ARN strings that might escape individual
    // statement-level checks.
    const templateJson = JSON.stringify(template.toJSON());
    expect(templateJson).not.toContain(CMS_VFO_QUEUE_ARN_FRAGMENT);
  });
});

// ─── (d) CmsKmsAccessForSseTable grant still present (scoped to escalate role) ─
//
// F5.4 (review Cycle 5 W5-5): the assertion must target the escalate Lambda's
// OWN policy — not "a kms:Decrypt exists anywhere in the API template".
// vsa-api-stack.ts has EIGHT kms:Decrypt grants (seven CmsKmsAccessForSseTable
// sid blocks). Deleting escalate's leaves seven and a stack-wide
// `kmsDecrypt.length >= 1` test still passes — the same presence-not-property
// shape F4.7 fixed for PutItem.
//
// Strategy: use statementsForPolicy(template, 'ApiEscalate') — already defined
// above — to scope the assertion to the escalate role only.
//
// Mutation: delete escalate's CmsKmsAccessForSseTable statement → this test MUST fail.

describe('CmsKmsAccessForSseTable grant on escalate role (F5.4 — scoped)', () => {
  const { template } = makeStack();

  test('kms:Decrypt grant present in escalate role (not just anywhere in the stack)', () => {
    // Scope to ApiEscalate's own policy — not stack-wide getAllIamStatements.
    const stmts = statementsForPolicy(template, 'ApiEscalate');

    // Anti-vacuity: if the policy name changes and we find nothing, fail loudly.
    expect(stmts.length).toBeGreaterThan(0);

    const kmsDecryptInEscalate = stmts.filter(s => {
      const actions = s['Action'];
      if (typeof actions === 'string' && actions === 'kms:Decrypt') return true;
      if (Array.isArray(actions) && (actions as string[]).includes('kms:Decrypt')) return true;
      return false;
    });
    // Mutation guard: delete escalate's kms:Decrypt → this count is 0.
    expect(kmsDecryptInEscalate.length).toBeGreaterThanOrEqual(1);
  });
});

// ─── (d2) RsaDispatchKmsAccess: escalate's grant on the RSA table's own key ──
//
// Fix Group 13 item 2: the dispatch table has its own customer-managed key.
// The PutItem in RsaDispatchTableWrite needs KMS on THAT key; the
// CmsKmsAccessForSseTable grant above names the CMS key and no longer covers
// the RSA table. The (d) test above would still pass with this grant deleted,
// because CmsKmsAccessForSseTable also carries kms:Decrypt. So pin by Sid and
// assert the resource.
//
// Mutations: delete the statement, point it at cmsKmsKeyArn, or widen it to
// '*' → the matching test below fails.

describe('RsaDispatchKmsAccess grant on escalate role (Fix Group 13 item 2)', () => {
  const { template } = makeStack();
  const RSA_KEY_ARN = 'arn:aws:kms:us-west-2:123456789012:key/test-rsa-dispatch-key';

  function stmt(): Record<string, unknown> {
    const stmts = statementsForPolicy(template, 'ApiEscalate');
    expect(stmts.length).toBeGreaterThan(0);
    const s = stmts.find(st => st['Sid'] === 'RsaDispatchKmsAccess');
    expect(s).toBeDefined();
    return s!;
  }

  test('resource is exactly the RSA dispatch table key', () => {
    expect(stmt()['Resource']).toBe(RSA_KEY_ARN);
  });

  test('actions are exactly Decrypt, DescribeKey, GenerateDataKey', () => {
    const actions = [...(stmt()['Action'] as string[])].sort();
    expect(actions).toEqual(['kms:Decrypt', 'kms:DescribeKey', 'kms:GenerateDataKey']);
  });

  test('conditioned on kms:ViaService = dynamodb.<region>.amazonaws.com', () => {
    expect(stmt()['Condition']).toEqual({
      StringEquals: { 'kms:ViaService': 'dynamodb.us-west-2.amazonaws.com' },
    });
  });
});


// ─── (e) F5.D1: live-state invoke wiring (Option D) ──────────────────────────
//
// F5.D1 (review Cycle 5): the escalate Lambda must be able to invoke
// api-vehicle-live-state for position resolution before writing the dispatch row.
// Mirrors api-vehicle-context → api-vehicle-live-state at vsa-api-stack.ts:854-856.
//
// Assertions:
//   1. LIVE_STATE_FN_NAME env var is set on the escalate Lambda.
//   2. RSA_POSITION_MAX_AGE_SECONDS env var is set on the escalate Lambda.
//   3. The escalate role has lambda:InvokeFunction on the live-state function
//      (not Resource: "*" — the grant must be scoped).
//
// Mutation: widen the invoke grant to Resource: "*" → test 3 MUST fail.
// Mutation: remove LIVE_STATE_FN_NAME env entry → test 1 MUST fail.

describe('Escalate Lambda live-state invoke wiring (F5.D1)', () => {
  const { template } = makeStack();

  test('escalate Lambda has LIVE_STATE_FN_NAME env var', () => {
    const functions = template.findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: 'vsa-staging-api-escalate' },
    });
    const fnKeys = Object.keys(functions);
    expect(fnKeys.length).toBe(1);
    const envVars = functions[fnKeys[0]].Properties?.Environment?.Variables ?? {};
    expect(envVars).toHaveProperty('LIVE_STATE_FN_NAME');
  });

  test('escalate Lambda has RSA_POSITION_MAX_AGE_SECONDS env var set to a positive integer', () => {
    const functions = template.findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: 'vsa-staging-api-escalate' },
    });
    const fnKeys = Object.keys(functions);
    expect(fnKeys.length).toBe(1);
    const envVars = functions[fnKeys[0]].Properties?.Environment?.Variables ?? {};
    // F7.4: assert presence AND that the value is a positive integer string.
    // `toHaveProperty` alone passes for '0', '-1', or 'abc' — all of which make
    // F6.3's freshness gate refuse every dispatch fleet-wide.
    const rawValue = envVars['RSA_POSITION_MAX_AGE_SECONDS'];
    expect(rawValue).toBeDefined();
    // Value is a string in the CFN template (Lambda env vars are always strings).
    // parseInt with radix 10, then assert > 0.
    const parsed = parseInt(String(rawValue), 10);
    expect(Number.isInteger(parsed) && parsed > 0).toBe(true);
  });

  test('escalate role has lambda:InvokeFunction grant scoped to live-state function (not wildcard)', () => {
    // The grant must be scoped to the live-state function ARN, not Resource: "*".
    // CDK's grantInvoke emits an Allow statement with action lambda:InvokeFunction
    // and Resource pointing to the function's ARN. We check:
    //   (a) the statement exists in escalate's own policy (anti-presence-anywhere)
    //   (b) the Resource is NOT "*"
    const stmts = statementsForPolicy(template, 'ApiEscalate');
    expect(stmts.length).toBeGreaterThan(0);

    const invokeStmts = stmts.filter(s => {
      const actions = s['Action'];
      if (typeof actions === 'string' && actions === 'lambda:InvokeFunction') return true;
      if (Array.isArray(actions) && (actions as string[]).includes('lambda:InvokeFunction')) return true;
      return false;
    });
    // Mutation guard: if the grant is absent, this fails.
    expect(invokeStmts.length).toBeGreaterThanOrEqual(1);

    for (const stmt of invokeStmts) {
      const res = stmt['Resource'];
      // Must NOT be a bare wildcard.
      expect(res).not.toBe('*');
      if (Array.isArray(res)) {
        expect((res as string[]).includes('*')).toBe(false);
      }
      // The resource must reference the live-state function (logical ID fragment).
      // CDK synthesises lambda:InvokeFunction as Fn::GetAtt on the function's
      // logical ID (e.g. "ApiVehicleLiveState425416A0") — the function name string
      // does not appear in the synthesised resource. Match on the construct ID.
      const resStr = JSON.stringify(res);
      expect(resStr).toContain('ApiVehicleLiveState');
    }
  });
});
