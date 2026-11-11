import * as cdk from 'aws-cdk-lib';
import * as athena from 'aws-cdk-lib/aws-athena';
import * as lakeformation from 'aws-cdk-lib/aws-lakeformation';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';
import { FI_RECALL_WARRANTY_LIVE } from './fi-recall-warranty-live';
import { FI_FLEET_HEALTH_LIVE } from './fi-fleet-health-live';

interface VsaAnalyticsStackProps extends cdk.StackProps {
  stage: string;
  /**
   * Name of the CVX AgentCore execution role (in the core stack's region) that
   * the persona-loader runs as. Used to grant Lake Formation DESCRIBE/SELECT on
   * the ADP product databases so cross-domain Athena queries succeed.
   */
  agentCoreRoleName: string;
  /**
   * Name of the Tier 2 Lambda execution role that runs the platform health agent.
   * Format: vsa-{stage}-tier2-scheduler-{region}  (IAM is account-wide; region-suffixed).
   * When supplied, grants Lake Formation SELECT + DESCRIBE on adp_{stage}_energy_usage
   * so the Tier 2 role can run Athena queries against the ADP lake.
   *
   * S1 grants energy_usage only — tire_health is absent from the ADP lake until
   * the ADP team deploys the product (T4.1 blocked, decisions.md § 2026-08-03).
   * Re-add tire_health alongside the tire component when T4.1 unblocks.
   *
   * Grant is read-only: SELECT + DESCRIBE; no CREATE_TABLE, ALTER, DROP, or ALL.
   */
  tier2RoleName?: string;
  /**
   * When true, provisions Lake Formation grants for the adp_{stage}_fleet_recall
   * database so the Tier 2 role can read the recall product via Athena.
   *
   * Must be false until ADP task 5.1 creates the database: a LF grant on a
   * missing database fails the deploy (the call 15 lesson from decisions.md).
   * Task 5.4 flips both stacks to true after 5.1 ships (decisions.md call 55).
   */
  fiRecallWarrantyLive?: boolean;
  /**
   * When true, provisions Lake Formation grants for the stage D fleet-health
   * named tables so the Tier 2 role can run Athena queries against them.
   *
   * Defaults to FI_FLEET_HEALTH_LIVE (true since Group 6 rest-plan step 7). Deploy
   * only once every named table exists (fi-fleet-health-live.ts lists them):
   * a LF grant on a missing table fails the deploy (same lesson as call 15).
   * No tableWildcard: fleet_health's views route through cms_ingest, which must
   * not be granted.
   */
  fiFleetHealthLive?: boolean;
}

/**
 * Cross-region Athena workgroup + region-suffixed results bucket.
 * Must be scoped to us-east-1 (env.region = 'us-east-1' in bin/vsa.ts)
 * so the workgroup runs in the same region as the ADP Glue catalog.
 *
 * Bucket name: cvx-{stage}-athena-results-{account}-us-east-1
 * Length budget: max 53 chars (staging + 12-digit account + longest region
 * ap-southeast-7 = 53 chars; well within S3 63-char limit).
 */
export class VsaAnalyticsStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: VsaAnalyticsStackProps) {
    super(scope, id, props);

    // Region suffix is hardcoded (not this.region) so the name matches the ARN
    // template in vsa-core-stack.ts CvxAthenaResultsS3 policy regardless of
    // any future stack-region drift. Stack is always deployed to us-east-1.
    const bucketName = `cvx-${props.stage}-athena-results-${this.account}-us-east-1`;

    const resultsBucket = new s3.Bucket(this, 'AthenaResults', {
      bucketName,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      lifecycleRules: [
        {
          prefix: 'athena-results/',
          expiration: cdk.Duration.days(30),
        },
      ],
    });

    const workgroup = new athena.CfnWorkGroup(this, 'AnalyticsWorkgroup', {
      name: `cvx-${props.stage}-analytics`,
      workGroupConfiguration: {
        resultConfiguration: {
          outputLocation: `s3://${resultsBucket.bucketName}/athena-results/`,
        },
        engineVersion: {
          selectedEngineVersion: 'AUTO',
        },
      },
    });

    new cdk.CfnOutput(this, 'WorkgroupName', {
      exportName: `cvx-${props.stage}-analytics-workgroup`,
      value: workgroup.name,
    });

