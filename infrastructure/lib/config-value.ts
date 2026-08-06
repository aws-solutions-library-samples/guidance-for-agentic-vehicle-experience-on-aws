/**
 * Per-stage infrastructure coordinates: resolution and shape validation.
 *
 * WHY THIS MODULE EXISTS
 * ----------------------
 * `bin/vsa.ts` used to bake each stage's real KMS key ARNs, Cognito user pool
 * IDs, VPC/SG IDs, Redis endpoint and AgentCore runtime ARN as literals in a
 * `STAGE_DEFAULTS` map. That made the CDK entrypoint — a file a published
 * reference architecture cannot omit, because it is the app entrypoint — carry
 * live account coordinates. Spec
 * `.kiro/specs/2026-08-05-cvx-stage-defaults-parameterization/` replaces them
 * with a three-tier split keyed on *how each value is consumed*:
 *
 *   Tier A — SSM `valueForStringParameter`, resolved by CloudFormation at
 *            deploy time. For values consumed as opaque strings (an IAM policy
 *            resource, a Lambda environment variable). THIS MODULE.
 *   Tier B — env var or `-c` context, no baked default, fail-closed throw at
 *            synth. For values that must be concrete before the template
 *            exists (`Vpc.fromLookup`, an N-element construct fan-out, a
 *            synth-time conditional). Handled by `requireSynthValue` in
 *            `bin/vsa.ts`, which is where the `App` node lives.
 *   Tier C — stage and region strings; still baked, neither sensitive nor
 *            matched by any scanner pattern.
 *
 * WHY TIER A CANNOT BE RESOLVED IN `bin/vsa.ts`
 * ---------------------------------------------
 * `ssm.StringParameter.valueForStringParameter` calls `Stack.of(scope)`
 * internally and throws `"... should be created in the scope of a Stack, but no
 * Stack found"` when handed the `App`. So `bin` passes a `ConfigValue`
 * describing *where* the value comes from, and each consuming stack calls
 * `resolveConfigValue(this, ...)` once in its constructor. Verified against
 * installed `aws-cdk-lib@2.251.0`; see `docs/tech.md` § "SSM and CDK API
 * surface — verified".
 *
 * THE TRAP THIS MODULE EXISTS TO CLOSE
 * ------------------------------------
 * A CFN token is a `string` that is **always truthy and never empty**. Three
 * separate places in this codebase gate behaviour on `if (someArnProp)`, and
 * every one of them becomes unconditionally true the moment the field it tests
 * is fed a token. At `vsa-core-stack.ts` that would silently grant KMS decrypt
 * where no key exists; at `vsa-api-stack.ts` it would silently trust an extra
 * Cognito pool. `isResolvedKmsKeyArn` / `isResolvedUserPoolId` /
 * `isResolvedUserPoolClientId` below exist so those branches gate on *"is this
 * a concrete value of the right shape"* rather than on *"is this truthy"*.
 *
 * Do not reintroduce a truthiness or emptiness test against any of these
 * fields. See `.kiro/specs/2026-08-03-cvx-tier2-platform-health-agent/decisions.md`
 * for the three prior occasions this repository shipped a guard that could not fire.
 */
import { Token } from 'aws-cdk-lib';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

/**
 * The canonical SSM location for a per-stage coordinate.
 *
 * `/cvx/{stage}/{kebab-field}` — chosen to match the existing `vsa-{stage}-*`
 * resource-naming convention and so that an IAM policy scoped to
 * `/cvx/{stage}/*` is expressible.
 *
 * Every field gets a path here, including Tier B fields that are not read from
 * SSM at synth. For Tier B the parameter is the *record* of the canonical value
 * — what the fail-closed error message points an operator at, and what
 * `scripts/seed-stage-parameters.sh` writes. Keeping one convention for all
 * fields means an operator never has to know a field's tier to find its value.
 */
export function cvxParameterName(stage: string, kebabField: string): string {
  return `/cvx/${stage}/${kebabField}`;
}

/**
 * Well-formedness check for a parameter path produced by `cvxParameterName`.
 *
 * This is the *only* thing about a Tier A parameter that can be validated
 * hermetically at synth. Whether the parameter exists is enforced by
 * CloudFormation at deploy — see `resolveConfigValue`.
 */
const PARAMETER_NAME_PATTERN = /^\/cvx\/[a-z0-9][a-z0-9-]*\/[a-z0-9][a-z0-9-]*$/;

/**
 * A Tier A value: either an explicit literal override, or the SSM parameter to
 * read it from.
 *
 * `literal` carries the env-var override so the existing
 * "env var always wins" ergonomics are preserved without `bin` needing a stack
 * scope. When it is absent or blank the parameter is read instead.
 */
export interface ConfigValue {
  /** Operator override (env var). Wins over the SSM parameter when non-blank. */
  readonly literal?: string;
  /** SSM parameter to read when no literal override was supplied. */
  readonly parameterName: string;
}

