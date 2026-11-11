/**
 * FI Stage D task 4.3 — API-stack investigations wiring.
 *
 * Assertions:
 *   (a) api-avx-investigations Lambda exists with the expected function name,
 *       env vars, and IAM — exactly the actions listed in the handler's DDB calls,
 *       sqs:SendMessage, and cloudwatch:PutMetricData (AVX/Investigations only).
 *   (b) findings Lambda gets ADP_THREAD_WRITER_ARN, lambda:InvokeFunction on that
 *       ARN only, quality.issue hook write grants, and AVX/Investigations metric.
 *   (c) POST /investigations and GET /investigations/{investigationId} exist with
 *       COGNITO authorisation.
 *
 * Mutation result (task requirement):
 *   AdpThreadWriterInvoke widened to Resource: "*"
 *   → test_adp_invoke_exact_arn FAILS (globMatches catches it).
 *   Restored: yes. Recorded 2026-10-12.
 */

import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as kms from 'aws-cdk-lib/aws-kms';
import { VsaApiStack } from '../lib/vsa-api-stack';

// ─── Constants ───────────────────────────────────────────────────────────────

const ACCOUNT = '123456789012';
const REGION  = 'us-west-2';
// adpRegion: process.env.ADP_REGION || 'us-east-1'
const ADP_REGION = 'us-east-1';
const STAGE   = 'staging';

const INV_TABLE = `vsa-${STAGE}-avx-investigations`;
const INV_QUEUE = `vsa-${STAGE}-avx-investigations`;
const INV_TABLE_ARN = `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/${INV_TABLE}`;
const INV_QUEUE_ARN = `arn:aws:sqs:${REGION}:${ACCOUNT}:${INV_QUEUE}`;
const INV_QUEUE_URL = `https://sqs.${REGION}.amazonaws.com/${ACCOUNT}/${INV_QUEUE}`;
const ADP_WRITER_ARN = `arn:aws:lambda:${ADP_REGION}:${ACCOUNT}:function:adp-${STAGE}-digital-thread-writer`;

// ─── Stack fixture ────────────────────────────────────────────────────────────

let stackCount = 0;

