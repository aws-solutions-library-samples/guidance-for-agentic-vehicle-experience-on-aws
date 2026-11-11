#!/usr/bin/env bash
#
# live-verify-avx-http-surface.sh — exercise the AVX HTTP surface end to end against
# deployed staging: real API Gateway, real Cognito authorizer, real JWTs, real DynamoDB.
#
# WHY THIS EXISTS
#
# `scripts/live-verify-avx-read-surface.py` closed the DATA-PLANE half of the
# live-verification gate by calling the real writer/handler modules against the real
# table. It does not traverse API Gateway, the Cognito authorizer, or the scope
# predicates as a request actually reaches them. That is the half this covers:
#
#   writer -> DynamoDB GSI -> Lambda -> API Gateway -> Cognito authz -> response body
#
# Three properties here cannot be established by any stubbed test:
#   1. The scope GSIs return REAL SEEDED ROWS through the route (not 200-with-empty,
#      which is what a broken filter looks like and what this spec shipped twice).
#   2. Cross-scope requests are denied by the deployed authorizer + predicates, not
#      merely by a unit test's mock claims.
#   3. 403 precedes the honest 501 on the actions-scope stubs — T11.5 Accept 2. If that
#      ordering inverts, the stub becomes an oracle disclosing which scopes exist.
#
# Creates synthetic Cognito users and Findings, and deletes both in a trap. Opt-in.
#
# USAGE
#   LIVE_TESTS=1 bash scripts/live-verify-avx-http-surface.sh
#
# EXIT 0 all checks passed · 1 a check failed · 2 refused to run
set -uo pipefail

[ "${LIVE_TESTS:-}" = "1" ] || { echo "refusing to run: set LIVE_TESTS=1 (creates real Cognito users + DDB rows)"; exit 2; }

REGION="${AWS_REGION:-us-west-2}"
STAGE="${VSA_STAGE:-staging}"

# Resolved at run time, never baked. These are real staging coordinates and this
# file SHIPS to the public mirror, so a literal pool ID trips
# `forbidden_patterns.cognito_user_pool_id` and fails the publish scan — it did,
# on 2026-09-21, at this line. See
# issues/2026-09-21-cognito-pool-id-in-live-verify-script/ (the value itself is
# deliberately NOT repeated here: naming it in the comment that forbids it is how
# ~/.kiro/steering/public-mirror-publish.md records four separate exposures
# happening).
#
# SSM is the same source `scripts/with-stage-env.sh` reads, and spec
# `2026-08-05-cvx-stage-defaults-parameterization` exists precisely to keep pool
# IDs and ARNs out of source that cannot be excluded from a publish. This is that
# pattern applied to a script rather than to the CDK entrypoint.
#
# Do NOT "fix" a failing lookup by pasting the literal back. Do NOT substitute a
# shaped placeholder either: a region-prefixed dummy of the same form (region,
# underscore, nine-plus alphanumerics) still matches the pool-ID regex, which is a
# documented trap in ~/.kiro/steering/public-mirror-publish.md. If you need a
# placeholder anywhere, use an angle-bracket form such as <user-pool-id>, which
# does not match. Override via the env vars below.
_ssm() {
  aws ssm get-parameter --name "$1" --region "$REGION" \
    --query 'Parameter.Value' --output text 2>/dev/null
}
_cfn_out() {
  aws cloudformation describe-stacks --stack-name "$1" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text 2>/dev/null
}

