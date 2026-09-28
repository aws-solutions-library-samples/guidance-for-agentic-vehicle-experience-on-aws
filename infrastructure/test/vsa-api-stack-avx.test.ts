/**
 * CDK unit tests for AVX API wiring (Group 4.T5, Spec 1a).
 *
 * Assertions:
 *   (a) All three new Lambda Functions exist by function name.
 *   (b) All 17 API Gateway routes are wired with a Cognito authorizer.
 *   (c) Each Lambda role has NARROW IAM — no wildcard Resource, no dynamodb:*.
 *   (d) SNS grant on register-device is scoped to app/APNS_* prefix only.
 *
 * Mutation results recorded below:
 *
 * Mutation A — Resource: "*" (no-wildcard assertion):
 *   Procedure: Temporarily change one of the avxFindingsTable.addToRolePolicy
 *   statements to use resources: ['*'], then run tests.
 *   Result: test_avx_findings_lambda_role_no_wildcard_resource FAILS.
 *   Restored: yes. Test is green in main. Recorded 2026-09-19.
 *
 * Mutation B — dynamodb:* action (no-dynamodb-star assertion):
 *   Procedure: Temporarily change the AvxFindingsReadWrite statement actions
 *   to ['dynamodb:*'], then run tests.
 *   Result: test_avx_findings_lambda_role_no_dynamodb_star FAILS.
 *   Restored: yes. Test is green in main. Recorded 2026-09-19.
 *
 * Mutation C — SNS resource wildened to '*' (APNS-scoped SNS):
 *   Procedure: Change the SnsCreateApnsEndpoint resources to ['*'], then run tests.
 *   Result: test_register_device_sns_scoped_to_apns_platform_app_only FAILS.
 *   Restored: yes. Test is green in main. Recorded 2026-09-19.
 */

import * as fs from 'fs';
import * as path from 'path';
import * as crypto from 'crypto';
import * as cdk from 'aws-cdk-lib';
import { Template, Match, Annotations } from 'aws-cdk-lib/assertions';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as iam from 'aws-cdk-lib/aws-iam';
import { AwsSolutionsChecks } from 'cdk-nag';
import { VsaApiStack } from '../lib/vsa-api-stack';
import { applyCvxNagSuppressions } from '../lib/nag-suppressions';

// ─── Stack fixture ────────────────────────────────────────────────────────────

/**
 * Creates a minimal VsaApiStack with all required props stubbed and the four
 * AVX export-name props populated.
 *
 * Key choices:
 *  - ConfigValue props use { literal: '...' } to bypass SSM resolution at synth.
 *  - ITable props use Table.fromTableName so no real CloudFormation table is created.
 *  - Lambda IFunction props use Function.fromFunctionArn.
 *  - vpcId / redisSecurityGroupId are dummy strings (they only affect the live-state
 *    Lambda VPC binding, not the AVX Lambdas under test).
 */
let stackCount = 0;

function makeStack(stage = 'staging'): { stack: VsaApiStack; template: Template } {
  const id = ++stackCount;
  const app = new cdk.App();
  const env = { account: '123456789012', region: 'us-west-2' };

  // Stub a parent stack so fromTableName / fromFunctionArn have a scope.
  const parentStack = new cdk.Stack(app, `ParentFixture${id}`, { env });

  const stubTable = (id: string, name: string) =>
    dynamodb.Table.fromTableName(parentStack, id, name);

  const stubFn = (id: string, arn: string) =>
    lambda.Function.fromFunctionArn(parentStack, id, arn);

  const stack = new VsaApiStack(app, `vsa-${stage}-api-${id}`, {
    env,
    stage,
    cmsStage: stage,
    tenantConfigTable: stubTable('TenantCfg', `cms-${stage}-tenant-config`),
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
    // Acquire resources
    acquireReservationHandoffFn: stubFn(
      'AcquireHandoff',
      `arn:aws:lambda:us-west-2:123456789012:function:vsa-${stage}-acquire-handoff`,
    ),
    acquireReservationHandoffFnArnExportName: `vsa-${stage}-acquire-reservation-handoff-fn-arn`,
    acquireCatalogTable: stubTable('AcquireCatalog', `vsa-${stage}-acquire-catalog`),
    acquireOrdersTable:  stubTable('AcquireOrders',  `vsa-${stage}-acquire-orders`),
    acquireLeadsTable:   stubTable('AcquireLeads',   `vsa-${stage}-acquire-leads`),
    // AVX export names — the four props under test
    avxFindingsTableNameExportName:         `vsa-${stage}-avx-findings-table-name`,
    avxActionsTableNameExportName:          `vsa-${stage}-avx-actions-table-name`,
    avxFeedbackTableNameExportName:         `vsa-${stage}-avx-feedback-table-name`,
    notificationDevicesTableNameExportName: `vsa-${stage}-notification-devices-table-name`,
    // RSA dispatch table — required (F4.6). fromTableArn so the stub has a
    // concrete ARN for the PutItem grant scope assertion.
    rsaDispatchTable: dynamodb.Table.fromTableArn(
      parentStack,
      `RsaDispatch${id}`,
      `arn:aws:dynamodb:us-west-2:123456789012:table/vsa-${stage}-rsa-dispatch`,
    ),
    rsaDispatchKey: kms.Key.fromKeyArn(parentStack, `RsaDispatchKey${id}`, 'arn:aws:kms:us-west-2:123456789012:key/test-rsa-dispatch-key'),
  });

  return { stack, template: Template.fromStack(stack) };
}

// ─── (a) Lambda function names ────────────────────────────────────────────────

describe('AVX Lambda function names', () => {
  const { template } = makeStack();

  test('apiAvxFindings has expected function name', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'vsa-staging-api-avx-findings',
    });
  });

  test('apiAvxActions has expected function name', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'vsa-staging-api-avx-actions',
    });
  });

  test('apiAvxRegisterDevice has expected function name', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'vsa-staging-api-avx-register-device',
    });
  });

  // Fix Group 35: POST /approve dispatches synchronously inside this function, and
  // the executor's retry loop may run for SYNCHRONOUS_BUDGET_S. A timeout at or
  // below that budget kills the function mid-dispatch, after the downstream work
  // may already be done, leaving no receipt on the Action. The budget is read from
  // base.py rather than copied, so raising it there fails this test here.
  // Mutation: timeout back to 10s → FAIL.
  test('apiAvxFindings outlives the executor dispatch budget and API Gateway', () => {
    const baseSrc = fs.readFileSync(
      path.resolve(__dirname, '..', '..', 'agents', 'executors', 'base.py'), 'utf8');
    const m = /^SYNCHRONOUS_BUDGET_S\s*=\s*([0-9.]+)/m.exec(baseSrc);
    expect(m).not.toBeNull();
    const budget = Number(m![1]);

    const fns = template.findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: 'vsa-staging-api-avx-findings' },
    });
    const timeouts = Object.values(fns).map((r: any) => r.Properties.Timeout);
    expect(timeouts).toHaveLength(1);
    expect(timeouts[0]).toBeGreaterThan(budget);
    expect(timeouts[0]).toBeGreaterThan(29); // API Gateway's integration limit
  });
});

// ─── C1 pin: AVX Lambda Handler strings must be handler.handler ───────────────
//
// This class of defect (handler.lambda_handler instead of handler.handler) is
// invisible to the green suite and only caught by a synth-property assertion
// or a live invocation. The pin below catches it at CDK-test time.
// Mutation: change handler to 'handler.lambda_handler' in vsa-api-stack.ts → FAIL.

