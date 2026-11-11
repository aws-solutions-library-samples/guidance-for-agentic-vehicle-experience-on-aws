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
 * The same happened to `build/api-vehicle-live-state` on 2026-11-06 22:44Z
 * (issues/2026-11-07-live-state-stale-values/).
 *
 * Synth is the one boundary every deploy crosses, including a narrow
 * `cdk deploy <stack>` that skips the Makefile, so the check lives here and not
 * in a Makefile pre-flight.
 *
 * Compares every runtime file of the source directory (excluding `tests/`,
 * `__pycache__/`, `*.pyc` and `conftest.py`, as the build scripts do), or only
 * `sourceFiles` when given, and each named shared file. `sharedFiles: 'all'`
 * compares every runtime file of `sharedDir`, and also fails on a runtime file
 * staged under `shared/` that the source no longer has (the build copies the
 * whole directory, so a deleted module would otherwise keep shipping). Each
 * `stampedInputs` pair compares a build input (e.g. `requirements.txt`) with the
 * copy the build script wrote beside the bundle when it ran. Content-equality
 * only: mtimes are ignored.
 */
export function assertStagedCopyMatchesSource(opts: {
  buildDir: string;
  sourceDir: string;
  sourceFiles?: string[];
  sharedDir?: string;
  sharedFiles?: string[] | 'all';
  stampedInputs?: Array<{ source: string; stamp: string }>;
  restageCommand: string;
}): void {
  const { buildDir, sourceDir, sourceFiles, sharedDir, sharedFiles = [], stampedInputs = [],
    restageCommand } = opts;
  const problems: string[] = [];

  const compare = (src: string, staged: string, label: string) => {
    if (!fs.existsSync(staged)) {
      problems.push(`${label}: missing from the staged bundle`);
    } else if (!fs.readFileSync(src).equals(fs.readFileSync(staged))) {
      problems.push(`${label}: staged copy differs from source`);
    }
  };

  for (const rel of sourceFiles ?? runtimeFiles(sourceDir)) {
    compare(path.join(sourceDir, rel), path.join(buildDir, rel), rel);
  }
  if (sharedDir) {
    const stagedShared = path.join(buildDir, 'shared');
    const names = sharedFiles === 'all' ? runtimeFiles(sharedDir) : sharedFiles;
    for (const f of names) {
      compare(path.join(sharedDir, f), path.join(stagedShared, f), `shared/${f}`);
    }
    if (sharedFiles === 'all' && fs.existsSync(stagedShared)) {
      const inSource = new Set(names);
      for (const f of runtimeFiles(stagedShared)) {
        if (!inSource.has(f)) problems.push(`shared/${f}: staged but no longer in the source`);
      }
    }
  }
  for (const { source, stamp } of stampedInputs) {
    const label = `${path.basename(source)} (build input)`;
    if (!fs.existsSync(stamp)) {
      problems.push(`${label}: the bundle has no record of the ${path.basename(source)} it was built from`);
    } else if (!fs.readFileSync(source).equals(fs.readFileSync(stamp))) {
      problems.push(`${label}: the bundle was built from a different ${path.basename(source)}`);
    }
  }

  if (problems.length > 0) {
    throw new Error(
      `Stale staged Lambda bundle at ${buildDir}:\n  - ${problems.join('\n  - ')}\n`
      + `Deploying now would ship code older than the source. Run: ${restageCommand}`,
    );
  }
}

/** Runtime files under `dir`, relative to it: no `tests/`, `__pycache__/`, `*.pyc`, `conftest.py`,
 * and no dotfiles (a Finder `.DS_Store` appearing after a build is not code and must not refuse synth). */
function runtimeFiles(dir: string, rel = ''): string[] {
  const out: string[] = [];
  for (const entry of fs.readdirSync(path.join(dir, rel), { withFileTypes: true })) {
    if (entry.name.startsWith('.')) continue;
    const r = path.join(rel, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === 'tests' || entry.name === '__pycache__') continue;
      out.push(...runtimeFiles(dir, r));
    } else if (entry.name !== 'conftest.py' && !entry.name.endsWith('.pyc')) {
      out.push(r);
    }
  }
  return out;
}