POOL="${AVX_POOL_ID:-$(_ssm "/cvx/$STAGE/voice-auth-user-pool-id")}"
CLIENT="${AVX_CLIENT_ID:-$(_ssm "/cvx/$STAGE/voice-auth-user-pool-client-id")}"
API="${AVX_API_BASE:-$(_cfn_out "vsa-$STAGE-api" RestApiUrl)}"
API="${API%/}"
TABLE="${AVX_FINDINGS_TABLE:-vsa-$STAGE-avx-findings}"
# Generated per run, never a literal. A hardcoded password here was a real exposure:
# these accounts are created with `admin-set-user-password --permanent`, and this file
# is in scope for the public GitHub mirror — it is not in `.publish-exclude` and no
# `forbidden_strings` / `forbidden_patterns` / digest entry covers it, so the literal
# shipped as-is (security review Group 16 Cycle 1 W1).
#
# Randomising eliminates the class rather than gating it: there is no longer a secret
# in the file to leak, so the fix holds even if the publish configuration changes
# later — which is the failure mode `~/.kiro/steering/public-mirror-publish.md`
# documents, where a denylist written in terms of the secret embeds the secret.
# It also bounds the blast radius of an orphaned account: the EXIT trap does not fire
# on SIGKILL or a host crash, so an orphan is possible, and a per-run password means
# an orphan cannot be authenticated with a value anyone can read from the repo.
# The `Aa1!` suffix guarantees Cognito's four character classes (upper, lower, digit,
# symbol) regardless of what base64 happens to emit — without it the run would fail
# intermittently on the pool's password policy, which is a worse failure than the one
# being fixed because it looks like a broken surface rather than a weak password.
PW="$(openssl rand -base64 24)Aa1!"
RUN="$(date +%s)"
REPO="$(cd "$(dirname "$0")/.." && pwd)"

# Resolution pre-flight, BEFORE the reachability check below. An unresolved value
# is empty, and `describe-user-pool --user-pool-id ''` fails with a message about
# an unreachable pool — which would misattribute a missing SSM parameter to a
# wrong region. That is exactly the "symptom three steps removed from the cause"
# this script's next check was written to stop, so it needs its own check.
for _pair in "POOL:/cvx/$STAGE/voice-auth-user-pool-id:AVX_POOL_ID" \
             "CLIENT:/cvx/$STAGE/voice-auth-user-pool-client-id:AVX_CLIENT_ID" \
             "API:vsa-$STAGE-api output RestApiUrl:AVX_API_BASE"; do
  _name="${_pair%%:*}"; _rest="${_pair#*:}"
  _src="${_rest%:*}"; _env="${_rest##*:}"
  if [ -z "${!_name}" ]; then
    echo "refusing to run: could not resolve $_name for stage '$STAGE' in '$REGION'." >&2
    echo "  source: $_src" >&2
    echo "  Either seed it (deployment/scripts/seed-stage-parameters.sh, or deploy" >&2
    echo "  the stack), or pass it explicitly: $_env=<value>" >&2
    exit 2
  fi
done

# Pre-flight: REGION comes from $AWS_REGION, but POOL/CLIENT/API/TABLE default to
# us-west-2 resources. A machine exporting AWS_REGION=us-east-1 (ADP's region) sent
# every Cognito call to a region where this pool does not exist; the calls failed,
# `admin-list-groups-for-user` returned empty, and the run reported "a token was not
# issued" — a symptom three steps removed from the cause. Bind the two together and
# fail loudly instead, naming the fix.
if ! aws cognito-idp describe-user-pool --user-pool-id "$POOL" --region "$REGION" \
     >/dev/null 2>&1; then
  echo "refusing to run: user pool '$POOL' is not reachable in region '$REGION'." >&2
  echo "  AWS_REGION is currently '${AWS_REGION:-<unset>}'." >&2
  echo "  The pool was resolved from /cvx/$STAGE/voice-auth-user-pool-id, which" >&2
  echo "  names a pool in that parameter's own region. Re-run with a matching region:" >&2
  echo "    LIVE_TESTS=1 AWS_REGION=us-west-2 bash scripts/$(basename "$0")" >&2
  echo "  (or override AVX_POOL_ID/AVX_CLIENT_ID/AVX_API_BASE/AVX_FINDINGS_TABLE" >&2
  echo "   to resources that do live in '$REGION'.)" >&2
  exit 2
fi

