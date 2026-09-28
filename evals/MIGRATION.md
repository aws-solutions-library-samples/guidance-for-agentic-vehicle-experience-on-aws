# Existing Tests & Scripts Inventory

This file inventories the existing test files and smoke scripts in the AVX project. The new evaluation pipeline at `evals/` is being built additively to provide tiered regression detection without requiring migration of these existing tests. Migration of existing test files to the eval framework is out of scope for this spec and will be addressed in a follow-up phase. The existing test infrastructure remains in place and operational during the eval pipeline ramp-up.

## Inventory

| File | Description | Disposition | Notes |
|------|-------------|-------------|-------|
| `agents/supervisor/tests/test_tools_unit.py` | Unit tests for tool factories (retrieve, book, triage); validates STAR envelope format and state transitions without Claude. | KEEP | Existing unit test layer; complements Tier 1 eval coverage. |
| `agents/supervisor/tests/test_bidi_app.py` | Tests the bidi entrypoint auth, session extraction, and header validation; no real BidiAgent or WebSocket. | KEEP | Covers entrypoint validation; separate from eval workflow. |
| `agents/supervisor/tests/test_scenarios.py` | End-to-end scenario tests (P3 tire, P1 coolant, P0 brake) against Claude Sonnet; slow, token-heavy. | KEEP | Opt-in integration tests; separate from eval pipeline. |
| `agents/supervisor/tests/test_agentcore_app.py` | Tests AgentCore @app.entrypoint function with mocked RequestContext; validates headers and payloads. | KEEP | Covers AgentCore surface; predates eval framework. |
| `agents/supervisor/tests/test_injection_queue.py` | Tests AgentInjectionQueue deferral logic, timeouts, and race conditions under grace windows. | KEEP | Async queue tests; orthogonal to eval tiers. |
| `lambdas/shared/tests/test_cms_client_real.py` | Unit tests for RealCmsClient protocol compliance, schema handling, unit conversion, and catalog lookup with fakeredis. | KEEP | Covers lambda shared layer; no AWS calls. |
| `lambdas/triage-classifier/tests/test_classifier.py` | Unit + replay tests for the VSA triage classifier; deterministic classification logic. | KEEP | Classifier tests; separate from agent eval scope. |
| `scripts/smoke-test-agentcore.py` | Smoke test for deployed AgentCore supervisor; invokes with real JWT, asserts tool sequence and response format. | KEEP | Pre-deploy validation; complements Tier 3 e2e evals. |
| `scripts/smoke-test-voice.py` | Smoke test for deployed bidi WebSocket runtime; streams audio, captures transcript and classification. | KEEP | Operator runbook; separate from automated evals. |
| `scripts/smoke-test-voice-multiturn.py` | Multi-turn smoke test (tire query + yes response); demonstrates full conversation flow and audio reliability. | KEEP | Operator runbook; replicates iOS demo flow. |
| `scripts/test-diy-advisor.py` | Interactive tester for diy_repair_advisor sub-agent; sends text queries and captures narration. | KEEP | Dev iteration tool; not part of CI pipeline. |
| `scripts/test-wav-clip.py` | One-off tester for arbitrary WAV streaming; useful for custom Polly phrases and sub-agent validation. | KEEP | Dev iteration tool; not part of CI pipeline. |

## Rationale

All existing files are marked **KEEP** because:

1. **They serve distinct purposes** — unit testing internal components (tools, queues, classifiers), integration testing against live services (scenarios), and operator smoke testing (deployments).
2. **The eval pipeline is additive** — Tier 1 evals will provide parameterized golden-case coverage of tools, and Tier 2 evals will provide mocked conversation workflows. Existing tests remain as-is.
3. **Migration is deferred** — consolidating or refactoring these tests into the eval framework is a separate initiative that does not block the current eval pipeline spec.

No files are marked for deprecation or folding into evals at this time.
