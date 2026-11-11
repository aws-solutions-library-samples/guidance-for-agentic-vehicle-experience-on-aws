/**
 * Guard: every CDK stack in vsa.ts must carry a CloudFormation Description
 * that contains the solution ID `(SO9676)`.
 *
 * WHY THIS EXISTS
 * ---------------
 * AWS Solutions Library accelerators embed the SO number in every stack
 * description so customers and support teams can identify the solution in the
 * CloudFormation console.  Without it, a deployed stack looks anonymous.
 *
 * TWO COMPLEMENTARY LAYERS
 * ------------------------
 * 1. Source-level describe block (below): reads `bin/vsa.ts` as text and
 *    asserts every `new <Stack>(` call passes a `description` argument that
 *    contains `(SO9676)`.  This is the completeness guard — it fails if a
 *    description is removed from `bin/vsa.ts` even before any CDK synth.
 *
 * 2. Per-class tests (below): synthesise each stack class in isolation and
 *    assert the CloudFormation template's `Description` field contains
 *    `(SO9676)`.  This proves that the stack class FORWARDS the `description`
 *    prop to CloudFormation (not just that `bin/vsa.ts` passes it in).
 *
 * MUTATION GUIDE (source-level tests)
 * ------------------------------------
 * Delete the `description:` line from ONE stack instantiation in
 * infrastructure/bin/vsa.ts, then run:
 *
 *   cd infrastructure && npx jest vsa-stack-descriptions --no-coverage
 *
 * Expected: the source-level test for that constructor FAILS — "expected call
 * args for <Stack> to contain a description matching /(SO9676)/".
 *
 * Delete ALL 8 `description:` lines, then re-run: all 8 source tests FAIL.
 *
 * These two mutations are explicitly required by the review; run them before
 * marking the critical finding resolved.
 *
 * Run:
 *   cd infrastructure && npx jest vsa-stack-descriptions --no-coverage
 */

import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { Template } from 'aws-cdk-lib/assertions';
import { AcquireStack } from '../lib/vsa-acquire-stack';
import { VsaFoundationStack } from '../lib/vsa-foundation-stack';
import { VsaAnalyticsStack } from '../lib/vsa-analytics-stack';
import { VsaRsaStack } from '../lib/vsa-rsa-stack';
import { VsaCoreStack } from '../lib/vsa-core-stack';
import { TriageClassifierStack } from '../lib/triage-classifier-stack';
import { VsaApiStack } from '../lib/vsa-api-stack';
import { VsaTier2Stack } from '../lib/vsa-tier2-stack';
import { VsaFindingsExportStack } from '../lib/vsa-findings-export-stack';
import { TEST_CMS_FI_INTERNAL_API_ENDPOINT } from './helpers/cms-fi-internal-endpoint';
import { TEST_DMS_FI_INTERNAL_API_ENDPOINT } from './helpers/dms-fi-internal-endpoint';

// ---------------------------------------------------------------------------
// Source-level helpers (same pattern as vsa-rsa-cmk-app-wiring.test.ts)
// ---------------------------------------------------------------------------

const BIN_SRC = fs.readFileSync(path.join(__dirname, '..', 'bin', 'vsa.ts'), 'utf-8');

/** The argument text of the single `new <ctor>(` call, paren-matched. */
function callArgs(src: string, ctor: string): string {
  const needle = `new ${ctor}(`;
  const start = src.indexOf(needle);
  expect(start).toBeGreaterThanOrEqual(0);
  let depth = 0;
  for (let i = src.indexOf('(', start); i < src.length; i++) {
    if (src[i] === '(') depth++;
    if (src[i] === ')' && --depth === 0) return src.slice(start, i + 1);
  }
  throw new Error(`unbalanced ${needle} call`);
}

/** Strip // and /* ... *\/ comments so a commented-out prop does not count. */
function code(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');
}

// ---------------------------------------------------------------------------
// Source-level guard: every stack call in bin/vsa.ts must pass (SO9676)
// ---------------------------------------------------------------------------

const STACK_CTORS = [
  'AcquireStack',
  'VsaCoreStack',
  'VsaFoundationStack',
  'VsaAnalyticsStack',
  'TriageClassifierStack',
  'VsaRsaStack',
  'VsaApiStack',
  'VsaTier2Stack',
  'VsaFindingsExportStack',
] as const;

describe('bin/vsa.ts source guard — every stack call passes a (SO9676) description', () => {
  for (const ctor of STACK_CTORS) {
    test(`new ${ctor}(...) in bin/vsa.ts includes description: '...(SO9676)...'`, () => {
      const args = code(callArgs(BIN_SRC, ctor));
      // Must have a description: key...
      expect(args).toMatch(/\bdescription\s*:/);
      // ...and its value must contain (SO9676)
      expect(args).toMatch(/\(SO9676\)/);
    });
  }
});

