/**
 * IAM tests for the `CvxAgentCoreMemoryOps` policy statement in VsaCoreStack.
 *
 * Added by spec `2026-09-22-cvx-text-session-memory` Fix Group 4 task F4.1.
 *
 * ## Why this test exists
 *
 * `MemoryClient.get_last_k_turns` calls `list_events` and nothing else
 * (confirmed against bedrock-agentcore==1.9.1 source).  Without
 * `bedrock-agentcore:ListEvents` on the execution role, every hydration read
 * fails `AccessDeniedException`, which `load_recent_turns` swallows into `[]`
 * — so the assistant stays amnesiac and the bug this spec fixes silently
 * persists even after the Python code ships.
 *
 * The existing test suite for this repo has a documented history of IAM tests
 * that assert a statement is **narrow** while being blind to an action being
 * **missing** (see `vsa-core-stack-dms-grants.test.ts` header and
 * `~/.kiro/steering/testing.md` § "Mutation testing at the green boundary").
 * This test asserts **presence** of the specific action, selected by action
 * name, not by `Sid`.
 *
 * ## Mutation record (required by testing.md)
 *
 * M1 — `ListEvents` removed from the statement:
 *   Procedure: delete `'bedrock-agentcore:ListEvents',` from the
 *              `CvxAgentCoreMemoryOps` actions array in vsa-core-stack.ts.
 *   Result: `CvxAgentCoreMemoryOps grants bedrock-agentcore:ListEvents` FAILS —
 *           "Expected: [expect.arrayContaining(...)] / Received: [actions
 *           without ListEvents]".
 *   The `other 7 actions are still present` test still passes (correct:
 *   the removal of one action should not accidentally affect the others).
 *   Restored: yes.
 *
 * Exercised 2026-09-22.
 */

import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
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
    // Tier A ConfigValue — literal override keeps synth hermetic (no SSM lookup).
    cmsKmsKeyArn: {
      literal: `arn:aws:kms:${REGION}:${ACCOUNT}:key/test-key-id`,
      parameterName: `/cvx/${STAGE}/cms-kms-key-arn`,
    },
  });
  return Template.fromStack(stack);
}

function allStatements(template: Template): any[] {
  const policies = template.findResources('AWS::IAM::Policy');
  return Object.values(policies).flatMap(
    (p: any) => p.Properties?.PolicyDocument?.Statement ?? [],
  );
}

function asArray<T>(v: T | T[] | undefined): T[] {
  if (v === undefined) return [];
  return Array.isArray(v) ? v : [v];
}

/**
 * Find the statement carrying a given action, searching by action name.
 *
 * Deliberately NOT searching by Sid — the task spec calls out that an
 * assertion selected by Sid passes vacuously when the action is removed
 * (the statement still exists with its Sid; only the action is gone).
 * Searching by action name makes the absence fail directly.
 */
function findStatementWithAction(template: Template, action: string): any | undefined {
  return allStatements(template).find((s) => {
    const acts = asArray<string>(s.Action);
    return acts.includes(action);
  });
}

describe('CvxAgentCoreMemoryOps — ListEvents presence (M1 target)', () => {
  const template = makeTemplate();

  test('CvxAgentCoreMemoryOps grants bedrock-agentcore:ListEvents', () => {
    // Selected by ACTION NAME, not by Sid — see file header for why.
    const stmt = findStatementWithAction(template, 'bedrock-agentcore:ListEvents');
    expect(stmt).toBeDefined();
    // Belt-and-braces: confirm the Sid matches, so we know we found the right statement
    // and not some other grant that happens to carry the action in future.
    expect(stmt.Sid).toBe('CvxAgentCoreMemoryOps');
  });

  test('the other 7 actions are still present on the same statement', () => {
    // Asserts the addition did not accidentally displace an existing action.
    const expected = [
      'bedrock-agentcore:CreateEvent',
      'bedrock-agentcore:BatchCreateMemoryRecords',
      'bedrock-agentcore:BatchUpdateMemoryRecords',
      'bedrock-agentcore:RetrieveMemoryRecords',
      'bedrock-agentcore:ListMemoryRecords',
      'bedrock-agentcore:GetMemoryRecord',
      'bedrock-agentcore:GetMemory',
    ];
    const stmt = findStatementWithAction(template, 'bedrock-agentcore:ListEvents');
    expect(stmt).toBeDefined();
    const actions = asArray<string>(stmt.Action);
    for (const a of expected) {
      expect(actions).toContain(a);
    }
  });

  test('resource ARN is scoped to this stage memory (not a wildcard)', () => {
    // Confirms the ListEvents grant did not widen the resource scope — the task
    // constraint is explicit: do NOT broaden the resource ARN.
    const stmt = findStatementWithAction(template, 'bedrock-agentcore:ListEvents');
    expect(stmt).toBeDefined();
    const resources = asArray<string>(stmt.Resource);
    expect(resources).not.toContain('*');
    expect(resources.length).toBeGreaterThan(0);
    for (const r of resources) {
      if (typeof r !== 'string') continue;
      expect(r).toMatch(
        new RegExp(`arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT}:memory/vsa_${STAGE}_cvx_foundation_memory`),
      );
    }
  });
});
