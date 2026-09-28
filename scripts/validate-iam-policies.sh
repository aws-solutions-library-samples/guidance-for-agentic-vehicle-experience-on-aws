#!/usr/bin/env bash
#
# validate-iam-policies.sh — run IAM Access Analyzer policy validation over the
# IAM policy documents in a synthesised CloudFormation template.
#
# WHY THIS EXISTS, and what it does that the other two guards do not.
#
# This repo now has three layers of IAM checking, and they catch different things.
# Adding this one was a deliberate decision (2026-09-20) after review Cycle 3's
# Critical C1, where a hand-rolled template walker was defeated for the third
# cycle running — nine guard defeats total, twice inside a guard written to fix the
# previous defeat:
#
#   1. cdk-nag (AwsSolutionsChecks, an Aspect in bin/vsa.ts, asserted in
#      infrastructure/test/vsa-api-stack-avx.test.ts) — a MAINTAINED rule pack over
#      the construct tree. Sees managed policies, inline policies and grants alike.
#      Catches wildcards (IAM5) and AWS-managed policies (IAM4).
#
#   2. The two-way correspondence guard (same test file) — repo-specific, and the
#      only thing that checks "this action is granted but no bundled code calls it"
#      and "this code calls but nothing grants it". No cdk-nag rule expresses that.
#
#   3. THIS SCRIPT — AWS's own policy grammar and semantics. Access Analyzer knows
#      things neither of the above models: actions that do not exist, action/resource
#      type mismatches (a grant that can never match anything), overly-permissive
#      principals, and policies that fail AWS's own linting. It is checking the
#      policy as AWS will evaluate it, not as we hope we wrote it.
#
# Non-interactive by design; safe to run in CI. Read-only — ValidatePolicy does not
# touch account state.
#
# USAGE
#   scripts/validate-iam-policies.sh --template <path-to-template.json> [--region <r>]
#   scripts/validate-iam-policies.sh --stack vsa-staging-api [--region <r>]
#
#   --template  A synthesised template JSON (e.g. infrastructure/cdk.out/vsa-staging-api.template.json)
#   --stack     Fetch the deployed template for a live stack instead
#
# EXIT CODES
#   0  no ERROR or SECURITY_WARNING findings
#   1  findings at ERROR or SECURITY_WARNING severity (details printed)
#   2  usage / prerequisite problem
#
set -euo pipefail

REGION="${AWS_REGION:-us-west-2}"
TEMPLATE=""
STACK=""

while [ $# -gt 0 ]; do
  case "$1" in
    --template) TEMPLATE="${2:-}"; shift 2 ;;
    --stack)    STACK="${2:-}";    shift 2 ;;
    --region)   REGION="${2:-}";   shift 2 ;;
    -h|--help)  sed -n '1,48p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$TEMPLATE" ] && [ -z "$STACK" ]; then
  echo "error: one of --template or --stack is required" >&2
  exit 2
