/**
 * Security review Cycle 1 W5 (spec 2026-09-23-cvx-escalation-recommend-not-act):
 * the vsa-{stage}-rsa-dispatch table holds driver phone, name, VIN and
 * location, and must be encrypted with a customer-managed key.
 *
 * Fix Group 13 item 2 (issues/2026-09-25-rsa-dispatch-table-on-aws-managed-key/)
 * changed how that holds. W5 used to pin that bin/vsa.ts passed `cmsKmsKeyArn`
 * to VsaRsaStack. That parameter names the AWS-managed alias/aws/dynamodb key
 * on staging, so the pin guaranteed a key ARN, not a customer-managed key.
 * VsaRsaStack now creates the table's own key unconditionally, which
 * `vsa-rsa-stack.test.ts` (l) pins; there is no app-level input to forget.
 *
 * What the app still wires, and what this file pins: the API stack's escalate
 * writer and status reader are granted on THAT key. If bin/vsa.ts passed any
 * other key (for example `kms.Key.fromKeyArn(..., cmsKmsKeyArn)`), both Lambdas
 * would lose table access at their first KMS call, and every stack test would
 * stay green because they use fixture keys.
 *
 * Source-level on purpose: synthesising bin/vsa.ts needs SSM and per-stage
 * environment, which unit tests do not have.
 *
 * Mutations, each caught:
 *   - `rsaDispatchKey: kms.Key.fromKeyArn(app, 'X', <any arn>)` in the API stack call
 *   - `cmsKmsKeyArn,` added back to the `new VsaRsaStack(...)` props (tsc also rejects it)
 */
import * as fs from 'fs';
import * as path from 'path';

const BIN = fs.readFileSync(path.join(__dirname, '..', 'bin', 'vsa.ts'), 'utf-8');

/** The argument text of the single `new <ctor>(` call, paren-matched. */
function callArgs(src: string, ctor: string): string {
  const needle = `new ${ctor}(`;
  const start = src.indexOf(needle);
  expect(start).toBeGreaterThanOrEqual(0);
  expect(src.indexOf(needle, start + 1)).toBe(-1); // exactly one
  let depth = 0;
  for (let i = src.indexOf('(', start); i < src.length; i++) {
    if (src[i] === '(') depth++;
    if (src[i] === ')' && --depth === 0) return src.slice(start, i + 1);
  }
  throw new Error(`unbalanced ${needle} call`);
}

/** Strip // and /* *\/ comments so a commented-out prop does not count. */
function code(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');
}

describe('W5 (Fix Group 13) — the RSA table key is the stack\'s own, and the API stack is granted on it', () => {
  test('new VsaRsaStack(...) takes no external key input', () => {
    const args = code(callArgs(BIN, 'VsaRsaStack'));
    expect(args).not.toMatch(/\bcmsKmsKeyArn\b/);
    expect(args).not.toMatch(/KmsKey|kmsKey|encryptionKey/);
  });

  test('new VsaApiStack(...) receives rsaDispatchKey: rsaStack.rsaDispatchKey, and nothing else for that prop', () => {
    const args = code(callArgs(BIN, 'VsaApiStack'));
    const assignments = args.match(/\brsaDispatchKey\b\s*(:[^,}\n]*)?/g) ?? [];
    expect(assignments).toHaveLength(1);
    expect((assignments[0] ?? '').replace(/\s+/g, '')).toBe('rsaDispatchKey:rsaStack.rsaDispatchKey');
  });

  test('the table handed to the API stack is the same RSA stack\'s table', () => {
    const args = code(callArgs(BIN, 'VsaApiStack'));
    const assignments = args.match(/\brsaDispatchTable\b\s*(:[^,}\n]*)?/g) ?? [];
    expect(assignments).toHaveLength(1);
    expect((assignments[0] ?? '').replace(/\s+/g, '')).toBe('rsaDispatchTable:rsaStack.rsaDispatchTable');
  });
});
