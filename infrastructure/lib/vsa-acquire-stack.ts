import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as events from 'aws-cdk-lib/aws-events';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as tasks from 'aws-cdk-lib/aws-stepfunctions-tasks';
import { Construct } from 'constructs';
import * as path from 'path';

interface AcquireStackProps extends cdk.StackProps {
  stage: string;
  /**
   * Default wait duration (seconds) between order-progression state-machine
   * steps.  Sourced from env var VSA_ACQUIRE_PROGRESSION_WAIT_SECONDS or
   * defaults to 12 s (the Meridian kiosk demo cadence).  Tune without a
   * code edit by adjusting the env var and re-deploying.
   */
  progressionWaitSeconds?: number;
}

/**
 * AcquireStack — provisions the DynamoDB tables, Lambda functions, Step
 * Functions state machine, and EventBridge bus required by the CVX Acquire
 * (Build & Configure a Vehicle) order path.
 *
 * Tables:
 *  - `vsa-<stage>-acquire-catalog`  — per-tenant catalog (categories, models,
 *    variants, colors, accessories, offers). Schema: `acquire-catalog-schema.md` § 2.
 *  - `vsa-<stage>-acquire-orders`   — reservation records + 10-stage delivery
 *    pipeline state machine. Schema: `acquire-catalog-schema.md` § 3.
 *  - `vsa-<stage>-acquire-leads`    — Discover-phase lead records (mock CRM).
 *    Schema: `acquire-leads-schema.md`. Wired by spec `2026-08-04-cvx-oem-discover-order-meridian`.
 *
 * Lambda quartet (Group 4, spec `2026-07-31-zone2-order-backend-and-kiosk`):
 *  - `reservation_handoff`  — POST /acquire/reservations; starts the SFN execution.
 *  - `order_progressor`     — invoked by SFN for each stage advance; also accepts
 *    EventBridge cron shape for real tenants.
 *  - `payment_initiate`     — POST /acquire/payment-initiate (mock, deterministic).
 *  - `finance_qualify`      — POST /acquire/finance-qualify (mock, deterministic).
 *
 * State machine (D4 — STANDARD, not EXPRESS):
 *  - Alternates Wait + LambdaInvoke of `order_progressor` nine times.
 *  - Wait duration sourced from `progressionWaitSeconds` (default 12 s).
 *  - Express capped at 5 minutes and is at-least-once; Standard runs to a
 *    year and is exactly-once with 90 days of queryable history. D4 chose
 *    Standard so an operator can open the console mid-show and see exactly
 *    which stage a stuck order halted on.
 *
 * Event bus:
 *  - `vsa-<stage>-order-handoff` — fire-and-forget `order.placed` publish.
 *    No subscribers or rules. Feature-flagged on the Lambda side via
 *    VSA_ACQUIRE_PUBLISH_ORDER_PLACED.
 *
 * Region-suffix note: DynamoDB table names are account-region scoped. Lambda
 * function names and SFN state machine names are also account-region scoped.
 * EventBridge bus names are account-region scoped. A stage-scoped name
 * (`vsa-${stage}-acquire-*`) is sufficient — region-suffixing is required
 * only for partition-global resources (S3, IAM, CloudFront) per
 * cross-region-namespace.md.
 */
export class AcquireStack extends cdk.Stack {
  /** Catalog table — PK tenantId (S), SK itemKey (S), GSI by-category. */
  readonly vsaAcquireCatalog: dynamodb.Table;
  /** Orders table — PK orderId (S), two GSIs for tenant+owner and progression-status. */
  readonly vsaAcquireOrders: dynamodb.Table;
  /**
   * Leads table — PK leadId (S), GSI by-tenant-and-contact, GSI by-session.
   * Mock CRM for the Discover phase; written by `lead_capture()` tool.
   * Per spec `2026-08-04-cvx-oem-discover-order-meridian` § Design — `vsa-acquire-leads` DDB table.
   */
  readonly vsaAcquireLeads: dynamodb.Table;