DEALER_A="DEALER-LIVE-A-$RUN"; DEALER_B="DEALER-LIVE-B-$RUN"
DIST_A="DIST-LIVE-A-$RUN";     DIST_B="DIST-LIVE-B-$RUN"
FLEET_A="FLEET-LIVE-A-$RUN";   FLEET_B="FLEET-LIVE-B-$RUN"
VIN="LIVEHTTP${RUN: -7}"

declare -a USERS=() FINDINGS=()
# Registered up-front, before any user exists, so the trap can always clean up.
for _l in dealer district fleet advisor; do USERS+=("avx-live-$_l-$RUN@example.com"); done
PASS=0; FAIL=0; KNOWN=0

# Findings written by the seeder are appended to this file one per line, flushed
# immediately after each _put_item. The trap reads THIS file, not the FINDINGS
# array, so a partial seed (write 1 succeeds, write 2 raises) still cleans up the
# rows that were written before the failure. Same orphan class as the read-surface
# script's `written` list and the user-accounts pre-registration above.
FINDINGS_FILE="$(mktemp /tmp/avx-live-findings-XXXXXX)"

cleanup() {
  # Preserve the status the script was exiting with, so a cleanup-only failure can
  # escalate it without masking a real one.
  local rc=$?
  local cleanup_failures=0
  echo
  echo "── cleanup ──"
  for u in "${USERS[@]:-}"; do
    [ -n "$u" ] || continue
    if aws cognito-idp admin-delete-user --user-pool-id "$POOL" --username "$u" \
        --region "$REGION" >/dev/null 2>&1; then
      echo "  deleted user $u"
    else
      # Cycle 3 W2: this loop previously had no `else` at all, so an orphaned user
      # printed nothing and the run still exited 0 — the same orphan class the
      # findings loop below had fixed, one loop away. It matters more here, not
      # less: these accounts are created `--permanent` with the password literal
      # that lives in this file, so an orphaned account is an orphaned credential.
      echo "  WARN: failed to delete user $u — ORPHANED CREDENTIAL in $POOL" >&2
      cleanup_failures=$((cleanup_failures+1))
    fi
  done
  # Read finding IDs from the file the seeder appended to, one per line. This
  # succeeds even when the seeder crashed mid-run because each ID was flushed before
  # the next write began.
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    if aws dynamodb delete-item --table-name "$TABLE" --region "$REGION" \
        --key "{\"finding_id\":{\"S\":\"$f\"}}" 2>/dev/null; then
      echo "  deleted finding $f"
    else
      echo "  WARN: failed to delete finding $f — orphaned row in $TABLE" >&2
      cleanup_failures=$((cleanup_failures+1))
    fi
  done < "$FINDINGS_FILE"
  rm -f "$FINDINGS_FILE"

  # Incrementing the outer FAIL here cannot work: this trap runs on EXIT, after the
  # summary block has already chosen `exit 0`, so the status is fixed by the time we
  # are reached (review Cycle 2 Warning). An EXIT trap CAN override it by exiting
  # itself, which is the only way an orphaned row becomes "a failure of the run" as
  # T16.4's Accept requires.
  if [ "$cleanup_failures" -gt 0 ]; then
    echo "  cleanup left $cleanup_failures orphaned row(s) — failing the run" >&2
    exit 1
  fi
  exit "$rc"
}
trap cleanup EXIT

# Usernames are deterministic so cleanup does not depend on state mutated inside a
# subshell. mk_user is called as `TOK=$(mk_user ...)`, and command substitution runs in a
# SUBSHELL -- so an earlier `USERS+=("$u")` inside it was silently discarded and the trap
# deleted nothing, leaving four synthetic accounts in a real staging pool. Same orphan
# class as the read-surface script's `written` list, found the same day.
user_name() { echo "avx-live-$1-$RUN@example.com"; }

