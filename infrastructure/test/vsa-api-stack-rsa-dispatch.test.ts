/**
 * CDK unit tests for Task 3.1: VsaApiStack api-rsa-dispatch Lambda wiring.
 *
 * Spec: .kiro/specs/2026-09-23-cvx-rsa-stub-dispatch/ Task 3.1
 * Decisions G3-D8 (allowlist), G3-D9 (tenant isolation), G3-D10 (KMS).
 *
 * Assertions:
 *   (a) ApiRsaDispatch Lambda exists with RSA_DISPATCH_TABLE env var set.
 *   (b) ApiRsaDispatch Lambda has VSA_RSA_CLIENT=fake (G3-D7).
 *   (c) ApiRsaDispatch role has GetItem (exactly) on the RSA dispatch table ARN.
 *       RSA_DISPATCH_TABLE env var references the same table as the grant.
 *       No Scan, no Query on the same statement (G3-D10).
 *   (d) ApiRsaDispatch role has kms:Decrypt + kms:DescribeKey on the RSA table key,
 *       exact resource ARN, exact condition kms:ViaService = dynamodb.<region>.amazonaws.com
 *       (G3-D10, mutation-verified).
 *   (e) Exactly one GET on /rsa/dispatch/{dispatchId} wired to ApiRsaDispatch,
 *       COGNITO_USER_POOLS, same AuthorizerId as /escalate POST (F10.1, mutation-verified).
 *       Resource path: rsa → dispatch → {dispatchId}.
 *
 * F10.1 mutations (authorized GET route):
 *   C1 - authorizationType: NONE on the route
 *     → test 'GET route has COGNITO_USER_POOLS auth targeting ApiRsaDispatch' FAILS
 *   C2 - delete addMethod entirely
 *     → same test FAILS (0 matching methods)
 *   C3 - integration targets a different Lambda
 *     → same test FAILS (0 methods with ApiRsaDispatch integration)
 *
 * F10.3 mutations (property-level IAM checks):
 *   C3k - KMS Resource: '*' → 'RSA dispatch read grant has exactly kms:Decrypt+kms:DescribeKey…' FAILS
 *   C4  - ViaService StringLike 'dynamodb.*' → exact condition test FAILS
 *   C5  - RSA_DISPATCH_TABLE env points at different table → table-equality test FAILS
 *   C6  - dynamodb:Scan added to read statement → exact-action test FAILS
 *
 * Earlier mutations:
 *   (c) widen GetItem Resource to "*" → 'GetItem resource is exact table ARN' FAILS
 *   (d) drop KMS statement → 'kms:Decrypt in own policy' FAILS
 *
 * F9.4 mutation:
 *   Remove ALLOWED_ORIGIN from ApiRsaDispatch env → ALLOWED_ORIGIN test FAILS
 *
 * F12.1 mutations (role-level bound, shared collector in helpers/iam-surface.ts):
 *   K1-K5, K7 (managed policies, Action "*", wildcard / other-table GetItem,
 *   unconditioned kms:Decrypt on "*") → the (F11.2, F12.1) block FAILS
 */

import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { VsaApiStack } from '../lib/vsa-api-stack';
import {
  LAMBDA_BASIC_EXECUTION_ROLE_ARN,
  actionsOf,
  malformedForBound,
  resourcesOf,
  roleIdOfFunction,
  roleManagedPolicyArns,
  roleStatements,
} from './helpers/iam-surface';

// ─── Fixture constants ────────────────────────────────────────────────────────
const FIXTURE_TABLE_ARN = 'arn:aws:dynamodb:us-west-2:123456789012:table/vsa-staging-rsa-dispatch';
const FIXTURE_TABLE_NAME = 'vsa-staging-rsa-dispatch';
const FIXTURE_CMK_ARN    = 'arn:aws:kms:us-west-2:123456789012:key/test-cms-key';
// Fix Group 13 item 2: the RSA table's own key, distinct from the CMS key so a
// grant that still names the CMS key is caught.
const FIXTURE_RSA_KEY_ARN = 'arn:aws:kms:us-west-2:123456789012:key/test-rsa-dispatch-key';
const FIXTURE_REGION     = 'us-west-2';

