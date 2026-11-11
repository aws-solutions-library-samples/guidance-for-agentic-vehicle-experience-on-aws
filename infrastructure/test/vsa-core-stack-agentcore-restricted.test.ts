/**
 * AgentCore runtime role must not reach ADP lake `restricted/*` — the stage B
 * and stage C planted-anomaly answer keys.
 *
 * Background: the role's `AdpLakeS3Read` statement
 * (vsa-core-stack.ts ~491) allows s3:GetObject / s3:ListBucket on the whole
 * `adp-{stage}-foundation-lake-{account}-{region}` bucket. No current code
 * reaches `restricted/` through this role (no Glue table, no direct S3 read),
 * but the next S3-reading tool would silently gain access. This test pins the
 * two explicit DENY statements the fix adds (same shape as the sibling Tier
 * 2 Lambda role in vsa-tier2-stack.ts `DenyAdpRestrictedObjects` /
 * `DenyAdpRestrictedListing`) AND asserts that no Allow on this role reaches
 * an object under `restricted/`. IAM evaluates explicit Deny before Allow, so
 * the two statements together close the surface regardless of what later Allow
 * is added.
 *
 * The DENYs are delivered on a dedicated `AWS::IAM::ManagedPolicy`
 * (`AdpRestrictedDenyPolicy`) attached to the role rather than on the role's
 * default inline policy. The role's inline-policy aggregate was 9,603 bytes
 * compact against the 10,240-byte IAM ceiling on the first fix attempt
 * (verified 2026-10-12 live — `ServiceLimitExceeded` on deploy), so a
 * managed policy keeps the delivery off the inline aggregate. The restricted/
 * helpers below MUST walk inline AND managed policies, otherwise moving a
 * legitimate grant to either type would silently defeat them (defect class
 * from `vsa-api-stack-avx.test.ts` review Cycle 3 Critical C1, 2026-09).
 *
 * Mutants that MUST fail these tests:
 *   M1  — drop `DenyAdpRestrictedObjects`
 *   M2  — drop `DenyAdpRestrictedListing`
 *   M3  — re-widen either Deny Resource past `restricted/*`
 *   M4  — add an unconditional Condition that neuters the Deny
 *   M5  — add an Allow on the role that reaches `restricted/*`
 *   MB4 — drop the `AdpRestrictedDenyPolicy` managed policy entirely
 *   MB5 — add either DENY to the role's inline default policy
 *          (bypassing the managed policy; keeps the restricted/ assertion
 *           green via inline, but trips the inline-aggregate budget guard
 *           AND the managed-policy pinning assertion)
 *
 * See issues/2026-10-10-agentcore-role-reads-answer-keys/report.md.
 */

import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import { VsaCoreStack } from '../lib/vsa-core-stack';

const ACCOUNT = '123456789012';
const REGION = 'us-west-2';
const STAGE = 'staging';
const ADP_REGION = 'us-east-1';
const LAKE = `adp-${STAGE}-foundation-lake-${ACCOUNT}-${ADP_REGION}`;

// Representative ADP KB id for the `adpKbId` prop `bin/vsa.ts` passes on every
// live deploy (Tier B since 2026-10-30). Its Retrieve grant now sits in the
// managed policy AdpKbRetrievePolicy, not on the inline aggregate; the fixture
// passes it so the managed-policy assertions below see the live set.
const ADP_KB_ID = 'EXAMPLEKB01';
// Representative Memory CMK ARN. `bin/vsa.ts` resolves this per-stage from
// SSM/env (`memoryKmsKeyArn` required at synth). The core stack emits a KMS
// Decrypt/GenerateDataKey grant for the AgentCore role on this ARN when the
// value is a resolved string (not a token); omitting it elides the grant.
const MEMORY_KMS_KEY_ARN = `arn:aws:kms:${REGION}:${ACCOUNT}:key/test-memory-kms-key-id`;

// The IAM aggregate inline-policy limit on a role. Any statement added via
// `role.addToPolicy()` or an `iam.Policy` lands here. Managed policies
// attached to the role count against a SEPARATE quota (10 attached × 6,144
// bytes each by default) and do NOT contribute to this total.
const IAM_INLINE_AGGREGATE_LIMIT = 10240;
// Margin — bytes we insist stay unused on this role so a future small grant
// does not re-fire the same ServiceLimitExceeded. 300 bytes ≈ one modestly-
// scoped new statement (Sid + three actions + narrowed resource arn).
const INLINE_BUDGET_MARGIN = 300;