mk_user() {  # mk_user <local> <group> <attrName> <attrValue>
  local u; u="$(user_name "$1")"
  aws cognito-idp admin-create-user --user-pool-id "$POOL" --username "$u" --region "$REGION" \
    --message-action SUPPRESS \
    --user-attributes Name=email,Value="$u" Name=email_verified,Value=true "Name=$3,Value=$4" >/dev/null 2>&1
  aws cognito-idp admin-set-user-password --user-pool-id "$POOL" --username "$u" \
    --password "$PW" --permanent --region "$REGION" >/dev/null 2>&1
  aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL" --username "$u" \
    --group-name "$2" --region "$REGION" >/dev/null 2>&1
  # Assert ONLY the intended group. An account carrying platform-admin would
  # short-circuit the very scope checks under test and produce a false pass.
  local got
  got=$(aws cognito-idp admin-list-groups-for-user --user-pool-id "$POOL" --username "$u" \
        --region "$REGION" --query 'Groups[].GroupName' --output text 2>/dev/null)
  if [ "$got" != "$2" ]; then
    # stderr, not stdout: this function's stdout is captured by `$(mk_user ...)`, so an
    # `echo` here lands in the caller's token variable instead of the terminal, and the
    # `exit 1` only leaves the command-substitution subshell. The parent then reports
    # the generic "a token was not issued" and the real reason is lost.
    echo "SETUP FAIL: $u has groups '$got', expected exactly '$2'" >&2
    exit 1
  fi
  aws cognito-idp initiate-auth --client-id "$CLIENT" --auth-flow USER_PASSWORD_AUTH \
    --auth-parameters USERNAME="$u",PASSWORD="$PW" --region "$REGION" \
    --query 'AuthenticationResult.IdToken' --output text 2>/dev/null
}

check() {  # check <name> <expected_status> <actual_status> [expected_substr] [body]
  local name="$1" want="$2" got="$3" substr="${4:-}" body="${5:-}"
  if [ "$got" != "$want" ]; then
    FAIL=$((FAIL+1)); echo "FAIL  $name — expected HTTP $want, got $got"
    [ -n "$body" ] && echo "        body: $(echo "$body" | head -c 200)"
    return
  fi
  if [ -n "$substr" ] && ! echo "$body" | grep -q "$substr"; then
    FAIL=$((FAIL+1)); echo "FAIL  $name — HTTP $got correct but body lacks '$substr'"
    echo "        body: $(echo "$body" | head -c 200)"; return
  fi
  PASS=$((PASS+1)); echo "PASS  $name (HTTP $got${substr:+, matched '$substr'})"
}

known_defect() {  # known_defect <name> <current_status> <actual> <body> <when_fixed>
  local name="$1" cur="$2" got="$3" body="${4:-}" fixed="${5:-}"
  if [ "$got" = "$cur" ]; then
    KNOWN=$((KNOWN+1)); echo "KNOWN-DEFECT  $name — HTTP $got as currently expected"
    return
  fi
  FAIL=$((FAIL+1))
  echo "FAIL  $name — behaviour CHANGED: expected the known-defect HTTP $cur, got $got."
  echo "        If the dealer group mismatch was fixed, update this check: $fixed"
  [ -n "$body" ] && echo "        body: $(echo "$body" | head -c 200)"
}

req() {  # req <token|-> <path>  -> "<status>\n<body>"
  local tok="$1" path="$2"
  if [ "$tok" = "-" ]; then
    curl -s -o /tmp/avxbody -w '%{http_code}' "$API$path"
  else
    curl -s -o /tmp/avxbody -w '%{http_code}' -H "Authorization: $tok" "$API$path"
  fi
}

echo "── setup ──"
TOK_DEALER=$(mk_user dealer   dealer-admin     custom:dealerIds   "$DEALER_A")
TOK_DIST=$(mk_user   district district-manager custom:districtIds "$DIST_A")
TOK_FLEET=$(mk_user  fleet    fleet-operator   custom:fleetIds    "$FLEET_A")
TOK_ADV=$(mk_user    advisor  service-advisor  custom:role        service-advisor
)
for t in "$TOK_DEALER" "$TOK_DIST" "$TOK_FLEET" "$TOK_ADV"; do
  [ "${t:0:2}" = "ey" ] || { echo "SETUP FAIL: a token was not issued"; exit 1; }
