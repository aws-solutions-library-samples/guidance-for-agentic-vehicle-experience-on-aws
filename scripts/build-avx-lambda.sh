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
# AVX_BUILD_BASE: tests build into a temp dir (tests/architecture/test_bundle_handler_cold_start.py).
BUILD_BASE="${AVX_BUILD_BASE:-$ROOT/infrastructure/build}"
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
  # AVX_BUILD_SKIP_PIP=1 skips it: the cold-start tests import with the venv's
  # packages and must not depend on the network.
  if [[ "${AVX_BUILD_SKIP_PIP:-0}" != "1" ]] && [[ -f "$OUT/requirements.txt" ]] && [[ -s "$OUT/requirements.txt" ]]; then
    echo "  Installing Python dependencies for $name..."
    # --python-version/--implementation pin wheels to the Lambda runtime (PYTHON_3_12),
    # not the host. Without them the 3.14 host bundled charset_normalizer
    # cpython-314 .so files into api-avx-findings, which worked only through
    # its pure-Python fallback
    # (issues/2026-10-01-ws-authorizer-bundle-built-for-python-3-14/).
    python3 -m pip install -q --platform manylinux2014_x86_64 --only-binary=:all: \
      --python-version 3.12 --implementation cp \
      --target "$OUT" -r "$OUT/requirements.txt" >/dev/null
    # Remove boto3 and other Lambda-runtime-provided packages.
    rm -rf "$OUT"/{boto3,botocore,s3transfer,jmespath,dateutil,urllib3,six.py}
    rm -rf "$OUT"/{boto3,botocore,s3transfer,jmespath,python_dateutil,urllib3,six}-*.dist-info
    rm -rf "$OUT"/bin "$OUT"/__pycache__
    find "$OUT" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
  else
    echo "  No runtime dependencies for $name (requirements.txt empty or absent)."
  fi

  # Refuse a bundle the Lambda cannot load (wrong CPython tag, or not Linux x86-64), and
  # delete it, so a stale bad bundle cannot be picked up by `cdk deploy`.
  python3 "$ROOT/scripts/lib/check-bundle-abi.py" "$OUT" --python 3.12 \
    || { echo "ERROR: removed $OUT so it cannot be deployed" >&2; rm -rf "$OUT"; exit 1; }

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

  # Stage the action.schema.json that policy.py reads at import time.
  #
  # C1 fix (review-g2-core.md 2026-10-02): policy.py computes its schema path as
  #   pathlib.Path(__file__).resolve().parents[2] / "agents/tier2/schemas/action.schema.json"
  # parents[2] of agents/executors/policy.py inside the bundle is the bundle root,
  # so the schema must live at $OUT/agents/tier2/schemas/action.schema.json.
  # Without it every Lambda that imports policy (findings, actions, approval-rules,
  # auto-approver) raises FileNotFoundError at cold start before serving anything.
  mkdir -p "$OUT/agents/tier2/schemas"
  cp "$ROOT/agents/tier2/schemas/action.schema.json" \
     "$OUT/agents/tier2/schemas/action.schema.json"

  # Fail loudly here rather than at cold start. A missing registry is the whole
  # failure mode this function exists to prevent, so it is asserted, not assumed.
  for required in agents/__init__.py agents/executors/__init__.py agents/executors/registry.py \
                  agents/tier2/schemas/action.schema.json; do
    [[ -f "$OUT/$required" ]] || { echo "  ERROR: $OUT/$required missing after staging" >&2; return 1; }
  done
}

