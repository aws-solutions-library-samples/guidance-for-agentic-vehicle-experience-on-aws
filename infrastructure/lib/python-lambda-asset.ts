import * as lambda from 'aws-cdk-lib/aws-lambda';

/**
 * Paths excluded from every Python Lambda asset in this app.
 *
 * Why this exists: `lambda.Code.fromAsset(dir)` with no `exclude` hashes the *entire*
 * directory, so a `__pycache__` written by a local `pytest` run changes the asset hash of
 * a Lambda whose tracked source did not change. The observable symptom is a `cdk diff`
 * that reports `Code.S3Key` changes for Lambdas nobody touched -- a one-line edit
 * produced a 9-Lambda diff -- which makes the diff useless as a statement of blast
 * radius. See `issues/2026-09-21-cdk-asset-hash-drift-unrelated-lambdas/`.
 *
 * Secondary effects this also fixes: `.pyc` compiled by the local interpreter (3.14) was
 * shipping into a Python 3.12 runtime package as dead weight, and the asset was not
 * reproducible from tracked source alone.
 *
 * The recursive-glob spelling of the `__pycache__` and `.pyc` entries below matches the
 * exclude list already in `vsa-tier2-stack.ts` rather than introducing a second idiom for
 * the same job. The globs are deliberately not quoted in this comment: a recursive-glob
 * prefix ends with the same two characters that close a block comment, so naming one here
 * truncates the comment and breaks the file. Read the array below for the literal values.
 *
 * NOTE: `exclude` is a no-op for assets that use the `bundling` property -- there the ZIP
 * content is whatever the bundling command emits. Every call site routed through this
 * helper is a plain directory asset with no bundling, so the list controls both the hash
 * and the shipped package.
 */
export const PYTHON_ASSET_EXCLUDES: string[] = [
  // Root-level `tests`. `tests/**` excludes the CONTENTS; the bare `tests` entry also
  // drops the now-empty directory entry. An empty directory does not move a CDK
  // fingerprint, so `tests/**` alone was already enough to keep root-level test *code*
  // out of the package — the empty dir was cosmetic, not a leak.
  'tests',
  'tests/**',
  // NESTED test directories, e.g. `shared/tests` in the api-vehicle-live-state build.
  // THIS is what the first version of the fix actually missed: `tests/**` is
  // root-anchored, so `shared/tests/test_cms_client_real.py` shipped inside a staged
  // asset while the exclude list looked like it covered `tests`. Found by inspecting
  // real synth output — the property tests in
  // `infrastructure/test/vsa-api-stack-asset-excludes.test.ts` (B, B2) both passed
  // while the leak was live, because neither injected a nested directory. B3 now does.
  //
  // Verified semantics, by mutation rather than assumption: `tests` and `**/tests` are
  // each INDIVIDUALLY SUFFICIENT for the nested case and therefore mutually redundant —
  // deleting either alone changes nothing, deleting BOTH breaks nested exclusion (B3
  // fails). A bare pattern with no slash matches at any depth, gitignore-style; a
  // pattern containing a slash is anchored. Both are kept because the redundancy is
  // free and the anchoring rule is easy to get wrong — it already was, once, here.
  '**/tests',
  '**/__pycache__',
  '**/*.pyc',
  '.pytest_cache/**',
];

/**
 * `lambda.Code.fromAsset` with {@link PYTHON_ASSET_EXCLUDES} applied.
 *
 * Use this for every Python Lambda asset instead of calling `lambda.Code.fromAsset`
 * directly, so the exclusion cannot be forgotten when the next Lambda is added. A
 * per-call-site exclude list is a list that drifts.
 *
 * Takes a full resolved path rather than a directory name: call sites in this app read
 * from two different bases (`../../lambdas/<name>` for source-tree assets and
 * `../build/<name>` for assets pre-staged by `scripts/build-avx-lambda.sh`), and encoding
 * that choice in the helper would hide it.
 */
export function pythonLambdaAsset(assetPath: string): lambda.AssetCode {
  return lambda.Code.fromAsset(assetPath, { exclude: PYTHON_ASSET_EXCLUDES });
}
