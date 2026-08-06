#!/usr/bin/env bash
#
# scripts/demo-preflight.sh — one-command pre-demo readiness check.
#
# Read-only, non-interactive. Runs the staging checks codified in
# `docs/DEMO-RUNBOOK.md` § 4 (Pre-flight checklist) and prints a
# GREEN/RED/WARN readout per check, then an overall verdict.
#
# Hard checks (exit non-zero on RED):
#   - vsa-staging-* CFN stack status (us-west-2)
#   - bidi runtime status == READY (us-west-2)
#   - text-agent smoke (scripts/smoke-test-agentcore.py)
#   - persona Tier-3 smoke (pytest evals/runner/tier3_e2e.py -k persona_smoke --staging)
#
# Soft checks (WARN, do not flip overall verdict):
#   - KB-retrieve smoke (must cite ≥1 s3:// source; non-zero exit IS hard-fail,
#     missing s3:// citation is a WARN)
#   - iOS Staging.xcconfig back-fill (out of scope for this repo; trivially
#     detected via the sibling CMS repo if present)
#
# The bidi runtime id and ARN are resolved automatically from
# `.bedrock_agentcore.yaml` (no hardcoded ids) — the runbook uses a placeholder.
#
# Usage:
#   make demo-preflight           # standard invocation
#   bash scripts/demo-preflight.sh
#
# Required env / credentials:
#   - AWS credentials with read access in us-west-2 (CFN, AgentCore control)
#     and us-east-1 (KB Retrieve).
#   - VSA_USER_TOKEN (or a cached ~/.vsa/tokens.json from prior login) — the
#     text-agent and persona smokes need a Cognito JWT to invoke the runtime.
#   - VSA_ADP_KB_ID (default: YULHRAUMNU) — the ADP vehicle KB id.
#
# Exit codes: 0 = all hard checks GREEN. 1 = any hard check RED.

set -u -o pipefail

# ── Repo root resolution (works regardless of cwd) ────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# Use the project venv's python if present (matches the Makefile pattern); fall
# back to system python3 otherwise. Tools imported by the smokes (boto3, yaml,
# websockets, pytest) live in .venv.
if [ -x "$REPO_ROOT/.venv/bin/python3" ]; then
  PYTHON="$REPO_ROOT/.venv/bin/python3"
else
  PYTHON="python3"
fi

# ── Color (TTY only) ──────────────────────────────────────────────────────
if [ -t 1 ]; then
  C_HEAD=$'\033[1;36m'   # bold cyan
  C_OK=$'\033[0;32m'     # green
  C_WARN=$'\033[0;33m'   # yellow
  C_CRIT=$'\033[1;31m'   # red
  C_DIM=$'\033[2m'       # dim
  C_RESET=$'\033[0m'
else
  C_HEAD=""; C_OK=""; C_WARN=""; C_CRIT=""; C_DIM=""; C_RESET=""
fi

# ── State ─────────────────────────────────────────────────────────────────
HARD_FAIL=0
WARN_COUNT=0
declare -a CHECK_ROWS=()  # one human-readable line per check, accumulated

# ── Helpers ───────────────────────────────────────────────────────────────
record_green() {
  CHECK_ROWS+=("${C_OK}GREEN${C_RESET}  $1")
}
record_red() {
  CHECK_ROWS+=("${C_CRIT}RED${C_RESET}    $1")
  HARD_FAIL=1
}
record_warn() {
  CHECK_ROWS+=("${C_WARN}WARN${C_RESET}   $1")
  WARN_COUNT=$((WARN_COUNT + 1))
}
record_note() {
  CHECK_ROWS+=("${C_DIM}NOTE${C_RESET}   $1")
}

print_header() {
  echo "${C_HEAD}━━━ CVX demo preflight @ $(date '+%Y-%m-%d %H:%M %Z') ━━━${C_RESET}"
  echo "${C_DIM}Repo: $REPO_ROOT${C_RESET}"
  echo "${C_DIM}Python: $PYTHON${C_RESET}"
  echo ""
}

# Resolve a sub-key from .bedrock_agentcore.yaml without hardcoding shell yaml
# parsing. Returns empty string on miss; never errors.
yaml_get() {
  local key="$1"  # e.g. agents.vsa_supervisor_bidi_staging.bedrock_agentcore.agent_id
  "$PYTHON" - <<PY 2>/dev/null || true
import sys, yaml, pathlib
p = pathlib.Path("$REPO_ROOT/.bedrock_agentcore.yaml")
if not p.exists():
    sys.exit(0)
d = yaml.safe_load(p.read_text()) or {}
for part in "$key".split("."):
    if not isinstance(d, dict) or part not in d:
        sys.exit(0)
    d = d[part]
print(d if d is not None else "")
PY
}

# ── Pre-flight: AWS creds reachable? (gate before any AWS calls) ──────────
require_aws_creds() {
  if ! aws sts get-caller-identity --output text --query Account >/dev/null 2>&1; then
    record_red "AWS credentials unreachable — \`aws sts get-caller-identity\` failed. Configure credentials and retry."
    return 1
  fi
  return 0
}

