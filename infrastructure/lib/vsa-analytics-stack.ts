import * as cdk from 'aws-cdk-lib';
import * as athena from 'aws-cdk-lib/aws-athena';
import * as lakeformation from 'aws-cdk-lib/aws-lakeformation';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';

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
    }
  }
}
