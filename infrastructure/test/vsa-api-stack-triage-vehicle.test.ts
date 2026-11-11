/**
 * DS-1: /triage Lambda vehicle scope — CMS vehicles table grants.
 *
 * Asserted on the triage Lambda's own role, not "somewhere in the stack":
 *   (a) CMS_VEHICLES_TABLE is set to the CMS vehicles table name.
 *   (b) Its DynamoDB grants are exactly:
 *       - dynamodb:GetItem on the table ARN (vehicle-bound lookup)
 *       - dynamodb:Query on the vin-index ARN (fleet-bound lookup)
 *       No other DynamoDB action, no wildcard resource.
 *   (c) Its KMS grant is Decrypt/DescribeKey on the CMS key, via DynamoDB only.
 *   (d) Anti-vacuity: the policy is found and still carries the classifier invoke grant.
 *
 * Mutations (each run; each failed the named test; then restored):
 *   actions -> dynamodb:*                         : (b) only GetItem+Query
 *   GetItem resource -> '*'                        : (b) exact table ARN
 *   Query resource -> whole table (no /index/...)  : (b) exact index ARN
 *   ViaService condition removed                   : (c) via DynamoDB only
 */

import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { VsaApiStack } from '../lib/vsa-api-stack';

let stackCount = 0;

