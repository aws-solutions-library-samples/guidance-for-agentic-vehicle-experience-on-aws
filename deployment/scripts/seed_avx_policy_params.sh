#!/usr/bin/env bash
# seed_avx_policy_params.sh — Seed AVX broker + executor policy defaults into
# SSM Parameter Store for a given stage.
#
# Reads staging defaults from deployment/config/avx-policy-defaults.json and
# writes one SSM parameter per policy key using put_parameter --type String
# --overwrite so the script is idempotent (re-running never leaves the store
# in a partially-seeded state and never errors on existing keys).
#
# The per-executor keys (retry-policy, escalation) default to the 'dms' target
# as defined in the config file. Extend the TARGETS array below for additional
# executor targets once they exist.
#
# Usage:
#   # Dry-run — print the five keys that would be written, without writing them:
#   bash deployment/scripts/seed_avx_policy_params.sh --stage staging --dry-run
#
#   # Live seed:
#   bash deployment/scripts/seed_avx_policy_params.sh --stage staging
#
#   # With an explicit AWS profile:
#   bash deployment/scripts/seed_avx_policy_params.sh --stage staging --profile my-profile
#
# Exit codes:
#   0 — all parameters written (or dry-run completed)
#   1 — fatal error (bad args, config not found, AWS CLI not available)
#   2 — one or more put_parameter calls failed (partial write; re-run is safe)
#
# Non-interactive: no prompts. Idempotent: safe to re-run.
# Dry-run: --dry-run prints all five keys and their resolved values without
#   calling put_parameter. Use this to audit what will be written before
#   touching live SSM state.
#
# Note on fail-permissive distinction: this script populates the keys so the
# Lambda's cold-start SSM read succeeds. If the read still fails at runtime
# (transient AWS API error), that is a different code path — a retry, not
# a silent fallback to hardcoded defaults. Keeping the keys present eliminates
# the 'config absent' branch entirely; the broker does not need to distinguish
# it from a transient error after seeding.
#
# Source of truth: deployment/config/avx-policy-defaults.json

set -euo pipefail

# ── Colour helpers ─────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BOLD='\033[1m'; RESET='\033[0m'
log()  { echo -e "${BOLD}[seed_avx_policy_params]${RESET} $*"; }
ok()   { echo -e "${GREEN}[OK]${RESET} $*"; }
warn() { echo -e "${YELLOW}[WARN]${RESET} $*"; }
err()  { echo -e "${RED}[ERROR]${RESET} $*" >&2; }
dry()  { echo -e "${YELLOW}[DRY-RUN]${RESET} $*"; }

# ── Argument parsing ──────────────────────────────────────────────────────────
STAGE=""
DRY_RUN=false
AWS_PROFILE_ARG=""

usage() {
    echo "Usage: $0 --stage <stage> [--dry-run] [--profile <aws-profile>]"
    echo ""
    echo "  --stage     Required. Target stage name (e.g. staging)"
    echo "  --dry-run   Print the five SSM keys and values; do NOT write to SSM"
    echo "  --profile   Optional AWS CLI named profile"
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --stage)    STAGE="$2"; shift 2 ;;
        --dry-run)  DRY_RUN=true; shift ;;
        --profile)  AWS_PROFILE_ARG="--profile $2"; shift 2 ;;
        -h|--help)  usage ;;
        *)          err "Unknown argument: $1"; usage ;;
    esac
done

if [[ -z "${STAGE}" ]]; then
    err "--stage is required"
    usage
fi

# ── Locate repo root ───────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
CONFIG_FILE="${REPO_ROOT}/deployment/config/avx-policy-defaults.json"

if [[ ! -f "${CONFIG_FILE}" ]]; then
    err "Config file not found: ${CONFIG_FILE}"
    exit 1
fi

# ── Prerequisite checks ────────────────────────────────────────────────────────
if ! command -v python3 >/dev/null 2>&1; then
    err "python3 not found in PATH — required to parse config JSON"
    exit 1
fi

if [[ "${DRY_RUN}" == false ]]; then
    if ! command -v aws >/dev/null 2>&1; then
        err "aws CLI not found in PATH"
        exit 1
    fi
    # Verify credentials are resolvable before touching SSM
    # shellcheck disable=SC2086
    if ! aws sts get-caller-identity ${AWS_PROFILE_ARG} >/dev/null 2>&1; then
        err "AWS credentials check failed (aws sts get-caller-identity). Configure credentials before seeding."
        exit 1
    fi
fi

# ── Executor targets to seed per-target keys for ──────────────────────────────
# Extend this array when additional executor targets are added (e.g. cvx, fleet).
TARGETS=("dms")

# ── Build the list of (path, value) pairs to write ────────────────────────────
# We use python3 to parse the JSON config cleanly rather than shell sed/awk.
# Values are recursively stage-substituted: any string within a dict/list value
# that contains the literal '{stage}' token is replaced with the real stage
# name before JSON-encoding, so SSM receives fully-resolved values.
PAIRS_FILE="$(mktemp /tmp/avx_seed_pairs_XXXXXX.json)"
trap 'rm -f "${PAIRS_FILE}"' EXIT

python3 - <<PYEOF > "${PAIRS_FILE}"
import json, sys

stage = "${STAGE}"
targets = "${TARGETS[@]}".split()