const SO_NUMBER = '(SO9676)';
const ACCOUNT = '123456789012';
const REGION = 'us-west-2';
const STAGE = 'test';
const ENV = { account: ACCOUNT, region: REGION };

// ---------------------------------------------------------------------------
// Per-class CDK helpers
// ---------------------------------------------------------------------------

/** Assert the synthesised template's top-level Description contains SO_NUMBER.
 *
 * These per-class tests prove that each stack class FORWARDS a `description`
 * prop to the CloudFormation template.  They are NOT the completeness guard
 * (that role belongs to the source-level describe block above, which reads
 * bin/vsa.ts directly).
 */
function assertDescription(stack: cdk.Stack, stackLabel: string): void {
  const desc: string | undefined = Template.fromStack(stack).toJSON().Description;
  expect(desc).toBeDefined();
  expect(desc).toContain(SO_NUMBER);
  // Belt-and-braces: also confirm the guidance name is present.
  expect(desc).toContain('Guidance for Agentic Vehicle Experience');
}

let idSeq = 0;
function nextId(name: string): string {
  return `${name}-${++idSeq}`;
}

/** Build a stub DynamoDB Table for prop wiring — no real table is created. */
function stubTable(parent: cdk.Stack, logicalId: string, tableName: string) {
  return dynamodb.Table.fromTableName(parent, logicalId, tableName);
}

/** Build a stub Lambda Function for prop wiring. */
function stubFn(parent: cdk.Stack, logicalId: string) {
  return lambda.Function.fromFunctionArn(
    parent,
    logicalId,
    `arn:aws:lambda:${REGION}:${ACCOUNT}:function/stub-${logicalId}`,
  );
}

/** Build a stub KMS Key for prop wiring. */
function stubKey(parent: cdk.Stack, logicalId: string) {
  return kms.Key.fromKeyArn(
    parent,
    logicalId,
    `arn:aws:kms:${REGION}:${ACCOUNT}:key/stub-${logicalId}`,
  );
}

// ---------------------------------------------------------------------------
// AcquireStack
// ---------------------------------------------------------------------------

test('acquire stack template Description contains (SO9676)', () => {
  const app = new cdk.App();
  const stack = new AcquireStack(app, nextId('vsa-test-acquire'), {
    env: ENV,
    stage: STAGE,
    description: `Guidance for Agentic Vehicle Experience ${SO_NUMBER} - Acquire`,
  });
  assertDescription(stack, 'AcquireStack');
});

// ---------------------------------------------------------------------------
// VsaCoreStack
// ---------------------------------------------------------------------------

test('core stack template Description contains (SO9676)', () => {
  const app = new cdk.App();

  // CoreStack depends on tables from AcquireStack — use an AcquireStack for
  // real table constructs rather than stub imports.
  const acquireParent = new AcquireStack(app, nextId('acquire-for-core'), {
    env: ENV,
    stage: STAGE,
  });

  const stack = new VsaCoreStack(app, nextId('vsa-test-core'), {
    env: ENV,
    stage: STAGE,
    cmsStage: STAGE,
    cmsKmsKeyArn: { literal: `arn:aws:kms:${REGION}:${ACCOUNT}:key/stub`, parameterName: `/cvx/${STAGE}/cms-kms-key-arn` },
    memoryKmsKeyArn: `arn:aws:kms:${REGION}:${ACCOUNT}:key/stub-mem`,
    vsaAcquireCatalog: acquireParent.vsaAcquireCatalog,
    vsaAcquireOrders: acquireParent.vsaAcquireOrders,
    vsaAcquireLeads: acquireParent.vsaAcquireLeads,
    description: `Guidance for Agentic Vehicle Experience ${SO_NUMBER} - Core`,
  });
  assertDescription(stack, 'VsaCoreStack');
});

// ---------------------------------------------------------------------------
// VsaFoundationStack
// ---------------------------------------------------------------------------

test('foundation stack template Description contains (SO9676)', () => {
  const app = new cdk.App();
  const stack = new VsaFoundationStack(app, nextId('vsa-test-foundation'), {
    env: ENV,
    stage: STAGE,
    description: `Guidance for Agentic Vehicle Experience ${SO_NUMBER} - Foundation`,
  });
  assertDescription(stack, 'VsaFoundationStack');
});

// ---------------------------------------------------------------------------
// VsaAnalyticsStack
// ---------------------------------------------------------------------------