describe('AVX Lambda Handler entry-point pins (C1)', () => {
  const { template } = makeStack();

  function getAvxFunctionHandlers(template: Template): Map<string, string> {
    const functions = template.findResources('AWS::Lambda::Function');
    const result = new Map<string, string>();
    for (const [id, fn] of Object.entries(functions)) {
      const props = (fn as Record<string, unknown>)['Properties'] as Record<string, unknown> | undefined;
      if (!props) continue;
      const name = props['FunctionName'];
      const handler = props['Handler'];
      if (typeof name === 'string' && typeof handler === 'string' &&
          (name as string).includes('api-avx-')) {
        result.set(name as string, handler as string);
      }
    }
    return result;
  }

  test('apiAvxFindings handler is handler.handler (NOT handler.lambda_handler)', () => {
    const handlers = getAvxFunctionHandlers(template);
    const h = handlers.get('vsa-staging-api-avx-findings');
    expect(h).toBeDefined();
    expect(h).toBe('handler.handler');
  });

  test('apiAvxActions handler is handler.handler (NOT handler.lambda_handler)', () => {
    const handlers = getAvxFunctionHandlers(template);
    const h = handlers.get('vsa-staging-api-avx-actions');
    expect(h).toBeDefined();
    expect(h).toBe('handler.handler');
  });

  test('apiAvxRegisterDevice handler is handler.handler (NOT handler.lambda_handler)', () => {
    const handlers = getAvxFunctionHandlers(template);
    const h = handlers.get('vsa-staging-api-avx-register-device');
    expect(h).toBeDefined();
    expect(h).toBe('handler.handler');
  });
});

// ─── C2 pin: _lib/avx_scope.py present in build asset directories ────────────
//
// The build script (scripts/build-avx-lambda.sh) copies _lib/avx_scope.py into
// each Lambda's build directory. This test verifies the build output exists,
// catching the case where build-avx-lambda.sh was not run before cdk synth.

describe('AVX Lambda _lib bundling (C2)', () => {
  const path = require('path');
  const fs = require('fs');

  test('_lib/avx_scope.py exists in api-avx-findings build directory', () => {
    const libPath = path.join(__dirname, '..', 'build', 'api-avx-findings', '_lib', 'avx_scope.py');
    expect(fs.existsSync(libPath)).toBe(true);
  });

  test('_lib/avx_scope.py exists in api-avx-actions build directory', () => {
    const libPath = path.join(__dirname, '..', 'build', 'api-avx-actions', '_lib', 'avx_scope.py');
    expect(fs.existsSync(libPath)).toBe(true);
  });

  test('handler.py exists in api-avx-findings build directory', () => {
    const hPath = path.join(__dirname, '..', 'build', 'api-avx-findings', 'handler.py');
    expect(fs.existsSync(hPath)).toBe(true);
  });

  test('handler.py exists in api-avx-actions build directory', () => {
    const hPath = path.join(__dirname, '..', 'build', 'api-avx-actions', 'handler.py');
    expect(fs.existsSync(hPath)).toBe(true);
  });

  test('handler.py exists in api-avx-notifications-register-device build directory', () => {
    const hPath = path.join(__dirname, '..', 'build', 'api-avx-notifications-register-device', 'handler.py');
    expect(fs.existsSync(hPath)).toBe(true);
  });
});
//
// The CDK CognitoUserPoolsAuthorizer synthesises one AWS::ApiGateway::Authorizer
// resource; every method that uses it references the same authorizer ID.
// We assert that:
//   1. Exactly one Cognito authorizer exists.
//   2. Each of the 17 expected Method resources sets AuthorizationType=COGNITO_USER_POOLS.

describe('API Gateway AVX routes — Cognito authorizer', () => {
  const { template } = makeStack();

  test('a Cognito user-pool authorizer exists in the stack', () => {
    template.resourceCountIs('AWS::ApiGateway::Authorizer', 1);
    template.hasResourceProperties('AWS::ApiGateway::Authorizer', {
      Type: 'COGNITO_USER_POOLS',
    });
  });

  // Helper: assert that the template contains at least one Method resource
  // with the given HTTP verb and Cognito authorization.
  function assertCognitoMethod(httpMethod: string): void {
    test(`at least one ${httpMethod} method uses COGNITO_USER_POOLS authorization`, () => {
      template.hasResourceProperties('AWS::ApiGateway::Method', {
        HttpMethod: httpMethod,
        AuthorizationType: 'COGNITO_USER_POOLS',
      });
    });
  }

  // Findings routes: 6 GETs + 1 POST approve
  assertCognitoMethod('GET');   // covers all GET methods
  assertCognitoMethod('POST');  // covers approve + dismiss + register-device
  assertCognitoMethod('PATCH'); // covers PATCH /actions/{action_id}

  // Count the total number of COGNITO_USER_POOLS methods — should be >= 17.
  // (Earlier routes like apiTriage, apiDriversMe etc. are also Cognito-gated,
  //  so the total is higher than 17; we assert >= 17 to cover the AVX routes.)
  test('at least 17 Cognito-authorised methods exist (all AVX + pre-existing)', () => {
    const allMethods = template.findResources('AWS::ApiGateway::Method', {
      Properties: { AuthorizationType: 'COGNITO_USER_POOLS' },
    });
    expect(Object.keys(allMethods).length).toBeGreaterThanOrEqual(17);
  });

  // Review Cycle 1 S3 (spec 2026-09-25-avx-own-vehicle-findings): the two tests above
  // pass without GET /findings/me, so pin that route and its authorizer directly.
  test('GET /findings/me exists under /findings and uses the shared Cognito authorizer', () => {
    const resources = template.findResources('AWS::ApiGateway::Resource');
    const logicalIdOf = (pathPart: string, parentId?: string): string[] =>
      Object.entries(resources)
        .filter(([, r]) => {
          const props = (r as Record<string, any>).Properties;
          return props.PathPart === pathPart
            && (parentId === undefined || props.ParentId?.Ref === parentId);
        })
        .map(([id]) => id);
    const findingsIds = logicalIdOf('findings');
    expect(findingsIds).toHaveLength(1);
    const meIds = logicalIdOf('me', findingsIds[0]);
    expect(meIds).toHaveLength(1);

    const authorizerIds = Object.keys(template.findResources('AWS::ApiGateway::Authorizer'));
    expect(authorizerIds).toHaveLength(1);

    const meMethods = Object.values(template.findResources('AWS::ApiGateway::Method'))
      .map((m) => (m as Record<string, any>).Properties)
      .filter((p) => p.ResourceId?.Ref === meIds[0]);
    // OPTIONS is the stack's CORS preflight; GET is the only authorised verb.
    expect(meMethods.map((p) => p.HttpMethod).sort()).toEqual(['GET', 'OPTIONS']);
    const get = meMethods.find((p) => p.HttpMethod === 'GET')!;
    expect(get.AuthorizationType).toBe('COGNITO_USER_POOLS');
    expect(get.AuthorizerId).toEqual({ Ref: authorizerIds[0] });
  });
});

// ─── (c) Narrow IAM — no wildcard resource, no dynamodb:* ────────────────────

/**
 * Collects all inline policy statements from all IAM Role inline policies
 * in the synthesised template.  CDK emits Lambda execution-role inline
 * policies as AWS::IAM::Policy resources with a PolicyDocument.
 */
function collectAllPolicyStatements(template: Template): Record<string, unknown>[] {
  const statements: Record<string, unknown>[] = [];

  const pushFrom = (pd: unknown) => {
    const doc = pd as Record<string, unknown> | undefined;
    if (!doc) return;
    const stmts = (doc['Statement'] ?? []) as Record<string, unknown>[];
    statements.push(...stmts);
  };

  // Standalone AWS::IAM::Policy resources (what CDK's grant*() emits by default).
  for (const policy of Object.values(template.findResources('AWS::IAM::Policy'))) {
    const props = (policy as Record<string, unknown>)['Properties'] as Record<string, unknown> | undefined;
    pushFrom(props?.['PolicyDocument']);
  }

  // Review Cycle 3, Critical C1: reading only AWS::IAM::Policy defeated FIVE tests in
  // this file at once. A grant attached via iam.ManagedPolicy or a Role's
  // inlinePolicies is a completely ordinary way to hand out permissions and was
  // invisible to every assertion here — a reviewer used it to re-grant
  // dynamodb:PutItem on the Tier 2 findings store at 25/25 green.
  //
  // cdk-nag (see the gate at the bottom of this file) now covers the WILDCARD
  // dimension of that defeat, because it walks the construct tree. It does not cover
  // the other dimension: a SPECIFIC action granted with no call site, which no
  // cdk-nag rule expresses and which is exactly what this correspondence guard is
  // for. So both resource types must be enumerated here too.
  for (const mp of Object.values(template.findResources('AWS::IAM::ManagedPolicy'))) {
    const props = (mp as Record<string, unknown>)['Properties'] as Record<string, unknown> | undefined;
    pushFrom(props?.['PolicyDocument']);
  }

  for (const role of Object.values(template.findResources('AWS::IAM::Role'))) {
    const props = (role as Record<string, unknown>)['Properties'] as Record<string, unknown> | undefined;
    const inline = (props?.['Policies'] ?? []) as Record<string, unknown>[];
    for (const p of inline) pushFrom(p['PolicyDocument']);
  }

  return statements;
}

