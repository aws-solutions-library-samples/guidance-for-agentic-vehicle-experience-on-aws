#!/usr/bin/env bash
# Pre-bundle AVX Lambda assets so each deployment package contains
# its own copy of lambdas/_lib/avx_scope.py alongside handler.py.
#
# Problem solved: `lambdas/_lib/` is a sibling directory to
# `lambdas/api-avx-*/`. Code.fromAsset() bundles only the Lambda's
# own directory, so the `from _lib.avx_scope import ...` at Lambda
# cold start fails with `No module named '_lib'`.
#
# Solution (mirrors build-triage-classifier.sh precedent):
#   For each AVX Lambda that imports from _lib, create a build directory
#   at `infrastructure/build/api-avx-{name}/` containing:
#     handler.py          — the Lambda entry point
#     requirements.txt    — runtime dependencies
#     _lib/avx_scope.py   — shared scope library
#   CDK Code.fromAsset() in vsa-api-stack.ts then references each
#   `infrastructure/build/api-avx-{name}/` directory instead of the
#   source directory.
#
# Lambdas bundled:
#   api-avx-findings              — imports _lib.avx_scope
#   api-avx-actions               — imports _lib.avx_scope
#   api-avx-notifications-register-device — no _lib import (standalone)
#
# Usage:
#   bash scripts/build-avx-lambda.sh
#   (called by CDK via a bundling hook, or run manually before deploy)
#
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
BUILD_BASE="$ROOT/infrastructure/build"
LAMBDAS="$ROOT/lambdas"

bundle_lambda() {
  local name="$1"         # e.g. api-avx-findings
  local use_lib="${2:-1}" # 1=copy _lib/avx_scope.py, 0=skip
  local OUT="$BUILD_BASE/$name"

  echo "Bundling $name → $OUT"
  rm -rf "$OUT"
  mkdir -p "$OUT"

  # Copy handler and any helper modules from the Lambda source directory.
  # Exclude the tests/ subdirectory (not needed at runtime).
  rsync -a --exclude='tests/' --exclude='__pycache__/' \
    "$LAMBDAS/$name/" "$OUT/"

  # Copy shared _lib/avx_scope.py into _lib/ inside the build directory.
  # This is the key step that allows `from _lib.avx_scope import ...`
  # to resolve at Lambda cold start, matching the sys.path.insert in handler.py.
  if [[ "$use_lib" == "1" ]]; then
    mkdir -p "$OUT/_lib"
    cp "$LAMBDAS/_lib/avx_scope.py" "$OUT/_lib/avx_scope.py"
    # Ensure the _lib package is importable
    touch "$OUT/_lib/__init__.py"
  fi

  # Install runtime dependencies (if requirements.txt exists and is non-empty).
  if [[ -f "$OUT/requirements.txt" ]] && [[ -s "$OUT/requirements.txt" ]]; then
    echo "  Installing Python dependencies for $name..."
    python3 -m pip install -q --platform manylinux2014_x86_64 --only-binary=:all: \
      --target "$OUT" -r "$OUT/requirements.txt" >/dev/null
    # Remove boto3 and other Lambda-runtime-provided packages.
    rm -rf "$OUT"/{boto3,botocore,s3transfer,jmespath,dateutil,urllib3,six.py}
    rm -rf "$OUT"/{boto3,botocore,s3transfer,jmespath,python_dateutil,urllib3,six}-*.dist-info
    rm -rf "$OUT"/bin "$OUT"/__pycache__
    find "$OUT" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
  else
    echo "  No runtime dependencies for $name (requirements.txt empty or absent)."
  fi

  echo "  bundled → $OUT"
}

bundle_executors() {
  # Stage the `agents/executors/` package into a Lambda's build directory.
  #
  # Step-5 prerequisite 2 of 5. `agents/executors/` was packaged into NOTHING —
  # `bundle_lambda` above copies only `lambdas/<name>/` plus `_lib/avx_scope.py`, and
  # `handler.py`'s own comment says it: "agents/executors/ is packaged into nothing —
  # zero references in infrastructure/lib/*.ts or the Makefile". So the 7 executors
  # Group 5 shipped and reviewed could never be imported by a deployed artifact.
  #
  # Opt-in per Lambda rather than blanket, because only the approve route dispatches.
  # Shipping the package into read-only Lambdas would add import surface and a
  # dependency they have no use for.
  #
  # `agents/__init__.py` is copied too: without it `agents` is not a package on the
  # Lambda's sys.path and `from agents.executors.registry import dispatch` fails at
  # cold start — the same "No module named" shape as the `_lib` problem this script
  # was written for, and the `redis` incident before that.
  #
  # tests/ is excluded for the same reason bundle_lambda excludes it: not needed at
  # runtime, and it pulls fixture files into the image.
  local name="$1"
  local OUT="$BUILD_BASE/$name"
  [[ -d "$OUT" ]] || { echo "  ERROR: $OUT does not exist — call bundle_lambda first" >&2; return 1; }

  echo "  staging agents/executors → $name"
  mkdir -p "$OUT/agents"
  # Package marker for `agents` itself. Copied from source when present so any
  # module-level content is preserved rather than replaced with an empty file.
  if [[ -f "$ROOT/agents/__init__.py" ]]; then
    cp "$ROOT/agents/__init__.py" "$OUT/agents/__init__.py"
  else
    touch "$OUT/agents/__init__.py"
  fi
  rsync -a --exclude='tests/' --exclude='__pycache__/' \
    "$ROOT/agents/executors/" "$OUT/agents/executors/"

  # Fail loudly here rather than at cold start. A missing registry is the whole
  # failure mode this function exists to prevent, so it is asserted, not assumed.
  for required in agents/__init__.py agents/executors/__init__.py agents/executors/registry.py; do
    [[ -f "$OUT/$required" ]] || { echo "  ERROR: $OUT/$required missing after staging" >&2; return 1; }
  done
}

mkdir -p "$BUILD_BASE"

# Lambdas that import from _lib (use_lib=1)
bundle_lambda "api-avx-findings"  1
bundle_lambda "api-avx-actions"   1

# The approve route in api-avx-findings dispatches to the executor registry, so it —
# and only it — carries the executor package. See bundle_executors for why opt-in.
bundle_executors "api-avx-findings"

# api-avx-notifications-register-device has no _lib import (standalone)
bundle_lambda "api-avx-notifications-register-device" 0

# api-avx-dms-event-consumer (Spec 2 T5.4) has no _lib import.
# EventBridge-triggered (not API Gateway), so it does not need avx_scope.py.
bundle_lambda "api-avx-dms-event-consumer" 0

echo ""
echo "All AVX Lambda assets bundled under $BUILD_BASE"
echo "CDK Code.fromAsset() should reference infrastructure/build/api-avx-*/"
