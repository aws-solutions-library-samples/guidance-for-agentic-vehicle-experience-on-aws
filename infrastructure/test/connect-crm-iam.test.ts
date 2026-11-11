/**
 * CDK unit tests for Task 2.2: Connect Customer Profiles + Cases IAM wiring.
 *
 * Spec: .kiro/specs/2026-11-02-avx-connect-profiles-cases/spec.md § IAM
 *
 * Assertions:
 *   OFF case (no connectCrmConfig prop):
 *     (a) ApiEscalate has NO CONNECT_CRM_CONFIG env var.
 *     (b) ApiEscalate has NO CONNECT_CRM_TENANTS env var.
 *     (c) ApiEscalate role has NO profile: action.
 *     (d) ApiEscalate role has NO cases: action.
 *     (e) ApiEscalate role has NO connect:DescribeContact action.
 *
 *   ON case (connectCrmConfig supplied):
 *     (f) CONNECT_CRM_CONFIG is present on ApiEscalate.
 *     (g) CONNECT_CRM_TENANTS is present on ApiEscalate.
 *     (h) ConnectProfilesDomainWrite: exact actions + exact domain ARN, no wildcard.
 *     (i) ConnectProfilesPutObject: exact actions + domain ARN + object-type ARN.
 *     (j) ConnectCasesWrite: exact actions + domain ARN + field/* + template/<tid> + case/WILDCARD
 *         (CreateCase authorizes the case resource too; live AccessDenied 2026-11-02).
 *     (k) ConnectCasesRelatedItem: exact actions + domain ARN + case/WILDCARD +
 *         case/WILDCARD/related-item/WILDCARD; no condition (the service doesn't put
 *         cases:RelatedItemType in the request context; live AccessDenied 2026-11-02).
 *     (l) ConnectDescribeContact: exact action + instanceArn/contact/*.
 *
 * Mutation tried and confirmed caught (M1):
 *   In ConnectCasesWrite resources, changed `casesDomainArn` (exact domain)
 *   to `casesDomainArn + '/*'` (wildcard). The (j) exact-resource test FAILED as
 *   expected (resource `...domain/test-cases-domain-id/*` was present, not
 *   `...domain/test-cases-domain-id`). Restored before committing.
 */

import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { VsaApiStack, ConnectCrmConfig } from '../lib/vsa-api-stack';
import { roleIdOfFunction, roleStatements, actionsOf, resourcesOf } from './helpers/iam-surface';

// ─── Constants ───────────────────────────────────────────────────────────────

const REGION        = 'us-west-2';
const ACCOUNT       = '111122223333';   // test account per task spec
const STAGE         = 'staging';
const CMS_STAGE     = 'staging';

// CRM config with fake ids per task spec
const CRM_INSTANCE_ARN  = 'arn:aws:connect:us-east-1:111122223333:instance/fake-connect-instance';
const CRM_REGION        = 'us-east-1';   // parsed from CRM_INSTANCE_ARN
const CRM_ACCOUNT       = '111122223333';
const PROFILES_DOMAIN   = 'fake-profiles-domain';
const OBJECT_TYPE       = 'CvxVehicle';
const CASES_DOMAIN_ID   = 'fake-cases-domain-id';
const CASES_TEMPLATE_ID = 'fake-cases-template-id';

const TEST_CRM_CONFIG: ConnectCrmConfig = {
  instanceArn:    CRM_INSTANCE_ARN,
  region:         CRM_REGION,
  account:        CRM_ACCOUNT,
  profilesDomain: PROFILES_DOMAIN,
  objectType:     OBJECT_TYPE,
  casesDomainId:  CASES_DOMAIN_ID,
  casesTemplateId: CASES_TEMPLATE_ID,
  fieldIds: {
    severity:        'field-severity',
    vin:             'field-vin',
    vehicleId:       'field-vehicle-id',
    faultCodes:      'field-fault-codes',
    escalationKey:   'field-escalation-key',
    triageSessionId: 'field-triage-session-id',
  },
};

