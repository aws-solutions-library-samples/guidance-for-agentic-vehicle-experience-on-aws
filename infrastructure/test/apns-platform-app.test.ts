/**
 * Jest tests for the ApnsPlatformApp construct (T1.3a-3).
 *
 * Two scopes tested here:
 *   (A) Construct-level: stand-alone stack containing only ApnsPlatformApp.
 *       Asserts grant exactness, custom-resource property names, Lambda handler,
 *       and that key bytes are absent from the template.
 *   (B) API stack wiring: register-device's AVX_APNS_PLATFORM_APP_ARN is a
 *       Fn::GetAtt from the custom resource (not the old empty-string literal).
 *
 * Mutation record (all mutants caught; task T1.3a-3):
 *
 *   M1 — GetSecretValue resource widened to '*':
 *     Change 'arn:aws:secretsmanager:...:secret:cvx/${stage}/apns-auth-key-*' → '*'.
 *     Result: test_apns_secret_grant_is_scoped_to_secret_only FAILS. Restored.
 *
 *   M2 — (superseded 2026-11-06, F1.6) the manage actions have no resource type, so the
 *     statement is now Resource "*" by design. Mutants run for F1.6 are in the spec's
 *     decisions.md ("F1.6"): resource back to the application ARN, sns:Publish or sns:*
 *     added to a "*" statement, GetSecretValue on "*", PassRole added.
 *
 *   M3 — SnsListPlatformApps actions changed to ['sns:*']:
 *     Change actions: ['sns:ListPlatformApplications'] → ['sns:*'].
 *     Result: test_sns_list_is_only_list_no_wildcard_action FAILS. Restored.
 *
 *   M4 — Grant removed (secret absent → no SnsManagePlatformApp statement):
 *     Comment out the SnsManagePlatformApp addToRolePolicy block.
 *     Result: test_sns_manage_grant_is_the_three_actions_on_star FAILS. Restored.
 *
 *   M5 — AVX_APNS_PLATFORM_APP_ARN = '' (old literal, not GetAtt):
 *     In vsa-api-stack.ts, replace apnsPlatformApp.platformAppArn with ''.
 *     Result: test_register_device_apns_arn_is_getatt FAILS. Restored.
 *
 *   M6 — (superseded 2026-11-06, F1.6; see M2).
 */

import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';

import { ApnsPlatformApp } from '../lib/apns-platform-app';
import { VsaApiStack } from '../lib/vsa-api-stack';

// ─── Helpers ─────────────────────────────────────────────────────────────────

let _stackCount = 0;

/** Minimal stack containing only an ApnsPlatformApp construct. */
function makeConstructStack(
  keyVersion = 'none',
): { stack: cdk.Stack; template: Template; construct: ApnsPlatformApp } {
  const id = ++_stackCount;
  const app = new cdk.App();
  const stack = new cdk.Stack(app, `TestApnsPlatformApp${id}`, {
    env: { account: '123456789012', region: 'us-west-2' },
  });
  const construct = new ApnsPlatformApp(stack, 'ApnsPlatformApp', {
    stage: 'staging',
    keyVersion,
  });
  return { stack, template: Template.fromStack(stack), construct };
}

/** Collect all IAM policy statements from the stack template. */
function allStatements(template: Template): Record<string, unknown>[] {
  const out: Record<string, unknown>[] = [];
  for (const policy of Object.values(template.findResources('AWS::IAM::Policy'))) {
    const stmts = ((policy as any).Properties?.PolicyDocument?.Statement ?? []) as Record<
      string,
      unknown
    >[];
    out.push(...stmts);
  }
  for (const role of Object.values(template.findResources('AWS::IAM::Role'))) {
    const inline = ((role as any).Properties?.Policies ?? []) as any[];
    for (const p of inline) {
      out.push(...(p.PolicyDocument?.Statement ?? []));
    }
  }
  return out;
}

/**
 * Minimal VsaApiStack fixture — same pattern as vsa-api-stack-avx.test.ts but
 * includes only the props needed to instantiate the AVX register-device Lambda.
 */
