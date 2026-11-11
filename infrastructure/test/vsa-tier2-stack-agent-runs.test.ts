/**
 * T1.5 hardening: AvxAgentRuns table TTL + broker stream-read cleanup.
 *
 * RED until:
 *   - vsa-tier2-stack.ts adds `timeToLiveAttribute: 'ttl'` to AvxAgentRuns.
 *   - The `BrokerAvxFindingsStreamRead` explicit policy statement is removed.
 *   - No remaining broker IAM statement references `stream/*`.
 *
 * Spec: .kiro/specs/2026-10-26-fi-sweep-avx-batch/spec.md (T1.5)
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { VsaTier2Stack } from '../lib/vsa-tier2-stack';
import { TEST_CMS_FI_INTERNAL_API_ENDPOINT } from './helpers/cms-fi-internal-endpoint';
import { TEST_DMS_FI_INTERNAL_API_ENDPOINT } from './helpers/dms-fi-internal-endpoint';

const ACCOUNT = '123456789012';
const REGION = 'us-west-2';
const IMPORTED_CMK = `arn:aws:kms:${REGION}:${ACCOUNT}:key/test-key-id`;

function buildTemplate(): Template {
  const prev = process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT;
  process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT = 'ops@example.invalid';
  try {
    const app = new cdk.App();
    const stack = new VsaTier2Stack(app, 'vsa-staging-tier2', {
      cmsFiInternalApiEndpoint: TEST_CMS_FI_INTERNAL_API_ENDPOINT,
      dmsFiInternalApiEndpoint: TEST_DMS_FI_INTERNAL_API_ENDPOINT,
      env: { account: ACCOUNT, region: REGION },
      stage: 'staging',
      adpRegion: 'us-east-1',
      adpAccountId: ACCOUNT,
      cmsKmsKeyArn: { literal: IMPORTED_CMK, parameterName: '/cvx/staging/cms-kms-key-arn' },
    });
    return Template.fromStack(stack);
  } finally {
    if (prev === undefined) delete process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT;
    else process.env.CVX_AVX_BROKER_ESCALATION_ENDPOINT = prev;
  }
}

const tmpl = buildTemplate();

// ─── helpers ─────────────────────────────────────────────────────────────────

function brokerStatements(): any[] {
  const brokerRoleId = Object.keys(tmpl.findResources('AWS::IAM::Role', {
    Properties: { RoleName: `vsa-staging-avx-broker-role-${REGION}` },
  }))[0];
  return Object.values(tmpl.findResources('AWS::IAM::Policy'))
    .filter((p: any) => JSON.stringify(p.Properties.Roles).includes(brokerRoleId))
    .flatMap((p: any) => p.Properties.PolicyDocument.Statement);
}

const asList = (x: unknown): unknown[] => (Array.isArray(x) ? x : [x]);

// ─── AvxAgentRuns table TTL ───────────────────────────────────────────────────

describe('AvxAgentRuns table TTL (T1.5)', () => {
  test('AvxAgentRuns table has a timeToLiveAttribute configured (RED until added)', () => {
    // RED: the table currently has no TimeToLiveSpecification; this test fails.
    const tables = tmpl.findResources('AWS::DynamoDB::Table', {
      Properties: { TableName: 'vsa-staging-avx-agent-runs' },
    });
    const ids = Object.keys(tables);
    expect(ids).toHaveLength(1);
    const props = (tables[ids[0]] as any).Properties;
    expect(props.TimeToLiveSpecification).toBeDefined();
    expect(props.TimeToLiveSpecification.Enabled).toBe(true);
    expect(typeof props.TimeToLiveSpecification.AttributeName).toBe('string');
    expect(props.TimeToLiveSpecification.AttributeName.length).toBeGreaterThan(0);
  });

  test('AvxAgentRuns TTL attribute is named "ttl" (matches other AVX tables)', () => {
    // RED: table has no TTL configuration yet; attribute name must be "ttl".
    const tables = tmpl.findResources('AWS::DynamoDB::Table', {
      Properties: { TableName: 'vsa-staging-avx-agent-runs' },
    });
    const ids = Object.keys(tables);
    expect(ids).toHaveLength(1);
    const ttlSpec = (tables[ids[0]] as any).Properties.TimeToLiveSpecification;
    expect(ttlSpec?.AttributeName).toBe('ttl');
  });

  test('AvxAgentRuns table PK and SK names are unchanged (guard: no key schema change)', () => {
    const tables = tmpl.findResources('AWS::DynamoDB::Table', {
      Properties: { TableName: 'vsa-staging-avx-agent-runs' },
    });
    const ids = Object.keys(tables);
    expect(ids).toHaveLength(1);
    const schema: any[] = (tables[ids[0]] as any).Properties.KeySchema;
    const hash = schema.find((k: any) => k.KeyType === 'HASH');
    const range = schema.find((k: any) => k.KeyType === 'RANGE');
    expect(hash?.AttributeName).toBe('agent_component');
    expect(range?.AttributeName).toBe('started_at');
  });
});

// ─── Broker stream-read cleanup ───────────────────────────────────────────────

describe('Broker stream-read cleanup (T1.5)', () => {
  test('BrokerAvxFindingsStreamRead explicit statement is absent (RED until removed)', () => {
    // RED: the statement currently exists; after the fix it must be gone.
    const stmts = brokerStatements();
    const streamReadSid = stmts.find((s: any) => s.Sid === 'BrokerAvxFindingsStreamRead');
    expect(streamReadSid).toBeUndefined();
  });

  test('no broker policy statement resource contains "/stream/*" (glob test)', () => {
    // RED: the explicit statement has resource "…/stream/*"; after the fix it must be gone.
    const stmts = brokerStatements();
    const allResources = stmts.flatMap((s: any) => asList(s.Resource));
    const streamWildcards = allResources.filter(
      (r: unknown) => typeof r === 'string' && r.includes('/stream/*'),
    );
    expect(streamWildcards).toEqual([]);
  });

  test('broker still has GetRecords, GetShardIterator and DescribeStream actions', () => {
    // The CDK-injected stream-read grant (no Sid) must still cover these actions.
    const stmts = brokerStatements();
    const allActions = stmts.flatMap((s: any) => asList(s.Action));
    expect(allActions).toContain('dynamodb:GetRecords');
    expect(allActions).toContain('dynamodb:GetShardIterator');
    expect(allActions).toContain('dynamodb:DescribeStream');
  });

  test('CDK-added stream-read grant uses the exact StreamArn, not a /stream/* wildcard', () => {
    // The DynamoEventSource addEventSource grants via Fn::GetAtt [..., "StreamArn"].
    const stmts = brokerStatements();
    const streamReadStmts = stmts.filter((s: any) =>
      asList(s.Action).includes('dynamodb:GetRecords') ||
      asList(s.Action).includes('dynamodb:GetShardIterator'),
    );
    expect(streamReadStmts.length).toBeGreaterThanOrEqual(1);
    for (const s of streamReadStmts) {
      const resources = asList(s.Resource);
      for (const r of resources) {
        if (typeof r === 'string') {
          expect(r).not.toContain('/stream/*');
        } else {
          // CDK intrinsic — must NOT embed "/stream/*" as a joined string
          const serialised = JSON.stringify(r);
          expect(serialised).not.toContain('/stream/*');
        }
      }
    }
  });
});