// ─── Stack fixture ────────────────────────────────────────────────────────────

let stackCount = 0;

function makeStack(stage = 'staging'): { stack: VsaApiStack; template: Template } {
  const id = ++stackCount;
  const app = new cdk.App();
  const env = { account: '123456789012', region: FIXTURE_REGION };

  const parentStack = new cdk.Stack(app, `ParentFixtureRsaDispatch${id}`, { env });

  const stubTable = (name: string) =>
    dynamodb.Table.fromTableName(parentStack, `Tbl${id}-${name}`, name);

  const stubFn = (fnName: string, arn: string) =>
    lambda.Function.fromFunctionArn(parentStack, `Fn${id}-${fnName}`, arn);

  const rsaDispatchTable = dynamodb.Table.fromTableArn(
    parentStack,
    `RsaDispatch${id}`,
    FIXTURE_TABLE_ARN,
  );
  const rsaDispatchKey = kms.Key.fromKeyArn(parentStack, `RsaDispatchKey${id}`, FIXTURE_RSA_KEY_ARN);

  const stack = new VsaApiStack(app, `vsa-${stage}-api-rsa-dispatch-${id}`, {
    env,
    stage,
    cmsStage: stage,
    tenantConfigTable: stubTable(`cms-${stage}-tenant-config`),
    classifier: stubFn(
      'Classifier',
      `arn:aws:lambda:us-west-2:123456789012:function:vsa-${stage}-triage-classifier`,
    ),
    cmsKmsKeyArn: {
      literal: FIXTURE_CMK_ARN,
      parameterName: `/cvx/${stage}/cms-kms-key-arn`,
    },
    redisEndpoint: {
      literal: 'redis://stub.redis.local:6379',
      parameterName: `/cvx/${stage}/redis-endpoint`,
    },
    connectInstanceArn: { literal: 'arn:aws:connect:us-east-1:123456789012:instance/test-instance', parameterName: `/cvx/${stage}/connect-instance-arn` },
    dmsApiEndpoint: {
      literal: 'https://dms.example.invalid/staging',
      parameterName: `/cvx/${stage}/dms-api-endpoint`,
    },
    vpcId: 'vpc-00000000000000000',
    redisSecurityGroupId: 'sg-00000000000000000',
    assistantChatRuntimeArn: {
      literal: `arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/stub-${stage}`,
      parameterName: `/cvx/${stage}/assistant-chat-runtime-arn`,
    },
    assistantChatRuntimeRegion: 'us-west-2',
    allowedOrigin: {
      literal: 'https://stub.example.com',
      parameterName: `/cvx/${stage}/allowed-origin`,
    },
    acquireReservationHandoffFn: stubFn(
      'AcquireHandoff',
      `arn:aws:lambda:us-west-2:123456789012:function:vsa-${stage}-acquire-handoff`,
    ),
    acquireReservationHandoffFnArnExportName: `vsa-${stage}-acquire-reservation-handoff-fn-arn`,
    acquireCatalogTable: stubTable(`vsa-${stage}-acquire-catalog`),
    acquireOrdersTable: stubTable(`vsa-${stage}-acquire-orders`),
    acquireLeadsTable: stubTable(`vsa-${stage}-acquire-leads`),
    avxFindingsTableNameExportName: `vsa-${stage}-avx-findings-table-name`,
    avxActionsTableNameExportName: `vsa-${stage}-avx-actions-table-name`,
    avxFeedbackTableNameExportName: `vsa-${stage}-avx-feedback-table-name`,
    notificationDevicesTableNameExportName: `vsa-${stage}-notification-devices-table-name`,
    rsaDispatchTable,
    rsaDispatchKey,
    rsaPositionMaxAgeSeconds: {
      literal: '900',
      parameterName: `/cvx/${stage}/rsa-position-max-age-seconds`,
    },
  });

  return { stack, template: Template.fromStack(stack) };
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

/** IAM statements from the policy whose logical ID contains `logicalIdFragment`. */
function statementsForPolicy(
  template: Template,
  logicalIdFragment: string,
): Record<string, unknown>[] {
  const stmts: Record<string, unknown>[] = [];
  const policies = template.findResources('AWS::IAM::Policy');
  for (const [logicalId, p] of Object.entries(policies)) {
    if (!logicalId.includes(logicalIdFragment)) continue;
    const pProps = ((p as { Properties?: Record<string, unknown> }).Properties) ?? {};
    const doc = (pProps['PolicyDocument'] as Record<string, unknown>) ?? {};
    const s = (doc['Statement'] as Record<string, unknown>[]) ?? [];
    if (Array.isArray(s)) stmts.push(...s);
  }
  return stmts;
}

/** Find the logical ID of the Lambda named `functionName`. */
function lambdaLogicalId(template: Template, functionName: string): string {
  const fns = template.findResources('AWS::Lambda::Function', {
    Properties: { FunctionName: functionName },
  });
  const keys = Object.keys(fns);
  expect(keys.length).toBe(1);
  return keys[0];
}

/** All API Gateway methods whose Integration.Uri resolves to `fnLogicalId`. */
function methodsForLambda(
  template: Template,
  fnLogicalId: string,
): Array<{ logicalId: string; props: Record<string, unknown> }> {
  const result: Array<{ logicalId: string; props: Record<string, unknown> }> = [];
  const methods = template.findResources('AWS::ApiGateway::Method');
  for (const [logicalId, m] of Object.entries(methods)) {
    const uri = JSON.stringify((m as Record<string, unknown>)['Properties'] ?? '');
    if (uri.includes(fnLogicalId)) {
      result.push({
        logicalId,
        props: ((m as Record<string, unknown>)['Properties'] as Record<string, unknown>) ?? {},
      });
    }
  }
  return result;
}

/** Resolve a CFN Ref to the logical ID it names. */
function refOf(val: unknown): string | null {
  if (val && typeof val === 'object' && 'Ref' in (val as object)) {
    return (val as { Ref: string }).Ref;
  }
  return null;
}

// ─── (a) Lambda env: RSA_DISPATCH_TABLE ───────────────────────────────────────

describe('ApiRsaDispatch Lambda environment', () => {
  const { template } = makeStack();

  test('api-rsa-dispatch Lambda exists with RSA_DISPATCH_TABLE env var', () => {
    const fns = template.findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: 'vsa-staging-api-rsa-dispatch' },
    });
    const keys = Object.keys(fns);
    expect(keys.length).toBe(1);
    const envVars = fns[keys[0]].Properties?.Environment?.Variables ?? {};
    expect(envVars).toHaveProperty('RSA_DISPATCH_TABLE');
  });
});

