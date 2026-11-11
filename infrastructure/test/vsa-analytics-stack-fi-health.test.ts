/**
 * FI Stage D task 4.3 stage C — analytics-stack Lake Formation grants for
 * the fleet-health named tables, gated on fiFleetHealthLive.
 *
 * Assertions:
 *   (a) Default synth (no prop: the constant, true since Group 6 rest-plan step 7):
 *       the same 15 stage D grants as an explicit true (12 'LfFiHealth' resources plus
 *       3 from 9.3's shared engineering helper); explicit false: no 'LfFiHealth'.
 *   (b) With fiFleetHealthLive: true: exactly 4 database DESCRIBE grants and
 *       12 named-table SELECT+DESCRIBE grants, all on the Tier 2 role ARN.
 *   (c) Permissions exactly ['DESCRIBE'] / ['SELECT','DESCRIBE']; none uses
 *       TableWildcard.
 *   (d) No CfnPrincipalPermissions for the Tier 2 role names a cms_ingest
 *       database, fleet_ops.daily_usage, or a dms_ database.
 *
 * Mutation record:
 *   M1 — at 4.3, constant set true: the default-has-none test failed (caught). Since
 *        Group 6 rest-plan step 7 the direction is reversed: constant back to false fails
 *        'the default synth carries exactly the live grants' (g6rest live-mut L01).
 *   M2 — fleet_health granted by tableWildcard instead of named table:
 *        test_no_table_wildcard FAILS. Caught. Restored.
 *   M3 — one named table dropped (fault_codes removed from fiHealthNamedTables):
 *        test_exactly_12_table_grants FAILS (11 found). Caught. Restored.
 */

import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { VsaAnalyticsStack } from '../lib/vsa-analytics-stack';

// ─── Constants ───────────────────────────────────────────────────────────────

const ACCOUNT    = '123456789012';
const ADP_REGION = 'us-east-1';
const STAGE      = 'staging';
const TIER2_ROLE = `vsa-${STAGE}-tier2-scheduler-us-west-2`;
const TIER2_ARN  = `arn:aws:iam::${ACCOUNT}:role/${TIER2_ROLE}`;

// ─── Factory ─────────────────────────────────────────────────────────────────

function makeAnalytics(fiFleetHealthLive?: boolean): Template {
  const app = new cdk.App();
  const stack = new VsaAnalyticsStack(app, 'vsa-staging-analytics', {
    env: { account: ACCOUNT, region: ADP_REGION },
    stage: STAGE,
    agentCoreRoleName: `AmazonBedrockAgentCoreSDKRuntime-us-west-2-${STAGE}`,
    tier2RoleName: TIER2_ROLE,
    // Keep recall/warranty live as the real constant decides (currently true);
    // this test does not exercise that path.
    ...(fiFleetHealthLive !== undefined ? { fiFleetHealthLive } : {}),
  });
  return Template.fromStack(stack);
}

// Lazy-computed templates — synthesise once per run.
// defaultTemplate() passes NO fiFleetHealthLive prop so the shared constant
// (FI_FLEET_HEALTH_LIVE, true since Group 6 rest-plan step 7) governs the synth. That is
// exactly what a deploy does, and it is what makes the M1 mutation (constant back to false)
// detectable: passing true explicitly would mask it. offTemplate() keeps the gate tested.
let _defaultTemplate: Template | undefined;
let _liveTemplate: Template | undefined;
let _offTemplate: Template | undefined;
function defaultTemplate(): Template { return (_defaultTemplate ??= makeAnalytics()); }
function liveTemplate():    Template { return (_liveTemplate    ??= makeAnalytics(true)); }
function offTemplate():     Template { return (_offTemplate     ??= makeAnalytics(false)); }

// ─── Helpers ─────────────────────────────────────────────────────────────────

/**
 * All CfnPrincipalPermissions resources in `t` whose logical ID starts with
 * 'LfFiHealth'.
 */
function lfFiHealthResources(t: Template): Array<{ id: string; props: Record<string, unknown> }> {
  const all = t.toJSON().Resources as Record<string, { Type: string; Properties: Record<string, unknown> }>;
  return Object.entries(all)
    .filter(([id, r]) => r.Type === 'AWS::LakeFormation::PrincipalPermissions' && id.startsWith('LfFiHealth'))
    .map(([id, r]) => ({ id, props: r.Properties }));
}