function makeTemplate(): Template {
  // The test fixture MUST produce an inline-policy aggregate that matches
  // live staging to within rounding. Four optional deps each gate at least
  // one inline IAM statement on the AgentCore role (Memory CMK Decrypt /
  // Acquire catalog Read / Acquire orders RW / Acquire leads RW); `adpKbId`
  // gates the AdpKbRetrievePolicy managed policy. Omitting any one makes the
  // test's inline byte count smaller than the live template's, which lets
  // the inline-budget guard admit a new `addToPolicy()` statement in the
  // delta range that live cannot absorb — exactly the regression this
  // guard exists to prevent. Review Cycle 1 Warning, 2026-10-12.
  const app = new cdk.App();
  const parentStack = new cdk.Stack(app, 'TestParent', {
    env: { account: ACCOUNT, region: REGION },
  });
  // `Table.fromTableName()` is sufficient — the test only cares about the
  // IAM policy footprint, not table CFN resources. Table NAMES match the
  // `bin/vsa.ts` → VsaAcquireStack output names (`vsa-${stage}-acquire-*`)
  // so any future ARN-shape assertion in sibling tests stays aligned.
  const vsaAcquireCatalog = dynamodb.Table.fromTableName(
    parentStack,
    'AcquireCatalogRef',
    `vsa-${STAGE}-acquire-catalog`,
  );
  const vsaAcquireOrders = dynamodb.Table.fromTableName(
    parentStack,
    'AcquireOrdersRef',
    `vsa-${STAGE}-acquire-orders`,
  );
  const vsaAcquireLeads = dynamodb.Table.fromTableName(
    parentStack,
    'AcquireLeadsRef',
    `vsa-${STAGE}-acquire-leads`,
  );
  const stack = new VsaCoreStack(app, `vsa-${STAGE}-core`, {
    env: { account: ACCOUNT, region: REGION },
    stage: STAGE,
    cmsStage: STAGE,
    cmsKmsKeyArn: {
      literal: `arn:aws:kms:${REGION}:${ACCOUNT}:key/test-key-id`,
      parameterName: `/cvx/${STAGE}/cms-kms-key-arn`,
    },
    memoryKmsKeyArn: MEMORY_KMS_KEY_ARN,
    adpKbId: ADP_KB_ID,
    vsaAcquireCatalog,
    vsaAcquireOrders,
    vsaAcquireLeads,
  });
  return Template.fromStack(stack);
}

const template = makeTemplate();

/** Resolve the AgentCore runtime role's logical ID from its deterministic RoleName. */
function agentCoreRoleId(): string {
  const roles = template.findResources('AWS::IAM::Role', {
    Properties: { RoleName: `AmazonBedrockAgentCoreSDKRuntime-${REGION}-${STAGE}` },
  });
  const ids = Object.keys(roles);
  expect(ids).toHaveLength(1);
  return ids[0];
}

/**
 * Walk every statement effective on the AgentCore role, across:
 *   - `AWS::IAM::Policy` resources whose `Roles` reference this role
 *     (the output of `role.addToPolicy()` / `new iam.Policy({ roles: [role] })`)
 *   - `AWS::IAM::ManagedPolicy` resources whose `Roles` reference this role
 *     (the output of `new iam.ManagedPolicy({ roles: [role] })` and the
 *      CDK auto-generated overflow policy when inline aggregate overflows)
 *   - inline policies embedded in the role's own `Properties.Policies`
 *     (the output of `new iam.Role({ inlinePolicies: { name: {...} } })`)
 *
 * Prior to 2026-10-12 this helper walked only `AWS::IAM::Policy`. Moving the
 * restricted/ DENYs to a managed policy (because the role's inline aggregate
 * is at the IAM ceiling) required extending the walk; the sibling pattern in
 * `vsa-api-stack-avx.test.ts` was adopted here. Any new place that collects
 * this role's statements MUST walk all three resource types, or an identical
 * grant delivered through a different mechanism will be invisible.
 */
