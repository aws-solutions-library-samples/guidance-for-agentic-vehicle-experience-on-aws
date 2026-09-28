/**
 * CDK unit tests for the cross-account DMS DynamoDB grants on the AgentCore
 * execution role (`DmsDynamoDbReadCrossAccount` in vsa-core-stack.ts).
 *
 * Added by DMS Spec 2 Task 3.4, which grants the role read access to
 * `dms-{stage}-avx-pre-briefs` so `visit_brief()` can surface the AVX
 * pre-brief artifact as a superset of the base visit brief.
 *
 * No test covered this statement before, so the grant's scope was unpinned:
 * widening it to `Resource: "*"`, or adding a write action, would have gone
 * unnoticed. These tests assert the PROPERTY (narrow, read-only, per-table)
 * rather than the mere presence of a statement.
 *
 * Mutation results recorded below (per ~/.kiro/steering/testing.md):
 *
 * M1 — grant removed:
 *   Procedure: delete `'avx-pre-briefs',` from the `dmsTables` array.
 *   Result: 2 tests FAIL — `grants read on dms-{stage}-avx-pre-briefs` and
 *           `grants read on the avx-pre-briefs indexes`. Both ARNs vanish
 *           together, since the grant is emitted per-table with an /index/*
 *           sibling.
 *   Restored: yes.
 *
 * M2 — resource widened to wildcard:
 *   Procedure: replace the statement's `resources: [...]` with `['*']`.
 *   Result: 4 tests FAIL — `never uses a wildcard resource` plus the three
 *           ARN-containment tests, which no longer find their table ARNs.
 *   Restored: yes.
 *
 * M3 — write action added:
 *   Procedure: add `'dynamodb:PutItem'` to the statement's `actions`.
 *   Result: 2 tests FAIL — `DMS read statement grants no write actions` and
 *           `no Allow statement grants write on certification-scores` (the
 *           widened statement covers every dms-staging-* table, including
 *           certification-scores, so the seam guard catches it too).
 *   Restored: yes.
 *
 * M4 — certification-scores write Deny (the deterministic seam):
 *   M4a procedure: rename the `DmsCertificationScoresDenyWrite` sid (to a
 *                  VALID alphanumeric name — see the note below).
 *   Result: `certification-scores keeps its explicit write Deny` FAILS as a
 *           named failure.
 *   M4b procedure: change that statement's effect from DENY to ALLOW.
 *   Result: BOTH `certification-scores keeps its explicit write Deny` and
 *           `no Allow statement grants write on certification-scores` FAIL.
 *           This is the mutation that matters — it is an actual privilege
 *           escalation, not a rename.
 *   Restored: yes.
 *
 *   Note on an invalid first attempt: renaming the sid to
 *   `DmsCertificationScoresDenyWrite_MUTATED` made *synth* throw, because CDK
 *   requires identity-policy sids to be alphanumeric — so jest reported
 *   "Tests: 0 total" and no property was exercised. A mutation that breaks
 *   synth tests nothing; if a mutation yields "0 total", check the mutation
 *   before concluding anything about the assertion.
 *
 * M5 — additive wildcard ARN (not an exact '*' element):
 *   Procedure: append `arn:aws:dynamodb:${dmsRegion}:${dmsAccountId}:table/dms-${dmsStage}-*`
 *              to the statement's resources, leaving the per-table ARNs in place.
 *   Result: `never uses a wildcard resource` FAILS.
 *   Why it matters: the original assertion was `not.toContain('*')`, which only
 *              catches an exact `'*'` ELEMENT — this ARN would have passed it.
 *              Review Cycle 1 flagged the gap; the assertion now strips the
 *              permitted `/index/*` suffix and rejects any remaining wildcard.
 *   Restored: yes.
 *
 * All exercised 2026-09-19; see the DMS spec's decisions.md.
 */

import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { VsaCoreStack } from '../lib/vsa-core-stack';

const ACCOUNT = '123456789012';
const REGION = 'us-west-2';
const STAGE = 'staging';

function makeTemplate(): Template {
  const app = new cdk.App();
  const stack = new VsaCoreStack(app, `vsa-${STAGE}-core`, {
    env: { account: ACCOUNT, region: REGION },
    stage: STAGE,
    cmsStage: STAGE,
    // Tier A ConfigValue — a literal override keeps synth hermetic (no SSM).
    cmsKmsKeyArn: {
      literal: `arn:aws:kms:${REGION}:${ACCOUNT}:key/test-key-id`,
      parameterName: `/cvx/${STAGE}/cms-kms-key-arn`,
    },
  });
  return Template.fromStack(stack);
}

/** Every Statement across every IAM policy in the template. */
function allStatements(template: Template): any[] {
  const policies = template.findResources('AWS::IAM::Policy');
  return Object.values(policies).flatMap(
    (p: any) => p.Properties?.PolicyDocument?.Statement ?? [],
  );
}