/**
 * All CfnPrincipalPermissions resources in `t` whose principal matches the
 * Tier 2 role ARN (regardless of logical ID prefix).
 */
function tier2GrantResources(t: Template): Array<Record<string, unknown>> {
  const all = t.toJSON().Resources as Record<string, { Type: string; Properties: Record<string, unknown> }>;
  return Object.values(all)
    .filter(
      (r) =>
        r.Type === 'AWS::LakeFormation::PrincipalPermissions' &&
        (r.Properties as any)?.Principal?.DataLakePrincipalIdentifier === TIER2_ARN,
    )
    .map((r) => r.Properties as Record<string, unknown>);
}

// ─── Tests: default (the constant) and explicit off ─────────────────────────────────────────────

/**
 * FI stage D 9.3: the Tier 2 role's grants on the three stage D databases, selected by
 * PRINCIPAL, not by the 'LfFiHealth' logical-id prefix. The engineering database grant and
 * vehicle_build_components are shared with the live recall run (fiRwLive) and made once by
 * the analytics stack's grantTier2Engineering helper (ids LfDbTier2Engineering,
 * LfTblTier2Engineering*), so a prefix filter no longer sees them. With the recall run live
 * (the shared constant) the union also holds engineering.recall_campaigns_issued.
 * Architect-authorized change (9.3): the four count/coverage tests below counted by prefix.
 */
function stageDGrants(t: Template): Array<{ id: string; props: Record<string, unknown> }> {
  const dbs = new Set(['fleet_health', 'engineering', 'ota_campaigns'].map((d) => `adp_${STAGE}_${d}`));
  const all = t.toJSON().Resources as Record<string, { Type: string; Properties: Record<string, unknown> }>;
  return Object.entries(all)
    .filter(([, r]) => r.Type === 'AWS::LakeFormation::PrincipalPermissions'
      && (r.Properties as any)?.Principal?.DataLakePrincipalIdentifier === TIER2_ARN)
    .filter(([, r]) => {
      const res = (r.Properties as any).Resource ?? {};
      return dbs.has(res.Database?.Name ?? res.Table?.DatabaseName ?? res.TableWithColumns?.DatabaseName);
    })
    .map(([id, r]) => ({ id, props: r.Properties }));
}

describe('default synth (no prop: FI_FLEET_HEALTH_LIVE, true since Group 6 rest-plan step 7)', () => {
  test('the default synth carries exactly the live grants', () => {
    // A default synth is what a deploy from main ships. M1 guard: setting the constant
    // back to false leaves it with none, and this fails.
    // By principal (stageDGrants), since 9.3 moved two of them to the shared helper's ids.
    const byId = (t: Template) => stageDGrants(t).sort((x, y) => x.id.localeCompare(y.id));
    expect(byId(defaultTemplate())).toHaveLength(3 + 12);
    expect(byId(defaultTemplate())).toEqual(byId(liveTemplate()));
    expect(lfFiHealthResources(defaultTemplate())).toHaveLength(12);
  });

  test('fiFleetHealthLive: false synthesises no LfFiHealth resources (the gate still works)', () => {
    expect(lfFiHealthResources(offTemplate())).toHaveLength(0);
  });
});

describe('default synth: only the 12 LfFiHealth grants are added for the Tier 2 role', () => {
  // The other 3 of the 15 (engineering DESCRIBE, vehicle_build_components and the recall
  // run's recall_campaigns_issued) come from the live recall run and exist with the switch off.
  test('default minus off is exactly the LfFiHealth set', () => {
    const tier2Ids = (t: Template) => Object.entries(t.toJSON().Resources as Record<string, any>)
      .filter(([, r]) => r.Type === 'AWS::LakeFormation::PrincipalPermissions'
        && r.Properties?.Principal?.DataLakePrincipalIdentifier === TIER2_ARN)
      .map(([id]) => id);
    const on = new Set(tier2Ids(defaultTemplate()));
    const off = new Set(tier2Ids(offTemplate()));
    const added = [...on].filter((id) => !off.has(id)).sort();
    const removed = [...off].filter((id) => !on.has(id));
    expect(removed).toEqual([]);
    expect(added).toEqual(lfFiHealthResources(defaultTemplate()).map(({ id }) => id).sort());
  });
});

