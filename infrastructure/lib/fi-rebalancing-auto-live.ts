/**
 * Feature flag for rule-approved moves (FI rebalancing stage E Group 8, U6; spec
 * `.kiro/specs/2026-10-06-fi-rebalancing-stage-e/group8-notes.md` G8-D9).
 *
 * When false (the default and every deploy until task 8.4's go; now only a rollback): the
 * auto-approver Lambda gets no `FI_AUTO_MOVES_ENABLED`, no CMS internal endpoint, no
 * `execute-api:Invoke` on CMS's internal move route, no bounds read and no escalation
 * grants, so it never considers `move_vehicles` and can't reach CMS. Approval rules for
 * moves can still be created (disabled) and enabled, and do nothing; every move rule that is
 * enabled when this flips takes effect at once, so list them per fleet before flipping.
 *
 * When true: those env vars and grants, and `/cvx/{stage}/cms-fi-internal-api-endpoint`
 * must be seeded (the deploy resolves it). Flip it only in 8.4, after CMS's internal API is
 * deployed and the bounds parameter is seeded.
 *
 * Flipped to true for task 8.4 (2026-10-24, the user's go): CMS's internal API is live, and
 * the bounds and endpoint parameters are seeded on staging. Rollback: a commit setting it
 * back to false (the jest suite's explicit-false case still pins what off withholds).
 * `agents/tier2/tests/test_fi_rebalancing_auto_live_defaults_off.py` pins the value.
 */
export const FI_REBALANCING_AUTO_APPROVAL_LIVE = true;
