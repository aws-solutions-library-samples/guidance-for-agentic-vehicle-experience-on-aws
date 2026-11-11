# Evals

Automated regression detection for the Agentic Vehicle Experience (AVX) agent. The pipeline is tiered by execution speed: Tier 1 (tool unit tests, <30s, no AWS) → Tier 2 (mocked-Nova conversation tests, <2min, no AWS) → Tier 3 (real AgentCore end-to-end, ~10min, requires AWS credentials and a deployed runtime).

## cases/

Golden test cases in YAML format, validated against the `EvalCase` Pydantic schema in `runner/schema.py`. Cases are grouped by concern: `routing/` covers tool-selection and persona-routing scenarios (e.g., tire pressure, DTC codes, recall lookups); `safety/` covers adversarial inputs where the agent must refuse DIY guidance for safety-critical repairs (brakes, airbags, SRS); `knowledge/` covers Bedrock Knowledge Base grounding assertions (TSBs, recalls, DTC explanations); `tools/` contains Tier 1 unit cases for each individual tool factory, one subdirectory per tool; `e2e/` contains Tier 3 end-to-end cases that stream audio or text to a deployed AgentCore runtime. To add a new case, copy an existing YAML from the relevant subdirectory, update the fields, and validate it with `python3 -c "import yaml; from evals.runner.schema import EvalCase; EvalCase.model_validate(yaml.safe_load(open('your-case.yaml')))"`.

## audio/

WAV files referenced by Tier 3 audio-input cases. Files are copied from `clients/ios/VSACompanion/DemoClips/` — do not regenerate them via scripts, as the demo clips encode the exact trailing silence (≥1600ms) required by Nova Sonic v2's end-of-turn detection. Each WAV should be a single sentence, 1.5–4 seconds of speech, with no internal periods (Nova VAD splits on periods). If a Tier 3 case needs a phrase that has no matching WAV, use `type: text` instead.

## baselines/

Committed reference outputs (`tier1.json`, `tier2.json`, `tier3.json`) used by the regression detector in `runner/reporter.py`. A run is flagged as a regression if any baseline-passing case now fails, if p99 latency increases by more than 20%, or if the tool call sequence diverges from the baseline. Update baselines intentionally after a confirmed behavior change using `make eval-update-baseline`. The Tier 3 baseline may be a stub (`{"cases": [], "note": "populated on first nightly run"}`) until a deployed runtime is available.

## runner/

Python package containing the eval engine. `schema.py` defines the `EvalCase` Pydantic v2 model. `mocks.py` provides mock clients for CMS, ADP, KB, and Connect so Tier 1 and Tier 2 run without AWS credentials. `mock_bidi.py` is a drop-in replacement for `BidiNovaSonicModel` that replays scripted event sequences deterministically. `tier1_tools.py`, `tier2_conversation.py`, and `tier3_e2e.py` are the pytest-based runners for each tier. `reporter.py` consumes per-tier JSON results, writes a Markdown summary table, and exits non-zero on regression.

## Tier Definitions

### Tier 1 — Tool Unit Evals
**Runner**: `evals/runner/tier1_tools.py`  
**Cases**: `evals/cases/tools/**/*.yaml` (24 cases)

Isolated unit tests for each tool factory with mocked external services (CMS, ADP, KB, Connect). No LLM calls. Tests tool behavior, output format, and latency (must complete within 1000ms for Nova compatibility). Zero cost, runs in <2s.

**Who runs it**: Every PR.

### Tier 2 — Mocked-Nova Conversation Evals
**Runner**: `evals/runner/tier2_conversation.py`  
**Cases**: `evals/cases/{routing,safety,knowledge}/**/*.yaml` (18 cases)

Scripted user inputs replayed against an in-process `BidiAgent` with `MockBidiModel`. Real tool factories + mocked external services. Verifies tool sequencing, routing decisions, and safety guarantees (e.g., never offer DIY for brakes/airbags). Zero cost, runs in <2s.