// ─── Tests: live (switch on) ─────────────────────────────────────────────────

describe('fiFleetHealthLive: true — database DESCRIBE grants', () => {
  test('exactly 4 database grants', () => {
    const dbGrants = stageDGrants(liveTemplate()).filter(
      ({ props }) => (props['Resource'] as any)?.Database !== undefined,
    );
    // M1 guard: count must be exactly 3 (fleet_health, engineering, ota_campaigns).
    expect(dbGrants).toHaveLength(3);
  });

  test('each database grant has permissions: [DESCRIBE] only', () => {
    const dbGrants = stageDGrants(liveTemplate()).filter(
      ({ props }) => (props['Resource'] as any)?.Database !== undefined,
    );
    for (const { props } of dbGrants) {
      expect(props['Permissions']).toEqual(['DESCRIBE']);
      expect(props['PermissionsWithGrantOption']).toEqual([]);
    }
  });

  test('all 4 expected databases are covered', () => {
    const dbGrants = stageDGrants(liveTemplate()).filter(
      ({ props }) => (props['Resource'] as any)?.Database !== undefined,
    );
    const names = dbGrants.map(({ props }) => (props['Resource'] as any).Database.Name as string);
    expect(names.sort()).toEqual([
      `adp_${STAGE}_engineering`,
      `adp_${STAGE}_fleet_health`,
      `adp_${STAGE}_ota_campaigns`,
    ]);
  });

  test('principal on every database grant is Tier 2 role ARN', () => {
    const dbGrants = stageDGrants(liveTemplate()).filter(
      ({ props }) => (props['Resource'] as any)?.Database !== undefined,
    );
    for (const { props } of dbGrants) {
      expect((props['Principal'] as any).DataLakePrincipalIdentifier).toBe(TIER2_ARN);
    }
  });
});

describe('fiFleetHealthLive: true — named table SELECT+DESCRIBE grants', () => {
  test('exactly 12 table grants (11 stage D + the recall run\'s recall_campaigns_issued)', () => {
    const tblGrants = stageDGrants(liveTemplate()).filter(
      ({ props }) => (props['Resource'] as any)?.Table !== undefined,
    );
    // M3 guard: dropping any table from the list causes this to fail.
    // 11 stage D tables + engineering.recall_campaigns_issued from the live recall run (9.3).
    expect(tblGrants).toHaveLength(12);
  });

  test('each table grant has permissions: [SELECT, DESCRIBE] only', () => {
    const tblGrants = stageDGrants(liveTemplate()).filter(
      ({ props }) => (props['Resource'] as any)?.Table !== undefined,
    );
    for (const { props } of tblGrants) {
      expect(props['Permissions']).toEqual(['SELECT', 'DESCRIBE']);
      expect(props['PermissionsWithGrantOption']).toEqual([]);
    }
  });

  test('no table grant uses TableWildcard', () => {
    const tblGrants = stageDGrants(liveTemplate()).filter(
      ({ props }) => (props['Resource'] as any)?.Table !== undefined,
    );
    // M2 guard: a tableWildcard grant makes this fail.
    for (const { props } of tblGrants) {
      const tbl = (props['Resource'] as any).Table;
      expect(tbl.TableWildcard).toBeUndefined();
      expect(typeof tbl.Name).toBe('string');
    }
  });

  test('all 12 expected named tables are covered', () => {
    const tblGrants = stageDGrants(liveTemplate()).filter(
      ({ props }) => (props['Resource'] as any)?.Table !== undefined,
    );
    const pairs = tblGrants
      .map(({ props }) => {
        const tbl = (props['Resource'] as any).Table;
        return `${tbl.DatabaseName}::${tbl.Name}` as string;
      })
      .sort();

    expect(pairs).toEqual([
      `adp_${STAGE}_engineering::engineering_failure_modes`,
      `adp_${STAGE}_engineering::engineering_remedy_options`,
      `adp_${STAGE}_engineering::fault_patterns`,
      `adp_${STAGE}_engineering::recall_campaigns_issued`,   // the live recall run (9.3)
      `adp_${STAGE}_engineering::vehicle_build_components`,
      `adp_${STAGE}_fleet_health::fault_codes`,
      `adp_${STAGE}_fleet_health::pm_vehicle_status_daily`,
      `adp_${STAGE}_fleet_health::snapshot_runs`,
      `adp_${STAGE}_fleet_health::vehicle_fault_status_daily`,
      `adp_${STAGE}_fleet_health::vehicle_utilisation_daily`,
      `adp_${STAGE}_ota_campaigns::ota_campaign_events`,
      `adp_${STAGE}_ota_campaigns::ota_campaigns`,
    ]);
  });

  test('principal on every table grant is Tier 2 role ARN', () => {
    const tblGrants = stageDGrants(liveTemplate()).filter(
      ({ props }) => (props['Resource'] as any)?.Table !== undefined,
    );
    for (const { props } of tblGrants) {
      expect((props['Principal'] as any).DataLakePrincipalIdentifier).toBe(TIER2_ARN);
    }
  });
});

