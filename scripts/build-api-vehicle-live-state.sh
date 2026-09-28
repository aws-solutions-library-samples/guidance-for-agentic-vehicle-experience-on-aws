#!/usr/bin/env bash
# Pre-bundle the api-vehicle-live-state Lambda asset without Docker.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
OUT="$ROOT/infrastructure/build/api-vehicle-live-state"

rm -rf "$OUT"
mkdir -p "$OUT"

cp "$ROOT/lambdas/api-vehicle-live-state/handler.py" "$OUT/"
cp -R "$ROOT/lambdas/shared" "$OUT/shared"

python3 -m pip install -q --platform manylinux2014_x86_64 --only-binary=:all: \
  --target "$OUT" -r "$ROOT/lambdas/api-vehicle-live-state/requirements.txt" >/dev/null

rm -rf "$OUT"/*.dist-info "$OUT"/bin "$OUT"/__pycache__ "$OUT"/shared/__pycache__

echo "bundled -> $OUT"
