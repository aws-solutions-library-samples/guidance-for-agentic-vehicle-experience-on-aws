/**
 * VsaRsaStack — provisions the RSA (Roadside Assistance) dispatch table,
 * DLQ, and stream consumer Lambda.
 *
 * Spec: `.kiro/specs/2026-09-23-cvx-rsa-stub-dispatch/`
 * Tasks: 2.1 (this stack), 2.4 (VsaApiStack wiring that consumes rsaDispatchTable)
 *
 * Table: `vsa-{stage}-rsa-dispatch`
 *   - PK: dispatchId (S)
 *   - Stream: NEW_AND_OLD_IMAGES (spec R7 — old image audits terminal-state enforcement)
 *   - GSI: status-createdAt-index (for listing by status + time)
 *
 * Consumer Lambda: `vsa-{stage}-rsa-dispatch-consumer`
 *   - Triggered by DynamoDB stream on INSERT of REQUESTED rows.
 *   - Calls rsa_client.get_client() → FakeRsaProvider (VSA_RSA_CLIENT=fake).
 *   - Writes DISPATCHED + providerRef + etaMinutes + simulated back to the row.
 *   - Any error → FAILED (never leaves row in REQUESTED — the dead letterbox fix).
 *   - retryAttempts: 3 (finite; default is -1=infinite, which recreates the
 *     dead letterbox one level up — see decisions.md "retryAttempts defaults to
 *     infinite" and docs/tech.md § Trap 1).
 *   - onFailure: SqsDlq — exhausted records land in vsa-{stage}-rsa-dispatch-dlq.
 *
 * Asset path: `infrastructure/build/rsa-dispatch-consumer/` — pre-staged by
 *   `scripts/build-rsa-lambda.sh`, which copies shared/rsa_client*.py into
 *   the bundle so `from shared.rsa_client import ...` resolves at cold start.
 *   Run the script before `cdk synth` or `cdk deploy`.
 *
 * IAM: least-privilege.
 *   Consumer role gets:
 *   - DynamoDB stream read (GetRecords/GetShardIterator/DescribeStream/ListStreams)
 *     on this table's stream ARN only.
 *   - dynamodb:UpdateItem on this table's ARN only (to write DISPATCHED/FAILED).
 *   - dynamodb:GetItem on this table's ARN only (idempotency pre-check).
 *   No Resource: "*". Asserted by the test file.
 *
 * Region-suffix note: DynamoDB table names are account-region scoped.
 * A stage-scoped name (`vsa-${stage}-rsa-dispatch`) is sufficient — region-
 * suffixing is required only for partition-global resources (S3, IAM, CloudFront)
 * per cross-region-namespace.md.
 */

import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cloudwatchActions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as lambdaEventSources from 'aws-cdk-lib/aws-lambda-event-sources';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as scheduler from 'aws-cdk-lib/aws-scheduler';
import * as schedulerTargets from 'aws-cdk-lib/aws-scheduler-targets';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as snsSubscriptions from 'aws-cdk-lib/aws-sns-subscriptions';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import { Construct } from 'constructs';
import * as path from 'path';
import { pythonLambdaAsset } from './python-lambda-asset';
import { ConfigValue } from './config-value';

