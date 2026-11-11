#!/usr/bin/env bash
# Pre-bundle the api-vehicle-live-state Lambda asset without Docker.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
OUT="$ROOT/infrastructure/build/api-vehicle-live-state"
# The requirements this bundle was built from, kept BESIDE the asset directory so it
# doesn't ship or change the asset hash. Synth compares it with the source's
# requirements.txt (infrastructure/lib/staged-asset-check.ts); it is written only after
# a successful build, so a failed or interrupted build leaves none and synth refuses.
STAMP="$OUT.requirements.txt"

rm -f "$STAMP"
rm -rf "$OUT"
mkdir -p "$OUT"

cp "$ROOT/lambdas/api-vehicle-live-state/handler.py" "$OUT/"
cp -R "$ROOT/lambdas/shared" "$OUT/shared"

# --python-version/--implementation pin wheel selection to the Lambda runtime
# (PYTHON_3_12), not to the host interpreter. Without them a 3.14 host bundled
# _cffi_backend.cpython-314 into the 3.12 authorizer
# (issues/2026-10-01-ws-authorizer-bundle-built-for-python-3-14/).
python3 -m pip install -q --platform manylinux2014_x86_64 --only-binary=:all: \
  --python-version 3.12 --implementation cp \
  --target "$OUT" -r "$ROOT/lambdas/api-vehicle-live-state/requirements.txt" >/dev/null

rm -rf "$OUT"/*.dist-info "$OUT"/bin "$OUT"/__pycache__ "$OUT"/shared/__pycache__

# Refuse a bundle the Lambda cannot load (wrong CPython tag, or not Linux x86-64), and
# delete it, so a stale bad bundle cannot be picked up by `cdk deploy` or a code push.
python3 "$ROOT/scripts/lib/check-bundle-abi.py" "$OUT" --python 3.12 \
  || { echo "ERROR: removed $OUT so it cannot be deployed" >&2; rm -rf "$OUT"; exit 1; }

cp "$ROOT/lambdas/api-vehicle-live-state/requirements.txt" "$STAMP"
echo "bundled -> $OUT"