bundle_policy() {
  # Stage the action-kind policy table, failure_reason builder, and the schema it reads
  # at import.
  #
  # For Lambdas that classify Actions by kind but never dispatch (api-avx-actions
  # needs the FI kinds for its iOS exclusion; api-avx-approval-rules reads
  # auto_approvable via _is_auto_approvable). `agents/executors/__init__.py` is a
  # docstring only, and policy.py imports nothing outside the standard library, so
  # this adds no `requests` dependency and no executor import surface; the executor
  # package stays opt-in for the dispatching Lambdas (tests/test_avx_build_path.py).
  #
  # failure_reason.py is staged here (F1.1 C1): api-avx-actions/handler.py imports it
  # at module scope (`from agents.executors.failure_reason import failure_reason`) for
  # the PATCH→declined audit entry.  Like policy.py, it has no third-party imports.
  local name="$1"
  local OUT="$BUILD_BASE/$name"
  [[ -d "$OUT" ]] || { echo "  ERROR: $OUT does not exist — call bundle_lambda first" >&2; return 1; }

  echo "  staging agents/executors/policy.py + failure_reason.py → $name"
  mkdir -p "$OUT/agents/executors" "$OUT/agents/tier2/schemas"
  if [[ -f "$ROOT/agents/__init__.py" ]]; then
    cp "$ROOT/agents/__init__.py" "$OUT/agents/__init__.py"
  else
    touch "$OUT/agents/__init__.py"
  fi
  cp "$ROOT/agents/executors/__init__.py" "$OUT/agents/executors/__init__.py"
  cp "$ROOT/agents/executors/policy.py" "$OUT/agents/executors/policy.py"
  cp "$ROOT/agents/executors/failure_reason.py" "$OUT/agents/executors/failure_reason.py"
  cp "$ROOT/agents/tier2/schemas/action.schema.json" "$OUT/agents/tier2/schemas/action.schema.json"
  for required in agents/__init__.py agents/executors/__init__.py agents/executors/policy.py \
                  agents/executors/failure_reason.py \
                  agents/tier2/schemas/action.schema.json; do
    [[ -f "$OUT/$required" ]] || { echo "  ERROR: $OUT/$required missing after staging" >&2; return 1; }
  done
}

bundle_run_records() {
  # Stage agents/tier2/run_records.py into a Lambda's build directory.
  #
  # For Lambdas that write run records lazily (avx-auto-approver imports
  # agents.tier2.run_records inside _write_run_record at line ~368).
  # bundle_executors already creates agents/tier2/schemas/ but does not copy
  # run_records.py or agents/tier2/__init__.py; without them the import raises
  # ModuleNotFoundError at the end of every processed batch, even when dispatch()
  # succeeds.
  #
  # run_records.py imports only stdlib (logging, os, typing) so this adds no
  # third-party dependency surface beyond what bundle_executors already stages.
  local name="$1"
  local OUT="$BUILD_BASE/$name"
  [[ -d "$OUT" ]] || { echo "  ERROR: $OUT does not exist — call bundle_lambda first" >&2; return 1; }

  echo "  staging agents/tier2/run_records.py → $name"
  mkdir -p "$OUT/agents/tier2"
  # Package marker (empty in source). Copied explicitly so agents.tier2 is a
  # regular package, not a namespace package, inside the bundle.
  cp "$ROOT/agents/tier2/__init__.py" "$OUT/agents/tier2/__init__.py"
  cp "$ROOT/agents/tier2/run_records.py" "$OUT/agents/tier2/run_records.py"
  for required in agents/tier2/__init__.py agents/tier2/run_records.py; do
    [[ -f "$OUT/$required" ]] || { echo "  ERROR: $OUT/$required missing after staging" >&2; return 1; }
  done
}

