/**
 * Regression tests for `issues/2026-09-21-cdk-asset-hash-drift-unrelated-lambdas/`.
 *
 * Defect: every `lambda.Code.fromAsset(dir)` in `vsa-api-stack.ts` was called with no
 * `exclude`, so CDK hashed the whole directory -- including any `__pycache__` written by a
 * local `pytest` run. Running the test suite was therefore enough to change the
 * `Code.S3Key` of Lambdas whose tracked source had not changed, and a one-line edit
 * produced a 9-Lambda `cdk diff`.
 *
 * These tests assert the PROPERTY (a `__pycache__` write does not move the hash), not the
 * PRESENCE of an `exclude` option. An assertion that only checked the construct props
 * would pass against an exclude list that excluded the wrong thing.
 *
 * Test roles:
 *   A  anti-vacuity  -- the assertion target is a real content hash, not a CFN token.
 *   B  the property  -- injecting `__pycache__` does NOT move the hash.
 *   B2 the property  -- injecting a `tests/` dir does NOT move the hash. Separate from B
 *                       because the two are covered by different exclude entries, and
 *                       mutation M2 showed B is blind to which one is doing the work.
 *   C  positive ctl  -- editing `handler.py` DOES move the hash. Without C, B passes
 *                       just as happily when synth is returning a cached hash and
 *                       reading nothing from disk at all.
 *   D  drift guard   -- no bare `lambda.Code.fromAsset(` returns to the stack file.
 *
 * ── Why C is not optional ─────────────────────────────────────────────────────
 * `AssetStaging` memoises staging in a process-wide static cache whose key is
 * {outdir, sourcePath, bundling, assetHashType, customFingerprint, extraHash, exclude,
 * ignoreMode, skip} -- verified by reading aws-cdk-lib@2.269.0's `asset-staging.js`. The
 * directory's content is NOT part of that key. A same-process re-synth that reused an
 * outdir would hand back the first hash it ever computed, and B would pass while proving
 * nothing. Each `makeStack()` builds a fresh `cdk.App` (hence a fresh outdir) and
 * `assetHashOf()` additionally calls `clearAssetHashCache()`; C is what demonstrates the
 * combination actually re-reads the filesystem.
 *
 * ── Mutation results (required by ~/.kiro/steering/testing.md) ────────────────
 * All six mutations below were executed 2026-09-21, not reasoned about. Two of them
 * refuted what this comment originally claimed; the corrected findings are recorded
 * verbatim because the surprise is the useful part. Full transcript in the issue's
 * `summary.md`.
 *
 *   M1 -- remove `exclude` from `pythonLambdaAsset()`, restoring the pre-fix
 *         `Code.fromAsset(assetPath)` behaviour.
 *         Expected B to fail. Observed: B FAILED, A/C/D unaffected. Restored.
 *
 *   M2 -- drop the `__pycache__` glob only, keeping the `.pyc` glob.
 *         Expected B to fail. Observed: **B PASSED**; only D's presence check failed.
 *         The `.pyc` glob alone already excludes the injected file, and the residual
 *         empty directory does not move a CDK fingerprint. Restored.
 *
 *   M2' - drop the `.pyc` glob only, keeping the `__pycache__` glob.
 *         Observed: **B PASSED** -- the directory glob covers the file recursively.
 *         Restored.
 *
 *   M2''- drop BOTH pycache globs, keeping `tests/**` and `.pytest_cache/**`.
 *         Observed: B FAILED. Restored.
 *
 *         M2/M2'/M2'' together establish that the two pycache globs are each
 *         INDIVIDUALLY SUFFICIENT and therefore mutually REDUNDANT for this property:
 *         no single-entry deletion can break B. Both are kept deliberately -- the pair
 *         matches the exclude list already in `vsa-tier2-stack.ts`, and the redundancy
 *         is free. Do not "simplify" by deleting one on the theory that a test will
 *         catch it; none will. That is a documented property of this list, not a gap.
 *
 *   M4 -- drop `tests/**`.
 *         Observed: B2 FAILED (B unaffected). Unlike the pycache pair, `tests/**` is
 *         NECESSARY -- it is the only entry covering the 16 `tests/` directories under
 *         `lambdas/`. B2 was added specifically because M2 revealed that a
 *         presence-only assertion was all that stood behind this entry.
 *
 *   M3 -- reintroduce one bare `lambda.Code.fromAsset(` at the ApiTriage site.
 *         Expected D to fail. Observed: B, B2, D-bare AND D-count all FAILED -- four
 *         tests, because ApiTriage is also B/B2's subject, so that asset immediately
 *         regains its sensitivity to the injected artifacts. Restored.
 *
 * ── Second round, after a real leak was found in synth output (2026-09-21) ────
 * Inspecting an actual `cdk synth` of `vsa-staging-api` showed
 * `shared/tests/test_cms_client_real.py` shipping inside the api-vehicle-live-state
 * asset -- an in-scope site -- while B and B2 were green. Neither injected a NESTED
 * directory, so neither could see it. A recursive-prefixed `tests` entry (written in the
 * exclude array itself, not quotable here) was added to the list and B3 was added to
 * guard it. Re-verified after the change:
 *
 *   M5 -- drop the recursive-prefixed `tests` entry only.
 *         Expected B3 to fail. Observed: **B3 PASSED**. Wrong again: a bare `tests`
 *         entry matches at any depth gitignore-style, so it alone still covers the
 *         nested case.
 *
 *   M6 -- drop bare `tests` only.
 *         Observed: B2 and B3 both PASSED -- the recursive-prefixed entry covers it
 *         from the other side.
 *
 *   M7 -- drop BOTH the bare and recursive-prefixed `tests` entries, leaving only the
 *         root-anchored `tests` glob.
 *         Observed: **B3 FAILED**, B2 passed. This is the mutation B3 exists to catch.
 *
 *         So the bare and recursive-prefixed entries are individually sufficient and
 *         mutually redundant, exactly like the pycache pair above. And B2 passing under
 *         M7 is correct, not a gap: the root-anchored glob excludes root-level test
 *         *contents*, and the residual empty directory does not move a fingerprint --
 *         so root-level test CODE was never shipping. The only real leak was nested.
 *
 * Two notes on writing this file, both learned the hard way. Three times a mutation has
 * refuted an assumption about which exclude entry was doing the work (M2, M5, M6) --
 * reading the list is not a substitute for deleting an entry and watching what fails.
 * And a recursive-glob prefix ends with the two characters that close a block comment,
 * so the patterns are described here in words rather than quoted; quoting one truncates
 * this comment and breaks the file. That happened three times in one session, including
 * once while writing this very warning.
 */