/**
 * Find the single statement carrying `sid`.
 *
 * Returns undefined rather than asserting, so a missing statement produces a
 * NAMED test failure. An earlier version asserted here; because the helper is
 * called from `describe` bodies, an assertion failure would abort collection
 * and report "Tests: 0 total", identifying no property. (That shape was first
 * seen while running M4 — though the abort in that instance came from an
 * invalid mutation, not from this helper: a sid containing `_` fails CDK's
 * alphanumeric validation, so synth threw before any assertion ran. The
 * lazy-lookup structure is kept regardless, because a genuinely absent
 * statement should name the property it broke.)
 */
function statementBySid(template: Template, sid: string): any | undefined {
  const found = allStatements(template).filter((s) => s.Sid === sid);
  return found.length === 1 ? found[0] : undefined;
}

function asArray<T>(v: T | T[] | undefined): T[] {
  if (v === undefined) return [];
  return Array.isArray(v) ? v : [v];
}

const tableArn = (t: string) =>
  `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/dms-${STAGE}-${t}`;

describe('DmsDynamoDbReadCrossAccount', () => {
  const template = makeTemplate();

  /** Resolve the statement inside each test so absence is a named failure. */
  function stmt(): any {
    const s = statementBySid(template, 'DmsDynamoDbReadCrossAccount');
    expect(s).toBeDefined();
    return s;
  }

  test('grants read on dms-{stage}-avx-pre-briefs (M1 target)', () => {
    expect(asArray<string>(stmt().Resource)).toContain(tableArn('avx-pre-briefs'));
  });

  test('grants read on the avx-pre-briefs indexes', () => {
    // visit_brief queries the table's key, not a GSI, today — but the grant is
    // emitted per-table with an /index/* sibling, so pin both to keep the
    // pattern honest if a GSI read is added later.
    expect(asArray<string>(stmt().Resource)).toContain(
      `${tableArn('avx-pre-briefs')}/index/*`,
    );
  });

  test('DMS read statement never uses a wildcard resource (M2 target)', () => {
    const resources = asArray<string>(stmt().Resource);
    expect(resources).not.toContain('*');
    expect(resources.length).toBeGreaterThan(0);
    const allowedSuffix = '/index/*';
    for (const r of resources) {
      expect(typeof r).toBe('string');
      expect(
        r.startsWith(`arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/dms-${STAGE}-`),
      ).toBe(true);
      // Reject any wildcard other than the per-table `/index/*` suffix. A bare
      // `not.toContain('*')` only catches an exact '*' ELEMENT, so an additive
      // ARN like `.../table/dms-staging-*` would slip through — review Cycle 1
      // flagged exactly that gap.
      const body = r.endsWith(allowedSuffix)
        ? r.slice(0, -allowedSuffix.length)
        : r;
      expect(body).not.toContain('*');
    }
  });

  test('DMS read statement grants no write actions (M3 target)', () => {
    const actions = asArray<string>(stmt().Action);
    expect([...actions].sort()).toEqual(['dynamodb:GetItem', 'dynamodb:Query']);
    for (const a of actions) {
      expect(a).not.toMatch(/dynamodb:\*/);
      expect(a).not.toMatch(/PutItem|UpdateItem|DeleteItem|BatchWrite/);
    }
  });

  test('the pre-existing base visit-briefs grant is retained', () => {
    // Task 3.4 reads avx-pre-briefs as a SUPERSET on top of dm-visit-briefs.
    // Losing the base grant would silently reduce visit_brief to AVX-only.
    expect(asArray<string>(stmt().Resource)).toContain(tableArn('dm-visit-briefs'));
  });
});

describe('deterministic seam — certification tier is not agent-writable', () => {
  const template = makeTemplate();

  test('certification-scores keeps its explicit write Deny (M4 target)', () => {
    const stmt = statementBySid(template, 'DmsCertificationScoresDenyWrite');
    expect(stmt).toBeDefined();
    expect(stmt.Effect).toBe('Deny');
    expect([...asArray<string>(stmt.Action)].sort()).toEqual([
      'dynamodb:DeleteItem',
      'dynamodb:PutItem',
      'dynamodb:UpdateItem',
    ]);
  });

  test('no Allow statement grants write on certification-scores', () => {
    const certArn = tableArn('certification-scores');
    const writes = /PutItem|UpdateItem|DeleteItem|BatchWriteItem|dynamodb:\*/;
    const allowedSuffix = '/index/*';
    for (const s of allStatements(template)) {
      if (s.Effect !== 'Allow') continue;
      const acts = asArray<string>(s.Action);
      if (!acts.some((a) => writes.test(a))) continue;
      for (const r of asArray<string>(s.Resource)) {
        if (typeof r !== 'string') continue;
        expect(r).not.toBe(certArn);
        // Strict equality alone would pass a wildcard-PATTERN resource such as
        // `arn:aws:dynamodb:*:*:table/dms-staging-*`, which grants writes on
        // certification-scores at IAM evaluation time while matching neither
        // literal. Reject any wildcard outside the permitted `/index/*` suffix.
        // Security review Cycle 1 Suggestion 2; same shape as the M5 fix above.
        const body = r.endsWith(allowedSuffix)
          ? r.slice(0, -allowedSuffix.length)
          : r;
        expect(body).not.toContain('*');
      }
    }
  });
});