# ── Check 1: vsa-staging-* CFN stack status (us-west-2) ───────────────────
check_cfn_stacks() {
  local out
  if ! out=$(aws cloudformation describe-stacks \
      --region us-west-2 \
      --query "Stacks[?starts_with(StackName,\`vsa-staging-\`)].[StackName,StackStatus]" \
      --output text 2>/dev/null); then
    record_red "CFN stacks (us-west-2): describe-stacks call failed (auth or region issue)"
    return
  fi

  if [ -z "${out//[[:space:]]/}" ]; then
    record_red "CFN stacks (us-west-2): no vsa-staging-* stacks found — has the staging deploy ever succeeded?"
    return
  fi

  local total=0 bad=0 bad_names=""
  while IFS=$'\t' read -r name status; do
    [ -z "$name" ] && continue
    total=$((total + 1))
    case "$status" in
      *_COMPLETE)
        ;;
      *)
        bad=$((bad + 1))
        bad_names="$bad_names $name=$status"
        ;;
    esac
  done <<< "$out"

  if [ "$bad" -eq 0 ]; then
    record_green "CFN stacks (us-west-2): $total stack(s) all *_COMPLETE"
  else
    record_red "CFN stacks (us-west-2): $bad/$total NOT *_COMPLETE —$bad_names"
  fi
}

# ── Check 2: bidi runtime status == READY (us-west-2) ─────────────────────
# Resolves the runtime id from .bedrock_agentcore.yaml's agent_id (preferred,
# avoids any hardcoded id). Falls back to extracting it from agent_arn if
# agent_id is unset. Exports RUNTIME_ID for the persona smoke check below.
check_bidi_runtime() {
  RUNTIME_ID="$(yaml_get agents.vsa_supervisor_bidi_staging.bedrock_agentcore.agent_id)"
  if [ -z "$RUNTIME_ID" ]; then
    local arn
    arn="$(yaml_get agents.vsa_supervisor_bidi_staging.bedrock_agentcore.agent_arn)"
    RUNTIME_ID="${arn##*/}"  # last path segment after the final /
  fi
  if [ -z "$RUNTIME_ID" ]; then
    record_red "Bidi runtime: could not resolve runtime id from .bedrock_agentcore.yaml (deploy hasn't run, or yaml is missing the staging agent)"
    return
  fi

  local status
  if ! status=$(aws bedrock-agentcore-control get-agent-runtime \
      --agent-runtime-id "$RUNTIME_ID" \
      --region us-west-2 \
      --query 'status' \
      --output text 2>/dev/null); then
    record_red "Bidi runtime ($RUNTIME_ID, us-west-2): get-agent-runtime call failed"
    return
  fi

  if [ "$status" = "READY" ]; then
    record_green "Bidi runtime ($RUNTIME_ID, us-west-2): status=READY"
    export RUNTIME_ID
  else
    record_red "Bidi runtime ($RUNTIME_ID, us-west-2): status=$status (expected READY)"
  fi
}

# ── Check 3: text-agent smoke ─────────────────────────────────────────────
# Runs the supervisor in-process (the same path as `python -m
# agents.supervisor.cli` — the demo's text CLI surface) against the
# deployed REST /triage endpoint, and asserts the deterministic
# classifier stamps a P0–P3 onto SessionContext. Output is captured to a
# temp file so callers can diagnose without re-running. We do NOT override
# --prompt here — the smoke's default ("Temperature gauge just spiked.")
# is the correct triage-triggering input; an idle status query like
# "preflight readiness check" would not trip the supervisor's tool-router
# and the classification assertion would always fail.
check_text_smoke() {
  local log
  log="$(mktemp -t cvx-preflight-text.XXXXXX)"
  if "$PYTHON" "$REPO_ROOT/scripts/smoke-test-agentcore.py" \
      >"$log" 2>&1; then
    record_green "Text-agent smoke: PASS  (log: $log)"
  else
    record_red "Text-agent smoke: FAIL  (log: $log — first error line below)"
    local first_err
    first_err=$(grep -m1 -E '^(✗|Error|Traceback)' "$log" || tail -n1 "$log")
    [ -n "$first_err" ] && CHECK_ROWS+=("       ${C_DIM}↳ $first_err${C_RESET}")
  fi
}

# ── Check 4: KB-retrieve smoke ────────────────────────────────────────────
# Hard requirement: script exits zero (proves the IAM grant works).
# Soft requirement: stdout contains an s3:// citation (proves live KB content,
# not local-corpora fallback). Missing s3:// → WARN.
check_kb_smoke() {
  local log
  log="$(mktemp -t cvx-preflight-kb.XXXXXX)"
  if "$PYTHON" "$REPO_ROOT/scripts/smoke-test-kb-retrieve.py" \
      --stage staging >"$log" 2>&1; then
    if grep -q 's3://' "$log"; then
      local cite
      cite=$(grep -m1 -oE 's3://[^[:space:]]+' "$log")
      record_green "KB-retrieve smoke: PASS, cited $cite  (log: $log)"
    else
      record_warn "KB-retrieve smoke: ran clean but NO s3:// citation — possible local-corpora fallback (check VSA_ADP_KB_ID env in runtime).  (log: $log)"
    fi
  else
    record_red "KB-retrieve smoke: FAIL  (log: $log)"
    local first_err
    first_err=$(grep -m1 -E '^(✗|Error|Traceback)' "$log" || tail -n1 "$log")
    [ -n "$first_err" ] && CHECK_ROWS+=("       ${C_DIM}↳ $first_err${C_RESET}")
  fi
}