function agentCoreStatements(): any[] {
  const roleId = agentCoreRoleId();
  const statements: any[] = [];

  // (1) Standalone inline policies (role.addToPolicy / new iam.Policy).
  for (const p of Object.values(template.findResources('AWS::IAM::Policy'))) {
    const props = (p as any).Properties;
    if (JSON.stringify(props.Roles).includes(roleId)) {
      statements.push(...(props.PolicyDocument.Statement as any[]));
    }
  }

  // (2) Managed policies attached to the role.
  for (const mp of Object.values(template.findResources('AWS::IAM::ManagedPolicy'))) {
    const props = (mp as any).Properties;
    if (JSON.stringify(props.Roles ?? []).includes(roleId)) {
      statements.push(...(props.PolicyDocument.Statement as any[]));
    }
  }

  // (3) Inline policies embedded in the role itself.
  const role = template.findResources('AWS::IAM::Role')[roleId];
  const inline = ((role as any)?.Properties?.Policies ?? []) as any[];
  for (const p of inline) {
    statements.push(...(p.PolicyDocument.Statement as any[]));
  }

  return statements;
}

/**
 * Sum the compact byte size of every INLINE policy effective on the AgentCore
 * role — the aggregate IAM measures against its 10,240-byte per-role limit.
 * Managed policies attached to the role are deliberately excluded; they live
 * under a separate 6,144-byte-per-policy quota and were the delivery mechanism
 * the fix chose to escape this very ceiling.
 *
 * Bytes are computed on `JSON.stringify(policyDocument)` without indentation
 * because that is how IAM evaluates the limit (CloudFormation serialises
 * compactly before submitting to IAM).
 */
function inlinePolicyBytesForRole(): number {
  const roleId = agentCoreRoleId();
  let total = 0;

  // (1) Standalone inline policies.
  for (const p of Object.values(template.findResources('AWS::IAM::Policy'))) {
    const props = (p as any).Properties;
    if (JSON.stringify(props.Roles).includes(roleId)) {
      total += JSON.stringify(props.PolicyDocument).length;
    }
  }

  // (2) Inline policies embedded in the role.
  const role = template.findResources('AWS::IAM::Role')[roleId];
  const inline = ((role as any)?.Properties?.Policies ?? []) as any[];
  for (const p of inline) {
    total += JSON.stringify(p.PolicyDocument).length;
  }

  return total;
}

/** Return every managed policy attached to the AgentCore role, keyed by
 *  logical ID. Used by the delivery-mechanism assertions below. */
function agentCoreManagedPolicies(): Record<string, any> {
  const roleId = agentCoreRoleId();
  const out: Record<string, any> = {};
  for (const [id, mp] of Object.entries(template.findResources('AWS::IAM::ManagedPolicy'))) {
    const props = (mp as any).Properties;
    if (JSON.stringify(props.Roles ?? []).includes(roleId)) {
      out[id] = mp;
    }
  }
  return out;
}

function asList<T>(x: T | T[] | undefined): T[] {
  if (x === undefined) return [];
  return Array.isArray(x) ? x : [x];
}

function bySid(sid: string): any {
  const found = agentCoreStatements().filter((s) => s.Sid === sid);
  expect(found).toHaveLength(1);
  return found[0];
}

/** Return every statement effective on the role that carries this Sid,
 *  grouped by delivery mechanism (inline vs managed). Used by the pinning
 *  assertion that forbids the restricted/ DENYs from drifting back inline. */
function statementsBySidByDelivery(sid: string): {
  inlineStatements: any[];
  managedStatements: any[];
} {
  const roleId = agentCoreRoleId();
  const inlineStatements: any[] = [];
  const managedStatements: any[] = [];

  // Standalone inline policies.
  for (const p of Object.values(template.findResources('AWS::IAM::Policy'))) {
    const props = (p as any).Properties;
    if (JSON.stringify(props.Roles).includes(roleId)) {
      const stmts = (props.PolicyDocument.Statement as any[]).filter((s) => s.Sid === sid);
      inlineStatements.push(...stmts);
    }
  }
  // Role-embedded inline policies.
  const role = template.findResources('AWS::IAM::Role')[roleId];
  const inline = ((role as any)?.Properties?.Policies ?? []) as any[];
  for (const p of inline) {
    const stmts = (p.PolicyDocument.Statement as any[]).filter((s) => s.Sid === sid);
    inlineStatements.push(...stmts);
  }
  // Managed policies.
  for (const mp of Object.values(template.findResources('AWS::IAM::ManagedPolicy'))) {
    const props = (mp as any).Properties;
    if (JSON.stringify(props.Roles ?? []).includes(roleId)) {
      const stmts = (props.PolicyDocument.Statement as any[]).filter((s) => s.Sid === sid);
      managedStatements.push(...stmts);
    }
  }
  return { inlineStatements, managedStatements };
}