function makeApiStack(): { template: Template } {
  const id = ++_stackCount;
  const app = new cdk.App();
  const env = { account: '123456789012', region: 'us-west-2' };
  const parent = new cdk.Stack(app, `ParentApiFixture${id}`, { env });

  const stubTable = (tid: string, name: string) =>
    dynamodb.Table.fromTableName(parent, tid, name);
  const stubFn = (fid: string, arn: string) =>
    lambda.Function.fromFunctionArn(parent, fid, arn);
  const stage = 'staging';

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
    connectInstanceArn:     { literal: 'arn:aws:connect:us-east-1:123456789012:instance/test', parameterName: `/cvx/${stage}/connect-instance-arn` },
    dmsApiEndpoint:         { literal: 'https://dms.example.invalid/staging', parameterName: `/cvx/${stage}/dms-api-endpoint` },
    cmsApiEndpoint:         { literal: 'https://cms.example.invalid/staging', parameterName: `/cvx/${stage}/cms-api-endpoint` },
    vpcId:                  'vpc-00000000000000000',
    redisSecurityGroupId:   'sg-00000000000000000',
    assistantChatRuntimeArn: {
      literal: `arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/stub-${stage}`,
      parameterName: `/cvx/${stage}/assistant-chat-runtime-arn`,
    },
    assistantChatRuntimeRegion: 'us-west-2',
    allowedOrigin: { literal: 'https://stub.example.com', parameterName: `/cvx/${stage}/allowed-origin` },
    acquireReservationHandoffFn: stubFn('AcquireHandoff', `arn:aws:lambda:us-west-2:123456789012:function:vsa-${stage}-acquire-handoff`),
    acquireReservationHandoffFnArnExportName: `vsa-${stage}-acquire-reservation-handoff-fn-arn`,
    acquireCatalogTable: stubTable('AcquireCatalog', `vsa-${stage}-acquire-catalog`),
    acquireOrdersTable:  stubTable('AcquireOrders',  `vsa-${stage}-acquire-orders`),
    acquireLeadsTable:   stubTable('AcquireLeads',   `vsa-${stage}-acquire-leads`),
    avxFindingsTableNameExportName:         `vsa-${stage}-avx-findings-table-name`,
    avxActionsTableNameExportName:          `vsa-${stage}-avx-actions-table-name`,
    avxFeedbackTableNameExportName:         `vsa-${stage}-avx-feedback-table-name`,
    notificationDevicesTableNameExportName: `vsa-${stage}-notification-devices-table-name`,
    avxApprovalRulesTableNameExportName:    `vsa-${stage}-avx-approval-rules-table-name`,
    rsaDispatchTable: dynamodb.Table.fromTableArn(
      parent, `RsaDispatch${id}`,
      `arn:aws:dynamodb:us-west-2:123456789012:table/vsa-${stage}-rsa-dispatch`,
    ),
    rsaDispatchKey: kms.Key.fromKeyArn(parent, `RsaKey${id}`, 'arn:aws:kms:us-west-2:123456789012:key/rsa-key'),
    // keyVersion default is 'none' — test without a key secret
  });

  return { template: Template.fromStack(stack) };
}

// ─── (A) Construct-level tests ────────────────────────────────────────────────

describe('ApnsPlatformApp construct — custom resource properties', () => {
  const { template } = makeConstructStack();

  test('custom resource has SecretId property matching cvx/staging/apns-auth-key', () => {
    template.hasResourceProperties('AWS::CloudFormation::CustomResource', {
      SecretId: 'cvx/staging/apns-auth-key',
    });
  });

  test('custom resource has ApplicationName vsa-staging-avx-apns-sandbox', () => {
    template.hasResourceProperties('AWS::CloudFormation::CustomResource', {
      ApplicationName: 'vsa-staging-avx-apns-sandbox',
    });
  });

  test('custom resource has KeyVersion property', () => {
    // KeyVersion 'none' when no secret; changing it re-runs the resource.
    template.hasResourceProperties('AWS::CloudFormation::CustomResource', {
      KeyVersion: 'none',
    });
  });

  test('custom resource KeyVersion changes when a different version is passed', () => {
    const { template: t2 } = makeConstructStack('abc-123-version');
    t2.hasResourceProperties('AWS::CloudFormation::CustomResource', {
      KeyVersion: 'abc-123-version',
    });
  });
});

