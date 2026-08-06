# Eval Pipeline Runbook

## Overview

The CVX eval pipeline is a tiered regression detector that runs automatically on every PR and before production deploys. It combines three tiers of increasing fidelity (and cost) to catch regressions before they reach production:

- **Tier 1** (tool unit evals) verifies each tool factory in isolation with mocked services — <2s, $0.
- **Tier 2** (mocked-Nova conversation) replays scripted conversations to verify routing and tool sequencing — <2s, $0.
- **Tier 3** (real AgentCore E2E) streams audio/text to a deployed runtime to verify end-to-end performance — ~10min, ~$1/run.

This runbook explains how to run, interpret, and troubleshoot each tier.

## Running Locally

### Tier 1 — Tool Unit Evals

```bash
make eval-tier1
```

**Output**: A Markdown summary table comparing against the baseline (`evals/baselines/tier1.json`).

```
# Tier 1 Tool Unit Evals — Regression Report

| Case ID | Status | Latency (ms) | Notes |
|---------|--------|--------------|-------|
| triage-happy-001 | ✅ PASSED | 45.2 | — |
| vehicle-identity-vin-decode-001 | ✅ PASSED | 78.5 | — |
| knowledge-tsb-lookup-001 | ✅ PASSED | 32.1 | — |
| ...
```

**Expected output**: 24 cases, all PASSED. If any PASSED case from the baseline now shows FAILED, the pipeline exits non-zero with a regression report.

### Tier 2 — Mocked-Nova Conversation Evals

```bash
make eval-tier2
```

**Output**: Markdown summary table for routing and safety cases.

```
# Tier 2 Conversation Evals — Regression Report

| Case ID | Status | Routing | Safety | Latency (ms) |
|---------|--------|---------|--------|--------------|
| tire-pressure-low-001 | ✅ PASSED | routing_match | — | 128.3 |
| brake-diy-refusal-001 | ✅ PASSED | — | escalate | 95.6 |
| ...
```

**Expected output**: 18 cases, all PASSED. Routing cases verify the agent selects the correct tool sequence. Safety cases verify the agent refuses DIY guidance for safety-critical repairs.

### Tier 3 — Real AgentCore E2E

Tier 3 requires a deployed AgentCore runtime in your dev account.

```bash
export AWS_REGION=us-east-1
export RUNTIME_ID=<your-runtime-id>
make eval-tier3
```

**Output**: Markdown summary table with E2E results and p99 latencies.

```
# Tier 3 E2E Evals — Regression Report

| Case ID | Status | Latency p99 (ms) | Notes |
|---------|--------|------------------|-------|
| audio-tire-pressure-001 | ✅ PASSED | 8234.5 | — |
| text-brake-warning-001 | ✅ PASSED | 6890.2 | — |
| ...
```

**Expected output**: 5 cases, all PASSED. If `RUNTIME_ID` is not set, all cases are skipped (gracefully).

### All Tiers

```bash
make eval-all
```

Runs Tier 1, Tier 2, then Tier 3 (if `RUNTIME_ID` is set). Exits non-zero if ANY tier has a regression.

## Interpreting the Report

### Status Columns

| Status | Meaning |
|--------|---------|
| ✅ PASSED | Case executed and matched baseline (latency, tool sequence, response text) |
| ❌ FAILED | Case produced an error or assertion failure |
| ⚠️ REGRESSED | Case was PASSED in baseline but is now FAILED (Type A regression) |
| 📈 LATENCY REGRESSED | p99 latency increased >20% vs baseline (Type B regression) |
| 🔄 DIVERGED | Tool sequence or response text diverged from baseline (Type C regression) |
| ⏭️ SKIPPED | Case was skipped (e.g., RUNTIME_ID not set for Tier 3) |

### Regression Types

**Type A: Passed → Failed**
- A case that was passing in the baseline is now failing.
- Indicates a breaking change in a tool, router, or mock fixture.
- **Action**: Review the error message in the report and the recent code changes. Check if the test YAML is still compatible with the current tool signatures.

