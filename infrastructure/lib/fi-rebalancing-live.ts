/**
 * Feature flag for the FI rebalancing Tier 2 component (spec D8, task 5.2).
 *
 * When true: the EventBridge Scheduler schedule
 * ``vsa-{stage}-tier2-daily-rebalance-{region}`` is ENABLED and the
 * run-staleness alarm for the ``rebalance`` component has
 * ``ActionsEnabled=true``.  When false, the schedule is DISABLED and the alarm
 * has no actions.  The IAM grants in ``FiRebalanceTier2Policy``, the Lambda
 * environment variables, and every ``cdk-nag`` suppression ship unchanged
 * regardless, so the only observable difference between live and not-live is
 * the schedule state and the alarm action.
 *
 * Flipped to true by Group 6 task 6.2 (2026-10-17), after ADP's
 * fleet_bookings product, forecast and views were published (6.1). Setting it
 * back to false is the off switch. ``agents/tier2/tests/
 * test_fi_rebalancing_live_defaults_off.py`` pins the value, so changing it is
 * a reviewed edit of both files together.
 */
export const FI_REBALANCING_LIVE = true;
