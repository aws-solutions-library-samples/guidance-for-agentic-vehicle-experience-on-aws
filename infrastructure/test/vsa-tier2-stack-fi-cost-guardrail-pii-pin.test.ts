/**
 * Guardrail PII-block pin (T1.3 item 7).
 *
 * The FI cost guardrail is shared by every FI agent's model path. Each reads
 * ``FI_COST_GUARDRAIL_ID`` (pinned per reading function below):
 *   - fi_cost (stage B cost diagnosis): fi_cost/agent.py ``_guardrail_id``, runner ``_run_cost_from_env``
 *   - fi_recall_warranty (plan_booking, draft_claim): runner ``_run_recall_from_env``, ``_run_warranty_from_env``
 *   - fi_fleet_health (stage D service planning): runner ``_run_service_from_env``
 *   - fi_rebalancing (stage E rebalance detail; spec D8 step 7): runner ``_run_rebalance_from_env``
 *   - fi_quality investigations: runner ``_run_investigation_sqs``
 *
 * The four core people-identifier types must be ``BLOCK``, not ``ANONYMIZE``.
 * A drifted action (e.g. ADDRESS → ANONYMIZE, or a dropped PHONE entry) would
 * let PII through to the narration text and ultimately to the Finding stored in
 * DynamoDB — the semantic opposite of what the guardrail exists to enforce.
 *
 * Reviewer item G5-7: "no test pins the cost guardrail's ADDRESS/PHONE/EMAIL
 * blocks" (fragment-A.md, Stage E G5 suggestions). This file closes that gap.
 *
 * Mutation guide:
 *   M1 ADDRESS → ANONYMIZE: the `ADDRESS:BLOCK` assertion fails.
 *   M2 drop PHONE entry:    the `PHONE:BLOCK` assertion fails.
 *   M3 EMAIL → ANONYMIZE:  the `EMAIL:BLOCK` assertion fails.
 *   M4 NAME → ANONYMIZE:   the `NAME:BLOCK` assertion fails.
 */

import * as cdk from 'aws-cdk-lib';
import * as fs from 'fs';
import * as path from 'path';
import { Template } from 'aws-cdk-lib/assertions';
import { VsaTier2Stack } from '../lib/vsa-tier2-stack';
import { TEST_CMS_FI_INTERNAL_API_ENDPOINT } from './helpers/cms-fi-internal-endpoint';
import { TEST_DMS_FI_INTERNAL_API_ENDPOINT } from './helpers/dms-fi-internal-endpoint';

const ACCOUNT = '123456789012';
const REGION = 'us-west-2';
const ADP_REGION = 'us-east-1';

let _cached: Template | undefined;

function tier2(): Template {
  if (_cached) return _cached;
  const prev = process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT;
  process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT = 'ops@example.invalid';
  try {
    const app = new cdk.App();
    const stack = new VsaTier2Stack(app, 'vsa-staging-tier2', {
      cmsFiInternalApiEndpoint: TEST_CMS_FI_INTERNAL_API_ENDPOINT,
      dmsFiInternalApiEndpoint: TEST_DMS_FI_INTERNAL_API_ENDPOINT,
      env: { account: ACCOUNT, region: REGION },
      stage: 'staging',
      adpRegion: ADP_REGION,
      adpAccountId: ACCOUNT,
      cmsKmsKeyArn: {
        literal: `arn:aws:kms:${REGION}:${ACCOUNT}:key/test-key-id`,
        parameterName: '/cvx/staging/cms-kms-key-arn',
      },
    });
    _cached = Template.fromStack(stack);
    return _cached;
  } finally {
    if (prev === undefined) delete process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT;
    else process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT = prev;
  }
}

function fiCostGuardrailPiiConfig(): { Type: string; Action: string }[] {
  const resources = tier2().findResources('AWS::Bedrock::Guardrail', {
    Properties: { Name: 'vsa-staging-fi-cost-guardrail' },
  });
  const ids = Object.keys(resources);
  expect(ids).toHaveLength(1);
  const props = (Object.values(resources)[0] as any).Properties;
  return props.SensitiveInformationPolicyConfig.PiiEntitiesConfig;
}