**Type B: Latency +20%**
- Tier-level p99 latency increased by more than 20%.
- Indicates a performance regression (slower tool, network issue, or algorithm change).
- **Action**: Profile the slowest tool or cases. Check for new AWS calls, loops, or mocks that are slower than before.

**Type C: Tool Sequence / Response Divergence**
- The tool sequence or response text changed from the baseline (but the case still passed).
- Indicates a change in tool routing, tool parameters, or LLM output.
- **Action**: Decide if the divergence is intentional (and update baselines) or a bug (and revert).

## Updating Baselines

Baselines are committed reference outputs that the reporter compares against. Update them when you intentionally change tool behavior or routing.

**Safe way to update**:

```bash
# Run the eval pipeline and inspect the results
make eval-tier1
make eval-tier2

# Check the summary — if all cases look correct, update the baselines
make eval-update-baseline

# Review the Git diff carefully
git diff evals/baselines/

# Commit
git add evals/baselines/
git commit -m "Update eval baselines for new routing rules"
```

**Why review the diff?**: The baseline JSON is human-readable. Before committing, scan for:
- Any unexpected case IDs (typo in the YAML `id` field?).
- Latency values that seem wrong (>1000ms for a Tier 1 tool?).
- Tool names that don't match production canonic names (should be `lookup_knowledge`, not `knowledge`).

**For Tier 3**: If your first nightly run populates the stub baseline, the diff will be large. Expected. Review the p99 latencies to spot obvious outliers.

## Triaging Failures

When a test fails, follow this decision tree:

### Tier 1 Failure

```
Error: "TypeError: make_triage_tool() missing required argument 'session_ctx'"

↓

Is the tool signature in YAML correct?
├─ YES: Check evals/runner/mocks.py to see what SessionContext fields the mock provides.
│       Update the YAML's input_args to match.
└─ NO: Fix the YAML. Verify with `python3 -c "from evals.runner.schema import EvalCase; ..."`

Error: "AttributeError: MockCMSClient has no attribute 'get_dtc_severity'"

↓

The mock is missing a method the tool expects.
→ Edit evals/runner/mocks.py and add the method.
  (Or adjust the YAML to only invoke methods the mock supports.)

Error: "FAILED — latency 1245ms > budget 1000ms"

↓

Tool is too slow.
→ Profile the tool. Check for N+1 queries, slow mock fixtures, or new AWS calls.
→ If intentional slowdown, update the YAML's latency_budget_ms.
```

### Tier 2 Failure

```
Error: "AssertionError: tool_calls mismatch. Expected ['lookup_knowledge'], got []"

↓

Tool is not being invoked. Why?
├─ YAML tool name is wrong? 
│  → Must use canonical Strands @tool name (e.g., lookup_knowledge), not kit key (knowledge).
├─ Input doesn't trigger the tool?
│  → Review the agent's routing logic in bidi_app_canonical.py.
│  → Try a different utterance or input_args.
└─ Mock is broken?
   → Check evals/conftest.py and evals/runner/mocks.py.

Error: "AssertionError: routing mismatch. Expected safety_critical=true, got false"

↓

Agent did not route to escalation for a safety-critical repair.
→ Check bidi_app_canonical.py voice_prompt.py for the routing rules.
→ Verify the expected.routing in the YAML matches the agent's decision logic.

Error: "AssertionError: response missing 'do not attempt'. Got 'you can safely attempt'"

↓

Safety check failed — agent is offering DIY for a safety-critical repair.
→ This is a production safety bug. Revert the code change immediately.
→ File an incident report. Update the YAML's must_not_contain to catch this in the future.
```

### Tier 3 Failure