test('analytics stack template Description contains (SO9676)', () => {
  const app = new cdk.App();
  const stack = new VsaAnalyticsStack(app, nextId('vsa-test-analytics'), {
    env: { account: ACCOUNT, region: 'us-east-1' },
    stage: STAGE,
    agentCoreRoleName: `AmazonBedrockAgentCoreSDKRuntime-${REGION}-${STAGE}`,
    tier2RoleName: `vsa-${STAGE}-tier2-scheduler-${REGION}`,
    description: `Guidance for Agentic Vehicle Experience ${SO_NUMBER} - Analytics`,
  });
  assertDescription(stack, 'VsaAnalyticsStack');
});

// ---------------------------------------------------------------------------
// TriageClassifierStack
// ---------------------------------------------------------------------------

test('triage classifier stack template Description contains (SO9676)', () => {
  const app = new cdk.App();

  // TriageClassifierStack needs tenantConfigTable and triageDecisionsTable.
  const parentStack = new cdk.Stack(app, nextId('parent-classifier'), { env: ENV });
  const tenantTable = stubTable(parentStack, 'TenantCfg', `cms-${STAGE}-tenant-config`);
  const triageTable = stubTable(parentStack, 'TriageDec', `vsa-${STAGE}-triage-decisions`);

  const stack = new TriageClassifierStack(app, nextId('vsa-test-triage'), {
    env: ENV,
    stage: STAGE,
    cmsStage: STAGE,
    cmsKmsKeyArn: { literal: `arn:aws:kms:${REGION}:${ACCOUNT}:key/stub`, parameterName: `/cvx/${STAGE}/cms-kms-key-arn` },
    tenantConfigTable: tenantTable,
    triageDecisionsTable: triageTable,
    description: `Guidance for Agentic Vehicle Experience ${SO_NUMBER} - Triage Classifier`,
  });
  assertDescription(stack, 'TriageClassifierStack');
});

// ---------------------------------------------------------------------------
// VsaRsaStack
// ---------------------------------------------------------------------------

test('rsa stack template Description contains (SO9676)', () => {
  const app = new cdk.App();
  const stack = new VsaRsaStack(app, nextId('vsa-test-rsa'), {
    env: ENV,
    stage: STAGE,
    rsaAlarmEmail: {
      literal: '',
      parameterName: `/cvx/${STAGE}/rsa-alarm-email`,
    },
    description: `Guidance for Agentic Vehicle Experience ${SO_NUMBER} - RSA`,
  });
  assertDescription(stack, 'VsaRsaStack');
});

// ---------------------------------------------------------------------------
// VsaApiStack
// ---------------------------------------------------------------------------

test('api stack template Description contains (SO9676)', () => {
  const app = new cdk.App();
  const parentStack = new cdk.Stack(app, nextId('parent-api'), { env: ENV });

  const tenantTable   = stubTable(parentStack, 'TenantCfg-api',  `cms-${STAGE}-tenant-config`);
  const catalogTable  = stubTable(parentStack, 'AcqCatalog-api',  `vsa-${STAGE}-acquire-catalog`);
  const ordersTable   = stubTable(parentStack, 'AcqOrders-api',   `vsa-${STAGE}-acquire-orders`);
  const leadsTable    = stubTable(parentStack, 'AcqLeads-api',    `vsa-${STAGE}-acquire-leads`);
  const classifierFn  = stubFn(parentStack, 'Classifier-api');
  const handoffFn     = stubFn(parentStack, 'Handoff-api');
  const rsaTable      = dynamodb.Table.fromTableArn(
    parentStack, 'RsaDisp-api',
    `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/vsa-${STAGE}-rsa-dispatch`,
  );
  const rsaKey        = stubKey(parentStack, 'RsaKey-api');

  const stack = new VsaApiStack(app, nextId('vsa-test-api'), {
    env: ENV,
    stage: STAGE,
    cmsStage: STAGE,
    tenantConfigTable: tenantTable,
    classifier: classifierFn,
    cmsKmsKeyArn:    { literal: `arn:aws:kms:${REGION}:${ACCOUNT}:key/stub`, parameterName: `/cvx/${STAGE}/cms-kms-key-arn` },
    redisEndpoint:   { literal: 'stub.cache.local:6379', parameterName: `/cvx/${STAGE}/redis-endpoint` },
    dmsApiEndpoint:  { literal: 'https://dms.example.invalid/test', parameterName: `/cvx/${STAGE}/dms-api-endpoint` },
    cmsApiEndpoint:  { literal: 'https://cms.example.invalid/test', parameterName: `/cvx/${STAGE}/cms-api-endpoint` },
    connectInstanceArn: { literal: `arn:aws:connect:us-east-1:${ACCOUNT}:instance/stub`, parameterName: `/cvx/${STAGE}/connect-instance-arn` },
    vpcId:           'vpc-00000000000000000',
    redisSecurityGroupId: 'sg-00000000000000000',
    assistantChatRuntimeArn: { literal: `arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT}:runtime/stub`, parameterName: `/cvx/${STAGE}/assistant-chat-runtime-arn` },
    assistantChatRuntimeRegion: REGION,
    allowedOrigin: { literal: 'https://example.invalid', parameterName: `/cvx/${STAGE}/allowed-origin` },
    acquireReservationHandoffFn: handoffFn,
    acquireReservationHandoffFnArnExportName: `vsa-${STAGE}-reservation-handoff-arn`,
    acquireCatalogTable: catalogTable,
    acquireOrdersTable: ordersTable,
    acquireLeadsTable: leadsTable,
    avxFindingsTableNameExportName:         `vsa-${STAGE}-avx-findings-table-name`,
    avxActionsTableNameExportName:          `vsa-${STAGE}-avx-actions-table-name`,
    avxFeedbackTableNameExportName:         `vsa-${STAGE}-avx-feedback-table-name`,
    notificationDevicesTableNameExportName: `vsa-${STAGE}-notification-devices-table-name`,
    avxApprovalRulesTableNameExportName:    `vsa-${STAGE}-avx-approval-rules-table-name`,
    rsaDispatchTable: rsaTable,
    rsaDispatchKey: rsaKey,
    rsaPositionMaxAgeSeconds: { literal: '900', parameterName: `/cvx/${STAGE}/rsa-position-max-age-seconds` },
    description: `Guidance for Agentic Vehicle Experience ${SO_NUMBER} - API`,
  });
  assertDescription(stack, 'VsaApiStack');
});

