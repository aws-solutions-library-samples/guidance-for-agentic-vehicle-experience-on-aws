/**
 * A1 infrastructure: TTL attribute on AvxFindings (spec §B, task A1).
 *
 * RED phase: AvxFindings currently has no TimeToLiveSpecification.
 *
 * Context 'aws:cdk:bundling-stacks': [] skips Docker asset bundling so the test
 * can run without Docker (same approach used for all CDK unit tests in this repo
 * that need VsaTier2Stack but not a deployed Lambda image).
 *
 * Mutation guard (recorded in mutations-A1-infra.md):
 *   Remove timeToLiveAttribute: 'ttl' from AvxFindings → this test MUST fail.
 */

import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { VsaTier2Stack } from '../lib/vsa-tier2-stack';
import { TEST_CMS_FI_INTERNAL_API_ENDPOINT } from './helpers/cms-fi-internal-endpoint';
import { TEST_DMS_FI_INTERNAL_API_ENDPOINT } from './helpers/dms-fi-internal-endpoint';

// Memoised: CDK synthesis takes time even without Docker; sharing the instance
// across tests in this file avoids duplicate work.
let _cached: { stack: VsaTier2Stack; template: Template } | undefined;

function makeStack(): { stack: VsaTier2Stack; template: Template } {
  if (_cached) return _cached;
  // 'aws:cdk:bundling-stacks': [] tells CDK that no stacks require asset
  // bundling (bundlingRequired = false for every stack), so Docker is never
  // invoked. The Lambda Code asset uses its source path as a placeholder;
  // DynamoDB table assertions are unaffected.
  const app = new cdk.App({ context: { 'aws:cdk:bundling-stacks': [] } });
  const stack = new VsaTier2Stack(app, 'vsa-staging-tier2-ttl-test', {
    cmsFiInternalApiEndpoint: TEST_CMS_FI_INTERNAL_API_ENDPOINT,
    dmsFiInternalApiEndpoint: TEST_DMS_FI_INTERNAL_API_ENDPOINT,
    env: { account: '123456789012', region: 'us-west-2' },
    stage: 'staging',
    adpRegion: 'us-east-1',
    adpAccountId: '123456789012',
    cmsKmsKeyArn: {
      literal: 'arn:aws:kms:us-west-2:123456789012:key/test-key-id',
      parameterName: '/cvx/staging/cms-kms-key-arn',
    },
  });
  _cached = { stack, template: Template.fromStack(stack) };
  return _cached;
}

describe('AvxFindings TTL — A1', () => {
  test('AvxFindings has TimeToLiveSpecification with AttributeName ttl — MUTATION GUARD A1', () => {
    // Will be RED until timeToLiveAttribute: 'ttl' is added to AvxFindings in
    // vsa-tier2-stack.ts.  After the fix this asserts the property is set and
    // enabled (Enabled: true is what CDK renders for timeToLiveAttribute).
    const { template } = makeStack();
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'vsa-staging-avx-findings',
      TimeToLiveSpecification: { AttributeName: 'ttl', Enabled: true },
    });
  });
});