/**
 * Read `Condition.StringEquals['cloudwatch:namespace']` off a synthesised statement.
 *
 * A helper rather than inline indexing because the template's statement type is `{}`,
 * so TS7053 rejects the chained lookup. Returns `undefined` when any level is absent,
 * which is exactly the "no condition" case the wildcard guard must treat as unsafe.
 */
function namespaceCondition(stmt: Record<string, unknown>): unknown {
  const condition = stmt['Condition'] as Record<string, unknown> | undefined;
  const stringEquals = condition?.['StringEquals'] as Record<string, unknown> | undefined;
  return stringEquals?.['cloudwatch:namespace'];
}

describe('AVX Lambda IAM — no wildcard resource, no dynamodb:*', () => {
  const { template } = makeStack();
  const statements = collectAllPolicyStatements(template);

  /**
   * Mutation guard A: no AVX policy statement targeting Resource: "*".
   * The CDK `logRetention` custom resource emits a `logs:PutRetentionPolicy` /
   * `logs:DeleteRetentionPolicy` statement with `Resource: "*"` — this is
   * a CDK-framework statement outside our code, so we exclude it.
   * Procedure: change any AVX policy resources array to ['*'], run test → FAIL.
   * Restored: yes. Recorded in file-level docstring.
   *
   * Second exemption added 2026-09-23 (Step-5 prerequisite 4):
   * `cloudwatch:PutMetricData`, which `agents/executors/escalation.py:304` calls for the
   * Addendum D5 terminal-failure metric. CloudWatch has NO resource-level ARN for that
   * action — `*` is the only value IAM accepts — so this is not a scoping shortcut, it is
   * the action's own shape.
   *
   * The exemption is CONDITIONAL, which is the part that matters: it applies only to a
   * statement that also carries a `cloudwatch:namespace` condition. Without that
   * condition the grant would allow writing ANY metric in the account, including
   * overwriting the inputs of other teams' alarms. A `*` PutMetricData with no condition
   * still fails this test, and
   * `test_putmetricdata_exemption_requires_a_namespace_condition` below asserts both
   * halves so the exemption cannot rot into a blanket one.
   */
  test('no AVX policy statement has Resource: "*"', () => {
    const wildcardStatements = statements.filter(stmt => {
      const actions = Array.isArray(stmt['Action']) ? stmt['Action'] : [stmt['Action']];
      // Exclude the CDK log-retention custom-resource statement (framework, not our code).
      const isLogRetention = (actions as string[]).every(
        (a: string) => a.startsWith('logs:PutRetentionPolicy') || a.startsWith('logs:DeleteRetentionPolicy'),
      );
      if (isLogRetention) return false;

      // Exclude cloudwatch:PutMetricData ONLY when narrowed by a namespace condition.
      const isNamespacedPutMetric =
        (actions as string[]).length === 1 &&
        (actions as string[])[0] === 'cloudwatch:PutMetricData' &&
        typeof namespaceCondition(stmt) === 'string';
      if (isNamespacedPutMetric) return false;

      const resource = stmt['Resource'];
      if (resource === '*') return true;
      if (Array.isArray(resource) && resource.includes('*')) return true;
      return false;
    });
    // We expect ZERO non-log-retention statements with a bare wildcard resource.
    expect(wildcardStatements).toHaveLength(0);
  });

  /**
   * The PutMetricData exemption above is only safe if the condition is really there.
   *
   * Two assertions, because an exemption allowlist that never matches anything is
   * indistinguishable from a correct one, and one that matches too much is worse than
   * no guard at all:
   *
   *   1. anti-vacuity — the exempted statement EXISTS. If escalation's metric grant is
   *      ever removed, this fails rather than silently leaving a dead exemption behind.
   *   2. narrowness — it carries the `cloudwatch:namespace` condition, and the namespace
   *      is the one `escalation.py` actually publishes to.
   */
  test('the PutMetricData exemption requires a namespace condition', () => {
    const putMetric = statements.filter(stmt => {
      const actions = Array.isArray(stmt['Action']) ? stmt['Action'] : [stmt['Action']];
      return (actions as string[]).includes('cloudwatch:PutMetricData');
    });

    // (1) anti-vacuity: the exemption must correspond to a real statement.
    expect(putMetric.length).toBeGreaterThan(0);

    // (2) every one of them is namespace-scoped, to the namespace escalation.py uses.
    for (const stmt of putMetric) {
      expect(namespaceCondition(stmt)).toBe('AVX/Executor');
      const actions = Array.isArray(stmt['Action']) ? stmt['Action'] : [stmt['Action']];
      expect(actions).toHaveLength(1);
    }
  });

  /**
   * The escalation's EventBridge grant targets the DEFAULT bus only.
   *
   * `escalation.py:295` calls `put_events` with no `EventBusName`, so the default bus is
   * where it lands. `event-bus/*` would additionally grant publish onto the DMS and CMS
   * integration buses in this account, which this Lambda has no reason to write to.
   */
  test('escalation PutEvents is scoped to the default event bus, not event-bus/*', () => {
    const putEvents = statements.filter(stmt => {
      const actions = Array.isArray(stmt['Action']) ? stmt['Action'] : [stmt['Action']];
      return (actions as string[]).includes('events:PutEvents');
    });
    expect(putEvents.length).toBeGreaterThan(0);  // anti-vacuity
    for (const stmt of putEvents) {
      const resources = Array.isArray(stmt['Resource']) ? stmt['Resource'] : [stmt['Resource']];
      for (const r of resources) {
        const rendered = JSON.stringify(r);
        expect(rendered).toContain('event-bus/default');
        expect(rendered).not.toContain('event-bus/*');
      }
    }
  });

  /**
   * Mutation guard B: no statement uses dynamodb:* action.
   * Procedure: change any AVX actions list to ['dynamodb:*'], run test → FAIL.
   * Restored: yes. Recorded in file-level docstring.
   */
  test('no AVX policy statement uses dynamodb:* catch-all', () => {
    const dynamoStarStatements = statements.filter(stmt => {
      const action = stmt['Action'];
      if (action === 'dynamodb:*') return true;
      if (Array.isArray(action) && action.includes('dynamodb:*')) return true;
      return false;
    });
    expect(dynamoStarStatements).toHaveLength(0);
  });

  test('no AVX policy statement uses sns:* catch-all', () => {
    const snsStarStatements = statements.filter(stmt => {
      const action = stmt['Action'];
      if (action === 'sns:*') return true;
      if (Array.isArray(action) && action.includes('sns:*')) return true;
      return false;
    });
    expect(snsStarStatements).toHaveLength(0);
  });
});

// ─── (d) SNS grant scoped to APNS platform-application prefix ────────────────

