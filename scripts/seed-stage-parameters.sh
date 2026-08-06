#!/usr/bin/env bash
# seed-stage-parameters.sh — create the per-stage SSM parameters that
# `infrastructure/bin/vsa.ts` now reads instead of carrying as baked literals.
#
# WHY THIS EXISTS
# ---------------
# Spec `.kiro/specs/2026-08-05-cvx-stage-defaults-parameterization/` removed the
# real KMS key ARNs, Cognito pool IDs, VPC/SG IDs, Redis endpoint and AgentCore
# runtime ARN from `STAGE_DEFAULTS`. Those values still have to come from
# somewhere, and until the parameters exist:
#
#   • a Tier A deploy fails in CloudFormation with "SSM Parameter ... not found"
#   • a Tier B synth fails with the fail-closed throw from `requireSynthValue`
#
# So this must run BEFORE the change reaches a stage's deploy path.
#
# WHERE THE VALUES COME FROM
# --------------------------
# The git ref that still has them baked, `--from-ref` (default `HEAD`). Once the
# parameterization commit lands, pass the commit before it. Nothing is invented and
# nothing is prompted for — if a value is not in that ref, this reports it as an
# operator-supplied gap rather than guessing.
#
# KNOWN GAP: prod's `cmsKmsKeyArn` was deliberately never baked (it came from
# `.env` locally / CI secrets), so it cannot be recovered from git. This script
# reports it and exits non-zero for prod unless `CMS_KMS_KEY_ARN` is exported.
#
# VALUES ARE NEVER PRINTED
# ------------------------
# Not to stdout, not to a log, not on failure. Every line of output names a
# parameter and a disposition. `--verify` lists names only, per the spec's Group 3
# constraint. That is deliberate: this script's whole purpose is getting these
# strings out of places they can be read from, and a script that echoes them while
# doing so would be self-defeating.
#
# WHY `String` AND NOT `SecureString`
# -----------------------------------
# CloudFormation's `AWS::SSM::Parameter::Value<String>` parameter type — which is
# what `valueForStringParameter` emits, and the whole mechanism Tier A depends on —
# does not support `SecureString`. So Tier A parameters must be `String`, and the
# Tier B ones are kept `String` for consistency. Consequence to be explicit about:
# any principal with `ssm:GetParameter` on `/cvx/*` can read these. Scope that
# permission to `/cvx/{stage}/*` and grant it only to deploy principals. These are
# infrastructure coordinates, not credentials — but they are exactly the coordinates
# this spec just spent effort removing from a publishable file.
#
# Usage:
#   bash scripts/seed-stage-parameters.sh --stage staging            # dry run (default)
#   bash scripts/seed-stage-parameters.sh --stage staging --apply
#   bash scripts/seed-stage-parameters.sh --stage prod --verify      # names only
#   bash scripts/seed-stage-parameters.sh --stage staging --from-ref abc1234 --apply
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

STAGE=""
FROM_REF="HEAD"
MODE="dry-run"
REGION_OVERRIDE=""

info() { printf '\033[0;34m[info]\033[0m %s\n' "$*"; }
ok()   { printf '\033[0;32m[ok]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[warn]\033[0m %s\n' "$*"; }
err()  { printf '\033[0;31m[fail]\033[0m %s\n' "$*" >&2; }

usage() { sed -n '1,55p' "${BASH_SOURCE[0]}"; exit 0; }

while [ $# -gt 0 ]; do
  case "$1" in
    --stage)    STAGE="${2:?--stage needs a value}"; shift 2 ;;
    --from-ref) FROM_REF="${2:?--from-ref needs a value}"; shift 2 ;;
    --region)   REGION_OVERRIDE="${2:?--region needs a value}"; shift 2 ;;
    --apply)    MODE="apply"; shift ;;
    --verify)   MODE="verify"; shift ;;
    --dry-run)  MODE="dry-run"; shift ;;
    -h|--help)  usage ;;
    *) err "unknown argument: $1"; exit 2 ;;
  esac
done

[ -n "$STAGE" ] || { err "--stage is required (staging | prod)"; exit 2; }

# Region only affects WHERE the parameters live. SSM is regional, and each stage's
# stacks read them from the stage's own primary region.
case "$STAGE" in
  staging) REGION="${REGION_OVERRIDE:-us-west-2}" ;;
  prod)    REGION="${REGION_OVERRIDE:-us-east-1}" ;;
  *)       REGION="${REGION_OVERRIDE:-}"
           [ -n "$REGION" ] || { err "stage '$STAGE' has no default region — pass --region"; exit 2; } ;;
esac

# ── The field → parameter mapping. Must match bin/vsa.ts exactly. ─────────────
# field-name : kebab-parameter-suffix : env-var-override : tier
FIELDS=(
  "cmsKmsKeyArn:cms-kms-key-arn:CMS_KMS_KEY_ARN:A"
  "redisEndpoint:redis-endpoint:REDIS_ENDPOINT:A"
  "assistantChatRuntimeArn:assistant-chat-runtime-arn:CVX_RUNTIME_ARN:A"
  "memoryKmsKeyArn:memory-kms-key-arn:VSA_MEMORY_KMS_KEY_ARN:B"
  "vpcId:vpc-id:CMS_VPC_ID:B"
  "redisSecurityGroupId:redis-security-group-id:CMS_REDIS_SG_ID:B"
  "voiceAuthUserPoolId:voice-auth-user-pool-id:VOICE_AUTH_USER_POOL_ID:B"
  "voiceAuthUserPoolClientId:voice-auth-user-pool-client-id:VOICE_AUTH_USER_POOL_CLIENT_ID:B"
  "extraCognitoUserPoolIds:extra-cognito-user-pool-ids:EXTRA_USER_POOL_IDS:B"
)
# Tier B parameters are not read at synth. They are seeded anyway because the
# fail-closed error message points the operator at the parameter path as the
# canonical record of the value — an error message naming a location that does not
# exist would be worse than no message.