// ─── (b) VSA_RSA_CLIENT=fake (G3-D7) ──────────────────────────────────────────

describe('ApiRsaDispatch Lambda VSA_RSA_CLIENT env var (G3-D7)', () => {
  const { template } = makeStack();

  test('VSA_RSA_CLIENT is set to "fake" so simulated-fallback leans toward true', () => {
    const fns = template.findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: 'vsa-staging-api-rsa-dispatch' },
    });
    const keys = Object.keys(fns);
    expect(keys.length).toBe(1);
    const envVars = fns[keys[0]].Properties?.Environment?.Variables ?? {};
    expect(envVars['VSA_RSA_CLIENT']).toBe('fake');
  });
});

// ─── (c) GetItem scoped to exact table ARN — NOT wildcard, NOT Scan/Query (G3-D10) ───

describe('ApiRsaDispatch Lambda GetItem grant (G3-D10)', () => {
  const { template } = makeStack();

  /** Find the RsaDispatchTableRead statement by Sid. */
  function readStmt(): Record<string, unknown> {
    const stmts = statementsForPolicy(template, 'ApiRsaDispatch');
    const s = stmts.find(st => st['Sid'] === 'RsaDispatchTableRead');
    expect(s).toBeDefined();
    return s!;
  }

  // MUTATION-VERIFIED (c): widen GetItem Resource to "*" → FAILS
  test('GetItem resource is the exact fixture table ARN, not a wildcard', () => {
    const s = readStmt();
    // Resource must equal the fixture table ARN exactly.
    expect(s['Resource']).toBe(FIXTURE_TABLE_ARN);
  });

  // MUTATION-VERIFIED (C6): add dynamodb:Scan to the read statement → FAILS (F10.3)
  test('Read statement action is exactly dynamodb:GetItem — no Scan or Query (G3-D10)', () => {
    const s = readStmt();
    // Action is a single string when CDK emits exactly one action.
    expect(s['Action']).toBe('dynamodb:GetItem');
  });

  // MUTATION-VERIFIED (C5): RSA_DISPATCH_TABLE points at different table → FAILS (F10.3)
  test('RSA_DISPATCH_TABLE env var references the same table as the GetItem grant', () => {
    const fns = template.findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: 'vsa-staging-api-rsa-dispatch' },
    });
    const keys = Object.keys(fns);
    expect(keys.length).toBe(1);
    const envVars = fns[keys[0]].Properties?.Environment?.Variables ?? {};
    // The env var resolves to the fixture table name.
    // The GetItem grant's Resource resolves to the fixture table ARN.
    // Both must name the same table.
    const envTableName = envVars['RSA_DISPATCH_TABLE'] as string;
    expect(envTableName).toBe(FIXTURE_TABLE_NAME);
    // And the grant's ARN must contain the same table name.
    const s = readStmt();
    expect((s['Resource'] as string)).toContain(FIXTURE_TABLE_NAME);
  });

  test('rsa-dispatch Lambda does NOT have PutItem (read-only, spec R1)', () => {
    const stmts = statementsForPolicy(template, 'ApiRsaDispatch');
    const putItemStmts = stmts.filter(s => {
      const a = s['Action'];
      return (
        (typeof a === 'string' && a === 'dynamodb:PutItem') ||
        (Array.isArray(a) && (a as string[]).includes('dynamodb:PutItem'))
      );
    });
    expect(putItemStmts.length).toBe(0);
  });
});

