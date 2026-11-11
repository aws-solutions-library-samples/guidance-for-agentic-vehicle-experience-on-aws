import * as cdk from 'aws-cdk-lib';
import * as bedrockagentcore from 'aws-cdk-lib/aws-bedrockagentcore';
import * as kms from 'aws-cdk-lib/aws-kms';
import { Construct } from 'constructs';

interface AgentCoreMemoryProps {
  /** Deployment stage, e.g. 'staging' or 'prod'. */
  stage: string;
  /** Optional external KMS key ARN. If omitted, a CMK is created in this construct. */
  encryptionKeyArn?: string;
}

/**
 * Creates an AgentCore Memory resource with CVX-managed CMK encryption and
 * the 6 indexed metadata keys required for persona-grounded LTM retrieval.
 *
 * Decision Q2 (decisions.md): CfnMemory native L1 is used; `indexedKeys` is
 * set via addPropertyOverride because CfnMemoryProps does not yet expose the
 * field as a typed prop at aws-cdk-lib 2.251.0.
 *
 * Decision Q5 (decisions.md): CVX-managed CMK, alias `alias/cvx-{stage}-cvx-foundation-memory`.
 */
export class AgentCoreMemoryConstruct extends Construct {
  /** The created KMS CMK for Memory encryption. */
  readonly encryptionKey: kms.Key;
  /** The Memory resource ID (Cfn attribute MemoryId). */
  readonly memoryId: string;
  /** The Memory resource ARN (Cfn attribute MemoryArn). */
  readonly memoryArn: string;

  constructor(scope: Construct, id: string, props: AgentCoreMemoryProps) {
    super(scope, id);

    const account = cdk.Stack.of(this).account;
    const region = cdk.Stack.of(this).region;

    // CVX-managed CMK — alias is stage-scoped and deterministic per
    // ~/.kiro/steering/cross-region-namespace.md (account-wide resource).
    this.encryptionKey = new kms.Key(this, 'MemoryKey', {
      description: `CVX AgentCore Memory CMK — vsa-${props.stage} (${region})`,
      enableKeyRotation: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    new kms.Alias(this, 'MemoryKeyAlias', {
      aliasName: `alias/cvx-${props.stage}-cvx-foundation-memory`,
      targetKey: this.encryptionKey,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    const keyArn = props.encryptionKeyArn ?? this.encryptionKey.keyArn;

    // Native L1 construct; eventExpiryDuration is in DAYS (max 365).
    const memory = new bedrockagentcore.CfnMemory(this, 'Memory', {
      name: `vsa_${props.stage}_cvx_foundation_memory`,
      encryptionKeyArn: keyArn,
      eventExpiryDuration: 90,
    });

    // 6 indexed keys per spec § "AgentCore Memory schema (v1)".
    // Escape hatch required: CfnMemoryProps does not expose indexedKeys (Q2).
    // CFN-template-level property names are PascalCase `Key`/`Type` —
    // see issues/2026-06-16-agentcore-memory-cfn-validation/ for the
    // CFN service-side validation that surfaced this requirement.
    memory.addPropertyOverride('IndexedKeys', [
      { Key: 'persona_id',   Type: 'STRING' },
      { Key: 'vehicle_id',   Type: 'STRING' },
      { Key: 'customer_id',  Type: 'STRING' },
      { Key: 'data_source',  Type: 'STRING' },
      { Key: 'last_updated', Type: 'NUMBER' },
      { Key: 'tenant_id',    Type: 'STRING' },
    ]);

    this.memoryId = memory.attrMemoryId;
    this.memoryArn = memory.attrMemoryArn;

    // Grant the AgentCore service principal (bedrock-agentcore.amazonaws.com)
    // permission to use the CMK for encrypt/decrypt on behalf of this account.
    this.encryptionKey.addToResourcePolicy(
      new cdk.aws_iam.PolicyStatement({
        sid: 'AllowAgentCoreServiceUse',
        effect: cdk.aws_iam.Effect.ALLOW,
        principals: [new cdk.aws_iam.ServicePrincipal('bedrock-agentcore.amazonaws.com')],
        actions: ['kms:Encrypt', 'kms:Decrypt', 'kms:ReEncrypt*', 'kms:GenerateDataKey*', 'kms:DescribeKey'],
        resources: ['*'],
        conditions: {
          StringEquals: { 'aws:SourceAccount': account },
          ArnLike: { 'aws:SourceArn': `arn:aws:bedrock-agentcore:${region}:${account}:*` },
        },
      }),
    );
  }
}
