/**
 * CMS's internal API endpoint for tests that build `VsaTier2Stack`.
 *
 * Since task 8.4's go (FI rebalancing stage E, 2026-10-24) `FI_REBALANCING_AUTO_APPROVAL_LIVE`
 * is true, so the default synth requires `cmsFiInternalApiEndpoint` and throws without it
 * (`vsa-tier2-stack.ts`). Tests that aren't about rule-approved moves pass this so they
 * synthesize the configuration that deploys. `vsa-tier2-stack-fi-auto-moves.test.ts` owns the
 * on/off and endpoint-shape cases and builds its own values.
 *
 * The host is the publish scanner's `yourapi000` placeholder; the shape is CMS's
 * `FiInternalApiEndpoint` output (`RestApi.url`, stage included), which the stack's synth
 * check requires of a literal.
 */
export const TEST_CMS_FI_INTERNAL_API_ENDPOINT = {
  literal: 'https://yourapi000.execute-api.us-west-2.amazonaws.com/prod/',
  parameterName: '/cvx/staging/cms-fi-internal-api-endpoint',
};