// ─── (d) KMS grant: exact resource, exact actions, exact condition (G3-D10) ─────

describe('ApiRsaDispatch Lambda KMS grant (G3-D10)', () => {
  const { template } = makeStack();

  /** Find the RsaDispatchKmsDecrypt statement by Sid. */
  function kmsStmt(): Record<string, unknown> {
    const stmts = statementsForPolicy(template, 'ApiRsaDispatch');
    const s = stmts.find(st => st['Sid'] === 'RsaDispatchKmsDecrypt');
    expect(s).toBeDefined();
    return s!;
  }

  // MUTATION-VERIFIED (d): drop RsaDispatchKmsDecrypt statement → FAILS
  test('rsa-dispatch Lambda role has RsaDispatchKmsDecrypt in its own policy', () => {
    // Anti-vacuity: if the Sid is removed or renamed this also fails.
    expect(kmsStmt()).toBeDefined();
  });

  // MUTATION-VERIFIED (C3k): KMS Resource: '*' → FAILS (F10.3)
  // Fix Group 13 item 2: the resource is the RSA table's own key. Mutation:
  // point the grant back at cmsKmsKeyArn → FAILS here and in the next test.
  test('KMS grant resource is the RSA dispatch table key ARN, not a wildcard', () => {
    const s = kmsStmt();
    expect(s['Resource']).toBe(FIXTURE_RSA_KEY_ARN);
  });

  test('KMS grant does not name the CMS key (this Lambda reads no CMS table)', () => {
    const s = kmsStmt();
    expect(JSON.stringify(s['Resource'])).not.toContain(FIXTURE_CMK_ARN);
  });

  test('KMS grant action is exactly [kms:Decrypt, kms:DescribeKey]', () => {
    const s = kmsStmt();
    const actions = s['Action'] as string[];
    expect(Array.isArray(actions)).toBe(true);
    expect(actions.sort()).toEqual(['kms:Decrypt', 'kms:DescribeKey'].sort());
  });

  // MUTATION-VERIFIED (C4): StringLike with dynamodb.* instead of exact value → FAILS (F10.3)
  test('KMS grant condition is StringEquals kms:ViaService = dynamodb.<region>.amazonaws.com', () => {
    const s = kmsStmt();
    expect(s['Condition']).toEqual({
      StringEquals: {
        'kms:ViaService': `dynamodb.${FIXTURE_REGION}.amazonaws.com`,
      },
    });
  });
});

