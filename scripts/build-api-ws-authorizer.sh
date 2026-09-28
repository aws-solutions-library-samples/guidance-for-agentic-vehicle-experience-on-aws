#!/usr/bin/env bash
# Pre-bundle the ws-authorizer Lambda asset without Docker.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
OUT="$ROOT/infrastructure/build/api-ws-authorizer"

rm -rf "$OUT"
mkdir -p "$OUT"

cp -R "$ROOT/lambdas/api-ws-authorizer/"* "$OUT/"

python3 -m pip install -q --platform manylinux2014_x86_64 --only-binary=:all: \
  --target "$OUT" -r "$ROOT/lambdas/api-ws-authorizer/requirements.txt" >/dev/null

rm -rf "$OUT"/*.dist-info "$OUT"/bin "$OUT"/__pycache__

echo "bundled -> $OUT"