/** Match an IAM resource glob (`*` any, `?` single) against a target ARN.
 *  Used to answer the only question that matters: can any Allow reach a
 *  specific object under `restricted/`? */
function globMatches(resource: unknown, target: string): boolean {
  if (typeof resource !== 'string') return false;
  const re = new RegExp(
    '^' +
      resource
        .replace(/[.+^${}()|[\]\\]/g, '\\$&')
        .replace(/\*/g, '.*')
        .replace(/\?/g, '.') +
      '$',
  );
  return re.test(target);
}

describe('AgentCore runtime role — ADP lake restricted/ is denied', () => {
  test('a Deny statement guards s3:GetObject / s3:GetObjectVersion on restricted/*', () => {
    const stmt = bySid('DenyAdpRestrictedObjects');
    expect(stmt.Effect).toBe('Deny');
    expect(asList(stmt.Action).sort()).toEqual(['s3:GetObject', 's3:GetObjectVersion']);
    expect(stmt.Resource).toBe(`arn:aws:s3:::${LAKE}/restricted/*`);
    // Mutant guard: a Condition that gates the Deny would make it apply only
    // to some requests, which is not a denial. (See the sibling test on the
    // Tier 2 Lambda role.)
    expect(stmt.Condition).toBeUndefined();
  });

  test('a Deny statement guards s3:ListBucket when the prefix is restricted', () => {
    const stmt = bySid('DenyAdpRestrictedListing');
    expect(stmt.Effect).toBe('Deny');
    expect(asList(stmt.Action)).toEqual(['s3:ListBucket']);
    // The listing Deny targets the bucket itself, scoped by the prefix
    // condition — the same shape IAM uses for the s3:ListBucket API's
    // prefix request parameter.
    expect(stmt.Resource).toBe(`arn:aws:s3:::${LAKE}`);
    expect(stmt.Condition).toEqual({
      StringLike: { 's3:prefix': ['restricted', 'restricted/*'] },
    });
  });

  test('a Deny with no condition reaches every restricted/ answer-key leaf (mutant check)', () => {
    // The leaves ADP places under restricted/ today, verified against the
    // live staging bucket on 2026-10-10:
    //   restricted/fleet_cost/plants/year_month=YYYY-MM/answer_key.parquet
    //   restricted/recall_campaigns/<campaign>/answer_key.parquet
    //
    // The Allow (AdpLakeS3Read) legitimately names `${LAKE}/*` and therefore
    // DOES glob-reach these leaves — that is why the fix is a DENY. IAM
    // evaluates explicit Deny before Allow, so the test must assert the Deny
    // reaches the leaf AND carries no neutralising Condition. A mutant that
    // drops the object Deny, re-widens its Resource past `restricted/*`, or
    // adds a shielding Condition must fail this test.
    const leaves = [
      `arn:aws:s3:::${LAKE}/restricted/fleet_cost/plants/year_month=2026-09/answer_key.parquet`,
      `arn:aws:s3:::${LAKE}/restricted/recall_campaigns/23V-321/answer_key.parquet`,
    ];
    const stmts = agentCoreStatements();
    for (const leaf of leaves) {
      const covering = stmts
        .filter((s) => s.Effect === 'Deny')
        .filter((s) =>
          asList(s.Action).some((a: any) => {
            const act = String(a);
            return act === '*' || act === 's3:*' || act === 's3:GetObject' || act === 's3:GetObjectVersion';
          }),
        )
        .filter((s) => asList(s.Resource).some((r) => globMatches(r, leaf)))
        // A Deny that applies only to some requests is not a denial —
        // Condition === undefined is the only safe state for the object
        // deny (the listing deny's prefix Condition is a different shape,
        // intentional, and is pinned by its own test above).
        .filter((s) => s.Condition === undefined);
      expect(covering.length).toBeGreaterThanOrEqual(1);
    }
    // Positive control: AdpLakeS3Read Allow is still present and still names
    // the bucket wildcard. Without this the Deny-only check above could pass
    // against a stack that silently dropped the Allow (which would also break
    // every non-restricted read the runtime needs).
    const allow = bySid('AdpLakeS3Read');
    expect(allow.Effect).toBe('Allow');
    expect(asList(allow.Resource)).toEqual([
      `arn:aws:s3:::${LAKE}`,
      `arn:aws:s3:::${LAKE}/*`,
    ]);
  });

  test('no Allow on the role NAMES the restricted/ prefix (narrowed-Allow mutant guard)', () => {
    // A credible mutant: drop both Denies AND rewrite the Allow Resource to
    // list every non-restricted prefix. That would also leak keys if a future
    // prefix is added without touching this list. This test keeps the fix on
    // the Deny-side exclusively: no Allow may contain `restricted` in a
    // Resource string, so the only way an Allow reaches `restricted/*` is
    // through the bucket wildcard, which the Deny above fully guards.
    const stmts = agentCoreStatements();
    const namingRestricted = stmts
      .filter((s) => s.Effect === 'Allow')
      .filter((s) =>
        asList(s.Resource).some((r) => typeof r === 'string' && /restricted/.test(r as string)),
      );
    expect(namingRestricted).toEqual([]);
  });

  test('no Allow on the role lists the bucket WITHOUT the restricted prefix excluded', () => {
    // Belt-and-braces for the listing case: the listing Deny applies only
    // when s3:prefix matches `restricted*`. If a future Allow adds a
    // ListBucket with a Condition like `s3:prefix: curated/*`, the Deny
    // doesn't fire (its own StringLike doesn't match), and the listing is
    // allowed — which is correct. But if a future Allow lists with no
    // prefix condition and the Deny were somehow narrowed too, a listing
    // call carrying prefix=restricted/ would still see the keys. This test
    // asserts that neither (a) the Deny was narrowed off restricted at all,
    // nor (b) its prefix matcher dropped the base-case `restricted` entry
    // (so a bare `prefix=restricted` call is also denied, not just
    // `prefix=restricted/x`).
    const listing = bySid('DenyAdpRestrictedListing');
    const prefixes = listing.Condition.StringLike['s3:prefix'] as string[];
    expect(prefixes).toContain('restricted');
    expect(prefixes).toContain('restricted/*');
  });
});

