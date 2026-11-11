/**
 * Stage C: api-drivers-me and api-vehicle-context snapshot read grants.
 *
 * Asserts on each Lambda's own role:
 *   (a) CMS_VEHICLE_SNAPSHOT_TABLE env var is set to cms-{cmsStage}-storage-vehicle-snapshot.
 *   (b) The role has exactly dynamodb:GetItem on the exact snapshot table ARN.
 *       No Scan, no Query, no writes on that statement.
 *       Resource is NOT `table/.../*` (no index ARN).
 *   (c) Anti-vacuity: the role has other grants (the snapshot statement is not the only one).
 *
 * Mutations guarded (M4):
 *   widen Resource to `table/*` or `table/.../*`   → (b) exact-ARN test FAILS
 *   add dynamodb:Scan to the snapshot statement    → (b) exact-action test FAILS
 *   omit CMS_VEHICLE_SNAPSHOT_TABLE env var        → (a) env test FAILS
 */

import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { VsaApiStack } from '../lib/vsa-api-stack';
import { actionsOf, resourcesOf, roleIdOfFunction, roleStatements, actionCovers, type IamStatement } from './helpers/iam-surface';

// ─── Fixture ─────────────────────────────────────────────────────────────────

const REGION = 'us-west-2';
const ACCOUNT = '123456789012';
const STAGE = 'staging';
const CMS_STAGE = STAGE;

const SNAPSHOT_TABLE_NAME = `cms-${CMS_STAGE}-storage-vehicle-snapshot`;
const SNAPSHOT_TABLE_ARN = `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/${SNAPSHOT_TABLE_NAME}`;

let stackCount = 0;

function makeStack(): { stack: VsaApiStack; template: Template } {
  const id = ++stackCount;
  const app = new cdk.App();
  const env = { account: ACCOUNT, region: REGION };

  const parent = new cdk.Stack(app, `SnapshotParent${id}`, { env });
  const tbl = (n: string) => dynamodb.Table.fromTableName(parent, `T${id}-${n}`, n);
  const fn  = (n: string, arn: string) => lambda.Function.fromFunctionArn(parent, `F${id}-${n}`, arn);

  const rsaDispatchTable = dynamodb.Table.fromTableArn(
    parent, `RsaD${id}`,
    `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/vsa-${STAGE}-rsa-dispatch`,
  );
  const rsaDispatchKey = kms.Key.fromKeyArn(
    parent, `RsaK${id}`,
    `arn:aws:kms:${REGION}:${ACCOUNT}:key/test-rsa-dispatch-key`,
  );

  const stack = new VsaApiStack(app, `vsa-${STAGE}-api-snap-${id}`, {
    env,
    stage: STAGE,
    cmsStage: CMS_STAGE,
    tenantConfigTable: tbl(`cms-${STAGE}-tenant-config`),
    classifier: fn('C', `arn:aws:lambda:${REGION}:${ACCOUNT}:function:vsa-${STAGE}-triage-classifier`),
    cmsKmsKeyArn: {
      literal: `arn:aws:kms:${REGION}:${ACCOUNT}:key/test-key`,
      parameterName: `/cvx/${STAGE}/cms-kms-key-arn`,
    },
    redisEndpoint: {
      literal: `redis://stub.redis.local:6379`,
      parameterName: `/cvx/${STAGE}/redis-endpoint`,
    },
    connectInstanceArn: {
      literal: `arn:aws:connect:us-east-1:${ACCOUNT}:instance/test-instance`,
      parameterName: `/cvx/${STAGE}/connect-instance-arn`,
    },
    dmsApiEndpoint: {
      literal: `https://dms.example.invalid/${STAGE}`,
      parameterName: `/cvx/${STAGE}/dms-api-endpoint`,
    },
    cmsApiEndpoint: {
      literal: `https://cms.example.invalid/${STAGE}`,
      parameterName: `/cvx/${STAGE}/cms-api-endpoint`,
    },
    vpcId: 'vpc-00000000000000000',
    redisSecurityGroupId: 'sg-00000000000000000',
    assistantChatRuntimeArn: {
      literal: `arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT}:runtime/stub-${STAGE}`,
      parameterName: `/cvx/${STAGE}/assistant-chat-runtime-arn`,
    },
    assistantChatRuntimeRegion: REGION,
    allowedOrigin: {
      literal: `https://stub.example.com`,
      parameterName: `/cvx/${STAGE}/allowed-origin`,
    },
    acquireReservationHandoffFn: fn('H', `arn:aws:lambda:${REGION}:${ACCOUNT}:function:vsa-${STAGE}-acquire-handoff`),
    acquireReservationHandoffFnArnExportName: `vsa-${STAGE}-acquire-reservation-handoff-fn-arn`,
    acquireCatalogTable: tbl(`vsa-${STAGE}-acquire-catalog`),
    acquireOrdersTable:  tbl(`vsa-${STAGE}-acquire-orders`),
    acquireLeadsTable:   tbl(`vsa-${STAGE}-acquire-leads`),
    avxFindingsTableNameExportName:        `vsa-${STAGE}-avx-findings-table-name`,
    avxActionsTableNameExportName:         `vsa-${STAGE}-avx-actions-table-name`,
    avxFeedbackTableNameExportName:        `vsa-${STAGE}-avx-feedback-table-name`,
    notificationDevicesTableNameExportName:`vsa-${STAGE}-notification-devices-table-name`,
    rsaDispatchTable,
    rsaDispatchKey,
    rsaPositionMaxAgeSeconds: {
      literal: '900',
      parameterName: `/cvx/${STAGE}/rsa-position-max-age-seconds`,
    },
  });

  return { stack, template: Template.fromStack(stack) };
}