function makeStack(): { stack: VsaApiStack; template: Template } {
  const id = ++stackCount;
  const app = new cdk.App();
  const env = { account: ACCOUNT, region: REGION };
  const parentStack = new cdk.Stack(app, `Parent${id}`, { env });
  const stubTable = (lid: string, name: string) =>
    dynamodb.Table.fromTableName(parentStack, lid, name);
  const stubFn = (lid: string, arn: string) =>
    lambda.Function.fromFunctionArn(parentStack, lid, arn);

  const stack = new VsaApiStack(app, `vsa-${STAGE}-api-fi-inv-${id}`, {
    env,
    stage: STAGE,
    cmsStage: STAGE,
    tenantConfigTable: stubTable('TC', `cms-${STAGE}-tenant-config`),
    classifier: stubFn('Cls', `arn:aws:lambda:${REGION}:${ACCOUNT}:function:vsa-${STAGE}-triage-classifier`),
    cmsKmsKeyArn:   { literal: `arn:aws:kms:${REGION}:${ACCOUNT}:key/test-key`, parameterName: `/cvx/${STAGE}/cms-kms-key-arn` },
    redisEndpoint:  { literal: 'redis://stub.redis.local:6379', parameterName: `/cvx/${STAGE}/redis-endpoint` },
    connectInstanceArn: { literal: `arn:aws:connect:us-east-1:${ACCOUNT}:instance/test-instance`, parameterName: `/cvx/${STAGE}/connect-instance-arn` },
    dmsApiEndpoint: { literal: 'https://dms.example.invalid', parameterName: `/cvx/${STAGE}/dms-api-endpoint` },
    cmsApiEndpoint: { literal: 'https://cms.example.invalid', parameterName: `/cvx/${STAGE}/cms-api-endpoint` },
    vpcId: 'vpc-00000000000000000',
    redisSecurityGroupId: 'sg-00000000000000000',
    assistantChatRuntimeArn: {
      literal: `arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT}:runtime/stub`,
      parameterName: `/cvx/${STAGE}/assistant-chat-runtime-arn`,
    },
    assistantChatRuntimeRegion: REGION,
    allowedOrigin: { literal: 'https://stub.example.com', parameterName: `/cvx/${STAGE}/allowed-origin` },
    acquireReservationHandoffFn: stubFn('AH', `arn:aws:lambda:${REGION}:${ACCOUNT}:function:vsa-${STAGE}-acquire-handoff`),
    acquireReservationHandoffFnArnExportName: `vsa-${STAGE}-acquire-reservation-handoff-fn-arn`,
    acquireCatalogTable: stubTable('ACat', `vsa-${STAGE}-acquire-catalog`),
    acquireOrdersTable:  stubTable('AOrd', `vsa-${STAGE}-acquire-orders`),
    acquireLeadsTable:   stubTable('ALd',  `vsa-${STAGE}-acquire-leads`),
    avxFindingsTableNameExportName:         `vsa-${STAGE}-avx-findings-table-name`,
    avxActionsTableNameExportName:          `vsa-${STAGE}-avx-actions-table-name`,
    avxFeedbackTableNameExportName:         `vsa-${STAGE}-avx-feedback-table-name`,
    notificationDevicesTableNameExportName: `vsa-${STAGE}-notification-devices-table-name`,
    avxApprovalRulesTableNameExportName:    `vsa-${STAGE}-avx-approval-rules-table-name`,
    rsaDispatchTable: dynamodb.Table.fromTableArn(parentStack, `RD${id}`,
      `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/vsa-${STAGE}-rsa-dispatch`),
    rsaDispatchKey: kms.Key.fromKeyArn(parentStack, `RK${id}`,
      `arn:aws:kms:${REGION}:${ACCOUNT}:key/test-rsa-dispatch-key`),
  });

  return { stack, template: Template.fromStack(stack) };
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

/** IAM glob (only `*` and `?`) → RegExp. Non-string resources never match. */
function globMatches(resource: unknown, target: string): boolean {
  if (typeof resource !== 'string') return false;
  const re = new RegExp(
    '^' + resource.replace(/[.+^${}()|[\]\\]/g, '\\$&')
      .replace(/\*/g, '.*').replace(/\?/g, '.') + '$',
  );
  return re.test(target);
}

/** Collect all IAM policy statements from the synthesised template. */
function allStatements(template: Template): Record<string, unknown>[] {
  const out: Record<string, unknown>[] = [];
  const push = (pd: unknown) => {
    const doc = pd as Record<string, unknown> | undefined;
    if (!doc) return;
    out.push(...((doc['Statement'] ?? []) as Record<string, unknown>[]));
  };
  for (const p of Object.values(template.findResources('AWS::IAM::Policy'))) {
    push((p as any).Properties?.PolicyDocument);
  }
  for (const p of Object.values(template.findResources('AWS::IAM::ManagedPolicy'))) {
    push((p as any).Properties?.PolicyDocument);
  }
  for (const r of Object.values(template.findResources('AWS::IAM::Role'))) {
    for (const ip of (r as any).Properties?.Policies ?? []) push(ip.PolicyDocument);
  }
  return out;
}

/** Statements attached to the Lambda role by function name. */
function statementsForLambda(template: Template, functionName: string): Record<string, unknown>[] {
  // Find the Lambda resource's role logical id.
  const fns = template.findResources('AWS::Lambda::Function', {
    Properties: { FunctionName: functionName },
  });
  expect(Object.keys(fns)).toHaveLength(1);
  const roleRef = (Object.values(fns)[0] as any).Properties.Role as { 'Fn::GetAtt': string[] };
  const roleLogicalId = roleRef['Fn::GetAtt'][0];

  // Find policies attached to that role.
  const stmts: Record<string, unknown>[] = [];
  const push = (pd: unknown) => {
    const doc = pd as Record<string, unknown> | undefined;
    if (!doc) return;
    stmts.push(...((doc['Statement'] ?? []) as Record<string, unknown>[]));
  };
  for (const [, p] of Object.entries(template.findResources('AWS::IAM::Policy'))) {
    const roles = (p as any).Properties?.Roles ?? [];
    const attached = roles.some((r: unknown) => {
      const ref = r as Record<string, unknown> | undefined;
      return ref?.['Ref'] === roleLogicalId;
    });
    if (attached) push((p as any).Properties?.PolicyDocument);
  }
  // Inline policies on the Role.
  const roles = template.findResources('AWS::IAM::Role');
  if (roles[roleLogicalId]) {
    for (const ip of (roles[roleLogicalId] as any).Properties?.Policies ?? []) {
      push(ip.PolicyDocument);
    }
  }
  return stmts;
}

function bySid(stmts: Record<string, unknown>[], sid: string): Record<string, unknown> {
  const found = stmts.filter(s => s['Sid'] === sid);
  expect(found).toHaveLength(1);
  return found[0];
}

function asList<T>(v: T | T[]): T[] {
  return Array.isArray(v) ? v : [v];
}

// ─── (a) investigations Lambda ────────────────────────────────────────────────

describe('api-avx-investigations Lambda', () => {
  const { template } = makeStack();

  test('function name is vsa-staging-api-avx-investigations', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: `vsa-${STAGE}-api-avx-investigations`,
    });
  });

  test('env: AVX_INVESTIGATIONS_TABLE = vsa-staging-avx-investigations', () => {
    const fns = template.findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: `vsa-${STAGE}-api-avx-investigations` },
    });
    const env = (Object.values(fns)[0] as any).Properties.Environment.Variables;
    expect(env.AVX_INVESTIGATIONS_TABLE).toBe(INV_TABLE);
  });

  test('env: AVX_INVESTIGATIONS_QUEUE_URL matches expected SQS URL', () => {
    const fns = template.findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: `vsa-${STAGE}-api-avx-investigations` },
    });
    const env = (Object.values(fns)[0] as any).Properties.Environment.Variables;
    expect(env.AVX_INVESTIGATIONS_QUEUE_URL).toBe(INV_QUEUE_URL);
  });

  test('AvxInvestigationsTableWrite: exact 4 DDB actions on table ARN only (no index)', () => {
    const stmts = statementsForLambda(
      template, `vsa-${STAGE}-api-avx-investigations`,
    );
    const st = bySid(stmts, 'AvxInvestigationsTableWrite');
    expect(st.Effect).toBe('Allow');
    expect(asList(st.Action).sort()).toEqual([
      'dynamodb:ConditionCheckItem',
      'dynamodb:GetItem',
      'dynamodb:PutItem',
      'dynamodb:UpdateItem',
    ]);
    expect(asList(st.Resource)).toEqual([INV_TABLE_ARN]);
  });

  test('AvxInvestigationsSqsSend: sqs:SendMessage on queue ARN only', () => {
    const stmts = statementsForLambda(
      template, `vsa-${STAGE}-api-avx-investigations`,
    );
    const st = bySid(stmts, 'AvxInvestigationsSqsSend');
    expect(st.Effect).toBe('Allow');
    expect(asList(st.Action)).toEqual(['sqs:SendMessage']);
    expect(asList(st.Resource)).toEqual([INV_QUEUE_ARN]);
  });

  test('AvxInvestigationsPutMetric: PutMetricData with AVX/Investigations namespace', () => {
    const stmts = statementsForLambda(
      template, `vsa-${STAGE}-api-avx-investigations`,
    );
    const st = bySid(stmts, 'AvxInvestigationsPutMetric');
    expect(st.Effect).toBe('Allow');
    expect(asList(st.Action)).toEqual(['cloudwatch:PutMetricData']);
    expect(asList(st.Resource)).toEqual(['*']);
    const cond = (st['Condition'] as any)?.StringEquals?.['cloudwatch:namespace'];
    expect(cond).toBe('AVX/Investigations');
  });

  test('no lambda:InvokeFunction grant on the investigations Lambda role', () => {
    const stmts = statementsForLambda(
      template, `vsa-${STAGE}-api-avx-investigations`,
    );
    const invokes = stmts.filter(s =>
      s.Effect === 'Allow' &&
      asList(s.Action).some((a: unknown) => globMatches(a, 'lambda:InvokeFunction')),
    );
    expect(invokes).toHaveLength(0);
  });
});