import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as kms from 'aws-cdk-lib/aws-kms';
import { VsaApiStack } from '../lib/vsa-api-stack';
import { PYTHON_ASSET_EXCLUDES } from '../lib/python-lambda-asset';

// ─── Subjects ─────────────────────────────────────────────────────────────────

/**
 * `api-triage` is the subject because its source directory contains exactly one file
 * (`handler.py`) and no pre-existing `__pycache__` or `tests/`, so the test creates and
 * removes every artifact it depends on. A subject that already carried a `__pycache__`
 * would make B's setup a no-op and hide a broken exclude list.
 */
const TRIAGE_DIR = path.join(__dirname, '..', '..', 'lambdas', 'api-triage');
const TRIAGE_HANDLER = path.join(TRIAGE_DIR, 'handler.py');
const TRIAGE_FUNCTION_NAME = 'vsa-staging-api-triage';

const STACK_SOURCE = path.join(__dirname, '..', 'lib', 'vsa-api-stack.ts');

// ─── Stack fixture ────────────────────────────────────────────────────────────
//
// Deliberately duplicated from `vsa-api-stack-avx.test.ts` rather than imported: that
// file is owned by an in-flight spec, and importing from it would couple this guard to
// another session's edits. ~40 lines of prop stubs is the cheaper trade.

let stackCount = 0;