interface VsaRsaStackProps extends cdk.StackProps {
  stage: string;
  // Fix Group 13 item 2 (escalation spec 2026-09-23-cvx-escalation-recommend-not-act):
  // the `cmsKmsKeyArn` prop that used to live here is gone. The table now
  // gets its own customer-managed key, created in this stack (see
  // `rsaDispatchKey`). `/cvx/{stage}/cms-kms-key-arn` named the AWS-managed
  // `alias/aws/dynamodb` key on staging, so passing it here produced
  // `CUSTOMER_MANAGED` in the template and an AWS-managed key in practice.
  // See issues/2026-09-25-rsa-dispatch-table-on-aws-managed-key/.
  /**
   * Operator email for the DLQ depth alarm.
   *
   * F5.1: The alarm had an SNS topic but zero subscribers, making it the
   * portfolio's third instance of "alarm fires, nobody is notified". To make
   * the subscription concrete rather than a runtime opt-in, pass the email as
   * a `ConfigValue` literal from `bin/vsa.ts`:
   *   `{ literal: process.env.CVX_RSA_ALARM_EMAIL || '',
   *      parameterName: \`/cvx/\${stage}/rsa-alarm-email\` }`.
   *
   * When the literal is a valid email address, an `EmailSubscription` is
   * created at synth time and the SNS confirm-subscription email is sent to
   * that address. The `Makefile` pre-flight guard (`deploy-staging`, `deploy`)
   * requires `CVX_RSA_ALARM_EMAIL` to be either a valid email or the literal
   * `none` — blank is rejected.
   *
   * When the literal is `none` or blank, **no subscription is created** and
   * synth emits a warning. `none` is the explicit opt-out: the operator has
   * recorded the choice and the stack deploys without a subscriber.
   *
   * F6.8: No SSM fallback is implemented. `ConfigValue.parameterName` is
   * unused by this stack — the field is kept only so the shape is consistent
   * with other `ConfigValue` users in the codebase. See decisions-fix6-cdk.md.
   */
  rsaAlarmEmail?: ConfigValue;
  /**
   * Seconds after createdAt before a simulated DISPATCHED row advances to EN_ROUTE.
   *
   * G3-D6 (amended F9.3): timings are plain numeric string literals, NOT
   * ConfigValues — the three `/cvx/{stage}/rsa-sim-*` and
   * `/cvx/{stage}/rsa-requested-stale-*` SSM parameters do not exist.
   * Using resolveConfigValue here would send an empty literal to
   * valueForStringParameter, causing the changeset to fail at deploy.
   *
   * Must satisfy /^[1-9][0-9]*$/. Defaults to '60' when absent.
   */
  rsaSimEnRouteAfterSeconds?: string;
  /**
   * Seconds after createdAt before a simulated EN_ROUTE row advances to ARRIVED.
   *
   * Must be greater than `rsaSimEnRouteAfterSeconds`. Defaults to '300' when absent.
   */
  rsaSimArrivedAfterSeconds?: string;
  /**
   * Seconds after createdAt before a stale REQUESTED row is reconciled to FAILED.
   *
   * Must exceed the consumer's worst-case retry window. Must be ≥ 600.
   * Defaults to '900' when absent.
   */
  rsaRequestedStaleAfterSeconds?: string;
}

export class VsaRsaStack extends cdk.Stack {
  /**
   * The RSA dispatch table — exported to VsaApiStack so the escalate Lambda
   * can write REQUESTED rows and the stack can grant PutItem on the correct ARN.
   */
  readonly rsaDispatchTable: dynamodb.Table;

  /**
   * The table's own customer-managed key (Fix Group 13 item 2). Exported to
   * VsaApiStack so the escalate writer and the status reader are granted on
   * this key and nothing else. With an AWS-managed key, any principal holding
   * a DynamoDB grant on the table could read its rows; with this key, a
   * principal also needs an IAM grant on the key.
   */
  readonly rsaDispatchKey: kms.Key;

