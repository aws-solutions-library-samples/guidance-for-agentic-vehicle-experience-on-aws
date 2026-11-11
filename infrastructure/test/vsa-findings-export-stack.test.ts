/**
 * A1 infrastructure: VsaFindingsExportStack (spec §B, task A1).
 *
 * RED phase: vsa-findings-export-stack.ts does not exist yet; this file will fail
 * to compile/import until the stack is created.
 *
 * Bucket spec: vsa-{stage}-avx-findings-export-{region}-{account}
 *   BlockPublicAccess.BLOCK_ALL, BucketEncryption.S3_MANAGED, enforceSSL: true,
 *   versioning on, RemovalPolicy.RETAIN, output FindingsExportBucketName.
 *   No other resources; no grants.
 *
 * Mutation guards recorded in mutations-A1-infra.md:
 *   RETAIN → DESTROY, BPA dropped, enforceSSL false, versioning off.
 */

import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { VsaFindingsExportStack } from '../lib/vsa-findings-export-stack';

function makeStack(opts: { stage?: string; region?: string; account?: string } = {}): {
  stack: VsaFindingsExportStack;
  template: Template;
} {
  const stage   = opts.stage   ?? 'staging';
  const region  = opts.region  ?? 'us-west-2';
  const account = opts.account ?? '123456789012';
  const app = new cdk.App();
  const stack = new VsaFindingsExportStack(app, `vsa-${stage}-findings-export`, {
    env: { account, region },
    stage,
    description: 'Guidance for Agentic Vehicle Experience (SO9676) - Findings Export',
  });
  return { stack, template: Template.fromStack(stack) };
}

// ─── Bucket name length budget ───────────────────────────────────────────────

describe('bucket name length budget', () => {
  test('name ≤ 63 chars for a 14-char region: synthesise with ap-southeast-7 and measure template BucketName', () => {
    // Longest commercial region today: ap-southeast-7 (14 chars, verified 2026-06-04).
    // We synthesise the real stack so the test measures what the CDK actually builds,
    // not a hand-constructed string that could drift from the stack's naming code.
    const { template } = makeStack({
      stage: 'staging',
      region: 'ap-southeast-7',
      account: '123456789012',
    });
    const buckets = template.findResources('AWS::S3::Bucket');
    const bucketKey = Object.keys(buckets)[0];
    const bucketName: string = buckets[bucketKey].Properties.BucketName;
    // "vsa-staging-avx-findings-export-ap-southeast-7-123456789012" = 59 chars ≤ 63
    expect(bucketName.length).toBeLessThanOrEqual(63);
  });
});

// ─── Bucket properties ───────────────────────────────────────────────────────

describe('VsaFindingsExportStack — bucket properties', () => {
  const { template } = makeStack();

  test('exactly one S3 bucket in the stack', () => {
    const buckets = template.findResources('AWS::S3::Bucket');
    expect(Object.keys(buckets)).toHaveLength(1);
  });

  test('bucket name is vsa-{stage}-avx-findings-export-{region}-{account}', () => {
    template.hasResourceProperties('AWS::S3::Bucket', {
      BucketName: 'vsa-staging-avx-findings-export-us-west-2-123456789012',
    });
  });

  test('BlockPublicAccess is BLOCK_ALL (all four booleans true)', () => {
    template.hasResourceProperties('AWS::S3::Bucket', {
      PublicAccessBlockConfiguration: {
        BlockPublicAcls:       true,
        BlockPublicPolicy:     true,
        IgnorePublicAcls:      true,
        RestrictPublicBuckets: true,
      },
    });
  });

  test('encryption is S3_MANAGED (SSE-S3)', () => {
    template.hasResourceProperties('AWS::S3::Bucket', {
      BucketEncryption: {
        ServerSideEncryptionConfiguration: Match.arrayWith([
          Match.objectLike({
            ServerSideEncryptionByDefault: {
              SSEAlgorithm: 'AES256',
            },
          }),
        ]),
      },
    });
  });

  test('versioning is enabled', () => {
    template.hasResourceProperties('AWS::S3::Bucket', {
      VersioningConfiguration: { Status: 'Enabled' },
    });
  });

  test('RemovalPolicy is RETAIN (DeletionPolicy: Retain) — MUTATION GUARD A1', () => {
    // CDK translates RemovalPolicy.RETAIN to DeletionPolicy: Retain on the L1.
    template.hasResource('AWS::S3::Bucket', {
      DeletionPolicy: 'Retain',
    });
  });

  test('enforceSSL — bucket policy denies non-HTTPS requests — MUTATION GUARD A1', () => {
    // CDK's enforceSSL: true adds a bucket policy statement with condition
    // aws:SecureTransport = false → Deny.
    template.hasResourceProperties('AWS::S3::BucketPolicy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: 'Deny',
            Condition: {
              Bool: { 'aws:SecureTransport': 'false' },
            },
          }),
        ]),
      },
    });
  });

  test('no IAM roles or grants (operator credentials run the export)', () => {
    // The export stack must not create any IAM roles or policies.
    const iamRoles    = template.findResources('AWS::IAM::Role');
    const iamPolicies = template.findResources('AWS::IAM::Policy');
    expect(Object.keys(iamRoles)).toHaveLength(0);
    expect(Object.keys(iamPolicies)).toHaveLength(0);
  });

  test('CfnOutput FindingsExportBucketName exists', () => {
    const outputs = template.findOutputs('FindingsExportBucketName');
    expect(Object.keys(outputs)).toHaveLength(1);
  });
});