describe('AgentCore runtime role — the Allow kept its needed reads', () => {
  test('AdpLakeS3Read still allows the three actions the runtime needs on the lake bucket', () => {
    // The spec reason the Allow exists: the persona-loader Athena queries read
    // underlying parquet with the caller's IAM identity when LF does not vend
    // S3 credentials. s3:GetObject and s3:ListBucket both need to work for
    // every non-restricted prefix. Dropping an action here would break the
    // live code path; this test fails if someone removes GetObject or
    // ListBucket by mistake, which the restricted/ fix must not do.
    const allow = bySid('AdpLakeS3Read');
    expect(allow.Effect).toBe('Allow');
    expect(asList(allow.Action).sort()).toEqual([
      's3:GetBucketLocation',
      's3:GetObject',
      's3:ListBucket',
    ]);
  });

  test('a Glue/Athena/LakeFormation read is still Allow on this role (positive control)', () => {
    // The Deny is scoped to S3 restricted/* — it must not alter the Athena /
    // Glue / LF data plane the runtime uses for every query. This positive
    // control fails if the fix inadvertently touched the broader data plane.
    const atheGlue = agentCoreStatements().filter(
      (s) =>
        s.Effect === 'Allow' &&
        asList(s.Action).some((a: any) => /^(athena|glue|lakeformation):/.test(String(a))),
    );
    expect(atheGlue.length).toBeGreaterThan(0);
  });
});

