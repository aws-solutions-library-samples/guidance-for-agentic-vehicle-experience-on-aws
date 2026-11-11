"""
agents/fi_cost — AVX Tier 2 cost anomaly component.

Spec: .kiro/specs/2026-10-02-fi-cost-stage-b/spec.md
Stage B: daily cost-anomaly Finding writer, one Bedrock call per anomaly.

Modules (implemented in Group 3):
    selection   — Athena query against vehicle_cost_baseline; paged NextToken
    evidence    — second Athena query; evidence bundle construction
    savings     — D7 formula: estimated_value_usd per action kind
    diagnose    — Bedrock Converse call; record_diagnosis tool
    validate    — tool-input validator; cause-consistency check
    fallback    — deterministic rules fallback (D6 step 5)
    sell_timing — crossover arithmetic ported from CMS lifecycle.py (D10)
    agent       — component runner; plugs into agents/tier2/runner.py
"""