/**
 * Resolve a Tier A value inside a stack scope.
 *
 * Returns the trimmed literal when one was supplied, otherwise a deploy-time
 * SSM token.
 *
 * ON EXISTENCE CHECKING — read before "improving" this function
 * -------------------------------------------------------------
 * There is deliberately **no synth-time check that the parameter exists**, and
 * adding one would be a regression rather than a hardening:
 *
 *   • A truthiness or emptiness test on the returned value cannot fail. The
 *     token is a non-empty string by construction. That is precisely the
 *     vacuous-guard shape this spec was written to remove — it is why the throws
 *     that used to sit at `bin/vsa.ts:215` and `vsa-core-stack.ts:107` were
 *     deleted rather than moved here.
 *   • Checking existence for real requires an SDK call at synth, i.e.
 *     `valueFromLookup`, which caches into `cdk.context.json` and makes synth
 *     non-hermetic. That reintroduces persisted deploy-affecting context — the
 *     defect class removed by CMS spec
 *     `2026-06-03-cms-clean-deploy-context-isolation`. Spec § Context
 *     constraint 3 rejects it.
 *
 * What actually enforces existence: `valueForStringParameter` emits a
 * `CfnParameter` of type `AWS::SSM::Parameter::Value<String>` whose default is
 * the parameter path. CloudFormation resolves those before provisioning any
 * resource and **fails the stack operation** if the parameter is missing. So the
 * failure is loud and immediate, just at deploy rather than at synth. The
 * accepted cost is that latency difference, recorded in the spec's
 * `decisions.md` Decision 4.
 */
export function resolveConfigValue(
  scope: Construct,
  fieldName: string,
  value: ConfigValue,
): string {
  const literal = value.literal?.trim();
  if (literal) {
    return literal;
  }

  if (!PARAMETER_NAME_PATTERN.test(value.parameterName)) {
    throw new Error(
      `${fieldName}: SSM parameter name "${value.parameterName}" is not of the form ` +
        `/cvx/{stage}/{kebab-field}. This is a wiring bug in bin/vsa.ts, not an ` +
        `operator misconfiguration.`,
    );
  }

  return ssm.StringParameter.valueForStringParameter(scope, value.parameterName);
}

// ─── Shape validation for synth-time conditionals ──────────────────────────
//
// Each predicate answers one question: "is this a concrete value of the
// expected shape, here, now, at synth?" All three return false for an absent
// value, for a CDK token, and for a CloudFormation dynamic reference such as
// a `{{resolve:ssm:...}}` string.
//
// The patterns describe *shape only* and contain no real identifier. A
// validation pattern written in terms of the value it guards would embed that
// value in the published source, which is the mechanism confusion documented
// in `~/.kiro/steering/public-mirror-publish.md` § "The exclusion-mechanism rule".

/** `arn:{partition}:kms:{region}:{account}:key/...` or `:alias/...`. */
const KMS_KEY_ARN_PATTERN = /^arn:aws[a-z0-9-]*:kms:[a-z0-9-]+:\d{12}:(key|alias)\/.+$/;

/** `{region}_{suffix}` — a Cognito user pool ID. */
const USER_POOL_ID_PATTERN = /^[a-z]{2}-[a-z]+-\d_[A-Za-z0-9]+$/;

/**
 * A Cognito app client ID: alphanumeric, 16+ characters.
 *
 * Case-insensitive deliberately. Cognito documents the client ID as `[\w+]+`, and
 * every real value in this account happens to be lowercase — but rejecting a valid
 * uppercase client ID would silently drop the voice-auth trust, which is the exact
 * failure class this module exists to prevent. Flagged by both the general and
 * security reviewers in cycle 1. The 16-character floor is what still excludes the
 * opt-out sentinels and any `{{resolve:...}}` string.
 */
const USER_POOL_CLIENT_ID_PATTERN = /^[A-Za-z0-9]{16,}$/;

function isConcrete(value: string | undefined): value is string {
  return value !== undefined && !Token.isUnresolved(value) && value.trim().length > 0;
}

/**
 * True only for a concrete, correctly shaped KMS key ARN.
 *
 * Gate the AgentCore Memory CMK grant on this, never on `if (props.memoryKmsKeyArn)`.
 */
export function isResolvedKmsKeyArn(value: string | undefined): boolean {
  return isConcrete(value) && KMS_KEY_ARN_PATTERN.test(value.trim());
}

/** True only for a concrete, correctly shaped Cognito user pool ID. */
export function isResolvedUserPoolId(value: string | undefined): boolean {
  return isConcrete(value) && USER_POOL_ID_PATTERN.test(value.trim());
}

/** True only for a concrete, correctly shaped Cognito app client ID. */
export function isResolvedUserPoolClientId(value: string | undefined): boolean {
  return isConcrete(value) && USER_POOL_CLIENT_ID_PATTERN.test(value.trim());
}
