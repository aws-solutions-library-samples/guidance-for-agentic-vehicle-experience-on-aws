import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as scheduler from 'aws-cdk-lib/aws-scheduler';
import * as schedulerTargets from 'aws-cdk-lib/aws-scheduler-targets';
import { Construct } from 'constructs';
import * as path from 'path';
import { ConfigValue, resolveConfigValue } from './config-value';

interface TriageClassifierStackProps extends cdk.StackProps {
  stage: string;
  cmsStage: string;
  /** Tier A `ConfigValue` — env-var override, else `/cvx/{stage}/cms-kms-key-arn`. */
  cmsKmsKeyArn: ConfigValue;
  tenantConfigTable: dynamodb.ITable;
  triageDecisionsTable: dynamodb.ITable;
}

export class TriageClassifierStack extends cdk.Stack {
  readonly classifier: lambda.Function;

  constructor(scope: Construct, id: string, props: TriageClassifierStackProps) {
    super(scope, id, props);

    // Tier A resolution happens here, not in bin/vsa.ts: valueForStringParameter
    // requires a Stack scope. No emptiness guard — the value is a CFN token
    // unless overridden, and a token is never empty.
    const cmsKmsKeyArn = resolveConfigValue(this, 'cmsKmsKeyArn', props.cmsKmsKeyArn);

    const cmsVehiclesTable = `cms-${props.cmsStage}-storage-vehicles`;
    const cmsDtcHistoryTable = `cms-${props.cmsStage}-storage-dtc-history`;

    this.classifier = new lambda.Function(this, 'TriageClassifier', {
      functionName: `vsa-${props.stage}-triage-classifier`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'triage_classifier.handler.handler',
      timeout: cdk.Duration.seconds(10),
      memorySize: 512,
      logRetention: logs.RetentionDays.ONE_MONTH,
      // Pre-bundled by scripts/build-triage-classifier.sh (no Docker required).
      code: lambda.Code.fromAsset(
        path.join(__dirname, '..', 'build', 'triage-classifier'),
      ),
      environment: {
        VSA_TENANT_CONFIG_TABLE: props.tenantConfigTable.tableName,
        VSA_TRIAGE_DECISIONS_TABLE: props.triageDecisionsTable.tableName,
        // Switched from 'fake' to 'real' on 2026-04-30 to back the demo with
        // live CMS data. The real client reads CMS DDB tables directly.
        // Redis is intentionally not configured — the lambda doesn't sit in
        // the CMS VPC, so telemetry reads return stale snapshots and triage
        // relies on DTCs + recalls. This is fine for the P0-via-simulator
        // demo because the simulator produces active DTCs via MaintenanceProcessor.
        VSA_CMS_CLIENT: 'real',
        CMS_VEHICLES_TABLE: cmsVehiclesTable,
        CMS_DTC_HISTORY_TABLE: cmsDtcHistoryTable,
      },
    });

    // Keep one execution environment warm.
    //
    // Measured 2026-08-19: cold invocations take 2510-2600 ms (init 433-477 ms plus
    // ~2100 ms of first-call boto3 + DynamoDB setup). Amazon Nova Sonic's bidi loop
    // assumes a tool returns within ~1 s, so a cold classifier makes the voice
    // assistant stop speaking mid-turn — indistinguishable from a crash to the user.
    // Warm, the same function returns in 2-143 ms, comfortably inside budget.
    //
    // A schedule rather than provisioned concurrency, for two reasons: provisioned
    // concurrency must target a version or alias, and the AgentCore runtime invokes
    // this function by name, so adopting it would require changing the caller as
    // well; and at one warm environment the schedule costs effectively nothing.
    //
    // Known limit, stated rather than hidden: this keeps exactly ONE environment
    // warm. A second concurrent request still pays a cold start. That is acceptable
    // for a single-presenter demo and is NOT sufficient for production traffic —
    // production should either use provisioned concurrency on an alias or take the
    // tool call off the conversational latency path entirely (the client already
    // holds the DTC data it is asking about).
    //
    // 4 minutes: comfortably inside the ~5-15 min idle window before AWS reclaims an
    // environment, without being so frequent that the schedule itself is noise.
    const warmerSchedule = new scheduler.Schedule(this, 'TriageClassifierWarmer', {
      scheduleName: `vsa-${props.stage}-triage-classifier-warmer`,
      schedule: scheduler.ScheduleExpression.rate(cdk.Duration.minutes(4)),
      target: new schedulerTargets.LambdaInvoke(this.classifier, {
        // The handler returns {status: 'warm'} on this payload before reading
        // tenantId/vin and before touching any table, so a ping cannot write a
        // spurious triage decision.
        input: scheduler.ScheduleTargetInput.fromObject({ warmup: true }),
        retryAttempts: 0,
      }),
      description:
        'Invokes the triage classifier every 4 minutes so Nova Sonic never hits a ' +
        'cold start inside its ~1s tool budget. See stack comment for the measured numbers.',
    });
    new cdk.CfnOutput(this, 'TriageClassifierWarmerName', {
      value: warmerSchedule.scheduleName,
      description: 'EventBridge schedule keeping the triage classifier warm',
    });

    // Existing VSA-side grants.
    props.tenantConfigTable.grantReadData(this.classifier);
    props.triageDecisionsTable.grantWriteData(this.classifier);

    // New: read access to CMS vehicles + dtc-history, plus the shared CMS KMS
    // key so DDB server-side decrypt succeeds. Scoped by kms:ViaService so this
    // key grant can't be used outside DDB. Both tables are referenced by ARN
    // since they live in another CDK app — we can't use grantReadData.
    this.classifier.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'CmsRead',
        effect: iam.Effect.ALLOW,
        actions: [
          'dynamodb:GetItem',
          'dynamodb:Query',
          'dynamodb:Scan',
          'dynamodb:BatchGetItem',
        ],
        resources: [
          `arn:aws:dynamodb:${this.region}:${this.account}:table/${cmsVehiclesTable}`,
          `arn:aws:dynamodb:${this.region}:${this.account}:table/${cmsDtcHistoryTable}`,
          `arn:aws:dynamodb:${this.region}:${this.account}:table/${cmsVehiclesTable}/index/*`,
          `arn:aws:dynamodb:${this.region}:${this.account}:table/${cmsDtcHistoryTable}/index/*`,
        ],
      }),
    );
    this.classifier.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'CmsKmsAccessForSseTables',
        effect: iam.Effect.ALLOW,
        actions: ['kms:Decrypt', 'kms:DescribeKey'],
        resources: [cmsKmsKeyArn],
        conditions: {
          StringEquals: {
            'kms:ViaService': `dynamodb.${this.region}.amazonaws.com`,
          },
        },
      }),
    );

    new cdk.CfnOutput(this, 'TriageClassifierArn', { value: this.classifier.functionArn });
    new cdk.CfnOutput(this, 'TriageClassifierName', { value: this.classifier.functionName });
  }
}