  // ── Lambda exports (for API stack integration) ────────────────────────────
  /** POST /acquire/reservations handler — starts the SFN execution. */
  readonly reservationHandoffFn: lambda.Function;
  /** Per-order stage advancer — invoked by the state machine. */
  readonly orderProgressorFn: lambda.Function;
  /** POST /acquire/payment-initiate — mock deterministic payment capture. */
  readonly paymentInitiateFn: lambda.Function;
  /** POST /acquire/finance-qualify — mock deterministic finance qualification. */
  readonly financeQualifyFn: lambda.Function;

  // ── State machine export (for ARN injection into reservation_handoff) ─────
  readonly orderProgressionSM: sfn.StateMachine;

  // ── Event bus export ──────────────────────────────────────────────────────
  readonly orderHandoffBus: events.EventBus;

  /**
   * CloudFormation export name for the reservation_handoff function ARN.
   * VsaApiStack imports this via Fn.importValue to wire the API Gateway
   * Lambda permission using CfnPermission (L1) with a proper sourceArn —
   * breaking the CDK dependency cycle that would otherwise result from a
   * direct construct cross-reference (see decisions.md Q14).
   */
  readonly reservationHandoffFnArnExportName: string;

  constructor(scope: Construct, id: string, props: AcquireStackProps) {
    super(scope, id, props);

    const p = `vsa-${props.stage}`;
    // Export name for the reservation_handoff function ARN — used by VsaApiStack
    // to wire the API GW Lambda permission via CfnPermission + Fn.importValue,
    // breaking the CDK dependency cycle (decisions.md Q14).
    this.reservationHandoffFnArnExportName = `${p}-reservation-handoff-fn-arn`;

    // ── Cadence config ────────────────────────────────────────────────────
    // Default 12 s per stage for the Meridian kiosk demo. Override via
    // VSA_ACQUIRE_PROGRESSION_WAIT_SECONDS at synth time.
    const progressionWaitSeconds = props.progressionWaitSeconds
      ?? parseInt(process.env.VSA_ACQUIRE_PROGRESSION_WAIT_SECONDS ?? '12', 10);

    // ─── vsa-<stage>-acquire-catalog ──────────────────────────────────
    //
    // Single-table design: itemKey prefixes distinguish CAT#, MODEL#, VAR#,
    // COLOR#, ACC#, ACC-COMPAT#, OFFER# item types. See acquire-catalog-schema.md § 2.
    this.vsaAcquireCatalog = new dynamodb.Table(this, 'AcquireCatalog', {
      tableName: `${p}-acquire-catalog`,
      partitionKey: { name: 'tenantId', type: dynamodb.AttributeType.STRING },
      sortKey:      { name: 'itemKey',  type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // GSI: by-category — "list all models in category X for tenant Y"
    // PK: tenantId, SK: categoryKey (e.g. "CAT#commuter")
    // Projection KEYS_ONLY: query returns itemKeys, then BatchGetItem for full items.
    this.vsaAcquireCatalog.addGlobalSecondaryIndex({
      indexName: 'by-category',
      partitionKey: { name: 'tenantId',    type: dynamodb.AttributeType.STRING },
      sortKey:      { name: 'categoryKey', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.KEYS_ONLY,
    });

    new cdk.CfnOutput(this, 'AcquireCatalogTableName', {
      value: this.vsaAcquireCatalog.tableName,
      description: 'Name of the vsa-acquire-catalog DynamoDB table',
      exportName: `${p}-acquire-catalog-table-name`,
    });

    // ─── vsa-<stage>-acquire-orders ───────────────────────────────────
    //
    // Reservation records + 10-stage delivery-pipeline state machine.
    // PK: orderId (UUID / human-readable order number).
    // DDB Stream NEW_IMAGE enabled — the order_progressor Lambda (Group 4)
    // is invoked per-order by the Step Functions state machine.
    // See acquire-catalog-schema.md § 3 for the full item shape.
    this.vsaAcquireOrders = new dynamodb.Table(this, 'AcquireOrders', {
      tableName: `${p}-acquire-orders`,
      partitionKey: { name: 'orderId', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      stream: dynamodb.StreamViewType.NEW_IMAGE,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // GSI: by-tenant-and-owner — list all orders for a signed-in user.
    // PK: tenantOwnerKey (`<tenantId>#<ownerSub>`), SK: orderPlacedAt (ISO-8601).
    // ALL projection so the iOS OrderTrackerView can build the list without
    // a second round-trip.
    this.vsaAcquireOrders.addGlobalSecondaryIndex({
      indexName: 'by-tenant-and-owner',
      partitionKey: { name: 'tenantOwnerKey', type: dynamodb.AttributeType.STRING },
      sortKey:      { name: 'orderPlacedAt',  type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // GSI: by-progression-status — the time-simulation Lambda (order_progressor)
    // scans for non-terminal orders efficiently.
    // PK: tenantId, SK: nextTransitionAt (ISO-8601; OMITTED on terminal orders
    // so they drop out of the GSI automatically).
    // KEYS_ONLY: the Lambda only needs orderId + tenantId + nextTransitionAt
    // to decide which orders to advance; it then UpdateItem by orderId.
    this.vsaAcquireOrders.addGlobalSecondaryIndex({
      indexName: 'by-progression-status',
      partitionKey: { name: 'tenantId',          type: dynamodb.AttributeType.STRING },
      sortKey:      { name: 'nextTransitionAt',   type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.KEYS_ONLY,
    });

    new cdk.CfnOutput(this, 'AcquireOrdersTableName', {
      value: this.vsaAcquireOrders.tableName,
      description: 'Name of the vsa-acquire-orders DynamoDB table',
      exportName: `${p}-acquire-orders-table-name`,
    });

    // ─── vsa-<stage>-acquire-leads ────────────────────────────────────
    //
    // Discover-phase lead records — the identity-promotion boundary write per
    // the IG framing (spec `2026-08-04-cvx-oem-discover-order-meridian`). Acts as a
    // mock CRM for v1; the seam is designed so a follow-up spec can wire a
    // real OEM CRM.
    //
    // PK:  leadId (S) — generated as `LEAD-YYYYMMDD-<8char>` random suffix,
    //      mirroring the sibling's `ORD-` order-ID pattern.
    this.vsaAcquireLeads = new dynamodb.Table(this, 'AcquireLeads', {
      tableName: `${p}-acquire-leads`,
      partitionKey: { name: 'leadId', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // GSI: by-tenant-and-contact — idempotency check before writing a new lead.
    this.vsaAcquireLeads.addGlobalSecondaryIndex({
      indexName: 'by-tenant-and-contact',
      partitionKey: { name: 'tenantId',     type: dynamodb.AttributeType.STRING },
      sortKey:      { name: 'contactHash',  type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.KEYS_ONLY,
    });

    // GSI: by-session — Discover→Order handoff lookup.
    this.vsaAcquireLeads.addGlobalSecondaryIndex({
      indexName: 'by-session',
      partitionKey: { name: 'sessionId', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.KEYS_ONLY,
    });

    new cdk.CfnOutput(this, 'AcquireLeadsTableName', {
      value: this.vsaAcquireLeads.tableName,
      description: 'Name of the vsa-acquire-leads DynamoDB table (Discover-phase mock CRM)',
      exportName: `${p}-acquire-leads-table-name`,
    });

    // ─────────────────────────────────────────────────────────────────────
    // Lambda quartet — Task: "Deploy the acquire Lambda quartet"
    // (spec `2026-07-31-zone2-order-backend-and-kiosk` Group 4)
    //
    // Function names follow vsa-${stage}-<name> convention.
    // Longest stage in use is "staging" (7 chars). Longest name below is
    // "reservation-handoff" (19 chars). Max = 3+1+7+1+19 = 31 chars < 64.
    //
    // Least-privilege IAM:
    //  - orders: read+write on table ARN + both GSI ARNs
    //  - catalog: read on table ARN + by-category GSI ARN
    //  - leads: read+write on table ARN + both GSI ARNs (for leads handler)
    //  - states:StartExecution: only on the single state machine ARN
    //  - events:PutEvents: only on the single bus ARN
    // ─────────────────────────────────────────────────────────────────────

    // ── Shared IAM policy statements (reused across functions) ───────────

    // Orders table: read + write with UpdateItem — for order_progressor which
    // advances stage transitions using UpdateItem with ConditionExpression.
    // reservation_handoff uses ordersTablePut below (PutItem only).
    const ordersTableReadWrite = new iam.PolicyStatement({
      sid: 'AcquireOrdersReadWrite',
      effect: iam.Effect.ALLOW,
      actions: [
        'dynamodb:GetItem',
        'dynamodb:PutItem',
        'dynamodb:UpdateItem',
        'dynamodb:Query',
      ],
      resources: [
        this.vsaAcquireOrders.tableArn,
        `${this.vsaAcquireOrders.tableArn}/index/by-tenant-and-owner`,
        `${this.vsaAcquireOrders.tableArn}/index/by-progression-status`,
      ],
    });

    // Orders table: PutItem + read — for reservation_handoff which only calls
    // put_item (with ConditionExpression for idempotency) and GetItem/Query.
    // UpdateItem is intentionally OMITTED here: reservation_handoff never calls
    // update_item. Granting it would allow a future code-execution bug in the
    // handler to modify existing order rows (e.g. stamp a different ownerSub),
    // breaking the Fix-Group-1 owner-match guard. (security-review Cycle 6 W1)
    const ordersTablePut = new iam.PolicyStatement({
      sid: 'AcquireOrdersPutRead',
      effect: iam.Effect.ALLOW,
      actions: [
        'dynamodb:GetItem',
        'dynamodb:PutItem',
        'dynamodb:Query',
      ],
      resources: [
        this.vsaAcquireOrders.tableArn,
        `${this.vsaAcquireOrders.tableArn}/index/by-tenant-and-owner`,
        `${this.vsaAcquireOrders.tableArn}/index/by-progression-status`,
      ],
    });

    // Catalog table: read-only (reservation_handoff reads tenantConfig + model rows;
    // order_progressor reads cadence config).
    const catalogTableRead = new iam.PolicyStatement({
      sid: 'AcquireCatalogRead',
      effect: iam.Effect.ALLOW,
      actions: [
        'dynamodb:GetItem',
        'dynamodb:Query',
        'dynamodb:BatchGetItem',
      ],
      resources: [
        this.vsaAcquireCatalog.tableArn,
        `${this.vsaAcquireCatalog.tableArn}/index/by-category`,
      ],
    });

    // ── order_progressor ─────────────────────────────────────────────────
    // Invoked by Step Functions for each stage advance; also accepts the
    // EventBridge cron tenant-scan shape for real tenants.
    this.orderProgressorFn = new lambda.Function(this, 'OrderProgressor', {
      functionName: `${p}-order-progressor`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      timeout: cdk.Duration.seconds(30),
      logRetention: logs.RetentionDays.ONE_MONTH,
      code: lambda.Code.fromAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'acquire', 'order_progressor'),
      ),
      environment: {
        // Table names — env-var driven so multi-stage deploys work without code changes.
        VSA_ACQUIRE_ORDERS_TABLE:   this.vsaAcquireOrders.tableName,
        VSA_ACQUIRE_CATALOG_TABLE:  this.vsaAcquireCatalog.tableName,
        // Comma-separated list of active tenant IDs for the cron/scan path.
        // Empty in kiosk mode — the SFN single-order path does not use this var.
        VSA_ACQUIRE_ACTIVE_TENANTS: '',
      },
    });
    this.orderProgressorFn.addToRolePolicy(ordersTableReadWrite);
    this.orderProgressorFn.addToRolePolicy(catalogTableRead);

    // ── payment_initiate ─────────────────────────────────────────────────
    // Mock deterministic payment capture — no real payment processor.
    // Reads orders for correlation; does NOT write to orders (idempotency
    // is handled by the calling ConfiguratorFlow logic).
    this.paymentInitiateFn = new lambda.Function(this, 'PaymentInitiate', {
      functionName: `${p}-payment-initiate`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      timeout: cdk.Duration.seconds(10),
      logRetention: logs.RetentionDays.ONE_MONTH,
      code: lambda.Code.fromAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'acquire', 'payment_initiate'),
      ),
      environment: {
        VSA_ACQUIRE_ORDERS_TABLE: this.vsaAcquireOrders.tableName,
      },
    });
    // Read-only access to orders for payment-initiate (correlation lookup only).
    this.paymentInitiateFn.addToRolePolicy(new iam.PolicyStatement({
      sid: 'AcquireOrdersReadForPayment',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:GetItem'],
      resources: [this.vsaAcquireOrders.tableArn],
    }));

    // ── finance_qualify ───────────────────────────────────────────────────
    // Mock deterministic captive-finance qualification — no real credit
    // bureau or finance-arm API is called.
    this.financeQualifyFn = new lambda.Function(this, 'FinanceQualify', {
      functionName: `${p}-finance-qualify`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      timeout: cdk.Duration.seconds(10),
      logRetention: logs.RetentionDays.ONE_MONTH,
      code: lambda.Code.fromAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'acquire', 'finance_qualify'),
      ),
      environment: {
        // finance_qualify is stateless; no DDB tables needed. Env var
        // provided for consistency with the other handlers and to allow
        // future reads of tenant config (e.g. rate tiers) without a code change.
        VSA_ACQUIRE_CATALOG_TABLE: this.vsaAcquireCatalog.tableName,
      },
    });
    // finance_qualify is stateless in v1 — no DDB grants needed.
    // Grant catalog read in case a future revision reads rate-tier config.
    this.financeQualifyFn.addToRolePolicy(catalogTableRead);

    // ─────────────────────────────────────────────────────────────────────
    // Per-order progression state machine — Task: "Per-order progression
    // state machine" (spec `2026-07-31-zone2-order-backend-and-kiosk` Group 4)
    //
    // D4 (spec § Decision): STANDARD workflow, not EXPRESS.
    //  - Express caps at 5 minutes and is at-least-once.
    //  - Standard runs to 1 year, is exactly-once, 90-day queryable history.
    //  - 10 stages × 12 s = ~120 s demo runtime — well inside Standard limits.
    //  - Standard's history lets an operator open the console mid-show to see
    //    exactly which stage a stuck order halted on.
    //
    // A synth assertion (tasks.md) checks StateMachineType == 'STANDARD'.
    //
    // Topology: 9 Wait+Invoke pairs (one per non-terminal transition).
    // The machine starts at the first Wait; on each step it waits
    // `progressionWaitSeconds` then invokes order_progressor with the
    // orderId from the execution input.
    // ─────────────────────────────────────────────────────────────────────

    // CloudWatch log group for the state machine.
    const smLogGroup = new logs.LogGroup(this, 'OrderProgressionLogs', {
      logGroupName: `/aws/states/${p}-order-progression`,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Build the linear chain: Wait0 → Invoke0 → Wait1 → Invoke1 → … (× 9)
    // Stage sequence: orderPlaced → paymentReceived → assignedToPlant →
    //   subAssembly → paint → finalAssembly → qualityControl →
    //   shipped → atDealer → readyForDelivery (terminal; 9 transitions).
    //
    // Each Wait+Invoke pair uses unique CDK construct IDs so CDK does not
    // deduplicate them. The Lambda IAM grant is emitted once per LambdaInvoke
    // but CDK deduplicates at the IAM policy level — no duplicate grants.
    const TRANSITION_COUNT = 9;
    const steps: sfn.IChainable[] = [];
    for (let i = 0; i < TRANSITION_COUNT; i++) {
      steps.push(
        new sfn.Wait(this, `Wait${i}`, {
          time: sfn.WaitTime.duration(cdk.Duration.seconds(progressionWaitSeconds)),
          comment: `Wait before stage transition ${i + 1} of ${TRANSITION_COUNT}`,
        }),
      );
      steps.push(
        new tasks.LambdaInvoke(this, `Invoke${i}`, {
          lambdaFunction: this.orderProgressorFn,
          // NO `payload` override. With `payloadResponseOnly: true`, CDK emits
          // `props.payload?.value` directly as the state's `Parameters`
          // (aws-stepfunctions-tasks/lib/lambda/invoke.js `_renderTask`), so
          // `TaskInput.fromJsonPathAt('$')` renders as the LITERAL STRING "$"
          // rather than a JSON-path reference — there is no key to carry the
          // `.$` suffix when the whole payload is the path. The Lambda then
          // receives the str "$" and dies on `event.get("orderId")` with
          // `'str' object has no attribute 'get'`. Omitting `payload` emits no
          // `Parameters` at all, which passes the state input through unchanged.
          //
          // `resultPath: DISCARD` keeps the state equal to the execution input
          // ({"orderId": "..."}) across all 9 invokes. Without it the Lambda's
          // return value ({"advanced":1,...}) REPLACES the state, so Invoke1
          // would receive an object with no `orderId`, fall through to
          // tenant-scan mode, find VSA_ACQUIRE_ACTIVE_TENANTS empty, and return
          // success having advanced nothing — the order would move exactly one
          // stage and then silently stall for the remaining eight. A silent
          // stall on a show floor is worse than a crash.
          //
          // Both failures observed live on staging 2026-08-02; see
          // issues/2026-08-02-order-progression-sfn-payload-and-resultpath/.
          // Asserted by scripts/tests/test_order_progression_asl.py.
          resultPath: sfn.JsonPath.DISCARD,
          // payloadResponseOnly keeps `Resource` as the bare function ARN
          // instead of the `states:::lambda:invoke` envelope. Permitted
          // alongside resultPath — it only conflicts with integrationPattern,
          // invocationType, clientContext, and qualifier.
          payloadResponseOnly: true,
          retryOnServiceExceptions: true,
        }),
      );
    }

    // Chain steps sequentially: Wait0 → Invoke0 → Wait1 → Invoke1 → …
    // Steps alternate between sfn.Wait and tasks.LambdaInvoke, both INextable.
    for (let i = 0; i < steps.length - 1; i++) {
      if (i % 2 === 0) {
        (steps[i] as sfn.Wait).next(steps[i + 1]);
      } else {
        (steps[i] as tasks.LambdaInvoke).next(steps[i + 1]);
      }
    }

    this.orderProgressionSM = new sfn.StateMachine(this, 'OrderProgressionSM', {
      stateMachineName: `${p}-order-progression`,
      // D4: STANDARD — not EXPRESS. Express caps at 5 minutes and is at-least-once.
      stateMachineType: sfn.StateMachineType.STANDARD,
      definitionBody: sfn.DefinitionBody.fromChainable(steps[0] as sfn.IChainable),
      logs: {
        destination: smLogGroup,
        // ALL rather than ERROR so every state transition is auditable
        // (cdk-nag AwsSolutions-SF1). `includeExecutionData` stays FALSE on
        // purpose: the execution payload carries order, lead and customer
        // fields, and turning it on would copy that data into CloudWatch Logs.
        // SF1 only requires level ALL, so there is no need to trade PII
        // exposure for compliance here.
        level: sfn.LogLevel.ALL,
        includeExecutionData: false,
      },
      // X-Ray tracing (cdk-nag AwsSolutions-SF2) — gives an end-to-end view
      // across the progression Lambdas.
      tracingEnabled: true,
    });

    new cdk.CfnOutput(this, 'OrderProgressionSMArn', {
      value: this.orderProgressionSM.stateMachineArn,
      description: 'ARN of the per-order progression Step Functions state machine',
      exportName: `${p}-order-progression-sm-arn`,
    });

    // ─────────────────────────────────────────────────────────────────────
    // Handoff event bus — Task: "Handoff event bus"
    // (spec `2026-07-31-zone2-order-backend-and-kiosk` Group 4)
    //
    // Fire-and-forget `order.placed` publish — PRD option B.
    // No subscribers, no rules to downstream targets in v1.
    // Feature-flagged on the Lambda side via VSA_ACQUIRE_PUBLISH_ORDER_PLACED.
    // ─────────────────────────────────────────────────────────────────────
    this.orderHandoffBus = new events.EventBus(this, 'OrderHandoffBus', {
      eventBusName: `${p}-order-handoff`,
      description: 'order.placed events for Zone 2 downstream integration (fire-and-forget)',
    });

    new cdk.CfnOutput(this, 'OrderHandoffBusArn', {
      value: this.orderHandoffBus.eventBusArn,
      description: 'ARN of the order.placed EventBridge bus',
      exportName: `${p}-order-handoff-bus-arn`,
    });

    // ─────────────────────────────────────────────────────────────────────
    // reservation_handoff — defined last because it depends on both the
    // state machine ARN and the event bus ARN.
    // ─────────────────────────────────────────────────────────────────────
    this.reservationHandoffFn = new lambda.Function(this, 'ReservationHandoff', {
      functionName: `${p}-reservation-handoff`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      timeout: cdk.Duration.seconds(28),
      logRetention: logs.RetentionDays.ONE_MONTH,
      code: lambda.Code.fromAsset(
        path.join(__dirname, '..', '..', 'lambdas', 'acquire', 'reservation_handoff'),
      ),
      environment: {
        VSA_ACQUIRE_ORDERS_TABLE:         this.vsaAcquireOrders.tableName,
        VSA_ACQUIRE_CATALOG_TABLE:        this.vsaAcquireCatalog.tableName,
        // Step Functions ARN injected so the handler can start the progression
        // machine after a successful DDB write (spec Group 2 Task 1 (g)).
        VSA_ACQUIRE_PROGRESSION_SFN_ARN:  this.orderProgressionSM.stateMachineArn,
        // EventBridge bus ARN; gated by VSA_ACQUIRE_PUBLISH_ORDER_PLACED.
        VSA_ACQUIRE_EVENT_BUS_ARN:        this.orderHandoffBus.eventBusArn,
        // Feature flag — defaults OFF; operator enables for production show.
        VSA_ACQUIRE_PUBLISH_ORDER_PLACED: 'false',
        // Comma-separated active tenants (used by cron/scan path; not needed
        // for reservation_handoff itself, but kept for consistency).
        VSA_ACQUIRE_ACTIVE_TENANTS:       '',
      },
    });

    // Orders: PutItem + read ONLY (no UpdateItem — reservation_handoff only
    // calls put_item; ordersTablePut omits UpdateItem per security-review Cycle 6 W1).
    this.reservationHandoffFn.addToRolePolicy(ordersTablePut);
    // Catalog: read (tenantConfig + model row for derivations).
    this.reservationHandoffFn.addToRolePolicy(catalogTableRead);

    // states:StartExecution — scoped to this single state machine only.
    this.reservationHandoffFn.addToRolePolicy(new iam.PolicyStatement({
      sid: 'StartOrderProgressionExecution',
      effect: iam.Effect.ALLOW,
      actions: ['states:StartExecution'],
      resources: [this.orderProgressionSM.stateMachineArn],
    }));

    // events:PutEvents — scoped to this single bus only.
    this.orderHandoffBus.grantPutEventsTo(this.reservationHandoffFn);

    // Export the reservation-handoff function ARN so VsaApiStack can wire
    // the API Gateway Lambda permission using Fn.importValue rather than a
    // direct construct reference. This breaks the CDK dependency cycle:
    //   (old) VsaApiStack → AcquireStack.fn.Arn (construct ref)
    //       + AcquireStack → VsaApiStack.RestApi.Ref (for sourceArn) = CYCLE
    //   (new) VsaApiStack reads the ARN via Fn::ImportValue — CDK sees no
    //       construct-level cross-stack dependency from VsaApiStack to
    //       AcquireStack via this export, so the sourceArn Ref is safe.
    // The CFN-level deploy order (AcquireStack before VsaApiStack) is
    // preserved by the explicit export/import relationship.
    // See decisions.md Q14 for the full rationale.
    new cdk.CfnOutput(this, 'ReservationHandoffFnArn', {
      value: this.reservationHandoffFn.functionArn,
      exportName: this.reservationHandoffFnArnExportName,
      description:
        'ARN of the reservation_handoff Lambda — imported by VsaApiStack to wire the ' +
        'API Gateway Lambda permission with sourceArn (breaks CDK dependency cycle).',
    });
  }
}