// ─── Boundary guard: Tier 2 role must not reach forbidden databases ───────────

describe('Tier 2 role must not reach forbidden databases', () => {
  test('no grant for Tier 2 role names a cms_ingest database', () => {
    const grants = tier2GrantResources(liveTemplate());
    const bad = grants.filter((p) => JSON.stringify(p).includes('cms_ingest'));
    expect(bad).toHaveLength(0);
  });

  test('no grant for Tier 2 role names fleet_ops.daily_usage', () => {
    const grants = tier2GrantResources(liveTemplate());
    const bad = grants.filter((p) => JSON.stringify(p).includes('daily_usage'));
    expect(bad).toHaveLength(0);
  });

  test('no grant for Tier 2 role names a dms_ database', () => {
    const grants = tier2GrantResources(liveTemplate());
    const bad = grants.filter((p) => JSON.stringify(p).includes('"dms_'));
    expect(bad).toHaveLength(0);
  });
});

// ─── Who holds stage D's grants (FI stage D 9.3, review g93 c1 W1) ───────────
//
// The count tests above select by principal, so on their own they can't see a stage D grant
// handed to ANOTHER principal (the LfFiHealth prefix selection used to). These two restore it:
// every LfFiHealth* resource is the Tier 2 role's, and on fleet_health and engineering the
// Tier 2 role is the only principal in the stack. (ota_campaigns is left out of the second:
// the AgentCore role's ota_campaigns wildcard predates stage D.)
describe('stage D grants belong to the Tier 2 role only', () => {
  const states: Array<[string, Template]> = [];
  beforeAll(() => {
    states.push(['live', liveTemplate()], ['default', defaultTemplate()]);
  });

  test('every LfFiHealth* grant names the Tier 2 role', () => {
    const fh = lfFiHealthResources(liveTemplate());
    expect(fh.length).toBeGreaterThan(0);   // anti-vacuity
    for (const { id, props } of fh) {
      expect({ id, principal: (props['Principal'] as any).DataLakePrincipalIdentifier })
        .toEqual({ id, principal: TIER2_ARN });
    }
  });

  test('on fleet_health and engineering, no principal but the Tier 2 role holds a grant', () => {
    expect(states.map(([l]) => l)).toEqual(['live', 'default']);   // anti-vacuity (review g93 c2 S2-2)
    for (const [label, t] of states) {
      const all = t.toJSON().Resources as Record<string, { Type: string; Properties: any }>;
      const holders = new Set<string>();
      for (const r of Object.values(all)) {
        if (r.Type !== 'AWS::LakeFormation::PrincipalPermissions') continue;
        const res = r.Properties.Resource ?? {};
        const db = res.Database?.Name ?? res.Table?.DatabaseName ?? res.TableWithColumns?.DatabaseName;
        if (db === `adp_${STAGE}_fleet_health` || db === `adp_${STAGE}_engineering`) {
          holders.add(JSON.stringify(r.Properties.Principal));
        }
      }
      // default: the recall run's engineering grants; live: those plus stage D's.
      expect({ label, holders: [...holders] }).toEqual({
        label, holders: [JSON.stringify({ DataLakePrincipalIdentifier: TIER2_ARN })],
      });
    }
  });
});
