"""Tests for the VSA supervisor.

Two test modules:
  - test_tools_unit.py: fast unit tests for each tool, no LLM, no network
    (except triage which can be opted in via VSA_TEST_HIT_API=1).
  - test_scenarios.py: end-to-end scenarios through Claude Sonnet 4.5.
    Slow (~15s per scenario) and costs Bedrock tokens; opt-in via
    VSA_TEST_E2E=1.
"""