// ─── Shared template (built once) ────────────────────────────────────────────

const { template } = makeStack();

// ─── Helpers ─────────────────────────────────────────────────────────────────

/** Env vars of a function by its FunctionName. */
function envOf(t: Template, functionName: string): Record<string, unknown> {
  const fns = t.findResources('AWS::Lambda::Function', {
    Properties: { FunctionName: functionName },
  });
  const ids = Object.keys(fns);
  if (ids.length !== 1) throw new Error(`expected 1 Lambda named ${functionName}, got ${ids.length}`);
  const props = (fns[ids[0]] as any).Properties;
  return props?.Environment?.Variables ?? {};
}

/** All statements across the role that include the snapshot table ARN. */
function snapshotStatements(t: Template, roleId: string) {
  return roleStatements(t, roleId).filter(stmt =>
    resourcesOf(stmt).some(r => {
      const rStr = JSON.stringify(r);
      return rStr.includes(SNAPSHOT_TABLE_NAME);
    }),
  );
}

const DRIVERS_ME_FN = `vsa-${STAGE}-api-drivers-me`;
const VEHICLE_CTX_FN = `vsa-${STAGE}-api-vehicle-context`;

// ─── Tests: api-drivers-me ───────────────────────────────────────────────────

describe('api-drivers-me snapshot read wiring', () => {
  const roleId = roleIdOfFunction(template, DRIVERS_ME_FN);

  test('(a) CMS_VEHICLE_SNAPSHOT_TABLE env var is set to the exact table name', () => {
    const env = envOf(template, DRIVERS_ME_FN);
    expect(env['CMS_VEHICLE_SNAPSHOT_TABLE']).toBe(SNAPSHOT_TABLE_NAME);
  });

  test('(b) role has dynamodb:GetItem on the exact snapshot table ARN', () => {
    const stmts = snapshotStatements(template, roleId);
    expect(stmts.length).toBeGreaterThan(0);
    const actions = stmts.flatMap(actionsOf);
    expect(actions).toContain('dynamodb:GetItem');
  });

  test('(b) snapshot statement does NOT include Scan, Query, or write actions (M4 guard)', () => {
    const stmts = snapshotStatements(template, roleId);
    const actions = stmts.flatMap(actionsOf);
    const forbidden = ['dynamodb:Scan', 'dynamodb:Query', 'dynamodb:PutItem',
                       'dynamodb:UpdateItem', 'dynamodb:DeleteItem', 'dynamodb:BatchWriteItem'];
    for (const act of forbidden) {
      expect(actions).not.toContain(act);
    }
  });

  test('(b) snapshot resource is the exact table ARN — no wildcard, no index ARN (M4 guard)', () => {
    const stmts = snapshotStatements(template, roleId);
    const resources = stmts.flatMap(resourcesOf).map(r => JSON.stringify(r));
    // Must include the exact ARN
    expect(resources.some(r => r.includes(SNAPSHOT_TABLE_ARN))).toBe(true);
    // Must NOT include a wildcard resource or an index ARN
    expect(resources.some(r => r.includes('/*'))).toBe(false);
  });

  test('(c) anti-vacuity: the role also has other grants (snapshot not the only statement)', () => {
    const all = roleStatements(template, roleId);
    const snap = snapshotStatements(template, roleId);
    expect(all.length).toBeGreaterThan(snap.length);
  });
});