describe('FI cost guardrail PII blocks (shared by fi_cost, fi_recall_warranty, fi_fleet_health, fi_rebalancing, fi_quality)', () => {
  /**
   * The guardrail is shared by every FI agent's model path; its PII config
   * is the single control protecting all of them. These tests pin the four
   * core people-identifier types that motivated the guardrail (see
   * issues/2026-10-10-cost-guardrail-blocks-replies).
   */

  test('ADDRESS is BLOCK (M1: ADDRESS → ANONYMIZE must fail this test)', () => {
    const pii = fiCostGuardrailPiiConfig();
    const entry = pii.find((e) => e.Type === 'ADDRESS');
    expect(entry).toBeDefined();
    expect(entry!.Action).toBe('BLOCK');
  });

  test('PHONE is BLOCK (M2: drop PHONE entry must fail this test)', () => {
    const pii = fiCostGuardrailPiiConfig();
    const entry = pii.find((e) => e.Type === 'PHONE');
    expect(entry).toBeDefined();
    expect(entry!.Action).toBe('BLOCK');
  });

  test('EMAIL is BLOCK (M3: EMAIL → ANONYMIZE must fail this test)', () => {
    const pii = fiCostGuardrailPiiConfig();
    const entry = pii.find((e) => e.Type === 'EMAIL');
    expect(entry).toBeDefined();
    expect(entry!.Action).toBe('BLOCK');
  });

  test('NAME is BLOCK (M4: NAME → ANONYMIZE must fail this test)', () => {
    const pii = fiCostGuardrailPiiConfig();
    const entry = pii.find((e) => e.Type === 'NAME');
    expect(entry).toBeDefined();
    expect(entry!.Action).toBe('BLOCK');
  });

  test('agents sharing the guardrail read FI_COST_GUARDRAIL_ID from their source', () => {
    /**
     * c3 S1: Pin the guardrail env-var read per reading function, not just a string
     * mention.  The previous ``toContain('FI_COST_GUARDRAIL_ID')`` matched docstrings and
     * comments, so a mutant renaming one function's read survived.
     *
     * Each assertion checks ``os.environ.get("FI_COST_GUARDRAIL_ID"`` inside the function
     * body that actually reads the variable:
     *   - fi_cost/agent.py  → _guardrail_id()
     *   - runner.py         → _run_cost_from_env()     (~:1578)
     *   - runner.py         → _run_recall_from_env()   (~:1882)
     *   - runner.py         → _run_warranty_from_env() (~:2121)
     *   - runner.py         → _run_service_from_env()  (~:2635, fleet health)
     *   - runner.py         → _run_rebalance_from_env()(~:2924, fi_rebalancing's reader)
     *   - runner.py         → _run_investigation_sqs()  (~:3150, fi_quality investigations)
     *
     * Mutant: rename FI_COST_GUARDRAIL_ID in any one of these functions → that
     * function's assertion fails.  ``fi_rebalancing/llm.py`` is NOT checked here; its
     * docstring mentions the variable but runner.py is the actual reader.
     */
    const repoRoot = path.resolve(__dirname, '..', '..');

    /** Extract the body of a Python function by its def-name from src. */
    function funcBody(src: string, fnName: string): string {
      const start = src.indexOf(`def ${fnName}(`);
      if (start === -1) return '';
      const end = src.indexOf('\ndef ', start + 1);
      return src.slice(start, end === -1 ? undefined : end);
    }

    // fi_cost/_guardrail_id: the sole reader in fi_cost/agent.py
    const fiCostSrc = fs.readFileSync(
      path.join(repoRoot, 'agents', 'fi_cost', 'agent.py'), 'utf8');
    const guardrailFnBody = funcBody(fiCostSrc, '_guardrail_id');
    expect(guardrailFnBody).not.toBe('');
    expect(guardrailFnBody).toContain('os.environ.get("FI_COST_GUARDRAIL_ID"');

    const runnerSrc = fs.readFileSync(
      path.join(repoRoot, 'agents', 'tier2', 'runner.py'), 'utf8');

    // _run_cost_from_env (~:1578)
    const costBody = funcBody(runnerSrc, '_run_cost_from_env');
    expect(costBody).not.toBe('');
    expect(costBody).toContain('os.environ.get("FI_COST_GUARDRAIL_ID"');

    // _run_recall_from_env (~:1882)
    const recallBody = funcBody(runnerSrc, '_run_recall_from_env');
    expect(recallBody).not.toBe('');
    expect(recallBody).toContain('os.environ.get("FI_COST_GUARDRAIL_ID"');

    // _run_warranty_from_env (~:2121)
    const warrantyBody = funcBody(runnerSrc, '_run_warranty_from_env');
    expect(warrantyBody).not.toBe('');
    expect(warrantyBody).toContain('os.environ.get("FI_COST_GUARDRAIL_ID"');

    // _run_service_from_env (~:2635, fleet health)
    const serviceBody = funcBody(runnerSrc, '_run_service_from_env');
    expect(serviceBody).not.toBe('');
    expect(serviceBody).toContain('os.environ.get("FI_COST_GUARDRAIL_ID"');

    // _run_rebalance_from_env (~:2924): fi_rebalancing's actual reader in runner.py
    const rebalBody = funcBody(runnerSrc, '_run_rebalance_from_env');
    expect(rebalBody).not.toBe('');
    expect(rebalBody).toContain('os.environ.get("FI_COST_GUARDRAIL_ID"');

    // _run_investigation_sqs (~:3150): fi_quality investigations (review c4 S2)
    const investigationBody = funcBody(runnerSrc, '_run_investigation_sqs');
    expect(investigationBody).not.toBe('');
    expect(investigationBody).toContain('os.environ.get("FI_COST_GUARDRAIL_ID"');
  });
});
