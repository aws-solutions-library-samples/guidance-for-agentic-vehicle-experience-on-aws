#!/usr/bin/env bash
# test-rsa-max-age-guard.sh
#
# Standalone test of the CVX_RSA_POSITION_MAX_AGE_SECONDS guard logic used in
# `_deploy-cdk`.
#
# F7.4 (spec 2026-09-23-cvx-rsa-stub-dispatch): tests the shell predicate in
# isolation so a guard defect cannot fall through into an actual deploy.
#
# Usage:
#   bash scripts/test-rsa-max-age-guard.sh
#
# Exit 0 if all tests pass. Exit 1 if any test fails.

set -euo pipefail

PASS=0
FAIL=0

# Reproduce the exact guard logic from _deploy-cdk in Makefile (F7.4, Fix Group 13).
# Empty is allowed (bin/vsa.ts defaults to 900 via ||).
# Non-empty must be a whole number from 1 to 3600 (escalation Fix Group 13: a
# huge value turns the stale-position check off, like nan/inf).
#   Returns 0 — guard passes, deploy may proceed.
#           1 — guard rejects, deploy must NOT proceed.
REGEX='^[1-9][0-9]{0,3}$'
CEILING=3600
guard() {
  local value="$1"
  if [ -n "$value" ] && { ! printf '%s' "$value" | grep -qE "$REGEX" || ! [ "$value" -le "$CEILING" ]; }; then
    return 1
  fi
  return 0
}

# The copy above must be the Makefile's logic, not a drifted twin.
MAKEFILE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/Makefile"
for needle in "grep -qE '^[1-9][0-9]{0,3}\$\$'" '"$$CVX_RSA_POSITION_MAX_AGE_SECONDS" -le 3600'; do
  if ! grep -qF -- "$needle" "$MAKEFILE"; then
    echo "  FAIL  Makefile no longer contains: $needle (update this test and the Makefile together)"
    exit 1
  fi
done

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

echo "=== CVX_RSA_POSITION_MAX_AGE_SECONDS guard logic tests ==="
echo ""

# ── empty: MUST pass (safe default 900 activates in bin/vsa.ts) ─────────────
expect_pass   "empty string"       ""

# ── valid positive integers: MUST pass ───────────────────────────────────────
expect_pass   "900 (default)"      "900"
expect_pass   "1 (minimum)"        "1"
expect_pass   "3600 (ceiling)"     "3600"

# ── above the ceiling: MUST reject (Fix Group 13) ────────────────────────────
expect_reject "3601"               "3601"
expect_reject "86400 (1 day)"      "86400"
expect_reject "1000000000"         "1000000000"
expect_reject "overflow-length"    "99999999999999999999999"
expect_reject "scientific 1e9"     "1e9"
expect_reject "nan"                "nan"
expect_reject "inf"                "inf"

# ── non-numeric: MUST reject ─────────────────────────────────────────────────
expect_reject "alphabetic"         "abc"
expect_reject "alphanumeric"       "900s"
expect_reject "float"              "900.5"
expect_reject "empty-ish spaces"   " "
expect_reject "negative (-1)"      "-1"
expect_reject "zero (0)"           "0"
expect_reject "leading zero"       "0900"   # octal-ambiguous and not ^[1-9]
expect_reject "leading plus"       "+900"

echo ""
echo "=== Results: ${PASS} passed, ${FAIL} failed ==="

[ "$FAIL" -eq 0 ]