done
echo "  4 users created, tokens issued, group membership asserted"

# Seed real rows so a 200 proves DATA, not an empty list from a broken filter.
# The Python seeder prints each finding_id on its own line immediately after
# _put_item succeeds (flush=True). We append those lines to FINDINGS_FILE as they
# arrive, so the cleanup trap can delete every row that was actually written even if
# the seeder raises mid-run. Previously the seeder collected all IDs and printed them
# at the end; a failure on write 2 left SEED_OUT empty, FINDINGS empty, and the
# first row orphaned in the real staging table.
SEED_OUT=$(cd "$REPO" && AVX_FINDINGS_TABLE="$TABLE" AWS_REGION="$REGION" python3 - "$DEALER_A" "$DIST_A" "$FLEET_A" "$FINDINGS_FILE" <<'PY'
import json, os, pathlib, sys, uuid
REPO = pathlib.Path(os.getcwd())
sys.path.insert(0, str(REPO / "agents" / "tier2"))
import boto3, writer as W
table = os.environ["AVX_FINDINGS_TABLE"]
cl = boto3.client("dynamodb", region_name=os.environ["AWS_REGION"])
fx = json.loads((REPO / "agents/tier2/schemas/fixtures/finding_health_tires_valid.json").read_text())
findings_file = sys.argv[4]
ids = []
for field, value in (("dealership_id", sys.argv[1]), ("district_id", sys.argv[2]), ("fleet_id", sys.argv[3])):
    f = dict(fx)
    f["finding_id"] = f"AVX-HTTP-{field}-{uuid.uuid4().hex[:10]}"
    f["scope"] = {field: value}
    f["finding_key"] = W.compute_finding_key(
        agent_id=f["agent_id"], scope_field=field, scope_value=value, finding_kind=f["finding_kind"])
    W._put_item(table_name=table, dynamodb_client=cl, finding=f)
    # Emit the ID immediately after each successful write, flushed, so the bash
    # trap can clean up partial seeds. The trap reads FINDINGS_FILE; stdout carries
    # ONLY the joined list below, which feeds the count check. Printing each id to
    # stdout here as well put 6 tokens in SEED_OUT where the check requires 3, and
    # aborted setup on every run (review Cycle 2 Critical).
    with open(findings_file, "a") as fh:
        print(f["finding_id"], file=fh, flush=True)
    ids.append(f["finding_id"])
# Space-separated for the count check that follows.
print(" ".join(ids))
PY
)
# shellcheck disable=SC2206
FINDINGS=($SEED_OUT)
[ "${#FINDINGS[@]}" -eq 3 ] || { echo "SETUP FAIL: seeding produced '${SEED_OUT}'"; exit 1; }
echo "  seeded 3 scope-scoped findings (dealership / district / fleet)"

echo
echo "── authorizer ──"
s=$(req - "/findings/dealership/$DEALER_A"); check "no token is rejected" 401 "$s" "" "$(cat /tmp/avxbody)"
s=$(req "Bearer not.a.jwt" "/findings/dealership/$DEALER_A"); check "malformed token is rejected" 401 "$s" "" "$(cat /tmp/avxbody)"

echo
echo "── findings scope routes: live GSI reads (T10.3 made these live; DMS T7.1 depends on them) ──"
# Was a KNOWN DEFECT until 2026-09-20: check_dealer_scope required Cognito group
# "dealer", which no pool provisions. Corrected to "dealer-admin" (the group CMS actually
# creates and DMS's own auth uses) and the frozen contract's mechanism string rotated to
# match. Now a real assertion. See
# issues/2026-09-20-dealer-scope-requires-nonexistent-cognito-group/.
s=$(req "$TOK_DEALER" "/findings/dealership/$DEALER_A"); b=$(cat /tmp/avxbody)
check "dealer reads own dealership findings" 200 "$s" "${FINDINGS[0]}" "$b"
s=$(req "$TOK_DIST" "/findings/district/$DIST_A"); b=$(cat /tmp/avxbody)
check "district manager reads own district findings" 200 "$s" "${FINDINGS[1]}" "$b"
s=$(req "$TOK_FLEET" "/findings/fleet/$FLEET_A"); b=$(cat /tmp/avxbody)
check "fleet operator reads own fleet findings" 200 "$s" "${FINDINGS[2]}" "$b"

