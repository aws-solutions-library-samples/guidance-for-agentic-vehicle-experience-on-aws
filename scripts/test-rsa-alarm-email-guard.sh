#!/usr/bin/env bash
# test-rsa-alarm-email-guard.sh
#
# Standalone test of the CVX_RSA_ALARM_EMAIL guard logic used in `_deploy-cdk`.
#
# F6.6 (spec 2026-09-23-cvx-rsa-stub-dispatch): tests the shell predicate in
# isolation so a guard defect cannot fall through into an actual deploy.
# `make -n <target>` confirms the recipe parses; this script confirms the
# logic is correct for all four inputs the Makefile documents.
#
# Usage:
#   bash scripts/test-rsa-alarm-email-guard.sh
#
# Exit 0 if all tests pass. Exit 1 if any test fails.

set -euo pipefail

PASS=0
FAIL=0

# Reproduce the exact guard logic from _deploy-cdk in Makefile.
#   Returns 0 (guard passes — deploy may proceed),
#           1 (guard rejects — deploy must NOT proceed).
guard() {
  local email="$1"
  if [ -z "$email" ]; then
    return 1
  fi
  if [ "$email" != "none" ] && ! printf '%s' "$email" | grep -qE '^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$'; then
    return 1
  fi
  return 0
}

expect_pass() {
  local label="$1" input="$2"
  if guard "$input"; then
    echo "  PASS  [$label] guard accepted '$input'"
    PASS=$((PASS + 1))
  else
    echo "  FAIL  [$label] guard rejected '$input' — expected PASS"
    FAIL=$((FAIL + 1))
  fi
}

expect_reject() {
  local label="$1" input="$2"
  if guard "$input"; then
    echo "  FAIL  [$label] guard accepted '$input' — expected REJECT"
    FAIL=$((FAIL + 1))
  else
    echo "  PASS  [$label] guard rejected '$input'"
    PASS=$((PASS + 1))
  fi
}

echo "=== CVX_RSA_ALARM_EMAIL guard logic tests ==="
echo ""

# ── empty / blank: MUST reject ─────────────────────────────────────────────
expect_reject "empty string"       ""
expect_reject "whitespace only"    "   "   # intentionally not trimmed (guard uses -z)

# ── 'none': MUST pass (explicit opt-out) ───────────────────────────────────
expect_pass   "literal none"       "none"

# ── valid email: MUST pass ─────────────────────────────────────────────────
expect_pass   "simple email"       "operator@example.com"
expect_pass   "subdomain email"    "you@team.example.com"
expect_pass   "plus-address"       "you+rsa@example.com"

# ── malformed: MUST reject ─────────────────────────────────────────────────
expect_reject "no-at"              "notanemail"
expect_reject "missing domain"     "user@"
expect_reject "missing user"       "@domain.com"
expect_reject "spaces inside"      "user @domain.com"
expect_reject "double at"          "user@@domain.com"
expect_reject "no tld"             "user@domain"
expect_reject "NONE uppercase"     "NONE"      # only lowercase 'none' is accepted
expect_reject "None mixedcase"     "None"

echo ""
echo "=== Results: ${PASS} passed, ${FAIL} failed ==="

[ "$FAIL" -eq 0 ]