// ─── (e) GET /rsa/dispatch/{dispatchId} — authorized, targeting ApiRsaDispatch (F10.1) ───
//
// C1: authorizationType: NONE → method still exists but has wrong auth → test fails
// C2: delete addMethod entirely → no matching methods found → test fails
// C3: integration targets another Lambda → no methods with ApiRsaDispatch URI → test fails

describe('GET /rsa/dispatch/{dispatchId} route (F10.1)', () => {
  const { template } = makeStack();

  test('GET route has COGNITO_USER_POOLS auth targeting ApiRsaDispatch (mutations C1/C2/C3)', () => {
    const rsaDispatchFnId = lambdaLogicalId(template, 'vsa-staging-api-rsa-dispatch');

    // Find all methods whose Integration.Uri includes the ApiRsaDispatch logical ID.
    const matchingMethods = methodsForLambda(template, rsaDispatchFnId);

    // Exactly one method targets this Lambda (mutation C2: 0 methods if deleted;
    // mutation C3: 0 methods with ApiRsaDispatch URI if integration points elsewhere).
    expect(matchingMethods.length).toBe(1);

    const props = matchingMethods[0].props;

    // HttpMethod must be GET.
    expect(props['HttpMethod']).toBe('GET');

    // Auth must be COGNITO_USER_POOLS (mutation C1: NONE would fail here).
    expect(props['AuthorizationType']).toBe('COGNITO_USER_POOLS');

    // AuthorizerId must be the same Ref as /escalate's POST, i.e. the only
    // authorizer in the stack (Authz32705C97 under this fixture).
    const escapedPostId = lambdaLogicalId(template, 'vsa-staging-api-escalate');
    const escapeMethods = methodsForLambda(template, escapedPostId);
    // There is exactly one escalate method and it also uses the Cognito authorizer.
    const escalateAuthorizerId = refOf(
      escapeMethods.find(m => m.props['HttpMethod'] === 'POST')?.props['AuthorizerId']
    );
    const rsaAuthorizerId = refOf(props['AuthorizerId']);
    expect(rsaAuthorizerId).not.toBeNull();
    expect(rsaAuthorizerId).toBe(escalateAuthorizerId);
  });

  test('GET route is wired to the rsa → dispatch → {dispatchId} resource path', () => {
    const rsaDispatchFnId = lambdaLogicalId(template, 'vsa-staging-api-rsa-dispatch');
    const matchingMethods = methodsForLambda(template, rsaDispatchFnId);
    expect(matchingMethods.length).toBe(1);

    const resourceId = refOf(matchingMethods[0].props['ResourceId']);
    expect(resourceId).not.toBeNull();

    const allResources = template.findResources('AWS::ApiGateway::Resource');

    // The method's resource must have PathPart = '{dispatchId}'.
    const dispatchIdResource = allResources[resourceId!];
    expect(dispatchIdResource).toBeDefined();
    expect(dispatchIdResource.Properties?.PathPart).toBe('{dispatchId}');

    // Its parent must have PathPart = 'dispatch'.
    const dispatchParentId = refOf(dispatchIdResource.Properties?.ParentId);
    expect(dispatchParentId).not.toBeNull();
    const dispatchResource = allResources[dispatchParentId!];
    expect(dispatchResource).toBeDefined();
    expect(dispatchResource.Properties?.PathPart).toBe('dispatch');

    // Dispatch's parent must have PathPart = 'rsa'.
    const rsaParentId = refOf(dispatchResource.Properties?.ParentId);
    expect(rsaParentId).not.toBeNull();
    const rsaResource = allResources[rsaParentId!];
    expect(rsaResource).toBeDefined();
    expect(rsaResource.Properties?.PathPart).toBe('rsa');
  });
});