// ─── Tests: api-vehicle-context ──────────────────────────────────────────────

describe('api-vehicle-context snapshot read wiring', () => {
  const roleId = roleIdOfFunction(template, VEHICLE_CTX_FN);

  test('(a) CMS_VEHICLE_SNAPSHOT_TABLE env var is set to the exact table name', () => {
    const env = envOf(template, VEHICLE_CTX_FN);
    expect(env['CMS_VEHICLE_SNAPSHOT_TABLE']).toBe(SNAPSHOT_TABLE_NAME);
  });

  test('(b) role has dynamodb:GetItem on the exact snapshot table ARN', () => {
    const stmts = snapshotStatements(template, roleId);
    expect(stmts.length).toBeGreaterThan(0);
    const actions = stmts.flatMap(actionsOf);
    expect(actions).toContain('dynamodb:GetItem');
  });

  test('(b) snapshot statement does NOT include Scan, Query, or write actions (M4 guard)', () => {
    const stmts = snapshotStatements(template, roleId);
    const actions = stmts.flatMap(actionsOf);
    const forbidden = ['dynamodb:Scan', 'dynamodb:Query', 'dynamodb:PutItem',
                       'dynamodb:UpdateItem', 'dynamodb:DeleteItem', 'dynamodb:BatchWriteItem'];
    for (const act of forbidden) {
      expect(actions).not.toContain(act);
    }
  });

  test('(b) snapshot resource is the exact table ARN — no wildcard, no index ARN (M4 guard)', () => {
    const stmts = snapshotStatements(template, roleId);
    const resources = stmts.flatMap(resourcesOf).map(r => JSON.stringify(r));
    expect(resources.some(r => r.includes(SNAPSHOT_TABLE_ARN))).toBe(true);
    expect(resources.some(r => r.includes('/*'))).toBe(false);
  });

  test('(c) anti-vacuity: the role also has other grants (snapshot not the only statement)', () => {
    const all = roleStatements(template, roleId);
    const snap = snapshotStatements(template, roleId);
    expect(all.length).toBeGreaterThan(snap.length);
  });
});

// ─── W5: Exact-action, exact-resource, and probe tests (C1/C2/C3/C5 guards) ─

/**
 * Returns true when a resource value (raw CFN, string, or intrinsic) could grant
 * access to the snapshot table or any index/stream beneath it.  Used by the probe
 * test to find every statement that could cover the table, not just the one we
 * already know about.
 *
 * Checks (conservative — any match triggers):
 *   1. Exact ARN: the JSON of the resource equals the JSON of the table ARN.
 *   2. Prefix wildcard: any string suffix with '*' that is a prefix of the ARN.
 *   3. Global or service wildcard: '*' or 'dynamodb:*' anywhere in the resource JSON.
 *   4. Contains the table name as a substring (catches ARN prefixes like table/cms-staging-*).
 */
function resourceCoversSnapshotTable(r: unknown, tableArn: string, tableName: string): boolean {
  const rStr = JSON.stringify(r);
  if (rStr === JSON.stringify(tableArn)) return true;          // exact
  if (rStr === '"*"') return true;                             // global wildcard
  if (rStr.includes(tableName)) return true;                   // contains table name (prefix/exact)
  if (rStr.includes('table/*') || rStr.includes('table/\\u002a')) return true; // arn:.../table/*
  if (typeof r === 'string') {
    const s = r as string;
    if (s.endsWith('*') && tableArn.startsWith(s.slice(0, -1))) return true;
  }
  return false;
}

/** True when the statement has at least one DynamoDB action (including wildcards). */
function stmtHasDynamoDbAction(stmt: IamStatement): boolean {
  return actionsOf(stmt).some((a) =>
    a === '*' || actionCovers(a, 'dynamodb:GetItem'),
  );
}

// ─── W5 tests: api-drivers-me ────────────────────────────────────────────────