**Who runs it**: Every PR.

### Tier 3 — Real AgentCore E2E Evals
**Runner**: `evals/runner/tier3_e2e.py`  
**Cases**: `evals/cases/e2e/**/*.yaml` (5 cases)

WebSocket client streams demo WAVs or text to a deployed AgentCore runtime in dev account. Real Nova Sonic, real KB, real CMS/ADP. Validates end-to-end tool sequence, transcript, and latency. Requires `RUNTIME_ID` env var. Cost ~$1/run, runs in ~10min.

**Who runs it**: Nightly cron + pre-deploy validation.

## Adding a New Tier 1 Case

This example adds a new test case for the `knowledge` tool.

**1. Create a YAML file** in `evals/cases/tools/knowledge/`:

```yaml
id: knowledge-cache-hit-002
description: "Verify cached KB lookups return quickly (p99 latency <1000ms)"
tier: 1
persona: fleet-driver
vehicle_id: VEH-0025
driver_id: drv-samantha-carter
input:
  type: text
  input_args:
    query: "what is TSB 2024-EN-0298"
expected:
  tool_output:
    found: true
    passages:
      - "TSB 2024-EN-0298: Turbocharger underboost condition..."
    answer: "This TSB addresses turbocharger wastegate actuator failures..."
latency_budget_ms: 1000
```

**2. Key points**:

- Use `input_args` (not `utterance`) for multi-parameter tools. These are kwargs passed directly to the tool function.
- Tool name in the YAML is the **production canonical name** (e.g., `lookup_knowledge`), not the `tool_factory_kit` key (e.g., `knowledge`). This is the name the agent registry uses to dispatch the tool. (See `decisions.md` Fix Cycle 4 for the distinction.)
- `expected.tool_output` must match what the mock fixture actually returns. Check `evals/runner/mocks.py` to see what `MockKBClient.retrieve()` returns for your query.
- Default `latency_budget_ms` is 1000ms (Nova tool-completion budget). Override if needed.

**3. Validate**:

```bash
python3 -c "import yaml; from evals.runner.schema import EvalCase; EvalCase.model_validate(yaml.safe_load(open('evals/cases/tools/knowledge/knowledge-cache-hit-002.yaml')))"
```

**4. Run the test**:

```bash
pytest evals/runner/tier1_tools.py::test_tool_unit[knowledge-cache-hit-002] -v
```

## Common Pitfalls

**Tool name mismatch**: YAML `tool_name` must be the Strands `@tool` registered name (e.g., `lookup_knowledge`), NOT the eval-side `tool_factory_kit` key (e.g., `knowledge`). Production tools use canonical names; eval kit keys are eval-side identifiers. (See `decisions.md` Fix Cycle 4.)

**Mock patch collision**: When mocking third-party modules that multiple tools import (e.g., `requests`), patch the SOURCE module (`requests.post`), not the importing modules' references (`agents.supervisor.tools.triage.requests.post`). Both imports bind the same underlying module object; patches on separate references collide. (See `decisions.md` Fix Cycle 3.)

**Latency budgets**: Tier 1 default is 1000ms (Nova compatibility); Tier 2 default is 5000ms; Tier 3 default is 10000ms. Override per-case in the YAML with `latency_budget_ms`.

**Audio path traversal**: `audio_path` values in Tier 3 cases must be relative paths inside `evals/audio/`. A path-traversal validator enforces `Path(audio_path).resolve().is_relative_to(Path('evals/audio').resolve())` before opening files.

## Migration Notes

See `evals/MIGRATION.md` for an inventory of existing tests (`agents/supervisor/tests/test_*.py`, `lambdas/*/tests/`) and their disposition (KEEP, FOLD-INTO-EVALS, or DEPRECATE). The eval framework is an *additive* build; existing tests remain in place and unchanged. Migration to the eval framework is a follow-up spec.

## Baseline Regeneration

For baseline management and when to regenerate, see `evals/baselines/README.md`.