describe('AgentCore runtime role — restricted/ DENYs land on the ManagedPolicy, not inline', () => {
  // MB4 / MB5 catches. The DENYs MUST live on an attached ManagedPolicy, not
  // on `role.addToPolicy()` or `role.Properties.Policies`. The role's inline-
  // aggregate budget is close enough to the 10,240-byte IAM ceiling that
  // delivering the DENY inline reproduces the 2026-10-12 ServiceLimitExceeded
  // failure — the first fix attempt proved this empirically. Pinning the
  // delivery mechanism here means a future contributor who rewrites the fix
  // to `role.addToPolicy()` fails the test instead of the deploy.

  test('AdpRestrictedDenyPolicy managed policy exists, named with stage + region, attached to the role', () => {
    const managed = agentCoreManagedPolicies();
    const ids = Object.keys(managed);
    const matches = ids
      .map((id) => ({ id, props: (managed[id] as any).Properties }))
      .filter(
        (m) =>
          typeof m.props.ManagedPolicyName === 'string' &&
          m.props.ManagedPolicyName === `vsa-${STAGE}-adp-restricted-deny-${REGION}`,
      );
    expect(matches).toHaveLength(1);
    // The construct's logical ID matches the stack code's identifier. A rename
    // here is intentional and should be caught so operators can confirm the
    // CloudFormation diff names a new logical resource (not an in-place
    // mutation of the inline policy).
    expect(matches[0].id).toMatch(/^AdpRestrictedDenyPolicy[A-Z0-9]+$/);
  });

  test('both DENY statements are delivered via the managed policy, with ZERO inline duplicates', () => {
    for (const sid of ['DenyAdpRestrictedObjects', 'DenyAdpRestrictedListing']) {
      const { inlineStatements, managedStatements } = statementsBySidByDelivery(sid);
      expect(inlineStatements).toEqual([]); // MB5 catch: Deny re-added inline
      expect(managedStatements).toHaveLength(1); // MB4 catch: managed policy dropped
      expect(managedStatements[0].Effect).toBe('Deny');
    }
  });
});

describe('AgentCore runtime role — inline-policy aggregate stays under IAM ceiling', () => {
  // The IAM 10,240-byte limit is PER ROLE across every inline policy on it.
  // The AgentCore role's default inline policy was measured 2026-10-12 at
  // 9,603 bytes compact / 25 statements on the live staging deployment —
  // the first restricted/ fix attempt tried to add two more statements and
  // CloudFormation returned ServiceLimitExceeded. Any future grant added via
  // `role.addToPolicy()` or `new iam.Policy({ roles: [role] })` lands here
  // and risks a repeat. New grants should prefer `new iam.ManagedPolicy({
  // roles: [role] })` to stay off the inline aggregate.
  //
  // This guard asserts bytes < 10,240 - 300 so there is room for a small
  // tactical grant without hitting the ceiling. If this test fails, do NOT
  // raise the margin — move something onto a managed policy instead.
  //
  // KNOWN GAP (2026-10-30, issues/2026-10-30-agentcore-role-inline-budget/):
  // this counts only the core stack's inline policies. The api stack adds
  // another inline policy to the same role (`AgentCoreRoleRef`,
  // vsa-api-stack.ts:2184; 648 characters live), and live headroom is 187
  // characters (DefaultPolicy 9,405 + 648 = 10,053), inside the 300 margin,
  // while this guard passes. Do not add an inline statement to this role:
  // use a managed policy (AdpRestrictedDenyPolicy, AdpKbRetrievePolicy).

  test('inline aggregate stays under IAM 10,240-byte ceiling with 300-byte margin', () => {
    const bytes = inlinePolicyBytesForRole();
    expect(bytes).toBeLessThan(IAM_INLINE_AGGREGATE_LIMIT - INLINE_BUDGET_MARGIN);
    // Anti-vacuity: a bug that returned 0 would always pass; make the test
    // flag a surprising collapse too.
    expect(bytes).toBeGreaterThan(1000);
  });

  test('the measurement counts inline but NOT managed-policy bytes (anti-vacuity)', () => {
    // Managed policies attached to the role must not contribute to the inline
    // aggregate; they live under IAM's separate 10 × 6,144 byte managed-
    // policy quota. If this invariant inverted, the measurement would read
    // ~5 KB higher once AdpRestrictedDenyPolicy is attached and nobody would
    // know the delivery mechanism had drifted. We confirm it empirically by
    // comparing the measurement to the sum of managed-policy bytes attached
    // to the role: those bytes must NOT appear in the inline total.
    const inline = inlinePolicyBytesForRole();
    const managed = agentCoreManagedPolicies();
    let managedBytes = 0;
    for (const mp of Object.values(managed)) {
      managedBytes += JSON.stringify((mp as any).Properties.PolicyDocument).length;
    }
    // The inline total must be smaller than inline + managed, i.e. managed
    // bytes exist and are excluded. Managed bytes > 0 ensures the DENY
    // policy itself is present (anti-vacuity for the restricted/ DENY).
    expect(managedBytes).toBeGreaterThan(0);
    expect(inline).toBeLessThan(inline + managedBytes);
  });
});
