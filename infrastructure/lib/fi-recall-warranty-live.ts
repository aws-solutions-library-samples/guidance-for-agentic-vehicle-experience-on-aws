/**
 * Feature flag for the FI recall/warranty Tier 2 components.
 *
 * When false (default): the two EventBridge Scheduler schedules are DISABLED,
 * their run-staleness alarms have ActionsEnabled=false, and the analytics stack
 * skips the fleet_recall Lake Formation grants (adp_{stage}_fleet_recall does not
 * exist until ADP task 5.1, and a LF grant on a missing database fails the deploy —
 * the same lesson as decisions.md call 15).
 *
 * Task 5.4 flipped this to true (2026-10-09) after ADP task 5.1 created
 * adp_{stage}_fleet_recall (decisions.md calls 55 and 61). Setting it back to false
 * disables both schedules and their alarm actions and removes the fleet_recall grants.
 */
export const FI_RECALL_WARRANTY_LIVE = true;
