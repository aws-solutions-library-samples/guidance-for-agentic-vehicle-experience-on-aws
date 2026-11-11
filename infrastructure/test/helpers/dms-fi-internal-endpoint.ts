/**
 * DMS's internal API endpoint for tests that build `VsaTier2Stack`.
 *
 * Since prompt 347's go (2026-11-08) `FI_AUTO_REBOOK_LIVE` is true, so the default synth requires
 * `dmsFiInternalApiEndpoint` and throws without it (`vsa-tier2-stack.ts`). Tests that aren't about
 * rule rebooking pass this so they synthesize the configuration that deploys, as they pass
 * `TEST_CMS_FI_INTERNAL_API_ENDPOINT` for rule moves. `vsa-tier2-stack-fi-auto-rebook.test.ts`
 * owns the on/off and endpoint-shape cases and builds its own values.
 *
 * The host (`dmsapi111`, nine characters) is below the publish scanner's ten-character
 * execute-api host pattern, as in `vsa-tier2-stack-fi-auto-rebook.test.ts`; the shape is DMS's
 * `DmsInternalApiEndpoint` output (stage included), which the stack's synth check requires of a
 * literal.
 */
export const TEST_DMS_FI_INTERNAL_API_ENDPOINT = {
  literal: 'https://dmsapi111.execute-api.us-west-2.amazonaws.com/staging/',
  parameterName: '/cvx/staging/dms-fi-internal-api-endpoint',
};
