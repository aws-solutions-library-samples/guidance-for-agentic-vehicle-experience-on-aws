import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { AgentCoreMemoryConstruct } from './agentcore-memory-cr';

interface VsaFoundationStackProps extends cdk.StackProps {
  /** Deployment stage, e.g. 'staging' or 'prod'. */
  stage: string;
}

/**
 * Creates the CVX AgentCore Memory resource and its CMK.
 *
 * Deployed to us-west-2 (colocated with AgentCore Runtime) per spec
 * § "Account / region matrix". Stack name: `vsa-{stage}-foundation`.
 *
 * CFN outputs:
 *   - `cvx-{stage}-memory-id`  — Memory ID for persona-loader config
 *   - `cvx-{stage}-memory-arn` — Memory ARN for IAM scoping
 *   - `cvx-{stage}-memory-kms-key-arn` — KMS key ARN for vsa-core-stack CvxAgentCoreMemoryOps
 *
 * DO NOT instantiate this stack from bin/vsa.ts in this task (Group 2.B).
 * The architect wires it in after 2.B and 2.C both ship to avoid merge conflict.
 */
export class VsaFoundationStack extends cdk.Stack {
  /** Memory construct exposing ID, ARN, and CMK. */
  readonly memory: AgentCoreMemoryConstruct;

  constructor(scope: Construct, id: string, props: VsaFoundationStackProps) {
    super(scope, id, props);

    this.memory = new AgentCoreMemoryConstruct(this, 'AgentCoreMemory', {
      stage: props.stage,
    });

    new cdk.CfnOutput(this, 'MemoryId', {
      value: this.memory.memoryId,
      description: 'AgentCore Memory ID for persona-loader configuration (VSA_FOUNDATION_MEMORY_ID)',
      exportName: `cvx-${props.stage}-memory-id`,
    });

    new cdk.CfnOutput(this, 'MemoryArn', {
      value: this.memory.memoryArn,
      description: 'AgentCore Memory ARN for IAM resource scoping',
      exportName: `cvx-${props.stage}-memory-arn`,
    });

    new cdk.CfnOutput(this, 'MemoryKmsKeyArn', {
      value: this.memory.encryptionKey.keyArn,
      description: 'CMK ARN for AgentCore Memory encryption — referenced by vsa-core-stack CvxAgentCoreMemoryOps',
      exportName: `cvx-${props.stage}-memory-kms-key-arn`,
    });
  }
}
