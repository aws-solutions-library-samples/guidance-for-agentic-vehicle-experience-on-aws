#!/usr/bin/env bash
# Pre-bundle the triage-classifier Lambda asset without Docker.
# Output: infrastructure/build/triage-classifier/ ready for Code.fromAsset().
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
OUT="$ROOT/infrastructure/build/triage-classifier"

# Refuse to bundle a severity table that under-grades a fault: a code missing from
# lambdas/shared/dtc_severity.yaml classifies P2 ("safe to drive"), which is how the
# fire and crash codes shipped as P2 until 2026-09-26
# (issues/2026-09-26-classifier-grades-fire-and-crash-codes-p2/). Offline, pyyaml only.
# Runs before the old bundle is removed, so a failed check leaves it in place rather
# than an empty directory that `cdk deploy` would ship as empty Lambda code.
PY="$ROOT/.venv/bin/python3"
if [ ! -x "$PY" ]; then
  echo "note: $PY not found; running the severity check with python3 on PATH (needs pyyaml)" >&2
  PY=python3
fi
"$PY" "$ROOT/scripts/check-dtc-severity-parity.py"

rm -rf "$OUT"
mkdir -p "$OUT"

# Copy code: shared/ as-is, triage-classifier/ renamed to triage_classifier.
cp -R "$ROOT/lambdas/shared" "$OUT/shared"
cp -R "$ROOT/lambdas/triage-classifier" "$OUT/triage_classifier"
rm -rf "$OUT/triage_classifier/tests" "$OUT/shared/__pycache__" "$OUT/triage_classifier/__pycache__"

# Install runtime dependencies into the asset root so Lambda can import them.
# --python-version/--implementation pin wheel selection to the Lambda runtime
# (PYTHON_3_12 in infrastructure/lib/triage-classifier-stack.ts), not to the
# host interpreter. Without them a 3.14 host fails on pinned pyyaml==6.0.2 (no
# cp314 wheel), and the 2026-09-22 bundle shipped a cp314 _yaml.so that a 3.12
# Lambda cannot load.
python3 -m pip install -q --platform manylinux2014_x86_64 --only-binary=:all: \
  --python-version 3.12 --implementation cp \
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
