/**
 * FiCost Guardrail topic shape.
 *
 * History:
 * - 2026-10-10: the 04:00Z health-agent run tripped the guardrail on 74 of ~106
 *   model attempts (issues/2026-10-10-cost-guardrail-blocks-replies/report.md).
 *   The topic, then `advice-outside-vehicle-cost`, ended "or any statement
 *   outside the operating cost of a fleet vehicle" and had a "Move the fleet
 *   budget into index funds" example. The 2026-10-12 fix dropped both and added
 *   an operational carve-out sentence.
 * - 2026-10-17, measured on temporary guardrails (same issue folder,
 *   2026-10-17-examples-attempt.md): the carve-out and the topic NAME were what
 *   kept blocking good replies. Bedrock's guidance says not to define exceptions
 *   in a topic and not to describe the topic in its name; the old name held
 *   "advice" and "vehicle cost", which every cost recommendation is about.
 *   Renamed `personal-advice`, definition without the exception, five block-side
 *   examples, STANDARD topic tier: 0 good replies blocked, every negative control
 *   blocked, 538 real recall/warranty replies unchanged.
 *
 * So the 2026-10-12 test "definition carves out the operational recommendations"
 * is REPLACED by its opposite below (no exception sentence, no operational words).
 * That is a deliberate contract change, approved by the user 2026-10-18 (PO
 * prompt 28, option 1), not a weakened test.
 *
 * The sibling file (vsa-tier2-stack-fi-cost.test.ts) pins the topic names and
 * types, the STANDARD tier, the cross-Region profile and the ApplyGuardrail
 * grant; this file pins the topic content. Synth tests cannot reach
 * ApplyGuardrail: a topic change is proven on a temporary guardrail with
 * issues/2026-10-10-cost-guardrail-blocks-replies/live_temp_guardrail_check.py.
 */

import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { VsaTier2Stack } from '../lib/vsa-tier2-stack';
import { TEST_CMS_FI_INTERNAL_API_ENDPOINT } from './helpers/cms-fi-internal-endpoint';
import { TEST_DMS_FI_INTERNAL_API_ENDPOINT } from './helpers/dms-fi-internal-endpoint';

const ACCOUNT = '123456789012';
const REGION = 'us-west-2';
const ADP_REGION = 'us-east-1';

let cached: Template | undefined;

function tier2(): Template {
  if (cached) return cached;
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
    cached = Template.fromStack(stack);
    return cached;
  } finally {
    if (prev === undefined) delete process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT;
    else process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT = prev;
  }
}

function fiCostGuardrailProps(): any {
  const resources = tier2().findResources('AWS::Bedrock::Guardrail', {
    Properties: { Name: 'vsa-staging-fi-cost-guardrail' },
  });
  const ids = Object.keys(resources);
  expect(ids).toHaveLength(1);
  return (Object.values(resources)[0] as any).Properties;
}

function topic(name: string): any {
  const t = fiCostGuardrailProps().TopicPolicyConfig.TopicsConfig
    .find((x: any) => x.Name === name);
  expect(t).toBeDefined();
  return t;
}

// Words of the operational recommendations the cost playbook produces
// (agents/fi_cost/playbook.md actions: shift_to_depot_off_peak, schedule_inspection,
// reassign_or_remarket, driver_coaching). The 2026-10-12 carve-out named them, and
// they kept tripping the topic; none may appear in its definition or examples.
const OPERATIONAL_WORDS = [
  /\bshift/, /\breassign/, /\bremarket/, /\binspect/, /\bcoach/, /\bcharg(e|ing)\b/,
  /\bdepot\b/, /\boff-peak\b/, /\boperational\b/,
];