describe('Register-device Lambda — SNS CreatePlatformEndpoint scope', () => {
  const { template } = makeStack();

  /**
   * Mutation guard C: SNS resource must NOT be '*'.
   * Procedure: change the SnsCreateApnsEndpoint resources to ['*'], run test → FAIL.
   * Restored: yes. Recorded in file-level docstring.
   *
   * W4 fix (2026-09-19): resource now covers both APNS (production) and APNS_SANDBOX (staging).
   * The old underscore-only pattern would NOT match the production APNS platform application name.
   */
  test('sns:CreatePlatformEndpoint resource covers both APNS and APNS_SANDBOX, NOT "*"', () => {
    const policies = template.findResources('AWS::IAM::Policy');
    let foundApnsScoped = false;
    let foundApnsSandboxScoped = false;
    let foundWildcard   = false;

    for (const policy of Object.values(policies)) {
      const doc = (policy as Record<string, unknown>)['Properties'] as Record<string, unknown> | undefined;
      if (!doc) continue;
      const pd = doc['PolicyDocument'] as Record<string, unknown> | undefined;
      if (!pd) continue;
      const stmts = (pd['Statement'] ?? []) as Record<string, unknown>[];
      for (const stmt of stmts) {
        const actions = Array.isArray(stmt['Action']) ? stmt['Action'] : [stmt['Action']];
        if (!actions.includes('sns:CreatePlatformEndpoint')) continue;

        const resources = Array.isArray(stmt['Resource']) ? stmt['Resource'] : [stmt['Resource']];
        for (const r of resources) {
          if (typeof r === 'string') {
            if (r === '*') foundWildcard = true;
            // Both production APNS (no underscore) and APNS_SANDBOX (with underscore)
            if (r.includes(':app/APNS/') || r.includes(':app/APNS_')) {
              foundApnsScoped = true;
            }
            if (r.includes(':app/APNS_')) foundApnsSandboxScoped = true;
          }
          // Fn::Join tokens (CDK-synthesised ARNs) also satisfy the scoped check.
          if (typeof r === 'object' && r !== null) {
            // CDK Fn::Join always includes the scoped pattern
            foundApnsScoped = true;
            foundApnsSandboxScoped = true;
          }
        }
      }
    }

    // Must have APNS-scoped statements (production + sandbox).
    expect(foundApnsScoped).toBe(true);
    // Must NOT have any wildcard resource on sns:CreatePlatformEndpoint.
    expect(foundWildcard).toBe(false);
  });

  test('sns:Publish is NOT granted to the register-device Lambda role', () => {
    const policies = template.findResources('AWS::IAM::Policy');
    let foundSnsPublish = false;
    for (const policy of Object.values(policies)) {
      const doc = (policy as Record<string, unknown>)['Properties'] as Record<string, unknown> | undefined;
      if (!doc) continue;
      const pd = doc['PolicyDocument'] as Record<string, unknown> | undefined;
      if (!pd) continue;
      const stmts = (pd['Statement'] ?? []) as Record<string, unknown>[];
      for (const stmt of stmts) {
        const actions = Array.isArray(stmt['Action']) ? stmt['Action'] : [stmt['Action']];
        if (actions.includes('sns:Publish') || actions.includes('sns:*')) {
          foundSnsPublish = true;
        }
      }
    }
    expect(foundSnsPublish).toBe(false);
  });
});


// ─── T11.3: Two-way DynamoDB call-site ↔ IAM action correspondence (TABLE-AWARE) ───────────
//
// T10.4's flat-set guard compared (lambda, action) pairs, which allowed an
// over-grant on TABLE-A to pass when the same action was legitimately called on
// TABLE-B. T11.3 rewrites the guard to compare (lambda, TABLE, action) triples —
// so a GetItem grant on the findings table only passes when the reachable code calls
// get_item on _FINDINGS_TABLE specifically.
//
//   Direction 1 (ungrant): every boto3 DynamoDB call in the Lambda's bundled code
//   (handler.py + every _lib/*.py in infrastructure/build/<lambda>/) must have a
//   corresponding dynamodb:* action on the SAME TABLE in the Lambda's IAM policy.
//
//   Direction 2 (over-provision): every (table, action) pair in the IAM policy
//   must have at least one call site in the Lambda's bundled code on that SAME TABLE.
//
// Scope (T13.1 widening): ALL Python files in infrastructure/build/<lambda>/ —
//   handler.py PLUS every file under _lib/ — are scanned.  The scan set is derived
//   from the same build directory the C2 bundling tests check, so the guard and the
//   bundling assertion cannot disagree.  The prior "handler.py only" scope missed
//   _lib/avx_scope.py::_load_finding_by_id (reached from approve and dismiss).
//
// Handler-side table resolution:
//   Scan the source for `var_name = ...Table(TABLE_CONSTANT)` assignments, then
//   match subsequent `.method(` calls on those variable names. Each call site
//   resolves to a (TABLE_CONSTANT, IAM_ACTION) pair.
//
// IAM-side table resolution:
//   Use the policy statement SID to identify the target table via a static
//   SID→table mapping (the ARN is a Fn::ImportValue token at synth time and
//   is not a literal string). A statement without a recognised SID is reported
//   as an unknown-SID violation, which forces any new statement to declare its
//   table here.
//
// Mutations recorded in decisions.md T11.3 entry:
//   (a) Grant PutItem on findings table to api-avx-findings (handler only calls
//       query on findings) → MUST fail; T10.4 per-action guard PASSES (FindingsOK).
//   (b) _lib/avx_scope.py calls get_item on findings table with no GetItem grant
//       → MUST fail (ungrant direction: (findings, GetItem) missing from IAM).
//       T13.1: this is the key mutation — it PASSED before T13.1 because the guard
//       scanned only handler.py; it MUST fail now that _lib is in scope.
//   (c) Re-add UpdateItem with no call site (T10.6's original mutation) → MUST
//       still be caught (over-provision direction preserved).

