/**
 * Shared IAM statement collector for CDK assertion tests.
 *
 * Spec: .kiro/specs/2026-09-23-cvx-rsa-stub-dispatch/ Fix Group 12 (review Cycle 3,
 * 3.1 W and 3.2 W2) and Fix Group 13 (review Cycle 4). Before this helper, four
 * separate collectors (3.1's `roleStatements`, the advancer's
 * `getAdvancerPolicyStatements`, the consumer's `getAllIamStatements`, and the
 * scheduler role's invoke test) each read only `AWS::IAM::Policy` documents
 * and inline role `Policies`. A role can also get permissions through more
 * channels, and each widening mutation through them left the suites green:
 *
 *   1. An `AWS::IAM::ManagedPolicy` resource whose `Roles` lists the role, or
 *      an `AWS::IAM::RolePolicy` (L1 `CfnRolePolicy`) whose `RoleName` names it.
 *      Their statements are in the template, so they are collected here. A role
 *      named through an intrinsic built from its ARN (`Role.fromRoleArn`) counts.
 *   2. An ARN in the role's `ManagedPolicyArns` (for example an AWS-managed
 *      `AmazonDynamoDBFullAccess`). Its statements are NOT in the template, so
 *      no collector can read them. Tests must pin the ARN list exactly; this
 *      helper returns it separately so that cannot be forgotten.
 *
 * Not covered: the role's trust policy (`AssumeRolePolicyDocument`), which says
 * who can act as the role, not what it can do.
 *
 * Actions are returned verbatim, including `*` and `service:*`. Tests that
 * bound a role compare the full action union, not a prefix-filtered subset,
 * so `Action: "*"` shows up as a difference rather than being skipped.
 */

import { Template } from 'aws-cdk-lib/assertions';

export type IamStatement = Record<string, unknown>;

/** The one managed-policy ARN every Lambda service role in these stacks carries. */
export const LAMBDA_BASIC_EXECUTION_ROLE_ARN = {
  'Fn::Join': [
    '',
    ['arn:', { Ref: 'AWS::Partition' }, ':iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'],
  ],
};

/** The logical ID a `{ Ref }` or `{ Fn::GetAtt: [id, ...] }` names, else null. */
export function logicalIdOf(val: unknown): string | null {
  if (!val || typeof val !== 'object') return null;
  const o = val as Record<string, unknown>;
  if (typeof o['Ref'] === 'string') return o['Ref'] as string;
  const getAtt = o['Fn::GetAtt'];
  if (Array.isArray(getAtt) && typeof getAtt[0] === 'string') return getAtt[0] as string;
  return null;
}

function statementsOfDocument(doc: unknown): IamStatement[] {
  const stmts = (doc as Record<string, unknown> | undefined)?.['Statement'];
  if (Array.isArray(stmts)) return stmts as IamStatement[];
  if (stmts && typeof stmts === 'object') return [stmts as IamStatement];
  return [];
}

function listsRole(props: Record<string, unknown>, roleId: string): boolean {
  const roles = props['Roles'];
  return Array.isArray(roles) && roles.some((r) => refersTo(r, roleId));
}

/**
 * True when `val` names the resource `id`: a direct Ref/GetAtt, or any intrinsic
 * built from one. `Role.fromRoleArn(scope, id, role.roleArn)` attaches policies
 * through `Fn::Select`/`Fn::Split` over the role's `Arn`, which a Ref-only match
 * skips (review Cycle 4 S1).
 */
function refersTo(val: unknown, id: string): boolean {
  if (logicalIdOf(val) === id) return true;
  return val !== null && typeof val === 'object' && JSON.stringify(val).includes(`"${id}"`);
}

/** Logical ID of the execution role of the Lambda whose FunctionName is `functionName`. */
export function roleIdOfFunction(template: Template, functionName: string): string {
  const fns = template.findResources('AWS::Lambda::Function', {
    Properties: { FunctionName: functionName },
  });
  const ids = Object.keys(fns);
  if (ids.length !== 1) {
    throw new Error(`expected exactly one Lambda named ${functionName}, found ${ids.length}`);
  }
  const roleId = logicalIdOf((fns[ids[0]].Properties as Record<string, unknown>)['Role']);
  if (!roleId) throw new Error(`Lambda ${functionName} has no resolvable Role`);
  return roleId;
}

/**
 * Every statement granted to `roleId` through a channel the template can show:
 * `AWS::IAM::Policy` and `AWS::IAM::ManagedPolicy` resources whose `Roles` list
 * it, `AWS::IAM::RolePolicy` resources whose `RoleName` names it, and the role's
 * own inline `Policies`. Throws if the role does not exist, so a renamed role
 * cannot make a bound vacuous.
 */