// ---------------------------------------------------------------------------
// VsaTier2Stack
// ---------------------------------------------------------------------------

test('tier2 stack template Description contains (SO9676)', () => {
  const app = new cdk.App();
  const stack = new VsaTier2Stack(app, nextId('vsa-test-tier2'), {
    cmsFiInternalApiEndpoint: TEST_CMS_FI_INTERNAL_API_ENDPOINT,
    dmsFiInternalApiEndpoint: TEST_DMS_FI_INTERNAL_API_ENDPOINT,
    env: ENV,
    stage: STAGE,
    adpRegion: 'us-east-1',
    adpAccountId: ACCOUNT,
    cmsKmsKeyArn: { literal: `arn:aws:kms:${REGION}:${ACCOUNT}:key/stub`, parameterName: `/cvx/${STAGE}/cms-kms-key-arn` },
    description: `Guidance for Agentic Vehicle Experience ${SO_NUMBER} - Tier 2`,
  });
  assertDescription(stack, 'VsaTier2Stack');
});

// ---------------------------------------------------------------------------
// Per-class: VsaFindingsExportStack
// ---------------------------------------------------------------------------

describe('VsaFindingsExportStack has (SO9676) in the stack description', () => {
  test('synthesised template Description contains (SO9676)', () => {
    const app = new cdk.App();
    const stack = new VsaFindingsExportStack(app, 'vsa-test-findings-export', {
      env: ENV,
      stage: STAGE,
      description: `Guidance for Agentic Vehicle Experience ${SO_NUMBER} - Findings Export`,
    });
    assertDescription(stack, 'VsaFindingsExportStack');
  });
});

// ---------------------------------------------------------------------------
// Completeness: bin/vsa.ts contains exactly the 9 expected stack constructors
// ---------------------------------------------------------------------------

test('bin/vsa.ts contains exactly the 9 expected stack constructors — no more, no fewer', () => {
  // Extract every `new <Name>Stack(app, …)` call from the comment-stripped source.
  // Sorted equality catches both a missing constructor (fewer than 8 entries) and
  // a new or duplicate constructor (entries differ from STACK_CTORS).
  //
  // Mutant A — add a 9th call without a description (e.g.
  //   `new cdk.Stack(app, "vsa-staging-extra")`):
  //   found will have 9 entries; sorted list != expected → THIS TEST FAILS.
  //
  // Mutant B — add a second call for an existing class (e.g.
  //   `new VsaFoundationStack(app, "vsa-staging-foundation-eu", {…})`):
  //   found will have two 'VsaFoundationStack' entries; sorted list != expected
  //   → THIS TEST FAILS.
  const src = code(BIN_SRC);
  const found: string[] = [];
  const re = /new\s+([\w.]+Stack)\s*\(\s*app\b/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(src)) !== null) {
    found.push(m[1]);
  }
  const expected = [...STACK_CTORS].sort();
  expect(found.sort()).toEqual(expected);
});