function makeStack(stage = 'staging'): Template {
  const id = ++stackCount;
  const app = new cdk.App();
  const env = { account: '123456789012', region: 'us-west-2' };

  const parentStack = new cdk.Stack(app, `ParentFixture${id}`, { env });
  const stubTable = (tid: string, name: string) =>
    dynamodb.Table.fromTableName(parentStack, tid, name);
  const stubFn = (fid: string, arn: string) =>
    lambda.Function.fromFunctionArn(parentStack, fid, arn);

  const stack = new VsaApiStack(app, `vsa-${stage}-api-${id}`, {
    env,
    stage,
    cmsStage: stage,
    tenantConfigTable: stubTable('TenantCfg', `cms-${stage}-tenant-config`),
    classifier: stubFn(
      'Classifier',
      `arn:aws:lambda:us-west-2:123456789012:function:vsa-${stage}-triage-classifier`,
    ),
    cmsKmsKeyArn: {
      literal: 'arn:aws:kms:us-west-2:123456789012:key/test-key',
      parameterName: `/cvx/${stage}/cms-kms-key-arn`,
    },
    redisEndpoint: {
      literal: 'redis://stub.redis.local:6379',
      parameterName: `/cvx/${stage}/redis-endpoint`,
    },
    connectInstanceArn: { literal: 'arn:aws:connect:us-east-1:123456789012:instance/test-instance', parameterName: `/cvx/${stage}/connect-instance-arn` },
    dmsApiEndpoint: {
      literal: 'https://dms.example.invalid/staging',
      parameterName: `/cvx/${stage}/dms-api-endpoint`,
    },
    vpcId: 'vpc-00000000000000000',
    redisSecurityGroupId: 'sg-00000000000000000',
    assistantChatRuntimeArn: {
      literal: `arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/stub-${stage}`,
      parameterName: `/cvx/${stage}/assistant-chat-runtime-arn`,
    },
    assistantChatRuntimeRegion: 'us-west-2',
    allowedOrigin: {
      literal: 'https://stub.example.com',
      parameterName: `/cvx/${stage}/allowed-origin`,
    },
    acquireReservationHandoffFn: stubFn(
      'AcquireHandoff',
      `arn:aws:lambda:us-west-2:123456789012:function:vsa-${stage}-acquire-handoff`,
    ),
    acquireReservationHandoffFnArnExportName: `vsa-${stage}-acquire-reservation-handoff-fn-arn`,
    acquireCatalogTable: stubTable('AcquireCatalog', `vsa-${stage}-acquire-catalog`),
    acquireOrdersTable: stubTable('AcquireOrders', `vsa-${stage}-acquire-orders`),
    acquireLeadsTable: stubTable('AcquireLeads', `vsa-${stage}-acquire-leads`),
    avxFindingsTableNameExportName: `vsa-${stage}-avx-findings-table-name`,
    avxActionsTableNameExportName: `vsa-${stage}-avx-actions-table-name`,
    avxFeedbackTableNameExportName: `vsa-${stage}-avx-feedback-table-name`,
    notificationDevicesTableNameExportName: `vsa-${stage}-notification-devices-table-name`,
    // RSA dispatch table — required (F4.6). fromTableArn so the stub has a
    // concrete ARN for the PutItem grant scope assertion.
    rsaDispatchTable: dynamodb.Table.fromTableArn(
      parentStack,
      `RsaDispatch${id}`,
      `arn:aws:dynamodb:us-west-2:123456789012:table/vsa-${stage}-rsa-dispatch`,
    ),
    rsaDispatchKey: kms.Key.fromKeyArn(parentStack, `RsaDispatchKey${id}`, 'arn:aws:kms:us-west-2:123456789012:key/test-rsa-dispatch-key'),
  });

  return Template.fromStack(stack);
}

/** Synthesizes fresh (no memoised staging) and returns the named Lambda's `Code.S3Key`. */
function assetHashOf(functionName: string): string {
  cdk.AssetStaging.clearAssetHashCache();
  const template = makeStack();
  const functions = template.findResources('AWS::Lambda::Function');

  for (const resource of Object.values(functions)) {
    const props = (resource as Record<string, unknown>).Properties as
      | Record<string, unknown>
      | undefined;
    if (props?.FunctionName !== functionName) continue;
    const code = props.Code as Record<string, unknown>;
    const s3Key = code?.S3Key;
    if (typeof s3Key !== 'string') {
      throw new Error(
        `${functionName} Code.S3Key is ${JSON.stringify(s3Key)}, not a string -- the ` +
          'assertion target is a token, so these tests cannot compare content hashes.',
      );
    }
    return s3Key;
  }
  throw new Error(
    `No AWS::Lambda::Function named ${functionName} in the synthesized template. If the ` +
      'function was renamed, update TRIAGE_FUNCTION_NAME -- do not delete this test.',
  );
}