def substitute_stage(obj, stage):
    """Recursively replace '{stage}' with the real stage name in all string leaves."""
    if isinstance(obj, str):
        return obj.replace("{stage}", stage)
    if isinstance(obj, dict):
        return {k: substitute_stage(v, stage) for k, v in obj.items()}
    if isinstance(obj, list):
        return [substitute_stage(v, stage) for v in obj]
    return obj

with open("${CONFIG_FILE}") as f:
    config = json.load(f)

pairs = []
for param in config["parameters"]:
    template = param["_key_template"]
    raw_default = param["staging_default"]
    provisional = param.get("_PROVISIONAL", "false")

    # Resolve '{stage}' in values (e.g. sns_topic_name inside a nested dict).
    default = substitute_stage(raw_default, stage)

    if "{target}" in template:
        for target in targets:
            path = template.replace("{stage}", stage).replace("{target}", target)
            if isinstance(default, (dict, list)):
                value = json.dumps(default, separators=(",", ":"))
            else:
                value = str(default)
            pairs.append({
                "path": path,
                "value": value,
                "provisional": provisional not in ("false", False),
                "description": param.get("description", ""),
            })
    else:
        path = template.replace("{stage}", stage)
        if isinstance(default, (dict, list)):
            value = json.dumps(default, separators=(",", ":"))
        else:
            value = str(default)
        pairs.append({
            "path": path,
            "value": value,
            "provisional": provisional not in ("false", False),
            "description": param.get("description", ""),
        })

print(json.dumps(pairs, indent=2))
PYEOF

PAIR_COUNT=$(python3 -c "import json; d=json.load(open('${PAIRS_FILE}')); print(len(d))")
log "Resolved ${PAIR_COUNT} SSM parameter(s) from config (stage=${STAGE})"

# ── Dry-run: print only ────────────────────────────────────────────────────────
if [[ "${DRY_RUN}" == true ]]; then
    dry "DRY-RUN MODE — no SSM writes will be performed"
    dry ""
    python3 - <<PYEOF
import json

with open("${PAIRS_FILE}") as f:
    pairs = json.load(f)

for p in pairs:
    prov = " [PROVISIONAL — pending user sign-off]" if p["provisional"] else ""
    print(f"  PATH  : {p['path']}")
    print(f"  VALUE : {p['value']}{prov}")
    print()
PYEOF
    dry "Would write ${PAIR_COUNT} parameter(s) — re-run without --dry-run to seed."
    exit 0
fi

# ── Live seed ──────────────────────────────────────────────────────────────────
# IMPORTANT: counters MUST live in the parent shell, not in a subshell.
# Using `command | while` runs the loop in a subshell on most bash versions,
# so any counter increments inside the loop are lost when the subshell exits.
# Instead we use process substitution `while ... done < <(command)` so the loop
# body runs in the CURRENT shell and can modify FAILURES and WRITTEN directly.

FAILURES=0
WRITTEN=0

# Build a newline-delimited list of JSON objects, one per pair.
LINES_CMD=(python3 -c "
import json, sys
with open('${PAIRS_FILE}') as f:
    pairs = json.load(f)
for p in pairs:
    print(json.dumps(p))
")

while IFS= read -r line; do
    PATH_VAL=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d['path'])" "$line")
    VALUE_VAL=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d['value'])" "$line")
    PROV=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print('1' if d['provisional'] else '0')" "$line")

    PROV_SUFFIX=""
    if [[ "${PROV}" == "1" ]]; then
        PROV_SUFFIX=" [PROVISIONAL — pending user sign-off]"
    fi

    log "Writing: ${PATH_VAL}${PROV_SUFFIX}"
    # shellcheck disable=SC2086
    if aws ssm put-parameter \
        --name "${PATH_VAL}" \
        --value "${VALUE_VAL}" \
        --type String \
        --overwrite \
        ${AWS_PROFILE_ARG} \
        --output text \
        --query 'Version' \
        >/dev/null 2>&1; then
        ok "  Wrote: ${PATH_VAL}"
        WRITTEN=$((WRITTEN + 1))
    else
        err "  FAILED: ${PATH_VAL}"
        FAILURES=$((FAILURES + 1))
    fi
done < <("${LINES_CMD[@]}")

if [[ "${FAILURES}" -gt 0 ]]; then
    err "${FAILURES} put-parameter call(s) failed — re-run is safe (put_parameter --overwrite is idempotent)"
    exit 2
fi

# ── Verification pass ──────────────────────────────────────────────────────────
# A second independent check: confirm every parameter is now readable.
# Uses process substitution for the same reason — MISSING must propagate to parent.
log "Verifying written parameters ..."
MISSING=0

while IFS= read -r param_path; do
    # shellcheck disable=SC2086
    if ! aws ssm get-parameter --name "${param_path}" ${AWS_PROFILE_ARG} --output text --query 'Parameter.Name' >/dev/null 2>&1; then
        err "Parameter not found after seed: ${param_path}"
        MISSING=$((MISSING + 1))
    fi
done < <(python3 -c "
import json
with open('${PAIRS_FILE}') as f:
    pairs = json.load(f)
for p in pairs:
    print(p['path'])
")

if [[ "${MISSING}" -gt 0 ]]; then
    err "${MISSING} parameter(s) missing after seed — re-run is safe (put_parameter --overwrite is idempotent)"
    exit 2
fi

ok "All ${PAIR_COUNT} AVX policy parameter(s) seeded for stage '${STAGE}'"
exit 0
