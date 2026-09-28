#!/usr/bin/env bash
# check-standing-claim-pools.sh — pre-flight for flipping any AVX claim-trust attestation.
#
# docs/identity-and-standing-contract.md § 5.1: an attestation is valid only if EVERY Cognito
# pool the API authorizer trusts keeps standing claims out of every app client's
# WriteAttributes. This script derives that pool set from the deployed authorizer(s), so it
# cannot be scoped too narrowly by a hand-kept list.
#
# Fail-closed: prints OK / VIOLATION / ERROR per client and exits non-zero on ANY VIOLATION
# or ERROR, including "found no pools" or "a lookup failed". A check that cannot see
# something must never report that it passed.
#
# Usage: bash scripts/check-standing-claim-pools.sh [--stage staging] [--region us-west-2]
# Exit:  0 all clients OK · 1 any VIOLATION or ERROR
set -uo pipefail

STAGE=staging
REGION=us-west-2
while [ $# -gt 0 ]; do
  case "$1" in
    --stage) STAGE="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    *) echo "ERROR: unknown argument $1" >&2; exit 1 ;;
  esac
done

FAIL=0
err() { echo "ERROR: $*"; FAIL=1; }

API_NAME="vsa-${STAGE}-api"
REST_API_ID=$(aws apigateway get-rest-apis --region "$REGION" \
  --query "items[?name=='${API_NAME}'].id" --output text 2>&1) || { err "get-rest-apis failed: $REST_API_ID"; REST_API_ID=""; }
if [ -z "$REST_API_ID" ] || [ "$REST_API_ID" = "None" ] || [ "$(printf '%s' "$REST_API_ID" | wc -w)" -ne 1 ]; then
  err "expected exactly one RestApi named ${API_NAME}, got '${REST_API_ID}'"
  echo "RESULT: FAIL (nothing was checked)"; exit 1
fi

POOL_IDS=$(aws apigateway get-authorizers --rest-api-id "$REST_API_ID" --region "$REGION" \
  --query 'items[].providerARNs[]' --output text 2>&1) || { err "get-authorizers failed: $POOL_IDS"; POOL_IDS=""; }
POOL_IDS=$(printf '%s' "$POOL_IDS" | tr '\t' '\n' | grep -E '^arn:aws:cognito-idp:' | sed 's|.*/||' | sort -u)
if [ -z "$POOL_IDS" ]; then
  err "no Cognito user pools found on the authorizers of ${API_NAME}"
  echo "RESULT: FAIL (nothing was checked)"; exit 1
fi

for POOL_ID in $POOL_IDS; do
  POOL_NAME=$(aws cognito-idp describe-user-pool --user-pool-id "$POOL_ID" --region "$REGION" \
    --query 'UserPool.Name' --output text 2>&1) || { err "describe-user-pool $POOL_ID failed: $POOL_NAME"; continue; }
  echo "=== pool $POOL_ID ($POOL_NAME) ==="
  # No --max-results: setting it switches the CLI to manual paging, so it stops following
  # NextToken and the check would pass having seen only the first page (review Cycle 5 W1).
  CIDS=$(aws cognito-idp list-user-pool-clients --user-pool-id "$POOL_ID" --region "$REGION" \
    --query 'UserPoolClients[].ClientId' --output text 2>&1) \
    || { err "list-user-pool-clients $POOL_ID failed: $CIDS"; continue; }
  CIDS=$(printf '%s' "$CIDS" | tr '\t' '\n' | grep -v '^None$' | grep -v '^$' || true)
  if [ -z "$CIDS" ]; then err "pool $POOL_ID lists no app clients"; continue; fi
  for CID in $CIDS; do
    WRITE_ATTRS=$(aws cognito-idp describe-user-pool-client --user-pool-id "$POOL_ID" --client-id "$CID" \
      --region "$REGION" --query 'UserPoolClient.WriteAttributes' --output json 2>&1) \
      || { echo "  client $CID: ERROR (describe-user-pool-client failed: ${WRITE_ATTRS})"; FAIL=1; continue; }
    # The CLI prints JSON null when WriteAttributes is absent, which means every attribute is writable.
    if [ "$WRITE_ATTRS" = "null" ]; then
      echo "  client $CID: VIOLATION (WriteAttributes unset — all attributes writable by default)"; FAIL=1
    elif printf '%s' "$WRITE_ATTRS" | grep -q '"custom:'; then
      echo "  client $CID: VIOLATION (WriteAttributes includes custom claim(s): $(printf '%s' "$WRITE_ATTRS" | tr -d ' \n'))"; FAIL=1
    elif ! printf '%s' "$WRITE_ATTRS" | grep -q '^\['; then
      echo "  client $CID: ERROR (unexpected WriteAttributes output: ${WRITE_ATTRS})"; FAIL=1
    else
      echo "  client $CID: OK (WriteAttributes=$(printf '%s' "$WRITE_ATTRS" | tr -d ' \n'))"
    fi
  done
done

if [ "$FAIL" -eq 0 ]; then echo "RESULT: PASS"; exit 0; fi
echo "RESULT: FAIL"; exit 1
