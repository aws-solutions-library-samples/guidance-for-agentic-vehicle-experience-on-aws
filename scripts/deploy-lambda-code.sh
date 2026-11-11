#!/usr/bin/env bash
# Push one Lambda's code from a pre-built bundle directory, and nothing else.
#
# Usage:
#   scripts/deploy-lambda-code.sh <function-name> <bundle-dir> <expected-current-sha> [region]
#
# <expected-current-sha> is the CodeSha256 you inspected before deciding to deploy (for
# example by downloading the live zip and diffing it). The push is refused if the
# function's code changed since then, and it carries the function's RevisionId, so a
# concurrent deploy between the check and the push makes the update fail rather than
# be silently overwritten. After the update the function's new CodeSha256 must equal
# the SHA-256 of the zip this script built.
#
# The bundle must pass scripts/lib/check-bundle-abi.py for the function's own runtime
# (read from the live configuration), so a bundle the Lambda cannot load is refused here
# even if it was built some other way.
#
# The zip leaves out what CDK's Python asset helper leaves out
# (infrastructure/lib/python-lambda-asset.ts PYTHON_ASSET_EXCLUDES): tests, __pycache__,
# *.pyc, .pytest_cache.
#
# A code-only push leaves the CloudFormation template's asset stale. The next
# `cdk deploy` of the owning stack ships whatever is in that checkout's build directory,
# so build it with the same, fixed script first.
#
# Written 2026-10-01 for the WebSocket authorizer
# (issues/2026-10-01-ws-authorizer-returns-http-api-response-shape/).
set -euo pipefail

if [[ $# -lt 3 || $# -gt 4 ]]; then
  echo "usage: $0 <function-name> <bundle-dir> <expected-current-sha> [region]" >&2
  exit 2
fi
FN="$1"
DIR="$2"
EXPECT="$3"
REGION="${4:-us-west-2}"

[[ -d "$DIR" ]] || { echo "ERROR: $DIR is not a directory" >&2; exit 2; }
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

read -r RUNTIME HANDLER < <(aws lambda get-function-configuration --function-name "$FN" \
  --region "$REGION" --query '[Runtime, Handler]' --output text)
# The function's configured handler must exist in the bundle: "handler.handler" means
# handler.py at the root, "triage_classifier.handler.handler" means
# triage_classifier/handler.py. Catches pushing the wrong directory.
HANDLER_FILE="$(echo "${HANDLER%.*}" | tr . /).py"
[[ -f "$DIR/$HANDLER_FILE" ]] || { echo "ERROR: $DIR has no $HANDLER_FILE (handler $HANDLER)" >&2; exit 2; }
if [[ ! "$RUNTIME" =~ ^python([0-9]+\.[0-9]+)$ ]]; then
  echo "ERROR: $FN runtime is '$RUNTIME', not a pythonX.Y runtime" >&2
  exit 2
fi
PYVER="${BASH_REMATCH[1]}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
ZIP="$WORK/code.zip"

(cd "$DIR" && zip -qr -X "$ZIP" . \
  -x 'tests' 'tests/*' '*/tests/*' '*/__pycache__/*' '__pycache__/*' '*.pyc' '.pytest_cache/*')
LOCAL_SHA="$(openssl dgst -sha256 -binary "$ZIP" | base64)"
echo "built     : $ZIP ($(du -h "$ZIP" | cut -f1)) sha256=$LOCAL_SHA"

# Check the zip that will be pushed, not the directory it came from, so nothing can
# change between the check and the upload.
mkdir "$WORK/check"
(cd "$WORK/check" && unzip -q "$ZIP")
python3 "$ROOT/scripts/lib/check-bundle-abi.py" "$WORK/check" --python "$PYVER" \
  || { echo "ERROR: $DIR cannot load on $RUNTIME; refusing to push it" >&2; exit 1; }

read -r LIVE_SHA REV < <(aws lambda get-function-configuration --function-name "$FN" \
  --region "$REGION" --query '[CodeSha256, RevisionId]' --output text)
echo "live      : sha256=$LIVE_SHA revision=$REV"

if [[ "$LIVE_SHA" != "$EXPECT" ]]; then
  echo "ERROR: $FN code changed since it was inspected (expected $EXPECT). Re-inspect." >&2
  exit 1
fi
if [[ "$LIVE_SHA" == "$LOCAL_SHA" ]]; then
  echo "unchanged : live code already equals this bundle; nothing to push"
  exit 0
fi

aws lambda update-function-code --function-name "$FN" --region "$REGION" \
  --zip-file "fileb://$ZIP" --revision-id "$REV" \
  --query '{sha:CodeSha256,rev:RevisionId}' --output text >/dev/null
aws lambda wait function-updated-v2 --function-name "$FN" --region "$REGION"

read -r NEW_SHA NEW_STATUS < <(aws lambda get-function-configuration --function-name "$FN" \
  --region "$REGION" --query '[CodeSha256, LastUpdateStatus]' --output text)
echo "deployed  : sha256=$NEW_SHA status=$NEW_STATUS"
if [[ "$NEW_SHA" != "$LOCAL_SHA" || "$NEW_STATUS" != "Successful" ]]; then
  echo "ERROR: deployed code does not match the bundle (or the update did not succeed)" >&2
  exit 1
fi
echo "OK        : $FN now runs the bundle from $DIR"