describe('api-drivers-me snapshot W5: exact action + resource + probe', () => {
  const roleId = roleIdOfFunction(template, DRIVERS_ME_FN);

  test('(b2) snapshot statement action list is EXACTLY [dynamodb:GetItem] — no extra actions (C1/C2 guard)', () => {
    const stmts = snapshotStatements(template, roleId);
    expect(stmts.length).toBeGreaterThan(0);
    const actions = stmts.flatMap(actionsOf);
    expect(actions).toHaveLength(1);
    expect(actions[0]).toBe('dynamodb:GetItem');
  });

  test('(b3) snapshot resource is EXACTLY the table ARN — no prefix, no wildcard (C3 guard)', () => {
    const stmts = snapshotStatements(template, roleId);
    const resources = stmts.flatMap(resourcesOf).map((r) => JSON.stringify(r));
    expect(resources).toHaveLength(1);
    expect(resources[0]).toBe(JSON.stringify(SNAPSHOT_TABLE_ARN));
  });

  test('(probe) no other statement on the role also covers the snapshot table with a dynamodb action (C5 guard)', () => {
    const allStmts = roleStatements(template, roleId);
    const snapStmts = snapshotStatements(template, roleId);
    const snapStmtJsons = new Set(snapStmts.map((s) => JSON.stringify(s)));

    const coveringStmts = allStmts.filter(
      (stmt) =>
        stmtHasDynamoDbAction(stmt) &&
        resourcesOf(stmt).some((r) =>
          resourceCoversSnapshotTable(r, SNAPSHOT_TABLE_ARN, SNAPSHOT_TABLE_NAME),
        ),
    );

    for (const stmt of coveringStmts) {
      const isKnownSnap = snapStmtJsons.has(JSON.stringify(stmt));
      if (!isKnownSnap) {
        // Surface what the extra statement grants so the failure is actionable.
        expect(
          `Extra statement covering snapshot table: actions=${JSON.stringify(actionsOf(stmt))} resources=${JSON.stringify(resourcesOf(stmt))}`,
        ).toBe('no extra statement (C5 guard)');
      }
    }
  });
});

// ─── W5 tests: api-vehicle-context ───────────────────────────────────────────

describe('api-vehicle-context snapshot W5: exact action + resource + probe', () => {
  const roleId = roleIdOfFunction(template, VEHICLE_CTX_FN);

  test('(b2) snapshot statement action list is EXACTLY [dynamodb:GetItem] — no extra actions (C1/C2 guard)', () => {
    const stmts = snapshotStatements(template, roleId);
    expect(stmts.length).toBeGreaterThan(0);
    const actions = stmts.flatMap(actionsOf);
    expect(actions).toHaveLength(1);
    expect(actions[0]).toBe('dynamodb:GetItem');
  });

  test('(b3) snapshot resource is EXACTLY the table ARN — no prefix, no wildcard (C3 guard)', () => {
    const stmts = snapshotStatements(template, roleId);
    const resources = stmts.flatMap(resourcesOf).map((r) => JSON.stringify(r));
    expect(resources).toHaveLength(1);
    expect(resources[0]).toBe(JSON.stringify(SNAPSHOT_TABLE_ARN));
  });

  test('(probe) no other statement on the role also covers the snapshot table with a dynamodb action (C5 guard)', () => {
    const allStmts = roleStatements(template, roleId);
    const snapStmts = snapshotStatements(template, roleId);
    const snapStmtJsons = new Set(snapStmts.map((s) => JSON.stringify(s)));

    const coveringStmts = allStmts.filter(
      (stmt) =>
        stmtHasDynamoDbAction(stmt) &&
        resourcesOf(stmt).some((r) =>
          resourceCoversSnapshotTable(r, SNAPSHOT_TABLE_ARN, SNAPSHOT_TABLE_NAME),
        ),
    );

    for (const stmt of coveringStmts) {
      const isKnownSnap = snapStmtJsons.has(JSON.stringify(stmt));
      if (!isKnownSnap) {
        expect(
          `Extra statement covering snapshot table: actions=${JSON.stringify(actionsOf(stmt))} resources=${JSON.stringify(resourcesOf(stmt))}`,
        ).toBe('no extra statement (C5 guard)');
      }
    }
  });
});

// ─── S4: cmsTable map entry ───────────────────────────────────────────────────