bundle_write_hook() {
  # Stage agents/fi_quality/write.py and agents/tier2/writer.py into a Lambda's
  # build directory.
  #
  # Required by api-avx-investigations: handler.py imports
  # agents.fi_quality.write at module scope (INVESTIGATION_ID_RE,
  # is_live_investigation, stale-age constants), which transitively imports
  # agents.tier2.writer at its own module scope (compute_finding_key).
  # Without these files the handler raises ModuleNotFoundError at cold start.
  #
  # agents/tier2/__init__.py is copied so agents.tier2 is a regular package.
  local name="$1"
  local OUT="$BUILD_BASE/$name"
  [[ -d "$OUT" ]] || { echo "  ERROR: $OUT does not exist — call bundle_lambda first" >&2; return 1; }

  echo "  staging agents/fi_quality/write.py + agents/tier2/writer.py → $name"
  mkdir -p "$OUT/agents/fi_quality" "$OUT/agents/tier2"
  if [[ -f "$ROOT/agents/__init__.py" ]]; then
    cp "$ROOT/agents/__init__.py" "$OUT/agents/__init__.py"
  else
    touch "$OUT/agents/__init__.py"
  fi
  cp "$ROOT/agents/fi_quality/__init__.py" "$OUT/agents/fi_quality/__init__.py"
  cp "$ROOT/agents/fi_quality/write.py" "$OUT/agents/fi_quality/write.py"
  cp "$ROOT/agents/tier2/__init__.py" "$OUT/agents/tier2/__init__.py"
  cp "$ROOT/agents/tier2/writer.py" "$OUT/agents/tier2/writer.py"
  for required in agents/__init__.py agents/fi_quality/__init__.py agents/fi_quality/write.py \
                  agents/tier2/__init__.py agents/tier2/writer.py; do
    [[ -f "$OUT/$required" ]] || { echo "  ERROR: $OUT/$required missing after staging" >&2; return 1; }
  done
}

bundle_issue_hook() {
  # Stage agents/fi_quality/ and agents/tier2/writer.py into a Lambda's build
  # directory.
  #
  # Required by api-avx-findings for the on_decision_recorded hook: after
  # registry.py's lazy-import fix, _make_adp_hook._hook imports
  # agents.fi_quality.issue at hook-call time (# noqa: PLC0415), which
  # transitively imports agents.tier2.writer at module scope (compute_finding_key).
  # Without these files the hook raises ModuleNotFoundError when the first
  # record_engineering_decision Action is approved.
  #
  # agents/tier2/__init__.py is copied too so agents.tier2 is a regular package.
  # bundle_executors already creates agents/tier2/schemas/ for the action schema;
  # this step adds writer.py alongside it.
  #
  # avx-auto-approver intentionally receives nothing here: it never dispatches
  # engineering Action kinds (TOKEN_FREE_KINDS = {"acknowledge_only"}) so the
  # hook is never called and agents.fi_quality is never imported at runtime.
  local name="$1"
  local OUT="$BUILD_BASE/$name"
  [[ -d "$OUT" ]] || { echo "  ERROR: $OUT does not exist — call bundle_lambda first" >&2; return 1; }

  echo "  staging agents/fi_quality + agents/tier2/writer.py → $name"
  mkdir -p "$OUT/agents/fi_quality" "$OUT/agents/tier2"
  cp "$ROOT/agents/fi_quality/__init__.py" "$OUT/agents/fi_quality/__init__.py"
  cp "$ROOT/agents/fi_quality/issue.py" "$OUT/agents/fi_quality/issue.py"
  # agents/tier2/__init__.py — package marker (empty in source).
  cp "$ROOT/agents/tier2/__init__.py" "$OUT/agents/tier2/__init__.py"
  cp "$ROOT/agents/tier2/writer.py" "$OUT/agents/tier2/writer.py"
  for required in agents/fi_quality/__init__.py agents/fi_quality/issue.py \
                  agents/tier2/__init__.py agents/tier2/writer.py; do
    [[ -f "$OUT/$required" ]] || { echo "  ERROR: $OUT/$required missing after staging" >&2; return 1; }
  done
}

mkdir -p "$BUILD_BASE"

# Lambdas that import from _lib (use_lib=1)
bundle_lambda "api-avx-findings"  1
bundle_lambda "api-avx-actions"   1

# Only api-avx-findings dispatches (its approve route calls the executor registry), so
# only it gets the executor package. bundle_executors also stages
# agents/tier2/schemas/action.schema.json, which policy.py reads at import
# (Group 2 review, core C1).
bundle_executors "api-avx-findings"

