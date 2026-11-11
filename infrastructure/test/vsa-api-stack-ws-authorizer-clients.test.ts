/**
 * The WebSocket authorizer's TENANT_POOL_IDS must name, for each tenant pool, exactly
 * the app client allowed to open a WebSocket: the pool's main AppClient, not its
 * KioskAppClient.
 *
 * The handler (lambdas/api-ws-authorizer/handler.py) accepts a token only if its `aud`
 * is in the pool entry's `clientIds`, and an entry with no `clientIds` accepts nothing.
 * So dropping `clientIds` here would lock every client out on the next stack deploy, and
 * adding the kiosk client would let kiosk tokens connect. Added 2026-10-01
 * (issues/2026-10-01-ws-authorizer-accepts-any-app-client/).
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { VsaApiStack } from '../lib/vsa-api-stack';

const AUTHORIZER_FN = 'vsa-staging-ws-authorizer';

function makeTemplate(): Template {
  const stage = 'staging';
  const app = new cdk.App();
  const env = { account: '123456789012', region: 'us-west-2' };
  const parent = new cdk.Stack(app, 'WsAuthzParent', { env });
  const tbl = (n: string) => dynamodb.Table.fromTableName(parent, `T-${n}`, n);
  const fn = (n: string, arn: string) => lambda.Function.fromFunctionArn(parent, `F-${n}`, arn);

  const stack = new VsaApiStack(app, `vsa-${stage}-api-ws-authz`, {
    env,
    stage,
    cmsStage: stage,
    tenantConfigTable: tbl(`cms-${stage}-tenant-config`),
    classifier: fn('C', `arn:aws:lambda:us-west-2:123456789012:function:vsa-${stage}-triage-classifier`),
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
    acquireReservationHandoffFn: fn('H', `arn:aws:lambda:us-west-2:123456789012:function:vsa-${stage}-acquire-handoff`),
    acquireReservationHandoffFnArnExportName: `vsa-${stage}-acquire-reservation-handoff-fn-arn`,
    acquireCatalogTable: tbl(`vsa-${stage}-acquire-catalog`),
    acquireOrdersTable: tbl(`vsa-${stage}-acquire-orders`),
    acquireLeadsTable: tbl(`vsa-${stage}-acquire-leads`),
    avxFindingsTableNameExportName: `vsa-${stage}-avx-findings-table-name`,
    avxActionsTableNameExportName: `vsa-${stage}-avx-actions-table-name`,
    avxFeedbackTableNameExportName: `vsa-${stage}-avx-feedback-table-name`,
    notificationDevicesTableNameExportName: `vsa-${stage}-notification-devices-table-name`,
    rsaDispatchTable: dynamodb.Table.fromTableArn(
      parent, 'R', `arn:aws:dynamodb:us-west-2:123456789012:table/vsa-${stage}-rsa-dispatch`,
    ),
    rsaDispatchKey: kms.Key.fromKeyArn(parent, 'RK', 'arn:aws:kms:us-west-2:123456789012:key/test-rsa-dispatch-key'),
    rsaPositionMaxAgeSeconds: { literal: '900', parameterName: `/cvx/${stage}/rsa-position-max-age-seconds` },
  });
  return Template.fromStack(stack);
}

/** Flatten an Fn::Join value into its literal parts and the logical ids it Refs. */
function joinParts(value: any): { text: string; refs: string[] } {
  if (typeof value === 'string') return { text: value, refs: [] };
  const [sep, parts] = value['Fn::Join'];
  const refs: string[] = [];
  const text = (parts as any[])
    .map(p => {
      if (typeof p === 'string') return p;
      if (p.Ref) { refs.push(p.Ref); return `<${p.Ref}>`; }
      return '<token>';
    })
    .join(sep);
  return { text, refs };
}

describe('WebSocket authorizer app-client allowlist', () => {
  const t = makeTemplate();
  const fns = t.findResources('AWS::Lambda::Function');
  const authz = Object.values(fns).find((r: any) => r.Properties.FunctionName === AUTHORIZER_FN) as any;
  const clients = t.findResources('AWS::Cognito::UserPoolClient');
  const appClientIds = Object.keys(clients).filter(id => /AppClient/.test(id) && !/Kiosk/.test(id));
  const kioskClientIds = Object.keys(clients).filter(id => /KioskAppClient/.test(id));

  test('the template has both an app client and a kiosk client to choose between', () => {
    expect(authz).toBeDefined();
    expect(appClientIds.length).toBeGreaterThanOrEqual(1);
    expect(kioskClientIds.length).toBeGreaterThanOrEqual(1);
  });

  test('every pool entry carries clientIds naming its AppClient, and never a kiosk client', () => {
    const { text, refs } = joinParts(authz.Properties.Environment.Variables.TENANT_POOL_IDS);
    const entries = text.match(/\{[^}]*\}/g) ?? [];
    expect(entries.length).toBeGreaterThanOrEqual(1);
    for (const entry of entries) {
      expect(entry).toMatch(/"clientIds":\["<[^>]+>"\]/);
    }
    for (const app of appClientIds) expect(refs).toContain(app);
    for (const kiosk of kioskClientIds) expect(refs).not.toContain(kiosk);
  });
});
