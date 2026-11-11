/**
 * ApnsPlatformApp — CDK custom-resource construct for the SNS APNs SANDBOX
 * platform application.
 *
 * Creates (or updates) the SNS APNS_SANDBOX platform application when the
 * Secrets Manager secret `cvx/{stage}/apns-auth-key` exists, and returns its
 * ARN. When the secret is absent the construct deploys cleanly and returns an
 * empty string — callers should treat an empty ARN as "push disabled".
 *
 * The secret VALUE is never read at synth time or in this file. The onEvent
 * Lambda reads it at CloudFormation event time only.
 *
 * SNS attribute name references (T1.3a-1 spec research):
 *   PlatformCredential    — .p8 signing key content
 *   PlatformPrincipal     — signing key ID
 *   ApplePlatformTeamID   — Apple Developer account Team ID
 *   ApplePlatformBundleID — iOS application bundle identifier
 *   (Source: AWS SNS CreatePlatformApplication API, 2026-11-05)
 */

import * as cdk from 'aws-cdk-lib';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as path from 'path';
import { Construct } from 'constructs';
import { pythonLambdaAsset } from './python-lambda-asset';

export interface ApnsPlatformAppProps {
  /** Deployment stage, e.g. 'staging'. */
  stage: string;
  /**
   * AWSCURRENT version ID of the APNs auth-key secret (from `describe-secret`
   * metadata, never from `get-secret-value`). Set to `'none'` when the secret
   * is absent. Changing this value triggers a re-run of the custom resource
   * (e.g. after the operator rotates the key via `apns-key-to-secret.py`).
   *
   * Source: `scripts/with-stage-env.sh` exports `CVX_APNS_KEY_VERSION`, read
   * by `bin/vsa.ts` as `apnsKeyVersion`.
   */
  keyVersion: string;
}

/**
 * CDK custom-resource construct wrapping the `lambdas/apns-platform-app`
 * Lambda. Provides a `platformAppArn` attribute that can be threaded into
 * the register-device Lambda's `AVX_APNS_PLATFORM_APP_ARN` environment
 * variable and the broker's `SnsCreateApnsEndpoint` IAM grant.
 */
export class ApnsPlatformApp extends Construct {
  /**
   * CloudFormation `Fn::GetAtt(PlatformApplicationArn)` from the custom
   * resource. Resolves to an SNS application ARN when the APNs auth-key
   * secret exists, or to an empty string when it is absent.
   */
  readonly platformAppArn: string;

  constructor(scope: Construct, id: string, props: ApnsPlatformAppProps) {
    super(scope, id);

    const stack = cdk.Stack.of(this);
    const secretId = `cvx/${props.stage}/apns-auth-key`;
    const applicationName = `vsa-${props.stage}-avx-apns-sandbox`;

    // onEvent Lambda: wraps lambdas/apns-platform-app/handler.py :: on_event.
    // Key bytes (p8) are never logged by the handler.
    const onEventFn = new lambda.Function(this, 'Fn', {
      functionName: `vsa-${props.stage}-apns-platform-app-cr`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.on_event',
      timeout: cdk.Duration.seconds(30),
      logRetention: logs.RetentionDays.ONE_MONTH,
      // Source tree, not a build directory: no _lib dependency, single file Lambda.
      code: pythonLambdaAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'apns-platform-app'),
      ),
    });

    // ── IAM grants on the onEvent Lambda ─────────────────────────────────

    // Secrets Manager: read only the APNs auth-key secret.
    // The 6-char suffix pattern matches the random suffix Secrets Manager
    // appends to every secret's ARN.
    onEventFn.addToRolePolicy(new iam.PolicyStatement({
      sid: 'ApnsSecretRead',
      effect: iam.Effect.ALLOW,
      actions: ['secretsmanager:GetSecretValue'],
      resources: [
        `arn:aws:secretsmanager:${stack.region}:${stack.account}:secret:${secretId}-*`,
      ],
    }));

    // SNS list + manage: these four actions have NO resource type in the Service
    // Authorization Reference for Amazon SNS
    // (https://docs.aws.amazon.com/service-authorization/latest/reference/list_sns.html,
    // checked 2026-11-06: the only SNS resource type is `topic`; ListPlatformApplications,
    // CreatePlatformApplication, SetPlatformApplicationAttributes and
    // DeletePlatformApplication list none and no condition keys). IAM evaluates such
    // actions against `*`, so a statement naming the application ARN never matches and the
    // custom resource's Create would be denied. `Resource: "*"` is the narrowest form IAM
    // evaluates; the handler only ever touches the application named `applicationName`
    // (found by name via ListPlatformApplications). No `iam:PassRole` is granted: the
    // handler sets no feedback-role attributes, which is what that dependent action covers.
    // Spec 2026-11-05-avx-proactive-p0-alert F1.6 (review cycle 1 W6).
    onEventFn.addToRolePolicy(new iam.PolicyStatement({
      sid: 'SnsListPlatformApps',
      effect: iam.Effect.ALLOW,
      actions: ['sns:ListPlatformApplications'],
      resources: ['*'],
    }));

    onEventFn.addToRolePolicy(new iam.PolicyStatement({
      sid: 'SnsManagePlatformApp',
      effect: iam.Effect.ALLOW,
      actions: [
        'sns:CreatePlatformApplication',
        'sns:SetPlatformApplicationAttributes',
        'sns:DeletePlatformApplication',
      ],
      resources: ['*'],
    }));

    // ── Custom resource Provider + resource ────────────────────────────────

    const provider = new cr.Provider(this, 'Provider', {
      onEventHandler: onEventFn,
    });

    const resource = new cdk.CustomResource(this, 'Resource', {
      serviceToken: provider.serviceToken,
      properties: {
        SecretId: secretId,
        ApplicationName: applicationName,
        KeyVersion: props.keyVersion,
      },
    });

    this.platformAppArn = resource.getAttString('PlatformApplicationArn');
  }
}