    new cdk.CfnOutput(this, 'ResultsBucketName', {
      exportName: `cvx-${props.stage}-athena-results-bucket`,
      value: resultsBucket.bucketName,
    });

    // ── Lake Formation grants for the CVX AgentCore execution role ──────────
    // The persona-loader runs cross-domain Athena queries against the ADP
    // product databases (same account). LF gates Glue table access, so the
    // role needs DESCRIBE on each database + SELECT/DESCRIBE on its tables.
    // Codifies the grants applied during the 2026-06-19 staging fix
    // (issues/2026-06-19-staging-runtime-missing-foundation-memory-id). The CFN
    // exec role is an LF data-lake admin, so it can act as grantor.
    const roleArn = `arn:aws:iam::${this.account}:role/${props.agentCoreRoleName}`;
    const adpProducts = [
      'vehicle_identity', 'service_records', 'charging_sessions', 'customer_360',
      'customer_interactions', 'energy_usage', 'vehicle_telemetry_aggregated',
      'ota_campaigns',
    ];
    for (const product of adpProducts) {
      const dbName = `adp_${props.stage}_${product}`;
      const id = product.split('_').map(p => p.charAt(0).toUpperCase() + p.slice(1)).join('');
      new lakeformation.CfnPrincipalPermissions(this, `LfDb${id}`, {
        principal: { dataLakePrincipalIdentifier: roleArn },
        resource: { database: { catalogId: this.account, name: dbName } },
        permissions: ['DESCRIBE'],
        permissionsWithGrantOption: [],
      });
      new lakeformation.CfnPrincipalPermissions(this, `LfTbl${id}`, {
        principal: { dataLakePrincipalIdentifier: roleArn },
        resource: {
          table: { catalogId: this.account, databaseName: dbName, tableWildcard: {} },
        },
        permissions: ['SELECT', 'DESCRIBE'],
        permissionsWithGrantOption: [],
      });
    }