// Expected IAM ARN literals (built from config fields, testable without a deploy)
const PROFILES_DOMAIN_ARN      = `arn:aws:profile:${CRM_REGION}:${CRM_ACCOUNT}:domains/${PROFILES_DOMAIN}`;
const PROFILES_OBJECT_TYPE_ARN = `${PROFILES_DOMAIN_ARN}/object-types/${OBJECT_TYPE}`;
const CASES_DOMAIN_ARN         = `arn:aws:cases:${CRM_REGION}:${CRM_ACCOUNT}:domain/${CASES_DOMAIN_ID}`;
const CASES_FIELD_WILDCARD_ARN = `${CASES_DOMAIN_ARN}/field/*`;
const CASES_TEMPLATE_ARN       = `${CASES_DOMAIN_ARN}/template/${CASES_TEMPLATE_ID}`;
const CASES_RELATED_ITEM_ARN   = `${CASES_DOMAIN_ARN}/case/*/related-item/*`;
const CONNECT_CONTACT_ARN      = `${CRM_INSTANCE_ARN}/contact/*`;

// ─── Fixture factory ─────────────────────────────────────────────────────────

let stackCount = 0;

function makeStack(connectCrmConfig?: ConnectCrmConfig): {
  stack: VsaApiStack;
  template: Template;
  escalateRoleId: string;
} {
  const id = ++stackCount;
  const app = new cdk.App();
  const env = { account: ACCOUNT, region: REGION };

  const parent = new cdk.Stack(app, `CrmParent${id}`, { env });
  const tbl = (n: string) => dynamodb.Table.fromTableName(parent, `T${id}-${n}`, n);
  const fn  = (n: string, arn: string) => lambda.Function.fromFunctionArn(parent, `F${id}-${n}`, arn);

  const rsaDispatchTable = dynamodb.Table.fromTableArn(
    parent, `RsaD${id}`,
    `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/vsa-${STAGE}-rsa-dispatch`,
  );
  const rsaDispatchKey = kms.Key.fromKeyArn(
    parent, `RsaK${id}`,
    `arn:aws:kms:${REGION}:${ACCOUNT}:key/fake-rsa-dispatch-key`,
  );

  const stack = new VsaApiStack(app, `vsa-${STAGE}-api-crm-${id}`, {
    env,
    stage:    STAGE,
    cmsStage: CMS_STAGE,
    tenantConfigTable: tbl(`cms-${STAGE}-tenant-config`),
    classifier: fn('C', `arn:aws:lambda:${REGION}:${ACCOUNT}:function:vsa-${STAGE}-triage-classifier`),
    cmsKmsKeyArn: {
      literal:       `arn:aws:kms:${REGION}:${ACCOUNT}:key/fake-cms-key`,
      parameterName: `/cvx/${STAGE}/cms-kms-key-arn`,
    },
    redisEndpoint: {
      literal:       'redis://stub.redis.local:6379',
      parameterName: `/cvx/${STAGE}/redis-endpoint`,
    },
    connectInstanceArn: {
      literal:       `arn:aws:connect:us-east-1:${ACCOUNT}:instance/fake-connect-callback-instance`,
      parameterName: `/cvx/${STAGE}/connect-instance-arn`,
    },
    dmsApiEndpoint: {
      literal:       `https://dms.example.invalid/${STAGE}`,
      parameterName: `/cvx/${STAGE}/dms-api-endpoint`,
    },
    cmsApiEndpoint: {
      literal:       `https://cms.example.invalid/${STAGE}`,
      parameterName: `/cvx/${STAGE}/cms-api-endpoint`,
    },
    vpcId:               'vpc-00000000000000000',
    redisSecurityGroupId:'sg-00000000000000000',
    assistantChatRuntimeArn: {
      literal:       `arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT}:runtime/fake-${STAGE}`,
      parameterName: `/cvx/${STAGE}/assistant-chat-runtime-arn`,
    },
    assistantChatRuntimeRegion: REGION,
    allowedOrigin: {
      literal:       'https://stub.example.com',
      parameterName: `/cvx/${STAGE}/allowed-origin`,
    },
    acquireReservationHandoffFn: fn(
      'H', `arn:aws:lambda:${REGION}:${ACCOUNT}:function:vsa-${STAGE}-acquire-handoff`,
    ),
    acquireReservationHandoffFnArnExportName: `vsa-${STAGE}-acquire-reservation-handoff-fn-arn`,
    acquireCatalogTable: tbl(`vsa-${STAGE}-acquire-catalog`),
    acquireOrdersTable:  tbl(`vsa-${STAGE}-acquire-orders`),
    acquireLeadsTable:   tbl(`vsa-${STAGE}-acquire-leads`),
    avxFindingsTableNameExportName:         `vsa-${STAGE}-avx-findings-table-name`,
    avxActionsTableNameExportName:          `vsa-${STAGE}-avx-actions-table-name`,
    avxFeedbackTableNameExportName:         `vsa-${STAGE}-avx-feedback-table-name`,
    notificationDevicesTableNameExportName: `vsa-${STAGE}-notification-devices-table-name`,
    rsaDispatchTable,
    rsaDispatchKey,
    rsaPositionMaxAgeSeconds: {
      literal:       '900',
      parameterName: `/cvx/${STAGE}/rsa-position-max-age-seconds`,
    },
    connectCrmConfig,
  });

  const template     = Template.fromStack(stack);
  const escalateRoleId = roleIdOfFunction(template, `vsa-${STAGE}-api-escalate`);
  return { stack, template, escalateRoleId };
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

/** Env vars of the ApiEscalate Lambda. */
function escalateEnv(template: Template): Record<string, unknown> {
  const fns = template.findResources('AWS::Lambda::Function', {
    Properties: { FunctionName: `vsa-${STAGE}-api-escalate` },
  });
  const keys = Object.keys(fns);
  if (keys.length !== 1) throw new Error(`expected 1 escalate Lambda, got ${keys.length}`);
  const props = (fns[keys[0]] as { Properties?: { Environment?: { Variables?: Record<string, unknown> } } })
    .Properties;
  return props?.Environment?.Variables ?? {};
}

/** IAM statements on the escalate role that include action(s) matching `prefix`. */
function stmtsWithActionPrefix(stmts: ReturnType<typeof roleStatements>, prefix: string) {
  return stmts.filter(s =>
    actionsOf(s).some(a => a.toLowerCase().startsWith(prefix.toLowerCase())),
  );
}

/** Find one statement by Sid in the escalate role (throws if not exactly one). */
function stmtBySid(stmts: ReturnType<typeof roleStatements>, sid: string) {
  const found = stmts.filter(s => s['Sid'] === sid);
  if (found.length !== 1) {
    throw new Error(`expected exactly 1 statement with Sid=${sid}, found ${found.length}`);
  }
  return found[0];
}

// ─── OFF case ────────────────────────────────────────────────────────────────

describe('Connect CRM — flag OFF (connectCrmConfig absent)', () => {
  const { template, escalateRoleId } = makeStack(/* no config */);
  const stmts = roleStatements(template, escalateRoleId);
  const env   = escalateEnv(template);

  test('(a) no CONNECT_CRM_CONFIG env var on ApiEscalate', () => {
    expect(env).not.toHaveProperty('CONNECT_CRM_CONFIG');
  });

  test('(b) no CONNECT_CRM_TENANTS env var on ApiEscalate', () => {
    expect(env).not.toHaveProperty('CONNECT_CRM_TENANTS');
  });

  test('(c) no profile: action in escalate role', () => {
    const profileStmts = stmtsWithActionPrefix(stmts, 'profile:');
    expect(profileStmts).toHaveLength(0);
  });

  test('(d) no cases: action in escalate role', () => {
    const casesStmts = stmtsWithActionPrefix(stmts, 'cases:');
    expect(casesStmts).toHaveLength(0);
  });

  test('(e) no connect:DescribeContact action in escalate role', () => {
    const describeStmts = stmts.filter(s =>
      actionsOf(s).some(a => a.toLowerCase() === 'connect:describecontact'),
    );
    expect(describeStmts).toHaveLength(0);
  });
});

// ─── ON case ─────────────────────────────────────────────────────────────────

describe('Connect CRM — flag ON (connectCrmConfig supplied)', () => {
  const { template, escalateRoleId } = makeStack(TEST_CRM_CONFIG);
  const stmts = roleStatements(template, escalateRoleId);
  const env   = escalateEnv(template);

  // ── Env vars ──────────────────────────────────────────────────────────────

  test('(f) CONNECT_CRM_CONFIG env var is present on ApiEscalate', () => {
    expect(env).toHaveProperty('CONNECT_CRM_CONFIG');
  });

  test('(g) CONNECT_CRM_TENANTS env var is present on ApiEscalate', () => {
    expect(env).toHaveProperty('CONNECT_CRM_TENANTS');
  });

  // ── Statement h: ConnectProfilesDomainWrite ──────────────────────────────

  describe('(h) ConnectProfilesDomainWrite', () => {
    let stmt: ReturnType<typeof stmtBySid>;
    beforeAll(() => { stmt = stmtBySid(stmts, 'ConnectProfilesDomainWrite'); });

    test('actions are exactly profile:SearchProfiles, profile:CreateProfile, profile:UpdateProfile', () => {
      const actions = [...actionsOf(stmt)].sort();
      expect(actions).toEqual([
        'profile:CreateProfile',
        'profile:SearchProfiles',
        'profile:UpdateProfile',
      ]);
    });

    test('resource is exactly the Profiles domain ARN — no wildcard', () => {
      const resources = resourcesOf(stmt).map(r => JSON.stringify(r));
      expect(resources).toHaveLength(1);
      expect(resources[0]).toBe(JSON.stringify(PROFILES_DOMAIN_ARN));
    });

    test('resource does NOT contain a wildcard domain (M1 guard)', () => {
      const resStr = JSON.stringify(stmt['Resource']);
      expect(resStr).not.toContain('/*');
      expect(resStr).not.toContain('domain/*');
    });
  });

  // ── Statement i: ConnectProfilesPutObject ────────────────────────────────

  describe('(i) ConnectProfilesPutObject', () => {
    let stmt: ReturnType<typeof stmtBySid>;
    beforeAll(() => { stmt = stmtBySid(stmts, 'ConnectProfilesPutObject'); });

    test('action is exactly profile:PutProfileObject', () => {
      const actions = actionsOf(stmt);
      expect(actions).toEqual(['profile:PutProfileObject']);
    });

    test('resources are exactly domain ARN + object-type ARN', () => {
      const resources = resourcesOf(stmt).map(r => JSON.stringify(r)).sort();
      const expected = [PROFILES_DOMAIN_ARN, PROFILES_OBJECT_TYPE_ARN].map(a => JSON.stringify(a)).sort();
      expect(resources).toEqual(expected);
    });

    test('object-type ARN contains the exact objectType — no wildcard', () => {
      const resStr = JSON.stringify(stmt['Resource']);
      expect(resStr).toContain(`/object-types/${OBJECT_TYPE}`);
      expect(resStr).not.toContain('/object-types/*');
    });
  });

  // ── Statement j: ConnectCasesWrite ───────────────────────────────────────

  describe('(j) ConnectCasesWrite', () => {
    let stmt: ReturnType<typeof stmtBySid>;
    beforeAll(() => { stmt = stmtBySid(stmts, 'ConnectCasesWrite'); });

    test('actions are exactly cases:SearchCases and cases:CreateCase', () => {
      const actions = [...actionsOf(stmt)].sort();
      expect(actions).toEqual(['cases:CreateCase', 'cases:SearchCases']);
    });

    test('resources are exactly domain ARN + case/* + field/* + template/<tid>', () => {
      const resources = resourcesOf(stmt).map(r => JSON.stringify(r)).sort();
      const expected  = [CASES_DOMAIN_ARN, `${CASES_DOMAIN_ARN}/case/*`, CASES_FIELD_WILDCARD_ARN, CASES_TEMPLATE_ARN]
        .map(a => JSON.stringify(a)).sort();
      expect(resources).toEqual(expected);
    });

    test('domain resource is exact — not a wildcard (M1 mutation guard)', () => {
      // M1: changing CASES_DOMAIN_ARN → CASES_DOMAIN_ARN + '/*' in vsa-api-stack.ts
      // caused this test to fail: resource "...domain/fake-cases-domain-id/*" was present
      // instead of "...domain/fake-cases-domain-id". Confirmed caught. Restored.
      const resources = resourcesOf(stmt).map(r => JSON.stringify(r));
      expect(resources).toContain(JSON.stringify(CASES_DOMAIN_ARN));
      // Must not have a wildcard on the bare domain entry
      expect(resources).not.toContain(JSON.stringify(`${CASES_DOMAIN_ARN}/*`));
    });

    test('template ARN names the exact casesTemplateId — no wildcard', () => {
      const resStr = JSON.stringify(stmt['Resource']);
      expect(resStr).toContain(`/template/${CASES_TEMPLATE_ID}`);
      expect(resStr).not.toContain('/template/*');
    });
  });

  // ── Statement k: ConnectCasesRelatedItem ─────────────────────────────────

  describe('(k) ConnectCasesRelatedItem', () => {
    let stmt: ReturnType<typeof stmtBySid>;
    beforeAll(() => { stmt = stmtBySid(stmts, 'ConnectCasesRelatedItem'); });

    test('action is exactly cases:CreateRelatedItem', () => {
      const actions = actionsOf(stmt);
      expect(actions).toEqual(['cases:CreateRelatedItem']);
    });

    test('resources are exactly domain ARN + case/* + case/*/related-item/*', () => {
      const resources = resourcesOf(stmt).map(r => JSON.stringify(r)).sort();
      const expected  = [CASES_DOMAIN_ARN, `${CASES_DOMAIN_ARN}/case/*`, CASES_RELATED_ITEM_ARN]
        .map(a => JSON.stringify(a)).sort();
      expect(resources).toEqual(expected);
    });

    test('no condition: the service ignores cases:RelatedItemType (live 2026-11-02); pinned so a change is deliberate', () => {
      expect(stmt['Condition']).toBeUndefined();
    });
  });

  // ── Statement l: ConnectDescribeContact ──────────────────────────────────

  describe('(l) ConnectDescribeContact', () => {
    let stmt: ReturnType<typeof stmtBySid>;
    beforeAll(() => { stmt = stmtBySid(stmts, 'ConnectDescribeContact'); });

    test('action is exactly connect:DescribeContact', () => {
      const actions = actionsOf(stmt);
      expect(actions).toEqual(['connect:DescribeContact']);
    });

    test('resource is exactly instanceArn/contact/* — no domain wildcard', () => {
      const resources = resourcesOf(stmt).map(r => JSON.stringify(r));
      expect(resources).toHaveLength(1);
      expect(resources[0]).toBe(JSON.stringify(CONNECT_CONTACT_ARN));
    });
  });

  // ── Anti-wildcard sweep ───────────────────────────────────────────────────

  test('no CRM statement uses a wildcard domain ARN (no-star-domain guard)', () => {
    const crmStmts = [
      ...stmtsWithActionPrefix(stmts, 'profile:'),
      ...stmtsWithActionPrefix(stmts, 'cases:'),
    ];
    expect(crmStmts.length).toBeGreaterThan(0); // anti-vacuity
    for (const s of crmStmts) {
      const resStr = JSON.stringify(s['Resource']);
      // A wildcard on the domain itself (not a sub-resource) is forbidden
      // e.g. "arn:aws:profile:...:domains/*" or "arn:aws:cases:...:domain/*"
      expect(resStr).not.toMatch(/domains\/\*/);
      expect(resStr).not.toMatch(/"domain\/\*"/);
    }
  });
});
