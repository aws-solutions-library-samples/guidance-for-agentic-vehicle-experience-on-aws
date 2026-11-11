/**
 * T1.1: Health findings Query grant — vsa-api-stack-health-findings-grant.test.ts
 *
 * Verifies that the imported AgentCore runtime role receives exactly one Allow
 * statement, `dynamodb:Query` only, on the `vsa-{stage}-health-findings` base
 * table ARN (no index, no wildcard, no write actions).
 *
 * Spec: .kiro/specs/2026-10-26-fi-sweep-avx-batch/spec.md (T1.1).
 * Mutations recorded in /tmp/2026-10-26-fi-sweep-avx/T1.1/note.md.
 *
 * IAM surface uses helpers from test/helpers/iam-surface.ts.
 * Resource matching uses the local globMatches / renderArn helpers
 * (same pattern as vsa-tier2-stack-fi-cost.test.ts).
 */

import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { VsaApiStack } from '../lib/vsa-api-stack';
import {
  actionsOf,
  actionCovers,
  IamStatement,
} from './helpers/iam-surface';

// ─── Constants ───────────────────────────────────────────────────────────────

const ACCOUNT = '123456789012';
const REGION  = 'us-west-2';
const STAGE   = 'staging';

const AGENT_CORE_ROLE_NAME = `AmazonBedrockAgentCoreSDKRuntime-${REGION}-${STAGE}`;
// Concrete ARN expected for the health-findings grant resource.
const HF_TABLE_ARN = `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/vsa-${STAGE}-health-findings`;

// ─── Stack fixture ────────────────────────────────────────────────────────────

let stackCount = 0;

