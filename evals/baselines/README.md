# Eval Baselines

Committed reference outputs used by the regression detector (`evals/runner/reporter.py`).
A baseline captures the expected pass/fail state and latency profile of each eval tier at a known-good point in time.
The reporter compares a fresh run against the baseline and exits non-zero if any regression is detected.

## Files

| File | Tier | Cases | Notes |
|------|------|-------|-------|
| `tier1.json` | Tool unit evals | 24 | Generated from `evals/cases/tools/**/*.yaml` |
| `tier2.json` | Conversation evals | 18 | Generated from `evals/cases/{routing,safety,knowledge}/*.yaml` |
| `tier3.json` | End-to-end evals | 0 (stub) | Populated on first nightly run when `RUNTIME_ID` is configured |

## When to regenerate

Regenerate a baseline **only after an intentional, reviewed change** — not to silence a failing test.

```bash
# After a deliberate change to Tier 1 tool behaviour:
make eval-update-baseline TIER=1

# After a deliberate change to Tier 2 routing/safety/knowledge cases:
make eval-update-baseline TIER=2

# After a successful Tier 3 nightly run against a new runtime:
make eval-update-baseline TIER=3
```

`make eval-update-baseline` invokes `reporter.py --update-baseline`, which rewrites the relevant
`evals/baselines/tierN.json` and exits 0. Commit the updated file with a message explaining why
the baseline changed (e.g., `evals: update tier1 baseline after adding parts_lookup edge case`).

## Why tier3.json is a stub

Tier 3 requires a deployed AgentCore runtime (`RUNTIME_ID` env var) and live AWS credentials.
It runs nightly in CI, not on every PR. The stub is committed so the reporter can load all three
files without error; the first successful nightly run will overwrite it via `--update-baseline`.

## JSON schema

Each file follows this structure (matches `evals/runner/reporter.py`):

```json
{
  "tier": 1,
  "schema_version": 1,
  "generated_at": "2026-05-25T19:13:15Z",
  "cases": [
    {
      "id": "triage-happy-001",
      "yaml_path": "evals/cases/tools/triage/triage-happy-001.yaml",
      "outcome": "passed",
      "latency_ms": 85.0,
      "expected_tool_calls": ["triage"],
      "actual_tool_calls": ["triage"],
      "failure_reason": null
    }
  ],
  "summary": {
    "total": 24,
    "passed": 24,
    "failed": 0,
    "skipped": 0,
    "latency_p50_ms": 85.0,
    "latency_p99_ms": 85.0,
    "duration_ms": 2040.0
  }
}
```

`outcome` is one of `"passed"`, `"failed"`, or `"skipped"`.
`failure_reason` is `null` on pass, a string on fail.
`expected_tool_calls` / `actual_tool_calls` are lists of tool name strings (single-element for Tier 1 unit tests; multi-element for Tier 2 conversation sequences).

## Regression definitions (enforced by reporter)

1. Any case that passed in the baseline now fails.
2. p99 latency increased by >20% vs. baseline.
3. Tool call sequence diverges from baseline for any case.