describe('the shipped topics are exactly what run D measured', () => {
  // Run D (2026-10-17 21:00-21:08Z, temporary guardrail 4w4mkkog4s16) is the only
  // measurement behind this config: issues/2026-10-10-cost-guardrail-blocks-replies/
  // 2026-10-17-run-D-results.json, field `temp_topics` (sorted there by name), created
  // in this order by create_params from the stack's props. The strings are inlined
  // because issues/ is publish-excluded. Any change to a topic, an example, or their
  // ORDER (Bedrock: "The order in which the topics are configured can result in varying
  // evaluation outcomes") needs a new temporary-guardrail run before this pin moves.
  test('ordered topics, definitions, examples and types equal run D', () => {
    expect(fiCostGuardrailProps().TopicPolicyConfig.TopicsConfig).toEqual([
      {
        Name: 'personal-advice',
        Definition: 'Personal legal, medical, tax, investment, or employment advice to the reader.',
        Examples: [
          'This expense may be deductible on your personal 2026 tax return.',
          'You should see a lawyer before signing the fleet lease amendment.',
          'Buy an index fund to hedge the energy budget.',
          'Ask your physician about medication for your recurring migraines.',
          'You should ask your manager for a raise or start looking for a new job.',
        ],
        Type: 'DENY',
      },
      {
        Name: 'blame-of-a-person',
        Definition:
          'Attributing cost to a named or identifiable person, or judging the character or ' +
          'conduct of a driver or staff member.',
        Examples: [
          'The driver is careless and wasteful.',
          'Fire the person responsible for charging.',
          'You should dismiss this driver.',
        ],
        Type: 'DENY',
      },
    ]);
  });
});

describe('personal-advice topic — the name does not describe the topic', () => {
  test('the topic is named personal-advice', () => {
    expect(topic('personal-advice').Type).toBe('DENY');
  });

  test('no topic name carries the subject words that matched every cost reply', () => {
    // Mutant guard: the old name `advice-outside-vehicle-cost` (or any name built
    // from "cost"/"vehicle"/"fleet") must not come back.
    const names = (fiCostGuardrailProps().TopicPolicyConfig.TopicsConfig as any[]).map((t) => t.Name);
    expect(names).toHaveLength(2);
    for (const n of names) expect(n).not.toMatch(/cost|vehicle|fleet|outside/i);
  });
});

describe('personal-advice topic — definition says only what the topic is', () => {
  test('names the five personal-advice categories and the reader', () => {
    const d = (topic('personal-advice').Definition as string).toLowerCase();
    for (const re of [/\blegal\b/, /\bmedical\b/, /\btax\b/, /\binvestment\b/, /\bemployment\b/]) {
      expect(d).toMatch(re);
    }
    // The boundary is the human reader, not every statement about fleet money.
    expect(d).toMatch(/\bpersonal\b/);
    expect(d).toMatch(/\breader\b/);
  });

  test('no catch-all clause (the 2026-10-10 over-block)', () => {
    const d = (topic('personal-advice').Definition as string).toLowerCase();
    expect(d).not.toMatch(/outside the operating cost/);
    expect(d).not.toMatch(/any statement/);
  });

  test('no exception or carve-out sentence, and no operational words (the 2026-10-12 over-block)', () => {
    const d = (topic('personal-advice').Definition as string).toLowerCase();
    expect(d).not.toMatch(/not this topic|\bexcept|\bnot (covered|included)\b|\bare not\b|\bis not\b/);
    for (const re of OPERATIONAL_WORDS) expect(d).not.toMatch(re);
    // One sentence: a second sentence is where the carve-out lived.
    expect(d.trim().split(/(?<=\.)\s+/)).toHaveLength(1);
  });

  test('definition respects Bedrock topic-definition length cap (1..200 chars)', () => {
    const d = topic('personal-advice').Definition as string;
    expect(d.length).toBeGreaterThanOrEqual(1);
    expect(d.length).toBeLessThanOrEqual(200);
  });
});

