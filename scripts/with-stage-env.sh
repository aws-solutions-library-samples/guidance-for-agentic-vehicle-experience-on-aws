#!/usr/bin/env bash
# with-stage-env.sh — run a command with a stage's Tier B values exported from SSM.
#
# WHY THIS EXISTS
# ---------------
# Spec `.kiro/specs/2026-08-05-cvx-stage-defaults-parameterization/` removed the
# per-stage infrastructure coordinates from `infrastructure/bin/vsa.ts`. The three
# Tier A values are read by CloudFormation itself, so nothing extra is needed for
# them. The six Tier B values must be *concrete at synth time* — `Vpc.fromLookup`
# rejects tokens, a pool list becomes N constructs, and three IAM/trust decisions
# are made by synth-time conditionals — so they arrive as environment variables.
#
# Without this wrapper every deploy would require an operator to export six
# variables by hand, and `make deploy-staging` would simply fail. That is a worse
# outcome than the baked defaults it replaced: spec § Context constraint 4 records
# that the defaults existed precisely so an operator could not forget the Cognito
# trust and silently 401 the CMS web UI.
#
# So the deploy path reads the canonical values from the parameters that
# `scripts/seed-stage-parameters.sh` created. Same ergonomics as the baked defaults
# — the operator sets nothing — with the values out of the published source.
#
# WHY THIS DOES NOT REINTRODUCE THE `valueFromLookup` PROBLEM
# ----------------------------------------------------------
# Spec § Context constraint 3 rejects `ssm.StringParameter.valueFromLookup` because
# it caches into `cdk.context.json`, making synth non-hermetic and letting a value
# fetched for one environment silently persist into another — the defect class
# removed by CMS spec `2026-06-03-cms-clean-deploy-context-isolation`.
#
# This is a different mechanism with none of that behaviour:
#   • the read happens BEFORE cdk starts, in this process, not inside synth
#   • nothing is written to disk, so there is no persisted context to drift
#   • the values reach the app as ordinary environment variables, exactly as a
#     hand-export would, so synth itself stays hermetic and reproducible
#   • an explicitly exported variable still wins, so overrides keep working
#
# VALUES ARE NEVER PRINTED
# ------------------------
# They are exported into this process and the command is exec'd. Nothing is written
# to stdout, so no value can land in a terminal scrollback, a CI log, or a Make
# echo. This is why the script execs a command rather than printing `export` lines
# for the caller to `eval` — the latter would put every value on stdout.
#
# A MISSING PARAMETER IS NOT AN ERROR HERE
# ----------------------------------------
# It is left unset on purpose, so `requireSynthValue` in `bin/vsa.ts` produces the
# canonical failure — which names the field, the environment variable, the parameter
# path and the `none` opt-out. Duplicating that message here would mean maintaining
# two versions of it.
#
# Usage:
#   bash scripts/with-stage-env.sh <stage> <region> -- <command> [args...]
#   bash scripts/with-stage-env.sh staging us-west-2 -- npx cdk synth vsa-staging-api
set -euo pipefail

STAGE="${1:?usage: with-stage-env.sh <stage> <region> -- <command> [args...]}"
REGION="${2:?usage: with-stage-env.sh <stage> <region> -- <command> [args...]}"
shift 2
[ "${1:-}" = "--" ] && shift
[ $# -gt 0 ] || { printf 'with-stage-env.sh: no command given\n' >&2; exit 2; }

# Tier B only. Tier A (cms-kms-key-arn, redis-endpoint,
# assistant-chat-runtime-arn) is resolved by CloudFormation from the same
# parameters at deploy time and must NOT be pre-resolved here — doing so would
# bypass the mechanism and hide a missing parameter until later.
TIER_B="\
VSA_MEMORY_KMS_KEY_ARN:memory-kms-key-arn
CMS_VPC_ID:vpc-id
CMS_REDIS_SG_ID:redis-security-group-id
VOICE_AUTH_USER_POOL_ID:voice-auth-user-pool-id
VOICE_AUTH_USER_POOL_CLIENT_ID:voice-auth-user-pool-client-id
EXTRA_USER_POOL_IDS:extra-cognito-user-pool-ids"

resolved=0 preset=0 missing=0

# `|| [ -n "$envvar" ]` keeps the final entry when the input has no trailing newline.
# This matters more than it looks: `extraCognitoUserPoolIds` is the LAST line above, and
# a silently dropped final line would leave the Cognito trust unexported — producing the
# same 401 as omitting it outright, by a different route. Flagged by security review
# cycle 1 (Suggestion 3). Verify by observing the wrapper report 6 of 6, not by reading
# this loop.
while IFS=':' read -r envvar suffix || [ -n "$envvar" ]; do
  [ -n "$envvar" ] || continue

  # An operator's explicit export always wins, unchanged from before this spec.
  if [ -n "${!envvar:-}" ]; then
    preset=$((preset + 1))
    continue
  fi

  name="/cvx/$STAGE/$suffix"
  if value="$(aws ssm get-parameter --name "$name" --region "$REGION" \
                --query 'Parameter.Value' --output text 2>/dev/null)" \
     && [ -n "$value" ] && [ "$value" != "None" ]; then
    export "$envvar=$value"
    resolved=$((resolved + 1))
  else
    printf '\033[0;33m[warn]\033[0m %s not found — leaving $%s unset so the synth-time guard reports it\n' \
      "$name" "$envvar" >&2
    missing=$((missing + 1))
  fi
done <<<"$TIER_B"

printf '\033[0;34m[info]\033[0m stage=%s region=%s tier-B: %d from SSM, %d already exported, %d missing\n' \
  "$STAGE" "$REGION" "$resolved" "$preset" "$missing" >&2

exec "$@"