fi
command -v aws >/dev/null 2>&1 || { echo "error: aws CLI not found" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "error: python3 not found" >&2; exit 2; }
aws sts get-caller-identity >/dev/null 2>&1 || {
  echo "error: AWS credentials not configured (ValidatePolicy is an API call)" >&2; exit 2; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if [ -n "$STACK" ]; then
  echo "→ fetching deployed template for stack $STACK ($REGION)"
  aws cloudformation get-template --stack-name "$STACK" --region "$REGION" \
    --query 'TemplateBody' --output json > "$WORK/template.json"
  TEMPLATE="$WORK/template.json"
fi

[ -f "$TEMPLATE" ] || { echo "error: template not found: $TEMPLATE" >&2; exit 2; }

# Extract every identity-policy document: standalone policies, managed policies, and
# role inline policies. All three shapes are enumerated on purpose — reading only
# AWS::IAM::Policy is precisely the blind spot that let C1 through five tests.
python3 - "$TEMPLATE" "$WORK" <<'PY'
import json, sys, pathlib

template_path, workdir = sys.argv[1], pathlib.Path(sys.argv[2])
raw = json.load(open(template_path))
# `get-template` wraps the body; a synthesised file is the template itself.
tpl = raw if "Resources" in raw else json.loads(raw) if isinstance(raw, str) else raw
resources = tpl.get("Resources", {})

docs = []
for logical_id, res in resources.items():
    rtype = res.get("Type")
    props = res.get("Properties", {}) or {}
    if rtype in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
        pd = props.get("PolicyDocument")
        if pd:
            docs.append((logical_id, pd))
    elif rtype == "AWS::IAM::Role":
        for i, p in enumerate(props.get("Policies", []) or []):
            pd = p.get("PolicyDocument")
            if pd:
                docs.append((f"{logical_id}/inline[{i}]", pd))

# CloudFormation intrinsics (Ref / Fn::Join / Fn::GetAtt) are not valid IAM JSON.
# Substitute a syntactically valid placeholder ARN so Access Analyzer can parse the
# document and still reason about actions, resource shapes and structure.
PLACEHOLDER = "arn:aws:iam::123456789012:role/CfnIntrinsicPlaceholder"

def deintrinsify(node):
    if isinstance(node, dict):
        if len(node) == 1 and next(iter(node)) in (
            "Ref", "Fn::Join", "Fn::GetAtt", "Fn::Sub", "Fn::ImportValue",
            "Fn::Select", "Fn::Split", "Fn::If",
        ):
            return PLACEHOLDER
        return {k: deintrinsify(v) for k, v in node.items()}
    if isinstance(node, list):
        return [deintrinsify(v) for v in node]
    return node

index = []
for i, (name, pd) in enumerate(docs):
    cleaned = deintrinsify(pd)
    out = workdir / f"policy_{i}.json"
    out.write_text(json.dumps(cleaned))
    index.append({"name": name, "file": str(out)})

(workdir / "index.json").write_text(json.dumps(index, indent=2))
print(f"→ extracted {len(index)} IAM policy document(s)")
PY

COUNT=$(python3 -c "import json;print(len(json.load(open('$WORK/index.json'))))")
if [ "$COUNT" -eq 0 ]; then
  # Fail loudly rather than reporting success on an empty set — a zero-document run
  # looks identical to a clean run, which is how a vacuous guard passes forever.
  echo "error: no IAM policy documents found in $TEMPLATE — refusing to report success" >&2
  exit 2
fi

FAILED=0
ERRORED=0
while read -r name file; do
  # Capture status and output separately. An earlier version ended this pipeline with
  # `|| echo '[]'`, which treated an API failure as "no findings" — so an unreachable
  # region, expired credentials or a throttle reported ✓ / PASS / exit 0. That is the
  # same fail-open shape this repo spent the day removing from the handlers (F4: a
  # failed Query swallowed by `except Exception` and degraded to a full-table scan),
  # reintroduced in the guard meant to catch such things. A validator that cannot reach
  # its validator must fail, not pass.
  set +e
  raw=$(aws accessanalyzer validate-policy \
      --policy-document "file://$file" \
      --policy-type IDENTITY_POLICY \
      --region "$REGION" \
      --output json 2>&1)
  status=$?
  set -e
  if [ "$status" -ne 0 ]; then
    ERRORED=$((ERRORED + 1))
    echo "✗ $name — ValidatePolicy call FAILED (exit $status); NOT treated as clean:"
    echo "$raw" | head -3 | sed 's/^/      /'
    continue
  fi
  findings=$(python3 - "$raw" <<'PY'
import json, sys
doc = json.loads(sys.argv[1])
sev = {"ERROR", "SECURITY_WARNING"}
print(json.dumps([f for f in doc.get("findings", []) if f.get("findingType") in sev]))
PY
  )
  n=$(python3 -c "import json,sys;print(len(json.loads(sys.argv[1])))" "$findings")
  if [ "$n" -gt 0 ]; then
    FAILED=$((FAILED + n))
    echo "✗ $name — $n ERROR/SECURITY_WARNING finding(s):"
    python3 - "$findings" <<'PY'
import json, sys
for f in json.loads(sys.argv[1]):
    print(f"    [{f.get('findingType')}] {f.get('issueCode')}: {f.get('findingDetails')}")
PY
  else
    echo "✓ $name"
  fi
done < <(python3 -c "
import json
for e in json.load(open('$WORK/index.json')):
    print(e['name'], e['file'])
")

echo
if [ "$ERRORED" -gt 0 ]; then
  echo "FAIL — $ERRORED policy document(s) could not be validated (API call failed)."
  echo "This is NOT a pass. The validator could not reach Access Analyzer, so nothing"
  echo "about these policies has been established either way."
  exit 1
fi
if [ "$FAILED" -gt 0 ]; then
  echo "FAIL — $FAILED ERROR/SECURITY_WARNING finding(s) across $COUNT policy document(s)."
  echo "Access Analyzer validates against AWS's own policy semantics; a finding here is"
  echo "a real defect or a pattern needing a documented exception, not a linting nit."
  exit 1
fi
echo "PASS — $COUNT policy document(s), no ERROR or SECURITY_WARNING findings."