# api-avx-findings' on_decision_recorded hook lazily imports agents.fi_quality.issue
# (registry.py _make_adp_hook, # noqa: PLC0415), which transitively imports
# agents.tier2.writer at module scope. Stage both packages so the hook can run.
bundle_issue_hook "api-avx-findings"

# api-avx-actions imports ACTION_KINDS from agents.executors.policy at module scope,
# but never dispatches: policy table and schema only (Group 2 review, core C1).
bundle_policy "api-avx-actions"

# api-avx-notifications-register-device has no _lib import (standalone)
bundle_lambda "api-avx-notifications-register-device" 0

# api-avx-dms-event-consumer (Spec 2 T5.4) has no _lib import.
# EventBridge-triggered (not API Gateway), so it does not need avx_scope.py.
bundle_lambda "api-avx-dms-event-consumer" 0

# api-avx-approval-rules: imports _lib.avx_scope at module scope (fleet scope check)
# and agents.executors.policy lazily inside _is_auto_approvable (line ~139).
# Policy-only — the handler never calls registry.dispatch(); no executor import surface
# is needed here.
bundle_lambda "api-avx-approval-rules" 1
bundle_policy "api-avx-approval-rules"

# avx-auto-approver (Spec D9): dispatches via agents.executors.registry inside
# _process_record (lines ~213-214) and writes run records via agents.tier2.run_records
# inside _write_run_record (line ~368). Both imports are lazy (# noqa: PLC0415), so
# `import handler` alone passes even when the packages are absent — the first batch
# invocation fails instead. Must carry both packages.
bundle_lambda "avx-auto-approver" 0
bundle_executors "avx-auto-approver"
bundle_run_records "avx-auto-approver"

# api-avx-investigations: imports _lib.avx_scope + agents.fi_quality.write at module scope;
# write.py imports agents.tier2.writer at module scope (compute_finding_key).
bundle_lambda "api-avx-investigations" 1
bundle_write_hook "api-avx-investigations"

bundle_fi_sim_operator() {
  # Stage agents/fi_sim_operator/ into a Lambda's build directory.
  #
  # Required by avx-sim-operator/handler.py which imports agents.fi_sim_operator.*
  # at module scope (config, select, pairing, plan, run).  Without this package the
  # handler raises ModuleNotFoundError at cold start before serving anything.
  #
  # agents/__init__.py is copied (or created) for the same reason bundle_executors
  # copies it: the `agents` namespace package must be a regular package inside the
  # bundle's sys.path so `from agents.fi_sim_operator.config import ...` resolves.
  local name="$1"
  local OUT="$BUILD_BASE/$name"
  [[ -d "$OUT" ]] || { echo "  ERROR: $OUT does not exist — call bundle_lambda first" >&2; return 1; }

  echo "  staging agents/fi_sim_operator → $name"
  mkdir -p "$OUT/agents/fi_sim_operator"
  if [[ -f "$ROOT/agents/__init__.py" ]]; then
    cp "$ROOT/agents/__init__.py" "$OUT/agents/__init__.py"
  else
    touch "$OUT/agents/__init__.py"
  fi
  rsync -a --exclude='tests/' --exclude='__pycache__/' \
    "$ROOT/agents/fi_sim_operator/" "$OUT/agents/fi_sim_operator/"
  for required in agents/__init__.py agents/fi_sim_operator/__init__.py; do
    [[ -f "$OUT/$required" ]] || { echo "  ERROR: $OUT/$required missing after staging" >&2; return 1; }
  done
}

# avx-sim-operator (D10): scheduled demo-fleet decision Lambda.
# Dispatches via agents.executors.registry, writes run records via agents.tier2.run_records,
# and reads selection/planning logic from agents.fi_sim_operator.
bundle_lambda "avx-sim-operator" 0
bundle_executors "avx-sim-operator"
bundle_run_records "avx-sim-operator"
bundle_fi_sim_operator "avx-sim-operator"

echo ""
echo "All AVX Lambda assets bundled under $BUILD_BASE"
echo "CDK Code.fromAsset() should reference infrastructure/build/api-avx-*/"
