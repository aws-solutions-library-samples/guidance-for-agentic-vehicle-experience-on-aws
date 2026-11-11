// Review Cycle 4 C4W5 (spec 2026-09-23-cvx-escalation-recommend-not-act, F5.5):
// a stale infrastructure/build/api-escalate must fail synth, not deploy.
// Mutations run: (1) drop the call from vsa-api-stack.ts -> the wiring test
// fails; (2) make compare() a no-op -> the stale/missing tests fail.
//
// issues/2026-11-07-live-state-stale-values/: the same guard for
// build/api-vehicle-live-state, which shipped a stale handler on 2026-11-06 22:44Z.
// Its bundle has a different shape (only handler.py from the Lambda dir, all of
// lambdas/shared, pip deps from requirements.txt), hence `sourceFiles`,
// `sharedFiles: 'all'` and `stampedInputs`. Mutations: mutate_cdk.py in that issue.
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { assertStagedCopyMatchesSource } from '../lib/staged-asset-check';

function fixture(): { root: string; src: string; shared: string; build: string } {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'staged-check-'));
  const src = path.join(root, 'src');
  const shared = path.join(root, 'shared');
  const build = path.join(root, 'build');
  fs.mkdirSync(path.join(src, 'tests'), { recursive: true });
  fs.mkdirSync(path.join(src, '__pycache__'), { recursive: true });
  fs.mkdirSync(shared, { recursive: true });
  fs.mkdirSync(path.join(build, 'shared'), { recursive: true });
  fs.writeFileSync(path.join(src, 'handler.py'), 'v = 2\n');
  fs.writeFileSync(path.join(src, 'conftest.py'), '# test-only\n');
  fs.writeFileSync(path.join(src, 'tests', 'test_x.py'), '# test-only\n');
  fs.writeFileSync(path.join(src, '__pycache__', 'handler.cpython-314.pyc'), 'x');
  fs.writeFileSync(path.join(shared, 'rsa_client.py'), 'c = 1\n');
  fs.writeFileSync(path.join(build, 'handler.py'), 'v = 2\n');
  fs.writeFileSync(path.join(build, 'shared', 'rsa_client.py'), 'c = 1\n');
  return { root, src, shared, build };
}

const run = (f: ReturnType<typeof fixture>) =>
  assertStagedCopyMatchesSource({
    buildDir: f.build, sourceDir: f.src, sharedDir: f.shared,
    sharedFiles: ['rsa_client.py'], restageCommand: 'bash scripts/build-rsa-lambda.sh',
  });

describe('assertStagedCopyMatchesSource', () => {
  test('a bundle equal to its source passes; tests/, __pycache__ and conftest.py are ignored', () => {
    expect(() => run(fixture())).not.toThrow();
  });

  test('a stale handler fails and names the restage command', () => {
    const f = fixture();
    fs.writeFileSync(path.join(f.src, 'handler.py'), 'v = 3\n');
    expect(() => run(f)).toThrow(/handler\.py: staged copy differs[\s\S]*build-rsa-lambda\.sh/);
  });

  test('a stale shared file fails', () => {
    const f = fixture();
    fs.writeFileSync(path.join(f.shared, 'rsa_client.py'), 'c = 2\n');
    expect(() => run(f)).toThrow(/shared\/rsa_client\.py: staged copy differs/);
  });

  test('a source module missing from the bundle fails', () => {
    const f = fixture();
    fs.writeFileSync(path.join(f.src, 'helpers.py'), 'h = 1\n');
    expect(() => run(f)).toThrow(/helpers\.py: missing from the staged bundle/);
  });
});

// The live-state bundle shape: handler.py only, all of shared/, a requirements stamp.
function liveStateFixture() {
  const f = fixture();
  fs.writeFileSync(path.join(f.src, 'requirements.txt'), 'redis==5.3.1\n');
  fs.mkdirSync(path.join(f.src, 'redis'), { recursive: true });   // gitignored vendored copy
  fs.writeFileSync(path.join(f.src, 'redis', '__init__.py'), '# vendored, not staged from here\n');
  fs.writeFileSync(path.join(f.shared, 'tenant_config.py'), 't = 1\n');
  fs.writeFileSync(path.join(f.shared, 'dtc_severity.yaml'), 'P0: x\n');
  fs.mkdirSync(path.join(f.shared, 'tests'), { recursive: true });
  fs.writeFileSync(path.join(f.shared, 'tests', 'test_s.py'), '# test-only\n');
  fs.writeFileSync(path.join(f.build, 'shared', 'tenant_config.py'), 't = 1\n');
  fs.writeFileSync(path.join(f.build, 'shared', 'dtc_severity.yaml'), 'P0: x\n');
  fs.mkdirSync(path.join(f.build, 'shared', '__pycache__'), { recursive: true });
  fs.writeFileSync(path.join(f.build, 'shared', '__pycache__', 'x.cpython-312.pyc'), 'x');
  const stamp = `${f.build}.requirements.txt`;
  fs.writeFileSync(stamp, 'redis==5.3.1\n');
  return { ...f, stamp };
}

