/**
 * VSA Findings Export Stack (spec §B A1, task A1).
 *
 * Provisions exactly one S3 bucket that the operator uses as the destination
 * for an `ExportTableToPointInTime` call before running the one-time purge of
 * expired informational rows.  No IAM grants are created here: the operator
 * supplies their own credentials when they invoke the export API.
 *
 * Bucket naming follows the cross-region-namespace discipline
 * (cross-region-namespace.md §1 resource-name region-suffix check):
 *   vsa-{stage}-avx-findings-export-{region}-{account}
 *
 * Length budget (S3 bucket name limit: 63 chars):
 *   "vsa-staging-avx-findings-export-ap-southeast-7-123456789012" = 59 chars ≤ 63 ✓
 *   (longest commercial AWS region ap-southeast-7 = 14 chars, verified 2026-06-04)
 */
import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';

// ─── Props ─────────────────────────────────────────────────────────────────

export interface VsaFindingsExportStackProps extends cdk.StackProps {
  /** VSA stage: 'staging' | 'prod'. */
  stage: string;
}

// ─── Stack ──────────────────────────────────────────────────────────────────

export class VsaFindingsExportStack extends cdk.Stack {
  /** S3 bucket for the one-time findings table export (PITR → S3). */
  readonly findingsExportBucket: s3.Bucket;

  constructor(scope: Construct, id: string, props: VsaFindingsExportStackProps) {
    super(scope, id, props);

    const { stage } = props;
    const region  = this.region;
    const account = this.account;

    // Bucket: vsa-{stage}-avx-findings-export-{region}-{account}
    // All four BlockPublicAccess booleans true (BLOCK_ALL).
    // SSE-S3 (S3_MANAGED) — the export API supports AES256; customer-managed
    // KMS is not needed here because this bucket holds a one-time snapshot that
    // is deleted after the purge is confirmed, and the operator controls access
    // via IAM.  enforceSSL: true adds the Deny:aws:SecureTransport=false policy.
    this.findingsExportBucket = new s3.Bucket(this, 'FindingsExport', {
      bucketName:        `vsa-${stage}-avx-findings-export-${region}-${account}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption:        s3.BucketEncryption.S3_MANAGED,
      enforceSSL:        true,
      versioned:         true,
      removalPolicy:     cdk.RemovalPolicy.RETAIN,
    });

    new cdk.CfnOutput(this, 'FindingsExportBucketName', {
      value: this.findingsExportBucket.bucketName,
    });
  }
}