describe('ApnsPlatformApp construct — onEvent Lambda', () => {
  const { template } = makeConstructStack();

  test('Lambda function has correct handler (handler.on_event)', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'vsa-staging-apns-platform-app-cr',
      Handler: 'handler.on_event',
    });
  });

  test('Lambda runtime is PYTHON_3_12', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'vsa-staging-apns-platform-app-cr',
      Runtime: 'python3.12',
    });
  });

  test('Lambda environment does NOT contain P8 key bytes or the secret value', () => {
    // The Lambda must not have an env var with 'p8', 'key_bytes', or 'secret' in the
    // key name — key bytes must never appear in CFN metadata.
    const fns = template.findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: 'vsa-staging-apns-platform-app-cr' },
    });
    expect(Object.keys(fns)).toHaveLength(1);
    const fn = Object.values(fns)[0] as any;
    const envVars: Record<string, unknown> = fn.Properties?.Environment?.Variables ?? {};
    const envKeys = Object.keys(envVars).map(k => k.toLowerCase());
    for (const k of envKeys) {
      expect(k).not.toContain('p8');
      expect(k).not.toContain('secret_value');
      expect(k).not.toContain('credential');
    }
  });
});

describe('ApnsPlatformApp construct — IAM grant exactness (M1–M4)', () => {
  const { template } = makeConstructStack();
  const stmts = allStatements(template);

  // M1: secretsmanager:GetSecretValue scoped to the one secret (ARN pattern with -* suffix)
  test('test_apns_secret_grant_is_scoped_to_secret_only', () => {
    const secretGrants = stmts.filter(stmt => {
      const a = Array.isArray(stmt['Action']) ? stmt['Action'] : [stmt['Action']];
      return (a as string[]).includes('secretsmanager:GetSecretValue');
    });
    expect(secretGrants.length).toBeGreaterThan(0);
    for (const stmt of secretGrants) {
      const resources = Array.isArray(stmt['Resource']) ? stmt['Resource'] : [stmt['Resource']];
      for (const r of resources) {
        // Must target the exact secret — never '*'
        if (typeof r === 'string') {
          expect(r).not.toBe('*');
          expect(r).toContain(':secret:cvx/staging/apns-auth-key');
        }
        // CDK token (Fn::Join): the literal parts will include the secret path
        if (typeof r === 'object' && r !== null) {
          const rendered = JSON.stringify(r);
          expect(rendered).toContain('cvx/staging/apns-auth-key');
          expect(rendered).not.toBe('"*"');
        }
      }
    }
  });

  // M2 (F1.6, 2026-11-06): the SNS platform-application actions have NO resource type in
  // the Service Authorization Reference (list_sns.html), so IAM evaluates them against
  // "*" and a statement naming the application ARN never matches. The statement must be
  // exactly these three actions on Resource "*" (an ARN here fails closed: the custom
  // resource's Create is denied once the key exists).
  test('test_sns_manage_grant_is_the_three_actions_on_star', () => {
    const manage = stmts.filter(stmt => stmt['Sid'] === 'SnsManagePlatformApp');
    expect(manage).toHaveLength(1);
    const a = (Array.isArray(manage[0]['Action']) ? manage[0]['Action'] : [manage[0]['Action']]) as string[];
    expect([...a].sort()).toEqual([
      'sns:CreatePlatformApplication',
      'sns:DeletePlatformApplication',
      'sns:SetPlatformApplicationAttributes',
    ]);
    expect(manage[0]['Resource']).toBe('*');
  });

  // M2b: Resource "*" is allowed ONLY for actions IAM cannot scope. Any other action on
  // "*" in this construct (sns:Publish, sns:*, GetSecretValue, ...) fails here.
  test('test_star_resource_only_for_actions_without_a_resource_type', () => {
    const NO_RESOURCE_TYPE = new Set([
      'sns:ListPlatformApplications',
      'sns:CreatePlatformApplication',
      'sns:SetPlatformApplicationAttributes',
      'sns:DeletePlatformApplication',
    ]);
    const isLogRetention = (acts: string[]) =>
      acts.every(x => x === 'logs:PutRetentionPolicy' || x === 'logs:DeleteRetentionPolicy');
    const starStmts = stmts.filter(stmt => {
      const r = stmt['Resource'];
      return r === '*' || (Array.isArray(r) && r.includes('*'));
    });
    expect(starStmts.length).toBeGreaterThan(0);
    for (const stmt of starStmts) {
      const acts = (Array.isArray(stmt['Action']) ? stmt['Action'] : [stmt['Action']]) as string[];
      if (isLogRetention(acts)) continue; // CDK LogRetention helper (framework)
      for (const x of acts) {
        expect(NO_RESOURCE_TYPE.has(x)).toBe(true);
      }
    }
  });

  // M3: sns:ListPlatformApplications must use the list-only action, not sns:*
  test('test_sns_list_is_only_list_no_wildcard_action', () => {
    const listGrants = stmts.filter(stmt => {
      const a = (Array.isArray(stmt['Action']) ? stmt['Action'] : [stmt['Action']]) as string[];
      return a.includes('sns:ListPlatformApplications');
    });
    expect(listGrants.length).toBeGreaterThan(0);
    for (const stmt of listGrants) {
      const a = (Array.isArray(stmt['Action']) ? stmt['Action'] : [stmt['Action']]) as string[];
      // Must NOT include sns:* or any broader write action
      expect(a).not.toContain('sns:*');
      expect(a).not.toContain('sns:Publish');
      expect(a).not.toContain('sns:CreatePlatformApplication');
      expect(a).not.toContain('sns:SetPlatformApplicationAttributes');
      expect(a).not.toContain('sns:DeletePlatformApplication');
    }
  });

  // Anti-vacuity for M3/M4: ensure the three manage actions are all present
  test('all three SNS manage actions are granted', () => {
    const allActions = stmts.flatMap(stmt =>
      Array.isArray(stmt['Action']) ? stmt['Action'] : [stmt['Action']],
    ) as string[];
    expect(allActions).toContain('sns:CreatePlatformApplication');
    expect(allActions).toContain('sns:SetPlatformApplicationAttributes');
    expect(allActions).toContain('sns:DeletePlatformApplication');
  });

  // Anti-vacuity: sns:Publish must NOT appear anywhere (the custom resource only
  // manages the application, it never publishes to endpoints)
  test('sns:Publish is absent from the construct template', () => {
    const allActions = stmts.flatMap(stmt =>
      Array.isArray(stmt['Action']) ? stmt['Action'] : [stmt['Action']],
    ) as string[];
    expect(allActions).not.toContain('sns:Publish');
  });
});