const runLive = (f: ReturnType<typeof liveStateFixture>) =>
  assertStagedCopyMatchesSource({
    buildDir: f.build, sourceDir: f.src, sourceFiles: ['handler.py'],
    sharedDir: f.shared, sharedFiles: 'all',
    stampedInputs: [{ source: path.join(f.src, 'requirements.txt'), stamp: f.stamp }],
    restageCommand: 'bash scripts/build-api-vehicle-live-state.sh',
  });

describe('assertStagedCopyMatchesSource: the live-state bundle shape', () => {
  test('a fresh live-state bundle passes (requirements.txt and vendored dirs are not staged files)', () => {
    expect(() => runLive(liveStateFixture())).not.toThrow();
  });

  test('a stale handler fails and names the live-state build script', () => {
    const f = liveStateFixture();
    fs.writeFileSync(path.join(f.src, 'handler.py'), 'v = 3\n');
    expect(() => runLive(f)).toThrow(/handler\.py: staged copy differs[\s\S]*build-api-vehicle-live-state\.sh/);
  });

  test("sharedFiles 'all' checks every shared runtime file, not a list", () => {
    const f = liveStateFixture();
    fs.writeFileSync(path.join(f.shared, 'dtc_severity.yaml'), 'P0: y\n');
    expect(() => runLive(f)).toThrow(/shared\/dtc_severity\.yaml: staged copy differs/);
  });

  test("sharedFiles 'all' fails on a shared module added since the build", () => {
    const f = liveStateFixture();
    fs.writeFileSync(path.join(f.shared, 'new_reader.py'), 'n = 1\n');
    expect(() => runLive(f)).toThrow(/shared\/new_reader\.py: missing from the staged bundle/);
  });

  test("sharedFiles 'all' fails on a shared module the source has dropped", () => {
    const f = liveStateFixture();
    fs.writeFileSync(path.join(f.build, 'shared', 'removed.py'), 'gone = 1\n');
    expect(() => runLive(f)).toThrow(/shared\/removed\.py: staged but no longer in the source/);
  });

  test('a bundle with no requirements stamp fails', () => {
    const f = liveStateFixture();
    fs.rmSync(f.stamp);
    expect(() => runLive(f)).toThrow(/requirements\.txt \(build input\): the bundle has no record/);
  });

  test('a dotfile appearing on either side after the build does not refuse synth', () => {
    const f = liveStateFixture();
    fs.writeFileSync(path.join(f.shared, '.DS_Store'), 'finder');
    fs.writeFileSync(path.join(f.build, 'shared', '.hidden'), 'x');
    expect(() => runLive(f)).not.toThrow();
    // ...while a real module next to it is still checked.
    fs.writeFileSync(path.join(f.shared, 'added.py'), 'a = 1\n');
    expect(() => runLive(f)).toThrow(/shared\/added\.py: missing from the staged bundle/);
  });

  test('a bundle built from other requirements fails', () => {
    const f = liveStateFixture();
    fs.writeFileSync(path.join(f.src, 'requirements.txt'), 'redis==5.3.2\n');
    expect(() => runLive(f)).toThrow(/requirements\.txt \(build input\): the bundle was built from a different/);
  });
});

const STACK_SRC = () => fs.readFileSync(path.join(__dirname, '..', 'lib', 'vsa-api-stack.ts'), 'utf8');

