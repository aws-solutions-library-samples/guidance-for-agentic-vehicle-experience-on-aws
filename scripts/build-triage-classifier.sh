#!/usr/bin/env bash
# Pre-bundle the triage-classifier Lambda asset without Docker.
# Output: infrastructure/build/triage-classifier/ ready for Code.fromAsset().
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
OUT="$ROOT/infrastructure/build/triage-classifier"

rm -rf "$OUT"
mkdir -p "$OUT"

# Copy code: shared/ as-is, triage-classifier/ renamed to triage_classifier.
cp -R "$ROOT/lambdas/shared" "$OUT/shared"
cp -R "$ROOT/lambdas/triage-classifier" "$OUT/triage_classifier"
rm -rf "$OUT/triage_classifier/tests" "$OUT/shared/__pycache__" "$OUT/triage_classifier/__pycache__"

# Install runtime dependencies into the asset root so Lambda can import them.
python3 -m pip install -q --platform manylinux2014_x86_64 --only-binary=:all: \
  --target "$OUT" -r "$ROOT/lambdas/triage-classifier/requirements.txt" >/dev/null

# Remove boto3 if pip pulled it in — Lambda runtime provides it.
rm -rf "$OUT"/{boto3,botocore,s3transfer,jmespath,dateutil,urllib3,six.py}
rm -rf "$OUT"/{boto3,botocore,s3transfer,jmespath,python_dateutil,urllib3,six,pyyaml,redis,async_timeout}-*.dist-info
# Strip test-only deps — fakeredis is NOT a runtime dep.
rm -rf "$OUT"/{fakeredis,sortedcontainers,lupa}
rm -rf "$OUT"/{fakeredis,sortedcontainers,lupa}-*.dist-info
rm -rf "$OUT"/bin "$OUT"/__pycache__
# Remove tests dirs from our own modules.
find "$OUT" -type d -name tests -exec rm -rf {} + 2>/dev/null || true

echo "bundled -> $OUT"
