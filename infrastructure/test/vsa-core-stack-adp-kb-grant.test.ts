/**
 * The AgentCore runtime role's ADP knowledge-base grant is scoped to ONE knowledge base.
 *
 * Issue: issues/2026-10-30-bidi-kb-access/. The bidi voice runtime got AccessDenied on
 * `bedrock:Retrieve` because a narrow `cdk deploy vsa-staging-core` without `-c adpKbId`
 * removed the statement (second time; first was 2026-09-23). The fix makes `adpKbId` a
 * Tier B prop resolved in `bin/vsa.ts`; this file pins what the stack emits for it:
 *
 *   - with a KB id: exactly one statement on the role can grant `bedrock:Retrieve`, and
 *     its only resource is that KB's ARN (no `knowledge-base/*`, no `*`, no condition);
 *   - no statement on the role, of any Sid, matches another knowledge base's ARN for any
 *     action that covers `bedrock:Retrieve` (wildcard actions included);
 *   - without a KB id: no statement can grant `bedrock:Retrieve`;
 *   - anything but a bare KB id (a wildcard, an ARN, lower case, a CDK token) throws.
 *
 * The statement walk covers `AWS::IAM::Policy`, `AWS::IAM::ManagedPolicy` and the role's
 * own inline policies, because the role is near its inline budget and CDK may move
 * statements into an overflow managed policy (see vsa-core-stack-agentcore-restricted).
 */

import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { ADP_KB_ID_PATTERN, VsaCoreStack } from '../lib/vsa-core-stack';

const ACCOUNT = '123456789012';
const REGION = 'us-west-2';
const STAGE = 'staging';
const KB_ID = 'EXAMPLEKB01';
const KB_ARN = `arn:aws:bedrock:us-east-1:${ACCOUNT}:knowledge-base/${KB_ID}`;
const ROLE_NAME = `AmazonBedrockAgentCoreSDKRuntime-${REGION}-${STAGE}`;

// ARNs the grant must NOT reach: another KB, the same id in another region or account,
// and a KB-family resource that is not a knowledge base.
const PROBE_ARNS = [
  `arn:aws:bedrock:us-east-1:${ACCOUNT}:knowledge-base/OTHERKB0001`,
  `arn:aws:bedrock:us-west-2:${ACCOUNT}:knowledge-base/${KB_ID}`,
  `arn:aws:bedrock:us-east-1:210987654321:knowledge-base/${KB_ID}`,
  `arn:aws:bedrock:us-east-1:${ACCOUNT}:knowledge-base/${KB_ID}0`,
];

function synth(adpKbId: string | undefined): Template {
  const app = new cdk.App();
  const stack = new VsaCoreStack(app, `vsa-${STAGE}-core`, {
    env: { account: ACCOUNT, region: REGION },
    stage: STAGE,
    cmsStage: STAGE,
    cmsKmsKeyArn: {
      literal: `arn:aws:kms:${REGION}:${ACCOUNT}:key/test-key-id`,
      parameterName: `/cvx/${STAGE}/cms-kms-key-arn`,
    },
    adpKbId,
  });
  return Template.fromStack(stack);
}

/** Every statement effective on the runtime role, from all three policy shapes. */
function roleStatements(t: Template): any[] {
  const roles = t.findResources('AWS::IAM::Role', { Properties: { RoleName: ROLE_NAME } });
  const ids = Object.keys(roles);
  expect(ids).toHaveLength(1);
  const roleId = ids[0];
  const out: any[] = [];
  for (const type of ['AWS::IAM::Policy', 'AWS::IAM::ManagedPolicy']) {
    for (const r of Object.values(t.findResources(type))) {
      const props = (r as any).Properties;
      if (JSON.stringify(props.Roles ?? []).includes(roleId)) {
        out.push(...(props.PolicyDocument.Statement as any[]));
      }
    }
  }
  for (const p of ((roles[roleId] as any).Properties?.Policies ?? []) as any[]) {
    out.push(...(p.PolicyDocument.Statement as any[]));
  }
  return out;
}

const asList = (v: unknown): any[] => (v === undefined ? [] : Array.isArray(v) ? v : [v]);

/** IAM-style wildcard match (`*` any run, `?` one char), case-sensitive like ARNs. */
function iamMatch(pattern: string, value: string): boolean {
  const re = pattern.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*').replace(/\?/g, '.');
  return new RegExp(`^${re}$`).test(value);
}

/** A resource as a match pattern: a literal string, or any intrinsic part as `*`. */
function resourcePattern(r: any): string {
  if (typeof r === 'string') return r;
  if (r && Array.isArray(r['Fn::Join'])) {
    const [sep, parts] = r['Fn::Join'];
    return (parts as any[]).map((p) => (typeof p === 'string' ? p : '*')).join(sep);
  }
  return '*';
}