  constructor(scope: Construct, id: string, props: VsaRsaStackProps) {
    super(scope, id, props);

    const p = `vsa-${props.stage}`;

    // ─── F9.3: Synth-time validation for lifecycle timing config ─────────
    //
    // These values are plain string literals (not ConfigValues). The three
    // /cvx/{stage}/rsa-sim-* and /cvx/{stage}/rsa-requested-stale-*  SSM
    // parameters do not exist; routing through resolveConfigValue would send
    // an empty literal to valueForStringParameter and fail the changeset.
    //
    // Validation rules (G3-D6 amended):
    //   - must match /^[1-9][0-9]*$/ (positive integer, no leading zero)
    //   - arrived > en_route
    //   - stale >= 600 (floor added in F9.3 so a typo cannot reconcile rows
    //     the consumer is still retrying within its max-retry window)
    const POSITIVE_INT = /^[1-9][0-9]*$/;

    const enRouteRaw = props.rsaSimEnRouteAfterSeconds ?? '60';
    const arrivedRaw = props.rsaSimArrivedAfterSeconds ?? '300';
    const staleRaw   = props.rsaRequestedStaleAfterSeconds ?? '900';

    if (!POSITIVE_INT.test(enRouteRaw)) {
      throw new Error(
        `VsaRsaStack: rsaSimEnRouteAfterSeconds="${enRouteRaw}" is not a positive integer. ` +
        `Pass a numeric string like "60" or omit to use the default.`,
      );
    }
    if (!POSITIVE_INT.test(arrivedRaw)) {
      throw new Error(
        `VsaRsaStack: rsaSimArrivedAfterSeconds="${arrivedRaw}" is not a positive integer. ` +
        `Pass a numeric string like "300" or omit to use the default.`,
      );
    }
    if (!POSITIVE_INT.test(staleRaw)) {
      throw new Error(
        `VsaRsaStack: rsaRequestedStaleAfterSeconds="${staleRaw}" is not a positive integer. ` +
        `Pass a numeric string like "900" or omit to use the default.`,
      );
    }
    const enRoute = parseInt(enRouteRaw, 10);
    const arrived = parseInt(arrivedRaw, 10);
    const stale   = parseInt(staleRaw,   10);
    if (arrived <= enRoute) {
      throw new Error(
        `VsaRsaStack: rsaSimArrivedAfterSeconds (${arrived}) must be greater than ` +
        `rsaSimEnRouteAfterSeconds (${enRoute}). ` +
        `A truck that arrives before it is en-route is nonsensical.`,
      );
    }
    if (stale < 600) {
      throw new Error(
        `VsaRsaStack: rsaRequestedStaleAfterSeconds (${stale}) must be ≥ 600. ` +
        `A value below 600s could reconcile rows the consumer is still retrying ` +
        `(max retry window is a few minutes with 3 retries and bisection).`,
      );
    }

    // ─── Dedicated table key (Fix Group 13 item 2) ───────────────────────
    //
    // Replaces F8.2's reuse of /cvx/{stage}/cms-kms-key-arn. On staging that
    // parameter names the AWS-managed alias/aws/dynamodb key: its key policy
    // lets every principal in the account use it through DynamoDB, so it adds
    // no control beyond the DynamoDB grant. The parameter also feeds four
    // other stacks (core, api, triage-classifier, tier2), so it is left alone.
    //
    // CDK's default key policy delegates to the account's IAM policies. The
    // readers and writers below each get an IAM grant on this key; a
    // principal with a DynamoDB grant on the table but no grant on this key
    // is refused.
    //
    // RETAIN: the table is RETAIN and has PITR. Deleting the key would make
    // the retained table and its backups unreadable.
    //
    // Created in-stack, so a fresh stage needs no seeded input.
    this.rsaDispatchKey = new kms.Key(this, 'RsaDispatchKey', {
      alias: `alias/${p}-rsa-dispatch`,
      description: `Encrypts the ${p}-rsa-dispatch DynamoDB table (rows carry driver PII)`,
      enableKeyRotation: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    const rsaDispatchKey = this.rsaDispatchKey;

    // ─── vsa-{stage}-rsa-dispatch ────────────────────────────────────────
    //
    // Roadside dispatch lifecycle table.
    // PK: dispatchId (S) — server-generated; written by /escalate.
    //
    // Stream type: NEW_AND_OLD_IMAGES
    //   Required for spec R7's terminal-state guard: the consumer inspects
    //   the old image to verify REQUESTED → DISPATCHED is a legal transition
    //   rather than re-processing an already-terminal row.
    //
    // This is the repo's first NEW_AND_OLD_IMAGES stream — three existing
    // streams (vsa-acquire-orders, vsa-core-stack session, vsa-tier2) are
    // all NEW_IMAGE. Verified by grep in docs/tech.md § Trap 2.
    //
    // removalPolicy: RETAIN — matches every other table in the repo.
    // deletionProtection (Fix Group 13 item 2): RETAIN guards `cdk destroy`
    // and stack deletion, not a direct `aws dynamodb delete-table`. This does.
    // billingMode: PAY_PER_REQUEST — matches repo convention.
    this.rsaDispatchTable = new dynamodb.Table(this, 'RsaDispatchTable', {
      tableName: `${p}-rsa-dispatch`,
      partitionKey: { name: 'dispatchId', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      stream: dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      deletionProtection: true,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: rsaDispatchKey,
    });

    // GSI: status-createdAt-index
    //   PK: status (S) — lifecycle state.
    //   SK: createdAt (S, ISO-8601) — time the row was created.
    //   Allows the Group 3.1 status read endpoint and Group 3.2 lifecycle
    //   advancer to query by status efficiently. KEYS_ONLY: callers follow
    //   with GetItem on dispatchId.
    this.rsaDispatchTable.addGlobalSecondaryIndex({
      indexName: 'status-createdAt-index',
      partitionKey: { name: 'status',    type: dynamodb.AttributeType.STRING },
      sortKey:      { name: 'createdAt', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.KEYS_ONLY,
    });

    new cdk.CfnOutput(this, 'RsaDispatchTableName', {
      value: this.rsaDispatchTable.tableName,
      description: 'Name of the vsa-rsa-dispatch DynamoDB table',
      exportName: `${p}-rsa-dispatch-table-name`,
    });

    new cdk.CfnOutput(this, 'RsaDispatchTableArn', {
      value: this.rsaDispatchTable.tableArn,
      description: 'ARN of the vsa-rsa-dispatch DynamoDB table',
      exportName: `${p}-rsa-dispatch-table-arn`,
    });

    // ─── Dead-letter queue ────────────────────────────────────────────────
    //
    // Receives records the consumer has exhausted retries on.
    // Retention: 14 days (DLQ default — long enough for operator investigation).
    //
    // See decisions.md "retryAttempts defaults to -1 = infinite": without this
    // DLQ, exhausted records are silently discarded and the dead letterbox is
    // recreated one level up. The DLQ makes failures visible and queryable.
    const consumerDlq = new sqs.Queue(this, 'RsaDispatchConsumerDlq', {
      queueName: `${p}-rsa-dispatch-dlq`,
      retentionPeriod: cdk.Duration.days(14),
      // Encryption at rest: server-side SQS-managed (SSE-SQS) by default.
    });

    // ─── rsa-dispatch-consumer Lambda ────────────────────────────────────
    //
    // Asset path: infrastructure/build/rsa-dispatch-consumer/
    // Pre-staged by scripts/build-rsa-lambda.sh (Task 2.5).
    //
    // Function name length check:
    //   "vsa-" (4) + "staging" (7) + "-rsa-dispatch-consumer" (22) = 33 < 64 OK.
    const rsaDispatchConsumerFn = new lambda.Function(this, 'RsaDispatchConsumer', {
      functionName: `${p}-rsa-dispatch-consumer`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      timeout: cdk.Duration.seconds(30),
      logRetention: logs.RetentionDays.ONE_MONTH,
      code: pythonLambdaAsset(
        // Build directory pre-assembled by scripts/build-rsa-lambda.sh. Contains
        // handler.py + shared/rsa_client*.py so the lifecycle constants and
        // get_client() resolve at Lambda cold start; a raw lambdas/ asset cannot,
        // because Code.fromAsset bundles one directory and cannot reach a sibling.
        //
        // Routed through pythonLambdaAsset for PYTHON_ASSET_EXCLUDES, same reason as
        // the escalate Lambda in vsa-api-stack.ts. NOTE: the "no bare
        // lambda.Code.fromAsset" guard in test/vsa-api-stack-asset-excludes.test.ts
        // only inspects vsa-api-stack.ts, so it did NOT catch a bare call here —
        // extending that guard to cover every lib/*.ts stack is a filed follow-on.
        path.join(__dirname, '..', 'build', 'rsa-dispatch-consumer'),
      ),
      environment: {
        RSA_DISPATCH_TABLE: this.rsaDispatchTable.tableName,
        // VSA_RSA_CLIENT defaults to "fake" in the source (rsa_client.py).
        // Explicit "fake" here prevents an operator deploy from accidentally
        // pointing at a real provider by omitting the env var.
        VSA_RSA_CLIENT: 'fake',
      },
    });

    // ─── IAM: consumer table access ───────────────────────────────────────
    //
    // Least-privilege: UpdateItem + GetItem on the dispatch table only.
    // No Resource: "*", no dynamodb:*.
    //
    // UpdateItem — writes DISPATCHED or FAILED back to the row.
    // GetItem — idempotency pre-check: reads current status before calling
    //           the provider (prevents duplicate provider calls on redelivery).
    rsaDispatchConsumerFn.addToRolePolicy(new iam.PolicyStatement({
      sid: 'RsaDispatchTableReadWrite',
      effect: iam.Effect.ALLOW,
      actions: [
        'dynamodb:GetItem',
        'dynamodb:UpdateItem',
      ],
      resources: [this.rsaDispatchTable.tableArn],
    }));

    // Fix Group 13 item 2: KMS on the table's own key for GetItem/UpdateItem.
    // Before, the consumer had no explicit KMS grant and worked only because
    // the AWS-managed key's policy allows every principal in the account
    // through DynamoDB. Same actions and ViaService condition as the advancer.
    // (DynamoEventSource's grantStreamRead separately adds kms:Decrypt on this
    // key for stream reads.)
    rsaDispatchConsumerFn.addToRolePolicy(new iam.PolicyStatement({
      sid: 'RsaDispatchConsumerKmsAccess',
      effect: iam.Effect.ALLOW,
      actions: ['kms:Decrypt', 'kms:DescribeKey', 'kms:GenerateDataKey'],
      resources: [rsaDispatchKey.keyArn],
      conditions: {
        StringEquals: {
          'kms:ViaService': `dynamodb.${this.region}.amazonaws.com`,
        },
      },
    }));

    // DynamoDB stream read — required for the event source mapping.
    // Scoped to this table's stream ARN only.
    rsaDispatchConsumerFn.addToRolePolicy(new iam.PolicyStatement({
      sid: 'RsaDispatchStreamRead',
      effect: iam.Effect.ALLOW,
      actions: [
        'dynamodb:GetRecords',
        'dynamodb:GetShardIterator',
        'dynamodb:DescribeStream',
        'dynamodb:ListStreams',
      ],
      resources: [
        `${this.rsaDispatchTable.tableArn}/stream/*`,
      ],
    }));

    // SQS send to the DLQ — needed so Lambda can write failed records.
    consumerDlq.grantSendMessages(rsaDispatchConsumerFn);

    // ─── DynamoDB stream event source ────────────────────────────────────
    //
    // retryAttempts: 3 — REQUIRED. Default is -1 (infinite), which makes the
    //   "exhausted retries → FAILED" branch unreachable and the row stays
    //   REQUESTED — the dead letterbox recreated one layer up. See decisions.md.
    //
    // onFailure: SqsDlq — sends exhausted records to the DLQ above rather than
    //   silently discarding them (the default when onFailure is omitted).
    //
    // startingPosition: TRIM_HORIZON — process all unprocessed records from
    //   the stream start on first deploy. LATEST would miss any rows written
    //   before the consumer was wired up.
    //
    // reportBatchItemFailures: true — per-record failure reporting. Without it,
    //   one bad record fails the entire batch; with it, Lambda retries only
    //   the failed record, not the whole batch.
    rsaDispatchConsumerFn.addEventSource(new lambdaEventSources.DynamoEventSource(
      this.rsaDispatchTable,
      {
        startingPosition: lambda.StartingPosition.TRIM_HORIZON,
        retryAttempts: 3,
        onFailure: new lambdaEventSources.SqsDlq(consumerDlq),
        reportBatchItemFailures: true,
        batchSize: 10,
        bisectBatchOnError: true,
      },
    ));

    // ─── Operator alert topic ─────────────────────────────────────────────
    //
    // F4.4 (review Cycle 4 W2): "a DLQ with no alarm is a silent failure".
    // Both previous Tier 2 agents shipped with empty AlarmActions and one
    // failed silently for two weeks. An operator notification does NOT breach
    // the pull-only boundary (§ D7) — that boundary governs an agent reaching
    // a *customer*, not infrastructure reaching an *operator*.
    //
    // F5.1 (review Cycle 5 W5-3): F4.4 gave the alarm an action but zero
    // subscribers. The stack produced 1 AWS::SNS::Topic and 0
    // AWS::SNS::Subscription. CVX_RSA_ALARM_EMAIL existed only in the stack
    // file and decisions.md — not in the Makefile, with-stage-env.sh, or CI —
    // and the comment promised an SSM fallback that was not implemented.
    //
    // Fix: use a ConfigValue (same pattern as every other per-stage value in
    // this codebase). When the caller passes a non-blank literal, an
    // EmailSubscription is created at synth time. When the literal is blank,
    // a warning is emitted — consistent with the graceful-degradation posture
    // the tier2 operator alert uses. See props.rsaAlarmEmail JSDoc.
    const rsaAlarmTopic = new sns.Topic(this, 'RsaOperatorAlertTopic', {
      topicName: `${p}-rsa-operator-alerts`,
      // No KMS key on this topic. The RSA dispatch table has its own
      // customer-managed key (rsaDispatchKey, Fix Group 13 item 2); the alarm
      // topic remains AWS-managed-key for operational simplicity.
    });

    const rsaAlarmEmail = props.rsaAlarmEmail?.literal?.trim() ?? '';
    if (rsaAlarmEmail.length > 0 && rsaAlarmEmail !== 'none') {
      // F5.1: concrete subscription at synth time so a staging synth contains
      // AWS::SNS::Subscription (tested in vsa-rsa-stack.test.ts).
      //
      // F6.5: `none` is the explicit opt-out — no subscription is created.
      // `none` previously passed the `length > 0` check and synthesized
      // `Endpoint: none`, a phantom subscriber that looks like coverage and
      // provides none. Adding `&& rsaAlarmEmail !== 'none'` closes that gap.
      rsaAlarmTopic.addSubscription(
        new snsSubscriptions.EmailSubscription(rsaAlarmEmail),
      );
    } else {
      // Operator must subscribe manually (or re-deploy with a valid email).
      // Warning rather than throw: the stack is functionally correct without
      // a subscriber — it will fire but notify no human.
      //
      // `none` is an explicit operator opt-out; blank means CVX_RSA_ALARM_EMAIL
      // was not set. Both paths hit this warning so the operator sees the notice
      // in either case.
      cdk.Annotations.of(this).addWarning(
        `CVX_RSA_ALARM_EMAIL is not set or is 'none'. The operator-alert SNS topic ` +
        `(${p}-rsa-operator-alerts) has no email subscriber. ` +
        `DLQ alarms will fire but no human will be notified. ` +
        `Set CVX_RSA_ALARM_EMAIL to a valid email at deploy time to create a subscription: ` +
        `aws sns subscribe --protocol email --topic-arn <arn> --notification-endpoint <email>`,
      );
    }

    // ─── DLQ depth alarm ──────────────────────────────────────────────────
    //
    // Fires when the DLQ has ≥ 1 message (i.e. at least one dispatch record
    // exhausted its 3 retries and was not re-processed). Threshold of 1 is
    // deliberate: every DLQ message represents a stranded dispatch row — a
    // driver whose roadside request may have been silently dropped.
    //
    // Period: 5 minutes — matches the Tier 2 run-failure alarm cadence.
    // treatMissingData: NOT_BREACHING — no data means no DLQ messages, which
    // is the normal healthy state. BREACHING would fire permanently when the
    // queue is empty and CloudWatch has no samples yet.
    //
    // AlarmActions + OKActions both point at rsaAlarmTopic so both the onset
    // and the resolution are visible to the operator. Recovery visibility
    // prevents an "alarm resolved without anyone knowing" incident — the same
    // pattern as vsa-tier2-stack.ts § runFailureAlarm.
    const topicAction = new cloudwatchActions.SnsAction(rsaAlarmTopic);

    const dlqDepthAlarm = new cloudwatch.Alarm(this, 'RsaDispatchDlqDepthAlarm', {
      alarmName: `${p}-rsa-dispatch-dlq-depth`,
      alarmDescription:
        `RSA dispatch DLQ (${p}-rsa-dispatch-dlq) has ≥ 1 message. ` +
        `At least one dispatch record exhausted 3 retries and was not processed. ` +
        `Investigate CloudWatch Logs for the rsa-dispatch-consumer Lambda and ` +
        `manually reconcile affected rows (see spec § F4.4 and Group 3.2 lifecycle advancer).`,
      metric: consumerDlq.metricApproximateNumberOfMessagesVisible({
        period: cdk.Duration.minutes(5),
        statistic: 'Maximum',
      }),
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    dlqDepthAlarm.addAlarmAction(topicAction);
    dlqDepthAlarm.addOkAction(topicAction);

    // ─── Lifecycle advancer Lambda (Task 3.2) ─────────────────────────────
    //
    // Reuses the same build/rsa-dispatch-consumer asset as the stream consumer
    // but with handler `lifecycle.handler` (G3-D1: EventBridge rate(1 minute)).
    //
    // Function name length check:
    //   "vsa-" (4) + "staging" (7) + "-rsa-lifecycle-advancer" (23) = 34 ≤ 64 ✓
    const lifecycleAdvancerFn = new lambda.Function(this, 'RsaLifecycleAdvancer', {
      functionName: `${p}-rsa-lifecycle-advancer`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'lifecycle.handler',
      timeout: cdk.Duration.seconds(30),
      logRetention: logs.RetentionDays.ONE_MONTH,
      code: pythonLambdaAsset(
        // Same build directory as the stream consumer — lifecycle.py is copied
        // alongside handler.py by scripts/build-rsa-lambda.sh's rsync step.
        path.join(__dirname, '..', 'build', 'rsa-dispatch-consumer'),
      ),
      environment: {
        RSA_DISPATCH_TABLE: this.rsaDispatchTable.tableName,
        // Timings — plain numeric string literals (G3-D6 amended by F9.3).
        // These are NOT ConfigValues: the SSM parameters do not exist, and
        // routing through resolveConfigValue with an empty literal would fail
        // the changeset. Synth-time validation above ensures all three are
        // positive integers with arrived > en_route and stale >= 600.
        RSA_SIM_EN_ROUTE_AFTER_SECONDS: enRouteRaw,
        RSA_SIM_ARRIVED_AFTER_SECONDS: arrivedRaw,
        RSA_REQUESTED_STALE_AFTER_SECONDS: staleRaw,
      },
    });

    // ─── IAM: lifecycle advancer table access ─────────────────────────────
    //
    // G3-D2: Query on the GSI ARN (not the table ARN — GSI queries require
    // the GSI ARN). UpdateItem on the table ARN (not the GSI ARN).
    // No dynamodb:Scan (G3-D2 mandates GSI KeyConditionExpression only).
    lifecycleAdvancerFn.addToRolePolicy(new iam.PolicyStatement({
      sid: 'RsaLifecycleAdvancerGsiQuery',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:Query'],
      resources: [
        `${this.rsaDispatchTable.tableArn}/index/status-createdAt-index`,
      ],
    }));

    lifecycleAdvancerFn.addToRolePolicy(new iam.PolicyStatement({
      sid: 'RsaLifecycleAdvancerTableUpdate',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:UpdateItem'],
      resources: [this.rsaDispatchTable.tableArn],
    }));

    // G3-D10: KMS grant for the advancer — the advancer reads rows via Query
    // (GSI returns KEYS_ONLY, so no row decryption at query time) and writes
    // via UpdateItem (requires Encrypt). Explicit grant conditioned on
    // kms:ViaService=dynamodb so the key cannot be used from outside the
    // DynamoDB service boundary. Fix Group 13 item 2: unconditional now, on
    // the table's own key (it was skipped when no CMS key ARN was supplied).
    lifecycleAdvancerFn.addToRolePolicy(new iam.PolicyStatement({
      sid: 'RsaLifecycleAdvancerCmkAccess',
      effect: iam.Effect.ALLOW,
      actions: ['kms:Decrypt', 'kms:DescribeKey', 'kms:GenerateDataKey'],
      resources: [rsaDispatchKey.keyArn],
      conditions: {
        StringEquals: {
          'kms:ViaService': `dynamodb.${this.region}.amazonaws.com`,
        },
      },
    }));

    // ─── EventBridge Scheduler (G3-D1: rate(1 minute)) ───────────────────
    //
    // Scheduler role: separate IAM role so the scheduler.amazonaws.com
    // principal can call lambda:InvokeFunction without conflating it with the
    // Lambda's own execution role.
    //
    // Role name: vsa-{stage}-rsa-sched-invoker-{region}
    // Length check: "vsa-staging-rsa-sched-invoker-ap-southeast-7" = 45 ≤ 64 ✓
    const region = this.region;
    const account = this.account;

    const advancerSchedulerRole = new iam.Role(this, 'RsaAdvancerSchedulerRole', {
      roleName: `vsa-${props.stage}-rsa-sched-invoker-${region}`,
      assumedBy: new iam.ServicePrincipal('scheduler.amazonaws.com', {
        conditions: {
          StringEquals: { 'aws:SourceAccount': account },
        },
      }),
      description: `EventBridge Scheduler invoker role for RSA lifecycle advancer (${props.stage}, ${region})`,
    });

    advancerSchedulerRole.addToPolicy(new iam.PolicyStatement({
      sid: 'InvokeRsaLifecycleAdvancerLambda',
      effect: iam.Effect.ALLOW,
      actions: ['lambda:InvokeFunction'],
      resources: [lifecycleAdvancerFn.functionArn],
    }));

    new scheduler.Schedule(this, 'RsaLifecycleAdvancerSchedule', {
      scheduleName: `vsa-${props.stage}-rsa-lifecycle-advancer-${region}`,
      schedule: scheduler.ScheduleExpression.rate(cdk.Duration.minutes(1)),
      target: new schedulerTargets.LambdaInvoke(lifecycleAdvancerFn, {
        role: advancerSchedulerRole,
        retryAttempts: 0,  // F10.13: Scheduler delivery retries set to 0.
        // The next scheduled tick is the retry. Overlapping retries would re-run
        // the advancer before the prior run completes — harmless (every write is
        // conditional) but wasteful and confusing in metrics.
        maxEventAge: cdk.Duration.minutes(5),
      }),
      description: `Every-minute lifecycle advancer for simulated RSA dispatch (${props.stage})`,
      enabled: true,
    });

    // F10.13: set retryAttempts to 0 on the Lambda async config so Lambda's
    // own async-invocation retry mechanism does not fire either. The next
    // scheduled tick (1 minute later) is the natural retry; overlapping
    // re-invocations only add noise to the Invocations metric that the
    // heartbeat alarm monitors.
    lifecycleAdvancerFn.configureAsyncInvoke({
      retryAttempts: 0,
    });

    // ─── Advancer alarm 1: Lambda Errors ≥ 1 (G3-D11) ───────────────────
    //
    // A failing advancer stalls every simulated lifecycle silently — the same
    // VFO failure mode. 5-minute window, treat-missing-data NOT_BREACHING
    // (no errors is the normal healthy state).
    const advancerErrorAlarm = new cloudwatch.Alarm(this, 'RsaAdvancerErrorAlarm', {
      alarmName: `${p}-rsa-lifecycle-advancer-errors`,
      alarmDescription:
        `RSA lifecycle advancer Lambda (${p}-rsa-lifecycle-advancer) errored in the last 5 minutes. ` +
        `A failing advancer stalls every simulated dispatch lifecycle silently — ` +
        `the VFO failure mode. Investigate CloudWatch Logs.`,
      metric: lifecycleAdvancerFn.metricErrors({
        period: cdk.Duration.minutes(5),
        statistic: 'Sum',
      }),
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    advancerErrorAlarm.addAlarmAction(topicAction);
    advancerErrorAlarm.addOkAction(topicAction);

    // ─── Advancer alarm 2 (heartbeat): Invocations < 1 (F10.7) ─────────
    //
    // Catches "the advancer did not run" — the silent failure class where the
    // scheduler cannot invoke the function (scheduler role lost, schedule
    // disabled, wrong target ARN). In that case the Errors alarm gets no data
    // and stays NOT_BREACHING forever, which is the exact failure mode the
    // prior VFO agent suffered.
    //
    // Invocations Sum < 1 over two 5-minute periods with
    // TreatMissingData: BREACHING — no data means no invocation.
    const advancerHeartbeatAlarm = new cloudwatch.Alarm(this, 'RsaAdvancerHeartbeatAlarm', {
      alarmName: `${p}-rsa-lifecycle-advancer-not-running`,
      alarmDescription:
        `RSA lifecycle advancer Lambda (${p}-rsa-lifecycle-advancer) was not invoked in the last 10 minutes. ` +
        `This covers failures the Errors alarm cannot see: the scheduler role cannot invoke ` +
        `the function, the schedule is disabled, or the schedule targets the wrong ARN.`,
      metric: lifecycleAdvancerFn.metricInvocations({
        period: cdk.Duration.minutes(5),
        statistic: 'Sum',
      }),
      threshold: 1,
      evaluationPeriods: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.BREACHING,
    });
    advancerHeartbeatAlarm.addAlarmAction(topicAction);
    advancerHeartbeatAlarm.addOkAction(topicAction);

    // ─── Advancer alarm 3: Reconciliation count ≥ 1 (G3-D11) ────────────
    //
    // A reconciliation is a roadside request nobody dispatched — each one
    // represents a driver whose request was silently stuck in REQUESTED.
    // Metric source: CloudWatch Logs metric filter on the RECONCILIATION_LOG_TOKEN
    // emitted by lifecycle.py: "[RSA_RECONCILIATION] dispatchId=..."
    //
    // F10.8: the MetricFilter must sit on the advancer's log group (not the
    // consumer's). The alarm is built from reconciliationMetric.metric() so
    // the namespace and metricName come from one constant, not a hand-copied
    // duplicate that can drift.
    const RSA_RECONCILIATION_NAMESPACE = `VSA/${props.stage}/RSA`;
    const RSA_RECONCILIATION_METRIC_NAME = 'ReconciliationCount';

    const reconciliationMetric = new logs.MetricFilter(this, 'RsaReconciliationMetricFilter', {
      logGroup: lifecycleAdvancerFn.logGroup,  // F10.8: advancer's log group, not consumer's
      // F9.2: the term must be quoted ("...") so CloudWatch treats it as an
      // exact literal search rather than space-delimited field syntax.
      filterPattern: logs.FilterPattern.literal('"[RSA_RECONCILIATION]"'),
      metricNamespace: RSA_RECONCILIATION_NAMESPACE,
      metricName: RSA_RECONCILIATION_METRIC_NAME,
      metricValue: '1',
      unit: cloudwatch.Unit.COUNT,
    });

    // F10.8: build the alarm from reconciliationMetric.metric() so namespace
    // and metricName come from the same constant, not a hand-copied duplicate.
    const reconciliationAlarm = new cloudwatch.Alarm(this, 'RsaReconciliationAlarm', {
      alarmName: `${p}-rsa-reconciliation-detected`,
      alarmDescription:
        `RSA lifecycle advancer reconciled ≥ 1 stale REQUESTED row to FAILED in the last 5 minutes. ` +
        `Each reconciliation is a roadside request nobody dispatched — a potential dead letterbox. ` +
        `Investigate the DLQ (${p}-rsa-dispatch-dlq) and CloudWatch Logs for the advancer.`,
      metric: reconciliationMetric.metric({
        period: cdk.Duration.minutes(5),
        statistic: 'Sum',
      }),
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    reconciliationAlarm.addAlarmAction(topicAction);
    reconciliationAlarm.addOkAction(topicAction);
  }
}