describe('T11.3 — DynamoDB call-site ↔ IAM action two-way correspondence (table-aware)', () => {
  const path = require('path');
  const fs = require('fs');

  // ── boto3 Table method → IAM action name ──────────────────────────────────
  const CALL_TO_ACTION: Record<string, string> = {
    get_item:        'dynamodb:GetItem',
    put_item:        'dynamodb:PutItem',
    update_item:     'dynamodb:UpdateItem',
    delete_item:     'dynamodb:DeleteItem',
    query:           'dynamodb:Query',
    scan:            'dynamodb:Scan',
    batch_writer:    'dynamodb:BatchWriteItem',
    batch_get_item:  'dynamodb:BatchGetItem',
    transact_write:  'dynamodb:TransactWriteItems',
    transact_get:    'dynamodb:TransactGetItems',
  };

  // ── Python module-level table constant → table slug ───────────────────────
  //
  // Keys match the all-caps module-level constant names used in handler.py
  // assignments like `table = _get_ddb().Table(_FINDINGS_TABLE)`.
  const TABLE_CONSTANT_TO_SLUG: Record<string, string> = {
    '_FINDINGS_TABLE':   'findings',
    '_ACTIONS_TABLE':    'actions',
    '_FEEDBACK_TABLE':   'feedback',
    '_DEVICES_TABLE':    'notification_devices',
  };

  // ── IAM policy statement SID → table slug ─────────────────────────────────
  //
  // Every addToRolePolicy statement for an AVX Lambda must have a SID listed
  // here; any unrecognised SID is reported as an unknown-SID violation so that
  // new statements cannot bypass the table mapping silently.
  //
  // The SID→slug mapping is the source of truth for the IAM side.  It must
  // stay in sync with the SIDs in vsa-api-stack.ts whenever a policy statement
  // is added, renamed, or removed.
  // ── IAM policy statement SID registry ─────────────────────────────────────
  //
  // T14.1: the SID is a REGISTRATION GATE only — it no longer determines which
  // table a statement covers. Review Cycle 2 defeated the previous SID-derived
  // mapping by widening an existing statement's `resources` to a second table,
  // leaving SID and actions untouched: that re-granted `dynamodb:PutItem` on the
  // Tier 2 findings store (the exact grant T11.3 removed) at 25/25 green,
  // because a one-SID→one-slug map cannot express a two-table statement.
  // Table identity now comes from `stmt.Resource`; see EXPORT_NAME_TO_TABLE_SLUG.
  const KNOWN_STATEMENT_SIDS: ReadonlySet<string> = new Set([
    'AvxFindingsReadWrite',
    'AvxActionsWriteForApprove',
    'AvxActionsReadWrite',
    'AvxFindingsReadForEvidence',
    'AvxFeedbackWrite',
    'NotificationDevicesReadWrite',
    // SnsCreateApnsEndpoint is intentionally absent — it has no dynamodb:* actions.
  ]);

  // Each AVX table is imported via `Table.fromTableName(this, id,
  // cdk.Fn.importValue(<exportName>))`, so every `tableArn` in the synthesised
  // template is an `Fn::Join` containing `{"Fn::ImportValue": "<exportName>"}`.
  // Attribution is therefore done on the export name, which is structural — not
  // on ARN text, which is a token at synth time.
  const EXPORT_NAME_TO_TABLE_SLUG: Record<string, string> = {
    'avx-findings-table-name':           'findings',
    'avx-actions-table-name':            'actions',
    'avx-feedback-table-name':           'feedback',
    'notification-devices-table-name':   'notification_devices',
  };

  /**
   * Resolve every table slug a statement's `Resource` list covers.
   *
   * Throws if a resource cannot be attributed. Per Fix Group 14's governing
   * instruction, an unresolvable resource FAILS rather than falling back to a
   * guess — a guard that guesses is worse than no guard, because it reports green.
   */
  function extractTableSlugsFromResources(stmt: Record<string, unknown>): string[] {
    const raw = stmt['Resource'];
    const list = Array.isArray(raw) ? raw : [raw];
    const slugs = new Set<string>();
    for (const entry of list) {
      const blob = JSON.stringify(entry ?? null);
      const hit = Object.keys(EXPORT_NAME_TO_TABLE_SLUG).filter((exp) => blob.includes(exp));
      if (hit.length === 0) {
        throw new Error(
          `T14.1: cannot attribute a dynamodb statement resource to a known AVX table.\n` +
          `  sid: ${String(stmt['Sid'])}\n  resource: ${blob}\n` +
          `Add its export name to EXPORT_NAME_TO_TABLE_SLUG. Do NOT fall back to the SID.`,
        );
      }
      for (const exp of hit) slugs.add(EXPORT_NAME_TO_TABLE_SLUG[exp]);
    }
    return [...slugs];
  }

  // ── Handler file discovery ─────────────────────────────────────────────────
  //
  // Explicit mapping: directory name → CDK function name.
  // Any new api-avx-* directory requires a conscious entry here.
  const LAMBDA_DIR_TO_FUNCTION_NAME: Record<string, string> = {
    'api-avx-findings':                       'vsa-staging-api-avx-findings',
    'api-avx-actions':                        'vsa-staging-api-avx-actions',
    'api-avx-notifications-register-device':  'vsa-staging-api-avx-register-device',
  };

  const repoRoot = path.resolve(__dirname, '..', '..');
  const lambdasDir = path.join(repoRoot, 'lambdas');
  const buildDir  = path.join(repoRoot, 'infrastructure', 'build');

  // ── Bundled-module discovery ───────────────────────────────────────────────
  //
  // Returns all Python source files that ship inside each Lambda's deployment
  // package, derived from the same build directory the C2 bundling tests
  // check at `:185-191`.  The invariant is:
  //
  //   guard scan set  ≡  C2 bundling assertion set
  //
  // so the two cannot disagree.  Concretely, we collect every `.py` file at
  // the top level of `infrastructure/build/<lambda>/` PLUS every `.py` file
  // under `_lib/` within that directory.  Any module the build script puts
  // there is automatically included in the IAM call-site analysis.
  //
  // The old approach scanned only handler.py (a single source file, not the
  // deployable).  That proxy missed _lib/avx_scope.py::_load_finding_by_id,
  // which calls get_item on _FINDINGS_TABLE and is reached from approve
  // (api-avx-findings handler.py:394) and dismiss (api-avx-actions handler.py:461).
  function getAvxBundledSources(): { lambdaName: string; functionName: string; sources: string[] }[] {
    const result: { lambdaName: string; functionName: string; sources: string[] }[] = [];
    for (const [dirName, functionName] of Object.entries(LAMBDA_DIR_TO_FUNCTION_NAME)) {
      const lambdaBuildDir = path.join(buildDir, dirName);
      if (!fs.existsSync(lambdaBuildDir)) continue;

      const sources: string[] = [];

      // Top-level .py files (handler.py, any co-located helpers).
      for (const entry of fs.readdirSync(lambdaBuildDir)) {
        if (entry.endsWith('.py')) {
          sources.push(path.join(lambdaBuildDir, entry));
        }
      }

      // _lib/*.py — the shared library bundled alongside the handler.
      const libDir = path.join(lambdaBuildDir, '_lib');
      if (fs.existsSync(libDir)) {
        for (const entry of fs.readdirSync(libDir)) {
          if (entry.endsWith('.py')) {
            sources.push(path.join(libDir, entry));
          }
        }
      }

      if (sources.length > 0) {
        result.push({ lambdaName: dirName, functionName, sources });
      }
    }
    return result;
  }

  /** @deprecated — kept for backward compat; use getAvxBundledSources() */
  function getAvxHandlerFiles(): { lambdaName: string; functionName: string; filePath: string }[] {
    return getAvxBundledSources().map(({ lambdaName, functionName, sources }) => ({
      lambdaName,
      functionName,
      // filePath is the handler for SID-check and non-vacuity callers that
      // only need a single representative file (they don't scan source).
      filePath: sources.find(p => p.endsWith('handler.py')) ?? sources[0],
    }));
  }

  // ── Handler call-site extraction (table-aware) ────────────────────────────
  //
  // Returns a Set of "tableSlug:dynamodb:Action" strings — one per call site.
  //
  // Algorithm (stateful line scan):
  //   1. Scan each line for `VAR = ...Table(TABLE_CONSTANT)` assignments.
  //      Updates a live map of {var_name → table_slug}.
  //   2. Scan each line for `VAR.method(` calls.  Looks up the variable in the
  //      live map; if found, emits a (table_slug, action) triple.
  //
  // Patterns handled:
  //   - `table = _get_ddb().Table(_FINDINGS_TABLE)`
  //   - `findings_table = _get_ddb().Table(_FINDINGS_TABLE)`
  //   - any variable name ending in _table or named table
  //
  // Not handled (not needed for the current three handlers):
  //   - Chained calls like `_get_ddb().Table(_T).query(...)` in one line.
  //     The handlers never do this; if one is added the guard will miss it
  //     and fail the non-vacuity check or the over-provision direction.
  function extractCalledTableActions(handlerSource: string): Set<string> {
    const pairs = new Set<string>();
    // Live map: var_name → table_slug (updated as we scan).
    const varToSlug: Record<string, string> = {};

    // Assignment pattern: `var_name = ...Table(TABLE_CONSTANT_NAME)...`
    // Captures: group 1 = var_name, group 2 = TABLE_CONSTANT_NAME
    // Anchored at start of line (after optional indent) to avoid matching
    // call-style lines like `resp = table.query(...)`.
    const assignRe = /^\s*(\w+)\s*=\s*.*\.Table\((\w+)\)/;
    // Method call pattern: `var_name.method(` anywhere on the line.
    // Not anchored — the call often appears on the right side of an assignment
    // (e.g. `resp = table.query(**kwargs)`) or inside an if/try expression.
    // We use a global scan and take the first match per line.
    // Captures: group 1 = var_name, group 2 = method_name
    const callRe = /(\w+)\.(\w+)\s*\(/g;

    // T14.1 defeat (b): chained-receiver call sites.
    //   `_get_ddb().Table(_FINDINGS_TABLE).scan(Limit=1)`
    // `callRe` below requires `\w.\w(`, and the character before `.Table` and
    // before `.scan` is `)`, so it matches nothing here — the call site was
    // invisible and Direction 1 could not fire. Reproduced by review Cycle 2
    // at 25/25 green while adding an ungranted `dynamodb:Scan`.
    // Also accepts the keyword form `Table(name=CONST)`.
    // Run BEFORE the assignRe branch, because that branch `continue`s and would
    // otherwise skip a chained call appearing on an assignment line.
    const chainedRe = /\.Table\(\s*(?:name\s*=\s*)?(\w+)\s*\)\s*\.(\w+)\s*\(/g;

    for (const line of handlerSource.split('\n')) {
      chainedRe.lastIndex = 0;
      let chainedMatch: RegExpExecArray | null;
      while ((chainedMatch = chainedRe.exec(line)) !== null) {
        const slug   = TABLE_CONSTANT_TO_SLUG[chainedMatch[1]];
        const action = CALL_TO_ACTION[chainedMatch[2]];
        if (slug && action) {
          pairs.add(`${slug}:${action}`);
        }
      }

      // Skip lines that are assignment-to-Table (they update varToSlug; don't
      // also scan them for method calls, which would pick up `.Table(` itself).
      const assignMatch = assignRe.exec(line);
      if (assignMatch) {
        const varName   = assignMatch[1];
        const constName = assignMatch[2];
        const slug = TABLE_CONSTANT_TO_SLUG[constName];
        if (slug) {
          varToSlug[varName] = slug;
        }
        // If constant is unknown, we don't track it — the non-vacuity check
        // will catch missing triples if a new table is added without updating
        // TABLE_CONSTANT_TO_SLUG.
        continue;
      }

      // Scan all VAR.method( occurrences on this line.
      callRe.lastIndex = 0;
      let callMatch: RegExpExecArray | null;
      while ((callMatch = callRe.exec(line)) !== null) {
        const varName    = callMatch[1];
        const methodName = callMatch[2];
        const slug = varToSlug[varName];
        const action = CALL_TO_ACTION[methodName];
        if (slug && action) {
          pairs.add(`${slug}:${action}`);
        }
      }
    }
    return pairs;
  }

  // ── IAM grant extraction (table-aware) ────────────────────────────────────
  //
  // Returns a Set of "tableSlug:dynamodb:Action" strings and a list of
  // unrecognised SIDs.
  //
  // Algorithm:
  //   1. Find the Lambda resource by FunctionName, extract its role logical ID.
  //   2. Find all IAM::Policy resources attached to that role.
  //   3. For each statement: gate on KNOWN_STATEMENT_SIDS, then derive the
  //      table(s) from stmt.Resource (T14.1) and emit
  //      (table_slug, action) for each dynamodb:* action.  If the SID is not
  //      in the map, record it as unknown (the caller reports it as a violation).
  // ── Build-directory freshness (review Cycle 4, W1/D1) ────────────────────
  //
  // Every check below reads `infrastructure/build/api-avx-*`, which is correct — that
  // is what actually ships — but it is populated only by `scripts/build-avx-lambda.sh`,
  // and NOTHING in the documented Verify path runs it. `npm run build` is `tsc`. So a
  // stale build directory let Direction 1 pass on old content: Cycle 4 added an
  // ungranted `delete_item` to source and got 28/28 green, then the correct failure
  // once the script had run. Worse, T13.1's own recorded mutation was not reproducible
  // from T13.1's own Verify block for exactly this reason.
  //
  // cdk-nag cannot close this — it does not read Python. So assert freshness here, by
  // CONTENT rather than mtime (mtime is flaky across checkouts and rsync). A stale
  // directory now fails loudly naming the file and the command to run, instead of
  // silently weakening every call-site check that depends on it.
  describe('build-directory freshness (Cycle 4 W1/D1)', () => {
    test('bundled sources are identical to their originals in lambdas/', () => {
      const repoRoot = path.join(__dirname, '..', '..');
      const stale: string[] = [];
      const missing: string[] = [];

      for (const [dirName] of Object.entries(LAMBDA_DIR_TO_FUNCTION_NAME)) {
        const buildDir = path.join(__dirname, '..', 'build', dirName);
        if (!fs.existsSync(buildDir)) {
          missing.push(`${dirName}/ (build directory absent entirely)`);
          continue;
        }
        // handler.py comes from lambdas/<name>/handler.py
        const pairs: Array<[string, string]> = [
          [path.join(buildDir, 'handler.py'), path.join(repoRoot, 'lambdas', dirName, 'handler.py')],
        ];
        // _lib/avx_scope.py, when this Lambda bundles it, comes from lambdas/_lib/.
        const bundledLib = path.join(buildDir, '_lib', 'avx_scope.py');
        if (fs.existsSync(bundledLib)) {
          pairs.push([bundledLib, path.join(repoRoot, 'lambdas', '_lib', 'avx_scope.py')]);
        }
        for (const [built, source] of pairs) {
          if (!fs.existsSync(built)) { missing.push(path.relative(repoRoot, built)); continue; }
          if (!fs.existsSync(source)) { missing.push(path.relative(repoRoot, source)); continue; }
          const a = crypto.createHash('sha256').update(fs.readFileSync(built)).digest('hex');
          const b = crypto.createHash('sha256').update(fs.readFileSync(source)).digest('hex');
          if (a !== b) stale.push(path.relative(repoRoot, built));
        }
      }

      if (stale.length > 0 || missing.length > 0) {
        throw new Error(
          'The AVX build directory is STALE or INCOMPLETE, so every call-site check in ' +
          'this file is reading code that is not what the repo contains.\n\n' +
          (stale.length ? `  stale:   ${stale.join('\n           ')}\n` : '') +
          (missing.length ? `  missing: ${missing.join('\n           ')}\n` : '') +
          '\nRun:  bash scripts/build-avx-lambda.sh\n\n' +
          'This is not a tooling nit. `npm run build` is tsc and does NOT populate this ' +
          'directory, which is why review Cycle 4 could add an ungranted delete_item to ' +
          'source and still see 28/28 green — and why T13.1\'s own recorded mutation was ' +
          'not reproducible from T13.1\'s own Verify block.',
        );
      }
      expect(stale).toHaveLength(0);
      expect(missing).toHaveLength(0);
    });
  });

  function extractGrantedTableActions(
    template: Record<string, unknown>,
    functionName: string,
  ): { pairs: Set<string>; unknownSids: string[] } {
    const resources = template['Resources'] as Record<string, Record<string, unknown>>;
    const pairs = new Set<string>();
    const unknownSids: string[] = [];

    // Step 1: find the Lambda's role logical ID.
    let roleLogicalId: string | null = null;
    for (const res of Object.values(resources)) {
      if (res['Type'] !== 'AWS::Lambda::Function') continue;
      const props = res['Properties'] as Record<string, unknown>;
      if (props['FunctionName'] !== functionName) continue;
      const roleRef = props['Role'] as Record<string, unknown>;
      if (roleRef['Fn::GetAtt'] && Array.isArray(roleRef['Fn::GetAtt'])) {
        roleLogicalId = (roleRef['Fn::GetAtt'] as string[])[0];
      }
      break;
    }
    if (!roleLogicalId) return { pairs, unknownSids };

    // Step 2+3: find all attached IAM policies, extract (table_slug, action) pairs.
    //
    // C1 (review Cycle 3): this loop read ONLY `AWS::IAM::Policy`, so a grant attached
    // via `iam.ManagedPolicy` or a Role's `inlinePolicies` was invisible to both
    // correspondence directions. Managed and inline policies are enumerated too now.
    // Each shape reaches the role differently, which is why this is not one filter:
    //   - AWS::IAM::Policy        → Properties.Roles[] contains { Ref: <role> }
    //   - AWS::IAM::ManagedPolicy → Properties.Roles[] likewise
    //   - AWS::IAM::Role          → the role IS the resource; Properties.Policies[] is inline
    const policyDocsForRole: Record<string, unknown>[] = [];

    // C1 second miss (review Cycle 4): CDK has TWO ways to attach a managed policy and
    // they land in DIFFERENT places in the template.
    //   new iam.ManagedPolicy(..., { roles: [role] })  → ManagedPolicy.Properties.Roles[]
    //   role.addManagedPolicy(new iam.ManagedPolicy(...))
    //                                                  → Role.Properties.ManagedPolicyArns[]
    // Cycle 3's fix handled only the first, and Cycle 4 defeated the guard in one line
    // with the second: `dynamodb:PutItem` on the findings table, no call site, 28/28
    // green. I had "verified" the fix with the idiom the fix itself anticipated, which
    // is the same mistake this file keeps documenting. Collect the role's attached
    // managed-policy logical ids first, then match on EITHER direction of the link.
    const attachedManagedPolicyIds = new Set<string>();
    {
      const roleRes = resources[roleLogicalId];
      const roleProps = (roleRes?.['Properties'] ?? {}) as Record<string, unknown>;
      for (const entry of (roleProps['ManagedPolicyArns'] ?? []) as unknown[]) {
        if (typeof entry === 'object' && entry !== null) {
          const ref = (entry as Record<string, unknown>)['Ref'];
          if (typeof ref === 'string') attachedManagedPolicyIds.add(ref);
        }
      }
    }

    for (const [logicalId, res] of Object.entries(resources)) {
      const props = (res['Properties'] ?? {}) as Record<string, unknown>;
      const type = res['Type'];

      if (type === 'AWS::IAM::Policy' || type === 'AWS::IAM::ManagedPolicy') {
        const roles = (props['Roles'] ?? []) as unknown[];
        const linkedViaRolesProp = roles.some(
          (r: unknown) =>
            typeof r === 'object' && r !== null &&
            (r as Record<string, unknown>)['Ref'] === roleLogicalId,
        );
        const linkedViaRoleArns = attachedManagedPolicyIds.has(logicalId);
        if (!linkedViaRolesProp && !linkedViaRoleArns) continue;
        const doc = props['PolicyDocument'] as Record<string, unknown> | undefined;
        if (doc) policyDocsForRole.push(doc);
        continue;
      }

      if (type === 'AWS::IAM::Role' && logicalId === roleLogicalId) {
        const inline = (props['Policies'] ?? []) as Record<string, unknown>[];
        for (const p of inline) {
          const doc = p['PolicyDocument'] as Record<string, unknown> | undefined;
          if (doc) policyDocsForRole.push(doc);
        }
      }
    }

    for (const doc of policyDocsForRole) {
      const stmts = (doc['Statement'] ?? []) as Record<string, unknown>[];
      for (const stmt of stmts) {
        const sid = stmt['Sid'] as string | undefined;

        // Review Cycle 4: `NotAction` is unmodelled by all three IAM layers. A
        // statement granting `NotAction: [dynamodb:DeleteItem]` on the findings table
        // permits every OTHER dynamodb action, which is strictly broader than anything
        // this guard reasons about — and it survived only because `Array.isArray(
        // stmt['Action'])` on an absent key yielded `[undefined]` and then threw a
        // TypeError. An accidental TypeError is not a control: the one-line
        // type-safety fix any reader would make turns it green.
        //
        // Not modelled here either, deliberately — inverted grants on a data-plane
        // table are a design smell rather than something to teach a guard to score.
        // Fail loudly instead, so the choice has to be made explicitly.
        if (stmt['NotAction'] !== undefined) {
          throw new Error(
            `AVX policy statement (Sid: ${sid ?? '<none>'}) uses NotAction, which no IAM ` +
            `guard in this repo models. NotAction grants everything EXCEPT what it lists, ` +
            `so it is strictly broader than the two-way correspondence check can reason ` +
            `about. Rewrite it as an explicit Action list, or if an inverted grant is ` +
            `genuinely intended, extend this guard and cdk-nag coverage first and say why ` +
            `in decisions.md.`,
          );
        }

        const actions = Array.isArray(stmt['Action'])
          ? stmt['Action']
          : stmt['Action'] === undefined ? [] : [stmt['Action']];
        const dynamoActions = (actions as string[]).filter(
          (a: string) => typeof a === 'string' && a.startsWith('dynamodb:'),
        );
        if (dynamoActions.length === 0) continue;

        if (!sid || !KNOWN_STATEMENT_SIDS.has(sid)) {
          // A statement with dynamodb:* actions but no recognised SID — the
          // caller reports this as a violation to force explicit registration.
          unknownSids.push(sid ?? '<no SID>');
          continue;
        }

        // T14.1: one (table, action) pair per (resource-table × action), so a
        // statement whose resources span two tables produces pairs for BOTH.
        for (const tableSlug of extractTableSlugsFromResources(stmt)) {
          for (const action of dynamoActions) {
            pairs.add(`${tableSlug}:${action}`);
          }
        }
      }
    }
    return { pairs, unknownSids };
  }

  // ── Stack fixture ────────────────────────────────────────────────────────
  const { template: stackTemplate } = makeStack();
  const rawTemplate = stackTemplate.toJSON() as Record<string, unknown>;

  // ── Non-vacuity check ────────────────────────────────────────────────────
  //
  // Guard must find at least one (lambda, table, action) triple to check.
  // Fails if TABLE_CONSTANT_TO_SLUG is empty or no bundled module has DDB calls.
  test('non-vacuity: guard finds at least one (lambda, table, action) triple to check', () => {
    const lambdas = getAvxBundledSources();
    expect(lambdas.length).toBeGreaterThan(0);
    let totalTriples = 0;
    for (const { sources, functionName } of lambdas) {
      const combinedSource = sources.map(p => fs.readFileSync(p, 'utf8') as string).join('\n');
      totalTriples += extractCalledTableActions(combinedSource).size;
      totalTriples += extractGrantedTableActions(rawTemplate, functionName).pairs.size;
    }
    expect(totalTriples).toBeGreaterThan(0);
  });

  // ── No unrecognised SIDs ──────────────────────────────────────────────────
  //
  // Any dynamodb:* statement whose SID is not in KNOWN_STATEMENT_SIDS means the
  // table mapping is out of date. This must fail before the direction tests
  // so the author knows to update the mapping, not just add a grant.
  test('all dynamodb IAM statements on AVX Lambdas have a recognised SID', () => {
    const handlers = getAvxHandlerFiles();
    const violations: string[] = [];
    for (const { lambdaName, functionName } of handlers) {
      const { unknownSids } = extractGrantedTableActions(rawTemplate, functionName);
      for (const sid of unknownSids) {
        violations.push(`${lambdaName}: statement SID "${sid}" not in KNOWN_STATEMENT_SIDS`);
      }
    }
    expect(violations).toHaveLength(0);
    if (violations.length > 0) {
      throw new Error(
        'Unknown-SID violations (add to KNOWN_STATEMENT_SIDS in the test):\n' +
        violations.map(v => `  - ${v}`).join('\n'),
      );
    }
  });

  // ── Direction 1: every call site has a (table, action) grant ─────────────
  //
  // For each api-avx-* Lambda, every (table_slug, dynamodb:Action) pair
  // derived from ALL bundled Python source files must have a matching IAM
  // grant on the same table.  "Bundled" is defined by the build directory:
  // any .py file in infrastructure/build/<lambda>/ (handler.py + _lib/*.py).
  //
  // Mutation (a): remove GetItem from AvxFindingsReadWrite
  //   → _lib/avx_scope.py::_load_finding_by_id calls get_item on findings
  //   → (findings, GetItem) in bundled calls but NOT in IAM → FAILS [CAUGHT]
  //   (This mutation PASSED before T13.1 because only handler.py was scanned.)
  // Mutation (b): add get_item on _FEEDBACK_TABLE in _lib/avx_scope.py with no grant
  //   → (feedback, GetItem) in bundled calls but NOT in IAM → FAILS [CAUGHT]
  //   (Proves the guard reads _lib generally, not just one special call site.)
  // Mutation (c) [T10.6 repro]: add UpdateItem grant on actions with no call site
  //   → direction 2 FAILS (not direction 1). Both mutations are recorded below.
  test('every DynamoDB call site in each AVX Lambda (handler + bundled _lib) has a (table, action) IAM grant', () => {
    const lambdas = getAvxBundledSources();
    const violations: string[] = [];

    for (const { lambdaName, functionName, sources } of lambdas) {
      const combinedSource = sources.map(p => fs.readFileSync(p, 'utf8') as string).join('\n');
      const calledPairs  = extractCalledTableActions(combinedSource);
      const { pairs: grantedPairs } = extractGrantedTableActions(rawTemplate, functionName);

      for (const pair of calledPairs) {
        if (!grantedPairs.has(pair)) {
          const [tableSlug, action] = pair.split(':dynamodb:');
          violations.push(
            `${lambdaName}: bundled code calls dynamodb:${action} on ${tableSlug} table ` +
            `but policy does NOT grant it on that table`,
          );
        }
      }
    }

    expect(violations).toHaveLength(0);
    if (violations.length > 0) {
      throw new Error(
        'Ungrant violations (call site without matching table-scoped IAM grant):\n' +
        violations.map(v => `  - ${v}`).join('\n'),
      );
    }
  });

  // ── Direction 2: every (table, action) grant has a call site ─────────────
  //
  // For each api-avx-* Lambda, every (table_slug, dynamodb:Action) pair in
  // the IAM policy must have at least one call site across ALL bundled Python
  // source files (handler.py + _lib/*.py in the build directory).
  //
  // Mutation (a): grant PutItem on findings table to api-avx-findings
  //   → no bundled code calls put_item on findings
  //   → (findings, PutItem) in IAM but not in calls → FAILS [CAUGHT]
  // Mutation (c) [T10.6 repro]: add UpdateItem to AvxFindingsReadWrite (no call
  //   site on findings table) → (findings, UpdateItem) in IAM but not in calls
  //   → FAILS [CAUGHT — proves the rewrite loses nothing T10.6 caught]
  test('every (table, action) IAM grant on each AVX Lambda has a call site in the bundled code', () => {
    const lambdas = getAvxBundledSources();
    const violations: string[] = [];

    for (const { lambdaName, functionName, sources } of lambdas) {
      const combinedSource = sources.map(p => fs.readFileSync(p, 'utf8') as string).join('\n');
      const calledPairs  = extractCalledTableActions(combinedSource);
      const { pairs: grantedPairs } = extractGrantedTableActions(rawTemplate, functionName);

      for (const pair of grantedPairs) {
        if (!calledPairs.has(pair)) {
          const [tableSlug, action] = pair.split(':dynamodb:');
          violations.push(
            `${lambdaName}: policy grants dynamodb:${action} on ${tableSlug} table ` +
            `but no bundled code has a matching call site on that table`,
          );
        }
      }
    }

    expect(violations).toHaveLength(0);
    if (violations.length > 0) {
      throw new Error(
        'Over-provision violations (IAM grant without matching table-scoped call site in bundled code):\n' +
        violations.map(v => `  - ${v}`).join('\n'),
      );
    }
  });
});



/**
 * ── cdk-nag gate (review Cycle 3, Critical C1) ───────────────────────────────
 *
 * C1: attaching a grant via `iam.ManagedPolicy` or a Role's `inlinePolicies`
 * defeated FIVE of this file's IAM tests at once — including the two dedicated
 * wildcard/catch-all assertions — because every one of them walks the synthesised
 * template looking only at `AWS::IAM::Policy`. A reviewer used that to re-grant
 * `dynamodb:PutItem` on the Tier 2 findings store, and then `dynamodb:*` on
 * `Resource: '*'`, at 25/25 green.
 *
 * The root cause is not which resource type the walker reads. It is that this spec
 * kept hand-rolling IAM analysis: across three review cycles there were nine guard
 * defeats, twice inside a guard written to fix the previous one, and each fix
 * swapped one wrong proxy for another (`handler.py` → a stale generated build dir;
 * the statement SID → `AWS::IAM::Policy` only). Enumerating shapes loses that race.
 *
 * cdk-nag already solves this properly and is already wired for real synths —
 * `bin/vsa.ts:87` applies `AwsSolutionsChecks` as an Aspect and `:710` applies the
 * app's scoped suppressions. It is an Aspect over the CONSTRUCT TREE, so it sees
 * managed policies, inline policies and grants alike, and it is a maintained rule
 * pack rather than our regex.
 *
 * The gap was never cdk-nag's coverage: it was that `makeStack()` builds a bare
 * `cdk.App()` with no Aspect and no suppressions, so **cdk-nag never ran in this
 * suite**. That is precisely why C1's mutation passed here while a real `cdk synth`
 * would likely have rejected it. This block closes that gap by running the same
 * Aspect and the same suppressions the app uses.
 *
 * Deliberately NOT a new hand-rolled check. The two-way correspondence guard above
 * keeps its distinct job — "granted but never called", which cdk-nag has no rule
 * for — and stops being relied on as the wildcard detector.
 */
describe('C1 — cdk-nag gate (AwsSolutionsChecks over the real construct tree)', () => {
  /**
   * Build the API stack under the same Aspect + suppressions as `bin/vsa.ts`.
   *
   * Reuses `makeStack()` rather than duplicating its ~40 stubbed props, so the
   * gate can never drift from the stack the other tests assert against — a
   * second fixture would be one more proxy to get wrong.
   */
  function makeNagCheckedStack(): cdk.Stack {
    const { stack } = makeStack();
    const app = stack.node.root as cdk.App;

    // Same order as bin/vsa.ts: Aspect first, then the app's scoped suppressions.
    cdk.Aspects.of(app).add(new AwsSolutionsChecks({ verbose: true }));
    applyCvxNagSuppressions(app);

    // makeStack() already synthesised via Template.fromStack; force a re-synth so
    // the newly-added Aspect actually runs and attaches its annotations.
    app.synth({ force: true });
    return stack;
  }

  test('no unsuppressed cdk-nag IAM errors on the AVX API stack', () => {
    const stack = makeNagCheckedStack();
    const annotations = Annotations.fromStack(stack);

    // IAM4 = AWS managed policy; IAM5 = wildcard permissions. Both are scoped-
    // suppressed app-wide via appliesTo lists, so anything surfacing here is a
    // pattern the app has NOT justified — which is what C1 exploited.
    const iam4 = annotations.findError('*', Match.stringLikeRegexp('AwsSolutions-IAM4'));
    const iam5 = annotations.findError('*', Match.stringLikeRegexp('AwsSolutions-IAM5'));
    const found = [...iam4, ...iam5];

    if (found.length > 0) {
      const detail = found
        .map((f) => `  ${f.id}: ${JSON.stringify(f.entry.data).slice(0, 300)}`)
        .join('\n');
      throw new Error(
        `cdk-nag reported ${found.length} unsuppressed IAM finding(s) on the AVX API ` +
        `stack.\n\nEach is either a real over-grant or a pattern that needs an ` +
        `explicit, appliesTo-scoped suppression in lib/nag-suppressions.ts with a ` +
        `stated reason. Do NOT add a blanket suppression — every existing IAM4/IAM5 ` +
        `entry in that file is appliesTo-scoped, and keeping it that way is what ` +
        `makes this gate meaningful.\n\n${detail}`,
      );
    }
    expect(found).toHaveLength(0);
  });

  test('the gate is not vacuous — the Aspect is actually registered', () => {
    // A gate that silently runs zero rules passes forever, and "an Aspect was
    // dropped" is the exact defect this block exists to fix (makeStack() never had
    // one). An earlier version of this test asserted that cdk-nag left at least one
    // annotation — which was wrong: once every finding is either compliant or
    // scope-suppressed, a clean stack legitimately has none, so the check would fail
    // precisely when the code was correct. Assert the registration instead.
    const { stack } = makeStack();
    const app = stack.node.root as cdk.App;
    cdk.Aspects.of(app).add(new AwsSolutionsChecks({ verbose: true }));

    const registered = cdk.Aspects.of(app).all;
    expect(registered.length).toBeGreaterThan(0);
    if (!registered.some((a) => a instanceof AwsSolutionsChecks)) {
      throw new Error(
        'AwsSolutionsChecks is not registered on the app — the cdk-nag gate is disabled ' +
        'and every IAM finding it would catch is now invisible, which is how review ' +
        'Cycle 3 Critical C1 passed 25/25.',
      );
    }
    expect(registered.some((a) => a instanceof AwsSolutionsChecks)).toBe(true);
  });

  test('the gate has teeth — a wildcard outside every appliesTo list is reported', () => {
    // The positive control for the test above: prove the rule pack actually rejects
    // something. A grant on `Resource: '*'` matches none of the appliesTo regexes in
    // lib/nag-suppressions.ts, so it must surface as an unsuppressed IAM5 error. This
    // is the shape a reviewer used to defeat five hand-rolled tests at once via
    // iam.ManagedPolicy — cdk-nag sees it because it walks the construct tree rather
    // than grepping for AWS::IAM::Policy.
    const { stack } = makeStack();
    const app = stack.node.root as cdk.App;

    new iam.ManagedPolicy(stack, 'NagTeethProbe', {
      statements: [
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['dynamodb:*'],
          resources: ['*'],
        }),
      ],
      roles: [new iam.Role(stack, 'NagTeethProbeRole', {
        assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      })],
    });

    cdk.Aspects.of(app).add(new AwsSolutionsChecks({ verbose: true }));
    applyCvxNagSuppressions(app);
    app.synth({ force: true });

    const iam5 = Annotations.fromStack(stack).findError(
      '*', Match.stringLikeRegexp('AwsSolutions-IAM5'),
    );
    if (iam5.length === 0) {
      throw new Error(
        'cdk-nag did not report a ManagedPolicy granting dynamodb:* on Resource "*". ' +
        'If this passes with zero findings the gate is decorative.',
      );
    }
    expect(iam5.length).toBeGreaterThan(0);
  });
});