echo
echo "── cross-scope denial, enforced by the DEPLOYED predicates ──"
s=$(req "$TOK_DEALER" "/findings/dealership/$DEALER_B"); check "dealer denied another dealership" 403 "$s" "" "$(cat /tmp/avxbody)"
s=$(req "$TOK_DIST" "/findings/district/$DIST_B");       check "district denied another district"  403 "$s" "" "$(cat /tmp/avxbody)"
s=$(req "$TOK_FLEET" "/findings/fleet/$FLEET_B");        check "fleet denied another fleet"        403 "$s" "" "$(cat /tmp/avxbody)"
s=$(req "$TOK_DEALER" "/findings/district/$DIST_A");     check "dealer denied a district route"    403 "$s" "" "$(cat /tmp/avxbody)"

echo
echo "── actions scope stubs: honest 501, and 403 BEFORE it (T11.5 Accept 2 — the oracle guard) ──"
s=$(req "$TOK_DEALER" "/actions/dealership/$DEALER_A"); b=$(cat /tmp/avxbody)
check "entitled dealer gets honest 501" 501 "$s" "actions_scope_index_absent" "$b"
s=$(req "$TOK_DEALER" "/actions/dealership/$DEALER_B"); b=$(cat /tmp/avxbody)
check "UNentitled dealer gets 403, not 501" 403 "$s" "" "$b"
s=$(req "$TOK_DIST" "/actions/district/$DIST_A"); b=$(cat /tmp/avxbody)
check "entitled district gets honest 501" 501 "$s" "actions_scope_index_absent" "$b"
s=$(req "$TOK_DIST" "/actions/district/$DIST_B"); b=$(cat /tmp/avxbody)
check "UNentitled district gets 403, not 501" 403 "$s" "" "$b"

echo
echo "── VIN routes: honest 503 while the fleet_id projection is absent (T12.1) ──"
s=$(req "$TOK_FLEET" "/findings/vehicle/$VIN"); b=$(cat /tmp/avxbody)
check "fleet caller gets 503 fleet_scope_unavailable" 503 "$s" "fleet_scope_unavailable" "$b"
s=$(req "$TOK_FLEET" "/actions/vehicle/$VIN"); b=$(cat /tmp/avxbody)
check "fleet caller gets 503 on actions VIN too" 503 "$s" "fleet_scope_unavailable" "$b"

echo
echo "── anti-oracle: a VIN with findings and one without must be indistinguishable ──"
s1=$(req "$TOK_FLEET" "/findings/vehicle/$VIN");            b1=$(cat /tmp/avxbody)
s2=$(req "$TOK_FLEET" "/findings/vehicle/VINWITHNOROWS99"); b2=$(cat /tmp/avxbody)
if [ "$s1" = "$s2" ] && [ "$b1" = "$b2" ]; then
  PASS=$((PASS+1)); echo "PASS  responses identical across both VINs (status $s1, bodies equal)"
else
  FAIL=$((FAIL+1)); echo "FAIL  ORACLE: differs — $s1/$s2, bodies $([ "$b1" = "$b2" ] && echo equal || echo DIFFER)"
fi

echo
echo "────────────────────────────────────────"
if [ "$FAIL" -gt 0 ]; then echo "FAIL — $PASS passed, $FAIL failed, $KNOWN known defect(s)"; exit 1; fi
echo "PASS — $PASS checks passed, $KNOWN known defect(s) asserted as current state"
[ "$KNOWN" -gt 0 ] && echo "      known defects: issues/2026-09-20-dealer-scope-requires-nonexistent-cognito-group/"
exit 0
