import * as fs from 'fs';
import * as path from 'path';

/**
 * Fail synth when a pre-staged Lambda bundle no longer matches its source.
 *
 * Some Lambdas are deployed from `infrastructure/build/<name>/`, a gitignored
 * directory that `scripts/build-rsa-lambda.sh` assembles from `lambdas/<name>/`
 * plus `lambdas/shared/`. CDK hashes whatever is in that directory. So a
 * `cdk deploy` run without re-staging ships the OLD code while reporting
 * UPDATE_COMPLETE. That is how review Cycle 4 C4W5 found `build/api-escalate`
 * two commits behind: it had no vehicle binding, which the reviewed source did.
 *
 * Synth is the one boundary every deploy crosses, including a narrow
 * `cdk deploy <stack>` that skips the Makefile, so the check lives here and not
 * in a Makefile pre-flight.
 *
 * Compares every runtime file of the source directory (excluding `tests/`,
 * `__pycache__/` and `conftest.py`, as the build script does) and each named
 * shared file. Content-equality only: mtimes are ignored.
 */
export function assertStagedCopyMatchesSource(opts: {
  buildDir: string;
  sourceDir: string;
  sharedDir?: string;
  sharedFiles?: string[];
  restageCommand: string;
}): void {
  const { buildDir, sourceDir, sharedDir, sharedFiles = [], restageCommand } = opts;
  const problems: string[] = [];

  const runtimeFiles = (dir: string, rel = ''): string[] => {
    const out: string[] = [];
    for (const entry of fs.readdirSync(path.join(dir, rel), { withFileTypes: true })) {
      const r = path.join(rel, entry.name);
      if (entry.isDirectory()) {
        if (entry.name === 'tests' || entry.name === '__pycache__') continue;
        out.push(...runtimeFiles(dir, r));
      } else if (entry.name !== 'conftest.py' && !entry.name.endsWith('.pyc')) {
        out.push(r);
      }
    }
    return out;
  };

  const compare = (src: string, staged: string, label: string) => {
    if (!fs.existsSync(staged)) {
      problems.push(`${label}: missing from the staged bundle`);
    } else if (!fs.readFileSync(src).equals(fs.readFileSync(staged))) {
      problems.push(`${label}: staged copy differs from source`);
    }
  };

  for (const rel of runtimeFiles(sourceDir)) {
    compare(path.join(sourceDir, rel), path.join(buildDir, rel), rel);
  }
  if (sharedDir) {
    for (const f of sharedFiles) {
      compare(path.join(sharedDir, f), path.join(buildDir, 'shared', f), `shared/${f}`);
    }
  }

  if (problems.length > 0) {
    throw new Error(
      `Stale staged Lambda bundle at ${buildDir}:\n  - ${problems.join('\n  - ')}\n`
      + `Deploying now would ship code older than the source. Run: ${restageCommand}`,
    );
  }
}