describe('S4: snapshot table name is derived from cmsTable.vehicleSnapshot', () => {
  // Both env vars and both ARNs must use the same name derived from cmsStage.
  // This test ensures the cmsTable map entry exists and is used consistently
  // (regression: previously two inline cms() calls could drift independently).

  test('api-drivers-me and api-vehicle-context share the same snapshot table name', () => {
    const envMe = (() => {
      const fns = template.findResources('AWS::Lambda::Function', {
        Properties: { FunctionName: DRIVERS_ME_FN },
      });
      const ids = Object.keys(fns);
      const props = (fns[ids[0]] as any).Properties;
      return props?.Environment?.Variables ?? {};
    })();
    const envCtx = (() => {
      const fns = template.findResources('AWS::Lambda::Function', {
        Properties: { FunctionName: VEHICLE_CTX_FN },
      });
      const ids = Object.keys(fns);
      const props = (fns[ids[0]] as any).Properties;
      return props?.Environment?.Variables ?? {};
    })();
    expect(envMe['CMS_VEHICLE_SNAPSHOT_TABLE']).toBe(envCtx['CMS_VEHICLE_SNAPSHOT_TABLE']);
    expect(envMe['CMS_VEHICLE_SNAPSHOT_TABLE']).toBe(SNAPSHOT_TABLE_NAME);
  });
});

// ─── api-vehicle-live-state (issues/2026-11-06-live-state-stale-odometer/) ────
//
// live-state returns a Redis odometer/charge only when it is newer than the snapshot
// value, so it reads the snapshot too. Same shape as the two reads above.
// Mutations guarded: widen the Resource (table/*, the table's /index/*), add Scan/Query
// or a write, drop the env var, point the env var at another table.

const LIVE_STATE_FN = `vsa-${STAGE}-api-vehicle-live-state`;

describe('api-vehicle-live-state snapshot read wiring', () => {
  const roleId = roleIdOfFunction(template, LIVE_STATE_FN);

  test('(a) CMS_VEHICLE_SNAPSHOT_TABLE env var is the same snapshot table the context route reads', () => {
    const env = envOf(template, LIVE_STATE_FN);
    expect(env['CMS_VEHICLE_SNAPSHOT_TABLE']).toBe(SNAPSHOT_TABLE_NAME);
    expect(env['CMS_VEHICLE_SNAPSHOT_TABLE']).toBe(envOf(template, VEHICLE_CTX_FN)['CMS_VEHICLE_SNAPSHOT_TABLE']);
  });

  test('(b2) snapshot statement action list is EXACTLY [dynamodb:GetItem]', () => {
    const stmts = snapshotStatements(template, roleId);
    expect(stmts).toHaveLength(1);
    expect(actionsOf(stmts[0])).toEqual(['dynamodb:GetItem']);
    expect(stmts[0].Effect).toBe('Allow');
  });

  test('(b3) snapshot resource is EXACTLY the table ARN — no prefix, no wildcard, no index', () => {
    const stmts = snapshotStatements(template, roleId);
    const resources = stmts.flatMap(resourcesOf).map((r) => JSON.stringify(r));
    expect(resources).toEqual([JSON.stringify(SNAPSHOT_TABLE_ARN)]);
  });

  test('(probe) no other statement on the role covers the snapshot table with a dynamodb action', () => {
    const snapStmtJsons = new Set(snapshotStatements(template, roleId).map((s) => JSON.stringify(s)));
    const extra = roleStatements(template, roleId).filter(
      (stmt) =>
        !snapStmtJsons.has(JSON.stringify(stmt)) &&
        stmtHasDynamoDbAction(stmt) &&
        resourcesOf(stmt).some((r) => resourceCoversSnapshotTable(r, SNAPSHOT_TABLE_ARN, SNAPSHOT_TABLE_NAME)),
    );
    expect(extra.map((s) => `${JSON.stringify(actionsOf(s))} ${JSON.stringify(resourcesOf(s))}`)).toEqual([]);
  });

  test('(c) anti-vacuity: the role keeps its vehicles read and other grants', () => {
    const all = roleStatements(template, roleId);
    expect(all.length).toBeGreaterThan(snapshotStatements(template, roleId).length);
    const vehiclesArn = `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/cms-${CMS_STAGE}-storage-vehicles`;
    expect(all.some((s) => resourcesOf(s).some((r) => JSON.stringify(r) === JSON.stringify(vehiclesArn)))).toBe(true);
  });
});