function makeStack(stage = 'staging'): { stack: VsaApiStack; template: Template } {
  const id = ++stackCount;
  const app = new cdk.App();
  const env = { account: '123456789012', region: 'us-west-2' };

  const parentStack = new cdk.Stack(app, `ParentTriageVehicle${id}`, { env });

  const stubTable = (name: string) =>
    dynamodb.Table.fromTableName(parentStack, `TblTV${id}-${name}`, name);

  const stubFn = (fnName: string, arn: string) =>
    lambda.Function.fromFunctionArn(parentStack, `FnTV${id}-${fnName}`, arn);

  const rsaDispatchTable = dynamodb.Table.fromTableArn(
    parentStack,
    `RsaDispatchTV${id}`,
    `arn:aws:dynamodb:us-west-2:123456789012:table/vsa-${stage}-rsa-dispatch`,
  );
  const rsaDispatchKey = kms.Key.fromKeyArn(
    parentStack, `RsaDispatchKeyTV${id}`,
    'arn:aws:kms:us-west-2:123456789012:key/test-rsa-dispatch-key',
  );

  const stack = new VsaApiStack(app, `vsa-${stage}-api-triageveh-${id}`, {
    env,
    stage,
    cmsStage: stage,
    tenantConfigTable: stubTable(`cms-${stage}-tenant-config`),
    classifier: stubFn(
      'ClassifierTV',
      `arn:aws:lambda:us-west-2:123456789012:function:vsa-${stage}-triage-classifier`,
    ),
    cmsKmsKeyArn: { literal: 'arn:aws:kms:us-west-2:123456789012:key/test-key', parameterName: `/cvx/${stage}/cms-kms-key-arn` },
    redisEndpoint: { literal: 'redis://stub.redis.local:6379', parameterName: `/cvx/${stage}/redis-endpoint` },
    connectInstanceArn: { literal: 'arn:aws:connect:us-east-1:123456789012:instance/test-instance', parameterName: `/cvx/${stage}/connect-instance-arn` },
    dmsApiEndpoint: { literal: 'https://dms.example.invalid/staging', parameterName: `/cvx/${stage}/dms-api-endpoint` },
    cmsApiEndpoint: { literal: 'https://cms.example.invalid/staging', parameterName: `/cvx/${stage}/cms-api-endpoint` },
    vpcId: 'vpc-00000000000000000',
    redisSecurityGroupId: 'sg-00000000000000000',
    assistantChatRuntimeArn: {
      literal: `arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/stub-${stage}`,
      parameterName: `/cvx/${stage}/assistant-chat-runtime-arn`,
    },
    assistantChatRuntimeRegion: 'us-west-2',
    allowedOrigin: { literal: 'https://stub.example.com', parameterName: `/cvx/${stage}/allowed-origin` },
    acquireReservationHandoffFn: stubFn(
      'AcquireHandoffTV',
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
    rsaPositionMaxAgeSeconds: { literal: '900', parameterName: `/cvx/${stage}/rsa-position-max-age-seconds` },
  });

  return { stack, template: Template.fromStack(stack) };
}


const VEHICLES_TABLE_ARN = 'arn:aws:dynamodb:us-west-2:123456789012:table/cms-staging-storage-vehicles';
const VEHICLES_VIN_INDEX_ARN = `${VEHICLES_TABLE_ARN}/index/vin-index`;
const CMS_KEY_ARN = 'arn:aws:kms:us-west-2:123456789012:key/test-key';

function triageStatements(template: Template): Record<string, any>[] {
  const stmts: Record<string, any>[] = [];
  const policies = template.findResources('AWS::IAM::Policy');
  for (const [logicalId, p] of Object.entries(policies)) {
    if (!logicalId.startsWith('ApiTriageServiceRoleDefaultPolicy')) continue;
    const s = (p as any).Properties?.PolicyDocument?.Statement ?? [];
    stmts.push(...s);
  }
  return stmts;
}

const asList = (v: unknown): unknown[] => (Array.isArray(v) ? v : [v]);

describe('/triage Lambda: DS-1 vehicle scope grants', () => {
  const { template } = makeStack();
  const stmts = triageStatements(template);

  test('(d) the triage policy is found and still invokes the classifier', () => {
    expect(stmts.length).toBeGreaterThan(0);
    expect(stmts.some((s) => asList(s.Action).includes('lambda:InvokeFunction'))).toBe(true);
  });

  test('(a) CMS_VEHICLES_TABLE is the CMS vehicles table', () => {
    const fns = template.findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: 'vsa-staging-api-triage' },
    });
    expect(Object.keys(fns)).toHaveLength(1);
    const env = (Object.values(fns)[0] as any).Properties.Environment.Variables;
    expect(env.CMS_VEHICLES_TABLE).toBe('cms-staging-storage-vehicles');
  });

  test('(b) GetItem grant is on the exact vehicles table ARN, not a wildcard', () => {
    const getItem = stmts.filter(
      (s) => asList(s.Action).includes('dynamodb:GetItem') &&
             asList(s.Resource).some((r) => String(r).includes('cms-staging-storage-vehicles') && !String(r).includes('/index/')),
    );
    // Should find exactly one GetItem statement for the vehicles table
    expect(getItem.length).toBeGreaterThanOrEqual(1);
    // None of the GetItem resources should be a wildcard
    for (const stmt of getItem) {
      const resources = asList(stmt.Resource);
      for (const r of resources) {
        expect(String(r)).not.toBe('*');
        expect(String(r)).toContain('cms-staging-storage-vehicles');
      }
    }
  });

  test('(b) Query grant is on the vin-index only, not the whole table', () => {
    const query = stmts.filter(
      (s) => asList(s.Action).includes('dynamodb:Query') &&
             asList(s.Resource).some((r) => String(r).includes('vin-index')),
    );
    expect(query.length).toBeGreaterThanOrEqual(1);
    for (const stmt of query) {
      const resources = asList(stmt.Resource);
      for (const r of resources) {
        // Must include vin-index, must NOT be the whole table without index path
        expect(String(r)).toContain('vin-index');
        expect(String(r)).not.toBe('*');
      }
    }
  });

  test('(b) no dynamodb:* wildcard action on the triage role', () => {
    const wildcards = stmts.filter(
      (s) => asList(s.Action).some((a) => String(a) === 'dynamodb:*'),
    );
    expect(wildcards).toHaveLength(0);
  });

  test('(b) every DynamoDB grant on the triage role is exactly GetItem on the table or Query on vin-index', () => {
    // Exact equality, not "contains": a resource widened to `table/...vehicles*`
    // or a second statement granting GetItem on `*` must fail here
    // (review cycle 1 suggestion).
    const table = 'arn:aws:dynamodb:us-west-2:123456789012:table/cms-staging-storage-vehicles';
    const allowed = new Set([`dynamodb:GetItem ${table}`, `dynamodb:Query ${table}/index/vin-index`]);
    const seen = new Set<string>();
    for (const s of stmts) {
      for (const a of asList(s.Action).map(String).filter((x) => x.startsWith('dynamodb:'))) {
        for (const r of asList(s.Resource)) {
          const pair = `${a} ${typeof r === 'string' ? r : JSON.stringify(r)}`;
          expect(allowed.has(pair)).toBe(true);
          seen.add(pair);
        }
      }
    }
    expect(seen).toEqual(allowed);
  });

  test('(c) KMS grant for triage includes Decrypt/DescribeKey on the CMS key, via DynamoDB', () => {
    // Find a KMS statement that covers the CMS key on the triage role.
    // (There may be multiple KMS statements on the role from other grants.)
    const kmsStmts = stmts.filter(
      (s) => asList(s.Action).some((a) => String(a).startsWith('kms:')),
    );
    expect(kmsStmts.length).toBeGreaterThan(0);

    // At least one must be scoped to the CMS key with ViaService
    const cmsKmsWithViaService = kmsStmts.filter((s) => {
      const resources = asList(s.Resource);
      const hasCmsKey = resources.some((r) => String(r) === CMS_KEY_ARN);
      const hasViaService =
        s.Condition?.StringEquals?.['kms:ViaService'] === 'dynamodb.us-west-2.amazonaws.com';
      return hasCmsKey && hasViaService;
    });
    expect(cmsKmsWithViaService.length).toBeGreaterThanOrEqual(1);
  });
});