command -v aws >/dev/null || { err "aws CLI not found"; exit 1; }

if [ "$MODE" = "verify" ]; then
  info "parameters under /cvx/$STAGE/ in $REGION (names only, values never printed)"
  aws ssm get-parameters-by-path --path "/cvx/$STAGE/" --region "$REGION" \
      --query 'Parameters[].Name' --output text | tr '\t' '\n' | sed 's/^/  /'
  exit 0
fi

aws sts get-caller-identity >/dev/null 2>&1 || { err "AWS credentials not configured"; exit 1; }

# ── Extract the stage block from the git ref that still has the values ────────
git cat-file -e "$FROM_REF:infrastructure/bin/vsa.ts" 2>/dev/null \
  || { err "$FROM_REF has no infrastructure/bin/vsa.ts"; exit 1; }

SRC="$(mktemp)"; trap 'rm -f "$SRC"' EXIT
git show "$FROM_REF:infrastructure/bin/vsa.ts" > "$SRC"

# Pull one field's value out of the named stage's STAGE_DEFAULTS block. Emits
# nothing (exit 1) when the field is absent, which the caller treats as a gap.
extract_value() {
  STAGE="$STAGE" FIELD="$1" python3 - "$SRC" <<'PY'
import os, re, sys
src = open(sys.argv[1], encoding="utf-8").read()
stage, field = os.environ["STAGE"], os.environ["FIELD"]
m = re.search(rf"^  {re.escape(stage)}: \{{", src, re.M)
if not m:
    sys.exit(1)
rest = src[m.end():]
nxt = re.search(r"^  \w+: \{", rest, re.M)     # start of the next stage block
block = rest[: nxt.start()] if nxt else rest
v = re.search(rf"^\s*{re.escape(field)}:\s*\n?\s*'([^']*)'", block, re.M)
if not v or not v.group(1).strip():
    sys.exit(1)
print(v.group(1), end="")
PY
}

info "stage=$STAGE region=$REGION from-ref=$FROM_REF mode=$MODE"
[ "$MODE" = "dry-run" ] && warn "DRY RUN — nothing will be written. Re-run with --apply."

created=0; updated=0; unchanged=0; gaps=0

for entry in "${FIELDS[@]}"; do
  IFS=':' read -r field kebab envvar tier <<<"$entry"
  name="/cvx/$STAGE/$kebab"

  # An exported env var wins — that is the escape hatch for values git never had.
  value="${!envvar:-}"
  source="env:$envvar"
  if [ -z "$value" ]; then
    if value="$(extract_value "$field")" && [ -n "$value" ]; then
      source="git:$FROM_REF"
    else
      value=""
    fi
  fi

  if [ -z "$value" ]; then
    warn "$name (tier $tier) — NO VALUE AVAILABLE. Not in $FROM_REF for stage '$STAGE', and \$$envvar is unset."
    gaps=$((gaps + 1))
    continue
  fi

  existing="$(aws ssm get-parameter --name "$name" --region "$REGION" \
                --query 'Parameter.Value' --output text 2>/dev/null || true)"

  if [ "$existing" = "$value" ]; then
    ok "$name (tier $tier) — already correct, no write"
    unchanged=$((unchanged + 1))
    continue
  fi

  if [ -n "$existing" ]; then
    action="OVERWRITE (existing value differs)"; updated=$((updated + 1))
  else
    action="CREATE"; created=$((created + 1))
  fi

  if [ "$MODE" = "apply" ]; then
    # The value is passed via --cli-input-json from a 600-mode temp file rather than
    # `--value "$value"`, because command-line arguments are world-readable through
    # `ps` for the lifetime of the call. That is the same exposure class as the ECS
    # `containerOverrides.environment` finding in
    # ~/.kiro/steering/secrets-handling.md § "Runtime launch parameters" — a routine
    # read surface returning credential-adjacent material as a side effect. Flagged
    # by security review cycle 1 (Suggestion 1).
    payload="$(mktemp)"
    chmod 600 "$payload"
    STAGE="$STAGE" NAME="$name" VALUE="$value" FIELD="$field" SOURCE="$source" \
      python3 -c 'import json, os; print(json.dumps({
        "Name": os.environ["NAME"],
        "Value": os.environ["VALUE"],
        "Type": "String",
        "Overwrite": True,
        "Description": "CVX %s %s — seeded by scripts/seed-stage-parameters.sh from %s" % (
            os.environ["STAGE"], os.environ["FIELD"], os.environ["SOURCE"]),
      }))' > "$payload"
    aws ssm put-parameter --region "$REGION" --cli-input-json "file://$payload" >/dev/null
    rm -f "$payload"
    ok "$name (tier $tier) — $action  [source $source]"
  else
    info "$name (tier $tier) — would $action  [source $source]"
  fi
done

echo
info "create=$created overwrite=$updated unchanged=$unchanged gaps=$gaps"

if [ "$gaps" -gt 0 ]; then
  err "$gaps parameter(s) have no available value."
  err "Export the corresponding env var and re-run, e.g.:"
  err "  CMS_KMS_KEY_ARN=<arn> bash scripts/seed-stage-parameters.sh --stage $STAGE --apply"
  err "Known case: prod's cmsKmsKeyArn was never baked into git by design."
  exit 1
fi

[ "$MODE" = "apply" ] && ok "seeding complete — verify with: bash ${BASH_SOURCE[0]##*/} --stage $STAGE --verify"
exit 0