```
Error: "RUNTIME_ID not configured — skipping"

↓

This is normal outside of nightly/pre-deploy runs.
→ To run Tier 3 locally, export RUNTIME_ID and AWS_REGION.

Error: "WebSocket connection refused"

↓

The deployed runtime is not reachable.
→ Check RUNTIME_ID is correct and the runtime is running.
→ Verify AWS credentials are configured (OIDC or IAM role).
→ Check security groups / network ACLs allow your IP to the runtime.

Error: "Audio transcript truncated — got 'check your' instead of full phrase"

↓

The WAV file is too short or has insufficient trailing silence.
→ Tier 3 WAVs must be 1.5–4 seconds with ≥1600ms trailing silence.
→ Check evals/audio/*.wav files. If the file is correct, the issue may be Nova's STT.
→ Consider using a different demo audio or a text-input case instead.

Error: "Tool sequence diverged from baseline"

↓

E2E behavior changed. Decide if intentional or bug.
→ If intentional, review the diff in the reporter output.
→ If accidental, revert the code change.
→ Update the baseline only after verification.
```

## Marking a Case as Flaky

If a Tier 3 case is genuinely intermittent (e.g., due to the documented Nova Sonic v2 TTS hallucination bug — see `HANDOFF.md`), you can mark it flaky in the baseline so its single-run failures are not treated as regressions.

**Mechanism**: edit the case's entry in `evals/baselines/tier{2,3}.json` and add `"flaky": true`:

```json
{
  "id": "tire-pressure-low-001",
  "yaml_path": "evals/cases/routing/tire-pressure-low-001.yaml",
  "outcome": "passed",
  "flaky": true,
  "...": "..."
}
```

The reporter checks for `flaky` at the case level. When a case is marked flaky, a single failure in a new run does NOT count as a Type A regression (`passed` → `failed`). Latency regressions (Type B) and tool sequence divergence (Type C) are still flagged. Verified by `test_flaky_case_suppresses_regression_a` in `evals/runner/test_reporter.py`.

**`--flaky-window N` flag (forward-work placeholder)**: the reporter accepts a `--flaky-window 7` flag for future history-window-based flakiness tracking. The flag is currently a stub — flakiness is determined by the explicit `flaky: true` baseline marker only. Building real history tracking (per-case pass-rate over N runs) is forward-work.

**Recommendation**: prefer investigating and fixing intermittent failures over marking them flaky. Use `flaky: true` only for failures with a documented external root cause (e.g., upstream AWS bug).

## Adding a New Case

See `evals/README.md` for a complete worked example. Quick summary:

1. Copy an existing YAML from the target subdirectory (`evals/cases/tools/`, `evals/cases/routing/`, or `evals/cases/e2e/`).
2. Update `id`, `description`, `input`, and `expected` fields.
3. Use the **canonical Strands @tool name** (not the kit key) in `tool_name` fields.
4. For multi-parameter tools, use `input_args` (not `utterance`).
5. Validate with `python3 -c "from evals.runner.schema import EvalCase; ..."`.
6. Run the test: `pytest evals/runner/tier{1,2,3}_tools.py::test_*[your-case-id]`.
7. If passing, commit and the baseline will be updated on the next run.

## Adding a New Tool

When implementing a new tool:

1. Implement the tool in `agents/supervisor/tools/<name>.py` with the `@tool` decorator.
2. Register it in `evals/conftest.py` → `tool_factory_kit` (add a mock + fixture binding).
3. Add Tier 1 cases under `evals/cases/tools/<name>/` (at least 3: happy path, error path, edge case).
4. Add Tier 2 cases under `evals/cases/routing/` or `evals/cases/safety/` that exercise the new tool.
5. If E2E (Tier 3) behavior is desired, add cases under `evals/cases/e2e/`.
6. Run locally: `make eval-all` (or `make eval-tier1 eval-tier2` if Tier 3 is not available).
7. Update baselines once all cases pass: `make eval-update-baseline`.

**Key**: The `@tool` decorator's name is the canonical name used everywhere (YAML, baselines, agent dispatch). The `tool_factory_kit` fixture key is an eval-side identifier for fixture wiring — don't confuse the two. (See `decisions.md` Fix Cycle 4.)

## Known Limitations & Forward Work

**From Security Review Cycle 5 Suggestions**:

1. **Markdown case ID escaping**: Case IDs in the reporter output are not HTML-escaped. Today this is safe because case IDs are project-controlled and follow kebab-case. If external contributors are permitted to submit case YAMLs, add a regex constraint at the schema layer: `id: str = Field(..., pattern="^[a-z0-9][a-z0-9-]*$")`.