// ─── Source-tree mutation helpers ─────────────────────────────────────────────

/** Runs `body` with a junk `__pycache__/<junk>.pyc` present in `dir`, then cleans up. */
function withInjectedPycache<T>(dir: string, body: () => T): T {
  const cacheDir = path.join(dir, '__pycache__');
  const junkFile = path.join(cacheDir, 'handler.cpython-312.regression-marker.pyc');
  const createdDir = !fs.existsSync(cacheDir);

  if (createdDir) fs.mkdirSync(cacheDir);
  // Bytes chosen to look like a .pyc: a magic-number-ish header plus filler. Content is
  // irrelevant to the assertion; only its presence in the hashed tree matters.
  fs.writeFileSync(junkFile, Buffer.from([0x6f, 0x0d, 0x0d, 0x0a, 0x00, 0x01, 0x02, 0x03]));

  try {
    return body();
  } finally {
    fs.rmSync(junkFile, { force: true });
    if (createdDir) fs.rmSync(cacheDir, { recursive: true, force: true });
  }
}

/** Runs `body` with a junk `tests/` dir present in `dir`, then cleans up. */
function withInjectedTestsDir<T>(dir: string, body: () => T): T {
  const testsDir = path.join(dir, 'tests');
  const junkFile = path.join(testsDir, 'test_asset_hash_regression_marker.py');
  const createdDir = !fs.existsSync(testsDir);

  if (createdDir) fs.mkdirSync(testsDir);
  fs.writeFileSync(junkFile, '# asset-hash regression marker\ndef test_noop():\n    pass\n');

  try {
    return body();
  } finally {
    fs.rmSync(junkFile, { force: true });
    if (createdDir) fs.rmSync(testsDir, { recursive: true, force: true });
  }
}

/** Runs `body` with a junk NESTED test dir (`<dir>/sub/tests/`) present, then cleans up. */
function withInjectedNestedTestsDir<T>(dir: string, body: () => T): T {
  const subDir = path.join(dir, 'sub');
  const testsDir = path.join(subDir, 'tests');
  const junkFile = path.join(testsDir, 'test_nested_marker.py');
  const createdSub = !fs.existsSync(subDir);

  fs.mkdirSync(testsDir, { recursive: true });
  fs.writeFileSync(junkFile, '# nested asset-hash regression marker\n');

  try {
    return body();
  } finally {
    if (createdSub) fs.rmSync(subDir, { recursive: true, force: true });
    else fs.rmSync(testsDir, { recursive: true, force: true });
  }
}

/** Runs `body` with a trailing comment appended to `file`, then restores it byte-exactly. */
function withAppendedComment<T>(file: string, body: () => T): T {
  const original = fs.readFileSync(file);
  try {
    fs.writeFileSync(file, Buffer.concat([original, Buffer.from('\n# asset-hash regression marker\n')]));
    return body();
  } finally {
    fs.writeFileSync(file, original);
  }
}

// Second safety net: if an assertion throws somewhere a `finally` cannot reach, restore
// the handler from the bytes captured before any test ran.
const HANDLER_BYTES_AT_START = fs.readFileSync(TRIAGE_HANDLER);
afterAll(() => {
  if (!fs.readFileSync(TRIAGE_HANDLER).equals(HANDLER_BYTES_AT_START)) {
    fs.writeFileSync(TRIAGE_HANDLER, HANDLER_BYTES_AT_START);
    throw new Error(
      'handler.py was left modified by this suite and has been restored. A test leaked a ' +
        'source-tree mutation -- fix the finally block before trusting these results.',
    );
  }
  fs.rmSync(path.join(TRIAGE_DIR, '__pycache__'), { recursive: true, force: true });
  fs.rmSync(path.join(TRIAGE_DIR, 'tests'), { recursive: true, force: true });
  fs.rmSync(path.join(TRIAGE_DIR, 'sub'), { recursive: true, force: true });
});

