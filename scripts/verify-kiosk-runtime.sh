#!/usr/bin/env bash
#
# Verify that the Zone 2 kiosk guardrail actually LANDED on a deployed AgentCore
# text runtime — not merely that the deploy resolved it.
#
# WHY THIS EXISTS
# ---------------
# On 2026-08-02 the staging text runtime ran with no VSA_KIOSK_GUARDRAIL_ID and
# no AGENTCORE_STAGE. The Bedrock Guardrail was deployed and correctly
# configured; nothing threaded its id onto the runtime. Because AGENTCORE_STAGE
# was also unset, KioskGuardrailEnforcer's fail-closed branch never engaged, so
# instead of returning 500 and making the gap obvious it silently disabled
# itself. Every request returned HTTP 200 and the system prompt's own deflection
# masked the missing content policy on an unattended public kiosk.
#
# The deploy-time guards in the Makefile prove the id was RESOLVED from the
# stack. This script proves it LANDED on the runtime, and that the two agree.
# Those are different failures.
#
# See issues/2026-08-02-text-runtime-env-unguarded-and-kiosk-guardrail-unwired/
#
# Exits non-zero on any check failure, so it is safe to chain into a deploy or a
# cron. Read-only: performs no mutations.
#
# Race note (review Cycle 7): `list-exports` and `get-agent-runtime` read
# eventually-consistent control-plane state, so running this while a stack update
# or an `agentcore deploy` is still in flight can observe a missing export or a
# pre-update runtime environment. Every such case is treated as a FAILURE rather
# than a pass, which is the safe direction — a spurious "not ready yet" costs a
# re-run, whereas a spurious pass would green-light public traffic against an
# unguarded kiosk. Run it after the deploy completes, not alongside it.
set -euo pipefail

STAGE="staging"
REGION="us-west-2"
RUNTIME_ID=""

while [ $# -gt 0 ]; do
  case "$1" in
    --stage)      STAGE="$2"; shift 2 ;;
    --region)     REGION="$2"; shift 2 ;;
    --runtime-id) RUNTIME_ID="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,30p' "$0"
      exit 0 ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2 ;;
  esac
done

if [ -z "$RUNTIME_ID" ]; then
  echo "❌ --runtime-id is required (e.g. vsa_supervisor_text_staging-yxYZomEgFz)" >&2
  exit 2
fi

fail=0
ok()   { printf '  ✅ %s\n' "$1"; }
bad()  { printf '  ❌ %s\n' "$1"; fail=1; }

echo "🔍 Kiosk runtime verification — stage=$STAGE region=$REGION runtime=$RUNTIME_ID"

runtime_env() {
  aws bedrock-agentcore-control get-agent-runtime \
    --agent-runtime-id "$RUNTIME_ID" --region "$REGION" \
    --query "environmentVariables.$1" --output text 2>/dev/null || echo ""
}

# --- 1. The guardrail id must be present on the runtime -----------------------
RUNTIME_GUARDRAIL="$(runtime_env VSA_KIOSK_GUARDRAIL_ID)"
if [ -z "$RUNTIME_GUARDRAIL" ] || [ "$RUNTIME_GUARDRAIL" = "None" ]; then
  bad "VSA_KIOSK_GUARDRAIL_ID is missing on the runtime — the kiosk guardrail
     enforces NOTHING and every request still returns 200. Do NOT open public
     traffic. Redeploy via 'make deploy-staging' / 'make deploy' rather than
     hand-patching, so the deploy path stays the single source of truth."
else
  ok "VSA_KIOSK_GUARDRAIL_ID present: $RUNTIME_GUARDRAIL"
fi

# --- 2. It must match what the stack actually deployed ------------------------
STACK_GUARDRAIL="$(aws cloudformation list-exports --region "$REGION" \
  --query "Exports[?Name=='vsa-${STAGE}-kiosk-guardrail-id'].Value" \
  --output text 2>/dev/null || echo "")"
if [ -z "$STACK_GUARDRAIL" ] || [ "$STACK_GUARDRAIL" = "None" ]; then
  bad "export vsa-${STAGE}-kiosk-guardrail-id not found — is vsa-${STAGE}-core deployed?"
elif [ "$STACK_GUARDRAIL" != "$RUNTIME_GUARDRAIL" ]; then
  bad "runtime guardrail '$RUNTIME_GUARDRAIL' != stack export '$STACK_GUARDRAIL'
     — the runtime is pinned to a stale or foreign guardrail, so the policy being
     enforced is not the policy that was reviewed."
else
  ok "runtime guardrail matches the stack export"
fi

# --- 3. AGENTCORE_STAGE must be set, or fail-closed never engages -------------
RUNTIME_STAGE="$(runtime_env AGENTCORE_STAGE)"
if [ -z "$RUNTIME_STAGE" ] || [ "$RUNTIME_STAGE" = "None" ]; then
  bad "AGENTCORE_STAGE is unset. KioskGuardrailEnforcer only fails closed on a
     recognised deployed stage; unset lands in the dev denylist and DISABLES
     enforcement silently. This is the second half of the 2026-08-02 defect."
elif [ "$RUNTIME_STAGE" != "$STAGE" ]; then
  bad "AGENTCORE_STAGE='$RUNTIME_STAGE' but this runtime was verified as '$STAGE'"
else
  ok "AGENTCORE_STAGE present: $RUNTIME_STAGE"
fi

# --- 4. Two env vars that shipped as the literal string "None" once -----------
for var in VSA_API_BASE VSA_FOUNDATION_MEMORY_ID; do
  val="$(runtime_env "$var")"
  if [ -z "$val" ] || [ "$val" = "None" ]; then
    bad "$var is '$val' on the runtime — the deploy resolved it to a literal
     'None' string. Fixed once already; the deploy guards should prevent it."
  else
    ok "$var present"
  fi
done

echo
if [ "$fail" -ne 0 ]; then
  echo "❌ Kiosk runtime verification FAILED — do not open public kiosk traffic."
  exit 1
fi

echo "✅ Kiosk runtime verification passed."
echo "   Reminder: this proves CONFIGURATION only. It does not prove the"
echo "   guardrail BLOCKS. Confirm enforcement with a live off-topic prompt AND a"
echo "   legitimate positive control — a guardrail that blocks everything looks"
echo "   identical to a working one if you only test the negative case."
echo "   See docs/DEPLOYMENT.md § Guardrail post-deploy verification."
