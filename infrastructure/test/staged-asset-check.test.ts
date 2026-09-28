// Review Cycle 4 C4W5 (spec 2026-09-23-cvx-escalation-recommend-not-act, F5.5):
// a stale infrastructure/build/api-escalate must fail synth, not deploy.
// Mutations run: (1) drop the call from vsa-api-stack.ts -> the wiring test
// fails; (2) make compare() a no-op -> the stale/missing tests fail.
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

describe('vsa-api-stack wiring', () => {
  test('the api-escalate bundle is checked against its source before the Lambda is built', () => {
    const src = fs.readFileSync(path.join(__dirname, '..', 'lib', 'vsa-api-stack.ts'), 'utf8');
    const call = src.indexOf('assertStagedCopyMatchesSource({');
    const fn = src.indexOf("new lambda.Function(this, 'ApiEscalate'");
    expect(call).toBeGreaterThan(-1);
    expect(fn).toBeGreaterThan(call);
    const block = src.slice(call, fn);
    expect(block).toContain("'build', 'api-escalate'");
    expect(block).toContain("'lambdas', 'api-escalate'");
    expect(block).toContain("'rsa_client.py', 'rsa_client_fake.py', 'rsa_client_real.py'");
  });
});