    // ── Lake Formation grants for the Tier 2 platform health agent role ────
    //
    // Security-review Cycle 4, T3.4: the ADP lake is strict-mode LF (no
    // IAMAllowedPrincipals, no default database permissions).  IAM alone grants
    // nothing; the Tier 2 Lambda role also needs explicit LF grants.
    //
    // S1 scope: energy_usage only.  tire_health is absent from the ADP lake
    // (decisions.md § 2026-08-03 T1.2 gate), so granting it would be a dead
    // grant that misleads readers into thinking tires are in scope.  Re-add when
    // scripts/verify-tier2-adp-reads.py --component tires exits 0.
    //
    // Read-only: SELECT + DESCRIBE only.  No CREATE_TABLE, ALTER, DROP, or ALL.
    // The grant lives here (us-east-1) rather than in the Tier 2 stack because
    // CfnPrincipalPermissions is a regional construct and must run in the same
    // region as the Glue catalog (us-east-1).  The Tier 2 stack deploys to the
    // Lambda's region (staging → us-west-2), so placing the grant there would
    // require a nested cross-region stack.  Concentrating all LF grants in the
    // analytics stack keeps the LF surface in one place.
    if (props.tier2RoleName) {
      const tier2RoleArn = `arn:aws:iam::${this.account}:role/${props.tier2RoleName}`;

      // Products newly read in S1 Group 4: energy_usage (battery), tire_health (tires),
      // service_records (brakes).  All three databases are granted identically:
      // DESCRIBE on the database + SELECT/DESCRIBE on the table wildcard, read-only,
      // no grant option.  The grant lives here (us-east-1, where the Glue catalog is)
      // rather than in the Tier 2 stack — CfnPrincipalPermissions is regional and must
      // run in the same region as the catalog.  Concentrating all LF grants in the
      // analytics stack keeps the LF surface in one place.
      //
      // Read-only: SELECT + DESCRIBE only.  No CREATE_TABLE, ALTER, DROP, or ALL.
      const tier2Products = [
        'energy_usage',    // battery component (S1 original)
        'tire_health',     // tires component  (unblocked 2026-08-04)
        'service_records', // brakes component (unblocked 2026-08-04)
        'vehicle_identity', // brakes query's model_year join (spec 2026-09-25-avx-own-vehicle-findings T4.3)
      ];

      for (const product of tier2Products) {
        const dbName = `adp_${props.stage}_${product}`;
        // Derive a PascalCase suffix for stable logical IDs (e.g. EnergyUsage, TireHealth).
        const suffix = product.split('_').map(p => p.charAt(0).toUpperCase() + p.slice(1)).join('');

        // DESCRIBE on the database (required alongside table-level grants).
        new lakeformation.CfnPrincipalPermissions(this, `LfDbTier2${suffix}`, {
          principal: { dataLakePrincipalIdentifier: tier2RoleArn },
          resource: { database: { catalogId: this.account, name: dbName } },
          permissions: ['DESCRIBE'],
          permissionsWithGrantOption: [],
        });

        // SELECT + DESCRIBE on all tables in the database (tableWildcard covers
        // the primary table and any future partitions or views).
        new lakeformation.CfnPrincipalPermissions(this, `LfTblTier2${suffix}`, {
          principal: { dataLakePrincipalIdentifier: tier2RoleArn },
          resource: {
            table: { catalogId: this.account, databaseName: dbName, tableWildcard: {} },
          },
          permissions: ['SELECT', 'DESCRIBE'],
          permissionsWithGrantOption: [],
        });
      }

      // ── FI cost component (spec 2026-10-02-fi-cost-stage-b task 4.2) ─────
      //
      // The cost agent reads ADP's two cost views (fleet_cost.vehicle_cost_monthly,
      // vehicle_cost_baseline), fleet_cost_events, and vehicle_pricing. A standard
      // Athena view resolves as the caller, so the views' base tables need grants too
      // (decisions 2026-10-04 "Group 1 findings" item 1): fleet_cost_events and
      // cost_parameters (fleet_cost), daily_usage (fleet_ops, itself a view over
      // energy_usage, granted above), service_records (above) and dimensions.vins.
      //
      // Read-only, no grant option, as above. Two databases by table wildcard: every
      // object in them is a cost product or its parameters. Two by NAMED table only:
      // `dimensions` also holds `customers` (PII), so a wildcard there would grant it;
      // `vehicle_pricing` holds one table, named so a later table needs its own grant.
      // ADP decisions 2026-10-04 item 6 (the AVX Tier 2 role moved to this task).
      const fiCostWildcardDbs = ['fleet_cost', 'fleet_ops'];
      for (const product of fiCostWildcardDbs) {
        const dbName = `adp_${props.stage}_${product}`;
        const suffix = product.split('_').map(p => p.charAt(0).toUpperCase() + p.slice(1)).join('');
        new lakeformation.CfnPrincipalPermissions(this, `LfDbTier2${suffix}`, {
          principal: { dataLakePrincipalIdentifier: tier2RoleArn },
          resource: { database: { catalogId: this.account, name: dbName } },
          permissions: ['DESCRIBE'],
          permissionsWithGrantOption: [],
        });
        new lakeformation.CfnPrincipalPermissions(this, `LfTblTier2${suffix}`, {
          principal: { dataLakePrincipalIdentifier: tier2RoleArn },
          resource: {
            table: { catalogId: this.account, databaseName: dbName, tableWildcard: {} },
          },
          permissions: ['SELECT', 'DESCRIBE'],
          permissionsWithGrantOption: [],
        });
      }
      const fiCostNamedTables: Array<[string, string]> = [
        ['dimensions', 'vins'],
        ['vehicle_pricing', 'vehicle_pricing'],
      ];
      for (const [product, table] of fiCostNamedTables) {
        const dbName = `adp_${props.stage}_${product}`;
        const suffix = product.split('_').map(p => p.charAt(0).toUpperCase() + p.slice(1)).join('');
        new lakeformation.CfnPrincipalPermissions(this, `LfDbTier2${suffix}`, {
          principal: { dataLakePrincipalIdentifier: tier2RoleArn },
          resource: { database: { catalogId: this.account, name: dbName } },
          permissions: ['DESCRIBE'],
          permissionsWithGrantOption: [],
        });
        new lakeformation.CfnPrincipalPermissions(this, `LfTblTier2${suffix}Named`, {
          principal: { dataLakePrincipalIdentifier: tier2RoleArn },
          resource: {
            table: { catalogId: this.account, databaseName: dbName, name: table },
          },
          permissions: ['SELECT', 'DESCRIBE'],
          permissionsWithGrantOption: [],
        });
      }

      // ── Engineering grants the recall run and stage D share (FI stage D 9.3) ──
      //
      // Since stage D 9.1, ADP's recall views read adp_{stage}_engineering: the received
      // view unions recall_campaigns_issued, and recall_vehicle_status joins it and
      // vehicle_build_components for the lot rule. A standard view resolves as the caller, so
      // the live recall run (fiRwLive, below) needs SELECT on both; the recall query also
      // reads recall_campaigns_issued itself, for the lots its evidence names. Stage D's
      // fleet-health reads (fiHealthLive, further below) name the database and
      // vehicle_build_components too.
      //
      // Lake Formation refuses a second grant on the same (principal, resource), and a grant
      // that moves to a new logical id is created before the old one is deleted, so the move
      // collides. So each engineering grant either block can need is made by this one helper,
      // with one logical id, whichever block asks first: flipping either switch never deletes
      // a grant and creates its twin. Read-only, no grant option, named tables only.
      // One-time precondition (security review g93 c1 S3): before 9.3 the fiHealthLive block
      // named two of these LfFiHealthDbEngineering / LfFiHealthTblEngineeringVehicleBuildComponents;
      // moving them to this helper's ids is safe only because FI_FLEET_HEALTH_LIVE = true had
      // never been deployed (live LF read 2026-10-20). Don't rename these ids once deployed.
      const engineeringDbName = `adp_${props.stage}_engineering`;
      const tier2EngineeringGrants = new Set<string>();
      const grantTier2Engineering = (table?: string): void => {
        const key = table ?? '';
        if (tier2EngineeringGrants.has(key)) return;
        tier2EngineeringGrants.add(key);
        if (table === undefined) {
          new lakeformation.CfnPrincipalPermissions(this, 'LfDbTier2Engineering', {
            principal: { dataLakePrincipalIdentifier: tier2RoleArn },
            resource: { database: { catalogId: this.account, name: engineeringDbName } },
            permissions: ['DESCRIBE'],
            permissionsWithGrantOption: [],
          });
          return;
        }
        const tblSuffix = table.split('_').map(p => p.charAt(0).toUpperCase() + p.slice(1)).join('');
        new lakeformation.CfnPrincipalPermissions(this, `LfTblTier2Engineering${tblSuffix}`, {
          principal: { dataLakePrincipalIdentifier: tier2RoleArn },
          resource: {
            table: { catalogId: this.account, databaseName: engineeringDbName, name: table },
          },
          permissions: ['SELECT', 'DESCRIBE'],
          permissionsWithGrantOption: [],
        });
      };
      // The engineering tables the recall run reads: ADP 9.1's two views
      // (platform-foundation/source/data-products/recall_campaigns/views/, ADP 9bb82e96) and
      // agents/fi_recall_warranty/sql/select_recalls.sql. Pinned both ways by
      // agents/tier2/tests/test_fi_rw_infra_pinned.py.
      const fiRwEngineeringTables = ['recall_campaigns_issued', 'vehicle_build_components'];

      // ── task 4.3: fleet_recall LF grants (gated on fiRecallWarrantyLive) ─
      //
      // adp_{stage}_fleet_recall does not exist until ADP task 5.1. A LF grant on a
      // missing database fails the deploy (decisions.md call 15), so the grant is
      // conditional on the flag. IAM (in vsa-tier2-stack.ts, FiRwGlueMetadataRead) is
      // unconditional: IAM does not need the resource to exist.
      // Same default as vsa-tier2-stack.ts: bin/vsa.ts passes nothing, so the shared
      // constant decides for both stacks and task 5.4's one flip enables both (call 55).
      const fiRwLive = props.fiRecallWarrantyLive ?? FI_RECALL_WARRANTY_LIVE;
      if (fiRwLive) {
        const recallDbName = `adp_${props.stage}_fleet_recall`;
        new lakeformation.CfnPrincipalPermissions(this, 'LfDbTier2FleetRecall', {
          principal: { dataLakePrincipalIdentifier: tier2RoleArn },
          resource: { database: { catalogId: this.account, name: recallDbName } },
          permissions: ['DESCRIBE'],
          permissionsWithGrantOption: [],
        });
        new lakeformation.CfnPrincipalPermissions(this, 'LfTblTier2FleetRecall', {
          principal: { dataLakePrincipalIdentifier: tier2RoleArn },
          resource: {
            table: { catalogId: this.account, databaseName: recallDbName, tableWildcard: {} },
          },
          permissions: ['SELECT', 'DESCRIBE'],
          permissionsWithGrantOption: [],
        });
        // The recall views' engineering base tables (FI stage D 9.3, helper above).
        grantTier2Engineering();
        for (const table of fiRwEngineeringTables) grantTier2Engineering(table);
      }

      // ── stage D fleet-health LF grants (gated on fiFleetHealthLive) ───────
      //
      // A LF grant on a missing table fails the deploy (decisions.md call 15), so the
      // grants waited for the flag; it is on since Group 6 rest-plan step 7, after ADP's
      // snapshots job created the last of the named tables.
      //
      // Named tables only — no tableWildcard.  fleet_health exposes views that
      // route through cms_ingest; a wildcard grant there would cover cms_ingest
      // data the Tier 2 role must never read.  engineering and ota_campaigns are
      // also named for the same safety margin.
      // vehicle_telemetry_aggregated omitted: read_telemetry_aggregates is a stub (S1).
      //
      // Logical IDs are prefixed LfFiHealth so they cannot collide with any
      // other grant block in this file (LfDbTier2*, LfTblTier2*, LfDb*, LfTbl*). The two
      // grants this block shares with the recall run (the engineering database and
      // vehicle_build_components) are made by grantTier2Engineering above instead, under its
      // ids LfDbTier2Engineering / LfTblTier2EngineeringVehicleBuildComponents (FI stage D 9.3).
      const fiHealthLive = props.fiFleetHealthLive ?? FI_FLEET_HEALTH_LIVE;
      if (fiHealthLive) {
        // 3 database DESCRIBE grants (engineering through the shared helper)
        const fiHealthDbs = [
          'fleet_health', 'engineering', 'ota_campaigns',
        ];
        for (const db of fiHealthDbs) {
          // The engineering database grant is shared with the recall run: one construct.
          if (db === 'engineering') { grantTier2Engineering(); continue; }
          const dbName = `adp_${props.stage}_${db}`;
          const dbSuffix = db.split('_').map(p => p.charAt(0).toUpperCase() + p.slice(1)).join('');
          new lakeformation.CfnPrincipalPermissions(this, `LfFiHealthDb${dbSuffix}`, {
            principal: { dataLakePrincipalIdentifier: tier2RoleArn },
            resource: { database: { catalogId: this.account, name: dbName } },
            permissions: ['DESCRIBE'],
            permissionsWithGrantOption: [],
          });
        }

        // 11 named-table SELECT + DESCRIBE grants (no tableWildcard; vehicle_build_components
        // through the shared helper)
        const fiHealthNamedTables: Array<[string, string]> = [
          ['fleet_health', 'vehicle_fault_status_daily'],
          ['fleet_health', 'pm_vehicle_status_daily'],
          ['fleet_health', 'vehicle_utilisation_daily'],
          ['fleet_health', 'fault_codes'],
          ['fleet_health', 'snapshot_runs'],
          ['engineering', 'fault_patterns'],
          ['engineering', 'vehicle_build_components'],
          ['engineering', 'engineering_failure_modes'],
          ['engineering', 'engineering_remedy_options'],
          ['ota_campaigns', 'ota_campaigns'],
          ['ota_campaigns', 'ota_campaign_events'],
        ];
        for (const [db, table] of fiHealthNamedTables) {
          // A table the recall run also reads is shared: one construct (helper above).
          if (db === 'engineering' && fiRwEngineeringTables.includes(table)) {
            grantTier2Engineering(table);
            continue;
          }
          const dbName = `adp_${props.stage}_${db}`;
          const dbSuffix = db.split('_').map(p => p.charAt(0).toUpperCase() + p.slice(1)).join('');
          const tblSuffix = table.split('_').map(p => p.charAt(0).toUpperCase() + p.slice(1)).join('');
          new lakeformation.CfnPrincipalPermissions(this, `LfFiHealthTbl${dbSuffix}${tblSuffix}`, {
            principal: { dataLakePrincipalIdentifier: tier2RoleArn },
            resource: {
              table: { catalogId: this.account, databaseName: dbName, name: table },
            },
            permissions: ['SELECT', 'DESCRIBE'],
            permissionsWithGrantOption: [],
          });
        }
      }
    }
  }
}
