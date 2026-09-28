#!/usr/bin/env bash
# Pre-bundle RSA Lambda assets so each deployment package contains
# its own copy of lambdas/shared/rsa_client*.py alongside handler.py.
#
# Problem solved: `lambdas/shared/` is a sibling directory to the two RSA
# Lambdas. Code.fromAsset() bundles only the Lambda's own directory, so the
# `from shared.rsa_client import ...` at cold start raises:
#   ModuleNotFoundError: No module named 'shared'
#
# Demonstrated by the decisions.md entry "Group 2 wave 1 shipped a cold-start
# break that two green suites could not see" — copying only handler.py to /tmp
# and importing from there reproduces the error:
#   cd /tmp && python3 -c "import sys; sys.path=['.']; import handler"
#   -> ModuleNotFoundError: No module named 'shared'
#
# Solution (mirrors build-avx-lambda.sh / build-triage-classifier.sh precedent):
#   For each RSA Lambda that imports from shared, create a build directory at
#   `infrastructure/build/<name>/` containing:
#     handler.py                       — the Lambda entry point
#     shared/rsa_client.py             — provider Protocol + constants
#     shared/rsa_client_fake.py        — FakeRsaProvider (default)
#     shared/rsa_client_real.py        — stub for real integration
#     shared/__init__.py               — makes shared/ a package on sys.path
#   CDK Code.fromAsset() in the stacks then references each
#   `infrastructure/build/<name>/` directory instead of the source directory.
#
# Lambdas bundled:
#   api-escalate              — imports shared.rsa_client (LIFECYCLE_REQUESTED)
#   rsa-dispatch-consumer     — imports shared.rsa_client (constants + get_client)
#
# Deploy dependency: this script MUST run before `cdk synth` or `cdk deploy`
# for either of the two stacks above. Wire it into whatever Makefile/deploy
# target builds these Lambdas (see Group 5's docs/DEPLOYMENT.md task 5.3).
#
# Usage:
#   bash scripts/build-rsa-lambda.sh
#
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
BUILD_BASE="$ROOT/infrastructure/build"
LAMBDAS="$ROOT/lambdas"
SHARED="$LAMBDAS/shared"

bundle_rsa_lambda() {
  local name="$1"
  local OUT="$BUILD_BASE/$name"

  echo "Bundling $name → $OUT"
  rm -rf "$OUT"
  mkdir -p "$OUT"

  # Copy handler and any helper modules from the Lambda source directory.
  # Exclude the tests/ subdirectory (not needed at runtime).
  #
  # conftest.py is excluded too: it is pytest-only scaffolding (it puts the
  # lambdas/ root on sys.path so the source-tree tests can resolve
  # `shared.rsa_client`), it is never imported by the Lambda runtime, and
  # shipping it would put test scaffolding inside a deployment artifact —
  # against the same asset-hygiene rule that PYTHON_ASSET_EXCLUDES exists to
  # enforce for tests/ and __pycache__/ (see infrastructure/lib/python-lambda-asset.ts).
  # Excluded here rather than in PYTHON_ASSET_EXCLUDES so the blast radius stays
  # these two Lambdas instead of moving every Python asset hash in the app.
  rsync -a --exclude='tests/' --exclude='__pycache__/' --exclude='conftest.py' \
    "$LAMBDAS/$name/" "$OUT/"

  # Copy shared/rsa_client*.py into shared/ inside the build directory.
  # This is the key step that allows `from shared.rsa_client import ...`
  # to resolve at Lambda cold start.
  mkdir -p "$OUT/shared"
  cp "$SHARED/rsa_client.py"      "$OUT/shared/rsa_client.py"
  cp "$SHARED/rsa_client_fake.py" "$OUT/shared/rsa_client_fake.py"
  cp "$SHARED/rsa_client_real.py" "$OUT/shared/rsa_client_real.py"
  # Make shared/ a proper package.
  touch "$OUT/shared/__init__.py"

  # Verify the critical files are present — fail loudly here rather than at cold start.
  for required in handler.py shared/__init__.py shared/rsa_client.py \
      shared/rsa_client_fake.py shared/rsa_client_real.py; do
    [[ -f "$OUT/$required" ]] || {
      echo "  ERROR: $OUT/$required missing after staging" >&2
      exit 1
    }
  done

  echo "  bundled → $OUT"
}

mkdir -p "$BUILD_BASE"

bundle_rsa_lambda "api-escalate"
bundle_rsa_lambda "rsa-dispatch-consumer"

echo ""
echo "All RSA Lambda assets bundled under $BUILD_BASE"
echo "CDK Code.fromAsset() should reference infrastructure/build/api-escalate/"
echo "                                    infrastructure/build/rsa-dispatch-consumer/"