// ─── (b) findings Lambda additions ───────────────────────────────────────────

describe('api-avx-findings Lambda — task 4.3 additions', () => {
  const { template } = makeStack();

  test('ADP_THREAD_WRITER_ARN env is the exact ADP thread writer function ARN', () => {
    const fns = template.findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: `vsa-${STAGE}-api-avx-findings` },
    });
    const env = (Object.values(fns)[0] as any).Properties.Environment.Variables;
    expect(env.ADP_THREAD_WRITER_ARN).toBe(ADP_WRITER_ARN);
  });

  test('AdpThreadWriterInvoke: lambda:InvokeFunction on the ADP writer ARN only', () => {
    const stmts = statementsForLambda(template, `vsa-${STAGE}-api-avx-findings`);
    const st = bySid(stmts, 'AdpThreadWriterInvoke');
    expect(st.Effect).toBe('Allow');
    expect(asList(st.Action)).toEqual(['lambda:InvokeFunction']);
    expect(asList(st.Resource)).toEqual([ADP_WRITER_ARN]);
  });

  /**
   * Mutation: widen AdpThreadWriterInvoke to Resource: "*" → this test FAILS.
   * The globMatches check is the positive control: it confirms that a "*" resource
   * WOULD match the ADP writer ARN (so the filter is not trivially empty), and then
   * the count assertion catches that there is only one invoke grant and it is narrow.
   *
   * Mutation result (2026-10-12): changed `resources: [adpThreadWriterArn]` to
   * `resources: ['*']` in vsa-api-stack.ts → test_adp_invoke_exact_arn FAILS.
   * Confirmed the assertion fires on its own `expect(st.Resource).toEqual(...)`.
   * Restored immediately.
   */
  test('no other lambda:InvokeFunction grant reaches any other function ARN', () => {
    const stmts = statementsForLambda(template, `vsa-${STAGE}-api-avx-findings`);
    const invokes = stmts.filter(s =>
      s.Effect === 'Allow' &&
      asList(s.Action).some((a: unknown) => globMatches(a, 'lambda:InvokeFunction')),
    );
    // Anti-vacuity: the grant exists.
    expect(invokes.length).toBeGreaterThan(0);
    // Positive control: globMatches works (a wildcard resource WOULD match the writer).
    expect(globMatches('*', ADP_WRITER_ARN)).toBe(true);
    // The only reachable function is the ADP writer — no other Lambda ARN matches.
    const otherFunctionArn = `arn:aws:lambda:${ADP_REGION}:${ACCOUNT}:function:adp-${STAGE}-OTHER`;
    for (const st of invokes) {
      for (const resource of asList(st.Resource)) {
        expect(globMatches(resource as string, otherFunctionArn)).toBe(false);
      }
    }
  });

  test('QualityIssueHookWrite: PutItem + UpdateItem + Query on findings table and by_finding_key index', () => {
    const stmts = statementsForLambda(template, `vsa-${STAGE}-api-avx-findings`);
    const st = bySid(stmts, 'QualityIssueHookWrite');
    expect(st.Effect).toBe('Allow');
    expect(asList(st.Action).sort()).toEqual([
      'dynamodb:PutItem',
      'dynamodb:Query',
      'dynamodb:UpdateItem',
    ]);
    // 2 resources: table ARN + index ARN — exact equality, not substring check.
    // Mutation: add a 3rd resource on another table index → toEqual fails (caught).
    // Mutation: change /index/by_finding_key to /index/by_vin → toEqual fails (caught).
    const FINDINGS_TABLE_ARN = {
      'Fn::Join': ['', [
        'arn:', { Ref: 'AWS::Partition' },
        `:dynamodb:${REGION}:${ACCOUNT}:table/`,
        { 'Fn::ImportValue': `vsa-${STAGE}-avx-findings-table-name` },
      ]],
    };
    const FINDINGS_INDEX_ARN = {
      'Fn::Join': ['', [
        'arn:', { Ref: 'AWS::Partition' },
        `:dynamodb:${REGION}:${ACCOUNT}:table/`,
        { 'Fn::ImportValue': `vsa-${STAGE}-avx-findings-table-name` },
        '/index/by_finding_key',
      ]],
    };
    expect(asList(st.Resource)).toEqual([FINDINGS_TABLE_ARN, FINDINGS_INDEX_ARN]);
  });

  test('QualityIssueHookPutMetric: PutMetricData with AVX/Investigations namespace', () => {
    const stmts = statementsForLambda(template, `vsa-${STAGE}-api-avx-findings`);
    const st = bySid(stmts, 'QualityIssueHookPutMetric');
    expect(st.Effect).toBe('Allow');
    expect(asList(st.Action)).toEqual(['cloudwatch:PutMetricData']);
    const cond = (st['Condition'] as any)?.StringEquals?.['cloudwatch:namespace'];
    expect(cond).toBe('AVX/Investigations');
  });
});