function buildTemplate(): Template {
  const id = ++stackCount;
  const app = new cdk.App();
  const env = { account: ACCOUNT, region: REGION };
  const parentStack = new cdk.Stack(app, `Parent${id}`, { env });

  const stubTable = (lid: string, name: string) =>
    dynamodb.Table.fromTableName(parentStack, lid, name);
  const stubFn = (lid: string, arn: string) =>
    lambda.Function.fromFunctionArn(parentStack, lid, arn);

  const stack = new VsaApiStack(app, `vsa-${STAGE}-api-hfgrant-${id}`, {
    env,
    stage: STAGE,
    cmsStage: STAGE,
    tenantConfigTable: stubTable('TC', `cms-${STAGE}-tenant-config`),
    classifier: stubFn('Cls', `arn:aws:lambda:${REGION}:${ACCOUNT}:function:vsa-${STAGE}-triage-classifier`),
    cmsKmsKeyArn:       { literal: `arn:aws:kms:${REGION}:${ACCOUNT}:key/test-key`, parameterName: `/cvx/${STAGE}/cms-kms-key-arn` },
    redisEndpoint:      { literal: 'redis://stub.redis.local:6379', parameterName: `/cvx/${STAGE}/redis-endpoint` },
    connectInstanceArn: { literal: `arn:aws:connect:us-east-1:${ACCOUNT}:instance/test-instance`, parameterName: `/cvx/${STAGE}/connect-instance-arn` },
    dmsApiEndpoint:     { literal: 'https://dms.example.invalid', parameterName: `/cvx/${STAGE}/dms-api-endpoint` },
    cmsApiEndpoint:     { literal: 'https://cms.example.invalid', parameterName: `/cvx/${STAGE}/cms-api-endpoint` },
    vpcId:              'vpc-00000000000000000',
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

  return Template.fromStack(stack);
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

/**
 * Render a CloudFormation resource value to a concrete ARN string.
 * Resolves `Fn::Join` with the test ACCOUNT / REGION / aws partition.
 * Unresolvable parts (e.g. `Fn::ImportValue`) become `*`.
 */
function renderArn(r: unknown): string {
  if (typeof r === 'string') return r;
  const join = (r as any)?.['Fn::Join'];
  if (Array.isArray(join)) {
    return (join[1] as unknown[]).map((p) => {
      if (typeof p === 'string') return p;
      if (p && typeof p === 'object') {
        const ref = (p as any)?.Ref;
        if (ref === 'AWS::Partition') return 'aws';
        if (ref === 'AWS::Region')    return REGION;
        if (ref === 'AWS::AccountId') return ACCOUNT;
      }
      return '*';
    }).join(join[0] as string);
  }
  return '*';
}

/** IAM glob pattern (only `*` and `?`) matches `target`. Non-string resources always return false. */
function globMatches(resource: unknown, target: string): boolean {
  if (typeof resource !== 'string') return false;
  const re = new RegExp(
    '^' + resource
      .replace(/[.+^${}()|[\]\\]/g, '\\$&')
      .replace(/\*/g, '.*')
      .replace(/\?/g, '.') + '$',
  );
  return re.test(target);
}

const asList = (x: unknown): unknown[] => (Array.isArray(x) ? x : [x]);

/**
 * All Allow/Deny statements from `AWS::IAM::Policy` resources whose `Roles`
 * array includes the AgentCore role name.
 *
 * With a concrete env (account+region), the role name is a plain string and
 * CDK serialises it directly into the `Roles` array — no intrinsics.
 */
function agentCoreStatements(template: Template): IamStatement[] {
  const stmts: IamStatement[] = [];
  for (const [, res] of Object.entries(template.findResources('AWS::IAM::Policy'))) {
    const roles: unknown[] = (res as any).Properties?.Roles ?? [];
    if (!roles.some((r) => JSON.stringify(r).includes(AGENT_CORE_ROLE_NAME))) continue;
    const doc = (res as any).Properties?.PolicyDocument;
    stmts.push(...((doc?.Statement ?? []) as IamStatement[]));
  }
  return stmts;
}

// ─── Cached template (avoid re-synthesis) ────────────────────────────────────

let _template: Template | undefined;
function tpl(): Template {
  if (!_template) _template = buildTemplate();
  return _template;
}

// ─── Tests ────────────────────────────────────────────────────────────────────

describe('HealthFindingsQuery grant — AgentCore role', () => {
  /**
   * Anti-vacuity: the agentCoreStatements collector returns at least the existing
   * three grantReadData statements, proving the role name matched.
   */
  test('agentCoreStatements finds the AgentCore policy (anti-vacuity)', () => {
    const stmts = agentCoreStatements(tpl());
    // At least 3 statements from the three existing grantReadData calls.
    expect(stmts.length).toBeGreaterThanOrEqual(3);
  });

  /**
   * Exactly one Allow statement on AgentCore policies covers dynamodb:Query for
   * the health-findings table specifically.
   *
   * Catches: mutant 6 (drop grant) — no such statement exists → length is 0.
   */
  test('exactly one Allow on AgentCore policies covers dynamodb:Query on health-findings', () => {
    const relevant = agentCoreStatements(tpl()).filter(
      (s) =>
        s['Effect'] === 'Allow' &&
        actionsOf(s).some((a) => actionCovers(a, 'dynamodb:Query')) &&
        asList(s['Resource']).some((r) => renderArn(r).includes('health-findings')),
    );
    expect(relevant).toHaveLength(1);
  });

  /**
   * The statement has the expected Sid.
   *
   * Catches: mutant 6 (drop grant) — no Sid found → find returns undefined.
   */
  test('the statement Sid is HealthFindingsQuery', () => {
    const found = agentCoreStatements(tpl()).filter((s) => s['Sid'] === 'HealthFindingsQuery');
    expect(found).toHaveLength(1);
    expect(found[0]['Effect']).toBe('Allow');
  });

  /**
   * Actions are exactly ['dynamodb:Query'] — no star, no write action.
   *
   * Catches:
   *   mutant 4 (actions → dynamodb:*)      — actions contains 'dynamodb:*'
   *   mutant 5 (grantReadWriteData)         — actions includes PutItem/UpdateItem/DeleteItem
   */
  test('actions are exactly [dynamodb:Query]', () => {
    const s = agentCoreStatements(tpl()).find((x) => x['Sid'] === 'HealthFindingsQuery');
    expect(s).toBeDefined();
    expect(asList(s!['Action']).sort()).toEqual(['dynamodb:Query']);
  });

  /**
   * The resource renders to the exact base-table ARN.
   *
   * Catches:
   *   mutant 1 (Resource → table/vsa-*-health-findings) — rendered has `*` in stage
   *   mutant 3 (Resource: '*')                          — renders to '*'
   */
  test('resource renders to the exact health-findings table ARN', () => {
    const s = agentCoreStatements(tpl()).find((x) => x['Sid'] === 'HealthFindingsQuery');
    expect(s).toBeDefined();
    const rendered = asList(s!['Resource']).map(renderArn);
    expect(rendered).toEqual([HF_TABLE_ARN]);
  });

  /**
   * No Allow on the AgentCore policy reaches the health-findings index.
   * Uses globMatches on the rendered resource strings.
   *
   * Only examines statements whose resources mention 'health-findings' to avoid
   * false positives from the existing grantReadData statements (which use
   * Fn::ImportValue table names that render as '*').
   *
   * Catches: mutant 2 (add /index/*) — rendered resource matches the index path.
   */
  test('no Allow on AgentCore policies reaches the health-findings index (globMatches)', () => {
    const hfIndexSample = `${HF_TABLE_ARN}/index/by_fleet`;
    // Collect statements whose resources specifically mention health-findings.
    const hfStmts = agentCoreStatements(tpl()).filter(
      (s) =>
        s['Effect'] === 'Allow' &&
        asList(s['Resource']).some((r) => renderArn(r).includes('health-findings')),
    );
    // Anti-vacuity: there is at least one such statement (the HealthFindingsQuery one).
    expect(hfStmts.length).toBeGreaterThanOrEqual(1);
    // None of those statements' resources matches a health-findings index path.
    for (const s of hfStmts) {
      for (const r of asList(s['Resource']).map(renderArn)) {
        expect(globMatches(r, hfIndexSample)).toBe(false);
      }
    }
  });

  /**
   * No Allow on AgentCore policies covers write actions (PutItem, DeleteItem, Scan)
   * on any statement whose resource is the universal wildcard '*' or explicitly names
   * health-findings.
   *
   * F1.5 S7: the previous filter only checked statements whose rendered resource
   * included the string 'health-findings' (and was not '*').  A new statement with
   * Resource:'*' and dynamodb:Scan would bypass the check, because '*' does not
   * contain 'health-findings'.  This update adds Resource:'*' to the scope.
   *
   * Existing grantReadData statements use Fn::ImportValue resources that render to
   * 'arn:...:table/*' — their resource is not '*' and does not contain 'health-findings',
   * so they are correctly excluded even though globMatches('arn:...:table/*', HF_TABLE_ARN)
   * is true.
   *
   * Catches: mutant 5 (grantReadWriteData → PutItem on exact ARN) and any new statement
   * that adds Scan/PutItem/DeleteItem on Resource:'*'.
   */
  test('no Allow on AgentCore policies covers write actions on a resource that is * or names health-findings', () => {
    const writeActions = ['dynamodb:PutItem', 'dynamodb:DeleteItem', 'dynamodb:Scan'];
    // Include statements with Resource:'*' (universal) OR explicit health-findings naming.
    // Exclude 'arn:...:table/*' (grantReadData breadth) which does not explicitly target
    // health-findings and is not the universal wildcard.
    const targetedStmts = agentCoreStatements(tpl()).filter(
      (s) =>
        s['Effect'] === 'Allow' &&
        asList(s['Resource']).map(renderArn).some(
          (r) => r === '*' || r.includes('health-findings'),
        ),
    );
    // Anti-vacuity: at least the HealthFindingsQuery statement is covered.
    expect(targetedStmts.length).toBeGreaterThanOrEqual(1);
    for (const s of targetedStmts) {
      const coversWrite = writeActions.some((wa) => actionsOf(s).some((a) => actionCovers(a, wa)));
      expect(coversWrite).toBe(false);
    }
  });

  /**
   * globMatches correctness: a wildcard '*' matches any concrete ARN.
   * Confirms the helper is exercised and not vacuous.
   */
  test('globMatches positive control: "*" matches the table ARN', () => {
    expect(globMatches('*', HF_TABLE_ARN)).toBe(true);
  });
});