// ─── A. Anti-vacuity ──────────────────────────────────────────────────────────

describe('A. the assertion target is a real content hash', () => {
  test('Code.S3Key is a literal <sha256>.zip, not a CFN token', () => {
    expect(assetHashOf(TRIAGE_FUNCTION_NAME)).toMatch(/^[0-9a-f]{64}\.zip$/);
  });
});

// ─── B. The property ─────────────────────────────────────────────────────────

describe('B. a __pycache__ write does not move an unrelated asset hash', () => {
  test('injecting __pycache__ into api-triage leaves its Code.S3Key unchanged', () => {
    const before = assetHashOf(TRIAGE_FUNCTION_NAME);
    const during = withInjectedPycache(TRIAGE_DIR, () => assetHashOf(TRIAGE_FUNCTION_NAME));

    expect(during).toBe(before);
  });

  // B2 exists because `'tests/**'` was otherwise asserted only by the presence check in
  // D, and 16 `tests/` directories live under `lambdas/`. A list that covered
  // `__pycache__` but silently stopped covering `tests/` would have passed B alone.
  test('injecting a tests/ dir into api-triage leaves its Code.S3Key unchanged', () => {
    const before = assetHashOf(TRIAGE_FUNCTION_NAME);
    const during = withInjectedTestsDir(TRIAGE_DIR, () => assetHashOf(TRIAGE_FUNCTION_NAME));

    expect(during).toBe(before);
  });

  // B3 exists because the original exclude list MISSED this case and shipped
  // `shared/tests/test_cms_client_real.py` inside the api-vehicle-live-state asset.
  // `tests/**` and `tests` are root-anchored; only a `**/`-prefixed pattern matches at
  // depth. B and B2 both passed while that leak was live, so neither could have caught
  // it — it was found by inspecting real synth output. This test closes that gap.
  test('injecting a NESTED sub/tests/ dir leaves its Code.S3Key unchanged', () => {
    const before = assetHashOf(TRIAGE_FUNCTION_NAME);
    const during = withInjectedNestedTestsDir(TRIAGE_DIR, () =>
      assetHashOf(TRIAGE_FUNCTION_NAME),
    );

    expect(during).toBe(before);
  });
});

// ─── C. Positive control ─────────────────────────────────────────────────────

describe('C. real source edits still move the asset hash', () => {
  test('editing handler.py changes api-triage Code.S3Key', () => {
    const before = assetHashOf(TRIAGE_FUNCTION_NAME);
    const during = withAppendedComment(TRIAGE_HANDLER, () => assetHashOf(TRIAGE_FUNCTION_NAME));
    const after = assetHashOf(TRIAGE_FUNCTION_NAME);

    // If this fails, B is meaningless: synth is not reading the filesystem.
    expect(during).not.toBe(before);
    // And the restore must be byte-exact, or the suite has a side effect.
    expect(after).toBe(before);
  });
});

// ─── D. Drift guard ──────────────────────────────────────────────────────────

describe('D. the helper cannot be bypassed by the next Lambda added', () => {
  test('vsa-api-stack.ts contains no bare lambda.Code.fromAsset( call', () => {
    const source = fs.readFileSync(STACK_SOURCE, 'utf8');
    const bare = source.match(/\blambda\.Code\.fromAsset\(/g) ?? [];

    expect(bare).toHaveLength(0);
  });

  test('every Python asset in the stack routes through pythonLambdaAsset', () => {
    const source = fs.readFileSync(STACK_SOURCE, 'utf8');
    const routed = source.match(/\bpythonLambdaAsset\(/g) ?? [];

    // Anti-vacuity floor: if a refactor drops the call count to near-zero this test
    // would otherwise keep passing while asserting nothing.
    expect(routed.length).toBeGreaterThanOrEqual(21);
  });

  test('the exclude list covers the artifacts this defect was about', () => {
    // Presence-only by design -- B is the property test. This exists so a reader can see
    // which artifacts are claimed to be covered without synthesizing.
    expect(PYTHON_ASSET_EXCLUDES).toEqual(
      expect.arrayContaining(['tests/**', '**' + '/__pycache__', '**' + '/*.pyc']),
    );
  });
});