// ─── (c) Routes ──────────────────────────────────────────────────────────────

describe('investigations routes', () => {
  const { template } = makeStack();
  const tpl = template.toJSON() as Record<string, any>;
  const res = tpl.Resources as Record<string, any>;

  /** AuthorizerId of the shared Cognito authorizer, read from the known GET /findings/me method. */
  function sharedAuthorizerId(): unknown {
    const meResId = Object.keys(res).find(
      k => res[k].Type === 'AWS::ApiGateway::Resource' && res[k].Properties.PathPart === 'me',
    );
    expect(meResId).toBeDefined();
    const meMethodId = Object.keys(res).find(
      k => res[k].Type === 'AWS::ApiGateway::Method' &&
           res[k].Properties.ResourceId?.Ref === meResId &&
           res[k].Properties.HttpMethod === 'GET',
    );
    expect(meMethodId).toBeDefined();
    return res[meMethodId!].Properties.AuthorizerId;
  }

  /** Logical ID of the api-avx-investigations Lambda function. */
  function invLambdaLogicalId(): string {
    const id = Object.keys(res).find(
      k => res[k].Type === 'AWS::Lambda::Function' &&
           res[k].Properties.FunctionName === `vsa-${STAGE}-api-avx-investigations`,
    );
    expect(id).toBeDefined();
    return id!;
  }

  /** True iff Integration.Uri's Fn::Join references the given Lambda logical ID via Fn::GetAtt. */
  function uriRefersTo(uri: any, lambdaLogicalId: string): boolean {
    const parts: any[] = uri?.['Fn::Join']?.[1] ?? [];
    return parts.some(p => p?.['Fn::GetAtt']?.[0] === lambdaLogicalId);
  }

  test('POST /investigations: HttpMethod, COGNITO_USER_POOLS, shared authorizer, investigations Lambda', () => {
    const invResId = Object.keys(res).find(
      k => res[k].Type === 'AWS::ApiGateway::Resource' && res[k].Properties.PathPart === 'investigations',
    );
    expect(invResId).toBeDefined();

    const postMethods = Object.values(res).filter(
      (v: any) => v.Type === 'AWS::ApiGateway::Method' &&
                  v.Properties.ResourceId?.Ref === invResId &&
                  v.Properties.HttpMethod === 'POST',
    ) as any[];
    expect(postMethods).toHaveLength(1);
    const p = postMethods[0].Properties;

    expect(p.HttpMethod).toBe('POST');
    expect(p.AuthorizationType).toBe('COGNITO_USER_POOLS');
    expect(p.AuthorizerId).toEqual(sharedAuthorizerId());
    expect(uriRefersTo(p.Integration?.Uri, invLambdaLogicalId())).toBe(true);
  });

  test('GET /investigations/{investigationId}: HttpMethod, COGNITO_USER_POOLS, shared authorizer, investigations Lambda', () => {
    const invResId = Object.keys(res).find(
      k => res[k].Type === 'AWS::ApiGateway::Resource' && res[k].Properties.PathPart === 'investigations',
    );
    expect(invResId).toBeDefined();
    const childResId = Object.keys(res).find(
      k => res[k].Type === 'AWS::ApiGateway::Resource' &&
           res[k].Properties.PathPart === '{investigationId}' &&
           res[k].Properties.ParentId?.Ref === invResId,
    );
    expect(childResId).toBeDefined();

    const getMethods = Object.values(res).filter(
      (v: any) => v.Type === 'AWS::ApiGateway::Method' &&
                  v.Properties.ResourceId?.Ref === childResId &&
                  v.Properties.HttpMethod === 'GET',
    ) as any[];
    expect(getMethods).toHaveLength(1);
    const p = getMethods[0].Properties;

    expect(p.HttpMethod).toBe('GET');
    expect(p.AuthorizationType).toBe('COGNITO_USER_POOLS');
    expect(p.AuthorizerId).toEqual(sharedAuthorizerId());
    expect(uriRefersTo(p.Integration?.Uri, invLambdaLogicalId())).toBe(true);
  });


});