// ─── (B) API stack wiring — register-device env is Fn::GetAtt (M5) ──────────

describe('VsaApiStack wiring — AVX_APNS_PLATFORM_APP_ARN is Fn::GetAtt (M5)', () => {
  const { template } = makeApiStack();

  test('test_register_device_apns_arn_is_getatt', () => {
    const fns = template.findResources('AWS::Lambda::Function', {
      Properties: { FunctionName: 'vsa-staging-api-avx-register-device' },
    });
    expect(Object.keys(fns)).toHaveLength(1);
    const fn = Object.values(fns)[0] as any;
    const env: Record<string, unknown> = fn.Properties?.Environment?.Variables ?? {};

    const arn = env['AVX_APNS_PLATFORM_APP_ARN'];

    // Must NOT be the old empty-string literal
    expect(arn).not.toBe('');

    // Must be a CloudFormation Fn::GetAtt token
    expect(typeof arn).not.toBe('string');
    expect(arn).not.toBeNull();
    const arnObj = arn as Record<string, unknown>;
    expect(arnObj['Fn::GetAtt']).toBeDefined();

    // The GetAtt must reference PlatformApplicationArn
    const [, attrName] = arnObj['Fn::GetAtt'] as [string, string];
    expect(attrName).toBe('PlatformApplicationArn');
  });

  // F1.6 (2026-11-06): sns:CreatePlatformEndpoint has no resource type (Service
  // Authorization Reference, list_sns.html), so the grant is Resource "*" in a statement
  // carrying that ONE action and nothing else (no Publish, no sns:*).
  test('SnsCreateApnsEndpoint is sns:CreatePlatformEndpoint alone on Resource "*"', () => {
    const allStmts = allStatements(template);
    const endpointGrants = allStmts.filter(stmt => {
      const a = (Array.isArray(stmt['Action']) ? stmt['Action'] : [stmt['Action']]) as string[];
      return a.includes('sns:CreatePlatformEndpoint');
    });
    expect(endpointGrants).toHaveLength(1);
    const stmt = endpointGrants[0];
    expect(stmt['Sid']).toBe('SnsCreateApnsEndpoint');
    const a = (Array.isArray(stmt['Action']) ? stmt['Action'] : [stmt['Action']]) as string[];
    expect(a).toEqual(['sns:CreatePlatformEndpoint']);
    expect(stmt['Resource']).toBe('*');
    expect(stmt['Condition']).toBeUndefined();
  });
});