export function roleStatements(template: Template, roleId: string): IamStatement[] {
  const resources = template.toJSON().Resources as Record<string, Record<string, unknown>>;
  const role = resources[roleId];
  if (!role || role['Type'] !== 'AWS::IAM::Role') {
    throw new Error(`no AWS::IAM::Role with logical ID ${roleId}`);
  }
  const stmts: IamStatement[] = [];
  for (const res of Object.values(resources)) {
    const type = res['Type'];
    const props = (res['Properties'] as Record<string, unknown>) ?? {};
    if (type === 'AWS::IAM::Policy' || type === 'AWS::IAM::ManagedPolicy') {
      if (!listsRole(props, roleId)) continue;
    } else if (type === 'AWS::IAM::RolePolicy') {
      if (!refersTo(props['RoleName'], roleId)) continue;
    } else {
      continue;
    }
    stmts.push(...statementsOfDocument(props['PolicyDocument']));
  }
  const roleProps = (role['Properties'] as Record<string, unknown>) ?? {};
  for (const inline of (roleProps['Policies'] as Record<string, unknown>[]) ?? []) {
    stmts.push(...statementsOfDocument(inline['PolicyDocument']));
  }
  return stmts;
}

/**
 * The role's `ManagedPolicyArns`, verbatim ([] when absent). Their statements
 * are not in the template, so a bound on a role is only complete if the test
 * also pins this list exactly.
 */
export function roleManagedPolicyArns(template: Template, roleId: string): unknown[] {
  const resources = template.toJSON().Resources as Record<string, Record<string, unknown>>;
  const role = resources[roleId];
  if (!role || role['Type'] !== 'AWS::IAM::Role') {
    throw new Error(`no AWS::IAM::Role with logical ID ${roleId}`);
  }
  const arns = ((role['Properties'] as Record<string, unknown>) ?? {})['ManagedPolicyArns'];
  return Array.isArray(arns) ? arns : [];
}

/** Logical IDs of every `AWS::IAM::Role` in the template. */
export function allRoleIds(template: Template): string[] {
  return Object.keys(template.findResources('AWS::IAM::Role'));
}

/**
 * Every statement attached to any role in the template, through every channel
 * `roleStatements` reads. A policy attached to several roles is counted once.
 * Policies attached only to users or groups are included too: none are expected
 * in these stacks, and silently skipping one would hide it.
 */
export function allIamStatements(template: Template): IamStatement[] {
  const resources = template.toJSON().Resources as Record<string, Record<string, unknown>>;
  const stmts: IamStatement[] = [];
  for (const res of Object.values(resources)) {
    const type = res['Type'];
    const props = (res['Properties'] as Record<string, unknown>) ?? {};
    if (type === 'AWS::IAM::Policy' || type === 'AWS::IAM::ManagedPolicy' || type === 'AWS::IAM::RolePolicy') {
      stmts.push(...statementsOfDocument(props['PolicyDocument']));
    } else if (type === 'AWS::IAM::Role') {
      for (const inline of (props['Policies'] as Record<string, unknown>[]) ?? []) {
        stmts.push(...statementsOfDocument(inline['PolicyDocument']));
      }
    }
  }
  return stmts;
}

/**
 * A statement's `Action` as a list ([] when it has none). A `NotAction`
 * statement returns [] here; `malformedForBound` reports it, so callers get an
 * assertion failure instead of a TypeError or a silent skip.
 */
export function actionsOf(stmt: IamStatement): string[] {
  const a = stmt['Action'];
  if (typeof a === 'string') return [a];
  if (Array.isArray(a)) return a as string[];
  return [];
}

/** `stmt`'s `Resource` as a list ([] when it has none). */
export function resourcesOf(stmt: IamStatement): unknown[] {
  const r = stmt['Resource'];
  if (r === undefined) return [];
  return Array.isArray(r) ? r : [r];
}

/** True when an IAM action pattern (`*`, `dynamodb:*`, `dynamodb:Get*`) covers `action`. */
export function actionCovers(pattern: string, action: string): boolean {
  const p = pattern.toLowerCase();
  const a = action.toLowerCase();
  if (!p.includes('*')) return p === a;
  const re = new RegExp('^' + p.split('*').map((s) => s.replace(/[.+?^${}()|[\]\\]/g, '\\$&')).join('.*') + '$');
  return re.test(a);
}

/**
 * Reasons a statement cannot be bounded by listing its actions and resources:
 * not `Allow`, uses `NotAction`/`NotResource`, or has no `Action`. An empty list
 * means the statement is in the shape the exact-union tests assume.
 */
export function malformedForBound(stmt: IamStatement): string[] {
  const reasons: string[] = [];
  if (stmt['Effect'] !== 'Allow') reasons.push(`Effect=${String(stmt['Effect'])}`);
  if ('NotAction' in stmt) reasons.push('NotAction');
  if ('NotResource' in stmt) reasons.push('NotResource');
  if (actionsOf(stmt).length === 0) reasons.push('no Action');
  return reasons;
}
