#!/usr/bin/env bash
# Fix Group 15 — CDK mutation harness (M-FG11-6a/6b, M-FG11-7a/7b).
#
# Applies each mutation to infrastructure/lib/vsa-tier2-stack.ts, rebuilds,
# runs the tier2 AVX jest suite, asserts it FAILS, then restores the file.
#
# Usage (from repo root):  bash scripts/fg15-cdk-mutations.sh
# Exit 0 only if every mutation was caught and the file was restored byte-identical.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STACK="$ROOT/infrastructure/lib/vsa-tier2-stack.ts"
BACKUP="$(mktemp -t fg15-stack)"
LOG="$(mktemp -t fg15-cdkmut)"

cp "$STACK" "$BACKUP"
trap 'cp "$BACKUP" "$STACK"; /bin/echo "[trap] stack restored"' EXIT

caught=(); missed=(); unapplied=()

run_suite() {
  ( cd "$ROOT/infrastructure" && npm run build >/dev/null 2>&1 \
      && npx jest test/vsa-tier2-stack-avx.test.ts ) > "$LOG" 2>&1
  return $?
}

# $1=id  $2=description  $3=python-escaped old  $4=python-escaped new
mutate() {
  local id="$1" desc="$2" old="$3" new="$4"
  /bin/echo ""
  /bin/echo "=== $id — $desc"
  cp "$BACKUP" "$STACK"
  if ! OLD="$old" NEW="$new" STACK="$STACK" python3 - <<'PY'
import os, sys, pathlib
p = pathlib.Path(os.environ["STACK"])
s = p.read_text()
old, new = os.environ["OLD"], os.environ["NEW"]
if s.count(old) != 1:
    print(f"  !! anchor count={s.count(old)} (need exactly 1)")
    sys.exit(1)
p.write_text(s.replace(old, new, 1))
PY
  then
    /bin/echo "  UNAPPLIED"
    unapplied+=("$id")
    return
  fi

  if run_suite; then
    /bin/echo "  NOT CAUGHT — suite stayed green"
    missed+=("$id")
  else
    /bin/echo "  caught: $(grep -E '^Tests:' "$LOG" | tail -1)"
    grep -E '✕' "$LOG" | head -4 | sed 's/^/    /'
    caught+=("$id")
  fi
  cp "$BACKUP" "$STACK"
}

/bin/echo "### pre-flight: suite must be green before mutating"
if run_suite; then
  /bin/echo "pre-flight green: $(grep -E '^Tests:' "$LOG" | tail -1)"
else
  /bin/echo "PRE-FLIGHT FAILED — aborting"; grep -E '✕|Tests:' "$LOG" | head; exit 2
fi

mutate "M-FG11-6a" "wildcard resource on CmsVehiclesOwnerGetItem" \
  "      resources: [cmsVehiclesTableArn],
    }));" \
  "      resources: ['*'],
    }));"

mutate "M-FG11-6b" "dynamodb:* catch-all on CmsVehiclesOwnerGetItem" \
  "      actions: ['dynamodb:GetItem'],
      // Narrow: the table ARN only." \
  "      actions: ['dynamodb:*'],
      // Narrow: the table ARN only."

mutate "M-FG11-7a" "dynamodb:Scan added to CmsVehiclesOwnerGetItem" \
  "      actions: ['dynamodb:GetItem'],
      // Narrow: the table ARN only." \
  "      actions: ['dynamodb:GetItem', 'dynamodb:Scan'],
      // Narrow: the table ARN only."

mutate "M-FG11-7b" "vin-index Query re-pointed at the table instead of the index" \
  "      resources: [\`\${cmsVehiclesTableArn}/index/vin-index\`]," \
  "      resources: [cmsVehiclesTableArn],"

/bin/echo ""
/bin/echo "======================================================"
/bin/echo "caught     : ${#caught[@]}  ${caught[*]:-}"
/bin/echo "NOT caught : ${#missed[@]}  ${missed[*]:-}"
/bin/echo "unapplied  : ${#unapplied[@]}  ${unapplied[*]:-}"

cp "$BACKUP" "$STACK"
if cmp -s "$BACKUP" "$STACK"; then /bin/echo "stack restored byte-identical"; else /bin/echo "RESTORE FAILED"; exit 1; fi

/bin/echo "### post-restore suite"
if run_suite; then
  /bin/echo "post-restore green: $(grep -E '^Tests:' "$LOG" | tail -1)"
else
  /bin/echo "POST-RESTORE RED"; exit 1
fi

[ "${#missed[@]}" -eq 0 ] && [ "${#unapplied[@]}" -eq 0 ]
