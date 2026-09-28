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
#
# Tier A — resolved by CloudFormation at deploy time via valueForStringParameter.
# Tier B — must be concrete at synth time (env var). Seeded here so the fail-closed
#          guard message can name the canonical SSM path.
#
# "Tier A / B" is the taxonomy from spec 2026-08-05-cvx-stage-defaults-parameterization.
# Parameters that do not fit that taxonomy are labelled explicitly below.
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
  # DMS REST API base URL, consumed by agents/executors/dms.py:67 via the
  # VSA_DMS_API_ENDPOINT env var on the AVX findings Lambda.
  #
  # Tier A. git has never carried this value — it was never a STAGE_DEFAULTS literal —
  # so it seeds exclusively through the env-var escape hatch above. Derive it from DMS's
  # own CloudFormation output rather than typing it:
  #
  #   VSA_DMS_API_ENDPOINT="$(aws cloudformation describe-stacks \
  #     --stack-name "DmsApiStack-$STAGE" --region us-west-2 \
  #     --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue | [0]" \
  #     --output text)" \
  #     scripts/seed-stage-parameters.sh --stage "$STAGE" --apply
  #
  # DO NOT derive it with `list-exports`, and do not "fix" that with --no-paginate.
  # Both are wrong, and both fail SILENTLY — verified 2026-09-23 by doing it:
  #
  #   list-exports --query "Exports[?Name=='dms-staging-api-url'].Value | [0]" --output text
  #     -> "None\n<the url>\nNone"   (the CLI paginates and emits one result PER PAGE,
  #                                   so pages without a match contribute the string
  #                                   "None". This value was actually written to SSM
  #                                   before it was caught.)
  #   ...the same query with --no-paginate
  #     -> "None"                    (the export is not on page 1, so limiting to one
  #                                   page returns a confident wrong answer instead of
  #                                   a multi-line one. Strictly worse.)
  #
  # `describe-stacks` on a single named stack aggregates nothing across pages, which is
  # why it is the form used above. Whatever you use, assert the result is ONE line and
  # starts with https:// before writing it — a corrupt endpoint here is not a deploy
  # failure, it is a dispatch that reaches nothing.
  #
  # Why SSM and not `cdk.Fn.importValue('dms-staging-api-url')`, which does exist:
  # a cross-stack CFN import would make CVX's stack a deletion-lock on DMS's export,
  # coupling two accelerators' lifecycles. There is no existing CVX->DMS infrastructure
  # reference to follow, so this establishes the pattern — and the precedent worth
  # following is cmsKmsKeyArn / redisEndpoint above, which are likewise values owned by
  # ANOTHER project and referenced through /cvx/{stage}/* rather than imported.
  #
  # The cost of that choice is staleness: if DMS's REST API is ever REPLACED (not merely
  # updated — the API id is stable across updates), this parameter silently points at a
  # dead endpoint. `--verify` lists it; re-running the seed command above re-derives it.
  "dmsApiEndpoint:dms-api-endpoint:VSA_DMS_API_ENDPOINT:A"
  # ARN of the Amazon Connect instance the P0 outbound callback may dial from.
  # Scopes connect:StartOutboundVoiceContact on the api-escalate role to
  # "<this ARN>/contact/*" (escalation spec security W2). Tier A, and like
  # dmsApiEndpoint it has no git source: the instance belongs to the Connect
  # setup, not to this repo. Staging's value was first seeded by
  # scripts/seed-personas.py. To reseed, take it from the tenant config the
  # callback actually uses (channels.connect.instanceArn), and check it is ONE
  # line that starts with arn:aws:connect: before writing it.
  "connectInstanceArn:connect-instance-arn:CVX_CONNECT_INSTANCE_ARN:A"
  # Operator attestation that `custom:fleetIds` may authorize AVX fleet standing.
  # Value is exactly "true" or "false". There is no git source for it, because it is a
  # decision rather than a coordinate, so a run without the env var reports a gap.
  # That is intended: an unseeded parameter fails the API-stack deploy, and trust
  # must be chosen, not inherited. Seed "true" only after checking the conditions in
  # docs/identity-and-standing-contract.md § 5.1 for every pool the authorizer trusts.
  "avxFleetIdsClaimTrusted:avx-fleet-ids-claim-trusted:CVX_AVX_FLEET_IDS_CLAIM_TRUSTED:A"
  # Operator attestation that custom:customerId may authorize AVX owner standing.
  # Same constraints as avxFleetIdsClaimTrusted above. Seed "true" only after checking
  # docs/identity-and-standing-contract.md § 5.1. Threaded to the findings Lambda ONLY
  # (not to api-avx-actions — no code path there reads this variable).
  "avxCustomerIdClaimTrusted:avx-customer-id-claim-trusted:CVX_AVX_CUSTOMER_ID_CLAIM_TRUSTED:A"
  # Operator attestation that custom:vehicleId may authorize AVX driver standing.
  # Same constraints as avxFleetIdsClaimTrusted above. Seed "true" only after checking
  # docs/identity-and-standing-contract.md § 5.1 AND after T3.4 deploys (vsa-staging-meridian-fleet
  # pool leaves custom:vehicleId self-writable until T3.4 restricts WriteAttributes).
  # Threaded to the findings Lambda ONLY (not to api-avx-actions — no code path there
  # reads this variable).
  "avxVehicleIdClaimTrusted:avx-vehicle-id-claim-trusted:CVX_AVX_VEHICLE_ID_CLAIM_TRUSTED:A"
  # Operator email for Tier 2 alarm SNS subscriptions (spec § D6).
  # OPTIONAL — deliberately outside the A/B taxonomy: it is neither a baked literal
  # nor fail-closed at synth. Absence warns and skips without failing the seed run;
  # the gaps counter is NOT incremented. A stage can deploy without it and add
  # the SNS subscriber separately via `aws sns subscribe`.
  "tier2AlarmEmail:tier2-alarm-email:CVX_TIER2_ALARM_EMAIL:OPTIONAL"
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
    if [ "$tier" = "OPTIONAL" ]; then
      warn "$name (optional) — not set. Export \$$envvar to seed this value."
      warn "  Without it, synth will warn but succeed; the SNS topic will have no subscriber."
    else
      warn "$name (tier $tier) — NO VALUE AVAILABLE. Not in $FROM_REF for stage '$STAGE', and \$$envvar is unset."
      gaps=$((gaps + 1))
    fi
    continue
  fi

  # ── Shape guard: refuse to write a value that is obviously CLI debris ───────
  #
  # Added 2026-09-23 after this script wrote `"None\n<url>\nNone"` to
  # /cvx/staging/dms-api-endpoint. The value came from
  # `aws cloudformation list-exports --output text`, which paginates and emits one
  # result PER PAGE, so every page without a match contributed the string "None".
  # Nothing objected: the env-var escape hatch passed it straight through, SSM
  # accepts multi-line String values, and the script reported a cheerful CREATE.
  #
  # A comment would not have prevented it — the buggy command WAS in a comment in
  # this file, written minutes earlier by the same person who then ran it. So the
  # check is executable.
  #
  # Both failing forms are covered, because the obvious fix is also wrong:
  # `--no-paginate` on that same query returns the bare string "None" (the export
  # is not on page 1), which is a confident wrong answer rather than a malformed
  # one, and would sail past a newline-only check.
  if [ "$(printf '%s' "$value" | wc -l | tr -d ' ')" != "0" ]; then
    err "$name — REFUSING to write a multi-line value (probable paginated --output text debris)."
    err "  Derive it with a single-resource call, e.g. describe-stacks on one stack,"
    err "  and assert the result is one line before exporting \$$envvar."
    gaps=$((gaps + 1))
    continue
  fi
  if [ "$value" = "None" ] || [ "$value" = "null" ]; then
    err "$name — REFUSING to write the literal '$value'. That is what the AWS CLI"
    err "  prints for an empty JMESPath result; the query matched nothing."
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
  err "Note: OPTIONAL parameters (e.g. tier2-alarm-email) are outside the A/B taxonomy —"
  err "      their absence warns but does not count as a gap. Tier A and Tier B gaps are blocking."
  exit 1
fi

[ "$MODE" = "apply" ] && ok "seeding complete — verify with: bash ${BASH_SOURCE[0]##*/} --stage $STAGE --verify"
exit 0