/** The guard call nearest before `new lambda.Function(this, '<id>'`, and the text between. */
function guardBlockBefore(src: string, constructId: string): string {
  const fn = src.indexOf(`new lambda.Function(this, '${constructId}'`);
  expect(fn).toBeGreaterThan(-1);
  const call = src.lastIndexOf('assertStagedCopyMatchesSource({', fn);
  expect(call).toBeGreaterThan(-1);
  const block = src.slice(call, fn);
  // Nothing else is constructed between the guard and the Lambda it guards.
  expect(block).not.toContain('new lambda.Function(');
  return block;
}

describe('vsa-api-stack wiring', () => {
  test('the api-escalate bundle is checked against its source before the Lambda is built', () => {
    const block = guardBlockBefore(STACK_SRC(), 'ApiEscalate');
    expect(block).toContain("'build', 'api-escalate'");
    expect(block).toContain("'lambdas', 'api-escalate'");
    expect(block).toContain("'rsa_client.py', 'rsa_client_fake.py', 'rsa_client_real.py'");
  });

  test('the api-vehicle-live-state bundle is checked against its source before the Lambda is built', () => {
    const src = STACK_SRC();
    const block = guardBlockBefore(src, 'ApiVehicleLiveState');
    // The checked directory is the asset directory: one constant, used by both.
    const constDef = "const liveStateBuildDir = path.join(__dirname, '..', 'build', 'api-vehicle-live-state');";
    expect(src.split(constDef).length - 1).toBe(1);
    expect(src.indexOf(constDef)).toBeLessThan(src.indexOf('assertStagedCopyMatchesSource({\n      buildDir: liveStateBuildDir,'));
    expect(block).toContain('buildDir: liveStateBuildDir,');
    const fnStart = src.indexOf("new lambda.Function(this, 'ApiVehicleLiveState'");
    const fnBody = src.slice(fnStart, src.indexOf('\n    });', fnStart));
    expect(fnBody).toContain('code: pythonLambdaAsset(liveStateBuildDir),');
    expect(block).toContain("sourceDir: path.join(__dirname, '..', '..', 'lambdas', 'api-vehicle-live-state')");
    expect(block).toContain("sourceFiles: ['handler.py']");
    expect(block).toContain("sharedDir: path.join(__dirname, '..', '..', 'lambdas', 'shared')");
    expect(block).toContain("sharedFiles: 'all'");
    expect(block).toContain("'lambdas', 'api-vehicle-live-state', 'requirements.txt'");
    expect(block).toContain("'build', 'api-vehicle-live-state.requirements.txt'");
    expect(block).toContain("restageCommand: 'bash scripts/build-api-vehicle-live-state.sh'");
  });

  test("the live-state guard's sourceFiles list every module the Lambda dir ships", () => {
    // The build copies handler.py only. A second top-level module would need the build
    // script AND sourceFiles updated; fail here so neither is forgotten.
    const dir = path.join(__dirname, '..', '..', 'lambdas', 'api-vehicle-live-state');
    const modules = fs.readdirSync(dir, { withFileTypes: true })
      .filter((e) => e.isFile() && e.name.endsWith('.py') && e.name !== 'conftest.py')
      .map((e) => e.name);
    expect(modules).toEqual(['handler.py']);
  });
});

describe('scripts/build-api-vehicle-live-state.sh', () => {
  const script = () => fs.readFileSync(
    path.join(__dirname, '..', '..', 'scripts', 'build-api-vehicle-live-state.sh'), 'utf8');

  test('writes the requirements stamp beside the bundle, not inside it', () => {
    expect(script()).toContain('STAMP="$OUT.requirements.txt"');
  });

  test('removes the old stamp first and writes the new one only after the ABI check passes', () => {
    const lines = script().split('\n').map((l) => l.trim());
    const rmStamp = lines.indexOf('rm -f "$STAMP"');
    const rmOut = lines.indexOf('rm -rf "$OUT"');
    const abi = lines.findIndex((l) => l.startsWith('python3 "$ROOT/scripts/lib/check-bundle-abi.py" "$OUT"'));
    const write = lines.indexOf('cp "$ROOT/lambdas/api-vehicle-live-state/requirements.txt" "$STAMP"');
    expect(rmStamp).toBeGreaterThan(-1);
    expect(rmOut).toBeGreaterThan(rmStamp);
    expect(abi).toBeGreaterThan(rmOut);
    expect(write).toBeGreaterThan(abi);
    // Exactly one write of the stamp, so no earlier copy survives a failed build.
    expect(lines.filter((l) => l.includes('"$STAMP"') && l.startsWith('cp ')).length).toBe(1);
  });
});