2. **Pytest output parsing**: The reporter uses substring matching for ` PASSED`, ` FAILED`, ` SKIPPED`. If pytest changes its output format, the parser could silently fail to categorize results. Add explicit format validation (e.g., parse structured JSON output from pytest instead of text scraping).

3. **Tool parameter allowlisting**: `args_contain` keys in Tier 2 YAMLs are unchecked — any kwarg is propagated to the tool function. Today this is safe because YAMLs are reviewed via PR. For external-contributor case flows, add per-tool Pydantic kwarg allowlists in `evals/runner/schema.py`.

4. **Case-load lazy loading**: `tier2_conversation.py` loads all 18 YAMLs at module-import time (`_CASES = _load_cases()`). If a malformed YAML is committed, `pytest --collect-only` will fail. Consider deferring case-load to inside the test function (lazy fixture) to isolate failures.

5. **Tier 3 latency budget placeholder**: Currently `latency_budget_ms: 0.0` is accepted (bypasses latency checks). Tighten the schema to require non-zero or null; distinguish "no latency check" (null) from "unlimited latency" (0).

6. **Audio cases pending**: iOS WAV files are colocated in the repository for now. If audio assets move to S3 or a CDN, update the `audio_path` loader in `tier3_e2e.py` to fetch remote files.

## CI/CD Integration

The eval pipeline runs automatically in GitHub Actions:

- **On PR**: Runs `make eval-tier1 eval-tier2` (no AWS credentials needed).
- **On push to main**: Runs `make eval-all` (including Tier 3 if `RUNTIME_ID` is configured).
- **Nightly cron** (02:00 UTC): Runs `make eval-tier3` against the dev runtime (requires `RUNTIME_ID` + `AWS_ROLE_ARN` secrets).

Workflow file: `.github/workflows/evals.yml` (all actions SHA-pinned per `dependency-versions.md`).

Exit code 0 = no regressions. Exit code 1 = regressions detected (fail the CI job). Exit code 2 = argument/file error (fail the CI job).

## Copy-Paste Commands

All commands below are runnable from the project root:

```bash
# Run Tier 1 only
.venv/bin/pytest evals/runner/tier1_tools.py -v

# Run a specific Tier 1 case
.venv/bin/pytest evals/runner/tier1_tools.py::test_tool_unit[knowledge-tsb-lookup-001] -v

# Run Tier 2 with detailed output
.venv/bin/pytest evals/runner/tier2_conversation.py -v --tb=short

# Run Tier 3 (requires RUNTIME_ID)
AWS_REGION=us-east-1 RUNTIME_ID=<id> .venv/bin/pytest evals/runner/tier3_e2e.py -v

# Validate a new YAML case
python3 -c "import yaml; from evals.runner.schema import EvalCase; EvalCase.model_validate(yaml.safe_load(open('evals/cases/tools/knowledge/my-new-case.yaml')))"

# Update baselines after confirming all tests pass
make eval-update-baseline

# Dry-run a Makefile target to see the command
make -n eval-tier1

# Run the reporter manually
python3 -m evals.runner.reporter --tier 1 --results /tmp/eval-tier1.json --baseline evals/baselines/tier1.json

# Run the reporter with flakiness tracking (forward-work)
python3 -m evals.runner.reporter --tier 2 --results /tmp/eval-tier2.json --baseline evals/baselines/tier2.json --flaky-window 7
```

## Questions?

- **Case YAML structure**: See `evals/runner/schema.py` and `evals/README.md` worked example.
- **Tool mocking**: See `evals/runner/mocks.py` and `evals/conftest.py`.
- **Test structure**: See `evals/runner/tier1_tools.py`, `tier2_conversation.py`, `tier3_e2e.py`.
- **Baseline regeneration**: See `evals/baselines/README.md`.
- **Implementation decisions & lessons learned**: See `.kiro/specs/2026-05-24-eval-pipeline/decisions.md`.