describe('personal-advice topic — five block-side examples, one per category', () => {
  const examples = (): string[] => (topic('personal-advice').Examples as string[]) ?? [];

  test('exactly five examples (Bedrock\'s limit), each 1..100 chars', () => {
    expect(examples()).toHaveLength(5);
    for (const e of examples()) {
      expect(e.length).toBeGreaterThanOrEqual(1);
      expect(e.length).toBeLessThanOrEqual(100);
    }
  });

  test('each category the definition names has its own example', () => {
    // Bedrock examples are samples of content the topic BLOCKS (there is no
    // pass-side example), so each category is taught by one personal-advice line.
    const cues: Record<string, RegExp> = {
      tax: /\btax\b|deductible/i,
      legal: /\blawyer\b|\battorney\b|\bsue\b/i,
      investment: /\binvest|\bindex fund\b|\bstocks?\b/i,
      medical: /\bphysician\b|\bdoctor\b|\bmedication\b/i,
      employment: /\bmanager\b|\braise\b|\bnew job\b/i,
    };
    for (const [cat, re] of Object.entries(cues)) {
      const hits = examples().filter((e) => re.test(e));
      expect({ cat, hits: hits.length }).toEqual({ cat, hits: 1 });
    }
  });

  test('every example is advice to the reader, none in fleet-operations language', () => {
    for (const e of examples()) {
      expect(e).toMatch(/\byou(r)?\b|^(buy|ask|sell|see|talk)\b/i);
      for (const re of OPERATIONAL_WORDS) expect(e.toLowerCase()).not.toMatch(re);
    }
    // The two phrases that trained the 2026-10-10 over-block stay out.
    expect(examples().every((e) => !/fleet budget/i.test(e))).toBe(true);
    expect(examples().every((e) => !/dismiss this driver/i.test(e))).toBe(true);
  });
});

describe('blame-of-a-person topic — still catches personnel judgment', () => {
  test('definition still names the pattern (named/identifiable person, driver/staff conduct)', () => {
    const d = (topic('blame-of-a-person').Definition as string).toLowerCase();
    expect(d).toMatch(/person/);
    expect(d).toMatch(/\bdriver\b|\bstaff\b/);
    expect(d).toMatch(/\bconduct\b|\bcharacter\b/);
  });

  test('examples still include the dismissal case the fi_cost topic no longer carries', () => {
    const examples = ((topic('blame-of-a-person').Examples as string[]) ?? [])
      .join(' | ').toLowerCase();
    // The matcher learned dismissal through the old fi_cost topic; the fix
    // moved that teacher here so the pattern remains blocked.
    expect(/\bdismiss|\bfire\b/.test(examples)).toBe(true);
    expect(/\bdriver\b|\bperson\b/.test(examples)).toBe(true);
  });
});

describe('neither topic widened to permit real violations', () => {
  test('exactly two DENY topics on the guardrail; no WARN tier anywhere', () => {
    const topics = fiCostGuardrailProps().TopicPolicyConfig.TopicsConfig as any[];
    expect(topics).toHaveLength(2);
    for (const t of topics) {
      expect(t.Type).toBe('DENY');
    }
  });

  test('both topics block on output: no detect-only action or disabled side', () => {
    // TopicConfig's InputAction/OutputAction (BLOCK | NONE) and InputEnabled /
    // OutputEnabled default to blocking; NONE or false would turn a topic into
    // detect-only and let its violations through with every other test green.
    for (const t of fiCostGuardrailProps().TopicPolicyConfig.TopicsConfig as any[]) {
      for (const key of ['InputAction', 'OutputAction', 'InputEnabled', 'OutputEnabled']) {
        expect({ topic: t.Name, key, value: t[key] }).toEqual({ topic: t.Name, key, value: undefined });
      }
    }
  });

  test('the six PII blocks are unchanged', () => {
    const pii = (fiCostGuardrailProps().SensitiveInformationPolicyConfig.PiiEntitiesConfig as any[])
      .map((e) => `${e.Type}:${e.Action}`).sort();
    // Belt-and-braces with the sibling test; a mutant that drops a BLOCK here
    // must fail two independent assertions, not one.
    expect(pii).toEqual([
      'ADDRESS:BLOCK',
      'CREDIT_DEBIT_CARD_NUMBER:BLOCK',
      'EMAIL:BLOCK',
      'NAME:BLOCK',
      'PHONE:BLOCK',
      'US_SOCIAL_SECURITY_NUMBER:BLOCK',
    ]);
  });

  test('content filters are five, all MEDIUM for both input and output', () => {
    const filters = (fiCostGuardrailProps().ContentPolicyConfig.FiltersConfig as any[])
      .map((f) => `${f.Type}:${f.InputStrength}:${f.OutputStrength}`).sort();
    expect(filters).toEqual([
      'HATE:MEDIUM:MEDIUM',
      'INSULTS:MEDIUM:MEDIUM',
      'MISCONDUCT:MEDIUM:MEDIUM',
      'SEXUAL:MEDIUM:MEDIUM',
      'VIOLENCE:MEDIUM:MEDIUM',
    ]);
  });
});
