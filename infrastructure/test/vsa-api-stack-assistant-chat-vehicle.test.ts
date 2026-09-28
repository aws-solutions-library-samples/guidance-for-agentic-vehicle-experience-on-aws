/**
 * Fix Group 18 (spec 2026-09-23-cvx-escalation-recommend-not-act, F18.2):
 * the /assistant/chat Lambda reads one row of the CMS vehicles table to turn a
 * caller's verified `custom:vehicleId` claim into a VIN.
 *
 * Asserted on the chat Lambda's own role and function, not "somewhere in the stack":
 *   (a) CMS_VEHICLES_TABLE is set to the CMS vehicles table name.
 *   (b) Its only DynamoDB grant is dynamodb:GetItem on that table's ARN exactly
 *       (no other action, no index, no wildcard).
 *   (c) Its KMS grant is Decrypt/DescribeKey on the CMS key, only via DynamoDB.
 *   (d) Anti-vacuity: the policy is found and still carries the runtime invoke grant.
 *
 * Mutations (each run 2026-09-28, each failed the named test, then restored):
 *   actions -> dynamodb:*                 : (b) only GetItem
 *   resources -> '*'                      : (b) exact table ARN
 *   ViaService condition removed          : (c) via DynamoDB only
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

  const stack = new VsaApiStack(app, `vsa-${stage}-api-chatveh-${id}`, {
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


const VEHICLES_ARN = 'arn:aws:dynamodb:us-west-2:123456789012:table/cms-staging-storage-vehicles';
const CMS_KEY_ARN = 'arn:aws:kms:us-west-2:123456789012:key/test-key';

function chatStatements(template: Template): Record<string, any>[] {
  const stmts: Record<string, any>[] = [];
  const policies = template.findResources('AWS::IAM::Policy');
  for (const [logicalId, p] of Object.entries(policies)) {
    if (!logicalId.startsWith('ApiAssistantChatServiceRoleDefaultPolicy')) continue;
    const s = (p as any).Properties?.PolicyDocument?.Statement ?? [];
    stmts.push(...s);
  }
  return stmts;
}

const asList = (v: unknown): unknown[] => (Array.isArray(v) ? v : [v]);

describe('/assistant/chat Lambda: claim-vehicle VIN lookup', () => {
  const { template } = makeStack();
  const stmts = chatStatements(template);

  test('(d) the chat policy is found and still invokes the runtime', () => {
    expect(stmts.length).toBeGreaterThan(0);
    expect(stmts.some((s) => asList(s.Action).includes('bedrock-agentcore:InvokeAgentRuntime'))).toBe(true);
  });

  test('(a) CMS_VEHICLES_TABLE is the CMS vehicles table', () => {
    const fns = template.findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: 'vsa-staging-api-assistant-chat' },
    });
    expect(Object.keys(fns)).toHaveLength(1);
    const env = (Object.values(fns)[0] as any).Properties.Environment.Variables;
    expect(env.CMS_VEHICLES_TABLE).toBe('cms-staging-storage-vehicles');
  });

  test('(b) the only DynamoDB grant is GetItem on the vehicles table ARN', () => {
    const ddb = stmts.filter((s) => asList(s.Action).some((a) => String(a).startsWith('dynamodb:')));
    expect(ddb).toHaveLength(1);
    expect(asList(ddb[0].Action)).toEqual(['dynamodb:GetItem']);
    expect(asList(ddb[0].Resource)).toEqual([VEHICLES_ARN]);
    expect(ddb[0].Effect).toBe('Allow');
  });

  test('(c) KMS is Decrypt/DescribeKey on the CMS key, via DynamoDB only', () => {
    const k = stmts.filter((s) => asList(s.Action).some((a) => String(a).startsWith('kms:')));
    expect(k).toHaveLength(1);
    expect([...asList(k[0].Action)].sort()).toEqual(['kms:Decrypt', 'kms:DescribeKey']);
    expect(asList(k[0].Resource)).toEqual([CMS_KEY_ARN]);
    expect(k[0].Condition).toEqual({
      StringEquals: { 'kms:ViaService': 'dynamodb.us-west-2.amazonaws.com' },
    });
  });
});
