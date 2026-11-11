/**
 * Feature flag for rule-approved rebooking (FI Ground Book Return task 11.7 / Group 8,
 * D7; spec `.kiro/specs/2026-11-02-fi-ground-book-return`).
 *
 * On since 2026-11-08: the user's yes in prompt 347 ("yes, lets just do it"), after predicate (g)
 * (the hold list) was added. Turning it off again is setting this false and deploying
 * `vsa-<stage>-tier2`; disabling a fleet's rule stops that fleet without a deploy.
 *
 * When false: the auto-approver Lambda
 * gets no `FI_AUTO_REBOOK_ENABLED`, no DMS internal endpoint, no
 * `execute-api:Invoke` on DMS's repair-orders internal route, no findings/groundings/
 * actions-GetItem grants, so it never considers rule-decided `book_fleet_service` and
 * can't reach DMS. Approval rules for rebooking can still be created (disabled) and
 * enabled, and do nothing; every rebook rule that is enabled when this flips takes
 * effect at once, so list them per fleet before flipping.
 *
 * When true: those env vars and grants (plus `FI_SIM_OPERATOR_PARAM` and GetParameter on
 * `/cvx/{stage}/fi/sim-operator`, the hold list), and `/cvx/{stage}/dms-fi-internal-api-endpoint`
 * must be seeded (the deploy resolves it). Flip it only after DMS's internal API is
 * deployed and the endpoint parameter is seeded.
 *
 * `agents/executors/tests/test_dms_rebook_signed.py` and
 * `infrastructure/test/vsa-tier2-stack-fi-auto-rebook.test.ts` pin what off withholds.
 */
export const FI_AUTO_REBOOK_LIVE = true;