/** Allow statements whose actions cover `bedrock:Retrieve` (incl. `*`, `bedrock:*`, `bedrock:Ret*`). */
function retrieveStatements(t: Template): any[] {
  return roleStatements(t).filter(
    (s) =>
      s.Effect === 'Allow' &&
      asList(s.Action).some((a) => typeof a === 'string' && iamMatch(a.toLowerCase(), 'bedrock:retrieve')),
  );
}

describe('runtime role bedrock:Retrieve grant with a KB id', () => {
  const t = synth(KB_ID);

  test('exactly one statement can grant bedrock:Retrieve, scoped to that KB only', () => {
    const stmts = retrieveStatements(t);
    expect(stmts).toHaveLength(1);
    const s = stmts[0];
    expect(s.Sid).toBe('AdpBedrockKbRetrieveCrossAccount');
    expect([...asList(s.Action)].sort()).toEqual(['bedrock:Retrieve', 'bedrock:RetrieveAndGenerate']);
    expect(asList(s.Resource).map(resourcePattern)).toEqual([KB_ARN]);
    expect(s.Condition).toBeUndefined();
    expect(s.NotResource).toBeUndefined();
  });

  test('no statement covering bedrock:Retrieve reaches another knowledge base', () => {
    const stmts = retrieveStatements(t);
    expect(stmts.length).toBeGreaterThan(0);
    for (const s of stmts) {
      for (const pattern of asList(s.Resource).map(resourcePattern)) {
        for (const probe of PROBE_ARNS) {
          expect({ sid: s.Sid, pattern, probe, matches: iamMatch(pattern, probe) }).toEqual({
            sid: s.Sid, pattern, probe, matches: false,
          });
        }
        expect(iamMatch(pattern, KB_ARN)).toBe(true);
      }
    }
  });
});

describe('runtime role bedrock:Retrieve grant without a KB id', () => {
  test.each([[undefined], [''], ['   ']])('adpKbId=%p emits no Retrieve statement', (v) => {
    expect(retrieveStatements(synth(v))).toEqual([]);
  });
});

describe('a value that is not a bare KB id is refused', () => {
  test.each([
    ['*'],
    ['knowledge-base/*'],
    ['EXAMPLEKB*'],
    ['examplekb01'],
    [KB_ARN],
    ['SHORT'],
    ['EXAMPLEKB01EXAMPLE'],
    [`${KB_ID},OTHERKB0001`],
  ])('adpKbId=%p throws', (v) => {
    expect(() => synth(v)).toThrow(/not a Bedrock knowledge base id/);
  });

  test('a CDK token (e.g. an SSM lookup) throws instead of granting', () => {
    expect(() => synth(cdk.Lazy.string({ produce: () => KB_ID }))).toThrow(/not a Bedrock knowledge base id/);
  });

  test('the stack and the Makefile pre-flight use the same id rule', () => {
    expect(ADP_KB_ID_PATTERN.source).toBe('^[A-Z0-9]{8,16}$');
    const makefile = require('fs').readFileSync(require('path').join(__dirname, '..', '..', 'Makefile'), 'utf8');
    expect(makefile).toContain("grep -qE '^[A-Z0-9]{8,16}$$'");
  });
});

describe('delivery: a named managed policy, not the role inline aggregate', () => {
  // The live inline aggregate was 10,053 of IAM's 10,240 characters on 2026-10-30; the
  // statement on the DefaultPolicy would have failed the deploy (issues/2026-10-30-bidi-kb-access/).
  const t = synth(KB_ID);

  test('the Retrieve statement is in AdpKbRetrievePolicy, attached to the runtime role', () => {
    const mps = t.findResources('AWS::IAM::ManagedPolicy', {
      Properties: { ManagedPolicyName: `vsa-${STAGE}-adp-kb-retrieve-${REGION}` },
    });
    expect(Object.keys(mps)).toHaveLength(1);
    const props = (Object.values(mps)[0] as any).Properties;
    const sids = (props.PolicyDocument.Statement as any[]).map((s) => s.Sid);
    expect(sids).toEqual(['AdpBedrockKbRetrieveCrossAccount']);
    const roleId = Object.keys(t.findResources('AWS::IAM::Role', { Properties: { RoleName: ROLE_NAME } }))[0];
    expect(props.Roles).toEqual([{ Ref: roleId }]);
  });

  test('no inline policy on the role carries a bedrock:Retrieve statement', () => {
    const inline: any[] = [];
    for (const r of Object.values(t.findResources('AWS::IAM::Policy'))) {
      inline.push(...((r as any).Properties.PolicyDocument.Statement as any[]));
    }
    const covering = inline.filter((s) =>
      asList(s.Action).some((a) => typeof a === 'string' && iamMatch(a.toLowerCase(), 'bedrock:retrieve')));
    expect(covering).toEqual([]);
  });
});