# ── Check 5: persona Tier-3 smoke ─────────────────────────────────────────
# Skips cleanly if the bidi-runtime check above did not export RUNTIME_ID
# (i.e. yaml didn't resolve). pytest skips the persona suite without it.
check_persona_smoke() {
  if [ -z "${RUNTIME_ID:-}" ]; then
    record_red "Persona Tier-3 smoke: skipped — RUNTIME_ID could not be resolved (see Bidi runtime check above)"
    return
  fi
  local log
  log="$(mktemp -t cvx-preflight-persona.XXXXXX)"
  # AWS_REGION/VSA_TENANT_ID match the runbook's documented invocation. We pass
  # them inline so they don't bleed into the caller's shell.
  if AWS_REGION=us-west-2 VSA_TENANT_ID="${VSA_TENANT_ID:-michelin}" RUNTIME_ID="$RUNTIME_ID" \
      "$PYTHON" -m pytest "$REPO_ROOT/evals/runner/tier3_e2e.py" \
        -k persona_smoke --staging -v --no-header --tb=line \
        >"$log" 2>&1; then
    local summary
    summary=$(grep -E '^=+.*(passed|failed|error)' "$log" | tail -n1 | sed 's/=//g' | xargs)
    record_green "Persona Tier-3 smoke: PASS — ${summary:-pytest exit 0}  (log: $log)"
  else
    local summary
    summary=$(grep -E '^=+.*(passed|failed|error)' "$log" | tail -n1 | sed 's/=//g' | xargs)
    record_red "Persona Tier-3 smoke: FAIL — ${summary:-pytest non-zero exit}  (log: $log)"
  fi
}

# ── Soft note: iOS Staging.xcconfig back-fill (out of scope) ──────────────
# Trivial detection only when the sibling CMS repo is present at the standard
# path. We do NOT block on this — the runbook explicitly notes voice/iOS as a
# manual back-fill step (CMS issue 2026-05-28-backfill-staging-xcconfig) and
# this script is CVX-scoped.
check_ios_xcconfig_note() {
  local cms_xcconfig="$HOME/connected-mobility-guidance-on-aws/clients/ios/VSACompanion/Config/Staging.xcconfig"
  if [ ! -f "$cms_xcconfig" ]; then
    record_note "iOS Staging.xcconfig: not checked (sibling CMS repo not at $cms_xcconfig). Voice demo readiness is out of scope; verify manually if demoing voice."
    return
  fi
  # Cheap placeholder grep — common patterns from the spec docs.
  if grep -qE 'PASTE_|PLACEHOLDER|TODO|<.+>' "$cms_xcconfig"; then
    record_warn "iOS Staging.xcconfig: appears to contain placeholder values — voice demo will likely fail. See CMS issue 2026-05-28-backfill-staging-xcconfig."
  else
    record_note "iOS Staging.xcconfig: present and contains no obvious placeholder strings (still verify in-app connectivity before demoing voice)."
  fi
}

# ── Main ──────────────────────────────────────────────────────────────────
print_header

if ! require_aws_creds; then
  # Print whatever rows we have and bail — every other check needs AWS creds.
  echo ""
  for row in "${CHECK_ROWS[@]}"; do echo " $row"; done
  echo ""
  echo "${C_CRIT}OVERALL: RED (AWS credentials missing).${C_RESET}"
  exit 1
fi

check_cfn_stacks
check_bidi_runtime
check_text_smoke
check_kb_smoke
check_persona_smoke
check_ios_xcconfig_note

# ── Readout ───────────────────────────────────────────────────────────────
echo "${C_HEAD}Checks${C_RESET}"
for row in "${CHECK_ROWS[@]}"; do
  echo " $row"
done
echo ""

if [ "$HARD_FAIL" -ne 0 ]; then
  echo "${C_CRIT}OVERALL: RED — at least one hard check failed. NOT demo-ready.${C_RESET}"
  if [ "$WARN_COUNT" -gt 0 ]; then
    echo "${C_DIM}        (also $WARN_COUNT warning(s) above.)${C_RESET}"
  fi
  exit 1
fi

if [ "$WARN_COUNT" -gt 0 ]; then
  echo "${C_OK}OVERALL: GREEN${C_RESET} ${C_WARN}with $WARN_COUNT warning(s)${C_RESET} — demo-ready, but review warnings."
else
  echo "${C_OK}OVERALL: GREEN — all hard checks passed. Demo-ready.${C_RESET}"
fi
exit 0
