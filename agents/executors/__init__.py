"""AVX Action executors — the dispatch layer behind `POST /findings/{id}/approve`.

Spec: `.kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/spec.md`
      § "Executor dispatch abstraction"

This package is deliberately NOT importable from any Tier 2 agent module.
Tier 2 agents *write Findings*; they never dispatch Actions. The boundary is
test-enforced by `agents/executors/tests/test_registry.py` rather than merely
stated here, because a boundary that exists only in a docstring will be
crossed (see `~/.kiro/steering/agentic-tiers.md` § "What stays deterministic").

Import `EXECUTORS` from `agents.executors.registry` to dispatch.
"""