// ─── (f) CORS: ALLOWED_ORIGIN env var (F9.4) ─────────────────────────────────

describe('ApiRsaDispatch Lambda — ALLOWED_ORIGIN env var (F9.4)', () => {
  const { template } = makeStack();

  test('api-rsa-dispatch Lambda has ALLOWED_ORIGIN env var', () => {
    const fns = template.findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: 'vsa-staging-api-rsa-dispatch' },
    });
    const keys = Object.keys(fns);
    expect(keys.length).toBe(1);
    const envVars = fns[keys[0]].Properties?.Environment?.Variables ?? {};
    expect(envVars).toHaveProperty('ALLOWED_ORIGIN');
    expect(envVars['ALLOWED_ORIGIN']).toBeTruthy();
  });
});


// ─── (F11.2, F12.1) Role-level bound on the whole permission surface ────────
//
// The earlier tests pin the two NAMED statements. They do not bound the role:
// an extra `grantReadData`, a Scan/Query statement, or `dynamodb:*` on `*`
// left all tests green (review Cycle 2, 3.1 W1). F11.2 added role-level tests,
// resolving the Lambda's actual role regardless of statement Sid or policy
// logical ID. Mutations (Fix Group 11): `grantReadData`; `dynamodb:*` on `*`.
//
// F12.1 (review Cycle 3, 3.1 W): the F11.2 collector read `AWS::IAM::Policy`
// documents and inline Policies only, and its union counted only actions that
// start with `dynamodb:`. These survived at 15/15:
//   K1 role.addManagedPolicy(AmazonDynamoDBReadOnlyAccess)
//   K2 new iam.ManagedPolicy(..., { roles: [role], statements: [dynamodb:* on *] })
//   K3 separate statement `Action: "*"` on `Resource: "*"`
//   K4 separate statement GetItem on `table/*`
//   K5 separate statement GetItem on tenantConfigTable
//   K7 separate statement kms:Decrypt on `*`, no ViaService condition
// The shared collector (test/helpers/iam-surface.ts) reads every channel the
// template shows, and the ManagedPolicyArns list is pinned for the one it
// cannot. With the Sid-pinned tests in (c) and (d), which pin both statements
// exactly, these pin the role's whole permission surface.
// Mutations K1-K7 → each MUST fail (recorded in decisions-g3-3.1.md, Fix Group 12).

describe('ApiRsaDispatch role — whole permission surface is bounded (F11.2, F12.1)', () => {
  const { template } = makeStack();
  const roleId = roleIdOfFunction(template, 'vsa-staging-api-rsa-dispatch');
  const stmts = roleStatements(template, roleId);

  test('the role has exactly two statements, each a plain Allow with an Action list', () => {
    expect(stmts.length).toBe(2);
    for (const s of stmts) {
      expect(malformedForBound(s)).toEqual([]);
    }
  });

  test('the union of every action on the role is exactly GetItem + Decrypt + DescribeKey', () => {
    const union = new Set<string>();
    for (const s of stmts) for (const a of actionsOf(s)) union.add(a);
    expect([...union].sort()).toEqual(['dynamodb:GetItem', 'kms:Decrypt', 'kms:DescribeKey']);
  });

  test('no statement on the role has a wildcard anywhere in its Resource', () => {
    for (const s of stmts) {
      for (const r of resourcesOf(s)) {
        expect(JSON.stringify(r)).not.toContain('*');
      }
    }
  });

  test('the role carries exactly one managed policy ARN, AWSLambdaBasicExecutionRole', () => {
    expect(roleManagedPolicyArns(template, roleId)).toEqual([LAMBDA_BASIC_EXECUTION_ROLE_ARN]);
  });
});
